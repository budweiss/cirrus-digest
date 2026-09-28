"""I06: kill a scratch-only writer at each side of the atomic replace boundary."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CHILD=r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import alert_policy, opus_approval
path=Path(sys.argv[2]);kind=sys.argv[3];boundary=sys.argv[4]
real=os.replace
def crash(src,dst):
    if boundary=='before':os._exit(73)
    real(src,dst)
    os._exit(73)
os.replace=crash
if kind=='request':
    opus_approval._write_json(path,{'request_id':'old','status':'pending','owner':'Cowork','delivery_attempted_at':200})
else:
    policy=alert_policy.IncidentPolicy(path)
    policy.active['snow']['next_check']=200
    policy.save()
'''

class CrashPersistence(unittest.TestCase):
    def test_process_crash_preserves_atomic_state_and_unrelated_decisions(self):
        for kind in ('request','incident'):
            for boundary in ('before','after'):
                with self.subTest(kind=kind,boundary=boundary),tempfile.TemporaryDirectory() as td:
                    root=Path(td);path=root/'state.json'
                    old=({'request_id':'old','status':'pending','owner':'Cowork','delivery_attempted_at':100} if kind=='request' else
                         {'incidents':{'snow':{'first_seen':1,'state':'action pending','owner':'Cowork','next_check':100}},'resolved':[]})
                    path.write_text(json.dumps(old))
                    other=root/'other.json';other.write_text('{"request_id":"other","status":"pending"}')
                    receipt=root/'receipt';receipt.write_text('delivery-42')
                    proc=subprocess.run([sys.executable,'-c',CHILD,str(Path(__file__).parent),str(path),kind,boundary],capture_output=True,text=True,timeout=10)
                    self.assertEqual(proc.returncode,73,proc.stderr)
                    restored=json.loads(path.read_text())
                    if boundary=='before':self.assertEqual(restored,old)
                    elif kind=='request':
                        self.assertEqual(restored['delivery_attempted_at'],200)
                        self.assertEqual(restored['status'],'pending');self.assertEqual(restored['request_id'],'old')
                    else:
                        self.assertEqual(restored['incidents']['snow']['next_check'],200)
                        self.assertEqual(restored['incidents']['snow']['state'],'action pending')
                        self.assertEqual(restored['resolved'],[])
                    self.assertEqual(json.loads(other.read_text()),{'request_id':'other','status':'pending'})
                    self.assertEqual(receipt.read_text(),'delivery-42')

if __name__=='__main__':unittest.main()
