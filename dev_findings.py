#!/usr/bin/env python3
"""The front door — what should decide what the dev-loop builds overnight? (S81)

THE PROBLEM THIS ANSWERS
------------------------
S80 measured it and the number is the whole argument. The nightly queue had been
empty since 2026-08-23: three straight nights of `[gate-starved] … 0 reached
/approve`, out of 26-31 candidates a week. The relevance gate was not broken —
reading the rejections, every one was correctly refused ("Add Eater.com for
restaurant trends", "Install an AMD-optimized llama.cpp" on a Mac Studio,
"Install M5 Ultra Mac Studio", hardware that does not exist).

The fault was upstream of the gate. **Every proposal the loop has ever seen came
from `self_review.py` reading `daily-actions-*.md` — i.e. from an AI-newsletter
reader.** In 26-31 candidates a week it has never once proposed working on
something known to be wrong with our own code.

> The engine got a better repair loop in S80. The fuel line was connected to the
> wrong tank.

THE PRINCIPLE
-------------
**A proposal should be a FINDING, not an opinion.** Something one of the checks
we already trust has proven about our own repo — with a file, a line, and
evidence — rather than something an article suggested.

And among findings, first in line are the ones that **blind the loop's own
verification**, because everything else it does rests on those:

    dev_agent's gate 2 runs "the changed module's own selftest". For a module
    with no selftest it reports "selftest" while inspecting NOTHING. That is
    exactly how config_snapshot.py's bug survived to S80 -- the gate said
    selftest and looked at nothing. Gate 3 runs the suites of everything that
    IMPORTS the changed module, so a module with high fan-in and no suite
    blinds gate 3 for every one of its importers too.

So the ranking is not "how interesting" but **how much of the loop's own sight
this gap costs** — fan-in, measured with `dev_agent.importers_of`, the same
function gate 3 uses. Fixing these compounds: each one restores a gate.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
* **It does not raise the size caps.** S80 framed the caps and the front door as
  one problem, because it assumed CLIENT REQUESTS would be the first source and
  Justin's was five files and thousands of lines against a 4-file/12-edit cap.
  Choosing findings first DISSOLVES that collision instead of negotiating it: a
  missing selftest or a stalled signal is small by nature. The loop earns trust
  on work it already fits through before anyone widens the door.
* **It does not bypass the risk classifier.** `classify_risk` / `may_build` still
  decide the tier, and a finding that classifies below Tier 1 is DROPPED with the
  reason recorded. It bypasses only the MISSION-RELEVANCE gate, and only because
  that gate exists to filter external newsletter noise — a finding is a fact
  about our own repo that a check we trust already produced. Relevance is not
  safety; safety is still `classify_risk`.
* **It does not flood /approve.** Emitting all 28 missing selftests would move
  the bottleneck rather than fix it: /approve would become a chore instead of a
  decision, and a chore gets skimmed. `MAX_PER_RUN` is deliberately small. **The
  scarce resource is Buddy's attention, not queue space.**

WHAT IT CANNOT DO, STATED PLAINLY
---------------------------------
Every collector here reports a property of CODE. None of them can notice we are
building the wrong thing. S80's Styx point stands: the best output of Halftime
came from Buddy asking a question, and no amount of finding-collection produces
that. This widens the fuel line; it does not make the engine imaginative.
"""
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

# How many findings may reach /approve in one run. Small on purpose -- see the
# module docstring. Two is one night's build cap (MAX_BUILDS_PER_RUN), so the
# queue cannot outrun the builder and grow a backlog nobody reads.
MAX_PER_RUN = 2

# ...and never let more than this many findings sit UNACTIONED in /approve at
# once. MAX_PER_RUN alone is not enough: it caps a single run, so a nightly job
# adds two every night whether or not Buddy has looked at yesterday's, and the
# queue grows without bound. Caught by running the thing twice in a row and
# watching it propose two more (S81).
#
# This is the front door's actual discipline. **The scarce resource is Buddy's
# attention, not queue space** -- so the door opens at exactly the rate he
# drains it, and a full queue means the right answer tonight is to propose
# NOTHING. A backlog nobody reads is the same as no front door, with more noise.
MAX_OUTSTANDING = 2

# A module needs enough substance for a selftest to be worth writing. Below
# this, "add a selftest" is busywork that spends a build slot.
MIN_LINES_FOR_SELFTEST = 80

# Files that are not application modules and must not be proposed against.
# Derived where possible rather than listed: anything whose NAME says it is a
# test or a one-shot script. A list here rots (T44); a rule does not.
_NOT_A_MODULE = re.compile(
    r"(^|_)(test|tests|selftest|e2e|conftest)(_|$)|_e2e$|^setup$", re.I)


def _rel(p) -> str:
    try:
        return str(Path(p).relative_to(PROJECT_DIR))
    except Exception:
        return str(p)


# ── Collectors ────────────────────────────────────────────────────────────────
# Each returns a list of findings:
#   {kind, key, title, detail, rank, evidence, files}
# `key` is the stable dedupe identity -- it must not contain dates, counts or
# anything else that drifts, or the same finding re-proposes itself nightly.

def collect_blind_gates(root=None, min_lines=MIN_LINES_FOR_SELFTEST):
    """Modules with no selftest, ranked by how many gates the gap blinds.

    Uses dev_agent's OWN importers_of/has_selftest, so this measures exactly
    what gate 2 and gate 3 will do -- not an approximation of it.
    """
    root = Path(root or PROJECT_DIR)
    try:
        import dev_agent
    except Exception:
        return []
    out = []
    for fp in sorted(root.glob("*.py")):
        stem = fp.stem
        if _NOT_A_MODULE.search(stem):
            continue
        try:
            lines = len(fp.read_text(errors="ignore").splitlines())
        except OSError:
            continue
        if lines < min_lines:
            continue
        # Two ways a gate is blind, and the second is worse because it LOOKS
        # covered: no selftest at all, or a selftest gate 2 cannot invoke
        # (defined but no argv dispatch, so running the file bare would execute
        # its production path instead). has_selftest answers both -- it is True
        # only when there is a suite the gate can actually run.
        if dev_agent.has_selftest(fp):
            continue
        try:
            uninvokable = bool(re.search(r"^\s*def\s+_?selftest\s*\(",
                                         fp.read_text(errors="ignore"), re.M))
        except OSError:
            uninvokable = False
        fan_in = len(dev_agent.importers_of(stem, root))
        # rank = gates blinded. 1 for this module's own gate 2, plus one per
        # importer whose gate-3 run inspects nothing when this file changes.
        rank = 1 + fan_in
        out.append({
            "kind": "blind_gate",
            "key": f"blind_gate:{fp.name}",
            "title": (f"{fp.name} has a selftest no gate can invoke"
                      if uninvokable else
                      f"{fp.name} has no selftest, so the dev-loop cannot verify changes to it"),
            "detail": (
                (f"{fp.name} ({lines} lines) DEFINES a selftest but dispatches to "
                 f"it from nothing, so dev_agent's gate 2 cannot run it: invoking "
                 f"the file bare would execute its production __main__ path "
                 f"instead. Add an argv dispatch so `python3 {fp.name} --selftest` "
                 f"runs the existing selftest and exits non-zero on failure. "
                 f"Do not change the selftest's assertions or any other behaviour."
                 if uninvokable else
                 f"{fp.name} ({lines} lines) defines no selftest(). Add one that "
                 f"exercises its decision-making functions with explicit inputs "
                 f"and expected outputs, and dispatch to it from argv so "
                 f"`python3 {fp.name} --selftest` exits non-zero on failure. "
                 f"Do not change any existing behaviour in the file.")
                + f" This matters because dev_agent's gate 2 runs 'the changed "
                  f"module's own selftest' and reports 'selftest' for this file "
                  f"while inspecting nothing, and gate 3 runs the suites of its "
                  f"{fan_in} importer(s), which cannot cover it either."),
            "rank": rank,
            "evidence": (f"{lines} lines, {fan_in} importer(s), "
                         + ("selftest defined but not dispatched" if uninvokable
                            else "no def selftest")),
            "files": [fp.name],
        })
    return sorted(out, key=lambda f: (-f["rank"], f["key"]))


def collect_stalled_signals():
    """STALL rows from stall_check -- signals we already decided matter."""
    try:
        import stall_check
        rows = stall_check.run_all()
    except Exception:
        return []
    out = []
    for r in rows:
        if r.get("state") != getattr(stall_check, "STALL", "stall"):
            continue
        name = str(r.get("name", "")).strip()
        msg = str(r.get("msg", "")).strip()
        out.append({
            "kind": "stalled_signal",
            "key": f"stalled_signal:{name}",
            # rank below a blind gate: a stalled signal is a real problem, but a
            # blind gate costs us the ability to SAFELY FIX any problem.
            "rank": 1,
            "title": f"stall-check reports '{name}' stalled",
            "detail": (f"stall_check reports the '{name}' signal as STALLED: {msg} "
                       f"Investigate and repair the underlying measurement or the "
                       f"process that feeds it."),
            "evidence": msg[:200],
            "files": [],
        })
    return out


def collect_repair_giveups(path=None, limit=50):
    """Attempts where the repair loop ran out of road -- the agent saying what
    it could not do. S80 built this journal precisely so it could be read back."""
    p = Path(path) if path else PROJECT_DIR / "logs/dev-loop/repairs.jsonl"
    try:
        lines = p.read_text(errors="ignore").splitlines()[-limit:]
    except OSError:
        return []
    seen, out = set(), []
    for line in lines:
        try:
            r = json.loads(line)
        except Exception:
            continue
        outcome = str(r.get("outcome", "")).lower()
        if not any(w in outcome for w in ("gave-up", "gave up", "exhausted", "refused")):
            continue
        bid = str(r.get("build_id") or r.get("id") or "")
        gate = str(r.get("gate", "") or "")
        key = f"repair_giveup:{bid}:{gate}"
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "kind": "repair_giveup",
            "key": key,
            "rank": 2,
            "title": f"the repair loop gave up on {bid or 'a build'} at gate {gate or '?'}",
            "detail": (f"dev_agent's repair loop recorded outcome '{outcome}' for "
                       f"build {bid} at gate {gate}. Approach was: "
                       f"{str(r.get('approach', ''))[:200]}. Diagnose why the gate "
                       f"could not be satisfied and fix the underlying defect."),
            "evidence": f"outcome={outcome} gate={gate} build={bid}",
            "files": [],
        })
    return out


COLLECTORS = (collect_blind_gates, collect_stalled_signals, collect_repair_giveups)


def collect_all(collectors=None):
    """Every finding from every collector. A collector that raises is SKIPPED
    LOUDLY rather than silently -- a front door that quietly stops producing is
    indistinguishable from a tree with nothing wrong in it (T44)."""
    findings, errors = [], []
    for c in (collectors or COLLECTORS):
        try:
            findings.extend(c() or [])
        except Exception as e:
            errors.append(f"{getattr(c, '__name__', c)}: {type(e).__name__}: {e}")
    return findings, errors


# ── Selection ─────────────────────────────────────────────────────────────────
def select(findings, seen_keys, limit=MAX_PER_RUN):
    """Highest-ranked findings we have not already proposed. Pure."""
    fresh = [f for f in findings if f.get("key") not in set(seen_keys)]
    fresh.sort(key=lambda f: (-int(f.get("rank", 0)), str(f.get("key", ""))))
    return fresh[:max(0, int(limit))]


def spec_id(finding_key: str, date=None) -> str:
    """A proposal id that is unique per finding and stable per day.

    Keeps make_spec's `prop-<date>-<n>` shape so every existing consumer and the
    approve-finding id pattern still match; only the number changes meaning,
    from "position in this run" to "which finding this is".
    """
    date = date or datetime.now().strftime("%Y-%m-%d")
    n = int(hashlib.sha1(finding_key.encode()).hexdigest()[:8], 16) % 900000 + 100000
    return f"prop-{date}-{n}"


def to_item(f: dict, date=None) -> dict:
    """A finding -> the proposal shape /approve and dev_agent already consume.

    `finding_key` rides along so dedupe can recognise this exact finding later
    however the detail text is reworded -- matching on prose is how the same
    thing gets proposed twice under two spellings.
    """
    date = date or datetime.now().strftime("%Y-%m-%d")
    return {
        "type": "BUG_FIX" if f.get("kind") != "blind_gate" else "TEST_GAP",
        "detail": f.get("detail", ""),
        "why": f.get("title", ""),
        "source_line": f"[{f.get('kind')}] {f.get('evidence', '')}",
        "source": f"dev_findings/{f.get('kind')}",
        "finding_key": f.get("key", ""),
        "added": date,
    }


def classify(item: dict):
    """(tier, reason) from the REAL classifier. Never bypassed here."""
    import dev_loop
    return dev_loop.classify_risk(item.get("type", ""),
                                  item.get("detail", ""),
                                  item.get("source_line", ""))


def buildable(item: dict):
    """(ok, reason). Tier 1 only -- same bar dev_agent.may_build applies."""
    import dev_loop
    tier, reason = classify(item)
    return tier == dev_loop.TIER_CONFIRM, f"tier {tier}: {reason}"


# ── Dedupe ────────────────────────────────────────────────────────────────────
def outstanding(pending) -> list:
    """Findings sitting in /approve still waiting on a human.

    Only `pending` counts. Once Buddy approves one it moves to the build queue
    and is no longer competing for his attention, so the door may open again.
    """
    out = []
    for row in (pending or []):
        r = row or {}
        if not r.get("finding_key"):
            continue
        if r.get("status") in (None, "", "pending"):
            out.append(r)
    return out


def room_for(pending, per_run=MAX_PER_RUN, cap=MAX_OUTSTANDING) -> int:
    """How many findings may be proposed right now. 0 when the queue is full."""
    return max(0, min(int(per_run), int(cap) - len(outstanding(pending))))


def seen_keys(pending=None, queue=None, builds=None):
    """Every finding_key already proposed, queued or built -- in ANY state.

    Includes filtered/deferred/discarded on purpose. A finding Buddy declined
    must not come back tomorrow; re-offering a rejected item nightly is how a
    queue trains you to ignore it. Reviving one is a deliberate act (`/revive`,
    or clearing the key), not the default.
    """
    keys = set()
    for row in (pending or []):
        k = (row or {}).get("finding_key")
        if k:
            keys.add(k)
    for row in (queue or []):
        k = ((row or {}).get("item") or {}).get("finding_key")
        if k:
            keys.add(k)
    for row in (builds or []):
        k = (row or {}).get("finding_key")
        if k:
            keys.add(k)
    return keys


# ── Run ───────────────────────────────────────────────────────────────────────
def run(dry_run=True, limit=MAX_PER_RUN):
    """Collect, select, and (unless dry) file into /approve. Returns a summary."""
    import cirrus_bot as B
    import dev_agent
    import dev_loop

    findings, errors = collect_all()
    for e in errors:
        print(f"  COLLECTOR FAILED: {e}")

    pending = B.load_pending()
    try:
        queue = dev_agent.queue_load()
    except Exception:
        queue = []
    try:
        builds = dev_agent.builds_load()
    except Exception:
        builds = []
    known = seen_keys(pending, queue, builds)

    # Top up to the backlog cap rather than adding `limit` every time.
    room = room_for(pending, limit)
    waiting = len(outstanding(pending))
    if room <= 0:
        print(f"  {waiting} finding(s) already waiting in /approve "
              f"(cap {MAX_OUTSTANDING}) — proposing nothing tonight.")
    picked = select(findings, known, room)
    proposed, dropped = [], []
    date = datetime.now().strftime("%Y-%m-%d")

    for f in picked:
        item = to_item(f, date)
        ok, why = buildable(item)
        if not ok:
            # The classifier refusing a finding is the gate WORKING. Recorded,
            # never reworded to slip past -- S80 declined to do exactly that and
            # said the gate was worth more than one night's demonstration.
            dropped.append((f, why))
            continue
        item["dev_spec"] = dev_loop.make_spec(item, len(proposed) + 1, date)
        # S81: make_spec numbers a spec by its POSITION in the run, so a second
        # run on the same day restarts at 1 and two different findings get the
        # same id. That is not cosmetic: dev_agent.queue_load dedupes on this id
        # and would SILENTLY DROP one of them, and find_buildable skips an id it
        # has a build record for. Derive it from the finding instead, so it is
        # unique across findings and stable across runs of the same one.
        item["dev_spec"]["id"] = spec_id(f["key"], date)
        if dry_run:
            proposed.append(item)
            continue
        item["status"] = "pending"
        pending.append(item)
        proposed.append(item)
        try:
            dev_loop.ledger_append(
                {"event": "proposal", "id": item["dev_spec"]["id"],
                 "tier_name": item["dev_spec"]["tier_name"],
                 "detail": item.get("detail", "")[:200],
                 "result": f"queued for /approve (dev_findings/{f['kind']})"},
                PROJECT_DIR)
        except Exception as e:
            print(f"  ledger append failed (continuing): {e}")

    if proposed and not dry_run:
        B.save_pending(pending)

    if not dry_run:
        # S81: this is a scheduled job now, so it reports like one. A front door
        # that silently stops producing looks exactly like a tree with nothing
        # wrong in it -- the T44 shape, and the reason placement.py's coverage
        # check would flag this unit the moment it went on the schedule unwatched.
        # A run that proposes NOTHING because the queue is full is a HEALTHY run,
        # so ok stays True and the note says which it was.
        try:
            import job_status
            job_status.record(
                "devfindings", True,
                f"{len(proposed)} proposed, {len(findings)} finding(s) collected, "
                f"{waiting} already waiting"
                + (f", {len(errors)} collector error(s)" if errors else ""))
        except Exception as e:
            print(f"  job_status.record failed: {e}")

    return {
        "dry_run": bool(dry_run),
        "collected": len(findings),
        "already_waiting": waiting,
        "room": room,
        "already_known": len([f for f in findings if f.get("key") in known]),
        "proposed": [{"id": (i.get("dev_spec") or {}).get("id"),
                      "type": i["type"], "why": i.get("why", "")} for i in proposed],
        "dropped": [{"key": f["key"], "why": w} for f, w in dropped],
        "collector_errors": errors,
    }


# ── selftest ──────────────────────────────────────────────────────────────────
def selftest() -> bool:
    import tempfile
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    def F(key, rank, kind="blind_gate"):
        return {"kind": kind, "key": key, "rank": rank, "title": key,
                "detail": "d", "evidence": "e", "files": []}

    # ---- selection ----------------------------------------------------------
    fs = [F("a", 1), F("b", 5), F("c", 3)]
    ck("selection is by rank, highest first",
       [f["key"] for f in select(fs, set(), 3)] == ["b", "c", "a"])
    ck("the limit is respected", len(select(fs, set(), 2)) == 2)
    ck("an already-proposed finding is not re-offered",
       [f["key"] for f in select(fs, {"b"}, 3)] == ["c", "a"])
    ck("a limit of 0 proposes nothing", select(fs, set(), 0) == [])
    ck("ties break deterministically, so two runs agree",
       [f["key"] for f in select([F("z", 2), F("y", 2)], set(), 2)] == ["y", "z"])

    # ---- the backlog cap ----------------------------------------------------
    # Found by running the thing twice: dedupe correctly skipped the first two
    # findings and then proposed two MORE. Per-run caps do not bound a nightly
    # job; only an outstanding cap does.
    P = lambda n, st="pending": [{"finding_key": f"k{i}", "status": st} for i in range(n)]
    ck("an empty /approve has room for a full run", room_for([]) == MAX_PER_RUN)
    ck("one waiting finding leaves room for one more", room_for(P(1)) == 1)
    ck("a FULL /approve leaves no room at all", room_for(P(MAX_OUTSTANDING)) == 0)
    ck("...and an over-full one does not go negative",
       room_for(P(MAX_OUTSTANDING + 5)) == 0)
    ck("an APPROVED finding no longer competes for attention",
       room_for(P(MAX_OUTSTANDING, st="approved")) == MAX_PER_RUN)
    ck("a FILTERED finding does not hold the door shut forever",
       room_for(P(MAX_OUTSTANDING, st="filtered")) == MAX_PER_RUN)
    ck("rows that are not findings do not count against the cap",
       room_for([{"type": "CIRRUS_NOTE", "detail": "x"}] * 5) == MAX_PER_RUN)
    ck("outstanding() counts only unactioned findings",
       len(outstanding(P(2) + P(2, st="approved"))) == 2)
    ck("a full queue means propose NOTHING, not one anyway",
       select([F("a", 9), F("b", 8)], set(), room_for(P(MAX_OUTSTANDING))) == [])

    # ---- dedupe reaches every lane ------------------------------------------
    ck("a key already in /approve is known", "k1" in seen_keys(pending=[{"finding_key": "k1"}]))
    ck("a key already QUEUED is known",
       "k2" in seen_keys(queue=[{"item": {"finding_key": "k2"}}]))
    ck("a key already BUILT is known", "k3" in seen_keys(builds=[{"finding_key": "k3"}]))
    ck("a declined finding stays known, so it is not re-offered nightly",
       "k4" in seen_keys(pending=[{"finding_key": "k4", "status": "filtered"}]))
    ck("rows without a finding_key are ignored, not crashed on",
       seen_keys(pending=[{}, None], queue=[{}], builds=[{}]) == set())

    # ---- the finding -> proposal shape --------------------------------------
    it = to_item(F("blind_gate:foo.py", 4))
    ck("a proposal carries its finding_key for later dedupe",
       it["finding_key"] == "blind_gate:foo.py")
    ck("provenance names the collector", it["source"].startswith("dev_findings/"))
    ck("a blind gate is typed TEST_GAP", it["type"] == "TEST_GAP")
    ck("a stalled signal is typed BUG_FIX",
       to_item(F("s", 1, kind="stalled_signal"))["type"] == "BUG_FIX")

    # ---- proposal ids must be unique per FINDING, not per position ----------
    # make_spec numbers by position in the run, so a second run the same day
    # restarts at 1 and two different findings collide. Found by listing
    # /approve after two runs and seeing prop-2026-08-27-1 twice. Not cosmetic:
    # dev_agent.queue_load dedupes on this id and would silently drop one.
    d = "2026-08-27"
    ck("two different findings get different ids",
       spec_id("blind_gate:ensemble.py", d) != spec_id("blind_gate:llm_providers.py", d))
    ck("the same finding gets the SAME id on a re-run (stable, not random)",
       spec_id("blind_gate:ensemble.py", d) == spec_id("blind_gate:ensemble.py", d))
    ck("the id keeps the prop-<date>-<n> shape every consumer expects",
       re.fullmatch(r"prop-\d{4}-\d{2}-\d{2}-\d+", spec_id("k", d)) is not None)
    ck("30 real-shaped keys produce 30 distinct ids",
       len({spec_id(f"blind_gate:mod{i}.py", d) for i in range(30)}) == 30)

    # ---- the type must actually be EXECUTABLE on approval --------------------
    # THE bug that nearly shipped: cirrus_bot.execute_action dispatches on
    # action_type, and TEST_GAP / BUG_FIX were not among the types it handled.
    # Approving a finding would have printed "Unknown action type" and queued
    # NOTHING -- a silent no-op at the exact moment Buddy believed he had
    # approved a build. Read out of the bot's SOURCE rather than retyped, so a
    # new collector with a new type fails here instead of at approval time.
    kinds = {"blind_gate", "stalled_signal", "repair_giveup"}
    emitted = {to_item(F("k", 1, kind=k))["type"] for k in kinds}
    ck("every collector kind maps to a proposal type", len(emitted) >= 1)
    bot = PROJECT_DIR / "cirrus_bot.py"
    if bot.exists():
        text = bot.read_text(errors="ignore")
        i = text.find("def execute_action")
        body = text[i:] if i >= 0 else ""
        unhandled = sorted(t for t in emitted
                           if not re.search(r"action_type\s*(?:==|in)[^\n]*"
                                            + re.escape(t), body))
        ck(f"every emitted type is handled by execute_action (unhandled: {unhandled})",
           not unhandled)
        ck("...and the handler reaches the build queue, not just a log line",
           "queue_append" in body)
    else:
        ck("cirrus_bot.py readable so the type check can run (NOT verified)", False)

    # ---- the classifier is NOT bypassed --------------------------------------
    ok, why = buildable(it)
    ck(f"a blind-gate finding classifies as buildable Tier 1 ({why})", ok)
    danger = to_item({"kind": "x", "key": "x", "title": "t", "evidence": "e",
                      "detail": "delete the old backups and drop the table",
                      "files": []})
    ok2, why2 = buildable(danger)
    ck(f"a destructive finding is REFUSED by the real classifier ({why2})", not ok2)

    # ---- collector: the blind-gate rule -------------------------------------
    tmp = Path(tempfile.mkdtemp())
    (tmp / "big_untested.py").write_text("\n".join(f"x = {i}" for i in range(200)))
    (tmp / "big_with_test.py").write_text(
        "\n".join(f"x = {i}" for i in range(200))
        + "\ndef selftest():\n    return True\nif '--selftest' in []:\n    selftest()\n")
    (tmp / "tiny.py").write_text("x = 1\n")
    (tmp / "thing_e2e.py").write_text("\n".join(f"x = {i}" for i in range(200)))
    (tmp / "test_helper.py").write_text("\n".join(f"x = {i}" for i in range(200)))
    (tmp / "importer.py").write_text(
        "import big_untested\n" + "\n".join(f"y = {i}" for i in range(200)))
    got = {f["key"] for f in collect_blind_gates(tmp)}
    ck("a big module with no selftest is found", "blind_gate:big_untested.py" in got)
    # The first version of this fixture was named big_no_test.py, which the
    # is-this-a-test-file rule correctly excluded -- the test was measuring its
    # own bad fixture name, not the collector. Pin that the rule still bites.
    ck("a name ending _test IS treated as a test file",
       _NOT_A_MODULE.search("big_no_test") is not None)
    ck("...but an ordinary module name is not",
       _NOT_A_MODULE.search("big_untested") is None)
    ck("a module that HAS a selftest is not proposed",
       "blind_gate:big_with_test.py" not in got)
    ck("a tiny module is not busywork", "blind_gate:tiny.py" not in got)
    ck("an _e2e test file is not mistaken for a module",
       "blind_gate:thing_e2e.py" not in got)
    ck("a test_ file is not mistaken for a module",
       "blind_gate:test_helper.py" not in got)

    # The SECOND kind of blind gate, and the worse one because it looks
    # covered: a selftest that exists but that gate 2 cannot invoke. Five real
    # modules are in this state (task_solver, mailer, deep_research,
    # client_promises, promise_detect).
    (tmp / "uninvokable.py").write_text(
        "\n".join(f"x = {i}" for i in range(200))
        + "\ndef selftest():\n    return True\n"
          "if __name__ == '__main__':\n    print('production path')\n")
    got2 = {f["key"]: f for f in collect_blind_gates(tmp)}
    ck("a selftest nothing can invoke is still a blind gate",
       "blind_gate:uninvokable.py" in got2)
    ck("...and the finding says so, rather than 'no selftest'",
       "no gate can invoke" in got2["blind_gate:uninvokable.py"]["title"])
    ck("...and does not tell the model to write a suite that already exists",
       "DEFINES a selftest" in got2["blind_gate:uninvokable.py"]["detail"])

    ranks = {f["key"]: f["rank"] for f in collect_blind_gates(tmp)}
    ck("fan-in raises the rank (an importer blinds gate 3 too)",
       ranks.get("blind_gate:big_untested.py", 0) > ranks.get("blind_gate:importer.py", 99))

    # The key must be stable, or the same gap is re-proposed every night.
    ck("the key carries no date or count",
       not re.search(r"\d{4}-\d{2}-\d{2}|\bline", "blind_gate:big_untested.py"))
    ck("two collections of an unchanged tree agree exactly",
       [f["key"] for f in collect_blind_gates(tmp)]
       == [f["key"] for f in collect_blind_gates(tmp)])

    # ---- a broken collector must be loud, not silent -------------------------
    def boom():
        raise RuntimeError("collector exploded")
    fnd, errs = collect_all([boom, lambda: [F("ok", 1)]])
    ck("a collector that raises does not kill the run", len(fnd) == 1)
    ck("...and its failure is REPORTED, not swallowed",
       len(errs) == 1 and "collector exploded" in errs[0])

    bad = 0
    for name, ok_ in checks:
        print(("  ok   " if ok_ else "  FAIL ") + name)
        bad += 0 if ok_ else 1
    print()
    print("all dev_findings selftests passed" if not bad else f"{bad} FAILED")
    return bad == 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(0 if selftest() else 1)
    dry = "--live" not in sys.argv
    lim = MAX_PER_RUN
    if "--limit" in sys.argv:
        try:
            lim = int(sys.argv[sys.argv.index("--limit") + 1])
        except Exception:
            pass
    print(json.dumps(run(dry_run=dry, limit=lim), indent=2, default=str))
