"""Durable per-request model-call ceiling shared by email, phone and desktop."""
import hashlib
import json
import time

MAX_CALLS = 6
MAX_SECONDS = 600


def request_key(row):
    origin = row['meta'].get('request_origin', row['id'])
    return hashlib.sha256(json.dumps([row['owner'], row['client'], row['project'], origin]).encode()).hexdigest()


def reserve(memory, row):
    """Claim before inference. Crashes consume the slot; restarts cannot reset it."""
    key = request_key(row)
    now = time.time()
    with memory.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS attempt_limits (key TEXT PRIMARY KEY, started REAL NOT NULL, calls INTEGER NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT OR IGNORE INTO attempt_limits VALUES (?,?,0)', (key, now))
        saved = db.execute('SELECT started,calls FROM attempt_limits WHERE key=?', (key,)).fetchone()
        if saved['calls'] >= MAX_CALLS or now-saved['started'] >= MAX_SECONDS or now < saved['started']:
            raise ValueError('Request attempt/time limit reached')
        db.execute('UPDATE attempt_limits SET calls=calls+1 WHERE key=?', (key,))
        return {'calls':saved['calls']+1, 'deadline':saved['started']+MAX_SECONDS}


def input_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
