"""S539: a handled request closes its ticket. Temp dirs only (T32)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import answer_workflow as aw
import dev_loop
import task_solver as ts


class FallbackTicketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = patch.object(ts, "PROJECT_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(dev_loop, "_notify_ticket")      # no Telegram from a test
        p.start()
        self.addCleanup(p.stop)

    def rec(self, mid, privacy="LOCAL_ONLY"):
        return {"requester": "buddy", "projects": ["stock-pickers"], "title": "PRIVATE",
                "body_head": "PRIVATE BODY", "message_id": mid, "privacy": privacy}

    def test_delivery_closes_the_fallback_ticket_for_that_message_only(self):
        ts._fallback_to_ticket(self.rec("<a@x>"))
        ts._fallback_to_ticket(self.rec("<b@x>"))
        a, b = dev_loop.ticket_load(self.root)
        self.assertEqual(a["meta"], {"request_origin": "<a@x>"})
        self.assertNotIn("PRIVATE", a["title"] + a["detail"])
        ts._resolve_fallback(self.rec("<a@x>#information-recovery-2"), "answer delivered")
        st = {t["meta"]["request_origin"]: t["status"] for t in dev_loop.ticket_load(self.root)}
        self.assertEqual(st, {"<a@x>": "resolved", "<b@x>": "queued"})

    def test_a_message_without_an_id_files_a_ticket_and_resolves_nothing(self):
        ts._fallback_to_ticket(self.rec(""))
        (t,) = dev_loop.ticket_load(self.root)
        self.assertNotIn("meta", t)
        ts._resolve_fallback(self.rec(""), "answer delivered")
        self.assertEqual(dev_loop.ticket_load(self.root)[0]["status"], "queued")

    def test_answer_workflow_resolves_when_the_answer_was_already_delivered(self):
        row = {"id": "r1", "state": "delivered", "privacy": "LOCAL_ONLY", "meta": {}}
        with patch.object(aw, "intake_record", return_value=row), \
             patch.object(ts, "_resolve_fallback") as resolve:
            out = aw._solve_and_answer(self.rec("<a@x>"), {}, "to@x", "Re: q")
        self.assertTrue(out["answered"])
        resolve.assert_called_once()
        self.assertEqual(resolve.call_args[0][0]["message_id"], "<a@x>")


if __name__ == "__main__":
    unittest.main()
