"""Two-way approval/guidance flow for the CUMULUS supervisor (Skywarden) — S64/S65.

Legacy primary request plus bounded incident-keyed decision files.
Two request kinds share a compatibility reader; unrelated guidance cannot block.
Previously, two request kinds shared one pending-request slot. Skywarden ends every run
with exactly one send_telegram call (CLAUDE.md sec 6), so in practice only
one ask is outstanding at a time. An unanswered or unconsumed request is
never overwritten; repeated guidance for the same incident is suppressed:

- "opus_upgrade": yes/no. Buddy's reply must be exactly
  "approve" (case-insensitive). Approved unlocks exactly ONE Opus pass on
  Skywarden's next invocation, then it reverts to Sonnet automatically.
- "guidance": free-text. Buddy's whole reply becomes Skywarden's direction
  on its next invocation. For when Skywarden is genuinely stuck — tried its
  allowed diagnostics/fixes, the problem persists, and it has no remaining
  tool that could address it (CLAUDE.md sec 3) — not for routine anomalies
  it can already report-and-move-on from via send_telegram.

Module polls for Buddy's Telegram reply via short, non-blocking `getUpdates`
calls (offset-tracked) folded into the existing 60s heartbeat tick —
deliberately NOT a persistent long-poll listener like cirrus_bot.py, since
Skywarden's process model is wake/check/sleep, not a standing service.
"""
import json
import fcntl
import functools
import threading
import hashlib
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

STATE_DIR = Path("/opt/cumulus-supervisor/state")
REQUEST_FILE = STATE_DIR / "pending-request.json"
UPDATE_OFFSET_FILE = STATE_DIR / "telegram-update-offset.txt"

REQUEST_EXPIRY_SEC = 2 * 3600  # one-time model upgrade approval
GUIDANCE_EXPIRY_SEC = 7 * 86400  # actual decisions survive time away

def _expiry(req):
    return GUIDANCE_EXPIRY_SEC if req.get("kind") == "guidance" else REQUEST_EXPIRY_SEC


def _write_json(path, data):
    fd, tmp = tempfile.mkstemp(prefix='.request-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _load_secrets() -> dict:
    with open("/opt/cumulus-supervisor/state/secrets.json") as f:
        return json.load(f)


def _api_call(method: str, params: dict, token: str, timeout: int = 10):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json",
                                  "User-Agent": "CUMULUS-supervisor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


_THREAD_LOCK = threading.RLock()
_LOCK_DEPTH = 0


def locked(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        global _LOCK_DEPTH
        with _THREAD_LOCK:
            if _LOCK_DEPTH:
                return fn(*args, **kwargs)
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with (STATE_DIR/'decisions.lock').open('a') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                _LOCK_DEPTH += 1
                try:
                    return fn(*args, **kwargs)
                finally:
                    _LOCK_DEPTH -= 1
                    fcntl.flock(handle, fcntl.LOCK_UN)
    return wrapped


# Legacy primary request remains in place for rollback and old replies. Extra
# decisions live in separate atomic files, bounded to 32 outstanding requests.
LAST_REQUEST_PATH = None


def requests():
    paths = ([REQUEST_FILE] if REQUEST_FILE.exists() else [])
    paths += sorted((STATE_DIR / 'pending-requests').glob('*.json'))
    return [(p, json.loads(p.read_text())) for p in paths]


def _request_slot_busy():
    return any(r.get('status') in ('answered', 'approved') or
               (r.get('status') == 'pending' and time.time()-r.get('requested_at', 0)<_expiry(r))
               for _, r in requests())


def ready_reply_id():
    return '|'.join(str(r.get('request_id', r.get('requested_at'))) for _, r in requests()
                    if r.get('status') in ('answered', 'approved'))


@locked
def record_request_delivery(sent):
    path = LAST_REQUEST_PATH or REQUEST_FILE
    req = json.loads(path.read_text())
    req['delivery_attempted_at'] = time.time()
    if not sent:
        req['status'] = 'delivery_failed'
    else:
        req['status'] = 'pending'
        if req.get('kind') == 'guidance':
            histpath = STATE_DIR / 'guidance-history.json'
            history = json.loads(histpath.read_text()) if histpath.exists() else {}
            history[req['dedup_key']] = time.time()
            _write_json(histpath, dict(sorted(history.items(), key=lambda x:x[1])[-256:]))
    _write_json(path, req)


def _create(req):
    global LAST_REQUEST_PATH
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rows = requests()
    if sum(r.get('status') in ('pending','answered','approved','delivery_failed') for _,r in rows)>=32:
        raise ValueError('guidance queue full; existing decisions preserved')
    req['request_id'] = hashlib.sha256((str(req['requested_at'])+req.get('dedup_key',req.get('reason',''))).encode()).hexdigest()[:12]
    # Never overwrite the legacy request, even after expiry; preserve history.
    if not REQUEST_FILE.exists():
        path = REQUEST_FILE
    else:
        directory = STATE_DIR / 'pending-requests'; directory.mkdir(exist_ok=True)
        path = directory / (req['request_id']+'.json')
    _write_json(path, req)
    LAST_REQUEST_PATH = path
    return req['request_id']


@locked
def create_opus_request(reason):
    if _request_slot_busy():
        return None
    rid = _create(dict(kind='opus_upgrade',reason=reason,requested_at=time.time(),status='pending'))
    return f'Skywarden requests one Opus pass: {reason}\nReply "{rid} approve" within 2 hours.'


@locked
def create_guidance_request(issue, question, context=''):
    identity = context or ' '.join((issue+' '+question).lower().split())
    key = hashlib.sha256(identity.encode()).hexdigest()
    rows = requests()
    if any(r.get('dedup_key')==key and r.get('status') in ('pending','answered','consumed') for _,r in rows):
        return None
    hp = STATE_DIR/'guidance-history.json'
    if hp.exists() and key in json.loads(hp.read_text()):
        return None
    # Retry failed delivery at most once per six hours, using the same identity.
    for path,r in rows:
        if r.get('dedup_key')==key and r.get('status')=='delivery_failed':
            if time.time()-r.get('delivery_attempted_at',0)<6*3600:
                return None
            global LAST_REQUEST_PATH
            LAST_REQUEST_PATH=path
            return f"Skywarden needs direction [{r['request_id']}]:\n{issue}\n{question}\nReply with {r['request_id']} followed by your direction."
    rid=_create(dict(kind='guidance',issue=issue,question=question,dedup_key=key,
                     requested_at=time.time(),status='pending'))
    return f'Skywarden needs direction [{rid}]:\n{issue}\n{question}\nReply with {rid} followed by your direction within 7 days.'


@locked
def answer(request_id, text, at=None):
    now=time.time() if at is None else at
    for path,r in requests():
        rid=str(r.get('request_id',r.get('requested_at')))
        if rid != str(request_id): continue
        if r.get('status')!='pending' or not r.get('requested_at',0)<=now<=r.get('requested_at',0)+_expiry(r):
            return False
        if r['kind']=='opus_upgrade':
            if text.strip().lower()!='approve': return False
            r['status']='approved'
        else:
            if not text.strip(): return False
            r.update(status='answered',reply=text.strip())
        _write_json(path,r); return True
    return False


@locked
def check_for_reply():
    rows=requests()  # corrupt evidence raises; never delete an unreadable decision
    now=time.time()
    for path,r in rows:
        if r.get('status')=='pending' and now-r.get('requested_at',0)>_expiry(r):
            r['status']='expired'; _write_json(path,r)
    pending=[r for _,r in rows if r.get('status')=='pending']
    if not pending: return
    try:
        secrets=_load_secrets()
        token,chat_id=secrets['telegram_bot_token'],str(secrets['telegram_user_id'])
        offset=int(UPDATE_OFFSET_FILE.read_text()) if UPDATE_OFFSET_FILE.exists() else 0
        result=_api_call('getUpdates',{'offset':offset,'timeout':0},token)
    except (OSError, ValueError, KeyError, urllib.error.URLError, TimeoutError):
        return
    max_id=offset-1
    for update in result.get('result',[]):
        max_id=max(max_id,update['update_id']); msg=update.get('message',{})
        if str(msg.get('from',{}).get('id',''))!=chat_id: continue
        text=(msg.get('text') or '').strip(); date=msg.get('date',0)
        for r in pending:
            rid=str(r.get('request_id',r.get('requested_at')))
            if text.startswith(rid+' '):
                answer(rid,text[len(rid)+1:],at=date); break
        else:
            # Bare replies accepted only for the pre-migration legacy request.
            # New requests always require ID: an old reply cannot approve a new ask.
            legacy=[r for r in pending if not r.get('request_id')]
            if len(legacy)==1:
                answer(legacy[0].get('requested_at'),text,at=date)
    if max_id>=offset:
        UPDATE_OFFSET_FILE.write_text(str(max_id+1))


@locked
def _consume(kind,status):
    for path,r in requests():
        if r.get('kind')==kind and r.get('status')==status:
            r['status']='consumed'; _write_json(path,r); return r
    return None


def consume_opus_approval():
    return bool(_consume('opus_upgrade','approved'))


def consume_guidance():
    r=_consume('guidance','answered')
    if r:
        return f"Request {r.get('request_id',r.get('requested_at'))}: {r.get('issue','')}\nDirection: {r.get('reply','')}"
    return None


@locked
def retry_failed_deliveries(sender):
    global LAST_REQUEST_PATH
    for path,r in requests():
        if r.get('status')!='delivery_failed' or time.time()-r.get('delivery_attempted_at',0)<6*3600:
            continue
        if time.time()-r.get('requested_at',0)>_expiry(r):
            r['status']='expired';_write_json(path,r);continue
        LAST_REQUEST_PATH=path
        # Persist reservation before network call, including interrupted sends.
        r['delivery_attempted_at']=time.time();_write_json(path,r)
        rid=r.get('request_id',str(r.get('requested_at')))
        text=f"Skywarden request {rid}: {r.get('issue',r.get('reason',''))}\n{r.get('question','')}\nReply with {rid} followed by your direction."
        try: sent=sender(text)=='sent'
        except Exception: sent=False
        record_request_delivery(sent)
        break  # at most one delivery attempt per heartbeat
