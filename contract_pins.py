"""
contract_pins.py  (S560, 2026-11-03)
===============================================================================
Which enabled model routes pin a file? An approval names the exact files it
was qualified on (`contract_files`), so changing one shuts that route until a
session requalifies it. Dev-loop builds were offered for `ship N` without
saying so: on 11-03 a test-only build to inbox_evidence.py would have shut the
answer workflow on both boxes, and only a by-hand check caught it.

Read-only, stdlib only, and it never raises: an unreadable config gives no
note, it does not block a listing.
"""
import glob
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def pinning(files, root=ROOT):
    """{file: [route, ...]} for each of `files` that an enabled route pins."""
    want, out = set(files or ()), {}
    for name in sorted(glob.glob(str(Path(root) / "config" / "*.json"))):
        if any(s in Path(name).name for s in ("credentials", "cookies")):
            continue
        try:
            stack = [(json.load(open(name)), Path(name).stem)]
        except (OSError, ValueError):
            continue
        while stack:
            node, label = stack.pop()
            if isinstance(node, dict):
                pins = node.get("contract_files")
                if isinstance(pins, dict) and node.get("enabled") is True:
                    for f in sorted(want & set(pins)):
                        out.setdefault(f, []).append(label)
                stack += [(v, k if isinstance(v, dict) and "contract_files" in v else label)
                          for k, v in node.items()]
            elif isinstance(node, list):
                stack += [(v, label) for v in node]
    return out


def note(files, root=ROOT):
    """One warning line for a build touching pinned files, or ''."""
    try:
        pins = pinning(files, root)
    except Exception:
        return ""
    if not pins:
        return ""
    routes = sorted({r for rs in pins.values() for r in rs})
    return (f"⚠️ {', '.join(sorted(pins))} is pinned by {', '.join(routes)}: "
            f"shipping shuts that route until a session requalifies it")


def selftest():
    import tempfile
    fail = 0

    def ck(label, cond):
        nonlocal fail
        fail += not cond
        print(("PASS " if cond else "FAIL ") + label)

    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config"
        cfg.mkdir()
        (cfg / "answer-workflow-capabilities.json").write_text(json.dumps(
            {"enabled": True, "contract_files": {"inbox_evidence.py": "x", "answer_workflow.py": "y"}}))
        (cfg / "model_capabilities.json").write_text(json.dumps({"projects": {
            "foundation:dev-agent-repair": {"enabled": True, "contract_files": {"dev_agent.py": "z"}},
            "foundation:old": {"enabled": False, "contract_files": {"dev_loop.py": "z"}}}}))
        (cfg / "credentials.json").write_text(json.dumps(
            {"enabled": True, "contract_files": {"stall_check.py": "s"}}))
        (cfg / "broken.json").write_text("{not json")
        ck("a top-level route is named after its file",
           pinning(["inbox_evidence.py"], td) == {"inbox_evidence.py": ["answer-workflow-capabilities"]})
        ck("a nested route is named by its key",
           pinning(["dev_agent.py"], td) == {"dev_agent.py": ["foundation:dev-agent-repair"]})
        ck("a disabled route does not pin", pinning(["dev_loop.py"], td) == {})
        ck("credential files are never read", pinning(["stall_check.py"], td) == {})
        ck("an unpinned build has no note", note(["failure_summary.py"], td) == "")
        line = note(["inbox_evidence.py", "failure_summary.py"], td)
        ck("a pinned build's note names the file and route",
           "inbox_evidence.py" in line and "answer-workflow-capabilities" in line
           and "failure_summary" not in line)
    print("ok" if not fail else f"{fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    import sys
    sys.exit(selftest() if sys.argv[1:] == ["--selftest"] else
             "usage: contract_pins.py --selftest")
