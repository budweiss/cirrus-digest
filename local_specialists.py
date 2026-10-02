"""Explicit, local-only specialist leases. No downloads or cloud fallback.

Installed and resident are separate states. Each request checks both and free
memory; Ollama loads on first inference and releases with keep_alive=0. A
cross-process lock serializes all specialists managed here. Other callers must
use this controller too to share that guarantee.
"""
import fcntl
import json
import socket
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class Unavailable(RuntimeError):
    pass


def request(endpoint, path, body=None, timeout=10):
    req = urllib.request.Request(endpoint + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as response:
        return json.load(response)


def available_gib():
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) / 1024 ** 2
    raise Unavailable('memory_reading_unavailable')


def configuration(root=ROOT):
    cfg = json.loads((Path(root) / 'config/local_specialists.json').read_text())
    if cfg['host'] != socket.gethostname():
        raise Unavailable('specialists_not_enabled_on_this_host')
    if cfg['endpoint'] != 'http://127.0.0.1:11434':
        raise Unavailable('specialists_require_local_ollama')
    return cfg


def status(name, cfg):
    spec = cfg['specialists'].get(name)
    if not spec or not spec.get('enabled'):
        raise Unavailable('specialist_not_enabled')
    if spec.get('remote_endpoint'):
        if spec['remote_endpoint'] != 'http://192.168.100.11:8012':
            raise Unavailable('unapproved_medical_worker')
        state = request(spec['remote_endpoint'], '/health')
        if state.get('host') != 'cumulus2' or state.get('model') != spec['model'] or state.get('protocol') != 1:
            raise Unavailable('invalid_worker_identity')
        return state
    model = spec['model']
    installed = {r['name'] for r in request(cfg['endpoint'], '/api/tags')['models']}
    loaded = {r['name'] for r in request(cfg['endpoint'], '/api/ps')['models']}
    free = available_gib()
    reason = None
    if model not in installed:
        reason = 'model_not_installed'
    elif model in cfg['protected_models']:
        reason = 'protected_model_cannot_be_specialist'
    elif model not in loaded and len(loaded) >= cfg['max_loaded_models']:
        reason = 'resident_slots_full'
    elif free < spec['minimum_available_gib']:
        reason = 'insufficient_memory'
    return {'model': model, 'installed': model in installed, 'loaded': model in loaded,
            'available_gib': round(free, 2), 'ready': reason is None, 'reason': reason}


def generate(name, messages, *, root=ROOT, creds=None, output_schema=None):
    cfg = configuration(root)
    if cfg['specialists'].get(name, {}).get('remote_endpoint'):
        raise Unavailable('use_medical_evidence_route')
    directory = Path(root) / 'logs/local-specialists'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'lease.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Unavailable('specialist_busy')
        state = status(name, cfg)
        if not state['ready']:
            raise Unavailable(state['reason'])
        spec = cfg['specialists'][name]
        model = spec['model']
        protected_before = {r['name'] for r in request(cfg['endpoint'], '/api/ps')['models']} & set(cfg['protected_models'])
        started = time.monotonic()
        outcome = 'failed'
        unloaded = False
        try:
            result = request(cfg['endpoint'], '/api/chat', {
                'model': model, 'messages': messages, 'stream': False,
                'format': output_schema if output_schema is not None else 'json', 'keep_alive': 0,
                'options': {'num_ctx': spec['num_ctx'], 'num_predict': spec['num_predict'], 'temperature': 0}
            }, timeout=spec['timeout_seconds'])
            import llm_budget
            content = result.get('message', {}).get('content', '')
            _peng_ms = result.get('prompt_eval_duration')
            _deng_ms = result.get('eval_duration')
            llm_budget.record_call(creds or {}, 'ollama', model,
                len(json.dumps(messages)), len(content), task='specialist:' + name,
                tier='local', app_dir=str(root), in_tok=result.get('prompt_eval_count'),
                out_tok=result.get('eval_count'),
                num_ctx=spec.get('num_ctx'),
                cached_tok=result.get('prompt_eval_cached_count'),
                prompt_eval_seconds=(_peng_ms / 1e9) if _peng_ms else None,
                eval_seconds=(_deng_ms / 1e9) if _deng_ms else None)
            if result.get('model') != model or not result.get('done') or result.get('done_reason') != 'stop':
                raise Unavailable('incomplete_or_wrong_model_response')
            outcome = 'completed'
            return content
        finally:
            # Also attempt explicit release after timeout/parse failure. The
            # inference request itself has zero retention, even if we disconnect.
            try:
                request(cfg['endpoint'], '/api/generate', {'model': model, 'keep_alive': 0}, timeout=30)
                for _ in range(10):
                    loaded = {r['name'] for r in request(cfg['endpoint'], '/api/ps')['models']}
                    if model not in loaded:
                        unloaded = True
                        break
                    time.sleep(0.5)
            except Exception:
                pass
            with (directory / 'events.jsonl').open('a') as log:
                log.write(json.dumps({'time': time.time(), 'specialty': name, 'model': model,
                    'outcome': outcome, 'unloaded': unloaded,
                    'seconds': round(time.monotonic() - started, 2)}) + '\n')
            if not unloaded:
                raise Unavailable('specialist_unload_not_confirmed')
            if not protected_before.issubset(loaded):
                raise Unavailable('protected_model_no_longer_resident')


def availability(root=ROOT):
    """Inventory for operators/routers, including a currently held lease."""
    cfg = configuration(root)
    states = {name: status(name, cfg) for name in cfg['specialists']
              if cfg['specialists'][name].get('enabled')}
    directory = Path(root) / 'logs/local-specialists'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'lease.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            for state in states.values():
                state.update(ready=False, reason='specialist_busy')
    return states


def selftest():
    """Exercise status() decision branches and configuration() gating offline.

    request() and available_gib() are monkeypatched so no Ollama or network
    is touched; originals are restored in the finally block.
    """
    import tempfile
    real_request, real_available = request, available_gib
    installed = {'alpha:1', 'delta:1', 'guard:1'}
    loaded = {'guard:1'}
    free_gib = [8.0]
    try:
        globals()['request'] = lambda endpoint, path, body=None, timeout=10: (
            {'models': [{'name': n} for n in sorted(installed)]} if path == '/api/tags'
            else {'models': [{'name': n} for n in sorted(loaded)]} if path == '/api/ps'
            else (_ for _ in ()).throw(AssertionError('unexpected path ' + path)))
        globals()['available_gib'] = lambda: free_gib[0]
        cfg = {'host': socket.gethostname(), 'endpoint': 'http://127.0.0.1:11434',
               'protected_models': ['guard:1'], 'max_loaded_models': 1,
               'specialists': {
                   'alpha': {'enabled': True, 'model': 'alpha:1', 'minimum_available_gib': 4},
                   'delta': {'enabled': True, 'model': 'delta:1', 'minimum_available_gib': 4},
                   'guard': {'enabled': True, 'model': 'guard:1', 'minimum_available_gib': 4},
                   'off': {'enabled': False, 'model': 'off:1', 'minimum_available_gib': 0}}}
        # Happy path: installed, not loaded, slot logic reached -> ready.
        loaded.clear(); loaded.add('guard:1'); cfg['max_loaded_models'] = 3
        s = status('alpha', cfg)
        assert s == {'model': 'alpha:1', 'installed': True, 'loaded': False,
                     'available_gib': 8.0, 'ready': True, 'reason': None}, s
        # Not installed -> model_not_installed.
        installed.discard('alpha:1')
        s = status('alpha', cfg)
        assert s['ready'] is False and s['reason'] == 'model_not_installed', s
        installed.add('alpha:1')
        # Protected model can never be a specialist.
        s = status('guard', cfg)
        assert s['ready'] is False and s['reason'] == 'protected_model_cannot_be_specialist', s
        # Resident slots full (delta not loaded, max=1 already held by guard).
        cfg['max_loaded_models'] = 1
        s = status('delta', cfg)
        assert s['ready'] is False and s['reason'] == 'resident_slots_full', s
        # Insufficient memory once a slot is free.
        cfg['max_loaded_models'] = 3
        free_gib[0] = 2.0
        s = status('delta', cfg)
        assert s['ready'] is False and s['reason'] == 'insufficient_memory', s
        assert s['available_gib'] == 2.0, s
        # Disabled specialist raises.
        try:
            status('off', cfg)
            raise AssertionError('disabled specialist did not raise')
        except Unavailable as e:
            assert str(e) == 'specialist_not_enabled', e
        # configuration() from a temp root: matching host passes, other host fails.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'config').mkdir()
            conf = Path(tmp) / 'config/local_specialists.json'
            conf.write_text(json.dumps(cfg))
            assert configuration(tmp)['endpoint'] == cfg['endpoint']
            bad = dict(cfg, host='no-such-host')
            conf.write_text(json.dumps(bad))
            try:
                configuration(tmp)
                raise AssertionError('wrong host did not raise')
            except Unavailable as e:
                assert str(e) == 'specialists_not_enabled_on_this_host', e
            bad = dict(cfg, endpoint='http://example:11434')
            conf.write_text(json.dumps(bad))
            try:
                configuration(tmp)
                raise AssertionError('non-local endpoint did not raise')
            except Unavailable as e:
                assert str(e) == 'specialists_require_local_ollama', e
    finally:
        globals()['request'], globals()['available_gib'] = real_request, real_available
    print('local_specialists selftest OK')


if __name__ == '__main__':
    import sys
    if '--selftest' in sys.argv[1:]:
        try:
            selftest()
        except Exception as e:
            print('local_specialists.py selftest failed: ' + repr(e), file=sys.stderr)
            sys.exit(1)
    else:
        print(json.dumps(availability(), indent=2))
