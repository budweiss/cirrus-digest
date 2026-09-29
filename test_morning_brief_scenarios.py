"""CIRRUS morning brief: operational scenarios for compose() (S348, O03).

Scratch only. HOME points at a TemporaryDirectory before the module is imported
(it reads config and credentials at import), `runtime_config` is stubbed, the
pending/builds files are real temp files, and the job ledger, stall check, Time
Machine, digest and attention sources are fixed per scenario. Every send path
fails the test if touched: compose() must never deliver anything.
"""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_HOME = tempfile.mkdtemp(prefix="mb-home-")
os.environ["HOME"] = _HOME
_CFG = Path(_HOME) / "projects/cirrus-digest/config"
_CFG.mkdir(parents=True)
(_CFG / "credentials.json").write_text(json.dumps({"telegram_bot_token": "", "telegram_user_id": ""}))
(_CFG / "sources.json").write_text("{}")
_stub = types.ModuleType("runtime_config")
_stub.load_sources = lambda path: {"digest": {"output_dir": str(Path(_HOME) / "out"),
                                              "log_dir": str(Path(_HOME) / "logs")}}
sys.modules["runtime_config"] = _stub
sys.path.insert(0, str(Path(__file__).resolve().parent))
import morning_brief as mb  # noqa: E402
import job_status as real_job_status  # noqa: E402  (before any test swaps the module)


def _no_send(*a, **k):
    raise AssertionError("compose() must never send")


class MorningBriefScenarios(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mb-"))
        self.pending = self.tmp / "pending_approvals.json"
        self.builds = self.tmp / "builds.json"
        self.pending.write_text("[]")
        self.builds.write_text("[]")
        self.ledger = types.ModuleType("job_status")
        self.ledger.summarize = lambda: (["✅ intake: ran 06:00"], True)
        for target, value in (
                ("PENDING_FILE", self.pending), ("BUILDS_FILE", self.builds),
                ("gather_digest", lambda: {"dated_today": True, "line": "Digest: 12 items", "notable": []}),
                ("gather_actions", lambda: {"actions": [], "notes": []}),
                ("gather_attention", lambda: []),
                ("gather_timemachine", lambda: ("Time Machine: last backup 2h ago", True)),
                ("gather_stalls", lambda: (["- ✅ nothing stalled (3 signals checked)"], True)),
                ("send_all", _no_send), ("send_telegram", _no_send)):
            p = patch.object(mb, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(sys.modules, {"job_status": self.ledger})
        p.start()
        self.addCleanup(p.stop)

    def body(self):
        subject, body = mb.compose()
        self.assertIn("CIRRUS Morning Brief", subject)
        return body

    def test_healthy_all_sources_read(self):
        body = self.body()
        self.assertIn("✅ CIRRUS healthy", body)
        self.assertIn("None — /accept queue is clear", body)
        self.assertIn("**Next:** Nothing needs you this morning.", body)
        self.assertIn("✅ intake: ran 06:00", body)

    def test_unreadable_pending_is_unknown_not_clear(self):
        for label, content in (("missing", None), ("corrupt", "{not json"),
                               ("not a list", '{"pending": []}'), ("non-object row", '["x"]')):
            with self.subTest(label):
                if content is None:
                    self.pending.unlink(missing_ok=True)
                else:
                    self.pending.write_text(content)
                body = self.body()
                self.assertNotIn("queue is clear", body)
                self.assertIn("UNKNOWN — pending_approvals.json could not be read", body)
                self.assertIn("**Pending decisions (?)**", body)
                self.assertIn("⚠️ Needs a look", body)
                self.assertIn("/accept queue state UNKNOWN", body)
                self.assertIn("**Next:** Check the attention flag(s) above.", body)

    def test_unreadable_builds_is_unknown_not_nothing_awaiting(self):
        for content in (None, "[1, 2]"):
            with self.subTest(content=content):
                if content is None:
                    self.builds.unlink(missing_ok=True)
                else:
                    self.builds.write_text(content)
                body = self.body()
                self.assertIn("builds awaiting ship/discard UNKNOWN", body)
                self.assertIn("⚠️ Needs a look", body)
                self.assertNotIn("Nothing needs you", body)

    def test_unreadable_job_ledger_is_unknown_not_absent(self):
        def boom():
            raise OSError("ledger unreadable")
        self.ledger.summarize = boom
        body = self.body()
        self.assertIn("**Scheduled jobs**", body)
        self.assertIn("job ledger could not be read — scheduled-job status UNKNOWN", body)
        self.assertIn("⚠️ Needs a look", body)

    def test_real_pending_and_awaiting_items_are_reported_with_the_right_action(self):
        self.pending.write_text(json.dumps([
            {"type": "TEST_GAP", "status": "pending", "detail": "fleet_queue.py has no selftest"},
            {"type": "NOTE", "status": "done", "detail": "old"}]))
        self.builds.write_text(json.dumps([
            {"id": "prop-1", "status": "awaiting-confirm", "summary": "built and approved"},
            {"id": "prop-2", "status": "blocked"}]))
        body = self.body()
        self.assertIn("**Pending decisions (1)**", body)
        self.assertIn("TEST_GAP: fleet_queue.py has no selftest", body)
        self.assertIn("Awaiting your ship/discard (1)", body)
        self.assertIn("prop-1: built and approved", body)
        self.assertIn("**Next:** Ship or discard 1 built Dev-Loop item(s)", body)
        self.assertNotIn("prop-2", body)

    def test_stall_check_failure_is_reported_but_not_folded_into_the_verdict(self):
        # S74 design, preserved: a stall line reports, it does not turn the header red.
        with patch.object(mb, "gather_stalls", lambda: (["- ⚠️ stall check could not run (boom)"], False)):
            body = self.body()
        self.assertIn("- ⚠️ stall check could not run (boom)", body)
        self.assertIn("✅ CIRRUS healthy", body)

    def real_ledger(self, rows, paused=(), node="CUMULUS", fetch=None):
        """The REAL job_status.summarize over a temp ledger (no live file, no SSH)."""
        js = real_job_status
        declared = self.tmp / "declared.json"
        self.ledger.summarize = lambda: js.summarize(_local=rows, _node=node, _fetch=fetch,
                                                     _declared_path=declared, _paused=set(paused))

    def row(self, ok, age_h=1.0, note=""):
        import time
        return {"last_run": "2026-09-28T06:00:00", "epoch": int(time.time() - age_h * 3600),
                "ok": ok, "note": note}

    def test_failed_or_overdue_job_is_not_healthy_and_is_named(self):
        # S348: the live brief printed "healthy" (bar a Dev-Loop build) beside
        # "⚠️ ytwatch ... FAILED" and "⚠️ billsnow (CUMULUS) ... FAILED".
        for label, rows, name in (
                ("failed", {"ytwatch": self.row(False, note="HTTPError 404")}, "ytwatch"),
                ("overdue", {"daily": self.row(True, age_h=30)}, "daily")):
            with self.subTest(label):
                self.real_ledger(rows)
                body = self.body()
                self.assertIn("⚠️ Needs a look", body)
                self.assertIn(f"scheduled job(s) failed or overdue: {name} — see Scheduled jobs", body)
                self.assertIn("**Next:** Check the attention flag(s) above.", body)
        self.real_ledger({"billsnow": self.row(False)}, node="CIRRUS",
                         fetch=lambda: {"billsnow": self.row(False)})
        self.assertIn("failed or overdue: billsnow (CUMULUS) — see", self.body())

    def test_resolved_held_and_unconfirmable_jobs_stay_healthy(self):
        self.real_ledger({"ytwatch": self.row(True, note="recovered after yesterday's 404s"),
                          "daily": self.row(False)}, paused={"daily"})
        body = self.body()
        self.assertIn("✅ CIRRUS healthy", body)
        self.assertIn("⏸ daily: planned maintenance; not executed", body)
        self.assertIn("✅ ytwatch", body)
        self.assertNotIn("failed or overdue", body)
        self.real_ledger({}, node="CIRRUS", fetch=lambda: None)     # CUMULUS unreachable: neutral (S57)
        body = self.body()
        self.assertIn("(CUMULUS): unreachable — can't confirm", body)
        self.assertIn("✅ CIRRUS healthy", body)

    def test_missing_backup_outranks_everything_but_the_digest(self):
        with patch.object(mb, "gather_timemachine", lambda: ("Time Machine: volume locked", False)):
            self.pending.unlink()
            body = self.body()
        self.assertIn("⚠️ Needs a look", body)
        self.assertIn("**Next:** Time Machine is not protecting this box", body)


if __name__ == "__main__":
    unittest.main()
