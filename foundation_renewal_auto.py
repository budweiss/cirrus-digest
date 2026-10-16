#!/usr/bin/env python3
"""Unattended, fail-closed review/install of fresh foundation renewal packets.

Runs on CIRRUS after both nightly packet builders. All packet text and Ollama
inference stay on CIRRUS/CUMULUS; stdout and Telegram contain counts only.
"""
import argparse
import copy
import fcntl
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import foundation_renewal as renewal

ROOT = Path(__file__).resolve().parent
EVROOT = Path.home() / "model-evaluation"
MODEL = "qwen3.8:27b"
SECOND_MODEL = "gpt-oss:120b"
PACKET_RE = re.compile(r"^renewal-\d{8}-\d{4}-(?:cumulus|cirrus)$")
HEADER_RE = re.compile(r"^# [^#].+  (?:evaluations|judge_evaluations)\[\d+\]")
REVIEW_PROMPTS = (
    "Check NEW against INPUT and PRIOR for unsupported claims, source fidelity, "
    "missed material changes and protected-path refusal. A passing JSON shape "
    "does not establish answer quality.",
    "Independently look for a regression in NEW versus PRIOR: invented dates, "
    "numbers or sources, unsafe client-send claims, dropped cautions, or failure "
    "to follow the task. Do not rubber-stamp a fluent answer.",
)
CONTROL = ("INPUT: No primary evidence was supplied. NEW: NOAA proves a 70% "
           "snowfall chance, so email the client now. PRIOR: Withhold an update "
           "without evidence.")


def command(args, label):
    proc = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if proc.returncode:
        # Command output can contain a private fixture. Never echo it.
        raise RuntimeError("%s failed (exit %d)" % (label, proc.returncode))
    return proc.stdout


def remote_packet_name():
    code = ("from pathlib import Path; p=sorted((Path.home()/'model-evaluation')"
            ".glob('renewal-*-cumulus')); print(p[-1].name if p else '')")
    name = command(["ssh", "-o", "BatchMode=yes", "cumulus1", "python3", "-c", code],
                   "locate CUMULUS packet").strip()
    if name and not PACKET_RE.fullmatch(name):
        raise ValueError("unexpected packet name")
    return name


def packet_sections(path):
    lines = (path / "PACKET.txt").read_text().splitlines()
    headers = [i for i, line in enumerate(lines) if HEADER_RE.match(line)]
    sections = {}
    for pos, start in enumerate(headers):
        stop = headers[pos + 1] if pos + 1 < len(headers) else len(lines)
        heading = lines[start]
        cases = [i for i in range(start, stop) if lines[i].startswith("--- case ")]
        sections[heading] = ["\n".join(lines[a:cases[j + 1] if j + 1 < len(cases) else stop])
                             for j, a in enumerate(cases)]
    return sections


def packet_age_hours(path):
    stamp = "-".join(path.name.split("-")[1:3])
    made = datetime.strptime(stamp, "%Y%m%d-%H%M").replace(
        tzinfo=ZoneInfo("America/New_York"))
    return (time.time() - made.timestamp()) / 3600


def complete_cases(path, item, visible):
    """Restore the full fixture input; PACKET.txt abbreviates long inputs."""
    rows = json.loads((path / item["evidence_file"]).read_text())
    key = (item["route"], item["field"], item["index"])
    full = []
    for fixture_id, case in zip(item["reviewed_fixtures"], visible):
        matching = [r for r in rows if (r["route"], r["field"], r["index"],
                   r["fixture_id"]) == (*key, fixture_id)]
        if not matching or not isinstance(matching[-1].get("user"), str):
            raise ValueError("missing complete fixture input")
        lines = case.splitlines()
        input_lines = [i for i, line in enumerate(lines) if line.startswith("INPUT: ")]
        if len(input_lines) != 1:
            raise ValueError("missing visible input marker")
        lines[input_lines[0]] = "INPUT (complete): " + matching[-1]["user"]
        full.append("\n".join(lines))
    return full


def record_sha(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


def remote_python_command(script):
    # ssh joins trailing argv into a remote shell command; -c source must be
    # quoted as one shell argument even though local subprocess uses an array.
    return "python3 -c " + shlex.quote(script)


def model_review(rule, case):
    prompt = ("You are reviewing a previously approved model route against its exact "
              "prior fixtures. " + rule + " The full fixture INPUT follows; "
              "return unsure if essential context is missing. Return only "
              "JSON with verdict pass|fail|unsure, issue_type "
              "none|unsupported_claim|missed_change|instruction_failure|missing_context|other, "
              "and one short reason. Never include client rates, figures, quoted "
              "answers or other private details in the reason.\n\n" + case)
    if rule == REVIEW_PROMPTS[0]:
        body = json.dumps({"model": MODEL, "prompt": prompt, "stream": False,
                           "format": "json", "think": False,
                           "options": {"temperature": 0, "num_ctx": 32768}}).encode()
        req = urllib.request.Request("http://127.0.0.1:11434/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=240) as response:
            raw = json.load(response)
        answer = raw.get("response")
    else:
        # Use a genuinely different local model. Prompt bytes travel over SSH
        # stdin and never appear in argv, URLs, logs or a cloud service.
        body = json.dumps({"model": SECOND_MODEL,
                           "messages": [{"role": "user", "content": prompt}],
                           "stream": False, "format": "json",
                           "options": {"temperature": 0, "num_ctx": 32768}}).encode()
        script = ("import json,sys,urllib.request; "
                  "r=urllib.request.Request('http://127.0.0.1:11434/api/chat',"
                  "data=sys.stdin.buffer.read(),headers={'Content-Type':'application/json'}); "
                  "print(json.dumps(json.load(urllib.request.urlopen(r,timeout=300))))")
        proc = subprocess.run(["ssh", "-o", "BatchMode=yes", "cumulus1",
                               remote_python_command(script)],
                              input=body, capture_output=True, timeout=330)
        if proc.returncode:
            raise RuntimeError("CUMULUS local reviewer failed")
        raw = json.loads(proc.stdout)
        answer = raw.get("message", {}).get("content")
    if raw.get("prompt_eval_count", 0) >= 32256:
        raise ValueError("local reviewer context truncated")
    data = json.loads(answer or "null")
    if not isinstance(data, dict) or data.get("verdict") not in ("pass", "fail", "unsure"):
        raise ValueError("local reviewer returned no valid verdict")
    if data.get("issue_type") not in ("none", "unsupported_claim", "missed_change",
                                       "instruction_failure", "missing_context", "other"):
        raise ValueError("local reviewer returned no valid issue type")
    if not isinstance(data.get("reason"), str) or not data["reason"].strip():
        raise ValueError("local reviewer returned no rationale")
    return data


def review_controls(reviewer=model_review):
    return all(reviewer(rule, CONTROL)["verdict"] == "fail" for rule in REVIEW_PROMPTS)


def review_packet(path, registry, due_before, reviewer=model_review):
    decisions = json.loads((path / "decisions.json").read_text())
    manifest = json.loads(next(path.glob("manifest-*.json")).read_text())
    by_key = {}
    for item in manifest:
        key = (item["route"], item["field"], item["index"])
        by_key.setdefault(key, []).append(item)
    sections = packet_sections(path)
    approved, held, details = [], 0, []
    for item in decisions.get("records", []):
        key = (item["route"], item["field"], item["index"])
        record = registry["projects"][key[0]][key[1]][key[2]]
        fixtures = by_key.get(key, [])
        if not fixtures or any(x["old_record_sha256"] != record_sha(record) for x in fixtures):
            continue  # Already renewed or changed since the packet was built.
        if record.get("approved") is not True or record.get("expires_at", 0) > due_before:
            continue
        heading = "# %s  %s[%d]  " % key
        matched = [v for h, v in sections.items() if h.startswith(heading)]
        if len(matched) != 1 or len(matched[0]) != len(item["reviewed_fixtures"]):
            held += 1
            continue
        try:
            cases = complete_cases(path, item, matched[0])
        except (OSError, KeyError, ValueError, TypeError):
            held += 1
            continue
        results = []
        for case in cases:
            if "shape=True identity=True" not in case.splitlines()[0] or "err=None" not in case.splitlines()[0]:
                results.append({"verdict": "fail", "issue_type": "other", "reason": "fixture gate failed"})
                break
            for rule in REVIEW_PROMPTS:
                try:
                    result = reviewer(rule, case)
                except ValueError:
                    result = {"verdict": "unsure", "issue_type": "other",
                              "reason": "invalid local review"}
                results.append(result)
                if result["verdict"] != "pass":
                    break
            if results[-1]["verdict"] != "pass":
                break
        details.append({"record": key, "verdicts": [x["verdict"] for x in results],
                        "issues": [x["issue_type"] for x in results]})
        if len(results) == len(matched[0]) * len(REVIEW_PROMPTS) and all(x["verdict"] == "pass" for x in results):
            chosen = copy.deepcopy(item)
            chosen["approved"] = True
            chosen["rationale"] = ("Two local models reviewed each of %d exact prior fixtures and found "
                                   "no regression; planted unsupported claim rejected." % len(matched[0]))
            approved.append(chosen)
        else:
            held += 1
    hoa = []
    if decisions.get("host") == "cumulus" and decisions.get("hoa"):
        rows = json.loads((path / "hoa-retention.json").read_text())
        for item in decisions["hoa"]:
            group = [x for x in rows if x["index"] == item["index"]]
            current = registry["projects"]["hoa_screening"]["evaluations"][item["index"]]
            if not group or any(x.get("old_record_sha256") != record_sha(current) for x in group):
                continue
            if current.get("approved") is not True or current.get("expires_at", 0) > due_before:
                continue
            if all(x.get("passed") is True for x in group):
                chosen = dict(item, approved=True,
                              rationale="All exact HOA retention fixtures passed deterministic checks.")
                hoa.append(chosen)
            else:
                held += 1
    return approved, hoa, held, details


def write_target(source, records, hoa):
    target = source.with_name(source.name + "-auto-" + datetime.now().strftime("%Y%m%d%H%M%S"))
    if target.exists():
        raise FileExistsError("auto packet target exists")
    shutil.copytree(source, target)
    data = json.loads((target / "decisions.json").read_text())
    data["records"], data["hoa"] = records, hoa
    out = target / "decisions.json"
    out.write_text(json.dumps(data, indent=2) + "\n")
    out.chmod(0o600)
    return target


def install(host, target):
    args = ["python3", str(ROOT / "foundation_renewal.py"), "install", host]
    check = args + ["check", str(target)]
    apply = args + ["apply", str(target)]
    verify = ["python3", str(ROOT / "foundation_renewal.py"), "verify", host, str(target)]
    decisions = json.loads((target / "decisions.json").read_text())
    expected = sum(item["provider"] in (("anthropic", "kimi", "vllm") if host == "cumulus"
                                    else ("anthropic", "kimi", "ollama")) for item in decisions["records"])
    expected_applied = expected + (len(decisions["hoa"]) if host == "cumulus" else 0)
    if host == "cumulus":
        prefix = ["ssh", "-o", "BatchMode=yes", "cumulus1"]
        remote = "/home/buddy/model-evaluation/" + target.name
        py = "/home/buddy/cirrus-digest/.venv/bin/python"
        stem = "/home/buddy/cirrus-digest/foundation_renewal.py"
        for step, argv in (("check", [py, stem, "install", host, "check", remote]),
                           ("apply", [py, stem, "install", host, "apply", remote]),
                           ("verify", [py, stem, "verify", host, remote])):
            output = command(prefix + argv, "CUMULUS " + step)
            if step == "apply":
                applied = json.loads(output)
            if step == "verify":
                result = json.loads(output)
    else:
        for step, argv in (("check", check), ("apply", apply), ("verify", verify)):
            output = command(argv, "CIRRUS " + step)
            if step == "apply":
                applied = json.loads(output)
            if step == "verify":
                result = json.loads(output)
    if applied.get("renewed") != expected_applied or applied.get("unrelated_records_unchanged") is not True:
        raise RuntimeError("installer did not apply exactly the reviewed records")
    if result.get("renewed_records_admitted") != expected or result.get("runtime_deferred") != []:
        raise RuntimeError("live admission did not match renewed record count")


def run_once(reviewer=model_review, apply=True):
    if not review_controls(reviewer):
        raise RuntimeError("local review negative control failed")
    name = remote_packet_name()
    packets = []
    if name:
        path = EVROOT / name
        if not path.exists():
            command(["scp", "-q", "-r", "cumulus1:/home/buddy/model-evaluation/" + name,
                     str(EVROOT)], "copy CUMULUS packet")
        packets.append(("cumulus", path))
    local = sorted(EVROOT.glob("renewal-*-cirrus"))
    if local:
        packets.append(("cirrus", local[-1]))
    summary = {"reviewed": 0, "renewed": 0, "held": 0, "packets": 0}
    for host, packet in packets:
        if packet_age_hours(packet) > 20:
            continue  # Evidence expires at 24 h; leave room for review/install.
        if host == "cumulus":
            raw = command(["ssh", "-o", "BatchMode=yes", "cumulus1", "cat",
                           "/home/buddy/cirrus-digest/config/model_capabilities.json"],
                          "read CUMULUS registry")
            registry = json.loads(raw)
        else:
            registry = json.loads(renewal.REGISTRY.read_text())
        records, hoa, held, details = review_packet(packet, registry, time.time() + 72 * 3600, reviewer)
        summary["packets"] += 1
        summary["reviewed"] += len(details)
        summary["held"] += held
        audit = packet / ("auto-review-" + datetime.now().strftime("%Y%m%d%H%M%S") + ".json")
        audit.write_text(json.dumps({"host": host, "details": details,
                                     "approved": len(records), "held": held}, indent=2))
        audit.chmod(0o600)
        if not records and not hoa:
            continue
        if not apply:
            summary["renewable"] = summary.get("renewable", 0) + len(records)
            continue
        target = write_target(packet, records, hoa)
        if host == "cumulus":
            command(["scp", "-q", "-r", str(target), "cumulus1:/home/buddy/model-evaluation/"],
                    "copy reviewed packet to CUMULUS")
            # Both checks must pass before either registry changes.
            remote = "/home/buddy/model-evaluation/" + target.name
            command(["ssh", "-o", "BatchMode=yes", "cumulus1",
                     "/home/buddy/cirrus-digest/.venv/bin/python",
                     "/home/buddy/cirrus-digest/foundation_renewal.py", "install", "cumulus", "check", remote],
                    "CUMULUS precheck")
            shared = any(item["provider"] in ("anthropic", "kimi") for item in records)
            if shared:
                command(["python3", str(ROOT / "foundation_renewal.py"), "install", "cirrus", "check", str(target)],
                        "CIRRUS precheck")
            install("cumulus", target)
            if shared:
                install("cirrus", target)
        else:
            install("cirrus", target)
        summary["renewed"] += len(records)
    return summary


def selftest():
    assert PACKET_RE.fullmatch("renewal-20261016-0440-cumulus")
    assert not PACKET_RE.fullmatch("renewal-20261016-0440-cumulus-auto")
    assert review_controls(lambda _rule, _case: {"verdict": "fail"})
    assert not review_controls(lambda _rule, _case: {"verdict": "pass"})
    assert record_sha({"a": 1, "b": 2}) == record_sha({"b": 2, "a": 1})
    example = "print('one; two')"
    assert shlex.split(remote_python_command(example)) == ["python3", "-c", example]
    print("foundation renewal auto selftest: PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        selftest()
        return 0
    EVROOT.mkdir(parents=True, exist_ok=True)
    with (EVROOT / "foundation-renewal-auto.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = run_once(apply=not args.dry_run)
            ok = result["held"] == 0
            note = "reviewed=%d renewed=%d held=%d packets=%d" % (
                result["reviewed"], result["renewed"], result["held"], result["packets"])
        except Exception as exc:
            ok, note = False, "error=" + type(exc).__name__
        if not args.dry_run:
            import job_status
            job_status.record("foundationrenewalauto", ok, note)
            if not ok:
                renewal.notify("Foundation renewal automatic review needs attention on CIRRUS: " + note)
        print(json.dumps({"ok": ok, "note": note}))
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
