"""A proved expired opaque cursor may restart one normally governed query.

This never replays a transport, edits fees or grants a retry slot. Once a new
cursor-free dispatch exists, that latest dispatch owns the ordinary cadence.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import timedelta

from .connector_runner import validate_connector_runner_request, validate_connector_runner_response
from .store import canonical_json, content_hash

CURSOR_LIFETIME = timedelta(minutes=15)


def expired_cursor_restart_available(coordinator, dispatch) -> bool:
    from .coverage_mission import CoverageMissionError
    from .mission_source_discovery import (ALPHAENGINE_SOURCE_REF, _parse_wire_time,
                                          discovery_query_hash)
    launcher = coordinator.search_launcher
    if (coordinator.source_ref != ALPHAENGINE_SOURCE_REF or dispatch.get('status') != 'failed'
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
                or path.stat().st_size > 4 * 1024 * 1024
                or summary_path.stat().st_size > 4 * 1024 * 1024):
            return False
        ticket = json.loads(path.read_text())
        summary = json.loads(summary_path.read_text())
        if (ticket.get('id') != ref or ticket.get('status') != 'failed'
                or ticket.get('exit_code') != 1 or not ticket.get('completed_at')
                or summary.get('status') != 'failed' or summary.get('discovery_ref') is not None):
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
        if not isinstance(parameters, Mapping) or not parameters.get('cursor'):
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
                or response['outcome'] != 'retryable' or search.get('outcome') != 'retryable'
                or response['source_envelope_ref'] is not None or not observed):
            return False
        if coordinator.clock() < _parse_wire_time(response['retry_at']):
            return False
        error = observed[-1].get('error') or {}
        if (error.get('code') != 'source_error' or error.get('retryable') is not True
                or not str(error.get('message') or '').startswith('E_CURSOR_EXPIRED:')):
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
                    or bound.get('outcome') != 'failed' or not bound.get('completed_at')):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error, CoverageMissionError):
        # Missing/ambiguous historical proof keeps the original cadence.
        return False
