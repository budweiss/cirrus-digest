"""S348 O04: merge resolved history back after an old-writer rollback. Scratch only.

The "old writer" is the ACTUAL pinned pre-S340 alert_policy (test_rollback_
preservation.historical), not an imitation. All state lives in a
TemporaryDirectory; nothing reads or writes live monitoring state, the network
or any service.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import incident_history_reconcile as hr
from supervisor import alert_policy, state_checkpoint
from test_rollback_preservation import historical

OLDER = {"incident": "older", "state": "resolved", "recovery_evidence": "two probes"}
R1 = {"incident": "unit:a.service", "state": "resolved", "resolved_at": 950,
      "first_seen": 800, "recovery_evidence": "absent from healthy probes for 120 seconds"}
ACTIVE = {"unit:fixture.service": {"first_seen": 900, "last_seen": 1000, "owner": "Cowork",
                                   "state": "action pending", "disposition": "inspect fixture"}}


def _digest(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).rglob("*")) if p.is_file()}


class HistoryReconcile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_policy = historical("alert_policy")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        (self.state / "pending-requests").mkdir(parents=True)
        self.incidents = self.state / "heartbeat-incidents.json"
        self.incidents.write_text(json.dumps({"incidents": ACTIVE, "resolved": [OLDER, R1]}))
        (self.state / "pending-request.json").write_text(json.dumps({"id": "primary", "state": "delivered"}))
        (self.state / "pending-requests" / "queued.json").write_text(json.dumps({"id": "queued"}))
        (self.state / "telegram-update-offset.txt").write_text("55")
        self.export = self.root / "protected-export"
        state_checkpoint.capture(self.state, self.export)
        self.output = self.root / "review" / "heartbeat-incidents.reconciled.json"

    def old_writer_saves(self):
        old = self.old_policy.IncidentPolicy(self.incidents)
        old.save()
        self.assertNotIn("resolved", json.loads(self.incidents.read_text()))   # the loss being repaired

    def run_cli(self, *extra):
        return subprocess.run([sys.executable, str(Path(hr.__file__)), "--checkpoint", str(self.export),
                               "--state", str(self.state), "--output", str(self.output), *extra],
                              capture_output=True, text=True)

    def test_history_lost_by_actual_old_writer_is_merged_back_without_touching_state(self):
        self.old_writer_saves()
        new = alert_policy.IncidentPolicy(self.incidents)           # re-upgraded code
        new.resolved.append({"incident": "unit:b.service", "state": "resolved", "resolved_at": 1500})
        new.active["unit:new.service"] = {"first_seen": 1400, "owner": "Skywarden", "state": "detected"}
        new.save()
        (self.state / "pending-requests" / "queued.json").unlink()   # consumed after the export
        (self.state / "telegram-update-offset.txt").write_text("99")
        before = _digest(self.state)
        candidate, report = hr.reconcile(self.export, self.state)
        hr.write_candidate(candidate, self.output, self.state)
        self.assertEqual(_digest(self.state), before)                # live state untouched
        self.assertEqual(candidate["incidents"], new.active)          # current actives, not the export's
        self.assertEqual([e["incident"] for e in candidate["resolved"]],
                         ["older", "unit:a.service", "unit:b.service"])
        self.assertEqual(report["resolved_added_from_export"], 2)
        self.assertEqual(report["queued_decisions_in_export_not_current_not_restored"],
                         ["pending-requests/queued.json"])
        self.assertEqual(set(candidate), {"incidents", "resolved"})     # no decision/receipt/offset data
        self.assertNotIn('"queued"', json.dumps(candidate))
        self.assertNotIn('"primary"', json.dumps(candidate))
        installed = alert_policy.IncidentPolicy(self.output)          # current reader accepts it
        self.assertFalse(installed.persistence_error)
        self.assertEqual(len(installed.resolved), 3)

    def test_present_entries_are_not_duplicated_and_conflicts_keep_current(self):
        changed = dict(R1, recovery_evidence="edited later")
        self.incidents.write_text(json.dumps({"incidents": ACTIVE, "resolved": [OLDER, changed]}))
        candidate, report = hr.reconcile(self.export, self.state)
        self.assertEqual(report["resolved_already_present"], 1)
        self.assertEqual(report["conflicts_kept_current"], ["unit:a.service"])
        self.assertEqual(report["resolved_added_from_export"], 0)
        self.assertIn(changed, candidate["resolved"])
        self.assertNotIn(R1, candidate["resolved"])

    def test_tampered_checkpoint_is_refused_and_nothing_written(self):
        self.old_writer_saves()
        (self.export / "heartbeat-incidents.json").write_text(json.dumps({"incidents": {}, "resolved": []}))
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn("REFUSED: checkpoint hash differs", result.stdout)
        self.assertFalse(self.output.exists())

    def test_output_is_never_overwritten_or_placed_in_state(self):
        self.output.parent.mkdir(parents=True)
        self.output.write_text("operator notes")
        self.assertEqual(self.run_cli().returncode, 2)
        self.assertEqual(self.output.read_text(), "operator notes")
        inside = self.state / "heartbeat-incidents.reconciled.json"
        candidate, _ = hr.reconcile(self.export, self.state)
        with self.assertRaisesRegex(ValueError, "outside the live state"):
            hr.write_candidate(candidate, inside, self.state)
        self.assertFalse(inside.exists())

    def test_unreadable_current_state_is_refused(self):
        for bad in ("{not json", json.dumps({"resolved": []}), json.dumps({"incidents": {}, "resolved": ["x"]})):
            with self.subTest(bad=bad):
                self.incidents.write_text(bad)
                with self.assertRaises(ValueError):
                    hr.reconcile(self.export, self.state)

    def test_dry_run_reports_and_writes_nothing(self):
        self.old_writer_saves()
        result = self.run_cli("--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report["resolved_added_from_export"], 2)
        self.assertIsNone(report["output"])
        self.assertFalse(self.output.exists())
        unknown = subprocess.run([sys.executable, str(Path(hr.__file__)), "--selftest"],
                                 capture_output=True, text=True)
        self.assertEqual(unknown.returncode, 2)                        # argparse refuses (T118)

    def test_writer_cap_is_reported(self):
        many = [{"incident": "u%d" % i, "state": "resolved", "resolved_at": 2000 + i} for i in range(300)]
        self.incidents.write_text(json.dumps({"incidents": ACTIVE, "resolved": many}))
        _, report = hr.reconcile(self.export, self.state)
        self.assertEqual(report["resolved_total"], 302)
        self.assertEqual(report["writer_will_truncate"], 302 - hr.WRITER_CAP)


if __name__ == "__main__":
    unittest.main()
