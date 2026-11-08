"""S588: hardware RAM total and vm_stat units must survive tool dispatch."""
import subprocess
import unittest
from unittest.mock import patch

from tools import system_tools


def vm_sample(page=16384):
    # Same physical usage on 4 KiB and 16 KiB machines; only counts differ.
    counts = {"Pages active": 12, "Pages wired down": 3, "Pages free": 10,
              "Pages inactive": 37, "Pages speculative": 1,
              "Pages occupied by compressor": 1}
    return (f"Mach Virtual Memory Statistics: (page size of {page} bytes)\n"
            + "\n".join(f"{key}: {gib * 2**30 // page}."
                        for key, gib in counts.items())
            + "\nPages stored in compressor: 99999999.\n")


class SystemMemoryTests(unittest.TestCase):
    def health(self, vm=None, total="68719476736", fail=None):
        def run(argv, **kwargs):
            if argv[0] == fail:
                raise subprocess.CalledProcessError(1, argv)
            output = {"vm_stat": vm_sample() if vm is None else vm,
                      "sysctl": total, "uptime": "up 1 day"}[argv[0]]
            return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

        with patch.object(system_tools.subprocess, "run", side_effect=run), \
                patch.object(system_tools.shutil, "which", return_value=None):
            # Same entry point and dispatch mapping used by the /ask registry.
            return system_tools.TOOL_FUNCTIONS["check_system_health"]()

    def test_apple_silicon_reports_hardware_total_and_correct_units(self):
        answer = self.health()
        self.assertIn("Installed RAM: 64.0 GiB (68719476736 bytes)", answer)
        for value in ("12.0 GiB active", "3.0 GiB wired", "10.0 GiB free",
                      "37.0 GiB inactive", "1.0 GiB speculative",
                      "1.0 GiB compressor physical"):
            self.assertIn(value, answer)
        self.assertIn("not the installed RAM total", answer)

    def test_intel_page_counts_produce_same_physical_usage(self):
        self.assertEqual(system_tools._format_vm_memory(vm_sample(4096)),
                         system_tools._format_vm_memory(vm_sample(16384)))

    def test_installed_ram_not_hardcoded_or_sum_of_counters(self):
        self.assertIn("Installed RAM: 32.0 GiB", self.health(total=str(32 * 2**30)))

    def test_missing_or_invalid_page_size_is_not_assumed(self):
        for vm in (vm_sample().replace("16384", "0"),
                   vm_sample().split("\n", 1)[1], ""):
            with self.subTest(vm=vm[:40]):
                answer = self.health(vm=vm)
                self.assertIn("Installed RAM: 64.0 GiB", answer)
                self.assertIn("Memory snapshot: unknown", answer)

    def test_missing_counter_is_not_reported_as_zero(self):
        answer = self.health(vm=vm_sample().replace("Pages active:", "Absent:"))
        self.assertIn("Memory snapshot: unknown", answer)
        self.assertNotIn("0.0 GiB active", answer)

    def test_vm_command_failure_preserves_hardware_total(self):
        answer = self.health(fail="vm_stat")
        self.assertIn("Installed RAM: 64.0 GiB", answer)
        self.assertIn("Memory snapshot: unknown", answer)

    def test_failed_hardware_query_never_infers_total_from_usage(self):
        answer = self.health(fail="sysctl")
        self.assertIn("Installed RAM: unknown", answer)
        self.assertIn("12.0 GiB active", answer)

    def test_invalid_hardware_total_is_unknown(self):
        for total in ("", "not a number", "0", "-1"):
            with self.subTest(total=total):
                self.assertIn("Installed RAM: unknown", self.health(total=total))


if __name__ == "__main__":
    unittest.main()
