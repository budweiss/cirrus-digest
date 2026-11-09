import json,unittest
from unittest.mock import patch
import foundation_renewal_auto as U
class TransportTests(unittest.TestCase):
 def test_direct_path_preserves_validation_and_does_not_ssh(self):
  for verdict,reference in [('pass','NONE'),('fail','NEW')]:
   answer={'verdict':verdict,'issue_type':'none' if verdict=='pass' else 'instruction_failure','reason':'Synthetic reviewed requirement.','new_span_id':reference}
   with patch.object(U,'local_review',return_value={'answer':json.dumps(answer)}) as local,patch.object(U.subprocess,'run') as ssh:
    self.assertEqual(U.model_review(U.REVIEW_PROMPTS[0],'synthetic',local=True)['verdict'],verdict);local.assert_called_once();ssh.assert_not_called()
 def test_direct_path_rejects_malformed_response(self):
  with patch.object(U,'local_review',return_value={'answer':'invalid'}),patch.object(U.subprocess,'run') as ssh:
   with self.assertRaises(ValueError):U.model_review(U.REVIEW_PROMPTS[0],'synthetic',local=True)
   ssh.assert_not_called()
if __name__=='__main__':unittest.main()
