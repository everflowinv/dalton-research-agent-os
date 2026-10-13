"""One content-only maintenance correction per exact memo macro and group.

This is separate from the original single extra slot. Original answers, all
fees and the four normal prompts remain immutable. A deployment or a new
failure diagnostic never creates another permission for the same group.
"""
from pathlib import Path
from types import SimpleNamespace

from .store import content_hash, canonical_json

LEGACY_CONTRACT_REF = 'investment-memo-group-maintenance:0.1'
CONTRACT_REF = 'investment-memo-group-maintenance:0.2'
TABLE = 'investment_memo_group_maintenance_authorizations'
MAX_GROUPS = 4
MAX_PERMISSION_RECORDS = 8


def _context(macro, context):
    from .investment_memo_draft import build_group_prompt, GROUPS
    if set(context) != {'group','section_titles','questions','material','company'}:
        raise ValueError('memo maintenance context has an open shape')
    group = context['group']
    index = [item[0] for item in GROUPS].index(group)
    if build_group_prompt(**context) != macro.prompts[index]:
        raise ValueError('memo maintenance context differs from the original frozen group')
    return index


def validate_answer(macro, context, call):
    """Re-run the production parser and every section/source validator."""
    from .investment_memo_draft import parse_group_call
    from .investment_memo_cli import _validated_group
    outcome = parse_group_call(macro.model, call, **{key:context[key] for key in
        ('group','section_titles','questions','material')})
    return _validated_group(SimpleNamespace(path=Path(macro.model.scheduler_db).parent/'core.sqlite'),
                            outcome, context['material'],company_ref=context['company']['company_ref'])


def _parent(macro, context, work_ref, *, through_rowid=None, _issued_diagnostics=None,
            _first_maintenance=None):
    from .scheduler import Scheduler
    from .contracts import WorkOrder
    from .cockpit_model import CockpitModel
    from .investment_memo_macro_accounting import _works, _body, REQUESTS, EXTRA, MAX_TOTAL_REQUESTS
    from .planner_paid_output_recovery import _authority_connection
    from .investment_memo_quiesce import settled_call
    index = _context(macro, context)
    first = None
    if _first_maintenance is not None:
        # Only an actual Scheduler owner row can extend the parent candidate
        # set. A caller's diagnostics or self-hashed proof is not authority.
        with _authority_connection(macro.model.scheduler_db) as source:
            row = source.execute(f'SELECT * FROM {TABLE} WHERE permission_ref=?',
                (macro.ref+':group-maintenance:'+str(index),)).fetchone()
            if row is None:
                raise ValueError('second memo correction lacks the original fixed owner permission')
            first = _body(row)
        if (first['permission_ref'] != _first_maintenance or first['context'] != context
                or first['mission'] != {k:macro.mission[k] for k in ('id','content_hash')}
                or _replay_issued(macro,first) != first):
            raise ValueError('second memo correction original permission/source/current mission differs')
    with _authority_connection(macro.model.scheduler_db) as connection:
        reader = Scheduler.__new__(Scheduler); reader.connection = connection
        authority = reader.work_order_authority(work_ref)
        if authority is None:
            raise ValueError('memo maintenance parent Work is absent')
        work = WorkOrder.from_dict(authority['work_order'])
        rows = connection.execute(f'SELECT * FROM {REQUESTS} WHERE macro_binding=? LIMIT ?',
            (macro.ref,MAX_TOTAL_REQUESTS+1)).fetchall()
        if len(rows)>MAX_TOTAL_REQUESTS:
            raise ValueError('memo maintenance request history exceeds its closed bound')
        entries = [_body(row) for row in rows]
        entries += macro._legacy_entries(connection, check_growth=False)
        extra_row=connection.execute(f'SELECT * FROM {EXTRA} WHERE macro_ref=?',(macro.ref,)).fetchone()
        original_extra = (None if macro.legacy is None else macro.legacy['extra']) if extra_row is None else _body(extra_row)
        if original_extra is None or original_extra.get('role') not in {'semantic','technical','legacy_exhausted'}:
            raise ValueError('memo original extra permission has not been consumed')
        if original_extra['role']=='semantic':
            held=next((entry for entry in entries if entry['role']=='semantic'
                       and entry['request_id']==original_extra['request_id']),None)
            if held is None or not _works(connection,held):
                raise ValueError('memo original semantic request still awaits dispatch')
        if original_extra['role']=='legacy_exhausted':
            if macro.legacy is None or macro.legacy['extra'] != original_extra:
                raise ValueError('memo exhausted original slot lacks its strong legacy import')
            semantic = original_extra['semantic_requests']
            technical = original_extra['technical_work_refs']
            if not semantic and not technical:
                raise ValueError('memo exhausted legacy slot has no original attempts')
            for request in semantic:
                held=next((entry for entry in entries if entry['role']=='semantic'
                           and entry['request_id']==request),None)
                if held is None or not _works(connection,held):
                    raise ValueError('memo exhausted legacy semantic attempt is absent')
            historical={row['work_ref'] for row in macro.legacy['historical_works']}
            if any(ref not in historical for ref in technical):
                raise ValueError('memo exhausted legacy technical attempt is absent')
        candidates = {}
        for entry in entries:
            if entry['group_index'] != index:
                continue
            if entry['role'] not in {'normal','semantic'} and (first is None or entry != first['entry']):
                continue
            for candidate, record in _works(connection,entry):
                if through_rowid is None or record['append_rowid'] <= through_rowid:
                    candidates[candidate.id] = (candidate, record)
        if not candidates or max(candidates.values(),key=lambda item:item[1]['append_rowid'])[0].id != work_ref:
            raise ValueError('memo maintenance parent is not the latest original same-group answer')
        if first is not None:
            first_works = _works(connection,first['entry'])
            if not first_works or first_works[-1][0].id != work_ref:
                raise ValueError('second memo correction parent is not the actual first maintenance answer')
        parent_rowid = candidates[work_ref][1]['append_rowid']
        if through_rowid is not None and parent_rowid != through_rowid:
            raise ValueError('memo maintenance parent append boundary differs')
        if (work.metadata.get('mission_version_ref') != macro.mission['id']
                or work.metadata.get('mission_version_hash') != macro.mission['content_hash']):
            raise ValueError('memo maintenance cannot borrow another mission authority')
        formal = reader.formal_result(work.id)
        # Only a completed successful provider answer can have content defects.
        # TIMEOUT/unknown-send/failed provider output is never a semantic parent.
        if formal is not None and formal['result_envelope'].get('metadata',{}).get('dispatch_state') == 'unknown':
            raise ValueError('memo maintenance parent delivery is unknown')
        call = CockpitModel._answer(formal,work,True,0,'replayed')
    paid = settled_call(macro.model,call,macro.mission,purpose='investment_memo')
    if _issued_diagnostics is None:
        outcome = validate_answer(macro,context,call)
        if outcome.get('failure_class') in {'source_unavailable','authority_unavailable'}:
            raise ValueError('memo maintenance parent '+outcome['failure_class']+': '+str(outcome.get('reason')))
        if (outcome.get('status') != 'refused' or outcome.get('failure_class') != 'content'
                or outcome.get('repairable') is not True or not outcome.get('findings')):
            raise ValueError('memo maintenance parent has no proved repairable content failure')
        diagnostics = {key:outcome.get(key) for key in ('failure_class','reason','refs','findings')}
    else:
        # Read-only replay of the Scheduler owner's immutable issued record.
        # This branch is never used by derive/register for a new permission.
        # A new validator decides acceptance of answers, not whether an old
        # paid authorization and its charges existed.
        diagnostics = _issued_diagnostics
        _validate_issued_diagnostics(diagnostics, context, call['text'])
    # All allowed passage rows must have acquired originals before another
    # model is asked to quote them. This bounded check cannot buy/fetch sources.
    from .investment_memo_draft import citation_index
    from .initial_screen_failure_feedback import _checked_originals
    from .product_passage_validation import rendered_passage_matches
    passages = citation_index(context['material'])['passages']
    if passages:
        originals = _checked_originals(Path(macro.model.scheduler_db).parent,set(passages),
                                      include_render_metadata=True,proof_scope='initial_screen_checkpoint')
        if any(not rendered_passage_matches(originals[ref]['text'],text,
                originals[ref].get('render_header')) for ref,text in passages.items()):
            raise ValueError('memo maintenance allowed source differs from its acquired original')
        original_proof = {ref:{key:row[key] for key in row if key != 'text'} for ref,row in originals.items()}
    else:
        original_proof = {}
    return index, call, paid, diagnostics, original_proof, parent_rowid, original_extra


def _proof_body(macro, context, parent, *, contract_ref, validation_contracts):
    from .investment_memo_draft import build_group_prompt
    from .investment_memo_macro_accounting import request_id
    index, call, paid, diagnostics, originals, rowid, original_extra = parent
    # Keep the normal prefix and old separator, so complete fee census still
    # recognizes this as part of this exact macro. Never alter old prompts.
    prompt = build_group_prompt(**context, repair=diagnostics)
    prompt += '\nMaintenance contract: '+contract_ref
    prompt += '\nExact refused provider answer (untrusted; repair only this group):\n'+call['text']
    prompt += '\nComplete validator diagnostics:\n'+canonical_json(diagnostics)
    prompt += '\nUse only the same frozen evidence above. Do not invent quotes or repair absent sources.'
    entry = {'role':'maintenance','group_index':index,'purpose':'investment_memo',
             'request_id':request_id(context['group'],prompt),'prompt_hash':content_hash(prompt)}
    body = {'contract_ref':contract_ref,
            'validation_contracts':validation_contracts,
            'macro_ref':macro.ref,
            'permission_ref':macro.ref+':group-maintenance:'+str(index),
            'company_ref':macro.scope['company_ref'], 'material_hash':macro.scope['material_hash'],
            'mission':{key:macro.mission[key] for key in ('id','content_hash')},
            'context':context, 'parent':paid, 'parent_append_rowid':rowid,
            'original_extra':original_extra, 'raw_response_hash':content_hash(call['text']),
            'diagnostics':diagnostics, 'originals':originals, 'entry':entry, 'prompt':prompt}
    return body


def _validate_issued_diagnostics(value, context, raw_response):
    if (not isinstance(value,dict) or set(value) != {'failure_class','reason','refs','findings'}
            or value['failure_class'] != 'content' or not isinstance(value['reason'],str)
            or not value['reason'] or len(value['reason']) > 100_000
            or not isinstance(value['refs'],list) or not all(isinstance(r,str) for r in value['refs'])
            or not isinstance(value['findings'],list) or not value['findings']
            or len(value['findings']) > 128):
        raise ValueError('memo historical issued diagnostics have an invalid closed shape')
    allowed_refs = {r['ref'] for r in context['material']}
    # A citation failure may correctly name a fabricated ref from the refused
    # answer. It is diagnostic text only, never an allowed evidence source.
    allowed_refs.update(ref for ref in value['refs'] if ref and ref in raw_response)
    if any(r not in allowed_refs for r in value['refs']):
        raise ValueError('memo historical diagnostics cite a foreign source')
    for finding in value['findings']:
        if (not isinstance(finding,dict) or finding.get('code') not in
                {'citation_slot','section_authority','group_shape'}
                or set(finding) - {'code','detail','refs','section'}
                or not isinstance(finding.get('detail'),str) or not finding['detail']
                or len(finding['detail']) > 100_000):
            raise ValueError('memo historical issued finding is unsupported')
        if 'section' in finding and finding['section'] not in context['section_titles']:
            raise ValueError('memo historical diagnostics refer to another group')
        if 'refs' in finding and (not isinstance(finding['refs'],list)
                or any(not isinstance(ref,str) or ref not in allowed_refs for ref in finding['refs'])):
            raise ValueError('memo historical finding cites a foreign source')


def _validation_contracts():
    from .product_passage_validation import PRODUCT_VALIDATION_CONTRACT
    from .mission_deliverable import SECTION_NUMBERS_VALIDATION_CONTRACT, NUMBER_SOURCE_CONTRACT_VERSION
    return [PRODUCT_VALIDATION_CONTRACT,SECTION_NUMBERS_VALIDATION_CONTRACT,NUMBER_SOURCE_CONTRACT_VERSION]


def derive(macro, context, work_ref, *, through_rowid=None):
    # Only CURRENT complete validation may create a new fixed permission.
    return _proof_body(macro,context,_parent(macro,context,work_ref,through_rowid=through_rowid),
                       contract_ref=CONTRACT_REF,validation_contracts=_validation_contracts())


def _replay_issued(macro, record, *, include_parent=False):
    contracts=record['validation_contracts']
    legacy=['product-validation:exact-rendered-passages-bound-arithmetic:1',
            'section-numbers-capacity:128:v1']
    if record['contract_ref'] == LEGACY_CONTRACT_REF:
        if contracts != legacy:
            raise ValueError('memo legacy issued validation contract is unknown')
    elif record['contract_ref'] == CONTRACT_REF:
        if (not isinstance(contracts,list) or len(contracts)!=3 or contracts[:2]!=legacy
                or contracts[2] not in {'number-source-contract:0.8','number-source-contract:0.9',
                                        'number-source-contract:0.10','number-source-contract:0.11'}):
            raise ValueError('memo issued numeric validation contract is unknown')
    else:
        raise ValueError('memo issued maintenance contract is unknown')
    parent=_parent(macro,record['context'],record['parent']['work_order_ref'],
                   through_rowid=record['parent_append_rowid'],_issued_diagnostics=record['diagnostics'])
    rebuilt = _proof_body(macro,record['context'],parent,contract_ref=record['contract_ref'],
                          validation_contracts=contracts)
    return (rebuilt,parent) if include_parent else rebuilt


def read_entries(macro, connection):
    from .investment_memo_macro_accounting import _body
    rows = connection.execute(f'SELECT * FROM {TABLE} WHERE macro_binding=? ORDER BY permission_ref LIMIT ?',
                              (macro.ref,MAX_PERMISSION_RECORDS+1)).fetchall()
    if len(rows)>MAX_PERMISSION_RECORDS:
        raise ValueError('memo maintenance exceeds two fixed corrections in each of four groups')
    entries = []
    for row in rows:
        record = _body(row)
        entry = record['entry']
        from .investment_memo_second_maintenance import CONTRACT_REF as SECOND_REF, replay_issued
        if record['contract_ref'] == SECOND_REF:
            from copy import copy
            original = copy(macro); original.mission = {**macro.mission, **record['mission']}
            if (row['permission_ref'] != record['permission_ref'] or row['macro_binding'] != macro.ref
                    or replay_issued(original,record) != record):
                raise ValueError('memo second correction stored source/parent differs')
            entries.append(entry)
            continue
        if (record['contract_ref'] not in {LEGACY_CONTRACT_REF,CONTRACT_REF} or record['macro_ref'] != macro.ref
                or record['permission_ref'] != macro.ref+':group-maintenance:'+str(entry['group_index'])
                or row['permission_ref'] != record['permission_ref']
                or row['macro_binding'] != macro.ref or entry['role'] != 'maintenance'):
            raise ValueError('memo maintenance persisted permission binding differs')
        # Historical charges must still be counted after mission changes.
        # Re-prove the original parent under its original mission, never grant
        # current dispatch with those old permissions.
        from copy import copy
        original = copy(macro); original.mission = {**macro.mission, **record['mission']}
        if _replay_issued(original,record) != record:
            raise ValueError('memo maintenance original parent/source/diagnostics no longer replay')
        entries.append(entry)
    return entries


def record_for(macro, connection, index, *, round_number=None):
    from .investment_memo_macro_accounting import _body
    key = macro.ref+':group-maintenance:'+str(index)
    if round_number in (None,2):
        row = connection.execute(f'SELECT * FROM {TABLE} WHERE permission_ref=?',(key+':round2',)).fetchone()
        if row is not None:
            return _body(row)
    if round_number == 2:
        return None
    row = connection.execute(f'SELECT * FROM {TABLE} WHERE permission_ref=?',(key,)).fetchone()
    return None if row is None else _body(row)


def free_revalidated_parent(macro, kwargs):
    """A prior paid answer that now fully passes; never a new call or grant.

    Only an existing owner-issued same-group permission supplies this parent.
    The original complete fee census and spent permission stay untouched.
    """
    from .planner_paid_output_recovery import _authority_connection
    from .investment_memo_macro_accounting import request_id
    if kwargs.get('purpose')!='investment_memo' or kwargs.get('prompt') not in macro.prompts:
        return None
    index=macro.prompts.index(kwargs['prompt'])
    with _authority_connection(macro.model.scheduler_db) as connection:
        record=record_for(macro,connection,index)
    if record is None:
        return None
    if (record['mission'] != {key:macro.mission[key] for key in ('id','content_hash')}
            or kwargs['mission']['id']!=macro.mission['id']
            or kwargs['mission']['content_hash']!=macro.mission['content_hash']
            or kwargs['request_id']!=request_id(record['context']['group'],kwargs['prompt'])):
        raise ValueError('memo free parent replay cannot borrow another mission/group')
    from .investment_memo_second_maintenance import CONTRACT_REF as SECOND_REF, replay_issued
    rebuilt,parent = (replay_issued(macro,record,include_parent=True) if record['contract_ref']==SECOND_REF
                      else _replay_issued(macro,record,include_parent=True))
    if rebuilt!=record:
        raise ValueError('memo maintenance original parent/source/diagnostics no longer replay')
    call=parent[1]
    # Current full parser, exact original quotations and every section's
    # arithmetic must all pass. A source timeout or partial answer cannot win.
    if validate_answer(macro,record['context'],call).get('status')!='drafted':
        return None
    return call
