"""Freshness boundary regressions: no network, credentials or private packets."""
import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import knowledge_freshness as K


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'dates.json'
        self.data = {'version': 1, 'models': {
            'known': {'cutoff': '2026-04-20', 'checked': '2026-11-17', 'source': 'https://example.test/model'},
            'month': {'cutoff': '2026-01', 'checked': '2026-11-17', 'source': 'https://example.test/model'},
            'unknown': {'cutoff': None, 'checked': '2026-11-17', 'source': None}}}
        self.save()
        self.kw = {'registry': self.path, 'as_of': date(2026, 11, 17)}

    def save(self):
        self.path.write_text(json.dumps(self.data))

    def test_exact_dates_and_month_precision(self):
        self.assertEqual(K.metadata('known', **self.kw)['cutoff'], '2026-04-20')
        self.assertEqual(K.metadata('month', **self.kw)['cutoff'], '2026-01')

    def test_alias_never_inherits_nearby_date(self):
        for name in ('known-new', 'sonnet', 'unknown'):
            self.assertIsNone(K.metadata(name, **self.kw)['cutoff'])

    def test_overdue_metadata_cannot_claim_verified_cutoff(self):
        kw = dict(self.kw, as_of=date(2026, 12, 23))
        self.assertEqual(K.metadata('known', **kw)['status'], 'overdue')
        self.assertIsNone(K.metadata('known', **kw)['cutoff'])
        self.assertEqual(K.audit(**kw)['overdue_or_invalid'], 3)

    def test_future_check_date_rejected(self):
        self.assertEqual(K.metadata('known', **dict(self.kw, as_of=date(2026, 11, 16)))['status'], 'invalid')

    def test_missing_corrupt_and_wrong_version_registry_fail_conservatively(self):
        for text in ('{', '{"version":2,"models":{}}', '[]'):
            self.path.write_text(text)
            self.assertEqual(K.metadata('known', **self.kw)['status'], 'invalid')
        self.path.unlink()
        self.assertEqual(K.metadata('known', **self.kw)['status'], 'invalid')
        self.assertEqual(K.audit(**self.kw)['models'], 0)

    def test_invalid_date_or_unsourced_date_is_not_admitted(self):
        for change in ({'cutoff': '2026-02-30'}, {'cutoff': '2027-01'}, {'source': None}):
            self.data['models']['known'].update(change)
            self.save()
            self.assertIsNone(K.metadata('known', **self.kw)['cutoff'])

    def test_messages_preserve_schema_and_do_not_mutate_input(self):
        rows = [{'role': 'system', 'content': 'Return only {"ok":true}.'},
                {'role': 'user', 'content': 'PRIVATE-FIXTURE'}]
        original = copy.deepcopy(rows)
        out = K.messages(rows, 'known', **self.kw)
        self.assertEqual(rows, original)
        self.assertEqual(out[1], rows[1])
        self.assertTrue(out[0]['content'].startswith(rows[0]['content']))
        self.assertIn('Preserve the required output schema', out[0]['content'])
        self.assertNotIn('PRIVATE-FIXTURE', out[0]['content'])

    def test_policy_covers_volatile_facts_and_no_tool_honesty(self):
        text = K.instruction('known', **self.kw)
        for rule in ('changing facts', 'post-cutoff', 'unknown/overdue',
                     'Never claim a search', 'newly retrieved old text',
                     'Stable reasoning', 'Never disclose private data'):
            self.assertIn(rule, text)

    def test_source_retrieval_is_not_a_publication_date(self):
        when = datetime(2026, 11, 17, tzinfo=timezone.utc)
        row = K.fetched_source('https://example.test', 'Published in 2001', now=when)
        self.assertEqual(row['retrieved_at'], when.isoformat())
        self.assertIsNone(row['publication_date'])
        self.assertIsNone(row['observation_date'])

    def test_metadata_output_never_contains_unvalidated_model_strings(self):
        m = K.metadata('https://example.test/?token=PRIVATE-FIXTURE', **self.kw)
        self.assertEqual(m['model'], 'unknown')

    def test_real_gateway_uses_requested_model_before_budget_check(self):
        import llm_providers as lp
        import llm_routing
        seen = []
        def auth(provider, task, creds, system, user, *args, **kw):
            seen.append(system)
            return {'profile': None, 'privacy': 'LOCAL_ONLY'}
        def answer(creds, system, user, max_tokens):
            self.assertEqual(system, seen[0])
            self.assertIn('requested model: gpt-oss:120b', system)
            return '{"ok":true}'
        with patch.object(llm_routing, 'authorize', side_effect=auth), \
                patch.dict(lp._PROVIDERS, {'ollama': answer}), lp.recording(False):
            self.assertEqual(lp.call('ollama', 'Return JSON.', 'fixture',
                                    {'ollama_model': 'gpt-oss:120b'}, record=False), '{"ok":true}')

    def test_added_policy_is_counted_by_context_admission(self):
        from test_capability_dispatch import DispatchTests
        case = DispatchTests()
        case.setUp()
        try:
            rows = case.records()
            for row in rows:
                row['usable_input_tokens'] = 2100  # old request fits; policy does not
            import llm_providers as lp
            with self.assertRaises(lp.ProviderError):
                case.invoke(candidates=rows)
            self.assertFalse(case.calls)
        finally:
            case.doCleanups()


if __name__ == '__main__':
    unittest.main()
