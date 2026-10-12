"""Source-bound deep-gate accounting; it never settles/refunds or retries a Work.

Request intents are recorded by the Scheduler owner before dispatch. No budget,
routing epoch or run-directory name is part of the frozen-input cost identity.
"""
from __future__ import annotations

import json
from .contracts import WorkOrder
from .scheduler import Scheduler
from .store import canonical_json, content_hash

CONTRACT_REF = 'deep-insight-gate-run-accounting:0.1'
SCOPES = 'deep_insight_gate_accounting_scopes'
REQUESTS = 'deep_insight_gate_accounting_requests'
MAX_REQUESTS = 18  # 4 normal + 4 shape + 4 numbers + 4 rubric + 2 verifier
MAX_WORKS = 64


def normal_request_id(group, company_ref, prompt):
    return content_hash({'group': group, 'company': company_ref,
                         'prompt_sha': content_hash(prompt)})[:32]


def _schema(cur):
    for table, key in ((SCOPES, 'scope_ref'), (REQUESTS, 'request_key')):
        cur.execute(f'CREATE TABLE IF NOT EXISTS {table} ('
                    f'{key} TEXT PRIMARY KEY, scope_ref_binding TEXT NOT NULL, '
                    'record_json TEXT NOT NULL, content_hash TEXT NOT NULL)')
        cur.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_insert_owner BEFORE INSERT ON {table} '
                    'WHEN dalton_scheduler_authorized() != 1 BEGIN SELECT RAISE(ABORT, '
                    "'deep-gate accounting requires Scheduler owner'); END")
        for action in ('UPDATE', 'DELETE'):
            cur.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()}_immutable '
                        f'BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, '
                        "'deep-gate accounting is append-only'); END")
    cur.execute(f'CREATE INDEX IF NOT EXISTS {REQUESTS}_scope ON {REQUESTS}(scope_ref_binding)')


def _record(cur, table, key, identity, scope, body):
    wire, digest = canonical_json(body), content_hash(body)
    row = cur.execute(f'SELECT record_json,content_hash FROM {table} WHERE {key}=?', (identity,)).fetchone()
    if row is not None:
        if row['record_json'] != wire or row['content_hash'] != digest:
            raise ValueError('deep-gate accounting immutable binding differs')
    else:
        cur.execute(f'INSERT INTO {table} VALUES (?,?,?,?)', (identity, scope, wire, digest))


def _body(row):
    body = json.loads(row['record_json'])
    if canonical_json(body) != row['record_json'] or content_hash(body) != row['content_hash']:
        raise ValueError('deep-gate accounting hash differs')
    return body


def works(connection, entry):
    """Complete exact request-prefix history; never a recent-row truncation."""
    prefix = entry['request_id']
    rows = connection.execute('SELECT rowid AS append_rowid,work_order_id FROM scheduler_work_orders WHERE '
        "json_extract(work_order_json,'$.metadata.request_id')>=? AND "
        "json_extract(work_order_json,'$.metadata.request_id')<? ORDER BY rowid LIMIT ?",
        (prefix, prefix+'\uffff', MAX_WORKS+1)).fetchall()
    if len(rows) > MAX_WORKS:
        raise ValueError('deep-gate exact request history exceeds bounded64 proof')
    reader = Scheduler.__new__(Scheduler); reader.connection = connection
    result = []
    for row in rows:
        authority = reader.work_order_authority(row['work_order_id'])
        work = WorkOrder.from_dict(authority['work_order'])
        if (content_hash(work.question) != entry['prompt_hash']
                or work.metadata.get('purpose') != entry['purpose']):
            raise ValueError('deep-gate request prefix has foreign prompt or purpose')
        result.append((work, authority, reader.formal_result(work.id)))
    return result


def _active_attempt(connection,work,attempt_number,now):
    """Close the currently leased event and latest revision without writes."""
    from datetime import datetime
    event=connection.execute('SELECT * FROM scheduler_attempt_events WHERE work_order_id=? '
        'ORDER BY event_seq DESC LIMIT 1',(work.id,)).fetchone()
    if event is None or event['state']!='leased' or event['attempt_number']!=attempt_number:
        raise ValueError('deep-gate admission does not name its current leased attempt')
    wire={'schema_version':'0.1','id':event['event_id'],'created_at':event['created_at'],
        'work_order_ref':event['work_order_id'],'attempt_number':event['attempt_number'],'state':event['state'],
        'lease_ref':event['lease_revision_id'],'result_envelope_ref':event['result_envelope_id'],
        'result_envelope_hash':event['result_envelope_hash'],'reason':event['reason'],
        'prior_event_ref':event['prior_event_id']}
    if event['wire_version']=='0.2':wire.update(wire_version='0.2',not_before=event['not_before'])
    elif event['wire_version'] is not None:raise ValueError('deep-gate leased event epoch is unsupported')
    if content_hash(wire)!=event['content_hash']:raise ValueError('deep-gate leased event hash differs')
    lease=connection.execute('SELECT * FROM scheduler_leases WHERE lease_id=(SELECT lease_id FROM '
        'scheduler_leases WHERE lease_revision_id=?) ORDER BY lease_version DESC LIMIT 1',
        (event['lease_revision_id'],)).fetchone()
    if lease is None:raise ValueError('deep-gate current lease authority missing')
    wire={'schema_version':'0.1','id':lease['lease_revision_id'],'lease_id':lease['lease_id'],
        'lease_version':lease['lease_version'],'created_at':lease['created_at'],
        'work_order_ref':lease['work_order_id'],'attempt_number':lease['attempt_number'],
        'owner_ref':lease['owner_ref'],'lease_token_hash':lease['lease_token_hash'],
        'issued_at':lease['issued_at'],'renewed_at':lease['renewed_at'],'expires_at':lease['expires_at'],
        'prior_lease_ref':lease['prior_lease_revision_id']}
    if (content_hash(wire)!=lease['content_hash'] or lease['work_order_id']!=work.id
            or lease['attempt_number']!=attempt_number or datetime.fromisoformat(lease['expires_at'])<=now):
        raise ValueError('deep-gate current lease hash, attempt or expiration differs')


def billed_work(connection, router, budget, authority, formal):
    """Keep terminal unknown charges at their original conservative ceiling."""
    from .contracts import ResultEnvelope
    work = WorkOrder.from_dict(authority['work_order'])
    if work.metadata.get('purpose')not in {'deep_insight_gate','deep_insight_gate_verifier'}:
        raise ValueError('deep-gate billing authority has another purpose')
    if formal is None:
        raise ValueError('deep-gate Work is still in flight or has no terminal formal')
    result = ResultEnvelope.from_dict(formal['result_envelope'])
    body = {k: formal[c] for k, c in (
        ('id', 'result_record_id'), ('work_order_id', 'work_order_id'),
        ('attempt_number', 'attempt_number'), ('result_envelope_id', 'result_envelope_id'),
        ('result_envelope_hash', 'result_envelope_hash'), ('terminal_state', 'terminal_state'),
        ('created_at', 'created_at'))}
    if (content_hash(body) != formal['content_hash']
            or content_hash(result.to_dict()) != formal['result_envelope_hash']
            or result.work_order_ref != work.id or result.id != formal['result_envelope_id']
            or result.status != formal['terminal_state']
            or result.status not in {'succeeded', 'failed'}):
        raise ValueError('deep-gate formal binding differs')
    rows = budget.connection.execute('SELECT rowid AS append_rowid,attempt_number,phase FROM thesis_impact_day_admissions '
        'WHERE work_order_ref=? ORDER BY attempt_number,phase', (work.id,)).fetchall()
    fees = []; total = 0; unknown = False; selected = False; mission_hashes = set()
    for row in rows:
        paid = budget.admission(work_order_ref=work.id, attempt_number=row['attempt_number'], phase=row['phase'])
        admission, settlement = paid['admission'], paid['settlement']
        route = router.get_decision(admission['route_decision_ref'])
        binding = paid['mission_binding'] or {}
        if (route['work_order_ref'] != work.id or route['work_order_hash'] != authority['work_order_hash']
                or route['attempt_number'] != row['attempt_number']
                or binding.get('mission_version_ref') != work.metadata.get('mission_version_ref')
                or not isinstance(binding.get('mission_version_hash'),str) or len(binding['mission_version_hash']) != 64
                or (work.metadata.get('mission_version_hash') is not None
                    and binding['mission_version_hash'] != work.metadata['mission_version_hash'])):
            raise ValueError('deep-gate admission route or mission differs')
        mission_hashes.add(binding['mission_version_hash'])
        selected |= row['attempt_number'] == formal['attempt_number']
        if settlement is None:
            unknown = True
        else:
            amount = settlement['actual_micros']
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise ValueError('deep-gate settled fee is invalid')
            total += amount
        fees.append({'admission_ref': admission['admission_id'], 'admission_hash': admission['content_hash'],
                     'reserved_micros': admission['reserved_micros'],
                     'append_rowid':row['append_rowid'],
                     'settled_micros':None if settlement is None else settlement['actual_micros'],
                     'settlement_ref': None if settlement is None else settlement['settlement_id'],
                     'settlement_hash': None if settlement is None else settlement['content_hash']})
    md = result.metadata
    if not rows:
        no_send = (result.status == 'failed' and md.get('control_plane_failure') is True
                   and md.get('dispatch_state', 'not_started') == 'not_started'
                   and result.invocation_ref.startswith('invocation:not-started:'))
        if not no_send:
            raise ValueError('deep-gate terminal answer lacks its fee admission')
    elif not selected:
        no_send = (result.status == 'failed' and md.get('control_plane_failure') is True
                   and md.get('dispatch_state','not_started') == 'not_started'
                   and result.invocation_ref.startswith('invocation:not-started:'))
        if not no_send:
            raise ValueError('deep-gate latest formal has no matching admission attempt')
    if len(mission_hashes)>1:
        raise ValueError('deep-gate same Work admissions bind different mission hashes')
    if rows and not (not selected and no_send and md.get('route_decision_ref') is None):
        served = router.get_decision(md['route_decision_ref'])
        if (served['work_order_ref'] != work.id or served['work_order_hash'] != authority['work_order_hash']
                or served['attempt_number'] != formal['attempt_number']):
            raise ValueError('deep-gate terminal route differs')
    unknown |= md.get('dispatch_state') == 'unknown'
    if unknown:
        # This is a macro gate, never a settlement or refund. Retain all known
        # settled attempts PLUS a ceiling for each still unaccounted attempt.
        unsettled = sum(f['settlement_ref'] is None for f in fees)
        ceiling = int(round(work.budget['max_cost_usd'] * 1_000_000))
        total = total + unsettled * ceiling if unsettled else max(total, ceiling)
    return {'work_ref': work.id, 'work_hash': authority['work_order_hash'],
            'formal_hash': formal['content_hash'], 'fees': fees, 'mission_hashes':sorted(mission_hashes),
            'unknown_fee_retained': unknown, 'accounted_micros': total}


class DeepGateAccounting:
    def __init__(self, model, prompts, *, company_ref):
        from .deep_insight_gate import GROUPS
        if set(prompts) != set(GROUPS):
            raise ValueError('deep-gate scope needs all four frozen group prompts')
        self.model = model
        self.entries = [{'role':'normal', 'group':g, 'purpose':'deep_insight_gate',
                         'request_id':normal_request_id(g, company_ref, prompts[g]),
                         'prompt_hash':content_hash(prompts[g])} for g in GROUPS]
        from .call_budget import resolve_run_budget
        original_run_budget=resolve_run_budget(model.config,'deep_insight_gate',defaults={'max_cost_usd':3.0,'max_units':4})
        body = {'contract_ref':CONTRACT_REF, 'company_ref':company_ref,
                'normal_requests':self.entries}
        self.ref = 'deep-gate-accounting:' + content_hash(body)
        with Scheduler(model.scheduler_db) as scheduler, scheduler._transaction() as cur:
            _schema(cur)
            row = cur.execute(f'SELECT * FROM {SCOPES} WHERE scope_ref=?', (self.ref,)).fetchone()
            if row is None:
                legacy = any(works(scheduler.connection, entry) for entry in self.entries)
                scope = {**body, 'complete':not legacy, 'original_run_budget':original_run_budget,
                         'registered_work_rowid':cur.execute('SELECT coalesce(max(rowid),0) FROM scheduler_work_orders').fetchone()[0]}
                _record(cur, SCOPES, 'scope_ref', self.ref, self.ref, scope)
            else:
                scope = _body(row)
                if {k:scope[k] for k in body} != body:
                    raise ValueError('deep-gate frozen input scope differs')
            for entry in self.entries:
                _record(cur, REQUESTS, 'request_key', self.ref+':'+entry['request_id'], self.ref, entry)
        self.scope = scope
        from .planner_paid_output_recovery import recovery_readers
        with recovery_readers(model.config['model_router_db'],model.config['budget_db'],now=model.clock()) as (_,budget):
            if budget is None:raise ValueError('deep-gate current-run fee authority unavailable')
            self.run_start_admission_rowid=budget.connection.execute(
                'SELECT coalesce(max(rowid),0) FROM thesis_impact_day_admissions').fetchone()[0]
        self.last = None

    def register(self, *, role, group, kwargs):
        if role not in {'shape','numbers','rubric','verifier'}:
            raise ValueError('deep-gate unknown request role')
        if role == 'verifier' and group != 'all':
            raise ValueError('deep-gate verifier must use the one fixed all-groups identity')
        if role != 'verifier' and group not in {e['group'] for e in self.entries}:
            raise ValueError('deep-gate correction has no frozen group')
        expected = 'deep_insight_gate_verifier' if role == 'verifier' else 'deep_insight_gate'
        if kwargs['purpose'] != expected:
            raise ValueError('deep-gate request role/purpose differs')
        entry = {'role':role, 'group':group, 'purpose':expected,
                 'request_id':kwargs['request_id'], 'prompt_hash':content_hash(kwargs['prompt'])}
        with Scheduler(self.model.scheduler_db) as scheduler, scheduler._transaction() as cur:
            _schema(cur)
            rows = cur.execute(f'SELECT * FROM {REQUESTS} WHERE scope_ref_binding=? LIMIT ?',
                               (self.ref,MAX_REQUESTS+1)).fetchall()
            if len(rows) > MAX_REQUESTS:
                raise ValueError('deep-gate request index exceeds bounded18')
            prior = [_body(r) for r in rows]
            if entry not in prior and len(prior)>=MAX_REQUESTS:
                raise ValueError('deep-gate complete request index exceeds bounded18')
            same = [e for e in prior if e['role']==role and e['group']==group]
            limit = 2 if role=='verifier' else 1
            if entry not in same and len(same)>=limit:
                raise ValueError('deep-gate existing correction generation is already used')
            _record(cur, REQUESTS, 'request_key', self.ref+':'+entry['request_id'], self.ref, entry)
        return entry

    def snapshot(self):
        from .planner_paid_output_recovery import _authority_connection, recovery_readers
        with _authority_connection(self.model.scheduler_db) as connection, recovery_readers(
                self.model.config['model_router_db'], self.model.config['budget_db'],
                now=self.model.clock()) as (router,budget):
            row = connection.execute(f'SELECT * FROM {SCOPES} WHERE scope_ref=?',(self.ref,)).fetchone()
            if row is None or _body(row)!=self.scope or budget is None:
                raise ValueError('deep-gate scope or fee authority is unavailable')
            rows=connection.execute(f'SELECT * FROM {REQUESTS} WHERE scope_ref_binding=? LIMIT ?',
                                    (self.ref,MAX_REQUESTS+1)).fetchall()
            if len(rows)>MAX_REQUESTS:raise ValueError('deep-gate request index exceeds bounded18')
            entries=[_body(r)for r in rows]
            if sorted((e for e in entries if e['role']=='normal'),key=lambda e:e['group']) != sorted(self.entries,key=lambda e:e['group']):
                raise ValueError('deep-gate normal request index is incomplete')
            bills={};latest={}
            for entry in entries:
                matches=works(connection,entry)
                for work,authority,formal in matches:
                    if formal is None:
                        self.last={'contract_ref':CONTRACT_REF,'scope_ref':self.ref,'status':'in_flight_or_unproved',
                                   'work_ref':work.id,'work_hash':authority['work_order_hash'],
                                   'retained_cap_micros':int(round(work.budget['max_cost_usd']*1_000_000))}
                        raise ValueError('deep-gate in-flight Work has no formal; retain original reservation')
                    bills[work.id]=billed_work(connection,router,budget,authority,formal)
                if matches:latest[entry['request_id']]=matches[-1]
            self.last={'contract_ref':CONTRACT_REF,'scope_ref':self.ref,'cost_complete':self.scope['complete'],
                       'status':'complete' if self.scope['complete'] else 'legacy_full_fee_import_required',
                       'accounted_micros':sum(b['accounted_micros']for b in bills.values()),
                       'unknown_work_refs':sorted(k for k,b in bills.items()if b['unknown_fee_retained']),
                       'bills':list(bills.values())}
            new_fees={f['admission_ref']:f for b in bills.values() for f in b['fees']
                      if f['append_rowid']>self.run_start_admission_rowid}
            self.last['current_run_cost']={'admission_after_rowid':self.run_start_admission_rowid,
                'settled_micros':sum(f['settled_micros'] or 0 for f in new_fees.values()),
                'unsettled_admission_refs':sorted(k for k,f in new_fees.items() if f['settled_micros'] is None),
                'unknown_work_refs':sorted(k for k,b in bills.items() if b['unknown_fee_retained']
                    and any(f['admission_ref'] in new_fees for f in b['fees']))}
            return entries,latest,self.last

    def replay(self, kwargs):
        """Only the exact latest formal answer; never a replacement paid Work."""
        from .cockpit_model import CockpitModel, _failed_work_trace
        from .planner_paid_output_recovery import _authority_connection, recovery_readers
        with _authority_connection(self.model.scheduler_db) as c:
            row=c.execute(f'SELECT * FROM {REQUESTS} WHERE request_key=?',
                          (self.ref+':'+kwargs['request_id'],)).fetchone()
            if row is None:return None
            entry=_body(row)
            if entry['purpose']!=kwargs['purpose'] or entry['prompt_hash']!=content_hash(kwargs['prompt']):
                raise ValueError('deep-gate replay request differs from its frozen index')
            matches=works(c,entry)
            if not matches:return None
            work,authority,formal=matches[-1]
            if formal is None:raise ValueError('deep-gate current Work is in flight; no replacement permitted')
            if work.metadata.get('mission_version_ref')!=kwargs['mission']['id']:return None
            with recovery_readers(self.model.config['model_router_db'],self.model.config['budget_db'],
                                  now=self.model.clock())as(router,budget):
                if budget is None:raise ValueError('deep-gate replay fee authority unavailable')
                bill=billed_work(c,router,budget,authority,formal)
                if bill['mission_hashes']!=[kwargs['mission']['content_hash']]:
                    raise ValueError('deep-gate replay admission mission hash differs')
                if kwargs['purpose']=='deep_insight_gate_verifier':
                    from .model_fallback_chain import served_family
                    refs=sorted(set(kwargs.get('producer_route_decision_refs')or()))
                    if not refs or sorted(work.metadata.get('producer_route_decision_refs')or())!=refs:
                        raise ValueError('deep-gate verifier producer identities differ')
                    selected=formal['result_envelope']['metadata']['route_decision_ref']
                    if served_family(router,selected)in{served_family(router,r)for r in refs}:
                        raise ValueError('deep-gate replay verifier is not independent')
            reader=Scheduler.__new__(Scheduler);reader.connection=c
            trace=_failed_work_trace(reader,work,purpose=kwargs['purpose'],request_id=work.metadata['request_id'])
            return CockpitModel._answer(formal,work,True,0,'replayed',failure_trace=trace)

    def before_admission(self, *, work, attempt_number, reserved_micros,
                         mission, run_micros, retained_future_micros=0):
        """Read-only gate for an active Work's next ordinary paid attempt.

        The only non-formal Work permitted here is this exact active Work.
        Every earlier admission needs its immutable result receipt and fee.
        """
        from .contracts import ResultEnvelope
        from .planner_paid_output_recovery import _authority_connection, recovery_readers
        with _authority_connection(self.model.scheduler_db) as c, recovery_readers(
                self.model.config['model_router_db'],self.model.config['budget_db'],
                now=self.model.clock()) as (router,budget):
            if budget is None:raise ValueError('deep-gate admission fee authority unavailable')
            row=c.execute(f'SELECT * FROM {SCOPES} WHERE scope_ref=?',(self.ref,)).fetchone()
            if row is None or _body(row)!=self.scope or not self.scope['complete']:
                raise ValueError('deep-gate complete frozen scope is unproved')
            entries=[_body(r) for r in c.execute(f'SELECT * FROM {REQUESTS} WHERE scope_ref_binding=? LIMIT ?',
                                               (self.ref,MAX_REQUESTS+1)).fetchall()]
            if len(entries)>MAX_REQUESTS:raise ValueError('deep-gate request index exceeds bounded18')
            _active_attempt(c,work,attempt_number,self.model.clock())
            total=0;found=False;seen=set()
            for entry in entries:
                for saved,authority,formal in works(c,entry):
                    if saved.id in seen:continue
                    seen.add(saved.id)
                    if saved.id!=work.id:
                        bill=billed_work(c,router,budget,authority,formal)
                        if bill['unknown_fee_retained']:
                            raise ValueError('deep-gate previous terminal unknown forbids a new paid attempt')
                        total+=bill['accounted_micros'];continue
                    found=True
                    if saved.to_dict()!=work.to_dict() or saved.metadata.get('mission_version_ref')!=mission['id']:
                        raise ValueError('deep-gate active Work binding differs')
                    prior=budget.connection.execute('SELECT attempt_number,phase FROM thesis_impact_day_admissions '
                        'WHERE work_order_ref=? ORDER BY attempt_number,phase LIMIT 65',(work.id,)).fetchall()
                    if len(prior)>64:raise ValueError('deep-gate active attempt history exceeds bounded64')
                    for a in prior:
                        if a['attempt_number']>=attempt_number:
                            raise ValueError('deep-gate active admission is already present or not earlier')
                        paid=budget.admission(work_order_ref=work.id,attempt_number=a['attempt_number'],phase=a['phase'])
                        route=router.get_decision(paid['admission']['route_decision_ref'])
                        binding=paid['mission_binding'] or {}
                        if (route['work_order_ref']!=work.id or route['work_order_hash']!=authority['work_order_hash']
                                or route['attempt_number']!=a['attempt_number']
                                or binding.get('mission_version_ref')!=mission['id']
                                or binding.get('mission_version_hash')!=mission['content_hash']):
                            raise ValueError('deep-gate earlier admission binding differs')
                        receipts=c.execute('SELECT * FROM scheduler_result_envelopes WHERE work_order_id=? '
                            'AND attempt_number=? LIMIT 2',(work.id,a['attempt_number'])).fetchall()
                        if len(receipts)!=1:raise ValueError('deep-gate earlier attempt has no unique receipt')
                        rr=dict(receipts[0]);wire=json.loads(rr['result_envelope_json'])
                        result=ResultEnvelope.from_dict(wire)
                        receipt={k:rr[k] for k in ('result_envelope_id','work_order_id','attempt_number',
                                                  'result_envelope_hash','outcome','created_at')}
                        if (canonical_json(wire)!=rr['result_envelope_json'] or content_hash(wire)!=rr['result_envelope_hash']
                                or content_hash(receipt)!=rr['content_hash'] or result.id!=rr['result_envelope_id']
                                or result.work_order_ref!=work.id or result.status!=rr['outcome']
                                or result.metadata.get('route_decision_ref')!=route['id']
                                or result.metadata.get('dispatch_state')=='unknown'):
                            raise ValueError('deep-gate earlier result receipt differs or retains unknown')
                        settlement=paid['settlement']
                        if settlement is None:
                            raise ValueError('deep-gate earlier attempt fee remains unknown; preserve reservation')
                        amount=settlement['actual_micros']
                        if not isinstance(amount,int) or isinstance(amount,bool) or amount<0:
                            raise ValueError('deep-gate earlier settled fee is invalid')
                        total+=amount
            if not found:raise ValueError('deep-gate active Work is outside its frozen request index')
            cap=min(run_micros,int(round(self.scope['original_run_budget']['max_cost_usd']*1_000_000)))
            if total+reserved_micros+retained_future_micros>cap:
                raise ValueError('deep-gate original run budget refuses another paid attempt')
            return {'accounted_micros':total,'reserved_next_micros':reserved_micros,'run_ceiling_micros':cap}

    def before_paid(self, *, run_micros, next_call_micros, retained_future_micros=0):
        _,_,proof=self.snapshot()
        if proof['unknown_work_refs']:
            raise ValueError('deep-gate systemic unknown fee stops this round; no new request authorized')
        if not proof['cost_complete']:
            raise ValueError('deep-gate legacy full fee import required; only exact success replay is allowed')
        original_cap=int(round(self.scope['original_run_budget']['max_cost_usd']*1_000_000))
        # A later configuration may tighten this scope. Raising it requires an
        # explicit future version-bound grant, never a new directory or tick.
        effective_cap=min(run_micros,original_cap)
        if proof['accounted_micros']+next_call_micros+retained_future_micros>effective_cap:
            raise ValueError('deep-gate historical costs plus retained stages exceed original run budget')
        return proof
