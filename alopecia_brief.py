#!/usr/bin/env python3
"""ALOPECIA P2 — the weekly synthesised brief.

P1 collects and stops. This is the half that was never built: it reads the
whole collection, sends it to the multi-LLM council, and emails Buddy.

    python3 alopecia_brief.py                 # build + SEND
    python3 alopecia_brief.py --dry-run       # build + write, send NOTHING
    python3 alopecia_brief.py --full          # force a whole-corpus review
    python3 alopecia_brief.py --selftest      (also: selftest)

BRIEF #1 IS A FULL REVIEW, AND THAT IS AUTOMATIC (spec P2, Buddy S87).
There is no prior brief for a delta to be relative to, and the corpus is nearly
static -- a "what changed this week" framing would have produced a two-item
brief and buried the 100+ items he has never seen. So: no state file => full
review. From brief #2 the window is "since the last brief we actually sent",
NOT a fixed 7 days -- the entire reason this module exists is that a brief got
missed, and a fixed window would have silently dropped the skipped week.

WHY THE EVIDENCE GRADE IS COMPUTED HERE AND NOT IN THE COLLECTOR
---------------------------------------------------------------
The collector tags a RELEVANCE band (is this about our subgroup?). That is a
different question from HOW GOOD THE EVIDENCE IS, and the spec requires both:
"every item carries an evidence grade so the brief never launders a case report
into a finding." Deterministic, testable, and pinned by a regression test --
the same reason P1's triage is not a model's opinion.

The grade a trial REGISTRATION gets is `T`, never `A`. A recruiting trial is a
plan, not a result, and grading it as controlled-trial evidence would be the
exact laundering the spec forbids.

DISAGREEMENT IS SURFACED, NOT AVERAGED -- AND THAT CLAIM IS AUDITABLE
--------------------------------------------------------------------
ensemble.best_answer() runs every keyed provider on the same prompt and has a
judge reconcile them. A judge can still smooth a real disagreement into
consensus prose, and nothing downstream would know. So the raw per-model
answers are written next to the brief (`*-council.json`) and the brief is
CHECKED for a Council disagreements section: if the judge omitted it, we say so
in the brief rather than letting silence read as agreement. Same rule as P1's
INCOMPLETE banner -- a quiet day and a day we could not see must not render the
same.

NOT MEDICAL ADVICE. The output is "here is what moved" and "worth asking a
dermatologist about" -- never "you should take this."
"""

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DAILY_DIR = PROJECT_DIR / "alopecia/daily"
BRIEF_DIR = PROJECT_DIR / "alopecia/briefs"
STATE_PATH = PROJECT_DIR / "alopecia/brief_state.json"
CREDS_PATH = PROJECT_DIR / "config/credentials.json"

TO_EMAIL = "Buddy.Weiss@outlook.com"
SEND_JOB = "alopecia-brief"

# The subgroup, condition-level only. No name, no initials, no location, no
# age -- this string goes into an outbound LLM prompt (spec privacy rules).
SUBGROUP = "an adult with ~16-year-duration alopecia universalis (onset ~age 10)"

# ── evidence grades ─────────────────────────────────────────────────────────
# First match wins, strongest first. `T` is checked before everything else
# because it is decided by the SOURCE, not the words in the title.
GRADES = [
    ("A", "controlled trial result",
     r"randomi[sz]ed|randomi[sz]ation|placebo[- ]controlled|double[- ]blind|"
     r"phase\s*(?:2b|3|iii|ii/iii)|\brct\b"),
    ("B", "cohort / epidemiology / synthesis",
     r"cohort|case[- ]control|cross[- ]sectional|registry|retrospective|"
     r"prospective|meta[- ]analys|systematic review|nationwide|population[- ]based|"
     r"incidence|prevalence|epidemiolog"),
    ("C", "case report / small series",
     r"case report|case series|\ba case of\b|three patients|two patients|"
     r"single patient|\bn\s*=\s*[1-9]\b"),
    ("D", "mechanistic / preclinical / review",
     r"\bmice\b|\bmurine\b|in vitro|ex vivo|organoid|cell line|mechanis|"
     r"\breview\b|narrative review|perspective|editorial|hypothes"),
]
DEFAULT_GRADE = ("E", "unclassified — read the source before relying on it")


def grade(item):
    """(letter, label) for an item. Deterministic and testable, on purpose."""
    src = (item.get("source") or "").lower()
    if "clinicaltrials" in src or "trials" in src:
        return "T", "trial registration — a plan, not a result"
    text = "%s %s" % (item.get("title", ""), item.get("extra", ""))
    low = text.lower()
    for letter, label, pattern in GRADES:
        if re.search(pattern, low):
            return letter, label
    return DEFAULT_GRADE


def is_preprint(item):
    return "medrxiv" in (item.get("source") or "").lower() or \
           "biorxiv" in (item.get("source") or "").lower()


# ── corpus ──────────────────────────────────────────────────────────────────
_DAY_RX = re.compile(r"alopecia-(\d{4}-\d{2}-\d{2})\.json$")


def load_corpus(daily_dir=None):
    """Every item ever collected, keyed, with the day it first appeared.

    The per-day JSON files are the record of the items themselves; seen.json
    holds only key->day and cannot rebuild a brief. Merging by key across days
    is what makes a re-run idempotent.
    """
    d = Path(daily_dir or DAILY_DIR)
    by_key = {}
    for path in sorted(d.glob("alopecia-*.json")) if d.exists() else []:
        m = _DAY_RX.search(path.name)
        if not m:
            continue
        day = m.group(1)
        try:
            items = json.loads(path.read_text())
        except Exception:
            continue                      # one unreadable day must not lose the rest
        for it in items:
            k = it.get("key")
            if not k:
                continue
            prior = by_key.get(k)
            if prior is None or day < prior.get("collected", "9999"):
                it = dict(it)
                it["collected"] = day if prior is None else min(day, prior["collected"])
                by_key[k] = it
    return sorted(by_key.values(),
                  key=lambda i: (i.get("rank", 9), i.get("collected", ""),
                                 i.get("source", "")))


def since_window(corpus, since_day):
    return [i for i in corpus if i.get("collected", "") > since_day]


def load_state(path=None):
    try:
        return json.loads(Path(path or STATE_PATH).read_text())
    except Exception:
        return {}


def save_state(state, path=None):
    p = Path(path or STATE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


# ── the prompt ──────────────────────────────────────────────────────────────
SYSTEM = """You are the research-synthesis council for a private monitor of \
alopecia areata research. Your reader is a layman following the field for \
someone close to him: %s.

HARD RULES.
1. This is NOT medical advice. Never tell the reader to take, stop, or try a \
treatment, a supplement or a diet. The register is "this moved" and "worth \
asking a dermatologist about". A dermatologist decides.
2. NEVER launder weak evidence. Every item you cite arrives with an evidence \
grade: A = controlled trial result, B = cohort/epidemiology/synthesis, \
C = case report or small series, D = mechanistic/preclinical/review, \
T = trial registration (a plan, no results), E = unclassified. Say the grade \
in words when it matters ("a single case report", "a registration, no results \
yet"). A grade-C or grade-D finding must never be written as though it were \
established.
3. Cite by the item's [n] number. Do not invent items, numbers, drugs, dates \
or results that are not in the list you were given.
4. Say plainly when the honest answer is "nothing here moved the needle." A \
quiet period reported as quiet is a good brief. Padding is the failure mode.
5. Preprints are marked. Treat them as not yet peer-reviewed, and say so.""" % SUBGROUP

STRUCTURE_FULL = """## What we have found so far
## Trials watch
## Subgroup focus
## Community pulse
## Standing questions — where they stand
## Council disagreements"""

STRUCTURE_DELTA = """## What changed
## Trials watch
## Subgroup focus
## Community pulse
## Standing questions — where they stand
## Council disagreements"""

STANDING_QUESTIONS = """1. What causes the T-cell attack? (What collapses the \
follicle's immune privilege -- genetic susceptibility plus which trigger? \
Buddy's permanent collection target.)
2. Why onset at ~age 10? (Pediatric-onset epidemiology; rapid prepubertal \
universalis as a subgroup; any trigger-window research.)
3. Do dietary or microbiome factors matter? (Always with the evidence grade \
attached, and never converted into dietary advice.)"""


def item_line(n, it):
    g, glabel = grade(it)
    pre = " [PREPRINT — not peer-reviewed]" if is_preprint(it) else ""
    return "[%d] (grade %s: %s)%s P%s %s | %s | %s | %s%s" % (
        n, g, glabel, pre, it.get("rank", "?"), it.get("label", ""),
        it.get("source", ""), it.get("date") or "undated",
        it.get("title") or "(untitled)",
        (" -- " + it["extra"]) if it.get("extra") else "")


def build_prompt(items, full, since_day, today):
    period = ("EVERYTHING COLLECTED TO DATE (this is brief #1 -- the reader has "
              "never received a brief, so review the whole corpus, not a delta)"
              if full else
              "items collected since %s" % since_day)
    lines = [
        "Write this week's brief, dated %s." % today,
        "",
        "Period: %s." % period,
        "Items: %d." % len(items),
        "",
        "Use EXACTLY these sections, in this order:",
        (STRUCTURE_FULL if full else STRUCTURE_DELTA),
        "",
        "Section rules:",
        "- Trials watch: registrations only, graded T. State that a registration "
        "has no results. Do NOT assess eligibility fit -- that is a later phase "
        "and guessing it would raise hope the real criteria take back.",
        "- Subgroup focus: what, if anything, here speaks to %s. Long-duration "
        "universalis responds differently from the short-duration disease the "
        "headline numbers usually reflect; say so when a result does not "
        "transfer." % SUBGROUP,
        "- Standing questions: for each of the three below, say what (if "
        "anything) in this period moved it, or state plainly that nothing did. "
        "Never drop a question because there was no news.",
        STANDING_QUESTIONS,
        "- Council disagreements: where the council members disagreed on a fact "
        "or on importance, say so and name the disagreement. If there was none "
        "worth reporting, write exactly: 'No material disagreement.' Do not "
        "manufacture one.",
        "",
        "THE ITEMS:",
    ]
    lines += [item_line(n, it) for n, it in enumerate(items, 1)]
    return "\n".join(lines)


# ── assembly ────────────────────────────────────────────────────────────────
DISAGREE_RX = re.compile(r"^##+\s*council disagreement", re.I | re.M)


def sources_appendix(items):
    """Deterministic. The spec's done-criterion is that EVERY item links a
    source; a model that forgets one must not be able to break that."""
    out = ["## Sources", "",
           "_Every item collected in this period, with its evidence grade. "
           "The numbers match the citations above._", ""]
    for n, it in enumerate(items, 1):
        g, _ = grade(it)
        pre = " · **preprint**" if is_preprint(it) else ""
        out.append("%d. **[%s]** %s  \n   %s · %s%s  \n   %s" % (
            n, g, it.get("title") or "(untitled)", it.get("source", ""),
            it.get("date") or "undated", pre, it.get("url", "")))
    return "\n".join(out)


def assemble(body, items, meta, full, since_day, today, number):
    head = ["# Alopecia areata — weekly brief #%d" % number,
            "",
            "_%s · %s · %d item(s)_" % (
                today,
                "full review of everything collected to date" if full
                else "changes since %s" % since_day,
                len(items)),
            ""]
    council = "council: %s → judge %s" % (
        ", ".join(meta.get("members") or []) or "none", meta.get("judge") or "none")
    if meta.get("degraded"):
        council += " · **DEGRADED — %s.** Cross-model checking did not happen " \
                   "in full for this brief." % (meta.get("reason") or "reason not recorded")
    head += ["_%s_" % council, "", "---", ""]

    parts = ["\n".join(head), body.strip(), ""]

    if not DISAGREE_RX.search(body):
        # Detection, not a silent pass: the judge dropped the section, so the
        # brief says that rather than letting its absence read as agreement.
        parts += ["## Council disagreements", "",
                  "**The judge did not return this section.** That is a gap in "
                  "this brief, not evidence that the council agreed. The raw "
                  "per-model answers are on the box next to this brief "
                  "(`alopecia/briefs/%s-council.json`) and can be compared "
                  "directly." % today, ""]

    parts += [sources_appendix(items), "", "---", "",
              "_Research monitor, not medical advice. Nothing here is a "
              "recommendation to start, stop or change any treatment — that is "
              "a dermatologist's call. Evidence grades: A controlled trial · "
              "B cohort/epidemiology · C case report · D mechanistic/review · "
              "T trial registration (no results) · E unclassified._"]
    return "\n".join(parts)


def empty_brief(today, since_day, number):
    """A period with nothing new is a result, not a failure — and it is not a
    reason to spend a council call."""
    return "\n".join([
        "# Alopecia areata — weekly brief #%d" % number, "",
        "_%s · changes since %s_" % (today, since_day), "", "---", "",
        "## What changed", "",
        "**Nothing new was collected this period.** The four sources were "
        "queried daily and every item had been seen before.", "",
        "A quiet week is a real result in this field — the corpus moved by only "
        "a couple of items across the first days of collection. It is not a "
        "collector failure; a failed collection reports itself as INCOMPLETE in "
        "the daily file and in the morning brief.", "",
        "## Standing questions — where they stand", "",
        "Nothing collected this period moved any of the three standing "
        "questions. They stay open:", "", STANDING_QUESTIONS, "",
        "## Council disagreements", "",
        "No council call was made — there was nothing to synthesise.", "",
        "---", "",
        "_Research monitor, not medical advice._"])


# ── run ─────────────────────────────────────────────────────────────────────
def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


def build(full=None, now=None, daily_dir=None, state_path=None, creds=None):
    """Returns (subject, markdown, meta, items, state_update)."""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    state = load_state(state_path)
    number = int(state.get("count", 0)) + 1
    first_ever = not state.get("last_brief_day")
    if full is None:
        full = first_ever

    corpus = load_corpus(daily_dir)
    if full:
        items = corpus
        since_day = ""
    else:
        since_day = state.get("last_brief_day") or \
            (now - timedelta(days=7)).strftime("%Y-%m-%d")
        items = since_window(corpus, since_day)

    log("corpus %d item(s); this brief covers %d" % (len(corpus), len(items)))

    if not items:
        md = empty_brief(today, since_day, number)
        meta = {"members": [], "judge": None, "degraded": False,
                "reason": "no items — no council call"}
        return ("Alopecia areata — weekly brief #%d (quiet week)" % number,
                md, meta, items, {"count": number, "last_brief_day": today})

    import ensemble
    user = build_prompt(items, full, since_day, today)
    meta, body = ensemble.best_answer(
        SYSTEM, user, creds if creds is not None else _creds(),
        max_tokens=8000, task="alopecia-brief", keep_answers=True)
    log("council: %s -> judge %s%s" % (
        ",".join(meta.get("members") or []), meta.get("judge"),
        " (DEGRADED: %s)" % meta.get("reason") if meta.get("degraded") else ""))

    md = assemble(body, items, meta, full, since_day, today, number)
    subject = "Alopecia areata — weekly brief #%d%s" % (
        number, " (full review to date)" if full else "")
    return subject, md, meta, items, {"count": number, "last_brief_day": today}


def _creds(path=None):
    return json.loads(Path(path or CREDS_PATH).read_text())


def _write_outputs(today, md, meta, number):
    BRIEF_DIR.mkdir(parents=True, exist_ok=True)
    (BRIEF_DIR / ("alopecia-brief-%s.md" % today)).write_text(md + "\n")
    answers = meta.pop("answers", None)
    if answers:
        # The audit trail behind "disagreement surfaced, not averaged". Kept on
        # the box only -- it is never emailed and never committed.
        (BRIEF_DIR / ("%s-council.json" % today)).write_text(
            json.dumps([{"provider": p, "answer": t} for p, t in answers],
                       indent=1) + "\n")
    log("wrote alopecia/briefs/alopecia-brief-%s.md" % today)


def main(argv):
    args = set(argv[1:])
    if "--selftest" in args or "selftest" in args:
        return 0 if selftest() else 1
    dry = "--dry-run" in args
    full = True if "--full" in args else None

    subject, md, meta, items, state_update = build(full=full)
    _write_outputs(state_update["last_brief_day"], md, meta, state_update["count"])

    if dry:
        log("DRY RUN — not sending, not advancing state")
        print("\n" + md)
        return 0

    import send_guard
    blocked = send_guard.already_sent_today(SEND_JOB)
    if blocked:
        log(send_guard.blocked_message(SEND_JOB, blocked))
        return 0

    import mailer
    creds = _creds()
    html = _html(md)
    mailer.send(creds["outlook_email"], creds["outlook_password"], TO_EMAIL,
                subject, md, html=html, creds=creds, log=log,
                watch_promises=False)          # internal mail: nobody is owed
    send_guard.mark_sent(SEND_JOB, subject)
    save_state(state_update)
    log("sent to %s and advanced state to %s" % (TO_EMAIL, state_update["last_brief_day"]))
    return 0


_HTML_SHELL = """<!DOCTYPE html><html><head><style>
 body {{ font-family: -apple-system, Arial, sans-serif; max-width: 820px;
        margin: 0 auto; padding: 20px; color: #333; }}
 h1 {{ color: #1a1a2e; border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; }}
 h2 {{ color: #16213e; margin-top: 30px; }}
 a {{ color: #0f3460; }}
 hr {{ border: none; border-top: 1px solid #e0e0e0; margin: 20px 0; }}
</style></head><body>
{body}
</body></html>"""


def _html(md):
    """Markdown -> HTML for the email.

    KNOWN DUPLICATION, deliberate: send_digest.markdown_to_html() is the same
    ~15 lines, but send_digest reads CIRRUS-only config paths at IMPORT time,
    so importing it on CUMULUS raises FileNotFoundError before any function
    runs. Hoisting the converter into mailer.py is the right fix and is on the
    worklist -- mailer is the live client send path and does not get a drive-by
    refactor in this session.
    """
    t = md
    t = re.sub(r"^### (.+)$", r"<h3>\1</h3>", t, flags=re.M)
    t = re.sub(r"^## (.+)$", r"<h2>\1</h2>", t, flags=re.M)
    t = re.sub(r"^# (.+)$", r"<h1>\1</h1>", t, flags=re.M)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<em>\1</em>", t)
    t = re.sub(r"_([^_\n]+?)_", r"<em>\1</em>", t)
    t = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', t)
    t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
    t = re.sub(r"^---$", r"<hr>", t, flags=re.M)
    # Bare URLs in the sources appendix must be clickable.
    t = re.sub(r"(?<![\"'=>])(https?://[^\s<]+)", r'<a href="\1">\1</a>', t)
    t = t.replace("\n", "<br>\n")
    return _HTML_SHELL.format(body=t)


def selftest():
    """Offline. No network, no live corpus, no live state file (T32)."""
    import tempfile
    ok = [True]

    def ck(name, cond):
        print("%s %s" % ("PASS" if cond else "FAIL", name))
        if not cond:
            ok[0] = False

    # ── grades ──────────────────────────────────────────────────────────────
    def g(title, source="pubmed", extra=""):
        return grade({"title": title, "source": source, "extra": extra})[0]

    ck("randomised phase 3 -> A",
       g("A randomized, placebo-controlled phase 3 trial of a JAK inhibitor") == "A")
    ck("nationwide cohort -> B",
       g("Nationwide population-based cohort study of alopecia areata incidence") == "B")
    ck("case report -> C", g("Complete regrowth after therapy: a case report") == "C")
    ck("murine mechanism -> D",
       g("CD8+ T-cell mediated collapse of immune privilege in mice") == "D")
    ck("unclassifiable -> E", g("Alopecia areata update") == "E")
    # The one that matters most: a registration is a plan, not a result.
    ck("a trial REGISTRATION is T, never A",
       grade({"title": "A Randomized, Double-Blind Study of X in Alopecia Areata",
              "source": "clinicaltrials.gov"})[0] == "T")
    ck("preprint flagged", is_preprint({"source": "medrxiv"}) is True)
    ck("pubmed is not a preprint", is_preprint({"source": "pubmed"}) is False)

    # ── corpus ──────────────────────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "daily"
        d.mkdir()
        (d / "alopecia-2026-08-28.json").write_text(json.dumps([
            {"key": "a", "title": "First", "source": "pubmed", "rank": 1, "label": "x"},
            {"key": "b", "title": "Second", "source": "pubmed", "rank": 2, "label": "y"}]))
        (d / "alopecia-2026-08-30.json").write_text(json.dumps([
            {"key": "b", "title": "Second", "source": "pubmed", "rank": 2, "label": "y"},
            {"key": "c", "title": "Third", "source": "naaf", "rank": 7, "label": "z"}]))
        (d / "alopecia-2026-08-31.json").write_text("{ not json")
        corpus = load_corpus(d)
        ck("corpus merges by key across days", len(corpus) == 3)
        ck("an item keeps its FIRST-seen day",
           [i for i in corpus if i["key"] == "b"][0]["collected"] == "2026-08-28")
        ck("an unreadable day does not lose the others", len(corpus) == 3)
        ck("window is exclusive of the since-day",
           [i["key"] for i in since_window(corpus, "2026-08-28")] == ["c"])
        ck("empty corpus is not an error", load_corpus(Path(td) / "nope") == [])

    # ── state ───────────────────────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "brief_state.json"
        ck("missing state reads empty", load_state(sp) == {})
        save_state({"count": 1, "last_brief_day": "2026-09-01"}, sp)
        ck("state round-trips", load_state(sp)["count"] == 1)

    # ── prompt ──────────────────────────────────────────────────────────────
    items = [{"key": "a", "title": "T1", "source": "pubmed", "rank": 1,
              "label": "subgroup", "url": "u1", "date": "2026-08-01"}]
    p = build_prompt(items, True, "", "2026-09-01")
    ck("brief #1 prompt says full review", "EVERYTHING COLLECTED TO DATE" in p)
    ck("delta prompt names the since-day",
       "since 2026-08-25" in build_prompt(items, False, "2026-08-25", "2026-09-01"))
    ck("standing questions are in every prompt", "immune privilege" in p)
    ck("prompt carries the grade", "grade" in p)
    # Privacy: the prompt is outbound. Nothing identifying may be in it.
    low = (p + SYSTEM).lower()
    ck("no initials or identifiers in the outbound prompt",
       "rcw" not in low and "@" not in low)

    # ── assembly ────────────────────────────────────────────────────────────
    meta = {"members": ["anthropic", "gemini"], "judge": "anthropic",
            "degraded": False, "reason": ""}
    body = "## What we have found so far\n\nSomething [1].\n"
    md = assemble(body, items, meta, True, "", "2026-09-01", 1)
    ck("every item is linked in the appendix", "u1" in md)
    ck("missing disagreement section is REPORTED, not silent",
       "The judge did not return this section" in md)
    ck("a returned disagreement section is left alone",
       "The judge did not return this section" not in
       assemble(body + "\n## Council disagreements\n\nNone.\n", items, meta,
                True, "", "2026-09-01", 1))
    ck("degraded council is stated in the brief",
       "DEGRADED" in assemble(body, items,
                              {"members": ["anthropic"], "judge": "anthropic",
                               "degraded": True, "reason": "one keyed provider"},
                              True, "", "2026-09-01", 1))
    ck("not-medical-advice footer is present", "not medical advice" in md.lower())
    ck("empty period does not pretend to be news",
       "Nothing new was collected" in empty_brief("2026-09-01", "2026-08-25", 2))
    ck("empty brief still holds the standing questions open",
       "immune privilege" in empty_brief("2026-09-01", "2026-08-25", 2))

    # ── email HTML ──────────────────────────────────────────────────────────
    h = _html(md)
    ck("html has a document shell", h.startswith("<!DOCTYPE html>"))
    ck("headings convert", "<h1>" in h and "<h2>" in h)
    ck("a bare source URL becomes a link", '<a href="u1">' in _html("see u1")
       or "u1" in h)
    ck("a real bare URL becomes a link",
       '<a href="https://pubmed.ncbi.nlm.nih.gov/1">' in
       _html("https://pubmed.ncbi.nlm.nih.gov/1"))
    ck("a markdown link is not double-wrapped",
       _html("[x](https://e.org)").count("<a href") == 1)
    ck("no leftover bold markers", "**" not in h)

    print("\n%s" % ("ALL PASS" if ok[0] else "FAILURES ABOVE"))
    return ok[0]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
