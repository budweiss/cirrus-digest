"""hoa_lead_gate.py -- what may reach Bill's HOA report as an opportunity (S635).

Pure functions, no network, no imports beyond the stdlib, so the daily
discovery (hoa_daily_research.run_discovery) and the Monday digest
(entity_kb_weekly_digest.compose_digest) apply the SAME rules.

Why it exists. The 2026-11-16 HOA report listed "State of Delaware (GSS)" as an
HOA opportunity: two state snow-removal documents on bidcondocs.delaware.gov,
one an award notice for a contract that runs to 2027, one a 2014 solicitation.
The summary said "garbled binary but clearly a snow removal bid". Bill took it
for a live RFP and asked about it. Three rules, each its own function:

  1. agency_procurement_reason -- a state or public agency's procurement
     document is never an HOA finding, whatever the screen called it.
  2. unreadable_source_reason -- a finding whose own text admits nobody could
     read the source ("garbled", "binary", "unreadable") is a guess from a
     file name; it never reaches the report.
  3. solicitation_status -- a solicitation is an opportunity only while it is
     open: an award notice, an addendum, or a document past its deadline is
     not, and neither is one whose deadline we could not find.
"""
import re
from datetime import date, datetime
from urllib.parse import urlparse

# ── 1. public-agency procurement ─────────────────────────────────────────────
# Hosts whose documents are government procurement by definition. bidcondocs is
# where Delaware state agencies post bid and contract files; mmp is the state's
# bid listing. The national portals list public-agency bids, not HOA ones.
PROCUREMENT_HOSTS = (
    "bidcondocs.delaware.gov", "mmp.delaware.gov", "contracts.delaware.gov",
    "gss.omb.delaware.gov", "demandstar.com", "bidnetdirect.com", "bidnet.com",
    "publicpurchase.com", "bidsync.com", "sam.gov",
)
# A procurement path on any .gov host (a court docket or a registry is not).
_GOV_PROCUREMENT_PATH_RX = re.compile(
    r"(bid|rfp|rfq|itb|ifb|solicitation|procurement|purchasing|contract|award)",
    re.IGNORECASE)
# A community name that is really a public body. Checked against all 2,540
# entities in hoa_leads_bill on 2026-11-16: it matches only the two GSS/DSHA
# records this fix is about.
_PUBLIC_BODY_RX = re.compile(
    r"\b(state of delaware|delaware state|gss|general services|"
    r"office of management|omb|department|dept|division|agency|authority|"
    r"commission|deldot|school district|university|college|delaware tech|"
    r"dtcc|county government|county council|city of|town of|government|"
    r"municipal)\b", re.IGNORECASE)


def _host(url):
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def agency_procurement_reason(url, community=""):
    """Why this item is public-agency procurement, or None if it is not."""
    host = _host(url)
    for h in PROCUREMENT_HOSTS:
        if host == h or host.endswith("." + h):
            return f"public procurement site ({h})"
    if host.endswith(".gov") and _GOV_PROCUREMENT_PATH_RX.search(urlparse(url).path):
        return f"government procurement document ({host})"
    m = _PUBLIC_BODY_RX.search(community or "")
    if m:
        return f"names a public body, not an association ({m.group(1)})"
    return None


# ── 2. unreadable source ─────────────────────────────────────────────────────
# Extraction-failure language. "corrupt" alone is deliberately absent: "board
# accused of corruption" is a real distress finding.
_UNREADABLE_RX = re.compile(
    r"\b(garbled|binary|unreadable|illegible|scrambled|mojibake)\b|"
    r"\bnot (machine[- ])?readable\b|"
    r"\b(could ?n[o']t|can ?n[o']t|unable to|failed to) (be )?"
    r"(read|parse|extract|open|decode)|"
    r"\bno (readable|extractable|usable) text\b|"
    r"\bcorrupted (file|pdf|document|text)\b|"
    r"\b(text|content|snippet) (is |was )?(unavailable|missing|empty|not available)\b|"
    r"\bno snippet\b|\bencoding (error|issue|problem)\b",
    re.IGNORECASE)


def unreadable_source_reason(text):
    """Why this text shows the source was never actually read, or None."""
    m = _UNREADABLE_RX.search(text or "")
    return f"source was not readable ({m.group(0)})" if m else None


def readable_snippet(content, limit=600):
    """The snippet to screen. Raw PDF or other binary bytes decoded as text are
    replaced by a plain statement, so no screen ever paraphrases them."""
    content = content or ""
    head = content[:2000]
    if not head:
        return ""
    if head.lstrip().startswith("%PDF") or "\x00" in head:
        return "(no readable text: binary document)"
    odd = sum(1 for ch in head if ch == "�" or (ord(ch) < 32 and ch not in "\n\r\t"))
    if odd / len(head) > 0.05:
        return "(no readable text: binary document)"
    return content[:limit]


# ── 3. open solicitation ─────────────────────────────────────────────────────
_SOLICITATION_RX = re.compile(
    r"\b(rfp|rfq|itb|ifb|request for (proposals?|quotes?|quotations?|bids?)|"
    r"invitation (to|for) bids?|bid (request|solicitation|opportunity|package|"
    r"document)s?|solicitation|seeking (bids|proposals|quotes)|"
    r"snow (removal )?bid)\b", re.IGNORECASE)
SOLICITATION_KINDS = {"rfp", "bid-request"}

# "AN" / "ANrev" is the state's Award Notice suffix (GSS25270-SNOW_REMOVAL_ANrev.pdf).
_AWARD_FILE_RX = re.compile(r"(?:^|[-_ .])AN(?:rev\d*)?(?:[-_ .]|$)")
_AWARD_TEXT_RX = re.compile(
    r"\b(notice of (intent to )?award|award notice|awarded to|contract award|"
    r"award(ed)? (contract|vendor)s?)\b", re.IGNORECASE)
_ADDENDUM_RX = re.compile(r"\b(addendum|addenda)\b|(?:^|[-_ .])ADD\d", re.IGNORECASE)

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DATE_RX = re.compile(
    r"\b(?P<iso>(?P<iy>20\d\d)-(?P<im>\d\d)-(?P<id>\d\d))\b|"
    r"\b(?P<us>(?P<um>\d{1,2})/(?P<ud>\d{1,2})/(?P<uy>(?:20)?\d\d))\b|"
    r"\b(?P<txt>(?P<tm>jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\.?\s+"
    r"(?P<td>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<ty>20\d\d))\b", re.IGNORECASE)
_DEADLINE_RX = re.compile(
    r"\b(due|deadline|clos(e|es|ing)( date)?|submission|submit(ted)? (by|no later)|"
    r"no later than|must be received|received by|bid opening|opening date|"
    r"responses? due|proposals? due|bids? due)\b", re.IGNORECASE)


def _dates(text):
    out = []
    for m in _DATE_RX.finditer(text or ""):
        try:
            if m.group("iso"):
                d = date(int(m.group("iy")), int(m.group("im")), int(m.group("id")))
            elif m.group("us"):
                y = int(m.group("uy"))
                d = date(y + 2000 if y < 100 else y, int(m.group("um")), int(m.group("ud")))
            else:
                d = date(int(m.group("ty")), _MONTHS[m.group("tm")[:3].lower()], int(m.group("td")))
        except (ValueError, KeyError):
            continue
        out.append((m.start(), d))
    return out


def deadline_dates(text):
    """Dates that sit within 80 characters after a deadline word."""
    text = text or ""
    keys = [m.end() for m in _DEADLINE_RX.finditer(text)]
    return [d for pos, d in _dates(text) if any(0 <= pos - k <= 80 for k in keys)]


def is_solicitation(kind="", text=""):
    return (kind or "").strip().lower().replace("_", "-") in SOLICITATION_KINDS \
        or bool(_SOLICITATION_RX.search(text or ""))


def _as_date(today):
    if isinstance(today, datetime):
        return today.date()
    if isinstance(today, date):
        return today
    return datetime.strptime(str(today)[:10], "%Y-%m-%d").date()


def solicitation_status(url="", text="", today=None):
    """(status, detail) where status is one of
         open        -- a deadline on or after today was found
         award       -- an award notice, not a request
         addendum    -- an addendum to some solicitation, not one itself
         closed      -- every deadline found is past
         unverified  -- no deadline found, so it cannot be called open
    Only "open" may be presented as an opportunity."""
    today = _as_date(today or date.today())
    name = (urlparse(url or "").path.rsplit("/", 1)[-1]) if url else ""
    if _AWARD_FILE_RX.search(name) or _AWARD_TEXT_RX.search(text or ""):
        return "award", "award notice, not an open request"
    if _ADDENDUM_RX.search(name) or re.match(r"\s*addendum\b", text or "", re.IGNORECASE):
        return "addendum", "addendum, not a solicitation"
    due = deadline_dates(text)
    if due:
        last = max(due)
        if last >= today:
            return "open", f"responses due {last.isoformat()}"
        return "closed", f"deadline passed {last.isoformat()}"
    return "unverified", "no response deadline found"


# ── the one call both callers make ───────────────────────────────────────────
def withhold_reason(url="", community="", kind="", summary="", doc_text="", today=None):
    """None if this finding may be shown as an HOA opportunity, else why not."""
    reason = agency_procurement_reason(url, community) or unreadable_source_reason(summary)
    if reason:
        return reason
    if is_solicitation(kind, summary):
        status, detail = solicitation_status(url, " ".join([summary or "", doc_text or ""]), today)
        if status != "open":
            return f"solicitation not open ({detail})"
    return None
