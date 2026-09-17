"""Join trusted reviewed evaluations to fresh model health before dispatch.

These are application-owned records, never model output or fetched documents.
This module does not approve an evaluation, probe a model, or refresh timestamps.
"""
import hashlib
import math
import time


def prompt_digest(system):
    return hashlib.sha256(system.encode('utf-8')).hexdigest()


def candidates(evaluations, health, *, task, capability, system, contract_sha256, now=None):
    """Return eligible records; absent or malformed evidence fails closed.

    Evaluation approval expires at most 30 days after evaluation. Health must
    identify the same model and be at most five minutes old. Context capacity
    is the lower of the evaluated and observed usable capacities. The caller
    hashes its versioned parser/schema/scoring contract; any change requires
    new reviewed evidence rather than silently reusing an earlier approval.
    """
    now = time.time() if now is None else now
    def number(value):
        return type(value) in (int, float) and math.isfinite(value)
    if not number(now) or now < 0 or not task or not capability:
        raise ValueError('invalid admission requirements')
    if not isinstance(evaluations, list) or not isinstance(health, list):
        return []
    if (not isinstance(contract_sha256, str) or len(contract_sha256) != 64
        or any(c not in "0123456789abcdef" for c in contract_sha256)):
        raise ValueError("task contract must be a SHA-256 digest")
    digest = prompt_digest(system)
    admitted = []
    seen = set()
    # Duplicate identities are ambiguous: admit neither record.
    keys = [(r.get('id'), r.get('model')) for r in evaluations if isinstance(r, dict)]
    health_keys = [(r.get('id'), r.get('model')) for r in health if isinstance(r, dict)]
    for row in evaluations:
        if not isinstance(row, dict):
            continue
        provider, model = row.get('id'), row.get('model')
        key = (provider, model)
        if not isinstance(provider, str) or not isinstance(model, str) or not provider or not model:
            continue
        if keys.count(key) != 1 or health_keys.count(key) != 1 or key in seen:
            continue
        if (row.get('approved') is not True or row.get('task') != task
            or row.get('capability') != capability or row.get('prompt_sha256') != digest
            or row.get('contract_sha256') != contract_sha256
            or not isinstance(row.get('evidence_id'), str) or not row['evidence_id'].strip()):
            continue
        evaluated, expires = row.get('evaluated_at'), row.get('expires_at')
        if not (number(evaluated) and number(expires) and 0 <= evaluated <= now < expires <= evaluated + 30*86400):
            continue
        quality, capacity = row.get('quality'), row.get('usable_input_tokens')
        if not number(quality) or not 0 <= quality <= 1 or type(capacity) is not int or capacity <= 0:
            continue
        state = next(h for h in health if isinstance(h, dict) and (h.get('id'), h.get('model')) == key)
        checked, observed_capacity = state.get('checked_at'), state.get('usable_input_tokens')
        location = row.get('location')
        if (state.get('healthy') is not True or location not in ('local', 'cloud')
            or state.get('location') != location or not number(checked) or not 0 <= now-checked <= 300
            or type(observed_capacity) is not int or observed_capacity <= 0):
            continue
        seen.add(key)
        admitted.append({'id':provider, 'model':model, 'enabled':True, 'healthy':True,
                         'health_checked_at':checked, 'location':location,
                         'usable_input_tokens':min(capacity, observed_capacity),
                         'capabilities':{capability:{'validated':True, 'model':model, 'quality':quality}}})
    return admitted


def dispatch_reviewed(system, user, creds, *, evaluations, health, task, capability, contract_sha256, **kwargs):
    """Use reviewed records with the existing privacy/accounting dispatcher."""
    from capability_dispatch import dispatch
    records = candidates(evaluations, health, task=task, capability=capability,
                         system=system, contract_sha256=contract_sha256, now=kwargs.get('now'))
    return dispatch(system, user, creds, candidates=records, task=task,
                    capability=capability, **kwargs)
