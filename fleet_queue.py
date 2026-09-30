"""Durable, policy-bound Cumulus queue. No shell execution or delivery authority.

The controller owns only registered attempts. Existing jobs stay observed-only
until explicitly qualified and migrated. Unknown attempts retain their slot:
lease expiry is evidence to investigate, never permission to run a duplicate.
"""
from __future__ import annotations
import contextlib
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

ACTIVE = ('running', 'validating', 'suspected_stall', 'cancelling', 'unknown')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Refused(ValueError):
    pass


class Queue:
    def __init__(self, path, policy, clock=time.time, monotonic=time.monotonic, epoch=None):
        self.path = str(path)
        self.policy = json.loads(json.dumps(policy))
        self.hash = digest(policy)
        self.clock, self.monotonic = clock, monotonic
        self.epoch = epoch or (Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                              if Path('/proc/sys/kernel/random/boot_id').exists() else 'local-test')
        self.validate_policy()
        marker = Path(self.path + '.initialized')
        if marker.exists() and not Path(path).exists():
            raise Refused('queue database missing; restore and reconcile, do not recreate')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as c:
            c.executescript('''
              CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, health TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'ready');
              CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, request_key TEXT NOT NULL,
                input_hash TEXT NOT NULL, policy_hash TEXT NOT NULL, state TEXT NOT NULL,
                created REAL NOT NULL, ready REAL NOT NULL, priority INTEGER NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0, worker TEXT, fence TEXT,
                stage TEXT NOT NULL DEFAULT 'queued', progress INTEGER NOT NULL DEFAULT 0,
                heartbeat REAL, advanced REAL, epoch TEXT, lease_until REAL,
                reason TEXT NOT NULL DEFAULT '', receipt TEXT, result TEXT,
                UNIQUE(project, request_key));
              CREATE UNIQUE INDEX IF NOT EXISTS occupied_worker ON jobs(worker)
                WHERE state IN ('running','validating','suspected_stall','cancelling','unknown');
              CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL, job TEXT, kind TEXT NOT NULL, detail TEXT NOT NULL);
            ''')
            if not c.in_transaction:
                c.execute('BEGIN IMMEDIATE')
            old = c.execute("SELECT value FROM meta WHERE key='policy'").fetchone()
            if old and old[0] != self.hash:
                raise Refused('policy drift: reconcile before opening this queue')
            c.execute("INSERT OR IGNORE INTO meta VALUES ('policy',?)", (self.hash,))
            c.execute("INSERT OR IGNORE INTO meta VALUES ('paused','false')")
        marker.touch(exist_ok=True)

    @contextlib.contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            c.execute('PRAGMA foreign_keys=ON')
            c.execute('PRAGMA busy_timeout=10000')
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def validate_policy(self):
        owners = {}
        for project, p in self.policy['projects'].items():
            required = {'workers','model_ids','validator','max_retries','stall_seconds',
                        'heartbeat_seconds','max_input_bytes','max_output_tokens','qualification_until',
                        'contract','mode','owner','resource'}
            if required - p.keys():
                raise Refused('missing project fields: '+','.join(sorted(required-p.keys())))
            if p['mode'] not in ('pilot', 'observed') or p['validator'] not in ('fixture_json','media_manifest'):
                raise Refused('unqualified mode or validator')
            if p['max_retries'] < 0 or p['max_retries'] > 2:
                raise Refused('retry policy outside bounded range')
            if p.get('delivery', 'none') != 'none':
                raise Refused('queue has no delivery authority')
            if not p['workers'] or any(w not in self.policy['workers'] for w in p['workers']):
                raise Refused('unknown worker')
            for w in p['workers']:
                if p['model_ids'].get(w) != self.policy['workers'][w]['model']:
                    raise Refused('qualification model mismatch')
            if p['mode'] != 'observed':
                resource = p['resource']
                if resource in owners and owners[resource] != p['owner']:
                    raise Refused('competing recovery owners')
                owners[resource] = p['owner']

    def event(self, c, job, kind, detail):
        c.execute('INSERT INTO events(ts,job,kind,detail) VALUES(?,?,?,?)',
                  (self.clock(),job,kind,json.dumps(detail,sort_keys=True)))

    def submit(self, project, request_key, payload, contract, priority=0):
        p = self.policy['projects'].get(project)
        if not p or p['mode'] != 'pilot' or p['contract'] != contract:
            raise Refused('unqualified project or contract')
        raw = json.dumps(payload, sort_keys=True).encode()
        if len(raw) > p['max_input_bytes'] or p['qualification_until'] <= self.clock():
            raise Refused('expired qualification or oversized input')
        input_hash = digest(payload)
        with self.db() as c:
            row = c.execute('SELECT * FROM jobs WHERE project=? AND request_key=?', (project,request_key)).fetchone()
            if row:
                if row['input_hash'] != input_hash:
                    raise Refused('deduplication key reused with different input')
                return row['id']
            job = uuid.uuid4().hex
            c.execute('INSERT INTO jobs(id,project,request_key,input_hash,policy_hash,state,created,ready,priority) VALUES(?,?,?,?,?,?,?,?,?)',
                      (job,project,request_key,input_hash,self.hash,'queued',self.clock(),self.clock(),int(priority)))
            c.execute("UPDATE jobs SET reason='awaiting qualified worker and capacity' WHERE id=?",(job,))
            self.event(c,job,'submitted',{'project':project})
            return job

    def health(self, worker, model, context, available_mib, busy=False, observed=None):
        if worker not in self.policy['workers']:
            raise Refused('unknown worker')
        record = dict(model=model,context=context,available_mib=available_mib,busy=bool(busy),
                      observed=self.clock() if observed is None else observed)
        with self.db() as c:
            c.execute("INSERT INTO workers(id,health) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET health=excluded.health",(worker,json.dumps(record)))

    def claim(self, worker, job_id=None):
        now = self.clock()
        with self.db() as c:
            if c.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] == 'true':
                return None
            w = c.execute('SELECT * FROM workers WHERE id=?',(worker,)).fetchone()
            if not w or w['mode'] != 'ready':
                return None
            h = json.loads(w['health'])
            spec = self.policy['workers'][worker]
            if not 0 <= now-h['observed'] <= 90 or h['model'] != spec['model'] or h['busy'] or h['available_mib'] < spec['min_available_mib']:
                return None
            if c.execute('SELECT 1 FROM jobs WHERE worker=? AND state IN (?,?,?,?,?)',(worker,*ACTIVE)).fetchone():
                return None
            # Aging makes one priority point per minute; bounded priorities prevent starvation.
            rows = c.execute("SELECT * FROM jobs WHERE state IN ('queued','retry_wait') AND ready<=? ORDER BY (MIN(10,MAX(-10,priority)) + (? - created)/60.0) DESC, created",(now,now)).fetchall()
            for row in rows:
                if job_id is not None and row['id'] != job_id:
                    continue
                p = self.policy['projects'][row['project']]
                if worker not in p['workers']:
                    continue
                if p['qualification_until'] <= now:
                    c.execute("UPDATE jobs SET state='blocked',reason='qualification expired' WHERE id=?",(row['id'],))
                    continue
                # UTF-8 bytes are a conservative tokenizer-independent bound for these bounded fixtures.
                if p.get('request_context_bound',p['max_input_bytes']) + p['max_output_tokens'] + 2048 > h['context']:
                    continue
                fence = uuid.uuid4().hex
                c.execute("UPDATE jobs SET state='running',worker=?,fence=?,attempt=attempt+1,stage='inference',progress=0,heartbeat=?,advanced=?,epoch=?,lease_until=?,reason='' WHERE id=?",(worker,fence,self.monotonic(),self.monotonic(),self.epoch,self.monotonic()+90,row['id']))
                self.event(c,row['id'],'claimed',{'worker':worker,'model':spec['model'],'attempt':row['attempt']+1})
                return dict(c.execute('SELECT * FROM jobs WHERE id=?',(row['id'],)).fetchone())
        return None

    def owned(self,c,job,fence):
        row = c.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone()
        if not row or row['fence'] != fence or row['policy_hash'] != self.hash or row['state'] not in ACTIVE:
            raise Refused('stale or unowned attempt')
        return row

    def progress(self,job,fence,value,stage='inference'):
        with self.db() as c:
            row = self.owned(c,job,fence)
            if row['state'] in ('unknown','cancelling'):
                raise Refused('attempt requires reconciliation')
            if value < row['progress']:
                raise Refused('progress cannot decrease')
            advanced = self.monotonic() if value > row['progress'] else row['advanced']
            state = 'running' if value > row['progress'] else row['state']
            c.execute('UPDATE jobs SET progress=?,stage=?,heartbeat=?,advanced=?,lease_until=?,state=? WHERE id=?',
                      (value,stage,self.monotonic(),advanced,self.monotonic()+90,state,job))

    def monitor(self):
        with self.db() as c:
            rows = c.execute('SELECT * FROM jobs WHERE state IN (?,?,?,?,?)',ACTIVE).fetchall()
            for row in rows:
                p = self.policy['projects'][row['project']]
                state, reason = row['state'], row['reason']
                if row['epoch'] != self.epoch or row['lease_until'] < self.monotonic():
                    state,reason='unknown','lease or boot changed; reconcile, never reassign'
                elif self.monotonic()-row['heartbeat'] > 3*p['heartbeat_seconds']:
                    state,reason='unknown','missing worker heartbeat'
                elif self.monotonic()-row['advanced'] > p['stall_seconds'] and state == 'running':
                    state,reason='suspected_stall','no meaningful progress; independent confirmation required'
                if state != row['state']:
                    c.execute('UPDATE jobs SET state=?,reason=? WHERE id=?',(state,reason,row['id']))
                    self.event(c,row['id'],state,{'reason':reason})

    def complete(self,job,fence,result):
        with self.db() as c:
            row = self.owned(c,job,fence)
            if row['state'] not in ('running','validating','suspected_stall'):
                raise Refused('cannot publish cancelled or unknown work')
            spec = self.policy['workers'][row['worker']]
            # Fixture contract validates exact source values, missing-evidence abstention and coverage.
            valid = (isinstance(result,dict) and result.get('model') == spec['model'] and
                     result.get('finish_reason') == 'stop' and result.get('data') ==
                     {'sample':12,'control_group':False,'trial_date':None} and result.get('coverage') == 1)
            if self.policy['projects'][row['project']]['validator'] == 'media_manifest':
                valid = (isinstance(result,dict) and result.get('model') == spec['model'] and
                         result.get('input_hash') == row['input_hash'] and result.get('complete') is True and
                         type(result.get('sections')) is int and result['sections']>0 and
                         result.get('completed') == result['sections'] and result.get('source_verified') is True)
            state='succeeded' if valid else 'failed'
            c.execute('UPDATE jobs SET state=?,stage=?,result=?,reason=? WHERE id=?',
                      (state,'validated',json.dumps(result) if valid else None,'' if valid else 'completion validator failed',job))
            self.event(c,job,state,{'validator':self.policy['projects'][row['project']]['validator']})
            return valid

    def unknown(self,job,fence,reason):
        with self.db() as c:
            self.owned(c,job,fence)
            c.execute("UPDATE jobs SET state='unknown',reason=? WHERE id=?",(reason,job))
            self.event(c,job,'unknown',{'reason':reason})

    def cancel(self,job,fence,policy_hash,confirmed_stall=False):
        if policy_hash != self.hash or not confirmed_stall:
            raise Refused('missing policy or independent stall confirmation')
        with self.db() as c:
            row=self.owned(c,job,fence)
            if row['state'] not in ('suspected_stall','unknown'):
                raise Refused('only confirmed stalled attempts may be cancelled')
            c.execute("UPDATE jobs SET state='cancelling',reason='awaiting verified termination' WHERE id=?",(job,))
            self.event(c,job,'cancel_requested',{'attempt':row['attempt']})

    def stopped(self,job,fence,confirmed,backend_idle,delivery='none'):
        if not confirmed or not backend_idle:
            raise Refused('process and backend termination must both be verified')
        with self.db() as c:
            row=self.owned(c,job,fence)
            if row['state'] not in ('unknown','cancelling'):
                raise Refused('not reconciling a stopped attempt')
            state='cancelled' if delivery == 'none' else 'blocked'
            c.execute('UPDATE jobs SET state=?,receipt=?,reason=? WHERE id=?',
                      (state,delivery,'reconciled' if state=='cancelled' else 'delivery outcome needs reconciliation',job))
            self.event(c,job,state,{'backend_idle':True})

    def retry(self,job,policy_hash):
        if policy_hash != self.hash:
            raise Refused('policy mismatch')
        with self.db() as c:
            row=c.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone()
            if not row or row['state'] != 'cancelled' or row['receipt'] != 'none':
                raise Refused('only reconciled, no-delivery attempts may retry')
            p=self.policy['projects'][row['project']]
            if row['attempt'] > p['max_retries']:
                c.execute("UPDATE jobs SET state='blocked',reason='retry budget exhausted' WHERE id=?",(job,))
                self.event(c,job,'blocked',{'reason':'retry budget exhausted'})
                return False
            delay=60*2**(row['attempt']-1)
            c.execute("UPDATE jobs SET state='retry_wait',ready=?,fence=NULL,worker=NULL,stage='backoff' WHERE id=?",(self.clock()+delay,job))
            self.event(c,job,'retry_wait',{'backoff_seconds':delay})
            return True

    def control(self,action,worker=None):
        with self.db() as c:
            if action in ('pause','resume') and worker is None:
                c.execute("UPDATE meta SET value=? WHERE key='paused'",('true' if action=='pause' else 'false',))
            elif action in ('drain','ready') and worker in self.policy['workers']:
                c.execute('UPDATE workers SET mode=? WHERE id=?',(action,worker))
            else:
                raise Refused('unsupported operator action')
            self.event(c,None,action,{'worker':worker})

    def status(self):
        with self.db() as c:
            return {'policy_hash':self.hash,'observed':self.clock(),
                    'paused':c.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0]=='true',
                    'jobs':[dict(r) for r in c.execute('SELECT * FROM jobs ORDER BY created')],
                    'workers':[dict(r) for r in c.execute('SELECT * FROM workers')],
                    'events':[dict(r) for r in c.execute('SELECT * FROM events ORDER BY seq DESC LIMIT 40')]}

    def backup(self,path):
        # sqlite online backup, never an unsafe copy of a live database.
        with contextlib.closing(sqlite3.connect(self.path)) as source, contextlib.closing(sqlite3.connect(path)) as dest:
            source.backup(dest)


def selftest():
    """Exercise the queue's decision-making functions with explicit inputs and
    expected outputs. Purely in temporary directories; no live queue touched."""
    import sys
    import tempfile
    checks = []
    def check(name, cond):
        checks.append((name, bool(cond)))
    policy = {
        'workers': {'w1': {'model': 'm1', 'min_available_mib': 100}},
        'projects': {
            'proj': {'workers': ['w1'], 'model_ids': {'w1': 'm1'},
                     'validator': 'fixture_json', 'max_retries': 1,
                     'stall_seconds': 30, 'heartbeat_seconds': 10,
                     'max_input_bytes': 100000, 'max_output_tokens': 100,
                     'qualification_until': 2000.0, 'contract': 'c1',
                     'mode': 'pilot', 'owner': 'o1', 'resource': 'r1'},
        },
    }
    check('digest deterministic', digest({'a': 1, 'b': 2}) == digest({'b': 2, 'a': 1}))
    check('digest sensitive to value', digest({'a': 1}) != digest({'a': 2}))
    with tempfile.TemporaryDirectory() as d:
        bad = json.loads(json.dumps(policy))
        del bad['projects']['proj']['resource']
        try:
            Queue(Path(d) / 'bad.db', bad, clock=lambda: 1000.0, monotonic=lambda: 50.0)
            check('incomplete policy refused', False)
        except Refused:
            check('incomplete policy refused', True)
        q = Queue(Path(d) / 'q.db', policy, clock=lambda: 1000.0, monotonic=lambda: 50.0)
        job = q.submit('proj', 'rk1', {'x': 1}, 'c1')
        check('submit returns job id', isinstance(job, str) and len(job) == 32)
        check('resubmit same payload idempotent', q.submit('proj', 'rk1', {'x': 1}, 'c1') == job)
        try:
            q.submit('proj', 'rk1', {'x': 2}, 'c1')
            check('dedup key reuse refused', False)
        except Refused:
            check('dedup key reuse refused', True)
        try:
            q.submit('proj', 'rk2', {'x': 1}, 'wrong-contract')
            check('wrong contract refused', False)
        except Refused:
            check('wrong contract refused', True)
        check('claim without health returns None', q.claim('w1') is None)
        q.health('w1', 'm1', 200000, 500)
        row = q.claim('w1')
        check('claim returns running job', row is not None and row['id'] == job and row['state'] == 'running')
        fence = row['fence']
        check('second claim while active returns None', q.claim('w1') is None)
        q.progress(job, fence, 5)
        try:
            q.progress(job, fence, 3)
            check('progress decrease refused', False)
        except Refused:
            check('progress decrease refused', True)
        good = {'model': 'm1', 'finish_reason': 'stop',
                'data': {'sample': 12, 'control_group': False, 'trial_date': None}, 'coverage': 1}
        check('valid fixture completion accepted', q.complete(job, fence, good) is True)
        check('job recorded succeeded', q.status()['jobs'][0]['state'] == 'succeeded')
        job2 = q.submit('proj', 'rk3', {'y': 2}, 'c1')
        row2 = q.claim('w1')
        check('second job claimable after completion', row2 is not None and row2['id'] == job2)
        check('invalid fixture completion rejected', q.complete(job2, row2['fence'], {'model': 'm1'}) is False)
        check('failed job recorded', [j for j in q.status()['jobs'] if j['id'] == job2][0]['state'] == 'failed')
        q.control('pause')
        check('pause reflected in status', q.status()['paused'] is True)
        check('claim while paused returns None', q.claim('w1') is None)
        q.control('resume')
        check('resume reflected in status', q.status()['paused'] is False)
        try:
            q.control('bogus')
            check('unsupported control refused', False)
        except Refused:
            check('unsupported control refused', True)
        # Lease-expiry path: monitor must flag the attempt unknown, then the
        # cancel/stopped/retry reconciliation chain must behave in order.
        mono = [50.0]
        q2 = Queue(Path(d) / 'q2.db', policy, clock=lambda: 1000.0, monotonic=lambda: mono[0])
        j = q2.submit('proj', 'rk9', {'z': 9}, 'c1')
        q2.health('w1', 'm1', 200000, 500)
        r = q2.claim('w1')
        check('q2 claim succeeds', r is not None and r['id'] == j)
        f = r['fence']
        try:
            q2.cancel(j, f, q2.hash, confirmed_stall=False)
            check('cancel without stall confirmation refused', False)
        except Refused:
            check('cancel without stall confirmation refused', True)
        mono[0] += 200.0
        q2.monitor()
        state = [x for x in q2.status()['jobs'] if x['id'] == j][0]['state']
        check('monitor flags expired lease unknown', state == 'unknown')
        q2.cancel(j, f, q2.hash, confirmed_stall=True)
        check('confirmed cancel moves to cancelling',
              [x for x in q2.status()['jobs'] if x['id'] == j][0]['state'] == 'cancelling')
        try:
            q2.stopped(j, f, confirmed=True, backend_idle=False)
            check('stopped without idle backend refused', False)
        except Refused:
            check('stopped without idle backend refused', True)
        q2.stopped(j, f, confirmed=True, backend_idle=True)
        check('reconciled stop cancelled',
              [x for x in q2.status()['jobs'] if x['id'] == j][0]['state'] == 'cancelled')
        check('retry within budget accepted', q2.retry(j, q2.hash) is True)
        check('retried job in retry_wait',
              [x for x in q2.status()['jobs'] if x['id'] == j][0]['state'] == 'retry_wait')
    failed = [n for n, ok in checks if not ok]
    for name, ok in checks:
        print(('PASS' if ok else 'FAIL'), name)
    print('selftest: %d/%d checks passed' % (len(checks) - len(failed), len(checks)))
    return not failed


if __name__ == '__main__':
    import sys
    if '--selftest' in sys.argv:
        sys.exit(0 if selftest() else 1)
