#!/usr/bin/env python3
"""gmail_inbox_peek.py — READ-ONLY peek at the cirrustask@gmail.com inbox.
Lists recent messages (date / from / subject) + total & unseen counts. Marks
nothing as read (readonly select + BODY.PEEK). Runs on CIRRUS via `gmail-inbox`.
"""
import imaplib, email, re, sys
from email.header import decode_header
from pathlib import Path

sys.path.insert(0, str(Path.home() / "projects/cirrus-digest"))
from send_digest import FROM_EMAIL, FROM_PASS  # cirrustask@gmail.com + app pw


def dec(s):
    if not s:
        return ""
    out = ""
    for t, enc in decode_header(s):
        out += t.decode(enc or "utf-8", "ignore") if isinstance(t, bytes) else t
    return out.replace("\n", " ").replace("\r", " ").strip()


def _clip(text, limit):
    """Truncate loudly. S79: a client email carrying a booking agent's price
    list was cut at the old silent 3000-char limit, and nothing in the output
    said so — the reader had no way to know the interesting part was missing."""
    if len(text) <= limit:
        return text
    return (text[:limit] +
            f"\n\n[TRUNCATED — {len(text)} chars total, showing {limit}. "
            f"Re-run with a larger limit: gmail-inbox args.limit]")


def body_text(msg, limit=3000):
    """Best-effort plain-text body, clipped to `limit` with a visible marker."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
               "attachment" not in str(part.get("Content-Disposition", "")):
                try:
                    return _clip(part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "ignore"), limit)
                except Exception:
                    pass
        return "(no text/plain part)"
    try:
        return _clip(msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", "ignore"), limit)
    except Exception:
        return "(could not decode body)"


def show_bodies(m, from_sub, limit=3000):
    """Print full headers + body of recent messages FROM a sender substring."""
    typ, data = m.search(None, "FROM", f'"{from_sub}"')
    ids = data[0].split()
    if not ids:
        print(f"No messages from '{from_sub}'."); return
    print(f"{len(ids)} message(s) from '{from_sub}' — showing the last 3 with bodies:\n")
    for i in reversed(ids[-3:]):
        typ, md = m.fetch(i, "(BODY.PEEK[])")
        raw = b"".join(p[1] for p in md if isinstance(p, tuple))
        msg = email.message_from_bytes(raw)
        print("=" * 70)
        print("From:   ", dec(msg.get("From", "")))
        print("Date:   ", dec(msg.get("Date", "")))
        print("Subject:", dec(msg.get("Subject", "")))
        print("-" * 70)
        print(body_text(msg, limit).strip())
        print()


def selftest():
    """S79: this file had no test at all, which the coverage check flagged.
    _clip is the whole point of the file's S79 change - it is what stopped a
    client email being cut in silence - so it is what gets pinned."""
    failures = []

    def check(label, ok):
        print(("  PASS  " if ok else "  FAIL  ") + label)
        if not ok:
            failures.append(label)

    check("a short body is returned untouched", _clip("abc", 10) == "abc")
    check("a body exactly at the limit is not marked",
          _clip("x" * 10, 10) == "x" * 10)
    long = _clip("x" * 50, 10)
    check("a long body is cut to the limit", long.startswith("x" * 10))
    check("...and SAYS it was cut", "TRUNCATED" in long)
    check("...and states the true total, so the reader knows how much is gone",
          "50 chars total" in long)
    check("...and names the way to get the rest", "args.limit" in long)
    # NOT a character count: the marker itself contains an "x" (gmail-inbox),
    # which is how this assertion first failed against correct code.
    check("the marker is appended, never substituted for the content",
          long.split("\n\n")[0] == "x" * 10)
    print()
    if failures:
        print("FAILURES: %d" % len(failures))
        return 1
    print("ALL PASS")
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1].strip() == "selftest":
        raise SystemExit(selftest())
    m = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=60)
    m.login(FROM_EMAIL, FROM_PASS)
    m.select("INBOX", readonly=True)
    # Force Gmail to report the CURRENT mailbox state before we search. A fresh
    # IMAP connection occasionally returns a stale/partial snapshot — seen
    # 2026-07-29: a peek returned 19 of 77 messages and missed a reply that had
    # just arrived. noop() flushes pending EXISTS updates; if the first ALL
    # search still comes back short of the authoritative STATUS count, re-select
    # once so we never report a partial inbox.
    m.noop()
    try:
        st = m.status("INBOX", "(MESSAGES)")[1][0].decode()
        authoritative = int(re.search(r"MESSAGES\s+(\d+)", st).group(1))
        if len(m.search(None, "ALL")[1][0].split()) < authoritative:
            m.close(); m.select("INBOX", readonly=True); m.noop()
    except Exception:
        pass
    if len(sys.argv) > 1 and sys.argv[1].strip():
        limit = 3000
        if len(sys.argv) > 2 and sys.argv[2].strip().isdigit():
            limit = max(500, min(int(sys.argv[2].strip()), 200000))
        show_bodies(m, sys.argv[1].strip(), limit)
        m.logout(); return
    total = len(m.search(None, "ALL")[1][0].split())
    unseen_ids = m.search(None, "UNSEEN")[1][0].split()
    print(f"INBOX {FROM_EMAIL}: {total} total message(s), {len(unseen_ids)} unseen")
    if total == 0:
        print("  (empty)"); m.logout(); return
    unseen_set = set(unseen_ids)
    ids = m.search(None, "ALL")[1][0].split()
    print("Most recent (newest first) — [*]=unseen:")
    for i in reversed(ids[-25:]):
        typ, md = m.fetch(i, "(BODY.PEEK[HEADER.FIELDS (DATE FROM SUBJECT)])")
        raw = b"".join(p[1] for p in md if isinstance(p, tuple))
        msg = email.message_from_bytes(raw)
        flag = "*" if i in unseen_set else " "
        print(f"  [{flag}] {dec(msg.get('Date',''))[:22]:22} | "
              f"{dec(msg.get('From',''))[:38]:38} | {dec(msg.get('Subject',''))[:58]}")
    m.logout()


if __name__ == "__main__":
    main()
