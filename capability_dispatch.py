"""Opt-in capability dispatch through the existing accounting/privacy boundary.

Candidate records are trusted application evaluation/health records, never LLM
output. No project uses this entry point until its records and caller are migrated.
"""
import math
import llm_providers as lp
import llm_routing as routing
from capability_selection import select, NoEligibleModel


def dispatch(system, user, creds, *, candidates, capability, task, max_cost_usd,
             pool='local', privacy=None, min_quality=0.0, max_tokens=1024,
             session_id=None, parse=None, now=None):
    """Select once, validate exact configured model, then call once. No failover."""
    lp._LAST.model = None
    if type(max_tokens) is not int or max_tokens <= 0:
        raise lp.ProviderError("invalid output token limit")
    try:
        policy = routing.policy(task, creds, privacy)
        if not policy.get('profile'):
            raise routing.RoutingError('capability dispatch requires a governed task')
        allowed = routing.LOCAL if pool == 'local' else set(policy['cloud_order'])
        prepared = []
        required_output = max_tokens
        for row in candidates:
            provider = row.get('id')
            if provider not in allowed:
                continue
            if provider in routing.LOCAL:
                if not creds.get(provider + '_url'):
                    continue
                configured = creds.get(provider + '_model')
                estimated = 0.0
            else:
                if not creds.get(lp._KEY_FIELD.get(provider, '')):
                    continue
                configured = ((creds.get('claude_dev_model') or creds.get('claude_model') or 'claude-sonnet-5')
                              if provider == 'anthropic' else creds.get(provider + '_model'))
                import llm_budget
                cfg, _, _ = llm_budget.resolve(creds, app_dir=str(routing.ROOT))
                output_limit = max(max_tokens, lp._EFFORT_MIN_MAX_TOKENS) if provider == 'anthropic' and lp._anthropic_extra(creds) else max_tokens
                required_output = max(required_output, output_limit)
                estimated = llm_budget.cost_usd(configured, len(system.encode()) + len(user.encode()) + 1024,
                                                output_limit, cfg)
                if provider == 'anthropic' and creds.get('prompt_cache', True):
                    estimated *= 1.25
            if not configured or row.get('model') != configured:
                continue
            if not math.isfinite(estimated) or estimated < 0:
                continue
            prepared.append(dict(row, estimated_request_cost_usd=estimated))
        chosen = select(prepared, capability=capability, privacy=policy['privacy'], pool=pool,
                        input_tokens=len(system.encode())+len(user.encode())+1024+required_output,
                        max_cost_usd=max_cost_usd, allowed_ids=allowed, min_quality=min_quality, now=now)
        routing.audit(task, chosen['id'], 'capability_selected', policy)
    except (routing.RoutingError, NoEligibleModel, OSError, ValueError, TypeError, KeyError) as exc:
        raise lp.ProviderError('capability selection unavailable or requirements unmet') from exc
    reply = lp.call(chosen['id'], system, user, creds, max_tokens=max_tokens, retries=0,
                    task=task, privacy=policy['privacy'], session_id=session_id)
    def outcome(event):
        try:
            routing.audit(task, chosen['id'], event, policy)
        except OSError as exc:
            raise lp.ProviderError('capability outcome accounting unavailable') from exc

    if lp.last_model() != chosen['model']:
        outcome('capability_model_mismatch')
        raise lp.ProviderError('responding model differs from evaluated model')
    try:
        result = parse(reply) if parse is not None else (reply if reply.strip() else None)
    except Exception as exc:
        outcome('capability_output_rejected')
        raise lp.ProviderError('selected model output failed validation') from exc
    if result is None:
        outcome('capability_output_rejected')
        raise lp.ProviderError('selected model returned unusable output')
    outcome('capability_output_accepted')
    return chosen['id'], result
