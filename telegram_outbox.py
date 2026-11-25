#!/usr/bin/env python3
"""
telegram_outbox.py  (S592, 2026-11-09) -- Telewarden step one
===============================================================================
Keeps a copy of every Telegram message this box's jobs send to Buddy, at the
moment it is sent, so a later agent can read the warnings without reading
Telegram. Telegram cannot be read instead: bots "will not be able to see
messages from other bots" (Telegram bot FAQ), and a second getUpdates receiver
on a token breaks the first (S471: 190 conflicts in one morning).

record() never changes delivery: it never raises, never blocks a send, and never
writes the bot token. S685: hold() is the one exception, for monitor jobs only;
see the gate section below. A copy that cannot be written prints one line on stderr,
which lands in the calling job's own log.

Rows go to logs/telegram-outbox.jsonl beside this file (TELEGRAM_OUTBOX
overrides it; tests always override it). A row is written only for a real send:
a bot token shaped like a real one AND the genuine urllib.request.urlopen.
Selftests that fake either one (most of them do) leave no row (T32).

  python3 telegram_outbox.py --report [--days 7]   # counts per day and job
  python3 telegram_outbox.py --selftest
"""
import json
import os
import re
import socket
import sys
import time
import urllib.request
from pathlib import Path

OUTBOX = Path(__file__).resolve().parent / "logs" / "telegram-outbox.jsonl"
MAX_BYTES = 20 * 1024 * 1024        # then the file moves to .1 (one old copy kept)
MAX_TEXT = 8000
_TOKEN_SHAPE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
_SECRETS = [
    (re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}"), "<redacted-token>"),
    (re.compile(r"(?i)\b(token|password|passwd|secret|api_key|apikey)=[^\s&]+"),
     r"\1=<redacted>"),
]


def _path():
    return Path(os.environ.get("TELEGRAM_OUTBOX") or OUTBOX)


def _live_send(token):
    """True only when the send just made could have reached Telegram."""
    real = urllib.request.urlopen
    return (isinstance(token, str) and bool(_TOKEN_SHAPE.match(token))
            and getattr(real, "__module__", "") == "urllib.request"
            and getattr(real, "__name__", "") == "urlopen")


def _redact(text):
    for pat, rep in _SECRETS:
        text = pat.sub(rep, text)
    return text


def _names(frame):
    """(job, via) for the sender: the running script, and the module that called us."""
    main = sys.modules.get("__main__")
    job = Path(getattr(main, "__file__", "") or sys.argv[0] or "?").stem
    via = frame.f_globals.get("__name__", "?")
    if job in ("", "?") or job.startswith("-"):   # G1 (S613): python3 -c / - name no script
        job = via if via != "__main__" else "?"
    return job, (via if via != "__main__" else job)


def _row(text, ok, job, via):
    return {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "host": socket.gethostname().split(".")[0].lower(),
            "job": job, "via": via,
            "ok": ok if ok in (True, False) else None,
            "chars": len(text), "text": _redact(text[:MAX_TEXT])}


def _write(row, sync=False):
    """Append one row; raises on failure."""
    line = (json.dumps(row, ensure_ascii=False) + "\n").encode()
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        if os.fstat(fd).st_size > MAX_BYTES:
            os.replace(str(path), str(path) + ".1")
            os.close(fd)
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line)
        if sync:
            os.fsync(fd)
    finally:
        os.close(fd)


# ── S685 gate: handled first, then Buddy (Buddy, 2026-11-25) ─────────────────
# "implement a solution so these errors are handled before they are message to
# me. Only issues that can't be resolved by the local harness and LLM using the
# foundational models for help, should be sent to me."
# A monitor job's warning or notice is written here (held) instead of sent.
# Telewarden reads it like any copy: a warning becomes a case that the CIRRUS
# repair worker tries (local model, then foundation model). Buddy hears only
# when a case moves to needs-buddy, or is still happening after WAIT_H. Held
# notices are counted in the morning brief. Everything else is sent as before.
# Fail-open: no fresh Telewarden lease (it renews one on each host every pass),
# an off switch, any error, or a fake token means the message is sent.
HOLD_JOBS = {"jobs_check", "foundation_renewal", "foundation_renewal_cirrus",
             "foundation_renewal_auto", "renewal_recovery", "bill_watch", "model_health",
             "client_contact_daily", "environment_scan", "fleet_qualify",
             "opinionai_review", "intake", "macbook_selfheal"}
FYI_ONLY_JOBS = {"intake"}       # client pipeline: only its notices wait; its warnings go now
# Words that ask for Buddy himself, or that Bill's reports may miss a week (S537).
SEND_RE = re.compile(
    r"(?i)(human needed|needs? (?:a )?live session|could not answer|without a verified "
    r"reply|retry did not fix|need a look|would miss|credential|password|security|breach|"
    r"over budget|budget (?:exceeded|exhausted)|spend (?:limit|cap)|payment|invoice)")
LEASE_MAX_S = 20 * 60            # Telewarden passes every 5 min


def _lease():
    return Path(os.environ.get("TELEGRAM_GATE_LEASE")
                or Path(__file__).resolve().parent / "logs" / "telewarden-lease")


def hold(text, token, job=None):
    """True: the message is safely queued for Telewarden; do not send it.
    False: send it as before. Never raises."""
    try:
        if not _live_send(token):
            return False
        text = text if isinstance(text, str) else str(text)
        name, via = _names(sys._getframe(1))
        name = job or name
        lease = _lease()
        if (name not in HOLD_JOBS or (lease.parent / "telegram-gate-off").exists()
                or time.time() - lease.stat().st_mtime > LEASE_MAX_S
                or SEND_RE.search(text)):
            return False
        import telewarden as tw
        probe = {"job": name, "text": text, "source": "", "kind": None}
        if tw.is_private(probe):
            return False
        kind, _ = tw.classify(probe)
        if name in FYI_ONLY_JOBS and kind == "warning":
            return False
        _write(dict(_row(text, None, name, via), held=True), sync=True)
        return True
    except Exception:
        return False


def record(text, ok, token):
    """Append one row for a message just sent. ok: True, False or None (unknown)."""
    try:
        if not _live_send(token):
            return
        text = text if isinstance(text, str) else str(text)
        _write(_row(text, ok, *_names(sys._getframe(1))))
    except Exception as e:  # never let the copy hurt the send
        try:
            print(f"telegram_outbox: not recorded ({type(e).__name__})", file=sys.stderr)
        except Exception:
            pass


def report(days=7, path=None):
    """Counts per day and job. A day with no rows is flagged: this box sends
    at least one message a day, so an empty day means the copy stopped."""
    path = Path(path or _path())
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except OSError as e:
        return f"{path}: unreadable ({type(e).__name__})", 1
    cut = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    today = time.strftime("%Y-%m-%d")
    per = {}
    for r in rows:
        d = str(r.get("ts", ""))[:10]
        if d >= cut:
            c = per.setdefault(d, {}).setdefault(r.get("job", "?"), [0, 0, 0])
            c[0] += 1
            c[1] += r.get("ok") is False
            c[2] += r.get("held") is True
    out, gaps = [f"{path}: {len(rows)} rows in file, last {days} days below"], 0
    for n in range(days, -1, -1):
        d = time.strftime("%Y-%m-%d", time.localtime(time.time() - n * 86400))
        if d not in per:
            if d < today:
                gaps += 1
                out.append(f"{d}  NO ROWS (copy not running?)")
            continue
        jobs = ", ".join(f"{j} {c[0]}" + (f" ({c[1]} failed)" if c[1] else "")
                         + (f" ({c[2]} held)" if c[2] else "")
                         for j, c in sorted(per[d].items(), key=lambda kv: -kv[1][0]))
        out.append(f"{d}  {sum(c[0] for c in per[d].values()):3d}  {jobs}")
    return "\n".join(out), 1 if gaps else 0


def selftest():
    import tempfile
    from unittest import mock
    ok = True

    def check(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("PASS " if cond else "FAIL ") + name)

    real_token = "123456789:" + "A" * 35
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "logs" / "out.jsonl"
        os.environ["TELEGRAM_OUTBOX"] = str(p)
        try:
            record("⚠️ disk low", True, real_token)
            rows = [json.loads(x) for x in p.read_text().splitlines()]
            check("a real send writes one row", len(rows) == 1 and rows[0]["text"] == "⚠️ disk low")
            check("the row has host, job, via and ok", all(rows[0].get(k) is not None
                  for k in ("host", "job", "via", "ok", "ts", "chars")))
            check("the file is private (0600)", (p.stat().st_mode & 0o777) == 0o600)
            import types
            ns = {"__name__": "fake_caller", "record": record}
            exec("def go(t, tok):\n    record(t, True, tok)\n", ns)
            with mock.patch.dict(sys.modules, {"__main__": types.ModuleType("__main__")}), \
                    mock.patch.object(sys, "argv", ["-c"]):
                ns["go"]("sent from python3 -c", real_token)
            last = json.loads(p.read_text().splitlines()[-1])
            check("a python3 -c sender is named by its caller, not '-c' (G1)",
                  last["job"] == "fake_caller")
            p.write_text(p.read_text().splitlines()[0] + "\n")   # keep the counts below as before
            record(f"leak {real_token} and url?token=abc123", False, real_token)
            last = json.loads(p.read_text().splitlines()[-1])
            check("a token in the text is redacted", real_token not in p.read_text()
                  and "abc123" not in last["text"] and last["ok"] is False)
            check("the token itself is never written",
                  real_token not in json.dumps([json.loads(x) for x in p.read_text().splitlines()]))
            n = len(p.read_text().splitlines())
            for fake in ("t", "", None, "tok", "123:short"):
                record("fake creds", True, fake)
            check("a fake-looking token writes nothing", len(p.read_text().splitlines()) == n)
            with mock.patch("urllib.request.urlopen"):
                record("patched network", True, real_token)
            urllib.request.urlopen, saved = (lambda *a, **k: None), urllib.request.urlopen
            try:
                record("hand-patched network", True, real_token)
            finally:
                urllib.request.urlopen = saved
            check("a faked urlopen writes nothing", len(p.read_text().splitlines()) == n)
            record("x" * (MAX_TEXT + 50), None, real_token)
            last = json.loads(p.read_text().splitlines()[-1])
            check("long text is capped, length kept, unknown ok is null",
                  len(last["text"]) == MAX_TEXT and last["chars"] == MAX_TEXT + 50 and last["ok"] is None)
            os.environ["TELEGRAM_OUTBOX"] = str(Path(td) / "blocked")
            (Path(td) / "blocked").mkdir()          # a directory: open() fails
            import contextlib, io
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                record("cannot write", True, real_token)
            check("an unwritable outbox never raises and says so on stderr",
                  "not recorded" in err.getvalue())
            os.environ["TELEGRAM_OUTBOX"] = str(p)
            text, rc = report(days=0)
            check("report counts today's rows by job", rc == 0 and "telegram_outbox 3 (1 failed)" in text)
            old = time.strftime("%Y-%m-%dT00:00:00%z", time.localtime(time.time() - 3 * 86400))
            q = Path(td) / "gap.jsonl"
            q.write_text(json.dumps({"ts": old, "job": "j", "ok": True}) + "\n")
            text, rc = report(days=3, path=q)
            check("a silent past day is flagged and fails the report",
                  rc == 1 and "NO ROWS" in text)
            big = Path(td) / "big.jsonl"
            big.write_bytes(b"x" * 10)
            os.environ["TELEGRAM_OUTBOX"] = str(big)
            with mock.patch(__name__ + ".MAX_BYTES", 5):
                record("after rotate", True, real_token)
            check("an oversized file moves to .1 and a fresh file starts",
                  Path(str(big) + ".1").exists() and "after rotate" in big.read_text())

            # S685 gate
            g = Path(td) / "gate" / "out.jsonl"
            lease = g.parent / "telewarden-lease"
            os.environ["TELEGRAM_OUTBOX"] = str(g)
            os.environ["TELEGRAM_GATE_LEASE"] = str(lease)
            warn = "⚠️ *CIRRUS jobs need attention*\n⚠️ shoredigest: 2026-11-06T08:36 FAILED — ValueError"
            check("no Telewarden lease: sent as before", not hold(warn, real_token, job="jobs_check"))
            lease.parent.mkdir(parents=True)
            lease.touch()
            check("a fake token is never held", not hold(warn, "t", job="jobs_check"))
            check("a job outside the hold list is sent", not hold(warn, real_token, job="cirrus_bot"))
            check("a monitor warning is held", hold(warn, real_token, job="jobs_check"))
            rows = [json.loads(x) for x in g.read_text().splitlines()]
            check("the held row is marked held, ok unknown, text kept",
                  len(rows) == 1 and rows[0]["held"] is True and rows[0]["ok"] is None
                  and rows[0]["job"] == "jobs_check" and "shoredigest" in rows[0]["text"])
            check("the job comes from the script when not given",
                  not hold(warn, real_token) or Path(sys.argv[0]).stem in HOLD_JOBS)
            for text, why in (("bill_watch: Bill's Monday emails need a look; automatic retry did not fix",
                               "Bill may miss a week"),
                              ("model_health: HUMAN NEEDED, credential expired", "asks for Buddy"),
                              ("model_health failed on the holdings feed", "private")):
                check(f"sent, not held: {why}", not hold(text, real_token, job="model_health"))
            check("intake: a client-pipeline warning is sent",
                  not hold("intake failed: IMAP timed out", real_token, job="intake"))
            check("intake: a notice is held",
                  hold("📥 *Intake*: 1 new request(s)\n• bill: Re: Research — ✅ Answer delivered",
                       real_token, job="intake"))
            old_t = time.time() - LEASE_MAX_S - 60
            os.utime(lease, (old_t, old_t))
            check("a stale lease (Telewarden stopped) sends", not hold(warn, real_token, job="jobs_check"))
            lease.touch()
            (lease.parent / "telegram-gate-off").touch()
            check("the off switch sends", not hold(warn, real_token, job="jobs_check"))
            (lease.parent / "telegram-gate-off").unlink()
            n = len(g.read_text().splitlines())
            os.environ["TELEGRAM_OUTBOX"] = str(Path(td) / "blocked")
            check("an unwritable outbox sends (never lost)", not hold(warn, real_token, job="jobs_check"))
            os.environ["TELEGRAM_OUTBOX"] = str(g)
            check("nothing extra was written by refusals", len(g.read_text().splitlines()) == n == 2)
            text, rc = report(days=0)
            check("report shows held rows", "jobs_check 1 (1 held)" in text and "intake 1 (1 held)" in text)
        finally:
            os.environ.pop("TELEGRAM_OUTBOX", None)
            os.environ.pop("TELEGRAM_GATE_LEASE", None)
    print("selftest", "OK" if ok else "FAILED")
    return ok


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(0 if selftest() else 1)
    if "--report" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 7
        text, rc = report(days)
        print(text)
        raise SystemExit(rc)
    print(__doc__)
