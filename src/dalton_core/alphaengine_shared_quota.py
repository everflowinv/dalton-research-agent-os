"""Append owner-authorized rate ceilings without replacing quota history.

The host's physical-call budget remains a separate, tighter admission gate.
Search uses search units; a document unit can contain at most twenty pages.
Profiles, prices, calendar windows and quota scopes remain immutable.
"""
from __future__ import annotations

import json
from pathlib import Path

from .connector_quota_policy import governed_daily_quota, apply_governed_quota_to_limits
from .store import content_hash

CONTRACT_REF = 'alphaengine-shared-owner-quota:0.1'


def load_authority(state):
    """Only a completed owner-config/shared-setting binding may raise a limit."""
    from .host_source_budget_sync import _receipt, _private_json
    from .host_research_settings import load, HostSettingsError
    from .service_config_location import service_config_path
    state = Path(state)
    receipt = _receipt(state)
    if receipt is None:
        return None  # Standalone/legacy rehearsals keep their exact v1 policy.
    config = _private_json(service_config_path(state))
    desired = load(receipt['manager_path'])
    cap = config.get('alphaengine_owner_call_cap')
    if (Path(config['core_db']).resolve() != (state / 'core.sqlite').resolve()
            or desired is None or desired['content_hash'] != receipt['desired_hash']
            or cap != receipt['owner_cap'] or type(cap) is not int or not 0 <= cap <= 2000):
        raise HostSettingsError('AlphaEngine quota requires the current completed owner binding')
    if not receipt['owner_managed']:
        return None  # An independent local ceiling was never inherited.
    if (desired['research_budget'] is None
            or desired['research_budget']['max_alphaengine_calls_24h'] not in (0, cap)):
        raise HostSettingsError('AlphaEngine quota differs from the explicit shared budget')
    # Sync deliberately retains the last positive service cap for a zero
    # shared denial. The shared denial still prevents every new admission.
    if desired['research_budget']['max_alphaengine_calls_24h'] == 0:
        cap = 0
    body = {'contract_ref': CONTRACT_REF, 'state_dir': str(state.absolute()), 'owner_cap': cap,
            'shared_settings_hash': desired['content_hash'],
            'binding_hash': receipt['content_hash'], 'config_hash': content_hash(config),
            'actor_ref': desired['actor_ref'], 'effective_from': desired['updated_at']}
    return {**body, 'content_hash': content_hash(body)}


def append_policy(connectors, legacy, authority, *, operation, profile):
    """Use a contiguous policy version under the original accounting scope."""
    if authority is None:
        return legacy
    if authority.get('contract_ref') != CONTRACT_REF or authority.get('content_hash') != content_hash(
            {k: v for k, v in authority.items() if k != 'content_hash'}):
        raise ValueError('AlphaEngine owner quota authority differs')
    if load_authority(authority['state_dir']) != authority:
        raise ValueError('AlphaEngine owner quota authority is no longer current')
    database = next((row[2] for row in connectors.connection.execute('PRAGMA database_list')
                     if row[1] == 'main'), None)
    if not database or Path(database).resolve() != (Path(authority['state_dir']) / 'core.sqlite').resolve():
        raise ValueError('AlphaEngine owner quota authority belongs to another Core')
    cap = authority['owner_cap']
    if type(cap) is not int or not 0 <= cap <= 2000:
        raise ValueError('AlphaEngine owner quota cap is invalid')
    if cap == 0:
        from .connector import ConnectorQuotaExceeded
        raise ConnectorQuotaExceeded('explicit shared AlphaEngine owner quota is zero')
    quota = governed_daily_quota('alphaengine', operation)
    quota['daily_unit_limit'] = cap
    limits = apply_governed_quota_to_limits(quota,
        max_response_bytes=profile['max_response_bytes'], max_records=profile['max_records'])
    if operation == 'get_document':
        # The saved host cap is physical calls, not twenty-page document
        # bundles. Keep a logical-document ceiling too, without multiplying
        # the administrator's physical allowance by the pagination ceiling.
        limits['calls'] = cap
        limits['bytes'] = cap * profile['max_response_bytes']
    row = connectors.connection.execute(
        'SELECT policy_version_id FROM connector_rate_policy_versions WHERE policy_ref=? '
        'ORDER BY version_number DESC LIMIT 1', (legacy['policy_ref'],)).fetchone()
    from .mission_source_discovery import MissionSourceDiscoveryCoordinator
    latest = MissionSourceDiscoveryCoordinator._core_record(connectors.connection,
        'connector_rate_policy_versions', 'policy_version_id', row['policy_version_id'])
    for key in ('quota_scope_ref', 'connector_profile_ref', 'window_seconds', 'reset_timezone',
                'quota_currency', 'price_rate_refs', 'required_price_meters', 'price_book_hash'):
        if latest[key] != legacy[key]:
            raise ValueError('AlphaEngine rate policy scope or price differs')
    if latest['limits'] == limits:
        return latest  # Unrelated admin revisions never append another version.
    version = latest['version'] + 1
    body = {k: v for k, v in latest.items() if k != 'content_hash'}
    body.update(id=legacy['policy_ref'] + ':owner:' + str(version), version=version,
        prior_version_ref=latest['id'], limits=limits,
        created_at=authority['effective_from'], effective_from=authority['effective_from'],
        actor_ref=authority['actor_ref'])
    return connectors.register_rate_policy(body,
        idempotency_key=legacy['policy_ref'] + ':owner:' + str(version))


def refused_before_send(connection, request_ref):
    """Exact original request and admitted barrier, with no transport/reserve."""
    from .runner_journal import RunnerJournal
    from .mission_source_discovery import MissionSourceDiscoveryCoordinator
    reader = RunnerJournal.__new__(RunnerJournal)
    reader._connection = connection
    request = reader.request(request_ref)
    event = reader.latest(request_ref)
    from .connector_runner import validate_connector_runner_request
    request = validate_connector_runner_request(request)
    row = connection.execute('SELECT request_json,request_hash,connector_invocation_ref '
        'FROM runner_request_journal WHERE runner_request_ref=?', (request_ref,)).fetchone()
    from .store import canonical_json
    if (row['request_json'] != canonical_json(request) or row['request_hash'] != request['content_hash']
            or row['connector_invocation_ref'] != request['connector_invocation_ref']):
        raise ValueError('quota refusal request hash differs')
    event_body = {k: v for k, v in event.items() if k not in ('id', 'content_hash')}
    if (event['state'] != 'admitted' or event['request_ordinal'] != 1
            or event['payload'] != {'request_hash': request['content_hash']}
            or event['content_hash'] != content_hash(event_body)
            or event['id'] != 'runner-journal-event:' + content_hash({
                'runner_request_ref': request_ref, 'request_ordinal': 1,
                'content_hash': event['content_hash']})):
        raise ValueError('quota refusal has no unsent admitted barrier')
    record = MissionSourceDiscoveryCoordinator._core_record
    invocation = record(connection, 'connector_invocations', 'connector_invocation_id',
                        request['connector_invocation_ref'])
    call = record(connection, 'connector_call_specs', 'call_spec_id', request['call_spec_ref'])
    if (request['connector_invocation_hash'] != invocation['content_hash']
            or request['call_spec_hash'] != call['content_hash']
            or invocation['call_spec_ref'] != call['id']
            or invocation['call_spec_hash'] != call['content_hash']
            or invocation['connector_profile_ref'] != request['connector_profile_ref']
            or call['connector_profile_ref'] != request['connector_profile_ref']
            or call['work_order_ref'] != request['work_order_ref']
            or call['work_order_hash'] != request['work_order_hash']):
        raise ValueError('quota refusal original binding differs')
    from .alphaengine_core_search import SEARCH_PROFILE_REF
    if (request['connector_profile_ref'] != SEARCH_PROFILE_REF
            or request['connector_profile_hash'] != invocation['connector_profile_hash']
            or call['operation'] != 'search_library'):
        raise ValueError('quota refusal is not this governed search')
    for table in ('connector_quota_reservations', 'connector_physical_attempts'):
        if connection.execute('SELECT 1 FROM ' + table + ' WHERE connector_invocation_ref=? LIMIT 1',
                              (invocation['id'],)).fetchone() is not None:
            raise ValueError('quota refusal already reserved or physically attempted')
    return request, call, event


def quota_increase_recovered(coordinator, dispatch):
    """Cadence-only recovery of exact local refusals, never provider unknowns."""
    import sqlite3
    from .runner_journal import RunnerJournalError
    from .coverage_mission import CoverageMissionError
    from .mission_source_discovery import ALPHAENGINE_SOURCE_REF, discovery_query_hash
    try:
        if (dispatch.get('status') != 'failed' or coordinator.source_ref != ALPHAENGINE_SOURCE_REF
                or dispatch.get('discovery_plan_ref') != coordinator.plan['id']
                or dispatch.get('discovery_plan_hash') != coordinator.plan['content_hash']):
            return False
        launcher = coordinator.search_launcher
        if launcher._ticket_re.fullmatch(dispatch['ticket_ref']) is None:
            return False
        path = launcher._ticket_path(dispatch['ticket_ref'])
        summary_path = path.with_name('summary.json')
        from .host_source_budget_sync import _private_json
        ticket, summary = _private_json(path), _private_json(summary_path)
        if (ticket.get('id') != dispatch['ticket_ref'] or ticket.get('status') != 'failed'
                or ticket.get('exit_code') != 1 or not ticket.get('completed_at')
                or summary.get('status') != 'failed'
                or summary.get('search') is not None or summary.get('discovery_ref') is not None
                or summary.get('provider_calls') != 0
                or summary.get('failure_reason') not in {
                    'unexpected ConnectorQuotaExceeded: connector calls quota exceeded',
                    'ConnectorQuotaExceeded: connector calls quota exceeded'}):
            return False
        for field in ('company_ref', 'spec_ref', 'requested_by'):
            if ticket.get(field) != dispatch.get(field) or summary.get(field) != dispatch.get(field):
                return False
        for field in ('mission_version_ref', 'mission_version_hash'):
            if ticket.get(field) != dispatch.get(field):
                return False
        for field in ('as_of', 'governance_ref', 'governance_hash'):
            if not isinstance(ticket.get(field), str) or ticket[field] != summary.get(field):
                return False
        for field, expected in (('plan_ref', coordinator.plan['id']),
                                ('plan_hash', coordinator.plan['content_hash'])):
            if ticket.get(field) != expected or summary.get(field) != expected:
                return False
        authorization = coordinator.missions.authorize_source_discovery(
            company_ref=dispatch['company_ref'], source_ref=ALPHAENGINE_SOURCE_REF,
            requested_by=dispatch['requested_by'], mission_version_ref=dispatch['mission_version_ref'],
            mission_version_hash=dispatch['mission_version_hash'])
        if dispatch.get('authorization') != authorization or summary.get('authorization') != authorization:
            return False
        if (ticket.get('actor_ref') != authorization['actor_ref']
                or dispatch.get('actor_ref') != authorization['actor_ref']):
            return False
        parameters = summary['parameters']
        query_hash = discovery_query_hash(coordinator.plan, parameters)
        if query_hash != dispatch.get('query_hash') or query_hash != summary.get('query_hash'):
            return False
        con = coordinator.store.connection
        # A bounded exact query/time lookup; no payload census or old retry mutation.
        rows = con.execute('SELECT call_spec_id FROM connector_call_specs WHERE query_hash=? '
            'AND created_at>=? AND created_at<=? LIMIT 3',
            (query_hash, ticket['started_at'], ticket['completed_at'])).fetchall()
        if len(rows) != 1:
            return False
        call = coordinator._core_record(con, 'connector_call_specs', 'call_spec_id', rows[0][0])
        if call['parameters'] != parameters or call['operation'] != 'search_library':
            return False
        request_hash = content_hash({'operation': call['operation'], 'parameters': parameters,
                                     'created_at': call['created_at']})
        request, strong_call, event = refused_before_send(con,
            'connector-runner-request:alphaengine-search:' + request_hash[:20])
        if strong_call != call:
            return False
        # The policy active at the admitted barrier establishes the old ceiling.
        from .alphaengine_core_search import SEARCH_RATE_POLICY_REF
        activation = con.execute('SELECT event_id FROM connector_rate_policy_activation_events '
            'WHERE policy_ref=? AND effective_at<=? AND created_at<=? '
            'ORDER BY effective_at DESC,rowid DESC LIMIT 1',
            (SEARCH_RATE_POLICY_REF, event['event_at'], event['recorded_at'])).fetchone()
        if activation is None:
            return False
        activated = coordinator._core_record(con, 'connector_rate_policy_activation_events', 'event_id', activation[0])
        old = coordinator._core_record(con, 'connector_rate_policy_versions', 'policy_version_id', activated['policy_version_ref'])
        if (activated['policy_ref'] != SEARCH_RATE_POLICY_REF
                or activated['policy_version_hash'] != old['content_hash']
                or activated['quota_scope_ref'] != old['quota_scope_ref']):
            return False
        authority = load_authority(launcher.state_dir)
        if authority is None or authority['owner_cap'] <= old['limits']['calls']:
            return False
        return old['connector_profile_ref'] == request['connector_profile_ref']
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error,
            RunnerJournalError, CoverageMissionError):
        return False
