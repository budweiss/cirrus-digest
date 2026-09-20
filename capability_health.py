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


def selftest():
    """Exercise observe()/observe_cloud() decision paths with fake get/clock."""
    failures = []

    def check(label, condition):
        if not condition:
            failures.append(label)

    # not_configured: missing model
    r = observe({'ollama_url': 'http://localhost:11434'}, 'ollama')
    check('ollama not_configured (no model)', r['status'] == 'not_configured' and not r['healthy'])

    # not_configured: missing/invalid url
    r = observe({'ollama_model': 'llama3'}, 'ollama')
    check('ollama not_configured (no url)', r['status'] == 'not_configured')

    # invalid_endpoint: bad scheme
    r = observe({'ollama_model': 'llama3', 'ollama_url': 'ftp://localhost'}, 'ollama')
    check('ollama invalid_endpoint', r['status'] == 'invalid_endpoint')

    # not_resident: model absent from /api/ps
    def get_empty(url):
        return {'models': []}
    r = observe({'ollama_model': 'llama3', 'ollama_url': 'http://localhost:11434'},
                'ollama', get=get_empty)
    check('ollama not_resident', r['status'] == 'not_resident')

    # ambiguous_model: model matches more than once
    def get_dupe(url):
        return {'models': [{'name': 'llama3'}, {'name': 'llama3'}]}
    r = observe({'ollama_model': 'llama3', 'ollama_url': 'http://localhost:11434'},
                'ollama', get=get_dupe)
    check('ollama ambiguous_model', r['status'] == 'ambiguous_model')

    # capacity_unknown: matched row has no usable context_length
    def get_no_capacity(url):
        return {'models': [{'name': 'llama3'}]}
    r = observe({'ollama_model': 'llama3', 'ollama_url': 'http://localhost:11434'},
                'ollama', get=get_no_capacity)
    check('ollama capacity_unknown', r['status'] == 'capacity_unknown')

    # ready_metadata: healthy true, checked_at present, usable_input_tokens set
    def get_ready(url):
        if url.endswith('/api/ps'):
            return {'models': [{'name': 'llama3', 'context_length': 8192}]}
        return {}
    r = observe({'ollama_model': 'llama3', 'ollama_url': 'http://localhost:11434'},
                'ollama', get=get_ready, clock=lambda: 12345)
    check('ollama ready_metadata', r['status'] == 'ready_metadata' and r['healthy'] is True
          and r.get('checked_at') == 12345 and r.get('usable_input_tokens') == 8192)

    # observation_failed: get raises
    def get_raises(url):
        raise RuntimeError('boom')
    r = observe({'ollama_model': 'llama3', 'ollama_url': 'http://localhost:11434'},
                'ollama', get=get_raises)
    check('ollama observation_failed', r['status'] == 'observation_failed'
          and r.get('error_type') == 'RuntimeError')

    # observe() rejects non-local providers
    try:
        observe({}, 'anthropic')
        check('observe rejects cloud provider', False)
    except ValueError:
        check('observe rejects cloud provider', True)

    # observe_cloud: not_configured when api key missing
    r = observe_cloud({'openai_model': 'gpt-4'}, 'openai')
    check('cloud not_configured (no key)', r['status'] == 'not_configured')

    # observe_cloud: not_configured when model missing
    r = observe_cloud({'openai_api_key': 'sk-x'}, 'openai')
    check('cloud not_configured (no model)', r['status'] == 'not_configured')

    # observe_cloud: ready_metadata for a non-anthropic/gemini provider
    def get_openai_ready(url, headers):
        return {'data': [{'id': 'gpt-4', 'context_length': 128000}]}
    r = observe_cloud({'openai_model': 'gpt-4', 'openai_api_key': 'sk-x'}, 'openai',
                       get=get_openai_ready, clock=lambda: 999)
    check('cloud ready_metadata (openai)', r['status'] == 'ready_metadata' and r['healthy'] is True
          and r.get('checked_at') == 999 and r.get('usable_input_tokens') == 128000)

    # observe_cloud: model_identity_mismatch when no matching id in data list
    def get_openai_mismatch(url, headers):
        return {'data': [{'id': 'gpt-3.5', 'context_length': 4096}]}
    r = observe_cloud({'openai_model': 'gpt-4', 'openai_api_key': 'sk-x'}, 'openai',
                       get=get_openai_mismatch)
    check('cloud model_identity_mismatch (openai)', r['status'] == 'model_identity_mismatch')

    # observe_cloud: ready_metadata for anthropic (direct id/capacity fields)
    def get_anthropic_ready(url, headers):
        return {'id': 'claude-3-opus', 'max_input_tokens': 200000}
    r = observe_cloud({'claude_model': 'claude-3-opus', 'anthropic_api_key': 'sk-a'}, 'anthropic',
                       get=get_anthropic_ready, clock=lambda: 111)
    check('cloud ready_metadata (anthropic)', r['status'] == 'ready_metadata' and r['healthy'] is True
          and r.get('checked_at') == 111 and r.get('usable_input_tokens') == 200000)

    # observe_cloud: model_identity_mismatch for anthropic (id doesn't match)
    def get_anthropic_mismatch(url, headers):
        return {'id': 'claude-3-sonnet', 'max_input_tokens': 200000}
    r = observe_cloud({'claude_model': 'claude-3-opus', 'anthropic_api_key': 'sk-a'}, 'anthropic',
                       get=get_anthropic_mismatch)
    check('cloud model_identity_mismatch (anthropic)', r['status'] == 'model_identity_mismatch')

    # observe_cloud: capacity_unknown for gemini (identity matches, capacity missing)
    def get_gemini_no_capacity(url, headers):
        return {'name': 'models/gemini-pro'}
    r = observe_cloud({'gemini_model': 'gemini-pro', 'gemini_api_key': 'g-x'}, 'gemini',
                       get=get_gemini_no_capacity)
    check('cloud capacity_unknown (gemini)', r['status'] == 'capacity_unknown')

    # observe_cloud: observation_failed when get raises
    def get_cloud_raises(url, headers):
        raise ValueError('bad')
    r = observe_cloud({'openai_model': 'gpt-4', 'openai_api_key': 'sk-x'}, 'openai',
                       get=get_cloud_raises)
    check('cloud observation_failed', r['status'] == 'observation_failed'
          and r.get('error_type') == 'ValueError')

    # observe_cloud() rejects unsupported providers
    try:
        observe_cloud({}, 'ollama')
        check('observe_cloud rejects local provider', False)
    except ValueError:
        check('observe_cloud rejects local provider', True)

    if failures:
        print('FAIL: capability_health.selftest ->', '; '.join(failures))
        return False
    print('OK: capability_health.selftest (%d checks)' % 18)
    return True


if __name__ == '__main__':
    import sys
    if '--selftest' in sys.argv:
        sys.exit(0 if selftest() else 1)
