"""How each client email was handled, in plain words (S679).

Buddy, 2026-11-23: "Can you always explain the process that is used when an email
request is received? Explain and keep track how the emails are answered and which
processes answer them."

Reads only records this box already keeps: the intake lifecycle, the conversation
memory and the research agent's ledger. Writes one trace per request to
data/client-request-trace/ (0600) and prints the explanation. Metadata and subject
lines only; never email bodies or model output. No network, no model calls.

  python3 client_request_trace.py report [--days 7] [--json]
  python3 client_request_trace.py update [--days 7]
"""
import argparse
import json
import os
import socket
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = 'data/client-request-trace'
WORKERS = {'lesson_build': 'the lesson-build worker', 'contact_list': 'the contact-list worker',
           'project-research': "the research-topic queue (answered in the client's daily digest)",
           'video_intake': 'the video worker', 'client_request_prepare': 'the text-preparation worker'}
FIXED_REFUSALS = ('request exceeds qualified input size',)


def host():
    return 'CUMULUS1' if socket.gethostname().lower().startswith('cumulus1') else 'CIRRUS'


def model_name(provider, model, where):
    """Plain name plus where it ran."""
    model = str(model or '')
    if provider == 'ollama' or (model and ':' in model and not model.startswith(('claude', 'gpt-'))):
        return '%s (local model on %s)' % (model or 'local model', where)
    if model.startswith('claude'):
        return '%s (Anthropic API)' % model
    if model.startswith('gpt-') or provider == 'openai':
        return '%s (OpenAI API)' % (model or 'OpenAI model')
    return model or str(provider or 'unknown model')


def _ro(path):
    db = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def _workflow(turn, where):
    """The local answer workflow's stages, from the conversation memory."""
    steps, meta = [], turn['meta']
    stages = meta.get('stages') or {}
    if not stages and 'awaiting' in str(meta.get('reason', '')).lower():
        steps.append('The answer workflow stopped before any model ran: %s' % meta['reason'])
        return steps
    for name in ('local', 'local_repair', 'foundation', 'cloud', 'cloud_correction'):
        stage = stages.get(name)
        if not isinstance(stage, dict):
            continue
        label = {'local': 'Local answer workflow, first draft', 'local_repair': 'Local answer workflow, repair',
                 'foundation': 'Foundation-model correction', 'cloud': 'Cloud correction',
                 'cloud_correction': 'Cloud correction'}[name]
        if stage.get('error_type'):
            detail = stage.get('error_detail') if stage.get('error_detail') in FIXED_REFUSALS else stage['error_type']
            steps.append('%s: refused before the model ran (%s).' % (label, detail))
        else:
            steps.append('%s: %s.' % (label, model_name(stage.get('provider'), stage.get('model'), where)))
    if stages and meta.get('reason'):
        steps.append('Workflow result: %s' % meta['reason'])
    return steps


def trace(root, row, memory_db, ledger_db, where=None):
    where = where or host()
    evidence = json.loads(row['evidence'] or '{}')
    payload = json.loads(row['payload'])
    out = {'id': row['id'][:16], 'host': where, 'client': evidence.get('client', 'unknown'),
           'subject': (payload[2] if len(payload) > 2 else '').strip(),
           'arrived': datetime.fromtimestamp(row['created']).strftime('%a %Y-%m-%d %H:%M'),
           'state': row['state'], 'owner': row['owner'], 'next_action': row['next_action'], 'steps': []}
    steps = out['steps']
    steps.append('Arrived on %s; intake logged it.' % where)
    turn = None
    if evidence.get('answer_request_id') and memory_db is not None:
        r = memory_db.execute('SELECT state, meta FROM turns WHERE id=?', (evidence['answer_request_id'],)).fetchone()
        if r:
            turn = {'state': r['state'], 'meta': json.loads(r['meta'])}
            steps.extend(_workflow(turn, where))
    attempt = None
    if ledger_db is not None:
        r = ledger_db.execute('SELECT outcome, cost, detail FROM attempts WHERE id=?', (row['id'],)).fetchone()
        if r:
            attempt = {'outcome': r['outcome'], 'cost': r['cost'], 'detail': json.loads(r['detail'] or '{}')}
    if attempt:
        d = attempt['detail']
        if d.get('hold_label'):
            steps.append('Held for help: %s.' % d['hold_label'])
        models = d.get('models') or {}
        used = ', '.join('%s $%.2f' % (model_name('', m, where), c) for m, c in sorted(models.items())) or \
            model_name('', d.get('model'), where) + ' (per-model use not recorded for this run)'
        checks = [e for e in d.get('log', []) if e.get('stage') == 'check']
        passed_on = next((e['attempt'] + 1 for e in checks if e.get('passed')), None)
        steps.append('Research agent (Claude Code on %s, web search and fetch only): %s.' % (where, used))
        if checks:
            steps.append('Independent source check: %s.' % (
                'passed on try %d' % passed_on if passed_on else 'did not pass (%d tries)' % len(checks)))
        steps.append('Research agent result: %s, $%.2f in all.' % (attempt['outcome'].replace('_', ' '), attempt['cost'] or 0))
        out['answered_by'] = 'research agent' if attempt['outcome'].startswith('sent') else None
        out['models'] = models
    if turn and turn['meta'].get('manual_answer'):
        steps.append('Answered by a Cowork session: %s.' % turn['meta']['manual_answer'])
        out['answered_by'] = 'Cowork session'
    if (not out.get('answered_by') and turn and turn['state'] == 'delivered'
            and not turn['meta'].get('research_agent')):
        local = [s for s in (turn['meta'].get('stages') or {}).values() if isinstance(s, dict) and s.get('model')]
        out['answered_by'] = 'answer workflow (%s)' % model_name(local[-1].get('provider'), local[-1].get('model'), where) \
            if local else 'answer workflow'
    if not out.get('answered_by') and row['owner'] in WORKERS:
        out['answered_by'] = WORKERS[row['owner']]
        steps.append('Handled by %s.' % WORKERS[row['owner']])
    steps.append({'sent_verified': 'Delivered: sent and proven in Sent Mail.',
                  'sent_unverified': 'Sent, but the Sent Mail proof is still missing.',
                  'noted': 'No reply was needed (%s).' % row['next_action'],
                  'waiting_on_client': 'Waiting on the client (%s).' % row['next_action'],
                  'working': 'Still being worked on (%s).' % row['next_action']}.get(
        row['state'], 'Not answered yet: %s (%s).' % (row['state'], row['next_action'])))
    return out


def collect(root=ROOT, days=7, now=None):
    root = Path(root)
    path = root / 'data/intake-lifecycle/requests.sqlite3'
    if not path.exists():
        return []
    now = datetime.now().timestamp() if now is None else now
    mem = root / 'data/conversations/memory.sqlite3'
    led = root / 'data/client-research-agent/ledger.sqlite3'
    with closing(_ro(path)) as lc:
        rows = [dict(r) for r in lc.execute('SELECT * FROM requests WHERE created>=? ORDER BY created',
                                            (now - days * 86400,))]
    memory_db = _ro(mem) if mem.exists() else None
    ledger_db = _ro(led) if led.exists() else None
    try:
        return [trace(root, r, memory_db, ledger_db) for r in rows
                if json.loads(r['evidence'] or '{}').get('client') not in (None, 'buddy')]
    finally:
        for db in (memory_db, ledger_db):
            if db is not None:
                db.close()


def text(t):
    lines = ['%s, "%s" (arrived %s on %s)' % (t['client'], t['subject'], t['arrived'], t['host']),
             '  Answered by: %s' % (t.get('answered_by') or 'not answered yet')]
    lines += ['  - ' + s for s in t['steps']]
    return '\n'.join(lines)


def update(root=ROOT, days=7):
    folder = Path(root) / OUT
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    traces = collect(root, days)
    for t in traces:
        path = folder / (t['id'] + '.json')
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(t, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    return len(traces)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('command', choices=('report', 'update'))
    p.add_argument('--days', type=int, default=7)
    p.add_argument('--json', action='store_true')
    a = p.parse_args()
    if a.command == 'update':
        print(json.dumps({'traces': update(ROOT, a.days)}))
        return 0
    traces = collect(ROOT, a.days)
    if a.json:
        print(json.dumps(traces, indent=1))
    else:
        print('\n\n'.join(text(t) for t in traces) or 'No client email in the last %d days on %s.' % (a.days, host()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
