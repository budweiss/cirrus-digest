"""S685: Skywarden's pass summary is recorded for Telewarden, not sent.

Fictional fixtures only: every test redirects the ledger and state to a temp
directory and replaces the Telegram transport, so nothing is sent and no live
file is written (T32).
"""
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import ledger
import tools

HERE = Path(__file__).resolve().parent


class GateTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        d = Path(self.td.name)
        self.saved = (ledger.STATE_DIR, ledger.LEDGER_JSONL, ledger.LEDGER_MD)
        ledger.STATE_DIR, ledger.LEDGER_JSONL, ledger.LEDGER_MD = d, d / "ledger.jsonl", d / "CHANGES.md"
        self.d = d
        self.sent = []
        def fake_send(text, kind="notify", via=""):
            self.sent.append((text, kind, via))
            ledger.ledger_append({"event": "notify", "tool": "send_telegram", "kind": kind,
                                  "via": via, "detail": text[:80], "summary": text, "result": "sent"})
            return "sent"
        self.p = patch.object(tools, "send_telegram", side_effect=fake_send)
        self.p.start()
        # Module globals the run loop sets; another suite's main_loop test can
        # leave a Mock in ALERT_CONTEXT, so set both here and restore them.
        self.globals = (tools.INCIDENT_ACTIONS, tools.ALERT_CONTEXT)
        tools.INCIDENT_ACTIONS, tools.ALERT_CONTEXT = [], ""

    def tearDown(self):
        self.p.stop()
        ledger.STATE_DIR, ledger.LEDGER_JSONL, ledger.LEDGER_MD = self.saved
        tools.INCIDENT_ACTIONS, tools.ALERT_CONTEXT = self.globals
        self.td.cleanup()

    def rows(self):
        p = ledger.LEDGER_JSONL
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def lease(self, age=0):
        f = self.d / tools.GATE_LEASE_NAME
        f.touch()
        t = time.time() - age
        os.utime(f, (t, t))

    def test_summary_is_held_in_full_while_telewarden_reads(self):
        self.lease()
        text = "Daily check 11-26\n" + "alopecia-brief.service: failed since 11-20; " * 10 + "\nOK: promises"
        out = tools.record_summary(text)
        self.assertIn("not sent to Buddy", out)
        self.assertEqual(self.sent, [])
        r = self.rows()[-1]
        self.assertEqual((r["event"], r["tool"], r["held"], r["kind"]), ("notify", "record_summary", True, "summary"))
        self.assertEqual(r["summary"], text.strip())
        self.assertGreater(len(r["summary"]), 80)

    def test_no_lease_or_stale_lease_or_gate_off_sends_as_before(self):
        self.assertIn("sent to Buddy instead", tools.record_summary("Daily check: no lease"))
        self.lease(age=tools.LEASE_MAX_S + 60)
        tools.record_summary("Daily check: stale lease")
        self.lease()
        (self.d / tools.GATE_OFF_NAME).touch()
        tools.record_summary("Daily check: gate off")
        self.assertEqual([k for _, k, _ in self.sent], ["summary"] * 3)
        self.assertFalse(any(r.get("held") for r in self.rows()))

    def test_unwritable_ledger_sends_rather_than_drops(self):
        self.lease()
        with patch.object(tools, "ledger_append", side_effect=OSError("disk full")):
            tools.record_summary("Daily check: ledger broken")
        self.assertEqual(len(self.sent), 1)

    def test_empty_summary_refused(self):
        self.assertTrue(tools.record_summary("  ").startswith("REFUSED"))

    def test_alert_reaches_buddy_once_per_incident(self):
        out = tools.alert_buddy("cumulus-intake stale 3h: client mail ignored", "cumulus-intake.service", "Bill")
        self.assertEqual(out, "sent")
        self.assertTrue(self.sent[0][0].startswith("Skywarden alert (Bill): "))
        again = tools.alert_buddy("intake still stale", "cumulus-intake", "bill")
        self.assertTrue(again.startswith("HELD"))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.rows()[-1]["tool"], "alert_buddy")
        self.assertTrue(self.rows()[-1]["held"])

    def test_alert_held_without_a_client_or_with_a_ticket_or_over_the_cap(self):
        self.assertTrue(tools.alert_buddy("foundation-renewal exit 1", "foundation-renewal", "").startswith("HELD"))
        self.assertTrue(tools.alert_buddy("x failed", "x", "Buddy").startswith("HELD"))
        tools.INCIDENT_ACTIONS = [{"owner": "dev-loop", "unit": "cirrus-pedagogy.service"}]
        self.assertIn("repair ticket", tools.alert_buddy("pedagogy failed", "pedagogy", "Alyssa"))
        tools.INCIDENT_ACTIONS = []
        self.assertEqual(tools.alert_buddy("a", "unit-a", "Justin"), "sent")
        self.assertEqual(tools.alert_buddy("b", "unit-b", "RCW"), "sent")
        self.assertIn("alerts already went", tools.alert_buddy("c", "unit-c", "Aggie"))
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.sent[1][0].startswith("Skywarden alert (RCW): "))

    def test_a_failed_alert_is_not_marked_alerted(self):
        self.p.stop()
        with patch.object(tools, "send_telegram", return_value="FAILED: offline"):
            self.assertEqual(tools.alert_buddy("intake stale", "cumulus-intake", "Bill"), "FAILED: offline")
        self.p.start()
        self.assertEqual(tools.alert_buddy("intake stale", "cumulus-intake", "Bill"), "sent")

    def test_requests_still_send_and_are_tagged(self):
        with patch.object(tools.opus_approval, "STATE_DIR", self.d), \
             patch.object(tools.opus_approval, "create_guidance_request", return_value="Skywarden needs direction [r1]:\nx\ny"), \
             patch.object(tools.opus_approval, "record_request_delivery"):
            self.assertEqual(tools.request_guidance("fictional issue", "fictional question"), "sent")
        self.assertEqual(self.sent[-1][1:], ("guidance", "request_guidance"))


class ContractTests(unittest.TestCase):
    def test_contract_names_record_summary_and_never_tells_it_to_send(self):
        text = (HERE / "CLAUDE.md").read_text()
        self.assertIn("## 6. Every run, end with exactly one `record_summary` call", text)
        # The only remaining mention says the tool is gone.
        mentions = [l for l in text.splitlines() if "send_telegram" in l]
        self.assertEqual(len(mentions), 1, mentions)
        self.assertIn("no longer have `send_telegram`", mentions[0])

    def test_agent_tool_set_has_no_send_telegram(self):
        src = (HERE / "supervisor_agent.py").read_text()
        names = re.findall(r'@tool\("([a-z_]+)"', src)
        self.assertIn("record_summary", names)
        self.assertIn("alert_buddy", names)
        self.assertNotIn("send_telegram", names)
        try:
            import supervisor_agent
        except ModuleNotFoundError as exc:
            if exc.name in ("claude_agent_sdk", "knowledge_freshness"):
                self.skipTest("built tool set is checked in the deployed supervisor venv")
            raise
        built = [t.name for t in supervisor_agent._build_mcp_tools()]
        self.assertNotIn("send_telegram", built)
        self.assertIn("record_summary", built)
        self.assertIn("alert_buddy", built)
        self.assertIn("request_guidance", built)
        self.assertIn("request_opus_upgrade", built)


if __name__ == "__main__":
    unittest.main()
