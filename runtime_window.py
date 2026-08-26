#!/usr/bin/env python3
"""Where in the day is it safe to run something? — S69 window policy.

WHY THIS EXISTS
---------------
Buddy (2026-08-19): "during the day 8am to 4pm the internet is being used by
local access so we should plan our larger processes before 8am in the morning
... each new project needs to find a window when access is available."

Before this, every new recurring job picked a fire time by eyeballing the two
schedules, and the times drifted into a pile: seven CIRRUS jobs were firing
between 07:00 and 08:15, all competing for the same uplink, minutes before the
constrained window opened. Nothing detected that, because nothing had an opinion
about when a job SHOULD run.

THE POLICY (from Buddy, 2026-08-19)
-----------------------------------
  * MONDAY-FRIDAY 08:00-16:00  -- local internet is in use by people. AVOID.
    Anything bandwidth-hungry scheduled here is a scheduling defect.
  * SATURDAY & SUNDAY          -- no such constraint. The whole day is open.
    ("Saturday and Sunday do not have the same rules, just monday thru Friday.")
  * 02:00-06:00                -- PREFERRED. "2am, 3am, 4am, 5am are good times
    to run for the day." Spread work across it; do NOT compact everything into
    the hour before 08:00.
  * The daily brief runs LAST, after everything it reports on.
    ("Only the daily brief should run last to catch the results.")

WHAT THIS MODULE IS FOR
-----------------------
Two jobs, and it is deliberately dumb about both:

  classify()   -- given a time, is it preferred / ok / avoid?
  find_window()-- given a duration and the jobs already scheduled, where should
                  a NEW job go? Returns the emptiest preferred slot.
  audit()      -- compare the LIVE schedule against the policy and report every
                  violation. This is the part that keeps the policy true six
                  months from now, when nobody remembers it exists.

It holds no schedule of its own. `audit` and `find_window` are fed the live
schedule (from the runner's `schedule-map`), so the policy cannot silently
disagree with reality -- a hand-maintained copy of the schedule in here would
rot within a week and then lie confidently.
"""

import re
import sys

# ── the policy ──────────────────────────────────────────────────────────────
# Refined by Buddy 2026-08-19, second pass. The FIRST version of this file
# treated every job the same and flagged anything inside 08:00-16:00. That was
# wrong: a status ping and a 165-item research crawl are not the same event.
#
#   "Things that access the internet heavily, should run after midnight and try
#    to complete before 8am. Small keep alive or checking status, can run
#    anytime there should be no restriction."
#
# So the constraint is on WEIGHT, not on the clock. Light jobs are unrestricted,
# full stop. Heavy jobs get 00:00-08:00 and must FINISH by 08:00 -- starting in
# the window is not enough, which is why durations below are measured rather
# than guessed.
#
#   "consider the estimate how much access it be using against the verizon line.
#    it is not a hard restriction but better planing"
#
# Hence LINE_* and the budget report: advisory, printed for planning, never a
# hard failure.
# Three tiers, not two. Buddy gave two separate facts and collapsing them
# loses information:
#   "8am to 4pm the internet is being used by local access"     -> real conflict
#   "heavy ... should run after midnight and complete before 8am" -> preference
# So 16:00-24:00 is neither. It is allowed for heavy work and simply is not the
# preferred window. Reporting an evening job as a VIOLATION would be crying
# wolf, and an audit that cries wolf stops being read.
HEAVY_START_H = 0     # preferred heavy window opens at midnight ...
HEAVY_END_H = 8       # ... and heavy work should be DONE by 08:00
BUSY_START_H = 8      # genuine conflict: local internet in use ...
BUSY_END_H = 16       # ... Mon-Fri only
BUSY_DAYS = {0, 1, 2, 3, 4}          # Mon-Fri only (Python weekday(): Mon=0)
BRIEF_JOB = "com.cirrus.morningbrief"
BRIEF_RULE = ("the daily brief must fire AFTER every job it reports on, so it "
              "catches the day's actual results")

# The Verizon Fios line, measured on CIRRUS behind UDM Port 1 (2026-07-21,
# docs/Infrastructure-and-Network-Notes.md). Used only to express a job's
# estimated draw as a share of the line -- advisory, per Buddy.
LINE_DOWN_MBPS = 880.0
LINE_UP_MBPS = 158.0

# ── the job registry ────────────────────────────────────────────────────────
# weight:   "heavy" = pulls substantial external content (crawls, feed/page
#           fetches, large LLM payloads). "light" = a ping, a status check, a
#           small compose/send. Only "heavy" is window-constrained.
# mins:     MEASURED, 2026-08-19, from systemd ExecMainStart/ExitTimestamp on
#           CUMULUS and per-job log write spans on CIRRUS. Not estimates.
# est_mb:   an ORDER-OF-MAGNITUDE GUESS, explicitly not measured yet. Per-job
#           byte attribution needs the sampler plus a night of separated runs;
#           that data arrives 2026-08-20. Treat these as placeholders and
#           replace them, rather than quoting them as if they were measured.
JOB = {
    # CIRRUS -------------------------------------------------------------
    "com.cirrus.daily":              ("heavy", 19, 250, "120-165 items, multi-source web research"),
    "com.cirrus.digest":             ("heavy", 4, 40, "weekly roll-up, Sundays"),
    "com.cirrus.privacymon":         ("heavy", 21, 60, "bi-weekly privacy sweep, many site checks"),
    "com.cirrus.businessideafeeds":  ("heavy", 1, 30, "RSS/feed pulls incl. Substack + Medium"),
    "com.cirrus.businessideascan":   ("heavy", 5, 40, "inbox fetch + per-email LLM prefilter"),
    "com.cirrus.businessideaideate": ("heavy", 7, 60, "council generation, large LLM payloads"),
    "com.cirrus.businessideasdigest": ("light", 1, 2, "composes and sends one digest"),
    "com.cirrus.vendormail":         ("light", 1, 5, "scans a small vendor inbox"),
    "com.cirrus.stratusreview":      ("light", 2, 10, "short review pass"),
    "com.cirrus.modelhealth":        ("light", 1, 2, "pings each model endpoint"),
    "com.cirrus.morningbrief":       ("light", 1, 3, "composes + sends; MUST BE LAST"),
    "com.cirrus.jobscheck":          ("light", 1, 1, "reads local job status"),
    "com.cirrus.devloop":            ("heavy", 15, 80, "autonomous dev agent, evening"),
    # S73: the monthly reboots. Buddy chose 08:15 deliberately — AFTER the daily
    # work finishes (07:30 morning brief is last) rather than before it, so that
    # if a reboot goes wrong there is a whole day to notice and recover. That
    # lands inside the Mon-Fri 08:00-16:00 local-use window, which the audit
    # rightly flagged while both were UNREGISTERED and therefore assumed heavy.
    # They are not heavy: a reboot moves essentially no data. Profiling them is
    # the honest fix — leaving a known-good choice permanently flagged is how a
    # check trains people to ignore it (T9).
    "com.cirrus.rebootmonthly":      ("light", 2, 1, "reboot: graceful stop then restart, no network work"),
    "cirrus-rebootmonthly.timer":    ("light", 2, 1, "reboot: graceful stop then restart, no network work"),
    # CUMULUS ------------------------------------------------------------
    "cirrus-hoaleads.timer":         ("heavy", 4, 120, "per-property deep-dive research"),
    "cirrus-billsnow.timer":         ("heavy", 3, 60, "weather + web research, Mondays"),
    "cirrus-billnewdev.timer":       ("heavy", 1, 40, "new-development research, Mondays"),
    "cirrus-pedagogy.timer":         ("heavy", 1, 50, "podcast/article research"),
    "cirrus-modelhealth.timer":      ("light", 1, 2, "pings each model endpoint"),
    "cirrus-deadman.timer":          ("light", 1, 0, "local liveness check, no network"),
    "cirrus-devloop.timer":          ("heavy", 15, 50, "autonomous dev agent, evening"),
    "cirrus-daily.timer":            ("heavy", 15, 50, "CUMULUS daily run"),
    "cirrus-intake.timer":           ("light", 1, 5, "polls the intake mailbox"),
    "cirrus-watchdog.timer":         ("light", 1, 1, "local service liveness"),
    "cowork-netsample.timer":        ("light", 1, 0, "reads local counters, no network"),
    # S79. The routing sweep is the longest job on either box by an order of
    # magnitude, and duration is exactly what the overlap check runs on: at the
    # unprofiled default of 15 minutes a job parked at 23:00 would have looked
    # clear of a sweep that is still running at midnight.
    "halftime-catalogue.timer":      ("heavy", 15, 50, "12+ search angles, nightly research"),
    "halftime-routing.timer":        ("heavy", 150, 120,
                                      "8 games x 7 metros, each a search + "
                                      "fetches + a model call; 2 games measured "
                                      "at 24 min on 2026-08-26"),
}
_DEFAULT = ("heavy", 15, 50, "UNREGISTERED -- treated as heavy until profiled")


def profile(job: str):
    """(weight, minutes, est_mb, basis) for a job.

    An unknown job defaults to HEAVY. A new job that nobody profiled should
    trip the window check and get looked at, not slip through as 'light' and
    quietly land in the middle of the working day.
    """
    return JOB.get(job, _DEFAULT)


def classify(hour: int, minute: int = 0, weekday: int = 0,
             weight: str = "heavy", minutes: int = 0) -> str:
    """'ok' | 'off-window' | 'overruns' | 'avoid' for one job placement.

    weekday is Python's Mon=0..Sun=6.

      * light jobs      -> always 'ok'. Buddy: "no restriction".
      * weekends        -> always 'ok', including heavy work.
      * heavy, Mon-Fri  -> 'avoid' inside 08:00-16:00 (a real conflict with
                           local use); 'off-window' in the evening (allowed,
                           just not preferred); 'overruns' if it starts in the
                           preferred window but is still running at 08:00;
                           'ok' otherwise.
    """
    if weight == "light" or weekday not in BUSY_DAYS:
        return "ok"
    if BUSY_START_H <= hour < BUSY_END_H:
        return "avoid"                       # real conflict with local use
    if not (HEAVY_START_H <= hour < HEAVY_END_H):
        return "off-window"                  # evening: allowed, not preferred
    if hour * 60 + minute + minutes > HEAVY_END_H * 60:
        return "overruns"
    return "ok"


def is_constrained(hour: int, weekday: int, weight: str = "heavy") -> bool:
    return classify(hour, 0, weekday, weight) != "ok"


def line_share(est_mb: float, minutes: float) -> float:
    """A job's average draw as a percentage of the Verizon downlink.

    Advisory only. Buddy: "it is not a hard restriction but better planing."
    """
    if minutes <= 0:
        return 0.0
    mbps = est_mb * 8 / (minutes * 60)
    return 100.0 * mbps / LINE_DOWN_MBPS


# ── parsing the live schedule ───────────────────────────────────────────────
# Fed the raw text of the runner's `schedule-map`, which prints CIRRUS launchd
# entries as "  com.cirrus.daily  02:00" and CUMULUS timers via list-timers.
# S79: `(day N)` -- the MONTHLY form -- had no place in this pattern, so
# com.cirrus.stratusreview (05:15, day 1) and com.cirrus.rebootmonthly (08:15,
# day 1) were dropped silently, exactly like the CUMULUS Mon form. Found by the
# new unparsed-line report the moment it was switched on, which is the argument
# for that report in one line.
_CIRRUS_LINE = re.compile(
    r"^\s+(?P<job>com\.cirrus\.\S+)\s+(?P<h>\d{2}):(?P<m>\d{2})"
    r"(?:\s+\(weekday (?P<wd>\d)\))?"
    r"(?:\s+\(day (?P<md>\d+)\))?"
    r"(?P<dormant>\s+\[dormant\])?\s*$")
# CUMULUS lines are the unit's own OnCalendar, e.g.
#   "  cirrus-billsnow.timer   Mon 04:00:00"  /  "  cirrus-daily.timer  *-*-* 03:00:00"
# NOT the NEXT column of list-timers: NEXT is derived, so an interval timer
# (deadman, every 10 min) reads as a fixed daily time and gets falsely flagged.
#
# S79: the prefix group was `(\S+)` -- exactly ONE token between the unit name
# and the time. systemd writes a day-scoped calendar as TWO:
#     entity-kb-weekly-digest.timer      Mon *-*-* 05:00:00
# so that line never matched and was DROPPED WITHOUT A WORD. It is Bill's
# Monday HOA digest -- a client-facing job -- and window-audit, the tool
# START-HERE says to trust instead of eyeballing the schedule, did not know it
# existed. Anything scheduled against 05:00 would be told the slot was free.
# The prefix is now non-greedy and may hold several tokens; the weekday is
# looked up by scanning it rather than by assuming its position.
_CUMULUS_LINE = re.compile(
    r"^\s+(\S+\.timer)\s+(.+?)\s+(\d{1,2}):(\d{2})(?::\d{2})?"
    r"(\s+\[dormant\])?\s*$")
# systemd OnCalendar shorthands, which carry no HH:MM to match on.
_SHORTHAND = {"daily": (None, 0, 0), "weekly": (0, 0, 0),
              "monthly": (None, 0, 0), "yearly": (None, 0, 0),
              "annually": (None, 0, 0), "midnight": (None, 0, 0)}
_SHORTHAND_LINE = re.compile(
    r"^\s+(\S+\.timer)\s+(" + "|".join(_SHORTHAND) + r")(\s+\[dormant\])?\s*$")
_DOW = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _weekday_in(prefix: str):
    """First day-of-week token in a systemd calendar prefix, or None.

    Scans instead of indexing: `Mon 04:00:00`, `Mon *-*-* 05:00:00` and
    `*-*-* 03:00:00` all reach here and only the first two name a day.
    """
    for tok in prefix.replace(",", " ").split():
        d = _DOW.get(tok[:3].lower())
        if d is not None:
            return d
    return None


_INTERVAL_RX = re.compile(r"\(interval / not calendar-scheduled\)")


def _looks_schedulish(line: str) -> bool:
    """Does this line name a unit we were supposed to have understood?"""
    if not line.strip() or "=====" in line or _INTERVAL_RX.search(line):
        return False
    return (".timer" in line or ".plist" in line
            or line.strip().startswith("com."))


def parse_schedule(text: str, unparsed: list = None):
    """-> [{'host','job','hour','minute','weekday'}]. Interval/continuous jobs
    are skipped: a watchdog every 1800s has no window to place.

    Pass `unparsed` to receive every unit line that did NOT parse (S79) --
    see the detection note at the bottom of the loop for why that matters.
    """
    out = []
    if unparsed is None:
        unparsed = []
    host = "cirrus"
    for line in (text or "").splitlines():
        if "CUMULUS" in line and "=====" in line:
            host = "cumulus"
            continue
        if "CIRRUS" in line and "=====" in line:
            host = "cirrus"
            continue
        m = _CIRRUS_LINE.match(line)
        if m:
            wd = m.group("wd")
            md = m.group("md")
            out.append({"host": "cirrus", "job": m.group("job"),
                        "hour": int(m.group("h")), "minute": int(m.group("m")),
                        # launchd Weekday: 0/7=Sun, 1=Mon .. 6=Sat -> Python Mon=0
                        "weekday": (int(wd) - 1) % 7 if wd else None,
                        # A monthly job contends on its one day. Recording it
                        # slightly over-states contention on the other 30; that
                        # is the safe direction for a tool whose job is to say
                        # whether a slot is free.
                        "monthday": int(md) if md else None,
                        "dormant": bool(m.group("dormant"))})
            continue
        m = _CUMULUS_LINE.match(line)
        if m:
            out.append({"host": "cumulus", "job": m.group(1),
                        "hour": int(m.group(3)), "minute": int(m.group(4)),
                        "weekday": _weekday_in(m.group(2)),
                        "dormant": bool(m.group(5))})
            continue
        m = _SHORTHAND_LINE.match(line)
        if m:
            wd, hh, mm = _SHORTHAND[m.group(2).lower()]
            out.append({"host": "cumulus", "job": m.group(1),
                        "hour": hh, "minute": mm, "weekday": wd,
                        "dormant": bool(m.group(3))})
            continue
        # S79 DETECTION. Everything above this line either matched or was a
        # header/blank/interval row. Anything left names a unit and did NOT
        # parse -- which is how Bill's Monday digest went missing for a whole
        # session while the audit reported "no policy violations" and printed a
        # confident "next free heavy slot". A schedule auditor that drops rows
        # it cannot read is worse than no auditor, because the silence is
        # indistinguishable from a clean schedule. Collect them and make the
        # caller say so.
        if _looks_schedulish(line):
            unparsed.append(line.strip())
    return out


def audit(jobs):
    """Every way the live schedule disagrees with the policy.

    Returns (violations, notes). A violation is actionable; a note is context.
    """
    violations, notes = [], []
    # A plist that exists but is not loaded cannot fire. Auditing it as a
    # violation produces a finding nobody can act on, which is how a monitor
    # gets ignored. Note it instead -- but keep it visible, because a dormant
    # unit with a bad time is a landmine for whoever loads it later.
    for j in [x for x in jobs if x.get("dormant")]:
        notes.append(f"dormant  {j['host']}/{j['job']} "
                     f"{j['hour']:02d}:{j['minute']:02d} (plist present, not loaded)")
    jobs = [j for j in jobs if not j.get("dormant")]

    for j in jobs:
        weight, mins, est_mb, basis = profile(j["job"])
        j["weight"], j["mins"], j["est_mb"], j["basis"] = weight, mins, est_mb, basis
        wd = j["weekday"]
        days = [wd] if wd is not None else sorted(BUSY_DAYS)
        verdicts = {classify(j["hour"], j["minute"], d, weight, mins) for d in days}
        end = j["hour"] * 60 + j["minute"] + mins
        if "off-window" in verdicts and "avoid" not in verdicts:
            notes.append(
                f"off-window  {j['host']}/{j['job']} "
                f"{j['hour']:02d}:{j['minute']:02d} — heavy, outside the "
                f"preferred {HEAVY_START_H:02d}:00-{HEAVY_END_H:02d}:00 window "
                f"but clear of local-use hours, so allowed")
        if "avoid" in verdicts:
            violations.append(
                f"{j['host']}/{j['job']} is HEAVY ({basis}) and starts at "
                f"{j['hour']:02d}:{j['minute']:02d}, inside the Mon-Fri "
                f"{BUSY_START_H:02d}:00-{BUSY_END_H:02d}:00 local-use window")
        elif "overruns" in verdicts:
            violations.append(
                f"{j['host']}/{j['job']} starts {j['hour']:02d}:{j['minute']:02d} "
                f"but runs ~{mins} min, finishing {end//60:02d}:{end%60:02d} -- "
                f"past the {HEAVY_END_H:02d}:00 deadline")

    # Pile-ups: only HEAVY jobs contend. Three status pings in the same minute
    # are harmless, and flagging them would bury the findings that matter.
    buckets = {}
    for j in jobs:
        if j.get("weight") == "heavy":
            buckets.setdefault((j["hour"], j["minute"] // 15), []).append(
                f"{j['host']}/{j['job']}")
    for (h, q), names in sorted(buckets.items()):
        if len(names) > 2:
            violations.append(
                f"{len(names)} HEAVY jobs all start in {h:02d}:{q*15:02d}-"
                f"{h:02d}:{q*15+14:02d}: {', '.join(names)}")

    # The brief must be last.
    brief = next((j for j in jobs if j["job"].startswith(BRIEF_JOB)), None)
    if brief is None:
        notes.append(f"{BRIEF_JOB} not found in the live schedule")
    else:
        bt = brief["hour"] * 60 + brief["minute"]
        # Only same-morning producers matter; an evening job (devloop 21:30)
        # reports into the NEXT day's brief, which is correct, not a violation.
        for j in jobs:
            if j["job"] == brief["job"]:
                continue
            # S73: a REBOOT is not a producer. The rule exists so that jobs whose
            # results the brief REPORTS run before it; a reboot reports nothing,
            # and Buddy's explicit policy is that it must run AFTER the daily
            # work finishes, not before — so a bad reboot has a whole day to be
            # noticed instead of surfacing when the 03:30 chain has failed.
            # Flagging it forever would train us to ignore the rule (T9).
            if "reboot" in j["job"]:
                continue
            start = j["hour"] * 60 + j["minute"]
            if 0 <= start - bt < 240:
                violations.append(
                    f"{j['host']}/{j['job']} at {j['hour']:02d}:{j['minute']:02d} "
                    f"fires AFTER the brief at {brief['hour']:02d}:{brief['minute']:02d} "
                    f"-- {BRIEF_RULE}")
            elif start < bt and start + j.get("mins", 0) > bt:
                violations.append(
                    f"{j['host']}/{j['job']} is still running at "
                    f"{brief['hour']:02d}:{brief['minute']:02d} (starts "
                    f"{j['hour']:02d}:{j['minute']:02d}, ~{j['mins']} min) -- "
                    f"the brief would report it incomplete")

    for j in jobs:
        if j.get("weight") == "light":
            notes.append(f"light    {j['host']}/{j['job']} "
                         f"{j['hour']:02d}:{j['minute']:02d} (unrestricted)")
    return violations, notes


def budget(jobs):
    """Advisory bandwidth planning against the Verizon line.

    Buddy: "consider the estimate how much access it be using against the
    verizon line ... it is not a hard restriction but better planing."

    Reports each heavy job's estimated average draw as a share of the downlink,
    and flags 15-minute slots where two heavy jobs overlap. Note the est_mb
    figures are placeholders until the sampler has a night of data -- the
    SHAPE of this report is trustworthy now, the magnitudes are not.
    """
    lines, overlaps = [], []
    heavy = [j for j in jobs
             if not j.get("dormant") and profile(j["job"])[0] == "heavy"]
    for j in sorted(heavy, key=lambda x: x["hour"] * 60 + x["minute"]):
        weight, mins, est_mb, basis = profile(j["job"])
        share = line_share(est_mb, mins)
        end = j["hour"] * 60 + j["minute"] + mins
        # S79: an UNPROFILED job was rendered identically to a measured one.
        # _DEFAULT's own basis says "treated as heavy until profiled" -- a
        # warning that lived in the data and never reached the reader, so every
        # duration on this plan looked equally trustworthy and the overlap
        # check silently ran on a guess.
        flag = " [UNPROFILED — 15min is a guess]" if basis is _DEFAULT[3] else ""
        lines.append(
            f"  {j['hour']:02d}:{j['minute']:02d}-{end//60:02d}:{end%60:02d}  "
            f"{j['job']:<32} ~{est_mb:4}MB over {mins:3}min  "
            f"= {share:5.2f}% of the {LINE_DOWN_MBPS:.0f} Mbps line{flag}")
    # S79: this comparison was `sa < sb < sa + dur` -- STRICTLY greater. Two
    # jobs starting in the SAME minute give sa == sb, so the single likeliest
    # collision (two jobs both parked on a round hour) was the one case it
    # could not see. entity-kb-weekly-digest and halftime-catalogue have both
    # fired at 05:00 since S78 and this line printed "no two heavy jobs
    # overlap" every time. Pairs are walked once now, so a clash is reported
    # in one direction rather than two.
    for i, a in enumerate(heavy):
        for b in heavy[i + 1:]:
            sa = a["hour"] * 60 + a["minute"]
            sb = b["hour"] * 60 + b["minute"]
            first, second = (a, b) if sa <= sb else (b, a)
            lo, hi = min(sa, sb), max(sa, sb)
            if hi < lo + profile(first["job"])[1]:
                same = " (SAME START)" if sa == sb else ""
                overlaps.append(
                    f"  {first['job']} overlaps {second['job']}{same}")
    return lines, sorted(set(overlaps))


def find_window(jobs, minutes: int = 30, host: str = "cirrus",
                weight: str = "heavy"):
    """Where should a NEW recurring job go? -> (hour, minute, why).

    A LIGHT job needs no window at all -- that is the point of the weight
    split -- so this says so instead of consuming a scarce night slot.

    For a heavy job, walks 00:00-08:00 in 15-minute steps and returns the first
    slot that (a) is clear of other HEAVY jobs on the same host by `minutes`,
    and (b) still finishes before 08:00.
    """
    if weight == "light":
        return None, None, ("light jobs are unrestricted -- schedule it whenever "
                            "is convenient; do not spend a night slot on it")
    taken = sorted((j["hour"] * 60 + j["minute"]) for j in jobs
                   if j["host"] == host and not j.get("dormant")
                   and profile(j["job"])[0] == "heavy")
    for t in range(HEAVY_START_H * 60, HEAVY_END_H * 60, 15):
        if t + minutes > HEAVY_END_H * 60:
            break
        if all(abs(t - o) >= minutes for o in taken):
            return t // 60, t % 60, (
                f"clear by {min((abs(t-o) for o in taken), default=999)} min "
                f"from the nearest heavy job on {host}; finishes "
                f"{(t+minutes)//60:02d}:{(t+minutes)%60:02d}")
    return None, None, (
        f"no {minutes}-min slot fits inside {HEAVY_START_H:02d}:00-"
        f"{HEAVY_END_H:02d}:00 on {host} -- move it to the other box, run it "
        f"on a weekend, or make it cheaper. Do NOT place it in working hours")


def selftest() -> bool:
    bad = 0

    def ck(label, got, want):
        nonlocal bad
        ok = got == want
        print(("  ok   " if ok else "  FAIL ") + f"{label}: {got!r}" +
              ("" if ok else f" (want {want!r})"))
        bad += 0 if ok else 1

    # --- the weight split, which IS the policy ---
    ck("heavy Wed 09:00 avoid", classify(9, 0, 2, "heavy"), "avoid")
    ck("LIGHT Wed 09:00 ok (no restriction)", classify(9, 0, 2, "light"), "ok")
    ck("LIGHT Wed 13:00 ok", classify(13, 0, 2, "light"), "ok")
    ck("heavy Wed 00:30 ok (after midnight)", classify(0, 30, 2, "heavy"), "ok")
    ck("heavy Wed 02:00 ok", classify(2, 0, 2, "heavy"), "ok")
    ck("heavy Wed 07:59 ok if instant", classify(7, 59, 2, "heavy", 0), "ok")
    # "try to complete before 8am" -- starting inside the window is not enough.
    ck("heavy 07:30 + 45min overruns", classify(7, 30, 2, "heavy", 45), "overruns")
    ck("heavy 06:00 + 45min ok", classify(6, 0, 2, "heavy", 45), "ok")
    # Weekends carry no restriction at all, heavy included.
    ck("heavy SAT 09:00 ok", classify(9, 0, 5, "heavy"), "ok")
    ck("heavy SUN 13:00 ok", classify(13, 0, 6, "heavy"), "ok")

    # An unprofiled job must default to HEAVY, so a new job gets noticed
    # rather than silently slipping into the working day as "light".
    ck("unknown job defaults heavy", profile("com.cirrus.brandnew")[0], "heavy")
    ck("known light job", profile("cirrus-deadman.timer")[0], "light")

    sched = """===== CIRRUS (launchd) =====
  com.cirrus.daily                   02:00
  com.cirrus.morningbrief            07:30
  com.cirrus.jobscheck               16:30
  com.cirrus.devloop                 09:00
  com.cirrus.billsnow                08:06  (weekday 1)  [dormant]
"""
    jobs = parse_schedule(sched)
    ck("parsed 5 jobs", len(jobs), 5)
    ck("dormant flag read", jobs[4]["dormant"], True)
    v, n = audit(jobs)
    # jobscheck is LIGHT at 16:30 -- under the old time-only policy this was
    # fine by luck; under the new one it must be fine BY RULE, and a light job
    # at 13:00 must be fine too. That distinction is the whole refinement.
    ck("light jobscheck 16:30 not flagged", any("jobscheck" in x for x in v), False)
    ck("heavy devloop 09:00 flagged", any("devloop" in x for x in v), True)
    # 21:30 is outside local-use hours: allowed, noted, NOT a violation.
    ev = parse_schedule("===== CIRRUS (launchd) =====\n"
                        "  com.cirrus.devloop                 21:30\n")
    ev_v, ev_n = audit(ev)
    ck("evening heavy job NOT a violation", ev_v, [])
    ck("evening heavy job IS noted",
       any("off-window" in x for x in ev_n), True)
    ck("evening classify", classify(21, 30, 2, "heavy", 15), "off-window")
    ck("dormant NOT a violation", any("billsnow" in x for x in v), False)
    ck("dormant IS reported", any("dormant" in x and "billsnow" in x for x in n), True)
    ck("daily 02:00 clean", any("com.cirrus.daily" in x for x in v), False)

    # A heavy job still running when the brief fires is a real defect: the
    # brief reports it incomplete while looking perfectly healthy.
    late = parse_schedule("===== CIRRUS (launchd) =====\n"
                          "  com.cirrus.daily                   07:20\n"
                          "  com.cirrus.morningbrief            07:30\n")
    lv, _ = audit(late)
    ck("job overlapping the brief flagged",
       any("still running at" in x for x in lv), True)

    # CUMULUS timers come through as OnCalendar lines, a different shape.
    cum = parse_schedule(
        "===== CUMULUS (systemd timers) =====\n"
        "  cirrus-hoaleads.timer              *-*-* 03:00:00\n"
        "  cirrus-billsnow.timer              Mon 04:00:00\n"
        "  cirrus-deadman.timer               (interval / not calendar-scheduled)\n"
        "  cirrus-daily.timer                 *-*-* 07:00:00  [dormant]\n")
    ck("cumulus rows parsed (interval skipped)", len(cum), 3)
    # A disabled systemd timer cannot fire; it must not read as a live job.
    ck("disabled cumulus timer marked dormant", cum[2]["dormant"], True)
    ck("enabled cumulus timer not dormant", cum[0].get("dormant"), False)
    ck("cumulus job name", cum[0]["job"], "cirrus-hoaleads.timer")
    ck("cumulus hour", cum[0]["hour"], 3)
    ck("cumulus Mon parsed to weekday 0", cum[1]["weekday"], 0)
    ck("interval timer produces no row",
       any("deadman" in c["job"] for c in cum), False)

    # S79: the day-scoped OnCalendar form. This is the exact line that was
    # silently dropped -- Bill's Monday HOA digest, client-facing.
    up = []
    day = parse_schedule(
        "===== CUMULUS (systemd timers) =====\n"
        "  entity-kb-weekly-digest.timer      Mon *-*-* 05:00:00\n"
        "  halftime-catalogue.timer           *-*-* 05:00:00\n", up)
    ck("Mon *-*-* HH:MM:SS parses at all", len(day), 2)
    ck("...to the right weekday", day[0]["weekday"], 0)
    ck("...and the right hour", day[0]["hour"], 5)
    ck("plain *-*-* still has no weekday", day[1]["weekday"], None)
    ck("a readable schedule reports nothing unparsed", up, [])

    # OnCalendar shorthand carries no HH:MM.
    sh = parse_schedule("===== CUMULUS (systemd timers) =====\n"
                        "  cloudflared-update.timer           daily  [dormant]\n")
    ck("daily shorthand parses to midnight", (sh[0]["hour"], sh[0]["minute"]), (0, 0))
    ck("...and keeps its dormant mark", sh[0]["dormant"], True)

    # THE DETECTION, tested by planting a failure. A green check on a file
    # where nothing is wrong proves only that nothing is wrong.
    bad_up = []
    parse_schedule("===== CUMULUS (systemd timers) =====\n"
                   "  wat.timer   every second tuesday when it rains\n", bad_up)
    ck("an unreadable unit line is REPORTED, not dropped", len(bad_up), 1)
    ck("...and quotes the line", "wat.timer" in bad_up[0], True)
    quiet = []
    parse_schedule("===== CUMULUS (systemd timers) =====\n"
                   "  cirrus-deadman.timer   (interval / not calendar-scheduled)\n",
                   quiet)
    ck("an interval row is not mistaken for unreadable", quiet, [])

    # S79: the CIRRUS monthly form, the other silently-dropped shape.
    mup = []
    mon = parse_schedule("===== CIRRUS (launchd) =====\n"
                         "  com.cirrus.stratusreview           05:15  (day 1)\n", mup)
    ck("(day N) monthly form parses", len(mon), 1)
    ck("...keeps its time", (mon[0]["hour"], mon[0]["minute"]), (5, 15))
    ck("...records the month day", mon[0]["monthday"], 1)
    ck("...and is not reported unreadable", mup, [])

    # S79: two heavy jobs starting in the SAME minute must be reported.
    # Planted deliberately -- this is the case the old strict `<` missed.
    same = [{"host": "cumulus", "job": "entity-kb-weekly-digest.timer",
             "hour": 5, "minute": 0, "weekday": 0, "dormant": False},
            {"host": "cumulus", "job": "halftime-catalogue.timer",
             "hour": 5, "minute": 0, "weekday": None, "dormant": False}]
    _, ov = budget(same)
    ck("simultaneous heavy jobs ARE flagged", len(ov), 1)
    ck("...and named as a same-start clash", "SAME START" in ov[0], True)
    # A pair is walked once, not twice.
    stagger = [dict(same[0]), dict(same[1])]
    stagger[1]["minute"] = 5
    _, ov2 = budget(stagger)
    ck("a staggered clash is reported once, not twice", len(ov2), 1)
    apart = [dict(same[0]), dict(same[1])]
    apart[1]["hour"] = 9
    _, ov3 = budget(apart)
    ck("jobs far apart do not clash", ov3, [])

    # find_window
    h, m, _ = find_window(jobs, minutes=30, host="cirrus")
    ck("new heavy job lands in the window",
       classify(h, m, 2, "heavy", 30), "ok")
    lh, lm, why = find_window(jobs, host="cirrus", weight="light")
    ck("light job told it needs no window", (lh, lm), (None, None))
    ck("...and told why", "unrestricted" in why, True)
    # A job too long to finish by 08:00 must be refused, not squeezed in.
    _, _, why2 = find_window([], minutes=9 * 60, host="cirrus")
    ck("over-long job refused", "no " in why2 and "slot fits" in why2, True)

    # Advisory budget maths.
    ck("line share of 250MB/19min",
       round(line_share(250, 19), 2), round(100 * (250 * 8 / (19 * 60)) / 880.0, 2))
    ck("zero-duration job draws nothing", line_share(10, 0), 0.0)

    print()
    print("all runtime_window selftests passed" if not bad else f"{bad} FAILED")

    # S79 — the routing sweep is the longest job on either box, and duration is
    # what the overlap check runs on.
    ck("the routing sweep is profiled, not left on the 15-minute default",
       profile("halftime-routing.timer")[1], 150)
    ck("the catalogue is profiled too",
       profile("halftime-catalogue.timer")[1], 15)
    ck("a job nobody profiled still defaults to HEAVY",
       profile("some-brand-new.timer")[0], "heavy")
    _plan, _ = budget([
        {"host": "cumulus", "job": "some-brand-new.timer", "hour": 23,
         "minute": 0, "dormant": False}])
    ck("an UNPROFILED job is marked as such on the plan",
       "[UNPROFILED" in "".join(_plan), True)
    _plan2, _ = budget([
        {"host": "cumulus", "job": "halftime-routing.timer", "hour": 22,
         "minute": 0, "dormant": False}])
    ck("a profiled job carries no warning",
       "[UNPROFILED" in "".join(_plan2), False)
    ck("the sweep's real duration reaches the plan line",
       "150min" in "".join(_plan2), True)

    return bad == 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(0 if selftest() else 1)
    text = sys.stdin.read()
    unparsed = []
    jobs = parse_schedule(text, unparsed)
    print(f"parsed {len(jobs)} scheduled job(s)\n")
    if unparsed:
        print(f"=== {len(unparsed)} SCHEDULE LINE(S) THIS AUDIT COULD NOT READ ===")
        print("  Every verdict below is computed WITHOUT them. Do not treat a")
        print("  free slot as free until these parse.")
        for u in unparsed:
            print("  ?? " + u)
        print()
    viol, notes = audit(jobs)
    if viol:
        print(f"=== {len(viol)} POLICY VIOLATION(S) ===")
        for v in viol:
            print("  !! " + v)
    else:
        print("=== no policy violations ===")

    blines, overlaps = budget(jobs)
    if blines:
        print(f"\n=== bandwidth plan (ADVISORY; est_mb are placeholders until "
              f"the sampler has a night of data) ===")
        for b in blines:
            print(b)
        if overlaps:
            print("  -- overlapping heavy jobs (attribution will be ambiguous) --")
            for o in overlaps:
                print(o)
        else:
            print("  no two heavy jobs overlap — sampler windows stay attributable")

    if "--verbose" in sys.argv:
        print()
        for n in notes:
            print("  " + n)
    h, m, why = find_window(jobs)
    print(f"\nnext free heavy slot on cirrus: {h:02d}:{m:02d}  ({why})"
          if h is not None else f"\n{why}")
    raise SystemExit(1 if viol else 0)
