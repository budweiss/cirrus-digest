"""No network or notifications: exercise actual bot check with synthetic time."""
import ast
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import types
import unittest
from macbook_availability import change, status, overdue


class AvailabilityTests(unittest.TestCase):
    def test_deadline_online_and_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"state.json"
            for hours in (0, -1, float("nan"), float("inf"), 169):
                with self.assertRaises(ValueError):
                    change("offline", hours, p, 1000)
            self.assertFalse(p.exists())
            row = change("offline", 8, p, 1000)
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertEqual(status(p, row["until"]-1)["mode"], "offline")
            expired = status(p, row["until"])
            self.assertTrue(overdue(expired, 1000, row["until"]))
            self.assertFalse(overdue(expired, row["until"], row["until"]))
            change("online", path=p, now=1100)
            self.assertEqual(status(p)["mode"], "online")
            p.write_text('{"started": 0, "until": 1e99}')
            self.assertEqual(status(p)["mode"], "invalid")

    def run_bot(self, window, age=120, hb_status="ok"):
        now = datetime.now().replace(hour=12)
        class Clock(datetime):
            @classmethod
            def now(cls):
                return now
        calls = []
        hb = {"ts": (now-timedelta(minutes=age)).isoformat(), "status": hb_status}
        scope = {"time": types.SimpleNamespace(time=lambda: now.timestamp()),
                 "datetime": Clock, "HB_CHECK_SEC": 0, "HB_STALE_MIN": 90,
                 "HB_QUIET_START": 22, "HB_QUIET_END": 8, "ALLOWED_ID": 1,
                 "_hb_watch": {"last_check": 0, "stale_alerted": False, "last_reported_ts": ""},
                 "_load_heartbeat": lambda src: hb,
                 "macbook_availability": lambda: window,
                 "macbook_overdue": overdue,
                 "clear_returned_window": lambda expected: None,
                 "send_message": lambda *args: calls.append(args)}
        tree = ast.parse(Path(__file__).with_name("cirrus_bot.py").read_text())
        fn = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == "check_heartbeats")
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "bot-check", "exec"), scope)
        scope["check_heartbeats"]()
        scope["check_heartbeats"]()  # expected warnings must deduplicate
        return calls, now

    def test_suppression_is_narrow_and_expiry_beats_sleep(self):
        self.assertEqual(self.run_bot({"mode": "offline"})[0], [])
        self.assertEqual(len(self.run_bot({"mode": "online"})[0]), 1)
        self.assertEqual(len(self.run_bot({"mode": "invalid"})[0]), 1)
        self.assertEqual(len(self.run_bot({"mode": "offline"}, age=1, hb_status="degraded")[0]), 1)
        now = datetime.now().replace(hour=12).timestamp()
        calls, _ = self.run_bot({"mode": "expired", "until": now-60}, hb_status="sleeping")
        self.assertEqual(len(calls), 1)
        self.assertIn("expired", calls[0][1])
        self.assertEqual(self.run_bot({"mode": "expired", "until": now-120}, age=1)[0], [])


if __name__ == "__main__":
    unittest.main()
