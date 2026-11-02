#!/usr/bin/env python3
"""bill_sam_weekly.py (S554, 2026-11-02): Bill's Monday SAM.gov summary.

Bill asked on 11-02 for a weekly summary of new SAM.gov bids matching the search
we ran for him by hand on 11-01: snow, landscaping, lumber and pallets, nationwide,
with SDVOSB set-asides marked. Buddy approved building it, and a quiet week says so.

Deterministic, with no model calls. Data is SAM.gov's public daily extract, which
needs no account or key. A failed or stale pull fails the run and sends nothing:
"nothing new" must only ever mean a complete file was read.

Usage:
  bill_sam_weekly.py --dry-run             print the email; send and save nothing
  bill_sam_weekly.py --seed YYYY-MM-DD     mark notices posted up to that date seen
  bill_sam_weekly.py                       live: email Bill, cc Buddy, once a day
"""
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIGEST_DIR = HERE.parent
sys.path.insert(0, str(DIGEST_DIR))
OUT = HERE / "out"
STATE = OUT / "seen.json"

EXTRACT_URL = ("https://sam.gov/api/prod/fileextractservices/v1/api/download/"
               "Contract%20Opportunities/datagov/ContractOpportunitiesFullCSV.csv?privacy=Public")
TO = "whutchins@knightpropertysvs.com"
CC = "Buddy.Weiss@outlook.com"
JOB = "billsam"

REQUIRED = {"NoticeId", "Title", "Sol#", "Office", "PostedDate", "BaseType", "SetASideCode",
            "SetASide", "ResponseDeadLine", "NaicsCode", "ClassificationCode", "PopCity",
            "PopState", "PopCountry", "AwardNumber", "PrimaryContactFullname",
            "PrimaryContactEmail", "Link", "Description"}
MIN_ROWS = 20000          # the file held 69,635 on 11-02; far fewer means a truncated pull
MAX_FILE_AGE_DAYS = 5     # newest PostedDate; covers a weekend plus a slow extract
SEEN_KEEP_DAYS = 180      # a recurring snow contract should alert again next season
MAX_LISTED = 40

# PSC S208 landscaping/groundskeeping, S218 snow removal. NAICS 561730 landscaping.
SERVICE_NAICS = {"561730"}
SERVICE_PSC = {"S208", "S218"}
SERVICE_TITLE = re.compile(r"\b(snow|ice removal|de-?icing|landscap\w*|grounds? ?(maint\w*|keep\w*)"
                           r"|lawn|mowing|turf|sod)\b", re.I)
# Lumber and pallets. A bare "pallet" title is usually equipment (pallet jacks,
# aircraft pallets), so it needs the wood-pallet NAICS; see supply_match.
SUPPLY_NAICS = {"321113", "321912", "321920", "423310"}
SUPPLY_PSC = {"5510", "5520", "5530"}
SUPPLY_TITLE = re.compile(r"\b(lumber|wood(en)? pallets?)\b", re.I)

SDVOSB_CODES = {"SDVOSBC", "SDVOSBS", "VSA", "VSS"}
# Programs Bill's business is not known to qualify for.
EXCLUDED_CODES = {"8A", "8AN", "HZC", "HZS", "WOSB", "WOSBSS", "EDWOSB", "EDWOSBSS", "ISBEE", "IEE"}
DROP_TYPES = ("Award", "Justification", "Sale", "Intent")
US = {"", "USA", "US", "UNITED STATES"}
TIERED = re.compile(r"\btiered\b|cascad|first[- ]tier", re.I)


class DataError(Exception):
    """The file cannot support a true statement about what is new."""


def code(row):
    return re.sub(r'[\[\]"\s]', "", row.get("SetASideCode") or "").upper()


def category(row):
    """snow/landscaping or lumber/pallets, else None."""
    title, naics, psc = row["Title"], row["NaicsCode"].strip(), row["ClassificationCode"].strip().upper()
    if naics in SERVICE_NAICS or psc in SERVICE_PSC:
        return "snow/landscaping"
    if SERVICE_TITLE.search(title) and psc[:1] == "S":
        return "snow/landscaping"
    if naics in SUPPLY_NAICS or psc in SUPPLY_PSC or SUPPLY_TITLE.search(title):
        return "lumber/pallets"
    return None


def deadline(row):
    try:
        return datetime.fromisoformat(row["ResponseDeadLine"].strip()[:19])
    except ValueError:
        return None


def posted(row):
    return row["PostedDate"].strip()[:10]


def eligible(row, now):
    """Could Bill's business act on this notice now?"""
    if row["BaseType"].startswith(DROP_TYPES) or row["AwardNumber"].strip():
        return False
    if code(row) in EXCLUDED_CODES or row["PopCountry"].strip().upper() not in US:
        return False
    due = deadline(row)
    return due is None or due >= now


def keys(row):
    """Each amendment is its own row, and SAM.gov sometimes rewrites the
    solicitation number, so a notice is known by either identity."""
    sol = re.sub(r"[_-]\d+$", "", re.sub(r"\s", "", row["Sol#"]).upper())
    title = re.sub(r"^[a-z]\d{3}--", "", row["Title"].strip().lower())
    title = re.sub(r"[^a-z0-9]+", " ", title).strip()[:60]
    # Place is in the key: one office can run separate snow contracts, and a
    # repeated line costs far less than a lost lead.
    place = "%s,%s" % (row["PopCity"].strip().lower(), row["PopState"].strip().lower())
    named = "t:%s|%s|%s" % (row["Office"].strip().lower(), place, title)
    return (["s:" + sol] if sol else []) + ([named] if title else ["n:" + row["NoticeId"]])


def fetch(path, url=EXTRACT_URL, attempts=3, wait=60):
    """Download the extract to path. Raises DataError when every attempt fails."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for n in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        return
                    f.write(chunk)
        except OSError as e:
            print("download attempt %d failed: %s" % (n, type(e).__name__))
            if n < attempts:
                time.sleep(wait)
    raise DataError("download failed after %d attempts" % attempts)


def read_extract(path, now=None):
    """(matching rows newest first, rows read, newest posted date). Raises DataError."""
    now = now or datetime.now()
    csv.field_size_limit(10 ** 8)
    matched, total, newest = [], 0, ""
    with open(path, encoding="cp1252", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED - set(reader.fieldnames or ())
        if missing:
            raise DataError("extract changed shape, missing " + ", ".join(sorted(missing)))
        for row in reader:
            total += 1
            newest = max(newest, posted(row))
            if category(row):
                matched.append(row)
    if total < MIN_ROWS:
        raise DataError("extract held only %d rows" % total)
    if not newest:
        raise DataError("extract has no posted dates")
    age = (now - datetime.fromisoformat(newest)).days
    if age > MAX_FILE_AGE_DAYS:
        raise DataError("extract is stale, newest notice %s" % newest)
    matched.sort(key=posted, reverse=True)
    return matched, total, newest


def load_seen(path=None):
    path = Path(path or STATE)
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_seen(seen, now, path=None):
    path = Path(path or STATE)
    path.parent.mkdir(parents=True, exist_ok=True)
    cutoff = (now - timedelta(days=SEEN_KEEP_DAYS)).strftime("%Y-%m-%d")
    keep = {k: v for k, v in seen.items() if v >= cutoff}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(keep))
    os.replace(tmp, path)


def new_notices(matched, seen, now):
    """(new eligible notices, the keys of everything read). Newest row wins, so an
    amended deadline replaces the old one."""
    fresh, done, today = [], set(), now.strftime("%Y-%m-%d")
    touched = {}
    for row in matched:
        ks = keys(row)
        known = any(k in seen or k in done for k in ks)
        done.update(ks)
        for k in ks:
            touched[k] = today
        if not known and eligible(row, now):
            fresh.append(row)
    return fresh, touched


def _when(row):
    due = deadline(row)
    if not due:
        return "none listed"
    zone = row["ResponseDeadLine"].strip()[19:]
    return due.strftime("%b %d, %-I:%M %p") + (" (UTC%s)" % zone if zone else "")


def _item(row):
    place = ", ".join(x for x in (row["PopCity"].strip(), row["PopState"].strip()) if x) or "not listed"
    lines = ["- %s" % " ".join(row["Title"].split())[:120],
             "  %s | Deadline: %s | %s" % (place, _when(row), row["BaseType"].strip()),
             "  Set-aside: %s" % (row["SetASide"].strip() or "none listed")]
    contact = " ".join(x for x in (row["PrimaryContactFullname"].strip(),
                                   row["PrimaryContactEmail"].strip()) if x)
    if contact:
        lines.append("  Contact: " + contact)
    if TIERED.search(row["Description"]):
        lines.append("  The notice mentions tiered competition; check whether it is exclusively SDVOSB.")
    lines.append("  " + row["Link"].strip())
    return "\n".join(lines)


def _section(title, rows):
    if not rows:
        return "%s: none new this week.\n" % title
    rows = sorted(rows, key=lambda r: deadline(r) or datetime.max)
    shown = "\n\n".join(_item(r) for r in rows[:MAX_LISTED])
    more = len(rows) - MAX_LISTED
    tail = "\n\n...and %d more on SAM.gov." % more if more > 0 else ""
    return "%s (%d):\n\n%s%s\n" % (title, len(rows), shown, tail)


def compose(fresh, total, newest, now):
    """(subject, body). Plain text only; every value is public SAM.gov data."""
    day = now.strftime("%b %d")
    sd = [r for r in fresh if code(r) in SDVOSB_CODES]
    other = [r for r in fresh if code(r) not in SDVOSB_CODES]
    wood = sum(1 for r in fresh if category(r) == "lumber/pallets")
    footer = ("Search: SAM.gov's public daily data file, %s notices, newest posted %s. Snow, "
              "landscaping, lumber and pallets, nationwide; 8(a), HUBZone and women-owned "
              "set-asides are left out. Deadlines come from that file and can lag an amendment, "
              "so confirm each one on the notice. No bid has been submitted and no contracting "
              "officer has been contacted.\n\n-- CUMULUS" % (format(total, ","), newest))
    if not fresh:
        return ("SAM.gov opportunities - nothing new this week (%s)" % day,
                "Bill,\n\nNothing new this week: no new SAM.gov opportunities matched your search.\n\n"
                + footer)
    subject = "SAM.gov opportunities - %d new this week, %d SDVOSB (%s)" % (len(fresh), len(sd), day)
    body = ["Bill,\n\nNew on SAM.gov since last week: %d, of which %d are SDVOSB or veteran-owned "
            "set-asides and %d are lumber or pallets.\n" % (len(fresh), len(sd), wood),
            _section("SDVOSB and veteran-owned set-asides", sd),
            _section("Small business set-asides and open competition", other), footer]
    return subject, "\n".join(body)


def note_samples():
    """Every note this job writes and how its rule must read it: (label, note,
    productive|zero|blind) for runner/rule_note_lint.py."""
    swept = "swept 69635 notices, newest 2026-11-08"
    return [("a week with new notices", "sent 3 new, " + swept, "productive"),
            ("a quiet week", "quiet week sent: 0 new, " + swept, "zero"),
            ("a stale data file", "failed: extract is stale, newest notice 2026-10-01", "blind"),
            ("a failed send", "failed: send did not complete, swept 69635 notices", "blind")]


def _record(ok, note):
    import job_status
    job_status.record("billsam", ok, note)


def _send(subject, body):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(body)
    try:
        r = subprocess.run([sys.executable, str(DIGEST_DIR / "send_bid_email.py"), TO, subject, tf.name],
                           cwd=str(DIGEST_DIR), capture_output=True, text=True,
                           env=dict(os.environ, CC_EMAIL=CC))
    finally:
        os.unlink(tf.name)
    print(r.stdout + r.stderr)
    return r.returncode == 0


def run(extract_path, dry=False, now=None, state=None, send=None, record=None, guard=None):
    """One pass over a downloaded extract. Returns a process exit code."""
    now = now or datetime.now()
    send = send or _send
    record = record or _record
    if guard is None:
        import send_guard as guard
    if not dry and guard.already_sent_today(JOB):
        print(guard.blocked_message(JOB, guard.already_sent_today(JOB)))
        return 0
    try:
        matched, total, newest = read_extract(extract_path, now)
    except DataError as e:
        print("FAILED:", e)
        if not dry:
            record(False, "failed: %s" % e)
        return 1
    seen = load_seen(state)
    fresh, touched = new_notices(matched, seen, now)
    subject, body = compose(fresh, total, newest, now)
    if dry:
        print("DRY RUN, nothing sent or saved\nSubject: %s\n\n%s" % (subject, body))
        return 0
    if not send(subject, body):
        record(False, "failed: send did not complete, swept %d notices" % total)
        return 1
    # Stamp first: a crash after this cannot mail Bill twice.
    if not guard.mark_sent(JOB, subject):
        print("WARNING: send stamp not written; a restart could re-send.")
    save_seen({**seen, **touched}, now, state)
    swept = "swept %d notices, newest %s" % (total, newest)
    record(True, "sent %d new, %s" % (len(fresh), swept) if fresh else "quiet week sent: 0 new, " + swept)
    return 0


def seed(extract_path, through, now=None, state=None):
    """Mark notices posted on or before `through` as already seen. Sends nothing."""
    now = now or datetime.now()
    matched, _, _ = read_extract(extract_path, now)
    today = now.strftime("%Y-%m-%d")
    seen = load_seen(state)
    n = 0
    for row in matched:
        if posted(row) <= through:
            n += 1
            for k in keys(row):
                seen[k] = today
    save_seen(seen, now, state)
    return n


def main(argv):
    OUT.mkdir(parents=True, exist_ok=True)
    extract = OUT / "extract.csv"
    dry = "--dry-run" in argv
    try:
        fetch(extract)
    except DataError as e:
        print("FAILED:", e)
        if not dry:
            _record(False, "failed: %s" % e)
        return 1
    try:
        if "--seed" in argv:
            through = argv[argv.index("--seed") + 1]
            datetime.fromisoformat(through)
            print("seeded %d notices posted through %s" % (seed(extract, through), through))
            return 0
        return run(extract, dry=dry)
    finally:
        extract.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
