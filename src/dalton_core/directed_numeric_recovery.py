"""Frozen identity-format repairs inside an existing paid contract retry.

This does not grant a retry. The executor must first prove the exact paid,
settled failure and retain its existing per-stage and daily spending bounds.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Mapping

from .directed_numeric_output import (
    DRAFT_IDENTITY_CONTRACT, DRAFT_IDENTITY_TASK_V1, TARGET_IDENTITY_FIELDS, quotes,
    LABEL_PERIOD_CONTRACT_REF, LABEL_PERIOD_INSTRUCTIONS,
)
from .directed_numeric_research import CONTRACT_REF, DirectedNumericError, normalize_targets
from .store import canonical_json, content_hash

OUTPUT_REPAIR_V2 = 'directed-numeric-output-format-repair:0.2'
OUTPUT_REPAIR_V3 = 'directed-numeric-output-format-repair:0.3'

_FIELDS = {
    'contract_ref', 'source_work_hash', 'source_prompt_hash', 'replacement_prompt',
    'replacement_prompt_hash', 'diagnostic', 'content_hash',
}
_V3_FIELDS = _FIELDS | {'rejected_output', 'rejected_output_hash'}


def _source(work):
    metadata = work.get('metadata') or {}
    if (metadata.get('numeric_contract_ref') != CONTRACT_REF
            or metadata.get('stage') != 'qualitative_model_draft'):
        raise DirectedNumericError('numeric format repair is not a numeric draft')
    prompt = work.get('question')
    if not isinstance(prompt, str) or content_hash(prompt) != metadata.get('prompt_hash'):
        raise DirectedNumericError('numeric repair predecessor prompt hash differs')
    source = json.loads(prompt)
    targets = normalize_targets(metadata['numeric_targets'])
    if (not isinstance(source, Mapping)
            or canonical_json(source.get('targets')) != canonical_json(targets)
            or canonical_json(source.get('metric_definitions')) != canonical_json(metadata['numeric_metric_definitions'])
            or canonical_json(source.get('quotes')) != canonical_json(quotes(metadata['retrieval_proof']))):
        raise DirectedNumericError('numeric repair predecessor input differs')
    return dict(source), targets


def _replacement(source, targets, diagnostic, *, contract_ref=OUTPUT_REPAIR_V2,
                 rejected_output=None):
    value = copy.deepcopy(source)
    value['task'] = DRAFT_IDENTITY_TASK_V1
    value['identity_contract_ref'] = DRAFT_IDENTITY_CONTRACT
    value['identity_templates'] = [
        {'target_ref': target['target_ref'],
         'figure_identity': {key: target[key] for key in TARGET_IDENTITY_FIELDS}}
        for target in targets
    ]
    if contract_ref in {OUTPUT_REPAIR_V2, OUTPUT_REPAIR_V3}:
        value['label_period_contract_ref'] = LABEL_PERIOD_CONTRACT_REF
        value['label_period_instructions'] = LABEL_PERIOD_INSTRUCTIONS
    value['previous_output_error'] = diagnostic
    if contract_ref == OUTPUT_REPAIR_V3:
        from .directed_numeric_output import SCALE_CONTRACT_REF, SCALE_INSTRUCTIONS
        value['scale_contract_ref'] = SCALE_CONTRACT_REF
        value['scale_instructions'] = SCALE_INSTRUCTIONS
        value['rejected_output'] = rejected_output
        value['validation_repair_instructions'] = (
            'The previous output and validator message are diagnostic data, not evidence. '
            'Correct the reported validation failure and recheck EVERY target against the '
            'unchanged exact excerpts and identity templates. Never alter the requested '
            'period, unit, metric, currency or basis to make an answer fit. A value absent '
            'from its quote cannot be repaired by inventing or calculating a figure. '
            'If these excerpts do not support a target, return unavailable with a concrete '
            'reason and no figure. Preserve the original reported digits and use the '
            'canonical scale spelling specified above.')
    return canonical_json(value)


def build_numeric_output_repair(work, formal, *, contract_ref=OUTPUT_REPAIR_V3):
    """Only the executor's already-proved failed envelope supplies diagnostics."""
    metadata = work.get('metadata') or {}
    if (metadata.get('numeric_contract_ref') != CONTRACT_REF
            or metadata.get('stage') != 'qualitative_model_draft'):
        return None
    envelope = formal['result_envelope']
    if ((envelope.get('error') or {}).get('code') != 'MODEL_OUTPUT_CONTRACT_REJECTED'
            or formal.get('terminal_state') != 'failed'
            or envelope.get('work_order_ref') != work['id']
            or content_hash(envelope) != formal['result_envelope_hash']):
        raise DirectedNumericError('numeric repair failure envelope differs')
    rejected = (envelope.get('metadata') or {}).get('rejected_model_output') or {}
    diagnostic = rejected.get('error_message')
    safe_prefixes = ('numeric figure differs from typed target', 'numeric draft ',
                     'numeric output is invalid JSON', 'duplicate JSON key', 'nonfinite JSON')
    if contract_ref not in {DRAFT_IDENTITY_CONTRACT, OUTPUT_REPAIR_V2, OUTPUT_REPAIR_V3}:
        raise DirectedNumericError('numeric repair version is unsupported')
    if contract_ref == OUTPUT_REPAIR_V2:
        safe_prefixes += ('as_reported_label must name the line', 'period must identify')
    typed_diagnostic = (contract_ref == OUTPUT_REPAIR_V3 and rejected.get('error_type')
                        in {'NumericCandidateError', 'DirectedNumericError'})
    if (rejected.get('diagnostic_only') is not True or not isinstance(diagnostic, str)
            or not (typed_diagnostic or diagnostic.startswith(safe_prefixes))):
        diagnostic = None
    else:
        diagnostic = diagnostic[:1000 if contract_ref == OUTPUT_REPAIR_V3 else 300]
    rejected_output = None
    if contract_ref == OUTPUT_REPAIR_V3 and rejected.get('diagnostic_only') is True:
        outputs = rejected.get('outputs') or {}
        text = outputs.get('text') if isinstance(outputs, Mapping) else None
        if (isinstance(text, str) and len(text.encode('utf-8')) <= 16384
                and outputs.get('content_hash') == hashlib.sha256(text.encode('utf-8')).hexdigest()):
            rejected_output = text
    source, targets = _source(work)
    prompt = _replacement(source, targets, diagnostic, contract_ref=contract_ref,
                          rejected_output=rejected_output)
    record = {
        'contract_ref': contract_ref,
        'source_work_hash': content_hash(work),
        'source_prompt_hash': content_hash(work['question']),
        'replacement_prompt': prompt,
        'replacement_prompt_hash': content_hash(prompt),
        'diagnostic': diagnostic,
    }
    if contract_ref == OUTPUT_REPAIR_V3:
        record.update(rejected_output=rejected_output,
                      rejected_output_hash=content_hash(rejected_output))
    record['content_hash'] = content_hash(record)
    return record


def replay_numeric_output_repair(work, record):
    """Replay a frozen repair without replacing targets, source or budget."""
    if not isinstance(record, Mapping) or set(record) != (
            _V3_FIELDS if record.get('contract_ref') == OUTPUT_REPAIR_V3 else _FIELDS):
        raise DirectedNumericError('numeric format repair has invalid closed shape')
    body = dict(record)
    digest = body.pop('content_hash')
    diagnostic = record['diagnostic']
    if (record['contract_ref'] not in {DRAFT_IDENTITY_CONTRACT, OUTPUT_REPAIR_V2, OUTPUT_REPAIR_V3}
            or digest != content_hash(body)
            or record['source_work_hash'] != content_hash(work)
            or record['source_prompt_hash'] != content_hash(work['question'])
            or diagnostic is not None and (not isinstance(diagnostic, str) or len(diagnostic) > (
                1000 if record['contract_ref'] == OUTPUT_REPAIR_V3 else 300))):
        raise DirectedNumericError('numeric format repair binding differs')
    rejected_output = record.get('rejected_output')
    if record['contract_ref'] == OUTPUT_REPAIR_V3 and (
            rejected_output is not None and (not isinstance(rejected_output, str)
                or len(rejected_output.encode('utf-8')) > 16384)
            or record['rejected_output_hash'] != content_hash(rejected_output)):
        raise DirectedNumericError('numeric repair rejected output differs')
    source, targets = _source(work)
    expected = _replacement(source, targets, diagnostic, contract_ref=record['contract_ref'],
                            rejected_output=rejected_output)
    if (record['replacement_prompt'] != expected
            or record['replacement_prompt_hash'] != content_hash(expected)):
        raise DirectedNumericError('numeric format repair changed more than output instructions')
    metadata = dict(work['metadata'])
    metadata['prompt_hash'] = content_hash(expected)
    metadata['model_request_binding_hash'] = content_hash({
        'predecessor_request_binding_hash': work['metadata']['model_request_binding_hash'],
        'predecessor_work_hash': content_hash(work),
        'prompt_hash': metadata['prompt_hash'],
        'numeric_output_repair_hash': digest,
    })
    return expected, metadata


def contract_retry_disposition(links):
    """The same stage capacity decision for execution and its read-only cards."""
    from .mission_document_research_executor import (
        OWNER_CONTRACT_RETRY_CLASSIFICATION, AUTOMATIC_CONTRACT_RETRY_CLASSIFICATION,
        MAX_AUTOMATIC_CONTRACT_RETRIES_PER_STAGE, CONTRACT_FAILED_AFTER_OWNER_RETRY,
        CONTRACT_FAILED_AFTER_AUTOMATIC_RETRY,
    )
    classifications = {link.get('failure_proof', {}).get('classification') for link in links}
    if OWNER_CONTRACT_RETRY_CLASSIFICATION in classifications:
        return CONTRACT_FAILED_AFTER_OWNER_RETRY
    if (len(links) >= MAX_AUTOMATIC_CONTRACT_RETRIES_PER_STAGE
            or sum(link.get('failure_proof', {}).get('classification') ==
                   AUTOMATIC_CONTRACT_RETRY_CLASSIFICATION for link in links)
            >= MAX_AUTOMATIC_CONTRACT_RETRIES_PER_STAGE):
        return CONTRACT_FAILED_AFTER_AUTOMATIC_RETRY
    return None


def read_contract_retry_links(connection, admission, stage_ordinal):
    """Read only the bounded exact stage chain; unknown/drift never grants capacity."""
    rows = connection.execute(
        'SELECT * FROM mission_document_research_recovery_links '
        'WHERE admission_ref=? AND stage_ordinal=? ORDER BY recovery_number',
        (admission['id'], stage_ordinal)).fetchall()
    links = []
    for row in rows:
        value = json.loads(row['record_json'])
        body = dict(value)
        digest = body.pop('content_hash')
        prior = links[-1] if links else None
        identity = {key: value[key] for key in (
            'admission_ref', 'admission_hash', 'stage_ordinal', 'recovery_number',
            'failed_work_order_ref', 'failed_work_order_hash', 'prior_recovery_link_ref',
            'prior_recovery_link_hash', 'policy_hash', 'window_started_at', 'failure_proof')}
        from .mission_document_research_executor import _ref
        columns = {'id': 'recovery_link_id', 'admission_ref': 'admission_ref',
                   'stage_ordinal': 'stage_ordinal', 'recovery_number': 'recovery_number',
                   'failed_work_order_ref': 'failed_work_order_ref',
                   'recovery_work_order_ref': 'recovery_work_order_ref',
                   'created_at': 'created_at', 'content_hash': 'content_hash'}
        if (canonical_json(value) != row['record_json'] or digest != content_hash(body)
                or any(value[key] != row[column] for key, column in columns.items())
                or value['admission_hash'] != admission['content_hash']
                or value['recovery_number'] != len(links) + 1
                or value['prior_recovery_link_ref'] != (prior['id'] if prior else None)
                or value['prior_recovery_link_hash'] != (prior['content_hash'] if prior else None)
                or value['id'] != _ref('mission-document-research-recovery-link', identity)
                or value['recovery_work_order_ref'] != 'work:mission-document-recovery-' + content_hash(identity)[:32]):
            raise DirectedNumericError('numeric contract retry chain differs')
        links.append(value)
    return links


def read_numeric_contract_hold(connection, admission_ref):
    """Pure projection: no authority constructor, writes, recovery or fee admission."""
    row = connection.execute(
        'SELECT * FROM mission_document_research_admissions WHERE admission_id=?',
        (admission_ref,)).fetchone()
    if row is None:
        raise DirectedNumericError('numeric contract hold admission is unavailable')
    from .mission_document_research import MissionDocumentResearchAuthority
    # This row validator reads no database and constructs no authority object.
    admission = MissionDocumentResearchAuthority._read_row(None, row)
    numeric = admission.get('numeric_authority')
    if not isinstance(numeric, Mapping) or numeric.get('contract_ref') != CONTRACT_REF:
        return None
    normalize_targets(numeric['targets'])
    from .mission_document_research_executor import read_mission_document_research_observations
    observations = read_mission_document_research_observations(
        connection, mission_version_ref=admission['mission_version_ref'],
        admission_ref=admission['id'])
    candidates = []
    ordinals = {'qualitative_model_draft': 2, 'independent_qualitative_verifier': 3}
    for observation in observations:
        recovery = observation.get('recovery') or {}
        if (observation['outcome'] != 'recovery_required'
                or recovery.get('reason') not in {'contract_failed_after_automatic_retry', 'numeric_technical_maintenance_failed',
                    'unproved_send_failed_after_automatic_retry', 'numeric_technical_maintenance_day_cap_reached',
                    'numeric_maintenance_budget_backoff_day_cap', 'numeric_maintenance_budget_waiting'}
                or recovery.get('status') not in {'stopped', 'waiting'}):
            continue
        ordinal = ordinals.get(observation['stage'])
        if ordinal is None:
            raise DirectedNumericError('numeric contract hold stage is unavailable')
        links = read_contract_retry_links(connection, admission, ordinal)
        if not links or links[-1]['recovery_work_order_ref'] != observation['work_order_ref']:
            continue  # A later exact link superseded that immutable old stop.
        if recovery['reason'] == 'unproved_send_failed_after_automatic_retry':
            from .directed_numeric_maintenance import can_resume
            if not can_resume(connection, admission, observation['work_order_ref'], ordinal):
                return None  # Genuine unrelated unknown-send holds retain their owner door.
        candidates.append((observation, links))
    # Compare only successive daily budget observations of the SAME exact
    # Work/stage, never infer the held stage from another stage's timestamp.
    daily_waiting = {}
    stopped = []
    for item in candidates:
        observation, _ = item
        if observation['recovery']['status'] != 'waiting':
            stopped.append(item)
            continue
        key = (observation['work_order_ref'], observation['stage'])
        if (key not in daily_waiting or str(observation['recovery'].get('day') or '') >
                str(daily_waiting[key][0]['recovery'].get('day') or '')):
            daily_waiting[key] = item
    candidates = [*stopped, *daily_waiting.values()]
    waiting = {item[0]['work_order_ref'] for item in candidates if item[0]['recovery']['status'] == 'waiting'}
    candidates = [item for item in candidates if item[0]['recovery']['status'] == 'waiting'
                  or item[0]['work_order_ref'] not in waiting]
    if len(candidates) != 1:
        raise DirectedNumericError('numeric contract hold selected failure is unavailable or ambiguous')
    observation, links = candidates[0]
    return {'admission_ref': admission['id'], 'admission_hash': admission['content_hash'],
            'stage_ordinal': links[-1]['stage_ordinal'],
            'selected_work_order_ref': observation['work_order_ref'],
            'selected_work_order_hash': observation['work_order_hash'],
            'result_envelope_ref': observation['result_envelope_ref'],
            'result_envelope_hash': observation['result_envelope_hash'],
            'used_recovery_links': len(links),
            'automatic_window_available': observation['recovery']['status'] == 'waiting' or (
                observation['recovery']['reason'] == 'contract_failed_after_automatic_retry'
                and contract_retry_disposition(links) is None),
            'budget_waiting': observation['recovery']['status'] == 'waiting',
            'retry_at': observation['recovery'].get('retry_at'),
            'technical_schema_candidate': observation['recovery']['reason'] == 'unproved_send_failed_after_automatic_retry',
            'technical_maintenance_failed': observation['recovery']['reason'] == 'numeric_technical_maintenance_failed',
            'latest_recovery_link_ref': links[-1]['id'],
            'latest_recovery_link_hash': links[-1]['content_hash']}
