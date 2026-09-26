#!/usr/bin/env python3
"""foundation_renewal.py -- the automated half of the weekly approval renewal (S316).

Buddy, S316: "automate the renewal". Reviewed foundation approvals are valid for
7 days (his choice, S315). The RUN half now runs itself: recover each approved
record's exact prior fixtures, re-run them, pre-check the gates, and lay every
fresh answer beside the previously approved one. The REVIEW does not: approval
stays an explicit written decision by a session (the S279/S287 design), and the
installer refuses any record left undecided.

Daily on each box (CUMULUS 04:40 systemd timer, CIRRUS 04:50 LaunchDaemon):
  * finds the earliest expiry among the LIVE approvals this box is responsible
    for -- enabled route, approved record, and for a local slot only the model
    it serves now. Cloud records are tested only on CUMULUS: they are identical
    on both boxes (S315 verified 47/47) and CUMULUS cannot ssh to CIRRUS;
  * more than 48 h away: records "next renewal due ..." and exits;
  * within 48 h: runs the tests into ~/model-evaluation/renewal-<stamp>/, writes
    PACKET.txt and decisions.json (every record approved=null), and Telegrams
    Buddy the deadline;
  * a packet under 20 h old for the same expiry: one reminder, no re-run (the
    installer accepts evidence under 24 h old, so a stale packet is re-run);
  * past expiry: says so in the alert and marks the job unhealthy.

  python3 foundation_renewal.py                    # the daily check
  python3 foundation_renewal.py --force            # run the tests now
  python3 foundation_renewal.py --plan             # what a run would do; no model calls
  python3 foundation_renewal.py install HOST check|apply DIR [DIR ...]
  python3 foundation_renewal.py verify  HOST DIR [DIR ...]
  python3 foundation_renewal.py selftest

Ported from docs/evaluations/s287-renewal and s315-renewal (the reviewed
scripts); every installer guard is kept.
"""
import copy
import hashlib
import inspect
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REGISTRY = HERE / "config" / "model_capabilities.json"
CREDS = HERE / "config" / "credentials.json"
EVROOT = Path.home() / "model-evaluation"
STATE = EVROOT / "renewal-state.json"
WINDOW_H = 48        # start the run half this long before the earliest expiry
STALE_H = 20         # re-run a packet older than this (installer accepts < 24 h)
VALIDITY_S = 7 * 86400
LOCAL = ("ollama", "vllm")
CLOUD = ("anthropic", "kimi")
PY = sys.executable
# evidence written before this harness recorded max_tokens (S313's STRATUS run);
# the value is the caller's real one (stratus_monthly: max_tokens=4000)
MAX_TOKENS_DEFAULT = {"stratus:monthly": 4000}
sha = lambda b: hashlib.sha256(b).hexdigest()


def on_cumulus():
    return socket.gethostname() == "cumulus1"


def providers_here():
    return CLOUD + ("vllm",) if on_cumulus() else ("ollama",)


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


# ── what is live, and when does it expire ─────────────────────────────────────
def live_records(projects, providers, served):
    """(route, field, index, record) for every approval this box must renew."""
    for key, route in projects.items():
        if not isinstance(route, dict) or route.get("enabled") is not True:
            continue
        for field in ("evaluations", "judge_evaluations"):
            for i, e in enumerate(route.get(field, [])):
                if e.get("approved") is not True or e.get("id") not in providers:
                    continue
                if e["id"] in LOCAL and served.get(e["id"]) != e.get("model"):
                    continue          # a model this slot no longer serves lapses (S315)
                yield key, field, i, e


def earliest_expiry(projects, providers, served):
    ex = [e.get("expires_at", 0) for *_, e in live_records(projects, providers, served)]
    return min(ex) if ex else None


def decide(now, earliest, state):
    """wait / remind / run / none. Pure, so the selftest can pin it."""
    if earliest is None:
        return "none"
    if earliest - now > WINDOW_H * 3600:
        return "wait"
    last = state.get("last_run_at")
    if last and now - last < STALE_H * 3600 and state.get("last_run_for") == earliest:
        return "remind"
    return "run"


def served_models(creds, providers):
    import capability_health as H
    out = {}
    for p in providers:
        if p in LOCAL:
            try:
                out[p] = H.observe(creds, p).get("model")
            except Exception:
                out[p] = None
    return out


# ── the run half ──────────────────────────────────────────────────────────────
def evidence_index(root=EVROOT):
    """sha -> path for every JSON under root, and fixture_id -> body for rows
    that carry their own text (S290-style evidence stores ids only)."""
    index, bodies = {}, {}
    for p in root.rglob("*.json"):
        try:
            raw = p.read_bytes()
            rows = json.loads(raw)
        except (OSError, ValueError):
            continue
        index.setdefault(sha(raw), p)
        for r in rows if isinstance(rows, list) else []:
            g = _body(r)
            if g:
                bodies[sha(json.dumps(g, sort_keys=True).encode())] = g
    return index, bodies


def _body(r):
    """The fixture a row was run on, or None."""
    if not isinstance(r, dict) or not {"case", "system", "user"} <= set(r):
        return None
    mt = r.get("max_tokens") or MAX_TOKENS_DEFAULT.get(r.get("task", ""))
    return {"case": r["case"], "system": r["system"], "user": r["user"], "max_tokens": mt} if mt else None


def build_manifest(projects, providers, served, index, bodies):
    out, missing = [], []
    for task, field, i, e in live_records(projects, providers, served):
        if not task.startswith("foundation:"):
            continue                          # hoa_screening: its own deterministic check
        group = []
        for name, h in (e.get("evidence_files") or {}).items():
            p = index.get(h)
            if not p:
                missing.append((task, field, e["id"], name)); continue
            for r in load(p, []):
                if not isinstance(r, dict):
                    continue
                if "system" not in r and r.get("fixture_id") in bodies and r.get("route") == task:
                    if r.get("prompt_sha256") == e["prompt_sha256"]:
                        group.append(bodies[r["fixture_id"]])
                    continue
                if r.get("provider") != e["id"] or sha(r.get("system", "").encode()) != e["prompt_sha256"]:
                    continue
                if r.get("task", task[11:]) != task[11:] or r.get("error_type"):
                    continue
                g = _body(dict(r, task=task[11:]))
                if g:
                    group.append(g)
        unique = {sha(json.dumps(g, sort_keys=True).encode()): g for g in group}
        if not unique:
            missing.append((task, field, e["id"], "no fixture rows")); continue
        for key, g in unique.items():
            out.append(dict(g, fixture_id=key, task=task[11:], route=task, field=field, index=i,
                            provider=e["id"], expected_model=e["model"],
                            old_record_sha256=sha(json.dumps(e, sort_keys=True).encode())))
    return out, missing


def run_provider(manifest_path, provider, out_path, session):
    """Fresh inference evidence only (port of S287/S315 run_checks.py). No approval writes."""
    import capability_registry as C, capability_health as H, llm_providers as L, llm_budget as B
    import foundation_contracts as F
    creds = json.loads(CREDS.read_text())
    registry = json.loads(REGISTRY.read_text())
    cfg, box, ledger = B.resolve(creds, str(HERE))
    assert cfg and ledger
    cases = [x for x in json.loads(Path(manifest_path).read_text()) if x["provider"] == provider]
    out = Path(out_path)
    rows = json.loads(out.read_text()) if out.exists() else []
    done = {(x["route"], x["field"], x["index"], x["fixture_id"]) for x in rows
            if x.get("shape_valid") and x.get("identity_valid") and x.get("finish") not in ("length", "max_tokens")}
    for case in cases:
        if (case["route"], case["field"], case["index"], case["fixture_id"]) in done:
            continue
        route = registry["projects"][case["route"]]
        record = route[case["field"]][case["index"]]
        assert sha(json.dumps(record, sort_keys=True).encode()) == case["old_record_sha256"]
        assert record["prompt_sha256"] == sha(case["system"].encode())
        assert C.contract_digest(route, HERE) == record["contract_sha256"]
        c = dict(creds, **route.get("provider_options", {}))
        bud = dict(c.get("llm_budget") or {})
        # the ROUTE's per-call cap is a production limit; a qualification must still
        # run its long fixture (S287 and S315 both hit this)
        bud.update(pricing_path=str(HERE / "config/llm_pricing.json"), ledger_path=ledger,
                   per_session_usd=min(10. if provider == "anthropic" else 3., cfg["caps_usd"]["per_session"]),
                   per_call_usd=min(1., cfg["caps_usd"]["per_call"]))
        c["llm_budget"] = bud
        private = provider in LOCAL
        if private:
            c["llm_privacy"] = "LOCAL_ONLY"
        row = dict(case, started_at=time.time())
        try:
            raw = L.call(provider, case["system"], case["user"], c, max_tokens=case["max_tokens"], retries=0,
                         task=case["task"], session_id=session, privacy="LOCAL_ONLY" if private else None,
                         strict_accounting=True)
            health = H.observe(c, provider) if private else H.observe_cloud(c, provider)
            row.update(response=raw, model=L.last_model(), finish=L.last_finish_reason(), usage=L._LAST.usage,
                       health=health)
            row["shape_valid"] = (bool(raw.strip()) if case["field"] == "judge_evaluations"
                                  else F.valid(case["task"], raw, case["user"]))
            row["identity_valid"] = row["model"] == case["expected_model"]
        except Exception as ex:
            row.update(error_type=type(ex).__name__, error=str(ex)[:300])
        row["seconds"] = round(time.time() - row["started_at"], 2)
        rows.append(row)
        out.write_text(json.dumps(rows, indent=2) + "\n")
    log("%s: %d attempts, cost $%.2f" % (provider, len(rows), B.session_spent(session, ledger) or 0))


def hoa_check(out_dir, served):
    """Bill's HOA screening, deterministic (port of S315 hoa_retention.py)."""
    import llm_providers as L
    from hoa_leads import hoa_monitor as M
    from capability_health import observe
    reg = json.loads(REGISTRY.read_text())["projects"]
    recs = [(i, e) for i, e in enumerate(reg.get("hoa_screening", {}).get("evaluations", []))
            if e.get("approved") is True and e.get("id") == "vllm" and e.get("model") == served.get("vllm")]
    fixtures = next(EVROOT.rglob("s279-hoa-qualification.json"), None)
    if not recs or not fixtures:
        return []
    c = dict(json.loads(CREDS.read_text()), llm_privacy="LOCAL_ONLY", vllm_timeout=300)
    contract = sha((M.FILTER_INSTRUCTIONS + inspect.getsource(M.screening_format) + inspect.getsource(M._screening_user)
                    + inspect.getsource(M.parse_screening)).encode())
    out = []
    for index, old in recs:
        assert old["contract_sha256"] == contract and old["prompt_sha256"] == sha(M.FILTER_SYSTEM.encode())
        for r in json.loads(fixtures.read_text())["rows"]:
            at = time.time(); source = r["source"]
            raw = L.call("vllm", M.FILTER_SYSTEM, M._screening_user(source),
                         dict(c, vllm_response_format=M.screening_format(len(source))), max_tokens=8000, retries=0,
                         task="hoa-leads:screen-local", privacy="LOCAL_ONLY", session_id="renewal-hoa",
                         strict_accounting=True)
            rows = M.parse_screening(raw, len(source)); expect = [x["lead"] for x in r["decisions"]]
            out.append(dict(project="hoa_screening", index=index, case=r["case"], started_at=at, model=L.last_model(),
                            raw=raw, passed=rows is not None and [x["lead"] for x in rows] == expect,
                            expected=expect, actual=[x["lead"] for x in rows] if rows else None,
                            contract_sha256=contract, prompt_sha256=old["prompt_sha256"],
                            old_record_sha256=sha(json.dumps(old, sort_keys=True).encode()), health=observe(c, "vllm")))
    (Path(out_dir) / "hoa-retention.json").write_text(json.dumps(out, indent=2))
    return out


def gates(rows):
    latest = {}
    for r in rows:
        latest[(r["route"], r["field"], r["index"], r["fixture_id"])] = r
    bad = [r for r in latest.values() if not (r.get("shape_valid") and r.get("identity_valid")
                                               and not r.get("error_type") and r.get("finish") not in ("length", "max_tokens"))]
    return latest, bad


def write_packet(out_dir, host, projects, index):
    """PACKET.txt: every fresh answer beside the answer in the record's CURRENTLY
    APPROVED evidence; decisions.json with every record approved=null for the
    reviewing session to fill in."""
    prior = {}
    for key, route in projects.items():
        for field in ("evaluations", "judge_evaluations"):
            for i, e in enumerate(route.get(field, []) if isinstance(route, dict) else []):
                for h in (e.get("evidence_files") or {}).values():
                    for r in load(index[h], []) if h in index else []:
                        g = _body(dict(r, task=key[11:])) if isinstance(r, dict) else None
                        if g and r.get("response") and r.get("provider", e["id"]) == e["id"]:
                            prior[(key, field, i, sha(json.dumps(g, sort_keys=True).encode()))] = r["response"]
    out_dir = Path(out_dir)
    rows = [r for f in out_dir.glob("run-*.json") for r in load(f, [])]
    latest, bad = gates(rows)
    lines = ["RENEWAL PACKET -- %s -- %s" % (host, out_dir.name),
             "Gate failures (shape/identity/finish/error): %d of %d fixtures" % (len(bad), len(latest)), ""]
    decisions = {}
    for (route, field, index_, fid), r in sorted(latest.items()):
        key = (route, field, index_)
        if key not in decisions:
            decisions[key] = {"route": route, "field": field, "index": index_, "provider": r["provider"],
                              "approved": None, "rationale": "", "reviewed_fixtures": [],
                              "evidence_file": "run-%s.json" % r["provider"]}
            lines += ["", "#" * 90, "# %s  %s[%d]  %s / %s" % (route, field, index_, r["provider"], r.get("model"))]
        decisions[key]["reviewed_fixtures"].append(fid)
        lines += ["--- case %s | shape=%s identity=%s finish=%s err=%s | %ss" % (
            r["case"], r.get("shape_valid"), r.get("identity_valid"), r.get("finish"), r.get("error_type"), r.get("seconds")),
            "INPUT: " + (r["user"][-400:] if len(r["user"]) < 3000 else "[%d chars] %s" % (len(r["user"]), r["user"][:300])).replace("\n", " "),
            "NEW:   " + (r.get("response") or "").replace("\n", " | "),
            "PRIOR: " + (prior.get((route, field, index_, fid)) or "(no prior answer on file)").replace("\n", " | ")[:1500], ""]
    hoa = load(out_dir / "hoa-retention.json", [])
    if hoa:
        lines += ["", "# hoa_screening (deterministic): " + ", ".join("%s passed=%s" % (x["case"], x["passed"]) for x in hoa)]
    (out_dir / "PACKET.txt").write_text("\n".join(lines) + "\n")
    tmpl = {"host": host, "records": sorted(decisions.values(), key=lambda d: (d["route"], d["field"], d["index"])),
            "hoa": [{"index": i, "approved": None, "rationale": ""} for i in sorted({x["index"] for x in hoa})]}
    (out_dir / "decisions.json").write_text(json.dumps(tmpl, indent=2) + "\n")
    return len(latest), len(bad)


def run_all(creds, providers, served, projects):
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out_dir = EVROOT / ("renewal-%s-%s" % (stamp, "cumulus" if on_cumulus() else "cirrus"))
    out_dir.mkdir(parents=True, exist_ok=True)
    index, bodies = evidence_index()
    manifest, missing = build_manifest(projects, providers, served, index, bodies)
    mpath = out_dir / ("manifest-%s.json" % ("cumulus" if on_cumulus() else "cirrus"))
    mpath.write_text(json.dumps(manifest, indent=2) + "\n")
    log("manifest: %d cases, %d missing evidence" % (len(manifest), len(missing)))
    procs = []
    for p in sorted({x["provider"] for x in manifest}):
        procs.append(subprocess.Popen([PY, str(Path(__file__)), "_run", str(mpath), p,
                                       str(out_dir / ("run-%s.json" % p)), "renewal-%s-%s" % (stamp, p)],
                                      cwd=str(HERE)))
    for pr in procs:
        pr.wait()
    if on_cumulus():
        hoa_check(out_dir, served)
    fixtures, bad = write_packet(out_dir, "cumulus" if on_cumulus() else "cirrus", projects, index)
    return out_dir, fixtures, bad, missing


def plan():
    """What a run WOULD do on this box -- no model calls, nothing written."""
    creds = json.loads(CREDS.read_text())
    providers = providers_here(); served = served_models(creds, providers)
    projects = json.loads(REGISTRY.read_text())["projects"]
    index, bodies = evidence_index()
    manifest, missing = build_manifest(projects, providers, served, index, bodies)
    earliest = earliest_expiry(projects, providers, served)
    by = {}
    for x in manifest:
        by[x["provider"]] = by.get(x["provider"], 0) + 1
    print(json.dumps({"host": "cumulus" if on_cumulus() else "cirrus", "served": served,
                      "records": len({(x["route"], x["field"], x["index"]) for x in manifest}),
                      "cases": by, "missing_evidence": missing,
                      "earliest_expiry": datetime.fromtimestamp(earliest).isoformat(timespec="minutes") if earliest else None,
                      "decision_now": decide(time.time(), earliest, load(STATE, {}) or {})}, indent=1))


def notify(text):
    try:
        from immaculate_agent import send_telegram
        return send_telegram(text)
    except Exception as ex:
        return "FAILED: %s" % type(ex).__name__


def main(force=False):
    creds = json.loads(CREDS.read_text())
    providers = providers_here()
    served = served_models(creds, providers)
    projects = json.loads(REGISTRY.read_text())["projects"]
    earliest = earliest_expiry(projects, providers, served)
    state = load(STATE, {}) or {}
    now = time.time()
    action = "run" if force else decide(now, earliest, state)
    host = "CUMULUS" if on_cumulus() else "CIRRUS"
    when = datetime.fromtimestamp(earliest).strftime("%a %b %d %H:%M") if earliest else "-"
    hours = (earliest - now) / 3600 if earliest else 0
    ok, note = True, ""
    if action == "none":
        note = "no live approvals for this box"
    elif action == "wait":
        note = "next renewal due by %s (%.0f h)" % (when, hours)
    elif action == "remind":
        msg = ("Approval renewal still pending on %s: packet %s is waiting for review; approvals expire %s "
               "(in %.0f h). Start a Cowork session: 'review the renewal packet'." % (host, state.get("last_dir"), when, hours))
        note = "reminded: " + notify(msg)
    else:
        out_dir, fixtures, bad, missing = run_all(creds, providers, served, projects)
        state.update(last_run_at=now, last_run_for=earliest, last_dir=str(out_dir))
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=2))
        expired = hours <= 0
        ok = not expired
        msg = ("%sApproval renewal tests ran on %s: %d fixtures, %d gate failure(s)%s. Review packet: %s/PACKET.txt. "
               "Approvals expire %s (%s). Start a Cowork session and say 'review the renewal packet'." % (
                   "EXPIRED -- " if expired else "", host, fixtures, bad,
                   ", %d record(s) missing evidence" % len(missing) if missing else "", out_dir, when,
                   "ALREADY EXPIRED" if expired else "in %.0f h" % hours))
        note = "ran %d fixtures, %d gate failures; packet %s; telegram %s" % (fixtures, bad, out_dir.name, notify(msg))
    log(note)
    return ok, note


# ── the install half (run by a reviewing session, never by the timer) ─────────
def _decisions(dirs, host):
    """Filled decisions.json from each packet dir; records keep the dir they came from."""
    recs, hoa = [], []
    for d in dirs:
        doc = load(Path(d) / "decisions.json", {}) or {}
        for r in doc.get("records", []):
            recs.append(dict(r, _dir=str(d)))
        if doc.get("host") == host:
            hoa += [dict(h, _dir=str(d)) for h in doc.get("hoa", [])]
    return recs, hoa


def install(host, mode, dirs):
    """Port of S287/S315 install_reviewed.py + install_retention.py; every guard kept.
    Cloud decisions from the CUMULUS packet apply to CIRRUS too (identical records;
    the old-record hash guard refuses if they ever diverge)."""
    assert mode in ("check", "apply")
    import capability_registry as C
    from hoa_leads import hoa_monitor as M
    path = REGISTRY; original = path.read_bytes(); before = json.loads(original); after = copy.deepcopy(before)
    recs, hoa = _decisions(dirs, host)
    here_providers = set(CLOUD) | {"vllm" if host == "cumulus" else "ollama"}
    recs = [r for r in recs if r["provider"] in here_providers]
    undecided = [r for r in recs + hoa if type(r.get("approved")) is not bool or not str(r.get("rationale", "")).strip()]
    assert not undecided, "undecided or unexplained records: %d -- review them first" % len(undecided)
    manifest = [x for d in dirs for f in Path(d).glob("manifest-*.json") for x in load(f, [])]
    changes = []
    for dec in recs:
        key, field, index = dec["route"], dec["field"], dec["index"]
        route = after["projects"][key]; old = before["projects"][key][field][index]
        assert C.contract_digest(route, HERE) == old["contract_sha256"]
        expected = [r for r in manifest if (r["route"], r["field"], r["index"]) == (key, field, index)]
        evf = Path(dec["_dir"]) / dec["evidence_file"]
        data = json.loads(evf.read_text()); chosen = []
        for fixture in expected:
            attempts = [r for r in data if (r["route"], r["field"], r["index"], r["fixture_id"]) == (key, field, index, fixture["fixture_id"])]
            assert attempts, "missing fixture"; row = attempts[-1]
            assert row["old_record_sha256"] == sha(json.dumps(old, sort_keys=True).encode()), "approval changed concurrently"
            assert row.get("shape_valid") and row.get("identity_valid") and not row.get("error_type")
            assert row["model"] == old["model"] and row["finish"] not in ("length", "max_tokens")
            assert sha(row["system"].encode()) == old["prompt_sha256"]
            h = row["health"]; assert h["healthy"] and h["model"] == old["model"] and h["location"] == old["location"]
            assert row["fixture_id"] in dec["reviewed_fixtures"]
            chosen.append(row)
        assert chosen and len(chosen) == len(dec["reviewed_fixtures"])
        at = min(r["started_at"] for r in chosen); assert time.time() - 86400 < at <= time.time()
        digest = sha(evf.read_bytes())
        rep = copy.deepcopy(old)
        rep.update(approved=dec["approved"], evaluated_at=at,
                   expires_at=(at + min(VALIDITY_S, old["expires_at"] - old["evaluated_at"])) if dec["approved"] else old["expires_at"],
                   evidence_id="RENEWAL:" + digest, evidence_files={"%s/%s" % (Path(dec["_dir"]).name, dec["evidence_file"]): digest},
                   scope=old["scope"] + " %s: identical fixture scope freshly re-evaluated; %s" % (Path(dec["_dir"]).name, dec["rationale"]))
        route[field][index] = rep
        changes.append((key, dec["provider"], dec["approved"]))
    for h in hoa:
        rows = load(Path(h["_dir"]) / "hoa-retention.json", [])
        group = [r for r in rows if r["index"] == h["index"]]
        old = before["projects"]["hoa_screening"]["evaluations"][h["index"]]
        contract = sha((M.FILTER_INSTRUCTIONS + inspect.getsource(M.screening_format) + inspect.getsource(M._screening_user)
                        + inspect.getsource(M.parse_screening)).encode())
        assert group and all(r["passed"] and r["health"]["healthy"] and r["health"]["model"] == r["model"] == old["model"]
                             and r["contract_sha256"] == old["contract_sha256"] == contract
                             and r["old_record_sha256"] == sha(json.dumps(old, sort_keys=True).encode()) for r in group)
        at = min(r["started_at"] for r in group); assert time.time() - 86400 < at <= time.time()
        if h["approved"]:
            digest = sha((Path(h["_dir"]) / "hoa-retention.json").read_bytes())
            rec = after["projects"]["hoa_screening"]["evaluations"][h["index"]]
            rec.update(evaluated_at=at, expires_at=at + VALIDITY_S, evidence_id="RENEWAL:" + digest,
                       evidence_files={"%s/hoa-retention.json" % Path(h["_dir"]).name: digest},
                       scope=rec["scope"] + " %s: HOA fixtures re-run, all decisions matched; %s" % (Path(h["_dir"]).name, h["rationale"]))
            changes.append(("hoa_screening", "vllm", True))
    # only approval/evidence/time/scope fields may change, and only on decided records
    allowed = {"approved", "evaluated_at", "expires_at", "evidence_id", "evidence_files", "scope"}
    check = copy.deepcopy(after)
    for dec in recs:
        check["projects"][dec["route"]][dec["field"]][dec["index"]] = before["projects"][dec["route"]][dec["field"]][dec["index"]]
        a, b = after["projects"][dec["route"]][dec["field"]][dec["index"]], before["projects"][dec["route"]][dec["field"]][dec["index"]]
        assert {k for k in set(a) | set(b) if a.get(k) != b.get(k)} <= allowed
    for h in hoa:
        check["projects"]["hoa_screening"]["evaluations"][h["index"]] = before["projects"]["hoa_screening"]["evaluations"][h["index"]]
    assert check == before, "a record outside the decisions would change"
    assert path.read_bytes() == original, "registry changed while preparing"
    if mode == "apply":
        snap = HERE / "config/snapshots" / ("renewal-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        snap.mkdir(parents=True, exist_ok=False)
        (snap / "registry-before.json").write_bytes(original)
        tmp = path.with_name(path.name + ".renewal.tmp")
        tmp.write_text(json.dumps(after, indent=2) + "\n"); os.chmod(tmp, path.stat().st_mode & 0o777); os.replace(tmp, path)
    print(json.dumps({"mode": mode, "host": host, "renewed": sum(1 for c in changes if c[2]),
                      "rejected": [c[0] + "/" + c[1] for c in changes if not c[2]], "unrelated_records_unchanged": True}))


def verify(host, dirs):
    """Port of verify_installed.py: admission code, fresh health, no inference."""
    import capability_registry as C, capability_admission as A, capability_health as H
    creds = json.loads(CREDS.read_text()); p = json.loads(REGISTRY.read_text())["projects"]
    recs, _ = _decisions(dirs, host)
    here_providers = set(CLOUD) | {"vllm" if host == "cumulus" else "ollama"}
    health, passed, held, deferred = {}, 0, 0, []
    for d in [r for r in recs if r["provider"] in here_providers]:
        route = p[d["route"]]; record = route[d["field"]][d["index"]]; provider = record["id"]
        if provider not in health:
            health[provider] = H.observe(creds, provider) if provider in LOCAL else H.observe_cloud(creds, provider)
        rows = load(Path(d["_dir"]) / d["evidence_file"], [])
        r = next(r for r in rows if (r["route"], r["field"], r["index"]) == (d["route"], d["field"], d["index"]) and r.get("response"))
        args = dict(task=record["task"], capability=record["capability"], system=r["system"],
                    contract_sha256=C.contract_digest(route, HERE))
        admitted = A.candidates([record], [health[provider]], **args)
        assert bool(admitted) == (d["approved"] and health[provider].get("healthy", False)), (d["route"], provider, "wrong admission")
        if d["approved"] and not health[provider].get("healthy"):
            deferred.append(d["route"]); continue
        if d["approved"]:
            expired = copy.deepcopy(record); expired["expires_at"] = time.time() - 1
            assert not A.candidates([expired], [health[provider]], **args)
            passed += 1
        else:
            held += 1
    print(json.dumps({"host": host, "renewed_records_admitted": passed, "failed_review_records_rejected": held,
                      "expired_copies_rejected": passed, "runtime_deferred": deferred}))


# ── selftest (offline, no registry or network) ────────────────────────────────
def selftest():
    import tempfile
    ok = True

    def ck(name, cond):
        nonlocal ok
        print("  [%s] %s" % ("OK " if cond else "FAIL", name)); ok = ok and cond
    now = 1_800_000_000.0
    rec = lambda pid, model, exp, approved=True: {"id": pid, "model": model, "approved": approved, "expires_at": exp}
    projects = {
        "foundation:a": {"enabled": True, "evaluations": [rec("anthropic", "claude", now + 30 * 3600),
                                                           rec("kimi", "kimi-k3", now + 90 * 3600)]},
        "foundation:off": {"enabled": False, "evaluations": [rec("anthropic", "claude", now + 3600)]},
        "foundation:b": {"enabled": True, "evaluations": [rec("vllm", "qwen-old", now + 2 * 3600),
                                                           rec("vllm", "gpt-oss", now + 60 * 3600),
                                                           rec("kimi", "kimi-k3", now + 1, approved=False)]},
        "hoa_screening": {"enabled": True, "evaluations": [rec("vllm", "gpt-oss", now + 50 * 3600)]},
    }
    served = {"vllm": "gpt-oss"}
    live = list(live_records(projects, ("anthropic", "kimi", "vllm"), served))
    ck("live: a disabled route is not renewed", all(k != "foundation:off" for k, *_ in live))
    ck("live: a local model the slot no longer serves lapses", all(e["model"] != "qwen-old" for *_, e in live))
    ck("live: an unapproved record is not renewed", all(e["approved"] for *_, e in live))
    ck("live: CIRRUS (ollama only) owns no cloud record", not list(live_records(projects, ("ollama",), {})))
    ck("expiry: earliest live one (30 h), not the lapsing qwen (2 h)",
       earliest_expiry(projects, ("anthropic", "kimi", "vllm"), served) == now + 30 * 3600)
    ck("decide: nothing live -> none", decide(now, None, {}) == "none")
    ck("decide: > 48 h away -> wait", decide(now, now + 49 * 3600, {}) == "wait")
    ck("decide: within 48 h, no packet -> run", decide(now, now + 30 * 3600, {}) == "run")
    ck("decide: fresh packet for the same expiry -> remind, no re-run",
       decide(now, now + 30 * 3600, {"last_run_at": now - 3600, "last_run_for": now + 30 * 3600}) == "remind")
    ck("decide: packet older than STALE_H -> run again (installer needs < 24 h)",
       decide(now, now + 30 * 3600, {"last_run_at": now - 21 * 3600, "last_run_for": now + 30 * 3600}) == "run")
    ck("decide: a packet for an OLD expiry does not count -> run",
       decide(now, now + 30 * 3600, {"last_run_at": now - 3600, "last_run_for": now - 99}) == "run")
    ck("decide: already expired -> run (and main marks it unhealthy)", decide(now, now - 60, {}) == "run")
    ck("body: a row without max_tokens uses the caller's real value (S313 STRATUS)",
       _body({"case": "c", "system": "s", "user": "u", "task": "stratus:monthly"})["max_tokens"] == 4000)
    ck("body: an unknown task without max_tokens is not guessed",
       _body({"case": "c", "system": "s", "user": "u", "task": "other"}) is None)
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "decisions.json").write_text(json.dumps({"host": "cumulus", "records": [
            {"route": "foundation:a", "field": "evaluations", "index": 0, "provider": "anthropic",
             "approved": None, "rationale": "", "reviewed_fixtures": ["f"], "evidence_file": "run-anthropic.json"}],
            "hoa": []}))
        recs, _ = _decisions([d], "cumulus")
        undecided = [r for r in recs if type(r.get("approved")) is not bool or not r.get("rationale", "").strip()]
        ck("install: a template left approved=null is refused (undecided)", len(undecided) == 1)
        ck("install: records remember the packet dir they came from", recs[0]["_dir"] == str(d))
    print("selftest:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["selftest"] or "--selftest" in a:
        sys.exit(0 if selftest() else 1)
    if a[:1] == ["_run"]:
        run_provider(a[1], a[2], a[3], a[4]); sys.exit(0)
    if a[:1] == ["install"]:
        install(a[1], a[2], a[3:]); sys.exit(0)
    if a[:1] == ["verify"]:
        verify(a[1], a[2:]); sys.exit(0)
    if a == ["--plan"]:
        plan(); sys.exit(0)
    if a and a != ["--force"]:
        sys.exit("usage: foundation_renewal.py [--force|--plan] | install HOST check|apply DIR... | verify HOST DIR... | selftest")
    # One job_status name per ledger, each in the file its box runs, so
    # job_status's static check can see which box records which name (S316):
    # this file is the CUMULUS entry point; CIRRUS runs foundation_renewal_cirrus.py.
    if not on_cumulus():
        sys.exit("on CIRRUS run foundation_renewal_cirrus.py (it records CIRRUS's own ledger name)")
    ok, note = main(force="--force" in a)
    try:
        import job_status
        job_status.record("foundationrenewalcumulus", ok, note[:300])
    except Exception as ex:
        log("job_status.record failed: %s" % ex)
    sys.exit(0 if ok else 1)
