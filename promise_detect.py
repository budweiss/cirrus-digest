#!/usr/bin/env python3
"""Promise detection — does an outbound email commit us to a future deliverable?

S78. Extracted from task_solver so it can run on EVERY client send, not just
the auto-answer path.

**Why it moved.** The first version hooked only `task_solver.solve_and_answer`,
the path where CUMULUS composes a reply itself. That left the path a human
stages by hand -- `client_mail.py` -- completely unwatched, which is where the
more deliberate promises get made. The 224-row workbook email of 2026-08-25 was
sent that way and carried a fresh conditional promise ("we would send the
existing New Castle backlog separately as its own workbook"). The ledger read
`0 open promises` while that sat in the client's inbox: a watchdog reporting
all-clear on a promise it structurally could not see.

Same shape as the county guard that covered one code path and not its twin, and
the same fix: put the rule where every caller passes through it. That is
`mailer.send()` -- the single chokepoint S77 consolidated the eight scattered
SMTP blocks into. A new send site is therefore watched by DEFAULT and has to
opt OUT, which is the right way round: forgetting to opt in is exactly how the
first gap happened.

It lives in its own module because `task_solver` imports `mailer`, so mailer
importing task_solver back would be a cycle. `llm_providers` is imported lazily
inside the call so the read-only supervisor probe can import the ledger side
without dragging in the LLM stack.
"""
# CIRRUS runs the SYSTEM python (3.9.6), not a venv, and 3.9 has no PEP 604
# unions -- `dict | None` in a signature is evaluated at def time and raises
# TypeError. task_solver.py carries this same import for exactly that reason.
# Without it this module imported fine on CUMULUS (3.11) and took intake.py
# down on CIRRUS the moment it was deployed. S78.
from __future__ import annotations

import json
import re

import client_promises

# ── Promise detection (S78) ──────────────────────────────────────────────────
# We record that mail arrived and that a reply left. Until now we recorded
# nothing about whether the thing we SAID we would do got done -- which is how
# Bill's 224-row workbook was offered, agreed to, and never built, with every
# health check green throughout.
#
# Local-first with escalation, which is the architecture Buddy asked for in the
# S77 handoff: qwen2.5:72b decides, and only an unusable verdict goes to a
# foundation model. Every decision records WHICH model made it, so
# client_promises.escalation_rate() produces the evidence the S73 _ollama
# docstring asked for before anything gets routed to the local model.

# Most outbound answers are a KB recap and promise nothing. This prefilter
# keeps the common case free -- no local call, no cloud call, no latency on a
# client's reply. It is deliberately loose: a false positive costs one cheap
# local call, a false negative costs a dropped promise.
_PROMISE_HINT_RX = re.compile(
    r"\b(we(?:'| wi)?ll|i(?:'| wi)?ll|we can (?:send|build|put|pull|get)"
    r"|we will|let us know and we|happy to (?:send|build|put)"
    r"|send (?:you|it|that|those|the)|clean (?:it|that) up"
    r"|cross[- ]reference|next week|by (?:monday|tuesday|wednesday|thursday|friday))\b",
    re.IGNORECASE)

_PROMISE_SYSTEM = (
    "You read one outbound email that a business has just sent to its client. "
    "Decide ONE thing: does it commit the business to producing or sending "
    "something in the FUTURE that has not been delivered in this same email?\n"
    "Answer with strict JSON and nothing else: "
    '{\"promise\": true|false, \"what\": \"<short description of the deliverable, '
    'or empty>\"}\n'
    "Rules: an email that ATTACHES or CONTAINS the thing is not a promise. "
    "Answering a question is not a promise. An offer conditional on the client "
    "saying yes IS a promise. Pleasantries are not promises."
)


def _parse_promise_json(raw: str):
    """Model output -> (is_promise, what) or None if unusable."""
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(d, dict) or "promise" not in d:
        return None
    return bool(d.get("promise")), str(d.get("what") or "").strip()


def _tag(provider: str) -> str:
    """`provider:model`, e.g. `ollama:qwen3.8:27b` — for the ledger's
    `detected_by`.

    S103 (Buddy), the same change halftime_catalogue got: every promise was
    stamped with the PROVIDER alone, so client_promises.escalation_rate()'s
    by_model split could not say WHICH local model decided. When ollama_model
    moved qwen2.5:72b -> qwen3.8:27b on 2026-09-05, promises either side of the
    switch were indistinguishable in the one report built to answer that.

    The model comes from llm_providers.last_model() -- the value actually put
    on the wire -- never from a second reading of creds, which would be a copy
    that can drift from what was really called. Falls back to the bare provider
    name if the model is unknown: less specific is fine, invented is not.
    """
    try:
        import llm_providers
        model = llm_providers.last_model()
    except Exception:
        model = None
    return f"{provider}:{model}" if model else provider


def detect_promise(text: str, creds: dict) -> dict | None:
    """Returns {"what":..., "by":..., "escalated":bool} or None.

    NEVER raises: called after a client's reply has already been sent, so a
    failure here must cost bookkeeping, not the client.
    """
    try:
        if not text or not _PROMISE_HINT_RX.search(text):
            return None
        user = f"The outbound email:\n\n{text[:6000]}"

        # 1. Local first. Absent ollama_url this raises and we fall through --
        #    which is the correct behaviour on a box where it is not enabled.
        try:
            import llm_providers
            raw = llm_providers.call("ollama", _PROMISE_SYSTEM, user, creds,
                                     max_tokens=300, retries=0)
            parsed = _parse_promise_json(raw)
            if parsed is not None:
                is_p, what = parsed
                if not is_p:
                    return None
                if what:
                    return {"what": what, "by": _tag("ollama"),
                            "escalated": False}
                # A "yes" with no deliverable named is not usable -- escalate
                # rather than open a promise nobody can act on.
        except Exception:
            pass

        # 2. Escalate. single mode = first keyed provider in the configured order.
        try:
            import llm_providers
            provider, raw = llm_providers.escalate(
                _PROMISE_SYSTEM, user, creds, max_tokens=300, mode="single")
            parsed = _parse_promise_json(raw)
            if parsed is None:
                return None
            is_p, what = parsed
            if is_p and what:
                return {"what": what, "by": _tag(provider), "escalated": True}
        except Exception:
            pass
        return None
    except Exception:
        return None




def record(text: str, creds: dict, *, client: str, subject: str,
           project: str = "general", message_id: str = "",
           log=None) -> str | None:
    """Detect a promise in one outbound body and open it in the ledger.

    NEVER raises. Called immediately AFTER a client's mail has gone out, so a
    bookkeeping failure must cost bookkeeping and nothing else -- the same
    discipline task_solver._record_question_attempt uses, for the same reason.

    Returns the promise id, or None when nothing was promised (the common case,
    settled by the regex prefilter without any model call at all).
    """
    try:
        found = detect_promise(text, creds)
        if not found:
            return None
        pid = client_promises.open_promise(
            client=client or "", project=project or "general",
            subject=subject or "", promise=found["what"],
            message_id=message_id or "", detected_by=found["by"],
            escalated=found["escalated"])
        if pid and log:
            log(f"  → promise recorded ({pid}, via {found['by']}"
                f"{', escalated' if found['escalated'] else ''}): "
                f"{found['what'][:90]}")
        return pid
    except Exception:
        return None


def selftest() -> int:
    bad = 0

    def check(label, ok):
        nonlocal bad
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            bad += 1

    check("an offer of future work trips the prefilter",
          bool(_PROMISE_HINT_RX.search(
              "Happy to clean it up and send you the whole 224 as a workbook.")))
    check("the promise in the workbook email trips it",
          bool(_PROMISE_HINT_RX.search(
              "If we switch it on we would send the existing New Castle backlog "
              "separately as its own workbook.")))
    check("a plain 'we will' trips it",
          bool(_PROMISE_HINT_RX.search("We'll pull those numbers together.")))
    check("a pure recap does NOT trip it",
          not _PROMISE_HINT_RX.search(
              "Back Creek. Status: cold. County: New Castle. "
              "Board Contact: Steven Foulk, President."))
    check("detect_promise returns None on a recap without calling a model",
          detect_promise("Status: cold. County: New Castle.", {}) is None)
    check("detect_promise survives creds with no providers at all",
          detect_promise("We'll send you the workbook next week.", {}) is None)
    check("record() never raises and returns None when nothing is promised",
          record("Status: cold.", {}, client="bill", subject="x") is None)
    check("record() survives a detection that cannot reach any model",
          record("We'll send it next week.", {}, client="bill", subject="x") is None)
    # ── S103 (Buddy): detected_by names the MODEL, not just the provider ──
    # Driven through the real detect_promise with llm_providers stubbed, so
    # this tests the label that actually reaches the ledger rather than a
    # restatement of the format. Each assertion is paired with its inverse.
    import sys
    import types
    _yes = '{"promise": true, "what": "send the workbook"}'
    _text = "We'll send you the whole 224 as a workbook next week."

    def _fake_llm(local_raw, esc_raw, model):
        m = types.ModuleType("llm_providers")
        st = {"model": None}

        def call(_p, _s, _u, _c, **_kw):
            st["model"] = None
            if local_raw is None:
                raise RuntimeError("no local model")
            st["model"] = model
            return local_raw

        def escalate(_s, _u, _c, **_kw):
            st["model"] = None
            if esc_raw is None:
                raise RuntimeError("no cloud provider")
            st["model"] = "claude-sonnet-5"
            return ("anthropic", esc_raw)
        m.call, m.escalate = call, escalate
        m.last_model = lambda: st["model"]
        return m

    _real = sys.modules.get("llm_providers")
    try:
        sys.modules["llm_providers"] = _fake_llm(_yes, None, "qwen3.8:27b")
        _got = detect_promise(_text, {})
        check("a locally-decided promise names the MODEL, not 'ollama'",
              _got and _got["by"] == "ollama:qwen3.8:27b"
              and _got["escalated"] is False)

        sys.modules["llm_providers"] = _fake_llm("junk", _yes, "x")
        _got = detect_promise(_text, {})
        check("an ESCALATED decision names the cloud model — the inverse of "
              "the local case, so neither can pass by never being reached",
              _got and _got["by"] == "anthropic:claude-sonnet-5"
              and _got["escalated"] is True)

        _fl = _fake_llm(_yes, None, "qwen3.8:27b")
        _fl.last_model = lambda: None
        sys.modules["llm_providers"] = _fl
        _got = detect_promise(_text, {})
        check("an unknown model degrades to the bare provider — never "
              "'ollama:None' written into a client ledger",
              _got and _got["by"] == "ollama")

        # record() is what writes the ledger, and record() has no path
        # argument -- so it is driven with open_promise STUBBED rather than
        # pointed at a file. T32: a test never touches the live ledger, and
        # this one would otherwise open a real promise against a real client.
        sys.modules["llm_providers"] = _fake_llm(_yes, None, "qwen3.8:27b")
        _opened = {}
        _real_open = client_promises.open_promise
        try:
            client_promises.open_promise = (
                lambda **kw: _opened.update(kw) or "p-test")
            _pid = record(_text, {}, client="bill", subject="s")
            check("record() passes the TAGGED label through to the ledger, "
                  "which is where escalation_rate() reads it from",
                  _pid == "p-test"
                  and _opened.get("detected_by") == "ollama:qwen3.8:27b")
            check("  ...and still records the escalated flag separately, so "
                  "the relabel cannot move the rate",
                  _opened.get("escalated") is False)
        finally:
            client_promises.open_promise = _real_open

        # And the report that consumes it splits on the tag, end to end.
        import pathlib
        import tempfile
        with tempfile.TemporaryDirectory() as _td:
            _lp = pathlib.Path(_td) / "promises.jsonl"
            _f = detect_promise(_text, {})
            client_promises.open_promise(
                client="bill", project="p", subject="s", promise=_f["what"],
                detected_by=_f["by"], escalated=_f["escalated"], path=_lp)
            _rate = client_promises.escalation_rate(path=_lp)
            check("escalation_rate()'s by_model split carries the model tag",
                  _rate["by_model"] == {"ollama:qwen3.8:27b": 1})
            check("  ...and the escalated COUNT is unchanged by the relabel",
                  _rate["decided"] == 1 and _rate["escalated"] == 0)
    finally:
        if _real is not None:
            sys.modules["llm_providers"] = _real
        else:
            sys.modules.pop("llm_providers", None)

    print("\nALL PASS" if not bad else f"\n{bad} FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    sys.exit(selftest())
