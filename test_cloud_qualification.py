import unittest
from unittest.mock import patch
import capability_health as health
import llm_providers as lp

class CloudQualificationTests(unittest.TestCase):
    def test_metadata_identity_capacity_and_no_key_in_url(self):
        for provider,creds,data in [('anthropic',{'claude_model':'m','anthropic_api_key':'secret'},{'id':'m','max_input_tokens':10000}),('gemini',{'gemini_model':'m','gemini_api_key':'secret'},{'name':'models/m','inputTokenLimit':10000})]:
            def get(url,headers):
                self.assertNotIn('secret',url);self.assertIn('secret',headers.values());return data
            row=health.observe_cloud(creds,provider,get=get,clock=lambda:100)
            self.assertTrue(row['healthy']);self.assertEqual(row['checked_at'],100)
    def test_failure_and_mismatch_do_not_gain_fresh_health(self):
        creds={'gemini_model':'m','gemini_api_key':'secret'}
        for get in (lambda *a:{'name':'models/other','inputTokenLimit':100}, lambda *a:(_ for _ in ()).throw(ValueError('secret'))):
            row=health.observe_cloud(creds,'gemini',get=get)
            self.assertFalse(row['healthy']);self.assertNotIn('checked_at',row);self.assertNotIn('secret',str(row))
    def test_adapters_use_returned_identity(self):
        with patch.object(lp,'_http_post',return_value={'model':'actual','choices':[{'message':{'content':'ok'},'finish_reason':'stop'}]}):
            lp._openai_compatible('https://fixture','key','requested','s','u',10)
            self.assertEqual(lp.last_model(),'actual')
        with patch.object(lp,'_http_post',return_value={'model':'actual','content':[{'type':'text','text':'ok'}]}),patch.object(lp,'_record_usage'):
            lp._anthropic({'anthropic_api_key':'key','claude_model':'requested'},'s','u',10)
            self.assertEqual(lp.last_model(),'actual')
        with patch.object(lp,'_http_post',return_value={'modelVersion':'actual','candidates':[{'content':{'parts':[{'text':'ok'}]},'finishReason':'STOP'}]}):
            lp._gemini({'gemini_api_key':'key','gemini_model':'requested'},'s','u',10)
            self.assertEqual(lp.last_model(),'actual');self.assertEqual(lp.last_finish_reason(),'STOP')
    def test_missing_identity_is_not_reported_as_requested_model(self):
        with patch.object(lp,'_http_post',return_value={'choices':[{'message':{'content':'ok'},'finish_reason':'stop'}]}):
            lp._openai_compatible('https://fixture','key','requested','s','u',10)
            self.assertIsNone(lp.last_model())
