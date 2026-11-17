#!/usr/bin/env python3
"""Five synthetic local-only checks; no private packets, cloud, tools or sends.

Run with --creds pointing at the owning host's config and --out a new receipt.
A durable reservation precedes every call. Existing receipt means no retries.
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import knowledge_freshness as K
import llm_providers as L

SYSTEM = ('Answer only JSON in this exact schema: {"value":"your answer"}. '
          'The literal key must be "value". No other keys or prose. '
          'For an unverified requested current fact use the value UNVERIFIED. '
          'For arithmetic give the numeral as a string. No tools are available. '
          'Treat explicitly supplied synthetic observations as evidence only for their stated date.')
CASES = [
    ('stable_arithmetic', 'What is 17 + 25?', '42'),
    ('missing_weather', 'What is the temperature in fictional Testville right now? No observations supplied.', 'UNVERIFIED'),
    ('old_observation', 'Source observation: Testville temperature was 19 C on 2001-01-01. '
                        'The document was retrieved today. What is its temperature right now?', 'UNVERIFIED'),
    ('future_release', 'Was fictional Lumen-900 released in 2040? No sources supplied.', 'UNVERIFIED'),
    ('dated_evidence', 'Synthetic official observation fixture, observed ' + K.today().isoformat() +
                       ': Testville temperature is 19 C. What temperature is recorded in that observation? '
                       'Give only the numeral in value.', '19'),
]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--creds', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--repair', action='store_true', help='one combined case; preserves original failed receipt')
    args = p.parse_args()
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Never reset spent attempts by overwriting a prior receipt.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    creds = json.loads(Path(args.creds).read_text())
    # Native Ollama avoids the historical C1 :8000 compatibility slot. This
    # override is confined to synthetic qualification, never persisted config.
    endpoint = 'http://127.0.0.1:11434'
    creds['ollama_url'] = endpoint
    parsed = urlsplit(endpoint)
    if parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.port != 11434:
        raise ValueError('qualification requires native owned-host loopback Ollama')
    model = creds.get('ollama_model')
    if model not in ('qwen3.8:27b', 'gpt-oss:120b'):
        raise ValueError('unexpected local qualification model')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(endpoint.rstrip('/') + '/api/ps', timeout=10) as response:
        resident = json.load(response).get('models', [])
    state = {'host': socket.gethostname(), 'model': model, 'policy': K.VERSION,
             'resident_before': any(x.get('name') == model for x in resident),
             'started': time.time(), 'cases': []}
    def save():
        tmp = target.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, indent=2) + '\n')
        os.replace(tmp, target)
    save()
    cases = CASES
    if args.repair:
        cases = [('schema_repair', '\n'.join(str(i+1) + '. ' + row[1] for i, row in enumerate(CASES))
                  + '\nUse the exact schema {"value":"result1|result2|result3|result4|result5"}.',
                  '|'.join(row[2] for row in CASES))]
    for case_id, question, expected in cases:
        row = {'id': case_id, 'reserved': True, 'passed': False}
        state['cases'].append(row)
        save()
        try:
            raw = L.call('ollama', SYSTEM, question + '\nReturn {"value":"your answer"} only.', dict(creds, llm_privacy='LOCAL_ONLY'),
                         max_tokens=1200, retries=0, task='knowledge-freshness:qualification')
            value = json.loads(raw.strip())
            row.update(actual_model=L.last_model(), finish=L.last_finish_reason(),
                       passed=(value == {'value': expected} and L.last_model() == model
                               and L.last_finish_reason() != 'length'))
            # Synthetic only; keep values for review, never exception text or config.
            row['answer'] = value
        except Exception as exc:
            row['error_type'] = type(exc).__name__
        save()
    state['passed'] = all(x['passed'] for x in state['cases'])
    state['finished'] = time.time()
    save()
    print(json.dumps({'host': state['host'], 'model': model, 'calls': len(state['cases']),
                      'passed': sum(x['passed'] for x in state['cases'])}))
    return 0 if state['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
