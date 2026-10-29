"""S541: a handled request closes its ticket. Temp dirs only (T32).

The answer code (answer_workflow.py, task_solver.py) is pinned by qualified
model routes, so intake links and closes the fallback ticket instead (T173)."""
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import dev_loop
import intake
import task_solver as ts


class FallbackTicketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for mod in (ts, intake):
            self.stack.enter_context(patch.object(mod, "PROJECT_DIR", self.root))
        self.stack.enter_context(patch.object(intake, "LOG_PATH", self.root / "intake.log"))
        self.stack.enter_context(patch.object(dev_loop, "_notify_ticket"))   # no Telegram

    def rec(self, mid):
        return {"requester": "buddy", "projects": ["stock-pickers"], "title": "PRIVATE",
                "body_head": "PRIVATE BODY", "message_id": mid, "privacy": "LOCAL_ONLY"}

    def attempt(self, rec, delivered):
        """What intake does around one answer attempt that falls back or not."""
        mark = len(dev_loop.ticket_load(self.root))
        if not delivered:
            ts._fallback_to_ticket(rec)
        intake._settle_tickets(rec, delivered, mark)

    def status(self):
        return [(t["status"], (t.get("meta") or {}).get("request_origin"))
                for t in dev_loop.ticket_load(self.root)]

    def test_a_later_delivery_closes_the_fallback_ticket_for_that_message_only(self):
        ts._fallback_to_ticket(self.rec("<old@x>"))          # filed before S541: no link
        self.attempt(self.rec("<a@x>"), delivered=False)
        self.attempt(self.rec("<b@x>"), delivered=False)
        self.assertEqual(self.status(), [("queued", None), ("queued", "<a@x>"), ("queued", "<b@x>")])
        for t in dev_loop.ticket_load(self.root):
            self.assertNotIn("PRIVATE", t["title"] + t["detail"])
        self.attempt(self.rec("<a@x>#information-recovery-2"), delivered=True)
        self.assertEqual(self.status(), [("queued", None), ("resolved", "<a@x>"), ("queued", "<b@x>")])

    def test_a_message_without_an_id_files_a_ticket_and_links_nothing(self):
        self.attempt(self.rec(""), delivered=False)
        self.attempt(self.rec(""), delivered=True)
        self.assertEqual(self.status(), [("queued", None)])

    def test_an_unreadable_queue_never_reaches_the_reply_path(self):
        with patch.object(dev_loop, "ticket_resolve_request", side_effect=OSError("disk")):
            intake._settle_tickets(self.rec("<a@x>"), True, 0)      # must not raise
        intake._settle_tickets(self.rec("<a@x>"), False, None)      # no mark: no link
        self.assertIn("ticket bookkeeping failed", (self.root / "intake.log").read_text())

    def test_intake_settles_after_both_the_answer_and_the_resend_attempt(self):
        src = Path(intake.__file__).read_text()
        self.assertIn('_settle_tickets(rec, bool(solved.get("answered")), mark)', src)
        self.assertIn('_settle_tickets(rec, bool(solved.get("resent")), mark)', src)


if __name__ == "__main__":
    unittest.main()
