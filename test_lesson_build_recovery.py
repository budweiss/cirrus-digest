"""Offline recovery regressions; never uses real models or email."""
import hashlib
import json
import tempfile
import unittest
from email.mime.text import MIMEText
from pathlib import Path
from unittest.mock import patch
import lesson_build as lb
import intake_lifecycle as life


class Recovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        self.config(True)
        (self.root / 'config/intake_senders.json').write_text(json.dumps({
            'fixture': {'emails': ['client@example.test'], 'projects': ['pedagogy']}}))
        req = self.root / lb.REQUESTS_REL
        req.mkdir(parents=True)
        (req / 'fixture.json').write_text(json.dumps({'client': 'fixture', 'auto_send': True,
            'threads': [{'from_addr': 'client@example.test', 'mid': '<fixture>',
                         'kinds': ['lessons'], 'subject': 'Lesson plans'}]}))
        self.rd = self.root / 'logs/lesson_builds/fixture'
        self.rd.mkdir()
        (self.rd / 'book.pdf').write_bytes(b'%PDF synthetic')
        life.capture(self.root, (None, 'client@example.test', 'Lesson plans', '', '<fixture>'))
        self.sends = []
        self.kw = dict(project_dir=self.root, send=self.send, creds={'outlook_email': 'sender@example.test'},
                       notify=lambda _: None, verify=lambda *a: list(a[1]), wait=0)

    def config(self, enabled):
        (self.root / 'config/lesson_build.json').write_text(json.dumps({'clients': {'fixture': {'auto_send': enabled}}}))

    def send(self, *args, **kw):
        self.sends.append(args[3])

    def build(self, pdf, kinds, folder, cfg, ctx, **kw):
        for arc in ['Arc A', 'Arc B']:
            kw['on_arc'](arc, [{'n': 1}], {'lessons': (folder / (arc + '.txt'), 'synthetic body')})
        return {'stats': {}}

    def run_job(self, build=None):
        with patch.object(lb, 'build_book', side_effect=build or self.build), patch('client_mail._record_sent'):
            return lb.run('fixture', **self.kw)

    def test_disabled_queued_job_never_sends(self):
        self.config(False)
        self.assertEqual(self.run_job(), 0)
        self.assertEqual(self.sends, [])

    def test_disable_between_arcs(self):
        def send(*a, **kw):
            self.send(*a, **kw)
            self.config(False)
        self.kw['send'] = send
        self.run_job()
        self.assertEqual(len(self.sends), 1)

    def test_post_smtp_ledger_failure_never_resends(self):
        original = Path.open
        def fail(path, mode='r', *a, **kw):
            if path.name == 'deliveries.jsonl' and mode == 'a':
                raise OSError('synthetic disk failure')
            return original(path, mode, *a, **kw)
        with patch.object(Path, 'open', fail):
            self.assertEqual(self.run_job(), 3)
        self.assertEqual(json.loads((self.rd / 'state.json').read_text())['status'], 'held')
        self.assertEqual(self.run_job(), 3)
        self.assertEqual(len(self.sends), 1)

    def test_uncertain_smtp_never_resends(self):
        def fail(*a, **kw):
            self.send(*a, **kw)
            raise TimeoutError('unknown SMTP outcome')
        self.kw['send'] = fail
        self.assertEqual(self.run_job(), 3)
        self.assertEqual(self.run_job(), 3)
        self.assertEqual(len(self.sends), 1)

    def test_unverified_arc_stops_later_arcs_even_on_resume(self):
        self.kw['verify'] = lambda *a: []
        self.assertEqual(self.run_job(), 3)
        self.assertEqual(self.run_job(), 3)
        self.assertEqual(len(self.sends), 1)

    def test_reserved_budget_survives_restart(self):
        contexts = []
        def build(pdf, kinds, folder, cfg, ctx, **kw):
            contexts.append(ctx['cloud_calls'])
            lb.reserve_cloud(ctx)
            raise lb.Hold('synthetic interruption after reservation')
        self.run_job(build)
        self.run_job(build)
        self.assertEqual(contexts, [0, 1])
        self.assertEqual(json.loads((self.rd / 'budget.json').read_text())['cloud_calls'], 2)

    def test_budget_reservation_failure_prevents_provider_call(self):
        ctx = {'creds': {}, 'cloud_calls': 29, 'budget_path': str(self.rd / 'budget.json')}
        with patch.object(lb, 'task_prompt', return_value='fixture'), \
             patch.object(lb, 'local_draft', return_value={}), \
             patch.object(lb, 'lesson_failures', return_value=['failed']), \
             patch.object(lb, 'durable_json', side_effect=OSError('disk')), \
             patch.object(lb, 'cloud_draft') as cloud:
            with self.assertRaises(lb.Hold):
                lb.build_lesson('lessons', {}, {'n': 1}, [], '', {}, ctx)
            cloud.assert_not_called()

    def test_cloud_retry_obeys_limit(self):
        ctx = {'cloud_calls': 29, 'creds': {}}
        with patch.object(lb, 'task_prompt', return_value='fixture'), \
             patch.object(lb, 'local_draft', return_value={}), \
             patch.object(lb, 'lesson_failures', return_value=['failed']), \
             patch.object(lb, 'cloud_draft', side_effect=ValueError('JSON')) as cloud:
            with self.assertRaises(lb.Hold):
                lb.build_lesson('lessons', {}, {'n': 1}, [], '', {}, ctx)
            self.assertEqual(cloud.call_count, 1)
            self.assertEqual(ctx['cloud_calls'], 30)

    def test_legacy_budget_holds_before_build(self):
        (self.rd / 'drafts').mkdir()
        with patch.object(lb, 'build_book') as build:
            self.assertEqual(lb.run('fixture', **self.kw), 3)
            build.assert_not_called()

    def test_run_lock_excludes_second_worker(self):
        import fcntl
        with (self.rd / 'run.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(lb, 'build_book') as build:
                self.assertEqual(lb.run('fixture', **self.kw), 3)
                build.assert_not_called()

    def test_exact_sent_body_and_recipients(self):
        msg = MIMEText('synthetic body')
        msg['To'] = 'client@example.test'
        msg['Cc'] = 'owner@example.test'
        row = {'body_sha256': hashlib.sha256(b'synthetic body').hexdigest()}
        self.assertTrue(lb.exact_message(msg.as_bytes(), row, 'client@example.test', 'owner@example.test'))
        wrong = dict(row, body_sha256=hashlib.sha256(b'wrong body').hexdigest())
        self.assertFalse(lb.exact_message(msg.as_bytes(), wrong, 'client@example.test', 'owner@example.test'))
        msg.replace_header('To', 'client@example.test, extra@example.test')
        self.assertFalse(lb.exact_message(msg.as_bytes(), row, 'client@example.test', 'owner@example.test'))

    def test_successful_run_does_not_resend(self):
        self.assertEqual(self.run_job(), 0)
        self.assertEqual(self.run_job(), 0)
        self.assertEqual(len(self.sends), 2)

    def test_cache_acceptance_invalidated_by_source_or_draft_change(self):
        guide = self.rd / 'guide.json'
        guide.write_text('{"level": 4}')
        book = {'level': 4, 'module': 1, 'arcs': ['Arc A'], 'lessons': [{'n': 1, 'arc': 'Arc A'}]}
        pdf = self.rd / 'book.pdf'
        cfg, ctx = {'clients': {}}, {}
        with patch.object(lb, 'GUIDE_PATH', guide), \
             patch.object(lb, 'pdf_pages', return_value=['source']), \
             patch.object(lb, 'pdf_title', return_value='title'), \
             patch.object(lb, 'split_book', return_value=book), \
             patch.object(lb, 'codes_by_lesson', return_value={1: []}), \
             patch.object(lb, 'lesson_text', return_value='source'), \
             patch.object(lb, 'assemble', return_value='body'), \
             patch.object(lb, 'build_lesson', return_value={'draft': {'text': 'good'}, 'stage': 'local', 'tries': []}) as build:
            lb.build_book(pdf, ['lessons'], self.rd, cfg, ctx)
            lb.build_book(pdf, ['lessons'], self.rd, cfg, ctx)
            self.assertEqual(build.call_count, 1)
            cache = self.rd / 'drafts/lessons-01.json'
            accepted = cache.read_text()
            data = json.loads(accepted)
            data['draft']['text'] = 'unreviewed edit'
            cache.write_text(json.dumps(data))
            with self.assertRaises(lb.Hold):
                lb.build_book(pdf, ['lessons'], self.rd, cfg, ctx)
            cache.write_text(accepted)
            pdf.write_bytes(b'%PDF changed')
            with self.assertRaises(lb.Hold):
                lb.build_book(pdf, ['lessons'], self.rd, cfg, ctx)
            self.assertEqual(build.call_count, 1)


if __name__ == '__main__':
    unittest.main()
