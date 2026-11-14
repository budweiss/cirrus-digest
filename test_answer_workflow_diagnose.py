import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import answer_workflow_diagnose as D

class DiagnosticTest(unittest.TestCase):
    def test_oversize_refused_before_attempt_and_without_private_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'config').mkdir()
            (root/'config/answer-workflow-capabilities.json').write_text(json.dumps({'max_user_bytes':100}))
            with patch.object(D.cli,'run') as run:
                out=D.run(root,'stable','Find conflict',{'private':'PRIVATE_SENTINEL'*100})
            self.assertEqual(out['state'],'input_too_large');run.assert_not_called()
            self.assertNotIn('PRIVATE_SENTINEL',json.dumps(out))
            with D.Memory(root).connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM turns').fetchone()[0],0)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='attempt_limits'").fetchone())

    def test_private_invalid_response_preserved_without_changing_result(self):
        class Model:
            def call(self,payload,**kw):
                assert kw['privacy']=='LOCAL_ONLY'
                return {'raw':'PRIVATE_INVALID_JSON','provider':'fixture'}
        with tempfile.TemporaryDirectory() as tmp:
            recorder=D.RecordedModels(Model(),Path(tmp)/'receipts')
            result=recorder.call({'question':'PRIVATE_QUESTION','sources':{}},privacy='LOCAL_ONLY')
            self.assertEqual(result['raw'],'PRIVATE_INVALID_JSON')
            path=recorder.receipts[0]
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertEqual(json.loads(path.read_text())['result'],result)

    def test_parser_diagnostics_do_not_accept_bad_quotes_or_short_codes(self):
        value={'answer':'REJECTS_PDF','verdict':'pass','evidence':[{'source':'s','quote':'altered quote'}]}
        out=D.response_metadata({'raw':json.dumps(value)},{'sources':{'s':'PRIVATE_SOURCE'}})
        self.assertFalse(out['accepted_shape'])
        self.assertTrue(out['passing_answer_too_short'])
        self.assertEqual(out['invalid_quote_count'],1)
        self.assertNotIn('PRIVATE_SOURCE',json.dumps(out))

    def test_private_exception_is_not_copied_to_error_receipt(self):
        class Model:
            def call(self,*a,**kw):raise ValueError('PRIVATE_TOKEN=secret-value')
        with tempfile.TemporaryDirectory() as tmp:
            recorder=D.RecordedModels(Model(),tmp)
            with self.assertRaises(ValueError):recorder.call({},privacy='LOCAL_ONLY')
            self.assertNotIn('PRIVATE_TOKEN',recorder.receipts[0].read_text())

if __name__=='__main__':unittest.main()
