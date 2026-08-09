#!/usr/bin/env python3
"""
hoa_monitor.py — Delaware HOA lead monitor for Bill (S57, v1).
===============================================================================
Weekly scan for PUBLIC signals that a Delaware HOA / condo / community
association may be looking for property-management, snow, or grounds help — then
email Bill (Knight Property Services, cc Buddy) the vetted LINKS so HE decides
whether to reach out. We surface public info only; we NEVER contact anyone.

Cousin of bill_newdev_weekly. Reuses the shared building blocks:
  * X (Twitter) v2 recent search  — x_bearer_token (read-only, read-capped)
  * web / news / RFP-portal search — cirrus_daily.search_web + fetch_article_content
  * relevance judgement            — ensemble.best_answer(mode="council") (all 4 LLMs)
  * delivery                       — send_bid_email.py (Gmail SMTP, cc Buddy, signed NODE)

Flow (fail-safe):
  gather X + web candidates → drop already-seen (dedupe) → the 4-LLM COUNCIL
  classifies each as a genuine DE HOA lead or not → compose a tight, ranked
  digest → live: email Bill ONLY if there are real leads (cc Buddy) + record the
  sent links; dry-run: print only. Any gather/LLM failure => send NOTHING.

Guardrails: public sources only (no scraping gated platforms); we share links,
Bill does outreach; every send ccs Buddy; consequential contact is never
auto-committed.

Usage:
  python3 hoa_monitor.py --dry-run   # search + filter + compose, PRINT, no send
  python3 hoa_monitor.py             # live: email Bill only if real leads appear
  python3 hoa_monitor.py selftest    # offline unit tests
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE       = Path(__file__).resolve().parent      # ~/projects/cirrus-digest/hoa_leads
DIGEST_DIR = HERE.parent
OUT        = HERE / "out"
SEEN_PATH  = OUT / "hoa_leads_seen.json"
CREDS_PATH = DIGEST_DIR / "config/credentials.json"

sys.path.insert(0, str(DIGEST_DIR))               # cirrus_daily, ensemble, node_info
import node_info                                   # sign as the running node
NODE = node_info.node_name()
try:
    import ensemble                                # 4-LLM council for the lead judgement
except Exception:
    ensemble = None

TO    = "whutchins@knightpropertysvs.com"
CC    = "Buddy.Weiss@outlook.com"
TODAY = datetime.now().strftime("%Y-%m-%d")

MAX_X_RESULTS  = 25     # per X query (v2 recent allows up to 100) — read-cap for cost
MAX_CANDIDATES = 40     # cap what we hand the council in one run

# X recent-search: DE + community-association + lead-intent, no retweets, English.
X_QUERIES = [
    '("homeowners association" OR HOA OR "condo association" OR "community association") '
    '(Delaware OR DE OR "New Castle County" OR "Sussex County" OR "Kent County") '
    '("property management" OR "management company" OR RFP OR "request for proposal" OR '
    '"seeking" OR "looking for" OR "snow removal" OR "landscaping") -is:retweet lang:en',
]
# Web / news / RFP-portal signal (RFP portals are the highest-intent leads).
WEB_QUERIES = [
    "Delaware HOA request for proposal property management",
    "Delaware homeowners association seeking property management company",
    "Delaware condo association RFP management services",
    "DemandStar Delaware community association property management",
    "BidNet Delaware HOA property management RFP",
    "Delaware HOA snow removal bid proposal 2026",
]


# ── gather: X recent search ─────────────────────────────────────────────────────
def _x_bearer():
    try:
        return json.loads(CREDS_PATH.read_text()).get("x_bearer_token", "")
    except Exception:
        return ""


def gather_x():
    """Candidate dicts from X recent search. Best-effort ([] on any failure)."""
    tok = _x_bearer()
    if not tok:
        print("no x_bearer_token — skipping X")
        return []
    out = []
    for q in X_QUERIES:
        url = ("https://api.x.com/2/tweets/search/recent"
               f"?max_results={MAX_X_RESULTS}&query={urllib.parse.quote(q)}")
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok,
                                                   "User-Agent": "CirrusLeadMonitor/1.0"})
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
        except Exception as e:
            print("X search error:", e)
            continue
        for t in (d.get("data") or []):
            tid = t.get("id")
            out.append({"source": "X",
                        "url": f"https://x.com/i/web/status/{tid}",
                        "text": (t.get("text") or "").replace("\n", " ")[:400]})
    print(f"X: {len(out)} candidate(s)")
    return out


def _resolve_url(u, timeout=8):
    """Follow a search redirect (e.g. a Gemini grounding-redirect) to the REAL
    destination URL so we never email Bill an opaque redirect link. Best-effort:
    returns the original URL on any failure. Only touches known redirect hosts."""
    if not u or ("grounding-api-redirect" not in u and "vertexaisearch" not in u):
        return u
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(u, method=method,
                                         headers={"User-Agent": "CirrusLeadMonitor/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                final = r.geturl()
                if final and "vertexaisearch" not in final:
                    return final
        except Exception:
            continue
    return u


# ── gather: web / news / RFP portals ────────────────────────────────────────────
def gather_web():
    """Candidate dicts from web/news/RFP search via CIRRUS's own tooling. Best-effort."""
    try:
        from cirrus_daily import search_web, fetch_article_content, is_article_url
    except Exception as e:
        print("web tools import failed:", e)
        return []
    seen, out = set(), []
    for q in WEB_QUERIES:
        try:
            urls = search_web(q, max_results=5) or []
        except Exception as e:
            print(f"search_web error for '{q}':", e)
            urls = []
        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            snippet = ""
            try:
                if is_article_url(u):
                    content, _paywalled = fetch_article_content(u)
                    snippet = (content or "")[:600]
            except Exception:
                pass
            out.append({"source": "web", "url": u,
                        "text": snippet.replace("\n", " ") or "(no snippet retrieved)"})
            if len(out) >= MAX_CANDIDATES:
                break
        if len(out) >= MAX_CANDIDATES:
            break
    print(f"web: {len(out)} candidate(s)")
    return out


# ── dedupe ──────────────────────────────────────────────────────────────────────
def load_seen():
    try:
        return set(json.loads(SEEN_PATH.read_text()))
    except Exception:
        return set()


def save_seen(seen):
    OUT.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(sorted(seen), indent=1))


# ── council filter (all 4 LLMs judge, Claude synthesizes) ───────────────────────
FILTER_SYSTEM = (
    "You are one of several AI models on a lead-screening council for a Delaware "
    "property-management + snow business (Knight Property Services). You are shown "
    "public posts and listings. For EACH item decide whether it is a GENUINE, "
    "actionable signal that a Delaware HOA, condominium association, or residential "
    "community is looking for — or plausibly needs — property management, snow "
    "removal, or grounds/landscaping services. Be STRICT: it must be Delaware, about "
    "a community/association (not a single homeowner, not a vendor advertising its "
    "own services, not national news or generic commentary), and reflect a real "
    "need/opportunity. When unsure, mark lead=false."
)

FILTER_INSTRUCTIONS = (
    "\n\nReturn ONLY a JSON array (no prose, no code fence), one object per item:\n"
    '  {"idx": <int>, "lead": true|false, '
    '"type": "RFP|management-change|new-community|complaint|snow|other", '
    '"community": "<name or empty>", "why": "<one short sentence>"}\n'
    "Include an object for every item; set lead=false for anything that isn't a "
    "clear Delaware community-association opportunity."
)


def council_filter(cands, creds):
    """Council-judge candidates → list of vetted leads (each cand + type/community/why).
    Fail-safe: returns [] on any problem (=> nothing sent). v1 requires the council."""
    if not cands or ensemble is None:
        return []
    numbered = "\n".join(f"[{i}] source={c['source']} url={c['url']}\n    {c['text'][:400]}"
                         for i, c in enumerate(cands))
    user = ("Screen these items:\n\n" + numbered + FILTER_INSTRUCTIONS)
    try:
        meta, text = ensemble.best_answer(FILTER_SYSTEM, user, creds, max_tokens=2500,
                                          task="hoa-leads", mode="council")
        print(f"[council] members={meta.get('members')} judge={meta.get('judge')} "
              f"degraded={meta.get('degraded')}")
    except Exception as e:
        print("council filter failed:", e)
        return []
    arr = _parse_json_array(text)
    leads = []
    for o in arr:
        try:
            i = int(o.get("idx"))
        except Exception:
            continue
        if o.get("lead") and 0 <= i < len(cands):
            leads.append({**cands[i], "type": o.get("type", "other"),
                          "community": (o.get("community") or "").strip(),
                          "why": (o.get("why") or "").strip()})
    return leads


def _parse_json_array(text):
    """Extract a JSON array from a model reply (tolerant of prose/fences)."""
    if not text:
        return []
    t = re.sub(r"```(?:json)?", "", text)
    i, j = t.find("["), t.rfind("]")
    if i == -1 or j == -1 or j < i:
        return []
    try:
        data = json.loads(t[i:j + 1])
        return data if isinstance(data, list) else []
    except Exception:
        return []


# ── compose ─────────────────────────────────────────────────────────────────────
_TYPE_ORDER = {"RFP": 0, "management-change": 1, "new-community": 2,
               "complaint": 3, "snow": 4, "other": 5}


def compose(leads):
    leads = sorted(leads, key=lambda l: _TYPE_ORDER.get(l.get("type", "other"), 9))
    lines = [
        "Hi Bill,", "",
        "Weekly scan for Delaware HOAs / communities that may be looking for help "
        "(property management, snow, grounds). These are public posts and listings — "
        "I have NOT contacted anyone; sharing the links so you can decide whether to "
        "reach out.", "",
    ]
    for l in leads:
        head = l.get("community") or l.get("type", "lead")
        lines.append(f"• [{l.get('type', 'other')}] {head} — {l.get('why', '')[:170]}")
        lines.append(f"  {l['url']}  ({l['source']})")
    lines += ["",
              "These are unvetted leads surfaced automatically — worth a quick look "
              "before you spend time on any. Reply if you want me to tune what I watch for.",
              "", f"— {NODE} (Buddy's assistant)"]
    return "\n".join(lines)


# ── run ─────────────────────────────────────────────────────────────────────────
def _rec(dry, ok, note=""):
    if dry:
        return
    try:
        import job_status
        job_status.record("hoaleads", ok, note)
    except Exception:
        pass


def main():
    dry = "--dry-run" in sys.argv
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] hoa_monitor ({'dry-run' if dry else 'live'}) node={NODE}")
    try:
        creds = json.loads(CREDS_PATH.read_text())
    except Exception as e:
        print("no creds — aborting:", e)
        _rec(dry, False, "no creds")
        return

    cands = gather_x() + gather_web()
    seen = load_seen()
    fresh = [c for c in cands if c["url"] not in seen]
    print(f"candidates: {len(cands)} total, {len(fresh)} new (after dedupe)")
    if not fresh:
        print("nothing new this run — sending nothing.")
        _rec(dry, True, "no new candidates")
        return

    leads = council_filter(fresh[:MAX_CANDIDATES], creds)
    # Resolve search-redirect links to real destination URLs — ONLY for the few the
    # council kept (fast), then final-dedupe on the real URL so Bill never gets a
    # repeat or an opaque redirect link.
    final = []
    for l in leads:
        l["url"] = _resolve_url(l["url"])
        if l["url"] in seen:
            continue
        final.append(l)
    leads = final
    print(f"council kept {len(leads)} genuine lead(s).")

    if dry:
        print("=" * 70)
        print(compose(leads) if leads else "No genuine leads this run -> live would send NOTHING.")
        print("=" * 70)
        print("DRY RUN — nothing sent, dedupe not updated.")
        return

    if not leads:
        print("no genuine leads — nothing sent.")
        _rec(dry, True, "no genuine leads")
        return

    subject = f"Delaware HOA leads — {len(leads)} this week ({TODAY})"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(compose(leads))
        bodyfile = tf.name
    env = dict(os.environ, CC_EMAIL=CC)
    r = subprocess.run([sys.executable, str(DIGEST_DIR / "send_bid_email.py"),
                        TO, subject, bodyfile],
                       cwd=str(DIGEST_DIR), capture_output=True, text=True, env=env)
    print((r.stdout or "") + (r.stderr or ""))
    print("send exit:", r.returncode)
    if r.returncode == 0:
        for l in leads:                       # only mark seen on a successful send
            seen.add(l["url"])
        save_seen(seen)
    _rec(dry, r.returncode == 0, f"sent {len(leads)} leads" if r.returncode == 0 else "send failed")


# ── selftest (offline) ──────────────────────────────────────────────────────────
def selftest():
    fails = 0

    def ck(name, cond):
        nonlocal fails
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        fails += 0 if cond else 1

    arr = _parse_json_array('here: [{"idx":0,"lead":true,"type":"RFP","community":"Bay Pointe","why":"posted RFP"},'
                            '{"idx":1,"lead":false}] thanks')
    ck("parse json array from prose", len(arr) == 2 and arr[0]["type"] == "RFP")
    ck("parse: no array -> []", _parse_json_array("no json") == [])
    ck("resolve passes through a clean URL untouched",
       _resolve_url("https://demandstar.com/x") == "https://demandstar.com/x")

    cands = [{"source": "X", "url": "https://x.com/i/web/status/1", "text": "our HOA needs a manager"},
             {"source": "web", "url": "http://ex/2", "text": "vendor ad"}]
    # simulate council output mapping
    leads = []
    for o in [{"idx": 0, "lead": True, "type": "RFP", "community": "Maple Run", "why": "seeking mgmt"},
              {"idx": 1, "lead": False}]:
        i = o.get("idx")
        if o.get("lead"):
            leads.append({**cands[i], "type": o["type"], "community": o["community"], "why": o["why"]})
    ck("only genuine leads kept", len(leads) == 1 and leads[0]["community"] == "Maple Run")

    body = compose(leads)
    ck("compose: includes link + community + no-contact note",
       "Maple Run" in body and "https://x.com/i/web/status/1" in body and "NOT contacted" in body)
    ck("compose: signed as node", body.strip().endswith("(Buddy's assistant)"))

    # dedupe filter logic
    seen = {"http://ex/2"}
    fresh = [c for c in cands if c["url"] not in seen]
    ck("dedupe drops seen url", len(fresh) == 1 and fresh[0]["url"].endswith("/1"))

    print("PASS" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    if "selftest" in sys.argv:
        sys.exit(selftest())
    main()
