"""One closed technical migration, never a reclassification of historic fees."""
from __future__ import annotations
import json
from collections.abc import Mapping
from .contracts import WorkOrder
from .store import canonical_json, content_hash
from .directed_numeric_research import CONTRACT_REF, DirectedNumericError

REPAIR_VERSION = 'directed-numeric-technical-maintenance:0.1'
CLASSIFICATION = 'versioned_numeric_technical_maintenance'
ACTOR = 'automation:directed-numeric-contract-maintenance'
# Fixed successor for this migration version. A later schema requires its own
# version; existing Work/link replay must not follow a mutable resource hash.
SUCCESSOR_SCHEMA_HASH = '8c7feeb36677222c89eceae97d553f628e4c11ed33d5d5fab69c6bb7005168c4'
FAILED_REASON = 'numeric_technical_maintenance_failed'
DAY_CAP_REASON = 'numeric_technical_maintenance_day_cap_reached'
_FIELDS = {'repair_version', 'admission_ref', 'admission_hash', 'stage_ordinal',
           'failed_work_order_ref', 'failed_work_order_hash', 'formal_result_ref',
           'formal_result_hash', 'result_envelope_ref', 'result_envelope_hash', 'defect', 'failure_accounting', 'content_hash'}


def candidate(work, index):
    """A cheap candidate only. Execution still proves its complete authority."""
    from .directed_numeric_executor import LEGACY_VERIFIER_SCHEMA_HASHES
    meta = work.get('metadata') or {}
    if meta.get('numeric_contract_ref') != CONTRACT_REF or meta.get('numeric_technical_maintenance'):
        return False
    if index == 1:
        try:
            prompt = json.loads(work['question'])
        except (TypeError, ValueError):
            return False
        return isinstance(prompt, Mapping) and 'identity_contract_ref' not in prompt
    return index == 2 and meta.get('verifier_provider_schema_hash') in LEGACY_VERIFIER_SCHEMA_HASHES


def evidence(authority, scheduler, admission, work, formal, index, worker):
    """Prove the finite existing defect, without declaring historic no-send."""
    from .mission_document_research_executor import (
        _failure_execution_authority, _paid_contract_failure_proof,
        _unproved_send_state_record, _formal_ref, _formal_hash,
    )
    if (not candidate(work, index) or admission.get('numeric_authority', {}).get('contract_ref') != CONTRACT_REF
            or formal.get('terminal_state') != 'failed'):
        return None
    meta = work['metadata']; envelope = formal['result_envelope']
    if (meta.get('mission_document_research_admission_ref') != admission['id']
            or meta.get('mission_document_research_admission_hash') != admission['content_hash']
            or envelope.get('work_order_ref') != work['id']
            or content_hash(envelope) != formal['result_envelope_hash']):
        raise DirectedNumericError('numeric technical maintenance authority differs')
    if _failure_execution_authority(scheduler, work, formal, worker):
        return None
    if index == 1:
        from .directed_numeric_recovery import build_numeric_output_repair
        from .directed_numeric_output import legacy_draft_prompt_v0
        paid = _paid_contract_failure_proof(authority, work, formal, index, worker)
        if paid is None:
            return None  # Unknown/unsettled sends never become a paid format repair.
        old = legacy_draft_prompt_v0(question=admission['planner_inquiry']['question'],
            search_proof=meta['retrieval_proof'], targets=meta['numeric_targets'],
            catalog=meta['numeric_metric_definitions'])
        if old != work['question']:
            return None
        rejected = (envelope.get('metadata') or {}).get('rejected_model_output') or {}
        message = rejected.get('error_message')
        if (not isinstance(message, str) or not message.startswith((
                'numeric figure differs from typed target', 'numeric draft target binding differs',
                'numeric draft has invalid closed shape', 'numeric draft must account'))):
            return None
        repair = build_numeric_output_repair(work, formal)
        if repair is None:
            return None
        defect = {'kind': 'legacy_target_identity_instructions', 'repair': repair}
        accounting = paid
    else:
        from .directed_numeric_executor import verifier_schema_hash
        from .openclaw_model_adapter import (
            _required_provider_controls, PROVIDER_CONTROL_MODE_REQUIRED, ModelAdmissionError,
        )
        if (envelope.get('error') or {}).get('code') != 'MODEL_ADAPTER_REJECTED':
            return None
        dispatch = (envelope.get('metadata') or {}).get('dispatch_proof')
        if isinstance(dispatch, Mapping) and dispatch.get('state') != 'definitely_not_sent':
            return None
        route = worker.router.get_decision(envelope['metadata']['route_decision_ref'])
        try:
            _required_provider_controls(WorkOrder.from_dict(work), route,
                work['budget']['max_output_tokens'], PROVIDER_CONTROL_MODE_REQUIRED)
        except ModelAdmissionError as exc:
            if str(exc) != 'independent verifier provider schema hash does not match its contract':
                return None
        else:
            return None
        accounting = _unproved_send_state_record(authority, admission, work, formal, index, worker)
        if accounting is None:
            return None
        if verifier_schema_hash() != SUCCESSOR_SCHEMA_HASH:
            return None
        defect = {'kind': 'finite_raw_provider_schema_identity',
                  'old_schema_hash': meta['verifier_provider_schema_hash'],
                  'new_schema_hash': SUCCESSOR_SCHEMA_HASH,
                  'present_local_contract_check': 'provider_schema_identity_mismatch',
                  'historical_send_state': 'retained_unproved'}
    result = {'repair_version': REPAIR_VERSION, 'admission_ref': admission['id'],
              'admission_hash': admission['content_hash'], 'stage_ordinal': index + 1,
              'failed_work_order_ref': work['id'], 'failed_work_order_hash': content_hash(work),
              'formal_result_ref': _formal_ref(formal), 'formal_result_hash': _formal_hash(formal),
              'result_envelope_ref': envelope['id'], 'result_envelope_hash': formal['result_envelope_hash'],
              'defect': defect, 'failure_accounting': accounting}
    result['content_hash'] = content_hash(result)
    return result


def transform(work, value):
    if (not isinstance(value, Mapping) or set(value) != _FIELDS
            or value['repair_version'] != REPAIR_VERSION
            or value['failed_work_order_ref'] != work['id']
            or value['failed_work_order_hash'] != content_hash(work)
            or value['content_hash'] != content_hash({k: v for k, v in value.items() if k != 'content_hash'})):
        raise DirectedNumericError('numeric technical migration identity differs')
    metadata = dict(work['metadata']); question = work['question']; defect = value['defect']
    if defect.get('kind') == 'legacy_target_identity_instructions' and value['stage_ordinal'] == 2:
        from .directed_numeric_recovery import replay_numeric_output_repair
        question, metadata = replay_numeric_output_repair(work, defect['repair'])
    elif defect.get('kind') == 'finite_raw_provider_schema_identity' and value['stage_ordinal'] == 3:
        from .directed_numeric_executor import LEGACY_VERIFIER_SCHEMA_HASHES, verifier_schema_hash
        if (set(defect) != {'kind','old_schema_hash','new_schema_hash','present_local_contract_check','historical_send_state'}
                or defect['old_schema_hash'] not in LEGACY_VERIFIER_SCHEMA_HASHES
                or defect['old_schema_hash'] != metadata['verifier_provider_schema_hash']
                or defect['new_schema_hash'] != SUCCESSOR_SCHEMA_HASH
                or defect['present_local_contract_check'] != 'provider_schema_identity_mismatch'
                or defect['historical_send_state'] != 'retained_unproved'):
            raise DirectedNumericError('numeric provider schema migration differs')
        metadata['verifier_provider_schema_hash'] = defect['new_schema_hash']
        metadata['model_request_binding_hash'] = content_hash({
            'predecessor_request_binding_hash': work['metadata']['model_request_binding_hash'],
            'predecessor_work_hash': content_hash(work), 'provider_schema_hash': defect['new_schema_hash'],
            'technical_repair_hash': value['content_hash']})
    else:
        raise DirectedNumericError('numeric technical defect is not supported')
    metadata['numeric_technical_maintenance'] = {
        'repair_version': REPAIR_VERSION, 'source_work_ref': work['id'],
        'source_work_hash': content_hash(work), 'evidence_hash': value['content_hash']}
    return question, metadata


def scope(admission, index):
    return {'admission_ref': admission['id'], 'stage_ordinal': index + 1, 'repair_version': REPAIR_VERSION}

_AUTH_FIELDS = {'schema_version','id','kind','actor_ref','repair_scope','admission_ref',
                'admission_hash','stage_ordinal','failed_work_order_ref','failed_work_order_hash',
                'formal_result_ref','formal_result_hash','max_fresh_work_orders','max_cost_usd',
                'authorized_at','day','technical_evidence','content_hash'}


def verify_authorization(authority, scheduler, admission, work, formal, index, worker, proof):
    row = authority.store.connection.execute(
        'SELECT * FROM mission_document_research_controlled_recovery_authorizations WHERE authorization_id=?',
        (proof.get('authorization_ref'),)).fetchone()
    if row is None:
        raise DirectedNumericError('numeric technical migration authorization unavailable')
    value = json.loads(row['record_json'])
    expected_id = 'mission-document-numeric-maintenance:' + content_hash(scope(admission, index))[:32]
    actual = evidence(authority, scheduler, admission, work, formal, index, worker)
    if actual is not None:
        from .mission_document_research_executor import (
            _with_recorded_binding_hash, _authentic_binding_hashes, _expected_budget_binding)
        actual['failure_accounting'] = _with_recorded_binding_hash(
            actual['failure_accounting'], value['technical_evidence']['failure_accounting'],
            lambda: _authentic_binding_hashes(authority, worker.budget_store,
                _expected_budget_binding(authority, admission, index)))
        actual['content_hash'] = content_hash({k:v for k,v in actual.items() if k!='content_hash'})
    if (actual is None or set(value) != _AUTH_FIELDS or value['kind'] != CLASSIFICATION or value['actor_ref'] != ACTOR
            or value['id'] != expected_id or value['repair_scope'] != scope(admission, index)
            or value['schema_version'] != '0.1'
            or value['admission_ref'] != admission['id'] or value['stage_ordinal'] != index + 1
            or value['admission_hash'] != admission['content_hash']
            or value['failed_work_order_ref'] != work['id']
            or value['failed_work_order_hash'] != content_hash(work)
            or value['formal_result_ref'] != actual['formal_result_ref']):
        raise DirectedNumericError('numeric technical migration authorization differs')
    if (canonical_json(value) != row['record_json']
            or value['content_hash'] != row['content_hash']
            or value['content_hash'] != content_hash({k:v for k,v in value.items() if k!='content_hash'})
            or proof.get('authorization_hash') != value['content_hash']
            or value['technical_evidence'] != actual or proof.get('technical_evidence') != actual
            or value['formal_result_hash'] != actual['formal_result_hash']
            or value['max_fresh_work_orders'] != 1
            or value['max_cost_usd'] != work['budget']['max_cost_usd']
            or value['day'] != value['authorized_at'][:10]
            or any(value[k] != row[c] for k,c in (
                ('id','authorization_id'),('admission_ref','admission_ref'),
                ('stage_ordinal','stage_ordinal'),('failed_work_order_ref','failed_work_order_ref'),
                ('authorized_at','created_at')))):
        raise DirectedNumericError('numeric technical migration proof differs')
    return value


def attempt(executor, admission, work, formal, index, links):
    from datetime import timedelta
    from .mission_document_research_executor import (
        _time, _formal_ref, _formal_hash, _recovery_policy, _expected_budget_binding,
        AUTOMATIC_CONTRACT_RETRY_CLASSIFICATION,
    )
    stage = work['metadata']['stage']; now = executor.clock()
    if work['metadata'].get('numeric_technical_maintenance'):
        safe = executor._safe_failure_proof(admission, work, formal, index)
        envelope = formal['result_envelope']
        placeholder = 'invocation:not-started:' + content_hash({
            'work_order_ref': work['id'], 'attempt_number': formal['attempt_number'],
            'code': (envelope.get('error') or {}).get('code'), 'invocation_ref': None,
            'route_ref': (envelope.get('metadata') or {}).get('route_decision_ref')})[:32]
        no_invocation = (envelope.get('invocation_ref') == placeholder
            and envelope.get('metadata',{}).get('control_plane_failure') is True
            and not envelope.get('usage_refs') and not envelope.get('actual_side_effects') and
            executor.connection.execute('SELECT 1 FROM model_invocations WHERE work_order_ref=? LIMIT 1',
                (work['id'],)).fetchone() is None)
        if (isinstance(safe, Mapping) and safe.get('classification') in {
                'atomic_day_budget_refusal', 'atomic_pool_budget_refusal'} and no_invocation):
            from .mission_document_research_executor import (
                read_recovery_round, append_recovery_round, recovery_rounds_on,
                AUTOMATIC_RECOVERY_ROUND_ACTOR, MAX_AUTOMATIC_RECOVERY_ROUNDS_PER_DAY)
            from datetime import datetime, timezone
            refusal_day = safe.get('refusal_day')
            reset = datetime.fromisoformat(refusal_day).replace(tzinfo=timezone.utc) + timedelta(days=1)
            if now < reset:
                executor._recovery_observation(admission,work,formal,index,{
                    'status':'waiting','reason':'numeric_maintenance_budget_waiting','day':_time(now)[:10],
                    'eligible':False,'used_fresh_work_orders':len(links),'max_fresh_work_orders':1,
                    'retry_at':_time(reset),'deadline':_time(reset),'proof':safe})
                return {'status':'waiting','reason':'numeric_maintenance_budget_waiting','day':_time(now)[:10],
                        'retry_at':_time(reset),'work_order_ref':work['id'],'stage':stage}
            prior_round = read_recovery_round(executor.connection, work['id'])
            basis = {'repair_version': REPAIR_VERSION,
                'technical_maintenance': work['metadata']['numeric_technical_maintenance'],
                'no_send_budget_proof': safe}
            if prior_round is not None:
                if prior_round['kind'] != 'automatic' or prior_round['basis'] != basis:
                    raise DirectedNumericError('maintenance budget backoff authority differs')
                return None  # Existing ordinary proof consumes the exact round.
            day = _time(now)[:10]
            if recovery_rounds_on(executor.connection, admission['id'], index+1,
                    kind='automatic', day=day) < MAX_AUTOMATIC_RECOVERY_ROUNDS_PER_DAY:
                append_recovery_round(executor.connection, admission_ref=admission['id'],
                    admission_hash=admission['content_hash'], stage_ordinal=index+1,
                    failed_work_order_ref=work['id'], failed_work_order_hash=content_hash(work),
                    hold_reason='numeric_maintenance_budget_backoff', kind='automatic',
                    actor_ref=AUTOMATIC_RECOVERY_ROUND_ACTOR, basis=basis, now=now)
                return None
            tomorrow = now.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)
            executor._recovery_observation(admission,work,formal,index,{
                'status':'waiting','reason':'numeric_maintenance_budget_backoff_day_cap','day':_time(now)[:10],
                'eligible':False,'used_fresh_work_orders':len(links),'max_fresh_work_orders':1,
                'retry_at':_time(tomorrow),'deadline':_time(tomorrow),'proof':safe})
            return {'status':'waiting','reason':'numeric_maintenance_budget_backoff_day_cap','day':_time(now)[:10],
                    'retry_at':_time(tomorrow),'work_order_ref':work['id'],'stage':stage}
        # An immutable one-generation migration is spent even when its output
        # fails. Neither this door nor generic automatic doors create a child.
        record = {'status':'stopped','reason':FAILED_REASON,'eligible':False,
                  'used_fresh_work_orders':len(links),'max_fresh_work_orders':1,
                  'retry_at':None,'deadline':formal['created_at'],
                  'proof':{**work['metadata']['numeric_technical_maintenance'],
                    'result_envelope_ref':formal['result_envelope']['id'],
                    'result_envelope_hash':formal['result_envelope_hash']}}
        executor._recovery_observation(admission, work, formal, index, record)
        return {'status':'stopped','reason':FAILED_REASON,'work_order_ref':work['id'],'stage':stage}
    # Preserve ordinary bounded recovery before the version migration. This
    # door repairs an exhausted historical contract, not its first failure.
    if ((index == 1 and executor._contract_retry_disposition(links) is None)
            or (index == 2 and executor._unproved_retry_disposition(links) is None)):
        return None
    worker = executor.draft_worker if index==1 else executor.verifier_worker
    value = evidence(executor.authority, executor.scheduler, admission, work, formal, index, worker)
    if value is None:
        return None
    # A single deterministic key, locked check/insert, and an immutable stored
    # timestamp make concurrent ticks and partial-write replay converge.
    key = 'mission-document-numeric-maintenance:' + content_hash(scope(admission,index))[:32]
    with executor._transaction() as cur:
        row = cur.execute('SELECT record_json FROM mission_document_research_controlled_recovery_authorizations '
                          'WHERE authorization_id=?',(key,)).fetchone()
        if row is not None:
            authorization = json.loads(row['record_json'])
            if authorization['technical_evidence'] != value:
                raise DirectedNumericError('numeric technical migration scope already consumed')
        else:
            day = _time(now)[:10]
            rows = cur.execute('SELECT record_json FROM mission_document_research_controlled_recovery_authorizations '
                               'WHERE substr(created_at,1,10)=?',(day,)).fetchall()
            used = sum(json.loads(r[0]).get('kind') in {CLASSIFICATION,AUTOMATIC_CONTRACT_RETRY_CLASSIFICATION} for r in rows)
            if used >= executor.max_automatic_contract_retries_per_day:
                authorization = None
            else:
                authorization = {'schema_version':'0.1','id':key,'kind':CLASSIFICATION,'actor_ref':ACTOR,
                    'repair_scope':scope(admission,index),'admission_ref':admission['id'],
                    'admission_hash':admission['content_hash'],'stage_ordinal':index+1,
                    'failed_work_order_ref':work['id'],'failed_work_order_hash':content_hash(work),
                    'formal_result_ref':_formal_ref(formal),'formal_result_hash':_formal_hash(formal),
                    'max_fresh_work_orders':1,'max_cost_usd':work['budget']['max_cost_usd'],
                    'authorized_at':_time(now),'day':day,'technical_evidence':value}
                authorization['content_hash'] = content_hash(authorization)
                cur.execute('INSERT INTO mission_document_research_controlled_recovery_authorizations VALUES(?,?,?,?,?,?,?)',
                    (key,admission['id'],index+1,work['id'],canonical_json(authorization),
                     authorization['content_hash'],authorization['authorized_at']))
    if authorization is None:
        tomorrow = now.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)
        executor._recovery_observation(admission,work,formal,index,{
            'status':'waiting','reason':DAY_CAP_REASON,'day':_time(now)[:10],'eligible':False,
            'used_fresh_work_orders':len(links),'max_fresh_work_orders':1,
            'retry_at':_time(tomorrow),'deadline':_time(tomorrow),'proof':value})
        return {'status':'waiting','reason':DAY_CAP_REASON,'retry_at':_time(tomorrow),
                'work_order_ref':work['id'],'stage':stage}
    envelope=formal['result_envelope']; accounting=value['failure_accounting']
    proof={'classification':CLASSIFICATION,'failed_at':formal['created_at'],
           'formal_result_ref':_formal_ref(formal),'formal_result_hash':_formal_hash(formal),
           'result_envelope_ref':envelope['id'],'result_envelope_hash':formal['result_envelope_hash'],
           'route_decision_ref':envelope['metadata']['route_decision_ref'],
           'authorization_ref':key,'authorization_hash':authorization['content_hash'],
           'technical_evidence':value,
           'mission_binding_hash':content_hash(_expected_budget_binding(executor.authority,admission,index)),
           'budget_authority_ref':None,'budget_authority_hash':None,
           'budget_settlement_ref':accounting.get('budget_settlement_ref'),
           'budget_settlement_hash':accounting.get('budget_settlement_hash'),'refusal_day':None}
    issued=executor._append_controlled_retry(admission,work,formal,index,links,authorization,proof=proof)
    executor._recovery_observation(admission,work,formal,index,{
        'status':'admitted','reason':CLASSIFICATION,'eligible':False,
        'used_fresh_work_orders':len(links),'max_fresh_work_orders':1,'retry_at':None,
        'deadline':_time(now+timedelta(seconds=_recovery_policy(admission,index)['max_elapsed_seconds'])),
        'proof':proof})
    return {**issued,'reason':CLASSIFICATION,'stage':stage}


def can_resume(connection, admission, work_ref, stage_ordinal):
    """A bounded lane wakeup, not a model-call authorization or send verdict."""
    if admission.get('numeric_authority',{}).get('contract_ref') != CONTRACT_REF or stage_ordinal not in (2,3):
        return False
    row=connection.execute('SELECT work_order_json,work_order_hash FROM scheduler_work_orders WHERE work_order_id=?',
                           (work_ref,)).fetchone()
    if row is None: return False
    work=WorkOrder.from_dict(json.loads(row['work_order_json'])).to_dict()
    meta=work['metadata']
    if (row['work_order_json'] != canonical_json(work) or row['work_order_hash'] != content_hash(work)
            or meta.get('mission_document_research_admission_ref') != admission['id']
            or meta.get('mission_document_research_admission_hash') != admission['content_hash']):
        raise DirectedNumericError('numeric technical lane candidate identity differs')
    return candidate(work,stage_ordinal-1)


def epoch_input(authorized, current_base):
    """Carry only the verified migration input through an existing policy rebind."""
    old=authorized['metadata']; current=current_base['metadata']
    marker=old.get('numeric_technical_maintenance')
    if marker is None: return None
    if (set(marker) != {'repair_version','source_work_ref','source_work_hash','evidence_hash'}
            or marker['repair_version'] != REPAIR_VERSION
            or old.get('numeric_contract_ref') != CONTRACT_REF
            or current.get('numeric_contract_ref') != CONTRACT_REF
            or any(canonical_json(old.get(k)) != canonical_json(current.get(k)) for k in (
                'numeric_targets','numeric_metric_definitions','retrieval_proof','draft','producer_model_family'))):
        raise DirectedNumericError('numeric maintenance policy rebind changes source or target')
    value={'question':authorized['question'],'prompt_hash':old['prompt_hash'],
           'maintenance_marker':marker,'authorized_request_hash':old['model_request_binding_hash'],
           'current_request_hash':current['model_request_binding_hash']}
    if old.get('stage') == 'independent_qualitative_verifier':
        if old.get('verifier_provider_schema_hash') != SUCCESSOR_SCHEMA_HASH:
            raise DirectedNumericError('numeric maintenance policy rebind schema differs')
        value['verifier_provider_schema_hash']=SUCCESSOR_SCHEMA_HASH
    return value


def rebind_input(current_base, record):
    value=record.get('numeric_maintenance_input')
    metadata=dict(current_base['metadata'])
    if value is None: return current_base['question'],metadata
    expected={'question','prompt_hash','maintenance_marker','authorized_request_hash','current_request_hash'}
    if 'verifier_provider_schema_hash' in value: expected.add('verifier_provider_schema_hash')
    if (set(value) != expected or value['prompt_hash'] != content_hash(value['question'])
            or value['current_request_hash'] != metadata['model_request_binding_hash']
            or value['maintenance_marker'].get('repair_version') != REPAIR_VERSION):
        raise DirectedNumericError('numeric maintenance rebound input differs')
    metadata['numeric_technical_maintenance']=value['maintenance_marker']
    metadata['prompt_hash']=value['prompt_hash']
    if 'verifier_provider_schema_hash' in value:
        metadata['verifier_provider_schema_hash']=value['verifier_provider_schema_hash']
    metadata['model_request_binding_hash']=content_hash({
        'authorized_request_hash':value['authorized_request_hash'],
        'current_request_hash':value['current_request_hash'], 'epoch_rebind_hash':record['content_hash']})
    return value['question'],metadata
