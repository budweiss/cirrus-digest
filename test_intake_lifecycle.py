"""Synthetic lost-fetch, crash, full-body and handoff regressions. No real mail."""
import email.message
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import intake
import intake_lifecycle as life
import client_watch

class LifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.row=(1,'client@example.test','Question','full body','<stable@example.test>')
    def test_durable_dedup_and_limit_recovery(self):
        key=life.capture(self.root,self.row,b'original')
        life.capture(self.root,self.row,b'replacement')
        self.assertEqual(life.pending(self.root),[list(self.row)])
        self.assertEqual((self.root/'data/intake-lifecycle'/f'{key}.eml').read_bytes(),b'original')
        self.assertEqual(life.summary(self.root,time.time()+1201)['attention'][0]['state'],'queued')
        self.assertTrue(life.begin(self.root,self.row));self.assertFalse(life.begin(self.root,self.row))
    def test_catchup_does_not_overwrite_existing_completion(self):
        life.capture(self.root,self.row)
        life.update(self.root,self.row,'sent_verified','review','done',{'sent_receipt':'synthetic proof'})
        life.hold_catchup(self.root,self.row)
        self.assertEqual(life.summary(self.root)['counts'],{'sent_verified':1})

    def test_cross_mailbox_claim_has_one_owner(self):
        key=life.identity(self.row[1],self.row[4])
        self.assertEqual(life.claim(self.root,key,'cirrus'),'cirrus')
        self.assertEqual(life.claim(self.root,key,'cumulus1'),'cirrus')
        self.assertEqual(life.claim(self.root,key,'cirrus'),'cirrus')
    def test_crash_never_replays_possible_send(self):
        life.begin(self.root,self.row)
        self.assertEqual(life.recover(self.root),1)
        self.assertFalse(life.begin(self.root,self.row))
        self.assertEqual(life.summary(self.root)['attention'][0]['state'],'blocked_on_evidence')
    def test_no_worker_explicit(self):
        life.capture(self.root,self.row)
        for kind in ('feedback','deliverable','confirmation'):
            life.finish(self.root,self.row,{'kind':kind})
            hit=life.summary(self.root,time.time()+1201)['attention'][0]
            self.assertEqual((hit['state'],hit['owner']),('queued_preparation','client_request_prepare'))
    def test_ack_or_smtp_not_delivery_proof(self):
        life.capture(self.root,self.row)
        life.finish(self.root,self.row,{'kind':'answer','ack_sent':True,'solved':{'answered':True}})
        self.assertEqual(life.summary(self.root)['attention'][0]['state'],'sent_unverified')
        for state in ('sent_verified','ready_for_send'):
            with self.assertRaises(ValueError):life.update(self.root,self.row,state,'review','done')
    def test_other_worker_and_waiting_state(self):
        life.capture(self.root,self.row)
        life.finish(self.root,self.row,{'contact_list_run':'fixture'})
        self.assertEqual(life.recover(self.root),0)
        life.update(self.root,self.row,'waiting_on_client','cowork','await reply')
        self.assertEqual(life.summary(self.root,time.time()+86400)['attention'],[])
    def test_corruption_not_healthy(self):
        life.capture(self.root,self.row)
        (self.root/'data/intake-lifecycle/requests.sqlite3').write_text('broken')
        with self.assertRaises(Exception):life.summary(self.root)
    def scan(self,fail=False,state=None,one=False):
        msg=email.message.EmailMessage();msg['From']='client@example.test'
        msg['Message-ID']='<fixture@example.test>';msg['Subject']='Question'
        msg.set_content('x'*5000+' IMPORTANT FINAL REQUIREMENT');raw=msg.as_bytes()
        class Mail:
            def __init__(self,*a,**k):pass
            def login(self,*a):pass
            def select(self,*a,**k):return 'OK',[]
            def status(self,*a):return 'OK',[b'INBOX (UIDVALIDITY 7)']
            def logout(self):pass
            def uid(self,op,*args):
                if op=='search':return 'OK',[b'11' if one else b'11 12']
                if args[1]=='(RFC822.SIZE)':return 'OK',[('RFC822.SIZE '+str(len(raw))).encode()]
                if fail and args[0]=='11':return 'NO',[]
                return 'OK',[(b'BODY',raw)]
        if state is None:state={'last_uid':10,'uidvalidity':7,'mailbox':'"[Gmail]/All Mail"'}
        with patch.object(intake.imaplib,'IMAP4_SSL',Mail),patch.object(intake,'log'):
            result=intake.scan_inbox({'imap_server':'fixture','address':'owned@example.test'},'unused',{'client@example.test':{'name':'fixture'}},state,capture_root=self.root)
        return state,result
    def test_migration_failure_stays_held_on_next_poll(self):
        state={'last_uid':9,'uidvalidity':7,'mailbox':'inbox'}
        state,_=self.scan(True,state,one=True)
        self.assertEqual(state['migration_hold_through_uid'],11)
        self.assertEqual(life.pending(self.root),[])
        state,_=self.scan(False,state,one=True)
        self.assertEqual(life.pending(self.root),[])
        self.assertEqual(life.summary(self.root)['attention'][0]['state'],'blocked_on_evidence')

    def test_failed_fetch_not_lost(self):
        state,_=self.scan(True)
        self.assertEqual(state['last_uid'],10);self.assertEqual(state['scan_failed_uids'],[11])
        self.assertEqual(len(life.pending(self.root)),1)
    def test_full_body_and_dedup(self):
        state,(rows,_)=self.scan()
        self.assertEqual(state['last_uid'],12);self.assertEqual(len(rows),1)
        self.assertIn('IMPORTANT FINAL REQUIREMENT',rows[0][3]);self.assertNotIn('TRUNCATED',rows[0][3])
    def test_error_line_not_success(self):
        log=self.root/'intake.log';err=self.root/'err.log'
        old=time.strftime(client_watch.TS_FMT,time.localtime(time.time()-7200));now=time.strftime(client_watch.TS_FMT)
        log.write_text(f'[{old}] poll completed\n[{now}] ERROR: broken\n');err.write_text('')
        self.assertTrue(client_watch.intake_health(log_path=log,err_path=err)['stale'])
    def test_all_open_origins_watched(self):
        path=self.root/'tickets.jsonl';old=time.strftime(client_watch.TS_FMT,time.localtime(time.time()-86400*5))
        path.write_text('\n'.join(json.dumps({'id':str(i),'created':old,'status':state,'origin':origin}) for i,(state,origin) in enumerate([('queued','client-deliverable'),('queued','client-confirmation'),('session','user-intake')])))
        self.assertEqual(len(client_watch.stale_tickets(path)),3)


class IntakeRoundTrip(unittest.TestCase):
    def test_rate_limit_then_dispatch_once_without_replaying_ack(self):
        from contextlib import ExitStack
        import subprocess
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root=Path(directory)
            for name,value in [('PROJECT_DIR',root),('FAST_POLL_STATE_PATH',root/'fast')]:
                stack.enter_context(patch.object(intake,name,value))
            entry={'name':'fixture','projects':['pedagogy'],'limit':10,'request_kind':'research','require_prefix':False}
            stack.enter_context(patch.object(intake,'load_allowlist',return_value={'fixture@example.test':entry}))
            stack.enter_context(patch('runtime_config.load_sources',return_value={}))
            stack.enter_context(patch.object(intake,'find_account',return_value={'credential_key':'password'}))
            stack.enter_context(patch.object(intake,'load_json',return_value={'password':'synthetic'}))
            stack.enter_context(patch.object(intake,'load_state',return_value={}))
            for name in ('save_state','_record_status','append_backlog','telegram','log','_notify'):
                stack.enter_context(patch.object(intake,name,return_value=0))
            stack.enter_context(patch.object(intake,'is_confirmation',return_value=[]))
            stack.enter_context(patch.object(intake.dev_loop,'ledger_append'))
            stack.enter_context(patch.object(life,'coordinate',return_value=True))
            row=(1,'fixture@example.test','Re: planning','Summarize the supplied notes.','<fixture>')
            stack.enter_context(patch.object(intake,'scan_inbox',side_effect=[([row],[]),([],[]),([],[])]))
            stack.enter_context(patch.object(intake,'under_limit',side_effect=[False,True]))
            ack=stack.enter_context(patch.object(intake,'send_ack',return_value=True))
            spawn=stack.enter_context(patch.object(subprocess,'Popen'))
            self.assertEqual(intake.run(),0)
            self.assertEqual(life.pending(root),[list(row)]);ack.assert_not_called()
            self.assertEqual(intake.run(),0)
            self.assertEqual(life.summary(root)['counts'],{'queued_preparation':1})
            self.assertEqual(intake.run(),0)
            ack.assert_called_once()
            self.assertTrue(spawn.called)

if __name__=='__main__':unittest.main()
