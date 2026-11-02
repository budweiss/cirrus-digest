"""Synthetic delivery and report guards; no network, real mail, or credentials."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stratus import stratus_monthly as S
import mailer
import vendor_mail_watch
import llm_providers

ENTRY = ('### 2026-10-14\n\n- No fresh source establishes a new purchase choice. '
         'The existing hardware notes remain dated background and creator claims are '
         'not our measured benchmarks. Continue waiting for verified specifications '
         'and configured pricing before considering a purchase.\n\nRecommendation: unchanged.')


class MonthlyTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            root = Path(temp)
            for key, val in {'STATE': root/'state', 'LOG': root/'log.md',
                             'CREDS_PATH': root/'creds.json'}.items():
                stack.enter_context(patch.object(S, key, val))
            S.LOG.write_text('# Log\n\n## Log entries (newest first)\n\n### Older entry\nKeep me.\n')
            S.CREDS_PATH.write_text(json.dumps({'outlook_email':'fixture@example.test','outlook_password':'fake'}))
            send = stack.enter_context(patch.object(mailer, 'send', return_value=True))
            stack.enter_context(patch.object(vendor_mail_watch, 'telegram', return_value=True))
            stack.enter_context(patch.object(S, 'git_push_log', return_value={'ok': True}))
            yield root, send

    def test_preview_constructs_both_recipients_without_sending_or_editing_log(self):
        with self.fixture() as (_, send):
            before = S.LOG.read_text()
            result = S.deliver(ENTRY, [], dry=True)
            self.assertEqual(result['recipients'], 2)
            self.assertEqual(S.LOG.read_text(), before)
            send.assert_not_called()

    def test_live_receipt_prevents_a_second_email(self):
        with self.fixture() as (_, send):
            self.assertEqual(S.deliver(ENTRY, [])['status'], 'sent')
            self.assertEqual(send.call_args.args[2], ['weiss_buddy@yahoo.com', 'buddy.weiss@outlook.com'])
            self.assertEqual(S.deliver(ENTRY, [])['status'], 'already_sent')
            self.assertEqual(send.call_count, 1)

    def test_uncertain_smtp_outcome_cannot_be_blindly_retried(self):
        with self.fixture() as (_, send):
            send.side_effect = OSError('synthetic transport failure')
            with self.assertRaises(OSError): S.deliver(ENTRY, [])
            self.assertEqual(json.loads((S.STATE/(S.TODAY[:7]+'.json')).read_text())['status'], 'sending')
            with self.assertRaises(RuntimeError): S.deliver(ENTRY, [])
            self.assertEqual(send.call_count, 1)

    def test_new_entry_is_first_and_history_is_preserved(self):
        with self.fixture():
            S.save_entry(ENTRY); S.save_entry(ENTRY)
            value = S.LOG.read_text()
            self.assertLess(value.index(ENTRY), value.index('### Older entry'))
            self.assertIn('Keep me.', value)
            self.assertEqual(value.count(ENTRY), 1)

    def test_email_failure_is_nonzero_and_failed_job_status(self):
        with self.fixture() as (_, send), patch.object(S, 'synthesize', return_value=(ENTRY, [], None)), \
             patch.object(S.sys, 'argv', ['stratus_monthly.py']), patch.object(S, '_rec') as rec, \
             contextlib.redirect_stdout(io.StringIO()):
            send.side_effect = OSError('synthetic')
            self.assertEqual(S.main(), 1)
            self.assertFalse(rec.call_args.args[1])

    def test_unknown_cli_argument_never_runs_research_or_send(self):
        with patch.object(S.sys, 'argv', ['stratus_monthly.py','--not-a-mode']), \
             patch.object(S, 'synthesize') as synth, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised: S.main()
            self.assertEqual(raised.exception.code, 2)
            synth.assert_not_called()

    def test_unoffered_link_is_rejected(self):
        with self.assertRaises(ValueError):
            S.validate_entry(ENTRY.replace('Continue waiting', '[Unsupported](https://example.test/) Continue waiting'), [])

    def test_current_user_decision_is_explicit_in_prompt(self):
        prompt = S.build_prompt('', [])
        self.assertIn('No order date is committed', prompt)
        self.assertIn('expensive refurbished', prompt)
        self.assertIn('creator claims, not our benchmark', prompt)

    def test_fallback_validation_failure_is_distinct_and_retained_privately(self):
        private = ENTRY.replace('Continue waiting', '[Private source](https://private.example/) Continue waiting')
        with self.fixture() as (root, send), \
             patch.object(S, 'gather_web', return_value=('', [])), \
             patch.object(S, 'learned_block', return_value=('', [])), \
             patch.object(S, 'local_entry', side_effect=ValueError('local_evidence_review_failed')), \
             patch.object(llm_providers, 'escalate', return_value=('anthropic', private)), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            entry, urls, error = S.synthesize()
            self.assertIsNone(entry)
            self.assertEqual(error, 'Report validation failed: unoffered_source')
            packet, = (root/'state/failures').glob('*.json')
            self.assertEqual(packet.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(packet.read_text())['draft'], private)
            self.assertNotIn('private.example', output.getvalue() + error)
            send.assert_not_called()

    def test_model_failure_does_not_masquerade_as_validation_or_leak_text(self):
        with self.fixture() as (_, send), \
             patch.object(S, 'gather_web', return_value=('', [])), \
             patch.object(S, 'learned_block', return_value=('', [])), \
             patch.object(S, 'local_entry', side_effect=ValueError('secret fixture body')), \
             patch.object(llm_providers, 'escalate', side_effect=RuntimeError('secret credential')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            entry, _, error = S.synthesize()
            self.assertIsNone(entry)
            self.assertEqual(error, 'Model call failed: RuntimeError')
            self.assertNotIn('secret', output.getvalue() + error)
            send.assert_not_called()

    def test_local_review_rejection_retains_draft_and_review(self):
        import media_pipeline
        review = '{"approved":false,"problems":["private reviewer finding"]}'
        with self.fixture() as (root, _), \
             patch.object(media_pipeline, 'call', side_effect=[ENTRY, review]):
            with self.assertRaisesRegex(ValueError, '^local_evidence_review_failed$'):
                S.local_entry('private prompt', [])
            packet, = (root/'state/failures').glob('*.json')
            data = json.loads(packet.read_text())
            self.assertEqual(data['draft'], ENTRY)
            self.assertEqual(data['review'], review)
            self.assertEqual(data['reason'], 'local_evidence_review_failed')

    def test_diagnostic_write_failure_preserves_original_validation_reason(self):
        with self.fixture(), patch.object(S.os, 'open', side_effect=OSError('secret path')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            S.retain_failure('fallback-validation', ValueError('entry_shape'), 'private', [])
            self.assertEqual(output.getvalue(), '[llm] failure evidence could not be saved\n')


if __name__ == '__main__': unittest.main()
