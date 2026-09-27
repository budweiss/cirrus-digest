"""Admission adapter for one existing no-delivery research stage.

Runs inside media_pipeline's existing cross-process lock. Source collection,
archives, quote validation, schedules, recipients and sends remain unchanged.
The observer owns no payload: the original caller executes after admission.
"""
import hashlib
import json
import threading
import time
from datetime import datetime,timezone
from pathlib import Path
from fleet_queue import Queue, Refused
from fleet_pilot import policy

STATE=Path('/home/buddy/model-serving/fleet/state-v2')


def verify(folder,text,model):
    coverage=json.loads((folder/'coverage.json').read_text())
    result=json.loads((folder/'result.json').read_text())['result']
    source=(folder/'transcript.txt').read_text()
    if source!=text or coverage['model']!=model or not coverage.get('complete'):
        raise Refused('source/model/coverage mismatch')
    spans=coverage['spans'];end=0
    for a,b in spans:
        if not 0<=a<=end<b<=len(text):raise Refused('source coverage gap')
        end=b
    if end!=len(text) or len(spans)!=coverage['sections'] or coverage['sections']!=coverage['completed']:
        raise Refused('incomplete source coverage')
    if not isinstance(result,list):raise Refused('claims result must be a list')
    import media_pipeline as media
    for claim in result:
        media.source_quote(claim['quote'],source)
    return result,coverage


def run_analysis(payload,root=None,state=STATE,observer=None,analyzer=None,wait_seconds=300):
    import media_pipeline as media
    from fleet_controller import observe_worker
    from fleet_status import check
    root=Path(root) if root is not None else media.ROOT
    if payload.get('domain')!='articles-infra' or payload.get('claims') is not True:
        raise Refused('only the qualified infrastructure claim path is admitted')
    from learn_watch import INSTRUCTIONS
    if payload.get('instructions') != INSTRUCTIONS:
        raise Refused('unqualified research prompt')
    if not check(state/'status.json')['ok']:raise Refused('controller unavailable or degraded; admission stopped')
    q=Queue(state/'queue.db',policy());worker='cumulus2-qwen'
    contract='media-'+media.VERSION
    key=datetime.now(timezone.utc).strftime('%Y-%m-%d')+'-'+hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    job=q.submit('articles-infra',key,payload,contract)
    old=next(r for r in q.status()['jobs'] if r['id']==job)
    if old['state']=='succeeded':
        saved=json.loads(old['result']);folder=Path(saved['artifact']).resolve()
        if not folder.is_relative_to((root/'media/articles-infra').resolve()):raise Refused('artifact outside project')
        return verify(folder,payload['text'],media.MODEL)[0]
    if old['state'] not in ('queued','retry_wait'):
        raise Refused('prior attempt needs reconciliation')
    observe=observer or observe_worker;deadline=time.monotonic()+wait_seconds;r=None
    while time.monotonic()<deadline:
        if not check(state/'status.json')['ok']:raise Refused('controller unavailable or degraded; admission stopped')
        h=observe(worker);q.health(worker,h['model'],h['context'],h['available_mib'],h['busy'],h['observed'])
        r=q.claim(worker,job)
        if r:break
        time.sleep(1)
    if not r:raise Refused('waiting for qualified worker capacity; no fallback')
    stop=threading.Event();progress=[0];heartbeat_error=[]
    def heartbeat():
        while not stop.wait(20):
            try:q.progress(job,r['fence'],progress[0],'analyzing')
            except Exception as e:heartbeat_error.append(type(e).__name__);return
    t=threading.Thread(target=heartbeat);t.start();report={}
    def advanced(n):
        progress[0]=n;q.progress(job,r['fence'],n,'analyzing')
    try:
        result=(analyzer or media.analyze)(payload['text'],payload['instructions'],payload['domain'],True,
            root=root,metadata=payload.get('metadata'),report=report,progress=advanced)
        if heartbeat_error:raise Refused('lost attempt ownership')
        folder=Path(report['folder']).resolve()
        if not folder.is_relative_to((root/'media/articles-infra').resolve()):raise Refused('artifact outside project')
        checked,coverage=verify(folder,payload['text'],media.MODEL)
        if result!=checked:raise Refused('result differs from published artifact')
        manifest={**coverage,'artifact':str(folder),'input_hash':r['input_hash'],'source_verified':True}
        if not q.complete(job,r['fence'],manifest):raise Refused('completion validation failed')
        return result
    except Exception:
        # Do not guess that a timed-out backend stopped. Keep the slot until
        # explicit reconciliation; the original scheduler records its failure.
        try:q.unknown(job,r['fence'],'analysis failed; backend and checkpoint reconciliation required')
        except Refused:pass
        raise
    finally:stop.set();t.join()
