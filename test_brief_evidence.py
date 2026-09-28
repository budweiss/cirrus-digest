"""O03 report fixtures: actual job-reader/composer, no credentials or deliveries."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import cumulus_daily_brief as brief

class BriefEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'jobs.json'
        for name,value in [('JOBS_STATUS_PATH',self.path),('TODAY','2026-09-28'),('DAY_NAME','Monday, September 28')]:
            p=patch.object(brief,name,value);p.start();self.addCleanup(p.stop)
    def compose(self,incidents=None):
        incidents=incidents if incidents is not None else {'incidents':{}}
        with patch.object(brief,'gather_client_events',return_value={}),patch.object(brief,'gather_skywarden',return_value=['  monitor fixture']),patch.object(brief,'_sudo_cat',return_value=json.dumps(incidents)):
            return brief.compose()[1]
    def test_missing_malformed_or_nonobject_file_never_claims_quiet(self):
        for raw in [None,'{broken','[]','null']:
            with self.subTest(raw=raw):
                if raw is not None:self.path.write_text(raw)
                body=self.compose()
                self.assertIn('Job evidence UNKNOWN',body)
                self.assertNotIn('**Quiet day**',body)
    def test_mixed_daily_and_weekly_evidence_preserves_failures_and_ownership(self):
        self.path.write_text(json.dumps({
            'hoaleads':{'ok':True,'last_run':'2026-09-28 04:00','note':'2 verified leads'},
            'billsnow':{'ok':True,'last_run':'2026-09-21 04:00','note':'no material change'},
            'billnewdev':{'ok':False,'last_run':'2026-09-28 05:00','note':'upstream failed'},
            'pedagogy':{'ok':True,'last_run':'2026-09-28 06:00','note':'valid quiet: awaiting reply'}}))
        body=self.compose({'incidents':{'failed_runs:billnewdev':{'first_seen':100,'owner':'Cowork','state':'action pending','next_check':200,'next_action':'inspect upstream artifact'}}})
        for text in ['✅ hoaleads: 04:00 — 2 verified leads','⏸ billsnow: no run recorded today','cadence not evaluated here','⚠️ billnewdev: 05:00 — upstream failed','✅ pedagogy: 06:00 — valid quiet: awaiting reply','Unresolved incidents: 1','owner Cowork','next check 200','next action inspect upstream artifact']:
            self.assertIn(text,body)
        self.assertNotIn('**Quiet day**',body)
    def test_unknown_boolean_and_missing_jobs_are_not_success(self):
        self.path.write_text(json.dumps({'hoaleads':{'ok':'false','last_run':'2026-09-28 04:00'}}))
        body=self.compose()
        for job in ['hoaleads','billsnow','billnewdev','pedagogy']:self.assertIn(job+': evidence UNKNOWN',body)
        self.assertNotIn('✅',body)

if __name__=='__main__':unittest.main()
