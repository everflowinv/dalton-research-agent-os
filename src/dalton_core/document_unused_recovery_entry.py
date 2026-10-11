"""Enter an existing, wholly unused paid-contract recovery; grant no new calls."""
from __future__ import annotations
from contextlib import ExitStack
import sqlite3
from .store import canonical_json, content_hash

CONTRACT = 'document-unused-recovery-entry:0.1'


def qualification(authority, scheduler, admission, work_ref, worker):
    from .directed_numeric_research import CONTRACT_REF
    from .directed_numeric_recovery import read_contract_retry_links
    from .mission_document_research_executor import (
        AUTOMATIC_CONTRACT_RETRY_CLASSIFICATION, AUTOMATIC_UNPROVED_SEND_RETRY_CLASSIFICATION,
        _paid_contract_failure_proof, _terminal_model_failure, _read_recovery_link,
        _recovery_work, _verify_recovery_failure_proof)
    if admission.get('model_authority_refresh') is not None:
        admission = {k:v for k,v in admission.items() if k not in {
            'admitted_model_execution', 'admitted_model_authority', 'model_authority_refresh'}}
        persisted = authority.admission(admission['id'])
        admission['model_execution'] = persisted['model_execution']
        admission['model_authority'] = persisted['model_authority']
    if admission.get('numeric_authority', {}).get('contract_ref') != CONTRACT_REF:
        return None
    stored = scheduler.work_order_authority(work_ref)
    if stored is None:
        return None
    work = stored['work_order'];meta = work['metadata']
    stage = {'qualitative_model_draft': 2, 'independent_qualitative_verifier': 3}.get(meta.get('stage'))
    if (stage is None or meta.get('mission_document_research_admission_ref') != admission['id']
            or meta.get('mission_document_research_admission_hash') != admission['content_hash']):
        return None
    links = read_contract_retry_links(authority.store.connection, admission, stage)
    if not links or links[-1]['recovery_work_order_ref'] != work_ref:
        return None
    link = links[-1]
    if link['failure_proof'].get('classification') not in {
            AUTOMATIC_CONTRACT_RETRY_CLASSIFICATION, AUTOMATIC_UNPROVED_SEND_RETRY_CLASSIFICATION}:
        return None
    # A prior epoch mapping may have already executed while its original Work
    # stays ready. Do not buy another entry against that obsolete placeholder.
    if authority.store.connection.execute(
            'SELECT 1 FROM mission_document_research_model_authority_epoch_rebinds '
            'WHERE recovery_link_ref=? LIMIT 1', (link['id'],)).fetchone() is not None:
        return None
    history = scheduler.attempt_history(work_ref)
    if (scheduler.formal_result(work_ref) is not None or len(history) != 1
            or history[0]['state'] != 'ready'
            or authority.store.connection.execute(
                'SELECT 1 FROM model_invocations WHERE work_order_ref=? LIMIT 1', (work_ref,)).fetchone() is not None
            or authority.store.connection.execute(
                'SELECT 1 FROM scheduler_leases WHERE work_order_id=? LIMIT 1', (work_ref,)).fetchone() is not None):
        return None
    parent = scheduler.work_order_authority(link['failed_work_order_ref'])
    if parent is None:
        return None
    row = authority.store.connection.execute(
        'SELECT * FROM mission_document_research_recovery_links WHERE recovery_link_id=?', (link['id'],)).fetchone()
    checked = _read_recovery_link(authority.store.connection, admission, stage - 1,
                                 row, parent['work_order'], links[:-1])
    expected = _recovery_work(parent['work_order'], checked)
    if (canonical_json(expected) != canonical_json(work)
            or stored['work_order_hash'] != content_hash(expected)):
        raise ValueError('unused recovery Work differs from its existing authorization')
    formal = _terminal_model_failure(scheduler, parent['work_order'])
    paid = (None if formal is None else _paid_contract_failure_proof(
        authority, parent['work_order'], formal, stage - 1, worker))
    # Old late-lease replies could be misclassified as unproved. Require the
    # actual paid contract proof now, while also verifying the original unknown
    # record below. Never rewrite its classification, fee or authorization.
    if paid is None:
        return None
    _verify_recovery_failure_proof(authority, scheduler, admission, stage - 1,
                                  parent['work_order'], checked, worker)
    body = {'contract_ref': CONTRACT, 'admission_ref': admission['id'],
            'admission_hash': admission['content_hash'], 'recovery_link_ref': link['id'],
            'recovery_link_hash': link['content_hash'], 'stage_ordinal': stage,
            'authorized_work_ref': work_ref, 'authorized_work_hash': stored['work_order_hash'],
            'parent_work_ref': parent['work_order']['id'],
            'parent_work_hash': parent['work_order_hash'],
            'failure_proof_hash': content_hash(checked['failure_proof']),
            'paid_contract_proof_hash': content_hash(paid),
            'additional_recovery_windows': 0}
    return {**body, 'content_hash': content_hash(body)}


def candidate(coordinator, admission, ticket_ref, work_ref):
    """Read exact local Router/budget authorities, without mutation constructors."""
    from .mission_document_model_authority import load_mission_document_model_configs
    from .mission_document_research import MissionDocumentResearchAuthority
    from .document_research_qualitative import MissionDocumentDraftWorker, MissionDocumentVerifierWorker
    from .scheduler import Scheduler
    from .model_router import ModelRouter
    from .observability import ObservabilityStore
    from .thesis_impact_budget import ThesisImpactBudgetStore
    from .readonly_sqlite import connect_read_only
    launcher = coordinator.launcher
    ticket = launcher.status(ticket_ref)
    if (ticket.get('status') not in {'failed', 'orphaned'}
            or ticket.get('admission_ref') != admission['id']
            or ticket.get('admission_hash') != admission['content_hash']):
        return None
    scheduler = object.__new__(Scheduler);scheduler.connection = coordinator.store.connection
    stored = scheduler.work_order_authority(work_ref)
    if stored is None:
        return None
    index = {'qualitative_model_draft': 0, 'independent_qualitative_verifier': 1}.get(stored['work_order']['metadata'].get('stage'))
    if index is None:
        return None
    configs = load_mission_document_model_configs(launcher.state_dir,
        draft_path=launcher.draft_model_config_path, verifier_path=launcher.verifier_model_config_path)
    with ExitStack() as stack:
        router = object.__new__(ModelRouter)
        router.connection = connect_read_only(configs[index]['model_router_db'])
        router.connection.row_factory = sqlite3.Row;stack.callback(router.connection.close)
        execution = admission['model_execution']['draft' if index == 0 else 'verifier']
        budget = stack.enter_context(ThesisImpactBudgetStore(execution['budget_db'], read_only=True))
        observability = object.__new__(ObservabilityStore)
        observability.store = coordinator.store;observability.connection = coordinator.store.connection
        authority = object.__new__(MissionDocumentResearchAuthority)
        from .coverage_mission import CoverageMissionAuthority
        from datetime import datetime, timezone
        authority.store = coordinator.store;authority.connection = coordinator.store.connection
        authority.clock = lambda: datetime.now(timezone.utc)
        authority._missions = object.__new__(CoverageMissionAuthority)
        authority._missions.store = coordinator.store;authority._missions.connection = coordinator.store.connection
        cls = MissionDocumentDraftWorker if index == 0 else MissionDocumentVerifierWorker
        worker = object.__new__(cls);worker.router = router;worker.budget_store = budget
        worker.observability = observability;worker.mission_document_research_authority = authority
        return qualification(authority, scheduler, admission, work_ref, worker)
