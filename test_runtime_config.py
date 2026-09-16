"""Offline configuration migrations: no services, network, or real mailboxes."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import runtime_config as runtime
from tools.migrate_runtime_config import migrate


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config'
        self.config.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.STDOUT)

    def source(self, name='sources.json'):
        path = self.config / name
        base = {'digest': {'output_dir': '/Users/test/digests', 'log_dir': '/Users/test/logs'},
                'rss': [{'name': 'tracked', 'rss': 'https://example.invalid/base'}],
                'email': {'accounts': []}}
        path.write_text(json.dumps(base))
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')
        return path, base

    def test_migration_preserves_effective_config_and_unpins(self):
        path, current = self.source()
        current['digest'].update(output_dir=str(self.root/'digests'), log_dir=str(self.root/'logs'))
        current['email']['accounts'].append({'label': 'cumulus-research', 'enabled': True})
        path.write_text(json.dumps(current))
        self.git('update-index', '--skip-worktree', 'config/sources.json')
        with patch.object(runtime.platform, 'system', return_value='Linux'):
            before = runtime.load_sources(path)
            original = path.read_bytes()
            migrate(path)
            self.assertEqual(path.read_bytes(), original)
            result = migrate(path, apply=True)
            self.assertEqual(runtime.load_sources(path), before)
            self.assertEqual(path.read_bytes(), self.git('show', 'HEAD:config/sources.json'))
            self.assertTrue(self.git('ls-files', '-v', 'config/sources.json').startswith(b'H '))
            self.assertTrue(Path(result['backup']).exists())
            self.assertEqual(migrate(path, apply=True)['status'], 'already migrated')
            # Simulate an incoming tracked config update after migration.
            incoming = json.loads(path.read_text())
            incoming['rss'].append({'name': 'new', 'rss': 'https://example.invalid/new'})
            path.write_text(json.dumps(incoming))
            after = runtime.load_sources(path)
            self.assertEqual(after['digest'], before['digest'])
            self.assertEqual(after['email'], before['email'])
            self.assertEqual(len(after['rss']), 2)

    def test_deploy_check_rejects_invalid_pedagogy_path(self):
        path, base = self.source()
        runtime.runtime_path(path).write_text(json.dumps({'digest': {
            'output_dir': str(self.root/'digests'), 'log_dir': str(self.root/'logs')}}))
        pedagogy = self.config/'sources-pedagogy.json'
        pedagogy.write_text(json.dumps({'digest': {'output_dir': '/Users/wrong/pedagogy'}}))
        with patch.object(runtime.platform, 'system', return_value='Linux'):
            with self.assertRaisesRegex(ValueError, 'macOS path'):
                runtime.check_all(self.config)

    def test_unrelated_local_change_blocks_migration_without_data_loss(self):
        path, current = self.source()
        current['digest'].update(output_dir=str(self.root/'digests'), log_dir=str(self.root/'logs'))
        current['recipient'] = 'changed@example.invalid'
        path.write_text(json.dumps(current)); original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'runtime behavior'):
            migrate(path, apply=True)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(runtime.runtime_path(path).exists())

    def test_pedagogy_has_separate_paths_and_persistent_feed_additions(self):
        path, base = self.source('sources-pedagogy.json')
        runtime.runtime_path(path).write_text(json.dumps({'digest': {'output_dir': str(self.root/'pedagogy')}}))
        self.config.joinpath('runtime.local.json').write_text(json.dumps({'digest': {'output_dir': '/wrong/main'}}))
        original = path.read_bytes()
        accepted = [{'type': 'blog', 'name': 'new', 'feed': 'https://example.invalid/new'}]
        runtime.save_pedagogy_sources(path, accepted)
        runtime.save_pedagogy_sources(path, accepted)
        with patch.object(runtime.platform, 'system', return_value='Linux'):
            cfg = runtime.load_sources(path)
        self.assertEqual(cfg['digest']['output_dir'], str(self.root/'pedagogy'))
        self.assertEqual(len(cfg['rss']), 2)
        self.assertEqual(path.read_bytes(), original)
        local = self.config/'sources-pedagogy.local.json'
        self.assertNotIn('digest', json.loads(local.read_text()))
        local.write_text('{broken')
        with self.assertRaises(ValueError):
            runtime.load_sources(path)


if __name__ == '__main__':
    unittest.main()
