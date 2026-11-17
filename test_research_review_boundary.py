import json,pathlib,tempfile,unittest
import foundation_renewal_auto as U
class BoundaryTests(unittest.TestCase):
 def case(self, raw, system, delivered=None):
  temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);p=pathlib.Path(temp.name)
  row={'route':'foundation:research:synthesise','field':'evaluations','index':0,'fixture_id':'fixture','old_record_sha256':'record','system':system,'user':'SOURCE https://example.org/a','response':raw}
  if delivered is not None:row['rendered_response']=delivered
  (p/'run.json').write_text(json.dumps([row]));(p/'review-priors.json').write_text(json.dumps([dict(row,record_sha256='record',prior='old')]))
  item={'route':row['route'],'field':'evaluations','index':0,'evidence_file':'run.json','reviewed_fixtures':['fixture']}
  text=U.complete_cases(p,item,['shape=True identity=True err=None'])[0]
  return json.loads(text.split('REVIEW EVIDENCE (JSON): ',1)[1])
 def record(self):
  c={'text':'A fact.','sources':['https://example.org/a']}
  return {'comparison':[{'requirement':'r','option':'a','status':'supported','evidence':[c]}],'recommendation':[c],'counterargument':[c],'WHAT I DID NOT CHECK':[{'text':'Unknown.','sources':[]}]}
 def test_reviews_delivered_report(self):
  new='Every factual claim lists its supplied source URLs in its sources array. An empty sources array explicitly means UNSOURCED.\nTransport schema (mandatory): schema'
  e=self.case(json.dumps(self.record()),new)
  self.assertEqual(e['SYSTEM_INSTRUCTIONS'],'Every factual claim carries its source URL inline. No URL, mark it    UNSOURCED.')
  self.assertIn('A fact. <https://example.org/a>',e['NEW']);self.assertIn('Unknown. UNSOURCED',e['NEW'])
 def test_legacy_unmodified(self):
  e=self.case('legacy answer','legacy system');self.assertEqual(e['NEW'],'legacy answer');self.assertEqual(e['SYSTEM_INSTRUCTIONS'],'legacy system')
 def test_invalid_raw_refused(self):
  with self.assertRaises(ValueError):self.case('invalid','\nTransport schema (mandatory): schema')
 def test_forged_delivery_refused(self):
  with self.assertRaises(ValueError):self.case(json.dumps(self.record()),'\nTransport schema (mandatory): schema','different report')
if __name__=='__main__':unittest.main()
