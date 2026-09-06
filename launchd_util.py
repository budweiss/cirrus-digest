#!/usr/bin/env python3
"""Where a launchd job lives, and the argv that restarts it — S84.

TWO FUNCTIONS, ONE COPY. Both of these existed in THREE files
(`cirrus_api.py`, `cirrus_watchdog.py`, `dev_agent.py`) as independent
copy-pasted definitions. That duplication is not incidental to the bug this
module fixes — it IS the bug:

  * S71 taught the restart path to ask launchctl which DOMAIN holds a job,
    instead of hardcoding `gui/<uid>/<label>`, so converted LaunchDaemons
    resolve to `system/<label>`. Correct, and copied to all three files.
  * Nobody taught any of them that reaching the `system` domain needs ROOT.
    So every one of the three ran an unprivileged `launchctl kickstart` and
    got back:

        Could not kickstart service "com.cirrus.bot": 1: Operation not permitted

The consequences differed by caller, and all three were silent:

  * `cirrus_api` — a deploy pulled the code and left the OLD process running.
    S83 shipped an argv guard to cirrus_bot.py; a day later the live bot was
    still the previous process, because the file on disk was right and every
    check reads the file.
  * `cirrus_watchdog` — the self-healing restart could not heal ANY daemon.
  * `dev_agent` — worse: the AUTO-ROLLBACK path. A build that failed verify
    reverted the commit and then could not restart, so the box kept running
    the code that had just failed.

One definition now, so the next correction lands everywhere at once.
See docs/TOOLING-TRAPS.md T51.

    python3 launchd_util.py --selftest
"""

import os
import subprocess


def launchctl_target(label):
    """Which domain actually holds this job — `system` or a GUI session?

    S71. Every call site used to hardcode gui/<uid>/<label>, which was right
    while every com.cirrus.* job was a user LaunchAgent. Two things break that
    as jobs convert to system LaunchDaemons: a daemon is in the `system`
    domain, so gui/<uid>/<label> no longer resolves; and after a reboot with
    nobody logged in, gui/<uid> DOES NOT EXIST AT ALL -- which is the whole
    reason for converting. So ask, rather than assume. Falls back to the GUI
    domain, leaving every not-yet-converted agent working exactly as before.
    """
    try:
        if subprocess.run(["launchctl", "print", "system/%s" % label],
                          capture_output=True, timeout=10).returncode == 0:
            return "system/%s" % label
    except Exception:
        pass
    return "gui/%d/%s" % (os.getuid(), label)


def kickstart_cmd(target):
    """The argv that actually restarts `target` — sudo-ed when it has to be.

    GUI-domain agents are deliberately left alone: they run as this user, and
    root has no session in gui/<uid>, so sudo would break the working case.

    `-n` matters. Without it a missing NOPASSWD grant BLOCKS on a password
    prompt that no unattended deploy, watchdog or dev-loop can answer, turning
    a clear error into a hang.

    Safe to sudo: every caller matches the label against an exact-match
    allowlist before reaching here (T11), and argv stays a LIST, so no shell is
    involved.
    """
    if target.startswith("system/"):
        return ["sudo", "-n", "launchctl", "kickstart", "-k", target]
    return ["launchctl", "kickstart", "-k", target]


def state_is_running(text):
    """Does `launchctl print` output describe a RUNNING service?

    Split out from is_running() so the parsing is testable without a launchd.
    """
    return any(l.strip() == "state = running" for l in (text or "").splitlines())


def is_running(label, _run=None):
    """Is this job running, asked in the DOMAIN THAT ACTUALLY HOLDS IT — S103.

    `dev_agent.ship()` step 5 verified a restart with a bare `launchctl list`
    and a substring test. dev_agent runs over ssh, and every com.cirrus.* job
    is now a /Library/LaunchDaemons system daemon, so that list does not
    contain them AT ALL:

        $ ssh cirrus 'launchctl list | grep -c com.cirrus.bot'
        0
        $ ssh cirrus 'launchctl print system/com.cirrus.bot'
            state = running
            pid = 13628

    So verification could only ever FAIL for the two services ship() is able to
    restart (cirrus_bot.py, cirrus_api.py). The build deployed, restarted
    cleanly, was declared dead and auto-rolled back. `prop-2026-08-29-562734`
    sat stranded five days on that. This is T33 — the trap is documented, the
    fix was written down, and the code still asked the wrong domain.

    `launchctl print` needs no root for a read (verified on CIRRUS), unlike the
    kickstart above it. Only ever used for long-running services: a periodic
    StartInterval job is legitimately `not running` between passes (T9), and
    reading that as failure would be the same mistake pointing the other way.
    """
    runner = _run or (lambda argv: subprocess.run(
        argv, capture_output=True, text=True, timeout=10).stdout)
    try:
        return state_is_running(runner(["launchctl", "print",
                                        launchctl_target(label)]))
    except Exception:
        return False


def selftest():
    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    cases = [
        ("system/com.cirrus.bot", True, "a LaunchDaemon needs root — the broken case"),
        ("system/com.cirrus.api", True, "same, for the API itself"),
        ("gui/501/com.ollama.serve", False, "a user agent must NOT be sudo-ed"),
        ("gui/0/com.cirrus.daily", False, "any gui/ target stays unprivileged"),
    ]
    for target, want_sudo, why in cases:
        cmd = kickstart_cmd(target)
        ok("%s -> %s  (%s)" % (target, "sudo" if want_sudo else "plain", why),
           (cmd[0] == "sudo") == want_sudo and cmd[-1] == target and "kickstart" in cmd)

    sys_cmd = kickstart_cmd("system/com.cirrus.bot")
    ok("sudo is non-interactive (-n), so a missing grant errors instead of hanging",
       "-n" in sys_cmd)
    ok("argv is a list, so no shell is involved",
       isinstance(sys_cmd, list) and all(isinstance(a, str) for a in sys_cmd))
    ok("-k is present (restart, not just start)", "-k" in sys_cmd)

    # launchctl_target must never invent a domain it did not verify. On a box
    # with no such job the answer is the GUI fallback, never system/.
    t = launchctl_target("com.cirrus.definitely-not-a-real-job-s84")
    ok("an unknown label falls back to gui/, never system/",
       t.startswith("gui/") and t.endswith("com.cirrus.definitely-not-a-real-job-s84"))
    ok("the fallback carries THIS user's uid", t.split("/")[1] == str(os.getuid()))

    # S103 — is_running() must be able to say NO. A verifier that always
    # answers "running" would have hidden the very bug it was written for.
    _RUNNING = "\tstate = running\n\tpid = 13628\n"
    _STOPPED = "\tstate = not running\n"
    _ABSENT = 'Could not find service "x" in domain for system\n'
    ok("running output reads as running", state_is_running(_RUNNING))
    ok("...a STOPPED service does not — the inverse, and the whole point",
       not state_is_running(_STOPPED))
    ok("...nor does a service launchd cannot find at all",
       not state_is_running(_ABSENT))
    ok("...nor does empty output, which is what a failed call returns",
       not state_is_running("") and not state_is_running(None))
    ok("a stopped service still contains the WORD 'running' — which is why "
       "this matches the whole line and not a substring",
       "running" in _STOPPED and not state_is_running(_STOPPED))
    ok("is_running() reports True through an injected runner",
       is_running("com.cirrus.bot", _run=lambda argv: _RUNNING))
    ok("...False when that runner says stopped",
       not is_running("com.cirrus.bot", _run=lambda argv: _STOPPED))
    ok("...and False, never a crash, when the call raises",
       not is_running("com.cirrus.bot",
                      _run=lambda argv: (_ for _ in ()).throw(OSError("boom"))))
    _seen = []
    is_running("com.cirrus.bot", _run=lambda argv: _seen.append(argv) or _RUNNING)
    ok("it asks `launchctl print`, NOT `launchctl list` (the T33 bug)",
       _seen and _seen[0][:2] == ["launchctl", "print"] and "list" not in _seen[0])

    failed = [n for n, g in checks if not g]
    for n, g in checks:
        print("  %s %s" % ("PASS" if g else "FAIL", n))
    print("%d/%d checks passed" % (len(checks) - len(failed), len(checks)))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        raise SystemExit(selftest())
    print(__doc__)
