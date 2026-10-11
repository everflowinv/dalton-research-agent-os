"""Report existing memo failure bills without authorizing or changing a call."""
from __future__ import annotations

import json
from pathlib import Path
from .contracts import WorkOrder, ResultEnvelope
from .store import canonical_json, content_hash


def _budget_identity(path):
    stat = Path(path).stat()
    return [stat.st_dev, stat.st_ino]


def budget_watermark(model):
    """One indexed maximum at run start; never a fee/answer replay authority."""
    from .planner_paid_output_recovery import _authority_connection
    try:
        path = str(Path(model.config['budget_db']).resolve())
        identity = _budget_identity(path)
        with _authority_connection(path) as connection:
            maximum = connection.execute('SELECT MAX(rowid) FROM thesis_impact_day_admissions').fetchone()[0]
        if identity != _budget_identity(path):
            raise ValueError('memo fee ledger changed during watermark read')
        return {'path': path, 'identity': identity, 'max_admission_rowid': maximum or 0}
    except Exception:
        return None


def failure_cost(model, exc, kwargs, *, watermark=None):
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
            rows = budget.connection.execute('SELECT rowid AS admission_rowid,admission_id,attempt_number,phase FROM thesis_impact_day_admissions '
                'WHERE work_order_ref=? ORDER BY attempt_number,phase', (work['id'],)).fetchall()
            if not rows:
                raise ValueError('memo failure has no admitted bill')
            fees, settled, retained, complete, selected = [], 0, 0, True, False
            for row in rows:
                proof = budget.admission(work_order_ref=work['id'], attempt_number=row['attempt_number'], phase=row['phase'])
                admission, settlement = proof['admission'], proof['settlement']
                binding = proof['mission_binding'] or {}
                admitted_route = router.get_decision(admission['route_decision_ref'])
                if (admission['admission_id'] != row['admission_id']
                        or binding.get('mission_version_ref') != mission['id']
                        or binding.get('mission_version_hash') != mission['content_hash']
                        or admitted_route.get('work_order_ref') != work['id']
                        or admitted_route.get('work_order_hash') != wr['work_order_hash']
                        or admitted_route.get('attempt_number') != row['attempt_number']):
                    raise ValueError('memo failure admission differs')
                selected |= row['attempt_number'] == fr['attempt_number']
                fee = {'admission_ref': admission['admission_id'], 'admission_hash': admission['content_hash'],
                       'admission_rowid': row['admission_rowid'], 'retained_reservation_micros': 0}
                if settlement is None:
                    complete = False
                    retained += admission['reserved_micros']
                    fee['retained_reservation_micros'] = admission['reserved_micros']
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
            attributed = (watermark is not None
                          and watermark['path'] == str(Path(model.config['budget_db']).resolve())
                          and watermark['identity'] == _budget_identity(watermark['path']))
            recent = [fee for fee in fees if attributed and fee['admission_rowid'] > watermark['max_admission_rowid']]
            historical = [fee for fee in fees if attributed and fee['admission_rowid'] <= watermark['max_admission_rowid']]
            return {'status': 'unsettled' if not complete else 'uncertain' if uncertain else 'settled',
                    'attribution_status': 'run_watermark' if attributed else 'unproved',
                    'new_settled_micros': sum(fee.get('settled_micros', 0) for fee in recent) if attributed else None,
                    'historical_settled_micros': sum(fee.get('settled_micros', 0) for fee in historical) if attributed else None,
                    'new_retained_reservation_micros': sum(fee['retained_reservation_micros'] for fee in recent) if attributed else None,
                    'watermark': watermark if attributed else None,
                    'new_cost_uncertain': (not attributed or any('settled_micros' not in fee for fee in recent)
                                           or bool(recent) and uncertain),
                    'settled_micros': settled, 'retained_reservation_micros': retained,
                    'work_order_ref': work['id'], 'work_order_hash': wr['work_order_hash'],
                    'formal_ref': fr['result_record_id'], 'formal_hash': fr['content_hash'],
                    'error_code': (result.get('error') or {}).get('code'), 'fees': fees}
    except Exception as error:
        return {**unknown, 'reason': type(error).__name__ + ': ' + str(error)}


class MemoFailureCostReporter:
    """Observe exceptions only; delegate the original call and recovery behavior."""
    def __init__(self, model, *, watermarks=None):
        self.model, self.failures = model, {}
        shared = {} if watermarks is None else watermarks
        try:
            key = str(Path(model.config['budget_db']).resolve())
        except Exception:
            key = None
        if key not in shared:
            shared[key] = budget_watermark(model)
        self.watermark = shared[key]

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
                proof = failure_cost(self.model, exc, kwargs, watermark=self.watermark)
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
    attributed = all(r.get('attribution_status') == 'run_watermark' for r in costs)
    new_settled = sum(r['new_settled_micros'] for r in costs) if attributed else None
    history = sum(r['historical_settled_micros'] for r in costs) if attributed else None
    unknown = (not attributed or any(r.get('new_cost_uncertain', True) for r in costs)
               or result.get('cost_status') in {'unproved', 'unsettled', 'partially_known', 'uncertain'})
    historical_unknown = any(r['status'] != 'settled' and (not attributed or r['historical_settled_micros'] or
        any(fee['admission_rowid'] <= r['watermark']['max_admission_rowid'] and
            fee['retained_reservation_micros'] for fee in r.get('fees', []))) for r in costs)
    known = int(result.get('cost_micros') or 0) + (new_settled or 0)
    return {**result, 'cost_micros': known if known or not unknown else None,
            'cost_status': 'partially_known' if unknown else 'settled',
            'settled_failure_cost_micros': settled,
            'new_settled_failure_cost_micros': new_settled,
            'historical_settled_failure_cost_micros': history,
            'failure_costs': costs, 'unknown_failure_cost': unknown or historical_unknown,
            'new_failure_cost_unknown': unknown, 'historical_failure_cost_unknown': bool(historical_unknown),
            'unsettled_failure_reservation_micros': (None if any(r['retained_reservation_micros'] is None for r in costs)
                else sum(r['retained_reservation_micros'] for r in costs)),
            'cost_note': 'This run adds only newly admitted settled failure bills. Historical bills are separate; unknown costs and reservations remain retained.'}
