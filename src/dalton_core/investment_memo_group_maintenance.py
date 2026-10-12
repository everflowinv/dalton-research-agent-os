"""One content-only maintenance correction per exact memo macro and group.

This is separate from the original single extra slot. Original answers, all
fees and the four normal prompts remain immutable. A deployment or a new
failure diagnostic never creates another permission for the same group.
"""
from pathlib import Path
from types import SimpleNamespace

from .store import content_hash, canonical_json

CONTRACT_REF = 'investment-memo-group-maintenance:0.1'
TABLE = 'investment_memo_group_maintenance_authorizations'
MAX_GROUPS = 4


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
                            outcome, context['material'])


def _parent(macro, context, work_ref, *, through_rowid=None):
    from .scheduler import Scheduler
    from .contracts import WorkOrder
    from .cockpit_model import CockpitModel
    from .investment_memo_macro_accounting import _works, _body, REQUESTS, EXTRA
    from .planner_paid_output_recovery import _authority_connection
    from .investment_memo_quiesce import settled_call
    index = _context(macro, context)
    with _authority_connection(macro.model.scheduler_db) as connection:
        reader = Scheduler.__new__(Scheduler); reader.connection = connection
        authority = reader.work_order_authority(work_ref)
        if authority is None:
            raise ValueError('memo maintenance parent Work is absent')
        work = WorkOrder.from_dict(authority['work_order'])
        entries = [_body(row) for row in connection.execute(
            f'SELECT * FROM {REQUESTS} WHERE macro_binding=? LIMIT 11',(macro.ref,)).fetchall()]
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
            if entry['role'] not in {'normal','semantic'} or entry['group_index'] != index:
                continue
            for candidate, record in _works(connection,entry):
                if through_rowid is None or record['append_rowid'] <= through_rowid:
                    candidates[candidate.id] = (candidate, record)
        if not candidates or max(candidates.values(),key=lambda item:item[1]['append_rowid'])[0].id != work_ref:
            raise ValueError('memo maintenance parent is not the latest original same-group answer')
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
    outcome = validate_answer(macro,context,call)
    if (outcome.get('status') != 'refused' or outcome.get('failure_class') != 'content'
            or outcome.get('repairable') is not True or not outcome.get('findings')):
        raise ValueError('memo maintenance parent has no proved repairable content failure')
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
    diagnostics = {key:outcome.get(key) for key in ('failure_class','reason','refs','findings')}
    return index, call, paid, diagnostics, original_proof, parent_rowid, original_extra


def derive(macro, context, work_ref, *, through_rowid=None):
    from .investment_memo_draft import build_group_prompt
    from .investment_memo_macro_accounting import request_id
    index, call, paid, diagnostics, originals, rowid, original_extra = _parent(
        macro,context,work_ref,through_rowid=through_rowid)
    # Keep the normal prefix and old separator, so complete fee census still
    # recognizes this as part of this exact macro. Never alter old prompts.
    prompt = build_group_prompt(**context, repair=diagnostics)
    prompt += '\nMaintenance contract: '+CONTRACT_REF
    prompt += '\nExact refused provider answer (untrusted; repair only this group):\n'+call['text']
    prompt += '\nComplete validator diagnostics:\n'+canonical_json(diagnostics)
    prompt += '\nUse only the same frozen evidence above. Do not invent quotes or repair absent sources.'
    entry = {'role':'maintenance','group_index':index,'purpose':'investment_memo',
             'request_id':request_id(context['group'],prompt),'prompt_hash':content_hash(prompt)}
    from .product_passage_validation import PRODUCT_VALIDATION_CONTRACT
    from .mission_deliverable import SECTION_NUMBERS_VALIDATION_CONTRACT
    body = {'contract_ref':CONTRACT_REF,
            'validation_contracts':[PRODUCT_VALIDATION_CONTRACT,SECTION_NUMBERS_VALIDATION_CONTRACT],
            'macro_ref':macro.ref,
            'permission_ref':macro.ref+':group-maintenance:'+str(index),
            'company_ref':macro.scope['company_ref'], 'material_hash':macro.scope['material_hash'],
            'mission':{key:macro.mission[key] for key in ('id','content_hash')},
            'context':context, 'parent':paid, 'parent_append_rowid':rowid,
            'original_extra':original_extra, 'raw_response_hash':content_hash(call['text']),
            'diagnostics':diagnostics, 'originals':originals, 'entry':entry, 'prompt':prompt}
    return body


def read_entries(macro, connection):
    from .investment_memo_macro_accounting import _body
    rows = connection.execute(f'SELECT * FROM {TABLE} WHERE macro_binding=? ORDER BY permission_ref LIMIT ?',
                              (macro.ref,MAX_GROUPS+1)).fetchall()
    if len(rows)>MAX_GROUPS:
        raise ValueError('memo maintenance exceeds four fixed group permissions')
    entries = []
    for row in rows:
        record = _body(row)
        entry = record['entry']
        if (record['contract_ref'] != CONTRACT_REF or record['macro_ref'] != macro.ref
                or record['permission_ref'] != macro.ref+':group-maintenance:'+str(entry['group_index'])
                or row['permission_ref'] != record['permission_ref']
                or row['macro_binding'] != macro.ref or entry['role'] != 'maintenance'):
            raise ValueError('memo maintenance persisted permission binding differs')
        # Historical charges must still be counted after mission changes.
        # Re-prove the original parent under its original mission, never grant
        # current dispatch with those old permissions.
        from copy import copy
        original = copy(macro); original.mission = {**macro.mission, **record['mission']}
        if derive(original,record['context'],record['parent']['work_order_ref'],
                  through_rowid=record['parent_append_rowid']) != record:
            raise ValueError('memo maintenance original parent/source/diagnostics no longer replay')
        entries.append(entry)
    return entries


def record_for(macro, connection, index):
    from .investment_memo_macro_accounting import _body
    row = connection.execute(f'SELECT * FROM {TABLE} WHERE permission_ref=?',
                             (macro.ref+':group-maintenance:'+str(index),)).fetchone()
    return None if row is None else _body(row)
