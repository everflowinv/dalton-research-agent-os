"""Free validation of one schema-only widening; no provider or recovery calls."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from .company_model_spec import (
    CASH_FLOW_OPERATING_BASE_SHAPE_CODE, CompanyModelSpecError,
    PRIOR_ASSESSMENT_TASK_HASH, assessment_compatibility_contract,
    build_prompt, parse_response, prior_assessment_output_schema, spec_from_response,
)
from .contracts import ResultEnvelope
from .scheduler import Scheduler, SchedulerError
from .readonly_sqlite import connect_read_only
from .store import canonical_json, content_hash

MAX_MATCHED_ROOTS = 32
MAX_MATCHED_REPAIRS = 16
_KNOWN_CASH_CONTRACTS = {
    'contract:company-model-spec-structured-output-repair:0.8':
        'daa42203c4623bfb137afa73d6fd91221be26163af508e83ffdafdddab3e3861',
    'contract:company-model-spec-structured-output-repair:0.9':
        'afb22cd7a12c408bfb166f5abb63b7072488e689d782958df9cacb4584310448',
}
_PROOF_KEYS = frozenset({
    'work_order_ref', 'work_order_hash', 'result_envelope_ref',
    'result_envelope_hash', 'invocation_ref', 'route_decision_ref',
})


class AssessmentReplayError(CompanyModelSpecError):
    pass


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _load(connection, row: sqlite3.Row) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        reader = object.__new__(Scheduler)
        reader.connection = connection
        authority = reader.work_order_authority(row['work_order_id'])
        work = authority['work_order']
        formal = ResultEnvelope.from_dict(json.loads(row['result_envelope_json'])).to_dict()
        record = {
            'id': row['result_record_id'], 'work_order_id': row['work_order_id'],
            'attempt_number': row['attempt_number'],
            'result_envelope_id': row['result_envelope_id'],
            'result_envelope_hash': row['result_envelope_hash'],
            'terminal_state': row['terminal_state'], 'created_at': row['formal_created_at'],
        }
        if (content_hash(record) != row['formal_content_hash']
                or canonical_json(formal) != row['result_envelope_json']
                or content_hash(work) != row['work_order_hash']
                or content_hash(formal) != row['result_envelope_hash']
                or work['id'] != row['work_order_id']
                or formal['work_order_ref'] != row['work_order_id']
                or formal['id'] != row['result_envelope_id']
                or formal['status'] != 'succeeded' or row['terminal_state'] != 'succeeded'
                or not isinstance(formal['outputs']['text'], str)
                or not isinstance(formal.get('invocation_ref'), str)
                or not formal['invocation_ref']
                or not isinstance(formal.get('metadata', {}).get('route_decision_ref'), str)
                or not formal['metadata']['route_decision_ref']):
            raise ValueError('immutable Work/formal binding differs')
    except (KeyError, TypeError, ValueError, SchedulerError) as exc:
        raise AssessmentReplayError('assessment replay Work/formal authority is incomplete or differs') from exc
    return work, {
        'text': formal['outputs']['text'], 'replayed': True, 'cost_micros': 0,
        'cost_status': 'replayed', 'work_order_ref': row['work_order_id'],
        'work_order_hash': row['work_order_hash'], 'result_envelope_ref': formal['id'],
        'result_envelope_hash': row['result_envelope_hash'],
        'invocation_ref': formal.get('invocation_ref'),
        'route_decision_ref': (formal.get('metadata') or {}).get('route_decision_ref'),
    }


def _mission(work: Mapping[str, Any], mission: Mapping[str, Any]) -> bool:
    metadata = work.get('metadata') or {}
    return (metadata.get('purpose') == 'model_spec'
            and metadata.get('mission_version_ref') == mission['id']
            and metadata.get('mission_version_hash') == mission['content_hash'])


def _rows(connection, clause, args, limit):
    return connection.execute(
        'SELECT w.work_order_id,w.work_order_json,w.work_order_hash,'
        'f.result_record_id,f.attempt_number,f.content_hash AS formal_content_hash,'
        'f.created_at AS formal_created_at,'
        'f.result_envelope_id,f.result_envelope_json,f.result_envelope_hash,f.terminal_state '
        'FROM scheduler_work_orders w LEFT JOIN scheduler_formal_results f '
        'ON f.work_order_id=w.work_order_id '
        "WHERE w.work_order_id >= 'work:cockpit-model_spec-' "
        "AND w.work_order_id < 'work:cockpit-model_spec.' AND " + clause +
        ' ORDER BY w.created_at DESC,w.work_order_id DESC LIMIT ?', (*args, limit),
    ).fetchall()


def find_parent(scheduler_db: str | Path, *, state: Mapping[str, Any],
                mission: Mapping[str, Any]) -> dict[str, Any] | None:
    """Match the old whole state/task/prompt/mission, never a similar question."""
    contract = assessment_compatibility_contract()
    if content_hash({k: v for k, v in state.items() if k != 'state_hash'}) != state['state_hash']:
        raise AssessmentReplayError('assessment replay full state hash differs')
    expected_prompt = build_prompt(state, output_schema=prior_assessment_output_schema())
    path = Path(scheduler_db)
    if not path.is_file():
        return None
    with closing(connect_read_only(path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = _rows(connection,
            "json_extract(w.work_order_json,'$.metadata.model_spec_request_identity.state_hash')=? "
            "AND json_extract(w.work_order_json,'$.metadata.model_spec_request_identity.task_hash')=?",
            (state['state_hash'], PRIOR_ASSESSMENT_TASK_HASH), MAX_MATCHED_ROOTS)
        if not rows:
            return None
        # Do not borrow an older answer while the newest exact root is still
        # in flight or failed. With no succeeded answer the normal new-task
        # admission/replay path owns its budget and concurrency decisions.
        if rows[0]['terminal_state'] != 'succeeded':
            return None
        work, call = _load(connection, rows[0])
        metadata = work.get('metadata') or {}
        identity = metadata.get('model_spec_request_identity')
        config = identity.get('structured_output_repair') if isinstance(identity, Mapping) else None
        if (not isinstance(identity, Mapping)
                or set(identity) != {'schema_version','state_hash','task_hash','structured_output_repair'}
                or identity['schema_version'] != 'company-model-spec-request-0.1'
                or not isinstance(config, Mapping) or set(config) != {'max_attempts'}
                or isinstance(config['max_attempts'], bool) or not isinstance(config['max_attempts'], int)
                or config['max_attempts'] < 0
                or not _mission(work, mission) or work.get('question') != expected_prompt
                or metadata.get('structured_output_repair') is not None
                or str(metadata.get('request_id','')).split(':',1)[0] != content_hash(identity)[:32]):
            raise AssessmentReplayError('assessment replay state/company/mission/prompt/task binding differs')
        return {'work': work, 'call': call, 'contract': contract,
                'scheduler_db': str(path), 'company_ref': state['company_ref'],
                'state_hash': state['state_hash'], 'mission_ref': mission['id'],
                'mission_hash': mission['content_hash'], 'prior_prompt_sha256': _digest(expected_prompt)}


def validate_parent(parent: Mapping[str, Any], *, state: Mapping[str, Any],
                    mission: Mapping[str, Any], decided_by: str,
                    financial_validator: Callable[[dict[str, Any]], Any] | None = None,
                    note_evidence_resolver: Any = None,
                    repair_attempts: list[dict[str, Any]] | None = None):
    """At most one already-paid exact cash-shape repair; errors never buy another."""
    # Re-read the exact immutable root instead of trusting caller-supplied text/proof.
    fresh = find_parent(parent['scheduler_db'], state=state, mission=mission)
    if fresh is None or fresh != dict(parent):
        raise AssessmentReplayError('assessment replay held root proof drifted')
    call = fresh['call']; repairs = [] if repair_attempts is None else repair_attempts
    try:
        spec = spec_from_response(state, call['text'], decided_by=decided_by,
                                  note_evidence_resolver=note_evidence_resolver)
    except CompanyModelSpecError as error:
        if error.code != CASH_FLOW_OPERATING_BASE_SHAPE_CODE:
            raise
        root_proof = {key: call.get(key) for key in _PROOF_KEYS}
        if any(not isinstance(value, str) or not value for value in root_proof.values()):
            raise AssessmentReplayError('assessment replay cash parent authority is incomplete') from error
        with closing(connect_read_only(Path(fresh['scheduler_db']))) as connection:
            connection.row_factory = sqlite3.Row
            rows = _rows(connection,
                "json_extract(w.work_order_json,'$.metadata.structured_output_repair.repair_parent.work_order_ref')=? "
                "AND json_extract(w.work_order_json,'$.metadata.structured_output_repair.task_hash')=?",
                (call['work_order_ref'], PRIOR_ASSESSMENT_TASK_HASH), MAX_MATCHED_REPAIRS)
            if not rows:
                raise AssessmentReplayError('assessment replay exact paid cash repair is unavailable') from error
            work, repaired = _load(connection, rows[0])
        binding = (work.get('metadata') or {}).get('structured_output_repair')
        expected_keys = {'schema_version','root_original','repair_parent','original_text_sha256',
                         'parent_text_sha256','state_hash','task_hash','validation_error',
                         'repair_contract_ref','repair_contract_hash','repair_prompt_sha256',
                         'repair_config','repair_number'}
        if (not isinstance(binding, Mapping) or set(binding) != expected_keys
                or binding['schema_version'] != 'company-model-spec-repair-binding-0.1'
                or not _mission(work, mission) or binding['state_hash'] != state['state_hash']
                or binding['root_original'] != root_proof or binding['repair_parent'] != root_proof
                or binding['original_text_sha256'] != _digest(call['text'])
                or binding['parent_text_sha256'] != _digest(call['text'])
                or binding['repair_prompt_sha256'] != _digest(work['question'])
                or binding['validation_error'] != {'code': error.code, 'message': str(error)}
                or binding['repair_config'] != fresh['work']['metadata']['model_spec_request_identity']['structured_output_repair']
                or binding['repair_number'] != 1
                or not str((work.get('metadata') or {}).get('request_id','')).startswith(
                    'model-spec-repair:' + content_hash(binding)[:32])
                or _KNOWN_CASH_CONTRACTS.get(binding['repair_contract_ref'])
                   != binding['repair_contract_hash']):
            raise AssessmentReplayError('assessment replay paid cash repair binding differs')
        from .company_model_cli import _assert_cash_flow_operating_base_only_changed, _repair_prompt
        try:
            old_contract = json.JSONDecoder().raw_decode(work['question'].split('REPAIR_CONTRACT:\n',1)[1])[0]
        except (IndexError, TypeError, ValueError) as exc:
            raise AssessmentReplayError('assessment replay frozen repair contract is unreadable') from exc
        if (content_hash(old_contract) != binding['repair_contract_hash']
                or old_contract.get('ref') != binding['repair_contract_ref']
                or work['question'] != _repair_prompt(
                    call['text'], error, contract=old_contract,
                    output_schema=prior_assessment_output_schema())):
            raise AssessmentReplayError('assessment replay exact repair prompt differs')
        repairs.append({**{key: repaired.get(key) for key in _PROOF_KEYS},
                        'replayed': True, 'cost_micros': 0, 'repair_number': 1,
                        'binding_hash': content_hash(binding),
                        'repair_contract_hash': binding['repair_contract_hash']})
        _assert_cash_flow_operating_base_only_changed(parse_response(call['text']),parse_response(repaired['text']))
        call = repaired
        spec = spec_from_response(state, call['text'], decided_by=decided_by,
                                  note_evidence_resolver=note_evidence_resolver)
    if financial_validator is not None:
        financial_validator(spec)
    proof = {**fresh['contract'], 'company_ref': state['company_ref'],
             'state_hash': state['state_hash'], 'mission_version_ref': mission['id'],
             'mission_version_hash': mission['content_hash'],
             'prior_prompt_sha256': fresh['prior_prompt_sha256'],
             'root_authority': {key: fresh['call'].get(key) for key in _PROOF_KEYS},
             'accepted_authority': {key: call.get(key) for key in _PROOF_KEYS}}
    return spec, repairs, call, {**proof, 'content_hash': content_hash(proof)}
