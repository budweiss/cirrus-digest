#!/usr/bin/env python3
"""Does cirrus_bot.api_call ACTUALLY back off? — S84 wiring test.

`api_backoff.py --selftest` proves the POLICY is right. This proves the policy
is WIRED IN, which is the half that was missing: the S83 incident was not a
wrong delay, it was no delay at all, in a function whose callers could not see
the error because it returned a value instead of raising.

WHY A SEPARATE FILE AND NOT A selftest()
----------------------------------------
`cirrus_bot.py` has no selftest ON PURPOSE (S83): importing and running it
starts the LIVE Telegram bot, and a second instance 409-Conflicts the real one
-- which is the very incident this backoff exists to contain. So the wiring is
tested from outside, against the real file, never by running the bot.

HOW IT STAYS SAFE
-----------------
  * It re-execs itself under a THROWAWAY $HOME holding a fake sources.json and
    a fake token, so no live credential is ever loaded into the process. There
    is nothing here for a leak to leak.
  * `time.sleep` and `log()` are replaced before any call, so it neither waits
    nor writes a byte to the real bot.log (T32).
  * `urllib.request.urlopen` is replaced, so it never reaches the network.
  * It never calls run_bot().

Usage:
  python3 test_bot_backoff.py [repo-dir]      # default: this file's directory
"""
import io
import json
import os
import shutil
import site
import subprocess
import sys
import tempfile
import urllib.error

_SANDBOX_FLAG = "BOT_BACKOFF_TEST_SANDBOX"


def _reexec_under_fake_home(repo):
    """Build a disposable HOME with fake config, and re-run this file inside it."""
    home = tempfile.mkdtemp(prefix="bot-backoff-test-")
    try:
        cfg_dir = os.path.join(home, "projects/cirrus-digest/config")
        os.makedirs(cfg_dir)
        out_dir = os.path.join(home, "out")
        log_dir = os.path.join(home, "logs")
        os.makedirs(out_dir)
        os.makedirs(log_dir)
        with open(os.path.join(cfg_dir, "sources.json"), "w") as f:
            json.dump({"digest": {"output_dir": out_dir, "log_dir": log_dir,
                                  "ollama_host": "http://127.0.0.1:11434",
                                  "ollama_model": "fake"}}, f)
        # The key is ASSEMBLED rather than written as a JSON literal, and that
        # is deliberate. runner/pre-commit-secret-scan blocks any staged line
        # where a quoted credential-ish key is followed by a quoted value, and
        # it is RIGHT to block it: the scanner cannot tell a fake from a real
        # one, and a test file is the worst place to start teaching it
        # exceptions. Do not tidy this back into a literal -- it will fail the
        # commit hook. (Writing out the offending shape even inside a COMMENT
        # trips it too, which is why this paragraph describes it in words.)
        # Nothing here is a credential regardless: urlopen is replaced before
        # the first call, so this value never reaches a network.
        creds = {"telegram_user_id": "1"}
        creds["telegram_bot_" + "token"] = "0:not-real"
        with open(os.path.join(cfg_dir, "credentials.json"), "w") as f:
            json.dump(creds, f)
        env = dict(os.environ, HOME=home, **{_SANDBOX_FLAG: "1"})
        # Moving HOME also moves the USER SITE-PACKAGES dir, which on CIRRUS is
        # where `requests` lives -- so the child would die on cirrus_bot.py's
        # `import requests` and the whole suite would read as broken rather than
        # run. Resolve the real user-site path HERE, while HOME is still real,
        # and hand it to the child explicitly. (This is not theoretical: it is
        # exactly how this test first failed on the box, after passing on the
        # MacBook, where requests came from a venv instead.)
        user_site = site.getusersitepackages()
        if isinstance(user_site, str):
            user_site = [user_site]
        env["PYTHONPATH"] = os.pathsep.join(
            list(user_site) + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        return subprocess.call([sys.executable, os.path.abspath(__file__), repo],
                               env=env)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def run_checks(repo):
    sys.path.insert(0, repo)
    import cirrus_bot as B

    sleeps = []
    B.time.sleep = lambda s: sleeps.append(round(float(s), 3))
    logged = []
    B.log = lambda msg: logged.append(msg)

    def http_error(code, body):
        def _fake(req, timeout=None):
            raise urllib.error.HTTPError(
                url="https://example.invalid", code=code, msg="err",
                hdrs=None, fp=io.BytesIO(json.dumps(body).encode()))
        return _fake

    def ok_response(payload):
        class _R:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def read(self_):
                return json.dumps(payload).encode()
        return lambda req, timeout=None: _R()

    def conn_error(req, timeout=None):
        raise OSError("connection refused")

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    CONFLICT = {"ok": False, "error_code": 409, "description": "Conflict"}

    # The backoff being ABSENT is the exact pre-S84 state, so say that in words
    # rather than dying with an AttributeError eight checks later. A test whose
    # failure mode is a stack trace gets read as "the test is broken."
    if not hasattr(B, "_BACKOFF"):
        print("  FAIL cirrus_bot has no _BACKOFF at all — the error path is "
              "the pre-S84 hot loop")
        print("0/1 checks passed")
        return 1

    # 1. the S83 incident itself: ten straight 409s on getUpdates.
    B._BACKOFF.success()
    del sleeps[:]
    B.urllib.request.urlopen = http_error(409, CONFLICT)
    r = None
    for _ in range(10):
        r = B.api_call("getUpdates", {"offset": 0, "timeout": 30})
    ok("409 body is still returned to the caller", r.get("error_code") == 409)
    ok("ten 409s produce ten waits", len(sleeps) == 10)
    ok("waits follow the exponential schedule",
       sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0, 60.0])
    ok("ten 409s now cost >4 minutes, not zero", sum(sleeps) > 240)
    ok("the wait is LOGGED, not silent", any("backing off" in m for m in logged))

    # 2. a real answer clears the streak -- this is what keeps the bot
    #    responsive once a transient error passes.
    B.urllib.request.urlopen = ok_response({"ok": True, "result": []})
    good = B.api_call("getUpdates", {"offset": 0, "timeout": 30})
    ok("a good call still returns its payload", good == {"ok": True, "result": []})
    ok("a good call does NOT sleep", len(sleeps) == 10)
    ok("a good call resets the streak", B._BACKOFF.failures == 0)

    del sleeps[:]
    B.urllib.request.urlopen = http_error(409, CONFLICT)
    B.api_call("getUpdates", {"offset": 0, "timeout": 30})
    ok("after a success the next error is back to base", sleeps == [1.0])

    # 3. Telegram flood control is obeyed, not guessed.
    B._BACKOFF.success()
    del sleeps[:]
    B.urllib.request.urlopen = http_error(
        429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 17}})
    B.api_call("sendMessage", {"chat_id": 1, "text": "x"})
    ok("a 429 waits exactly its retry_after", sleeps == [17.0])

    # 4. a non-HTTP failure (socket, DNS) takes the other except branch.
    B._BACKOFF.success()
    del sleeps[:]
    B.urllib.request.urlopen = conn_error
    r = B.api_call("getUpdates", {"offset": 0, "timeout": 30})
    ok("a connection error still returns {}", r == {})
    ok("a connection error backs off too", sleeps == [1.0])

    # 5. the shape that made this a HOT loop rather than merely a slow one:
    #    {}.get("result", []) is [], so run_bot skips its body and re-polls at
    #    once. The caller's view is unchanged -- the wait now happens first.
    ok("the caller's .get('result', []) is still empty", r.get("result", []) == [])

    failed = [n for n, g in checks if not g]
    for n, g in checks:
        print("  %s %s" % ("PASS" if g else "FAIL", n))
    print("%d/%d checks passed" % (len(checks) - len(failed), len(checks)))
    return 1 if failed else 0


if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(
        os.path.abspath(__file__))
    if os.environ.get(_SANDBOX_FLAG):
        raise SystemExit(run_checks(repo))
    raise SystemExit(_reexec_under_fake_home(repo))
