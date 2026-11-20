"""Bounded C1/C2 synthetic qualification. No real inputs, quotes, trades or sends."""
import argparse
import hashlib
import json
import os
import signal
import socket
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import stock_research_checks as C
import stock_schedule as S
import personal_schedule as P

ROOT = Path(__file__).resolve().parent
HOME = ROOT / 'private/research-check-verification/s671'
RECEIPT = HOME / 'result.json'
FILES = ('stock_research_checks.py', 'stock_schedule.py', 'personal_schedule.py')
MAX_SECONDS = 1200


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in FILES}


def save(value):
    HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = HOME / 'result.tmp'
    temporary.write_text(json.dumps(value, indent=2)); temporary.chmod(0o600)
    temporary.replace(RECEIPT)


def status():
    if not RECEIPT.exists(): return {'status': 'not_started'}
    value = json.loads(RECEIPT.read_text())
    # Fixed metadata only; synthetic drafts and reviewers remain on C1 too.
    result = {k: value[k] for k in ('status', 'machine', 'models', 'tests', 'paper', 'personal',
            'started_at', 'finished_at', 'error_type', 'sha256') if k in value}
    if (HOME / 'tests-current.json').exists():
        result['current_tests'] = json.loads((HOME / 'tests-current.json').read_text())
    return result


def unit_tests():
    HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    suite = unittest.defaultTestLoader.loadTestsFromNames([
        'test_stock_research_checks', 'test_stock_schedule', 'test_personal_schedule',
        'test_stock_paper', 'test_stock_paper_ledger', 'test_stock_paper_market',
        'test_stock_paper_delivery', 'test_stock_paper_email_format', 'test_personal_delivery_failure'])
    with (HOME / 'tests-current.txt').open('w') as stream:
        result = unittest.TextTestRunner(stream=stream).run(suite)
    value = {'run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
             'sha256': hashes(), 'checked_at': datetime.now(timezone.utc).isoformat()}
    P.atomic(HOME / 'tests-current.json', value)
    if not result.wasSuccessful(): raise ValueError('synthetic_tests_failed')
    return value


def timeout(signum, frame):
    raise TimeoutError('synthetic_verification_deadline')


def run():
    if socket.gethostname() != 'cumulus1': raise ValueError('verification_requires_cumulus1')
    os.umask(0o077)
    HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A detached invocation cannot restart an interrupted/spent qualification.
    with (HOME / 'started').open('x') as stream:
        stream.write(datetime.now(timezone.utc).isoformat())
    value = {'status': 'running', 'machine': 'CUMULUS1',
             'models': ['CUMULUS1:gpt-oss:120b', 'CUMULUS2:qwen3.8-27b-fp8'],
             'started_at': datetime.now(timezone.utc).isoformat(), 'sha256': hashes()}
    save(value); signal.signal(signal.SIGALRM, timeout); signal.alarm(MAX_SECONDS)
    try:
        value['tests'] = unit_tests()
        save(value)
        quote = ('Synthetic company filing: the business makes industrial components. '
                 'No earnings date, analyst consensus, peer financials, market-move series '
                 'or owner buying reason has been supplied. Revenue, operating margin and '
                 'cash generation are useful measures to investigate when filings arrive.')
        evidence = {'companies': [{'ticker': 'EXAMPLE', 'recorded_at': datetime.now(timezone.utc).isoformat(),
            'company': {'ticker': 'EXAMPLE', 'name': 'Synthetic Example', 'role': 'watchlist'},
            'documents': [{'id': 1, 'text': quote, 'url': 'https://example.invalid/synthetic-filing',
                           'published': '2026-11-20'}],
            'draft': {'paragraphs': [{'text': 'Synthetic evidence only.', 'evidence': [{'id': 1, 'quote': quote}]}]}}]}
        prior_checks = {key: 'Synthetic prior review: evidence missing; no conclusion established.' for key in C.HEADINGS}
        prior = {'prior_reviews': [{'recorded_at': '2026-11-19T12:00:00Z', 'checks': prior_checks}],
                 'coverage': 'Synthetic history only; no pre-event expectations or owner buying reasons supplied.'}
        audits = []
        with patch.object(S, 'HOME', HOME / 'paper'):
            request, note = S.choose(evidence, {}, {}, datetime.now(S.NY).date().isoformat(), 'am',
                                    prior=prior, audit=lambda row: (audits.append(row), P.atomic(HOME / 'paper-audit.json', audits)))
        C.validate(request.get('research_checks'))
        if any(row['action'] != 'HOLD' for row in request['decisions']): raise ValueError('synthetic_am_trade')
        P.atomic(HOME / 'paper-draft.json', {'request': request, 'note': note})
        value['paper'] = 'passed'; save(value)
        folder = HOME / 'personal'; folder.mkdir(exist_ok=True, mode=0o700)
        rules = '\n'.join('%d. Require a verified source for synthetic sell condition %d; no condition is supplied in this fixture.' % (i, i) for i in range(1, 8))
        draft = P.assess([], {}, [], [], 4, 'Historical synthetic background, no original buying reason.',
                         rules, folder, prior=prior)
        C.validate(draft.get('research_checks'))
        if any(row['status'] != 'not_established' for row in draft['rules']):
            raise ValueError('synthetic_rule_trigger_without_evidence')
        P.atomic(HOME / 'personal-draft.json', draft)
        value.update(personal='passed', status='passed')
    except Exception as exc:
        value.update(status='failed', error_type=type(exc).__name__)
    finally:
        signal.alarm(0)
        value['finished_at'] = datetime.now(timezone.utc).isoformat(); save(value)
    return status()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--status', action='store_true')
    modes.add_argument('--tests-only', action='store_true')
    args = parser.parse_args()
    print(json.dumps(status() if args.status else unit_tests() if args.tests_only else run()))
