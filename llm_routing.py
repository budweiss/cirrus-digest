"""S187 shared routing controls. Metadata only; never stores prompts or keys.

Applies opt-in workload policy to existing provider calls. Direct SDK/HTTP
clients outside llm_providers are not governed by this module.
"""
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
POLICY_PATH = ROOT / 'config/llm_routing.json'
AUDIT_PATH = ROOT / 'logs/llm-routing.jsonl'
LOCAL = {'ollama', 'vllm'}
PRIVACY = {'LOCAL_ONLY', 'CLOUD_ALLOWED'}


class RoutingError(ValueError):
    pass


def policy(task, creds, privacy=None):
    # Caller may tighten but cannot weaken a credential-level privacy rule.
    inherited = creds.get('llm_privacy', 'CLOUD_ALLOWED')
    requested = inherited if privacy is None else privacy
    if inherited not in PRIVACY or requested not in PRIVACY:
        raise RoutingError('unknown privacy policy')
    effective = 'LOCAL_ONLY' if 'LOCAL_ONLY' in (inherited, requested) else 'CLOUD_ALLOWED'
    try:
        data = json.loads(POLICY_PATH.read_text())
        if data.get('version') != 1 or not isinstance(data.get('tasks'), dict) or not isinstance(data.get('profiles'), dict):
            raise ValueError('invalid policy schema')
        name = data['tasks'].get(task)
        profile = dict(data['profiles'][name]) if name else {}
        if name:
            order = profile['cloud_order']
            if not isinstance(order, list) or not order or any(p not in ('anthropic','gemini','grok','openai','deepseek','kimi') for p in order):
                raise ValueError('invalid cloud providers')
            if not isinstance(profile['max_cloud_providers'], int) or not 1 <= profile['max_cloud_providers'] <= 2:
                raise ValueError('invalid cloud limit')
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RoutingError('routing policy unavailable or invalid') from exc
    return {**profile, 'profile': name, 'privacy': effective}


def cloud_order(task, creds, order=None, privacy=None):
    p = policy(task, creds, privacy)
    if p['privacy'] == 'LOCAL_ONLY':
        raise RoutingError('cloud blocked by LOCAL_ONLY policy')
    chosen = list(order if order is not None else p.get('cloud_order', []))
    if p.get('profile'):
        # A requested order may narrow/reorder the approved pool, never widen it.
        chosen = [x for x in chosen if x in p['cloud_order']]
    return chosen, p


def audit(task, provider, event, profile, **extra):
    # Fixed vocabulary and sanitized identifiers only. No exception text/URLs.
    safe = lambda x: re.sub(r'[^a-zA-Z0-9_.:-]', '_', str(x))[:100]
    row = {'ts': datetime.now(timezone.utc).isoformat(), 'task': safe(task),
           'provider': safe(provider), 'event': event, 'profile': profile.get('profile'),
           'privacy': profile['privacy'], **extra}
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open('a') as f:
        f.write(json.dumps(row, sort_keys=True)+'\n')


def authorize(provider, task, creds, system, user, max_tokens, privacy=None, session_id=None):
    p = policy(task, creds, privacy)
    if provider not in LOCAL and p['privacy'] == 'LOCAL_ONLY':
        raise RoutingError('cloud blocked by LOCAL_ONLY policy')
    if not p.get('profile') or provider in LOCAL:
        return p
    if provider not in p['cloud_order']:
        raise RoutingError('provider not approved for workload')
    import llm_budget
    cfg, box, ledger = llm_budget.resolve(creds, app_dir=str(ROOT))
    model = ((creds.get('claude_dev_model') or creds.get('claude_model') or 'claude-sonnet-5')
             if provider == 'anthropic' else creds.get(provider+'_model'))
    try:
        if not cfg or not ledger or not model:
            raise ValueError('missing budget/model')
        # UTF-8 bytes are a conservative text-token estimate; reserve input
        # overhead and maximum output. This is a preflight, not invoice pricing.
        n = len((system or '').encode()) + len((user or '').encode()) + 1024
        est = llm_budget.cost_usd(model, n, max_tokens, cfg)
        if provider == 'anthropic' and creds.get('prompt_cache', True):
            est *= 1.25  # conservative allowance for cache-write overhead
        if not math.isfinite(est) or est < 0:
            raise ValueError('invalid cost')
        ok, _ = llm_budget.allow(session_id or task, est, cfg, box=box, ledger_path=ledger)
        if not ok:
            raise ValueError('budget denied')
        # Require readable accounting and a writable destination before paid work.
        target = Path(ledger); target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('a'):
            pass
        audit(task, provider, 'authorized', p, estimated_max_cost_usd=est)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RoutingError('paid call blocked: budget or accounting unavailable') from exc
    return p


def selftest():
    import unittest
    suite = unittest.defaultTestLoader.loadTestsFromName('test_llm_routing')
    return unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful()


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in ('selftest', '--selftest'):
        raise SystemExit(0 if selftest() else 1)
    raise SystemExit('Use --selftest; runtime policy is config/llm_routing.json')
