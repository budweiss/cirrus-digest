#!/usr/bin/env python3
"""Switchboard -- outbound-reply tier classifier + per-thread turn-count gate.

S242 (2026-09-20), first build slice of the S240 design in
docs/CUMULUS-SelfImprovement-and-CrossProject-Design.md (Cowork repo).
Read that doc's "POLICY UPDATE (S240)" section before changing this file --
it is the design this module is one piece of, not the whole thing.

WHAT THIS FILE IS
------------------
Two things, both pure logic, neither wired to a send path yet:

1. `classify_reply_risk()` -- the S45 outbound-reply tier classifier
   (distinct from dev_loop.classify_risk(), which tiers INTERNAL code
   changes, not client email content). S45 specified this in 2026-07-23
   and it sat as spec-only for two months; this is the actual build.
2. Per-thread turn tracking, using the SAME thread_key() the promise
   ledger (client_promises.py) already uses for "whose words are these" --
   a fresh topic from a client starts a fresh count of 2, regardless of
   how long some other thread with them has run.

WHAT THIS FILE DELIBERATELY IS NOT
------------------------------------
It does not send anything, draft anything, or touch client_mail.py.
`decide()` is a SHADOW-MODE function: it computes what SHOULD happen and
logs it, and that is all. Per Buddy's explicit confirmation (S240), a
shadow period -- proving the classifier and turn-gate never mis-fire on
real Bill/Aggie/Alyssa/Justin threads -- is required before any of this is
allowed to actually send. Wiring an actual send path is later, separate
work, gated on that shadow period, not on this file existing.

TIER MEANINGS (matches the design doc's own vocabulary, not dev_loop's
integer TIER_NEVER/TIER_AUTO/etc, which is a different axis for different
content -- see the design doc for why these are deliberately separate):
  TIER_ZERO  -- reversible, informational, no promise, no numbers the
                recipient could act on financially/legally. Auto-sendable,
                but ONLY on turns 1-2 of a thread (see decide()).
  TIER_ONE   -- queue for a one-tap. Default / fail-open outcome: anything
                that doesn't cleanly clear every TIER_ZERO condition lands
                here, never silently downgraded.
  TIER_TWO   -- stage as a design proposal for a scheduled session with
                Buddy. A thread going in circles (turn 3+, same topic)
                lands here regardless of content tier.
  TIER_NEVER -- money movement, credentials/access, legal signature,
                identity/financial numbers. Hard-blocked regardless of
                sender or turn count; never auto-sendable at any turn.

Design notes worth keeping (same discipline as client_promises.py):

* **Append-only.** The shadow ledger is a log of decisions, not a mutable
  counter -- the turn number for a thread is COUNTED from the log, never
  stored and incremented separately, so there is nothing to get out of
  sync with the actual history.
* **Fail-open, always.** classify_reply_risk() defaults to TIER_ONE.
  TIER_ZERO must be positively earned by clearing every check; nothing
  reaches it by a regex simply failing to match. This mirrors
  dev_loop.classify_risk()'s own baseline-is-the-safe-tier shape.
* **Regex catches the obvious cases fast and offline; the optional model
  check catches the subtler ones.** classify_reply_risk() takes an
  optional `creds` -- when given, it calls promise_detect.detect_promise()
  as a second signal (a text that quietly commits to a future deliverable
  without using any of the keyword list should still land at TIER_ONE).
  When creds is omitted (as in selftest(), which must stay offline and
  fast), only the regex layer runs -- still fail-open, just with one
  fewer signal.
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import client_promises  # noqa: E402 -- for thread_key(), the shared thread identity

LEDGER = HERE / "logs" / "switchboard-shadow.jsonl"

TIER_ZERO = "tier0"
TIER_ONE = "tier1"
TIER_TWO = "tier2"
TIER_NEVER = "never"

# Turns 1-2 of a thread may auto-send at TIER_ZERO; turn 3 onward never does,
# regardless of content tier (Buddy, S240: "if we are on the 3rd or 4th
# question, prompt me").
AUTO_SEND_TURN_LIMIT = 2

# Hard-blocked regardless of sender, turn count, or anything else. Matches
# CLAUDE.md's own NEVER-tier categories: money, legal, access, irreversible.
_NEVER_PATTERNS = [
    ("money movement", r"\b(wire (the |your )?(money|funds)|"
                        r"send (the )?payment|transfer \$|"
                        r"routing number|wire transfer instructions)\b"),
    ("credential/access", r"\b(password|api[- ]?key|ssh key|"
                           r"login credentials|access code|2fa code|"
                           r"one[- ]time (code|passcode))\b"),
    ("legal signature", r"\b(sign(ed|ing)? the (contract|agreement|lease|deed)|"
                         r"legally binding|notariz\w*)\b"),
    ("identity/financial numbers", r"\b(social security|\bssn\b|"
                                    r"bank account number|"
                                    r"credit card number)\b"),
]

# Forces at least TIER_ONE. Deliberately narrower than a generic "I will"
# scan -- that would catch nearly every helpful reply and make TIER_ZERO
# unreachable for the routine informational replies it exists for. These
# are words specific to a consequential commitment, not ordinary helpfulness.
_CONSEQUENTIAL_KEYWORDS = re.compile(
    r"\b(price|bid|quote|rate|offer|contract|deposit|retainer|invoice|"
    r"guarantee|promise(d)?|commit(ted|ting)?|deadline|due date|"
    r"refund|discount)\b", re.IGNORECASE)


def classify_reply_risk(reply_text: str, *, creds: dict = None) -> tuple[str, str]:
    """(tier, reason). NEVER-patterns win first, then consequential keywords,
    then an optional model-based promise check, else TIER_ZERO -- the reply
    has then positively cleared every check, not merely failed to trip one.

    Input is the REPLY text only (what would be sent), not the inbound
    request -- text/informational scope, per the S45 spec. Attachments,
    config/recipient/access changes, and sender-allowlist membership are
    NOT this function's job: those are enforced by the caller (whatever
    eventually wires this to a send path), not by classifying reply text.
    """
    text = reply_text or ""

    for label, rx in _NEVER_PATTERNS:
        if re.search(rx, text, re.IGNORECASE):
            return TIER_NEVER, f"matches NEVER-tier category: {label}"

    m = _CONSEQUENTIAL_KEYWORDS.search(text)
    if m:
        return TIER_ONE, f"consequential keyword: {m.group(0)!r}"

    if creds:
        try:
            import promise_detect
            found = promise_detect.detect_promise(text, creds)
        except Exception:
            found = None  # never let a model-layer failure block fail-open
        if found:
            return TIER_ONE, "promise_detect found a commitment in the reply"

    return TIER_ZERO, "no NEVER pattern, no consequential keyword" + (
        ", no promise_detect hit" if creds else " (regex-only check, no creds given)")


def _append(event: dict, path: Path = None) -> bool:
    """Never raises -- this is instrumentation on a client-facing path, same
    discipline as client_promises._append. Returns False if the write failed."""
    p = path or LEDGER
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(event) + "\n")
        return True
    except Exception:
        return False


def _events(path: Path = None) -> list:
    p = path or LEDGER
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text().splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def turn_count(client: str, subject: str, path: Path = None) -> int:
    """How many decisions are already logged for this client+thread. The
    NEXT decision for this thread is turn (this number + 1) -- the log
    itself is the counter, so there is nothing to keep in sync separately."""
    key = client_promises.thread_key(subject)
    return sum(1 for e in _events(path)
               if e.get("client") == client and e.get("thread") == key)


def decide(client: str, subject: str, reply_text: str, *,
           creds: dict = None, path: Path = None) -> dict:
    """SHADOW MODE ONLY. Computes what should happen to this reply and logs
    the decision -- never sends, never drafts, never touches client_mail.py.

    Returns {client, thread, turn, tier, reason, action, would_send} where
    action is "auto_send" (only tier0 AND turn <= AUTO_SEND_TURN_LIMIT) or
    "queue" (everything else -- tier1/tier2/never at any turn, or tier0
    once the turn limit is passed). would_send mirrors action as a bool,
    for a caller that just wants a yes/no without the label.
    """
    key = client_promises.thread_key(subject)
    turn = turn_count(client, subject, path) + 1
    tier, reason = classify_reply_risk(reply_text, creds=creds)

    if turn > AUTO_SEND_TURN_LIMIT + 1:
        # A thread still going after 4+ turns is "going in circles," not
        # just long -- escalate to a scheduled session regardless of the
        # content tier. NEVER stays NEVER; it is already the most severe
        # tier, escalating it to tier2 would be a downgrade in disguise.
        action = "queue"
        if tier != TIER_NEVER:
            reason = f"{reason}; thread at turn {turn}, escalated to tier2 (going in circles)"
            tier = TIER_TWO
    elif tier == TIER_ZERO and turn <= AUTO_SEND_TURN_LIMIT:
        action = "auto_send"
    else:
        action = "queue"
        if tier == TIER_ZERO:
            reason = f"{reason}; but turn {turn} > {AUTO_SEND_TURN_LIMIT} (S240 turn-gate)"

    record = {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "client": client,
        "subject": subject,
        "thread": key,
        "turn": turn,
        "tier": tier,
        "reason": reason,
        "action": action,
        "would_send": action == "auto_send",
        "shadow": True,
    }
    _append(record, path)
    return record


def selftest() -> int:
    import tempfile
    import os

    fails = 0

    def ck(name, cond):
        nonlocal fails
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        fails += 0 if cond else 1

    # T32: never the live shadow ledger.
    fd, tmp = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    os.unlink(tmp)
    tmp = Path(tmp)

    try:
        # -- classify_reply_risk: regex-only (no creds) --
        t, r = classify_reply_risk("Here's this week's literacy digest, no action needed.")
        ck("a plain informational reply classifies tier0", t == TIER_ZERO)

        t, r = classify_reply_risk("We can offer a discount if you sign by Friday.")
        ck("a reply with consequential keywords is at least tier1", t == TIER_ONE)

        t, r = classify_reply_risk("Please wire the funds to the routing number below.")
        ck("a money-movement reply is NEVER, not just tier1", t == TIER_NEVER)

        t, r = classify_reply_risk("Here is the password reset link and your access code.")
        ck("a credential/access reply is NEVER", t == TIER_NEVER)

        t, r = classify_reply_risk("")
        ck("empty text fails open to tier0, not an exception", t == TIER_ZERO)

        # NEVER must win even if a consequential keyword is also present.
        t, r = classify_reply_risk("Our rate is fine, but please wire the funds today.")
        ck("NEVER-pattern outranks a consequential keyword in the same text", t == TIER_NEVER)

        # -- classify_reply_risk: creds path, promise_detect failure is swallowed --
        class _BoomProvider:
            pass
        # monkeypatch promise_detect at import time inside the function via sys.modules
        import types
        fake_promise_detect = types.ModuleType("promise_detect")
        fake_promise_detect.detect_promise = lambda text, creds: (_ for _ in ()).throw(RuntimeError("boom"))
        sys.modules["promise_detect"] = fake_promise_detect
        try:
            t, r = classify_reply_risk("Sounds good, talk soon.", creds={"fake": "creds"})
            ck("a promise_detect exception never blocks classification (fails open to tier0)",
               t == TIER_ZERO)
        finally:
            del sys.modules["promise_detect"]

        fake_promise_detect2 = types.ModuleType("promise_detect")
        fake_promise_detect2.detect_promise = lambda text, creds: {"promise": "we'll send the report Monday"}
        sys.modules["promise_detect"] = fake_promise_detect2
        try:
            t, r = classify_reply_risk("Sounds good, talk soon.", creds={"fake": "creds"})
            ck("a real promise_detect hit forces at least tier1", t == TIER_ONE)
        finally:
            del sys.modules["promise_detect"]

        # -- turn_count / decide --
        ck("turn_count on an empty ledger is 0", turn_count("alyssa", "Some topic", path=tmp) == 0)

        d1 = decide("alyssa", "Re: mind-map examples", "Here are two picture examples.", path=tmp)
        ck("turn 1, tier0 -> auto_send", d1["turn"] == 1 and d1["action"] == "auto_send")

        d2 = decide("alyssa", "Re: mind-map examples", "One more example, hope that helps.", path=tmp)
        ck("turn 2, still tier0 -> auto_send", d2["turn"] == 2 and d2["action"] == "auto_send")

        d3 = decide("alyssa", "Re: mind-map examples", "Here's a third clarification.", path=tmp)
        ck("turn 3, tier0 content but past the turn limit -> queue, not auto_send",
           d3["turn"] == 3 and d3["action"] == "queue" and d3["tier"] == TIER_ZERO)

        d4 = decide("alyssa", "Re: mind-map examples", "Still going back and forth here.", path=tmp)
        ck("turn 4 on the same thread escalates to tier2 (going in circles)",
           d4["turn"] == 4 and d4["tier"] == TIER_TWO and d4["action"] == "queue")

        ck("a DIFFERENT thread from the same client starts back at turn 1",
           decide("alyssa", "Re: a completely different question",
                  "Sure, here's an answer.", path=tmp)["turn"] == 1)

        ck("a NEVER-tier reply never auto-sends even on turn 1",
           decide("bill", "Re: invoice", "Please wire the funds to the account below.",
                  path=tmp)["action"] == "queue")

        ents = _events(tmp)
        ck("the shadow ledger recorded every decision made above", len(ents) == 6)
        ck("every logged decision is explicitly marked shadow=True (never a real send)",
           all(e.get("shadow") is True for e in ents))
    finally:
        if tmp.exists():
            tmp.unlink()

    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    if "selftest" in sys.argv:
        sys.exit(selftest())
    print(__doc__)
