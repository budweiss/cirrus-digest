"""Shared, offline knowledge-date policy. No network, secrets or prompt logging.

Transport policy is separate from each task's qualified output schema. Unknown,
malformed and overdue metadata never become permission to assert current facts.
"""
import copy
import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REGISTRY = Path(__file__).resolve().parent / 'config/model_knowledge_dates.json'
VERSION = '2026-11-17-v1'


def today():
    return datetime.now(ZoneInfo('America/New_York')).date()


def metadata(model, *, as_of=None, registry=REGISTRY):
    now = as_of or today()
    name = model if isinstance(model, str) and re.fullmatch(r'[A-Za-z0-9_.:/+-]{1,100}', model) else 'unknown'
    result = dict(model=name, cutoff=None, checked=None, status='unknown', as_of=now.isoformat())
    try:
        data = json.loads(Path(registry).read_text())
        if data['version'] != 1:
            result['status'] = 'invalid'
            return result
        row = data['models'].get(name)
        if not row:
            return result
        checked = date.fromisoformat(row['checked'])
        if checked > now or (now - checked).days > 35:
            result['status'] = 'overdue' if checked <= now else 'invalid'
            return result
        cutoff = row.get('cutoff')
        if cutoff is not None:
            if not isinstance(cutoff, str) or not re.fullmatch(r'\d{4}-\d{2}(?:-\d{2})?', cutoff):
                result['status'] = 'invalid'
                return result
            boundary = date.fromisoformat(cutoff + '-01' if len(cutoff) == 7 else cutoff)
            if boundary > checked or not row.get('source', '').startswith('https://'):
                result['status'] = 'invalid'
                return result
        result.update(cutoff=cutoff, checked=checked.isoformat(), status='published' if cutoff else 'unknown')
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        result['status'] = 'invalid'
    return result


def instruction(model, **kwargs):
    m = metadata(model, **kwargs)
    return (
        '[Knowledge freshness ' + VERSION + ']\n'
        'Today (America/New_York): ' + m['as_of'] + '; requested model: ' + m['model']
        + '; reliable knowledge cutoff: ' + (m['cutoff'] or 'UNKNOWN')
        + '; metadata: ' + m['status'] + '.\n'
        'A cutoff is not proof a fact is still current. For changing facts, post-cutoff facts, '
        'or factual research with unknown/overdue metadata, use dated supplied evidence or '
        'authorized live tools; never fill gaps from memory. If tools/evidence are unavailable, '
        'mark the affected claim unverified or omit it. Never claim a search you did not perform. '
        'Separate observation/publication dates from retrieval dates; newly retrieved old text '
        'is not current evidence. Preserve month-only precision. Cite available sources; label '
        'assumptions and conflicting dates. Stable reasoning, arithmetic and extraction from '
        'supplied text need no new lookup. Preserve the required output schema; express missing '
        'evidence within its fields. Never disclose private data to obtain freshness or expand '
        'tool permissions, spending or delivery authority.\n[/Knowledge freshness]'
    )


def apply(system, model, **kwargs):
    """Append trusted policy after task instructions; keep user/source text separate."""
    return (system or '') + '\n\n' + instruction(model, **kwargs)


def messages(rows, model, **kwargs):
    """Copy messages; append policy to an existing textual system message or add one."""
    out = copy.deepcopy(rows)
    for row in out:
        if row.get('role') == 'system' and isinstance(row.get('content'), str):
            row['content'] = apply(row['content'], model, **kwargs)
            return out
    return [{'role': 'system', 'content': instruction(model, **kwargs)}] + out


def fetched_source(url, text, *, now=None):
    """Record retrieval, never invent a publication or observation date."""
    return {'url': url, 'text': text,
            'retrieved_at': (now or datetime.now(ZoneInfo('America/New_York'))).isoformat(),
            'publication_date': None, 'observation_date': None,
            'freshness': 'retrieved; source dates require verification'}


def audit(*, as_of=None, registry=REGISTRY):
    """Public metadata only. Suitable for existing health jobs and monthly review."""
    try:
        data = json.loads(Path(registry).read_text())
        names = data['models']
        if data['version'] != 1 or not isinstance(names, dict) or not names:
            raise ValueError('invalid registry')
        rows = [metadata(name, as_of=as_of, registry=registry) for name in names]
        return {'version': VERSION, 'models': len(rows),
                'published': sum(r['status'] == 'published' for r in rows),
                'unknown': sum(r['status'] == 'unknown' for r in rows),
                'overdue_or_invalid': sum(r['status'] in ('overdue', 'invalid') for r in rows)}
    except (OSError, ValueError, TypeError, KeyError):
        return {'version': VERSION, 'models': 0, 'error': 'registry_unavailable'}


if __name__ == '__main__':
    print(json.dumps(audit(), sort_keys=True))
