import json
import tempfile
import unittest
from pathlib import Path
import schedule_run_audit as A


class AuditChecks(unittest.TestCase):
    def test_private_findings_and_offending_figures_never_leave_audit(self):
        marker = 'PRIVATE_FINANCIAL_SENTINEL'
        checks = [{'passed': False, 'issues': [marker]}, {'error_type': marker}]
        result = A.summary(checks)
        self.assertEqual(result['issue_counts'], [1, 0])
        self.assertEqual(result['error_count'], 1)
        data = {'status': 'preview', 'opinion': marker, 'offenders': [marker],
                'holdings': marker, 'error_type': marker, 'body_sha256': marker,
                'started_at': marker, 'export_stale': marker}
        self.assertNotIn(marker, json.dumps([result, A.summary(data)]))
        self.assertEqual(A.summary(data)['offender_count'], 1)

    def test_stale_code_evidence_is_identified_and_reports_are_not_read(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'personal_schedule.py').write_text('current code')
            folder = root / 'private/personal-holdings/runs/2026-10-15'
            folder.mkdir(parents=True)
            (folder / 'preview-result.json').write_text(json.dumps({'status': 'preview', 'source_sha256': 'a' * 64}))
            (folder / 'report.md').write_bytes(b'\xffPRIVATE_REPORT_NOT_JSON')
            result = A.audit('personal', date='2026-10-15', root=root)
            self.assertFalse(result['files']['preview-result.json']['source_matches_current'])
            self.assertNotIn('report.md', result['files'])

    def test_invalid_paths_and_missing_runs_are_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertFalse(A.audit('stock', 'am', root=root)['run_exists'])
            for date in ('../../secrets', '2026-10-99', '2026-1-1'):
                with self.assertRaises(ValueError): A.audit('personal', date=date, root=root)
            with self.assertRaises(ValueError): A.audit('stock', '../private', root=root)


if __name__ == '__main__':
    unittest.main()
