"""Shared research discipline; separate private histories for each stock project.

No network, model, trading or delivery calls. History is evidence of what a
review said at the time, never independent proof of its financial claims.
"""
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone

VERSION = 's671-v1'
HEADINGS = {
    'earnings': 'Earnings expectations and results',
    'thesis': 'Original investment thesis',
    'peers': 'Business and valuation comparisons',
    'news': 'News and price moves',
    'shared_risks': 'Shared portfolio risks',
}
SCHEMA = {'type': 'object', 'additionalProperties': False,
          'required': list(HEADINGS), 'properties': {
              key: {'type': 'string', 'minLength': 30, 'maxLength': 1000}
              for key in HEADINGS}}
INSTRUCTIONS = '''Return research_checks with all five fields, at most 350 words TOTAL.
Use supplied dated evidence only. Each field must give a supported finding OR
name the missing evidence; never fill gaps with remembered facts. Prior checks
are historical opinions, not primary sources or proof of current holdings.
earnings: Before a verified earnings event, identify its date, dated consensus
if available, management's previous guidance and three to five observable
metrics to watch, including what would strengthen or weaken the thesis. After
results, compare only with expectations demonstrably recorded BEFORE that
event. If that baseline or results are missing, say comparison unavailable;
never reconstruct a pre-event prediction after seeing results. Distinguish
event date, fiscal period, publication date and retrieval date.
thesis: Compare new evidence with the recorded original buying reason: stronger,
weaker, unchanged or not established. For paper holdings, original_executed_buy
contains the earliest executed purchase reason (not an unfilled idea). For real
holdings, use only an explicitly supplied owner buying reason; a prior AI
opinion or historical sell review cannot establish why the owner bought.
peers: Compare actual revenue segments, growth, margins, cash flow, debt,
customers and valuation on matching periods, units and accounting definitions.
Explain why a peer fits; label estimates and missing competitor evidence.
news: Trace an event through demand, costs or profits, distinguishing direct
from indirect effects. Assess what may already be priced in only with dated
before/after price evidence. Compare company, peer and market moves over the
same interval when available. Correlation does not prove causation; say cause
unclear when unsupported. Never infer a price move from a lone price quote.
shared_risks: Identify supported common sector, customer, geography or economic
exposures across this account only. Do not invent correlations or fund holdings.
Concentration alone is not a sell instruction; existing account rules prevail.
Primary filings and company releases remain the factual basis; secondary views
need corroboration. No Briefs-only restriction, new subscriptions or tools.
Do not recommend trades merely to populate these checks.'''
REVIEW = '''Require all five research_checks. Reject invented earnings dates,
consensus, peer facts, price moves, correlations or original buying reasons.
Accept explicit evidence gaps. A prior review is an opinion with its original
timestamp, not a current source. Reject hindsight: an earnings comparison needs
an expectation recorded before the verified event and subsequently reported
results. No baseline means no claimed beat/miss against our own expectations.
Check factual claims against primary evidence and matching periods. No automatic
sell solely for concentration. These checks cannot override existing account
rules, privacy, quote validation or execution restrictions.'''


def validate(value):
    if not isinstance(value, dict) or set(value) != set(HEADINGS):
        raise ValueError('research_checks_required')
    if any(not isinstance(v, str) or not 30 <= len(v.strip()) <= 1000 for v in value.values()):
        raise ValueError('research_check_length')
    if sum(len(v.split()) for v in value.values()) > 350:
        raise ValueError('research_check_word_budget')
    return value


def render(value):
    validate(value)
    return '\n\n## Research checks\n\n' + '\n\n'.join(
        '### ' + label + '\n\n' + value[key] for key, label in HEADINGS.items())


def stamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('research_history_timezone_required')
    return result.astimezone(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def record_hash(row):
    return digest({k: row.get(k) for k in ('version', 'recorded_at', 'checks')})


def remember(home, key, checks, recorded_at):
    """Commit only independently reviewed LIVE-run notes; never rewrite them."""
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:-(?:am|pm))?', key):
        raise ValueError('research_history_key')
    validate(checks); stamp(recorded_at)
    folder = home / 'research-check-history'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / (key + '.json')
    if path.exists():
        old = json.loads(path.read_text())
        if old.get('checks') != checks or old.get('sha256') != record_hash(old):
            raise ValueError('research_history_conflict')
        return path
    record = {'version': VERSION, 'recorded_at': recorded_at, 'checks': checks}
    record['sha256'] = record_hash(record)
    # Install an entire file atomically without ever replacing an earlier note.
    fd, temporary = tempfile.mkstemp(dir=folder, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(record, stream, indent=2)
        os.link(temporary, path)
    finally:
        os.unlink(temporary)
    return path


def history(home, as_of):
    """Bounded prior notes, never future notes, previews, or other projects."""
    clock = stamp(as_of); result = []; excluded = 0
    for path in sorted((home / 'research-check-history').glob('*.json'), reverse=True):
        try:
            row = json.loads(path.read_text())
            validate(row['checks'])
            if row['sha256'] != record_hash(row) or stamp(row['recorded_at']) >= clock:
                excluded += 1; continue
            result.append({'recorded_at': row['recorded_at'], 'checks': row['checks']})
        except (ValueError, KeyError, TypeError, OSError):
            excluded += 1
        if len(result) == 2:
            break
    return {'prior_reviews': result, 'invalid_or_future_excluded': excluded,
            'coverage': 'At most two most recent prior accepted live reviews; older notes remain archived. '
                        'Absent pre-event expectations or original buying reasons must remain gaps.'}


def original_buys(account, tickers):
    """Use executed BUYs only; never import a real portfolio or unfilled intent."""
    decisions = {d.get('decision_id'): d for d in account.get('decisions', [])}
    result = {}
    for trade in sorted(account.get('trades', []), key=lambda t: t.get('executed_at', '')):
        ticker = trade.get('ticker')
        if trade.get('side') != 'BUY' or ticker not in tickers or ticker in result:
            continue
        decision = decisions.get(trade.get('decision_id'))
        if decision:
            result[ticker] = {k: decision.get(k) for k in
                              ('decided_at', 'reason', 'valuation', 'counterargument', 'exit_trigger')}
            result[ticker]['executed_at'] = trade.get('executed_at')
    return result
