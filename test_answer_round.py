import concurrent.futures
import contextlib
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from answer_round import (DeadlineReached, Held, Ledger, digest, execute, hard_deadline,
                          private_write, validate)


def packet():
    text = 'The fictional cohort contained twelve mice. No human benefit is established.'
    return {'question': 'State the cohort and the limitation.', 'sources': {'S01': text},
            'original_sources': {'original': text},
            'registry': {'S01': {'original_source_key': 'original', 'start': 0, 'end': len(text)}}}


def manifest():
    return {'authorization': 'Synthetic test only', 'items': [
        {'name': name, 'predecessor_id': 'old-' + name, 'packet': packet(),
         'allowed_source_aliases': ['S01']} for name in ('size', 'quotation')]}


def result():
    return {'provider': 'ollama', 'model': 'synthetic', 'raw': json.dumps({
        'answer': 'There were twelve mice; human benefit is unestablished.',
        'reason': 'The source states both the cohort and limitation.', 'confidence': .95,
        'verdict': 'pass', 'kind': 'grounded', 'missing_request_quote': '',
        'checks': dict.fromkeys(['supported', 'complete', 'scope_correct', 'no_unperformed_actions'], True),
        'evidence': [{'source': 'S01', 'quote': packet()['sources']['S01']}]})}


class FakeModels:
    def __init__(self, fail=False, bad=False):
        self.calls, self.fail, self.bad = [], fail, bad
    def preflight(self):
        return 'synthetic'
    def call(self, payload, deadline, session_id):
        self.calls.append((copy.deepcopy(payload), deadline, session_id))
        if self.fail:
            raise RuntimeError('This private body must not reach receipts or stdout')
        return {'provider': 'ollama', 'raw': '{}'} if self.bad else result()


class RoundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'round'
        self.now = 1000.0
        self.ledger = Ledger(self.directory, lambda: self.now)
        self.ledger.initialize(manifest())

    def test_four_calls_and_no_fifth_or_reset(self):
        for item in ('size', 'quotation'):
            self.ledger.reserve(item, 'draft')
            self.ledger.finish(item, 'draft', True)
            self.ledger.reserve(item, 'verify')
            self.ledger.finish(item, 'verify', True)
        self.assertEqual(self.ledger.summary()['calls'], 4)
        with self.assertRaises(Held): self.ledger.reserve('size', 'draft')
        with self.assertRaises(FileExistsError): self.ledger.initialize(manifest())
        self.assertEqual(self.ledger.summary()['calls'], 4)

    def test_deadline_survives_reopen_and_clock_reversal(self):
        self.assertEqual(self.ledger.reserve('size', 'draft'), 1600)
        self.ledger.finish('size', 'draft', True)
        reopened = Ledger(self.directory, lambda: self.now)
        self.now = 1600
        with self.assertRaises(Held): reopened.reserve('size', 'verify')
        self.now = 999
        with self.assertRaises(Held): reopened.reserve('quotation', 'draft')
        self.assertEqual(reopened.summary()['calls'], 1)

    def test_failed_calls_count_and_no_retry(self):
        self.ledger.reserve('size', 'draft')
        self.ledger.finish('size', 'draft', False, 'provider_failure')
        with self.assertRaises(Held): self.ledger.reserve('size', 'draft')
        self.assertEqual(self.ledger.summary()['calls'], 1)

    def test_concurrent_reservation_claims_only_once(self):
        def reserve(_):
            try: self.ledger.reserve('size', 'draft'); return 1
            except Held: return 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(reserve, range(4))), 1)

    def test_interrupted_call_never_replayed(self):
        self.ledger.reserve('size', 'draft')
        self.ledger.recover()
        with self.assertRaises(Held): self.ledger.reserve('size', 'draft')
        self.assertEqual(self.ledger.item_state('size'), 'held')

    def test_manifest_change_refused(self):
        value = manifest(); value['items'][0]['packet']['question'] += ' Altered scope.'
        (self.directory / 'manifest.json').write_text(json.dumps(value))
        with self.assertRaises(Held): self.ledger.reserve('size', 'draft')
        self.assertEqual(self.ledger.summary()['calls'], 0)

    def test_persisted_limits_cannot_be_expanded(self):
        with contextlib.closing(self.ledger.connect()) as db, db:
            db.execute('UPDATE control SET max_calls=8')
        with self.assertRaises(Held): self.ledger.reserve('size', 'draft')
        self.assertEqual(self.ledger.summary()['calls'], 0)

    def test_predecessor_files_are_not_modified(self):
        predecessor = Path(self.temp.name) / 'old-budget.json'
        predecessor.write_text('{"calls":4,"started":1}')
        old = predecessor.read_bytes()
        fresh = manifest()
        for item in fresh['items']:
            item['predecessor_budget_path'] = str(predecessor)
        directory = Path(self.temp.name) / 'linked-round'
        Ledger(directory).initialize(fresh)
        execute(directory, FakeModels())
        self.assertEqual(predecessor.read_bytes(), old)

    def test_real_timer_interrupts_blocking_provider(self):
        started = time.monotonic()
        with self.assertRaises(DeadlineReached):
            with hard_deadline(time.time() + .03): time.sleep(1)
        self.assertLess(time.monotonic() - started, .5)

    def test_success_flow_is_exactly_two_calls_each(self):
        model = FakeModels()
        original = (self.directory / 'manifest.json').read_bytes()
        summary = execute(self.directory, model)
        self.assertEqual((summary['calls'], summary['verified']), (4, 2))
        self.assertEqual([p['mode'] for p, _, _ in model.calls], ['draft', 'review', 'draft', 'review'])
        self.assertEqual((self.directory / 'manifest.json').read_bytes(), original)
        self.assertEqual(model.calls[0][0]['question'], packet()['question'])
        execute(self.directory, model)
        self.assertEqual(len(model.calls), 4)

    def test_invalid_output_skips_verification_and_repairs(self):
        model = FakeModels(bad=True)
        summary = execute(self.directory, model)
        self.assertEqual((summary['calls'], summary['held']), (2, 2))
        self.assertEqual(len(model.calls), 2)

    def test_private_exception_is_not_saved(self):
        summary = execute(self.directory, FakeModels(fail=True))
        self.assertEqual(summary['calls'], 2)
        receipt = (self.directory / 'size-draft-held.json').read_text()
        self.assertNotIn('This private body', receipt)
        self.assertIn('RuntimeError', receipt)

    def test_quote_mismatch_and_oversize_refused(self):
        reply = result(); parsed = json.loads(reply['raw'])
        parsed['evidence'][0]['quote'] = 'Twelve human participants.'
        reply['raw'] = json.dumps(parsed)
        with self.assertRaises(Held): validate(reply, packet(), ['S01'])

    def test_unsubstantiated_suggestion_is_not_research_verification(self):
        reply = result(); parsed = json.loads(reply['raw'])
        parsed.update(kind='suggestion', evidence=[])
        reply['raw'] = json.dumps(parsed)
        with self.assertRaises(Held): validate(reply, packet(), ['S01'])
        parsed = json.loads(result()['raw']); parsed['answer'] = 'x' * 1800
        reply['raw'] = json.dumps(parsed)
        with self.assertRaises(Held): validate(reply, packet(), ['S01'])


if __name__ == '__main__': unittest.main()
