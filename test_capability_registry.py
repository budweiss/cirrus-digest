import json,tempfile,unittest
from pathlib import Path
from capability_registry import load_project

class Registry(unittest.TestCase):
    def test_migration_and_removal_do_not_resurrect_legacy(self):
        with tempfile.TemporaryDirectory() as d:
            legacy=Path(d)/'old.json';shared=Path(d)/'model_capabilities.json'
            self.assertIsNone(load_project('task',legacy))
            legacy.write_text(json.dumps({'enabled':True}))
            self.assertEqual(load_project('task',legacy),{'enabled':True})
            shared.write_text(json.dumps({'version':1,'projects':{'task':{'enabled':False}}}))
            self.assertEqual(load_project('task',legacy),{'enabled':False})
            shared.write_text(json.dumps({'version':1,'projects':{}}))
            self.assertIsNone(load_project('task',legacy))

    def test_bad_shared_registry_never_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            legacy=Path(d)/'old.json';shared=Path(d)/'model_capabilities.json'
            legacy.write_text('{"enabled":true}')
            for value in [[],{}, {'version':2,'projects':{}}, {'version':1,'projects':{'task':[]}}]:
                shared.write_text(json.dumps(value))
                with self.assertRaises(ValueError):load_project('task',legacy)

if __name__=='__main__':unittest.main()
