#!/usr/bin/env python3
"""research_task.py — hand it requirements, walk away, get a recommendation back.

S74, 2026-08-23.

    Buddy: "I have another project I want to start tomorrow, that will require a
    lot of research. I don't want to spend hours waiting for response but give
    some requirements and have you either on cirrus or Cumulus do the research
    and come back with the best solution."

WHY THIS IS A THIN FILE
-----------------------
Almost none of this is new. CIRRUS already has every part:

    cirrus_daily.search_web          Brave -> Gemini grounding -> Claude -> DDG,
                                     each hard-deadline bounded
    cirrus_daily.fetch_article_content
    ensemble.best_answer             the 4-provider council (anthropic/gemini/
                                     grok/openai), degrading to escalate()
    deep_research.py                 the same shape, but ENTITY-scoped

What was missing is a front door for an OPEN QUESTION rather than a known
entity: "here are my requirements, find me the best option." So this composes
the existing pieces and adds only the three things that were genuinely absent —
decomposition into sub-questions, a comparison across candidates, and a written
recommendation with its reasoning exposed.

IT RUNS DETACHED. That is the point. Submit and go; the answer lands in
out/research/ and a Telegram line says it is ready. Nobody waits at a prompt.

HONESTY REQUIREMENTS, because a research tool that flatters is worse than none:
  * every claim carries its source URL, or it is marked UNSOURCED
  * the council's DISAGREEMENTS are reported, not averaged away — where the
    providers split is exactly where Buddy's judgment is needed
  * "no good option found" is a valid, first-class answer
  * what was NOT checked is stated, so absence of a caveat is never mistaken
    for absence of risk

USAGE
    research_task.py --brief briefs/my-question.md
    research_task.py --question "..." --requirement "..." --requirement "..."
    research_task.py --brief b.md --dry-run     # plan only, no searches, no spend
"""
import argparse
import re
import json
import os
import sys
import time
from datetime import datetime

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
OUT_DIR = os.path.join(REPO, "out", "research")

MAX_SUBQ = 6          # decomposition width — more is rarely better, just slower
MAX_RESULTS = 4       # search results per sub-question
MAX_FETCH = 3         # pages actually fetched per sub-question


def _creds():
    return json.load(open(os.path.join(REPO, "config", "credentials.json")))


def _council(system, user, creds, task, max_tokens=6000):
    """The existing 4-provider council. Returns (meta, text)."""
    import ensemble
    return ensemble.best_answer(system, user, creds, task=task,
                                max_tokens=max_tokens, mode="council")


def decompose(question, requirements, creds):
    """Turn a brief into concrete, separately-searchable sub-questions."""
    reqs = "\n".join(f"- {r}" for r in requirements) or "(none stated)"
    sys_p = (
        "You break a research brief into at most %d SEPARATELY SEARCHABLE "
        "sub-questions. Each must be answerable from public sources and must "
        "matter to the decision. Do not pad to reach the limit — fewer, sharper "
        "questions beat more vague ones. Reply ONLY with JSON: "
        '{"subquestions": ["..."], "success_looks_like": "one sentence"}' % MAX_SUBQ
    )
    usr = f"BRIEF\n{question}\n\nREQUIREMENTS (hard constraints)\n{reqs}"
    _, text = _council(sys_p, usr, creds, task="research:decompose", max_tokens=1500)
    try:
        t = text.strip()
        if t.startswith("```"):
            t = t.split("```")[1]
            t = t[4:] if t.lower().startswith("json") else t
        d = json.loads(t.strip())
        subs = [s for s in d.get("subquestions", []) if s][:MAX_SUBQ]
        return subs, d.get("success_looks_like", "")
    except Exception:
        # Fail useful, not empty: the brief itself is always a valid question.
        return [question], ""


def gather(subq, creds):
    """search -> fetch. Returns [{url, text}] with whatever actually came back.

    S77: a sub-question that names a known primary source now OPENS IT DIRECTLY
    instead of searching around it. Two runs on 2026-08-25 reported every
    per-unit rate as UNSOURCED while the vendors publish those rates on public
    pages -- search returned blog posts *about* pricing and never the pricing.
    The registry lives in opportunity_scout.PRIMARY_SOURCES so there is one
    list, not two."""
    import cirrus_daily
    out = []
    try:
        import opportunity_scout
        low = (subq or "").lower()
        # Tokenise the KEY on any non-alphanumeric run. Splitting only on "-"
        # left "transcription/captioning" as one token containing a space, so
        # a question literally asking about transcription matched nothing.
        direct = [k for k in opportunity_scout.PRIMARY_SOURCES
                  if any(len(w) > 3 and w in low
                         for w in re.split(r"[^a-z0-9]+", k))]
        for key in direct[:2]:
            url = opportunity_scout.PRIMARY_SOURCES[key]
            body, _paywalled = cirrus_daily.fetch_article_content(url)
            if body:
                out.append({"url": url, "text": body[:6000], "paywalled": False})
                print(f"    primary source [{key}] opened directly: {url}")
    except Exception as e:
        print(f"    primary-source step skipped ({type(e).__name__}: {e})")

    try:
        urls = cirrus_daily.search_web(subq, max_results=MAX_RESULTS) or []
    except Exception as e:
        return out, f"search failed: {e}"
    for u in urls[:MAX_FETCH]:
        try:
            # fetch_article_content returns (content, IS_PAYWALLED) -- every
            # other call site in the repo names it `paywalled`. This one called
            # it `ok` and gated on it, so a clean fetch (text, False) was thrown
            # away and only PAYWALLED pages could ever become sources. Net
            # effect since S74: every run reported 0 sources and the council
            # synthesised from nothing, which reads exactly like "the web has
            # nothing on this" (2026-08-25, found on Bill's Back Creek lookup).
            body, paywalled = cirrus_daily.fetch_article_content(u)
            if body:
                out.append({"url": u, "text": body[:6000],
                            "paywalled": bool(paywalled)})
        except Exception:
            continue
    return out, None


def synthesise(question, requirements, findings, creds):
    """Compare the options and recommend one — with disagreements preserved."""
    ev = []
    for sq, docs in findings.items():
        ev.append(f"\n### SUB-QUESTION: {sq}")
        if not docs:
            ev.append("  (nothing retrieved — treat this angle as UNCHECKED)")
        for d in docs:
            tag = " (PAYWALLED — partial text)" if d.get("paywalled") else ""
            ev.append(f"  SOURCE {d['url']}{tag}\n  {d['text'][:2500]}")
    reqs = "\n".join(f"- {r}" for r in requirements) or "(none stated)"

    sys_p = (
        "You are advising someone who will ACT on this and cannot afford a "
        "flattering answer.\n"
        "Rules, all mandatory:\n"
        "1. Every factual claim carries its source URL inline. No URL, mark it "
        "   UNSOURCED.\n"
        "2. Compare the real candidate options against the stated requirements. "
        "   Say which requirement each option FAILS — an option with no downside "
        "   listed means you have not looked hard enough.\n"
        "3. Recommend ONE, and give the single strongest argument AGAINST your "
        "   own recommendation.\n"
        "4. 'No good option found' is a valid answer. Say it plainly if true.\n"
        "5. End with WHAT I DID NOT CHECK — angles with no sources retrieved, "
        "   claims you could not verify. Absence of a caveat must never be "
        "   mistaken for absence of risk.\n"
        "Markdown. No preamble."
    )
    usr = (f"BRIEF\n{question}\n\nREQUIREMENTS\n{reqs}\n\nEVIDENCE\n"
           + "\n".join(ev)[:90000])
    meta, text = _council(sys_p, usr, creds, task="research:synthesise",
                          max_tokens=8000)
    return meta, text


def notify(msg, creds):
    try:
        import urllib.parse, urllib.request
        tok, chat = creds.get("telegram_bot_token"), creds.get("telegram_user_id")
        if not tok or not chat:
            return
        data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
        # url carries the bot token, so it must not reach argv (S73/T21)
        urllib.request.urlopen(
            urllib.request.Request(
                f"https://api.telegram.org/bot{tok}/sendMessage", data=data),
            timeout=20)
    except Exception:
        pass


def parse_brief(path):
    """A brief is markdown: prose is the question, '- ' lines are requirements."""
    q, reqs = [], []
    for line in open(path):
        s = line.strip()
        if s.startswith("- ") or s.startswith("* "):
            reqs.append(s[2:].strip())
        elif s and not s.startswith("#"):
            q.append(s)
    return " ".join(q), reqs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief")
    ap.add_argument("--question")
    ap.add_argument("--requirement", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.brief:
        question, requirements = parse_brief(a.brief)
        requirements += a.requirement
    else:
        question, requirements = a.question, a.requirement
    if not question:
        print("need --brief FILE or --question TEXT")
        return 2

    creds = _creds()
    t0 = time.time()
    print(f"brief       : {question[:100]}")
    print(f"requirements: {len(requirements)}")

    subs, success = decompose(question, requirements, creds)
    print(f"\ndecomposed into {len(subs)} sub-question(s):")
    for s in subs:
        print(f"  - {s}")
    if success:
        print(f"success looks like: {success}")

    if a.dry_run:
        print("\n== DRY RUN — no searches run, no spend ==")
        return 0

    findings, sourced = {}, 0
    for i, sq in enumerate(subs, 1):
        docs, err = gather(sq, creds)
        findings[sq] = docs
        sourced += len(docs)
        print(f"  [{i}/{len(subs)}] {len(docs)} source(s)"
              + (f"  !! {err}" if err else ""))

    meta, report = synthesise(question, requirements, findings, creds)

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(OUT_DIR, f"research-{stamp}.md")
    with open(path, "w") as f:
        f.write(f"# Research: {question}\n\n")
        f.write(f"*{datetime.now():%Y-%m-%d %H:%M} · {len(subs)} sub-questions · "
                f"{sourced} sources · {time.time()-t0:.0f}s*\n\n")
        if requirements:
            f.write("**Requirements**\n" + "".join(f"- {r}\n" for r in requirements) + "\n")
        f.write("---\n\n" + report + "\n")
        f.write("\n---\n## Sub-questions and sources\n")
        for sq, docs in findings.items():
            f.write(f"\n**{sq}**\n")
            f.write("".join(f"- {d['url']}\n" for d in docs)
                    or "- (nothing retrieved — UNCHECKED)\n")

    print(f"\nreport: {path}   ({sourced} sources, {time.time()-t0:.0f}s)")

    # Detection, not just a number. Searches that RETURN results and then yield
    # zero sources is the signature of a broken fetch path, not a hard topic --
    # the shape that hid the paywalled/ok bug above for a month. Say so, in the
    # report and in the Telegram, so the next zero-source run is a question
    # rather than a shrug.
    warn = ""
    if subs and sourced == 0:
        warn = ("\n\n> **WARNING — 0 sources retrieved across every sub-question.** "
                "The findings below are UNGROUNDED: the council answered from its "
                "own priors, not from anything read. Searches returning results "
                "while nothing is fetched usually means the fetch path is broken, "
                "not that the topic is unfindable. Check `logs/research-task.log` "
                "for fetch errors before trusting or discarding this report.")
        with open(path, "a") as f:
            f.write(warn + "\n")
        print("  !! 0 sources across all sub-questions — report is UNGROUNDED")

    notify(f"🔎 Research done: {question[:80]}\n{sourced} sources"
           + (" — UNGROUNDED, fetch path suspect" if warn else "")
           + f"\n{os.path.basename(path)}", creds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
