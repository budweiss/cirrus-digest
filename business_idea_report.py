"""business_idea_report.py — the daily business-opportunity report (S66).

Replaces entity_kb_weekly_digest.py for the business_ideas project. That
digest is built for Bill's HOA CRM, where an append-only EVENT STREAM is
exactly right ("what changed on which property this week"). Pointed at this
project it produced an unreadable dump: rejected ideas tangled with live
ones, the same idea listed three times, internal field-change noise
("fit_score updated"), and 55 undifferentiated "updates" with no ranking.

A business report needs the opposite shape -- current STATE, ranked, with
the reasoning attached:
  * the live shortlist, best first, with enough detail to act on
  * what is NEW today, called out
  * what was rejected, grouped BY REASON rather than listed one by one --
    the recurring kill-reason is the most useful signal in the whole
    pipeline (e.g. "liability bar" killed ~16 straight ideas, which is what
    told us to stop proposing compliance-briefing products)
  * pipeline volume + spend, so the thing is auditable at a glance

Usage:
  python3 business_idea_report.py [--days N] [--dry-run]
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import entity_kb

PROJECT_DIR = Path.home() / "projects/cirrus-digest"
CREDS_PATH = PROJECT_DIR / "config/credentials.json"
SPEND_LEDGER = PROJECT_DIR / "out/llm-spend-ledger.jsonl"
KB_PROJECT = "business_ideas"
TO_ADDR = "Buddy.Weiss@outlook.com"

# Recurring kill-reasons, matched against the critique text. The point is to
# report "9 ideas died on the liability bar" rather than nine near-identical
# paragraphs -- the pattern is the insight, not each instance.
REJECT_THEMES = [
    ("Liability / accuracy bar", (
        "liabilit", "legal-grade", "legal exposure", "near-100", "near 100",
        "accuracy", "misclassif", "hallucinat", "unvalidated", "audit")),
    ("Incumbent or free alternative already serves it", (
        "incumbent", "already exist", "already provide", "already receive",
        "already sell", "already serve", "free alternative", "free,")),
    ("Platform rules / ToS / demonetization", (
        "tos", "terms of service", "demonetiz", "monetization polic",
        "scraping", "prohibit", "platform")),
    ("No reachable buyer or distribution path", (
        "distribution", "first 20", "first 100", "reachable", "no budget",
        "willingness to pay", "cancel")),
]


def _theme_for(flaw: str) -> str:
    f = (flaw or "").lower()
    for name, needles in REJECT_THEMES:
        if any(n in f for n in needles):
            return name
    return "Other"


def _spend_today(day: str) -> tuple:
    total, calls = 0.0, 0
    try:
        for line in SPEND_LEDGER.read_text().splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            if "business-idea" in (d.get("task") or "") and str(d.get("ts", "")).startswith(day):
                total += float(d.get("cost", 0) or 0)
                calls += 1
    except Exception:
        pass
    return round(total, 2), calls


def compose(days: int = 1, db_path: str = None) -> tuple:
    today = datetime.now().strftime("%Y-%m-%d")
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")

    candidates = entity_kb.list_entities(KB_PROJECT, lead_state="candidate", db_path=db_path)
    candidates.sort(key=lambda e: -((e.get("state") or {}).get("final_score") or 0))

    recent = entity_kb.get_events(KB_PROJECT, since=since, event_type="signal", db_path=db_path)
    new_slugs = {e["slug"] for e in recent if e.get("signal_kind") == "candidate"}
    rejected_today = [e for e in recent if e.get("signal_kind") == "rejected"]

    L = []
    L.append(f"# Business opportunities — {datetime.now():%A, %B %d}")
    L.append("")
    L.append(f"**{len(candidates)} live candidate(s).** Ideas are scored twice: how well they "
             f"fit the brief, then adversarially for whether they'd actually survive. The "
             f"score shown is the weaker of the two.")
    L.append("")
    L.append("_These are researched hypotheses, not validated businesses — no revenue has "
             "been confirmed with a real buyer. Treat them as candidates worth investigating, "
             "not opportunities proven to work._")
    L.append("")

    if candidates:
        L.append("## The shortlist")
        L.append("")
        for i, e in enumerate(candidates, 1):
            s = e.get("state") or {}
            is_new = e["slug"] in new_slugs
            L.append(f"### {i}. {e['name']} — {s.get('final_score','?')}/10"
                     + ("  ★ NEW TODAY" if is_new else ""))
            L.append(f"*fit {s.get('fit_score','?')} · survives critique "
                     f"{s.get('survival_score','?')} · via {s.get('category','')}*")
            L.append("")
            if s.get("what"):
                L.append(f"- **What it is:** {s['what']}")
            if s.get("who_pays"):
                L.append(f"- **Who pays:** {s['who_pays']}")
            if s.get("autonomous_loop"):
                L.append(f"- **How it runs itself:** {s['autonomous_loop']}")
            if s.get("needs_building"):
                L.append(f"- **Still to build:** {s['needs_building']}")
            if s.get("main_risk"):
                L.append(f"- **Biggest risk:** {s['main_risk']}")
            # What it would take -- the part that decides whether it is worth
            # this month, as opposed to whether it is a good idea at all.
            if any(s.get(k) for k in ("build_effort", "run_cost", "time_to_revenue")):
                L.append("")
                L.append(f"  | | |")
                L.append(f"  |---|---|")
                if s.get("build_effort"):
                    L.append(f"  | **Build effort** | {s['build_effort']} |")
                if s.get("run_cost"):
                    L.append(f"  | **Running cost** | {s['run_cost']} |")
                if s.get("time_to_revenue"):
                    L.append(f"  | **Time to first customer** | {s['time_to_revenue']} |")
            if s.get("first_step"):
                L.append("")
                L.append(f"- **▶ Cheapest first test:** {s['first_step']}")
            L.append("")
    else:
        L.append("## The shortlist")
        L.append("")
        L.append("Nothing has cleared the bar yet.")
        L.append("")

    if rejected_today:
        L.append(f"## Rejected in the last {days}d ({len(rejected_today)}) — grouped by why")
        L.append("")
        by_theme = {}
        for ev in rejected_today:
            summ = ev.get("summary") or ""
            flaw = ""
            for line in summ.splitlines():
                if "Killed by:" in line:
                    flaw = line.split("Killed by:", 1)[1].strip()
                    break
            by_theme.setdefault(_theme_for(flaw), []).append((ev.get("name", "?"), flaw))
        for theme, items in sorted(by_theme.items(), key=lambda kv: -len(kv[1])):
            L.append(f"**{theme}** — {len(items)}")
            for name, flaw in items[:6]:
                L.append(f"- {name}" + (f" — {flaw[:150]}" if flaw else ""))
            if len(items) > 6:
                L.append(f"- …and {len(items)-6} more")
            L.append("")
        L.append("_A reason that keeps recurring is the useful signal here — it tells us which "
                 "whole categories to stop proposing. Rejected ideas are remembered and fed "
                 "back, so the same dead end isn't proposed twice._")
        L.append("")

    cost, calls = _spend_today(today)
    counts = entity_kb.project_counts(KB_PROJECT, db_path=db_path)
    L.append("## Pipeline")
    L.append(f"- {counts.get('total_entities',0)} ideas tracked "
             f"({counts.get('by_lead_state',{}).get('candidate',0)} live, "
             f"{counts.get('by_lead_state',{}).get('rejected',0)} rejected and remembered)")
    L.append(f"- {calls} model call(s) today, ${cost} spent")
    L.append("")
    L.append("*Sources: your Medium/Substack subscriptions (by email), followed feeds, "
             "targeted web search, and direct generation by the model council.*")

    # Count only NEW ideas that are actually on the live shortlist. new_slugs
    # is every slug with a candidate-signal in the window, which also counts
    # ideas later flipped to rejected (an idea can be proposed, admitted, and
    # then killed by a second run's harsher critique) -- reporting those as
    # "new" alongside a shorter shortlist read as a contradiction.
    new_live = len({e["slug"] for e in candidates} & new_slugs)
    subject = (f"Business opportunities — {len(candidates)} live"
               + (f", {new_live} new" if new_live else ""))
    return subject, "\n".join(L)


def run(days: int = 1, dry_run: bool = False, db_path: str = None) -> dict:
    subject, body = compose(days=days, db_path=db_path)
    if dry_run:
        print("SUBJECT:", subject)
        print("-" * 70)
        print(body)
        return {"sent": False, "reason": "dry-run"}
    try:
        creds = json.loads(CREDS_PATH.read_text())
    except Exception as e:
        return {"sent": False, "reason": f"no creds: {e}"}
    from entity_kb_weekly_digest import _send_mail
    ok = _send_mail(creds.get("outlook_email", ""), creds.get("outlook_password", ""),
                    TO_ADDR, "", subject, body)
    return {"sent": ok, "reason": "" if ok else "send failed"}


def selftest() -> bool:
    checks = []
    checks.append(("liability flaws group under the accuracy theme",
                   _theme_for("QA teams won't trust unvalidated LLM-extracted data, legal exposure")
                   == "Liability / accuracy bar"))
    checks.append(("incumbent flaws group separately",
                   _theme_for("Free, comprehensive alternatives already exist from BIS")
                   == "Incumbent or free alternative already serves it"))
    checks.append(("platform flaws group separately",
                   _theme_for("YouTube's monetization policy demonetizes faceless content")
                   == "Platform rules / ToS / demonetization"))
    checks.append(("an unmatched flaw falls back to Other",
                   _theme_for("something entirely unanticipated") == "Other"))
    checks.append(("an empty flaw does not crash", _theme_for("") == "Other"))
    subject, body = compose(days=1)
    checks.append(("report states these are unvalidated hypotheses",
                   "not validated businesses" in body))
    checks.append(("report has no raw field-change noise",
                   "fit_score updated" not in body and "category updated" not in body))
    checks.append(("subject reports the live count", "live" in subject))
    all_ok = all(ok for _, ok in checks)
    for d, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {d}")
    return all_ok


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(0 if selftest() else 1)
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=1)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    try:
        out = run(days=a.days, dry_run=a.dry_run)
        print(out)
        if not a.dry_run:
            import job_status
            job_status.record("businessideareport", bool(out.get("sent")),
                              out.get("reason", "") or "sent")
    except Exception as exc:
        if not a.dry_run:
            try:
                import job_status
                job_status.record("businessideareport", False, str(exc)[:180])
            except Exception:
                pass
        raise
    sys.exit(0 if (out.get("sent") or a.dry_run) else 1)
