"""Bounded complete memo-namespace census for one exact frozen legacy input.

This reader never buys a call, changes old fees, or guesses a missing parent.
The owner may import its result only while the complete namespace still matches.
"""
from __future__ import annotations

from .contracts import WorkOrder, ResultEnvelope
from .scheduler import Scheduler
from .store import content_hash
from .investment_memo_macro_accounting import request_id

CONTRACT_REF = 'investment-memo-legacy-cost-import:0.1'
MAX_NAMESPACE_WORKS = 512
REPAIR_SEPARATOR = '\nYour previous answer for this group was refused and was not used:\n'


def classify(work, prompts):
    from .investment_memo_draft import GROUPS
    purpose=work.metadata.get('purpose')
    if purpose=='investment_memo_verifier':
        return {'role':'verifier_candidate','work_ref':work.id}
    if purpose!='investment_memo':
        return None
    for i,prompt in enumerate(prompts):
        if work.question==prompt or work.question.startswith(prompt+REPAIR_SEPARATOR):
            key=request_id(GROUPS[i][0],work.question)
            if not str(work.metadata.get('request_id','')).startswith(key):
                raise ValueError('legacy memo frozen prompt request identity differs')
            return {'role':'normal' if work.question==prompt else 'semantic', 'group_index':i,
                    'purpose':purpose,'request_id':key,'prompt_hash':content_hash(work.question)}
    return None


def page(connection, *, through_rowid, cursor=None, after_rowid=0, limit=128):
    expression="json_extract(work_order_json,'$.metadata.request_id')"
    sql=f'SELECT rowid,work_order_id,{expression} AS request_id FROM scheduler_work_orders WHERE '\
        f'{expression}>=? AND {expression}<? AND rowid>? AND rowid<=? '
    args=['memo-','memo.',after_rowid,through_rowid]
    if cursor is not None:
        sql+=f'AND ({expression}>? OR ({expression}=? AND rowid>?)) '
        args.extend([cursor['request_id'],cursor['request_id'],cursor['rowid']])
    rows=connection.execute(sql+f'ORDER BY {expression},rowid LIMIT ?',(*args,limit+1)).fetchall()
    return [{'rowid':r['rowid'],'work_ref':r['work_order_id'],'request_id':r['request_id']} for r in rows]


def census(connection, *, through_rowid=None):
    """A complete indexed namespace, never the most recent N rows."""
    sql="SELECT rowid,work_order_id FROM scheduler_work_orders WHERE "\
        "json_extract(work_order_json,'$.metadata.request_id')>=? AND "
    sql+="json_extract(work_order_json,'$.metadata.request_id')<? "
    args=['memo-','memo.']
    if through_rowid is not None:
        sql+='AND rowid<=? '
        args.append(through_rowid)
    rows=connection.execute(sql+'LIMIT ?',(*args,MAX_NAMESPACE_WORKS+1)).fetchall()
    if len(rows) > MAX_NAMESPACE_WORKS:
        raise ValueError('legacy memo namespace exceeds the complete bounded512 census')
    return sorted([{'rowid': row['rowid'], 'work_ref': row['work_order_id']} for row in rows], key=lambda r:r['rowid'])


def bill(connection, router, budget, authority, formal):
    """Keep terminal unknown charges at their original conservative ceiling."""
    work = WorkOrder.from_dict(authority['work_order'])
    if formal is None:
        raise ValueError('legacy memo Work is still in flight or has no terminal formal')
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
        raise ValueError('legacy memo formal binding differs')
    rows = budget.connection.execute('SELECT attempt_number,phase FROM thesis_impact_day_admissions '
        'WHERE work_order_ref=? ORDER BY attempt_number,phase', (work.id,)).fetchall()
    fees = []; total = 0; unknown = False; selected = False
    for row in rows:
        paid = budget.admission(work_order_ref=work.id, attempt_number=row['attempt_number'], phase=row['phase'])
        admission, settlement = paid['admission'], paid['settlement']
        route = router.get_decision(admission['route_decision_ref'])
        binding = paid['mission_binding'] or {}
        if (route['work_order_ref'] != work.id or route['work_order_hash'] != authority['work_order_hash']
                or route['attempt_number'] != row['attempt_number']
                or binding.get('mission_version_ref') != work.metadata.get('mission_version_ref')
                or binding.get('mission_version_hash') != work.metadata.get('mission_version_hash')):
            raise ValueError('legacy memo admission route or mission differs')
        selected |= row['attempt_number'] == formal['attempt_number']
        if settlement is None:
            unknown = True
        else:
            amount = settlement['actual_micros']
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise ValueError('legacy memo settled fee is invalid')
            total += amount
        fees.append({'admission_ref': admission['admission_id'], 'admission_hash': admission['content_hash'],
                     'reserved_micros': admission['reserved_micros'],
                     'settlement_ref': None if settlement is None else settlement['settlement_id'],
                     'settlement_hash': None if settlement is None else settlement['content_hash']})
    md = result.metadata
    if not rows:
        no_send = (result.status == 'failed' and md.get('control_plane_failure') is True
                   and md.get('dispatch_state', 'not_started') == 'not_started'
                   and result.invocation_ref.startswith('invocation:not-started:'))
        if not no_send:
            raise ValueError('legacy memo terminal answer lacks its fee admission')
    elif not selected:
        raise ValueError('legacy memo latest formal has no matching admission attempt')
    if rows:
        served = router.get_decision(md['route_decision_ref'])
        if (served['work_order_ref'] != work.id or served['work_order_hash'] != authority['work_order_hash']
                or served['attempt_number'] != formal['attempt_number']):
            raise ValueError('legacy memo terminal route differs')
    unknown |= md.get('dispatch_state') == 'unknown'
    if unknown:
        # This is a macro gate, never a settlement or refund. Retain all known
        # settled attempts PLUS a ceiling for each still unaccounted attempt.
        unsettled = sum(f['settlement_ref'] is None for f in fees)
        ceiling = int(round(work.budget['max_cost_usd'] * 1_000_000))
        total = total + unsettled * ceiling if unsettled else max(total, ceiling)
    return {'work_ref': work.id, 'work_hash': authority['work_order_hash'],
            'formal_hash': formal['content_hash'], 'fees': fees,
            'unknown_fee_retained': unknown, 'accounted_micros': total}


def derive(model, prompts, *, through_rowid=None, _paged=None):
    from .investment_memo_draft import GROUPS
    from .investment_memo_timeout_recovery import prompt_identity
    from .planner_paid_output_recovery import _authority_connection, recovery_readers
    if len(prompts) != len(GROUPS):
        raise ValueError('legacy import needs all four frozen prompts')
    identities = [prompt_identity(p) for p in prompts]
    if (any(item[2] != i for i, item in enumerate(identities))
            or len({(item[0]['company_ref'], item[3]) for item in identities}) != 1):
        raise ValueError('legacy import frozen group/company/material differs')
    normal = [{'role':'normal', 'group_index': i, 'purpose':'investment_memo',
               'request_id':request_id(GROUPS[i][0], p), 'prompt_hash':content_hash(p)}
              for i, p in enumerate(prompts)]
    with _authority_connection(model.scheduler_db) as connection, recovery_readers(
            model.config['model_router_db'], model.config['budget_db'], now=model.clock()) as (router, budget):
        if budget is None:
            raise ValueError('legacy memo budget authority is unavailable')
        connection.execute('BEGIN')
        router.connection.execute('BEGIN')
        budget.connection.execute('BEGIN')
        reader = Scheduler.__new__(Scheduler); reader.connection = connection
        namespace = census(connection,through_rowid=through_rowid) if _paged is None else _paged['namespace']
        matched = {}; semantic = {}; technical = []; verifiers = []; entries = {}
        held = []
        for item in namespace:
            if _paged is not None and item['work_ref'] not in _paged['candidate_refs']:
                continue
            authority = reader.work_order_authority(item['work_ref'])
            work = WorkOrder.from_dict(authority['work_order'])
            entry=classify(work,prompts)
            if entry is not None and entry['role']=='verifier_candidate':
                verifiers.append((work, authority))
            elif entry is not None:
                if entry['role']=='semantic':
                    semantic[entry['request_id']]=entry
                held.append((entry,work,authority))
        producer_routes = {}
        for entry, work, authority in held:
            formal = reader.formal_result(work.id)
            charged = bill(connection, router, budget, authority, formal)
            matched[work.id] = {**charged, 'append_rowid': next(i['rowid'] for i in namespace if i['work_ref']==work.id)}
            entries[entry['request_id']] = entry
            route_ref = formal['result_envelope']['metadata'].get('route_decision_ref')
            if formal['terminal_state'] == 'succeeded':
                producer_routes[route_ref] = entry['group_index']
            if work.metadata.get('timeout_recovery') is not None:
                # A legacy technical generation already consumes the extra;
                # this import cannot grant another technical authorization.
                technical.append(work.id)
        for work, authority in verifiers:
            refs = set(work.metadata.get('producer_route_decision_refs') or ())
            if not refs or not refs.intersection(producer_routes):
                continue
            if not refs.issubset(producer_routes) or {producer_routes[ref] for ref in refs} != set(range(4)):
                raise ValueError('legacy verifier mixes different frozen producer inputs')
            formal = reader.formal_result(work.id)
            charged = bill(connection, router, budget, authority, formal)
            entry = {'role':'verifier','group_index':None,'purpose':'investment_memo_verifier',
                     'request_id':work.metadata['request_id'].split(':',1)[0],
                     'prompt_hash':content_hash(work.question)}
            entries[entry['request_id']] = entry
            matched[work.id] = {**charged, 'append_rowid': next(i['rowid'] for i in namespace if i['work_ref']==work.id)}
        if not matched:
            raise ValueError('legacy memo census has no matching frozen producer')
        if len(semantic) == 1 and not technical:
            entry = next(iter(semantic.values()))
            extra = {'role':'semantic','request_id':entry['request_id'],'prompt_hash':entry['prompt_hash']}
        elif semantic or technical:
            extra = {'role':'legacy_exhausted','semantic_requests':sorted(semantic),
                     'technical_work_refs':sorted(technical)}
        else:
            extra = None
        result = {'contract_ref':CONTRACT_REF, 'company_ref':identities[0][0]['company_ref'],
                  'material_hash':identities[0][3], 'normal_requests':normal,
                  'namespace':namespace,'namespace_hash':content_hash(namespace),
                  'historical_requests':sorted(entries.values(),key=lambda e:e['request_id']),
                  'historical_works':sorted(matched.values(),key=lambda e:e['append_rowid']),
                  'extra':extra, 'accounted_micros':sum(item['accounted_micros'] for item in matched.values())}
        if _paged is not None:
            result['paged_census_hash']=_paged['content_hash']
        return {**result, 'content_hash':content_hash(result)}
