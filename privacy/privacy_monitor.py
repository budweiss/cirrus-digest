#!/usr/bin/env python3
"""
privacy_monitor.py  (S50, 2026-08-01)  —  Privacy Exposure Monitor, Phase 1
===============================================================================
An ongoing, self-improving CIRRUS agent that checks where Buddy's personal info
and his projects may be exposed on the internet, triages the hits with an
LLM council, and emails Buddy an exposure report. P1 is READ-ONLY: it finds and
reports; it never submits a removal or contacts anyone.

Reuses the S49 scheduled-agent core:
  * web search / fetch  — cirrus_daily.search_web + fetch_article_content
  * multi-LLM reasoning — llm_providers.escalate(..., mode="council")
  * delivery            — send_digest.send_email (Gmail SMTP -> Buddy) + Telegram
  * run ledger          — job_status.record("privacymon", ok)

Guardrails (from docs/PRIVACY-EXPOSURE-MONITOR-SPEC.md):
  * OWN-INFO ONLY. Watchlist entries carry a per-entry scope:
      scope="own"  -> only Buddy's own exposure surface (share-link dorks,
                      own_footprint dorks, and — for emails — HIBP breach check).
      scope="full" -> ALSO people-search / data-broker dorks.
    Clients default to "own"; people-search profiling of a third party only
    happens if Buddy explicitly sets that client to "full" in the watchlist.
  * No autonomous external actions in P1 (no removals, no messages).
  * The watchlist + findings ledger hold PII and live on CIRRUS only
    (config/watchlist.json + logs/privacy/), never in the Cowork git repo.

Usage:
  python3 privacy/privacy_monitor.py --dry-run   # search + triage + PRINT report, no send/alert, still writes ledger
  python3 privacy/privacy_monitor.py             # live: email + Telegram the report; alert on new high-severity
  python3 privacy/privacy_monitor.py --no-llm    # skip the LLM council (raw hits only) — fast test
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE       = Path(__file__).resolve().parent          # ~/projects/cirrus-digest/privacy
DIGEST_DIR = HERE.parent                               # ~/projects/cirrus-digest
CONFIG_DIR = DIGEST_DIR / "config"
LOG_DIR    = DIGEST_DIR / "logs" / "privacy"
WATCHLIST  = CONFIG_DIR / "watchlist.json"             # CIRRUS-only, gitignored
QUERIES    = CONFIG_DIR / "privacy_queries.json"
CREDS_PATH = CONFIG_DIR / "credentials.json"
LEDGER     = LOG_DIR / "findings.json"

sys.path.insert(0, str(DIGEST_DIR))                    # cirrus_daily, llm_providers, etc.
TODAY = datetime.now().strftime("%Y-%m-%d")


# ── loading ─────────────────────────────────────────────────────────────────
def _load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def load_watchlist():
    wl = _load_json(WATCHLIST, None)
    if wl is None:
        return None
    return wl


def load_queries():
    q = _load_json(QUERIES, {})
    # Minimal built-in fallback so a missing catalog still does something useful.
    if not q:
        q = {"share_link": ["site:claude.ai/share {v}", "site:chatgpt.com/share {v}"],
             "own_footprint": ["site:github.com {v}", "site:pastebin.com {v}"],
             "general": ["{v}"], "people_search": ["site:spokeo.com {v}"]}
    return q


def load_creds():
    return _load_json(CREDS_PATH, {})


# ── target expansion ────────────────────────────────────────────────────────
def build_targets(wl):
    """Flatten the watchlist into a list of {value, kind, scope, source} targets."""
    targets = []

    def add(value, kind, scope, source):
        value = (value or "").strip()
        if value:
            targets.append({"value": value, "kind": kind,
                            "scope": (scope or "own").lower(), "source": source})

    for e in wl.get("identities", []):
        add(e.get("value"), e.get("type", "identity"), e.get("scope"), "identity")
    for p in wl.get("projects", []):
        for ident in p.get("identifiers", []):
            add(ident, "project", p.get("scope"), f"project:{p.get('name','?')}")
    for c in wl.get("clients", []):
        add(c.get("email"), "client-email", c.get("scope"), f"client:{c.get('name','?')}")
    return targets


def _quote(value, kind):
    """Quote multi-word names/emails for exact-match dorks."""
    if kind in ("email", "client-email") or " " in value:
        return f'"{value}"'
    return value


# ── category selection per scope ────────────────────────────────────────────
def categories_for(scope):
    base = ["share_link", "own_footprint", "general"]
    if scope == "full":
        base.append("people_search")
    return base


# ── web gathering ───────────────────────────────────────────────────────────
# S50 dry-run finding: DuckDuckGo (cirrus_daily.search_web) times out from CIRRUS
# and the Gemini fallback returns opaque grounding-redirect URLs, not real pages —
# useless for detecting an actual indexed share link. So we search via the Brave
# Search API when brave_api_key is present (real URLs, fast), and only fall back to
# search_web when it is not. Artifact URLs are filtered out either way.

_ARTIFACT_HOSTS = (
    "vertexaisearch.cloud.google.com",   # Gemini grounding redirects
    "duckduckgo.com", "www.google.com/search", "www.bing.com/search",
)


def _is_real_url(u):
    if not isinstance(u, str) or not u.startswith("http"):
        return False
    return not any(h in u for h in _ARTIFACT_HOSTS)


def _own_domain(value):
    """Return a bare domain if `value` looks like one (else None)."""
    v = (value or "").strip().lower()
    if "@" in v or "." not in v or " " in v:
        return None
    return v


def _is_own_site(url, domain):
    """True if url is on the project's OWN domain (its own site, not an exposure)."""
    if not domain:
        return False
    try:
        host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return False
    return host == domain or host.endswith("." + domain)


def _brave_search(query, api_key, count=6):
    """Brave Search API → list of real result URLs. Raises on transport error."""
    params = urllib.parse.urlencode({"q": query, "count": count})
    url = "https://api.search.brave.com/res/v1/web/search?" + params
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
        "User-Agent": "CIRRUS-PrivacyMonitor/1.0",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    return [item.get("url") for item in
            (data.get("web", {}) or {}).get("results", []) if item.get("url")]


def gather(wl, queries, creds=None, verbose=True):
    """Run the dork/search catalog against every target. Returns list of hit dicts."""
    creds = creds or {}
    brave_key = creds.get("brave_api_key")
    search_web = None
    if not brave_key:
        try:
            from cirrus_daily import search_web as _sw
            search_web = _sw
        except Exception as e:
            print("!! no brave_api_key AND search_web import failed:", e)
            return []
    print(f"   search backend: {'Brave API' if brave_key else 'DuckDuckGo/Gemini fallback'}")

    settings = wl.get("settings", {})
    max_results = int(settings.get("max_results_per_query", 6))
    targets = build_targets(wl)
    hits, seen = [], set()
    # Brave free tier is ~1 query/sec; DDG we throttle lighter.
    pause = 1.1 if brave_key else 0.4

    def do_search(q):
        if brave_key:
            return _brave_search(q, brave_key, count=max_results)
        return search_web(q, max_results=max_results) or []

    dropped_own = 0
    for t in targets:
        v, kind, scope = t["value"], t["kind"], t["scope"]
        qv = _quote(v, kind)
        own = _own_domain(v) if kind == "project" else None
        for cat in categories_for(scope):
            for tmpl in queries.get(cat, []):
                query = tmpl.replace("{v}", qv)
                try:
                    urls = do_search(query) or []
                except Exception as e:
                    if verbose:
                        print(f"   search error [{cat}] {query[:60]!r}: {e}")
                    urls = []
                for u in urls:
                    if not _is_real_url(u):
                        continue
                    if own and _is_own_site(u, own):
                        dropped_own += 1        # project's own site ≠ exposure
                        continue
                    key = (v, u)
                    if key in seen:
                        continue
                    seen.add(key)
                    hits.append({
                        "target": v, "kind": kind, "scope": scope,
                        "source": t["source"], "category": cat,
                        "query": query, "url": u,
                    })
                time.sleep(pause)
        if verbose:
            print(f"   scanned {kind}: {v}  (scope={scope}) — {len(hits)} hits so far")
    if verbose and dropped_own:
        print(f"   ({dropped_own} own-site results dropped as non-exposure)")
    return hits


# ── HIBP breach check (dormant until keyed) ─────────────────────────────────
def hibp_check(email, api_key):
    """Return list of breach names for an email, or [] (404/none), or None on error."""
    if not api_key:
        return None
    url = ("https://haveibeenpwned.com/api/v3/breachedaccount/"
           + urllib.parse.quote(email) + "?truncateResponse=true")
    req = urllib.request.Request(url, headers={
        "hibp-api-key": api_key,
        "user-agent": "CIRRUS-PrivacyMonitor/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        return [b.get("Name", "?") for b in data]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []          # no breach found — the good case
        print(f"   HIBP HTTP {e.code} for {email}")
        return None
    except Exception as e:
        print(f"   HIBP error for {email}: {e}")
        return None


def gather_breaches(wl, creds):
    """Check every email-type target against HIBP. Returns {email: [breaches]}."""
    api_key = creds.get("hibp_api_key") or wl.get("settings", {}).get("hibp_api_key")
    out = {}
    if not api_key:
        return out, False       # not keyed -> dormant
    emails = []
    for e in wl.get("identities", []):
        if e.get("type") == "email" and e.get("value"):
            emails.append(e["value"])
    for c in wl.get("clients", []):
        if c.get("email"):
            emails.append(c["email"])
    for em in emails:
        res = hibp_check(em, api_key)
        if res:                 # non-empty list = breached
            out[em] = res
        time.sleep(1.6)         # HIBP rate limit
    return out, True


# ── LLM council triage ──────────────────────────────────────────────────────
TRIAGE_SYSTEM = (
    "You are CIRRUS's privacy-exposure analyst. You are given raw web/search hits for "
    "Buddy's own identifiers and projects (and, where he opted in, client emails). Judge "
    "each hit as REAL exposure of Buddy's information vs. a FALSE POSITIVE (unrelated "
    "person/company, generic page, the query echoed back, a login wall, etc.). Be "
    "conservative: flag as real only when the hit plausibly exposes the specific value. "
    "Rate severity: high (a live indexed shared-chat/paste/leak or a fresh breach exposing "
    "PII), medium (a data-broker/people-search listing or a public profile), low (a benign "
    "mention). Never invent hits that were not provided."
)


def _parse_jsonl_items(text):
    """Parse JSONL (one JSON object per line) from a model reply, tolerating
    ```fences/prose and truncation. Returns a list of item dicts (possibly empty).

    JSONL is used deliberately: if the reply is cut off at the token limit, only
    the final line is lost instead of the whole object failing to parse (the S50
    failure mode when we asked for one big JSON object)."""
    items = []
    if not text:
        return items
    for line in text.splitlines():
        line = line.strip().strip(",")
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("target"):
            items.append(obj)
    return items


def _triage_prompt(batch, breaches):
    payload = {"hits": batch, "breaches": breaches}
    return (
        "Assess these exposure candidates for Buddy Weiss (Buddy.Weiss@outlook.com, "
        "buddy.weiss@icloud.com, cirrustask@gmail.com; projects: CIRRUS/cirrustask.com, "
        "CUMULUS, STRATUS). Many candidates are a DIFFERENT person named Weiss, a generic "
        "support/homepage, or a breach NEWS article that does not name Buddy — mark those "
        "verdict='false_positive'. Mark verdict='real' only when the page plausibly exposes "
        "one of Buddy's specific values.\n\n"
        "Return ONE JSON object per line (JSONL) — NO markdown, NO code fences, NO prose, "
        "no wrapping array. One line per candidate you assessed:\n"
        '{"target":"<value>","url":"<url>","category":"<category>",'
        '"verdict":"real|false_positive","severity":"high|medium|low","why":"<short>"}\n'
        "Also emit one line per breached email with category=\"breach\", url=\"\", "
        "verdict=\"real\", severity=\"high\".\n\n"
        "CANDIDATES:\n" + json.dumps(payload)[:45000]
    )


def triage(hits, breaches, creds, no_llm=False):
    """Return (assessed_items, council_note). Falls back to raw hits if no LLM.

    S50: candidates are triaged in BOUNDED BATCHES (was first-80-only) so every
    hit gets judged — council on the first batch (compare providers), failover on
    the rest (cheaper). Each provider returns JSONL (one item per line) so a
    truncated reply only loses its last line. Raw hits are the last resort."""
    if no_llm or (not hits and not breaches):
        return _raw_items(hits, breaches), "LLM triage skipped."
    try:
        import llm_providers as L
    except Exception as e:
        return _raw_items(hits, breaches), f"llm_providers import failed: {e}"

    BATCH, MAX_BATCHES = 70, 8
    batches = [hits[i:i + BATCH] for i in range(0, len(hits), BATCH)]
    truncated = len(batches) > MAX_BATCHES
    batches = batches[:MAX_BATCHES] or [[]]

    def _best(result):
        best_items, best_prov, notes = [], None, []
        for provider, text in result:
            if text.startswith("ERROR:"):
                notes.append(f"{provider}:ERR")
                continue
            got = _parse_jsonl_items(text)
            notes.append(f"{provider}:{len(got)}")
            if len(got) > len(best_items):
                best_items, best_prov = got, provider
        return best_items, best_prov, notes

    all_items, notes = [], []
    for bi, batch in enumerate(batches):
        br = breaches if bi == 0 else {}
        if not batch and not br:
            continue
        user = _triage_prompt(batch, br)
        mode = "council" if bi == 0 else "failover"   # council once, then cheap
        try:
            res = L.escalate(TRIAGE_SYSTEM, user, creds, max_tokens=6000, mode=mode)
        except Exception as e:
            notes.append(f"b{bi}:{e}")
            continue
        if mode == "council":
            got, prov, ns = _best(res)
            notes.append(f"b{bi}[council {prov}: {','.join(ns)}]")
        else:
            prov, text = res
            got = _parse_jsonl_items(text)
            notes.append(f"b{bi}[{prov}:{len(got)}]")
        all_items.extend(got)

    note = "Council: " + "; ".join(notes)
    if truncated:
        note += f" | NOTE: {len(hits)} candidates exceed {BATCH * MAX_BATCHES} cap; remainder untriaged"
    if not all_items:
        return _raw_items(hits, breaches), note + " — reporting raw hits."
    return all_items, note


def _raw_items(hits, breaches):
    items = [{"target": h["target"], "url": h["url"], "category": h["category"],
              "verdict": "unreviewed", "severity": "unknown",
              "why": f"{h['source']} / {h['query']}"} for h in hits]
    for em, names in (breaches or {}).items():
        items.append({"target": em, "url": "", "category": "breach",
                      "verdict": "real", "severity": "high",
                      "why": "HIBP breaches: " + ", ".join(names)})
    return items


# ── ledger (findings state: new / known / resolved) ─────────────────────────
def _fid(item):
    return f"{item.get('target','')}|{item.get('category','')}|{item.get('url','')}"


def load_ledger():
    return _load_json(LEDGER, {})


def update_ledger(items):
    """Merge assessed items into the ledger. Returns (new_items, high_new)."""
    ledger = load_ledger()
    now = datetime.now().isoformat(timespec="seconds")
    new_items, high_new = [], []
    for it in items:
        if it.get("verdict") == "false_positive":
            continue
        fid = _fid(it)
        if fid not in ledger:
            ledger[fid] = {"first_seen": now, "last_seen": now, "status": "new",
                           **{k: it.get(k) for k in ("target", "category", "url",
                                                     "severity", "why")}}
            new_items.append(ledger[fid])
            if (it.get("severity") == "high"):
                high_new.append(ledger[fid])
        else:
            ledger[fid]["last_seen"] = now
            if ledger[fid].get("status") == "new":
                ledger[fid]["status"] = "known"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        LEDGER.write_text(json.dumps(ledger, indent=2) + "\n")
    except Exception as e:
        print("!! ledger write failed:", e)
    return new_items, high_new


# ── report compose ──────────────────────────────────────────────────────────
def compose_report(items, breaches, council_note, hibp_keyed, new_items, high_new):
    real = [i for i in items if i.get("verdict") in ("real", "unreviewed")]
    by_sev = {"high": [], "medium": [], "low": [], "unknown": []}
    for i in real:
        by_sev.get(i.get("severity", "unknown"), by_sev["unknown"]).append(i)

    lines = [f"# Privacy Exposure Report — {TODAY}", ""]
    lines.append(f"Scanned exposure candidates and triaged them with the LLM council. "
                 f"**{len(real)} flagged**, **{len(new_items)} new since last run** "
                 f"({len(high_new)} high-severity new).")
    lines.append("")
    if not hibp_keyed:
        lines.append("> HIBP breach check is **dormant** — add `hibp_api_key` to "
                     "credentials.json on CIRRUS to enable email breach monitoring.")
        lines.append("")

    for sev in ("high", "medium", "low", "unknown"):
        group = by_sev[sev]
        if not group:
            continue
        lines.append(f"## {sev.title()} severity ({len(group)})")
        for i in group[:40]:
            tgt = i.get("target", "?")
            cat = i.get("category", "?")
            url = i.get("url", "")
            why = i.get("why", "")
            loc = f" — {url}" if url else ""
            lines.append(f"- **{tgt}** [{cat}]{loc}\n  {why}")
        lines.append("")

    if breaches:
        lines.append("## Breach exposure (HIBP)")
        for em, names in breaches.items():
            lines.append(f"- **{em}** — {len(names)} breach(es): {', '.join(names)}")
            lines.append("  → **ACTION:** change this account's password now and turn on "
                         "2-factor authentication; make sure it isn't reused on other sites.")
        lines.append("")

    lines.append("---")
    lines.append(f"_{council_note}_")
    lines.append("_P1 is read-only — no removals were submitted. Reply to queue takedown "
                 "drafts (P3) once you review these._")
    subject = f"Privacy Exposure Report {TODAY} — {len(real)} flagged, {len(high_new)} new high"
    return subject, "\n".join(lines)


# ── delivery ────────────────────────────────────────────────────────────────
def deliver(subject, body):
    results = []
    try:
        from send_digest import send_email
        send_email(subject, body)
        results.append("email: sent")
    except Exception as e:
        results.append(f"email: error {e}")
    try:
        sys.path.insert(0, str(DIGEST_DIR))
        from morning_brief import send_telegram      # reuse the tested sender
        results.append(send_telegram(f"🔒 {subject}"))
    except Exception as e:
        results.append(f"telegram: error {e}")
    return results


# ── main ────────────────────────────────────────────────────────────────────
def main():
    dry    = "--dry-run" in sys.argv or "--dry" in sys.argv
    no_llm = "--no-llm" in sys.argv
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] privacy_monitor "
          f"({'dry-run' if dry else 'live'}{' no-llm' if no_llm else ''})")

    precheck = "--precheck" in sys.argv

    wl = load_watchlist()
    if wl is None:
        print("!! No watchlist at", WATCHLIST)
        print("   Scaffold it with the runner:  {\"cmd\":\"privacy-scaffold\"}")
        print("   then fill sensitive values on CIRRUS via vi. Aborting (nothing sent).")
        _record(dry, False, "no watchlist")
        return

    queries = load_queries()
    creds   = load_creds()

    if precheck:
        # Fast config check — no web searches, no LLM. Confirms the run will be
        # meaningful before committing to a full sweep.
        targets = build_targets(wl)
        n_full = sum(1 for t in targets if t["scope"] == "full")
        est = sum(len(categories_for(t["scope"])) for t in targets)
        est_q = sum(sum(len(queries.get(c, [])) for c in categories_for(t["scope"]))
                    for t in targets)
        try:
            import llm_providers as L
            provs = L.available(creds)
        except Exception:
            provs = []
        print("PRECHECK:")
        print(f"  targets: {len(targets)}  (full-scope: {n_full})")
        print(f"  approx queries this sweep: {est_q}")
        print(f"  brave_api_key: {'YES' if creds.get('brave_api_key') else 'NO (would use dead DDG path)'}")
        print(f"  hibp_api_key:  {'YES' if creds.get('hibp_api_key') else 'NO (breach check dormant)'}")
        print(f"  LLM providers keyed: {provs or 'none'}")
        return

    print("→ gathering web/search hits…")
    hits = gather(wl, queries, creds=creds)
    print(f"   {len(hits)} raw hits")

    print("→ checking breaches (HIBP)…")
    breaches, hibp_keyed = gather_breaches(wl, creds)
    print(f"   HIBP {'keyed' if hibp_keyed else 'DORMANT (no key)'}; "
          f"{len(breaches)} breached email(s)")

    print("→ triaging with LLM council…" if not no_llm else "→ skipping LLM (raw)…")
    items, council_note = triage(hits, breaches, creds, no_llm=no_llm)
    print("   " + council_note)

    new_items, high_new = update_ledger(items)
    subject, body = compose_report(items, breaches, council_note,
                                   hibp_keyed, new_items, high_new)

    if dry:
        print("=" * 72)
        print("SUBJECT:", subject)
        print("-" * 72)
        print(body)
        print("=" * 72)
        print("DRY RUN — nothing sent. Ledger updated at", LEDGER)
        return

    for r in deliver(subject, body):
        print("  ", r)
    _record(dry, True, f"{len(items)} flagged, {len(high_new)} new high")
    print("done.")


def _record(dry, ok, note=""):
    if dry:
        return
    try:
        import job_status
        job_status.record("privacymon", ok, note)
    except Exception:
        pass


if __name__ == "__main__":
    main()
