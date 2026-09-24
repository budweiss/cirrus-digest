"""Regression for scheduled extraction inheriting max-effort empty responses."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import llm_providers as lp
import halftime_catalogue as catalogue
import halftime_routing as routing


class ExtractionEffortTests(unittest.TestCase):
    def setUp(self):
        # Test the legacy low-effort fallback, independent of installed routing
        # approvals. Both registry lookups must stay inside the fixture tree.
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for field, value in (("PROJECT_DIR", root),
                             ("CAPABILITY_RECORDS", root / "config/halftime_capabilities.json")):
            fixture = patch.object(catalogue, field, value)
            fixture.start()
            self.addCleanup(fixture.stop)

    def test_both_cloud_fallbacks_send_low_effort_preserve_other_settings(self):
        creds={'anthropic_effort':'max','llm_privacy':'CLOUD_ALLOWED',
               'llm_budget':{'per_call_usd':.1},'ollama_url':'http://fixture',
               'vllm_url':'http://fixture'}
        original=copy.deepcopy(creds)
        observed=[]
        def fallback(system,user,c,**kwargs):
            observed.append(c)
            # Reproduce both incidents: reasoning consumes output before text
            # at the host's max effort (S190) AND with the key dropped, which
            # claude-sonnet-5 runs at its default, high (S274).
            return 'anthropic', '[]' if c.get('anthropic_effort') == 'low' else ''
        with patch.object(lp,'call',side_effect=lp.ProviderError('local unavailable')), patch.object(lp,'escalate',side_effect=fallback):
            acts,model,escalated=catalogue.extract_acts('synthetic',creds)
            self.assertEqual(acts,[]);self.assertTrue(escalated)
            stats={}
            self.assertEqual(routing._extract('synthetic',creds,stats),[])
            self.assertEqual(stats.get('escalated'),1)
        self.assertEqual(creds,original)
        self.assertEqual(len(observed),2)
        for c in observed:
            self.assertEqual(c['anthropic_effort'],'low')
            self.assertEqual(c['llm_budget'],original['llm_budget'])
            self.assertEqual(c['llm_privacy'],original['llm_privacy'])

    def test_nonempty_invalid_records_are_not_successful_empty_results(self):
        for pool in ('variety', 'program'):
            self.assertEqual(catalogue.parse_acts('[]', pool), [])
            for raw in ('[null]', '[42]', '[{}]', '[{"category":"acrobat"}]'):
                self.assertIsNone(catalogue.parse_acts(raw, pool))
        self.assertEqual(len(catalogue.parse_acts('[{}, {"name":"Fixture Act"}]')), 1)
        with patch.object(lp, 'call', return_value='[{}]'), patch.object(lp, 'escalate', return_value=('anthropic', '[]')) as cloud:
            acts, model, escalated = catalogue.extract_acts('synthetic', {})
        self.assertEqual(acts, [])
        self.assertTrue(escalated)
        cloud.assert_called_once()

    def test_routing_invalid_records_trigger_fallback(self):
        self.assertEqual(routing.parse_events('[]'), [])
        for raw in ('[null]', '[{}]', '[{"artist":"A"}]', '[{"date":"2026-11-01"}]'):
            self.assertIsNone(routing.parse_events(raw))
        self.assertEqual(len(routing.parse_events('[{}, {"artist":"A","date":"2026-11-01"}]')), 1)
        with patch.object(lp, 'call', return_value='[{}]'), patch.object(lp, 'escalate', return_value=('anthropic', '[]')) as cloud:
            stats = {}
            self.assertEqual(routing._extract('synthetic', {}, stats), [])
        self.assertEqual(stats.get('escalated'), 1)
        cloud.assert_called_once()

    def test_anthropic_truncation_is_observable_without_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            ledger=Path(td)/'truncations.jsonl'
            with patch.object(lp,'_TRUNC_LEDGER',ledger), patch.object(lp,'_record_usage'), patch.object(lp,'_http_post',return_value={'content':[], 'stop_reason':'max_tokens','usage':{'input_tokens':10,'output_tokens':4096}}):
                result=lp._anthropic({'anthropic_api_key':'fixture','claude_model':'m','anthropic_effort':'max'},'PRIVATE-SYSTEM','PRIVATE-INPUT',4000)
            self.assertEqual(result,'')
            self.assertEqual(lp.last_finish_reason(),'length')
            data=ledger.read_text()
            self.assertEqual(json.loads(data)['model'],'m')
            self.assertNotIn('PRIVATE',data)


if __name__=='__main__':
    unittest.main()
