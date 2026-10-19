"""Import the real digest with synthetic config; never fetch mail or send it."""
import json
import os
from pathlib import Path
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch
import runtime_isolation


class DailyOutputIsolation(unittest.TestCase):
    def test_real_digest_import_uses_private_dryrun_paths(self):
        source=Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)/'home';config=home/'projects/cirrus-digest/config';config.mkdir(parents=True)
            root=Path(td)/'source';root.mkdir()
            # Source code only, no runtime files, cookies or credential configs.
            for module in source.glob('*.py'):
                shutil.copyfile(module,root/module.name)
            data=json.loads((source/'config/sources.json').read_text())
            data['email']['accounts']=[]
            data['digest'].update(output_dir=str(home/'shared-output'),log_dir=str(home/'shared-log'))
            (config/'sources.json').write_text(json.dumps(data))
            code='''import os,sys,json
from pathlib import Path
sys.argv=['cirrus_daily.py','--dry-run']
import cirrus_daily as d
p=Path(os.environ['COWORK_DRYRUN_ROOT'])
assert d.OUTPUT_DIR==p/'digests'
assert d.LOG_DIR==p/'logs'
assert d.REFRESH_FLAG==p/'logs/cookie_refresh.needed'
print(json.dumps({'private_output':True}))
'''
            with patch.dict(os.environ,{'HOME':str(home)}):
                r=runtime_isolation.run([sys.executable,'-c',code,'--dry-run'],root,offline=False)
            self.assertEqual(r.returncode,0,r.stderr[-1200:])
            self.assertIn('"private_output": true',r.stdout)
            self.assertFalse((home/'shared-output').exists())
            self.assertFalse((home/'shared-log').exists())

if __name__=='__main__':unittest.main()
