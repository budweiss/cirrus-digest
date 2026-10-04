"""No-delivery, no-cloud live acceptance pilot for the two Cumulus workers."""
import argparse
import concurrent.futures
import json
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from fleet_queue import Queue

WORKERS={
 'cumulus1-gptoss':{'model':'gpt-oss:120b','endpoint':'http://127.0.0.1:8000','min_available_mib':8192},
 'cumulus2-qwen':{'model':'qwen3.8-27b-fp8','endpoint':'http://192.168.100.11:8000','min_available_mib':8192}}
MESSAGES=[{'role':'system','content':'Extract only supplied facts. Reply with a JSON object only, keys sample (integer), control_group (boolean), trial_date (null if absent). No markdown.'},
 {'role':'user','content':'S318 SYNTHETIC TEST ONLY. The study included 12 participants and had no control group. No trial date was supplied.'}]


def request(endpoint,path,payload=None,timeout=120):
    req=urllib.request.Request(endpoint+path,data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Request-Id':'s318-synthetic-pilot'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=timeout) as r:return json.load(r)


def policy():
    p = {'workers':WORKERS,'projects':{w:{'workers':[w],'model_ids':{w:s['model']},'validator':'fixture_json','max_retries':2,'stall_seconds':150,'heartbeat_seconds':30,'max_input_bytes':2048,'max_output_tokens':1024,'qualification_until':1791139200,'contract':'synthetic-v1','mode':'pilot','owner':'fleet-pilot','resource':w,'delivery':'none'} for w,s in WORKERS.items()}}
    p['projects']['articles-infra'] = {'workers':['cumulus2-qwen'],
      'model_ids':{'cumulus2-qwen':'qwen3.8-27b-fp8'},'validator':'media_manifest',
      'max_retries':2,'stall_seconds':600,'heartbeat_seconds':30,'max_input_bytes':2000000,
      'request_context_bound':10000,'max_output_tokens':2048,'qualification_until':1791139200,
      'contract':'media-s307-v3','mode':'pilot','owner':'fleet-media','resource':'articles-infra',
      'delivery':'none'}
    return p


def run(root, allow_existing=False):
    local_c1 = socket.gethostname() == 'cumulus1'
    if not local_c1:
        WORKERS['cumulus1-gptoss']['endpoint']='http://127.0.0.1:18001'
        WORKERS['cumulus2-qwen']['endpoint']='http://127.0.0.1:18002'
    root.mkdir(parents=True,exist_ok=allow_existing)
    p=policy();(root/'policy.json').write_text(json.dumps(p,indent=2))
    q=Queue(root/'queue.db',p)
    for w,s in WORKERS.items():
        models=request(s['endpoint'],'/v1/models')['data']
        row=next(r for r in models if r['id']==s['model'])
        host='cumulus1' if w=='cumulus1-gptoss' else 'cumulus2'
        if local_c1 and host=='cumulus2':
            resource=request('http://192.168.100.11:8011','/health')
            if not 0 <= time.time()-resource['observed'] <= 30:raise RuntimeError('stale resource')
            mem=resource['available_mib']
        else:
            cmd=['awk','/MemAvailable/{print $2}','/proc/meminfo']
            if not local_c1:cmd=['ssh','-o','BatchMode=yes',host,"awk '/MemAvailable/{print $2}' /proc/meminfo"]
            mem=int(subprocess.check_output(cmd,text=True).strip())//1024
        q.health(w,row['id'],row['max_model_len'],mem)
        q.submit(w,'s318-parallel-'+str(time.time_ns()),MESSAGES,'synthetic-v1')
    barrier=threading.Barrier(2)
    stop=threading.Event();samples=[]
    def sample(host):
        while not stop.is_set():
            cmd=['nvidia-smi','--query-gpu=utilization.gpu','--format=csv,noheader,nounits']
            if local_c1 and host=='cumulus2':return  # GPU evidence comes from the Mac-orchestrated baseline
            if not local_c1:cmd=['ssh','-o','BatchMode=yes',host]+cmd
            try:
                v=subprocess.check_output(cmd,text=True,timeout=5).strip()
                samples.append({'host':host,'ts':time.time(),'gpu_percent':int(v)})
            except Exception as exc:samples.append({'host':host,'ts':time.time(),'error':type(exc).__name__})
            stop.wait(.2)
    samplers=[threading.Thread(target=sample,args=(h,)) for h in ('cumulus1','cumulus2')]
    for t in samplers:t.start()
    def work(w):
        r=q.claim(w)
        if not r:raise RuntimeError('pilot admission refused')
        barrier.wait(timeout=10);start=time.time()
        response=request(WORKERS[w]['endpoint'],'/v1/chat/completions',{'model':WORKERS[w]['model'],'messages':MESSAGES,'max_tokens':1024,'temperature':0,'chat_template_kwargs':{'enable_thinking':False},'reasoning_effort':'low'})
        end=time.time();choice=response['choices'][0];text=choice['message'].get('content','')
        try:data=json.loads(text)
        except ValueError:data=None
        result={'model':response.get('model'),'finish_reason':choice.get('finish_reason'),'data':data,'coverage':1}
        passed=q.complete(r['id'],r['fence'],result)
        return {'worker':w,'start':start,'end':end,'result':result,'usage':response.get('usage'),'passed':passed}
    try:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(work,WORKERS))
    finally:
        stop.set()
        for t in samplers:t.join()
    overlap=max(0,min(r['end'] for r in results)-max(r['start'] for r in results))
    report={'results':results,'request_overlap_seconds':overlap,'gpu_samples':samples,'queue':q.status()}
    (root/'result.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({'passed':all(r['passed'] for r in results),'request_overlap_seconds':overlap,'timings':[{k:r[k] for k in ('worker','start','end','passed')} for r in results],'gpu_sample_count':len(samples)}))
    if not all(r['passed'] for r in results) or overlap<=0:raise SystemExit(1)

def selftest():
    p = policy()
    assert set(p['workers']) == {'cumulus1-gptoss', 'cumulus2-qwen'}, p['workers']
    assert p['workers']['cumulus1-gptoss']['model'] == 'gpt-oss:120b'
    assert p['workers']['cumulus2-qwen']['model'] == 'qwen3.8-27b-fp8'
    for w, s in WORKERS.items():
        proj = p['projects'][w]
        assert proj['workers'] == [w], proj
        assert proj['model_ids'][w] == s['model']
        assert proj['validator'] == 'fixture_json'
        assert proj['delivery'] == 'none'
        assert proj['resource'] == w
    media = p['projects']['articles-infra']
    assert media['workers'] == ['cumulus2-qwen']
    assert media['model_ids']['cumulus2-qwen'] == 'qwen3.8-27b-fp8'
    assert media['validator'] == 'media_manifest'
    assert media['delivery'] == 'none'
    assert media['contract'] == 'media-s307-v3'
    assert MESSAGES[0]['role'] == 'system'
    assert MESSAGES[1]['role'] == 'user'
    assert 'JSON object only' in MESSAGES[0]['content']
    print('fleet_pilot selftest OK')


if __name__=='__main__':
    import sys
    if '--selftest' in sys.argv:
        try:
            selftest()
        except AssertionError as e:
            print('SELFTEST FAILED:', e)
            raise SystemExit(1)
        raise SystemExit(0)
    a=argparse.ArgumentParser();a.add_argument('output',type=Path);run(a.parse_args().output)
