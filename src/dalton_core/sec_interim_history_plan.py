"""Explicit owner plan version for foreign history; old plans remain immutable."""
import json
from .store import canonical_json, content_hash

RULE_REF = 'sec-foreign-interim-history-plan:0.1'


def build_history_plan(plan, *, created_at, lookback_days=1095):
    from .mission_source_discovery import validate_discovery_plan, _plan_time
    from .sec_plan_specs import effective_sec_plan
    old = validate_discovery_plan(plan)
    if old['source_ref'] != 'source:sec-edgar' or type(lookback_days) is not int or not 1 <= lookback_days <= 3650:
        raise ValueError('foreign history plan requires an explicit SEC calendar window')
    effective = effective_sec_plan(old)
    wanted = next(s for s in effective['specs'] if s['spec_ref'] == 'foreign-disclosure-6k')
    if lookback_days <= wanted['lookback_days']:
        raise ValueError('foreign history plan must explicitly enlarge the original window')
    body = json.loads(canonical_json({k: v for k, v in old.items() if k != 'content_hash'}))
    body['id'] = old['id'] + ':foreign-interim-history:' + str(lookback_days)
    body['created_at'] = _plan_time(created_at)
    replacement = {**wanted, 'lookback_days': lookback_days}
    for index, spec in enumerate(body['specs']):
        if spec['spec_ref'] == 'foreign-disclosure-6k':
            body['specs'][index] = replacement
            break
    else:
        body['specs'].append(replacement)
    return validate_discovery_plan({**body, 'content_hash': content_hash(body)})
