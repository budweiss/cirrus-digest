import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import dev_nightly as n
class Admission(unittest.TestCase):
 def test_task_routing(self):
  for detail,files,state in [('Fix parser in parser.py',['parser.py'],'ready'),('Investigate missing output',[],'needs-discovery'),('Install library',['config/sources.json'],'operator-task'),('Improve creative prompt',[],'needs-scope')]:
   self.assertEqual(n.disposition({'detail':detail,'dev_spec':{'files_to_change':files}})[0],state)
 def test_preflight_preserves_work_without_model_or_worktree(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);(root/'logs/dev-loop').mkdir(parents=True)
   item={'detail':'Investigate missing output','dev_spec':{'id':'scope-one','files_to_change':[]}}
   with patch.object(n.agent,'find_buildable',return_value=[item]),patch.object(n.agent,'build_item') as build:
    held=n.preflight(root);build.assert_not_called()
   self.assertEqual(held[0]['status'],'blocked');self.assertFalse(held[0]['attempted'])
   self.assertEqual(n.agent.builds_load(root)[0]['next_action'],held[0]['next_action'])
   with patch.object(n.agent,'queue_load',return_value=[{'item':dict(item,dev_spec=dict(item['dev_spec'],tier=1))}]),patch.object(n.agent,'may_build',return_value=True):
    self.assertEqual(n.agent.find_buildable(root),[])
if __name__=='__main__':unittest.main()
