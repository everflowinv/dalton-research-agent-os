"""Memo-local cost completeness, never a fee refund or model retry authority.

Every request is registered before dispatch. Old unregistered producer Work
proves that a legacy repair set may be missing; only its paid success can be
read for free, until a separate complete-history audit exists.
"""
from __future__ import annotations

import json
import sqlite3
from .contracts import WorkOrder
from .scheduler import Scheduler
from .store import canonical_json, content_hash
from .scheduler import SchedulerError
from .model_router import ModelRouterError
from .thesis_impact_budget import ThesisImpactBudgetError

AUDIT_ERRORS = (ValueError, OSError, KeyError, sqlite3.Error, SchedulerError,
                ModelRouterError, ThesisImpactBudgetError)

CONTRACT_REF = 'investment-memo-macro-accounting:0.1'
SCOPES = 'investment_memo_accounting_scopes'
REQUESTS = 'investment_memo_accounting_requests'
EXTRA = 'investment_memo_accounting_extra_slots'
MAX_REQUESTS = 6  # four producer groups, one optional redo, one verifier
MAX_WORKS = 16


def request_id(group, prompt):
    from .investment_memo_draft import MEMO_PROMPT_CONTRACT_VERSION
    return 'memo-' + content_hash({'contract': MEMO_PROMPT_CONTRACT_VERSION,
                                  'group': group, 'prompt': prompt})[:24]


def _schema(cur):
    for table, key in ((SCOPES, 'macro_ref'), (REQUESTS, 'request_key'), (EXTRA, 'macro_ref')):
        cur.execute(f'CREATE TABLE IF NOT EXISTS {table} ('
                    f'{key} TEXT PRIMARY KEY, macro_binding TEXT NOT NULL, '
                    'record_json TEXT NOT NULL, content_hash TEXT NOT NULL)')
    cur.execute(f'CREATE INDEX IF NOT EXISTS {REQUESTS}_macro ON {REQUESTS}(macro_binding)')
    for table in (SCOPES, REQUESTS, EXTRA):
        cur.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_insert_owner BEFORE INSERT ON {table} '
                    'WHEN dalton_scheduler_authorized() != 1 BEGIN SELECT RAISE(ABORT, '
                    "'memo accounting insert requires the Scheduler owner'); END")
        for action in ('UPDATE', 'DELETE'):
            cur.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()}_immutable '
                        f'BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, '
                        "'memo accounting authority is append-only'); END")


def _record(cur, table, key, identity, macro, body):
    wire = canonical_json(body); digest = content_hash(body)
    row = cur.execute(f'SELECT record_json,content_hash FROM {table} WHERE {key}=?', (identity,)).fetchone()
    if row is not None:
        if row['record_json'] != wire or row['content_hash'] != digest:
            raise ValueError('memo macro accounting immutable binding differs')
    else:
        cur.execute(f'INSERT INTO {table} VALUES (?,?,?,?)', (identity, macro, wire, digest))


def _body(row):
    record = json.loads(row['record_json'])
    if canonical_json(record) != row['record_json'] or content_hash(record) != row['content_hash']:
        raise ValueError('memo macro accounting record hash differs')
    return record


def _works(connection, entry):
    prefix = entry['request_id']
    rows = connection.execute('SELECT rowid AS append_rowid,work_order_id FROM scheduler_work_orders WHERE '
        "json_extract(work_order_json,'$.metadata.request_id')>=? AND "
        "json_extract(work_order_json,'$.metadata.request_id')<? LIMIT ?",
        (prefix, prefix+'\uffff', MAX_WORKS+1)).fetchall()
    if len(rows) > MAX_WORKS:
        raise ValueError('memo macro matching Work exceeds bounded16 request history')
    reader = Scheduler.__new__(Scheduler); reader.connection = connection
    result = []
    for row in rows:
        authority = reader.work_order_authority(row['work_order_id'])
        work = WorkOrder.from_dict(authority['work_order'])
        if content_hash(work.question) != entry['prompt_hash'] or work.metadata.get('purpose') != entry['purpose']:
            raise ValueError('memo macro request prefix has a foreign prompt or purpose')
        result.append((work, {**authority, 'append_rowid':row['append_rowid']}))
    # A technical continuation deliberately retains its parent's created_at.
    # Use the Scheduler's immutable insertion order, not those tied timestamps
    # or an arbitrary lexical Work id, to avoid reviving the failed parent.
    return sorted(result, key=lambda item: item[1]['append_rowid'])


class MemoMacroAccounting:
    def __init__(self, model, prompts, mission):
        from .investment_memo_draft import GROUPS
        from .investment_memo_timeout_recovery import prompt_identity
        if len(prompts) != len(GROUPS):
            raise ValueError('memo macro needs the complete four-group frozen input')
        entries = []
        companies = set(); materials = set()
        for i, prompt in enumerate(prompts):
            company, group, index, material = prompt_identity(prompt)
            if i != index:
                raise ValueError('memo macro normal group order differs')
            companies.add(company['company_ref']); materials.add(material)
            entries.append({'role': 'normal', 'group_index': i, 'purpose': 'investment_memo',
                            'request_id': request_id(group, prompt), 'prompt_hash': content_hash(prompt)})
        if len(companies) != 1 or len(materials) != 1:
            raise ValueError('memo macro company or frozen material differs')
        scope = {'contract_ref': CONTRACT_REF, 'company_ref': next(iter(companies)),
                 'material_hash': next(iter(materials)), 'normal_requests': entries}
        self.ref = 'memo-accounting-macro:' + content_hash(scope)
        self.model = model; self.mission = mission
        self.prompts = tuple(prompts)
        # The scope excludes budgets, routing/config epochs, clocks and run ids.
        # New true source material or Playbook prompts creates a different scope.
        with Scheduler(model.scheduler_db) as scheduler, scheduler._transaction() as cur:
            _schema(cur)
            row = cur.execute(f'SELECT * FROM {SCOPES} WHERE macro_ref=?', (self.ref,)).fetchone()
            if row is None:
                legacy = any(_works(scheduler.connection, entry) for entry in entries)
                maximum = cur.execute('SELECT MAX(rowid) FROM scheduler_work_orders').fetchone()[0] or 0
                body = {**scope, 'registered_work_rowid': maximum, 'complete': not legacy,
                        'status': 'legacy_fee_audit_required' if legacy else 'complete'}
                _record(cur, SCOPES, 'macro_ref', self.ref, self.ref, body)
            else:
                body = _body(row)
                if {key: body[key] for key in scope} != scope:
                    raise ValueError('memo macro frozen input scope differs')
            self.complete = body['complete'] is True
            self.scope = body
            for entry in entries:
                self._register(cur, entry)
        self.last = None

    def _register(self, cur, entry):
        key = self.ref + ':' + entry['request_id']
        _record(cur, REQUESTS, 'request_key', key, self.ref, entry)

    def register(self, *, role, kwargs, group_index=None):
        if role not in {'semantic', 'verifier'} or not self.complete:
            raise ValueError('memo macro legacy fee audit is required before another paid request')
        if role == 'semantic':
            from .investment_memo_timeout_recovery import prompt_identity
            from .investment_memo_draft import GROUPS
            company, group, index, material = prompt_identity(kwargs['prompt'])
            if (kwargs['purpose'] != 'investment_memo'
                    or not kwargs['prompt'].startswith(self.prompts[index]+'\nYour previous answer for this group was refused and was not used:\n')
                    or kwargs['request_id'] != request_id(group, kwargs['prompt'])):
                raise ValueError('memo semantic request is not an exact frozen normal-group correction')
            group_index = index
        elif kwargs['purpose'] != 'investment_memo_verifier':
            raise ValueError('memo verifier request has another purpose')
        entry = {'role': role, 'group_index': group_index, 'purpose': kwargs['purpose'],
                 'request_id': kwargs['request_id'], 'prompt_hash': content_hash(kwargs['prompt'])}
        with Scheduler(self.model.scheduler_db) as scheduler, scheduler._transaction() as cur:
            _schema(cur)
            if role == 'semantic':
                _record(cur, EXTRA, 'macro_ref', self.ref, self.ref,
                        {'role': role, 'request_id': entry['request_id'], 'prompt_hash': entry['prompt_hash']})
            elif cur.execute(f'SELECT count(*) FROM {REQUESTS} WHERE macro_binding=? '
                             "AND json_extract(record_json,'$.role')='verifier'", (self.ref,)).fetchone()[0]:
                row = cur.execute(f'SELECT * FROM {REQUESTS} WHERE request_key=?',
                                  (self.ref+':'+entry['request_id'],)).fetchone()
                if row is None:
                    raise ValueError('memo macro already has its sole verifier request')
            self._register(cur, entry)

    def claim_technical(self, parent_work_ref):
        if not self.complete:
            raise ValueError('memo macro legacy fee audit is required before technical continuation')
        with Scheduler(self.model.scheduler_db) as scheduler, scheduler._transaction() as cur:
            _schema(cur)
            authority = scheduler.work_order_authority(parent_work_ref)
            if authority is None:
                raise ValueError('memo technical slot parent Work is absent')
            work = WorkOrder.from_dict(authority['work_order'])
            if (work.question not in self.prompts or work.metadata.get('purpose') != 'investment_memo'
                    or work.metadata.get('timeout_recovery') is not None):
                raise ValueError('memo technical slot parent is not a frozen normal group')
            from .cockpit_timeout_recovery import failure_proof
            from .planner_paid_output_recovery import recovery_readers
            with recovery_readers(self.model.config['model_router_db'],self.model.config['budget_db'],
                                  now=self.model.clock()) as (router,budget):
                proof = failure_proof(scheduler.connection,router,budget,work_ref=work.id,mission=self.mission)
            if proof is None:
                raise ValueError('memo technical slot lacks the existing strong pure-timeout proof')
            _record(cur, EXTRA, 'macro_ref', self.ref, self.ref,
                    {'role': 'technical', 'parent_work_ref': parent_work_ref, 'parent_failure_proof_hash':content_hash(proof)})

    def snapshot(self):
        from .planner_paid_output_recovery import _authority_connection, recovery_readers
        from .investment_memo_timeout_recovery import _historical_bill
        with _authority_connection(self.model.scheduler_db) as connection, recovery_readers(
                self.model.config['model_router_db'], self.model.config['budget_db'],
                now=self.model.clock()) as (router, budget):
            if budget is None:
                raise ValueError('memo macro fee authority is unavailable')
            scope = connection.execute(f'SELECT * FROM {SCOPES} WHERE macro_ref=?', (self.ref,)).fetchone()
            if scope is None or _body(scope) != self.scope:
                raise ValueError('memo macro scope completeness changed')
            rows = connection.execute(f'SELECT * FROM {REQUESTS} WHERE macro_binding=? ORDER BY request_key '
                                      'LIMIT ?', (self.ref, MAX_REQUESTS+1)).fetchall()
            if len(rows) > MAX_REQUESTS:
                raise ValueError('memo macro exceeds its six bounded logical requests')
            reader = Scheduler.__new__(Scheduler); reader.connection = connection
            bills = {}; latest = {}; entries = []
            for row in rows:
                entry = _body(row); entries.append(entry)
                if entry['role'] == 'normal' and entry not in self.scope['normal_requests']:
                    raise ValueError('memo macro normal request differs from its frozen scope')
                works = _works(connection, entry)
                for work, authority in works:
                    if self.complete and entry['role'] == 'normal':
                        rowid = connection.execute('SELECT rowid FROM scheduler_work_orders WHERE work_order_id=?', (work.id,)).fetchone()[0]
                        if rowid <= self.scope['registered_work_rowid']:
                            raise ValueError('memo complete scope predates an unregistered normal Work')
                    formal = reader.formal_result(work.id)
                    if formal is None:
                        self.last = {'contract_ref': CONTRACT_REF, 'macro_ref': self.ref,
                            'status': 'in_flight_fee_unknown', 'cost_complete': False,
                            'unproved_work_ref':work.id, 'unproved_work_hash':authority['work_order_hash'],
                            'retained_macro_reservation_micros':int(round(work.budget['max_cost_usd']*1_000_000)),
                            'known_indexed_micros':sum(item['accounted_micros'] for item in bills.values())}
                        raise ValueError('memo registered Work is in flight or lacks a formal result; reservation retained')
                    try:
                        bill = _historical_bill(connection, budget, work.id)
                    except ValueError:
                        self.last = {'contract_ref': CONTRACT_REF, 'macro_ref': self.ref,
                            'status':'fee_authority_unproved','cost_complete':False,
                            'unproved_work_ref':work.id,'unproved_work_hash':authority['work_order_hash'],
                            'retained_macro_reservation_micros':int(round(work.budget['max_cost_usd']*1_000_000)),
                            'known_indexed_micros':sum(item['accounted_micros'] for item in bills.values())}
                        raise
                    if (formal['result_envelope'].get('error') or {}).get('code') == 'TIMEOUT':
                        bill['accounted_micros'] = max(bill['accounted_micros'], int(round(work.budget['max_cost_usd']*1_000_000)))
                    bills[work.id] = bill
                if works:
                    work, authority = works[-1]
                    formal = reader.formal_result(work.id)
                    latest[entry['request_id']] = (entry, work, formal)
            if sorted((entry for entry in entries if entry['role']=='normal'), key=lambda e:e['group_index']) != self.scope['normal_requests']:
                raise ValueError('memo macro has an incomplete frozen normal-request index')
            semantic = [entry for entry in entries if entry['role']=='semantic']
            verifier = [entry for entry in entries if entry['role']=='verifier']
            extra = connection.execute(f'SELECT * FROM {EXTRA} WHERE macro_ref=?', (self.ref,)).fetchone()
            self.extra = None if extra is None else _body(extra)
            if len(semantic)>1 or len(verifier)>1:
                raise ValueError('memo macro has multiple correction or verifier generations')
            if semantic and (self.extra is None or self.extra.get('role')!='semantic'
                    or self.extra.get('request_id')!=semantic[0]['request_id']
                    or self.extra.get('prompt_hash')!=semantic[0]['prompt_hash']):
                raise ValueError('memo macro correction is not bound to its unique extra slot')
            if self.extra is not None and self.extra.get('role')=='semantic' and not semantic:
                raise ValueError('memo macro correction extra slot has no registered request')
            total = sum(item['accounted_micros'] for item in bills.values())
            self.last = {'contract_ref': CONTRACT_REF, 'macro_ref': self.ref,
                         'status': 'complete' if self.complete else 'legacy_fee_audit_required',
                         'cost_complete': self.complete,
                         'known_indexed_micros':total,
                         'accounted_micros': total, 'historical_work_count': len(bills),
                         'bills': list(bills.values())}
            return entries, latest, total

    def replay(self, kwargs):
        from .cockpit_model import CockpitModel, _failed_work_trace
        from .investment_memo_quiesce import settled_call
        from .planner_paid_output_recovery import _authority_connection
        from .planner_paid_output_recovery import recovery_readers
        with _authority_connection(self.model.scheduler_db) as connection:
            row = connection.execute(f'SELECT * FROM {REQUESTS} WHERE request_key=?',
                (self.ref+':'+kwargs['request_id'],)).fetchone()
            if row is None:
                return None
            entry = _body(row)
            works = _works(connection, entry)
            if not works:
                return None
            work, authority = works[-1]
            reader = Scheduler.__new__(Scheduler); reader.connection = connection
            formal = reader.formal_result(work.id)
            if formal is None:
                self.last = {'contract_ref':CONTRACT_REF,'macro_ref':self.ref,
                    'status':'in_flight_fee_unknown','cost_complete':False,
                    'unproved_work_ref':work.id,'unproved_work_hash':authority['work_order_hash'],
                    'retained_macro_reservation_micros':int(round(work.budget['max_cost_usd']*1_000_000))}
                raise ValueError('memo registered request is still in flight or lacks a formal result')
        if work.question != kwargs['prompt']:
            raise ValueError('memo macro replay prompt differs')
        # A new mission version does not silently reset old bills or borrow an
        # answer bound to another mission. It must pass a fresh normal gate.
        if (work.metadata.get('mission_version_ref') != kwargs['mission']['id']
                or work.metadata.get('mission_version_hash') != kwargs['mission']['content_hash']):
            return None
        wire = formal['result_envelope']
        # Keep existing admission/pool recovery policy reachable. A typed
        # local no-send refusal is not a paid terminal answer and is not a
        # grant from this module; ordinary CockpitModel must re-prove it.
        if (formal['terminal_state']=='failed'
                and (wire.get('error') or {}).get('code') in {'MODEL_ROUTE_REJECTED','BUDGET_REFUSED','POOL_EXHAUSTED'}
                and wire.get('metadata',{}).get('control_plane_failure') is True
                and wire.get('metadata',{}).get('dispatch_state','not_started')=='not_started'
                and str(wire.get('invocation_ref','')).startswith('invocation:not-started:')):
            with recovery_readers(self.model.config['model_router_db'], self.model.config['budget_db'],
                                  now=self.model.clock()) as (router,budget):
                if budget is None:
                    raise ValueError('memo macro fee authority is unavailable')
                paid = budget.connection.execute('SELECT 1 FROM thesis_impact_day_admissions '
                                                 'WHERE work_order_ref=? LIMIT 1',(work.id,)).fetchone()
            if paid is None:
                return None
        if kwargs['purpose'] == 'investment_memo_verifier':
            from .model_fallback_chain import served_family
            supplied = sorted(set(kwargs.get('producer_route_decision_refs') or ()))
            if not supplied or sorted(work.metadata.get('producer_route_decision_refs') or ()) != supplied:
                raise ValueError('memo verifier replay producer bindings differ')
            with recovery_readers(self.model.config['model_router_db'], self.model.config['budget_db'],
                                  now=self.model.clock()) as (router, budget):
                chosen = formal['result_envelope']['metadata']['route_decision_ref']
                if served_family(router, chosen) in {served_family(router, ref) for ref in supplied}:
                    raise ValueError('memo verifier replay is not provider-independent')
        with _authority_connection(self.model.scheduler_db) as connection:
            reader = Scheduler.__new__(Scheduler); reader.connection = connection
            trace = _failed_work_trace(reader, work, purpose=kwargs['purpose'], request_id=work.metadata['request_id'])
        answer = CockpitModel._answer(formal, work, True, 0, 'replayed', failure_trace=trace)
        settled_call(self.model, answer, kwargs['mission'], purpose=kwargs['purpose'])
        if work.metadata.get('timeout_recovery') is not None:
            answer['timeout_recovery_authorization_ref'] = work.metadata['timeout_recovery']['id']
        return answer

    def room(self, *, group_index, producer_micros, verifier_micros):
        entries, latest, total = self.snapshot()
        remaining = 0
        for entry in entries:
            if entry['role'] != 'normal' or entry['group_index'] <= group_index:
                continue
            current = latest.get(entry['request_id'])
            if current is None or not self._free_current(current[1], current[2]):
                remaining += producer_micros
        verifier = next((entry for entry in entries if entry['role'] == 'verifier'), None)
        current = None if verifier is None else latest.get(verifier['request_id'])
        if current is None or not self._free_current(current[1], current[2]):
            remaining += verifier_micros
        return total, remaining

    def _free_current(self, work, formal):
        return (formal is not None and formal['terminal_state'] == 'succeeded'
                and work.metadata.get('mission_version_ref') == self.mission['id']
                and work.metadata.get('mission_version_hash') == self.mission['content_hash'])

    def before_paid(self, kwargs, *, run_micros, producer_micros, verifier_micros, role):
        if not self.complete:
            raise ValueError('memo legacy_fee_audit_required: old repair generations are not indexed')
        from .investment_memo_timeout_recovery import prompt_identity
        index = 3 if role == 'verifier' else prompt_identity(kwargs['prompt'])[2]
        total, future = self.room(group_index=index, producer_micros=producer_micros,
                                  verifier_micros=0 if role == 'verifier' else verifier_micros)
        cost = verifier_micros if role == 'verifier' else producer_micros
        if total + future + cost > run_micros:
            raise ValueError('memo macro historical costs and retained future reservations exceed the run ceiling')
