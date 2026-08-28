#!/usr/bin/env python3
"""Backoff policy for Telegram API calls — S84.

WHY THIS EXISTS
---------------
S83 found two cirrus_bot processes long-polling the same token since
2026-08-24. Telegram 409-Conflicted both of them, and the pair wrote ~268,000
"HTTP Error 409: Conflict" lines into a 21 MB bot.log. The duplicate process
was the trigger; the reason one duplicate became a quarter-million log lines
is that THE ERROR PATH HAD NO BACKOFF.

The mechanism, exactly:

    result = api_call("getUpdates", {"offset": offset, "timeout": 30})

`api_call` catches its own exceptions and returns `{}` (or the parsed HTTP
error body), so the poll loop's `except Exception: time.sleep(5)` NEVER FIRES
for an API error. `{}.get("result", [])` is `[]`, the for-loop body is skipped,
and the loop immediately polls again. A healthy quiet poll blocks ~30s inside
Telegram's long-poll; a 409 comes back instantly. So the failure mode is not
"slower" — it is a hot spin, measured at 12,400 lines in one minute.

S83's carry-over reframed the fix: backoff belongs INSIDE api_call, not in the
outer handler that cannot see these errors.

WHY IT IS ITS OWN MODULE
------------------------
`cirrus_bot.py` deliberately has NO selftest and refuses every argument
(S83) — running it starts the LIVE bot, and a second instance 409-Conflicts
the real one. Giving it an importable selftest would re-open exactly the door
S83 shut. So the policy lives here, where it can be tested for real, and
cirrus_bot.py keeps its guard.

This module is PURE POLICY: it computes a delay and never sleeps, never opens
a socket, never touches a file (T32). The caller does the sleeping.

    python3 api_backoff.py --selftest
"""

DEFAULT_BASE = 1.0     # first failure waits this long
DEFAULT_CAP = 60.0     # exponential ceiling: a real outage recovers within a minute
RETRY_AFTER_CAP = 300.0  # ceiling on a server-supplied wait; see Backoff.failure()
_MAX_SHIFT = 32          # exponent clamp; 2**32 s is ~136 years, far past any cap


def retry_after_from(payload):
    """Pull Telegram's flood-control wait out of an error body.

    Telegram answers a rate-limited call with
        {"ok": false, "error_code": 429, "parameters": {"retry_after": 17}}
    Returns the number of seconds as a float, or None when the payload carries
    no usable value. Anything malformed is None rather than an exception --
    this parses a REMOTE box's output, and a crash here would take down the
    poll loop that the backoff exists to protect.
    """
    if not isinstance(payload, dict):
        return None
    params = payload.get("parameters")
    if not isinstance(params, dict):
        return None
    raw = params.get("retry_after")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0 or value != value:  # negative, or NaN
        return None
    return value


class Backoff:
    """Exponential backoff with reset-on-success.

    Schedule with the defaults: 1, 2, 4, 8, 16, 32, 60, 60, ... seconds.
    At the measured 409 rate that turns ~12,400 log lines a minute into one a
    minute -- and, more to the point, stops hammering Telegram.
    """

    def __init__(self, base=DEFAULT_BASE, cap=DEFAULT_CAP,
                 retry_after_cap=RETRY_AFTER_CAP):
        self.base = float(base)
        self.cap = float(cap)
        self.retry_after_cap = float(retry_after_cap)
        self.failures = 0

    def success(self):
        """A call came back clean. Forget the streak."""
        self.failures = 0

    def failure(self, retry_after=None):
        """Record one failed call; return the seconds to wait before the next.

        `retry_after` is honoured when Telegram supplies one, because ignoring
        flood control is how a rate limit becomes a ban. It is still CAPPED:
        the value comes from a remote server, and an unbounded sleep would let
        one bad response wedge the bot for hours. The cap is deliberate and
        tested, not an accident of arithmetic.
        """
        self.failures += 1
        if retry_after is not None:
            return min(float(retry_after), self.retry_after_cap)
        # The exponent is CLAMPED, and this is not cosmetic. `failures` keeps
        # counting (the log line reports it, and a streak of 4,000 is worth
        # seeing), but 2**n as a float overflows at n≈1024:
        #
        #     1.0 * 2 ** 1024  ->  OverflowError: int too large to convert to float
        #
        # At the 60s cap that arrives after ~17 hours of unbroken failure. The
        # S83 incident ran for FOUR DAYS, so this is comfortably reachable, and
        # the raised OverflowError would escape api_call into run_bot's generic
        # handler -- silently dropping the bot back to the flat 5s loop this
        # module exists to replace. Everything past _MAX_SHIFT is already
        # thousands of times the cap, so clamping changes no real delay.
        shift = min(self.failures - 1, _MAX_SHIFT)
        return min(self.cap, self.base * (2 ** shift))


def selftest():
    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    # --- the schedule itself -------------------------------------------------
    b = Backoff()
    seq = [b.failure() for _ in range(9)]
    ok("schedule is exponential from the base",
       seq[:6] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0])
    ok("schedule saturates at the cap and stays there",
       seq[6:] == [60.0, 60.0, 60.0])
    ok("never exceeds the cap", max(seq) <= 60.0)

    # The whole point: the FIRST failure must already cost real time. A policy
    # that returns 0 on the first error is the hot spin with extra steps.
    ok("first failure already waits", seq[0] >= 1.0)

    # --- reset on success ----------------------------------------------------
    b.success()
    ok("success resets the streak", b.failures == 0)
    ok("after reset the next failure is back to base", b.failure() == 1.0)

    # A success in the MIDDLE of a streak must reset, not merely pause: this is
    # what keeps the bot responsive once a transient error clears.
    b2 = Backoff()
    b2.failure(); b2.failure(); b2.failure()
    b2.success()
    ok("mid-streak success resets fully", b2.failure() == 1.0)

    # --- retry_after is honoured and bounded ---------------------------------
    b3 = Backoff()
    ok("retry_after overrides the exponential value", b3.failure(retry_after=17) == 17.0)
    ok("retry_after still advances the failure count", b3.failures == 1)
    ok("retry_after is capped", Backoff().failure(retry_after=99999) == 300.0)
    ok("retry_after of 0 is honoured, not treated as absent",
       Backoff().failure(retry_after=0) == 0.0)

    # --- parsing a real Telegram 429 -----------------------------------------
    ok("parses a real 429 body",
       retry_after_from({"ok": False, "error_code": 429,
                         "parameters": {"retry_after": 17}}) == 17.0)
    ok("a 409 body has no retry_after",
       retry_after_from({"ok": False, "error_code": 409,
                         "description": "Conflict"}) is None)
    ok("empty dict is None", retry_after_from({}) is None)
    ok("non-dict is None", retry_after_from(None) is None)
    ok("non-dict parameters is None",
       retry_after_from({"parameters": "soon"}) is None)
    ok("garbage retry_after is None, not a crash",
       retry_after_from({"parameters": {"retry_after": "later"}}) is None)
    ok("negative retry_after is rejected",
       retry_after_from({"parameters": {"retry_after": -5}}) is None)
    # True is an int in Python; sleeping 1s because a flag said so is a bug.
    ok("boolean retry_after is rejected",
       retry_after_from({"parameters": {"retry_after": True}}) is None)
    ok("string digits are accepted",
       retry_after_from({"parameters": {"retry_after": "30"}}) == 30.0)

    # --- the incident, replayed ----------------------------------------------
    # 409s arrived instantly with no wait between them. Under this policy the
    # same streak costs real seconds almost immediately.
    b4 = Backoff()
    total = sum(b4.failure() for _ in range(10))
    ok("ten straight 409s now cost >60s of waiting, not zero", total > 60)

    # --- a LONG outage must not blow up ------------------------------------
    # 2**n as a float overflows at n~1024, which at the 60s cap is ~17h of
    # unbroken failure -- and the incident this module exists for ran four
    # days. An OverflowError here would escape api_call and drop the bot back
    # to the flat 5s loop, i.e. undo the fix at exactly the worst moment.
    b5 = Backoff()
    b5.failures = 5000
    try:
        long_delay = b5.failure()
        ok("a 5,000-failure streak still returns the cap", long_delay == 60.0)
    except OverflowError:
        ok("a 5,000-failure streak still returns the cap", False)
    b6 = Backoff()
    b6.failures = 10 ** 6
    try:
        ok("even an absurd streak is arithmetic-safe", b6.failure() == 60.0)
    except OverflowError:
        ok("even an absurd streak is arithmetic-safe", False)
    ok("the streak counter itself keeps counting (the log line needs it)",
       b6.failures == 10 ** 6 + 1)

    failed = [n for n, good in checks if not good]
    for name, good in checks:
        print("  %s %s" % ("PASS" if good else "FAIL", name))
    print("%d/%d checks passed" % (len(checks) - len(failed), len(checks)))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        raise SystemExit(selftest())
    print(__doc__)
