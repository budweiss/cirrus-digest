#!/usr/bin/env python3
"""trap_selfstop.py — T20 detector for runner/trap_lint.sh.

A launchd job that boots ITSELF out kills the process running the script, so
every line after the bootout is dead code. On 2026-08-22 CIRRUS's one-time
reboot did exactly that: it logged its pre-state, ran
`launchctl bootout system/com.cirrus.rebootonce`, and never reached the `rm` or
the `shutdown` on the following lines. The box stayed up, the plist stayed on
disk, and nothing anywhere reported a failure.

Lives in its own file rather than inline in trap_lint.sh because a heredoc
inside a heredoc is its own trap (T13 territory).

Prints one `relpath:line:explanation` per hit; silent and exit 0 when clean.
"""
import os
import re
import sys

root = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/Documents/Cowork")
roots = [os.path.join(root, "cirrus-repo"), os.path.join(root, "runner")]

files = []
for r in roots:
    for dirpath, _dirnames, filenames in os.walk(r):
        if os.sep + ".git" in dirpath:
            continue
        for f in filenames:
            if f.endswith((".sh", ".plist", ".py", ".service")):
                files.append(os.path.join(dirpath, f))

# label -> {script basenames that plist binds it to}
bindings = {}
# scripts bound to a plist that supplies NO HOME
no_home = set()
for path in files:
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        continue
    # A plist generated from a heredoc often points at "$SRC"; resolve simple
    # VAR=/path/to/x.sh assignments in the same file so the binding is found.
    # One file can assign the SAME variable several times (run-command.sh sets
    # SRC for tm_unlock.sh AND for scheduled_reboot_once.sh), so keep every
    # assignment WITH ITS POSITION and use the nearest one ABOVE the plist.
    # A plain dict kept only the last, which silently bound the label to the
    # wrong script and made this whole check pass on the very bug it was
    # written for.
    assigns = [(m.start(), m.group(1), m.group(2))
               for m in re.finditer(r'^\s*(\w+)=["\']?([^"\'\s]+\.sh)', text, re.M)]
    for block_m in re.finditer(r"<plist.*?</plist>", text, re.S):
        block = block_m.group(0)
        m = re.search(r"<key>Label</key>\s*<string>([^<]+)</string>", block)
        if not m:
            continue
        label = m.group(1).strip()
        # S73/T24: does this plist give its job a HOME? launchd supplies none,
        # and bash does NOT invent one, so `$HOME` under `set -u` is an instant
        # abort. Some of our plists set it (com.cirrus.tunnel), some do not
        # (com.cirrus.rebootonce) — that inconsistency is the whole trap.
        envblock = re.search(r"<key>EnvironmentVariables</key>\s*<dict>(.*?)</dict>",
                             block, re.S)
        has_home = bool(envblock and re.search(r"<key>HOME</key>", envblock.group(1)))
        # ...and only a SYSTEM DAEMON lacks HOME. A gui LaunchAgent gets one:
        # runner/sync_cookies.sh runs `set -euo pipefail`, uses $HOME, and its
        # log is written every morning — it could not survive an unset HOME.
        # The first version of this check did not distinguish, and reported
        # three healthy com.cowork.* AGENTS as bugs. A lint that cries wolf gets
        # ignored (T9), so the distinction is made from the install target that
        # appears just above the plist in the generating file.
        head = text[max(0, block_m.start() - 400):block_m.start()]
        is_daemon = ("/Library/LaunchDaemons" in head
                     or "/Library/LaunchDaemons" in path)
        for val in re.findall(r"<string>([^<]*)</string>", block):
            val = val.strip()
            var = re.fullmatch(r"\$\{?(\w+)\}?", val)
            if var:
                above = [v for pos, name, v in assigns
                         if name == var.group(1) and pos < block_m.start()]
                val = above[-1] if above else ""
            if val.endswith(".sh"):
                bindings.setdefault(label, set()).add(os.path.basename(val))
                if not has_home and is_daemon:
                    no_home.add(os.path.basename(val))

STOP = re.compile(r"launchctl\s+(?:bootout|unload|kickstart\s+-k)\b[^\n]*?([\w.]*\.[\w.]+)")

found = 0
for path in sorted(files):
    if not path.endswith(".sh"):
        continue
    base = os.path.basename(path)
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        continue
    for n, line in enumerate(lines, 1):
        if line.lstrip().startswith("#"):
            continue
        m = STOP.search(line)
        if not m:
            continue
        label = m.group(1).split("/")[-1]
        if base in bindings.get(label, ()):
            found = 1
            rel = os.path.relpath(path, root)
            print(
                f"T20:{rel}:{n}:this script runs AS {label}, so this stops ITSELF — "
                "every line after it is dead code. Remove the plist instead, or "
                "have a different process do the bootout."
            )

# ── T24: login-session env inside a script a daemon runs ────────────────────
HOMEREF = re.compile(r"(\$HOME|\$\{HOME|(?<![\w/.])~/)")
for path in sorted(files):
    if not path.endswith(".sh") or os.path.basename(path) not in no_home:
        continue
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        continue
    for n, line in enumerate(lines, 1):
        if line.lstrip().startswith("#") or not HOMEREF.search(line):
            continue
        found = 1
        rel = os.path.relpath(path, root)
        print(f"T24:{rel}:{n}:a LaunchDaemon runs this script and its plist sets no "
              "HOME — launchd supplies none and bash does not invent one, so under "
              "`set -u` this aborts the whole script. Derive from \"$0\" instead.")
        break   # one per file: the fix is the same everywhere in it

sys.exit(0)  # reporting is trap_lint.sh's job
