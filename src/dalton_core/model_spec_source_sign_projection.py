"""Owner-recorded source-proven coefficient projection, never a model reply.

Only one unique single-coefficient proof that passes every held financial
period is eligible. Original answers, Work, grants and accounting stay intact.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from collections.abc import Mapping

from .store import canonical_json, content_hash

CONTRACT_REF = 'model-spec-source-sign-projection:0.1'
TABLE = 'company_model_spec_source_sign_projections'
_SCHEMA = Path(__file__).with_name('model_spec_source_sign_projection_schema.sql')


def _maintenance_reply(missions, model, state, mission, connection, work, root_call):
    """Rebuild the existing permission and its exact successful output guard.

    Never call verify_child: that is the paid CAS/write entry, not a reader.
    """
    from .model_spec_maintenance_repair import (MaintenanceAuthority, TABLE as GRANTS,
        CLAIMS, qualify, binding_for, _paid_child_matches, assert_changed)
    binding=work['metadata']['structured_output_repair'];grant=binding['maintenance_grant']
    row=missions.connection.execute('SELECT * FROM '+GRANTS+' WHERE root_ref=?',(grant['root_ref'],)).fetchone()
    if row is None:raise ValueError('source-sign projection maintenance grant missing')
    body=json.loads(row['record_json'])
    if (canonical_json(body)!=row['record_json'] or content_hash(body)!=row['content_hash']
            or grant['content_hash']!=row['content_hash'] or body['root_original']['work_order_ref']!=row['root_ref']):
        raise ValueError('source-sign projection maintenance grant differs')
    rebuilt,root,parent=qualify(connection,missions=missions,model=model,state=state,mission=mission,
        root_ref=grant['root_ref'],parent_ref=body['repair_parent']['work_order_ref'])
    if (root!=root_call or rebuilt!={k:v for k,v in body.items() if k!='authorization_at_issue'}):
        raise ValueError('source-sign projection maintenance parent differs')
    MaintenanceAuthority(missions,model,state,mission)._issued_permission(body)
    claim=missions.connection.execute('SELECT * FROM '+CLAIMS+' WHERE work_ref=?',(work['id'],)).fetchone()
    expected,prompt=binding_for(body,parent['text'])
    if (claim is None or claim['root_ref']!=grant['root_ref'] or claim['grant_hash']!=content_hash(body)
            or not _paid_child_matches(body,parent['text'],work,claim,expected,prompt)):
        raise ValueError('source-sign projection maintenance child claim differs')
    return parent,body['diagnostic_bundle']


def _permission(missions, mission, company):
    current = missions._resolve_active_mission_for_company(company,
        purpose=CONTRACT_REF, mission_version_ref=mission['id'],
        mission_version_hash=mission['content_hash'])
    if (current != mission or 'model_run' not in current['autonomy']['may_write']
            or 'company_model' not in current['deliverables']
            or company not in {v['company_ref'] for v in current['universe']}):
        raise ValueError('source-sign projection current permission differs')
    cursor = missions.connection.cursor()
    missions._validate_playbook_binding(cursor, current['bindings']['playbook_version'])
    missions._validate_constitution_binding(cursor, current['bindings']['constitution_version'], current['industry_ref'])
    missions._validate_mandate_binding(cursor, current['bindings']['mandate_version'], current['industry_ref'])
    return current


def _settled_success_history(model, connection, work, source, mission):
    """Require fully receipted successful attempts, not a conservative reserve.

    This initial projection contract deliberately declines lineages containing
    failed, retryable or unknown attempts even if a budget ledger settled a
    conservative amount. Their recovery permissions remain independent.
    """
    from .contracts import ResultEnvelope
    from .investment_memo_quiesce import settled_call
    from .planner_paid_output_recovery import recovery_readers
    receipts=connection.execute('SELECT * FROM scheduler_result_envelopes WHERE work_order_id=? '
        'ORDER BY attempt_number LIMIT 65',(work['id'],)).fetchall()
    if not receipts or len(receipts)>64:
        raise ValueError('source-sign projection attempt history exceeds bound or is absent')
    attempts=set();receipt_hashes=[]
    for rr in receipts:
        wire=json.loads(rr['result_envelope_json']);result=ResultEnvelope.from_dict(wire).to_dict()
        body={k:rr[k] for k in ('result_envelope_id','work_order_id','attempt_number',
            'result_envelope_hash','outcome','created_at')}
        if (canonical_json(result)!=rr['result_envelope_json'] or content_hash(result)!=rr['result_envelope_hash']
                or content_hash(body)!=rr['content_hash'] or result['id']!=rr['result_envelope_id']
                or result['work_order_ref']!=work['id'] or rr['work_order_id']!=work['id']
                or result['status']!='succeeded' or rr['outcome']!='succeeded'
                or result.get('error') is not None or result['metadata'].get('dispatch_state')=='unknown'
                or rr['attempt_number'] in attempts):
            raise ValueError('source-sign projection attempt is not a unique proven success')
        attempts.add(rr['attempt_number']);receipt_hashes.append(rr['content_hash'])
    with recovery_readers(model.config['model_router_db'],model.config['budget_db'],now=model.clock()) as (router,budget):
        rows=budget.connection.execute(
            'SELECT attempt_number FROM thesis_impact_day_admissions WHERE work_order_ref=? LIMIT 129',
            (work['id'],)).fetchall()
        admitted={r[0] for r in rows}
        if len(rows)>128 or admitted!=attempts:
            raise ValueError('source-sign projection admissions and receipts differ')
        for rr in receipts:
            route=router.get_decision(json.loads(rr['result_envelope_json'])['metadata']['route_decision_ref'])
            if (route['work_order_ref']!=work['id'] or route['work_order_hash']!=content_hash(work)
                    or route['attempt_number']!=rr['attempt_number'] or route['outcome']!='selected'):
                raise ValueError('source-sign projection attempt route differs')
    bill=settled_call(model,source,mission,purpose='model_spec')
    return {**bill,'receipt_hashes':receipt_hashes}


def derive(missions, model, state, mission, call, *, note_evidence_resolver=None):
    """Pure strong read; no DDL, write, model, or admission."""
    from .planner_paid_output_recovery import _authority_connection
    from .model_spec_maintenance_repair import _load, _proof
    from .company_model_cli import build_prompt, validate_model_spec_request_identity, TASK_HASH
    from .company_model_spec import spec_from_response, parse_response
    from .model_spec_signed_expense_repair import (
        SignedExpenseRepairError, CONTRACT_REF as SIGNED_REF,
        validate_financials_for_repair, assert_only_coefficient_changed)
    from .document_mission_lineage import document_mission_versions

    scheduler_db=getattr(model,'scheduler_db',None)
    if not isinstance(scheduler_db,(str,Path)) or not Path(scheduler_db).is_file():
        raise ValueError('source-sign projection requires the durable Scheduler')
    if content_hash({k:v for k,v in state.items() if k!='state_hash'}) != state['state_hash']:
        raise ValueError('source-sign projection current state hash differs')
    current = _permission(missions, mission, state['company_ref'])
    with _authority_connection(scheduler_db) as connection:
        work, original = _load(connection, call['work_order_ref'])
        if (_proof(original) != _proof(call) or original['text'] != call['text']):
            raise ValueError('source-sign projection original result differs')
        binding = work['metadata'].get('structured_output_repair') or {}
        root_ref = (binding.get('root_original') or {}).get('work_order_ref', work['id'])
        root, root_call = _load(connection, root_ref)
        identity = validate_model_spec_request_identity(root['metadata'].get('model_spec_request_identity', {}))
        if (identity.get('task_hash') != TASK_HASH or identity.get('state_hash') != state['state_hash']
                or root['question'] != build_prompt(state)
                or root['metadata'].get('structured_output_repair') is not None
                or (binding and binding.get('root_original') != _proof(root_call))):
            raise ValueError('source-sign projection original root derivation differs')
        if binding:
            # Initial scope deliberately supports the existing owner-issued
            # maintenance chain. Other repair forms need their own complete
            # historical reply guards before this reader can accept them.
            if binding.get('maintenance_grant') is None:
                raise ValueError('source-sign projection ordinary repair guard not supported yet')
            parent,scope=_maintenance_reply(missions,model,state,mission,connection,work,root_call)
            from .model_spec_maintenance_repair import assert_changed
            from .company_model_spec import parse_response
            assert_changed(parse_response(parent['text']),parse_response(original['text']),scope,state=state)
        refs = [r[0] for r in connection.execute(
            "SELECT work_order_id FROM scheduler_work_orders WHERE work_order_id >= 'work:cockpit-model_spec-' "
            "AND work_order_id < 'work:cockpit-model_spec.' AND "
            "json_extract(work_order_json,'$.metadata.structured_output_repair.root_original.work_order_ref')=? "
            'ORDER BY work_order_id LIMIT 65', (root_ref,)).fetchall()]
        if len(refs)>64 or (work['id']!=root_ref and work['id'] not in refs):
            raise ValueError('source-sign projection root history bound differs')
        # A projection cannot select an earlier answer and bypass any later
        # registered repair, including one with unknown fees or no formal.
        ancestors=set();walk=work
        while walk['id']!=root_ref:
            if walk['id'] in ancestors or len(ancestors)>=64:
                raise ValueError('source-sign projection repair ancestry is cyclic or too large')
            ancestors.add(walk['id'])
            parent_proof=(walk['metadata'].get('structured_output_repair') or {}).get('repair_parent')
            if not isinstance(parent_proof,Mapping):
                raise ValueError('source-sign projection repair ancestry is incomplete')
            walk,parent_call=_load(connection,parent_proof.get('work_order_ref'))
            if _proof(parent_call)!=parent_proof:
                raise ValueError('source-sign projection repair ancestor differs')
        if set(refs)!=ancestors:
            raise ValueError('source-sign projection selected answer is not the complete latest repair chain')
        fees = []
        versions = document_mission_versions(missions.connection, current)
        for ref in sorted(set(refs) | {root_ref}):
            sibling, source = _load(connection, ref)
            version = missions._mission_record(sibling['metadata']['mission_version_ref'])
            if (version['id'] not in versions or version['content_hash'] != sibling['metadata']['mission_version_hash']):
                raise ValueError('source-sign projection original mission ancestry differs')
            fees.append(_settled_success_history(model,connection,sibling,source,version))

    payload = parse_response(original['text'])
    spec = spec_from_response(state, original['text'], decided_by=current['autonomy']['automation_principal'],
        note_evidence_resolver=note_evidence_resolver)
    try:
        validate_financials_for_repair(missions, spec, note_evidence_resolver=note_evidence_resolver)
    except SignedExpenseRepairError as error:
        proof = error.proof
    else:
        return None
    if not isinstance(proof, Mapping) or proof.get('contract_ref') != SIGNED_REF:
        raise ValueError('source-sign projection needs one unique coefficient')
    projected = copy.deepcopy(payload)
    node = projected
    for key in proof['coefficient_path'][:-1]:
        node = node[key]
    node[proof['coefficient_path'][-1]] = proof['required_coefficient']
    assert_only_coefficient_changed(payload, projected, proof)
    corrected = spec_from_response(state, canonical_json(projected),
        decided_by=current['autonomy']['automation_principal'], note_evidence_resolver=note_evidence_resolver)
    financial = validate_financials_for_repair(missions, corrected, note_evidence_resolver=note_evidence_resolver)
    body = {'contract_ref':CONTRACT_REF,'root_original':_proof(root_call),
        'source_call':_proof(original),'company_ref':state['company_ref'], 'state_hash':state['state_hash'],
        'original_text_sha256':hashlib.sha256(original['text'].encode()).hexdigest(), 'original_spec_hash':spec['content_hash'],
        'projected_text_sha256':hashlib.sha256(canonical_json(projected).encode()).hexdigest(),
        'projected_spec_hash':corrected['content_hash'], 'signed_proof':dict(proof),
        'financial_validation_hash':content_hash(financial), 'fees':fees,
        'actor_ref':current['autonomy']['automation_principal']}
    return body, corrected


def _record(row):
    value = json.loads(row['record_json'])
    body={k:v for k,v in value.items() if k not in {'id','created_at','content_hash'}}
    if (value['id']!='model-spec-source-sign-projection:'+content_hash(body)[:32]
            or canonical_json(value)!=row['record_json'] or value['content_hash']!=row['content_hash']
            or value['content_hash']!=content_hash({k:v for k,v in value.items() if k!='content_hash'})
            or value['id']!=row['projection_ref'] or value['root_original']['work_order_ref']!=row['root_work_ref']
            or value['source_call']['work_order_ref']!=row['source_work_ref']
            or value['company_ref']!=row['company_ref'] or value['created_at']!=row['created_at']):
        raise ValueError('source-sign projection owner record differs')
    return value


def project(missions, model, state, mission, call, *, note_evidence_resolver=None):
    result = derive(missions,model,state,mission,call,note_evidence_resolver=note_evidence_resolver)
    if result is None:
        raise ValueError('source-sign projection has no qualifying failure')
    body, corrected = result
    # Normal owner installation only, after all strong pure qualification.
    missions.connection.executescript(_SCHEMA.read_text())
    with missions._transaction() as cursor:
        fresh=derive(missions,model,state,mission,call,note_evidence_resolver=note_evidence_resolver)
        if fresh is None or fresh[0]!=body or fresh[1]!=corrected:
            raise ValueError('source-sign projection source or permission changed before owner CAS')
        row=cursor.execute('SELECT * FROM '+TABLE+' WHERE root_work_ref=?',
            (body['root_original']['work_order_ref'],)).fetchone()
        if row is not None:
            record=_record(row)
            if {k:v for k,v in record.items() if k not in {'id','created_at','content_hash'}} != body:
                raise ValueError('source-sign projection fixed root already used differently')
            return corrected, record
        record={**body,'id':'model-spec-source-sign-projection:'+content_hash(body)[:32],
            'created_at':model.clock().isoformat(timespec='microseconds')}
        record['content_hash']=content_hash(record)
        cursor.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?,?,?)',
            (record['id'],body['root_original']['work_order_ref'],body['source_call']['work_order_ref'],
             body['company_ref'],canonical_json(record),record['content_hash'],record['created_at']))
        return corrected,record


def read(missions, model, state, mission, call, *, note_evidence_resolver=None):
    """Free read of existing authority, with current source and permission replay."""
    if missions.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone() is None:
        return None
    result=derive(missions,model,state,mission,call,note_evidence_resolver=note_evidence_resolver)
    if result is None:return None
    body,corrected=result
    row=missions.connection.execute('SELECT * FROM '+TABLE+' WHERE root_work_ref=?',
        (body['root_original']['work_order_ref'],)).fetchone()
    if row is None:return None
    record=_record(row)
    if {k:v for k,v in record.items() if k not in {'id','created_at','content_hash'}}!=body:
        raise ValueError('source-sign projection read no longer matches original authority')
    return corrected,record
