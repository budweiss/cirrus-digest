"""S664: numeric /ask lifecycle only. No identities, content or delivery claims."""
import contextvars, functools, json, os, time, uuid
from pathlib import Path
ACTIVE=contextvars.ContextVar('request_telemetry',default=None)
SUPPRESS=contextvars.ContextVar('request_telemetry_suppressed',default=False)
PATH=Path(__file__).resolve().parent/'data/request-telemetry.jsonl'

def emit(row):
    try:
        import fcntl
        PATH.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(PATH,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
        with os.fdopen(fd,'a') as f:
            fcntl.flock(f,fcntl.LOCK_EX);f.write(json.dumps(row,allow_nan=False)+'\n')
    except (OSError,ValueError):pass

def mark(key):
    if key not in ('tool_loop_attempt','local_attempt','cloud_attempt','cloud_escalation','ready_local','ready_cloud','ready_harness','ready_unknown'):return
    row=ACTIVE.get()
    if row is not None:row[key]=row.get(key,0)+1

def no_recording(fn):
    @functools.wraps(fn)
    def wrapped(*args,**kwargs):
        token=SUPPRESS.set(True)
        try:return fn(*args,**kwargs)
        finally:SUPPRESS.reset(token)
    return wrapped

class MeasuredText(str):
    """An ordinary text reply carrying only its own numeric receipt locally."""
    pass

def delivery(text, acknowledged):
    if SUPPRESS.get() or not isinstance(text, MeasuredText):return
    row=getattr(text, '_request_measurement', None)
    if not row:return
    text._request_measurement=None
    emit(dict(row,event='delivery',delivery_acknowledged=acknowledged is True,delivered_at=time.time()))

def measured(fn):
    @functools.wraps(fn)
    def wrapped(*args,**kwargs):
        if SUPPRESS.get():return fn(*args,**kwargs)
        row={'request':uuid.uuid4().hex,'harness':'bot_ask','started_at':time.time()}
        token=ACTIVE.set(row);started=time.monotonic()
        emit(dict(row,event='start'))
        result=None
        try:
            result=fn(*args,**kwargs)
            if isinstance(result,str):result=MeasuredText(result)
            return result
        finally:
            finished=dict(row,event='finish',ended_at=time.time(),wall_seconds=time.monotonic()-started)
            emit(finished)
            if isinstance(result,MeasuredText):result._request_measurement=finished
            ACTIVE.reset(token)
    return wrapped
