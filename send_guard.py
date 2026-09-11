#!/usr/bin/env python3
"""One-send-per-day guard for the client mailers (S81, Buddy 2026-08-27).

WHY THIS EXISTS
---------------
`cirrus-billsnow.service` and `cirrus-billnewdev.service` have been in
Skywarden's restart allowlist since S63, and both EMAIL BILL. Neither had any
record of having sent. A systemd restart re-runs the unit from the top, so if
either ever failed *after* a successful send, the rerun would compose and send
Bill a second copy of the same weekly update.

Narrow — in both jobs the send is the last real step — but the standing rule is
that an agent never makes an outward-facing send on its own, and "it probably
will not happen" is not that rule.

THE FAILURE DIRECTION IS DELIBERATE, AND IT IS NOT THE OBVIOUS ONE
------------------------------------------------------------------
Buddy's constraint when approving this: *a buggy guard means Bill misses a real
update, which is worse than a rare duplicate.* So this guard **fails OPEN**.

    It blocks a send ONLY on positive, readable evidence that this job already
    sent successfully today. Missing file, unreadable file, unparseable JSON,
    permission error, no logs directory — every one of those means SEND.

That is the opposite of how a lock is normally written, and it is correct here:
the thing being protected against is a rare duplicate, and the thing that must
never happen is a suppressed weekly update. A guard that fails closed would
convert a filesystem hiccup into Bill silently not hearing from us.

WHAT COUNTS AS SENT
-------------------
Only a send whose sender process exited 0. "Composed", "attempted" and "failed"
all leave no stamp, so a rerun after a genuine send failure still sends — which
is the whole point of the retry.

SCOPE
-----
Per job, per calendar day. Both jobs are WEEKLY (Monday), so a same-day second
send is never legitimate, while a rerun a week later is. The date lives in the
FILENAME rather than inside the file, so "is this today's stamp?" needs no
parsing and cannot be got wrong -- the S80 `_prune` bug was a hardcoded slice
into a timestamp string, and there is no reason to invite that twice.

No pruning: one ~150-byte file per job per send, 52 a year. Left unpruned on
purpose — a prune is moving parts, and the last prune written in this tree
(config_snapshot, S80) was itself the bug.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

DIGEST_DIR = Path(__file__).resolve().parent
STAMP_DIR = DIGEST_DIR / "logs/send-stamps"


def _today(now=None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def stamp_path(job: str, now=None, root=None) -> Path:
    root = Path(root) if root else STAMP_DIR
    # Keep the job name filesystem-safe without silently mangling two different
    # jobs into one filename.
    safe = "".join(c for c in job if c.isalnum() or c in "-_")
    if safe != job:
        raise ValueError(f"unsafe job name for a stamp file: {job!r}")
    return root / f"{safe}-{_today(now)}.json"


def already_sent_today(job: str, now=None, root=None):
    """The stamp for a successful send by `job` today, or None.

    None means "go ahead and send" and is returned for every uncertain case —
    see the module docstring. Never raises.
    """
    try:
        p = stamp_path(job, now, root)
    except ValueError:
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None                       # missing/unreadable/corrupt -> SEND
    if not isinstance(data, dict) or not data.get("sent_ok"):
        return None
    return data


def days_since_last_send(job: str, now=None, root=None):
    """Days since `job` last sent successfully, or None if it never has.

    S146. already_sent_today() answers a DAILY question, which is the right one
    for a job that could legitimately fire twice in a day. billnewdev is WEEKLY,
    and on 2026-09-10 it gained a quiet-week email that goes out even when there
    is nothing to report. Those two facts combine badly: the unit is a oneshot on
    Skywarden's restart allowlist, so a restart on any day but the scheduled one
    passes the daily guard and mails the client a second "nothing new this week".

    Read-only; never raises. None means "no successful send on record", which
    callers must treat as "go ahead" -- the same fail-open posture as the rest of
    this module, because refusing to send on a missing stamp would let one
    unreadable file silence a client feed indefinitely.
    """
    root = Path(root) if root else STAMP_DIR
    safe = "".join(c for c in job if c.isalnum() or c in "-_")
    if safe != job:
        return None
    newest = None
    try:
        for f in root.glob(f"{safe}-*.json"):
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue                  # corrupt stamp is not a send
            if not isinstance(data, dict) or not data.get("sent_ok"):
                continue
            try:
                when = datetime.strptime(f.stem[len(safe) + 1:], "%Y-%m-%d")
            except ValueError:
                continue
            if newest is None or when > newest:
                newest = when
    except Exception:
        return None
    if newest is None:
        return None
    ref = now or datetime.now()
    return (ref.replace(hour=0, minute=0, second=0, microsecond=0) - newest).days


def mark_sent(job: str, detail: str = "", now=None, root=None) -> bool:
    """Record that `job` sent successfully today. Best-effort; never raises.

    Returns True if the stamp is on disk. A False here means the NEXT rerun
    would send again — worth logging, never worth failing the run over, since
    the mail has already gone out by the time this is called.
    """
    try:
        p = stamp_path(job, now, root)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"job": job, "sent_ok": True,
                   "at": (now or datetime.now()).isoformat(timespec="seconds"),
                   "detail": (detail or "")[:200],
                   "pid": os.getpid()}
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, p)                # atomic: no half-written stamp
        return True
    except Exception as e:
        print(f"send_guard: could not write stamp for {job}: {e}", file=sys.stderr)
        return False


def blocked_message(job: str, stamp: dict) -> str:
    return (f"send_guard: {job} ALREADY SENT successfully today at "
            f"{stamp.get('at', '?')} ({stamp.get('detail', '')}). "
            f"Not sending again — this run is almost certainly a restart of the "
            f"one that sent. Override with --force-send.")


# ── selftest ──────────────────────────────────────────────────────────────────
def selftest() -> bool:
    import tempfile
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    root = Path(tempfile.mkdtemp())
    d1 = datetime(2026, 8, 31, 4, 0, 0)          # a Monday
    d1_later = datetime(2026, 8, 31, 4, 6, 0)    # the restart, six minutes on
    d8 = datetime(2026, 9, 7, 4, 0, 0)           # the next Monday

    ck("a job that has never sent is not blocked",
       already_sent_today("billsnow", d1, root) is None)

    ck("marking a send writes a stamp", mark_sent("billsnow", "sent material update", d1, root))

    # THE case: the restart six minutes later must not send again.
    ck("a restart the same day IS blocked",
       already_sent_today("billsnow", d1_later, root) is not None)
    ck("...and the message says why and how to override",
       "--force-send" in blocked_message("billsnow", already_sent_today("billsnow", d1_later, root)))

    # The failure that must never happen: next week's real update goes out.
    ck("the NEXT WEEK's run is not blocked",
       already_sent_today("billsnow", d8, root) is None)

    # Jobs must not share a stamp.
    ck("a different job is unaffected",
       already_sent_today("billnewdev", d1_later, root) is None)

    # ---- fail-OPEN, the direction Buddy asked for -------------------------
    empty = Path(tempfile.mkdtemp())
    ck("a missing stamp directory means SEND",
       already_sent_today("billsnow", d1, empty / "nope") is None)

    bad = Path(tempfile.mkdtemp())
    (bad).mkdir(parents=True, exist_ok=True)
    (bad / f"billsnow-{d1:%Y-%m-%d}.json").write_text("{ this is not json")
    ck("a CORRUPT stamp means SEND, not block",
       already_sent_today("billsnow", d1, bad) is None)

    (bad / f"billsnow-{d1:%Y-%m-%d}.json").write_text('{"sent_ok": false}')
    ck("a stamp that records a FAILED send means SEND",
       already_sent_today("billsnow", d1, bad) is None)

    (bad / f"billsnow-{d1:%Y-%m-%d}.json").write_text('["wrong", "shape"]')
    ck("a stamp of the wrong TYPE means SEND",
       already_sent_today("billsnow", d1, bad) is None)

    unreadable = Path(tempfile.mkdtemp())
    f = unreadable / f"billsnow-{d1:%Y-%m-%d}.json"
    f.write_text('{"sent_ok": true}')
    try:
        os.chmod(f, 0o000)
        ck("an UNREADABLE stamp means SEND (fail open)",
           already_sent_today("billsnow", d1, unreadable) is None)
        os.chmod(f, 0o644)
    except OSError:
        ck("an UNREADABLE stamp means SEND (fail open) [skipped: chmod]", True)

    # mark_sent must never raise, even somewhere unwritable.
    try:
        r = mark_sent("billsnow", "x", d1, Path("/proc/nonexistent-s81"))
        ck("mark_sent returns False rather than raising when it cannot write", r is False)
    except Exception:
        ck("mark_sent returns False rather than raising when it cannot write", False)

    # A job name that would escape the directory must be refused, not sanitised
    # into a collision with a real job's stamp.
    try:
        stamp_path("../../etc/passwd", d1, root)
        ck("a path-traversing job name is refused", False)
    except ValueError:
        ck("a path-traversing job name is refused", True)

    # No half-written stamp is ever visible (atomic replace).
    ck("the stamp write is atomic (tmp + os.replace)",
       "os.replace" in Path(__file__).read_text())

    # ── S146: days_since_last_send — the WEEKLY question, not the daily one.
    root2 = Path(tempfile.mkdtemp())
    ck("no stamps at all reads as None (never sent), not as 0",
       days_since_last_send("billnewdev", now=d8, root=root2) is None)
    mark_sent("billnewdev", "quiet note", now=d1, root=root2)
    ck("a send on the 31st is 7 days before the 7th",
       days_since_last_send("billnewdev", now=d8, root=root2) == 7)
    ck("...and 0 days on the day itself",
       days_since_last_send("billnewdev", now=d1_later, root=root2) == 0)
    mark_sent("billnewdev", "later note", now=d8, root=root2)
    ck("the NEWEST successful stamp wins, not the first one found",
       days_since_last_send("billnewdev", now=d8, root=root2) == 0)
    (root2 / "billnewdev-2026-09-09.json").write_text("{ not json")
    ck("a corrupt stamp is ignored, not counted as a send",
       days_since_last_send("billnewdev", now=d8, root=root2) == 0)
    (root2 / "billnewdev-2026-09-09.json").write_text('{"sent_ok": false}')
    ck("a FAILED send is not a send",
       days_since_last_send("billnewdev", now=d8, root=root2) == 0)
    ck("another job's stamps are not counted",
       days_since_last_send("billsnow", now=d8, root=root2) is None)

    bad_n = 0
    for name, ok in checks:
        print(("  ok   " if ok else "  FAIL ") + name)
        bad_n += 0 if ok else 1
    print()
    print("all send_guard selftests passed" if not bad_n else f"{bad_n} FAILED")
    return bad_n == 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(0 if selftest() else 1)
    for j in ("billsnow", "billnewdev"):
        s = already_sent_today(j)
        print(f"{j}: {'BLOCKED — ' + s['at'] if s else 'clear to send'}")
