import unittest
from capability_health import observe

class HealthTests(unittest.TestCase):
    def test_vllm_requires_health_and_exact_model(self):
        calls=[]
        def get(url):
            calls.append(url)
            return {} if url.endswith('/health') else {'data':[{'id':'m','max_model_len':32768}]}
        row=observe({'vllm_url':'http://localhost:8000','vllm_model':'m'},'vllm',get=get,clock=lambda:1000)
        self.assertTrue(row['healthy']);self.assertEqual(row['checked_at'],1000)
        self.assertEqual(row['usable_input_tokens'],32768)
        self.assertEqual(calls,['http://localhost:8000/health','http://localhost:8000/v1/models'])
    def test_unloaded_is_not_resident_not_a_fresh_failure(self):
        row=observe({'ollama_url':'http://localhost:11434','ollama_model':'m'},'ollama',get=lambda u:{'models':[]})
        self.assertEqual(row['status'],'not_resident');self.assertFalse(row['healthy'])
        self.assertNotIn('checked_at',row)
    def test_loaded_model_needs_capacity_and_exact_identity(self):
        for payload in ({'models':[{'name':'other','context_length':8192}]},{'models':[{'name':'m'}]},{'models':[{'name':'m','context_length':True}]}):
            self.assertFalse(observe({'ollama_url':'http://localhost','ollama_model':'m'},'ollama',get=lambda u:payload)['healthy'])
        row=observe({'ollama_url':'http://localhost','ollama_model':'m'},'ollama',get=lambda u:{'models':[{'name':'m','context_length':8192}]},clock=lambda:1000)
        self.assertTrue(row['healthy'])
    def test_errors_do_not_expose_endpoint_or_error_body(self):
        def get(url):raise ValueError('PRIVATE BODY')
        row=observe({'vllm_url':'http://localhost','vllm_model':'m'},'vllm',get=get)
        self.assertEqual(row['error_type'],'ValueError');self.assertNotIn('PRIVATE',str(row))
        self.assertNotIn('checked_at',row)
    def test_cloud_provider_cannot_be_probed(self):
        with self.assertRaises(ValueError):observe({},'anthropic')

if __name__=='__main__':unittest.main()
