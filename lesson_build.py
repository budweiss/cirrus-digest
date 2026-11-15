#!/usr/bin/env python3
"""lesson_build.py — per-lesson curriculum work built from a client's Teach Book (S629).

Plan: docs/LESSON-BUILD-WORKER-PLAN.md (Buddy, 2026-11-15: "Yes, build it").

Route: intake marks a pedagogy email with an attached Great Minds Arts & Letters
Teach Book `document_build`; start_job() records it and launches `run` through
job_runner, detached. The job splits the book into lessons by script, takes
the PA codes from Great Minds' alignment guide by script, has the local model
draft each lesson (six-part lesson plan and/or 3-4 multiple-choice questions),
checks every lesson, and assembles one email per arc per deliverable in the
layout Alyssa approved. Arcs go out in order, from this box, cc Buddy, each
email asking "Is this what you were looking for?". A lesson that fails its
checks after one local repair and one cloud attempt holds that arc and every
later one, and Buddy is told on Telegram which lesson and which check.

What this module guarantees, because a client email cannot be unsent:
  * ONE send per run, arc and deliverable (ledger checked before, written after).
  * Nothing goes out unless every lesson in that arc passed every check.
  * auto_send is per client in config/lesson_build.json and starts false
    (shadow: the job writes the emails and sends nothing).
  * The recipient comes from intake_senders.json by client key, never from mail.
"""
from __future__ import annotations

import collections
import json
import random
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
RUNS_DIR = PROJECT_DIR / "logs/lesson_builds"
REQUESTS_REL = "logs/lesson_builds/requests"
GUIDE_PATH = PROJECT_DIR / "data/pedagogy/pa_level4.json"
EXAMPLE_PATH = PROJECT_DIR / "data/pedagogy/example_lesson.json"
CONFIG_PATH = PROJECT_DIR / "config/lesson_build.json"
SENDERS_PATH = PROJECT_DIR / "config/intake_senders.json"
FIT_CHECK = "Is this what you were looking for?"
SEP = "-" * 40
ROUTE_TASK = "pedagogy-lesson-build"
MAX_JOBS_PER_CLIENT_PER_DAY = 2
MAX_CLOUD_CALLS_PER_JOB = 30
LABELS = ("1. PA Core Standards", "2. Learning Goals / I can statements",
          "3. Student considerations", "4. Learning Progress / Instructional Plan",
          "5. Success Criteria / Evidence of learning", "6. Next steps")
DEFAULTS = {
    "draft": {"endpoint": "http://127.0.0.1:11434", "model": "gpt-oss:120b", "think": "medium"},
    "check": {"endpoint": "http://192.168.100.11:8000", "model": "qwen3.8-27b-fp8"},
    "clients": {},
}
KINDS = ("lessons", "checks")


class Hold(Exception):
    """Stop here; the reason is said to Buddy and kept with the run."""


# ── text helpers ─────────────────────────────────────────────────────────────

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(text: str) -> str:
    text = _CTRL.sub("", text.replace("\f", "\n")).replace(" ", " ").replace(" ", " ")
    return re.sub(r"[ \t]+", " ", text)


def norm(text: str) -> str:
    """For containment checks: case, quotes, dashes, spacing and soft hyphens."""
    t = (text or "").lower()
    t = t.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                                   "–": "-", "—": "-", "­": None}))
    return " ".join(re.sub(r"[^a-z0-9'\- ]+", " ", t).split())


def pdf_pages(pdf: Path) -> list:
    """Reading-order text per page (S621: -layout scrambles these columns)."""
    out = subprocess.run(["pdftotext", "-q", str(pdf), "-"], capture_output=True, timeout=300)
    if out.returncode != 0:
        raise Hold("pdftotext could not read the book")
    pages = out.stdout.decode("utf-8", "replace").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def pdf_title(pdf: Path) -> str:
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, timeout=60)
    m = re.search(r"(?m)^Title:\s*(.+)$", out.stdout.decode("utf-8", "replace"))
    return m.group(1).strip() if m else ""


# ── the alignment guide (script only) ────────────────────────────────────────

_CODE = re.compile(r"^CC\.\d\.\d\.(\d+)\.[A-Z]$")
_LEVEL = re.compile(r"^Level (\d+), Module (\d+), Lessons? ([\d,\s]+)$")
_CONT = re.compile(r"^[\d,\s]+$")


def parse_guide(text: str) -> dict:
    """Great Minds' PA correlation guide -> {"level", "codes": [{code, text,
    modules: {module: [lessons]}}]}. Only a code's own lesson lists count; the
    bullets under it repeat or narrow them. S621 trap: the page footer
    ("© ... greatminds.org" then a bare page number) once read as a lesson."""
    codes, cur = [], None
    after_bullet = text_done = False
    in_list = None
    header = re.compile(r"^\d+\s*\|\s*Pennsylvania Core Standards")
    footer = lambda s: "greatminds.org" in s or s.startswith("©")
    lines = [l for l in (clean(x).strip() for x in text.splitlines()) if l]
    # A page number is a bare number just before the next page's header, or
    # just after the footer; it is not always next to the footer.
    page_numbers = {i for i, l in enumerate(lines) if re.fullmatch(r"\d+", l) and (
        i + 1 == len(lines) or header.match(lines[i + 1]) or (i and footer(lines[i - 1])))}
    for i, line in enumerate(lines):
        if i in page_numbers or footer(line):
            continue
        if header.match(line) or line in (
                "Pennsylvania Standards", "Aligned Components of Arts & Letters"):
            continue
        if _CODE.match(line):
            cur = {"code": line, "text": [], "modules": {}}
            codes.append(cur)
            after_bullet = text_done = False
            in_list = None
            continue
        if cur is None:
            continue
        if line.startswith("•"):
            after_bullet, in_list = True, None
            continue
        m = _LEVEL.match(line)
        if m:
            in_list = "skip" if after_bullet else m.group(2)
            if in_list != "skip":
                cur["modules"].setdefault(in_list, []).extend(
                    int(x) for x in re.findall(r"\d+", m.group(3)))
            continue
        if in_list and _CONT.match(line):
            if in_list != "skip":
                cur["modules"][in_list].extend(int(x) for x in re.findall(r"\d+", line))
            continue
        in_list = None
        if not after_bullet and not text_done:
            cur["text"].append(line)
            text_done = line.endswith(".")
    levels = {_CODE.match(c["code"]).group(1) for c in codes}
    if len(levels) != 1:
        raise ValueError("guide mixes levels %s" % sorted(levels))
    for c in codes:
        c["text"] = " ".join(c["text"])
        if not c["text"].endswith("."):
            raise ValueError("standard text did not end cleanly for %s" % c["code"])
        for mod, lessons in c["modules"].items():
            if len(set(lessons)) != len(lessons):
                raise ValueError("duplicate lesson under %s module %s" % (c["code"], mod))
    if len({c["code"] for c in codes}) != len(codes):
        raise ValueError("a code appears twice in the guide")
    return {"level": int(levels.pop()), "codes": codes}


def codes_by_lesson(guide: dict, module: int) -> dict:
    out = collections.defaultdict(list)
    for c in guide["codes"]:
        for n in c["modules"].get(str(module), []):
            out[n].append((c["code"], c["text"]))
    return {n: sorted(v) for n, v in out.items()}


# ── the Teach Book (script only) ─────────────────────────────────────────────

def _descriptor_page(ls: list) -> bool:
    """Blank, or the achievement-descriptor pages after a lesson's Land
    ("ACHIEVEMENT DESCRIPTORS", or a two-letter strand such as CP or MM)."""
    return (not ls or "ACHIEVEMENT DESCRIPTORS" in ls[:3]
            or (len(ls) > 2 and re.fullmatch(r"[A-Z]{2}", ls[1]) is not None))


_TOC_LESSON = re.compile(r"^L(\d+)\s*\|\s*(.+?)[\s.…�]*\s(\d+)$")
_TOC_SECTION = re.compile(r"^(Achievement Descriptors|Works Cited)[\s.…�]*\s(\d+)$")
_TOC_ARC = re.compile(r"^(Arc [A-Z])\s*\|\s*(.+)$")
_HEAD = re.compile(r"^\d+\s*\|\s*Module (\d+)\s*\|\s*(Arc [A-Z]|Module Finale)\s*\|\s*Lesson (\d+)$")


def _lines(page: str) -> list:
    return [l for l in (clean(x).strip() for x in page.splitlines()) if l]


def split_book(pages: list, title: str) -> dict:
    """Lessons, arcs and printed page numbers, or Hold with what did not line up."""
    m = re.search(r"Level (\d+) Module (\d+) Teach", title or "")
    if not m:
        raise Hold("the attached file is not an Arts & Letters Teach Book (title %r)" % title)
    level, module = int(m.group(1)), int(m.group(2))
    toc, ends, arc_names = {}, [], {}
    for page in pages[:40]:
        for line in _lines(page):
            t = _TOC_LESSON.match(line)
            if t:
                toc.setdefault(int(t.group(1)), (t.group(2).strip(), int(t.group(3))))
            s = _TOC_SECTION.match(line)
            if s:
                ends.append(int(s.group(2)))
            a = _TOC_ARC.match(line)
            if a:
                arc_names.setdefault(a.group(1), a.group(2).strip())
    openers = []
    for i, page in enumerate(pages):
        ls = _lines(page)
        if (ls and re.fullmatch(r"Lesson \d+", ls[0])
                and any(x.startswith("Essential Question") for x in ls[1:4])):
            openers.append((int(ls[0].split()[1]), i))
    nums = [n for n, _ in openers]
    if not nums or nums != list(range(1, len(nums) + 1)):
        raise Hold("lesson openings do not run 1 to N without gaps (found %s)" % nums[:60])
    if sorted(toc) != nums:
        raise Hold("the contents list %d lessons but the book opens %d" % (len(toc), len(nums)))
    offsets = {i - (toc[n][1] - 1) for n, i in openers}
    if len(offsets) != 1:
        raise Hold("printed page numbers do not line up with the file (offsets %s)" % sorted(offsets))
    offset = offsets.pop()          # file index (0-based) = printed - 1 + offset
    last_start = toc[nums[-1]][1]
    after = [e for e in ends if e > last_start]
    if not after:
        raise Hold("could not find where the last lesson ends")
    lessons = []
    for k, (n, start) in enumerate(openers):
        end = openers[k + 1][1] - 1 if k + 1 < len(openers) else min(after) - 2 + offset
        arcs = collections.Counter()
        launch = land = None
        for i in range(start, end + 1):
            ls = _lines(pages[i])
            h = _HEAD.match(ls[0]) if ls else None
            if h and int(h.group(1)) == module and int(h.group(3)) == n:
                arcs[h.group(2)] += 1
            # The section heads carry minutes ("LAUNCH 2 minutes"); the agenda's do not.
            if launch is None and any(re.match(r"^LAUNCH \d+ minutes?$", x) for x in ls):
                launch = i
            if land is None and any(re.match(r"^LAND \d+ minutes?$", x) for x in ls):
                land = i
        if not arcs:
            raise Hold("Lesson %d has no running heads naming its arc" % n)
        if launch is None or land is None or land < launch:
            raise Hold("Lesson %d: could not find its Launch and Land pages" % n)
        # The plan runs to the page before the achievement-descriptor pages,
        # which open with CP, BU, DF or DM under the running head.
        plan_end = land
        for i in range(land + 1, end + 1):
            ls = _lines(pages[i])
            if _descriptor_page(ls):
                break
            plan_end = i
        arc = arcs.most_common(1)[0][0]
        label = toc[n][0]
        head = ("Module Finale, " + label if arc == "Module Finale" and "Bookend" not in label
                else label)
        p = lambda i: i - offset + 1
        lessons.append({"n": n, "arc": arc, "label": label, "title": head,
                        "first": start, "last": end, "printed": (p(start), p(end)),
                        "plan": (p(launch), p(plan_end))})
    order = []
    for L in lessons:
        if not order or order[-1] != L["arc"]:
            if L["arc"] in order:
                raise Hold("arc %s is not contiguous" % L["arc"])
            order.append(L["arc"])
    return {"level": level, "module": module, "offset": offset, "lessons": lessons,
            "arcs": order, "arc_names": arc_names}


def lesson_text(pages: list, book: dict, L: dict) -> str:
    """The lesson's pages, each under its printed page number."""
    out = []
    for i in range(L["first"], L["last"] + 1):
        ls = _lines(pages[i])
        if _descriptor_page(ls):
            continue    # blank or achievement-descriptor pages
        body = "\n".join(ls)
        out.append("=== printed p. %d ===\n%s" % (i - book["offset"] + 1, body))
    return "\n".join(out)


# ── drafting (local first) ───────────────────────────────────────────────────

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "i_can": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4},
        "why_it_matters": {"type": "string"},
        "student_considerations": {"type": "string"},
        "plan": {"type": "string"},
        "success_criteria": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 5},
        "next_steps": {"type": "string"},
        "standards_note": {"type": "string"},
    },
    "required": ["i_can", "why_it_matters", "student_considerations", "plan",
                 "success_criteria", "next_steps", "standards_note"],
}
MCQ_SCHEMA = {
    "type": "object",
    "properties": {
        "focus": {"type": "string"},
        "questions": {"type": "array", "minItems": 3, "maxItems": 4, "items": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "enum": ["target", "vocab", "word study"]},
                "stem": {"type": "string"},
                "correct": {"type": "string"},
                "wrong": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 3},
            },
            "required": ["tag", "stem", "correct", "wrong"]}},
    },
    "required": ["focus", "questions"],
}

SYSTEM = ("You write planning material for a grade 4 teacher in Pennsylvania who teaches "
          "Great Minds' Arts & Letters curriculum. Work only from the Teach Book pages you "
          "are given. The teacher copies your text straight into her planning documents, so "
          "it must be accurate, specific to this lesson, and plain text: no markdown, no "
          "bullet characters, no headings.")

RULES = """Rules:
- Every fact about texts, characters, events, definitions and activities must be stated in these pages. Do not add story events or details from your own knowledge of the books.
- Use quotation marks only around words copied exactly from these pages.
- A page number you mention must be a printed page number shown in these pages ("=== printed p. N ===") or a page reference printed in them, such as a Learn book page."""

REVIEW_TASK = """Teach Book: Level {level}, Module {module}. Lesson {n} ({title}), printed pages {p1}-{p2}. Its instructional plan runs from Launch to Land on pages {q1}-{q2}.

PA Core codes Great Minds assigns to this lesson (the teacher's copy lists them already; do not repeat the list):
{codes}

Write these parts:
- i_can: each Learning Goal in the lesson's Overview, rewritten as one "I can ..." statement. Keep the goal's own words and meaning and add nothing.
- why_it_matters: one to three sentences on what the lesson does and why it matters in the module.
- student_considerations: one paragraph for the teacher on what students need to access the lesson: the Differentiation, Language Support and Teacher Note boxes, materials or setup to plan, and likely difficulties.
- plan: one paragraph walking through the lesson in order. Name each part (Launch, each Learn segment, Land) with its minutes, what students do, and each learning task. End with one sentence on where the lesson's rigor comes from.
- success_criteria: two to four statements, each beginning "Students will", matching the learning tasks and the "Monitor" questions under Analyze Student Progress.
- next_steps: how this lesson connects to later lessons, from the "Plan Future Practice" lines and what the lesson says comes next.
- standards_note: "" or one sentence linking one or two of the listed codes to specific activities. Name only codes from the list above.

{rules}

Example of the expected style and depth, from a different lesson:
{example}

Teach Book pages for Lesson {n}:
{pages}"""

MCQ_TASK = """Teach Book: Level {level}, Module {module}. Lesson {n} ({title}).

Write 3 or 4 multiple-choice questions a grade 4 student can answer after this lesson, drawn from its learning targets, vocabulary or word study. Tag each "target", "vocab" or "word study".
- Ask about what students learn, read or do in the lesson (ideas, events, words, skills), not about the Teach Book's own wording or instructions to the teacher.
- Each question has one correct answer and three wrong answers. All four options are different and similar in length and style. Wrong answers should be plausible to a student who did not learn the lesson (a common mix-up, a detail from another part of the text), yet clearly wrong to one who did. No silly options, and no "all of the above" or "none of the above".
- When the lesson teaches vocabulary or word study, include one question on it.
- The correct answer must be stated or shown in these pages. A definition must be the one these pages give.{assessment}
- focus: a short phrase saying what the questions cover.

{rules}

Example from a different lesson:
{example}

Teach Book pages for Lesson {n}:
{pages}"""

ASSESSMENT_NOTE = ("\n- This is an assessment day. Write review questions on module ideas and vocabulary "
                   "named in these pages, not on the assessment text itself.")


def _post(url: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as r:
        return json.load(r)


def _json(raw: str) -> dict:
    raw = (raw or "").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0) if m else raw)


def local_draft(user: str, schema: dict, cfg: dict, timeout: int = 1800) -> dict:
    d = cfg["draft"]
    r = _post(d["endpoint"] + "/api/chat", {
        "model": d["model"], "stream": False, "think": d.get("think", "medium"), "format": schema,
        "options": {"temperature": 0.3},
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}, timeout)
    if r.get("model") != d["model"]:
        raise RuntimeError("draft answered by %r, not %r" % (r.get("model"), d["model"]))
    return json.loads(r["message"]["content"])


def local_check(system: str, user: str, schema: dict, cfg: dict, max_tokens: int = 2000,
                timeout: int = 900) -> dict:
    c = cfg["check"]
    r = _post(c["endpoint"] + "/v1/chat/completions", {
        "model": c["model"], "temperature": 0, "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_schema", "json_schema": {"name": "check", "schema": schema}},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}, timeout)
    if r.get("model") != c["model"]:
        raise RuntimeError("check answered by %r, not %r" % (r.get("model"), c["model"]))
    return json.loads(r["choices"][0]["message"]["content"])


def cloud_draft(user: str, schema: dict, creds: dict) -> tuple:
    import llm_providers
    system = (SYSTEM + "\nReply with exactly one JSON object that matches this JSON schema, "
              "and nothing else:\n" + json.dumps(schema))
    provider, raw = llm_providers.escalate(system, user, creds, max_tokens=8000,
                                           mode="single", task=ROUTE_TASK)
    return provider, _json(raw)


# ── checks ───────────────────────────────────────────────────────────────────

_QUOTE = re.compile(r"[“\"]([^“”\"]{2,}?)[”\"]")
_PAGE_REF = re.compile(r"\bpp?\.\s*(\d+)(?:\s*[-–]\s*(\d+))?")
_CODE_REF = re.compile(r"CC\.\d\.\d\.\d+\.[A-Z]")


def _quotes_ok(text: str, src_norm: str) -> list:
    return ["quoted words are not in the pages: “%s”" % q for q in _QUOTE.findall(text)
            if norm(q) and norm(q) not in src_norm]


def _pages_ok(text: str, L: dict, src: str) -> list:
    bad = []
    for m in _PAGE_REF.finditer(text):
        for g in m.groups():
            if not g:
                continue
            n = int(g)
            if L["printed"][0] <= n <= L["printed"][1]:
                continue
            # The book cites "p. 75", "page 53" and "pages 54–57".
            if re.search(r"\b(?:pages?|pp?\.)\s*(?:\d+\s*(?:[-–,]|and)\s*)*%d\b" % n, src):
                continue
            bad.append("page %d is outside the lesson and not a page the pages cite" % n)
    return bad


_JUDGMENT = re.compile(r"^(?:rigor comes from|the rigor|anticipated difficulties|likely difficulties|"
                       r"students may (?:struggle|find)|some students may)", re.I)


def checkable_text(d: dict) -> str:
    """What the fact check reads: the factual parts. Why it matters, where the
    rigor comes from and likely difficulties are judgment the teacher asked
    for (S629 Module 2 Lesson 34: flagged as "invented" at every stage)."""
    parts = []
    for k in ("student_considerations", "plan", "next_steps"):
        sents = re.split(r"(?<=[.!?])\s+", d.get(k, "") or "")
        parts.append(" ".join(x for x in sents if not _JUDGMENT.match(x.strip())))
    return "\n".join([*d.get("i_can", []), *parts, *d.get("success_criteria", [])])


def review_text_fields(d: dict, with_note: bool = True) -> str:
    return "\n".join([*d["i_can"], d["why_it_matters"], d["student_considerations"], d["plan"],
                      *d["success_criteria"], d["next_steps"],
                      d.get("standards_note", "") if with_note else ""])


def check_review(d: dict, L: dict, codes: list, src: str) -> list:
    f = []
    for k, n in (("why_it_matters", 40), ("student_considerations", 150), ("plan", 300),
                 ("next_steps", 40)):
        if len((d.get(k) or "").strip()) < n:
            f.append("%s is missing or too short" % k)
    if not all(s.strip().startswith("I can ") for s in d.get("i_can", [])):
        f.append('every learning goal must start "I can"')
    if not all(s.strip().startswith("Students will ") for s in d.get("success_criteria", [])):
        f.append('every success criterion must start "Students will"')
    text = review_text_fields(d)
    # "___" is a sentence-frame blank (Module 2 Lesson 27), not bold markup.
    if re.search(r"\*\*|(?<!_)__(?=[A-Za-z])|`|^\s*#|^\s*[-*•]\s", text, re.M):
        f.append("markdown or bullet characters in the text")
    named = set(_CODE_REF.findall(text))
    extra = named - {c for c, _ in codes}
    if extra:
        f.append("names codes not assigned to this lesson: %s" % ", ".join(sorted(extra)))
    if len(re.findall(r"\bmin(?:ute)?s?\b", d.get("plan", ""))) < 2:
        f.append("the plan does not give the parts' minutes")
    book_minutes = {int(x) for x in re.findall(r"\b(\d+) minutes?\b", src)}
    for x in sorted({int(x) for x in re.findall(r"\b(\d+)\s*(?:-\s*)?min(?:ute)?s?\b", text)}):
        if x not in book_minutes:
            f.append("%d minutes is not a timing in the pages" % x)
    s = norm(src)
    return f + _quotes_ok(text, s) + _pages_ok(text, L, src)


def check_mcq(d: dict, L: dict, src: str) -> list:
    f = []
    qs = d.get("questions") or []
    if not 3 <= len(qs) <= 4:
        f.append("needs 3 or 4 questions, has %d" % len(qs))
    if not (d.get("focus") or "").strip():
        f.append("focus is missing")
    s = norm(src)
    for i, q in enumerate(qs, 1):
        opts = [q.get("correct", "")] + list(q.get("wrong") or [])
        if len(opts) != 4 or len({norm(o) for o in opts}) != 4 or not all(norm(o) for o in opts):
            f.append("question %d: needs four different options" % i)
        if any(re.search(r"\b(all|none) of the above\b", o, re.I) for o in opts):
            f.append("question %d: 'all/none of the above' option" % i)
        # A definition is naturally longer; twice the longest wrong answer gives it away.
        if len(opts) == 4 and len(opts[0]) > 2 * max(len(o) for o in opts[1:]) + 10:
            f.append("question %d: the correct answer gives itself away by length" % i)
        if q.get("tag") not in ("target", "vocab", "word study"):
            f.append("question %d: bad tag" % i)
        f += ["question %d: %s" % (i, x) for x in _quotes_ok(" ".join([q.get("stem", "")] + opts), s)]
        f += ["question %d: %s" % (i, x) for x in _pages_ok(q.get("stem", ""), L, src)]
    return f


CLAIM_SYSTEM = ("You check teaching material against the Teach Book pages it was written from. "
                "You are strict about facts and relaxed about wording.")
CLAIM_TASK = """Below are a lesson's Teach Book pages and a draft written from them for the teacher.

List each statement in the draft that states a specific fact the pages do not support, with its kind:
- "contradicted": the pages say something different (a wrong event, character, definition or activity). Give in "evidence" the exact words from the pages that show what they really say.
- "invented": a specific detail that appears nowhere in the pages, such as a story event, a character's action, a definition, an activity or a material. Leave "evidence" empty.

Do not list interpretation or judgment, which the teacher asked for: why the lesson matters, where its rigor comes from, likely difficulties for students, suggestions for supporting students, or how the lesson's parts connect. Do not check minutes, page numbers or PA Core codes; those are checked separately.
If every specific fact is supported, return an empty list.

TEACH BOOK PAGES:
{pages}

DRAFT:
{draft}"""
CLAIM_SCHEMA = {"type": "object", "properties": {"unsupported": {"type": "array", "items": {
    "type": "object", "properties": {"statement": {"type": "string"}, "reason": {"type": "string"},
                                     "kind": {"type": "string", "enum": ["contradicted", "invented"]},
                                     "evidence": {"type": "string"}},
    "required": ["statement", "reason", "kind", "evidence"]}}}, "required": ["unsupported"]}

SUPPORT_TASK = """Here are a lesson's Teach Book pages and one statement from a teacher's summary of the lesson.

Is the statement supported by the pages? A fair paraphrase counts as supported. If it is supported, give in "quote" the exact words from the pages that support it (one passage, copied exactly). If it is not supported, leave "quote" empty.

TEACH BOOK PAGES:
{pages}

STATEMENT:
{statement}"""
SUPPORT_SCHEMA = {"type": "object", "properties": {"supported": {"type": "boolean"}, "quote": {"type": "string"}},
                  "required": ["supported", "quote"]}

ANSWER_SYSTEM = "You answer grade 4 multiple-choice questions using only the Teach Book pages given."
ANSWER_TASK = """Answer each question using only these Teach Book pages. For each, give the letter of the best answer, and list any OTHER letter that would also be a correct answer according to the pages (usually none).

TEACH BOOK PAGES:
{pages}

QUESTIONS:
{questions}"""
ANSWER_SCHEMA = {"type": "object", "properties": {"answers": {"type": "array", "items": {
    "type": "object", "properties": {
        "question": {"type": "integer"},
        "answer": {"type": "string", "enum": ["A", "B", "C", "D"]},
        "also_correct": {"type": "array", "items": {"type": "string", "enum": ["A", "B", "C", "D"]}}},
    "required": ["question", "answer", "also_correct"]}}}, "required": ["answers"]}


def claim_check(draft_text: str, src: str, cfg: dict) -> list:
    """The checker's flags count only when they hold up: a "contradicted" flag
    must quote the pages (S629: it once called six correct timings wrong), and
    an "invented" flag gets a second look asking for supporting words."""
    r = local_check(CLAIM_SYSTEM, CLAIM_TASK.format(pages=src, draft=draft_text), CLAIM_SCHEMA, cfg)
    s, out = norm(src), []
    for u in r.get("unsupported", [])[:8]:
        if u["kind"] == "contradicted":
            ev = norm(u.get("evidence", ""))
            if len(ev.split()) < 3 or ev not in s:
                continue    # no real passage behind it
        # Second look for every flag: S629 Lesson 5, the checker called two
        # correct statements "contradicted" and quoted passages that support them.
        try:
            back = local_check(CLAIM_SYSTEM, SUPPORT_TASK.format(pages=src, statement=u["statement"]),
                               SUPPORT_SCHEMA, cfg, max_tokens=400)
        except Exception:
            back = {}
        q = norm(back.get("quote", ""))
        if back.get("supported") and len(q.split()) >= 3 and q in s:
            continue    # the pages do say it
        out.append("%s: %s (%s)" % (u["kind"], u["statement"][:160], u["reason"][:120]))
    return out


def blind_answer(d: dict, src: str, cfg: dict, seed: str) -> list:
    """A second model answers without the key; it must pick the keyed answer
    and see no other correct option."""
    rng = random.Random(seed)
    blocks, keys = [], []
    for i, q in enumerate(d["questions"], 1):
        opts = [q["correct"]] + list(q["wrong"])
        rng.shuffle(opts)
        keys.append("ABCD"[opts.index(q["correct"])])
        blocks.append("%d. %s\n%s" % (i, q["stem"], "\n".join(
            "%s. %s" % ("ABCD"[j], o) for j, o in enumerate(opts))))
    r = local_check(ANSWER_SYSTEM, ANSWER_TASK.format(pages=src, questions="\n\n".join(blocks)),
                    ANSWER_SCHEMA, cfg, max_tokens=800)
    got = {a["question"]: a for a in r.get("answers", [])}
    f = []
    for i, k in enumerate(keys, 1):
        a = got.get(i)
        if not a:
            f.append("question %d: the checker gave no answer" % i)
        elif a["answer"] != k:
            f.append("question %d: the checker chose a different answer from the key" % i)
        elif [x for x in a.get("also_correct", []) if x != k]:
            f.append("question %d: the checker found a second correct option" % i)
    return f


def mcq_text(d: dict) -> str:
    """What the fact check reads: stems and correct answers only. Wrong answers
    are false on purpose (S629 Lesson 6: the checker flagged them as invented)."""
    return "\n".join("%d. %s\nCorrect answer: %s" % (i, q["stem"], q["correct"])
                     for i, q in enumerate(d.get("questions", []), 1))


# ── one lesson: local, one local repair, cloud, then hold ────────────────────

def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    for k in ("draft", "check"):
        cfg[k].update(data.get(k) or {})
    cfg["clients"] = data.get("clients") or {}
    cfg["host"] = data.get("host")
    return cfg


def _example(kind: str) -> str:
    ex = json.loads(EXAMPLE_PATH.read_text())
    return ex["review"] if kind == "lessons" else json.dumps(ex["mcq"], indent=1, ensure_ascii=False)


def task_prompt(kind: str, book: dict, L: dict, codes: list, src: str) -> str:
    base = dict(level=book["level"], module=book["module"], n=L["n"], title=L["title"],
                rules=RULES, example=_example(kind), pages=src)
    if kind == "lessons":
        return REVIEW_TASK.format(p1=L["printed"][0], p2=L["printed"][1], q1=L["plan"][0],
                                  q2=L["plan"][1], codes="\n".join("- %s — %s" % c for c in codes)
                                  or "- none assigned", **base)
    return MCQ_TASK.format(assessment=ASSESSMENT_NOTE if "Assessment" in L["label"] else "", **base)


def lesson_failures(kind: str, d: dict, book: dict, L: dict, codes: list, src: str, cfg: dict,
                    model_checks: bool = True) -> list:
    if kind == "lessons":
        f = check_review(d, L, codes, src)
        if not f and model_checks:
            # The note links guide codes to activities; the checker cannot see
            # the guide and calls every code "invented". Codes are checked above.
            f = claim_check(checkable_text(d), src, cfg)
    else:
        f = check_mcq(d, L, src)
        if not f and model_checks:
            f = blind_answer(d, src, cfg, "%s-%d" % (book["module"], L["n"]))
            if not f:
                f = claim_check(mcq_text(d), src, cfg)
    return f


def build_lesson(kind: str, book: dict, L: dict, codes: list, src: str, cfg: dict, ctx: dict) -> dict:
    """Returns {"draft", "stage", "tries"}; raises Hold after the last stage."""
    prompt = task_prompt(kind, book, L, codes, src)
    schema = REVIEW_SCHEMA if kind == "lessons" else MCQ_SCHEMA
    tries, prev = [], None
    for stage in ("local", "local-repair", "cloud"):
        t0 = time.time()
        try:
            if stage == "local":
                d = local_draft(prompt, schema, cfg)
            elif stage == "local-repair":
                d = local_draft(prompt + "\n\nYour previous draft:\n" + json.dumps(prev, ensure_ascii=False)
                                + "\n\nIt failed these checks:\n- " + "\n- ".join(tries[-1]["failures"])
                                + "\n\nFix them and keep everything else that was right.", schema, cfg)
            else:
                if ctx.get("no_cloud"):
                    tries.append({"stage": stage, "failures": ["cloud fallback is off for this run"]})
                    break
                if ctx.setdefault("cloud_calls", 0) >= MAX_CLOUD_CALLS_PER_JOB:
                    tries.append({"stage": stage, "failures": ["this job's cloud allowance is used up"]})
                    break
                ctx["cloud_calls"] += 1
                try:
                    provider, d = cloud_draft(prompt, schema, ctx["creds"])
                except ValueError:      # a reply that is not JSON gets one retry (Lesson 34)
                    ctx["cloud_calls"] += 1
                    provider, d = cloud_draft(prompt, schema, ctx["creds"])
                stage = "cloud:" + provider
            failures = lesson_failures(kind, d, book, L, codes, src, cfg)
        except Hold:
            raise
        except Exception as e:      # an outage or bad JSON is a failed try, not a crash
            d, failures = prev, ["%s did not complete (%s)" % (stage, type(e).__name__)]
        tries.append({"stage": stage, "seconds": round(time.time() - t0, 1), "failures": failures})
        if d is not None and not failures:
            return {"draft": d, "stage": stage, "tries": tries}
        prev = d if d is not None else prev
    held = Hold("Lesson %d %s failed its checks: %s" % (
        L["n"], "lesson plan" if kind == "lessons" else "questions",
        "; ".join(f for t in tries[-2:] for f in t["failures"][:2]) or "no draft"))
    held.record = {"tries": tries, "last_draft": prev}
    raise held


# ── assembly (the layout Alyssa approved; S621 build.py) ─────────────────────

def review_block(d: dict, L: dict, codes: list) -> str:
    if codes:
        std = "\n".join("- %s — %s" % c for c in codes)
        if d.get("standards_note", "").strip():
            std += "\n\n" + d["standards_note"].strip()
    else:
        std = ("None assigned. Great Minds' PA alignment guide does not code Lesson %d to any "
               "standard." % L["n"])
    return "\n\n".join([
        "LESSON %d — %s (pp. %d-%d)" % (L["n"], L["title"], *L["printed"]),
        LABELS[0], std,
        LABELS[1], "\n".join("- " + s.strip() for s in d["i_can"]),
        "Why it matters: " + d["why_it_matters"].strip(),
        LABELS[2], d["student_considerations"].strip(),
        "%s (pp. %d-%d)" % (LABELS[3], *L["plan"]), d["plan"].strip(),
        LABELS[4], "\n".join("- " + s.strip() for s in d["success_criteria"]),
        LABELS[5], d["next_steps"].strip()])


def _plain(x):
    """Ordinary spaces and hyphens in what we send (models emit U+202F and U+2011)."""
    if isinstance(x, str):
        return x.replace("\u202f", " ").replace("\u2011", "-").replace("\u00a0", " ")
    if isinstance(x, list):
        return [_plain(v) for v in x]
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items()}
    return x


def arc_label(arc: str, lessons: list) -> str:
    if len(lessons) == 1:
        return "%s (Lesson %d)" % (arc, lessons[0]["n"])
    return "%s (Lessons %d-%d)" % (arc, lessons[0]["n"], lessons[-1]["n"])


def assemble(kind: str, arc: str, lessons: list, drafts: dict, codes: dict, module: int,
             name: str, box: str, seed: str, other_thread: bool) -> str:
    sep = "\n\n%s\n\n" % SEP
    drafts = {n: _plain(d) for n, d in drafts.items()}
    label = arc_label(arc, lessons)
    outro = sep + FIT_CHECK + "\n\n— " + box + "\n"
    if kind == "lessons":
        intro = ("Hi %s,\n\nHere is %s of Module %d in the six-part format, ready to copy and paste."
                 % (name, label, module))
        if other_thread:
            intro += (" The checks for understanding for this arc are in a separate email on "
                      "your other thread.")
        intro += ("\n\nEach lesson lists every PA Core code Great Minds' alignment guide assigns "
                  "to it, with the standard's wording, and page numbers are the printed page "
                  "numbers in your Teach Book.")
        body = intro + sep + sep.join(review_block(drafts[L["n"]], L, codes.get(L["n"], []))
                                      for L in lessons) + outro
    else:
        rng = random.Random(seed)
        out, key = [], []
        for L in lessons:
            d = drafts[L["n"]]
            out.append("LESSON %d — %s\nFocus: %s\n" % (L["n"], L["title"], d["focus"].strip()))
            for _ in range(50):     # no answer letter more than twice in one lesson
                shuffled = []
                for q in d["questions"]:
                    opts = [q["correct"]] + list(q["wrong"])
                    rng.shuffle(opts)
                    shuffled.append((q, opts, "ABCD"[opts.index(q["correct"])]))
                if max(collections.Counter(s[2] for s in shuffled).values()) <= 2:
                    break
            ks = []
            for i, (q, opts, k) in enumerate(shuffled, 1):
                ks.append("%d-%s" % (i, k))
                out.append("%d. (%s) %s\n%s\n" % (i, q["tag"], q["stem"].strip(), "\n".join(
                    "%s. %s" % ("ABCD"[j], o.strip()) for j, o in enumerate(opts))))
            key.append("Lesson %d: %s" % (L["n"], ", ".join(ks)))
            out.append(SEP + "\n")
        intro = ("Hi %s,\n\nHere are the checks for understanding for Module %d, %s: "
                 "multiple-choice questions for each lesson, drawn from its learning targets, "
                 "vocabulary, and word study, each tagged (target), (vocab), or (word study). "
                 "The answer key is at the end, so the question pages print clean."
                 % (name, module, label))
        body = (intro + sep + "\n".join(out) + "\nANSWER KEY — %s\n\n" % label.split(" (")[0]
                + "\n".join(key) + outro)
        verify_key(body, lessons, drafts)
    if "**" in body or re.search(r"(?m)^#", body):
        raise Hold("markdown in the assembled %s email for %s" % (kind, arc))
    return body


def verify_key(body: str, lessons: list, drafts: dict) -> None:
    """Re-read the assembled email: every key letter must point at the intended answer."""
    for L in lessons:
        sec = body.split("LESSON %d — " % L["n"])[1].split(SEP)[0]
        line = re.search(r"(?m)^Lesson %d: (.+)$" % L["n"], body).group(1)
        kmap = dict(x.split("-") for x in line.split(", "))
        for i, q in enumerate(drafts[L["n"]]["questions"], 1):
            blk = sec.split("\n%d. (" % i)[1].split("\n%d. (" % (i + 1))[0]
            m = re.search(r"(?m)^%s\. (.+)$" % kmap[str(i)], blk)
            if not m or m.group(1) != q["correct"].strip():
                raise Hold("answer key check failed at Lesson %d question %d" % (L["n"], i))


# ── the whole book ───────────────────────────────────────────────────────────

def build_book(pdf: Path, kinds: list, out_dir: Path, cfg: dict, ctx: dict, *, name: str = "Alyssa",
               box: str = "CUMULUS", lessons_filter=None, other_thread=None,
               on_arc=None) -> dict:
    """Draft, check and assemble every arc in order; drafts are cached in
    out_dir/drafts so a rerun resumes. on_arc(arc, {kind: (path, body)}) is
    called as each arc is ready (the delivery step); it may raise Hold."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "drafts").mkdir(exist_ok=True)
    (out_dir / "mail").mkdir(exist_ok=True)
    pages = pdf_pages(pdf)
    book = split_book(pages, pdf_title(pdf))
    guide = json.loads(GUIDE_PATH.read_text())
    if guide["level"] != book["level"]:
        raise Hold("no alignment guide for Level %d (we hold Level %d)" % (book["level"], guide["level"]))
    codes = codes_by_lesson(guide, book["module"])
    stats = {"module": book["module"], "lessons": {}, "arcs": [], "started": time.time()}
    for arc in book["arcs"]:
        lessons = [L for L in book["lessons"] if L["arc"] == arc
                   and (lessons_filter is None or L["n"] in lessons_filter)]
        if not lessons:
            continue
        ready = {}
        for kind in kinds:
            drafts = {}
            for L in lessons:
                cache = out_dir / "drafts" / ("%s-%02d.json" % (kind, L["n"]))
                if cache.exists():
                    got = json.loads(cache.read_text())
                else:
                    src = lesson_text(pages, book, L)
                    try:
                        got = build_lesson(kind, book, L, codes.get(L["n"], []), src, cfg, ctx)
                    except Hold as e:
                        cache.with_suffix(".held.json").write_text(json.dumps(
                            getattr(e, "record", {}), indent=1, ensure_ascii=False))
                        if not ctx.get("keep_going"):
                            raise
                        # Qualification only: note it and test the rest.
                        stats.setdefault("held", []).append(str(e))
                        stats["lessons"]["%s-%d" % (kind, L["n"])] = {
                            "stage": "held", "tries": e.record["tries"]}
                        continue
                    cache.write_text(json.dumps(got, indent=1, ensure_ascii=False))
                drafts[L["n"]] = got["draft"]
                stats["lessons"]["%s-%d" % (kind, L["n"])] = {"stage": got["stage"], "tries": got["tries"]}
            if len(drafts) < len(lessons):
                continue
            body = assemble(kind, arc, lessons, drafts, codes, book["module"], name, box,
                            seed="%s-%s" % (out_dir.name, arc),
                            other_thread=(len(kinds) > 1) if other_thread is None else other_thread)
            path = out_dir / "mail" / ("%s-%s.txt" % (re.sub(r"\W", "", arc), kind))
            path.write_text(body)
            ready[kind] = (path, body)
        stats["arcs"].append(arc)
        (out_dir / "stats.json").write_text(json.dumps(stats, indent=1, default=str))
        if on_arc:
            on_arc(arc, lessons, ready)
    stats["seconds"] = round(time.time() - stats.pop("started"), 1)
    stats["cloud_calls"] = ctx.get("cloud_calls", 0)
    (out_dir / "stats.json").write_text(json.dumps(stats, indent=1))
    return {"book": book, "stats": stats}


# ── a client's job: start (from intake), run, deliver ────────────────────────

class DailyCap(Exception):
    pass


_LESSONS_RX = re.compile(r"(?i)\blesson plans?\b|\bi can statements?\b|\bsuccess criteria\b|\bsix[- ]part\b")
_CHECKS_RX = re.compile(r"(?i)\bmultiple[- ]choice\b|\bchecks? for understanding\b|\banswer keys?\b|\bmcqs?\b")
_TEACH_RX = re.compile(r"Level (\d+) Module (\d+) Teach")


def kinds_in(text: str) -> list:
    return [k for k, rx in (("lessons", _LESSONS_RX), ("checks", _CHECKS_RX)) if rx.search(text or "")]


def _bare(subject: str) -> str:
    return re.sub(r"(?i)^\s*((re|fwd?|fw)\s*:\s*)+", "", subject or "").strip()


def supported(subject: str, own_words: str, attachments: list, guide_path: Path = GUIDE_PATH,
              config_path: Path = CONFIG_PATH, host: str = None):
    """What the job can build for this email, or None, which leaves it for a
    session as before (S621). Needs an attached Teach Book PDF of a level we
    hold a guide for, and a deliverable this job knows: lesson plans, checks."""
    import socket
    try:
        if json.loads(Path(config_path).read_text()).get("host") != (host or socket.gethostname()):
            return None     # only the box with the drafting and checking models
    except (OSError, ValueError):
        return None
    book = None
    for a in attachments or []:
        m = _TEACH_RX.search(a.get("title") or "")
        if m and (a.get("name") or "").lower().endswith(".pdf"):
            book = {"name": a["name"], "level": int(m.group(1)), "module": int(m.group(2))}
            break
    if not book:
        return None
    try:
        if json.loads(Path(guide_path).read_text())["level"] != book["level"]:
            return None
    except (OSError, ValueError, KeyError):
        return None
    kinds = kinds_in(own_words) or kinds_in(_bare(subject))
    return dict(book, kinds=kinds) if kinds else None


def _waiting_requests(project_dir: Path, client: str, module: int) -> list:
    """Her other requests for this module that are waiting on a file (the
    S621 case: two requests, one corrected file sent on one thread)."""
    import intake_lifecycle
    out = []
    with intake_lifecycle.connect(project_dir) as db:
        for r in db.execute("SELECT payload, evidence FROM requests WHERE state='waiting_on_client'"):
            row, ev = json.loads(r["payload"]), json.loads(r["evidence"])
            subject, body = row[2] or "", row[3] or ""
            if ev.get("client") != client or not re.search(
                    r"(?i)\bmodule\s*%d\b" % module, subject + "\n" + body[:4000]):
                continue
            kinds = kinds_in(_bare(subject) + "\n" + body[:4000])
            if kinds:
                out.append({"subject": subject, "from_addr": row[1], "mid": row[4], "kinds": kinds})
    return out


def _new_run_id(client: str, now: datetime) -> str:
    who = re.sub(r"[^a-z0-9]", "", (client or "").lower())[:12] or "client"
    return "lb-%s-%s" % (who, now.strftime("%Y%m%d-%H%M%S"))


def start_job(client: str, from_addr: str, mid: str, subject: str, found: dict,
              project_dir: Path = PROJECT_DIR, popen=None, now=None) -> str:
    """One job per original client message; launches `run` detached through
    job_runner and returns the run id. Raises DailyCap past the cap."""
    import fcntl
    import intake_lifecycle
    if not client or not mid or not found or not found.get("kinds"):
        raise ValueError("client, Message-ID and a supported request are required")
    now = now or datetime.now()
    req_dir = Path(project_dir) / REQUESTS_REL
    req_dir.mkdir(parents=True, exist_ok=True)
    with (req_dir / ".launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        today = 0
        for p in req_dir.glob("lb-*.json"):
            meta = json.loads(p.read_text())
            if meta.get("client") == client and any(t["mid"] == mid for t in meta["threads"]):
                return p.stem
            if meta.get("client") == client and meta.get("requested", "")[:10] == now.date().isoformat():
                today += 1
        if today >= MAX_JOBS_PER_CLIENT_PER_DAY:
            raise DailyCap("%s already has %d lesson build(s) today" % (client, today))
        threads = [{"subject": subject, "from_addr": from_addr, "mid": mid,
                    "kinds": list(found["kinds"]), "book": True}]
        for w in _waiting_requests(project_dir, client, found["module"]):
            if w["mid"] != mid:
                covered = {k for t in threads for k in t["kinds"]}
                threads.append(dict(w, kinds=[k for k in w["kinds"] if k not in covered]))
        cfg = load_config(Path(project_dir) / "config/lesson_build.json")
        run_id = _new_run_id(client, now)
        meta = {"client": client, "threads": threads, "book": found, "from_intake": True,
                "auto_send": bool((cfg["clients"].get(client) or {}).get("auto_send")),
                "requested": now.isoformat(timespec="seconds")}
        (req_dir / (run_id + ".json")).write_text(json.dumps(meta, indent=1))
    for t in threads[1:]:
        try:
            intake_lifecycle.update(project_dir, [None, t["from_addr"], None, None, t["mid"]],
                                    "working", "lesson_build",
                                    "answered by lesson build %s" % run_id, {"lesson_build_run": run_id})
        except ValueError:
            pass
    venv = Path(project_dir) / ".venv/bin/python"
    py = str(venv) if venv.exists() else sys.executable
    (popen or subprocess.Popen)(
        [py, "-u", "job_runner.py", run_id, "lesson_build.py", "run", run_id],
        cwd=str(project_dir), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    return run_id


def _ledger(runs: Path) -> Path:
    return runs / "deliveries.jsonl"


def delivered(runs: Path, run_id: str, arc: str, kind: str):
    try:
        for line in _ledger(runs).read_text().splitlines():
            row = json.loads(line)
            if (row.get("run_id"), row.get("arc"), row.get("kind")) == (run_id, arc, kind):
                return row
    except (OSError, ValueError):
        pass
    return None


def extract_book(project_dir: Path, meta: dict, out: Path) -> Path:
    """The Teach Book from intake's raw capture of the email that carried it."""
    import email
    import intake_lifecycle
    t = next(t for t in meta["threads"] if t.get("book"))
    raw = (Path(project_dir) / "data/intake-lifecycle" /
           (intake_lifecycle.identity(t["from_addr"], t["mid"]) + ".eml")).read_bytes()
    for part in email.message_from_bytes(raw).walk():
        if part.get_filename() == meta["book"]["name"]:
            out.write_bytes(part.get_payload(decode=True))
            out.chmod(0o600)
            return out
    raise Hold("the attached book is not in intake's copy of the email")


def verify_sent(project_dir: Path, rows: list, recipients: list, creds: dict,
                connect=None) -> list:
    """Read-only: one Sent Mail copy per delivered email (From this box, To the
    client, Cc Buddy, exact subject). Returns the rows that were found."""
    import imaplib
    import contact_delivery_evidence
    import intake
    from runtime_config import load_sources
    from client_mail_review import quoted
    account = intake.find_account(load_sources(intake.CONFIG_PATH))
    if not account or not rows:
        return []
    from list_delivery import CC_ADDR
    since = (datetime.fromisoformat(rows[0]["ts"])).strftime("%d-%b-%Y")
    found = []
    with (connect or imaplib.IMAP4_SSL)(account["imap_server"], account.get("imap_port", 993),
                                         timeout=15) as mail:
        mail.login(account["address"], creds[account["credential_key"]])
        if mail.select('"[Gmail]/Sent Mail"', readonly=True)[0] != "OK":
            return []
        uids = []
        for r in recipients:
            status, ids = mail.uid("search", None, "SINCE", since, "TO", quoted(r))
            if status == "OK":
                uids += [u for u in ids[0].split() if u not in uids]
        raws = []
        for uid in uids[-100:]:
            status, parts = mail.uid("fetch", uid, "(BODY.PEEK[])")
            if status == "OK":
                raws.append(b"".join(x[1] for x in parts if isinstance(x, tuple)))
        for row in rows:
            after = datetime.fromisoformat(row["ts"]).timestamp() - 600
            hits = [x for x in raws if contact_delivery_evidence.matches(
                x, account["address"], recipients, CC_ADDR, row["subject"], None, after)]
            if len(hits) == 1:
                found.append(row)
    return found


def run(run_id: str, project_dir: Path = PROJECT_DIR, send=None, creds=None, notify=None,
        verify=None, cfg=None, wait: int = 60) -> int:
    """The job intake launched. Exit 0 delivered (or built, in shadow), 3 held."""
    import intake_lifecycle
    project_dir = Path(project_dir)
    runs = project_dir / "logs/lesson_builds"
    run_dir = runs / run_id
    meta = json.loads((project_dir / REQUESTS_REL / (run_id + ".json")).read_text())
    run_dir.mkdir(parents=True, exist_ok=True)
    if meta.get("from_intake") and wait:
        time.sleep(wait)        # let intake's poll record the request first
    creds = creds if creds is not None else json.loads((project_dir / "config/credentials.json").read_text())
    cfg = cfg or load_config(project_dir / "config/lesson_build.json")
    rows = [[None, t["from_addr"], None, None, t["mid"]] for t in meta["threads"]]
    # Every email this run has sent, including earlier invocations (the ledger).
    try:
        prior = [json.loads(x) for x in _ledger(runs).read_text().splitlines()]
    except (OSError, ValueError):
        prior = []
    state = {"run_id": run_id, "client": meta["client"], "auto_send": meta["auto_send"],
             "sent": [r for r in prior if r.get("run_id") == run_id]}

    def save(**kw):
        state.update(kw)
        (run_dir / "state.json").write_text(json.dumps(state, indent=1))

    def lifecycle(st, owner, action, evidence=None):
        for row in rows:
            try:
                intake_lifecycle.update(project_dir, row, st, owner, action,
                                        dict({"lesson_build_run": run_id}, **(evidence or {})))
            except ValueError:
                pass

    def tell(text):
        try:
            if notify:
                notify(text)
            else:
                import intake
                intake.telegram(text, creds)
        except Exception:
            pass

    kinds = [k for k in KINDS if any(k in t["kinds"] for t in meta["threads"])]
    thread_for = {k: next(t for t in meta["threads"] if k in t["kinds"]) for k in kinds}
    other = len(kinds) > 1 and thread_for["lessons"] is not thread_for["checks"]
    save(status="building", kinds=kinds)
    try:
        senders = json.loads((project_dir / "config/intake_senders.json").read_text())
        entry = senders.get(meta["client"])
        if not isinstance(entry, dict) or not entry.get("emails"):
            raise Hold("client %r is not in intake_senders.json" % meta["client"])
        import mailer
        from list_delivery import CC_ADDR
        from_email = creds.get("outlook_email", "")
        box = mailer.sender_name(from_email, creds)
        pdf = run_dir / "book.pdf"
        if not pdf.exists():
            extract_book(project_dir, meta, pdf)

        def on_arc(arc, lessons, ready):
            for kind in kinds:
                path, body = ready[kind]
                subject = "Re: %s — %s" % (_bare(thread_for[kind]["subject"]), arc_label(arc, lessons))
                # Send-ready copy: a session can send it as is (cumulus-client-mail).
                path.with_suffix(".md").write_text("Subject: %s\n\n%s" % (subject, body))
                if not meta["auto_send"] or delivered(runs, run_id, arc, kind):
                    continue
                (send or mailer.send)(from_email, creds.get("outlook_password", ""), entry["emails"][0],
                                      subject, body, cc=CC_ADDR, creds=creds, client=meta["client"],
                                      project=(entry.get("projects") or ["general"])[0])
                row = {"event": "delivered", "run_id": run_id, "arc": arc, "kind": kind,
                       "subject": subject, "client": meta["client"],
                       "ts": datetime.now().isoformat(timespec="seconds")}
                with _ledger(runs).open("a") as f:
                    f.write(json.dumps(row) + "\n")
                state["sent"].append(row)
                save()
                try:
                    import client_mail
                    client_mail._record_sent(meta["client"], subject,
                                             str(path.relative_to(project_dir)), project_dir)
                except Exception:
                    pass

        ctx = {"creds": creds}
        res = build_book(pdf, kinds, run_dir, cfg, ctx, name=meta["client"].capitalize(), box=box,
                         other_thread=other, on_arc=on_arc)
    except Hold as e:
        save(status="held", reason=str(e))
        lifecycle("blocked_on_evidence", "cowork",
                  "lesson build held: %s; a session finishes it from logs/lesson_builds/%s" % (str(e)[:200], run_id))
        tell("\U0001F4C4 Lesson build %s for %s is HELD: %s\n%d email(s) already sent. "
             "A session finishes it from logs/lesson_builds/%s." % (
                 run_id, meta["client"], str(e)[:300], len(state["sent"]), run_id))
        return 3
    if not meta["auto_send"]:
        save(status="built", stats=res["stats"])
        lifecycle("ready_for_send", "cowork", "shadow build: review the emails, then send",
                  {"artifact": str(run_dir / "mail")})
        tell("\U0001F4C4 Lesson build %s for %s is built (shadow mode, nothing sent). A session "
             "reviews logs/lesson_builds/%s/mail/*.md and sends them in arc order." % (
                 run_id, meta["client"], run_id))
        return 0
    time.sleep(20 if wait else 0)
    try:
        found = (verify or verify_sent)(project_dir, state["sent"], entry["emails"], creds)
    except Exception:
        found = []
    if state["sent"] and len(found) == len(state["sent"]):
        receipt = run_dir / "sent-receipt.json"
        receipt.write_text(json.dumps({"verified": True, "emails": found}, indent=1))
        save(status="delivered", stats=res["stats"])
        lifecycle("sent_verified", "delivery-review", "completed with actual Sent Mail proof",
                  {"sent_receipt": str(receipt)})
    else:
        save(status="sent_unverified", stats=res["stats"])
        lifecycle("sent_unverified", "delivery-review",
                  "verify actual Sent Mail for lesson build %s; never resend" % run_id)
        tell("\U0001F4C4 Lesson build %s for %s: %d email(s) sent, but only %d found in Sent "
             "Mail. Check before anything is resent." % (run_id, meta["client"], len(state["sent"]),
                                                       len(found)))
    return 0


# ── command line ─────────────────────────────────────────────────────────────

def _range(s: str):
    if not s:
        return None
    a, _, b = s.partition("-")
    return set(range(int(a), int(b or a) + 1))


def main(argv: list) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="lesson_build.py")
    sub = ap.add_subparsers(dest="cmd")
    sh = sub.add_parser("shadow", help="build from a PDF into a folder; never sends")
    sh.add_argument("--pdf", required=True)
    sh.add_argument("--out", required=True)
    sh.add_argument("--kinds", default="lessons,checks")
    sh.add_argument("--lessons", default="")
    sh.add_argument("--no-cloud", action="store_true")
    sh.add_argument("--keep-going", action="store_true",
                    help="qualification: record a held lesson and carry on")
    rn = sub.add_parser("run", help="the job intake launches")
    rn.add_argument("run_id")
    sub.add_parser("selftest")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        return run(a.run_id)
    if a.cmd == "shadow":
        ctx = {"no_cloud": a.no_cloud, "keep_going": a.keep_going}
        if not a.no_cloud:
            ctx["creds"] = json.loads((PROJECT_DIR / "config/credentials.json").read_text())
        try:
            res = build_book(Path(a.pdf), a.kinds.split(","), Path(a.out), load_config(), ctx,
                             lessons_filter=_range(a.lessons))
        except Hold as e:
            print("HELD:", e)
            return 3
        print(json.dumps(res["stats"], indent=1)[:4000])
        return 0
    if a.cmd == "selftest":
        return 0 if selftest() else 1
    ap.print_help()
    return 2


def selftest() -> bool:
    """Offline: temp folders, fake models and a fake send; no live files (T32)."""
    import tempfile
    ok = [True]

    def check(name, cond):
        print(("  ok   " if cond else "  FAIL ") + name)
        ok[0] = ok[0] and bool(cond)

    # guide: a page number that sits apart from its footer is not a lesson (S621/S629 trap)
    guide_txt = "\n".join([
        "4| Pennsylvania Core Standards English Language Arts Correlation to Arts & Letters",
        "Pennsylvania Standards", "CC.1.4.4.L", "Demonstrate a command of", "standard English.",
        "© 2025 Great Minds PBC | greatminds.org", "Aligned Components of Arts & Letters",
        "Level 4, Module 3, Lessons 8, 10, 12,", "38, 39", "", "14", "",
        "4| Pennsylvania Core Standards English Language Arts Correlation to Arts & Letters",
        "CC.1.4.4.N", "Aligned Components of Arts & Letters", "Level 4, Module 2, Lessons 10, 13",
        "Orient the reader by establishing a situation.", "•", "A bullet that is not the standard.",
        "Level 4, Module 2, Lessons 99"])
    g = parse_guide(guide_txt)
    check("guide: page number 14 is not read as a lesson",
          g["codes"][0]["modules"] == {"3": [8, 10, 12, 38, 39]})
    check("guide: text after the lesson list is the standard; bullets are not",
          g["codes"][1]["text"] == "Orient the reader by establishing a situation."
          and g["codes"][1]["modules"] == {"2": [10, 13]})
    check("guide: codes by lesson", codes_by_lesson(g, 2) == {
        10: [("CC.1.4.4.N", "Orient the reader by establishing a situation.")],
        13: [("CC.1.4.4.N", "Orient the reader by establishing a situation.")]})

    # book: contents, openers, running heads, Launch/Land, descriptor pages
    def head(n, arc="Arc A"):
        return "4 | Module 2 | %s | Lesson %d" % (arc, n)
    toc = "\n".join(["Arc A | Gifts", "L1 | Opening Bookend .... 3", "L2 | Wonder .... 6",
                     "Module Finale", "L3 | Know .... 9", "Achievement Descriptors  12"])
    pages = ["cover", toc,
             "Lesson 1\nEssential Question | Q?\nOVERVIEW\nAgenda\nLAUNCH\nLAN D",
             head(1) + "\nLAUNCH 2 minutes\nRead (10 minutes)", head(1) + "\nLAND 5 minutes",
             "Lesson 2\nEssential Question | Q?\nOVERVIEW", head(2) + "\nLAUNCH 2 minutes\nLAND 5 minutes",
             head(2) + "\nACHIEVEMENT DESCRIPTORS\nMM\nMake Meaning",
             "Lesson 3\nEssential Question | Q?", head(3, "Module Finale") + "\nLAUNCH 2 minutes",
             head(3, "Module Finale") + "\nLAND 5 minutes\nSee Learn book, p. 75.",
             head(3, "Module Finale") + "\nCP\nCompose\nCP.1.4"]
    b = split_book(pages, "Level 4 Module 2 Teach")
    Ls = b["lessons"]
    check("book: three lessons, printed pages from the contents",
          [(L["n"], L["printed"]) for L in Ls] == [(1, (3, 5)), (2, (6, 8)), (3, (9, 11))])
    check("book: plan runs Launch head (not the agenda) to the end of Land",
          [L["plan"] for L in Ls] == [(4, 5), (7, 7), (10, 11)])
    check("book: arcs and Finale titles",
          b["arcs"] == ["Arc A", "Module Finale"] and Ls[2]["title"] == "Module Finale, Know")
    try:
        split_book(pages[:5] + pages[8:], "Level 4 Module 2 Teach")
        check("book: a missing lesson holds", False)
    except Hold:
        check("book: a missing lesson holds", True)
    try:
        split_book(pages, "Some Other Book")
        check("book: a non-Teach-Book holds", False)
    except Hold:
        check("book: a non-Teach-Book holds", True)
    src = lesson_text(pages, b, Ls[2])
    check("book: descriptor pages are left out of the model's text", "CP.1.4" not in src)

    # checks
    L = Ls[0]
    src1 = "Students read “Gifts from the Gods”.\nRead (10 minutes)\nLAUNCH 2 minutes\nLearn book, p. 75"
    good = {"i_can": ["I can read a myth."], "why_it_matters": "x" * 50,
            "student_considerations": "y" * 160,
            "plan": "Launch (2 min): read. Read (10 min): students read “Gifts from the Gods”. " + "z" * 300,
            "success_criteria": ["Students will read.", "Students will write."],
            "next_steps": "w" * 50, "standards_note": ""}
    check("review: a good draft passes", check_review(good, L, [], src1) == [])
    bad = dict(good, plan=good["plan"].replace("(10 min)", "(15 min)") + " See “Hugo nearly gets hit by a train” on p. 40. **Bold**",
               i_can=["Read a myth."], standards_note="Covers CC.1.4.4.M.")
    f = check_review(bad, L, [("CC.1.4.4.N", "t")], src1)
    for want in ("15 minutes", "quoted words", "page 40", "markdown", "I can", "CC.1.4.4.M"):
        check("review: catches %s" % want, any(want in x for x in f))
    jt = checkable_text(dict(good, plan="Launch (2 min): read. Rigor comes from close reading.",
                             student_considerations="Use the frame. Anticipated difficulties include pacing."))
    check("fact check skips judgment sentences", "Rigor" not in jt and "Anticipated" not in jt
          and "Use the frame." in jt and "Launch (2 min)" in jt)
    check("review: a sentence-frame blank is not markdown",
          check_review(dict(good, student_considerations=good["student_considerations"]
                            + " A frame (Hugo ___, but now he ___.) helps."), L, [], src1) == [])
    check("review: a cited Learn book page is allowed",
          check_review(dict(good, next_steps=good["next_steps"] + " (Learn book, p. 75)"), L, [], src1) == [])
    q = {"tag": "vocab", "stem": "Endure means:", "correct": "to last", "wrong": ["to end", "to eat", "to run"]}
    m = {"focus": "f", "questions": [q, dict(q, stem="A?"), dict(q, stem="B?")]}
    check("questions: a good set passes", check_mcq(m, L, src1) == [])
    m2 = {"focus": "f", "questions": [q, dict(q, wrong=["to last", "x", "y"]),
                                       dict(q, correct="a very long answer that is clearly the right one here")]}
    f = check_mcq(m2, L, src1)
    check("questions: duplicate option and length giveaway caught",
          any("four different" in x for x in f) and any("by length" in x for x in f))

    # one lesson: local fails, the repair passes; with no cloud, a second failure holds
    orig = dict(local_draft=globals()["local_draft"], claim_check=globals()["claim_check"])
    calls = []
    try:
        globals()["claim_check"] = lambda text, src, cfg: []
        drafts = [dict(good, i_can=["Read."]), good]
        globals()["local_draft"] = lambda user, schema, cfg, timeout=0: (calls.append(user), drafts.pop(0))[1]
        got = build_lesson("lessons", b, L, [], src1, DEFAULTS, {"no_cloud": True})
        check("lesson: repair stage fixes a failed draft and names the failure",
              got["stage"] == "local-repair" and "I can" in calls[1])
        drafts[:] = [dict(good, i_can=["Read."]), dict(good, i_can=["Read."])]
        try:
            build_lesson("lessons", b, L, [], src1, DEFAULTS, {"no_cloud": True})
            check("lesson: holds when every stage fails", False)
        except Hold as e:
            check("lesson: holds when every stage fails", "Lesson 1" in str(e) and e.record["tries"])
    finally:
        globals().update(orig)

    # assembly: the key points at the intended answers, no letter more than twice
    drafts = {1: {"focus": "f", "questions": [dict(q, correct="to last %d" % i) for i in range(4)]}}
    body = assemble("checks", "Arc A", [L], drafts, {}, 2, "Alyssa", "CUMULUS", "seed", True)
    key = re.search(r"(?m)^Lesson 1: (.+)$", body).group(1)
    check("assembly: key verified and letters spread",
          max(collections.Counter(k.split("-")[1] for k in key.split(", ")).values()) <= 2)
    rb = assemble("lessons", "Arc A", [L], {1: good}, {}, 2, "Alyssa", "CUMULUS", "s", True)
    check("assembly: six labels in order, no codes line says none assigned",
          all(lab in rb for lab in LABELS) and "None assigned" in rb
          and rb.index(LABELS[0]) < rb.index(LABELS[5]) and FIT_CHECK in rb)

    # request detection and the host guard
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "g.json").write_text(json.dumps({"level": 4}))
        (td / "c.json").write_text(json.dumps({"host": "box1"}))
        att = [{"name": "AL_L4_M2_Module2TeachBook.pdf", "type": "application/pdf",
                "title": "Level 4 Module 2 Teach"}]
        sup = lambda subj, own, a=att, host="box1": supported(subj, own, a, td / "g.json", td / "c.json", host)
        check("detect: her lesson-plans email",
              (sup("Arts and Letters Module 2 Lesson Plans", "For each lesson...") or {}).get("kinds") == ["lessons"])
        check("detect: her checks email", (sup("Arts and Letters Grade 4 Module 2 Checks for Understanding",
                                                "3-4 multiple choice questions with an answer key") or {}).get("kinds") == ["checks"])
        check("detect: a corrected file on the reply thread uses the thread subject",
              (sup("Re: Arts and Letters Module 2 Lesson Plans", "Here is module 2.") or {}).get("module") == 2)
        check("detect: another box does not start jobs", sup("Lesson Plans", "x", host="other") is None)
        check("detect: an unknown deliverable stays for a session", sup("Module 2", "a rubric please") is None)
        check("detect: a Level 5 book stays for a session", sup("Lesson Plans", "x", [dict(att[0], title="Level 5 Module 1 Teach")]) is None)

    # start_job and run: identity, cap, the waiting thread, shadow and live delivery, holds
    import intake_lifecycle
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "config").mkdir()
        (root / "config/lesson_build.json").write_text(json.dumps({"host": "x", "clients": {"alyssa": {"auto_send": False}}}))
        (root / "config/intake_senders.json").write_text(json.dumps({"alyssa": {"emails": ["client@example.com"], "projects": ["pedagogy"]}}))
        waiting = (None, "client@example.com", "Arts and Letters Grade 4 Module 2 Checks for Understanding",
                   "3-4 multiple choice questions per lesson", "<m-checks>")
        intake_lifecycle.capture(root, waiting)
        intake_lifecycle.update(root, waiting, "waiting_on_client", "client", "asked for file", {"client": "alyssa"})
        launched = []
        found = {"name": "b.pdf", "level": 4, "module": 2, "kinds": ["lessons"]}
        now = datetime(2026, 11, 13, 23, 20, 0)
        rid = start_job("alyssa", "client@example.com", "<m-reply>", "Re: Arts and Letters Module 2 Lesson Plans",
                        found, root, popen=lambda *a, **k: launched.append(a[0]), now=now)
        meta = json.loads((root / REQUESTS_REL / (rid + ".json")).read_text())
        check("start: the waiting checks request joins the job",
              [t["kinds"] for t in meta["threads"]] == [["lessons"], ["checks"]])
        check("start: launched through job_runner with the run id",
              launched and launched[0][2:6] == ["job_runner.py", rid, "lesson_build.py", "run"])
        again = start_job("alyssa", "client@example.com", "<m-reply>", "x", found, root,
                          popen=lambda *a, **k: launched.append(1), now=now)
        check("start: the same message never starts a second job", again == rid and len(launched) == 1)
        start_job("alyssa", "client@example.com", "<m-2>", "x", found, root, popen=lambda *a, **k: None,
                  now=datetime(2026, 11, 13, 23, 21, 0))
        try:
            start_job("alyssa", "client@example.com", "<m-3>", "x", found, root, popen=lambda *a, **k: None,
                      now=datetime(2026, 11, 13, 23, 22, 0))
            check("start: daily cap", False)
        except DailyCap:
            check("start: daily cap", True)
        reply = (None, "client@example.com", "Re: Arts and Letters Module 2 Lesson Plans", "", "<m-reply>")
        intake_lifecycle.capture(root, reply)
        orig_build = globals()["build_book"]
        sent, told = [], []

        def fake_build(pdf, kinds, out_dir, cfg, ctx, name, box, other_thread, on_arc, **kw):
            for arc in ("Arc A", "Arc B"):
                ready = {k: (out_dir / ("%s-%s.txt" % (arc, k)), "body %s %s" % (arc, k)) for k in kinds}
                on_arc(arc, [{"n": 1}, {"n": 2}], ready)
                if ctx.get("hold_after") == arc:
                    raise Hold("Lesson 9 questions failed its checks: test")
            return {"stats": {"ok": 1}}
        try:
            globals()["build_book"] = fake_build
            rd = root / "logs/lesson_builds" / rid
            rd.mkdir(parents=True)
            (rd / "book.pdf").write_bytes(b"%PDF")
            creds = {"outlook_email": "cumulus@example.com"}
            fake_send = lambda *a, **k: sent.append(a[3])
            rc = run(rid, root, send=fake_send, creds=creds, notify=told.append, wait=0)
            def row(r):
                with intake_lifecycle.connect(root) as db:
                    return db.execute("SELECT state FROM requests WHERE id=?",
                                      (intake_lifecycle.identity(r[1], r[4]),)).fetchone()[0]
            check("run (shadow): builds, sends nothing, both requests ready for review",
                  rc == 0 and not sent and row(reply) == "ready_for_send" and row(waiting) == "ready_for_send"
                  and told)
            check("run (shadow): a send-ready copy carries the thread subject",
                  (rd / "Arc A-checks.md").read_text().startswith(
                      "Subject: Re: Arts and Letters Grade 4 Module 2 Checks"))
            meta["auto_send"] = True
            (root / REQUESTS_REL / (rid + ".json")).write_text(json.dumps(meta))
            verified = lambda pd, rows, rcp, cr: list(rows)
            rc = run(rid, root, send=fake_send, creds=creds, notify=told.append, verify=verified, wait=0)
            check("run (live): one email per arc and deliverable, each on its own thread",
                  rc == 0 and sent == [
                      "Re: Arts and Letters Module 2 Lesson Plans — Arc A (Lessons 1-2)",
                      "Re: Arts and Letters Grade 4 Module 2 Checks for Understanding — Arc A (Lessons 1-2)",
                      "Re: Arts and Letters Module 2 Lesson Plans — Arc B (Lessons 1-2)",
                      "Re: Arts and Letters Grade 4 Module 2 Checks for Understanding — Arc B (Lessons 1-2)"])
            check("run (live): Sent Mail proof closes both requests",
                  row(reply) == "sent_verified" and row(waiting) == "sent_verified")
            n = len(sent)
            run(rid, root, send=fake_send, creds=creds, notify=told.append, verify=verified, wait=0)
            check("run (live): a rerun never sends twice and stays verified",
                  len(sent) == n and row(reply) == "sent_verified")
            rid2 = "lb-alyssa-20261113-232100"
            (root / "logs/lesson_builds" / rid2).mkdir(parents=True)
            (root / "logs/lesson_builds" / rid2 / "book.pdf").write_bytes(b"%PDF")
            m2 = json.loads((root / REQUESTS_REL / (rid2 + ".json")).read_text())
            m2["auto_send"] = True
            (root / REQUESTS_REL / (rid2 + ".json")).write_text(json.dumps(m2))
            globals()["build_book"] = lambda *a, **k: fake_build(*a[:4], dict(a[4], hold_after="Arc A"), **k)
            told.clear()
            rc = run(rid2, root, send=fake_send, creds=creds, notify=told.append, verify=verified, wait=0)
            st = json.loads((root / "logs/lesson_builds" / rid2 / "state.json").read_text())
            check("run: a hold stops later arcs, keeps sent ones, and tells Buddy which lesson",
                  rc == 3 and st["status"] == "held" and len(st["sent"]) == 1
                  and told and "Lesson 9" in told[0])
        finally:
            globals()["build_book"] = orig_build
    return ok[0]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
