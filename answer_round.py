"""Explicit two-item local continuation: four calls, 600 seconds, no repairs.

Separate private ledger; never writes predecessor requests or their budgets.
Uses the unchanged qualified answer interface, parser and exact-source tracing.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import time
import urllib.parse
import urllib.request

MAX_CALLS, ITEM_CALLS, MAX_SECONDS = 4, 2, 600
MAX_INPUT, MAX_CANDIDATE, TARGET = 12000, 1800, 1600
GUIDANCE = (
    'Fresh explicitly authorized continuation; previous failures remain historical. '
    'Answer the COMPLETE original question with every necessary limitation. '
    'Target at most 1600 UTF-8 bytes for the ENTIRE result JSON, including provider metadata. '
    'Use concise answer/reason text and short EXACT contiguous evidence quotations. '
    'The answer field MUST be a string, even if the question asks for compact JSON: '
    'put that JSON inside the answer string. Never substitute an approximate quotation. '
    'If support, claim mapping, population, causality or scope is uncertain, abstain. '
    'Do not remove a required claim or limitation to fit. There are no repair attempts. '
    'Verification must check every ask and the complete source scope independently '
    'of the candidate assertions. No clinical or ranking changes are authorized.'
)


class Held(ValueError):
    pass


class DeadlineReached(BaseException):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def private_write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


class Ledger:
    def __init__(self, directory, clock=time.time):
        self.directory, self.clock = Path(directory), clock

    def connect(self):
        db = sqlite3.connect('file:' + str(self.directory / 'round.sqlite3') + '?mode=rw',
                             uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def initialize(self, manifest):
        if (not isinstance(manifest.get('authorization'), str) or not manifest['authorization'].strip()
                or len(manifest.get('items', [])) != 2):
            raise Held('explicit_two_item_authorization_required')
        items = manifest['items']
        if ({x['name'] for x in items} != {'size', 'quotation'}
                or len({x['predecessor_id'] for x in items}) != 2):
            raise Held('invalid_predecessors')
        for item in items:
            packet = item['packet']
            if (not isinstance(packet['question'], str) or not packet['question']
                    or not isinstance(packet['sources'], dict)
                    or not isinstance(packet['original_sources'], dict)
                    or not isinstance(packet['registry'], dict)
                    or set(packet['sources']) != set(packet['registry'])
                    or not item['allowed_source_aliases']
                    or not set(item['allowed_source_aliases']) <= set(packet['sources'])):
                raise Held('invalid_scoped_packet')
        self.directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        private_write(self.directory / 'manifest.json', manifest)
        db = sqlite3.connect(self.directory / 'round.sqlite3')
        try:
            db.executescript('''
                CREATE TABLE control (manifest_hash TEXT, started REAL, calls INTEGER,
                    max_calls INTEGER, item_calls INTEGER, max_seconds INTEGER);
                CREATE TABLE items (name TEXT PRIMARY KEY, state TEXT, reason TEXT);
                CREATE TABLE attempts (item TEXT, stage TEXT, state TEXT, reserved REAL,
                    PRIMARY KEY(item,stage));
            ''')
            db.execute('INSERT INTO control VALUES (?,NULL,0,?,?,?)',
                       (digest(manifest), MAX_CALLS, ITEM_CALLS, MAX_SECONDS))
            db.executemany('INSERT INTO items VALUES (?,\'pending\',\'\')',
                           [(x['name'],) for x in items])
            db.commit()
        finally:
            db.close()
        os.chmod(self.directory / 'round.sqlite3', 0o600)

    def reserve(self, item, stage):
        now = self.clock()
        with contextlib.closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            control = db.execute('SELECT * FROM control').fetchone()
            if (control['max_calls'], control['item_calls'], control['max_seconds']) != (
                    MAX_CALLS, ITEM_CALLS, MAX_SECONDS):
                raise Held('round_limits_changed')
            if digest(read(self.directory / 'manifest.json')) != control['manifest_hash']:
                raise Held('manifest_changed')
            row = db.execute('SELECT * FROM items WHERE name=?', (item,)).fetchone()
            previous = list(db.execute('SELECT * FROM attempts WHERE item=?', (item,)))
            if not row or row['state'] != 'pending' or stage not in ('draft', 'verify'):
                raise Held('item_closed')
            if (len(previous) >= ITEM_CALLS or any(r['stage'] == stage for r in previous)
                    or (stage == 'verify' and not any(r['stage'] == 'draft' and r['state'] == 'pass'
                                                     for r in previous))
                    or (stage == 'draft' and previous)):
                raise Held('stage_not_available')
            started = control['started'] if control['started'] is not None else now
            if control['calls'] >= MAX_CALLS or not 0 <= now - started < MAX_SECONDS:
                raise Held('round_exhausted')
            db.execute('UPDATE control SET started=?,calls=calls+1', (started,))
            db.execute('INSERT INTO attempts VALUES (?,?,\'reserved\',?)', (item, stage, now))
            return started + MAX_SECONDS

    def finish(self, item, stage, passed, reason=''):
        with contextlib.closing(self.connect()) as db, db:
            db.execute('UPDATE attempts SET state=? WHERE item=? AND stage=?',
                       ('pass' if passed else 'held', item, stage))
            if not passed or stage == 'verify':
                db.execute('UPDATE items SET state=?,reason=? WHERE name=?',
                           ('verified' if passed else 'held', reason, item))

    def hold(self, item, reason):
        with contextlib.closing(self.connect()) as db, db:
            db.execute('UPDATE items SET state=\'held\',reason=? WHERE name=?', (reason, item))

    def recover(self):
        with contextlib.closing(self.connect()) as db, db:
            db.execute("UPDATE items SET state='held',reason='interrupted_call' WHERE name IN "
                       "(SELECT item FROM attempts WHERE state='reserved')")

    def summary(self):
        with contextlib.closing(self.connect()) as db:
            control = dict(db.execute('SELECT * FROM control').fetchone())
            states = [r['state'] for r in db.execute('SELECT state FROM items')]
        return {'items': len(states), 'calls': control['calls'], 'call_limit': MAX_CALLS,
                'verified': states.count('verified'), 'held': states.count('held'),
                'pending': states.count('pending'), 'time_limit_seconds': MAX_SECONDS,
                'elapsed_seconds': 0 if control['started'] is None else
                    max(0, int(self.clock() - control['started'])), 'old_budget_changes': 0}

    def item_state(self, name):
        with contextlib.closing(self.connect()) as db:
            return db.execute('SELECT state FROM items WHERE name=?', (name,)).fetchone()['state']


@contextlib.contextmanager
def hard_deadline(deadline):
    remaining = deadline - time.time()
    if remaining <= 0:
        raise DeadlineReached()
    def expire(*_):
        raise DeadlineReached()
    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class QualifiedLocal:
    def __init__(self, root):
        import answer_workflow as aw
        self.root = Path(root)
        self.creds = read(self.root / 'config/credentials.json')
        self.models = aw.Models(self.root, self.creds)

    def preflight(self):
        import answer_workflow as aw
        import capability_registry as registry
        import capability_admission as admission
        import capability_health as health
        endpoint = urllib.parse.urlsplit(self.creds.get('ollama_url', ''))
        model = self.creds.get('ollama_model', '')
        if (endpoint.hostname not in ('127.0.0.1', 'localhost', '::1')
                or endpoint.scheme != 'http' or not model or 'cloud' in model.lower()):
            raise Held('native_owned_route_required')
        config = read(self.root / 'config/answer-workflow-capabilities.json')
        contract = registry.contract_digest(config, self.root)
        records = [r for r in config['evaluations'] if r.get('id') == 'ollama']
        observed = health.observe(self.creds, 'ollama')
        eligible = admission.candidates(records, [observed], task=aw.TASK, capability=aw.CAPABILITY,
                                        system=aw.SYSTEM, contract_sha256=contract)
        prefix = urllib.parse.urlunsplit((endpoint.scheme, endpoint.netloc, '', '', ''))
        with urllib.request.urlopen(prefix + '/api/ps', timeout=5) as response:
            resident = json.load(response).get('models', [])
        owned = [r for r in resident if (r.get('name') or r.get('model')) == model
                 and r.get('size_vram', 0) > 0 and r.get('digest')]
        if config.get('enabled') is not True or len(eligible) != 1 or len(owned) != 1:
            raise Held('qualified_resident_model_required')
        return model

    def call(self, payload, deadline, session_id):
        return self.models.call(payload, pool='local', exclude=('vllm',), privacy='LOCAL_ONLY',
                                session_id=session_id, deadline=deadline)


def validate(result, packet, allowed):
    import answer_workflow as aw
    from inbox_evidence import trace_canonical_quote
    sources = {k: aw.canonical_view(v) for k, v in packet['sources'].items()}
    parsed = aw.parse(result['raw'], sources)
    if (not parsed or parsed['kind'] != 'grounded' or not parsed['evidence']
            or not aw.reviewed(parsed, packet['question'])):
        raise Held('candidate_abstained_or_invalid')
    candidate = dict(parsed, provider=result['provider'])
    if len(encoded(candidate)) > MAX_CANDIDATE:
        raise Held('candidate_over_budget')
    for evidence in parsed['evidence']:
        sid, quote = evidence['source'], evidence['quote']
        if sid not in allowed or quote not in packet['sources'][sid] or len(quote.strip()) < 8:
            raise Held('quotation_or_source_scope')
        entry = packet['registry'][sid]
        raw = packet['original_sources'][entry['original_source_key']]
        if (type(entry['start']) is not int or type(entry['end']) is not int
                or not 0 <= entry['start'] < entry['end'] <= len(raw)):
            raise Held('source_registry_mismatch')
        segment = raw[entry['start']:entry['end']]
        if segment != packet['sources'][sid]:
            raise Held('source_registry_mismatch')
        trace_canonical_quote(segment, quote)
    return candidate


def execute(directory, models):
    import answer_workflow as aw
    ledger = Ledger(directory)
    with (Path(directory) / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ledger.recover()
        manifest = read(Path(directory) / 'manifest.json')
        states = ledger.summary()
        if not states['pending']:
            return states
        try:
            model = models.preflight()
        except Exception:
            for item in manifest['items']:
                ledger.hold(item['name'], 'preflight_failed')
            return ledger.summary()
        for item in manifest['items']:
            name, packet = item['name'], item['packet']
            if ledger.item_state(name) != 'pending':
                continue
            candidate = None
            for stage in ('draft', 'verify'):
                receipt = {'stage': stage, 'model': model}
                try:
                    payload = {'question': packet['question'],
                               'sources': {k: aw.canonical_view(v) for k, v in packet['sources'].items()},
                               'history': [], 'drafts': [] if candidate is None else [candidate],
                               'mode': 'draft' if stage == 'draft' else 'review',
                               'response_constraints': GUIDANCE}
                    if len(encoded(payload)) > MAX_INPUT:
                        raise Held('complete_input_exceeds_capacity')
                    deadline = ledger.reserve(name, stage)
                    receipt.update(input_bytes=len(encoded(payload)), deadline=deadline)
                    with hard_deadline(deadline):
                        result = models.call(payload, deadline, digest([str(directory), name]))
                    receipt['result'] = result
                    if time.time() >= deadline:
                        raise Held('late_output')
                    candidate = validate(result, packet, item['allowed_source_aliases'])
                    receipt['candidate_bytes'] = len(encoded(candidate))
                    receipt['candidate'] = candidate
                    private_write(Path(directory) / (name + '-' + stage + '.json'), receipt)
                    ledger.finish(name, stage, True)
                except (Exception, DeadlineReached) as exc:
                    receipt['held_reason'] = str(exc) if isinstance(exc, Held) else type(exc).__name__
                    # Do not serialize provider exception strings: they can contain private bodies.
                    ledger.finish(name, stage, False, receipt['held_reason'])
                    path = Path(directory) / (name + '-' + stage + '-held.json')
                    if not path.exists():
                        private_write(path, receipt)
                    break
        return ledger.summary()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'run', 'status'])
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    try:
        ledger = Ledger(args.directory)
        if args.action == 'init':
            ledger.initialize(read(args.manifest))
        if args.action == 'run':
            result = execute(args.directory, QualifiedLocal(args.root))
        else:
            result = ledger.summary()
        print(json.dumps(result))
    except (Exception, DeadlineReached):
        print(json.dumps({'round_command_errors': 1, 'private_output_withheld': 1}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
