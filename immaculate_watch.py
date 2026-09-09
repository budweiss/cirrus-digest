#!/usr/bin/env python3
"""Watch for NEW Immaculate Prediction contests during the season.

WHY THIS EXISTS
---------------
Buddy said follow-up questions would come during the season. My first reading
of the rules said otherwise -- they describe one locked 24-question entry -- and
he was right and I was wrong. The tell was the URL: the season rules live at
`/legal/app-immaculate-prediction-terms-SEASON`, and that suffix implies
siblings. `-weekly` also returns 200, and today it serves the **2026 NFL Draft
Giveaway** rules from April: eight days, a predictive question EACH DAY, points
per correct answer, a leaderboard, top three win autographed memorabilia.

So the Steelers run a series of short Immaculate contests and RECYCLE the legal
slugs. The April draft contest is the template for whatever runs in-season; the
`-weekly` page will be overwritten when the next one starts.

That makes the watch simple and precise: remember each slug's contest TITLE and
PERIOD, and shout when either changes or a new slug appears.

⚠ HONEST LIMIT: the season contest published its questions as a PDF in the
alternative-entry clause. **The draft contest did not** -- its mail-in clause
just says "answers to the predictive questions" with no document. So a new
contest may be detectable while its QUESTIONS stay app-only. If that happens,
this job can tell Buddy a contest has opened and by when, but he will have to
screenshot the questions for us to work them.
"""
import html
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "logs" / "immaculate-contests.json"
BASE = "https://www.steelers.com/legal/%s"

# Known live slugs plus plausible siblings. A 200 on an unknown slug is itself
# the finding, so guessing costs nothing but a HEAD request.
SLUGS = [
    "app-immaculate-prediction-terms-season",
    "app-immaculate-prediction-terms-weekly",
    "app-immaculate-prediction-terms-week",
    "app-immaculate-prediction-terms-game",
    "app-immaculate-prediction-terms-playoffs",
    "app-immaculate-prediction-terms-postseason",
    "app-immaculate-prediction-terms-inseason",
    "app-immaculate-prediction-terms-2026",
]


def fetch(slug):
    """(status, text) — text is None when the page is not a live rules page."""
    r = subprocess.run(["curl", "-sS", "-m", "30", "-w", "\n%{http_code}",
                        BASE % slug], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout:
        return None, None                 # unreachable: NOT the same as absent
    body, _, code = r.stdout.rpartition("\n")
    if code.strip() != "200":
        return code.strip(), None
    t = re.sub(r"<script.*?</script>", "", body, flags=re.S)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return "200", re.sub(r"\s+", " ", t)


def parse(text):
    if not text:
        return {}
    out = {}
    m = re.search(r"(PITTSBURGH STEELERS[^.]{0,120}OFFICIAL RULES)", text)
    if m:
        out["title"] = m.group(1).strip()[:120]
    m = re.search(r"will begin on ([A-Z][a-z]+ \d+, \d{4}[^ ]* at [\d:]+ [AP]M \w+)"
                  r" and end on ([A-Z][a-z]+ \d+, \d{4}[^ ]* at [\d:]+ [AP]M \w+)",
                  text)
    if m:
        out["begins"], out["ends"] = m.group(1), m.group(2)
    m = re.search(r"https://static\.clubs\.nfl\.com/\S+", text)
    if m:
        out["questions_url"] = m.group(0).rstrip(".,")
    return out


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(st):
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2) + "\n")
        tmp.replace(STATE)
    except Exception:
        pass


def diff(old, new):
    """Findings worth waking a human for."""
    news = []
    for slug, cur in new.items():
        was = old.get(slug)
        if cur.get("status") != "200":
            continue
        if not was or was.get("status") != "200":
            news.append(("NEW", slug, f"a contest page appeared: "
                                      f"{cur.get('title','(untitled)')}"))
            continue
        if cur.get("title") != was.get("title"):
            news.append(("CHANGED", slug,
                         f"contest REPLACED — was '{was.get('title')}', "
                         f"now '{cur.get('title')}'"))
        elif cur.get("begins") != was.get("begins") or cur.get("ends") != was.get("ends"):
            news.append(("CHANGED", slug,
                         f"same contest, new dates: {cur.get('begins')} -> "
                         f"{cur.get('ends')}"))
        if cur.get("questions_url") and not was.get("questions_url"):
            news.append(("QUESTIONS", slug,
                         f"a questions document is now published: "
                         f"{cur['questions_url']}"))
    return news


def scan():
    new = {}
    unreachable = []
    for slug in SLUGS:
        status, text = fetch(slug)
        if status is None:
            unreachable.append(slug)
            continue
        rec = {"status": status, "checked": datetime.now().strftime("%Y-%m-%d %H:%M")}
        rec.update(parse(text))
        new[slug] = rec
    return new, unreachable


def main():
    old = load_state()
    new, unreachable = scan()
    findings = diff(old, new)

    print(f"=== Immaculate contest watch — {datetime.now():%Y-%m-%d %H:%M} ===")
    live = [s for s, r in new.items() if r.get("status") == "200"]
    print(f"  {len(live)} live contest page(s) of {len(SLUGS)} probed")
    for s in live:
        r = new[s]
        print(f"    {s.split('terms-')[-1]:12s} {r.get('title','?')[:70]}")
        print(f"    {'':12s} {r.get('begins','?')} -> {r.get('ends','?')}")
        if r.get("questions_url"):
            print(f"    {'':12s} questions: {r['questions_url']}")
    if unreachable:
        print(f"  [UNCHECKED] could not reach: {', '.join(unreachable)} "
              f"— this is NOT 'nothing new'")
    if findings:
        print("\n  *** FINDINGS ***")
        for kind, slug, detail in findings:
            print(f"    [{kind}] {slug}: {detail}")
    else:
        print("  no change since the last check")

    # merge rather than replace, so an unreachable page does not erase what we knew
    merged = dict(old)
    merged.update(new)
    save_state(merged)
    return 2 if findings else (1 if unreachable else 0)


def selftest() -> int:
    fails = 0
    def ck(n, c):
        nonlocal fails
        print(f"  [{'OK ' if c else 'FAIL'}] {n}")
        fails += 0 if c else 1

    SEASON = ("PITTSBURGH STEELERS IMMACULATE PREDICTION 2026 SEASON CHALLENGE "
              "CONTEST OFFICIAL RULES ... will begin on September 7, 2026 at "
              "3:01 AM EST and end on September 13, 2026 at 1:00 PM EST ... go to "
              "https://static.clubs.nfl.com/image/upload/steelers/wkvr9j26n5nxzpbxhbha to access")
    p = parse(SEASON)
    ck("parses the real season rules: title", "SEASON CHALLENGE" in p.get("title", ""))
    ck("...period", p.get("begins", "").startswith("September 7")
       and p.get("ends", "").startswith("September 13"))
    ck("...and the published questions URL",
       p.get("questions_url", "").endswith("wkvr9j26n5nxzpbxhbha"))

    DRAFT = ("PITTSBURGH STEELERS IMMACULATE PREDICTION 2026 NFL DRAFT GIVEAWAY "
             "CONTEST OFFICIAL RULES ... will begin on April 17, 2026 at 3:01 AM "
             "EDST and end on April 25, 2026 at 12:00 PM EDST ...")
    d = parse(DRAFT)
    ck("parses the real draft rules, which publish NO questions URL",
       "DRAFT GIVEAWAY" in d.get("title", "") and "questions_url" not in d)

    old = {"a": dict(status="200", **d)}
    new = {"a": dict(status="200", **p)}
    f = diff(old, new)
    ck("a slug whose contest is REPLACED is flagged",
       any(k == "CHANGED" and "REPLACED" in det for k, _, det in f))
    ck("...and a newly published questions document is flagged separately",
       any(k == "QUESTIONS" for k, _, _ in f))
    ck("a brand-new slug appearing is flagged",
       any(k == "NEW" for k, _, _ in diff({}, {"z": dict(status="200", **p)})))
    ck("an unchanged page produces NO finding", diff(new, new) == [])
    ck("a 404 slug is never reported as a contest",
       diff({}, {"z": {"status": "404"}}) == [])
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(selftest() if "selftest" in sys.argv else main())
