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
IMPORTS = 'investment_memo_accounting_legacy_imports'
SCANS = 'investment_memo_accounting_legacy_scans'
PAGES = 'investment_memo_accounting_import_pages'
SCAN_BATCH = 128
MAX_REQUESTS = 6  # four producer groups, one optional redo, one verifier
MAX_TOTAL_REQUESTS = MAX_REQUESTS + 4  # independently proved maintenance permissions
MAX_WORKS = 16


def request_id(group, prompt):
    from .investment_memo_draft import MEMO_PROMPT_CONTRACT_VERSION
    return 'memo-' + content_hash({'contract': MEMO_PROMPT_CONTRACT_VERSION,
                                  'group': group, 'prompt': prompt})[:24]


def _schema(cur):
    from .investment_memo_group_maintenance import TABLE as MAINTENANCE
    for table, key in ((MAINTENANCE,'permission_ref'), (SCOPES, 'macro_ref'), (REQUESTS, 'request_key'), (EXTRA, 'macro_ref'), (IMPORTS,'macro_ref'), (SCANS,'checkpoint_id'), (PAGES,'checkpoint_id')):
        cur.execute(f'CREATE TABLE IF NOT EXISTS {table} ('
                    f'{key} TEXT PRIMARY KEY, macro_binding TEXT NOT NULL, '
                    'record_json TEXT NOT NULL, content_hash TEXT NOT NULL)')
    cur.execute(f'CREATE INDEX IF NOT EXISTS {REQUESTS}_macro ON {REQUESTS}(macro_binding)')
    cur.execute(f'CREATE INDEX IF NOT EXISTS {SCANS}_macro ON {SCANS}(macro_binding,checkpoint_id DESC)')
    cur.execute(f'CREATE INDEX IF NOT EXISTS {PAGES}_macro ON {PAGES}(macro_binding,checkpoint_id)')
    cur.execute(f'CREATE INDEX IF NOT EXISTS {MAINTENANCE}_macro ON {MAINTENANCE}(macro_binding)')
    for table in (SCOPES, REQUESTS, EXTRA, IMPORTS, SCANS, PAGES, MAINTENANCE):
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
        # No budget, routing epoch or run clock is added to the scope. The
        # complete prompts retain all actual source rows and their timestamps.
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
        self.legacy = None
        self.import_error = None
        self.scan_error = None
        self.pending = None
        if not self.complete:
            self._import_legacy()

    def _import_legacy(self):
        from .investment_memo_legacy_accounting import derive, census
        # An owner transaction excludes new Scheduler Work during the census
        # and commit. Original scope/Work/fees are never rewritten.
        try:
            with Scheduler(self.model.scheduler_db) as scheduler, scheduler._transaction() as cur:
                _schema(cur)
                row = cur.execute(f'SELECT * FROM {IMPORTS} WHERE macro_ref=?',(self.ref,)).fetchone()
                if row is None:
                    try:
                        proof = derive(self.model,self.prompts)
                    except ValueError as exc:
                        if 'exceeds the complete bounded512 census' not in str(exc):
                            raise
                        paged=self._initial_page(scheduler,cur)
                        if paged is None:
                            # Commit this page before reporting pending. This
                            # is progress, never authority for another call.
                            self.import_error='legacy memo initial complete census is pending; next bounded128 page will resume'
                            row=cur.execute(f'SELECT * FROM {PAGES} WHERE macro_binding=? ORDER BY checkpoint_id DESC LIMIT 1',(self.ref,)).fetchone()
                            self._pending(PAGES,row)
                            return
                        proof=derive(self.model,self.prompts,_paged=paged)
                    if 'paged_census_hash' not in proof and census(scheduler.connection) != proof['namespace']:
                        raise ValueError('legacy memo namespace changed before owner import')
                    _record(cur,IMPORTS,'macro_ref',self.ref,self.ref,proof)
                else:
                    proof = _body(row)
                    if 'paged_census_hash' in proof:
                        paged=self._pages(scheduler.connection)
                        if paged is None or paged['content_hash']!=proof['paged_census_hash']:
                            raise ValueError('legacy memo initial pages no longer bind its census')
                        current=derive(self.model,self.prompts,_paged=paged)
                    else:
                        current = derive(self.model,self.prompts,
                            through_rowid=max(r['rowid'] for r in proof['namespace']))
                    static = ('contract_ref','company_ref','material_hash','normal_requests',
                              'namespace','namespace_hash','historical_requests','extra')
                    if any(current[k] != proof[k] for k in static) or [
                            (w['work_ref'],w['work_hash'],w['append_rowid']) for w in current['historical_works']] != [
                            (w['work_ref'],w['work_hash'],w['append_rowid']) for w in proof['historical_works']]:
                        raise ValueError('legacy memo import no longer derives its complete original membership')
                if (proof['normal_requests'] != self.scope['normal_requests']
                        or proof['company_ref'] != self.scope['company_ref']
                        or proof['material_hash'] != self.scope['material_hash']):
                    raise ValueError('legacy memo imported frozen scope differs')
                self.legacy = proof
                self.complete = True
        except AUDIT_ERRORS as exc:
            # A previously successful group remains freely replayable. Only
            # new paid work needs a complete census and fee authority.
            self.import_error = str(exc)

    def _pending(self, table, row):
        body={'contract_ref':CONTRACT_REF,'macro_ref':self.ref,
              'scope_hash':content_hash(self.scope),'company_ref':self.scope['company_ref'],
              'table':table,'checkpoint_ref':row['checkpoint_id'],
              'checkpoint_hash':row['content_hash']}
        self.pending={**body,'content_hash':content_hash(body)}

    def _initial_page(self, scheduler, cur):
        from .investment_memo_legacy_accounting import page,classify
        rows=cur.execute(f'SELECT * FROM {PAGES} WHERE macro_binding=? ORDER BY checkpoint_id',
                         (self.ref,)).fetchall()
        if rows and _body(rows[-1])['complete']:
            return self._pages(scheduler.connection)
        last=None if not rows else _body(rows[-1])
        cursor=None if last is None else last['cursor']
        target=self.scope['registered_work_rowid']
        batch=page(scheduler.connection,through_rowid=target,cursor=cursor,limit=SCAN_BATCH)
        source=[]
        for item in batch[:SCAN_BATCH]:
            authority=scheduler.work_order_authority(item['work_ref'])
            work=WorkOrder.from_dict(authority['work_order'])
            source.append({**item,'work_hash':authority['work_order_hash'],
                           'candidate':classify(work,self.prompts)})
        next_cursor=None if not source else {k:source[-1][k] for k in ('request_id','rowid')}
        sequence=1 if last is None else last['sequence']+1
        record={'scope_hash':content_hash(self.scope),'sequence':sequence,
            'previous_hash':None if not rows else rows[-1]['content_hash'],
            'through_rowid':target,'start_cursor':cursor,'cursor':next_cursor,
            'complete':len(batch)<=SCAN_BATCH,'source':source}
        _record(cur,PAGES,'checkpoint_id',f'{self.ref}:{sequence:020d}',self.ref,record)
        return self._pages(scheduler.connection) if record['complete'] else None

    def _pages(self, connection):
        from .investment_memo_legacy_accounting import page,classify
        rows=connection.execute(f'SELECT * FROM {PAGES} WHERE macro_binding=? ORDER BY checkpoint_id',
                                (self.ref,)).fetchall()
        namespace=[]; candidates=[]; prior=None; cursor=None
        reader=Scheduler.__new__(Scheduler);reader.connection=connection
        for sequence,row in enumerate(rows,1):
            record=_body(row)
            if (record['sequence']!=sequence or record['previous_hash']!=prior
                    or record['scope_hash']!=content_hash(self.scope) or record['start_cursor']!=cursor
                    or record['through_rowid']!=self.scope['registered_work_rowid']):
                raise ValueError('legacy memo initial page chain differs')
            actual=page(connection,through_rowid=record['through_rowid'],cursor=cursor,limit=SCAN_BATCH)
            if [ {k:item[k] for k in ('rowid','work_ref','request_id')} for item in record['source']] != actual[:SCAN_BATCH]:
                raise ValueError('legacy memo initial page omits source namespace rows')
            if record['complete'] != (len(actual)<=SCAN_BATCH):
                raise ValueError('legacy memo initial page falsely claims completion')
            for item in record['source']:
                # Re-read only the source candidates. Unrelated immutable
                # Work remains covered by the owner-hashed complete page.
                if item['candidate'] is not None:
                    authority=reader.work_order_authority(item['work_ref'])
                    work=WorkOrder.from_dict(authority['work_order'])
                    if authority['work_order_hash']!=item['work_hash'] or classify(work,self.prompts)!=item['candidate']:
                        raise ValueError('legacy memo page candidate source differs')
                    candidates.append(item['work_ref'])
                namespace.append({'rowid':item['rowid'],'work_ref':item['work_ref']})
            expected_cursor=None if not record['source'] else {k:record['source'][-1][k] for k in ('request_id','rowid')}
            if record['cursor']!=expected_cursor:
                raise ValueError('legacy memo initial page cursor skips its exact source boundary')
            prior=row['content_hash'];cursor=record['cursor']
        if not rows or not _body(rows[-1])['complete']:
            return None
        body={'namespace':sorted(namespace,key=lambda r:r['rowid']),
              'candidate_refs':candidates,'pages_hash':prior}
        return {**body,'content_hash':content_hash(body)}

    def _legacy_entries(self, connection, *, check_growth=True):
        if self.legacy is None:
            return []
        from .investment_memo_legacy_accounting import census, REPAIR_SEPARATOR
        row = connection.execute(f'SELECT * FROM {IMPORTS} WHERE macro_ref=?',(self.ref,)).fetchone()
        if row is None or _body(row) != self.legacy:
            raise ValueError('legacy memo import authority changed')
        initial = self.legacy['namespace']
        watermark = max((r['rowid'] for r in initial),default=0)
        if 'paged_census_hash' in self.legacy:
            paged=self._pages(connection)
            if paged is None or paged['content_hash']!=self.legacy['paged_census_hash']:
                raise ValueError('legacy memo initial paged census differs')
        elif census(connection,through_rowid=watermark) != initial:
            raise ValueError('legacy memo initial complete census differs')
        if check_growth and self.scan_error is not None:
            raise ValueError(self.scan_error)
        return list(self.legacy['historical_requests'])

    def _advance_legacy_scan(self):
        if self.legacy is None:
            return
        self.scan_error=None
        self.pending=None
        try:
            # Finish owner metadata writes before opening any immutable cold
            # reader below. No write may occur inside its file-stability guard.
            with Scheduler(self.model.scheduler_db) as scheduler:
                rows=scheduler.connection.execute(f'SELECT * FROM {REQUESTS} WHERE macro_binding=? LIMIT ?',
                                                   (self.ref,MAX_TOTAL_REQUESTS+1)).fetchall()
                maintenance = self._registered_requests(rows,scheduler.connection)
            entries={_body(r)['request_id']:_body(r) for r in rows}
            entries.update({e['request_id']:e for e in maintenance})
            entries.update({e['request_id']:e for e in self.legacy['historical_requests']})
            routes=set()
            with Scheduler(self.model.scheduler_db) as scheduler:
                for entry in entries.values():
                    if entry['purpose']!='investment_memo':
                        continue
                    for work,_ in _works(scheduler.connection,entry):
                        formal=scheduler.formal_result(work.id)
                        if formal is not None and formal['terminal_state']=='succeeded':
                            routes.add(formal['result_envelope']['metadata'].get('route_decision_ref'))
            self._scan_appended(max(r['rowid'] for r in self.legacy['namespace']),entries,routes)
        except AUDIT_ERRORS as exc:
            self.scan_error=str(exc)

    def _scan_appended(self, initial_watermark, entries, producer_routes):
        from .investment_memo_legacy_accounting import REPAIR_SEPARATOR
        expression="json_extract(work_order_json,'$.metadata.request_id')"
        with Scheduler(self.model.scheduler_db) as scheduler, scheduler._transaction() as cur:
            _schema(cur)
            row=cur.execute(f'SELECT * FROM {SCANS} WHERE macro_binding=? ORDER BY checkpoint_id DESC LIMIT 1',
                            (self.ref,)).fetchone()
            last=None if row is None else _body(row)
            if last is not None and last['import_hash']!=self.legacy['content_hash']:
                raise ValueError('legacy memo incremental cursor belongs to another import')
            maximum=cur.execute('SELECT MAX(rowid) FROM scheduler_work_orders').fetchone()[0] or 0
            if last is None:
                start,target,cursor=initial_watermark,maximum,None
            elif last['complete']:
                start,target,cursor=last['target_rowid'],maximum,None
            else:
                start,target,cursor=last['from_rowid'],last['target_rowid'],last['cursor']
            if start>=target:
                return
            sql=f'SELECT rowid,work_order_id,{expression} AS request_id FROM scheduler_work_orders WHERE '\
                f'{expression}>=? AND {expression}<? AND rowid>? AND rowid<=? '
            args=['memo-','memo.',start,target]
            if cursor is not None:
                sql+=f'AND ({expression}>? OR ({expression}=? AND rowid>?)) '
                args.extend([cursor['request_id'],cursor['request_id'],cursor['rowid']])
            sql+=f'ORDER BY {expression},rowid LIMIT ?'
            page=cur.execute(sql,(*args,SCAN_BATCH+1)).fetchall()
            reader=Scheduler.__new__(Scheduler);reader.connection=scheduler.connection
            for item in page[:SCAN_BATCH]:
                work=WorkOrder.from_dict(reader.work_order_authority(item['work_order_id'])['work_order'])
                belongs=any(work.question==p or work.question.startswith(p+REPAIR_SEPARATOR) for p in self.prompts)
                if work.metadata.get('purpose')=='investment_memo_verifier':
                    belongs=bool(set(work.metadata.get('producer_route_decision_refs') or ()).intersection(producer_routes))
                if belongs and not any(work.metadata.get('request_id','').startswith(e['request_id'])
                       and content_hash(work.question)==e['prompt_hash'] for e in entries.values()):
                    raise ValueError('legacy memo gained an unregistered matching request after import')
            complete=len(page)<=SCAN_BATCH
            next_cursor=None if not page else {'request_id':page[min(len(page),SCAN_BATCH)-1]['request_id'],
                                             'rowid':page[min(len(page),SCAN_BATCH)-1]['rowid']}
            sequence=1 if last is None else last['sequence']+1
            checkpoint={'import_hash':self.legacy['content_hash'],'sequence':sequence,
                'previous_hash':None if row is None else row['content_hash'],
                'from_rowid':start,'target_rowid':target,'cursor':next_cursor,'complete':complete,
                'page_work_refs':[r['work_order_id'] for r in page[:SCAN_BATCH]]}
            _record(cur,SCANS,'checkpoint_id',f'{self.ref}:{sequence:020d}',self.ref,checkpoint)
        if not complete or target<maximum:
            with Scheduler(self.model.scheduler_db) as scheduler:
                row=scheduler.connection.execute(f'SELECT * FROM {SCANS} WHERE checkpoint_id=?',(f'{self.ref}:{sequence:020d}',)).fetchone()
                self._pending(SCANS,row)
            raise ValueError('legacy memo incremental census is incomplete; next bounded128 page is pending')

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
        if self.legacy is not None:
            extra=self.legacy['extra']
            if role=='semantic' and extra is not None and extra != {
                    'role':'semantic','request_id':entry['request_id'],'prompt_hash':entry['prompt_hash']}:
                raise ValueError('legacy memo optional slot was already consumed')
            verifiers=[e['request_id'] for e in self.legacy['historical_requests'] if e['role']=='verifier']
            if role=='verifier' and verifiers and entry['request_id'] not in verifiers:
                raise ValueError('legacy memo already has its sole verifier request')
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

    def _registered_requests(self, rows, connection):
        entries = [_body(row) for row in rows]
        ordinary = [entry for entry in entries if entry['role'] != 'maintenance']
        if (len(entries)>MAX_TOTAL_REQUESTS or len(ordinary)>MAX_REQUESTS
                or any(entry['role'] not in {'normal','semantic','verifier','maintenance'} for entry in entries)):
            raise ValueError('memo macro exceeds six original or four maintenance requests')
        # The extra entries cannot be manufactured by the ordinary registrar.
        from .investment_memo_group_maintenance import read_entries
        proved = read_entries(self,connection)
        if sorted((entry for entry in entries if entry['role']=='maintenance'),key=lambda e:e['request_id']) != sorted(proved,key=lambda e:e['request_id']):
            raise ValueError('memo maintenance accounting entries lack exact owner permissions')
        return proved

    def prepare_maintenance(self, context, outcome):
        from .investment_memo_group_maintenance import derive, record_for, _context
        from .planner_paid_output_recovery import _authority_connection
        if not self.complete or outcome.get('failure_class') != 'content':
            raise ValueError('memo maintenance needs complete fees and a typed content failure')
        self.snapshot()  # includes every historical known/unknown charge
        if self.extra is None:
            raise ValueError('memo original extra slot must be used before group maintenance')
        index = _context(self,context)
        with _authority_connection(self.model.scheduler_db) as connection:
            if record_for(self,connection,index) is not None:
                raise ValueError('memo fixed group maintenance was already attempted')
        return derive(self,context,outcome['model']['work_order_ref'])

    def register_maintenance(self, body, kwargs):
        from .investment_memo_group_maintenance import derive, TABLE
        if (not self.complete or body['mission'] != {k:self.mission[k] for k in ('id','content_hash')}
                or kwargs != {'purpose':'investment_memo','request_id':body['entry']['request_id'],
                              'prompt':body['prompt'],'mission':self.mission}):
            raise ValueError('memo maintenance current mission or request differs')
        if derive(self,body['context'],body['parent']['work_order_ref'],
                  through_rowid=body['parent_append_rowid']) != body:
            raise ValueError('memo maintenance parent diagnostics/source differ before registration')
        with Scheduler(self.model.scheduler_db) as scheduler, scheduler._transaction() as cur:
            _schema(cur)
            _record(cur,TABLE,'permission_ref',body['permission_ref'],self.ref,body)
            # Old runtimes still account these Work rows, or refuse once their
            # original six-request bound is exceeded; never hide new charges.
            self._register(cur,body['entry'])

    def group_request(self, kwargs):
        """Free selection of the latest registered answer for a normal group.

        Old successful semantic answers must be re-used on restart, rather
        than discovering the old normal error and purchasing its repair again.
        A consumed maintenance permission always selects its exact request,
        including a no-send/in-flight refusal; never a fresh generation.
        """
        from .investment_memo_timeout_recovery import prompt_identity
        from .investment_memo_group_maintenance import record_for
        from .planner_paid_output_recovery import _authority_connection
        if (not self.complete or kwargs['purpose'] != 'investment_memo'
                or kwargs['prompt'] not in self.prompts):
            return kwargs, None
        index = prompt_identity(kwargs['prompt'])[2]
        with _authority_connection(self.model.scheduler_db) as connection:
            rows = connection.execute(f'SELECT * FROM {REQUESTS} WHERE macro_binding=? LIMIT ?',
                                      (self.ref,MAX_TOTAL_REQUESTS+1)).fetchall()
            self._registered_requests(rows,connection)
            # Selecting an already-paid answer is free. Preserve the existing
            # incremental-census contract: growth blocks new fees, not this
            # strong same-request replay. before_paid still runs full snapshot.
            entries = {_body(row)['request_id']:_body(row) for row in rows}
            entries.update({entry['request_id']:entry for entry in
                            self._legacy_entries(connection,check_growth=False)})
            reader=Scheduler.__new__(Scheduler);reader.connection=connection
            permission = record_for(self,connection,index)
            if permission is not None:
                if permission['mission'] != {k:self.mission[k] for k in ('id','content_hash')}:
                    raise ValueError('memo group maintenance was consumed under another mission')
                return {**kwargs,'request_id':permission['entry']['request_id'],
                        'prompt':permission['prompt']}, permission
            from .investment_memo_profile_maintenance import persisted_entry as profile_entry
            profile=profile_entry(self,connection,index)
            if profile is not None:
                entry,proof=profile
                return {**kwargs,'request_id':entry['request_id'],'prompt':proof['prompt']}, None
            from .investment_memo_terminal_maintenance import persisted_entry
            terminal=persisted_entry(self,connection,index)
            if terminal is not None:
                entry,proof=terminal
                return {**kwargs,'request_id':entry['request_id'],'prompt':proof['prompt']}, None
            candidates = []
            for entry in entries.values():
                if entry['role'] not in {'normal','semantic'} or entry['group_index'] != index:
                    continue
                works = _works(connection,entry)
                if works:
                    work,_ = works[-1]
                    rowid = connection.execute('SELECT rowid FROM scheduler_work_orders WHERE work_order_id=?',
                                               (work.id,)).fetchone()[0]
                    candidates.append((rowid,entry,work))
            if candidates:
                _, entry, work = max(candidates,key=lambda row:row[0])
                if entry['role']=='semantic':
                    # Old authority is not borrowed into a different mission.
                    if not self._free_current(work,reader.formal_result(work.id)):
                        from .investment_memo_profile_maintenance import qualify as capability_qualification
                        proof=capability_qualification(self,None,connection)
                        if proof['parent']['work_ref']!=work.id or proof['required_limits']['failure_code']!='TIMEOUT':
                            raise ValueError('memo old semantic answer cannot authorize a new mission or failed call')
                    return {**kwargs,'request_id':entry['request_id'],'prompt':work.question}, None
        return kwargs, None

    def claim_technical(self, parent_work_ref):
        if not self.complete:
            raise ValueError('memo macro legacy fee audit is required before technical continuation')
        if self.legacy is not None and self.legacy['extra'] is not None:
            raise ValueError('legacy memo optional slot was already consumed')
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
        from .investment_memo_legacy_accounting import bill as accounted_bill
        self._advance_legacy_scan()
        with _authority_connection(self.model.scheduler_db) as connection, recovery_readers(
                self.model.config['model_router_db'], self.model.config['budget_db'],
                now=self.model.clock()) as (router, budget):
            if budget is None:
                raise ValueError('memo macro fee authority is unavailable')
            scope = connection.execute(f'SELECT * FROM {SCOPES} WHERE macro_ref=?', (self.ref,)).fetchone()
            if scope is None or _body(scope) != self.scope:
                raise ValueError('memo macro scope completeness changed')
            rows = connection.execute(f'SELECT * FROM {REQUESTS} WHERE macro_binding=? ORDER BY request_key '
                                      'LIMIT ?', (self.ref, MAX_TOTAL_REQUESTS+1)).fetchall()
            maintenance = self._registered_requests(rows,connection)
            reader = Scheduler.__new__(Scheduler); reader.connection = connection
            bills = {}; latest = {}; entries = []
            imported = self._legacy_entries(connection)
            held_entries = {_body(row)['request_id']:_body(row) for row in rows}
            held_entries.update({e['request_id']:e for e in maintenance})
            for entry in imported:
                existing=held_entries.get(entry['request_id'])
                if existing is not None and existing != entry:
                    raise ValueError('legacy memo registered request differs from imported authority')
                held_entries[entry['request_id']]=entry
            for row in rows:
                entry = _body(row)
                if entry['role'] == 'normal' and entry not in self.scope['normal_requests']:
                    raise ValueError('memo macro normal request differs from its frozen scope')
            for entry in held_entries.values():
                entries.append(entry)
                if entry['role'] == 'normal' and entry not in self.scope['normal_requests']:
                    raise ValueError('memo macro normal request differs from its frozen scope')
                works = _works(connection, entry)
                for work, authority in works:
                    if self.scope['complete'] is True and entry['role'] == 'normal':
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
                        bill = accounted_bill(connection,router,budget,authority,formal)
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
            self.extra = (None if self.legacy is None else self.legacy['extra']) if extra is None else _body(extra)
            if self.legacy is None and (len(semantic)>1 or len(verifier)>1):
                raise ValueError('memo macro has multiple correction or verifier generations')
            if self.legacy is None and semantic and (self.extra is None or self.extra.get('role')!='semantic'
                    or self.extra.get('request_id')!=semantic[0]['request_id']
                    or self.extra.get('prompt_hash')!=semantic[0]['prompt_hash']):
                raise ValueError('memo macro correction is not bound to its unique extra slot')
            if self.extra is not None and self.extra.get('role')=='semantic' and not semantic:
                raise ValueError('memo macro correction extra slot has no registered request')
            total = sum(item['accounted_micros'] for item in bills.values())
            self.last = {'contract_ref': CONTRACT_REF, 'macro_ref': self.ref,
                         'status': 'complete' if self.complete else 'legacy_fee_audit_required',
                         'cost_complete': self.complete,
                         'unknown_fee_retained_work_refs':sorted(k for k,v in bills.items() if v.get('unknown_fee_retained')),
                         'fee_certainty':'conservative_upper_bound' if any(v.get('unknown_fee_retained') for v in bills.values()) else 'settled',
                         'known_indexed_micros':total,
                         'legacy_import_hash':None if self.legacy is None else self.legacy['content_hash'],
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
                from .investment_memo_group_maintenance import read_entries
                maintenance = read_entries(self,connection)
                from .investment_memo_terminal_maintenance import persisted_entry
                terminal=persisted_entry(self,connection)
                terminal_entries=[] if terminal is None else [terminal[0]]
                from .investment_memo_profile_maintenance import persisted_entry as profile_entry
                profile=profile_entry(self,connection)
                if profile is not None:terminal_entries.append(profile[0])
                entry=next((e for e in maintenance+terminal_entries+self._legacy_entries(connection,check_growth=False) if e['request_id']==kwargs['request_id']),None)
                if entry is None:
                    return None
            else:
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
        if role == 'maintenance':
            future = max(future,(3-index)*producer_micros+verifier_micros)
        cost = verifier_micros if role == 'verifier' else producer_micros
        if total + future + cost > run_micros:
            raise ValueError('memo macro historical costs and retained future reservations exceed the run ceiling')
