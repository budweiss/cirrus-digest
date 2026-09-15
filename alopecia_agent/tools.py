"""Tool implementations for the Alopecia etiology-synthesis agent (S177).

Narrow, purpose-built, each one ledgered -- same discipline as
supervisor/tools.py: the agent literally cannot do anything outside this
list, structurally, not by prompt instruction. In particular there is NO
tool that can contact anyone but Buddy, and NO tool that phrases anything
as treatment advice -- those constraints don't need the model to remember
them, because the capability to violate them doesn't exist here.

Runs as buddy (same account as alopecia_collect.py/alopecia_brief.py),
unlike Skywarden's isolated cumulus-supervisor account -- this agent needs
the full llm_providers.py multi-provider machinery (local Ollama, vLLM,
Anthropic, Kimi), which a cross-account-isolated process could not reach.
Its safety comes from the narrow tool surface below, not from OS isolation
-- the same posture alopecia_collect.py/alopecia_brief.py already run
under today.
"""
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import alopecia_kb                      # noqa: E402
import llm_providers                     # noqa: E402
from alopecia_agent import hypothesis_store  # noqa: E402
from alopecia_agent import ledger as _ledger_mod  # noqa: E402
from alopecia_agent.ledger import ledger_append, TIER_AUTO, TIER_NAME  # noqa: E402

CREDS_PATH = PROJECT_DIR / "config" / "credentials.json"
COLLECT_DIR = PROJECT_DIR / "alopecia" / "daily"
DRAFT_PATH = PROJECT_DIR / "alopecia" / "cause_research_draft.md"
REQUEST_PATH = PROJECT_DIR / "logs" / "alopecia-agent" / "pending-request.json"
OFFSET_PATH = PROJECT_DIR / "logs" / "alopecia-agent" / "telegram-update-offset.txt"
REQUEST_EXPIRY_SEC = 2 * 3600

ETIOLOGY_RANK = 3  # alopecia_collect.PRIORITIES[2] == (3, "etiology / cause / trigger")


def _log(event, tool, detail="", result="", tier=TIER_AUTO):
    ledger_append({"event": event, "tool": tool, "tier_name": TIER_NAME[tier],
                  "detail": str(detail)[:200], "result": str(result)[:200]})


def _load_creds():
    try:
        return json.loads(CREDS_PATH.read_text())
    except Exception:
        return {}


# ── read_kb ──────────────────────────────────────────────────────────────────
def read_kb(question: str, top_k: int = 3) -> str:
    """Query the grounded Alopecia foundation KB (alopecia_kb.py). Read-only."""
    hits = alopecia_kb.query(question, top_k=top_k)
    if not hits:
        result = "no grounded hits above the similarity threshold"
    else:
        result = "\n\n".join(
            f"[{h['similarity']:.2f}] {h['source']} §{h['section']}\n{h['text']}"
            for h in hits)
    _log("read", "read_kb", question, f"{len(hits)} hit(s)")
    return result


# ── read_new_etiology_items ─────────────────────────────────────────────────
def _collector_files_since(since_date: str):
    """alopecia-YYYY-MM-DD.json files strictly after since_date (or all of
    them if since_date is None -- the agent's very first run)."""
    if not COLLECT_DIR.exists():
        return []
    files = sorted(COLLECT_DIR.glob("alopecia-*.json"))
    if since_date is None:
        return files
    return [f for f in files if f.stem.replace("alopecia-", "") > since_date]


def read_new_etiology_items() -> str:
    """New P3 ("etiology / cause / trigger") band items collected since this
    agent's own last processed date -- NOT the collector's own seen-state,
    which tracks what was ever collected, not what THIS agent has already
    reasoned about. Read-only."""
    state = hypothesis_store.load()
    files = _collector_files_since(state.get("last_processed_date"))
    items = []
    for f in files:
        try:
            day_items = json.loads(f.read_text())
        except Exception:
            continue
        items.extend(it for it in day_items if it.get("rank") == ETIOLOGY_RANK)
    if not items:
        result = "no new etiology-band items since the last run"
    else:
        lines = [f"- {it.get('title', '(untitled)')}  ({it.get('source', '?')}, "
                f"{it.get('date', 'undated')})  {it.get('url', '')}  key={it.get('key', '')}"
                for it in items]
        result = "\n".join(lines)
    _log("read", "read_new_etiology_items", f"{len(files)} day file(s) scanned",
        f"{len(items)} etiology-band item(s)")
    return result


# ── hypothesis state ─────────────────────────────────────────────────────────
def read_hypothesis_state() -> str:
    """Current ranked hypotheses, as the agent's own persistent memory. Read-only."""
    state = hypothesis_store.load()
    hyps = state.get("hypotheses", [])
    if not hyps:
        result = "no hypotheses recorded yet -- this may be the first real run"
    else:
        lines = []
        for h in sorted(hyps, key=lambda x: x.get("evidence_grade", "E")):
            lines.append(
                f"[{h['id']}] grade {h['evidence_grade']}: {h['statement']}\n"
                f"    supporting: {', '.join(h.get('supporting_items', [])) or '(none)'}\n"
                f"    contradicting: {', '.join(h.get('contradicting_items', [])) or '(none)'}\n"
                f"    first seen {h.get('first_seen')}, last updated {h.get('last_updated')}")
        result = "\n".join(lines)
    _log("read", "read_hypothesis_state", "", f"{len(hyps)} hypothesis(es)")
    return result


def write_hypothesis(hyp_id: str, statement: str, evidence_grade: str,
                     supporting: str = "", contradicting: str = "") -> str:
    """Create or refine ONE hypothesis. evidence_grade must be one of A-E
    (same vocabulary as alopecia_brief.py's item grading: A controlled trial
    ... E unclassified). supporting/contradicting are comma-separated item
    keys (from read_new_etiology_items's key=... field or the KB's citations).
    Never phrase statement as treatment advice -- this is a causation-research
    finding, not a recommendation."""
    sup = [s.strip() for s in supporting.split(",") if s.strip()]
    con = [s.strip() for s in contradicting.split(",") if s.strip()]
    try:
        state = hypothesis_store.load()
        hypothesis_store.upsert(state, hyp_id, statement, evidence_grade,
                                supporting=sup, contradicting=con)
        hypothesis_store.save(state)
        result = f"saved (grade {evidence_grade})"
    except ValueError as e:
        result = f"REJECTED: {e}"
    _log("action", "write_hypothesis", f"{hyp_id}: {statement[:100]}", result)
    return result


def mark_run_processed() -> str:
    """Advance the agent's own cursor to today, so the next wake's
    read_new_etiology_items only sees items collected after this run. Call
    this ONCE, at the end of a run that actually reviewed the new items --
    not if you skipped review entirely."""
    state = hypothesis_store.load()
    today = datetime.now().strftime("%Y-%m-%d")
    hypothesis_store.mark_processed(state, today)
    hypothesis_store.save(state)
    _log("action", "mark_run_processed", "", today)
    return f"cursor advanced to {today}"


# ── LLM calls ─────────────────────────────────────────────────────────────────
def call_local(task_class: str, prompt: str) -> str:
    """Cheap local model call (vLLM/ollama, cloud fallback if both are down)
    for ROUTINE sub-steps: clustering similar items, extracting a claim from
    an abstract, checking whether a new item duplicates existing evidence.
    Do NOT use this for the actual hypothesis judgment -- that is
    call_council's job, on purpose (see CLAUDE.md)."""
    creds = _load_creds()
    system = ("You are a research-literature assistant helping cluster and "
             "extract claims from alopecia areata research items. Be terse "
             "and factual.")
    try:
        result, tier = llm_providers.call_local_first(
            system, prompt, creds, max_tokens=1024, task="alopecia-agent")
    except llm_providers.ProviderError as e:
        result, tier = f"ERROR: {e}", "none"
    _log("action", "call_local", f"{task_class}: {prompt[:80]}", f"[{tier}] {result[:120]}")
    return result


def call_council(prompt: str) -> str:
    """The actual judgment step: ask the full cloud council (Anthropic at
    max effort, plus every other keyed provider including Kimi) to weigh
    new evidence against existing hypotheses. This is where real reasoning
    happens -- routine local calls feed it, they do not replace it."""
    creds = _load_creds()
    system = (
        "You are helping synthesize evidence about what triggers the T-cell "
        "attack in alopecia areata, for a research monitor. Weigh new "
        "evidence against existing hypotheses; grade confidence A-E (A "
        "controlled trial, B cohort/epidemiology, C case report, D "
        "mechanistic/review, E unclassified). This is research synthesis, "
        "NEVER treatment advice -- do not recommend any action to a patient.")
    try:
        pairs = llm_providers.escalate(system, prompt, creds, max_tokens=2048,
                                      mode="council", task="alopecia-agent")
        result = "\n\n".join(f"--- {p} ---\n{t}" for p, t in pairs)
        members = [p for p, _ in pairs]
    except llm_providers.ProviderError as e:
        result, members = f"ERROR: {e}", []
    _log("action", "call_council", prompt[:80], f"{len(members)} member(s): {members}")
    return result


# ── brief draft (staging only -- NOT yet spliced into the real weekly brief) ──
def append_to_brief_draft(section_text: str) -> str:
    """Append a dated entry to a STAGING file
    (alopecia/cause_research_draft.md), reviewed by Buddy before any of this
    is folded into the real weekly brief. This is NOT a send path -- it
    writes a local file only."""
    DRAFT_PATH.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    with open(DRAFT_PATH, "a") as f:
        f.write(f"\n---\n## {today}\n\n{section_text.strip()}\n")
    _log("action", "append_to_brief_draft", section_text[:100], "appended")
    return "appended to staging draft"


# ── Telegram (one-way + the two-way guidance exception) ────────────────────
def send_telegram_summary(message: str) -> str:
    """One-way notification to Buddy -- only for a hypothesis ranking that
    changed meaningfully, not routine per-run noise. Capped at 4000 chars,
    same convention as Skywarden's send_telegram (this is a short summary,
    not a long digest that needs smart chunking)."""
    creds = _load_creds()
    token = creds.get("telegram_bot_token", "")
    chat_id = creds.get("telegram_user_id", "")
    if not token or not chat_id:
        result = "FAILED: telegram creds missing"
        _log("notify", "send_telegram_summary", message[:80], result)
        return result
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": message[:4000]}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        result = "sent"
    except urllib.error.URLError as e:
        result = f"FAILED: {e}"
    _log("notify", "send_telegram_summary", message[:80], result)
    return result


def create_guidance_request(issue: str, question: str) -> str:
    """Ask Buddy for actual direction when genuinely stuck: tried the
    available diagnostics, still can't proceed, and no remaining tool
    addresses it. NOT for routine hypothesis updates. His reply is handed
    back at the start of the next run via consume_guidance()."""
    if len(issue) < 20 or len(question) < 10:
        result = "REFUSED: issue/question too short to be a real escalation"
        _log("action", "request_guidance", f"{issue[:80]} / {question[:80]}", result)
        return result
    REQUEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    REQUEST_PATH.write_text(json.dumps({
        "issue": issue, "question": question,
        "requested_at": datetime.now().isoformat(), "status": "pending",
    }))
    text = (f"Alopecia agent is stuck:\n\n{issue}\n\n{question}\n\n"
           f"Reply with what you'd like done (within 2 hours) -- the agent "
           f"reads your reply on its next daily run.")
    result = send_telegram_summary(text)
    _log("action", "request_guidance", f"{issue[:80]} / {question[:80]}", result)
    return result


def request_guidance(issue: str, question: str) -> str:
    return create_guidance_request(issue, question)


def _telegram_api_call(method, params, token, timeout=10):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def consume_guidance() -> str:
    """Called ONCE at the start of a run (not by the model -- by agent.py
    itself, before the reasoning pass starts). Checks for a Telegram reply
    since the last run and returns Buddy's text if the pending request was
    answered, else None. A single getUpdates call, not continuous polling --
    this agent wakes once a day, unlike Skywarden's 60s heartbeat."""
    if not REQUEST_PATH.exists():
        return None
    try:
        req = json.loads(REQUEST_PATH.read_text())
    except Exception:
        REQUEST_PATH.unlink(missing_ok=True)
        return None
    if req.get("status") != "pending":
        return None
    requested_at = datetime.fromisoformat(req["requested_at"])
    if datetime.now() - requested_at > timedelta(seconds=REQUEST_EXPIRY_SEC):
        req["status"] = "expired"
        REQUEST_PATH.write_text(json.dumps(req))
        return None

    creds = _load_creds()
    token = creds.get("telegram_bot_token", "")
    chat_id = str(creds.get("telegram_user_id", ""))
    if not token or not chat_id:
        return None

    offset = 0
    if OFFSET_PATH.exists():
        try:
            offset = int(OFFSET_PATH.read_text().strip())
        except ValueError:
            pass
    try:
        result = _telegram_api_call("getUpdates", {"offset": offset, "timeout": 0}, token)
    except (urllib.error.URLError, TimeoutError):
        return None

    reply_text = None
    max_update_id = offset - 1
    for update in result.get("result", []):
        max_update_id = max(max_update_id, update["update_id"])
        msg = update.get("message", {})
        if str(msg.get("from", {}).get("id", "")) != chat_id:
            continue
        text = (msg.get("text") or "").strip()
        if text:
            reply_text = text
    if max_update_id >= offset:
        OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
        OFFSET_PATH.write_text(str(max_update_id + 1))

    if reply_text is None:
        return None
    req["status"] = "consumed"
    REQUEST_PATH.write_text(json.dumps(req))
    return reply_text


# ── selftest ──────────────────────────────────────────────────────────────────
def selftest():
    """Offline: no network, no live Ollama/vLLM/Telegram, no live files (T32).
    Monkeypatches this module's path constants to a tempdir for the duration,
    same idiom llm_providers.py's own selftest uses for its globals."""
    import tempfile
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    g = globals()
    saved = {k: g[k] for k in
             ("CREDS_PATH", "COLLECT_DIR", "DRAFT_PATH", "REQUEST_PATH", "OFFSET_PATH")}
    saved_hs_path = hypothesis_store.STATE_PATH
    saved_ledger_dir = _ledger_mod.STATE_DIR
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        g["CREDS_PATH"] = d / "credentials.json"
        g["COLLECT_DIR"] = d / "daily"
        g["DRAFT_PATH"] = d / "cause_research_draft.md"
        g["REQUEST_PATH"] = d / "pending-request.json"
        g["OFFSET_PATH"] = d / "telegram-update-offset.txt"
        hypothesis_store.STATE_PATH = d / "hypothesis_state.json"
        _ledger_mod.STATE_DIR = d / "logs"
        (d / "credentials.json").write_text("{}")
        try:
            check("read_new_etiology_items: an empty collector dir reports "
                  "'no new items', not an error",
                  "no new" in read_new_etiology_items())

            (d / "daily").mkdir(parents=True, exist_ok=True)
            (d / "daily" / "alopecia-2026-09-01.json").write_text(json.dumps([
                {"key": "pmid:1", "title": "EBV infection precedes onset",
                "rank": 3, "label": "etiology / cause / trigger",
                "source": "pubmed", "date": "2026-09-01", "url": "https://x"},
                {"key": "pmid:2", "title": "Recruiting trial near Philadelphia",
                "rank": 6, "label": "trials",
                "source": "trials", "date": "2026-09-01", "url": "https://y"},
            ]))
            out = read_new_etiology_items()
            check("read_new_etiology_items: filters to rank==3 (etiology) only",
                  "pmid:1" in out and "pmid:2" not in out)

            (d / "daily" / "alopecia-2026-09-08.json").write_text(json.dumps([
                {"key": "pmid:3", "title": "HLA haplotype study",
                "rank": 3, "label": "etiology / cause / trigger",
                "source": "pubmed", "date": "2026-09-08", "url": "https://z"},
            ]))
            check("read_new_etiology_items: reads ALL day files when the "
                  "agent has never run before (last_processed_date=None)",
                  "pmid:1" in read_new_etiology_items()
                  and "pmid:3" in read_new_etiology_items())

            check("write_hypothesis: a valid grade saves",
                  "saved" in write_hypothesis(
                      "h1", "viral trigger via molecular mimicry", "C",
                      supporting="pmid:1"))
            check("write_hypothesis: an invalid grade is REJECTED, not "
                  "silently coerced", "REJECTED" in write_hypothesis(
                      "h2", "x", "Z"))
            check("read_hypothesis_state: reflects what was just written",
                  "h1" in read_hypothesis_state()
                  and "molecular mimicry" in read_hypothesis_state())

            res = mark_run_processed()
            check("mark_run_processed: advances the cursor",
                  hypothesis_store.load()["last_processed_date"] is not None
                  and "cursor advanced" in res)
            check("read_new_etiology_items: after mark_run_processed, "
                  "OLDER day files (2026-09-01, 2026-09-08) are excluded "
                  "since they now predate the cursor",
                  "no new" in read_new_etiology_items())

            check("append_to_brief_draft: writes to the STAGING file, "
                  "not any real send path",
                  "appended" in append_to_brief_draft("finding: X")
                  and DRAFT_PATH.exists()
                  and "finding: X" in DRAFT_PATH.read_text())

            check("send_telegram_summary: missing creds fails cleanly, "
                  "never raises", "FAILED" in send_telegram_summary("hi"))

            check("request_guidance: too-short issue/question is REFUSED "
                  "(guards against a routine update posing as an escalation)",
                  "REFUSED" in create_guidance_request("short", "q"))

            check("consume_guidance: no pending request -> None",
                  consume_guidance() is None)

            call_local_res = call_local("classify", "test prompt")
            check("call_local: no local/cloud creds configured -> an ERROR "
                  "string, never an uncaught exception",
                  call_local_res.startswith("ERROR"))
            call_council_res = call_council("test prompt")
            check("call_council: no provider keys configured -> an ERROR "
                  "string, never an uncaught exception",
                  call_council_res.startswith("ERROR"))
        finally:
            for k, v in saved.items():
                g[k] = v
            hypothesis_store.STATE_PATH = saved_hs_path
            _ledger_mod.STATE_DIR = saved_ledger_dir

    print("PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(0 if selftest() else 1)
