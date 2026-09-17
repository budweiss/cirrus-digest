import unittest
import json
from unittest.mock import patch, mock_open
from snowbrief import bill_snow_weekly as s

class Decision(unittest.TestCase):
    def test_valid_quiet_and_material(self):
        self.assertTrue(s.validate_decision(dict(material_change=False, reason='unchanged', refresh_md='', email_subject='', email_body='')))
        self.assertTrue(s.validate_decision(dict(material_change=True, reason='changed', refresh_md='outlook', email_subject='update', email_body='body')))

    def test_false_string_cannot_authorize_delivery(self):
        for value in ['false', 'true', 0, 1, None, [], {}]:
            self.assertFalse(s.validate_decision(dict(material_change=value, reason='unchanged', refresh_md='outlook', email_subject='update', email_body='body')))

    def test_incomplete_and_contradictory(self):
        good=dict(material_change=True, reason='changed', refresh_md='outlook', email_subject='update', email_body='body')
        for key in ['reason','refresh_md','email_subject','email_body']:
            for value in [None, [], {}, 1, '  ']:
                self.assertFalse(s.validate_decision(dict(good, **{key:value})))
        self.assertFalse(s.validate_decision(dict(good, material_change=False)))
        for value in [None, [], 'text', {'material_change':True}]:
            self.assertFalse(s.validate_decision(value))

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
        self.assertEqual(scoped['llm_budget'],{'per_session_usd':2,'per_call_usd':1,'per_day_usd':200})
        self.assertEqual(original['llm_budget']['per_session_usd'],100)
        lower,_=s.weekly_budget({'llm_budget':{'per_session_usd':0.5,'per_call_usd':0.1}})
        self.assertEqual(lower['llm_budget']['per_session_usd'],0.5)
        self.assertEqual(lower['llm_budget']['per_call_usd'],0.1)
        self.assertEqual(s.weekly_budget({},datetime(2026,9,27))[1],session)
        self.assertNotEqual(s.weekly_budget({},datetime(2026,9,28))[1],session)
        with self.assertRaises(ValueError):s.weekly_budget({'llm_budget':{'per_session_usd':float('nan')}})

if __name__=='__main__': unittest.main()
