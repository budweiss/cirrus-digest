import copy, importlib.util, json, pathlib, unittest
p=pathlib.Path(__file__).with_name('research_task.py')
spec=importlib.util.spec_from_file_location('candidate',p); m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class CitationTests(unittest.TestCase):
 def setUp(self):
  self.c={'text':'A supplied fact. Another fact.','sources':['https://example.org/a']}
  self.d={'comparison':[{'requirement':'Offline','option':'A','status':'supported','evidence':[self.c]}], 'recommendation':[self.c], 'counterargument':[{'text':'An unresolved question.','sources':[]}], 'WHAT I DID NOT CHECK':[{'text':'No additional evidence was supplied.','sources':[]}]}
 def render(self,d=None): return m.render_synthesis(json.dumps(self.d if d is None else d),'SOURCE https://example.org/a\nA supplied fact. Another fact.')
 def test_labels_every_sentence(self):
  out=self.render();self.assertIn('A supplied fact. <https://example.org/a> Another fact. <https://example.org/a>',out);self.assertIn('An unresolved question. UNSOURCED',out)
 def test_exact_fence_accepted(self):
  self.assertEqual(self.render(),m.render_synthesis('```json\n'+json.dumps(self.d)+'\n```','SOURCE https://example.org/a'))
 def test_fence_with_extra_prose_refused(self):
  with self.assertRaises(ValueError):m.render_synthesis('preface\n```json\n'+json.dumps(self.d)+'\n```','SOURCE https://example.org/a')
 def test_no_evidence(self):
  for key in ('recommendation','counterargument','WHAT I DID NOT CHECK'): self.d[key]=[{'text':'No evidence was supplied.','sources':[]}]
  self.d['comparison'][0]['status']='UNCHECKED';self.d['comparison'][0]['evidence']=[{'text':'Unknown.','sources':[]}]
  self.assertIn('| UNCHECKED | Unknown. UNSOURCED |',m.render_synthesis(json.dumps(self.d),'no sources'))
 def test_unknown_source_refused(self):
  self.c['sources']=['https://invented.example/a']
  with self.assertRaises(ValueError):self.render()
 def test_malformed_refused(self):
  for v in ('not json','{}','{"comparison":[],"comparison":[]}'):
   with self.assertRaises(ValueError):m.render_synthesis(v,'')
 def test_multiple_recommendations_or_counterarguments_refused(self):
  for key in ('recommendation','counterargument'):
   d=copy.deepcopy(self.d);d[key].append(copy.deepcopy(d[key][0]))
   with self.assertRaises(ValueError):self.render(d)
 def test_status_refused(self):
  self.d['comparison'][0]['status']='supported because yes'
  with self.assertRaises(ValueError):self.render()
 def test_hidden_url_refused(self):
  self.c['text']='See https://invented.example/a'
  with self.assertRaises(ValueError):self.render()
 def test_missing_and_extra_fields_refused(self):
  for key in list(self.d):
   d=copy.deepcopy(self.d);del d[key]
   with self.assertRaises(ValueError):self.render(d)
  self.d['extra']='uncited prose'
  with self.assertRaises(ValueError):self.render()
 def test_table_and_html_escaped(self):
  self.c['text']='<script>x</script> | [claim]'
  out=self.render(); self.assertNotIn('<script>',out);self.assertIn('\\|',out);self.assertIn('\\[claim\\]',out)
 def test_synthesis_integration(self):
  from unittest.mock import patch
  with patch.object(m,'_council',return_value=({'test':True},json.dumps(self.d))):
   meta,out=m.synthesise('q',['Offline'],{'q':[{'url':'https://example.org/a','text':'facts'}]}, {})
  self.assertTrue(meta['test']);self.assertIn('WHAT I DID NOT CHECK',out);self.assertIn('UNSOURCED',out)
 def test_integration_refuses_invalid(self):
  from unittest.mock import patch
  with patch.object(m,'_council',return_value=({},'uncited text')):
   with self.assertRaises(ValueError):m.synthesise('q',[],{}, {})
if __name__=='__main__':unittest.main()
