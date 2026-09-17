"""On-demand, read-only local model residency observations. No inference calls.

A healthy record means metadata and endpoint checks passed, not that a future
completion is guaranteed. Nonresident Ollama models are not declared broken.
"""
import json
import time
import urllib.parse
import urllib.request


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        raw = response.read(1024*1024 + 1)
    if len(raw) > 1024*1024:
        raise ValueError('oversized metadata')
    return json.loads(raw) if raw.strip() else {}


def observe(creds, provider, *, get=None, clock=None):
    """Read configured local endpoint, returning only allowlisted metadata.

    Failure records have no checked_at: callers cannot treat an unsuccessful
    observation as fresh health. Errors contain types, never URLs or bodies.
    """
    if provider not in ('ollama', 'vllm'):
        raise ValueError('only local providers may be observed')
    get, clock = get or _get, clock or time.time
    model = creds.get(provider+'_model')
    record = {'id':provider, 'model':model, 'location':'local', 'healthy':False,
              'basis':'runtime_metadata_only'}
    try:
        endpoint = creds.get(provider+'_url')
        if not isinstance(endpoint, str) or not isinstance(model, str) or not model:
            return dict(record, status='not_configured')
        url = urllib.parse.urlsplit(endpoint)
        if url.scheme not in ('http', 'https') or not url.hostname:
            return dict(record, status='invalid_endpoint')
        def address(path):
            return urllib.parse.urlunsplit((url.scheme,url.netloc,path,'',''))
        if provider == 'vllm':
            get(address('/health'))
        data = get(address('/api/ps' if provider == 'ollama' else '/v1/models'))
        rows = data.get('models' if provider == 'ollama' else 'data', [])
        matches = [r for r in rows if isinstance(r, dict) and
                   (r.get('name') or r.get('model') if provider == 'ollama' else r.get('id')) == model]
        if len(matches) != 1:
            return dict(record, status='not_resident' if not matches else 'ambiguous_model')
        row = matches[0]
        capacity = row.get('context_length' if provider == 'ollama' else 'max_model_len')
        if type(capacity) is not int or capacity <= 0:
            return dict(record, status='capacity_unknown')
        return dict(record, healthy=True, status='ready_metadata', checked_at=clock(),
                    usable_input_tokens=capacity)
    except Exception as exc:
        return dict(record, status='observation_failed', error_type=type(exc).__name__)
