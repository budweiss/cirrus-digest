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


def _get_authenticated(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=5) as response:
        raw = response.read(1024*1024 + 1)
    if len(raw) > 1024*1024:
        raise ValueError('oversized metadata')
    return json.loads(raw)


def observe_cloud(creds, provider, *, get=None, clock=None):
    """Authenticated model metadata, no inference and no credentials in URLs.

    This proves metadata access and model identity/capacity, not completion health.
    Exact aliases that resolve to a different identifier must be requalified.
    """
    if provider not in ('anthropic', 'gemini', 'kimi', 'openai', 'grok', 'deepseek'):
        raise ValueError('unsupported cloud observation')
    model = ((creds.get('claude_dev_model') or creds.get('claude_model'))
             if provider == 'anthropic' else creds.get(provider + '_model'))
    row = dict(id=provider, model=model, location='cloud', healthy=False,
               basis='authenticated_model_metadata_only')
    key = creds.get(provider + '_api_key')
    if not key or not isinstance(model, str) or not model:
        return dict(row, status='not_configured')
    encoded = urllib.parse.quote(model, safe='')
    if provider == 'anthropic':
        url = 'https://api.anthropic.com/v1/models/' + encoded
        headers = {'x-api-key':key, 'anthropic-version':'2023-06-01'}
    elif provider == 'gemini':
        url = 'https://generativelanguage.googleapis.com/v1beta/models/' + encoded
        headers = {'x-goog-api-key':key}
    else:
        base = {'kimi':'https://api.moonshot.ai', 'openai':'https://api.openai.com',
                'grok':'https://api.x.ai', 'deepseek':'https://api.deepseek.com'}[provider]
        url = base + '/v1/models'
        headers = {'Authorization':'Bearer ' + key}
    try:
        data = (get or _get_authenticated)(url, headers)
        if provider not in ('anthropic', 'gemini'):
            matches = [r for r in data.get('data', []) if r.get('id') == model]
            if len(matches) != 1:
                return dict(row, status='model_identity_mismatch')
            data = matches[0]
        identity = data.get('id') if provider != 'gemini' else data.get('name', '').removeprefix('models/')
        capacity = data.get('max_input_tokens') if provider == 'anthropic' else data.get('inputTokenLimit')
        if provider not in ('anthropic', 'gemini'):
            capacity = data.get('context_length') or data.get('max_model_len')
        if identity != model:
            return dict(row, status='model_identity_mismatch')
        if type(capacity) is not int or capacity <= 0:
            return dict(row, status='capacity_unknown')
        return dict(row, healthy=True, status='ready_metadata',
                    checked_at=(clock or time.time)(), usable_input_tokens=capacity)
    except Exception as exc:
        return dict(row, status='observation_failed', error_type=type(exc).__name__)
