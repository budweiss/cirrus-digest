"""Scratch-only tests: production worker, SSH, models and sends are never run."""
import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo
import media_pipeline as m
import media_jobs
import yt_watch as yt
import job_status

class DeadlineTests(unittest.TestCase):
    def setUp(self):
        guard=patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden'))
        guard.start();self.addCleanup(guard.stop)

    def test_windows(self):
        for stamp,minutes in [('2026-09-28T05:30',90),('2026-09-28T07:30',25),('2026-09-28T16:00',90),('2026-10-03T08:00',90)]:
            now=datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo('America/New_York'))
            self.assertEqual(m.youtube_deadline(now)-now.timestamp(),minutes*60)
        for stamp in ['2026-09-28T07:55','2026-09-28T08:00','2026-09-28T15:59']:
            with self.assertRaisesRegex(RuntimeError,'youtube_deferred_busy_window'):
                m.youtube_deadline(datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo('America/New_York')))

    def test_invalid_expired_never_spawn(self):
        with patch.object(m,'youtube_deadline',return_value=time.time()+60),patch.object(m.subprocess,'Popen') as spawn:
            for value in [float('nan'),float('inf'),True,'later',None]:
                with self.assertRaisesRegex(ValueError,'invalid_youtube_budget'):
                    m.supervised_youtube({'budget_seconds':value})
            with self.assertRaises(m.YouTubeDeadline):m.supervised_youtube({'budget_seconds':-1})
            spawn.assert_not_called()

    def test_alarm_interrupts_lease_wait(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'worker.lock'
            with path.open('a') as holder:
                fcntl.flock(holder,fcntl.LOCK_EX|fcntl.LOCK_NB)
                start=time.monotonic()
                with patch.object(m,'youtube_deadline',return_value=time.time()+60):
                    with self.assertRaises(m.YouTubeDeadline):
                        with m.youtube_time_limit(time.time()+0.1):
                            with m.lease(path,timeout=30):self.fail('lock was held')
                self.assertLess(time.monotonic()-start,2)
            with m.lease(path,timeout=0.1):pass

    def test_parent_kills_worker_ignoring_alarm(self):
        real_popen=subprocess.Popen;children=[]
        def spawn(*args,**kwargs):
            child=real_popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGALRM,signal.SIG_IGN); time.sleep(30)'],**kwargs)
            children.append(child);return child
        with patch.object(m,'youtube_deadline',return_value=time.time()+60),patch.object(m.subprocess,'Popen',side_effect=spawn):
            start=time.monotonic()
            with self.assertRaises(m.YouTubeDeadline):m.supervised_youtube({'action':'youtube','budget_seconds':0.2})
            self.assertLess(time.monotonic()-start,3)
        self.assertEqual(children[0].returncode,-signal.SIGKILL)

    def test_parent_success_and_failure(self):
        real_popen=subprocess.Popen
        for program,success in [('import json; print(json.dumps({"result":{"ok":True}}))',True),('raise SystemExit(7)',False)]:
            with patch.object(m,'youtube_deadline',return_value=time.time()+60),patch.object(m.subprocess,'Popen',side_effect=lambda *a,**kw:real_popen([sys.executable,'-c',program],**kw)):
                payload={'action':'youtube','budget_seconds':5}
                if success:self.assertEqual(m.supervised_youtube(payload),{'ok':True})
                else:
                    with self.assertRaisesRegex(RuntimeError,'youtube_worker_failed_exit_7'):m.supervised_youtube(payload)

    def test_ssh_budget(self):
        with patch.object(m.socket,'gethostname',return_value='cirrus'),patch.object(m,'youtube_deadline',return_value=6400),patch.object(m.time,'time',return_value=1000),patch.object(m.subprocess,'run') as run:
            run.return_value.returncode=0;run.return_value.stdout='{"result":{"ok":true}}'
            self.assertEqual(m.call('youtube',seen={'video_ids':[]}),{'ok':True})
            self.assertEqual(run.call_args.kwargs['timeout'],5400)
            self.assertEqual(json.loads(run.call_args.kwargs['input'])['budget_seconds'],5370)

    def test_dispatch_uses_supervisor(self):
        payload={'action':'youtube','budget_seconds':1}
        with patch.object(m.socket,'gethostname',return_value='cumulus1'),patch.object(m,'supervised_youtube',return_value={'ok':True}) as supervisor,patch.object(m,'youtube') as worker:
            self.assertEqual(m.dispatch(payload),{'ok':True});supervisor.assert_called_once_with(payload);worker.assert_not_called()

    def test_busy_window_never_opens_ssh(self):
        with patch.object(m,'youtube_deadline',side_effect=RuntimeError('youtube_deferred_busy_window')),patch.object(m.subprocess,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'youtube_deferred_busy_window'):m.call('youtube')
            run.assert_not_called()

    def test_remote_deadline_remains_classified(self):
        with patch.object(m.socket,'gethostname',return_value='cirrus'),patch.object(m,'youtube_deadline',return_value=time.time()+60),patch.object(m.subprocess,'run') as run:
            run.return_value.returncode=75;run.return_value.stderr='earlier worker progress\nmedia worker failed: YouTubeDeadline\n'
            with self.assertRaises(m.YouTubeDeadline):m.call('youtube')

    def test_child_deadline_with_progress_stays_classified(self):
        real_popen=subprocess.Popen
        program='import sys; print("worker progress",file=sys.stderr); print("media worker failed: YouTubeDeadline",file=sys.stderr); raise SystemExit(75)'
        with patch.object(m,'youtube_deadline',return_value=time.time()+60),patch.object(m.subprocess,'Popen',side_effect=lambda *a,**kw:real_popen([sys.executable,'-c',program],**kw)):
            with self.assertRaises(m.YouTubeDeadline):m.supervised_youtube({'action':'youtube','budget_seconds':5})

    def test_unconfirmed_cleanup_is_failure_and_bounded(self):
        with patch.object(m,'youtube_deadline',return_value=time.time()+60),patch.object(m.subprocess,'Popen') as popen,patch.object(m.os,'killpg'):
            child=popen.return_value
            child.communicate.side_effect=subprocess.TimeoutExpired('worker',1)
            with self.assertRaisesRegex(RuntimeError,'youtube_worker_cleanup_unconfirmed'):
                m.supervised_youtube({'action':'youtube','budget_seconds':1})
            self.assertEqual(child.communicate.call_args_list[1].kwargs['timeout'],5)

    def test_real_worker_entry_exits_gracefully_and_cleans_scratch(self):
        real_popen=subprocess.Popen;children=[]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);worker=root/'media_pipeline.py'
            worker.write_bytes(Path(m.__file__).read_bytes())
            script=('import runpy,socket,sys,time,runtime_window,yt_watch; '
                    'socket.gethostname=lambda:"cumulus1"; runtime_window.BUSY_DAYS=set(); '
                    'yt_watch.run=lambda **kw:time.sleep(30); '
                    'sys.argv=[sys.argv[1],"--youtube-worker"]; '
                    'runpy.run_path(sys.argv[0],run_name="__main__")')
            def spawn(*args,**kwargs):
                env=dict(os.environ,TMPDIR=tmp,PYTHONPATH=str(Path(m.__file__).parent))
                child=real_popen([sys.executable,'-c',script,str(worker)],env=env,**kwargs)
                children.append(child);return child
            with patch.object(m,'youtube_deadline',return_value=time.time()+60),patch.object(m.subprocess,'Popen',side_effect=spawn):
                with self.assertRaises(m.YouTubeDeadline):
                    m.supervised_youtube({'action':'youtube','budget_seconds':1,'seen':{'video_ids':[]},'channels':[]})
            self.assertEqual(children[0].returncode,75)
            self.assertEqual(list(root.glob('yt-state-*')),[])

class CursorTests(unittest.TestCase):
    def exercise(self,failure=None,dry=False):
        with tempfile.TemporaryDirectory() as tmp,contextlib.ExitStack() as stack:
            root=Path(tmp);seen=root/'seen.json';seen.write_text('{"video_ids":["old-video"]}');out=root/'findings';original=seen.read_bytes()
            if not failure and not dry:
                out.mkdir();(out/'yt-watch-2026-09-28.md').write_text('earlier batch\n')
            for name,value in [('SEEN_PATH',seen),('OUT_DIR',out),('run',yt.run)]:stack.enter_context(patch.object(yt,name,value))
            stack.enter_context(patch.object(yt,'load_channels',return_value=[]))
            stack.enter_context(patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')))
            record=stack.enter_context(patch.object(job_status,'record'))
            response={'files':{'yt-watch-2026-09-28.md':'one finding'},'seen':{'video_ids':['old-video','new-video']},'stats':{'processed':1}}
            stack.enter_context(patch.object(m,'call',return_value=response,side_effect=failure))
            stack.enter_context(patch.object(yt,'main',side_effect=lambda:yt.run(dry_run=dry)))
            if failure:
                with self.assertRaises(type(failure)):media_jobs.youtube()
                self.assertEqual(seen.read_bytes(),original);self.assertFalse(out.exists())
                if dry:record.assert_not_called()
                else:
                    reason='worker_failed'
                    if isinstance(failure,m.YouTubeDeadline):reason='deadline_expired'
                    elif isinstance(failure,subprocess.TimeoutExpired):reason='ssh_timeout'
                    elif str(failure)=='youtube_deferred_busy_window':reason='deferred_busy_window'
                    record.assert_called_once_with('ytwatch',False,reason+'; cursor preserved')
            else:
                self.assertEqual(media_jobs.youtube(),response['stats'])
                if dry:self.assertEqual(seen.read_bytes(),original);self.assertFalse(out.exists())
                else:
                    self.assertEqual(json.loads(seen.read_text()),response['seen'])
                    self.assertIn('one finding',(out/'yt-watch-2026-09-28.md').read_text())
                    self.assertIn('earlier batch',(out/'yt-watch-2026-09-28.md').read_text())
                    media_jobs.youtube()
                    self.assertEqual(len(list(out.iterdir())),1);self.assertEqual(json.loads(seen.read_text()),response['seen'])
                    self.assertEqual((out/'yt-watch-2026-09-28.md').read_text().count('one finding'),1)
                record.assert_not_called()
    def test_failure(self):
        for failure in [RuntimeError('offline'),m.YouTubeDeadline('expired'),RuntimeError('youtube_deferred_busy_window'),subprocess.TimeoutExpired('ssh',1)]:
            with self.subTest(kind=type(failure).__name__):self.exercise(failure)
    def test_dry_failure(self):self.exercise(RuntimeError('offline'),True)
    def test_replay(self):self.exercise()
    def test_dry_success(self):self.exercise(dry=True)
    def test_second_batch_keeps_first_and_replays_once(self):
        first=media_jobs.merge_findings('', '# Day\n\nFirst video\n')
        both=media_jobs.merge_findings(first, '# Day\n\nSecond video\n')
        self.assertIn('First video',both);self.assertIn('Second video',both)
        self.assertEqual(media_jobs.merge_findings(both,'# Day\n\nSecond video\n'),both)
        self.assertEqual(media_jobs.merge_findings(both,'# Day\n\nFirst video\n'),both)
    def test_legacy_batch_survives_and_replays_once(self):
        legacy='# Day\n\nLegacy finding\n'
        both=media_jobs.merge_findings(legacy,'# Day\n\nNew finding\n')
        self.assertTrue(both.startswith(legacy))
        self.assertEqual(media_jobs.merge_findings(both,legacy),both)

if __name__=='__main__':unittest.main()
