"""One versioned, local-only archival repair of already paid numeric proofs.

This authority cannot route a model or change targets, usage, prior attempts or
fees. Its single Work uses the existing canonical Figure admission rules.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .contracts import WorkOrder
from .store import canonical_json, content_hash
from .directed_numeric_research import DirectedNumericError

CONTRACT = 'numeric-local-figure-reconciliation:0.1'
ACTOR = 'automation:numeric-local-figure-reconciliation'
TABLE = 'mission_document_numeric_local_reconciliations'


def _row(connection, admission_ref):
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is None:
        return None
    return connection.execute('SELECT * FROM '+TABLE+' WHERE admission_ref=? AND repair_version=?',
                              (admission_ref, CONTRACT)).fetchone()


def _prefix(scheduler, works):
    result = []
    for work in works[:3]:
        stored = scheduler.work_order_authority(work['id'])
        formal = scheduler.formal_result(work['id'])
        if (stored is None or stored['work_order_hash'] != content_hash(work)
                or canonical_json(stored['work_order']) != canonical_json(work)
                or formal is None or formal['terminal_state'] != 'succeeded'):
            raise DirectedNumericError('local numeric reconciliation requires exact succeeded paid prefix')
        result.append({'work_order_ref': work['id'], 'work_order_hash': content_hash(work),
                       'formal_result_ref': formal['result_record_id'],
                       'formal_result_hash': formal['result_envelope_hash']})
    return result


def _derive_work(parent, authorization):
    key = authorization['id']
    metadata = {**parent['metadata'], 'numeric_local_reconciliation_ref': key,
                'numeric_local_reconciliation_hash': authorization['content_hash'],
                'numeric_local_reconciliation_contract': CONTRACT,
                'local_reconciliation_parent_work_ref': parent['id']}
    return WorkOrder.from_dict({**parent,
        'id': 'work:numeric-local-reconciliation:'+key.rsplit(':', 1)[1],
        'created_at': authorization['created_at'], 'updated_at': authorization['created_at'],
        'idempotency_key': key, 'budget': {**parent['budget'], 'max_cost_usd': 0,
                                        'max_attempts': 1, 'step_max_attempts': 1},
        'input_refs': list(dict.fromkeys([*parent['input_refs'], parent['id'], key])),
        'metadata': metadata}).to_dict()


def _parents(admission, scheduler, registry, effective, current):
    """Rebuild the two existing local names; never trust a supplied parent body."""
    from .mission_document_research_executor import _derive, _blueprints
    candidates = [current]
    if 'admitted_model_execution' in admission:
        prior = {k:v for k,v in admission.items() if k not in {
            'admitted_model_execution', 'admitted_model_authority', 'model_authority_refresh'}}
        prior['model_execution'] = admission['admitted_model_execution']
        prior['model_authority'] = admission['admitted_model_authority']
        candidates.append(_derive(prior, scheduler, registry, [*effective, _blueprints(prior)[3]], 3))
    return list({p['id']:p for p in candidates}.values())


def _validate(row, *, admission, scheduler, registry, effective, current):
    value = json.loads(row['record_json'])
    body = dict(value); digest = body.pop('content_hash', None)
    fields = {'schema_version','id','repair_version','actor_ref','requested_by','admission_ref',
              'admission_hash','parent_work_order_ref','parent_work_order_hash','prefix',
              'source_proof_hash','draft_proof_hash','verifier_proof_hash','accounting_hash',
              'repair_evidence','created_at','content_hash'}
    expected_id='numeric-local-reconciliation:'+content_hash({'admission_ref':admission['id'],'repair_version':CONTRACT})[:32]
    if (value.get('id') != expected_id or not isinstance(value.get('requested_by'),str) or not value['requested_by']
            or set(value) != fields or canonical_json(value) != row['record_json']
            or digest != content_hash(body) or digest != row['content_hash']
            or value['schema_version'] != CONTRACT or value['repair_version'] != CONTRACT
            or value['actor_ref'] != ACTOR or value['admission_ref'] != admission['id']
            or value['admission_hash'] != admission['content_hash']
            or row['admission_ref'] != value['admission_ref'] or row['admission_hash'] != value['admission_hash']
            or row['repair_version'] != CONTRACT or row['authorization_ref'] != value['id']
            or row['created_at'] != value['created_at'] or not value['repair_evidence']):
        raise DirectedNumericError('local numeric reconciliation authorization differs')
    parents = _parents(admission, scheduler, registry, effective, current)
    matches = [p for p in parents if p['id']==value['parent_work_order_ref'] and content_hash(p)==value['parent_work_order_hash']]
    if len(matches) != 1:
        # When no refresh exists, the closed candidate list contains one name.
        raise DirectedNumericError('local numeric reconciliation parent no longer replays')
    parent = matches[0]
    stored = scheduler.work_order_authority(parent['id'])
    if (stored is None or canonical_json(stored['work_order']) != canonical_json(parent)
            or parent['budget'].get('max_cost_usd') != 0
            or parent['runtime_profile_ref'] == 'runtime-profile:dalton-model-broker:0.1'
            or value['prefix'] != _prefix(scheduler, effective)):
        raise DirectedNumericError('local numeric reconciliation prefix or local budget differs')
    metadata = parent['metadata']
    proof = registry.verify_search_proof(metadata['retrieval_proof'])
    if (proof['content_hash'] != value['source_proof_hash']
            or metadata['draft_proof']['content_hash'] != value['draft_proof_hash']
            or metadata['verifier_proof']['content_hash'] != value['verifier_proof_hash']):
        raise DirectedNumericError('local numeric reconciliation source/model proofs differ')
    return value, _derive_work(parent, value)


def effective_reconciliation_work(admission, scheduler, registry, effective, current):
    row = _row(scheduler.connection, admission['id'])
    if row is None:
        return current
    _, work = _validate(row, admission=admission, scheduler=scheduler, registry=registry,
                        effective=effective, current=current)
    return work


def _chain(executor, admission):
    from .mission_document_research_executor import _blueprints, _effective_stage, _ordinary_staging_work
    original = _blueprints(admission)
    draft, _ = _effective_stage(executor.authority, executor.scheduler, admission, 1, worker=executor.draft_worker)
    verifier, _ = _effective_stage(executor.authority, executor.scheduler, admission, 2, worker=executor.verifier_worker)
    effective = [original[0], draft, verifier]
    current = _ordinary_staging_work(admission, executor.scheduler, executor.registry, effective)
    return [*effective, current]


def _repair_evidence(executor, admission, works):
    from .directed_numeric_executor import _execution_proof, source_binding
    from .directed_numeric_figure_support import figure_support, NumericMeasureIdentityConflict
    from .claim_index_figures import MissionFigureAuthorityResolver
    proof, draft, verifier, accounting = _execution_proof(executor, admission, works)
    source_binding(executor.connection,admission['request']['registration'],admission['company_ref'])
    resolver = MissionFigureAuthorityResolver(executor.connection)
    targets = {t['target_ref']:t for t in admission['numeric_authority']['targets']}
    accepted = {t['target_ref'] for t in verifier['output']['targets'] if t['verdict']=='accept'}
    evidence = []
    for item in draft['output']['targets']:
        if item['status'] != 'supported' or item['target_ref'] not in accepted or verifier['output']['verdict'] != 'pass':
            continue
        target = targets[item['target_ref']]
        rows = executor.connection.execute('SELECT figure_id FROM coverage_mission_document_figures '
            'WHERE company_ref=? AND document_ref=? AND metric_ref=? AND period=? AND source_manifest_hash=?',
            (admission['company_ref'], admission['request']['registration']['document_ref'], target['metric_ref'],
             target['period'], admission['request']['registration']['manifest_hash'])).fetchall()
        if not rows:
            continue
        if len(rows) != 1:
            raise DirectedNumericError('local reconciliation held Figure is ambiguous')
        figure = resolver.figure(rows[0]['figure_id'])
        try:
            mapping = figure_support(connection=executor.connection, registry=executor.registry,
                admission=admission,target=target,figure=figure,search_proof=proof,draft=draft,verifier=verifier)
            evidence.append({'kind':'exact_figure_support','proof':mapping})
        except NumericMeasureIdentityConflict as exc:
            evidence.append({'kind':'numeric_measure_identity_conflict','proof':exc.proof})
    if not evidence:
        raise DirectedNumericError('no proved Figure-support/measure defect qualifies for local reconciliation')
    return proof, draft, verifier, accounting, evidence


def _inactive(scheduler, work, now):
    status = scheduler.status(work['id'])
    if status['state'] == 'succeeded' or status['attempt_number'] < 1:
        raise DirectedNumericError('local reconciliation requires an unfinished attempted local stage')
    if status['state'] == 'leased':
        row = scheduler.connection.execute('SELECT expires_at FROM scheduler_leases WHERE work_order_id=? '
            'ORDER BY lease_version DESC LIMIT 1', (work['id'],)).fetchone()
        if row is None or datetime.fromisoformat(row['expires_at']).astimezone(timezone.utc) > now.astimezone(timezone.utc):
            raise DirectedNumericError('local numeric stage still has an active lease')
    return status


def qualification(executor, admission_ref, expected_admission_hash):
    """No writes, no provider: derive proof for one exact local maintenance case."""
    admission = executor.authority.resolve_for_execution(admission_ref)
    if admission['content_hash'] != expected_admission_hash or admission.get('numeric_authority') is None:
        raise DirectedNumericError('local reconciliation admission hash/numeric contract differs')
    works = _chain(executor, admission)
    existing = _row(executor.connection, admission_ref)
    if existing is not None:
        auth, work = _validate(existing, admission=admission,scheduler=executor.scheduler,registry=executor.registry,
                               effective=works[:3],current=works[3])
        return admission, [*works[:3], work], auth
    if executor.connection.execute('SELECT 1 FROM mission_document_numeric_results WHERE admission_ref=?', (admission_ref,)).fetchone():
        raise DirectedNumericError('existing numeric result cannot be replaced by local reconciliation')
    parent = None
    for item in _parents(admission,executor.scheduler,executor.registry,works[:3],works[3]):
        stored = executor.scheduler.work_order_authority(item['id'])
        if stored is not None and canonical_json(stored['work_order'])==canonical_json(item):
            parent=item;break
    if parent is None:
        raise DirectedNumericError('local reconciliation has no exact attempted parent Work')
    _inactive(executor.scheduler,parent,executor.clock())
    works[3]=parent
    if parent['budget'].get('max_cost_usd') != 0 or parent['runtime_profile_ref']=='runtime-profile:dalton-model-broker:0.1':
        raise DirectedNumericError('local reconciliation parent is not zero-cost local work')
    proof,draft,verifier,accounting,evidence=_repair_evidence(executor,admission,works)
    return admission, works, {'prefix':_prefix(executor.scheduler,works),
        'source_proof_hash':proof['content_hash'],'draft_proof_hash':draft['content_hash'],
        'verifier_proof_hash':verifier['content_hash'],'accounting_hash':content_hash(accounting),
        'repair_evidence':evidence}


def reconcile(executor, admission_ref, *, expected_admission_hash, requested_by):
    """Official maintenance operation; the sole executable stage is local."""
    from .directed_numeric_executor import complete_numeric_stage, read_numeric_result
    admission, works, candidate = qualification(executor,admission_ref,expected_admission_hash)
    row = _row(executor.connection, admission_ref)
    if row is None:
        now = executor.clock().isoformat()
        parent = works[3]
        body = {'schema_version':CONTRACT,'repair_version':CONTRACT,'actor_ref':ACTOR,
            'requested_by':requested_by,'admission_ref':admission_ref,'admission_hash':expected_admission_hash,
            'parent_work_order_ref':parent['id'],'parent_work_order_hash':content_hash(parent),
            'created_at':now,**candidate}
        body['id']='numeric-local-reconciliation:'+content_hash({'admission_ref':admission_ref,'repair_version':CONTRACT})[:32]
        body['content_hash']=content_hash(body)
        with executor.authority._transaction() as cur:
            _inactive(executor.scheduler,parent,executor.clock())
            if cur.execute('SELECT 1 FROM mission_document_numeric_results WHERE admission_ref=?', (admission_ref,)).fetchone():
                raise DirectedNumericError('numeric result appeared before local reconciliation authorization')
            cur.execute('INSERT OR IGNORE INTO '+TABLE+' VALUES(?,?,?,?,?,?,?)',
                        (admission_ref,CONTRACT,body['id'],expected_admission_hash,canonical_json(body),body['content_hash'],now))
        row = _row(executor.connection, admission_ref)
        auth, work = _validate(row,admission=admission,scheduler=executor.scheduler,registry=executor.registry,
                              effective=works[:3],current=parent)
        works[3]=work
    else:
        auth = candidate
    # Recheck the exact paid proofs and all dual-original evidence every time,
    # including after authorization was durable but enqueue/claim was interrupted.
    _,_,_,accounting,evidence=_repair_evidence(executor,admission,works)
    if content_hash(accounting)!=auth['accounting_hash']:
        raise DirectedNumericError('local reconciliation actual paid accounting changed')
    for binding in auth['repair_evidence']:
        if binding not in evidence:
            raise DirectedNumericError('local reconciliation repair evidence no longer replays')
    queued = executor.scheduler.enqueue(works[3])
    if queued['status']=='conflict':
        raise DirectedNumericError('local reconciliation Work conflicts')
    formal = executor.scheduler.formal_result(works[3]['id'])
    if formal is not None:
        if formal['terminal_state']!='succeeded':
            return {'status':'blocked','reason':'numeric_local_reconciliation_failed',
                    'responsibility':'administrator','work_order_ref':works[3]['id'],'authorization_ref':auth['id']}
        result=read_numeric_result(executor,admission,works,formal)
    else:
        result=complete_numeric_stage(executor,admission,works,failure_as_terminal=True)
    return {**result,'local_reconciliation_ref':auth['id'],'local_reconciliation_hash':auth['content_hash'],
            'model_calls':0}



def verify_reconciliation_work(executor, admission, works):
    from .mission_document_research_executor import _ordinary_staging_work
    current=_ordinary_staging_work(admission,executor.scheduler,executor.registry,works[:3])
    row=_row(executor.connection,admission['id'])
    if row is None:
        raise DirectedNumericError('numeric local reconciliation authority is absent')
    auth,expected=_validate(row,admission=admission,scheduler=executor.scheduler,registry=executor.registry,
                            effective=works[:3],current=current)
    if canonical_json(expected)!=canonical_json(works[3]):
        raise DirectedNumericError('numeric local reconciliation Work does not replay')
    _,_,_,accounting,evidence=_repair_evidence(executor,admission,works)
    if content_hash(accounting)!=auth['accounting_hash'] or any(x not in evidence for x in auth['repair_evidence']):
        raise DirectedNumericError('numeric local reconciliation paid/source evidence does not replay')
    return auth


def writer_reconcile(server, params):
    from .mission_document_research_lane import LAUNCHER_KWARG, _open_runtime
    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        raise DirectedNumericError('document research lane is unavailable for local reconciliation')
    from .lane_child_launcher import ticket_identity
    with launcher._lock:
        for ref, path in list(launcher._running_index.items()):
            ticket = json.loads(path.read_text(encoding='utf-8'))
            if ticket.get('admission_ref') != params['admission_ref']:
                continue
            process = launcher._children.get(ref)
            active = process.poll() is None if process is not None else ticket_identity(ticket,path) is not False
            if active:
                raise DirectedNumericError('document research child must settle before local reconciliation')
        # The launcher uses this same lock for spawning. Keep it through local
        # archival completion, so another thread cannot start this admission
        # after the inactive-child proof and before reconciliation claims its Work.
        runtime = _open_runtime(launcher,params['admission_ref'],params['expected_admission_hash'])
        try:
            return reconcile(runtime.executor,params['admission_ref'],expected_admission_hash=params['expected_admission_hash'],
                             requested_by=params['actor_ref'])
        finally:
            runtime.close()
