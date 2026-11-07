"""S554: a held answer's Telegram names what is needed and carries no mail text."""
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import answer_workflow
import held_label
import intake
import intake_lifecycle as life


class LabelTests(unittest.TestCase):
    def test_only_the_exact_categories_survive(self):
        for raw, want in (('{"category": "web_research"}', "web_research"),
                          ('```json\n{"category": "new_report"}\n```', "new_report"),
                          ('{"category": "review"}', "review"),
                          ('{"category": "send it now"}', "review"),
                          ('{"category": ["web_research"]}', "review"),
                          ('["web_research"]', "review"),
                          ('web_research', "review"),
                          ('', "review"), (None, "review")):
            self.assertEqual(held_label.parse(raw), want, raw)

    def test_a_dead_model_reads_needs_review_and_does_not_raise(self):
        def dead(*a, **k):
            raise RuntimeError("connection refused")
        self.assertEqual(held_label.label("s", "b", {}, call=dead), "review")

    def test_a_reply_that_will_not_parse_never_reaches_another_provider(self):
        calls = []

        def garbage(provider, *a, **k):
            calls.append((provider, k))
            return "not json"
        self.assertEqual(held_label.label("s", "b", {}, call=garbage), "review")
        self.assertEqual([c[0] for c in calls], ["ollama"])
        self.assertEqual(calls[0][1]["retries"], 0)
        self.assertEqual(calls[0][1]["privacy"], "LOCAL_ONLY")

    def test_input_is_bounded(self):
        seen = []

        def spy(provider, system, user, creds, **k):
            seen.append(user)
            return '{"category": "review"}'
        held_label.label("s", "x" * 50000, {}, call=spy)
        self.assertLess(len(seen[0]), held_label.MAX_INPUT_CHARS + 50)

    def test_wording_is_fixed_and_unknown_is_review(self):
        self.assertIn("no job started", held_label.wording("web_research"))
        self.assertIn("your decision", held_label.wording("new_report"))
        self.assertEqual(held_label.wording("anything else"), "needs review")


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.row = (1, "client@example.test", "Question", "full body", "<stable@example.test>")
        life.begin(self.root, self.row)

    def test_the_notice_says_what_is_needed(self):
        life.finish(self.root, self.row, {"kind": "answer", "requester": "bill", "held_label": "new_report"})
        hit = life.summary(self.root)["attention"][0]
        self.assertEqual((hit["state"], hit["owner"]), ("blocked_on_evidence", "cowork"))
        self.assertIn("your decision", hit["next_action"])
        self.assertIn("preserve budget", hit["next_action"])
        self.assertNotIn("full body", life.notice(self.root))

    def test_an_unlabelled_hold_keeps_the_old_action(self):
        life.finish(self.root, self.row, {"kind": "answer", "requester": "bill"})
        self.assertEqual(life.summary(self.root)["attention"][0]["next_action"],
                         "review saved attempt; preserve budget and send uncertainty")


class PollTests(unittest.TestCase):
    def poll(self, solved):
        """One intake pass over a fixture answer request; returns (Telegram text, label calls)."""
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config/answer-workflow-intake.enabled").touch()
            for name, value in [("PROJECT_DIR", root), ("FAST_POLL_STATE_PATH", root / "fast")]:
                stack.enter_context(patch.object(intake, name, value))
            entry = {"name": "fixture", "projects": ["general"], "limit": 10,
                     "request_kind": "answer", "require_prefix": False}
            stack.enter_context(patch.object(intake, "load_allowlist", return_value={"fixture@example.test": entry}))
            stack.enter_context(patch("runtime_config.load_sources", return_value={}))
            stack.enter_context(patch.object(intake, "find_account", return_value={"credential_key": "password"}))
            stack.enter_context(patch.object(intake, "load_json", return_value=dict(password="synthetic")))
            stack.enter_context(patch.object(intake, "load_state", return_value={}))
            for name in ("save_state", "_record_status", "append_backlog", "telegram", "log", "_settle_tickets"):
                stack.enter_context(patch.object(intake, name, return_value=0))
            notify = stack.enter_context(patch.object(intake, "_notify", return_value=0))
            stack.enter_context(patch.object(intake, "is_confirmation", return_value=[]))
            stack.enter_context(patch.object(intake.dev_loop, "ledger_append"))
            stack.enter_context(patch.object(life, "coordinate", return_value=True))
            row = (1, "fixture@example.test", "Permit", "QUOTED-MAIL-TEXT what does the county permit cost?", "<fixture>")
            stack.enter_context(patch.object(intake, "scan_inbox", return_value=([row], [])))
            stack.enter_context(patch.object(intake, "under_limit", return_value=True))
            stack.enter_context(patch.object(intake, "send_ack", return_value=True))
            stack.enter_context(patch.object(subprocess, "Popen"))
            stack.enter_context(patch.object(answer_workflow, "intake_record"))
            stack.enter_context(patch.object(answer_workflow, "solve_and_answer", return_value=solved))
            stack.enter_context(patch("intake_delivery_evidence.verify", return_value=None))
            label = stack.enter_context(patch.object(held_label, "label", return_value="web_research"))
            self.assertEqual(intake.run(), 0)
            sent = "\n".join(str(c.args[0]) for c in notify.call_args_list if c.args[0])
            return sent, life.summary(root)["attention"], label

    def test_a_held_answer_tells_buddy_the_category_and_nothing_private(self):
        text, attention, label = self.poll({"answered": False, "request_id": "abc123",
                                            "reason": "INTERNAL-REASON quoting the draft"})
        self.assertIn("Answer held: needs web research", text)
        self.assertIn("abc123", text)
        self.assertNotIn("INTERNAL-REASON", text)
        self.assertNotIn("-review.json", text)
        self.assertNotIn("QUOTED-MAIL-TEXT", text)
        self.assertIn("needs web research", attention[0]["next_action"])
        label.assert_called_once()
        self.assertIn("QUOTED-MAIL-TEXT", label.call_args.args[1])

    def test_a_delivered_answer_is_not_labelled(self):
        text, _, label = self.poll({"answered": True, "request_id": "abc123", "reason": "ok"})
        self.assertIn("Answer delivered", text)
        label.assert_not_called()


if __name__ == "__main__":
    unittest.main()
