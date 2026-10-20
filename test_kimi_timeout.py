import unittest
from unittest.mock import patch
import llm_providers as lp

class KimiTimeoutTests(unittest.TestCase):
    def test_explicit_timeout_reaches_transport_and_keeps_request(self):
        creds={'kimi_api_key':'EXAMPLE-only','kimi_model':'kimi-k3','kimi_timeout':300}
        with patch.object(lp,'_openai_compatible',return_value='ok') as call:
            self.assertEqual(lp._kimi(creds,'system','user',8192),'ok')
            self.assertEqual(call.call_args.kwargs['timeout'],300)
            self.assertEqual(call.call_args.args[2:],('kimi-k3','system','user',8192))
    def test_default_unchanged(self):
        with patch.object(lp,'_openai_compatible') as call:
            lp._kimi({'kimi_api_key':'EXAMPLE-only','kimi_model':'kimi-k3'},'s','u',10)
            self.assertEqual(call.call_args.kwargs['timeout'],lp._TIMEOUT)
    def test_invalid_timeout_never_calls_provider(self):
        for value in (True,'300',0,-1,601,float('nan'),float('inf')):
            with patch.object(lp,'_openai_compatible') as call:
                with self.assertRaises(lp.ProviderError):
                    lp._kimi({'kimi_api_key':'EXAMPLE-only','kimi_model':'kimi-k3','kimi_timeout':value},'s','u',10)
                call.assert_not_called()
    def test_overall_deadline_still_limits_socket_timeout(self):
        import time
        lp._LAST.request_deadline=time.time()+20
        try:
            with patch.object(lp.urllib.request,'urlopen',side_effect=TimeoutError('test')) as call:
                with self.assertRaises(lp.ProviderError):
                    lp._http_post('https://example.invalid',{}, {},timeout=300)
                self.assertLessEqual(call.call_args.kwargs['timeout'],20)
        finally:
            del lp._LAST.request_deadline
