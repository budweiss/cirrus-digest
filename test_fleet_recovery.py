import json,os,signal,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from fleet_recovery import cancel_child,checkpoint
class RecoveryTests(unittest.TestCase):
 def child(self,code):
  p=subprocess.Popen([sys.executable,'-u','-c',code],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
  self.addCleanup(self.cleanup,p)
  self.assertEqual(p.stdout.readline().strip(),'ready')
  return p
 def cleanup(self,p):
  if p.poll() is None:os.killpg(p.pid,signal.SIGKILL);p.wait()
  p.stdout.close();p.stderr.close()
 def test_D01_graceful_exact_child(self):
  other=self.child('import time;print("ready");time.sleep(60)')
  child=self.child('import signal,time,sys;signal.signal(signal.SIGTERM,lambda *_:sys.exit(0));print("ready");time.sleep(60)')
  r=cancel_child(child,1);self.assertTrue(r['stopped']);self.assertFalse(r['forced']);self.assertIsNone(other.poll())
 def test_D02_force_only_if_permitted(self):
  code='import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print("ready");time.sleep(60)'
  child=self.child(code);r=cancel_child(child,.1);self.assertFalse(r['stopped']);self.assertIsNone(child.poll())
  r=cancel_child(child,.1,True);self.assertTrue(r['stopped']);self.assertTrue(r['forced'])
 def test_C09_bare_pid_not_authority(self):
  with self.assertRaises(ValueError):cancel_child(os.getpid())
 def test_D06_checkpoint_identity(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'checkpoint.json';d={'version':1,'project':'a','input_hash':'abc','completed_sections':[0,1]};p.write_text(json.dumps(d))
   self.assertEqual(checkpoint(p,'a','abc')['completed_sections'],[0,1])
   for field,value in [('version',2),('project','b'),('input_hash','wrong'),('completed_sections',[-1])]:
    bad=dict(d);bad[field]=value;p.write_text(json.dumps(bad))
    with self.assertRaises(ValueError):checkpoint(p,'a','abc')
if __name__=='__main__':unittest.main()
