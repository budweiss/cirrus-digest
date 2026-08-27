#!/usr/bin/env python3
"""Does the INSTALLED sudo grant match the code's restart allowlist? (S81)

WHY A SEPARATE SCRIPT. tools.selftest() checks this too, but it runs as
`cumulus-supervisor` from /opt, and that account can read neither
/etc/sudoers.d/cumulus-supervisor (0440 root) nor the repo copy under
/home/buddy (0750). So on the box that matters it printed SKIP -- honest, but
a skip is not a verification, and the whole point of T44 is that "could not
check" must not be where the story ends.

This runs as `buddy`, who can sudo, and is handed the INSTALLED file on stdin.
It compares against the allowlist as WRITTEN IN tools.py, parsed with `ast`
rather than imported -- so it needs none of tools.py's dependencies and cannot
be fooled by an import-time side effect.

  sudo cat /etc/sudoers.d/cumulus-supervisor | python3 verify_grant.py [tools.py]

Exit 0 = the two agree. Exit 1 = drift. Exit 2 = could not compare (NOT a pass).

Drift is silent in the dangerous direction: restart_service() checks its own
ALLOWED_UNITS, so a unit present in code but missing from the grant passes that
check and is then refused by sudo -- at the moment a service is down and being
recovered, which is the worst possible time to discover it.
"""
import ast
import re
import sys
from pathlib import Path

VERBS = ("restart", "reset-failed")


def allowlist_from_source(path: Path) -> set:
    """ALLOWED_UNITS as literally assigned in tools.py, without importing it."""
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "ALLOWED_UNITS":
                return set(ast.literal_eval(node.value))
    raise LookupError(f"no ALLOWED_UNITS assignment in {path}")


def granted(sudoers_text: str, verb: str) -> set:
    return set(re.findall(
        r"/usr/bin/systemctl\s+" + re.escape(verb) + r"\s+(\S+?)[,\s\\]",
        sudoers_text))


def compare(sudoers_text: str, allowed: set):
    problems = []
    for verb in VERBS:
        g = granted(sudoers_text, verb)
        missing = sorted(allowed - g)
        extra = sorted(g - allowed)
        if missing:
            problems.append(f"{verb}: in code but NOT granted -> {missing} "
                            f"(restart_service would allow it, sudo would refuse)")
        if extra:
            problems.append(f"{verb}: granted but NOT in code -> {extra} "
                            f"(authority nothing in code intends)")
    return problems


def selftest() -> bool:
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    good = ("Cmnd_Alias X = \\\n"
            "    /usr/bin/systemctl restart a.service, \\\n"
            "    /usr/bin/systemctl restart b.service\n"
            "Cmnd_Alias Y = \\\n"
            "    /usr/bin/systemctl reset-failed a.service, \\\n"
            "    /usr/bin/systemctl reset-failed b.service\n")
    ck("a matching pair reports no problem",
       compare(good, {"a.service", "b.service"}) == [])
    ck("the LAST entry on a line is parsed (no trailing comma)",
       "b.service" in granted(good, "restart"))

    p = compare(good, {"a.service", "b.service", "c.service"})
    ck("a unit in code but not granted is caught", any("c.service" in x for x in p))
    ck("...and is named as the dangerous direction",
       any("sudo would refuse" in x for x in p))

    p = compare(good, {"a.service"})
    ck("a grant beyond the code allowlist is caught", any("b.service" in x for x in p))

    ck("drift in only ONE verb is still caught",
       compare(good.replace("reset-failed b.service", "reset-failed z.service"),
               {"a.service", "b.service"}) != [])
    ck("empty input is not mistaken for agreement",
       compare("", {"a.service"}) != [])

    src = Path(__file__).resolve().parent / "tools.py"
    if src.exists():
        al = allowlist_from_source(src)
        ck("ALLOWED_UNITS parses out of tools.py without importing it", len(al) > 5)
        ck("...and includes the units Buddy approved 2026-08-27",
           {"cirrus-offer.service", "cloudflared.service"} <= al)

    bad = 0
    for name, ok in checks:
        print(("  ok   " if ok else "  FAIL ") + name)
        bad += 0 if ok else 1
    print()
    print("all verify_grant selftests passed" if not bad else f"{bad} FAILED")
    return bad == 0


def main() -> int:
    if "--selftest" in sys.argv:
        return 0 if selftest() else 1
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    src = Path(args[0]) if args else Path(__file__).resolve().parent / "tools.py"
    text = sys.stdin.read()
    if not text.strip():
        print("VERIFY-GRANT: no sudoers content on stdin — NOT VERIFIED "
              "(this is a gap, not a pass).")
        return 2
    try:
        allowed = allowlist_from_source(src)
    except Exception as e:
        print(f"VERIFY-GRANT: could not read the allowlist from {src}: {e} "
              f"— NOT VERIFIED.")
        return 2
    problems = compare(text, allowed)
    if problems:
        print(f"VERIFY-GRANT: DRIFT — code allowlist ({len(allowed)} units) and the "
              f"INSTALLED sudo grant disagree:")
        for p in problems:
            print("  !! " + p)
        return 1
    print(f"VERIFY-GRANT: OK — the installed grant matches ALLOWED_UNITS exactly "
          f"({len(allowed)} units, both verbs).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
