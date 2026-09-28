import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import foundation_renewal as f
class Renewal(unittest.TestCase):
 def test_expiry_and_notification_failure_cannot_report_healthy(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); c=root/'creds';c.write_text('{}');r=root/'registry';r.write_text('{"projects":{}}')
   with patch.object(f,'CREDS',c),patch.object(f,'REGISTRY',r),patch.object(f,'STATE',root/'state'),patch.object(f,'providers_here',return_value=[]),patch.object(f,'served_models',return_value={}),patch.object(f,'log'),patch.object(f.time,'time',return_value=100),patch.object(f,'decide',return_value='remind'):
    for expiry,delivery,want in [(200,'sent',True),(99,'sent',False),(200,'FAILED: network',False)]:
     with patch.object(f,'earliest_expiry',return_value=expiry),patch.object(f,'notify',return_value=delivery):
      self.assertEqual(f.main()[0],want)
 def test_packet_gate_failure_is_not_success(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);c=root/'creds';c.write_text('{}');r=root/'registry';r.write_text('{"projects":{}}')
   with patch.object(f,'CREDS',c),patch.object(f,'REGISTRY',r),patch.object(f,'STATE',root/'state'),patch.object(f,'providers_here',return_value=[]),patch.object(f,'served_models',return_value={}),patch.object(f,'log'),patch.object(f.time,'time',return_value=100),patch.object(f,'decide',return_value='run'),patch.object(f,'earliest_expiry',return_value=200),patch.object(f,'notify',return_value='sent'):
    for bad,missing in [(1,[]),(0,['fixture'])]:
     with patch.object(f,'run_all',return_value=(root,2,bad,missing)):self.assertFalse(f.main()[0])
if __name__=='__main__':unittest.main()
