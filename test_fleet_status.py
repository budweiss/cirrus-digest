import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import fleet_status as f
class StatusTests(unittest.TestCase):
 def test_fresh_stale_future_and_missing(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'status.json';self.assertFalse(f.check(p,1000)['ok'])
   for stamp,ok,workers,expected in [(999,True,[1,2],True),(800,True,[1,2],False),(1001,True,[1,2],False),(999,False,[1,2],False),(999,True,[1],False)]:
    p.write_text(json.dumps({'observed':stamp,'ok':ok,'workers':workers}));self.assertEqual(f.check(p,1000)['ok'],expected)
 def test_independent_observer_no_mutation(self):
  import cirrus_watchdog as w
  with patch.object(w.subprocess,'run') as run:
   run.return_value.returncode=0;run.return_value.stdout='{"ok":true}'
   self.assertTrue(w.fleet_controller_health())
   self.assertIn('fleet_status.py',run.call_args.args[0][-1]);self.assertNotIn('restart',str(run.call_args))
   run.return_value.stdout='broken';self.assertFalse(w.fleet_controller_health())
   run.side_effect=TimeoutError;self.assertFalse(w.fleet_controller_health())
if __name__=='__main__':unittest.main()
