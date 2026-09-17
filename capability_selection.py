"""Capability selection for opt-in dispatch. No network or credential access.

Candidates must identify an exact model and record validated capabilities.
Synthetic test records are not authorizations to route real workloads.
"""
import math
import time


class NoEligibleModel(ValueError):
    pass


def select(candidates, *, capability, privacy, input_tokens, max_cost_usd, allowed_ids,
           pool="local", min_quality=0.0, now=None, health_max_age=300):
    if privacy not in ('LOCAL_ONLY', 'CLOUD_ALLOWED'):
        raise NoEligibleModel('unknown privacy policy')
    if not isinstance(capability, str) or not capability:
        raise NoEligibleModel('missing capability')
    if type(input_tokens) is not int or input_tokens < 0:
        raise NoEligibleModel('invalid token requirement')
    if type(max_cost_usd) not in (int, float) or not math.isfinite(max_cost_usd) or max_cost_usd < 0:
        raise NoEligibleModel('invalid cost limit')
    now = time.time() if now is None else now
    if pool not in ('local', 'cloud') or (privacy == 'LOCAL_ONLY' and pool == 'cloud'):
        raise NoEligibleModel('pool not permitted')
    for value in (now, health_max_age, min_quality):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise NoEligibleModel('invalid freshness or quality limit')
    if now < 0 or health_max_age <= 0 or not 0 <= min_quality <= 1:
        raise NoEligibleModel('invalid freshness or quality limit')
    eligible = []
    for candidate in candidates:
        if candidate.get('id') not in allowed_ids or candidate.get('enabled') is not True:
            continue
        if candidate.get('healthy') is not True or not candidate.get('model'):
            continue
        if candidate.get('location') != pool:
            continue
        if privacy == 'LOCAL_ONLY' and candidate['location'] != 'local':
            continue
        checked = candidate.get('health_checked_at')
        if type(checked) not in (int, float) or not math.isfinite(checked) or not 0 <= now - checked <= health_max_age:
            continue
        evidence = candidate.get('capabilities', {}).get(capability, {})
        if evidence.get('validated') is not True or evidence.get('model') != candidate['model']:
            continue
        quality = evidence.get('quality')
        cost = candidate.get('estimated_request_cost_usd')
        capacity = candidate.get('usable_input_tokens')
        if (type(quality) not in (int, float) or not math.isfinite(quality) or not min_quality <= quality <= 1
            or type(cost) not in (int, float) or not math.isfinite(cost) or not 0 <= cost <= max_cost_usd
            or type(capacity) is not int or capacity < input_tokens):
            continue
        # Quality precedes cost. Input order has no bearing on the choice.
        eligible.append((-quality, cost, candidate['id'], candidate))
    if not eligible:
        raise NoEligibleModel('no validated model meets requirements')
    chosen = min(eligible, key=lambda row: row[:3])[3]
    return {'id': chosen['id'], 'model': chosen['model'], 'capability': capability,
            'privacy': privacy, 'reason': 'validated_capability_within_limits'}


def selftest():
    import copy
    import unittest

    class Tests(unittest.TestCase):
        def setUp(self):
            def candidate(name, location, quality):
                return {'id': name, 'model': name+'-v1', 'enabled': True, 'healthy': True,
                        'health_checked_at':1000, 'location': location, 'usable_input_tokens': 4096, 'estimated_request_cost_usd': .01,
                        'capabilities': {'extraction': {'validated': True, 'model': name+'-v1', 'quality': quality}}}
            self.candidates = [candidate('first', 'cloud', .7), candidate('better', 'cloud', .9),
                               candidate('private', 'local', .8)]
            self.args = dict(capability='extraction', privacy='CLOUD_ALLOWED', input_tokens=100,
                             max_cost_usd=.02, now=1001, pool='cloud', allowed_ids={'first','better','private'})

        def test_capability_score_not_provider_order(self):
            for rows in (self.candidates, list(reversed(self.candidates))):
                self.assertEqual(select(rows, **self.args)['id'], 'better')

        def test_privacy_and_allowlist(self):
            self.assertEqual(select(self.candidates, **dict(self.args, privacy='LOCAL_ONLY', pool='local'))['id'], 'private')
            self.assertEqual(select(self.candidates, **dict(self.args, allowed_ids={'first'}))['id'], 'first')

        def test_missing_capability_or_exceeded_limits_block(self):
            for override in ({'capability':'coding'}, {'max_cost_usd':0}, {'input_tokens':10000}, {'privacy':'typo'}):
                with self.assertRaises(NoEligibleModel):
                    select(self.candidates, **dict(self.args, **override))

        def test_stale_future_or_missing_health_blocks(self):
            for checked in (None, 600, 1100, float('nan')):
                rows=copy.deepcopy(self.candidates)
                for row in rows: row['health_checked_at']=checked
                with self.assertRaises(NoEligibleModel):select(rows, **self.args)

        def test_pool_is_explicit_and_quality_threshold_enforced(self):
            self.assertEqual(select(self.candidates, **dict(self.args, pool='local'))['id'], 'private')
            with self.assertRaises(NoEligibleModel):
                select(self.candidates, **dict(self.args, privacy='LOCAL_ONLY', pool='cloud'))
            with self.assertRaises(NoEligibleModel):
                select(self.candidates, **dict(self.args, min_quality=.95))

        def test_model_change_invalidates_evaluation(self):
            rows=copy.deepcopy(self.candidates)
            for row in rows: row['model'] += '-changed'
            with self.assertRaises(NoEligibleModel): select(rows, **self.args)

        def test_health_and_evidence_required(self):
            for field in ('healthy','enabled'):
                rows=copy.deepcopy(self.candidates)
                for row in rows:row[field]=False
                with self.assertRaises(NoEligibleModel):select(rows, **self.args)
            rows=copy.deepcopy(self.candidates)
            for row in rows:row['capabilities']['extraction']['validated']=False
            with self.assertRaises(NoEligibleModel):select(rows, **self.args)

    return unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests)).wasSuccessful()


if __name__=='__main__':
    raise SystemExit(0 if selftest() else 1)
