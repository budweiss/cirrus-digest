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


def generate(name, messages, *, root=ROOT, creds=None):
    cfg = configuration(root)
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
                'format': 'json', 'keep_alive': 0,
                'options': {'num_ctx': spec['num_ctx'], 'num_predict': spec['num_predict'], 'temperature': 0}
            }, timeout=spec['timeout_seconds'])
            import llm_budget
            content = result.get('message', {}).get('content', '')
            llm_budget.record_call(creds or {}, 'ollama', model,
                len(json.dumps(messages)), len(content), task='specialist:' + name,
                tier='local', app_dir=str(root), in_tok=result.get('prompt_eval_count'),
                out_tok=result.get('eval_count'))
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


if __name__ == '__main__':
    print(json.dumps(availability(), indent=2))
