"""Report existing memo failure bills without authorizing or changing a call."""
from __future__ import annotations

import json
from .contracts import WorkOrder, ResultEnvelope
from .store import canonical_json, content_hash


def failure_cost(model, exc, kwargs):
    """Read the surfaced terminal failure and its exact admissions, without DDL."""
    from .cockpit_model import model_failure_trace
    from .planner_paid_output_recovery import _authority_connection, recovery_readers
    trace = model_failure_trace(exc)
    unknown = {'status': 'unproved', 'settled_micros': None,
               'retained_reservation_micros': None, 'work_order_ref': None}
    if trace is None or kwargs.get('purpose') not in {'investment_memo', 'investment_memo_verifier'}:
        return unknown
    unknown['work_order_ref'] = trace['work_order_ref']
    try:
        with _authority_connection(model.scheduler_db) as scheduler, recovery_readers(
                model.config['model_router_db'], model.config['budget_db'], now=model.clock()) as (router, budget):
            wr = scheduler.execute('SELECT * FROM scheduler_work_orders WHERE work_order_id=?',
                                   (trace['work_order_ref'],)).fetchone()
            fr = scheduler.execute('SELECT * FROM scheduler_formal_results WHERE work_order_id=? '
                'ORDER BY attempt_number DESC,created_at DESC,result_record_id DESC LIMIT 1',
                (trace['work_order_ref'],)).fetchone()
            if wr is None or fr is None or budget is None:
                raise ValueError('memo failure lacks formal or budget authority')
            work = WorkOrder.from_dict(json.loads(wr['work_order_json'])).to_dict()
            result = ResultEnvelope.from_dict(json.loads(fr['result_envelope_json'])).to_dict()
            formal = {k: fr[col] for k, col in (
                ('id','result_record_id'), ('work_order_id','work_order_id'),
                ('attempt_number','attempt_number'), ('result_envelope_id','result_envelope_id'),
                ('result_envelope_hash','result_envelope_hash'), ('terminal_state','terminal_state'),
                ('created_at','created_at'))}
            meta, mission = work['metadata'], kwargs['mission']
            expected_request = kwargs['request_id']
            if kwargs.get('_timeout_recovery') is None:
                for name, label in (('capacity_retry', 'capacity-policy'), ('transport_retry', 'transport-policy')):
                    if name in model.config:
                        suffix = ':' + label + ':' + content_hash(model.config[name])[:16]
                        if suffix not in expected_request:
                            expected_request += suffix
            if (canonical_json(work) != wr['work_order_json']
                    or content_hash(work) != wr['work_order_hash']
                    or wr['work_order_hash'] != trace['work_order_hash']
                    or work['id'] != trace['work_order_ref']
                    or work['question'] != kwargs['prompt']
                    or trace['base_request_id'] != expected_request
                    or meta.get('request_id') != trace['work_request_id']
                    or meta.get('purpose') != kwargs['purpose']
                    or meta.get('purpose') != trace['purpose']
                    or meta.get('mission_version_ref') != mission['id']
                    or meta.get('mission_version_hash') != mission['content_hash']
                    or canonical_json(result) != fr['result_envelope_json']
                    or content_hash(result) != fr['result_envelope_hash']
                    or fr['result_envelope_hash'] != trace['formal_result_envelope_hash']
                    or content_hash(formal) != fr['content_hash']
                    or fr['terminal_state'] != 'failed' or result['status'] != 'failed'
                    or result['work_order_ref'] != work['id']):
                raise ValueError('memo failure binding differs')
            route_ref = result['metadata'].get('route_decision_ref')
            route = router.get_decision(route_ref)
            if (route.get('outcome') != 'selected' or route.get('work_order_ref') != work['id']
                    or route.get('work_order_hash') != wr['work_order_hash']
                    or route.get('attempt_number') != fr['attempt_number']):
                raise ValueError('memo failure route differs')
            rows = budget.connection.execute('SELECT attempt_number,phase FROM thesis_impact_day_admissions '
                'WHERE work_order_ref=? ORDER BY attempt_number,phase', (work['id'],)).fetchall()
            if not rows:
                raise ValueError('memo failure has no admitted bill')
            fees, settled, retained, complete, selected = [], 0, 0, True, False
            for row in rows:
                proof = budget.admission(work_order_ref=work['id'], attempt_number=row['attempt_number'], phase=row['phase'])
                admission, settlement = proof['admission'], proof['settlement']
                binding = proof['mission_binding'] or {}
                admitted_route = router.get_decision(admission['route_decision_ref'])
                if (binding.get('mission_version_ref') != mission['id']
                        or binding.get('mission_version_hash') != mission['content_hash']
                        or admitted_route.get('work_order_ref') != work['id']
                        or admitted_route.get('work_order_hash') != wr['work_order_hash']
                        or admitted_route.get('attempt_number') != row['attempt_number']):
                    raise ValueError('memo failure admission differs')
                selected |= row['attempt_number'] == fr['attempt_number']
                fee = {'admission_ref': admission['admission_id'], 'admission_hash': admission['content_hash']}
                if settlement is None:
                    complete = False
                    retained += admission['reserved_micros']
                else:
                    amount = settlement['actual_micros']
                    if type(amount) is not int or amount < 0:
                        raise ValueError('memo failure bill is invalid')
                    settled += amount
                    fee.update(settlement_ref=settlement['settlement_id'], settlement_hash=settlement['content_hash'], settled_micros=amount)
                fees.append(fee)
            if not selected:
                raise ValueError('memo latest failure has no admission')
            uncertain = (result['metadata'].get('dispatch_state') == 'unknown'
                         or (result.get('error') or {}).get('code') == 'TIMEOUT')
            return {'status': 'unsettled' if not complete else 'uncertain' if uncertain else 'settled',
                    'settled_micros': settled, 'retained_reservation_micros': retained,
                    'work_order_ref': work['id'], 'work_order_hash': wr['work_order_hash'],
                    'formal_ref': fr['result_record_id'], 'formal_hash': fr['content_hash'],
                    'error_code': (result.get('error') or {}).get('code'), 'fees': fees}
    except Exception as error:
        return {**unknown, 'reason': type(error).__name__ + ': ' + str(error)}


class MemoFailureCostReporter:
    """Observe exceptions only; delegate the original call and recovery behavior."""
    def __init__(self, model):
        self.model, self.failures = model, {}

    def __getattr__(self, name):
        return getattr(self.model, name)

    def call(self, *, producer_route_decision_refs=None, **kwargs):
        try:
            if producer_route_decision_refs is not None:
                from .cockpit_model import independent_model_call
                return independent_model_call(self.model, producer_route_decision_refs=producer_route_decision_refs, **kwargs)
            return self.model.call(**kwargs)
        except BaseException as exc:
            from .cockpit_model import CockpitModelError
            if isinstance(exc, CockpitModelError):
                proof = failure_cost(self.model, exc, kwargs)
                # Same immutable failed Work can replay on restart or technical
                # resume. Report its bill once, not once per observed exception.
                key = proof['work_order_ref'] or ('unbound', kwargs['purpose'], kwargs['request_id'])
                self.failures[key] = proof
            raise


def with_failure_costs(result, *reporters):
    """Add failed bills to the existing successful/semantic-repair sum once."""
    records = {}
    for reporter in reporters:
        records.update(reporter.failures)
    if not records:
        return result
    costs = list(records.values())
    settled = sum(r['settled_micros'] or 0 for r in costs)
    unknown = (any(r['status'] != 'settled' for r in costs)
               or result.get('cost_status') in {'unproved', 'unsettled', 'partially_known', 'uncertain'})
    known = int(result.get('cost_micros') or 0) + settled
    return {**result, 'cost_micros': known if known or not unknown else None,
            'cost_status': 'partially_known' if unknown else 'settled',
            'settled_failure_cost_micros': settled, 'failure_costs': costs,
            'unknown_failure_cost': unknown,
            'unsettled_failure_reservation_micros': (None if any(r['retained_reservation_micros'] is None for r in costs)
                else sum(r['retained_reservation_micros'] for r in costs)),
            'cost_note': 'Recorded settled bills; uncertain usage or unsettled reservations remain retained.' if unknown
                         else 'Includes the existing settled failed calls; no additional charge or refund.'}
