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

        # S137 (Buddy: "go ahead with step 7"): the TP=2 vLLM endpoint FIRST when
        # this box has one (cumulus1 since S127), then ollama exactly as before,
        # then the paid escalation -- the same chain the halftime jobs run
        # (LOCAL-MODEL-PLAN.md Phase 1 step 7). A vLLM miss is not an escalation:
        # it falls through to ollama, and the spend ledger tells them apart by
        # provider under task "intake:promise_detect" -- vllm / ollama rows at $0,
        # anthropic rows at cost -- which is how the fallback rate is measured
        # (S103: unprinted is unmeasured). On a box with no vllm_url this block
        # is skipped and the path below is byte-for-byte the old one.
        #
        # LOCAL_MAX_TOKENS: the budget was 300, sized in S78 for a non-thinking
        # qwen2.5:72b. Both local models now THINK (qwen3.8), and reasoning tokens
        # count against max_tokens, so at 300 the JSON was being cut off mid-value
        # -- measured 2026-09-08 on a realistic two-clause email: vLLM@300 and
        # ollama@300 both unparseable, both parse at 2000. Every such truncation
        # became a paid escalation or a MISSED promise. Escalation keeps 300: the
        # cloud model does not think and 300 is ample for the JSON.
        LOCAL_MAX_TOKENS = 2000
        _TASK = "intake:promise_detect"

        # 0. vLLM endpoint first, when configured.
        if creds.get("vllm_url"):
            try:
                import llm_providers
                raw = llm_providers.call("vllm", _PROMISE_SYSTEM, user, creds,
                                         max_tokens=LOCAL_MAX_TOKENS, retries=0,
                                         task=_TASK)
                parsed = _parse_promise_json(raw)
                if parsed is not None:
                    is_p, what = parsed
                    if not is_p:
                        return None
                    if what:
                        return {"what": what, "by": _tag("vllm"),
                                "escalated": False}
                    # yes-with-no-deliverable: fall through, same as ollama below
            except Exception:
                pass

        # 1. Local (ollama). Absent ollama_url this raises and we fall through --
        #    which is the correct behaviour on a box where it is not enabled.
        try:
            import llm_providers
            raw = llm_providers.call("ollama", _PROMISE_SYSTEM, user, creds,
                                     max_tokens=LOCAL_MAX_TOKENS, retries=0,
                                     task=_TASK)
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
                _PROMISE_SYSTEM, user, creds, max_tokens=300, mode="single",
                task=_TASK)
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

    def _fake_llm(local_raw, esc_raw, model, vllm_raw="__unset__"):
        """Fake llm_providers. `vllm_raw`: "__unset__" = vLLM behaves like the
        local model (legacy checks below never set vllm_url, so it is never
        reached); a string = what call("vllm") returns; None = call("vllm")
        raises. `seen` records (provider, max_tokens) for every call() and
        ("escalate", max_tokens) for escalate() -- the S137 checks read it."""
        m = types.ModuleType("llm_providers")
        st = {"model": None}
        seen = []

        def call(_p, _s, _u, _c, **_kw):
            seen.append((_p, _kw.get("max_tokens")))
            st["model"] = None
            if _p == "vllm" and vllm_raw != "__unset__":
                if vllm_raw is None:
                    raise RuntimeError("endpoint down")
                st["model"] = "qwen3.8-27b-fp8"
                return vllm_raw
            if local_raw is None:
                raise RuntimeError("no local model")
            st["model"] = model
            return local_raw

        def escalate(_s, _u, _c, **_kw):
            seen.append(("escalate", _kw.get("max_tokens")))
            st["model"] = None
            if esc_raw is None:
                raise RuntimeError("no cloud provider")
            st["model"] = "claude-sonnet-5"
            return ("anthropic", esc_raw)
        m.call, m.escalate = call, escalate
        m.last_model = lambda: st["model"]
        m.seen = seen
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

        # ── S137: vLLM first, ollama second, paid third -- and the budget fix.
        # Same three shapes the halftime jobs got in Phase B, asserted on which
        # providers were actually CALLED (the fake records them), not only on the
        # label that came back.
        _vc = {"vllm_url": "http://v", "vllm_model": "m"}
        _fl = _fake_llm(_yes, None, "qwen3.8:27b", vllm_raw=_yes)
        sys.modules["llm_providers"] = _fl
        _got = detect_promise(_text, _vc)
        check("S137: with vllm_url the ENDPOINT decides and is labelled as such",
              _got and _got["by"] == "vllm:qwen3.8-27b-fp8" and _got["escalated"] is False)
        check("S137: ...and ollama is NOT called when vLLM answered",
              [p for p, _ in _fl.seen] == ["vllm"])
        _fl = _fake_llm(_yes, None, "qwen3.8:27b", vllm_raw=None)
        sys.modules["llm_providers"] = _fl
        _got = detect_promise(_text, _vc)
        check("S137: a vLLM failure falls through to ollama -- NOT to the paid model",
              _got and _got["by"] == "ollama:qwen3.8:27b" and _got["escalated"] is False
              and [p for p, _ in _fl.seen] == ["vllm", "ollama"])
        _fl = _fake_llm(_yes, None, "qwen3.8:27b", vllm_raw="not json at all")
        sys.modules["llm_providers"] = _fl
        _got = detect_promise(_text, _vc)
        check("S137: an UNPARSEABLE vLLM reply also falls through to ollama",
              _got and _got["by"] == "ollama:qwen3.8:27b"
              and [p for p, _ in _fl.seen] == ["vllm", "ollama"])
        _fl = _fake_llm(_yes, None, "qwen3.8:27b", vllm_raw=_yes)
        sys.modules["llm_providers"] = _fl
        _got = detect_promise(_text, {})
        check("S137: with NO vllm_url the endpoint is never called (byte-for-byte old path)",
              _got and _got["by"] == "ollama:qwen3.8:27b"
              and [p for p, _ in _fl.seen] == ["ollama"])
        # The budget: local calls must get room for the model to THINK before the
        # JSON; the paid escalation keeps 300 (a non-thinking model, ample).
        _fl = _fake_llm("junk", _yes, "qwen3.8:27b", vllm_raw="junk")
        sys.modules["llm_providers"] = _fl
        detect_promise(_text, _vc)
        _mt = dict(_fl.seen)
        check("S137: local calls (vllm, ollama) get >= 2000 tokens -- 300 was truncating "
              "thinking models mid-JSON (measured 2026-09-08)",
              (_mt.get("vllm") or 0) >= 2000 and (_mt.get("ollama") or 0) >= 2000)
        check("S137: the paid escalation keeps its 300-token budget",
              _mt.get("escalate") == 300)

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
