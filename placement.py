#!/usr/bin/env python3
"""WHERE does each project run, and is that still true? — S69.

Buddy (2026-08-19): "let make sure we have documented where projects run and we
need to maintain this table for both machines. we want to prevent from running
on both machines if possible. All new projects need to be added to these tables
depending where it is running. We want to push what make sense and it is ready
to run on Cumulus. We are want to keep cirrus for developing new projects and
self ongoing self improvement."

WHY A CHECKER AND NOT A CHECKLIST ITEM
--------------------------------------
docs/PROJECT-RUNTIME-REGISTRY.md already opened with "This is the canonical
answer" and "update this file in the same session." It was last verified
2026-08-10 and by 2026-08-19 it was wrong about six jobs, missing the whole
business-idea chain, and unaware of cumulus-daily-brief. The instruction to keep
it current was already there; following it is what failed.

So the registry is now MACHINE-READABLE (the ```registry block in that file) and
this module diffs it against what the boxes are actually running. Session wrap
runs it. Drift is reported, not remembered.

WHAT IT CATCHES
---------------
  1. live but UNDECLARED  -- a job running on a box with no registry row. This
     is the "new project got added and nobody updated the table" case.
  2. declared but MISSING -- a registry row with no live unit. Stale doc, or a
     job that silently stopped existing.
  3. schedule DRIFT       -- declared time != live time.
  4. RUNNING ON BOTH      -- the same project active on CIRRUS and CUMULUS.
     This is the one Buddy specifically asked to prevent, and it has bitten
     twice: the billsnow/billnewdev plists, and a disabled cirrus-daily.timer
     that read as a live 07:00 duplicate of the CIRRUS daily.

Dormant units (unloaded plists, disabled timers) are NOT duplicates -- they
cannot fire. That distinction is the whole reason the inventory records state.

    python3 placement.py --selftest
    placement-map | python3 placement.py --registry docs/PROJECT-RUNTIME-REGISTRY.md
"""

import re
import sys
from pathlib import Path

# Per-box infrastructure that SHOULD exist on both machines. Each entry needs a
# reason: an unexplained exception here is how a real duplicate gets waved
# through. These are not "projects" -- they are each box watching itself.
ALLOWED_BOTH = {
    "modelhealth": "each box checks the health of ITS OWN model endpoints",
    "netsample": "each box samples its own counters; the whole point is per-box",
    "watchdog": "each box watches its own services",
    "deadman": "each box proves its own liveness",
    "rebootmonthly": ("each box reboots ITSELF, on DIFFERENT days (CIRRUS the 1st, "
                      "CUMULUS the 15th) — staggered on purpose so the two are "
                      "never down together. One box owning both would be the bug"),
    "api": "each box serves its own admin API",
    "bot": "each box runs its own Telegram bot",
    "tunnel": "each box exposes itself",
    "creds-materialize": "each box materializes its own tmpfs credentials",
    "intake": "SEPARATE MAILBOXES — CIRRUS reads cirrustask@, CUMULUS reads "
              "cumulus@cumulustask.com. Not redundant coverage of one inbox.",
    # S70, 2026-08-20. Buddy: "move config over but leave MLS lookup on CIRRUS."
    # These two copies are NOT the same job:
    #   CUMULUS (www.cirrustask.com) — the Agreement-of-Sale generator, the part
    #     Aggie uses daily. Loopback-bound; Cloudflare Access is its only door.
    #   CIRRUS  (mls.cirrustask.com) — the Bright MLS lookup half. Owns
    #     Playwright, the MLS login and the live session cookies, which were
    #     deliberately NOT copied to CUMULUS: re-homing that session to a new IP
    #     and browser fingerprint would likely force a fresh 2FA and could flag
    #     Aggie's account.
    # So there is no double-send and no doubled work — the two halves are
    # disjoint. Revisit if option C (proxying MLS through CUMULUS) is ever
    # built: docs/OFFER-MLS-PROXY-DESIGN.md.
    "offer": "SPLIT APP, not a duplicate — CUMULUS serves the PDF generator at "
             "www.cirrustask.com; CIRRUS keeps the Bright MLS lookup half at "
             "mls.cirrustask.com because Playwright and the live MLS session "
             "stay there. See docs/OFFER-MLS-PROXY-DESIGN.md.",
}

# Which box a job belongs on, by role. Buddy, 2026-08-19.
ROLE_HOME = {
    "dev": "cirrus",        # building something new
    "selfimprove": "cirrus",  # the system working on itself
    "buddy": "cirrus",      # Buddy-facing digests/briefs/monitors
    "client": "cumulus",    # anything that reaches a real client
    "infra": "both",        # per-box infrastructure
    # Reports on, or supervises, the box it runs on. Its home is wherever its
    # subject is, so the role rule does not apply. Without this, Skywarden (the
    # CUMULUS supervisor) and the CUMULUS activity recap were both recommended
    # for a move to CIRRUS -- which would have pointed each of them at the wrong
    # machine. A placement rule that produces confidently wrong advice is worse
    # than no rule.
    "boxlocal": None,
}

_UNIT_RE = re.compile(r"^\s+(\S+)\s+(\S+(?:\s+wd\d)?|\S+)\s+(LOADED|dormant|ENABLED|service|active\S*)",
                      re.I)


def normalize(unit: str) -> str:
    """A unit name on either box -> the project it belongs to.

    com.cirrus.billsnow / cirrus-billsnow.timer / cirrus-billsnow.service all
    normalize to 'billsnow'. Without this, a duplicate is invisible: the two
    copies never share a string.
    """
    u = unit.strip()
    for suf in (".timer", ".service", ".plist"):
        if u.endswith(suf):
            u = u[: -len(suf)]
    for pre in ("com.cirrus.", "com.cowork.", "cirrus-", "cumulus-", "cowork-"):
        if u.startswith(pre):
            u = u[len(pre):]
            break
    return u


def parse_registry(text: str):
    """The ```registry fenced block in PROJECT-RUNTIME-REGISTRY.md.

    Columns: host  unit  role  schedule  state. '#' comments and blanks skipped.
    """
    rows, inside = [], False
    for line in (text or "").splitlines():
        st = line.strip()
        if st.startswith("```registry"):
            inside = True
            continue
        if inside and st.startswith("```"):
            break
        if not inside or not st or st.startswith("#"):
            continue
        f = st.split()
        if len(f) < 5:
            continue
        rows.append({"host": f[0].lower(), "unit": f[1], "role": f[2].lower(),
                     "schedule": f[3], "state": f[4].lower(),
                     "key": normalize(f[1])})
    return rows


def parse_live(text: str):
    """Output of the runner's `placement-map`."""
    rows, host = [], None
    for line in (text or "").splitlines():
        if "=====" in line:
            up = line.upper()
            if "CIRRUS" in up:
                host = "cirrus"
            elif "CUMULUS" in up:
                host = "cumulus"
            continue
        if not host or not line.startswith("  ") or line.strip().startswith("--"):
            continue
        f = line.split()
        if len(f) < 3:
            continue
        unit, state = f[0], f[-1]
        sched = " ".join(f[1:-1])
        if not re.match(r"^[A-Za-z0-9._-]+$", unit):
            continue
        # active/enabled -> live; dormant/disabled -> not live
        live = state.upper().startswith(("LOADED", "ENABLED")) or \
            state.lower().startswith("active")
        rows.append({"host": host, "unit": unit, "schedule": sched,
                     "state": state, "live": live, "key": normalize(unit)})
    return rows


def parse_unreachable(text: str):
    """Hosts the inventory could not enumerate at all.

    S73: `placement-audit` reported 15 CUMULUS units as MISSING — "remove the
    row or restore the job" — when the box was healthy and every one of those
    units was running. The inventory simply could not SSH to it, printed
    nothing, and an empty section is indistinguishable from an empty box. The
    advice was to delete a correct registry.

    So the inventory now emits an explicit UNREACHABLE banner, and a host that
    was never looked at is excluded from every "it is not there" conclusion.
    """
    hosts = set()
    for line in (text or "").splitlines():
        if "=====" not in line or "UNREACHABLE" not in line.upper():
            continue
        up = line.upper()
        if "CIRRUS" in up:
            hosts.add("cirrus")
        elif "CUMULUS" in up:
            hosts.add("cumulus")
    return hosts


# Placements deliberately decided against the role default, with the reasoning
# recorded elsewhere. Add to this ONLY with a doc reference — the point is that
# the decision is written down, not that the warning is annoying.
SETTLED_PLACEMENT = {
    "com.cirrus.offer",   # docs/OFFER-MLS-PROXY-DESIGN.md — split app, MLS half
                          # needs Playwright + the live session on CIRRUS
}


def check(registry, live, unreachable=()):
    """-> (problems, notes). A problem is actionable drift."""
    problems, notes = [], []
    unreachable = set(unreachable)

    # A box we could not read tells us NOTHING about its units. Drop its rows
    # from both sides so no absence-based check can fire on them, and say so.
    for host in sorted(unreachable):
        problems.append(
            f"UNREACHABLE could not enumerate {host} — this audit says NOTHING "
            f"about that box. Fix the connection and re-run; do not act on "
            f"missing/undeclared units for {host}.")
    registry = [r for r in registry if r["host"] not in unreachable]
    live = [r for r in live if r["host"] not in unreachable]

    # S73: a host the registry knows about, that was NOT reported unreachable,
    # yet produced ZERO live units, means the inventory FAILED — not that the
    # box is empty. A syntax error in placement_inventory.sh did exactly this
    # and the audit answered with "38 PLACEMENT PROBLEM(S)", advising that 38
    # healthy rows be deleted. The UNREACHABLE banner only covers a host we
    # could not ssh to; this covers an inventory that ran and said nothing.
    seen_hosts = {r["host"] for r in live}
    for host in sorted({r["host"] for r in registry} - seen_hosts - unreachable):
        problems.append(
            f"NO INVENTORY the registry lists units on {host} but the inventory "
            f"returned NONE. That means the inventory failed, not that {host} is "
            f"empty — check placement_inventory.sh before believing any absence.")
    registry = [r for r in registry if r["host"] in seen_hosts]

    # 1 + 2: declared vs live, per (host, key).
    # Keyed on (host, UNIT), not (host, key). Normalising first collided
    # cirrus-intake.timer with cumulus-intake.service -- two distinct units on
    # ONE box -- and silently dropped whichever was seen second. The normalised
    # key is for cross-host duplicate detection only (check 4); it is the wrong
    # identity for "is this exact unit declared?".
    reg_idx = {(r["host"], r["unit"]): r for r in registry}
    live_idx = {(r["host"], r["unit"]): r for r in live}

    for k, r in sorted(live_idx.items()):
        if not r["live"]:
            continue
        if k not in reg_idx:
            problems.append(
                f"UNDECLARED  {r['host']}/{r['unit']} ({r['schedule']}) is running "
                f"but has no row in the registry — add it")
    for k, r in sorted(reg_idx.items()):
        if r["state"] == "dormant":
            continue
        if k not in live_idx:
            problems.append(
                f"MISSING     registry lists {r['host']}/{r['unit']} but no such "
                f"unit exists on that box — remove the row or restore the job")
        elif not live_idx[k]["live"]:
            problems.append(
                f"NOT RUNNING registry lists {r['host']}/{r['unit']} as "
                f"'{r['state']}' but the box reports {live_idx[k]['state']}")

    # 3: schedule drift, compared loosely — the registry records "02:00", the
    # box may say "02:00 wd0". A prefix match keeps weekday detail from reading
    # as drift while still catching a real time change.
    for k, r in sorted(reg_idx.items()):
        lv = live_idx.get(k)
        if not lv or r["schedule"] in ("-", "n/a"):
            continue
        # The registry writes spaces as '_' so the columns stay parseable;
        # undo that before comparing, or every multi-word schedule reads as
        # drift against itself.
        a = r["schedule"].strip().replace("_", " ")
        b = lv["schedule"].strip()
        if a and b and not (b.startswith(a) or a.startswith(b)):
            problems.append(
                f"DRIFT       {r['host']}/{r['unit']}: registry says "
                f"'{a}', box says '{b}'")

    # 4: the same project live on BOTH boxes.
    by_key = {}
    for r in live:
        if r["live"]:
            by_key.setdefault(r["key"], set()).add(r["host"])
    for key, hosts in sorted(by_key.items()):
        if len(hosts) < 2:
            continue
        if key in ALLOWED_BOTH:
            notes.append(f"both-ok  {key}: {ALLOWED_BOTH[key]}")
        else:
            problems.append(
                f"ON BOTH     '{key}' is live on CIRRUS *and* CUMULUS. One box "
                f"must own it — disable the other copy (do not delete it)")

    # 5: role placement. A client-facing job on CIRRUS is the case Buddy wants
    # pushed to CUMULUS; a dev job on CUMULUS is the reverse mistake.
    for r in registry:
        want = ROLE_HOME.get(r["role"])
        # S73: a placement already DECIDED and written down should not be
        # re-proposed on every run. The OFFER split is deliberate — CUMULUS
        # serves the PDF generator, CIRRUS keeps the Bright MLS lookup because
        # Playwright and the live MLS session stay here
        # (docs/OFFER-MLS-PROXY-DESIGN.md), and the both-ok line directly above
        # already says so. Advising "candidate to move" underneath it is
        # wallpaper: a recommendation that never changes teaches people to skim
        # past the section that also carries the real ones (T9).
        if r["unit"] in SETTLED_PLACEMENT:
            continue
        if want and want not in ("both",) and want != r["host"] and r["state"] != "dormant":
            notes.append(
                f"placement  {r['host']}/{r['unit']} is role '{r['role']}', which "
                f"belongs on {want.upper()} — candidate to move")
    return problems, notes


def selftest() -> bool:
    bad = 0

    def ck(label, got, want):
        nonlocal bad
        ok = got == want
        print(("  ok   " if ok else "  FAIL ") + f"{label}: {got!r}" +
              ("" if ok else f" (want {want!r})"))
        bad += 0 if ok else 1

    # Normalisation is what makes a cross-box duplicate visible at all.
    ck("launchd -> key", normalize("com.cirrus.billsnow"), "billsnow")
    ck("timer -> key", normalize("cirrus-billsnow.timer"), "billsnow")
    ck("service -> key", normalize("cirrus-billsnow.service"), "billsnow")
    ck("cumulus prefix", normalize("cumulus-daily-brief.timer"), "daily-brief")
    ck("cowork prefix", normalize("cowork-netsample.timer"), "netsample")

    reg = parse_registry("""
```registry
# host unit role schedule state
cirrus  com.cirrus.daily        buddy   02:00   live
cumulus cirrus-hoaleads.timer   client  03:00   live
cirrus  com.cirrus.offer        client  always-on live
cirrus  com.cirrus.clientjob    client  09:00   live
cumulus cirrus-gone.timer       client  04:00   live
```
""")
    ck("registry rows", len(reg), 5)

    live = parse_live("""===== CIRRUS: launchd com.cirrus.* =====
  com.cirrus.daily                 02:00          LOADED
  com.cirrus.offer                 always-on      LOADED
  com.cirrus.clientjob             09:00          LOADED
  com.cirrus.newthing              06:00          LOADED
  com.cirrus.billsnow              04:00 wd1      dormant
  com.cirrus.modelhealth           05:30          LOADED

===== CUMULUS: systemd units =====
  cirrus-hoaleads.timer            *-*-* 09:00:00       ENABLED
  cirrus-billsnow.timer            Mon 04:00:00         ENABLED
  cirrus-modelhealth.timer         *-*-* 05:30:00       ENABLED
""")
    ck("live rows", len(live), 9)

    p, n = check(reg, live)
    j = " | ".join(p)
    ck("undeclared job caught", "UNDECLARED" in j and "newthing" in j, True)
    ck("missing registry row caught", "MISSING" in j and "gone" in j, True)
    ck("schedule drift caught", "DRIFT" in j and "hoaleads" in j, True)
    # billsnow is dormant on CIRRUS and enabled on CUMULUS -> NOT a duplicate.
    ck("dormant copy is not a duplicate", "billsnow" in j and "ON BOTH" in j, False)
    # modelhealth runs on both ON PURPOSE.
    ck("allow-listed both-box job not flagged",
       any("ON BOTH" in x and "modelhealth" in x for x in p), False)
    ck("...but is explained in notes",
       any("both-ok" in x and "modelhealth" in x for x in n), True)
    # A client job on CIRRUS is the move-to-CUMULUS candidate...
    ck("client job on cirrus flagged as candidate",
       any("placement" in x and "clientjob" in x for x in n), True)
    # ...but a placement already DECIDED and documented is not re-proposed every
    # run. S73: com.cirrus.offer is a deliberate split (Playwright + the live
    # MLS session must stay on CIRRUS). Both halves are tested, because
    # exempting one job must not disable the rule for the rest.
    ck("settled placement NOT re-proposed",
       any("placement" in x and "offer" in x and "candidate to move" in x for x in n), False)
    # A box-local job must never be recommended for a move: the CUMULUS
    # supervisor supervises CUMULUS.
    bl = parse_registry("```registry\n"
                        "cumulus cumulus-supervisor.service boxlocal service live\n```")
    _, bn = check(bl, [])
    ck("boxlocal job not recommended for a move",
       any("placement" in x for x in bn), False)

    # A genuine duplicate MUST be caught.
    dup = parse_live("""===== CIRRUS: launchd com.cirrus.* =====
  com.cirrus.hoaleads              03:00          LOADED

===== CUMULUS: systemd units =====
  cirrus-hoaleads.timer            *-*-* 03:00:00       ENABLED
""")
    dp, _ = check([], dup)
    ck("real duplicate caught",
       any("ON BOTH" in x and "hoaleads" in x for x in dp), True)

    # Underscores are the registry's space escape; comparing them raw made
    # every multi-word schedule read as drift against itself.
    us_reg = parse_registry("```registry\n"
                            "cirrus com.cirrus.digest buddy 02:30_wd0 live\n```")
    us_live = parse_live("===== CIRRUS =====\n"
                         "  com.cirrus.digest                02:30 wd0      LOADED\n")
    ck("underscore escape not read as drift",
       any("DRIFT" in x for x in check(us_reg, us_live)[0]), False)

    # Two units on ONE box that normalise to the same project must both survive
    # -- cirrus-intake.timer and cumulus-intake.service are distinct units.
    coll_reg = parse_registry(
        "```registry\n"
        "cumulus cirrus-intake.timer     infra interval live\n"
        "cumulus cumulus-intake.service  infra service  live\n```")
    coll_live = parse_live("===== CUMULUS =====\n"
                           "  cirrus-intake.timer              interval             ENABLED\n"
                           "  cumulus-intake.service           service              active/enabled\n")
    cp, _ = check(coll_reg, coll_live)
    ck("same-key units on one box do not collide", cp, [])

    # S73: an UNREACHABLE box must not produce a single MISSING. This is the
    # exact input that told us to delete 15 correct rows for a healthy CUMULUS.
    ur_reg = parse_registry("```registry\n"
                            "cumulus cirrus-bot.service    infra service live\n"
                            "cirrus  com.cirrus.api        infra always-on live\n```")
    ur_text = ("===== CIRRUS: launchd com.cirrus.* =====\n"
               "  com.cirrus.api                   always-on      LOADED\n"
               "===== CUMULUS: UNREACHABLE =====\n"
               "  !! could not ssh to the box\n")
    ur_live = parse_live(ur_text)
    ur_hosts = parse_unreachable(ur_text)
    ck("unreachable host detected", ur_hosts, {"cumulus"})
    up, _ = check(ur_reg, ur_live, ur_hosts)
    ck("no MISSING invented for an unread box",
       any("MISSING" in x for x in up), False)
    ck("...but the audit says loudly that it could not look",
       any("UNREACHABLE" in x for x in up), True)
    # And the reachable half must still be audited normally.
    ck("reachable host still checked",
       any("com.cirrus.api" in x for x in up), False)

    print()
    print("all placement selftests passed" if not bad else f"{bad} FAILED")
    return bad == 0




# ── Monitor coverage (S81) ────────────────────────────────────────────────────
# WHY THIS EXISTS
# ---------------
# On 2026-08-27 `opportunity-scout.service` died at 02:00 on CUMULUS and sat in
# `failed` for six hours with nobody told. It had done everything right: it
# wrote ok=False into the job_status ledger exactly as designed. Nothing read
# it, because a ledger entry is only ever looked at if the job ALSO appears in
# job_status.CADENCE_H -- a hand-written table that had not been extended since
# the job was built. Meanwhile cirrus-modelhealth, which IS in every list,
# failed at 05:30, was caught, restarted and healed inside sixty seconds. Same
# box, same minute-by-minute supervisor, opposite outcomes; the only difference
# was membership of a list somebody had to remember to edit.
#
# This is the FOURTH time a hand-maintained scope has gone stale in this tree
# (placement_inventory.sh S72 and S73, window-audit S79, this). Every previous
# fix widened one list. This one asks a different question: given the LIVE
# schedule the inventory already collects, is there a scheduled job that NO
# monitor watches? A new job is then unwatched-and-reported from the day it is
# installed, instead of unwatched-and-silent until it breaks.
#
# STALENESS DIRECTION. WATCH_ALIAS and EXEMPT are themselves hand-maintained,
# so they can rot too -- but they can only rot toward NOISE. An unknown unit is
# reported as unwatched; forgetting to add an alias makes this complain about a
# job that is fine. It can never produce a false all-clear, which is the only
# failure mode that actually costs us anything.

# Unit name (after normalize()) -> the key that job_status.CADENCE_H uses.
# Only irregular spellings need an entry; identical names need nothing.
WATCH_ALIAS = {
    "businessideasdigest":     "businessideareport",  # plist and ledger disagree
    "daily-brief":             "cumulusdailybrief",   # prefix stripped by normalize()
    "entity-kb-weekly-digest": "entitykbdigest",
}


def watch_keys(key: str):
    """Every spelling of `key` that a ledger might reasonably use.

    systemd unit names carry dashes (`halftime-catalogue`) and ledger keys
    generally do not (`halftimecatalogue`). Deriving that instead of listing it
    keeps three entries out of WATCH_ALIAS -- and every entry NOT in a
    hand-maintained list is one that cannot go stale, which is the whole point
    of this section. Only genuinely irregular names need an alias.
    """
    forms = {key, key.replace("-", ""), key.replace("-", "_")}
    if key in WATCH_ALIAS:
        forms.add(WATCH_ALIAS[key])
    return forms

# Scheduled jobs that deliberately have no ledger entry, each with the reason.
# An exemption must say WHY, so it can be argued with later.
EXEMPT = {
    "jobscheck":  "IS the overdue checker -- it reads the ledger, so watching "
                  "itself in the ledger would be circular",
    "watchdog":   "the liveness watchdog itself; its own failure surfaces via "
                  "the morning brief watchdog.log tail",
    # S81. Argued, not waved through. A ledger write from this job would have
    # to happen moments before the box goes down, so the entry would say "I am
    # about to reboot" and prove nothing about whether the reboot completed.
    # UPTIME is the direct evidence and cannot go stale: a box whose monthly
    # reboot stopped firing shows an uptime longer than a month, which
    # `cumulus-status` and `boot-readiness` both print. Watch the effect, not
    # the intent.
    "rebootmonthly": "reboots the box it runs on, so a ledger write could only "
                     "record the INTENT, never the result -- uptime is the "
                     "direct evidence and is already reported by cumulus-status "
                     "and boot-readiness",
}

# Schedule strings that mean 'not a scheduled job' -- always-on services and
# poll loops. These are liveness questions, answered by the supervisor and the
# API service list, not by an overdue ledger.
_NOT_SCHEDULED = ("always-on", "service", "interval", "every ")


def is_scheduled(row) -> bool:
    """Does this row describe a job that fires on a clock/calendar?"""
    sched = (row.get("schedule") or "").strip().lower()
    if not sched:
        return False
    return not any(sched.startswith(x) or sched == x.strip()
                   for x in _NOT_SCHEDULED)


def coverage(live, watched, unreachable=()):
    """Which live, scheduled jobs does no monitor watch?

    `watched` is the set of keys some monitor actually checks -- in practice
    job_status.CADENCE_H. Returns (unwatched, notes).

    A host we could not read contributes NOTHING and says so: an inventory that
    could not look must never render as "everything there is watched" (S73).
    """
    watched = set(watched)
    unreachable = {h.lower() for h in unreachable}
    unwatched, notes = [], []
    for r in sorted(live, key=lambda x: (x["host"], x["unit"])):
        if r["host"] in unreachable or not r["live"] or not is_scheduled(r):
            continue
        key = r["key"]
        if key in EXEMPT:
            continue
        if watch_keys(key) & watched:
            continue
        unwatched.append({"host": r["host"], "unit": r["unit"],
                          "schedule": r["schedule"], "key": key})
    for h in sorted(unreachable):
        notes.append(f"UNREACHABLE {h} — its jobs are NOT included above; "
                     f"this is not a clean result for that box.")
    return unwatched, notes


def coverage_selftest() -> bool:
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    def row(host, unit, sched, live=True):
        return {"host": host, "unit": unit, "schedule": sched, "state": "x",
                "live": live, "key": normalize(unit)}

    # THE case. opportunity-scout was live, scheduled daily, and watched by
    # nothing -- this must name it.
    scout = row("cumulus", "opportunity-scout.timer", "*-*-* 02:00:00")
    un, _ = coverage([scout], watched=set())
    ck("an unwatched scheduled job is reported",
       [u["unit"] for u in un] == ["opportunity-scout.timer"])
    un, _ = coverage([scout], watched={"opportunityscout"})
    ck("...and is silent once the ledger watches it", not un)

    # Dash-stripping must be doing real work -- the raw key does not match.
    ck("the scout's unit key is NOT the ledger key",
       normalize("opportunity-scout.timer") != "opportunityscout")
    ck("a dashed unit name matches an undashed ledger key without an alias",
       "opportunity-scout" not in WATCH_ALIAS
       and "opportunityscout" in watch_keys("opportunity-scout"))
    ck("halftime-catalogue matches halftimecatalogue with no alias entry",
       "halftime-catalogue" not in WATCH_ALIAS
       and not coverage([row("cumulus", "halftime-catalogue.timer", "*-*-* 06:30:00")],
                        watched={"halftimecatalogue"})[0])
    ck("an irregular name still needs (and has) its alias",
       not coverage([row("cumulus", "entity-kb-weekly-digest.timer", "Mon *-*-* 05:00:00")],
                    watched={"entitykbdigest"})[0])

    ck("the digest plist aliases to its ledger name",
       not coverage([row("cirrus", "com.cirrus.businessideasdigest", "04:45")],
                    watched={"businessideareport"})[0])

    # Always-on and poll loops are a different question; they must not appear.
    for sched in ("always-on", "service", "interval", "every 900s"):
        un, _ = coverage([row("cirrus", "com.cirrus.api", sched)], watched=set())
        ck(f"a {sched!r} unit is not treated as a scheduled job", not un)

    # A dormant unit is not a gap -- it is off on purpose.
    un, _ = coverage([row("cirrus", "com.cirrus.billsnow", "04:00 wd1", live=False)],
                     watched=set())
    ck("a dormant job is not reported as unwatched", not un)

    # Exemptions apply, and only to what is listed.
    un, _ = coverage([row("cirrus", "com.cirrus.jobscheck", "16:30")], watched=set())
    ck("an exempt job is not reported", not un)
    ck("every exemption states a reason",
       all(isinstance(v, str) and len(v) > 20 for v in EXEMPT.values()))
    un, _ = coverage([row("cumulus", "cirrus-rebootmonthly.timer", "*-*-15 08:15:00")],
                     watched=set())
    ck("the reboot job is exempt on BOTH boxes (name normalizes the same)", not un)
    ck("...and on cirrus too",
       not coverage([row("cirrus", "com.cirrus.rebootmonthly", "08:15")],
                    watched=set())[0])

    # "could not look" must not read as "all watched" (S73's lesson).
    un, notes = coverage([scout], watched=set(), unreachable=("cumulus",))
    ck("an unreachable host contributes no findings", not un)
    ck("...and says so out loud", any("UNREACHABLE" in n for n in notes))
    ck("...naming the box", any("cumulus" in n for n in notes))

    # A weekly job counts too -- cadence is irrelevant to coverage.
    un, _ = coverage([row("cumulus", "halftime-routing.timer", "Sun *-*-* 22:00:00")],
                     watched=set())
    ck("a weekly scheduled job is in scope", len(un) == 1)

    bad = 0
    for name, ok in checks:
        print(("  ok   " if ok else "  FAIL ") + name)
        bad += 0 if ok else 1
    print()
    print("all coverage selftests passed" if not bad else f"{bad} FAILED")
    return bad == 0


def main():
    if "--selftest" in sys.argv:
        ok = selftest()
        print()
        ok = coverage_selftest() and ok
        return 0 if ok else 1
    if "--coverage-selftest" in sys.argv:
        return 0 if coverage_selftest() else 1
    a = sys.argv
    # S72: make the registry AUTHORITATIVE for tooling, not just auditable after
    # the fact. `--live-units <host>` prints the units the registry says that box
    # should actually be running, so a command that starts/enables/converts jobs
    # can gate on ownership instead of on "a plist exists here".
    #
    # This is the fix for a real incident: the S72 batch converter enumerated
    # plists on CIRRUS and bootstrapped all of them, resurrecting billsnow,
    # billnewdev and pedagogy — which the registry already recorded as `dormant`
    # on cirrus and `live` on cumulus. The fact was written down; nothing asked.
    if "--live-units" in a:
        host = a[a.index("--live-units") + 1].lower()
        rp = a[a.index("--registry") + 1] if "--registry" in a else \
            "docs/PROJECT-RUNTIME-REGISTRY.md"
        for r in parse_registry(Path(rp).read_text()):
            if r["host"] == host and r["state"] == "live":
                print(r["unit"])
        return 0
    rp = a[a.index("--registry") + 1] if "--registry" in a else \
        "docs/PROJECT-RUNTIME-REGISTRY.md"
    registry = parse_registry(Path(rp).read_text())
    raw = sys.stdin.read()
    live = parse_live(raw)
    unreachable = parse_unreachable(raw)
    print(f"registry rows: {len(registry)}   live units: {len(live)}   "
          f"live+running: {sum(1 for r in live if r['live'])}\n")
    problems, notes = check(registry, live, unreachable)
    if problems:
        # T9: say what is actually wrong. "registry is out of date" is a false
        # accusation when the only finding is that a box could not be read.
        why = ("registry is out of date"
               if any(not x.startswith("UNREACHABLE") for x in problems)
               else "a box could not be read")
        print(f"=== {len(problems)} PLACEMENT PROBLEM(S) — {why} ===")
        for p in problems:
            print("  !! " + p)
    else:
        print("=== registry matches both boxes ===")
    if notes:
        print()
        for n in notes:
            print("  " + n)

    # S81 — second question off the SAME inventory: is every live scheduled job
    # watched by something? One collection, two questions, so the two answers
    # can never be taken against different views of the boxes.
    print()
    print("=== monitor coverage — is every scheduled job watched? ===")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import job_status
        watched = set(job_status.CADENCE_H)
    except Exception as e:
        print(f"  !! could not read job_status.CADENCE_H ({e}) — coverage NOT checked.")
        print("     That is a gap, not a pass.")
        return 1 if problems else 2
    unwatched, cnotes = coverage(live, watched, unreachable)
    for n in cnotes:
        print("  " + n)
    if unwatched:
        print(f"  {len(unwatched)} scheduled job(s) that NO monitor watches:")
        for u in unwatched:
            print(f"    !! {u['host']:<8} {u['unit']:<32} {u['schedule']}")
        print("     A job nobody watches fails silently. Add it to")
        print("     job_status.CADENCE_H (and have it call job_status.record),")
        print("     or add it to placement.EXEMPT with the reason why not.")
    else:
        print("  every live scheduled job on both boxes is watched.")
    # Exit 2 == coverage gap only, so the caller can name the RIGHT reason.
    # Folding it into 1 would print "registry is out of date" at a session
    # whose registry is perfect, and a check that misdescribes its own finding
    # is one people learn to skim (T9).
    if problems:
        return 1
    return 2 if unwatched else 0


if __name__ == "__main__":
    raise SystemExit(main())
