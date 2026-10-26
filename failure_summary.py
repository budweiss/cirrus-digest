#!/usr/bin/env python3
"""Non-private failure summaries for private jobs (S497 P5).

Buddy, 2026-10-26. Stock and OpinionAI content is protected local work (CLAUDE.md):
it stays on the owned server and never enters a cloud chat. That left no way to
triage WHY a private review step rejected something, so stock discovery's
10-25 failure could not be diagnosed. This turns the private reviewer findings,
ON THE SERVER, into a summary that holds only:

  * fixed category LABELS chosen from this file (never text from the finding);
  * counts;
  * error codes that match a strict identifier pattern (e.g. source_decision_reason).

Classification is deterministic keyword rules, so no model and nothing leaves the
box. Anything unmatched is counted as 'other'; the detail stays in the private
evidence beside the summary. `sanitize()` re-applies the allow-list when a summary
is read back, so even a tampered or hand-edited file cannot leak free text.

usage: failure_summary.py --show [N]     (prints the newest N sanitized summaries)
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# (label, pattern). Order matters: the first match wins.
RULES = (
    ('claims to have read paywalled content', r'paywall'),
    ('invented or unsupported date', r'\bdate|\bdated\b|undated|transaction time'),
    ('index page or snippet treated as full content', r'\bindex\b|snippet|excerpt|teaser|full content|landing page'),
    ('promotional or subscription source', r'promot|subscri|newsletter|signup|sign-up'),
    ('delayed disclosure treated as a live trade', r'delay|disclosure|13f|live trade|live signal'),
    ('tool claim without installable evidence', r'install|\btrial\b|package|repositor|\bsdk\b|library'),
    ('useful technique rated SKIP', r'\bskip\b|technique|practice'),
    ('claim beyond the supplied metadata', r'pric|capabilit|compatib|invent|not in the metadata|unsupported claim'),
    ('relevance not supported', r'relevan|research need|unrelated|does not answer'),
    ('missing or inexact evidence quote', r'quote|verbatim|exact evidence|evidence'),
    ('opinion, prediction or allegation stated as fact', r'as fact|verified fact|prediction|alleg|opinion'),
)
LABELS = {label for label, _ in RULES} | {'other'}
CODE = re.compile(r'[a-z][a-z0-9_]{2,59}')
KEYS = ('job', 'code', 'at', 'attempts', 'review_categories', 'validation_errors')


def category(text):
    low = str(text).lower()
    return next((label for label, pattern in RULES if re.search(pattern, low)), 'other')


def error_code(text):
    """An identifier-shaped error code, or a fixed label -- never free text."""
    tail = str(text).split(':')[-1].strip()
    return tail if CODE.fullmatch(tail) else 'unparseable_or_invalid_model_output'


def build(job, code, issues=(), errors=(), attempts=1):
    cats, errs = {}, {}
    for issue in issues or ():
        label = category(issue)
        cats[label] = cats.get(label, 0) + 1
    for err in errors or ():
        e = error_code(err)
        errs[e] = errs.get(e, 0) + 1
    return sanitize({'job': job, 'code': code, 'at': datetime.now().isoformat(timespec='seconds'),
                     'attempts': attempts, 'review_categories': cats, 'validation_errors': errs})


def sanitize(data):
    """Allow-list a summary: fixed labels, identifier codes, ints. Drops everything else."""
    if not isinstance(data, dict):
        return None
    out = {}
    for k in ('job', 'code'):
        v = data.get(k)
        out[k] = v if isinstance(v, str) and CODE.fullmatch(v) else 'unknown'
    at = data.get('at')
    out['at'] = at if isinstance(at, str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d', at) else None
    n = data.get('attempts')
    out['attempts'] = n if type(n) is int and 0 <= n <= 20 else None
    cats = data.get('review_categories') if isinstance(data.get('review_categories'), dict) else {}
    out['review_categories'] = {k: v for k, v in cats.items() if k in LABELS and type(v) is int and 0 < v < 1000}
    errs = data.get('validation_errors') if isinstance(data.get('validation_errors'), dict) else {}
    out['validation_errors'] = {k: v for k, v in errs.items()
                                if isinstance(k, str) and CODE.fullmatch(k) and type(v) is int and 0 < v < 1000}
    return out


def write(path, summary):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(sanitize(summary), indent=1) + '\n')
    tmp.replace(path)


def newest(root=None, n=6):
    root = ROOT if root is None else root
    paths = list((root / 'private/stock-paper/scheduled').glob('*/failure-summary.json')) + \
            list((root / 'private/opinionai').glob('*failure-summary.json'))
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in paths[:n]:
        try:
            out.append(dict(sanitize(json.loads(p.read_text())) or {}, run=p.parent.name if p.parent.name != 'opinionai'
                            else p.name.replace('-failure-summary.json', '')))
        except (OSError, ValueError):
            out.append({'run': 'unreadable summary'})
    return out


def main(argv):
    if argv[:1] != ['--show'] or len(argv) > 2 or (len(argv) == 2 and not argv[1].isdigit()):
        print(__doc__.strip().splitlines()[-1])
        return 2
    for row in newest(n=int(argv[1]) if len(argv) == 2 else 6):
        # 'run' is a dated folder or file stem (e.g. 2026-10-25-discovery), never content.
        if not re.fullmatch(r'[0-9A-Za-z_.-]{1,60}|unreadable summary', str(row.get('run', ''))):
            row['run'] = 'unknown'
        print(json.dumps(row))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
