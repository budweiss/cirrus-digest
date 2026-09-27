"""Skywarden's deterministic fleet sidecar: observation and qualified pilots.

One local controller lock, one SQLite writer authority, no cloud, no sends,
no production cancellation. Skywarden retains existing recovery ownership.
"""
import argparse
import concurrent.futures
import fcntl
import html
import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path
from fleet_queue import Queue
from fleet_pilot import policy, request, WORKERS
from fleet_worker import facts


def atomic(path,data):
    temp=path.with_name(path.name+'.tmp-'+str(os.getpid()))
    temp.write_text(json.dumps(data,indent=2));os.replace(temp,path)


def units():
    rows=[]
    for scope,flags in [('system',[]),('user',['--user'])]:
        p=subprocess.run(['systemctl',*flags,'list-units','--all','--type=service,timer','--plain','--no-legend','--no-pager'],capture_output=True,text=True,timeout=10)
        if p.returncode:raise RuntimeError('unit_inventory_unreadable')
        for line in p.stdout.splitlines():
            fields=line.strip().lstrip('●×* ').split()
            if len(fields)<4:continue
            name=fields[0]
            app=name.startswith(('cirrus','cumulus','halftime','alopecia','immaculate','accesscheck','foundation','gpt-oss','qwen','ray-','fleet-','opportunity','entity-','vllm','ollama','network-diagnostic'))
            rows.append({'unit':name,'scope':scope,'active':fields[2],'sub':fields[3],
                         'mode':'observed-only' if app else 'excluded',
                         'reason':'existing supervisor/scheduler retains authority' if app else 'operating-system service outside application scope'})
    return rows


def vllm_busy(endpoint):
    with urllib.request.urlopen(endpoint+'/metrics',timeout=5) as r:text=r.read().decode()
    metrics={}
    for name in ('vllm:num_requests_running','vllm:num_requests_waiting'):
        rows=[float(line.rsplit(' ',1)[1]) for line in text.splitlines() if line.startswith(name+'{')]
        if not rows:raise RuntimeError('inference_load_unknown')
        metrics[name]=sum(rows)
    return sum(metrics.values())>0


def observe_worker(worker):
    s=WORKERS[worker]
    rows=request(s['endpoint'],'/v1/models',timeout=5)['data']
    model=next(r for r in rows if r['id']==s['model'])
    resource=facts() if worker=='cumulus1-gptoss' else request('http://192.168.100.11:8011','/health',timeout=5)
    if not 0 <= time.time()-resource['observed'] <= 30:raise RuntimeError('stale_resource_observation')
    if resource['host'] != ('cumulus1' if worker=='cumulus1-gptoss' else 'cumulus2'):raise RuntimeError('wrong_host')
    return {'worker':worker,'model':model['id'],'context':model['max_model_len'],**resource,
            'busy':vllm_busy(s['endpoint']) if worker=='cumulus2-qwen' else False,
            'scope':'pilot windows only; legacy C1 requests are not globally admitted'}


def snapshot(q,app=Path('/home/buddy/cirrus-digest')):
    workers=[];errors=[]
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        pending={pool.submit(observe_worker,w):w for w in WORKERS}
        for future,w in pending.items():
            try:
                r=future.result();workers.append(r)
                q.health(w,r['model'],r['context'],r['available_mib'],r['busy'],r['observed'])
            except Exception as e:
                errors.append({'worker':w,'error':type(e).__name__})
                q.health(w,'unavailable',0,0,True)
    q.monitor()
    try:inventory=units()
    except Exception as e:inventory=[];errors.append({'inventory':type(e).__name__})
    try:
        ledger=json.loads((app/'logs/jobs-status.json').read_text())
        jobs={name:{k:row[k] for k in ('epoch','ok') if k in row} for name,row in ledger.items() if isinstance(row,dict)}
    except Exception as e:jobs={};errors.append({'job_ledger':type(e).__name__})
    state=q.status()
    unsettled=[{'id':r['id'],'state':r['state']} for r in state['jobs'] if r['state'] in ('unknown','suspected_stall','cancelling','blocked','failed')]
    return {'observed':time.time(),'ok':not errors and not unsettled,'mode':'observation, qualified synthetic pilots and opt-in infrastructure analysis',
            'workers':workers,'errors':errors,'unsettled':unsettled,'inventory':inventory,'legacy_jobs':jobs,'queue':state}


def render(data):
    esc=lambda x:html.escape(str(x))
    rows=''
    for r in data['queue']['jobs']:
        worker=next((w for w in data['workers'] if w['worker']==r['worker']),{})
        row=dict(r,model=worker.get('model','unobserved'),scheduler='CIRRUS learnwatch 01:15 ET' if r['project']=='articles-infra' else 'explicit no-delivery pilot',host='cumulus1 coordinator',created_utc=time.strftime('%Y-%m-%d %H:%M:%S',time.gmtime(r['created'])))
        rows+='<tr>'+''.join('<td>'+esc(row.get(k,''))+'</td>' for k in ('project','scheduler','host','worker','model','state','stage','progress','attempt','reason','created_utc'))+'</tr>'
    return '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30"><title>Cumulus supervision</title><style>body{font:16px system-ui;margin:40px;background:#f5f7fa;color:#152239}table{border-collapse:collapse;width:100%;background:white}td,th{padding:12px;text-align:left;border-bottom:1px solid #ccd}pre{white-space:pre-wrap}</style><h1>Cumulus supervision</h1><p>'+esc(data['mode'])+'</p><p>Observed UTC: '+time.strftime('%Y-%m-%d %H:%M:%S',time.gmtime(data['observed']))+'</p><table><tr><th>Project</th><th>Scheduler</th><th>Execution host</th><th>Worker</th><th>Observed model</th><th>State</th><th>Stage</th><th>Progress</th><th>Attempt</th><th>Reason</th><th>Submitted UTC</th></tr>'+rows+'</table><h2>Workers</h2><pre>'+esc(json.dumps(data['workers'],indent=2))+'</pre><h2>Unresolved observations</h2><pre>'+esc(json.dumps(data['errors']+data['unsettled'],indent=2))+'</pre><h2>Recent admission and recovery events</h2><pre>'+esc(json.dumps(data['queue']['events'],indent=2))+'</pre><h2>Hermes proposal reviews</h2><pre>'+esc(json.dumps(data.get('hermes',{}),indent=2))+'</pre><p>Other production jobs remain observed-only; Skywarden retains their recovery authority.</p>'


class HermesReview:
    """Bounded, asynchronous, proposal-only review of new managed incidents."""
    def __init__(self,state,lock_path=Path("/home/buddy/cirrus-digest/logs/media/worker.lock")):
        self.lock_path=lock_path;self.media_lock=None
        self.state=state;self.process=None;self.log=None;self.started=0;self.folder=None
    def tick(self,data):
        if self.process is not None:
            if self.process.poll() is None:
                if time.monotonic()-self.started>180:
                    from fleet_recovery import cancel_child
                    cancel_child(self.process,5,True)
                return
            atomic(self.folder/'finished.json',{'finished':time.time(),'exit_code':self.process.returncode})
            self.log.close();self.log=None;self.process=None
            self.media_lock.close();self.media_lock=None
        if not (self.state/'hermes.enabled').exists():return
        worker=next((w for w in data['workers'] if w['worker']=='cumulus2-qwen'),None)
        if not worker or worker['busy'] or worker['context']<65536:return
        incidents=[r for r in data['queue']['jobs'] if r['state'] in ('unknown','suspected_stall','blocked','failed')]
        if not incidents:return
        from fleet_queue import digest
        for r in incidents:
            key=digest({k:r[k] for k in ('id','state','attempt','reason')})
            folder=self.state/'hermes'/key
            if not (folder/'submitted.json').exists():break
        else:return
        self.lock_path.parent.mkdir(parents=True,exist_ok=True)
        self.media_lock=self.lock_path.open('a')
        try:fcntl.flock(self.media_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.media_lock.close();self.media_lock=None;return
        folder.mkdir(parents=True,exist_ok=True)
        # Once per incident; a crash does not trigger an inference storm.
        marker=folder/'submitted.json'
        self.folder=folder
        incident={'job_id':r['id'],'state':r['state'],'stage':r['stage'],'progress':r['progress'],
                  'backend_idle':not worker['busy'],'delivery':'none','retries_left':max(0,3-r['attempt']),
                  'independent_confirmation':False,'evidence':'Controller metadata only; no raw project payload.'}
        atomic(folder/'incident.json',incident);atomic(marker,{'submitted':time.time(),'proposal_only':True})
        runtime=Path('/home/buddy/model-evaluation/s318-supervisor/hermes/.venv/bin/python')
        self.log=(folder/'review.log').open('w')
        try:
            self.process=subprocess.Popen([str(runtime),str(Path(__file__).with_name('fleet_hermes.py')),'--incident',str(folder/'incident.json')],
                                         stdout=self.log,stderr=subprocess.STDOUT,start_new_session=True,pass_fds=(self.media_lock.fileno(),))
        except Exception:
            self.log.close();self.log=None;self.media_lock.close();self.media_lock=None
            atomic(folder/'finished.json',{'finished':time.time(),'exit_code':-1})
            raise
        self.started=time.monotonic()
    def close(self):
        if self.process is not None and self.process.poll() is None:
            from fleet_recovery import cancel_child
            cancel_child(self.process,5,True)
        if self.log:self.log.close()
        if self.media_lock:self.media_lock.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['observe','serve','status','pause','resume','pilot','backup','drain','ready'])
    parser.add_argument('--state',type=Path,default=Path.home()/'model-serving/fleet/state-v2')
    parser.add_argument('--worker',choices=list(WORKERS))
    args=parser.parse_args();args.state.mkdir(parents=True,exist_ok=True)
    q=Queue(args.state/'queue.db',policy())
    if args.action=='status':
        p=args.state/'status.json'
        data=json.loads(p.read_text());data['stale']=not 0<=time.time()-data['observed']<=180
        print(json.dumps(data,indent=2));return
    if args.action in ('pause','resume'):q.control(args.action);return
    if args.action in ('drain','ready'):q.control(args.action,args.worker);return
    if args.action=='backup':q.backup(args.state/('backup-'+str(int(time.time()))+'.db'));return
    if args.action=='pilot':
        from fleet_pilot import run
        run(args.state,allow_existing=True);return
    with (args.state/'controller.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        stop=False
        def halt(*_):
            nonlocal stop
            stop=True
        signal.signal(signal.SIGTERM,halt)
        review=HermesReview(args.state)
        while not stop:
            data=snapshot(q)
            try:
                if args.action=="serve":review.tick(data)
            except Exception as exc:
                atomic(args.state/'hermes-error.json',{'observed':time.time(),'error':type(exc).__name__})
            data['hermes']={'enabled':(args.state/'hermes.enabled').exists(),
                'running':review.process is not None and review.process.poll() is None,
                'error':json.loads((args.state/'hermes-error.json').read_text()) if (args.state/'hermes-error.json').exists() else None,
                'reviews':[{"incident":p.parent.name,**json.loads(p.read_text())} for p in sorted((args.state/'hermes').glob('*/finished.json'))]}
            if data['hermes']['error'] or any(r['exit_code']!=0 for r in data['hermes']['reviews']):data['ok']=False
            atomic(args.state/'status.json',data)
            (args.state/'status.html').write_text(render(data))
            if args.action=='observe':
                print(json.dumps({'ok':data['ok'],'workers':len(data['workers']),'units':len(data['inventory']),'errors':data['errors']}));return
            for _ in range(30):
                if stop:break
                time.sleep(1)
        review.close()

if __name__=='__main__':main()
