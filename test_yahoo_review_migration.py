"""Synthetic messages only; no real mailbox, credentials or model calls."""
import contextlib
import datetime
import http.client
import json
import io
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent / 'yahoo_review'))
import yahoo_review_lib as lib
import yahoo_review_server as web
import apply_yahoo_actions as actions
import schedule as scheduler
import tunnel
import plistlib


class ReviewTests(unittest.TestCase):
    def test_reused_message_id_does_not_match_different_mail(self):
        first = lib.headers(b'From: first@example.test\nSubject: original\nDate: today\nMessage-ID: <same>\n\n', '1')
        other = dict(first, subject='replacement')
        self.assertNotEqual(lib.fingerprint(first), lib.fingerprint(other))

    def test_keep_period_is_at_least_seven_full_days(self):
        submitted = '2026-10-14T16:00:00-04:00'
        entry = web.batch_entry('1', 'trash_after_7_days_review', 'fp', submitted, 30, 7)
        self.assertEqual(datetime.datetime.fromisoformat(entry['due_at']) -
                         datetime.datetime.fromisoformat(submitted), datetime.timedelta(days=7))

    def test_protected_subject_overrides_model_suggestion(self):
        item = lib.headers(b'From: bank@example.test\nSubject: Your bank statement\n\n', '1')
        with patch.object(lib, 'classify', return_value={'1': {'category': 'filing_candidate', 'reason': 'Routine'}}):
            self.assertEqual(lib.triage([item]), 0)
        self.assertEqual(item['category'], 'keep')

    def test_mismatched_identity_is_not_located_for_an_action(self):
        item = lib.headers(b'From: original@example.test\nSubject: original\nMessage-ID: <same>\n\n', '1')
        entry = {'uid': '1', 'fingerprint': lib.fingerprint(item)}
        with patch.object(lib, 'scan_headers', return_value=[dict(item, subject='different')]):
            self.assertEqual(actions.locate_entries(None, [entry], also_trash=False)[0], {})

    @contextlib.contextmanager
    def server(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            folder = Path(tmp)
            (folder/'page.html').write_text('outdated page')
            for key, value in {'OUTPUT_DIR': folder, 'QUEUE': folder/'queue.json',
                               'BATCH_META': folder/'meta.json', 'LATEST': folder/'page.html'}.items():
                stack.enter_context(patch.object(lib, key, value))
            lib.BATCH_META.write_text(json.dumps({'review_id': 'current', 'fingerprint_version': 2,
                'entries': [{'uid': '1', 'fingerprint': 'one'}, {'uid': '2', 'fingerprint': 'two'}]}))
            stack.enter_context(patch.object(web, 'regenerate', return_value={'headers_fetched': 0}))
            srv = web.ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
            thread = threading.Thread(target=srv.serve_forever, daemon=True); thread.start()
            try: yield srv.server_address[1]
            finally: srv.shutdown(); thread.join(); srv.server_close()

    def post(self, port, data, origin=None):
        conn = http.client.HTTPConnection('127.0.0.1', port)
        headers = {'Content-Type': 'application/json', 'Origin': origin or 'http://127.0.0.1:%d' % port}
        conn.request('POST', '/submit', json.dumps(data), headers)
        result = conn.getresponse(); code = result.status; result.read(); conn.close()
        return code

    def payload(self, review_id='current'):
        return {'review_id': review_id, 'proposed_filing_days': 30,
                'items': [{'uid': '1', 'decision': 'undecided'}, {'uid': '2', 'decision': 'keep'}]}

    def test_decide_later_is_not_recorded_or_excluded(self):
        with self.server() as port:
            self.assertEqual(self.post(port, self.payload()), 200)
            self.assertEqual([x['uid'] for x in lib.load_queue()], ['2'])
            self.assertNotIn('1', lib.tracked_uids())

    def test_cross_origin_cannot_submit_choices(self):
        with self.server() as port:
            self.assertEqual(self.post(port, self.payload(), 'https://untrusted.example'), 403)
            self.assertEqual(lib.load_queue(), [])

    def test_old_page_cannot_submit_against_new_batch(self):
        with self.server() as port:
            self.assertEqual(self.post(port, self.payload('old')), 409)
            self.assertEqual(lib.load_queue(), [])

    def test_client_cannot_backdate_a_keep_period(self):
        with self.server() as port:
            value = self.payload(); value.update(reviewed_at='2000-01-01T00:00:00-05:00', trash_after_days=1)
            value['items'] = [{'uid': '2', 'decision': 'trash_after_7_days_review'}]
            before = lib.now_eastern()
            self.assertEqual(self.post(port, value), 200)
            self.assertGreaterEqual(datetime.datetime.fromisoformat(lib.load_queue()[0]['due_at']),
                                    before + datetime.timedelta(days=7))


    def test_refresh_keeps_draft_identity_for_unchanged_batch(self):
        with self.server():
            item = lib.headers(b'From: fake@example.test\nSubject: Synthetic\n\n', '1')
            item.update(category='review', reason='Synthetic fixture')
            lib.write_batch([item], 'first', 'server')
            initial = lib.batch_meta_now()['review_id']
            lib.write_batch([item], 'second', 'server')
            self.assertEqual(lib.batch_meta_now()['review_id'], initial)

    def test_failed_refresh_does_not_serve_old_actionable_page(self):
        with self.server() as port, patch.object(web, 'regenerate', side_effect=RuntimeError('failed')):
            conn = http.client.HTTPConnection('127.0.0.1', port)
            conn.request('GET', '/')
            res = conn.getresponse()
            self.assertEqual(res.status, 503)
            self.assertNotIn(b'outdated page', res.read()); conn.close()

    def test_atomic_move_reselects_writable_and_journals_result(self):
        with self.server(), tempfile.TemporaryDirectory() as tmp, patch.object(lib, 'LOGS', Path(tmp)):
            item = lib.headers(b'From: fake@example.test\nSubject: Synthetic\nMessage-ID: <x>\n\n', '2')
            entry = web.batch_entry('2', 'trash_after_7_days_review', lib.fingerprint(item),
                (lib.now_eastern()-datetime.timedelta(days=8)).isoformat(), 30, 7)
            lib.save_queue([entry])
            conn = MagicMock(); conn.capabilities = (b'IMAP4rev1',)
            conn.capability.return_value = ('OK', [b'IMAP4rev1 MOVE UIDPLUS'])
            conn.select.return_value = ('OK', [])
            conn.uid.return_value = ('OK', [(b'2', b'From: fake@example.test\nSubject: Synthetic\nMessage-ID: <x>\n\n')])
            def moving(connection, uid, target):
                self.assertEqual(connection.select.call_args.args, ('INBOX',))
                self.assertEqual(connection.select.call_args.kwargs, {'readonly': False})
                self.assertEqual(lib.load_queue()[0]['status'], 'verification_required')
                return 'move'
            with patch.object(lib, 'connect', return_value=conn), patch.object(lib, 'trash_folder', return_value='Trash'), \
                 patch.object(actions, 'locate_entries', return_value=({0:'2'},set())), patch.object(actions, 'move_uid', side_effect=moving):
                actions._apply()
            self.assertEqual(lib.load_queue()[0]['status'], 'applied')

    def test_unconfirmed_move_does_not_fall_back_to_copy(self):
        conn = MagicMock(); conn.uid.side_effect = OSError('disconnected')
        with self.assertRaises(OSError): actions.move_uid(conn, b'2', 'Trash')
        self.assertEqual([x.args[0] for x in conn.uid.call_args_list], ['MOVE'])


class DispatcherTests(unittest.TestCase):
    def test_launchd_parent_passes_credentials_only_in_child_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            creds = root/'Documents/Cowork/cirrus/config/local.json'
            creds.parent.mkdir(parents=True)
            creds.write_text(json.dumps({'cf_ssh_service_token_id':'synthetic-id',
                                         'cf_ssh_service_token_secret':'synthetic-secret'}))
            with patch.object(tunnel.Path, 'home', return_value=root), patch.object(tunnel.os, 'execve') as execute:
                tunnel.main()
            _, args, env = execute.call_args.args
            self.assertNotIn('synthetic-secret', ' '.join(args))
            self.assertEqual(env['TUNNEL_SERVICE_TOKEN_SECRET'], 'synthetic-secret')
            plist = plistlib.loads((Path(__file__).parent/'yahoo_review/com.buddy.yahoo-review-tunnel.plist').read_bytes())
            self.assertEqual(plist['ProgramArguments'][0], '/opt/homebrew/bin/python3')
            self.assertTrue(plist['ProgramArguments'][1].endswith('/yahoo_review_tunnel.py'))

    def dispatch(self, remote, action='collect', exitcode=0):
        with tempfile.TemporaryDirectory() as tmp, patch.object(scheduler, 'ROOT', Path(tmp)), \
             patch.object(scheduler.subprocess, 'run', return_value=MagicMock(stdout=json.dumps(remote), returncode=exitcode)), \
             patch.object(scheduler, 'telegram', return_value=True) as telegram, \
             patch.object(scheduler.job_status, 'record') as record, contextlib.redirect_stdout(io.StringIO()) as out:
            code = scheduler.run(action)
            return code, json.loads(out.getvalue()), telegram.call_count, record.call_args.args[1]

    def test_private_fields_never_leave_dispatcher(self):
        code, result, notices, ok = self.dispatch({'headers_fetched':20,'mailbox_mode':'readonly',
            'mailbox_changes':0,'message_bodies_fetched':0,'batch_fingerprint':'synthetic','classification_failures':0,
            'private_subject':'should not leave worker'})
        self.assertEqual((code,notices,ok),(0,1,True))
        self.assertNotIn('private_subject', result)

    def test_readonly_contract_failure_is_not_success(self):
        code, result, notices, ok = self.dispatch({'headers_fetched':20,'mailbox_mode':'writable',
            'mailbox_changes':0,'message_bodies_fetched':0})
        self.assertEqual((code,notices,ok),(1,1,False))

    def test_empty_review_is_healthy_and_quiet(self):
        code, result, notices, ok = self.dispatch({'error_type':'nothing_new'})
        self.assertEqual((code,notices,ok),(0,0,True))

    def test_unconfirmed_mail_action_needs_attention(self):
        code, result, notices, ok = self.dispatch({'due':0,'failed':1,'fail_categories':{'verification_required':1}},'apply')
        self.assertEqual((code,notices,ok),(1,1,False))


if __name__ == '__main__': unittest.main()
