"""Independent metadata-only request checks on the existing supervisor heartbeat."""
import time

_cached = None
_checked = 0
CACHE_SECONDS = 300


def check(now=None, probes=None):
    global _cached, _checked
    now=time.time() if now is None else now
    if probes is None and _cached is not None and now-_checked<CACHE_SECONDS:
        return _cached
    production=probes is None
    if probes is None:
        from tools import _client_digest, _cirrus_digest
        probes={'cumulus1':_client_digest,'cirrus':_cirrus_digest}
    issues=[]
    for host,probe in probes.items():
        try:
            health=probe().get('intake_health')
            if not health:
                raise ValueError('missing intake health')
            if health.get('stale') or health.get('recent_failures'):
                issues.append(host+':intake-not-completing')
            handoffs=health.get('handoffs')
            if not handoffs or handoffs.get('coverage')=='not_initialized':
                raise ValueError('handoff coverage unavailable')
            issues.extend(host+':request:'+r['id']+':'+r['state'] for r in handoffs['attention'])
            issues.extend(host+':ticket:'+r['id'] for r in health.get('open_request_tickets',[]))
        except Exception:
            issues.append(host+':request-check-unavailable')
    result={'ok':not issues,'issues':sorted(issues),'checked_at':now}
    if production:_cached,_checked=result,now
    return result
