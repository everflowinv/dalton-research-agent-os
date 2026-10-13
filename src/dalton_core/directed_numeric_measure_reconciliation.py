"""One local supplement to an immutable partial numeric result.

No provider is executable here. The old result/formal/fees remain unchanged;
the new receipt names that exact origin and replays both paid proofs.
"""
from __future__ import annotations
import json
from .store import canonical_json, content_hash
from .contracts import WorkOrder, ResultEnvelope
from .directed_numeric_research import DirectedNumericError

CONTRACT='numeric-local-measure-reconciliation:0.1'
ACTOR='automation:numeric-local-measure-reconciliation'
TABLE='mission_document_numeric_measure_reconciliations'
RESULTS='mission_document_numeric_measure_results'


def _row(connection,admission_ref,table=TABLE):
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() is None:
        return None
    return connection.execute('SELECT * FROM '+table+' WHERE admission_ref=?',(admission_ref,)).fetchone()


def _origin(executor,admission):
    from .directed_numeric_local_reconciliation import _chain,_prefix
    from .directed_numeric_executor import read_numeric_result,_execution_proof
    from .directed_numeric_measure_identity import measure_support
    from .claim_index_figures import MissionFigureAuthorityResolver
    row=executor.connection.execute('SELECT record_json,content_hash FROM mission_document_numeric_results WHERE admission_ref=?',(admission['id'],)).fetchone()
    if row is None:
        raise DirectedNumericError('measure reconciliation requires an existing partial numeric result')
    old=json.loads(row['record_json'])
    works=_chain(executor,admission)
    parent=executor.scheduler.work_order_authority(old['work_order_ref'])
    if parent is None or parent['work_order_hash']!=old['work_order_hash']:
        raise DirectedNumericError('measure reconciliation origin Work differs')
    works[3]=parent['work_order']
    prior=read_numeric_result(executor,admission,works,executor.scheduler.formal_result(works[3]['id']),include_supplement=False)['numeric_result']
    if prior['status']!='waiting_evidence' or not prior['pending_targets']:
        raise DirectedNumericError('completed numeric result cannot be replaced by measure reconciliation')
    if works[3]['budget']['max_cost_usd']!=0 or works[3]['runtime_profile_ref']=='runtime-profile:dalton-model-broker:0.1':
        raise DirectedNumericError('measure reconciliation origin is not local')
    proof,draft,verifier,accounting=_execution_proof(executor,admission,works)
    targets={t['target_ref']:t for t in admission['numeric_authority']['targets']}
    resolver=MissionFigureAuthorityResolver(executor.connection)
    mappings=[]
    for item in prior['pending_targets']:
        if 'measure_conflict_proof' not in item:
            continue
        held=resolver.figure(item['measure_conflict_proof']['figure_ref'])
        mapping=measure_support(connection=executor.connection,registry=executor.registry,admission=admission,
            target=targets[item['target_ref']],figure=held,search_proof=proof,draft=draft,verifier=verifier)
        if mapping['conflict_proof']!=item['measure_conflict_proof']:
            raise DirectedNumericError('measure reconciliation origin conflict proof differs')
        mappings.append({'target_ref':item['target_ref'],'mapping':mapping})
    if not mappings:
        raise DirectedNumericError('partial result has no independently proved distinct source measure')
    return works,prior,{'prefix':_prefix(executor.scheduler,works),
        'prior_numeric_result_hash':prior['content_hash'],'parent_work_ref':works[3]['id'],
        'parent_work_hash':content_hash(works[3]),'source_proof_hash':proof['content_hash'],
        'draft_proof_hash':draft['content_hash'],'verifier_proof_hash':verifier['content_hash'],
        'accounting_hash':content_hash(accounting),'source_measures':mappings}


def _work(parent,auth):
    metadata={k:v for k,v in parent['metadata'].items() if k not in {
        'numeric_local_reconciliation_ref','numeric_local_reconciliation_hash','numeric_local_reconciliation_contract'}}
    metadata.update(numeric_measure_reconciliation_ref=auth['id'],numeric_measure_reconciliation_hash=auth['content_hash'],
                    numeric_measure_reconciliation_contract=CONTRACT)
    return WorkOrder.from_dict({**parent,'id':'work:'+auth['id'],'created_at':auth['created_at'],
        'updated_at':auth['created_at'],'idempotency_key':auth['id'],
        'input_refs':list(dict.fromkeys([*parent['input_refs'],parent['id'],auth['id']])),
        'budget':{**parent['budget'],'max_cost_usd':0,'max_attempts':1,'step_max_attempts':1},
        'metadata':metadata}).to_dict()


def _validate(executor,admission,row):
    works,prior,candidate=_origin(executor,admission)
    auth=json.loads(row['record_json']);body={k:v for k,v in auth.items() if k!='content_hash'}
    identity='numeric-measure-reconciliation:'+content_hash({'admission_ref':admission['id'],'contract':CONTRACT})[:32]
    expected_fields=set(candidate)|{'schema_version','id','actor_ref','requested_by','admission_ref','admission_hash','created_at','content_hash'}
    if (set(auth)!=expected_fields or auth['id']!=identity or auth['schema_version']!=CONTRACT
            or auth['actor_ref']!=ACTOR or not isinstance(auth['requested_by'],str) or not auth['requested_by']
            or auth['admission_ref']!=admission['id'] or auth['admission_hash']!=admission['content_hash']
            or any(auth[k]!=v for k,v in candidate.items())
            or auth['content_hash']!=row['content_hash'] or content_hash(body)!=auth['content_hash']
            or canonical_json(auth)!=row['record_json'] or row['admission_hash']!=admission['content_hash']
            or row['authorization_ref']!=identity or row['created_at']!=auth['created_at']):
        raise DirectedNumericError('measure reconciliation authorization does not replay')
    return auth,[*works[:3],_work(works[3],auth)],prior


def effective_work(executor,admission):
    row=_row(executor.connection,admission['id'])
    if row is None:return None
    return _validate(executor,admission,row)[1]


def verify_work(executor,admission,works,receipt):
    row=_row(executor.connection,admission['id'])
    if row is None:raise DirectedNumericError('measure reconciliation authority is absent')
    auth,expected,prior=_validate(executor,admission,row)
    if canonical_json(works)!=canonical_json(expected) or receipt.get('measure_reconciliation')!={
            'authorization_ref':auth['id'],'authorization_hash':auth['content_hash'],
            'contract_ref':CONTRACT,'prior_numeric_result_hash':prior['content_hash']}:
        raise DirectedNumericError('measure reconciliation receipt/Work differs')
    if receipt.get('source_measures')!=auth['source_measures']:
        raise DirectedNumericError('measure reconciliation receipt drops proved measures')
    old_completed={x['target_ref']:x for x in prior['completed_targets']}
    current={x['target_ref']:x for x in receipt['completed_targets']}
    if any(current.get(k)!=v for k,v in old_completed.items()):
        raise DirectedNumericError('measure reconciliation changed an already completed target')
    return auth


def read_result(executor,admission,works,formal):
    from .directed_numeric_executor import read_numeric_result
    row=_row(executor.connection,admission['id'],RESULTS)
    if row is None:raise DirectedNumericError('measure reconciliation supplement is absent')
    return read_numeric_result(executor,admission,works,formal,receipt_row=row)


def reconcile(executor,admission_ref,*,expected_admission_hash,requested_by):
    from .directed_numeric_executor import _record_numeric,read_numeric_result
    admission=executor.authority.resolve_for_execution(admission_ref)
    if admission['content_hash']!=expected_admission_hash:
        raise DirectedNumericError('measure reconciliation admission hash differs')
    row=_row(executor.connection,admission_ref)
    if row is None:
        origin,prior,candidate=_origin(executor,admission)
        body={'schema_version':CONTRACT,'actor_ref':ACTOR,'requested_by':requested_by,
            'admission_ref':admission_ref,'admission_hash':expected_admission_hash,
            'created_at':executor.clock().isoformat(),**candidate}
        body['id']='numeric-measure-reconciliation:'+content_hash({'admission_ref':admission_ref,'contract':CONTRACT})[:32]
        body['content_hash']=content_hash(body)
        with executor.authority._transaction() as cur:
            # Revalidate the immutable origin inside the unique authorization CAS.
            _,_,fresh=_origin(executor,admission)
            if candidate!=fresh:raise DirectedNumericError('measure origin changed before authorization')
            cur.execute('INSERT OR IGNORE INTO '+TABLE+' VALUES(?,?,?,?,?,?)',
                (admission_ref,body['id'],expected_admission_hash,canonical_json(body),body['content_hash'],body['created_at']))
        row=_row(executor.connection,admission_ref)
    auth,works,prior=_validate(executor,admission,row)
    queued=executor.scheduler.enqueue(works[3])
    if queued['status']=='conflict':raise DirectedNumericError('measure reconciliation Work conflicts')
    formal=executor.scheduler.formal_result(works[3]['id'])
    if formal is not None:
        if formal['terminal_state']!='succeeded':
            return {'status':'blocked','reason':'numeric_measure_reconciliation_failed','responsibility':'administrator','model_calls':0}
        return {**read_result(executor,admission,works,formal),'model_calls':0}
    lease=executor.scheduler.claim(executor.actor_ref,work_order_id=works[3]['id'])
    if lease is None:return {'status':'pending','work_order_ref':works[3]['id'],'model_calls':0}
    executor.scheduler.validate_lease_for_use(works[3]['id'],lease['attempt']['attempt_number'],executor.actor_ref,
        lease['lease_token'],lease_revision_ref=lease['lease']['id'],lease_hash=lease['lease']['content_hash'],work_order_hash=lease['work_order_hash'])
    try:
        saved=_row(executor.connection,admission_ref,RESULTS)
        if saved is None:
            result=_record_numeric(executor,admission,works,measure_repair=True)
            result['measure_reconciliation']={'authorization_ref':auth['id'],'authorization_hash':auth['content_hash'],
                'contract_ref':CONTRACT,'prior_numeric_result_hash':prior['content_hash']}
            result['content_hash']=content_hash({k:v for k,v in result.items() if k!='content_hash'})
            verify_work(executor,admission,works,result)
            with executor._transaction() as cur:
                cur.execute('INSERT INTO '+RESULTS+' VALUES(?,?,?,?,?,?)',(admission_ref,expected_admission_hash,
                    works[3]['id'],canonical_json(result),result['content_hash'],result['created_at']))
        else:result=json.loads(saved['record_json'])
        envelope=ResultEnvelope(schema_version='0.1',id='result-envelope:measure-reconciliation:'+result['content_hash'][:32],
            created_at=result['created_at'],work_order_ref=works[3]['id'],invocation_ref='execution:'+works[3]['id'],
            status='succeeded',outputs=result,actual_side_effects=(),usage_refs=(),artifact_refs=(),error=None,
            metadata={'authority_ref':admission_ref,'measure_reconciliation_contract':CONTRACT}).to_dict()
    except Exception as exc:
        envelope=ResultEnvelope(schema_version='0.1',id='result-envelope:measure-reconciliation-failed:'+content_hash(works[3])[:32],
            created_at=executor.clock().isoformat(),work_order_ref=works[3]['id'],invocation_ref='execution:'+works[3]['id'],
            status='failed',outputs={},actual_side_effects=(),usage_refs=(),artifact_refs=(),
            error={'code':'NUMERIC_MEASURE_RECONCILIATION_FAILED','message':str(exc)[:500],'retryable':False},
            metadata={'authority_ref':admission_ref}).to_dict()
        executor.scheduler.complete(works[3]['id'],lease['attempt']['attempt_number'],executor.actor_ref,
            lease['lease_token'],envelope,idempotency_key=works[3]['id']+':failed',result_envelope_hash=content_hash(envelope))
        raise
    executor.scheduler.complete(works[3]['id'],lease['attempt']['attempt_number'],executor.actor_ref,
        lease['lease_token'],envelope,idempotency_key=works[3]['id']+':complete',result_envelope_hash=content_hash(envelope))
    return {**read_result(executor,admission,works,executor.scheduler.formal_result(works[3]['id'])),'model_calls':0}


def effective_result(executor,admission):
    row=_row(executor.connection,admission['id'])
    if row is None:return None
    auth,works,prior=_validate(executor,admission,row)
    formal=executor.scheduler.formal_result(works[3]['id'])
    if formal is None or formal['terminal_state']!='succeeded':return None
    return read_result(executor,admission,works,formal)


def feedback_supplement(connection,admission,prior):
    """Bounded authority-only projection; no source fetch or writer constructor.

    Execution/read_result re-read the registered originals. This projection
    additionally rechecks their sealed retrieval/formal proofs and each exact
    immutable Figure, rather than trusting a maintenance summary string.
    """
    from .scheduler import Scheduler
    from .mission_document_research_executor import _formal_hash
    from .claim_index_figures import MissionFigureAuthorityResolver
    from .directed_numeric_measure_identity import CONTRACT as measure_contract,_row_identity
    from .directed_numeric_figure_support import FACT_FIELDS
    from .directed_numeric_output import quotes
    from .directed_numeric_research import coverage
    row=_row(connection,admission['id']);saved=_row(connection,admission['id'],RESULTS)
    if row is None or saved is None:return None
    auth=json.loads(row['record_json']);value=json.loads(saved['record_json'])
    scheduler=object.__new__(Scheduler);scheduler.connection=connection
    old=scheduler.work_order_authority(prior['work_order_ref'])
    expected_id='numeric-measure-reconciliation:'+content_hash({'admission_ref':admission['id'],'contract':CONTRACT})[:32]
    if (old is None or old['work_order_hash']!=prior['work_order_hash']
            or auth['id']!=expected_id or row['authorization_ref']!=expected_id
            or auth['schema_version']!=CONTRACT or auth['actor_ref']!=ACTOR
            or auth['admission_ref']!=admission['id'] or auth['admission_hash']!=admission['content_hash']
            or row['admission_hash']!=admission['content_hash'] or row['created_at']!=auth['created_at']
            or auth['prior_numeric_result_hash']!=prior['content_hash']
            or auth['parent_work_ref']!=prior['work_order_ref'] or auth['parent_work_hash']!=prior['work_order_hash']
            or canonical_json(auth)!=row['record_json'] or auth['content_hash']!=row['content_hash']
            or auth['content_hash']!=content_hash({k:v for k,v in auth.items() if k!='content_hash'})):
        raise DirectedNumericError('measure feedback authority differs')
    expected=_work(old['work_order'],auth);stored=scheduler.work_order_authority(expected['id'])
    formal=scheduler.formal_result(expected['id'])
    if formal is None:return None
    if (stored is None or stored['work_order_hash']!=content_hash(expected) or stored['work_order']!=expected
            or formal['terminal_state']!='succeeded' or formal['content_hash']!=_formal_hash(formal)
            or content_hash(formal['result_envelope'])!=formal['result_envelope_hash']
            or formal['result_envelope']['outputs']!=value
            or canonical_json(value)!=saved['record_json'] or value['content_hash']!=saved['content_hash']
            or value['content_hash']!=content_hash({k:v for k,v in value.items() if k!='content_hash'})
            or value['admission_ref']!=admission['id'] or value['admission_hash']!=admission['content_hash']
            or value['numeric_authority']!=prior['numeric_authority']
            or value['work_order_ref']!=expected['id'] or value['work_order_hash']!=content_hash(expected)
            or value['source_binding']!=prior['source_binding']
            or value['measure_reconciliation']!={'authorization_ref':auth['id'],'authorization_hash':auth['content_hash'],
                'contract_ref':CONTRACT,'prior_numeric_result_hash':prior['content_hash']}
            or value.get('source_measure_contract')!=measure_contract or value.get('source_measures')!=auth['source_measures']):
        raise DirectedNumericError('measure feedback supplement differs')
    if len(auth['prefix'])!=3:
        raise DirectedNumericError('measure feedback lacks three formally complete original stages')
    for prefix in auth['prefix']:
        work=scheduler.work_order_authority(prefix['work_order_ref']);result=scheduler.formal_result(prefix['work_order_ref'])
        if (work is None or work['work_order_hash']!=prefix['work_order_hash'] or result is None
                or result['terminal_state']!='succeeded' or result['result_record_id']!=prefix['formal_result_ref']
                or result['content_hash']!=_formal_hash(result) or result['result_envelope_hash']!=prefix['formal_result_hash']
                or content_hash(result['result_envelope'])!=result['result_envelope_hash']):
            raise DirectedNumericError('measure feedback original formal stage differs')
    metadata=old['work_order']['metadata'];proof=metadata['retrieval_proof']
    for key,field in (('source_proof_hash','retrieval_proof'),('draft_proof_hash','draft_proof'),('verifier_proof_hash','verifier_proof')):
        held=metadata[field]
        if (auth[key]!=prior[key] or value[key]!=prior[key] or held['content_hash']!=prior[key]
                or held['content_hash']!=content_hash({k:v for k,v in held.items() if k!='content_hash'})):
            raise DirectedNumericError('measure feedback source/model proof differs')
    if value['model_accounting_proofs']!=prior['model_accounting_proofs'] or auth['accounting_hash']!=content_hash(prior['model_accounting_proofs']):
        raise DirectedNumericError('measure feedback changed historical paid accounting')
    pending={p['target_ref']:p for p in prior['pending_targets']}
    completed={p['target_ref']:p for p in value['completed_targets']}
    if any(completed.get(p['target_ref'])!=p for p in prior['completed_targets']):
        raise DirectedNumericError('measure feedback changed an already completed target')
    resolver=MissionFigureAuthorityResolver(connection)
    figures=[]
    for item in auth['source_measures']:
        ref=item['target_ref'];mapping=item['mapping'];conflict=pending[ref]['measure_conflict_proof']
        figure=resolver.figure(completed[ref]['figure_ref']);resolver.verify_figure(figure)
        held=resolver.figure(conflict['figure_ref']);resolver.verify_figure(held)
        new=conflict['verified_answer'];old_fact=conflict['held_fact']
        old_row=_row_identity(held['citation_text'],old_fact);new_row=_row_identity(quotes(proof)[conflict['answer_quote_id']],new)
        identity={'schema_version':measure_contract,'company_ref':admission['company_ref'],
            'document_ref':conflict['document_ref'],'source_manifest_hash':conflict['source_manifest_hash'],
            **{k:new[k] for k in ('metric_ref','period','unit','currency','scale','basis')},'reported_measure':new_row['label']}
        rebuilt={'schema_version':measure_contract,'measure_identity':identity,'measure_identity_hash':content_hash(identity),
            'figure_ref':'mission-document-figure:source-measure:'+content_hash(identity)[:32],
            'conflict_proof':conflict,'held_source_row':old_row,'answer_source_row':new_row}
        rebuilt['content_hash']=content_hash(rebuilt)
        if (mapping!=rebuilt or old_row['label']==new_row['label'] or old_row['text_hash']==new_row['text_hash']
                or set(conflict['differing_fields'])-{'as_reported_label','value'} or 'as_reported_label' not in conflict['differing_fields']
                or figure['figure_id']!=mapping['figure_ref'] or figure['content_hash']!=completed[ref]['figure_hash']
                or any(figure[k]!=new[k] for k in FACT_FIELDS) or figure['quote_id']!=conflict['answer_quote_id']
                or figure['citation_text']!=quotes(proof)[figure['quote_id']]):
            raise DirectedNumericError('measure feedback reported row mapping differs')
    targets={t['target_ref']:t for t in value['numeric_authority']['targets']}
    for item in value['completed_targets']:
        figure=resolver.figure(item['figure_ref']);resolver.verify_figure(figure)
        figures.append({**figure,'metric_definition_hash':item['metric_definition_hash']})
    verified=coverage(list(targets.values()),figures,company_ref=admission['company_ref'],
        document_ref=value['source_binding']['document_ref'],source_manifest_hash=value['source_binding']['source_manifest_hash'])
    if (value['status']!=verified['status'] or value['completed_targets']!=verified['completed_targets']
            or {p['target_ref'] for p in value['pending_targets']}!={p['target_ref'] for p in verified['pending_targets']}):
        raise DirectedNumericError('measure feedback target coverage differs')
    return value


REPLAY_CONTRACT='document-source-measure-postprocessing:0.1'


def replay_candidate(coordinator,admission):
    """A hint to one local-only child; actual authorization is re-proved there."""
    from .directed_numeric_research import pending_numeric_result
    from .scheduler import Scheduler
    from .mission_document_research_executor import _formal_hash
    from .claim_index_figures import MissionFigureAuthorityResolver
    from .directed_numeric_measure_identity import _row_identity
    from .directed_numeric_output import quotes
    if _row(coordinator.store.connection,admission['id']) is not None:return None
    pending=pending_numeric_result(coordinator.store.connection,admission['id'])
    if pending is None or pending['admission_hash']!=admission['content_hash']:return None
    prior=pending['result'];conflicts=[p for p in prior['pending_targets'] if 'measure_conflict_proof' in p]
    if not conflicts:return None
    scope={'contract_ref':REPLAY_CONTRACT,'admission_ref':admission['id'],'admission_hash':admission['content_hash']}
    ticket_ref=coordinator.launcher.TICKET_PREFIX+':'+content_hash(scope)[:24]
    if coordinator.launcher._ticket_path(ticket_ref).exists():return None
    scheduler=object.__new__(Scheduler);scheduler.connection=coordinator.store.connection
    held=scheduler.work_order_authority(prior['work_order_ref'])
    if held is None or held['work_order_hash']!=prior['work_order_hash']:return None
    metadata=held['work_order']['metadata']
    resolver=MissionFigureAuthorityResolver(coordinator.store.connection)
    for item in conflicts:
        conflict=item['measure_conflict_proof'];original=resolver.figure(conflict['figure_ref']);resolver.verify_figure(original)
        old,new=conflict['held_fact'],conflict['verified_answer']
        old_row=_row_identity(original['citation_text'],old)
        new_row=_row_identity(quotes(metadata['retrieval_proof'])[conflict['answer_quote_id']],new)
        if (set(conflict['differing_fields'])-{'as_reported_label','value'} or 'as_reported_label' not in conflict['differing_fields']
                or old_row['label']==new_row['label'] or old_row['text_hash']==new_row['text_hash']
                or conflict['figure_hash']!=original['content_hash']
                or conflict['content_hash']!=content_hash({k:v for k,v in conflict.items() if k!='content_hash'})):
            return None
    stages=[]
    for ref in coordinator._effective_work_hints(admission)[:3]:
        stored=scheduler.work_order_authority(ref);formal=scheduler.formal_result(ref)
        if (stored is None or formal is None or formal['terminal_state']!='succeeded'
                or formal['content_hash']!=_formal_hash(formal)
                or formal['result_envelope_hash']!=content_hash(formal['result_envelope'])
                or stored['work_order']['metadata'].get('mission_document_research_admission_ref')!=admission['id']
                or stored['work_order']['metadata'].get('mission_document_research_admission_hash')!=admission['content_hash']):return None
        stages.append({'work_ref':ref,'work_hash':stored['work_order_hash'],'formal_hash':formal['content_hash']})
    if len(stages)!=3:return None
    body={**scope,'prior_numeric_result_hash':prior['content_hash'],'completed_stages':stages,'model_calls_allowed':0}
    return {**body,'content_hash':content_hash(body)}
