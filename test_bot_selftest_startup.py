"""S523: exercise the actual bot test entry point without live config/network."""
import os
from pathlib import Path
import site
import subprocess
import sys
import tempfile
import unittest


class BotTestStartup(unittest.TestCase):
    def invoke(self, mode):
        root = Path(__file__).resolve().parent
        # Fail if startup even tries to read production config or open a socket.
        program = '''
import pathlib, runpy, sys
def guard(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        if pathlib.Path(args[0]).name in ('sources.json', 'credentials.json', 'runtime.local.json'):
            raise RuntimeError('LIVE_CONFIG_READ_FORBIDDEN')
    if event in ('socket.connect', 'socket.bind', 'socket.getaddrinfo'):
        raise RuntimeError('NETWORK_FORBIDDEN')
sys.addaudithook(guard)
mode = sys.argv[1]
sys.argv = ['cirrus_bot.py'] + (['--selftest'] if mode == 'test' else [])
runpy.run_path('cirrus_bot.py', run_name='__main__' if mode != 'import' else 'bot_import_probe')
'''
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home)
            user_site = site.getusersitepackages()
            env['PYTHONPATH'] = os.pathsep.join(
                [str(root)] + ([user_site] if isinstance(user_site, str) else user_site))
            return subprocess.run([sys.executable, '-c', program, mode],
                                  cwd=root, env=env, capture_output=True,
                                  text=True, timeout=60)

    def test_selftest_runs_without_live_configuration_or_network(self):
        result = self.invoke('test')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('SELFTEST OK:', result.stdout)

    def test_service_and_import_still_require_real_configuration(self):
        for mode in ('service', 'import'):
            with self.subTest(mode=mode):
                result = self.invoke(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('LIVE_CONFIG_READ_FORBIDDEN', result.stderr)


if __name__ == '__main__':
    unittest.main()
