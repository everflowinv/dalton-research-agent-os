"""Complete, indexed accounting lineage when source changes one gate group.

A transition carries bills and occupied correction slots, never grants a new
retry. Every ancestor comes from an exact shared normal request and is reread.
"""
from .store import content_hash

CONTRACT_REF = 'deep-insight-gate-indexed-source-transition:0.1'
MAX_SCOPES = 16


def request_entries(c, ref):
    from .deep_insight_gate_accounting import REQUESTS, MAX_REQUESTS, _body
    rows = c.execute(f'SELECT * FROM {REQUESTS} WHERE scope_ref_binding=? LIMIT ?',
                     (ref, MAX_REQUESTS + 1)).fetchall()
    if len(rows) > MAX_REQUESTS:
        raise ValueError('deep-gate ancestor request index exceeds bounded18')
    entries = [_body(row) for row in rows]
    if any(row['request_key'] != ref + ':' + e['request_id'] for row, e in zip(rows, entries)):
        raise ValueError('deep-gate ancestor request primary key differs')
    return entries


def _consumer(c, ref, scope):
    from .deep_insight_gate_accounting import CONSUMERS, _body
    from .deep_insight_gate_draft import build_group_prompt
    row = c.execute(f'SELECT * FROM {CONSUMERS} WHERE consumer_ref=?', (ref,)).fetchone()
    if row is None or row['scope_ref_binding'] != scope['scope_ref']:
        raise ValueError('deep-gate ancestor source consumer is unavailable')
    body = _body(row)
    if (body['scope_ref'] != scope['scope_ref']
            or ref != scope['scope_ref'] + ':consumer:' + content_hash(body)):
        raise ValueError('deep-gate ancestor consumer scope differs')
    for entry in scope['normal_requests']:
        if body['group_inputs'][entry['group']]['company'].get('company_ref') != scope.get('company_ref', body['group_inputs'][entry['group']]['company'].get('company_ref')):
            raise ValueError('deep-gate ancestor consumer company differs')
        if content_hash(build_group_prompt(group=entry['group'], **body['group_inputs'][entry['group']])) != entry['prompt_hash']:
            raise ValueError('deep-gate ancestor consumer prompt differs')
    return {**body, 'ref': ref}


def verified_parent(c, router, budget, model, ref, *, path=(), _cache=None):
    from .deep_insight_gate_accounting import SCOPES, COMPLETIONS, CONTRACT_REF as ACCOUNTING, _body, works, billed_work
    from .deep_insight_gate_legacy import derive as legacy_derive
    from .deep_insight_gate_budget_grant import load_archived_policy
    if ref in path or len(path) >= MAX_SCOPES:
        raise ValueError('deep-gate ancestor cycle or lineage bound exceeded')
    if _cache is None:_cache={}
    if ref in _cache:return _cache[ref]
    row = c.execute(f'SELECT rowid AS ordinal,* FROM {SCOPES} WHERE scope_ref=?', (ref,)).fetchone()
    if row is None:
        raise ValueError('deep-gate indexed ancestor is unavailable')
    scope = _body(row)
    normal = {'contract_ref': ACCOUNTING, 'company_ref': scope['company_ref'], 'normal_requests': scope['normal_requests']}
    if ref != 'deep-gate-accounting:' + content_hash(normal):
        raise ValueError('deep-gate ancestor scope identity differs')
    indexed = request_entries(c, ref)
    if sorted((e for e in indexed if e['role'] == 'normal'), key=lambda e:e['group']) != sorted(scope['normal_requests'], key=lambda e:e['group']):
        raise ValueError('deep-gate ancestor normal index is incomplete')
    inherited = []
    inherited_grants = {}
    inherited_unknowns = set()
    completion = None
    original_budget = scope['original_run_budget']
    if scope['complete'] and any(works(c,e,through_rowid=scope['registered_work_rowid']) for e in scope['normal_requests']):
        raise ValueError('deep-gate ancestor marked fresh contains pre-registration legacy Work')
    if not scope['complete']:
        rows = c.execute(f'SELECT * FROM {COMPLETIONS} WHERE scope_ref_binding=? LIMIT 17', (ref,)).fetchall()
        if not rows or len(rows) > 16:
            raise ValueError('deep-gate indexed ancestor has no bounded complete fee proof')
        # Multiple current consumers may legitimately revalidate one immutable
        # prompt. Every saved completion is checked; none is picked to hide fees.
        for r in rows:
            saved = _body(r); consumer = _consumer(c, saved['consumer_ref'], {**scope, 'scope_ref':ref})
            proof = saved['proof']
            if proof.get('contract_ref') == CONTRACT_REF:
                expected = derive(c, router, budget, model, scope=scope, ref=ref,
                                  consumer=consumer, parents=proof['parents'], path=(*path, ref), _cache=_cache)
                inherited.extend(expected['inherited_entries'])
                inherited_grants.update({g['ref']:g for g in expected['grants']})
                inherited_unknowns.update(expected['allowed_unknown_work_refs'])
                original_budget = expected['original_run_budget']
            else:
                archived = load_archived_policy(model, proof['original_policy']['content_hash'])
                if archived != proof['original_policy']:
                    raise ValueError('deep-gate ancestor original policy archive differs')
                expected = legacy_derive(c, router, budget, entries=scope['normal_requests'],
                    group_inputs=consumer['group_inputs'], wanted=consumer['wanted'],
                    through_rowid=scope['registered_work_rowid'], original_policy=archived)
                from .shared_call_budget_policy import effective_shared_limits
                original_budget = effective_shared_limits(archived, 'deep_insight_gate', kind='run')
            if saved != {'consumer_ref': consumer['ref'], 'proof':expected}:
                raise ValueError('deep-gate ancestor completion proof differs')
            completion = {'ref':r['completion_ref'], 'hash':r['content_hash']}
    unique = {}
    for e in [*indexed, *inherited]:
        key = e['request_id']
        if key in unique and unique[key] != e:
            raise ValueError('deep-gate inherited request identity conflicts')
        unique[key] = e
    bills = {}
    for e in unique.values():
        for w, a, f in works(c, e):
            bills[w.id] = billed_work(c, router, budget, a, f)
    result = {'ref':ref, 'scope_hash':row['content_hash'], 'ordinal':row['ordinal'],
            'company_ref':scope['company_ref'], 'normal_requests':scope['normal_requests'],
            'entries':list(unique.values()), 'bills':list(bills.values()),
            'completion':completion, 'original_run_budget':original_budget,
            'inherited_grants':list(inherited_grants.values()),
            'inherited_unknowns':sorted(inherited_unknowns)}
    _cache[ref]=result
    return result


def derive(c, router, budget, model, *, scope, ref, consumer, parents=None, path=(), _cache=None):
    from .deep_insight_gate_accounting import REQUESTS, SCOPES, GRANTS, CONTRACT_REF as ACCOUNTING, _body, works, billed_work
    from .deep_insight_gate_budget_grant import grant_history, grant_proof, limits, current_policy
    if _cache is None:_cache={}
    child = c.execute(f'SELECT rowid AS ordinal,* FROM {SCOPES} WHERE scope_ref=?', (ref,)).fetchone()
    identity={'contract_ref':ACCOUNTING,'company_ref':scope['company_ref'],'normal_requests':scope['normal_requests']}
    if child is None or _body(child) != scope or ref != 'deep-gate-accounting:' + content_hash(identity):
        raise ValueError('deep-gate transition child scope differs')
    if (consumer['scope_ref'] != ref or _consumer(c,consumer['ref'],{**scope,'scope_ref':ref}) != consumer):
        raise ValueError('deep-gate transition current consumer differs')
    provided_parents = parents
    ids = [e['request_id'] for e in scope['normal_requests']]
    rows = c.execute(f'SELECT DISTINCT r.scope_ref_binding FROM {REQUESTS} r '
        f'JOIN {SCOPES} s ON s.scope_ref=r.scope_ref_binding '
        "WHERE json_extract(r.record_json,'$.role')='normal' "
        "AND json_extract(r.record_json,'$.request_id') IN (?,?,?,?) "
        'AND r.scope_ref_binding!=? AND s.rowid<? LIMIT 17', (*ids, ref, child['ordinal'])).fetchall()
    if len(rows) > MAX_SCOPES:
        raise ValueError('deep-gate indexed parent census exceeds bounded16')
    parents = []
    for row in rows:
        pr = c.execute(f'SELECT rowid AS ordinal,* FROM {SCOPES} WHERE scope_ref=?', (row[0],)).fetchone()
        if pr is not None and pr['ordinal'] < child['ordinal']:
            parents.append(row[0])
    parents.sort()
    if provided_parents is not None and provided_parents != parents:
        raise ValueError('deep-gate ancestor census omits or adds an indexed parent')
    if not parents or len(parents) > MAX_SCOPES or len(set(parents)) != len(parents):
        raise ValueError('deep-gate complete indexed parent set is unavailable')
    entries = {}; bills = {}; lineage = []; grants = []; allowed = set(); caps = []
    for parent in parents:
        verified = verified_parent(c, router, budget, model, parent, path=(*path, ref), _cache=_cache)
        if verified['ordinal'] >= child['ordinal'] or verified['company_ref'] != scope['company_ref']:
            raise ValueError('deep-gate parent chronology or company differs')
        shared = [e['group'] for e in scope['normal_requests'] if e in verified['normal_requests']]
        if not shared:
            raise ValueError('deep-gate ancestor has no exact shared normal source request')
        lineage.append({'scope_ref':parent, 'scope_hash':verified['scope_hash'],
                        'completion':verified['completion'], 'shared_groups':shared})
        for e in verified['entries']:
            if e['request_id'] in entries and entries[e['request_id']] != e:
                raise ValueError('deep-gate parent request union conflicts')
            entries[e['request_id']] = e
        for b in verified['bills']:
            if b['work_ref'] in bills and bills[b['work_ref']] != b:
                raise ValueError('deep-gate parent bill union conflicts')
            bills[b['work_ref']] = b
        rows = c.execute(f'SELECT * FROM {GRANTS} WHERE scope_ref_binding=? LIMIT 5', (parent,)).fetchall()
        if len(rows) > 4:
            raise ValueError('deep-gate parent grants exceed four fixed slots')
        grants.extend(verified['inherited_grants'])
        allowed.update(verified['inherited_unknowns'])
        parent_caps = []
        for row in rows:
            g = _body(row); entry = next(e for e in verified['normal_requests'] if e['group'] == g['group'])
            from .scheduler import Scheduler
            reader = Scheduler.__new__(Scheduler); reader.connection = c
            a = reader.work_order_authority(g['parent_work_ref']); f = reader.formal_result(g['parent_work_ref'])
            original = grant_proof(scope_ref=parent, entry=entry, parent=a, formal=f,
                                   bill=billed_work(c,router,budget,a,f), history=grant_history(model,g))
            if {k:v for k,v in original.items() if k!='content_hash'} != {k:v for k,v in g.items() if k not in {'content_hash','consumer_ref','allowed_unknown_work_refs'}}:
                raise ValueError('deep-gate ancestor immutable grant proof differs')
            _consumer(c,g['consumer_ref'],{'scope_ref':parent,'company_ref':verified['company_ref'],'normal_requests':verified['normal_requests']})
            if limits(current_policy(model)) != g['limits']:
                raise ValueError('deep-gate ancestor grant limits are no longer current')
            retained=[]
            parent_scope = _body(c.execute(f'SELECT * FROM {SCOPES} WHERE scope_ref=?',(parent,)).fetchone())
            for w,a,f in works(c,entry,through_rowid=parent_scope['registered_work_rowid']):
                b=billed_work(c,router,budget,a,f)
                if b['unknown_fee_retained']:
                    grant_proof(scope_ref=parent,entry=entry,parent=a,formal=f,bill=b,history=grant_history(model,g));retained.append(w.id)
            if sorted(retained)!=g['allowed_unknown_work_refs']:
                raise ValueError('deep-gate ancestor retained grant fee set differs')
            allowed.update(retained);grants.append({'ref':g['id'],'hash':row['content_hash'],'group':g['group'],'scope_ref':parent})
            parent_caps.append(g['limits']['run']['max_cost_usd'])
        caps.append(min(parent_caps) if parent_caps else verified['original_run_budget']['max_cost_usd'])
    return {'contract_ref':CONTRACT_REF,'scope_ref':ref,'consumer_ref':consumer['ref'],
            'parents':sorted(parents),'lineage':lineage,'inherited_entries':sorted(entries.values(),key=lambda e:e['request_id']),
            'bills':sorted(bills.values(),key=lambda b:b['work_ref']),
            'grants':sorted({g['ref']:g for g in grants}.values(),key=lambda g:g['ref']),'allowed_unknown_work_refs':sorted(allowed),
            'original_run_budget':{'max_cost_usd':min(caps+[scope['original_run_budget']['max_cost_usd']]),'max_units':4}}
