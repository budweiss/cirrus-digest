import unittest
import json
from unittest.mock import patch, mock_open
from snowbrief import bill_snow_weekly as s

SECTION=f'{s.CHANGE_HEADING}\nSnowfall estimate unchanged. Source, published 2026-11-01.'

class Decision(unittest.TestCase):
    def test_reviewed_system_is_node_independent_but_task_keeps_sender(self):
        # Cloud review evidence is shared between the two hosts. Do not bake
        # NODE into its exact system-prompt hash; the task carries the sender.
        self.assertNotIn('CIRRUS',s.SYSTEM)
        self.assertNotIn('CUMULUS',s.SYSTEM)
        self.assertIn("sending node named in the task's voice instructions",s.SYSTEM)
        for node in ['CIRRUS','CUMULUS']:
            with patch.object(s,'NODE',node),patch.object(s,'_read',return_value='fixture'):
                self.assertIn('sign as '+node,s.build_prompt('synthetic evidence',[]))

    def test_valid_quiet_and_material(self):
        self.assertTrue(s.validate_decision(dict(material_change=False, reason='unchanged', refresh_md='', email_subject='', email_body='')))
        self.assertTrue(s.validate_decision(dict(material_change=True, reason='changed', refresh_md=SECTION, email_subject='update', email_body=SECTION)))

    def test_false_string_cannot_authorize_delivery(self):
        for value in ['false', 'true', 0, 1, None, [], {}]:
            self.assertFalse(s.validate_decision(dict(material_change=value, reason='unchanged', refresh_md='outlook', email_subject='update', email_body='body')))

    def test_incomplete_and_contradictory(self):
        good=dict(material_change=True, reason='changed', refresh_md=SECTION, email_subject='update', email_body=SECTION)
        for key in ['reason','refresh_md','email_subject','email_body']:
            for value in [None, [], {}, 1, '  ']:
                self.assertFalse(s.validate_decision(dict(good, **{key:value})))
        self.assertFalse(s.validate_decision(dict(good, material_change=False)))
        for value in [None, [], 'text', {'material_change':True}]:
            self.assertFalse(s.validate_decision(value))

    def test_material_update_must_carry_the_change_section(self):
        good=dict(material_change=True, reason='changed', refresh_md=SECTION, email_subject='update', email_body=SECTION)
        for key in ['refresh_md','email_body']:
            self.assertFalse(s.validate_decision(dict(good, **{key:'outlook'})))
            self.assertFalse(s.validate_decision(dict(good, **{key:s.CHANGE_HEADING+' Source, published 11-01.'})))
            self.assertFalse(s.validate_decision(dict(good, **{key:s.CHANGE_HEADING+' Snowfall estimate unchanged.'})))
        self.assertFalse(s.validate_decision(dict(good, refresh_md=SECTION+'x'*s.REFRESH_MAX_CHARS)))

    def test_prompt_and_system_require_the_section_and_dates(self):
        with patch.object(s,'_read',return_value='fixture'):
            prompt=s.build_prompt('synthetic evidence',[])
        self.assertIn(s.CHANGE_HEADING,prompt)
        self.assertIn('newly published corridor seasonal snowfall forecast',prompt)
        self.assertIn('published before the baseline',prompt)
        self.assertIn('figures the baseline does not\nalready report',prompt)
        for words in [s.CHANGE_HEADING,'publish date not shown','never guess a date',"line starting 'Snowfall estimate:'","line starting 'Sources:'","never present last week's facts as new",
                      'directional estimate','placeholders pending',"You NEVER fabricate numbers",'never blend sources into a new range','what happens when Bill replies','exact node signature']:
            self.assertIn(words,s.SYSTEM)

    def test_every_query_gets_a_turn_before_any_gets_a_second(self):
        def search(q,max_results=6):return [f'{s.QUERIES.index(q)}-{i}' for i in range(max_results)]
        picked=[u for u,_ in s.select_sources(s.QUERIES,search,lambda u:'x'*400)]
        self.assertEqual(picked,[f'{i}-0' for i in range(6)])
        self.assertTrue(any('snowfall' in q and 'Mount Holly' in q for q in s.QUERIES))
        self.assertTrue(any('snowfall' in q and 'Sterling' in q for q in s.QUERIES))
        self.assertTrue(any('ENSO advisory' in q for q in s.QUERIES))
        fetched=s.select_sources(['a'],lambda q,max_results=6:['u'],lambda u:'y'*9000)
        self.assertEqual(len(fetched[0][1]),3500)

    def test_final_answer_missing_a_required_line_fails_the_run_not_quiet(self):
        meta=dict(mode='test',members=[],judge='test',degraded=False,reason='fixture')
        loose=f'{s.CHANGE_HEADING}\nThe estimate held. Published 11-01.'
        full=f'{s.CHANGE_HEADING}\nSnowfall estimate: unchanged.\nSources: x, published 11-01.'
        def run(body):
            decision=dict(material_change=True,reason='changed',refresh_md=full,email_subject='update',email_body=body)
            with patch('builtins.open',mock_open(read_data='{}')), patch.object(s,'gather_web',return_value=('fresh synthetic evidence',[])), patch.object(s,'build_prompt',return_value='synthetic prompt'), patch.object(s,'_local_hint',return_value={}), patch.object(s.ensemble,'best_answer',return_value=(meta,json.dumps(decision))):
                return s.decide()[0]
        self.assertTrue(s.validate_decision(dict(material_change=True,reason='r',refresh_md=loose,email_subject='s',email_body=loose)))
        data=run(loose)
        self.assertTrue(s._run_failed(data))
        self.assertNotIn('email_body',data)
        self.assertEqual(run(full)['email_body'],full)

    def test_decide_reports_malformed_reply_as_error(self):
        meta=dict(mode='test',members=[],judge='test',degraded=False,reason='fixture')
        bad=dict(material_change='false',reason='unchanged',refresh_md='outlook',email_subject='update',email_body='body')
        with patch('builtins.open',mock_open(read_data='{}')), patch.object(s,'gather_web',return_value=('fresh synthetic evidence',[])), patch.object(s,'build_prompt',return_value='synthetic prompt'), patch.object(s,'_local_hint',return_value={}), patch.object(s.ensemble,'best_answer',return_value=(meta,json.dumps(bad))):
            data,_=s.decide()
        self.assertIs(data['material_change'],False)
        self.assertTrue(s._run_failed(data))
        self.assertNotIn('email_body',data)

    def test_weekly_budget_is_scoped_and_preserves_lower_limits(self):
        from datetime import datetime
        original={'llm_budget':{'per_session_usd':100,'per_call_usd':10,'per_day_usd':200}}
        scoped, session=s.weekly_budget(original,datetime(2026,9,21))
        self.assertEqual(session,'billsnow:2026-W39')
        self.assertEqual(scoped['llm_budget'],{'per_session_usd':20,'per_call_usd':5,'per_day_usd':200})
        self.assertEqual(original['llm_budget']['per_session_usd'],100)
        lower,_=s.weekly_budget({'llm_budget':{'per_session_usd':0.5,'per_call_usd':0.1}})
        self.assertEqual(lower['llm_budget']['per_session_usd'],0.5)
        self.assertEqual(lower['llm_budget']['per_call_usd'],0.1)
        self.assertEqual(s.weekly_budget({},datetime(2026,9,27))[1],session)
        self.assertNotEqual(s.weekly_budget({},datetime(2026,9,28))[1],session)
        with self.assertRaises(ValueError):s.weekly_budget({'llm_budget':{'per_session_usd':float('nan')}})

    def test_five_dollar_admission_and_twenty_dollar_weekly_limit(self):
        import tempfile
        from datetime import datetime
        from pathlib import Path
        import llm_budget
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'pricing.json').write_text(json.dumps({'models':{}, 'caps_usd':{
                'per_call':10, 'per_session':100, 'per_day':200}}))
            creds={'llm_budget':{'pricing_path':'pricing.json', 'ledger_path':'spend.jsonl'}}
            scoped,session=s.weekly_budget(creds,datetime(2026,9,28))
            cfg,box,ledger=llm_budget.resolve(scoped,app_dir=str(root))
            def allowed(cost,session_id=session):
                return llm_budget.allow(session_id,cost,cfg,box=box,ledger_path=ledger)[0]
            self.assertTrue(allowed(4.99))
            self.assertTrue(allowed(5.00))
            self.assertFalse(allowed(5.01))
            Path(ledger).write_text(json.dumps({'session_id':session,'cost':3.00})+'\n')
            self.assertTrue(allowed(5.00))
            Path(ledger).write_text(json.dumps({'session_id':session,'cost':15.00})+'\n')
            self.assertTrue(allowed(5.00))
            Path(ledger).write_text(json.dumps({'session_id':session,'cost':18.00})+'\n')
            self.assertTrue(allowed(2.00))
            self.assertFalse(allowed(2.01))
            Path(ledger).write_text(json.dumps({'session_id':session,'cost':20.00})+'\n')
            self.assertFalse(allowed(0.01))
            _,next_week=s.weekly_budget(creds,datetime(2026,10,5))
            self.assertTrue(allowed(5.00,next_week))

    def test_every_snow_stage_has_paid_call_governance(self):
        import llm_routing
        for task in ['billsnow','billsnow:draft','billsnow:council','billsnow:judge']:
            self.assertEqual(llm_routing.policy(task, {})['profile'],'research')

if __name__=='__main__': unittest.main()
