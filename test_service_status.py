"""S654: /ask check_service_status must read the right service domain on each box.

`launchctl list <label>` asks the caller's own domain, so a shell-driven run on
CIRRUS called every com.cirrus.* LaunchDaemon "not loaded", and CUMULUS1 (no
launchctl) only ever returned an error. Fake command output only (T32): no
live launchctl or systemctl runs here.
"""
import subprocess
import unittest
from unittest.mock import patch

from tools import system_tools

PRINT_RUNNING = """system/com.cirrus.bot = {
\tactive count = 1
\tpath = /Library/LaunchDaemons/com.cirrus.bot.plist
\ttype = LaunchDaemon
\tstate = running

\tprogram = /usr/bin/python3
\targuments = {
\t\t/usr/bin/python3
\t\t/Users/buddy/projects/cirrus-digest/cirrus_bot.py
\t}

\tdomain = system
\tusername = buddy
\truns = 6
\tpid = 45928
\tendpoints = {
\t\t"com.example" = {
\t\t\tstate = not running
\t\t}
\t}
}
"""

PRINT_INTERVAL = """system/com.cirrus.netsample = {
\tstate = not running
\truns = 799
\tlast exit code = 0
\trun interval = 300 seconds
}
"""

PRINT_SYSTEM = """system = {
\ttype = System
\tservices = {
\t\t       0      0 \tcom.cirrus.personalopinion
\t\t   42253    -15 \tcom.cirrus.api
\t\t     812      0 \tcom.apple.Finder
\t\t       0      1 \tcom.cirrus.stockpm
\t\t   45928    -15 \tcom.cirrus.bot
\t}

\tdisabled services = {
\t\t"com.cirrus.ytwatch" => enabled
\t}
}
"""

SHOW_RUNNING = ("LoadState=loaded\nActiveState=active\nSubState=running\n"
                "MainPID=515587\nResult=success\n")
SHOW_ONESHOT = ("LoadState=loaded\nActiveState=inactive\nSubState=dead\n"
                "MainPID=0\nResult=success\n")
SHOW_TIMER = ("LoadState=loaded\nActiveState=active\nSubState=waiting\n"
              "MainPID=0\nResult=success\n")
SHOW_MISSING = ("LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
                "MainPID=0\nResult=success\n")

UID = 4242  # fake uid; the gui domain is built from os.getuid()
GUI = f"gui/{UID}"

LIST_UNITS = (
    "cirrus-api.service                  loaded active   running cirrus-api (beta)\n"
    "cirrus-billsnow.service             loaded inactive dead    cirrus-billsnow (beta)\n"
    "cirrus-bot.service                  loaded active   running cirrus-bot (beta)\n"
    "cirrus-billsnow.timer               loaded active   waiting cirrus-billsnow weekly\n"
)


class ServiceStatusTests(unittest.TestCase):
    def call(self, outputs, tool, service_name=None):
        """outputs maps a tuple argv -> (rc, stdout); anything else is rc 113."""
        calls = []

        def run(argv, **kwargs):
            calls.append(list(argv))
            rc, out = outputs.get(tuple(argv), (113, ""))
            return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

        which = (lambda cmd: "/bin/" + cmd if cmd == tool else None)
        args = {} if service_name is None else {"service_name": service_name}
        with patch.object(system_tools.subprocess, "run", side_effect=run), \
                patch.object(system_tools.shutil, "which", side_effect=which), \
                patch.object(system_tools.os, "getuid", return_value=UID):
            # Same entry point and dispatch mapping used by the /ask registry.
            answer = system_tools.TOOL_FUNCTIONS["check_service_status"](**args)
        # registry.call_tool logs any answer containing "unknown" as an error.
        self.assertNotIn("unknown", answer.lower())
        self.assertFalse([c for c in calls if c[:2] == ["launchctl", "list"]],
                         "launchctl list asks the caller's domain (T33)")
        return answer, calls

    # ── CIRRUS: launchd ──────────────────────────────────────────────────────

    def test_running_daemon_read_from_system_domain(self):
        answer, calls = self.call(
            {("launchctl", "print", "system/com.cirrus.bot"): (0, PRINT_RUNNING)},
            "launchctl", "com.cirrus.bot")
        self.assertEqual(calls, [["launchctl", "print", "system/com.cirrus.bot"]])
        self.assertIn("running (PID: 45928)", answer)
        self.assertNotIn("not", answer)

    def test_interval_job_between_runs_is_loaded_not_missing(self):
        answer, _ = self.call(
            {("launchctl", "print", "system/com.cirrus.netsample"): (0, PRINT_INTERVAL)},
            "launchctl", "com.cirrus.netsample")
        self.assertIn("loaded, not running right now", answer)
        self.assertIn("last exit code = 0", answer)
        self.assertIn("between runs", answer)

    def test_gui_agent_found_when_not_a_daemon(self):
        answer, calls = self.call(
            {("launchctl", "print", f"{GUI}/com.cirrus.agent"): (0, PRINT_RUNNING)},
            "launchctl", "com.cirrus.agent")
        self.assertEqual([c[2] for c in calls],
                         ["system/com.cirrus.agent", f"{GUI}/com.cirrus.agent"])
        self.assertIn(f"({GUI}/com.cirrus.agent): running", answer)

    def test_missing_label_names_the_domains_inspected(self):
        answer, _ = self.call({}, "launchctl", "com.cirrus.nosuch")
        self.assertIn(f"not loaded in the system or {GUI} launchd domain", answer)

    def test_list_reads_system_services_block_only(self):
        answer, calls = self.call(
            {("launchctl", "print", "system"): (0, PRINT_SYSTEM)}, "launchctl")
        self.assertEqual(calls, [["launchctl", "print", "system"]])
        rows = answer.split("PID\tLastExit\tLabel\n", 1)[1].splitlines()
        self.assertEqual(rows, ["42253\t-15\tcom.cirrus.api",
                                "45928\t-15\tcom.cirrus.bot",
                                "-\t0\tcom.cirrus.personalopinion",
                                "-\t1\tcom.cirrus.stockpm"])
        self.assertNotIn("Finder", answer)
        self.assertNotIn("ytwatch", answer)  # disabled-services block

    def test_empty_list_says_what_it_inspected(self):
        answer, _ = self.call({}, "launchctl")
        self.assertIn("No com.cirrus.* services found in `launchctl print system`", answer)
        self.assertIn("exit 113", answer)

    # ── CUMULUS1: systemd ────────────────────────────────────────────────────

    def show(self, unit):
        return ("systemctl", "show", unit, "--no-pager",
                "-p", "LoadState,ActiveState,SubState,MainPID,Result")

    def test_launchd_label_maps_to_systemd_unit(self):
        answer, calls = self.call({self.show("cirrus-bot.service"): (0, SHOW_RUNNING)},
                                  "systemctl", "com.cirrus.bot")
        self.assertEqual(calls, [list(self.show("cirrus-bot.service"))])
        self.assertEqual(answer, "Service 'cirrus-bot.service' (asked as "
                                 "'com.cirrus.bot'): running (PID: 515587)")

    def test_unit_name_passes_through(self):
        answer, _ = self.call({self.show("cirrus-bot.service"): (0, SHOW_RUNNING)},
                              "systemctl", "cirrus-bot.service")
        self.assertEqual(answer, "Service 'cirrus-bot.service': running (PID: 515587)")

    def test_oneshot_between_runs_and_waiting_timer(self):
        answer, _ = self.call({self.show("cirrus-billsnow.service"): (0, SHOW_ONESHOT)},
                              "systemctl", "cirrus-billsnow")
        self.assertIn("loaded, not running right now (inactive (dead)", answer)
        answer, _ = self.call({self.show("cirrus-billsnow.timer"): (0, SHOW_TIMER)},
                              "systemctl", "cirrus-billsnow.timer")
        self.assertIn("active (waiting)", answer)

    def test_missing_unit_is_not_loaded(self):
        answer, _ = self.call({self.show("cirrus-nosuch.service"): (0, SHOW_MISSING)},
                              "systemctl", "cirrus-nosuch")
        self.assertIn("is not loaded in systemd", answer)

    def test_unit_list(self):
        argv = ("systemctl", "list-units", "--all", "--no-legend", "--plain",
                "--no-pager", "cirrus-*", "cumulus-*")
        answer, _ = self.call({argv: (0, LIST_UNITS)}, "systemctl")
        rows = answer.split("Unit\tLoad\tActive\tSub\n", 1)[1].splitlines()
        self.assertEqual(rows[2], "cirrus-bot.service\tloaded\tactive\trunning")
        self.assertEqual(rows[1], "cirrus-billsnow.service\tloaded\tinactive\tdead")
        self.assertEqual(len(rows), 4)

    def test_neither_tool_present(self):
        answer, calls = self.call({}, "nothing", "com.cirrus.bot")
        self.assertEqual(calls, [])
        self.assertIn("neither launchctl nor systemctl", answer)


if __name__ == "__main__":
    unittest.main()
