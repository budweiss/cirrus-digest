"""No network or notifications: exercise actual bot check with synthetic time."""
import ast
from datetime import datetime, timedelta
import json
import re
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

    def run_bot(self, window, age=120, hb_status="ok", beats=None, held=False):
        now = datetime.now().replace(hour=12)
        class Clock(datetime):
            @classmethod
            def now(cls):
                return now
        calls, holds = [], []
        hb = {"ts": (now-timedelta(minutes=age)).isoformat(), "status": hb_status}
        scope = {"time": types.SimpleNamespace(time=lambda: now.timestamp()), "re": re,
                 "BOT_TOKEN": "x", "_hold": lambda text, tok, job=None: holds.append((job, text)) or held,
                 "datetime": Clock, "HB_CHECK_SEC": 0, "HB_STALE_MIN": 90,
                 "HB_QUIET_START": 22, "HB_QUIET_END": 8, "ALLOWED_ID": 1,
                 "_hb_watch": {"last_check": 0, "stale_alerted": False, "last_reported_ts": ""},
                 "_load_heartbeat": lambda src: hb,
                 "macbook_availability": lambda: window,
                 "macbook_overdue": overdue,
                 "clear_returned_window": lambda expected: None,
                 "send_message": lambda *args: calls.append(args),
                 "send_alert": lambda *args: calls.append(args)}   # S592: alerts go via send_alert
        tree = ast.parse(Path(__file__).with_name("cirrus_bot.py").read_text())
        fns = [x for x in tree.body if isinstance(x, ast.FunctionDef)
               and x.name in ("check_heartbeats", "_hb_key")]
        exec(compile(ast.Module(body=fns, type_ignores=[]), "bot-check", "exec"), scope)
        if beats is not None:                # a series of fresh heartbeats, one check each
            for i, (st, note) in enumerate(beats):
                hb.clear()
                hb.update({"ts": (now-timedelta(minutes=1, seconds=len(beats)-i)).isoformat(),
                           "status": st, "note": note})
                scope["check_heartbeats"]()
            self.holds = holds
            return calls, now
        scope["check_heartbeats"]()
        scope["check_heartbeats"]()  # expected warnings must deduplicate
        return calls, now

    def test_same_selfheal_report_is_sent_once(self):
        """S685: 11 identical reports on 11-25, one per 30-minute heartbeat."""
        same = ("com.cowork.callbacks: receipt/work check needs review — NOT REPAIRED; "
                "routes: cirrus-cf=open cirrus-lan=shut; claude sessions 3")
        moved = same.replace("sessions 3", "sessions 4").replace("lan=shut", "lan=open")
        other = "com.cowork.runner: last exit code 1 — NOT REPAIRED"
        calls, _ = self.run_bot({"mode": "online"}, beats=[
            ("degraded", same), ("degraded", moved), ("degraded", same),
            ("degraded", other), ("ok", ""), ("degraded", same)])
        self.assertEqual(len(calls), 3)     # first, a different problem, again after an ok
        self.assertEqual([j for j, _ in self.holds], ["macbook_selfheal"] * 3)
        calls, _ = self.run_bot({"mode": "online"}, beats=[("degraded", same)], held=True)
        self.assertEqual(calls, [])          # held for Telewarden: not sent

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
