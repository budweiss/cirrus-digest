"""P03: full Snow caller/admission/dispatch with fake providers and no delivery."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch
import ensemble
import llm_providers as L
from capability_admission import prompt_digest
import test_foundation_routing as foundation_fixtures
import test_llm_routing as routing_fixtures
from snowbrief import bill_snow_weekly as snow

class SnowFailurePipeline(unittest.TestCase):
    setUp=routing_fixtures.RoutingTests.setUp

    def run_case(self, failure=None, material_preview=False):
        route=foundation_fixtures.FoundationTests.route(self,reviewers=2,review_reason='independent review',max_cost_usd=1)
        for row in route['evaluations']:
            row.update(task='billsnow',prompt_sha256=prompt_digest(snow.SYSTEM),usable_input_tokens=128000)
        for row in self.health:row['usable_input_tokens']=128000
        route['judge_evaluations']=[dict(row,capability='research:synthesis',prompt_sha256=prompt_digest(ensemble._JUDGE_SYSTEM)) for row in route['evaluations']]
        creds=self.root/'creds.json';creds.write_text(json.dumps(self.creds))
        decision=dict(material_change=False,reason='fixture unchanged',refresh_md='',email_subject='',email_body='')
        if material_preview:
            decision.update(material_change=True, reason='fixture change', refresh_md='fixture outlook',
                            email_subject='fixture subject', email_body='fixture body')
        def answer(*args):
            self.calls.append('provider')
            L._LAST.model='m';L._LAST.finish_reason='length' if failure=='truncated' else 'stop'
            if failure=='provider':raise L.ProviderError('fixture provider unavailable')
            return json.dumps(decision)
        if failure=='price':
            self.pricing.write_text(json.dumps({'models':{'m':{'in':10000,'out':20000}},'caps_usd':{'per_call':1,'per_session':1,'per_day':1}}))
        if failure=='contract':(self.root/'caller.py').write_text('changed contract')
        self.failure_causes = []
        real_best_answer = ensemble.best_answer
        def observe_failure(*args, **kwargs):
            try:
                return real_best_answer(*args, **kwargs)
            except Exception as exc:
                cause = exc
                while cause is not None:
                    self.failure_causes.append(str(cause))
                    cause = cause.__cause__
                raise
        with contextlib.ExitStack() as st:
            st.enter_context(patch.object(ensemble, 'best_answer', side_effect=observe_failure))
            for name,value in [('CREDS_PATH',creds),('OUT',self.root/'out'),('DIGEST_DIR',self.root)]:st.enter_context(patch.object(snow,name,value))
            st.enter_context(patch.object(snow.sys,'argv',['bill_snow_weekly.py'] + (['--dry-run'] if material_preview else [])))
            st.enter_context(patch.object(snow,'gather_web',return_value=('' if failure=='no-evidence' else 'synthetic evidence',['https://example.invalid/source'])))
            st.enter_context(patch.object(snow,'build_prompt',return_value='synthetic forecast'))
            st.enter_context(patch.object(snow,'_local_hint',return_value={}))
            st.enter_context(patch('capability_registry.foundation_route',return_value=route))
            st.enter_context(patch('capability_health.observe_cloud',side_effect=lambda c,p:next(h for h in self.health if h['id']==p)))
            st.enter_context(patch.dict(L._PROVIDERS,{'anthropic':answer,'gemini':answer}))
            st.enter_context(patch.object(L,'_http_post',side_effect=AssertionError('network forbidden')))
            st.enter_context(patch.object(L,'escalate',side_effect=AssertionError('legacy fallback forbidden')))
            st.enter_context(patch('send_guard.already_sent_today',return_value=None))
            stamp=st.enter_context(patch('send_guard.mark_sent'))
            send=st.enter_context(patch.object(snow.subprocess,'run',side_effect=AssertionError('send forbidden')))
            record=st.enter_context(patch.object(snow,'_rec'))
            st.enter_context(patch('socket.socket.connect',side_effect=AssertionError('network forbidden')))
            st.enter_context(patch('socket.create_connection',side_effect=AssertionError('network forbidden')))
            output=io.StringIO()
            with contextlib.redirect_stdout(output):snow.main()
            send.assert_not_called();stamp.assert_not_called()
            self.assertEqual(list((self.root/'out').iterdir()),[])
            if material_preview:
                record.assert_not_called()
                return output.getvalue()
            return record.call_args.args

    def test_valid_quiet_is_healthy_with_two_reviewers_and_judge(self):
        record=self.run_case();self.assertIs(record[1],True);self.assertEqual(len(self.calls),3)

    def test_material_preview_runs_full_panel_without_delivery(self):
        output=self.run_case(material_preview=True)
        self.assertEqual(len(self.calls),3)
        self.assertIn('fixture subject',output)
        self.assertIn('fixture body',output)
        self.assertIn('DRY RUN',output)

    def test_missing_evidence_stops_before_panel_or_delivery(self):
        record=self.run_case('no-evidence')
        self.assertIs(record[1],False)
        self.assertEqual(self.calls,[])

    def test_price_increase_defers_before_any_provider_or_send(self):
        record=self.run_case('price');self.assertIs(record[1],False);self.assertEqual(self.calls,[])
        self.assertIn('foundation route requires review or deferral', record[2])
        self.assertEqual(self.failure_causes[-1], 'no validated model meets requirements')

    def test_changed_contract_defers_before_any_provider_or_send(self):
        record=self.run_case('contract');self.assertIs(record[1],False);self.assertEqual(self.calls,[])
        self.assertIn('foundation contract changed; review required', record[2])
        self.assertEqual(self.failure_causes[-1], 'foundation contract changed; review required')

    def test_truncated_valid_json_is_failed_not_quiet_success(self):
        record=self.run_case('truncated');self.assertIs(record[1],False);self.assertEqual(len(self.calls),1)
        self.assertIn('capability_output_truncated',self.audit.read_text())

    def test_provider_failure_is_failed_not_quiet_success(self):
        record=self.run_case('provider');self.assertIs(record[1],False);self.assertEqual(len(self.calls),1)

if __name__=='__main__':unittest.main()
