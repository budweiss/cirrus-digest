"""The real builder dispatch must run intake's existing suites and reject failures."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import dev_agent
import intake_lifecycle

ROOT = Path(__file__).resolve().parent


class IntakeBuilderGate(unittest.TestCase):
    def test_builder_discovers_existing_suite_entrypoint(self):
        self.assertEqual(dev_agent.selftest_argvs(ROOT / 'intake_lifecycle.py'), [['--selftest']])

    def test_existing_suites_are_loaded_and_failure_is_nonzero(self):
        for success, expected in [(True, 0), (False, 1)]:
            with patch('unittest.defaultTestLoader.loadTestsFromNames') as load, \
                    patch('unittest.TextTestRunner') as runner:
                load.return_value.countTestCases.return_value = 1
                runner.return_value.run.return_value.wasSuccessful.return_value = success
                self.assertEqual(intake_lifecycle.selftest(), expected)
                self.assertEqual(load.call_args.args[0],
                                 ['test_intake_lifecycle', 'test_held_label', 'test_intake_notice_outbox'])
                runner.return_value.run.assert_called_once_with(load.return_value)

    def test_empty_suite_cannot_pass(self):
        with patch('unittest.defaultTestLoader.loadTestsFromNames') as load:
            load.return_value.countTestCases.return_value = 0
            self.assertEqual(intake_lifecycle.selftest(), 1)

    def test_real_gate_runs_tests_and_rejects_a_planted_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(ROOT / 'intake_lifecycle.py', root / 'intake_lifecycle.py')
            fixture = ('import unittest\nclass Fixture(unittest.TestCase):\n'
                       '    def test_gate(self): self.assertTrue(True)\n')
            for name in ('test_intake_lifecycle', 'test_held_label', 'test_intake_notice_outbox'):
                (root / (name + '.py')).write_text(fixture)
            # Exercise verify_build's real subprocess dispatch, using temporary synthetic
            # fixtures only. Production containment is checked separately on each host.
            def run(args, cwd=None, **kwargs):
                r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
                return r.returncode, r.stdout + r.stderr
            with patch.object(dev_agent, '_run', side_effect=run), \
                    patch.object(dev_agent, 'importers_of', return_value=[]):
                good = dev_agent.verify_build(root, ['intake_lifecycle.py'],
                                              run_dryrun=False, run_remote=False)
                self.assertTrue(good['ok'], good)
                self.assertIn('selftest(1/1)', good['ran'])
                (root / 'test_intake_lifecycle.py').write_text(fixture.replace('True)', 'False)'))
                bad = dev_agent.verify_build(root, ['intake_lifecycle.py'],
                                             run_dryrun=False, run_remote=False)
                self.assertFalse(bad['ok'], bad)
                self.assertEqual(bad['gate'], 'selftest')
                self.assertIn('FAILED', bad['detail'])


if __name__ == '__main__':
    unittest.main()
