"""S517: exercise the real entry point with isolated files and no network."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cloudflared_update as updater
import job_status


class MonitorTest(unittest.TestCase):
    def test_entry_point_records_results_but_not_checks(self):
        for result, code, check, expected in (
            ("current", 0, False, True),
            ("rolled-back", 1, False, False),
            ("deferred", 0, False, False),
            ("current", 0, True, None),
        ):
            with self.subTest(result=result, check=check), tempfile.TemporaryDirectory() as tmp:
                with patch.object(updater, "LOGS", Path(tmp)), \
                     patch.object(updater.os.path, "exists", return_value=True), \
                     patch.object(updater, "Live"), \
                     patch.object(updater, "run", return_value=(code, {"result": result})), \
                     patch.object(job_status, "record") as record, \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(updater.main(["--check"] if check else []), code)
                if expected is None:
                    record.assert_not_called()
                else:
                    record.assert_called_once_with("cloudflaredupdate", ok=expected, note=result)
        self.assertEqual(job_status.CADENCE_H["cloudflaredupdate"], 26)


if __name__ == "__main__":
    unittest.main()
