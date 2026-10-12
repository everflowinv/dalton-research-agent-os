"""One zero-model follow-up for a proved historical period promotion conflict."""
import json
from .store import canonical_json,content_hash
from .contracts import WorkOrder,ResultEnvelope
from .directed_numeric_research import DirectedNumericError

CONTRACT='numeric-local-promotion-reconciliation:0.1'
REPLAY_CONTRACT='document-completed-model-postprocessing:0.3'
ERROR='IdempotencyConflict: review decision or candidate was already promoted'
MESSAGE='review decision or candidate was already promoted'
ACTOR='automation:numeric-local-promotion-reconciliation'
TABLE='mission_document_numeric_promotion_reconciliations'


def row(connection,admission_ref):
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone()is None:return None
    return connection.execute('SELECT * FROM '+TABLE+' WHERE admission_ref=?',(admission_ref,)).fetchone()


def failure(connection,admission):
    """Closed old local authorization, Work and failed formal; no summary grant."""
    from .directed_numeric_local_reconciliation import _row,CONTRACT as OLD,ACTOR as OLD_ACTOR
    from .scheduler import Scheduler
    from .mission_document_research_executor import _formal_hash
    old=_row(connection,admission['id'])
    if old is None:return None
    auth=json.loads(old['record_json'])
    if (canonical_json(auth)!=old['record_json'] or content_hash({k:v for k,v in auth.items()if k!='content_hash'})!=auth['content_hash']
            or auth['content_hash']!=old['content_hash'] or auth['repair_version']!=OLD or auth['schema_version']!=OLD
            or auth['actor_ref']!=OLD_ACTOR or auth['admission_ref']!=admission['id'] or auth['admission_hash']!=admission['content_hash']
            or old['authorization_ref']!=auth['id'] or old['admission_hash']!=auth['admission_hash']
            or old['repair_version']!=OLD or old['created_at']!=auth['created_at']
            or auth['id']!='numeric-local-reconciliation:'+content_hash({'admission_ref':admission['id'],'repair_version':OLD})[:32]):
        raise DirectedNumericError('promotion-local old authorization differs')
    scheduler=object.__new__(Scheduler);scheduler.connection=connection
    ref='work:numeric-local-reconciliation:'+auth['id'].rsplit(':',1)[1]
    authority=scheduler.work_order_authority(ref);formal=scheduler.formal_result(ref)
    if authority is None or formal is None or formal['terminal_state']!='failed':return None
    work=WorkOrder.from_dict(authority['work_order']).to_dict();envelope=ResultEnvelope.from_dict(formal['result_envelope']).to_dict()
    if (formal['content_hash']!=_formal_hash(formal) or content_hash(envelope)!=formal['result_envelope_hash']
            or envelope['id']!=formal['result_envelope_id'] or envelope['work_order_ref']!=ref or envelope['status']!='failed'
            or work['budget'].get('max_cost_usd')!=0 or work['budget'].get('max_attempts')!=1
            or work['runtime_profile_ref']=='runtime-profile:dalton-model-broker:0.1'
            or envelope['metadata'].get('local_reconciliation_contract')!=OLD
            or work['metadata'].get('numeric_local_reconciliation_ref')!=auth['id']
            or work['metadata'].get('numeric_local_reconciliation_hash')!=auth['content_hash']
            or work['metadata'].get('mission_document_research_admission_ref')!=admission['id']
            or work['metadata'].get('mission_document_research_admission_hash')!=admission['content_hash']):
        raise DirectedNumericError('promotion-local failed formal/Work binding differs')
    if (envelope.get('error',{}).get('code')!='NUMERIC_LOCAL_RECONCILIATION_FAILED'
            or envelope['error'].get('message')!=MESSAGE or envelope['outputs']
            or envelope['actual_side_effects'] or envelope['usage_refs'] or envelope['artifact_refs']):return None
    return {'parent_work_ref':ref,'parent_work_hash':authority['work_order_hash'],
            'failed_formal_hash':formal['content_hash'],'failed_envelope_hash':formal['result_envelope_hash'],
            'original_local_authorization_ref':auth['id'],'original_local_authorization_hash':auth['content_hash']}


def qualification(executor,admission_ref,expected_hash):
    from .directed_numeric_local_reconciliation import qualification as original,_repair_evidence
    from .claim_index_figures import MissionFigureAuthorityResolver,build_figure_candidate
    from .directed_numeric_promotion_replay import replay
    admission=executor.authority.resolve_for_execution(admission_ref)
    if admission['content_hash']!=expected_hash:raise DirectedNumericError('promotion-local admission differs')
    failed=failure(executor.connection,admission)
    if failed is None:raise DirectedNumericError('promotion-local exact failed local formal is absent')
    # Original qualification replays its immutable grant and all three paid stages.
    admission,works,oldauth=original(executor,admission_ref,expected_hash)
    if works[3]['id']!=failed['parent_work_ref'] or content_hash(works[3])!=failed['parent_work_hash']:
        raise DirectedNumericError('promotion-local original parent no longer derives')
    proof,draft,verifier,accounting,supports=_repair_evidence(executor,admission,works)
    if content_hash(accounting)!=oldauth['accounting_hash'] or any(x not in supports for x in oldauth['repair_evidence']):
        raise DirectedNumericError('promotion-local original accounting/source proof changed')
    resolver=MissionFigureAuthorityResolver(executor.connection);mappings=[]
    votes={t['target_ref'] for t in verifier['output']['targets']if t['verdict']=='accept'}
    for item in oldauth['repair_evidence']:
        if item['kind']!='exact_figure_support':raise DirectedNumericError('promotion-local cannot repair a measure conflict')
        target=item['proof']['target_ref']
        if target not in votes or verifier['output']['verdict']!='pass':raise DirectedNumericError('promotion-local target not independently accepted')
        figure=resolver.figure(item['proof']['figure_ref'])
        bundle=build_figure_candidate(executor.connection,figure=figure,actor_ref=admission['actor_ref'],resolver=resolver)
        held=replay(executor.authority.store,bundle=bundle)
        if held is None:raise DirectedNumericError('promotion-local has no exact existing promotion')
        mappings.append({'target_ref':target,'mapping':held['mapping']})
    if not mappings:raise DirectedNumericError('promotion-local has no strong period replay')
    return admission,works,{**failed,'prefix':oldauth['prefix'],'source_proof_hash':proof['content_hash'],
        'draft_proof_hash':draft['content_hash'],'verifier_proof_hash':verifier['content_hash'],
        'accounting_hash':content_hash(accounting),'promotion_mappings':mappings}


def derive(parent,auth):
    metadata={k:v for k,v in parent['metadata'].items()if not k.startswith('numeric_local_reconciliation_')}
    metadata.update(numeric_promotion_reconciliation_ref=auth['id'],numeric_promotion_reconciliation_hash=auth['content_hash'],
                    numeric_promotion_reconciliation_contract=CONTRACT)
    return WorkOrder.from_dict({**parent,'id':'work:numeric-promotion-reconciliation:'+auth['id'].rsplit(':',1)[1],
        'created_at':auth['created_at'],'updated_at':auth['created_at'],'idempotency_key':auth['id'],
        'budget':{**parent['budget'],'max_cost_usd':0,'max_attempts':1,'step_max_attempts':1},
        'input_refs':list(dict.fromkeys([*parent['input_refs'],parent['id'],auth['id']])),'metadata':metadata}).to_dict()


def validate(executor,admission,stored):
    value=json.loads(stored['record_json']);actual,works,proof=qualification(executor,admission['id'],admission['content_hash'])
    body={'contract_ref':CONTRACT,'id':'numeric-promotion-reconciliation:'+content_hash({'admission_ref':admission['id'],'contract_ref':CONTRACT})[:32],
          'actor_ref':ACTOR,'admission_ref':admission['id'],'admission_hash':admission['content_hash'],
          'requested_by':value.get('requested_by'),'created_at':value.get('created_at'),**proof}
    body['content_hash']=content_hash(body)
    if (not isinstance(body['requested_by'],str) or not body['requested_by'] or canonical_json(body)!=canonical_json(value)
            or canonical_json(value)!=stored['record_json'] or stored['content_hash']!=body['content_hash']
            or stored['admission_ref']!=admission['id'] or stored['authorization_ref']!=body['id']
            or stored['created_at']!=body['created_at']):raise DirectedNumericError('promotion-local authorization no longer replays')
    return value,[*works[:3],derive(works[3],value)]


def reconcile(executor,admission_ref,*,expected_admission_hash,requested_by):
    from .directed_numeric_executor import complete_numeric_stage,read_numeric_result
    admission,works,proof=qualification(executor,admission_ref,expected_admission_hash)
    saved=row(executor.connection,admission_ref)
    if saved is None:
        if executor.connection.execute('SELECT 1 FROM mission_document_numeric_results WHERE admission_ref=?',(admission_ref,)).fetchone():
            raise DirectedNumericError('promotion-local cannot replace a numeric result')
        now=executor.clock().isoformat();body={'contract_ref':CONTRACT,
            'id':'numeric-promotion-reconciliation:'+content_hash({'admission_ref':admission_ref,'contract_ref':CONTRACT})[:32],
            'actor_ref':ACTOR,'admission_ref':admission_ref,'admission_hash':expected_admission_hash,
            'requested_by':requested_by,'created_at':now,**proof};body['content_hash']=content_hash(body)
        with executor.authority._transaction()as cur:
            cur.execute('CREATE TABLE IF NOT EXISTS '+TABLE+'(admission_ref TEXT PRIMARY KEY,authorization_ref TEXT NOT NULL UNIQUE,record_json TEXT NOT NULL,content_hash TEXT NOT NULL,created_at TEXT NOT NULL)')
            cur.execute('CREATE TRIGGER IF NOT EXISTS '+TABLE+'_insert_owner BEFORE INSERT ON '+TABLE+" WHEN dalton_mission_document_research_authorized()=0 BEGIN SELECT RAISE(ABORT,'promotion-local requires MissionDocumentResearchAuthority');END")
            for action in ('UPDATE','DELETE'):
                cur.execute('CREATE TRIGGER IF NOT EXISTS '+TABLE+'_'+action.lower()+' BEFORE '+action+' ON '+TABLE+" BEGIN SELECT RAISE(ABORT,'promotion-local is immutable');END")
            qualification(executor,admission_ref,expected_admission_hash)
            if cur.execute('SELECT 1 FROM mission_document_numeric_results WHERE admission_ref=?',(admission_ref,)).fetchone():
                raise DirectedNumericError('numeric result appeared before promotion-local authorization')
            cur.execute('INSERT OR IGNORE INTO '+TABLE+' VALUES(?,?,?,?,?)',(admission_ref,body['id'],canonical_json(body),body['content_hash'],now))
        saved=row(executor.connection,admission_ref)
    auth,works=validate(executor,admission,saved)
    queued=executor.scheduler.enqueue(works[3])
    if queued['status']=='conflict':raise DirectedNumericError('promotion-local Work conflicts')
    formal=executor.scheduler.formal_result(works[3]['id'])
    if formal is None:done=complete_numeric_stage(executor,admission,works,failure_as_terminal=True)
    elif formal['terminal_state']=='succeeded':done=read_numeric_result(executor,admission,works,formal)
    else:return {'status':'blocked','reason':'numeric_promotion_local_failed','work_order_ref':works[3]['id'],'model_calls':0}
    return {**done,'promotion_local_ref':auth['id'],'model_calls':0}


def effective_work(executor,admission):
    saved=row(executor.connection,admission['id'])
    return None if saved is None else validate(executor,admission,saved)[1][3]


def verify_work(executor,admission,works,receipt):
    auth,expected=validate(executor,admission,row(executor.connection,admission['id']))
    binding={k:expected[3]['metadata'][k]for k in ('numeric_promotion_reconciliation_ref','numeric_promotion_reconciliation_hash','numeric_promotion_reconciliation_contract')}
    if canonical_json(works)!=canonical_json(expected) or receipt.get('promotion_reconciliation')!=binding:
        raise DirectedNumericError('promotion-local receipt/Work differs')
    return auth


def replay_candidate(coordinator,admission):
    """A zero-model hint from the exact failed local formal, not weak summaries."""
    from .scheduler import Scheduler
    from .mission_document_research_executor import _formal_hash
    if admission.get('numeric_authority') is None:return None
    if coordinator.store.connection.execute('SELECT 1 FROM mission_document_numeric_results WHERE admission_ref=?',(admission['id'],)).fetchone():return None
    failed=failure(coordinator.store.connection,admission)
    if failed is None:return None
    scope={'contract_ref':REPLAY_CONTRACT,'admission_ref':admission['id'],'admission_hash':admission['content_hash']}
    ticket_ref=coordinator.launcher.TICKET_PREFIX+':'+content_hash(scope)[:24]
    if coordinator.launcher._ticket_path(ticket_ref).exists():return None
    scheduler=object.__new__(Scheduler);scheduler.connection=coordinator.store.connection
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
    body={**scope,'source_local_failure':failed,'completed_stages':stages,'model_calls_allowed':0}
    return {**body,'content_hash':content_hash(body)}
