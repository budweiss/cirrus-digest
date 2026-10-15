"""Read aggregate schedule evidence without exposing private reports or findings."""
import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATUSES = {'running', 'preview', 'sent', 'already_sent', 'failed', 'complete', 'holiday', 'passed', 'partial'}
COUNTS = {'recipients', 'holdings', 'verified_quotes', 'sources', 'source_gaps', 'rules_reviewed',
          'unknown_input_fields', 'accepted_companies', 'decisions', 'filled', 'trials', 'fetched',
          'adopted', 'due_reviews', 'reviewed', 'prior_reviews'}
FLAGS = {'export_stale', 'extra_email', 'telegram_confirmed'}


def summary(data):
    if isinstance(data, list):
        checks = [x for x in data if isinstance(x, dict)]
        return {'attempts': len(data), 'passed_checks': sum(x.get('passed') is True for x in checks),
                'failed_checks': sum(x.get('passed') is False for x in checks),
                'error_count': sum('error_type' in x for x in checks),
                'issue_counts': [len(x['issues']) if isinstance(x.get('issues'), list) else 0 for x in checks]}
    if not isinstance(data, dict):
        return {'invalid_shape': True}
    result = {}
    for key in ('status', 'decision_review', 'research_status'):
        if key in data:
            result[key] = data[key] if data[key] in STATUSES else 'other'
    for key in COUNTS:
        if type(data.get(key)) is int and data[key] >= 0:
            result[key] = data[key]
    for key in FLAGS:
        if type(data.get(key)) is bool:
            result[key] = data[key]
    for key in ('body_sha256', 'source_sha256'):
        if re.fullmatch(r'[a-f0-9]{64}', str(data.get(key, ''))):
            result[key] = data[key]
    for key in ('started_at', 'completed_at'):
        try:
            result[key] = datetime.fromisoformat(data[key].replace('Z', '+00:00')).isoformat()
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
    if 'error_type' in data:
        result['has_error'] = True
    if isinstance(data.get('offenders'), list):
        result['offender_count'] = len(data['offenders'])
    return result


def audit(lane, job=None, date=None, root=ROOT):
    if lane not in ('personal', 'stock') or (lane == 'stock' and job not in ('am', 'pm', 'scorecard', 'discovery')):
        raise ValueError('invalid_lane_or_job')
    if date is not None:
        datetime.strptime(date, '%Y-%m-%d')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
            raise ValueError('invalid_date')
    base = root / ('private/personal-holdings/runs' if lane == 'personal' else 'private/stock-paper/scheduled')
    suffix = '' if lane == 'personal' else '-' + job
    folders = [base / (date + suffix)] if date else sorted(
        (p for p in base.glob('*') if p.is_dir() and re.fullmatch(r'\d{4}-\d{2}-\d{2}' + suffix, p.name)),
        key=lambda p: p.name, reverse=True)[:1]
    result = {'lane': lane, 'job': job, 'run_exists': bool(folders and folders[0].is_dir()), 'files': {}}
    if not result['run_exists']:
        return result
    folder = folders[0]
    result['date'] = folder.name[:10]
    source = root / ('personal_schedule.py' if lane == 'personal' else 'stock_schedule.py')
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else None
    result['current_source_sha256'] = source_hash
    for name in ('checks.json', 'decision-checks.json', 'scorecard-checks.json', 'numbers-offenders.json',
                 'preview-result.json', 'result.json'):
        path = folder / name
        if not path.exists():
            continue
        try:
            item = summary(json.loads(path.read_text()))
            item['modified_at'] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
            if 'source_sha256' in item:
                item['source_matches_current'] = item['source_sha256'] == source_hash
            result['files'][name] = item
        except (OSError, ValueError, TypeError):
            result['files'][name] = {'unreadable': True}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lane', required=True, choices=('personal', 'stock'))
    parser.add_argument('--job', choices=('am', 'pm', 'scorecard', 'discovery'))
    parser.add_argument('--date')
    args = parser.parse_args()
    try:
        result = audit(args.lane, args.job, args.date)
    except ValueError:
        print(json.dumps({'error': 'invalid_audit_arguments'}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result['run_exists'] else 3


if __name__ == '__main__':
    raise SystemExit(main())
