#!/usr/bin/env python3
"""stock_digest_send — deliver a Stock Pickers digest to Buddy's Yahoo inbox.

The digests are produced by the desktop Stock Pickers sessions on Buddy's
Mac (stock-pickers/digests/, gitignored — picks/sizes never leave his
folder), which pipes ONE digest file's markdown on stdin. This script runs
ON CUMULUS (where the SMTP credentials live), composes via mailer — the
consolidated send path — and sends.

Buddy authorized this send target (2026-10-06): TO = his own
weiss_buddy@yahoo.com, so privacy is bounded by construction. Replies to the
digest go to the From address, which is the intake inbox this repo already
reads — Buddy's feedback is therefore picked up by the normal inbox peeks.

Modes: dry-run (default) composes and prints headers, sends NOTHING — an
external send is always inspected first. live sends with on_error="raise"
("it didn't arrive" must never look like "it did").

CLI guard: mode is whitelisted to exactly {dry-run, live}; anything else
exits non-zero BEFORE any send path is touched (T118 shape).
"""
import json
import re
import sys
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import mailer

PROJECT_DIR = Path(__file__).resolve().parent
CREDS_PATH = PROJECT_DIR / "config" / "credentials.json"
TO_EMAIL = "weiss_buddy@yahoo.com"
MODES = ("dry-run", "live")
PAPER_HOME = PROJECT_DIR / 'private' / 'stock-paper'


def report_html(md):
    """Small escaped renderer for our headings, paragraphs, links and tables."""
    from html import escape
    def inline(text):
        text = escape(text)
        text = re.sub(r'\[([^\]]+)\]\((https://[^\s)]+)\)', r'<a href="\2">\1</a>', text)
        return re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    out, paragraph, table = [], [], []
    def flush():
        if paragraph:
            out.append('<p>' + inline(' '.join(paragraph)) + '</p>')
            paragraph.clear()
        if table:
            out.append('<table cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:14px">')
            for index, row in enumerate(table):
                if all(re.fullmatch(r'[: -]+', cell) for cell in row):
                    continue
                tag = 'th' if index == 0 else 'td'
                out.append('<tr>' + ''.join('<' + tag + ' style="border:1px solid #d8dee4;text-align:left">' + inline(cell) + '</' + tag + '>' for cell in row) + '</tr>')
            out.append('</table>')
            table.clear()
    for line in md.splitlines():
        if re.fullmatch(r'<!-- stock-paper-report:[a-f0-9]{32} -->', line.strip()):
            continue
        if line.strip().startswith('|') and line.strip().endswith('|'):
            if paragraph:
                flush()
            table.append([cell.strip() for cell in line.strip().strip('|').split('|')])
            continue
        if table:
            flush()
        header = re.match(r'^(#{1,3})\s+(.+)$', line)
        if header:
            flush()
            level = len(header[1])
            out.append('<h' + str(level) + '>' + inline(header[2]) + '</h' + str(level) + '>')
        elif not line.strip():
            flush()
        elif line.startswith('- '):
            flush()
            out.append('<p>• ' + inline(line[2:]) + '</p>')
        else:
            paragraph.append(line)
    flush()
    return '<!doctype html><html><body style="font-family:Arial,sans-serif;line-height:1.55;color:#172333;max-width:1000px;margin:24px auto;padding:0 16px">' + '\n'.join(out) + '</body></html>'


def check_paper_report(md, at=None, home=None):
    """A letter must contain a recent, exact account section from the ledger."""
    at = at or datetime.now(timezone.utc)
    home = home or PAPER_HOME
    matches = re.findall(r'<!-- stock-paper-report:([a-f0-9]{32}) -->', md)
    if len(matches) != 1:
        raise ValueError('one_current_independent_account_report_required')
    report = json.loads((home / 'reports' / (matches[0] + '.json')).read_text())
    created = datetime.fromisoformat(report['created_at'].replace('Z', '+00:00'))
    age = (at - created).total_seconds()
    if not -5 <= age <= 1800:
        raise ValueError('paper_report_not_current_refresh_before_delivery')
    if report['date'] != at.astimezone(ZoneInfo('America/New_York')).date().isoformat():
        raise ValueError('paper_report_date_mismatch')
    if report.get('slot') not in ('am', 'pm'):
        raise ValueError('paper_report_needs_daily_slot')
    if (report.get('account_id') != 'independent-200k-v1' or report.get('report_id') != matches[0]
            or not report.get('account_section') or report['account_section'] not in md):
        raise ValueError('canonical_paper_account_section_missing_or_modified')
    from stock_paper_ledger import Ledger
    with Ledger(home / 'ledger.sqlite3') as ledger:
        trades = ledger.list_trades(10000)
        saved_snapshot = ledger.get_snapshot(report['report_id'])
        current = ledger.snapshot()
    if report.get('snapshot') != saved_snapshot:
        raise ValueError('paper_report_snapshot_does_not_match_ledger')
    if any(current.get(key) != saved_snapshot.get(key) for key in ('trade_count', 'last_trade_id', 'event_count', 'last_event_id')):
        raise ValueError('new_trade_after_snapshot_refresh_before_delivery')
    from stock_paper import account_section
    if report['account_section'] != account_section(saved_snapshot, trades, report['benchmark'], report['slot']):
        raise ValueError('canonical_paper_account_section_invalid')
    if any(datetime.fromisoformat(t['recorded_at'].replace('Z', '+00:00')) > created for t in trades):
        raise ValueError('new_trade_after_snapshot_refresh_before_delivery')
    return report


def deliver_once(md, creds, subject, report, send_fn=None, home=None):
    """Reserve one daily slot; an ambiguous SMTP outcome is never retried blindly."""
    import fcntl
    import hashlib
    import os
    home = home or PAPER_HOME
    folder = home / 'delivery'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt = folder / (report['date'] + '-' + report['slot'] + '.json')
    digest = hashlib.sha256(md.encode()).hexdigest()
    with (folder / 'send.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if receipt.exists():
            old = json.loads(receipt.read_text())
            if old['status'] == 'sent' and old['body_sha256'] == digest:
                print('already_sent_same_daily_report')
                return True
            raise ValueError('daily_slot_already_reserved_check_receipt_do_not_resend')
        record = {'status': 'sending_outcome_unknown_until_confirmed', 'body_sha256': digest,
                  'report_id': report['report_id'], 'reserved_at': datetime.now(timezone.utc).isoformat()}
        with receipt.open('x') as out:
            out.write(json.dumps(record))
        os.chmod(receipt, 0o600)
        sender = send_fn or mailer.send
        ok = sender(creds['outlook_email'], creds['outlook_password'], TO_EMAIL, subject, md,
                    html=report_html(md), creds=creds, on_error='raise', log=print)
        if not ok:
            raise ValueError('send_unconfirmed_receipt_requires_review')
        record.update(status='sent', sent_at=datetime.now(timezone.utc).isoformat())
        temporary = receipt.with_suffix('.tmp')
        temporary.write_text(json.dumps(record))
        os.chmod(temporary, 0o600)
        temporary.replace(receipt)
        return True


def send_verified_once(md, creds, subject):
    import fcntl
    # Shares the workflow's operation lock: no fills can slip between this
    # final snapshot check and SMTP submission.
    with (PAPER_HOME / 'operation.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = check_paper_report(md)
        return deliver_once(md, creds, subject, report)


def check_weekdays(md, now):
    """Refuse internally inconsistent calendar labels before any delivery."""
    from datetime import date
    weekdays = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
    months = {name: i for i, name in enumerate(
        ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'], 1)}
    text = md.replace('**', '').replace('__', '')
    header = re.search(r'\b(20\d{2})-(\d{2})-(\d{2})\b', text.split('\n', 1)[0])
    anchor = date(*map(int, header.groups())) if header else now.date()
    day = r'\b(Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)\.?[,]?\s+'
    month = r'(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+'
    dates = []
    for match in re.finditer(day + r'(20\d{2})-(\d{2})-(\d{2})\b', text, re.I):
        label, year, mon, num = match.groups()
        dates.append((label, date(int(year), int(mon), int(num))))
    for match in re.finditer(day + month + r'(\d{1,2})(?:,?\s+(20\d{2}))?\b', text, re.I):
        label, mon, num, year = match.groups()
        if year:
            value = date(int(year), months[mon[:3].lower()], int(num))
        else:
            # Nearby review dates can cross New Year; choose the nearest year.
            candidates = []
            for y in (anchor.year-1, anchor.year, anchor.year+1):
                try:
                    candidates.append(date(y, months[mon[:3].lower()], int(num)))
                except ValueError:
                    pass
            if not candidates:
                raise ValueError('invalid_calendar_date')
            value = min(candidates, key=lambda d: abs((d-anchor).days))
        dates.append((label, value))
    for label, value in dates:
        if weekdays.index(label[:3].lower()) != value.weekday():
            raise ValueError('calendar_weekday_mismatch: %s is %s' %
                             (value.isoformat(), value.strftime('%A')))


def load_digest(stream) -> str:
    text = stream.read()
    if not text.strip():
        raise ValueError("empty_digest")
    if len(text) > 120_000:
        raise ValueError("digest_too_large")
    return text


def build_message(md: str, now=None, creds: dict = None):
    """Compose the message the same way the scheduled digests would. Returns
    (mailer-message tuple, subject) so selftest can inspect without network."""
    from datetime import datetime
    now = now or datetime.now(ZoneInfo('America/New_York'))
    check_weekdays(md, now)
    subject = "Stock Pickers — %s (our paper portfolio)" % now.strftime("%Y-%m-%d %H:%M ET")
    creds = creds or {}
    return mailer.build(creds["outlook_email"], TO_EMAIL, subject, md, html=report_html(md), creds=creds), subject


def main(argv) -> int:
    args = argv[1:]
    if [a for a in args if a.startswith("--") and
            a.split("=", 1)[0] not in ("--mode",)]:
        print("stock_digest_send: unknown argument(s). Use --mode=dry-run|live; "
              "digest markdown arrives on stdin.", file=sys.stderr)
        return 2  # fail-closed CLI guard: a typo must not reach a send
    mode = "dry-run"
    for a in args:
        if a.startswith("--mode"):
            mode = a.split("=", 1)[1] if "=" in a else "dry-run"
    if mode not in MODES:
        print("stock_digest_send: need --mode with one of %s" % ", ".join(MODES),
              file=sys.stderr)
        return 2
    try:
        creds = json.loads(CREDS_PATH.read_text())
    except Exception as e:
        print("stock_digest_send: credentials unreadable (%s)" % type(e).__name__,
              file=sys.stderr)
        return 3
    if not creds.get("outlook_email") or not creds.get("outlook_password"):
        print("stock_digest_send: outlook_email/outlook_password unset",
              file=sys.stderr)
        return 3
    try:
        md = load_digest(sys.stdin)
        report = check_paper_report(md)
        (msg, recipients), subject = build_message(md, creds=creds)
    except Exception as e:
        print("stock_digest_send: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 4
    from_email = creds["outlook_email"]
    if mode == "dry-run":
        mailer.send(from_email, "", TO_EMAIL, subject, md, creds=creds,
                    html=report_html(md), dry_run=True)
        return 0
    ok = send_verified_once(md, creds, subject)
    return 0 if ok else 5


def selftest() -> int:
    """Hermetic: compose/headers/guards only — no network, no creds read."""
    import io
    import tempfile
    from datetime import datetime

    def check(name, cond):
        print("PASS" if cond else "FAIL", name)
        return bool(cond)

    results = []
    results.append(check("wrong_weekday_refused", _raises(
        check_weekdays, '# Thursday 2026-10-08\nReview Tuesday Oct 29', datetime(2026, 10, 8))))
    results.append(check("wrong_iso_weekday_refused", _raises(
        check_weekdays, '# Wednesday 2026-10-08', datetime(2026, 10, 8))))
    check_weekdays('# Thursday 2026-10-08\nReview Thu Oct 29; Thu Nov 5.', datetime(2026, 10, 8))
    check_weekdays('# Thursday 2026-12-31\nFriday Jan 1', datetime(2026, 12, 31))
    results.append(check("correct_calendar_and_year_boundary", True))
    results.append(check("empty_digest_refused",
                         (_raises(load_digest, io.StringIO("   ")))))
    md = "# digest\n\nBody row.\n"
    results.append(check("digest_loads", load_digest(io.StringIO(md)) == md))
    results.append(check("too_large_refused",
                         _raises(load_digest, io.StringIO("x" * 120_001))))
    m, subject = build_message(md, now=datetime(2026, 10, 6, 14, 45),
                               creds={"outlook_email": "feedback@cumulustask.com"})
    headers = (subject, m[0]["From"], m[0]["To"])
    results.append(check("subject_has_date",
                         "2026-10-06" in subject and "14:45" in subject))
    results.append(check("from_is_box_address",
                         "feedback@cumulustask.com" in headers[1]))
    results.append(check("to_is_buddy_yahoo", headers[2] == TO_EMAIL))
    results.append(check("body_preserved", md in m[0].as_string()))
    # CLI guards: an unknown flag or an unknown mode must exit non-zero BEFORE
    # any credential read — reorder-proof by construction, so assert it with
    # an unreadable credentials path via monkeypatched state.
    global CREDS_PATH
    real = CREDS_PATH
    CREDS_PATH = Path("/nonexistent/s377/credentials.json")
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        code = main(["stock_digest_send.py", "--weird"])
    results.append(check("unknown_flag_exits_nonzero", code != 0))
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        code = main(["stock_digest_send.py", "--mode=bogus"])
    results.append(check("bad_mode_exits_nonzero", code == 2))
    CREDS_PATH = real
    return 0 if all(results) else 1


def _raises(fn, *args):
    try:
        fn(*args)
        return False
    except Exception:
        return True


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in ("selftest", "--selftest"):
        sys.exit(selftest())
    sys.exit(main(sys.argv) or 0)
