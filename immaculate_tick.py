#!/usr/bin/env python3
"""immaculate_tick.py -- Project Immaculate's twice-daily tick on CUMULUS (S253).

Buddy, 2026-09-22: "check for the contest in the beginning of the day and at
the end of the day. like 9am and 9pm. We don't need to check daily. Once
found, we don't need to check again for the rest of the week. When the game
is over, we should look up the results and compare how we did."

Each run (immaculate-tick.timer, 09:00 and 21:00 ET):
  1. ESPN schedule: every Steelers game, its status and kickoff. Games fall on
     Thursday, Saturday, Sunday or Monday -- nothing here assumes Sunday.
  2. POST-GAME. Any Final game not yet compared: the resolve pass
     (`immaculate_agent.py postgame`, the only AI step) records the results,
     then the recap email compares them with our picks. A step that fails is
     retried on the next tick; one that succeeded is not repeated.
  3. CONTEST. "This week's game" is the first game not yet Final. Until an
     OPEN contest (entry period not yet ended) has been seen for it, run
     immaculate_watch.py; the first time one is open, Telegram Buddy its
     deadline and the kickoff, then stop checking until that game is Final.
     With under 36h to kickoff and nothing found, one heads-up instead -- a
     watch that missed a renamed contest page would otherwise stay silent.
     Either alert counts as done only once Telegram says "sent"; a failed one
     fails the tick and is retried by the next (S292).

State: logs/immaculate-tick.json. Records job_status "immaculatetick".

    python3 immaculate_tick.py             # a scheduled run
    python3 immaculate_tick.py --dry-run   # say what it would do; run, send, write nothing
    python3 immaculate_tick.py selftest
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

STATE = HERE / "logs" / "immaculate-tick.json"
WATCH_STATE = HERE / "logs" / "immaculate-contests.json"
VENV_PY = str(HERE / ".venv" / "bin" / "python")
AGENT_PY = str(Path.home() / ".venvs" / "alopecia-agent" / "bin" / "python")
ET = ZoneInfo("America/New_York")
HEADS_UP = timedelta(hours=36)
RELAY = ("The questions appear only in the Steelers app once it opens — send them "
         "(screenshot or typed) to any Cowork session and it will research and "
         "email you answers.")


def kickoff(row):
    return datetime.strptime(row["kickoff_utc"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


def game_label(row):
    return f"Week {row['week']} {row['game']}, {kickoff(row).astimezone(ET):%a %-m/%-d %-I:%M %p} ET"


def final_label(row):
    score = ", ".join(f"{t} {s}" for t, s in row["score"].items())
    return f"Week {row['week']} {row['game']} (final {score})"


_ENDS = re.compile(r"([A-Z][a-z]+) (\d{1,2}), (\d{4})\S* at (\d{1,2}):(\d{2}) ([AP]M)")


def contest_end(text):
    """'September 20, 2026 at 1:00 PM EDST' -> an aware datetime, or None."""
    m = _ENDS.search(text or "")
    if not m:
        return None
    mon, d, y, h, mi, ap = m.groups()
    try:
        return datetime.strptime(f"{mon} {d} {y} {h}:{mi} {ap}",
                                 "%B %d %Y %I:%M %p").replace(tzinfo=ET)
    except ValueError:
        return None


def open_contests(watch_state, now):
    """Live contest pages whose entry period has not ended. An end date that
    does not parse counts as OPEN: one extra alert beats a missed contest."""
    out = []
    for slug, r in (watch_state or {}).items():
        if r.get("status") != "200":
            continue
        end = contest_end(r.get("ends"))
        if end is None or end > now:
            out.append((slug, r))
    return out


def plan(rows, state, now):
    """Pure: what this tick should do. -> dict with
    postgame  Final games not yet compared (oldest first)
    target    this week's game (first non-Final), or None off-season
    watch     run the contest watch this tick?
    heads_up  send the 'nothing found yet' warning this tick?"""
    compared = set(state.get("compared", []))
    postgame = [r for r in rows if r["status"] == "Final" and r["event_id"] not in compared]
    upcoming = [r for r in rows if r["status"] != "Final"]
    target = upcoming[0] if upcoming else None
    found = target is not None and target["event_id"] in state.get("found", {})
    watch = target is not None and not found and kickoff(target) > now
    heads_up = (watch and target["event_id"] not in state.get("warned", [])
                and kickoff(target) - now < HEADS_UP)
    return {"postgame": postgame, "target": target, "watch": watch, "heads_up": heads_up}


# S292: send_telegram never raises -- it RETURNS "FAILED: ...". Until then the
# tick ignored that return, so a failed contest alert still marked the week
# found and stopped the checks: Buddy never heard, and nothing retried. Both
# alerts below change state only once the send says "sent". -> (ok, note)
def why(sent):
    return str(sent).removeprefix("FAILED: ")


def contest_alert(r, t, slug):
    """The contest-found Telegram. Deadline and ask come first: Buddy, 09-25,
    "I got the telegram but it was truncated" -- the phone preview showed only
    the 110-character rules title the S255 text led with."""
    # S255: say when it OPENS, not just when it closes. Buddy: for a
    # Sunday game "you may not see the questions until Thursday" --
    # Week 2's rules page went up Wed 9/16 for a Thu 9/17 open, so this
    # can fire a day before the questions are in the app.
    return (f"Immaculate: Week {t['week']} contest posted. Entry closes "
            f"{r.get('ends') or '(see the app)'}.\n"
            "Send the questions (screenshots are fine) to any Cowork session; "
            "it will research and email answers.\n"
            f"Game: {game_label(t)}. Entry opens {r.get('begins') or '(see the app)'}. "
            f"Rules: {r.get('title') or slug}")


def alert_found(tell, state, t, slug, r, now):
    sent = tell(contest_alert(r, t, slug))
    if sent != "sent":
        return False, (f"contest found for {game_label(t)} but Telegram FAILED "
                       f"({why(sent)}); will retry next tick")
    state.setdefault("found", {})[t["event_id"]] = {
        "slug": slug, "title": r.get("title"), "ends": r.get("ends"),
        "found_utc": now.isoformat()}
    return True, f"contest FOUND for {game_label(t)}; no more checks until it is played"


def alert_heads_up(tell, state, t):
    sent = tell(f"Immaculate: no weekly contest found yet for {game_label(t)}. If the "
                f"Steelers app shows one, the watch missed it — {RELAY}")
    if sent != "sent":
        return False, f"under-36h heads-up Telegram FAILED ({why(sent)}); will retry next tick"
    state.setdefault("warned", []).append(t["event_id"])
    return True, "sent the under-36h heads-up"


def _load(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(STATE)


def _run(argv, timeout):
    try:
        r = subprocess.run(argv, cwd=HERE, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def main(dry=False):
    from immaculate_agent import send_telegram
    import immaculate_espn
    now = datetime.now(timezone.utc)
    state = _load(STATE)
    notes, ok = [], True
    tell = (lambda m: print("WOULD TELEGRAM:", m) or "sent") if dry else send_telegram

    try:
        rows = immaculate_espn.schedule()
    except Exception as e:
        return _finish(dry, False, [f"ESPN schedule unreachable: {type(e).__name__}"], state)
    p = plan(rows, state, now)

    # ── post-game: resolve, then compare ─────────────────────────────────────
    if p["postgame"]:
        ids = [r["event_id"] for r in p["postgame"]]
        what = final_label(p["postgame"][-1])
        if not set(ids) <= set(state.get("resolved", [])):
            if dry:
                notes.append(f"would run the resolve pass for {what}")
            else:
                rc, out = _run([AGENT_PY, "immaculate_agent.py", "postgame"], timeout=25 * 60)
                if rc == 0:
                    state.setdefault("resolved", []).extend(i for i in ids if i not in state["resolved"])
                    notes.append(f"resolved {what}")
                else:
                    ok = False   # the agent Telegrams its own failure, with the reason
                    notes.append(f"resolve pass FAILED for {what} (rc {rc}); retry next tick")
        if dry or set(ids) <= set(state.get("resolved", [])):
            rc, out = _run([VENV_PY, "immaculate_wednesday_report.py", "--window",
                            "--game", what] + (["--dry-run"] if dry else []), timeout=5 * 60)
            if dry:
                notes.append(f"recap email preview for {what}:\n{out}")
            elif rc == 0:
                state.setdefault("compared", []).extend(i for i in ids if i not in state["compared"])
                notes.append(f"recap emailed for {what}")
            else:
                ok = False
                sent = tell(f"Immaculate: the recap email for {what} FAILED to send; retrying at the next 9am/9pm check.")
                notes.append(f"recap email FAILED for {what}"
                             + ("" if sent == "sent" else f"; its Telegram alert FAILED too ({why(sent)})"))

    # ── contest: look until found, then rest until the game is played ────────
    t = p["target"]
    if t is None:
        notes.append("no upcoming game (off-season)")
    elif t["event_id"] in state.get("found", {}):
        notes.append(f"contest already found for {game_label(t)} — not checking")
    elif not p["watch"]:
        notes.append(f"{game_label(t)} is under way — entry is closed")
    elif dry:
        notes.append(f"would run the contest watch for {game_label(t)}")
    else:
        rc, out = _run([VENV_PY, "immaculate_watch.py"], timeout=5 * 60)
        opened = open_contests(_load(WATCH_STATE), now)
        if opened:
            slug, r = opened[0]
            sent, note = alert_found(tell, state, t, slug, r, now)
            ok = ok and sent
            notes.append(note)
        else:
            notes.append(f"no open contest yet for {game_label(t)}"
                         + (" (some pages unreachable)" if rc == 1 else ""))
            if p["heads_up"]:
                sent, note = alert_heads_up(tell, state, t)
                ok = ok and sent
                notes.append(note)

    return _finish(dry, ok, notes, state)


def _finish(dry, ok, notes, state):
    note = "; ".join(n.splitlines()[0] for n in notes)
    print("\n".join(notes))
    if dry:
        print("(dry run: nothing written)")
        return 0
    _save(state)
    try:
        import job_status
        job_status.record("immaculatetick", ok, note[:200])
    except Exception as e:
        print(f"job_status.record failed: {e}")
    return 0 if ok else 1


# ── selftest ──────────────────────────────────────────────────────────────────
def selftest():
    """Offline (T32): pure planning only -- no ESPN, no watch, no Telegram."""
    ok = True

    def ck(label, cond):
        nonlocal ok
        print(f"  [{'OK ' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    def g(week, eid, status, when):
        return {"week": week, "event_id": eid, "status": status, "kickoff_utc": when,
                "game": f"G{week}", "score": {"PIT": "0", "X": "0"}}

    rows = [g(2, "e2", "Final", "2026-09-20T17:00Z"),
            g(3, "e3", "Scheduled", "2026-09-27T17:00Z"),
            g(4, "e4", "Scheduled", "2026-10-02T00:15Z")]
    tue = datetime(2026, 9, 22, 22, 0, tzinfo=timezone.utc)
    sat = datetime(2026, 9, 26, 13, 0, tzinfo=timezone.utc)      # 28h before kickoff

    p = plan(rows, {}, tue)
    ck("an uncompared Final game is queued for post-game", [r["event_id"] for r in p["postgame"]] == ["e2"])
    ck("this week's game is the first non-Final one", p["target"]["event_id"] == "e3")
    ck("not found yet -> watch runs", p["watch"])
    ck("kickoff 4+ days out -> no heads-up", not p["heads_up"])
    ck("compared games are not redone", plan(rows, {"compared": ["e2"]}, tue)["postgame"] == [])
    ck("found -> the watch rests for the rest of the week",
       not plan(rows, {"found": {"e3": {}}}, tue)["watch"])
    ck("under 36h, nothing found -> one heads-up", plan(rows, {}, sat)["heads_up"])
    ck("...and only once", not plan(rows, {"warned": ["e3"]}, sat)["heads_up"])

    # S292: a failed send must change nothing, so the next tick tries again
    down, up = (lambda m: "FAILED: URLError"), (lambda m: "sent")
    wk3 = {"title": "Week 3", "ends": "September 27, 2026 at 1:00 PM EDST"}
    st = {}
    sent, note = alert_found(down, st, rows[1], "weekly", wk3, tue)
    ck("contest found, Telegram FAILED -> tick fails, week NOT marked found, watch runs again",
       not sent and "FAILED" in note and "found" not in st and plan(rows, st, tue)["watch"])
    sent, note = alert_found(up, st, rows[1], "weekly", wk3, tue)
    ck("...retried and sent -> marked found, watch rests",
       sent and "e3" in st["found"] and not plan(rows, st, tue)["watch"])
    st = {}
    sent, note = alert_heads_up(down, st, rows[1])
    ck("heads-up Telegram FAILED -> tick fails, NOT marked warned, sent again next tick",
       not sent and "FAILED" in note and plan(rows, st, sat)["heads_up"])
    sent, note = alert_heads_up(up, st, rows[1])
    ck("...retried and sent -> only once", sent and not plan(rows, st, sat)["heads_up"])
    long_title = ("PITTSBURGH STEELERS IMMACULATE PREDICTION 2026 NICK HERBIG AUTOGRAPHED "
                  "REPLICA JERSEY GIVEAWAY CONTEST OFFICIAL RULES")   # the real 09-24 title
    head = contest_alert(dict(wk3, title=long_title), rows[1], "weekly")[:120]
    ck("contest alert: the close time and the ask fit the phone preview (first 120 chars)",
       wk3["ends"] in head and "questions" in head)

    done3 = [g(2, "e2", "Final", "2026-09-20T17:00Z"), g(3, "e3", "Final", "2026-09-27T17:00Z"),
             g(4, "e4", "Scheduled", "2026-10-02T00:15Z")]
    p = plan(done3, {"compared": ["e2"], "found": {"e3": {}}}, sat + timedelta(days=1, hours=12))
    ck("game played -> post-game for it AND the next week's watch resumes",
       [r["event_id"] for r in p["postgame"]] == ["e3"] and p["target"]["event_id"] == "e4"
       and p["watch"])
    # Week 4 kicks off Thu 10/1 8:15 PM ET: the Wed 9am tick is 35h15m out
    ck("Thursday-night game: heads-up at the Wednesday 9am tick",
       plan(done3, {"compared": ["e2", "e3"]}, datetime(2026, 9, 30, 13, 0, tzinfo=timezone.utc))["heads_up"])
    ck("...but not at the Tuesday 9pm tick before it (47h out)",
       not plan(done3, {"compared": ["e2", "e3"]}, datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc))["heads_up"])
    ck("game under way, no contest found -> no watch (entry is closed)",
       not plan(rows, {}, datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc))["watch"])
    ck("off-season: nothing to watch",
       plan([g(18, "e18", "Final", "2027-01-10T18:00Z")], {"compared": ["e18"]}, tue)["target"] is None)

    ck("contest end parses (the real Week 2 text)",
       contest_end("September 20, 2026 at 1:00 PM EDST")
       == datetime(2026, 9, 20, 13, 0, tzinfo=ET))
    ws = {"weekly": {"status": "200", "ends": "September 20, 2026 at 1:00 PM EDST"},
          "season": {"status": "200", "ends": "September 13, 2026 at 1:00 PM EST"},
          "gone": {"status": "404"}}
    ck("closed contests are not 'found'", open_contests(ws, tue) == [])
    ws["weekly"]["ends"] = "September 27, 2026 at 1:00 PM EDST"
    ck("an open contest is found", [s for s, _ in open_contests(ws, tue)] == ["weekly"])
    ck("an unparseable end date counts as open (alert rather than miss)",
       open_contests({"x": {"status": "200", "ends": "soon"}}, tue) != [])
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["selftest"]:
        sys.exit(selftest())
    sys.exit(main(dry="--dry-run" in a))
