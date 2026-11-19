#!/usr/bin/env python3
"""Buddy's VIDEO: emails -> a 5-second LTX-2.5 clip from cumulus2, replied by the box (S660).

Buddy's ask (relayed by the S659 session, 2026-11-18): "if I sent an email to
Cumulus inbox, requesting a short video to be created and it could return an
link or .mp file back to my email address?"

- Narrow on purpose: only Buddy's allowlisted address, only a subject that
  starts with "VIDEO:" (not a reply), only on a host with
  config/video-intake.json. No client can trigger it (new client-facing
  behaviour, plus the LTX license's real-likeness rules).
- Intake (the one shared poller) calls route() and start_job(); start_job()
  launches `video_intake.py worker RUN` detached, so the poll returns at once.
- The worker submits the body as the prompt to the LTX page on cumulus2
  (/api/submit over the cumulus1-cumulus2 link). The page's one-at-a-time
  queue, shared render lock and ltx25_shots.sh memory guard all apply, as does
  the page's lab-window setting.
- When the clip is done: download it, run a local sound check (Whisper on
  this box: are the quoted words spoken?), reply to Buddy only, from this box,
  signed by the box. The mp4 is attached up to 20 MB, else linked. The reply
  says plainly what was and was not checked: lip sync is never machine-checked.
- One reply per request, ever: result.json is written before any retry could
  send again. Addresses never go to disk or logs (the reply address is looked up
  from intake_senders.json by index at send time).
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG = PROJECT_DIR / "config/video-intake.json"   # presence enables the route on this host
SENDERS = PROJECT_DIR / "config/intake_senders.json"
CREDS = PROJECT_DIR / "config/credentials.json"
RUNS = PROJECT_DIR / "logs/video_jobs"
PAGE = "http://192.168.100.11:8765"                 # ltx25_web.py, cumulus2's end of the link
SIZE = "1280x704"
MAX_ATTACH = 20 * 1024 * 1024
MAX_PER_DAY = 10
WAIT_HOURS = 26                                      # a "wait for the window" clip can sit until 01:30
SUBJECT_RE = re.compile(r"^\s*video\s*:\s*(.*)$", re.I | re.S)
ADDR_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
WHISPER = Path.home() / ".cache/whisper/large-v3-turbo.pt"
BUSY_ET = (14 * 60 + 20, 15 * 60)                    # holdings run on this box (local_stt_mt.py)


class DailyCap(Exception):
    pass


def hide(text):
    return ADDR_RE.sub("<address>", str(text))


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {hide(msg)}", flush=True)


# ── intake side ──────────────────────────────────────────────────────────────

def route(rec, subject, body):
    """Mark Buddy's own VIDEO: request. Requester comes from the allowlist, never the body."""
    if not CONFIG.exists() or rec.get("requester", "").lower() != "buddy":
        return False
    if rec.get("status") == "refused" or rec.get("kind") in ("resend", "confirmation"):
        return False
    if not SUBJECT_RE.match(subject or ""):
        return False
    rec.update(kind="video", tier=0, tier_name="Tier 0 (auto)",
               tier_reason="Owner VIDEO: request; rendered on cumulus2 and replied with the clip.",
               privacy="LOCAL_ONLY")
    if "dev_spec" in rec:
        rec["dev_spec"].update(tier=0, tier_name=rec["tier_name"], tier_reason=rec["tier_reason"])
    return True


def prompt_of(subject, body):
    """The body is the prompt (intake may hand it over collapsed to one line)."""
    text = (body or "").replace("\r", "")
    text = re.split(r"(?:^|\s)(?:--\s|Sent from my |Get Outlook for |On [^\n]{4,80} wrote:)", text, maxsplit=1)[0]
    text = "\n".join(l for l in text.split("\n") if not l.lstrip().startswith(">")).strip()
    if not text:
        m = SUBJECT_RE.match(subject or "")
        text = m.group(1).strip() if m else ""
    return text[:3000]


def _buddy_emails():
    raw = json.loads(SENDERS.read_text())
    entry = next((v for k, v in raw.items() if k.lower() == "buddy" and isinstance(v, dict)), {})
    return [str(e).strip().lower() for e in entry.get("emails", [])]


def load_now():
    """cumulus2's load as its page sees it, for the first reply (Buddy, 2026-11-18: say whether the
    clip runs now or is delayed by other work). {} when the page does not answer."""
    try:
        got = page("GET", "/api/load", timeout=8)
        return {"free": bool(got.get("free")), "reason": str(got.get("reason") or "")[:200],
                "queued": int(got.get("queued") or 0)}
    except Exception:
        return {}


def start_job(rec, subject, body, popen=None, now=None):
    """One job per original Message-ID; returns the run id and sets rec["video_load"]. Raises DailyCap."""
    import fcntl
    mid = rec.get("message_id") or ""
    if not mid:
        raise ValueError("Message-ID required")
    prompt = prompt_of(subject, body)
    if not prompt:
        raise ValueError("empty video description")
    idx = _buddy_emails().index((rec.get("from_email") or "").strip().lower())  # ValueError if not Buddy's
    RUNS.mkdir(parents=True, exist_ok=True)
    with (RUNS / ".launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for meta in RUNS.glob("v-*/meta.json"):
            if json.loads(meta.read_text()).get("message_id") == mid:
                return meta.parent.name
        now = now or datetime.now()
        if len(list(RUNS.glob(f"v-{now:%Y%m%d}-*"))) >= MAX_PER_DAY:
            raise DailyCap(f"already {MAX_PER_DAY} video requests today")
        run_id = f"v-{now:%Y%m%d-%H%M%S}-{hashlib.sha256(mid.encode()).hexdigest()[:6]}"
        d = RUNS / run_id
        d.mkdir()
        (d / "prompt.txt").write_text(prompt + "\n")
        (d / "meta.json").write_text(json.dumps({
            "message_id": mid, "subject": subject, "reply_to_index": idx,
            "requested": now.isoformat(timespec="seconds")}, indent=1))
        rec["video_load"] = load_now()  # before the launch, so our own clip is not counted as ahead
        _launch(run_id, popen)
        return run_id


MAX_LAUNCHES = 5


def _launch(run_id, popen=None):
    d = RUNS / run_id
    meta = json.loads((d / "meta.json").read_text())
    meta["launches"] = meta.get("launches", 0) + 1
    (d / "meta.json").write_text(json.dumps(meta, indent=1))
    with open(d / "worker.log", "a") as out:
        p = (popen or subprocess.Popen)(
            [sys.executable, "-u", str(PROJECT_DIR / "video_intake.py"), "worker", run_id],
            cwd=str(PROJECT_DIR), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True)
    (d / "worker.pid").write_text(str(getattr(p, "pid", 0)))


def _alive(pid):
    try:
        os.kill(pid, 0)
        return pid > 0
    except (OSError, ValueError):
        return False


def resume(popen=None, alive=_alive):
    """Each intake pass: restart a job whose worker died (an intake restart kills it)
    before it finished. The worker carries on from its saved page id; a job that
    already wrote result.json is never touched, so nothing is sent twice."""
    restarted = []
    for d in sorted(RUNS.glob("v-*")) if RUNS.exists() else []:
        if (d / "result.json").exists() or not (d / "meta.json").exists():
            continue
        pid = (d / "worker.pid").read_text().strip() if (d / "worker.pid").exists() else "0"
        if alive(int(pid or 0)):
            continue
        meta = json.loads((d / "meta.json").read_text())
        if meta.get("launches", 0) >= MAX_LAUNCHES:
            (d / "result.json").write_text(json.dumps({
                "state": "gave_up", "sent": False,
                "detail": f"the worker stopped {MAX_LAUNCHES} times; no reply was sent"}, indent=1))
            log(f"{d.name}: gave up after {MAX_LAUNCHES} launches; no reply sent")
            continue
        _launch(d.name, popen)
        restarted.append(d.name)
        log(f"{d.name}: worker was not running; restarted it")
    return restarted


# ── worker side ──────────────────────────────────────────────────────────────

def page(method, path, body=None, timeout=30):
    req = urllib.request.Request(PAGE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def download(page_id, dest, expect):
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(f"{PAGE}/clip/{page_id}.mp4", timeout=120) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    if tmp.stat().st_size != expect or expect <= 0:
        raise ValueError(f"download size {tmp.stat().st_size} != {expect}")
    tmp.rename(dest)


def whisper_check(mp4):
    """(spoken lines, audio RMS) with the box's own Whisper; waits out the holdings run."""
    while True:
        t = datetime.now()
        if not BUSY_ET[0] <= t.hour * 60 + t.minute < BUSY_ET[1]:
            break
        time.sleep(60)
    import numpy as np
    import whisper
    audio = whisper.load_audio(str(mp4))
    rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
    result = whisper.load_model(str(WHISPER)).transcribe(audio)
    return [s["text"].strip() for s in result["segments"] if s["text"].strip()], rms


def words(s):
    return re.findall(r"[a-z0-9']+", s.lower())


def sound_check(mp4, prompt, transcribe=None):
    want = [q.strip() for q in re.findall(r'["“]([^"”]{2,400})["”]', prompt)]
    try:
        lines, rms = (transcribe or whisper_check)(mp4)
    except Exception as e:
        return {"ran": False, "why": type(e).__name__, "want": want}
    heard = " ".join(lines).strip()
    res = {"ran": True, "heard": heard, "want": want, "rms": round(rms, 4)}
    if want:
        exp, got = words(" ".join(want)), set(words(heard))
        res["recall"] = round(sum(w in got for w in exp) / max(len(exp), 1), 2)
        res["match"] = res["recall"] >= 0.8
    return res


def sound_lines(s):
    if not s.get("ran"):
        return ["- Sound was not checked: the check could not run on CUMULUS. "
                "Please watch it before sharing."]
    out = []
    bare = lambda t: t.strip().rstrip(".!?,;: ")
    if s["want"]:
        asked = " / ".join(f'"{bare(w)}"' for w in s["want"])
        heard = f'"{bare(s["heard"])}"' if s["heard"] else "no speech"
        verdict = ("They match." if s["match"]
                   else "They do not match, so watch it before using it.")
        out.append(f"- Spoken words: you asked for {asked}, and the transcript heard {heard}. {verdict}")
    elif s["heard"]:
        out.append(f'- You did not ask for spoken words, but the transcript heard "{bare(s["heard"])}". '
                   "(Whisper sometimes hears words in music or noise.)")
    else:
        out.append("- You did not ask for spoken words, and none were heard.")
    out.append("- The soundtrack is silent." if s["rms"] < 0.005 else "- The clip has sound.")
    out.append("- Lip sync, and whether each sound fits what is on screen, were not checked "
               "by machine. Please watch it before sharing.")
    return out


def reply_body(box, prompt, st, mp4_bytes, attached, link, sound, wall, run_id, waited=0):
    shown = prompt if len(prompt) <= 300 else prompt[:297] + "..."
    head = f"Hi Buddy,\n\n"
    if st["state"] != "done":
        why = ("cumulus2's memory guard refused to start it, to protect the other models on "
               "the box" if st["state"] == "refused" else "the render did not finish")
        return (head + f"Your video could not be made this time:\n\n    {shown}\n\n"
                f"Reason: {why} ({st.get('detail') or st['state']}).\n"
                "Please send it again later, or use the video page.\n\n"
                f"Request {run_id}.\n\n— {box}")
    mb = f"{mp4_bytes / 1048576:.1f} MB"
    if attached:
        where = f"It is attached: {st['width']}x{st['height']}, 5 seconds with sound, {mb}."
    else:
        where = (f"It is {mb}, too big to attach. Download it here:\n{link}\n"
                 "That page is private: it opens only for you, after you sign in.")
    took = ""
    if wall:
        took = f"It took {wall // 60} min {wall % 60} s from your email to the finished clip"
        took += (f", {waited // 60} min of it waiting for cumulus2 to be free of other work."
                 if waited >= 120 else ".")
    return (head + f"Here is your video from cumulus2:\n\n    {shown}\n\n{where}\n{took}\n\n"
            "Sound check (by machine, on CUMULUS):\n" + "\n".join(sound_lines(sound)) + "\n\n"
            "It is AI-generated. If you share it, label it as AI-generated (LTX-2 Community "
            "License), and never use it to imitate a real person.\n\n"
            f"Request {run_id}.\n\n— {box}")


def sent_mail_check(creds, run_id, since, tries=6):
    """The reply in this box's actual Sent Mail, found by its request id. None if not seen."""
    import imaplib
    import intake
    from runtime_config import load_sources
    account = intake.find_account(load_sources(intake.CONFIG_PATH))
    for _ in range(tries):
        with imaplib.IMAP4_SSL(account["imap_server"], account.get("imap_port", 993), timeout=20) as m:
            m.login(account["address"], creds[account["credential_key"]])
            if m.select('"[Gmail]/Sent Mail"', readonly=True)[0] == "OK":
                ok, ids = m.uid("search", None, "SINCE", since.strftime("%d-%b-%Y"),
                                "X-GM-RAW", f'"{run_id}"')
                uids = ids[0].split() if ok == "OK" and ids and ids[0] else []
                if len(uids) == 1:
                    return {"uid": uids[0].decode(), "checked": datetime.now().isoformat(timespec="seconds")}
        time.sleep(10)
    return None


def worker(run_id, http=page, fetch=download, send=None, transcribe=None, verify=None,
           lifecycle=None, sleep=time.sleep, clock=time.time, creds=None, emails=None):
    d = RUNS / run_id
    meta = json.loads((d / "meta.json").read_text())
    if (d / "result.json").exists():
        log(f"{run_id}: already finished; nothing to do")
        return json.loads((d / "result.json").read_text())
    prompt = (d / "prompt.txt").read_text().strip()
    started = datetime.fromisoformat(meta["requested"])
    if not meta.get("page_id"):
        meta["page_id"] = http("POST", "/api/submit", {"prompt": prompt, "size": SIZE, "mode": "now"})["id"]
        (d / "meta.json").write_text(json.dumps(meta, indent=1))
        log(f"{run_id}: submitted to cumulus2 as {meta['page_id']}")
    deadline, misses, ran_at = clock() + WAIT_HOURS * 3600, 0, None
    while True:
        try:
            st = http("GET", f"/api/job/{meta['page_id']}")
            misses = 0
        except (urllib.error.URLError, OSError, ValueError) as e:
            misses += 1
            st = {"state": "unreachable", "detail": type(e).__name__}
            if misses >= 30:
                st = {"state": "failed", "detail": "cumulus2's page stopped answering"}
                break
        if st["state"] == "running" and not ran_at:
            ran_at = datetime.now()
        if st["state"] in ("done", "refused", "failed", "interrupted"):
            break
        if clock() > deadline:
            st = {"state": "failed", "detail": f"no result after {WAIT_HOURS} hours"}
            break
        sleep(20)
    log(f"{run_id}: page state {st['state']}")
    mp4, attached, sound, link, size = d / f"ltx25-{meta.get('page_id')}-AI-generated.mp4", False, {}, "", 0
    if st["state"] == "done":
        try:
            fetch(meta["page_id"], mp4, st["bytes"])
            size = mp4.stat().st_size
            attached = size <= MAX_ATTACH
            sound = sound_check(mp4, prompt, transcribe)
            log(f"{run_id}: {size} bytes; sound check ran={sound.get('ran')} match={sound.get('match')}")
        except Exception as e:
            st = {"state": "failed", "detail": f"could not fetch the clip ({type(e).__name__})"}
    if st["state"] == "done" and not attached:
        cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
        link = f"{cfg.get('link_base', 'http://cumulus2:8765').rstrip('/')}/clip/{meta['page_id']}.mp4"
    creds = creds if creds is not None else json.loads(CREDS.read_text())
    emails = emails if emails is not None else _buddy_emails()
    to = emails[meta["reply_to_index"]]
    import mailer
    from_email = creds.get("outlook_email", "")
    box = mailer.sender_name(from_email, creds)
    wall = int((datetime.now() - started).total_seconds()) if st["state"] == "done" else 0
    waited = int((ran_at - started).total_seconds()) if ran_at and wall else 0
    body = reply_body(box, prompt, st, size, attached, link, sound, wall, run_id, waited)
    subject = meta["subject"]
    subject = subject if subject.lower().startswith("re:") else "Re: " + subject
    result = {"state": st["state"], "detail": st.get("detail", ""), "bytes": size,
              "attached": attached, "linked": bool(link), "sound": {k: v for k, v in sound.items()
                                                                 if k not in ("heard", "want")},
              "sent": None, "finished": datetime.now().isoformat(timespec="seconds")}
    (d / "result.json").write_text(json.dumps(dict(result, sent="pending"), indent=1))  # never twice
    ok = (send or mailer.send)(from_email, creds.get("outlook_password", ""), to, subject, body,
                               attachments=[str(mp4)] if attached else None, creds=creds,
                               on_error="false", log=log, watch_promises=False, auto_submitted=True)
    result["sent"] = bool(ok)
    if ok:
        try:
            result["sent_mail"] = (verify or sent_mail_check)(creds, run_id, started - timedelta(days=1))
        except Exception as e:
            result["sent_mail"] = None
            result["sent_mail_error"] = type(e).__name__
    (d / "result.json").write_text(json.dumps(result, indent=1))
    log(f"{run_id}: reply sent={result['sent']} sent_mail={'seen' if result.get('sent_mail') else 'not seen'}")
    try:
        import intake_lifecycle
        row = (None, to, subject, None, meta["message_id"])
        if ok and result.get("sent_mail"):
            (lifecycle or intake_lifecycle.update)(PROJECT_DIR, row, "sent_verified", "delivery-review",
                "video reply seen in actual Sent Mail",
                {"video_run": run_id, "sent_receipt": f"Sent Mail uid {result['sent_mail']['uid']}"})
        else:
            (lifecycle or intake_lifecycle.update)(PROJECT_DIR, row,
                "sent_unverified" if ok else "blocked_on_evidence", "cowork",
                "check the video reply in Sent Mail" if ok else "video reply failed to send; see result.json",
                {"video_run": run_id})
    except Exception as e:
        log(f"{run_id}: lifecycle update failed ({type(e).__name__}); the reply is unaffected")
    return result


# ── selftest (offline: temp dir, fake page/mail/whisper, no network, no live files) ──

def selftest():
    import tempfile
    global RUNS, CONFIG, SENDERS
    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    global load_now
    saved = RUNS, CONFIG, SENDERS, load_now
    load_now = lambda: {"free": False, "reason": "another video is rendering", "queued": 1}  # never the live page
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        RUNS, CONFIG, SENDERS = tmp / "runs", tmp / "video-intake.json", tmp / "senders.json"
        SENDERS.write_text(json.dumps({"Buddy": {"emails": ["owner@example.test"]},
                                       "bill": {"emails": ["client@example.test"]}}))
        rec = lambda who="buddy", **k: dict({"requester": who, "status": "queued", "kind": "build",
                                             "message_id": "<m1@x>", "from_email": "owner@example.test",
                                             "dev_spec": {}}, **k)
        r = rec()
        ok("route is off on a host without the config", not route(r, "VIDEO: a dog", "") and r["kind"] == "build")
        CONFIG.write_text("{}")
        ok("Buddy's VIDEO: subject routes to video", route(r, "VIDEO: a dog", "") and r["kind"] == "video")
        ok("a client's VIDEO: subject does not", not route(rec("bill"), "VIDEO: a dog", ""))
        ok("a reply (Re: VIDEO:) does not", not route(rec(), "Re: VIDEO: a dog", ""))
        ok("a held request stays held", not route(rec(status="refused"), "VIDEO: x", ""))
        ok("a plain subject does not", not route(rec(), "Make me a video", ""))
        ok("the body is the prompt, signature cut",
           prompt_of("VIDEO: x", "A cat on a sofa. Sent from my iPhone") == "A cat on a sofa.")
        ok("an empty body uses the subject", prompt_of("VIDEO: a red kite", "  ") == "a red kite")
        launched = []
        fake_popen = lambda args, **k: launched.append(args)
        run_id = start_job(r, "VIDEO: a dog", "A dog barks twice.", popen=fake_popen)
        again = start_job(r, "VIDEO: a dog", "A dog barks twice.", popen=fake_popen)
        ok("one job per Message-ID", run_id == again and len(launched) == 1)
        meta = json.loads((RUNS / run_id / "meta.json").read_text())
        ok("no address is written to disk (only its index)",
           "owner@example.test" not in (RUNS / run_id / "meta.json").read_text()
           and meta["reply_to_index"] == 0)
        try:
            start_job(rec(message_id="<m2@x>", from_email="client@example.test"), "VIDEO: x", "y", popen=fake_popen)
            ok("a non-Buddy address cannot start a job", False)
        except ValueError:
            ok("a non-Buddy address cannot start a job", True)

        def fake_page(states):
            seq = iter(states)
            return lambda m, path, body=None: ({"id": "20261118-120000-abcd"} if m == "POST" else next(seq))

        sent, lifecycle = [], []

        def fake_send(*a, **k):
            sent.append((a, k))
            return True

        def fetch(pid, dest, expect):  # relative_to() raises if a path outside the temp dir ever arrives (T32)
            (tmp / Path(dest).relative_to(tmp)).write_bytes(b"x" * expect)

        done = {"state": "done", "detail": "clip: exit 0", "width": 1280, "height": 704, "bytes": 2048}
        res = worker(run_id, http=fake_page([{"state": "queued"}, {"state": "running"}, done]),
                     fetch=fetch, send=fake_send, sleep=lambda s: None,
                     transcribe=lambda p: (["Woof woof."], 0.2), verify=lambda *a: {"uid": "7"},
                     lifecycle=lambda *a: lifecycle.append(a), creds={"outlook_email": "box@cumulustask.com"},
                     emails=["owner@example.test"])
        (a, k) = sent[0]
        ok("the reply goes to Buddy's address only", a[2] == "owner@example.test" and not k.get("cc"))
        ok("the clip is attached under 20 MB", res["attached"] and k["attachments"][0].endswith(".mp4"))
        ok("the subject is a reply to his", a[3] == "Re: VIDEO: a dog")
        ok("the reply is signed by the box, not Buddy", a[4].rstrip().endswith("— CUMULUS"))
        ok("the reply says lip sync was not checked", "were not checked by machine" in a[4])
        ok("the reply carries the AI-generated reminder", "AI-generated" in a[4])
        ok("auto-submitted, no promise tracking",
           k.get("auto_submitted") is True and k.get("watch_promises") is False)
        ok("Sent Mail proof marks the request sent_verified",
           lifecycle and lifecycle[0][2] == "sent_verified")
        ok("a finished job never sends twice",
           worker(run_id, http=None, send=fake_send) and len(sent) == 1)

        s = sound_check(Path("x"), 'A man says "the plows came through at six"',
                        transcribe=lambda p: (["The plows came through at six."], 0.1))
        ok("quoted words that were spoken match", s["match"])
        s = sound_check(Path("x"), 'A man says "the plows came through at six"',
                        transcribe=lambda p: (["Hello there."], 0.1))
        ok("quoted words that were not spoken do not match", s["match"] is False
           and any("do not match" in l for l in sound_lines(s)))
        s = sound_check(Path("x"), "a dog", transcribe=lambda p: (_ for _ in ()).throw(RuntimeError()))
        ok("a sound check that fails says so plainly",
           not s["ran"] and "not checked" in sound_lines(s)[0])

        r2 = rec(message_id="<m3@x>")
        route(r2, "VIDEO: big", "")
        big = start_job(r2, "VIDEO: big", "A long river.", popen=fake_popen)
        CONFIG.write_text(json.dumps({"link_base": "https://video.example.test"}))
        sent.clear()
        res = worker(big, http=fake_page([dict(done, bytes=MAX_ATTACH + 1)]), fetch=fetch, send=fake_send,
                     sleep=lambda s: None, transcribe=lambda p: ([], 0.2), verify=lambda *a: None,
                     lifecycle=lambda *a: None, creds={"outlook_email": "box@cumulustask.com"},
                     emails=["owner@example.test"])
        a, k = sent[0]
        ok("over 20 MB it links instead of attaching",
           not k["attachments"] and "https://video.example.test/clip/20261118-120000-abcd.mp4" in a[4]
           and "private" in a[4])
        r3 = rec(message_id="<m4@x>")
        route(r3, "VIDEO: x", "")
        refused = start_job(r3, "VIDEO: x", "A boat.", popen=fake_popen)
        sent.clear()
        res = worker(refused, http=fake_page([{"state": "refused", "detail": "REFUSED clip: only 30 GiB"}]),
                     send=fake_send, sleep=lambda s: None, lifecycle=lambda *a: None,
                     creds={"outlook_email": "box@cumulustask.com"}, emails=["owner@example.test"])
        ok("a refused render gets a plain reply with the guard's reason",
           res["state"] == "refused" and "memory guard" in sent[0][0][4] and not sent[0][1]["attachments"])
        ok("hide() removes addresses from log lines", hide("to owner@example.test ok") == "to <address> ok")
        r5 = rec(message_id="<m5@x>")
        route(r5, "VIDEO: y", "")
        orphan = start_job(r5, "VIDEO: y", "A lighthouse.", popen=fake_popen)
        launched.clear()
        ok("resume leaves a live worker alone", resume(popen=fake_popen, alive=lambda p: True) == []
           and not launched)
        ok("resume restarts a dead worker, and only unfinished jobs",
           resume(popen=fake_popen, alive=lambda p: False) == [orphan] and len(launched) == 1)
        for _ in range(MAX_LAUNCHES):
            resume(popen=fake_popen, alive=lambda p: False)
        gave = json.loads((RUNS / orphan / "result.json").read_text())
        ok("resume gives up after MAX_LAUNCHES without sending", gave["state"] == "gave_up"
           and gave["sent"] is False)
        ok("start_job records cumulus2's load for the first reply",
           r.get("video_load", {}).get("reason") == "another video is rendering")
        long_wait = reply_body("CUMULUS", "p", done, 2048, True, "", {}, 400, "v-x", waited=310)
        ok("a long wait is named in the reply", "5 min of it waiting for cumulus2" in long_wait)
        ok("a short wait is not", "waiting" not in reply_body("CUMULUS", "p", done, 2048, True, "", {}, 200,
                                                              "v-x", waited=40))
    RUNS, CONFIG, SENDERS, load_now = saved
    for name, good in checks:
        print(("PASS " if good else "FAIL ") + name)
    print(f"{sum(g for _, g in checks)}/{len(checks)} checks passed")
    return 0 if all(g for _, g in checks) else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["selftest"]:
        sys.exit(selftest())
    if len(sys.argv) == 3 and sys.argv[1] == "worker" and re.fullmatch(r"v-\d{8}-\d{6}-[0-9a-f]{6}", sys.argv[2]):
        worker(sys.argv[2])
        sys.exit(0)
    sys.exit("usage: video_intake.py selftest | worker v-YYYYmmdd-HHMMSS-xxxxxx")
