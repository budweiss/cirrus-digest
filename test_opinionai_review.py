"""Offline tests of identity, evidence review and duplicate-send protection."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import opinionai_review as r


POST = {'id': 1, 'title': 'A local tool', 'subtitle': 'A test candidate',
        'truncated_body_text': 'Public teaser only.', 'post_date': '2026-10-13T12:00:00Z',
        'canonical_url': 'https://opinionai.substack.com/p/example'}


class OpinionTests(unittest.TestCase):
    def test_missing_or_unknown_post_is_rejected(self):
        for data in [{'items': []}, {'items': [{'index': 1, 'verdict': 'ACT', 'reason': 'Try it.'}]},
                     {'items': [{'index': False, 'verdict': 'ACT', 'reason': 'Try it.'}]}]:
            with self.assertRaises(ValueError): r.validate(data, [POST])

    def test_failed_review_is_not_published_as_findings(self):
        draft = '{"items":[{"index":0,"verdict":"ACT","kind":"tool","reason":"It is free and compatible."}]}'
        caller = Mock(side_effect=[draft, '{"approved":false,"problems":["invented price"]}'] * 2)
        with self.assertRaisesRegex(ValueError, 'review_failed'): r.triage([POST], caller)
        self.assertEqual(caller.call_count, 4)

    def test_technique_cannot_be_counted_as_installable_tool_trial(self):
        data = {'items': [{'index': 0, 'verdict': 'TRIAL', 'kind': 'technique',
                          'reason': 'Use evaluation driven development.'}]}
        with self.assertRaisesRegex(ValueError, 'requires_a_tool'):
            r.validate(data, [POST])

    def test_tool_name_alone_cannot_prove_installability(self):
        data = {'items': [{'index': 0, 'verdict': 'TRIAL', 'kind': 'tool',
                          'reason': 'Try the tool.', 'trial_evidence': POST['subtitle']}]}
        with self.assertRaisesRegex(ValueError, 'exact_metadata_evidence'):
            r.validate(data, [POST])
        post = dict(POST, subtitle='An open-source library for evaluation.')
        data['items'][0]['trial_evidence'] = post['subtitle']
        self.assertEqual(r.validate(data, [post])[0]['verdict'], 'TRIAL')

    def test_success_records_send_once_and_keeps_both_recipients(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(r, 'fetch_posts', return_value=([], {})), \
                patch.object(r.sender, 'CREDS_PATH', Path(tmp)/'creds'), \
                patch.object(r.sender, 'build_message', return_value=(None, 'subject', r.sender.TO_EMAILS)), \
                patch.object(r.sender, 'send_live', return_value=True) as send:
            (Path(tmp)/'creds').write_text('{}')
            at = datetime(2026, 10, 14, 14, tzinfo=timezone.utc)
            self.assertEqual(r.run(True, Path(tmp), at)['recipients'], 2)
            self.assertEqual(r.run(True, Path(tmp), at)['status'], 'already_sent')
            send.assert_called_once()
            self.assertEqual(send.call_args.args[3], r.sender.TO_EMAILS)

    def test_unknown_smtp_outcome_never_blindly_retries(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(r, 'fetch_posts', return_value=([], {})), \
                patch.object(r.sender, 'CREDS_PATH', Path(tmp)/'creds'), \
                patch.object(r.sender, 'build_message', return_value=(None, 'subject', r.sender.TO_EMAILS)), \
                patch.object(r.sender, 'send_live', side_effect=RuntimeError('unknown')) as send:
            (Path(tmp)/'creds').write_text('{}')
            at = datetime(2026, 10, 14, 14, tzinfo=timezone.utc)
            with self.assertRaisesRegex(RuntimeError, 'unknown'): r.run(True, Path(tmp), at)
            with self.assertRaisesRegex(RuntimeError, 'uncertain'): r.run(True, Path(tmp), at)
            send.assert_called_once()

    def test_prior_coverage_is_skipped_but_preview_does_not_consume_new_posts(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(r, 'fetch_posts', return_value=([POST], {'scanned': 20, 'in_window': 1})), \
                patch.object(r, 'triage', return_value=[]) as triage:
            home = Path(tmp)
            r.media.atomic_json(home / 'prior.receipt.json', {'status': 'sent', 'posts': [POST['canonical_url']]})
            result = r.run(False, home)
            self.assertEqual(result['posts'], 0)
            triage.assert_called_once_with([])
            self.assertEqual(len(list(home.glob('*.receipt.json'))), 1)

    def test_failed_triage_keeps_drafts_and_findings_in_private_evidence(self):
        # S497 P4(a): the 10-25 failure left only "ValueError" behind.
        import json
        triage_json = json.dumps({'items': [{'index': 0, 'verdict': 'SKIP', 'kind': 'other',
                                             'reason': 'Not useful for current lanes.', 'trial_evidence': ''}]})
        finding = {'approved': False, 'problems': ['SKIP ignores a useful technique.']}
        caller = Mock(side_effect=lambda system, text, task: triage_json if task.endswith('triage') else json.dumps(finding))
        with self.assertRaisesRegex(ValueError, '^triage_review_failed$') as ctx:
            r.triage([POST], caller=caller)
        self.assertEqual([a['review'] for a in ctx.exception.attempts], [finding, finding])
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(r, 'fetch_posts', return_value=([POST], {})), \
                patch.object(r, 'triage', side_effect=ctx.exception):
            r.run(False, Path(tmp))
            saved = json.loads(next(Path(tmp).glob('*-preview-evidence.json')).read_text())
        self.assertEqual(saved['failure'], 'ValueError')
        self.assertEqual(saved['failure_detail'], 'triage_review_failed')
        self.assertEqual(saved['review_attempts'][1]['review'], finding)

    def test_preview_failed_review_does_not_claim_actionable_findings(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(r, 'fetch_posts', return_value=([POST], {})), \
                patch.object(r, 'triage', side_effect=ValueError('review_failed')):
            result = r.run(False, Path(tmp))
            self.assertEqual(result['failure'], 'ValueError')
            self.assertIn('lane DOWN', Path(result['path']).read_text())
            self.assertEqual(list(Path(tmp).glob('*.receipt.json')), [])

    def test_dispatch_does_not_claim_one_recipient_is_success(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp, patch.object(r, 'REVIEW_DIR', Path(tmp)), \
                patch.object(r.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0,
                             '{"status":"sent","recipients":1}', '')), \
                patch('job_status.record') as record, patch('vendor_mail_watch.telegram', return_value=True) as notify:
            result = r.dispatch(True)
            self.assertEqual(result['status'], 'failed')
            self.assertFalse(record.call_args.args[1])
            self.assertIn('needs attention', notify.call_args.args[0])

    def test_failed_dispatch_keeps_reason_and_remote_delivery_status(self):
        # S497: 10-25 recorded only "RuntimeError" for a run whose email WAS
        # accepted by both recipients (the remote triage review had failed).
        # Proving that took a server receipt; the ledger must say it directly.
        import json, subprocess
        with tempfile.TemporaryDirectory() as tmp, patch.object(r, 'REVIEW_DIR', Path(tmp)), \
                patch.object(r.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1,
                             '{"status":"sent","recipients":2,"failure":"ValueError"}', '')), \
                patch('job_status.record') as record, patch('vendor_mail_watch.telegram', return_value=True):
            result = r.dispatch(True)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['detail'], 'remote_run_failed')
            self.assertEqual(result['remote'], {'status': 'sent', 'recipients': 2, 'failure': 'ValueError'})
            note = json.loads(record.call_args_list[0].args[2])
            self.assertFalse(record.call_args_list[0].args[1])
            self.assertEqual((note['detail'], note['remote']['recipients']), ('remote_run_failed', 2))

    def test_duplicate_dispatch_is_quiet_and_keeps_confirmed_delivery(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp, patch.object(r, 'REVIEW_DIR', Path(tmp)), \
                patch.object(r.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0,
                             '{"status":"already_sent","recipients":2}', '')), \
                patch('job_status.record') as record, patch('vendor_mail_watch.telegram') as notify:
            self.assertEqual(r.dispatch(True)['status'], 'already_sent')
            self.assertTrue(record.call_args.args[1])
            notify.assert_not_called()


if __name__ == '__main__': unittest.main()
