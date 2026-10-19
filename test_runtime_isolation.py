"""Real kernel probes against synthetic worktrees; no services or client data."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
import runtime_isolation as isolation


class IsolationTests(unittest.TestCase):
    def test_two_worktrees_cannot_share_writes_or_ports(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a, b = root/'a', root/'b'
            a.mkdir(); b.mkdir()
            sentinel = b/'state.db'; sentinel.write_text('untouched')
            (a/'escape').symlink_to(b, target_is_directory=True)
            code = '''import json,os,socket,sqlite3,tempfile
from pathlib import Path
home=Path.home(); home.joinpath('home-marker').write_text('private')
cache=Path(os.environ['XDG_CACHE_HOME']);cache.joinpath('cache').write_text('private')
c=sqlite3.connect('local.db');c.execute('create table probe(x)');c.commit();c.close()
blocked=[]
for target in [Path(%r),Path('escape/state.db')]:
 try:target.write_text('BAD');blocked.append(False)
 except PermissionError:blocked.append(True)
try:
 s=socket.socket();s.bind(('127.0.0.1',0));blocked.append(False)
except PermissionError:blocked.append(True)
try:Path(%r).read_text();blocked.append(False)
except PermissionError:blocked.append(True)
print(json.dumps({'blocked':blocked,'home':str(home),'tmp':tempfile.gettempdir()}))
''' % (str(sentinel),str(sentinel))
            r=isolation.run([sys.executable,'-c',code],a)
            self.assertEqual(r.returncode,0,r.stderr)
            d=json.loads(r.stdout);self.assertEqual(d['blocked'],[True,True,True,True])
            self.assertEqual(sentinel.read_text(),'untouched')
            self.assertTrue((a/'local.db').exists())
            self.assertNotEqual(d['home'],str(Path.home()))
            def probe(p):
                return isolation.run([sys.executable,'-c',
                    'import json,os;print(json.dumps([os.environ["HOME"],os.environ["TMPDIR"]]))'],p)
            with ThreadPoolExecutor() as pool:
                results=list(pool.map(probe,[a,b]))
            for result in results:self.assertEqual(result.returncode,0,result.stderr)
            self.assertNotEqual(json.loads(results[0].stdout),json.loads(results[1].stdout))

    def test_dryruns_have_unique_output_and_cannot_bind_or_write_neighbours(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);wt=root/'wt';wt.mkdir();other=root/'other';other.write_text('safe')
            code = """import os,json,socket
from pathlib import Path
p=Path(os.environ['COWORK_DRYRUN_ROOT']);(p/'result').write_text('own output')
blocked=[]
try:Path(%r).write_text('BAD');blocked.append(False)
except PermissionError:blocked.append(True)
try:s=socket.socket();s.bind(('127.0.0.1',0));blocked.append(False)
except PermissionError:blocked.append(True)
print(json.dumps({'output':str(p),'blocked':blocked}))
""" % str(other)
            results=[isolation.run([sys.executable,'-c',code,'--dry-run'],wt,offline=False) for _ in range(2)]
            for r in results:self.assertEqual(r.returncode,0,r.stderr)
            data=[json.loads(r.stdout) for r in results]
            self.assertNotEqual(data[0]['output'],data[1]['output'])
            self.assertTrue(all(d['blocked']==[True,True] for d in data))
            self.assertEqual(other.read_text(),'safe')

    def test_timeout_stops_descendant_writes(self):
        import subprocess,time
        with tempfile.TemporaryDirectory() as td:
            code = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',\"import time;from pathlib import Path;time.sleep(0.6);Path('late').write_text('BAD')\"]);time.sleep(5)"
            with self.assertRaises(subprocess.TimeoutExpired):
                isolation.run([sys.executable,'-c',code],td,timeout=0.2)
            time.sleep(0.8)
            self.assertFalse((Path(td)/'late').exists())

    def test_actual_build_and_remote_gates_contain_absolute_writes(self):
        import dev_agent,remote_verify
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);wt=root/'wt';wt.mkdir();other=root/'live-state';other.write_text('safe')
            script="""import sys
from pathlib import Path
def selftest():
 Path(%r).write_text('BAD')
 return True
if __name__ == '__main__':
 if '--selftest' in sys.argv:sys.exit(0 if selftest() else 1)
""" % str(other)
            (wt/'probe.py').write_text(script)
            result=dev_agent.verify_build(wt,['probe.py'],run_dryrun=False,run_remote=False)
            self.assertFalse(result['ok'])
            self.assertEqual(result['gate'],'selftest')
            self.assertEqual(other.read_text(),'safe')
            remote=remote_verify.run_selftests(wt,['probe.py'])
            self.assertNotEqual(remote['probe.py']['rc'],0)
            self.assertEqual(other.read_text(),'safe')

    def test_live_checkout_is_not_an_allowed_writable_root(self):
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            live=Path(td)/'projects/cirrus-digest';live.mkdir(parents=True)
            with patch.dict(os.environ,{'HOME':td}):
                with self.assertRaisesRegex(ValueError,'live operational'):
                    isolation.run([sys.executable,'-c','pass'],live)

    def test_parent_credentials_not_inherited(self):
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ,{'SYNTHETIC_API_KEY':'not-a-secret'}):
            r=isolation.run([sys.executable,'-c',
                'import os;assert "SYNTHETIC_API_KEY" not in os.environ'],td)
            self.assertEqual(r.returncode,0,r.stderr)

if __name__=='__main__':unittest.main()
