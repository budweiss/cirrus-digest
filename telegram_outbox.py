#!/usr/bin/env python3
"""
telegram_outbox.py  (S592, 2026-11-09) -- Telewarden step one
===============================================================================
Keeps a copy of every Telegram message this box's jobs send to Buddy, at the
moment it is sent, so a later agent can read the warnings without reading
Telegram. Telegram cannot be read instead: bots "will not be able to see
messages from other bots" (Telegram bot FAQ), and a second getUpdates receiver
on a token breaks the first (S471: 190 conflicts in one morning).

Delivery never changes. record() never raises, never blocks a send, and never
writes the bot token. A copy that cannot be written prints one line on stderr,
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


def record(text, ok, token):
    """Append one row for a message just sent. ok: True, False or None (unknown)."""
    try:
        if not _live_send(token):
            return
        text = text if isinstance(text, str) else str(text)
        main = sys.modules.get("__main__")
        job = Path(getattr(main, "__file__", "") or sys.argv[0] or "?").stem
        via = sys._getframe(1).f_globals.get("__name__", "?")
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "host": socket.gethostname().split(".")[0].lower(),
               "job": job, "via": via if via != "__main__" else job,
               "ok": ok if ok in (True, False) else None,
               "chars": len(text), "text": _redact(text[:MAX_TEXT])}
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
        finally:
            os.close(fd)
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
            c = per.setdefault(d, {}).setdefault(r.get("job", "?"), [0, 0])
            c[0] += 1
            c[1] += r.get("ok") is False
    out, gaps = [f"{path}: {len(rows)} rows in file, last {days} days below"], 0
    for n in range(days, -1, -1):
        d = time.strftime("%Y-%m-%d", time.localtime(time.time() - n * 86400))
        if d not in per:
            if d < today:
                gaps += 1
                out.append(f"{d}  NO ROWS (copy not running?)")
            continue
        jobs = ", ".join(f"{j} {c[0]}" + (f" ({c[1]} failed)" if c[1] else "")
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
        finally:
            os.environ.pop("TELEGRAM_OUTBOX", None)
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
