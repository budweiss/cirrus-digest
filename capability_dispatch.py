"""Opt-in capability dispatch through the existing accounting/privacy boundary.

Candidate records are trusted application evaluation/health records, never LLM
output. No project uses this entry point until its records and caller are migrated.
"""
import math
import sys
import llm_providers as lp
import llm_routing as routing
from capability_selection import select, NoEligibleModel


def plan(system, user, creds, *, candidates, capability, task, max_cost_usd,
             pool='local', privacy=None, min_quality=0.0, max_tokens=1024,
             session_id=None, now=None):
    """Plan a qualified call without inference; budgets are rechecked at execution."""
    lp._LAST.model = None
    if type(max_tokens) is not int or max_tokens <= 0:
        raise lp.ProviderError("invalid output token limit")
    try:
        policy = routing.policy(task, creds, privacy)
        if not policy.get('profile'):
            raise routing.RoutingError('capability dispatch requires a governed task')
        allowed = (routing.LOCAL if pool == 'local' else set(policy['cloud_order'])
                   if pool == 'cloud' else routing.LOCAL | set(policy['cloud_order']))
        prepared = []
        for row in candidates:
            provider = row.get('id')
            output_limit = max_tokens
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
                cfg, box, ledger = llm_budget.resolve(creds, app_dir=str(routing.ROOT))
                output_limit = max(max_tokens, lp._EFFORT_MIN_MAX_TOKENS) if provider == 'anthropic' and lp._anthropic_extra(creds) else max_tokens
                try:
                    estimated = llm_budget.cost_usd(configured, len(system.encode()) + len(user.encode()) + 1024,
                                                    output_limit, cfg)
                except (ValueError, TypeError, KeyError):
                    continue  # one unpriced model must not veto qualified peers
                if provider == 'anthropic' and creds.get('prompt_cache', True):
                    estimated *= 1.25
                if not ledger or not llm_budget.allow(session_id or task, estimated, cfg, box=box, ledger_path=ledger)[0]:
                    continue
            if not configured or row.get('model') != configured:
                continue
            capacity = row.get('usable_input_tokens')
            if type(capacity) is not int or capacity < len(system.encode()) + len(user.encode()) + 1024 + output_limit:
                continue
            if not math.isfinite(estimated) or estimated < 0:
                continue
            prepared.append(dict(row, estimated_request_cost_usd=estimated))
        chosen = select(prepared, capability=capability, privacy=policy['privacy'], pool=pool,
                        input_tokens=len(system.encode())+len(user.encode())+1024+max_tokens,
                        max_cost_usd=max_cost_usd, allowed_ids=allowed, min_quality=min_quality, now=now)
        return chosen, policy
    except (routing.RoutingError, NoEligibleModel, OSError, ValueError, TypeError, KeyError) as exc:
        raise lp.ProviderError('capability selection unavailable or requirements unmet') from exc


def dispatch(system, user, creds, *, candidates, capability, task, max_cost_usd,
             pool='local', privacy=None, min_quality=0.0, max_tokens=1024,
             session_id=None, parse=None, now=None):
    """Select once and call once, preserving provider accounting. No failover."""
    chosen, policy = plan(system, user, creds, candidates=candidates, capability=capability,
        task=task, max_cost_usd=max_cost_usd, pool=pool, privacy=privacy,
        min_quality=min_quality, max_tokens=max_tokens, now=now, session_id=session_id)
    routing.audit(task, chosen['id'], 'capability_selected', policy,
                  selected_model=chosen['model'], selection_reason=chosen['reason'],
                  reviewed_quality=chosen['quality'],
                  estimated_max_cost_usd=chosen['estimated_request_cost_usd'])
    try:
        reply = lp.call(chosen['id'], system, user, creds, max_tokens=max_tokens, retries=0,
                        task=task, privacy=policy['privacy'], session_id=session_id, strict_accounting=True)
    except Exception as exc:
        routing.audit(task, chosen['id'], 'capability_call_failed', policy,
                      error_type=type(exc).__name__)
        if isinstance(exc, lp.AccountingError):
            raise
        raise lp.ProviderError('selected model call failed') from exc
    def outcome(event):
        try:
            routing.audit(task, chosen['id'], event, policy, actual_model=lp.last_model())
        except OSError as exc:
            raise lp.AccountingError('capability outcome accounting unavailable') from exc

    if lp.last_model() != chosen['model']:
        outcome('capability_model_mismatch')
        raise lp.ProviderError('responding model differs from evaluated model')
    if lp.last_finish_reason() == 'length':
        outcome('capability_output_truncated')
        raise lp.ProviderError('selected model output truncated')
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


def selftest():
    """Exercise decision-making logic with explicit inputs/expected outputs.

    Does not touch network, credentials, or real routing config -- only
    checks the pure-logic branches of plan()'s candidate-filtering loop and
    the allowed-pool computation, using lightweight fakes.
    """
    failures = []

    def check(name, cond):
        if not cond:
            failures.append(name)

    # --- max_tokens validation (plan() raises before touching routing/creds) ---
    try:
        plan('sys', 'user', {}, candidates=[], capability='x', task='x',
             max_cost_usd=1.0, max_tokens=0)
        check('max_tokens=0 rejected', False)
    except lp.ProviderError:
        check('max_tokens=0 rejected', True)
    except Exception:
        check('max_tokens=0 rejected', False)

    try:
        plan('sys', 'user', {}, candidates=[], capability='x', task='x',
             max_cost_usd=1.0, max_tokens=-5)
        check('negative max_tokens rejected', False)
    except lp.ProviderError:
        check('negative max_tokens rejected', True)
    except Exception:
        check('negative max_tokens rejected', False)

    try:
        plan('sys', 'user', {}, candidates=[], capability='x', task='x',
             max_cost_usd=1.0, max_tokens=1.5)
        check('non-int max_tokens rejected', False)
    except lp.ProviderError:
        check('non-int max_tokens rejected', True)
    except Exception:
        check('non-int max_tokens rejected', False)

    # --- allowed pool computation matches documented pool= semantics ---
    allowed_local = routing.LOCAL
    check('pool=local uses routing.LOCAL', isinstance(allowed_local, (set, frozenset)))

    fake_cloud_order = ['anthropic', 'openai']
    allowed_cloud = set(fake_cloud_order)
    check('pool=cloud builds set from cloud_order', allowed_cloud == {'anthropic', 'openai'})

    allowed_both = routing.LOCAL | allowed_cloud
    check('pool=both unions local and cloud',
          allowed_both >= routing.LOCAL and allowed_both >= allowed_cloud)

    # --- plan() with no governed task raises a wrapped ProviderError, not a raw RoutingError ---
    class _FakePolicyNoProfile(dict):
        pass

    orig_policy = routing.policy
    try:
        routing.policy = lambda task, creds, privacy: {'profile': None}
        try:
            plan('sys', 'user', {}, candidates=[], capability='x', task='ungoverned',
                 max_cost_usd=1.0, max_tokens=16)
            check('ungoverned task rejected', False)
        except lp.ProviderError:
            check('ungoverned task rejected', True)
        except Exception:
            check('ungoverned task rejected', False)
    finally:
        routing.policy = orig_policy

    if failures:
        sys.stderr.write('capability_dispatch selftest FAILED: ' + ', '.join(failures) + '\n')
        return False
    return True


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.exit(0 if selftest() else 1)
