"""S415: delivery regressions; all mail, status and feeds are offline fakes."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import aggie_yt_digest as digest


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "yt-watch").mkdir()
        (self.root / "yt-watch/aggie_intro_note.md").write_text(
            "# Internal template note\n# Do not send these notes\n\nYour brief is ready.\n")
        self.state = self.root / "state.json"
        self.state.write_text(json.dumps({"intro_sent": "2026-10-01"}))
        self.creds = self.root / "creds.json"
        self.creds.write_text(json.dumps({"outlook_email": "fixture@example.invalid",
                                         "outlook_password": "EXAMPLE_PLACEHOLDER"}))
        self.sec = {"channel": "Fixture", "title": "Example", "published": "2026-10-01",
                    "url": "https://www.youtube.com/watch?v=fixture0001", "claims": ["Presenter reports: example"],
                    "extract_error": "", "no_transcript": ""}
        self.mail = types.SimpleNamespace(send=Mock(return_value=True))
        self.status = types.SimpleNamespace(record=Mock())
        patches = [patch.dict("sys.modules", {"mailer": self.mail, "job_status": self.status}),
                   patch.object(digest, "PROJECT_DIR", self.root),
                   patch.object(digest, "STATE_PATH", self.state),
                   patch.object(digest, "CREDS_PATH", self.creds),
                   patch.object(digest, "aggie_channels", return_value=[{"name": "Fixture", "channel_id": "fixture"}]),
                   patch.object(digest, "findings_files", return_value=[]),
                   patch.object(digest, "reviewed_summaries", return_value={self.sec["url"]: self.sec}),
                   patch.object(digest, "newest_video", return_value={"video_id": "fixture0001", "title": "Example",
                               "published": "2026-10-01", "url": self.sec["url"]}),
                   patch.object(digest, "log")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_refused_send_keeps_cursor_and_records_failure(self):
        before = self.state.read_bytes()
        self.mail.send.return_value = False
        self.assertEqual(digest.work(False), 3)
        self.assertEqual(self.state.read_bytes(), before)
        self.status.record.assert_called_once_with("aggieyt", False, "digest send failed")

    def test_raised_send_keeps_cursor(self):
        before = self.state.read_bytes()
        self.mail.send.side_effect = RuntimeError("fixture failure")
        with self.assertRaises(RuntimeError):
            digest.work(False)
        self.assertEqual(self.state.read_bytes(), before)
        self.status.record.assert_called_once_with('aggieyt', False, 'digest send raised; cursor preserved')

    def test_confirmed_send_advances_cursor_and_prevents_repeat(self):
        self.assertEqual(digest.work(False), 0)
        self.assertEqual(json.loads(self.state.read_text())["sent"]["Fixture"]["video_id"], "fixture0001")
        self.assertEqual(digest.work(False), 0)
        self.mail.send.assert_called_once()

    def test_failed_extraction_waits_and_sends_when_repaired(self):
        before = self.state.read_bytes()
        self.sec["extract_error"] = "fixture extraction failed"
        self.assertEqual(digest.work(False), 1)
        self.mail.send.assert_not_called()
        self.assertEqual(self.state.read_bytes(), before)
        self.sec["extract_error"] = ""
        self.assertEqual(digest.work(False), 0)
        self.mail.send.assert_called_once()

    def test_intro_preview_matches_sent_body_without_template_notes(self):
        self.state.write_text("{}")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(digest.work(True), 0)
        self.mail.send.assert_not_called()
        self.assertEqual(self.state.read_text(), "{}")
        self.assertIn("Your brief is ready.", output.getvalue())
        self.assertNotIn("Internal template", output.getvalue())
        self.assertEqual(digest.work(False), 0)
        sent_body = self.mail.send.call_args.args[4]
        self.assertIn(sent_body, output.getvalue())

    def test_cli_requires_explicit_live_flag(self):
        for args, dry, backfill in [([], True, False), (["--live"], False, False),
                                    (["--backfill"], True, True),
                                    (["--backfill", "--live"], False, True)]:
            with self.subTest(args=args), patch.object(digest, "work", return_value=0) as work:
                digest.main(args)
                work.assert_called_once_with(dry, backfill=backfill)


if __name__ == "__main__":
    unittest.main()
