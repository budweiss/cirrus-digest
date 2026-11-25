"""S674/S685: guarded C1 daily-caller context policy; numeric-only, no inference."""
import json, math, os, socket, time
from pathlib import Path
from urllib.parse import urlparse

CONTROL = 'daily-context-trial.json'
STOP = 'daily-context-trial.stop'
_STOPPED = set()

def memory():
    rows = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    return float(rows['MemAvailable'].split()[0]) / 1048576

def stop(root):
    _STOPPED.add(str(root))
    path = Path(root)/'config'/STOP
    path.touch(mode=0o600, exist_ok=True)

def settings(root):
    return json.loads((Path(root)/'config'/CONTROL).read_text())

def select(root, endpoint, model):
    """Unknown state, wrong host/model/endpoint, expiry or stop => old context."""
    if socket.gethostname().lower().split('.')[0] != 'cumulus1': return 8192
    u = urlparse(endpoint)
    if model != 'gpt-oss:120b' or u.scheme != 'http' or u.hostname not in ('localhost','127.0.0.1','::1') or u.port != 11434 or u.path not in ('','/'):
        return 8192
    try:
        cfg=settings(root)
        if cfg.get('trial') != 's674' or cfg.get('enabled') is not True: return 8192
        if str(root) in _STOPPED or (Path(root)/'config'/STOP).exists(): return 8192
        mode = cfg.get('mode', 'trial')
        if mode == 'retained':
            # Buddy's explicit retention approval; preserve the original trial dates.
            since = cfg.get('retained_from')
            if type(since) not in (int, float) or not math.isfinite(since) or not 0 <= since <= time.time(): return 8192
        elif mode == 'trial':
            if not cfg['starts_at'] <= time.time() < cfg['ends_at']: return 8192
            if cfg['ends_at']-cfg['starts_at'] > 4*86400: return 8192
        else: return 8192
        if memory() < 32: stop(root); return 8192
        # Prove the numeric monitor can be opened before admitting a trial call.
        ledger=Path(root)/'out/daily-context-trial.jsonl'
        ledger.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(ledger,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600);os.close(fd)
        return 65536
    except Exception:
        # Unknown monitoring while enabled ends the trial; absent control is inactive.
        if (Path(root)/'config'/CONTROL).exists():
            try: stop(root)
            except Exception: pass
        return 8192

def record(root, context, data=None, elapsed=None, error=False, observation=None):
    """Observe only trial calls. Never save input, output, IDs or error text."""
    if context != 65536: return
    try:
        data=data or {}; obs=observation or {}; row={'ts':time.time(),'num_ctx':context,'error':bool(error)}
        for src,dst,scale in [('prompt_eval_count','input_tokens',1),('eval_count','output_tokens',1),('load_duration','load_seconds',1e-9),('prompt_eval_duration','prompt_seconds',1e-9),('eval_duration','generation_seconds',1e-9)]:
            v=data.get(src)
            if type(v) in (int,float) and math.isfinite(v) and v>=0: row[dst]=v*scale
        if type(elapsed) in (int,float) and math.isfinite(elapsed) and elapsed>=0:row['wall_seconds']=elapsed
        for k in ['resident_before','resident_context_before','runtime_uptime_seconds']:
            v=obs.get(k)
            if type(v) is bool or (type(v) in (int,float) and math.isfinite(v) and v>=0):row[k]=v
        row['empty']=not error and not str(data.get('response') or '').strip()
        row['finish_length']=data.get('done_reason')=='length'
        try:row['available_gib']=memory()
        except Exception:row['memory_unknown']=True
        inp=row.get('input_tokens');out=row.get('output_tokens');wall=row.get('wall_seconds')
        # Conservative one-sample Nov20 bucket baselines; any false alarm reverts.
        limit=14.918*1.2 if inp is not None and inp<2000 and out is not None and out<128 else (29.308*1.2 if inp is not None and inp<2000 and out is not None and out>=512 else None)
        row['wall_regression']=limit is not None and wall is not None and wall>limit
        row['load_regression']=row.get('load_seconds',0)>12.308*1.2
        row['memory_regression']=row.get('available_gib',0)<32
        row['rollback']=any(row[k] for k in ['error','empty','finish_length','wall_regression','load_regression','memory_regression'])
        if row['rollback']:stop(root)
        path=Path(root)/'out/daily-context-trial.jsonl';path.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
        with os.fdopen(fd,'a') as f:f.write(json.dumps(row,allow_nan=False)+'\n')
    except Exception:
        # Loss of trial monitoring ends the trial; original answer semantics survive.
        try:stop(root)
        except Exception:pass
