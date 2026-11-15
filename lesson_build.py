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
            if re.search(r"\bpp?\.\s*(?:\d+\s*[-–]\s*)?%d\b" % n, src):
                continue
            bad.append("page %d is outside the lesson and not a page the pages cite" % n)
    return bad


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
    if re.search(r"\*\*|__|`|^\s*#|^\s*[-*•]\s", text, re.M):
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
        if len(opts) == 4 and len(opts[0]) > 1.6 * max(len(o) for o in opts[1:]) + 10:
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
- "contradicted": the pages say something different (a wrong event, character, definition, timing, page or activity).
- "invented": a specific detail that appears nowhere in the pages, such as a story event, a character's action, a definition, an activity, a material, a quotation or a page number.

Do not list interpretation or judgment, which the teacher asked for: why the lesson matters, where its rigor comes from, likely difficulties for students, suggestions for supporting students, or how the lesson's parts connect. Do not check PA Core codes; they come from a separate alignment guide.
If every specific fact is supported, return an empty list.

TEACH BOOK PAGES:
{pages}

DRAFT:
{draft}"""
CLAIM_SCHEMA = {"type": "object", "properties": {"unsupported": {"type": "array", "items": {
    "type": "object", "properties": {"statement": {"type": "string"}, "reason": {"type": "string"},
                                     "kind": {"type": "string", "enum": ["contradicted", "invented"]}},
    "required": ["statement", "reason", "kind"]}}}, "required": ["unsupported"]}

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
    r = local_check(CLAIM_SYSTEM, CLAIM_TASK.format(pages=src, draft=draft_text), CLAIM_SCHEMA, cfg)
    return ["%s: %s (%s)" % (u["kind"], u["statement"][:160], u["reason"][:120])
            for u in r.get("unsupported", [])]


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
    return "\n".join("%d. (%s) %s\nCorrect answer: %s\nWrong answers: %s" % (
        i, q["tag"], q["stem"], q["correct"], "; ".join(q["wrong"]))
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
            f = claim_check(review_text_fields(d, with_note=False), src, cfg)
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
                        raise
                    cache.write_text(json.dumps(got, indent=1, ensure_ascii=False))
                drafts[L["n"]] = got["draft"]
                stats["lessons"]["%s-%d" % (kind, L["n"])] = {"stage": got["stage"], "tries": got["tries"]}
            body = assemble(kind, arc, lessons, drafts, codes, book["module"], name, box,
                            seed="%s-%s" % (out_dir.name, arc),
                            other_thread=(len(kinds) > 1) if other_thread is None else other_thread)
            path = out_dir / "mail" / ("%s-%s.txt" % (re.sub(r"\W", "", arc), kind))
            path.write_text(body)
            ready[kind] = (path, body)
        stats["arcs"].append(arc)
        if on_arc:
            on_arc(arc, lessons, ready)
    stats["seconds"] = round(time.time() - stats.pop("started"), 1)
    stats["cloud_calls"] = ctx.get("cloud_calls", 0)
    (out_dir / "stats.json").write_text(json.dumps(stats, indent=1))
    return {"book": book, "stats": stats}


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
    sub.add_parser("selftest")
    a = ap.parse_args(argv)
    if a.cmd == "shadow":
        ctx = {"no_cloud": a.no_cloud}
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
    return True


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
