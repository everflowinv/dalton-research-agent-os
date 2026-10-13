"""Proved provider availability supersedes one stale desktop-module failure.

Only cadence changes. Current mission/selection/budget gates still admit the
new query. Old transport, reservation and fee records remain untouched.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from collections.abc import Mapping

from .connector_runner import validate_connector_runner_request, validate_connector_runner_response, RunnerValidationError
from .coverage_mission import CoverageMissionError
from .raw_spool import RawSpoolError
from .store import canonical_json, content_hash

MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_RAW_BYTES = 4 * 1024 * 1024
MAX_SUCCESS_CANDIDATES = 8
PROOF_SECONDS = 2.0


def _terminal(coordinator, dispatch, *, succeeded=False):
    from .coverage_mission import CoverageMissionError
    from .mission_source_discovery import (ALPHAENGINE_SOURCE_REF, _parse_wire_time,
                                          discovery_query_hash)
    launcher = coordinator.search_launcher
    if (coordinator.source_ref != ALPHAENGINE_SOURCE_REF or dispatch.get('status') != ('succeeded' if succeeded else 'failed')
            or dispatch.get('source_ref') != ALPHAENGINE_SOURCE_REF
            or dispatch.get('discovery_plan_ref') != coordinator.plan['id']
            or dispatch.get('discovery_plan_hash') != coordinator.plan['content_hash']):
        return False
    try:
        ref = dispatch['ticket_ref']
        if launcher._ticket_re.fullmatch(ref) is None:
            return False
        path = launcher._ticket_path(ref)
        summary_path = path.with_name('summary.json')
        if (path.is_symlink() or summary_path.is_symlink()
                or path.stat().st_size > MAX_RECORD_BYTES
                or summary_path.stat().st_size > MAX_RECORD_BYTES):
            return False
        ticket = json.loads(path.read_text())
        summary = json.loads(summary_path.read_text())
        if (ticket.get('id') != ref or ticket.get('status') != ('succeeded' if succeeded else 'failed')
                or ticket.get('exit_code') != (0 if succeeded else 1) or not ticket.get('completed_at')
                or summary.get('status') != ('succeeded' if succeeded else 'failed')
                or (summary.get('discovery_ref') is None if succeeded else summary.get('discovery_ref') is not None)):
            return False
        _parse_wire_time(ticket['completed_at'])
        for field in ('company_ref', 'spec_ref', 'requested_by'):
            if ticket.get(field) != dispatch.get(field) or summary.get(field) != dispatch.get(field):
                return False
        for field in ('mission_version_ref', 'mission_version_hash'):
            if ticket.get(field) != dispatch.get(field):
                return False
        for field, expected in (('plan_ref', coordinator.plan['id']),
                                ('plan_hash', coordinator.plan['content_hash'])):
            if ticket.get(field) != expected or summary.get(field) != expected:
                return False
        if summary.get('source_ref', ALPHAENGINE_SOURCE_REF) != ALPHAENGINE_SOURCE_REF:
            return False
        for field in ('as_of', 'governance_ref', 'governance_hash'):
            if not isinstance(ticket.get(field), str) or ticket[field] != summary.get(field):
                return False
        authorization = coordinator.missions.authorize_source_discovery(
            company_ref=dispatch['company_ref'], source_ref=ALPHAENGINE_SOURCE_REF,
            requested_by=dispatch['requested_by'],
            mission_version_ref=dispatch['mission_version_ref'],
            mission_version_hash=dispatch['mission_version_hash'])
        if summary.get('authorization') != authorization or dispatch.get('authorization') != authorization:
            return False
        if ticket.get('actor_ref') != authorization['actor_ref'] or dispatch.get('actor_ref') != authorization['actor_ref']:
            return False
        parameters = summary['parameters']
        if not isinstance(parameters, Mapping):
            return False
        query_hash = discovery_query_hash(coordinator.plan, parameters)
        if summary.get('query_hash') != query_hash or dispatch.get('query_hash') != query_hash:
            return False
        # Exact request parameters are additionally proved by the durable call
        # and runner request, not by editable summary/error strings alone.
        search = summary['search']
        con = coordinator.store.connection
        record = coordinator._core_record
        invocation = record(con, 'connector_invocations', 'connector_invocation_id',
                            search['connector_invocation_ref'])
        call = record(con, 'connector_call_specs', 'call_spec_id', invocation['call_spec_ref'])
        request_hash = content_hash({'operation': 'search_library', 'parameters': dict(parameters),
                                     'created_at': call['created_at']})
        if (search.get('connector_invocation_hash') != invocation['content_hash']
                or invocation['call_spec_hash'] != call['content_hash']
                or call['operation'] != 'search_library' or call['parameters'] != parameters
                or call['query_hash'] != query_hash or search.get('request_hash') != request_hash):
            return False
        suffix = request_hash[:20]
        if (invocation['id'] != 'connector-invocation:alphaengine-search:' + suffix
                or call['id'] != 'connector-call:alphaengine-search:' + suffix):
            return False
        rows = con.execute('SELECT * FROM runner_request_journal WHERE connector_invocation_ref=?',
                           (invocation['id'],)).fetchall()
        if len(rows) != 1:
            return False
        row = rows[0]
        request = validate_connector_runner_request(json.loads(row['request_json']))
        if (canonical_json(request) != row['request_json'] or request['id'] != row['runner_request_ref']
                or request['content_hash'] != row['request_hash']
                or request['connector_invocation_ref'] != invocation['id']
                or request['connector_invocation_hash'] != invocation['content_hash']
                or request['call_spec_ref'] != call['id'] or request['call_spec_hash'] != call['content_hash']):
            return False
        events = con.execute('SELECT * FROM runner_attempt_journal_events WHERE runner_request_ref=? '
                             'ORDER BY event_seq LIMIT 65', (request['id'],)).fetchall()
        if not events or len(events) > 64 or events[-1]['state'] != 'responded':
            return False
        observed = []
        for ordinal, event in enumerate(events, 1):
            payload = json.loads(event['payload_json'])
            body = {'runner_request_ref': request['id'], 'request_ordinal': ordinal,
                    'state': event['state'], 'reservation_ref': event['reservation_ref'],
                    'event_at': event['event_at'], 'recorded_at': event['created_at'], 'payload': payload}
            digest = content_hash(body)
            if (canonical_json(payload) != event['payload_json'] or digest != event['content_hash']
                    or event['event_id'] != 'runner-journal-event:' + content_hash({
                        'runner_request_ref': request['id'], 'request_ordinal': ordinal,
                        'content_hash': digest})):
                return False
            if event['state'] == 'observed':
                observed.append(payload)
        response = validate_connector_runner_response(payload['response'])
        if (response['runner_request_ref'] != request['id']
                or response['runner_request_hash'] != request['content_hash']
                or response['connector_invocation_ref'] != invocation['id']
                or response['connector_invocation_hash'] != invocation['content_hash']
                or response['id'] != search.get('runner_response_ref')
                or response['outcome'] != ('succeeded' if succeeded else 'retryable')
                or search.get('outcome') != response['outcome'] or not observed
                or (response['source_envelope_ref'] is None if succeeded else response['source_envelope_ref'] is not None)):
            return False
        if not succeeded and coordinator.clock() < _parse_wire_time(response['retry_at']):
            return False
        error = observed[-1].get('error') or {}
        if not succeeded and (error.get('code') != 'source_error' or error.get('retryable') is not True
                or not str(error.get('message') or '').startswith('E_DESKTOP_NO_MODULE_PAGE:')):
            return False
        # This predicate does not reinterpret unknown costs or release them.
        # Require the response's physical/usage/cost/settlement authorities to
        # remain exactly the persisted objects it names.
        for table, pk, field in (
            ('connector_physical_attempts', 'physical_attempt_id', 'physical_attempt'),
            ('connector_usage_entries', 'usage_entry_id', 'usage_entry'),
            ('connector_cost_entries', 'cost_entry_id', 'cost_entry'),
            ('connector_quota_settlements', 'settlement_id', 'quota_settlement')):
            bound = record(con, table, pk, response[field + '_ref'])
            if bound['content_hash'] != response[field + '_hash']:
                return False
            if field == 'physical_attempt' and (bound.get('connector_invocation_ref') != invocation['id']
                    or bound.get('outcome') != ('succeeded' if succeeded else 'failed') or not bound.get('completed_at')
                    or bound.get('error') != observed[-1].get('error')):
                return False
        return {'invocation':invocation,'call':call,'request':request,'response':response,
                'observed':observed[-1],'events':events,'ticket':ticket,'summary':summary}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error, CoverageMissionError):
        # Missing/ambiguous historical proof keeps the original cadence.
        return False


def _raw_original(coordinator, version):
    from .raw_spool import RawSpoolReader
    root = getattr(coordinator.search_launcher, 'spool_dir', None)
    if root is None:
        state = getattr(coordinator.search_launcher, 'state_dir', None)
        if state is None:return None
        root = Path(state) / 'connector-spool'
    size = version.get('size_bytes')
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_RAW_BYTES:return None
    digest = version['artifact_content_hash']
    if (not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest)
            or version['storage_locator'] != 'spool:objects/'+digest[:2]+'/'+digest):return None
    # Bound decompression as well as advertised size. No file/sidecar creation.
    reader = RawSpoolReader(root)
    directory = reader._objects / digest[:2]
    if directory.is_symlink():return None
    plain, compressed = directory / digest, directory / (digest+'.gz')
    path = plain if plain.exists() else compressed
    if path.is_symlink() or not path.is_file():return None
    opener = path.open if path == plain else lambda mode:gzip.open(path,mode)
    with opener('rb') as file:raw = file.read(MAX_RAW_BYTES+1)
    if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=digest:return None
    return raw


def _success(coordinator, proof):
    from .live_mcp_connector import AlphaEngineLiveAdapter, HostToolInvocationResult
    from .connector import source_envelope_content_hash
    invocation,request,response = (proof[k] for k in ('invocation','request','response'))
    con = coordinator.store.connection;record = coordinator._core_record
    source = record(con,'connector_source_envelopes','source_envelope_id',response['source_envelope_ref'])
    version = record(con,'observability_artifact_versions_v2','version_id',response['raw_artifact_version_ref'])
    profile = record(con,'connector_profile_versions','profile_version_id',invocation['connector_profile_ref'])
    if (profile['content_hash']!=invocation['connector_profile_hash']
            or source['content_hash']!=response['source_envelope_hash']
            or source['content_hash']!=proof['summary']['search']['source_envelope_hash']
            or source['connector_invocation_ref']!=invocation['id']
            or source['connector_profile_ref']!=profile['id'] or source['operation']!='search_library'
            or source['source']!='source:alphaengine' or source['error'] is not None
            or source['source_schema_hash']!=profile['output_schema_hashes']['search_library']
            or any(source[k]!=profile[k] for k in ('access_policy_ref','retention_policy_ref','terms_policy_ref'))
            or source['status'] not in {'complete','partial','empty'}
            or source['source_content_hash']!=source_envelope_content_hash(source)
            or source['result_physical_attempt_ref']!=response['physical_attempt_ref']
            or response['physical_attempt_ref'] not in source['physical_attempt_refs']
            or source['raw_artifact_version_ref']!=version['id']
            or version['content_hash']!=response['raw_artifact_version_hash']
            or version['artifact_content_hash']!=source['raw_response_hash']
            or version['producer_execution_ref']!=invocation['id']
            or version['work_order_ref']!=invocation['work_order_ref']
            or version['result_envelope_ref']!=response['result_envelope_ref']
            or version['result_envelope_hash']!=response['result_envelope_hash']):return False
    raw = _raw_original(coordinator,version)
    if raw is None:return False
    parsed = json.loads(raw)
    if parsed.get('jsonrpc')!='2.0' or not isinstance(parsed.get('result'),Mapping):return False
    starts = [json.loads(e['payload_json']) for e in proof['events'] if e['state']=='transport_started']
    if len(starts)!=1:return False
    adapter_request = starts[0]['adapter_request']
    if starts[0]['adapter_request_hash']!=adapter_request['content_hash']:return False
    class FrozenResult:
        def invoke(self,tool_name,arguments,**kwargs):
            from .live_mcp_connector import alphaengine_tool_arguments
            if tool_name!=adapter_request['tool_name'] or arguments!=alphaengine_tool_arguments(adapter_request):
                raise ValueError('stored provider search arguments differ')
            return HostToolInvocationResult(request_id=parsed['id'],raw_response=raw,result=parsed['result'])
    class ReadOnlySink:
        def write(self,body):
            if body!=raw:raise ValueError('stored provider response differs')
    rebuilt = AlphaEngineLiveAdapter()(adapter_request,ReadOnlySink(),FrozenResult())
    observed = proof['observed']
    if (rebuilt!=observed['observation'] or observed['attempt_outcome']!='succeeded'
            or observed.get('error') is not None
            or (observed.get('raw_object') or {}).get('content_hash')!=source['raw_response_hash']
            or (observed.get('raw_object') or {}).get('size_bytes')!=len(raw)
            or rebuilt['source_record_refs']!=source['source_record_refs']
            or rebuilt['cursor']!=source['cursor'] or rebuilt['source_status']!=source['status']):return False
    return True


def desktop_module_recovered(coordinator, dispatch) -> bool:
    """One ordinary query may follow an exact failure and later real success."""
    from .mission_source_discovery import _parse_wire_time
    deadline = min(time.monotonic()+PROOF_SECONDS,
                   getattr(coordinator,'_tick_read_deadline',None) or float('inf'))
    try:
        if not str(dispatch.get('failure_reason') or '').startswith('search outcome retryable'):
            return False
        # Avoid expensive reads for unrelated, typed failures.
        if 'E_DESKTOP_NO_MODULE_PAGE:' not in str(dispatch.get('failure_reason') or ''):
            return False
        failed = _terminal(coordinator,dispatch)
        if not failed:return False
        failed_at = _parse_wire_time(failed['response']['created_at'])
        rows = coordinator.store.connection.execute(
            "SELECT * FROM coverage_mission_discovery_dispatches WHERE source_ref=? AND status='succeeded' "
            "AND created_at>? ORDER BY created_at DESC,dispatch_id DESC LIMIT ?",
            (coordinator.source_ref,failed['response']['created_at'],MAX_SUCCESS_CANDIDATES)).fetchall()
        for row in rows:
            if time.monotonic()>=deadline:return False
            successor = coordinator.missions._dispatch_row(row)
            succeeded = _terminal(coordinator,successor,succeeded=True)
            if not succeeded:continue
            invocation = failed['invocation'];other = succeeded['invocation']
            if any(invocation[k]!=other[k] for k in ('connector_profile_ref','connector_profile_hash','descriptor_revision_ref','catalog_epoch')):
                continue
            if any(failed['request'][k]!=succeeded['request'][k] for k in
                    ('principal_ref','runner_actor_ref','runner_runtime_ref','runner_environment_hash')):
                continue
            if (_parse_wire_time(succeeded['ticket']['completed_at'])<=failed_at
                    or _parse_wire_time(succeeded['response']['created_at'])<=failed_at
                    or _parse_wire_time(succeeded['response']['created_at'])>coordinator.clock()):continue
            if _success(coordinator,succeeded):return time.monotonic()<deadline
        return False
    except (OSError,ValueError,KeyError,TypeError,AttributeError,sqlite3.Error,
            CoverageMissionError,RawSpoolError,RunnerValidationError,EOFError,RecursionError):
        return False
