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


_DATE_H2_RX = re.compile(r"^##\s*\d{4}-\d{2}-\d{2}\s*$")


def strip_model_title(body):
    """Drop a title block the judge added on top of ours.

    Observed on the first live run: our header is already "# Alopecia areata —
    weekly brief #1" plus a date line, and the judge opened its answer with
    "# Alopecia Areata Research Brief" / "## 2026-09-01" -- so the email led
    with two titles and two dates. Stripped here rather than forbidden in the
    prompt, because a model instruction is a request and this is a guarantee.
    Only a LEADING h1 (and an immediately following date-only h2 or rule) goes;
    a real "# " later in the body is left alone.
    """
    lines = body.lstrip("\n").split("\n")
    if not lines or not lines[0].startswith("# "):
        return body
    i = 1
    while i < len(lines) and (not lines[i].strip()
                              or _DATE_H2_RX.match(lines[i].strip())
                              or lines[i].strip() == "---"):
        i += 1
    return "\n".join(lines[i:])


CAUSE_RESEARCH_DRAFT_PATH = PROJECT_DIR / "alopecia" / "cause_research_draft.md"


def consume_cause_research_section(path=None):
    """S177: read the etiology-synthesis agent's staging draft
    (alopecia_agent's append_to_brief_draft output) and consume it -- the
    content becomes part of THIS brief, and the file is cleared so next
    week doesn't repeat it. Returns "" (section omitted entirely, not
    printed empty) if nothing is staged.

    A side-effecting helper, deliberately kept OUT of assemble()/
    empty_brief() themselves -- both stay pure functions of their already-
    computed inputs (body/items/meta, same as today), so calling them in a
    test never touches a real file. build() calls this once and passes the
    result in.
    """
    path = Path(path) if path else CAUSE_RESEARCH_DRAFT_PATH
    try:
        content = path.read_text().strip()
    except Exception:
        return ""
    if not content:
        return ""
    path.write_text("")
    return "\n".join(["## Cause research (etiology-synthesis agent)", "",
                      content, ""])


def trials_section():
    """S258: alopecia_trials' section, or a VISIBLE line saying it failed --
    never a silent gap in the brief, and never a failed brief."""
    try:
        import alopecia_trials
        return alopecia_trials.brief_section()
    except Exception as e:
        return ("## Trials watch — nearest sites and fit\n\n**Could not be built "
                "this week** (%s: %s).\n" % (type(e).__name__, str(e)[:120]))


def assemble(body, items, meta, full, since_day, today, number, cause_section=""):
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

    body = strip_model_title(body)
    parts = ["\n".join(head), body.strip(), ""]

    if not body.strip():
        # S256: brief #4 (2026-09-18) went out with NO synthesis -- the model
        # spent its whole max_tokens on thinking and returned empty text -- and
        # the only trace was a disagreement note under a blank page.
        parts += ["## Synthesis missing", "",
                  "**The council returned no text for this brief.** The items "
                  "below were collected but not reviewed; the next brief "
                  "covers them again. This is a failure of this run, not a "
                  "quiet week.", ""]
    elif not DISAGREE_RX.search(body):
        # Detection, not a silent pass: the judge dropped the section, so the
        # brief says that rather than letting its absence read as agreement.
        # S256: name the answers file only when one is written -- a
        # single-model run keeps none, and brief #4 pointed at a missing file.
        where = ("The raw per-model answers are on the box next to this brief "
                 "(`alopecia/briefs/%s-council.json`) and can be compared "
                 "directly." % today if meta.get("answers") else
                 "No per-model answers were kept for this run, so there is "
                 "nothing to compare against.")
        parts += ["## Council disagreements", "",
                  "**The judge did not return this section.** That is a gap in "
                  "this brief, not evidence that the council agreed. " + where, ""]

    if cause_section:
        parts += [cause_section]

    parts += [sources_appendix(items), "", "---", "",
              "_Research monitor, not medical advice. Nothing here is a "
              "recommendation to start, stop or change any treatment — that is "
              "a dermatologist's call. Evidence grades: A controlled trial · "
              "B cohort/epidemiology · C case report · D mechanistic/review · "
              "T trial registration (no results) · E unclassified._"]
    return "\n".join(parts)


def empty_brief(today, since_day, number, cause_section=""):
    """A period with nothing new is a result, not a failure — and it is not a
    reason to spend a council call. cause_section can still be non-empty
    even on a quiet collector week -- the etiology agent runs on its own
    daily cadence and may have refined a hypothesis with no NEW collector
    items at all (e.g. re-weighing existing evidence)."""
    parts = [
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
    ]
    if cause_section:
        parts += [cause_section]
    parts += ["---", "", "_Research monitor, not medical advice._"]
    return "\n".join(parts)


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

    # S177: consumed ONCE per brief build, regardless of which path below
    # renders it -- a quiet collector week can still have a real
    # etiology-agent update (it runs on its own daily cadence and may
    # refine a hypothesis with no NEW collector items at all).
    cause_section = consume_cause_research_section()
    # S258: the P3 trials watch rides in the same appended slot, after the
    # cause research, on quiet weeks too -- recruiting status moves on its own.
    cause_section = "\n".join(x for x in (cause_section, trials_section()) if x)

    if not items:
        md = empty_brief(today, since_day, number, cause_section=cause_section)
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

    md = assemble(body, items, meta, full, since_day, today, number,
                  cause_section=cause_section)
    subject = "Alopecia areata — weekly brief #%d%s" % (
        number, " (full review to date)" if full else "")
    # S256: an empty synthesis reviewed nothing, so the window does not move --
    # the next brief covers these items again rather than skipping them.
    meta["synthesis_empty"] = not strip_model_title(body or "").strip()
    meta["brief_day"] = today
    last_day = state.get("last_brief_day", "") if meta["synthesis_empty"] else today
    return subject, md, meta, items, {"count": number, "last_brief_day": last_day}


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


def build_note(info):
    """The ledger note for one run. Self-contained (S150).

    The DEGRADED clause is the reason this is worth extracting: a brief that
    went out with half the council down still says "sent", and the only place
    that fact appears is here.
    """
    note = "brief #%d sent, %d item(s), council %s" % (
        info.get("count", 0), info.get("items", 0),
        ",".join(info.get("members") or []) or "none")
    if info.get("degraded"):
        note += " [DEGRADED: %s]" % info.get("reason")
    if info.get("synthesis_empty"):
        note += " [SYNTHESIS EMPTY: items carried to next brief]"
    return note


def note_samples():
    """Every note shape this job writes. Built by CALLING build_note (S150)."""
    full = {"count": 3, "items": 13, "members": ["anthropic", "gemini"]}
    return [
        ("a normal brief", build_note(full), "productive"),
        ("a brief with an empty council",
         build_note({**full, "members": []}), "productive"),
        # The live ledger has never shown this one.
        ("a DEGRADED council",
         build_note({**full, "degraded": True, "reason": "grok timeout"}),
         "productive"),
        ("a brief with no items at all",
         build_note({**full, "items": 0}), "zero"),
        # S256: the item count still parses; ok=False is what makes it red.
        ("a brief whose synthesis came back EMPTY",
         build_note({**full, "synthesis_empty": True}), "productive"),
    ]


def main(argv):
    args = set(argv[1:])
    if "--selftest" in args or "selftest" in args:
        return 0 if selftest() else 1
    dry = "--dry-run" in args
    full = True if "--full" in args else None

    subject, md, meta, items, state_update = build(full=full)
    # S256: the file is named for the day the brief was BUILT -- last_brief_day
    # stays put after an empty synthesis, and would overwrite last week's file.
    _write_outputs(meta.get("brief_day") or state_update["last_brief_day"],
                   md, meta, state_update["count"])

    if dry:
        log("DRY RUN — not sending, not advancing state")
        print("\n" + md)
        return 0

    import send_guard
    blocked = send_guard.already_sent_today(SEND_JOB)
    if blocked:
        log(send_guard.blocked_message(SEND_JOB, blocked))
        # Still a HEALTHY run: today's brief did go out, this is a rerun. Not
        # recording here would let the watchdog call the job overdue for a week
        # on the strength of a guard doing its job.
        _record("already sent today — guard held the rerun", ok=True)
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
    # Only AFTER a send that did not raise. mailer.send defaults to
    # on_error="raise", so a failed send never reaches this line and the job
    # goes stale in the watchdog -- which is the correct signal. This whole
    # project exists because a brief silently did not arrive.
    # ok=True even on a degraded council: the health question for THIS job is
    # "did the brief go out", and it did. A degraded run is reported where it
    # belongs -- as a banner in the brief itself, and in this note -- rather
    # than as a red job for a brief that was delivered. A check that cries wolf
    # stops being read.
    # S256: an EMPTY synthesis is not a degraded council -- nothing was reviewed,
    # so it is red. Brief #4 recorded "sent, 8 item(s)" ok=True over a blank page.
    _record(build_note({"count": state_update["count"], "items": len(items),
                        "members": meta.get("members"),
                        "degraded": meta.get("degraded"),
                        "reason": meta.get("reason"),
                        "synthesis_empty": meta.get("synthesis_empty")}),
            ok=not meta.get("synthesis_empty"))
    return 0


def _record(note, ok=True):
    """Best-effort watchdog ping. Never turns bookkeeping into a failed send."""
    try:
        import job_status
        job_status.record("alopeciabrief", ok, note)
    except Exception as e:
        print("job_status.record failed: %s" % e)


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
    # S256: brief #4 went out with an EMPTY synthesis under a false pointer
    # to a council file that a single-model run never writes.
    hollow = assemble("", items, {"members": ["anthropic"], "judge": "anthropic",
                                  "degraded": True, "reason": "x"},
                      False, "2026-09-11", "2026-09-18", 4)
    ck("an EMPTY synthesis is announced, not a blank page",
       "## Synthesis missing" in hollow)
    ck("an empty synthesis still carries the sources", "u1" in hollow)
    ck("no council-file pointer when no answers were kept",
       "council.json" not in assemble(body, items, meta, True, "", "2026-09-01", 1))
    ck("council-file pointer when answers WERE kept",
       "2026-09-01-council.json" in assemble(
           body, items, {**meta, "answers": [("a", "x"), ("b", "y")]},
           True, "", "2026-09-01", 1))
    ck("an empty-synthesis note says so",
       "SYNTHESIS EMPTY" in build_note({"count": 4, "items": 8,
                                        "synthesis_empty": True}))

    ck("empty period does not pretend to be news",
       "Nothing new was collected" in empty_brief("2026-09-01", "2026-08-25", 2))
    ck("empty brief still holds the standing questions open",
       "immune privilege" in empty_brief("2026-09-01", "2026-08-25", 2))

    # ── the judge's own title block ─────────────────────────────────────────
    ck("a leading model title is stripped",
       strip_model_title("# Their Title\n## 2026-09-01\n\n---\n\n## Real\n\nx")
       == "## Real\n\nx")
    ck("a body with no title is untouched",
       strip_model_title("## What changed\n\nx") == "## What changed\n\nx")
    ck("a later h1 is NOT stripped",
       "# Deeper" in strip_model_title("## A\n\n# Deeper\n\nx"))
    ck("only ONE leading title goes",
       strip_model_title("# One\n\n# Two\n\nx") == "# Two\n\nx")
    ck("the assembled brief has exactly one h1",
       assemble("# Their Title\n## 2026-09-01\n\n## Real\n\nx [1].\n",
                items, meta, True, "", "2026-09-01", 1)
       .count("\n# ") + 1 == 1)

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

    # ── S177: cause-research section (etiology agent's brief splice) ────────
    with tempfile.TemporaryDirectory() as td:
        draft = Path(td) / "cause_research_draft.md"

        ck("consume_cause_research_section: a missing draft file -> "
           "empty section, not an error",
           consume_cause_research_section(draft) == "")

        draft.write_text("   \n\n  ")
        ck("consume_cause_research_section: a whitespace-only draft is "
           "treated as nothing staged", consume_cause_research_section(draft) == "")

        draft.write_text("\n---\n## 2026-09-15\n\nEvidence points toward "
                         "viral triggers (grade C).\n")
        section = consume_cause_research_section(draft)
        ck("consume_cause_research_section: real staged content becomes a "
           "titled section", section.startswith("## Cause research")
           and "viral triggers" in section)
        ck("consume_cause_research_section: CONSUMES it -- the file reads "
           "empty on a second call, so next week doesn't repeat it",
           consume_cause_research_section(draft) == "")

        ck("assemble: an empty cause_section adds nothing (default "
           "behavior unchanged for every existing caller)",
           "Cause research" not in assemble(
               "## Real\n\nx.", items, meta, True, "", "2026-09-01", 1))
        ck("assemble: a non-empty cause_section is included",
           "viral triggers" in assemble(
               "## Real\n\nx.", items, meta, True, "", "2026-09-01", 1,
               cause_section="## Cause research\n\nviral triggers here"))

        ck("empty_brief: an empty cause_section adds nothing",
           "Cause research" not in empty_brief("2026-09-15", "2026-09-08", 1))
        ck("empty_brief: a non-empty cause_section appears even on a "
           "QUIET collector week -- the etiology agent has its own cadence",
           "viral triggers" in empty_brief(
               "2026-09-15", "2026-09-08", 1,
               cause_section="## Cause research\n\nviral triggers here"))

    # ── S256: an empty synthesis holds the window (stubbed council, temp files) ──
    import types
    global CAUSE_RESEARCH_DRAFT_PATH
    saved_draft, saved_ens = CAUSE_RESEARCH_DRAFT_PATH, sys.modules.get("ensemble")
    saved_trials = sys.modules.get("alopecia_trials")
    sys.modules["alopecia_trials"] = types.SimpleNamespace(
        brief_section=lambda: "## Trials watch — stub")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "daily"
        d.mkdir()
        (d / "alopecia-2026-09-15.json").write_text(json.dumps([
            {"key": "k", "title": "T", "source": "pubmed", "rank": 1, "label": "x"}]))
        sp = Path(td) / "state.json"
        CAUSE_RESEARCH_DRAFT_PATH = Path(td) / "draft.md"
        try:
            for reply, want_day in (("", "2026-09-11"),
                                    ("## What changed\n\nx [1].", "2026-09-18")):
                save_state({"count": 3, "last_brief_day": "2026-09-11"}, sp)
                sys.modules["ensemble"] = types.SimpleNamespace(
                    best_answer=lambda *a, _r=reply, **k: (
                        {"members": ["anthropic"], "judge": "anthropic"}, _r))
                _, _, m, _, st = build(now=datetime(2026, 9, 18, 7), daily_dir=d,
                                       state_path=sp, creds={})
                ck("build: %s synthesis -> window %s, file day 2026-09-18" % (
                       "EMPTY" if not reply else "real", want_day),
                   st["last_brief_day"] == want_day and st["count"] == 4
                   and m["brief_day"] == "2026-09-18"
                   and m["synthesis_empty"] == (not reply))
            ck("build: the trials section rides in the brief",
               "## Trials watch — stub" in build(now=datetime(2026, 9, 18, 7),
                                                 daily_dir=d, state_path=sp, creds={})[1])

            def _boom():
                raise RuntimeError("clinicaltrials.gov timed out")
            sys.modules["alopecia_trials"] = types.SimpleNamespace(brief_section=_boom)
            ck("build: a failed trials fetch is VISIBLE in the brief, not a gap",
               "Could not be built this week" in trials_section()
               and "timed out" in trials_section())
        finally:
            if saved_trials is None:
                sys.modules.pop("alopecia_trials", None)
            else:
                sys.modules["alopecia_trials"] = saved_trials
            CAUSE_RESEARCH_DRAFT_PATH = saved_draft
            if saved_ens is None:
                sys.modules.pop("ensemble", None)
            else:
                sys.modules["ensemble"] = saved_ens

    print("\n%s" % ("ALL PASS" if ok[0] else "FAILURES ABOVE"))
    return ok[0]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
