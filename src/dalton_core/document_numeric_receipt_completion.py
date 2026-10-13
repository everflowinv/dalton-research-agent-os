"""One free child to finish an already durable local numeric result.

This does not issue a Work or extend Scheduler attempts. The child reuses the
original local authority, fully verifies source/paid/canonical proofs, and must
claim the same Work under its original frozen Scheduler policy.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from .store import canonical_json, content_hash

CONTRACT = 'document-completed-local-receipt:0.1'
ERROR = 'LeaseExpired: late completion rejected after lease expiry'


def _event_hash(row):
    if row['wire_version'] != '0.2':
        return None
    wire = {'schema_version':'0.1', 'wire_version':'0.2',
        'id':row['event_id'], 'created_at':row['created_at'],
        'work_order_ref':row['work_order_id'], 'attempt_number':row['attempt_number'],
        'state':row['state'], 'lease_ref':row['lease_revision_id'],
        'result_envelope_ref':row['result_envelope_id'],
        'result_envelope_hash':row['result_envelope_hash'], 'reason':row['reason'],
        'not_before':row['not_before'], 'prior_event_ref':row['prior_event_id']}
    return content_hash(wire)


def candidate(coordinator, admission, *, original_proof, ticket_ref):
    from .scheduler import Scheduler
    from .directed_numeric_local_reconciliation import _row, _derive_work, CONTRACT as LOCAL
    from .directed_numeric_executor import _read_receipt
    ticket = coordinator.launcher.status(ticket_ref)
    summary = ticket.get('summary')
    if (ticket.get('id') != ticket_ref or ticket.get('status') != 'failed'
            or ticket.get('exit_code') != 1 or not ticket.get('completed_at')
            or ticket.get('completed_model_replay') != original_proof
            or ticket.get('admission_ref') != admission['id']
            or ticket.get('admission_hash') != admission['content_hash']
            or not isinstance(summary, Mapping) or summary.get('error') != ERROR
            or summary.get('status') != 'failed'
            or summary.get('admission_ref') != admission['id']
            or summary.get('admission_hash') != admission['content_hash']
            or summary.get('content_hash') != content_hash({k:v for k,v in summary.items() if k != 'content_hash'})):
        return None
    path = coordinator.launcher._ticket_path(ticket_ref).with_name('summary.json')
    if path.is_symlink():return None
    raw = path.read_bytes()
    if json.loads(raw) != summary:return None
    connection = coordinator.store.connection
    row = _row(connection, admission['id'])
    if row is None:return None
    auth = json.loads(row['record_json'])
    if (canonical_json(auth) != row['record_json']
            or auth['content_hash'] != row['content_hash']
            or auth['content_hash'] != content_hash({k:v for k,v in auth.items() if k != 'content_hash'})
            or auth['admission_ref'] != admission['id'] or auth['admission_hash'] != admission['content_hash']
            or auth['repair_version'] != LOCAL or auth['schema_version'] != LOCAL
            or row['authorization_ref'] != auth['id'] or row['admission_ref'] != auth['admission_ref']
            or row['admission_hash'] != auth['admission_hash'] or row['repair_version'] != LOCAL
            or row['created_at'] != auth['created_at']
            or auth['id'] != 'numeric-local-reconciliation:'+content_hash({'admission_ref':admission['id'],'repair_version':LOCAL})[:32]):
        raise ValueError('durable numeric completion authority differs')
    scheduler = Scheduler.__new__(Scheduler);scheduler.connection = connection
    parent = scheduler.work_order_authority(auth['parent_work_order_ref'])
    if parent is None or parent['work_order_hash'] != auth['parent_work_order_hash']:return None
    work = _derive_work(parent['work_order'], auth)
    stored = scheduler.work_order_authority(work['id'])
    if (stored is None or stored['work_order'] != work or stored['work_order_hash'] != content_hash(work)
            or work['runtime_profile_ref'] != 'runtime-profile:dalton-local-python:0.1'
            or work['budget']['max_cost_usd'] != 0 or work['requested_capabilities']
            or scheduler.formal_result(work['id']) is not None):
        return None
    history = scheduler.attempt_history(work['id'])
    if len(history) < 2:return None
    expired, ready = history[-2:]
    if (expired['state'] != 'expired' or expired['reason'] != 'lease_expired'
            or ready['state'] != 'ready' or ready['reason'] != 'retry_after_attempt_'+str(expired['attempt_number'])
            or ready['prior_event_id'] != expired['event_id']
            or ready['attempt_number'] != expired['attempt_number'] + 1
            or any(item['content_hash'] != _event_hash(item) for item in (expired, ready))):
        return None
    receipt_row = connection.execute('SELECT record_json,content_hash FROM mission_document_numeric_results WHERE admission_ref=?',(admission['id'],)).fetchone()
    if receipt_row is None:return None
    receipt = _read_receipt(receipt_row, admission, [None,None,None,work])
    if receipt.get('local_reconciliation') != {k:work['metadata'][k] for k in
            ('numeric_local_reconciliation_ref','numeric_local_reconciliation_hash','numeric_local_reconciliation_contract')}:
        raise ValueError('durable numeric completion local binding differs')
    # Eligibility is not completion: the child still verifies every current
    # source/canonical/fee proof and obtains a fresh ordinary Scheduler lease.
    body = {'contract_ref':CONTRACT, 'admission_ref':admission['id'],
        'admission_hash':admission['content_hash'], 'source_ticket_ref':ticket_ref,
        'source_summary_sha256':hashlib.sha256(raw).hexdigest(),
        'original_replay_proof':original_proof, 'work_order_ref':work['id'],
        'work_order_hash':content_hash(work), 'local_authority_ref':auth['id'],
        'local_authority_hash':auth['content_hash'], 'numeric_receipt_hash':receipt['content_hash'],
        'ready_event_ref':ready['event_id'], 'ready_event_hash':ready['content_hash'],
        'model_calls_allowed':0, 'additional_scheduler_attempts':0}
    scope = {k:body[k] for k in ('contract_ref','admission_ref','admission_hash')}
    fixed = coordinator.launcher.TICKET_PREFIX+':'+content_hash(scope)[:24]
    if coordinator.launcher._ticket_path(fixed).exists():return None
    return {**body,'content_hash':content_hash(body)}
