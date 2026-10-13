"""Bounded, exact failed-answer diagnostics; no model/retry authority.

A repeated typed failure of the same immutable prompt is not useful provider
retry. It still owns its original accounting and rejected-answer evidence.
"""
from __future__ import annotations
import json
from collections.abc import Mapping
from .store import canonical_json, content_hash
from .contracts import ResultEnvelope

CONTRACT_REF = 'directed-numeric-repeated-contract-failure:0.1'
MAX_DIAGNOSTIC_CHARS = 1000
MAX_RECEIPTS = 16


def _checked_result(row, work):
    result = ResultEnvelope.from_dict(json.loads(row['result_envelope_json'])).to_dict()
    receipt = {k: row[k] for k in ('result_envelope_id', 'work_order_id',
        'attempt_number', 'result_envelope_hash', 'outcome', 'created_at')}
    if (canonical_json(result) != row['result_envelope_json']
            or content_hash(result) != row['result_envelope_hash']
            or content_hash(receipt) != row['content_hash']
            or result['id'] != row['result_envelope_id']
            or result['work_order_ref'] != work['id']
            or result['status'] != row['outcome']):
        raise ValueError('numeric failure diagnostic receipt differs')
    return result


def failure_fingerprint(work, error_type, message):
    meta = work.get('metadata') or {}
    if (meta.get('numeric_contract_ref') != 'directed-document-numeric:0.1'
            or meta.get('stage') != 'qualitative_model_draft'
            or error_type not in {'NumericCandidateError', 'DirectedNumericError'}
            or not isinstance(message, str) or not message):
        return None
    if content_hash(work['question']) != meta.get('prompt_hash'):
        raise ValueError('numeric failed prompt hash differs')
    return content_hash({'contract_ref': CONTRACT_REF, 'work_ref': work['id'],
        'work_hash': content_hash(work), 'prompt_hash': meta['prompt_hash'],
        'error_type': error_type, 'diagnostic_hash': content_hash(message)})


def prior_same_failure(connection, work, *, before_attempt, error_type, message):
    """Return one original receipt only after complete canonical/hash replay."""
    fingerprint = failure_fingerprint(work, error_type, message)
    if fingerprint is None:
        return None
    rows = connection.execute(
        'SELECT * FROM scheduler_result_envelopes WHERE work_order_id=? '
        'AND attempt_number<? ORDER BY attempt_number DESC LIMIT ?',
        (work['id'], before_attempt, MAX_RECEIPTS + 1)).fetchall()
    if len(rows) > MAX_RECEIPTS:
        raise ValueError('numeric failure diagnostic receipt limit exceeded')
    for row in rows:
        result = _checked_result(row, work)
        rejected = (result.get('metadata') or {}).get('rejected_model_output')
        if ((result.get('error') or {}).get('code') != 'MODEL_OUTPUT_CONTRACT_REJECTED'
                or result['status'] not in {'retryable', 'failed'}
                or not isinstance(rejected, Mapping)
                or rejected.get('diagnostic_only') is not True):
            continue
        if failure_fingerprint(work, rejected.get('error_type'),
                               rejected.get('error_message')) != fingerprint:
            continue
        return {'contract_ref': CONTRACT_REF, 'fingerprint': fingerprint,
            'work_order_ref': work['id'], 'work_order_hash': content_hash(work),
            'prompt_hash': work['metadata']['prompt_hash'],
            'prior_result_envelope_ref': result['id'],
            'prior_result_envelope_hash': row['result_envelope_hash'],
            'prior_attempt': row['attempt_number'],
            'error_type': error_type, 'diagnostic': message[:MAX_DIAGNOSTIC_CHARS],
            'diagnostic_hash': content_hash(message)}
    return None


def repeated_before_admission(connection, work, *, before_attempt):
    """Stop an old ready Work after two checked equal failures, without a send.

    This reads historical receipts only; it does not infer that those earlier
    calls were free or change their accounting or rejected original answers.
    """
    row = connection.execute('SELECT * FROM scheduler_result_envelopes '
        'WHERE work_order_id=? AND attempt_number<? ORDER BY attempt_number DESC LIMIT 1',
        (work['id'], before_attempt)).fetchone()
    if row is None:return None
    result = _checked_result(row, work)
    rejected = (result.get('metadata') or {}).get('rejected_model_output')
    if ((result.get('error') or {}).get('code')!='MODEL_OUTPUT_CONTRACT_REJECTED'
            or result['status'] not in {'retryable','failed'}
            or not isinstance(rejected, Mapping) or rejected.get('diagnostic_only') is not True):
        return None
    prior = prior_same_failure(connection, work, before_attempt=row['attempt_number'],
        error_type=rejected.get('error_type'), message=rejected.get('error_message'))
    if prior is None:return None
    return {**prior, 'latest_result_envelope_ref':result['id'],
        'latest_result_envelope_hash':row['result_envelope_hash'],
        'latest_attempt':row['attempt_number']}
