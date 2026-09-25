"""Offline lease, evidence and failure-path tests; never use live services."""
import json
import fcntl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import alopecia_medical as medical
import local_specialists as specialists


class SpecialistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = {'endpoint': 'http://127.0.0.1:11434',
                    'protected_models': ['general'], 'max_loaded_models': 2,
                    'specialists': {'medical_evidence': {'enabled': True, 'model': 'med',
                    'minimum_available_gib': 36, 'num_ctx': 8192,
                    'num_predict': 1024, 'timeout_seconds': 240}}}
        self.calls = []
        self.loaded = ['general']
        self.fail_chat = False
        self.fail_unload = False

    def api(self, endpoint, path, body=None, timeout=10):
        self.calls.append((path, body))
        if path == '/api/tags':
            return {'models': [{'name': 'med'}]}
        if path == '/api/ps':
            return {'models': [{'name': name} for name in self.loaded]}
        if path == '/api/chat':
            self.loaded.append('med')
            if self.fail_chat:
                raise TimeoutError()
            return {'model': 'med', 'done': True, 'done_reason': 'stop',
                    'message': {'content': '{"claims":[],"abstain":true}'}}
        if path == '/api/generate':
            if self.fail_unload:
                raise TimeoutError()
            self.loaded = ['general']
            return {'done': True}
        raise AssertionError(path)

    def generate(self):
        with patch.object(specialists, 'configuration', return_value=self.cfg), \
             patch.object(specialists, 'request', side_effect=self.api), \
             patch.object(specialists, 'available_gib', return_value=44), \
             patch('llm_budget.record_call'):
            return specialists.generate('medical_evidence', [], root=self.root)

    def test_success_releases_only_specialist(self):
        self.generate()
        self.assertEqual(self.loaded, ['general'])
        chat = next(body for path, body in self.calls if path == '/api/chat')
        self.assertEqual(chat['keep_alive'], 0)
        event = json.loads((self.root / 'logs/local-specialists/events.jsonl').read_text())
        self.assertTrue(event['unloaded'])

    def test_timeout_still_unloads(self):
        self.fail_chat = True
        with self.assertRaises(TimeoutError):
            self.generate()
        self.assertEqual(self.loaded, ['general'])

    def test_unload_failure_is_visible(self):
        self.fail_unload = True
        with self.assertRaisesRegex(specialists.Unavailable, 'unload_not_confirmed'):
            self.generate()

    def test_slot_guard_does_not_evict_generalist(self):
        self.loaded.append('embedding')
        with self.assertRaisesRegex(specialists.Unavailable, 'resident_slots_full'):
            self.generate()
        self.assertFalse(any(path == '/api/chat' for path, _ in self.calls))

    def test_memory_guard(self):
        self.cfg['specialists']['medical_evidence']['minimum_available_gib'] = 100
        with self.assertRaisesRegex(specialists.Unavailable, 'insufficient_memory'):
            self.generate()

    def test_unknown_specialist(self):
        self.cfg['specialists'] = {}
        with self.assertRaisesRegex(specialists.Unavailable, 'not_enabled'):
            self.generate()

    def test_concurrent_request_is_refused(self):
        directory = self.root / 'logs/local-specialists'
        directory.mkdir(parents=True)
        with (directory / 'lease.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(specialists.Unavailable, 'specialist_busy'):
                self.generate()
        self.assertFalse(any(path == '/api/chat' for path, _ in self.calls))

    def test_quotes_and_abstention(self):
        source = {'S1': {'text': 'This observational study does not establish causation.'}}
        raw = json.dumps({'claims': [{'source_id': 'S1', 'quote': source['S1']['text']}], 'abstain': False})
        self.assertEqual(len(medical.validate(raw, source)['claims']), 1)
        with self.assertRaisesRegex(ValueError, 'unsupported_quote'):
            medical.validate(raw.replace('does not', 'does indeed'), source)
        with self.assertRaises(ValueError):
            medical.validate(raw.replace('S1', 'S99'), source)
        self.assertTrue(medical.validate('{"claims":[],"abstain":true}', {})['abstain'])

    def test_no_hits_never_loads_model(self):
        with patch.object(medical.alopecia_kb, 'query', return_value=[]), \
             patch.object(specialists, 'generate') as generate:
            self.assertTrue(json.loads(medical.extract('unknown'))['abstain'])
            generate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
