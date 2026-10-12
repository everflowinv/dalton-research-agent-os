"""Bounded read-only replay of a current first-forecast driver refusal.

The stored summary locates a failure; it is not authority for that failure.
The current source-bound builder and publication execution validator must
reproduce the exact report and contract before a request gets a priority turn.
No acquisition, model call, retry permission or budget is created here.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import signal
import subprocess
import sys
import time
from contextlib import closing
from types import SimpleNamespace
from typing import Any, Mapping

from .store import canonical_json, content_hash

BASIS = 'current_first_forecast_driver_failure'
MAX_ARTIFACT_BYTES = 512_000
MAX_REQUEST_BYTES = 128_000
MAX_OUTPUT_BYTES = 64_000


def _artifact(path: Path) -> tuple[dict, str]:
    before = path.lstat()
    if (path.is_symlink() or not path.is_file() or before.st_uid != os.getuid()
            or before.st_mode & 0o077 or before.st_size > MAX_ARTIFACT_BYTES):
        raise ValueError('forecast failure artifact is not private and bounded')
    raw = path.read_bytes()
    after = path.lstat()
    identity = lambda stat: (stat.st_dev, stat.st_ino, stat.st_uid, stat.st_mode,
                             stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if identity(after) != identity(before):
        raise ValueError('forecast failure artifact changed during read')
    wire = json.loads(raw)
    if not isinstance(wire, dict):
        raise ValueError('forecast failure artifact shape differs')
    return wire, hashlib.sha256(raw).hexdigest()


def _replay(connection, state: Path, spec, mission, *, deadline: float):
    """Only pure builders/readers; never construct a writable authority."""
    from .coverage_mission import CoverageMissionAuthority
    from .company_model_inputs import build_model_inputs
    from .company_financial_statement_structure import (
        materialize_financial_statement_structure, forecast_structure_binding,
    )
    from .company_model_forecast import (
        model_digest, coverage_sources, forecast_reporting_boundary,
    )
    from .financial_note_context import FinancialNoteReadContext
    from .forecast_source_accuracy import prepare_accuracy
    from .forecast_company_fcf import load_company_fcf_definition
    from .model_forecast_driver import build_structured_forecast_model, ForecastDriverExecutionRefused
    from .forecast_financial_execution import validate_execution_contract
    authority = object.__new__(CoverageMissionAuthority)
    authority.connection = connection
    authority.store = SimpleNamespace(connection=connection)
    with FinancialNoteReadContext(store=authority.store, state_dir=state) as notes:
        table = build_model_inputs(authority, spec)
        definition = spec.get('financial_statement_structure')
        if not isinstance(definition, Mapping):
            return None
        context = notes.projection(spec['company_ref']) if definition.get('note_evidence') else None
        resolver = None if context is None else lambda ref: notes.resolver(ref, expected_context=context)
        structure, replay = materialize_financial_statement_structure(spec, table, note_evidence_resolver=resolver)
        binding = forecast_structure_binding(structure, replay, table)
        boundary = forecast_reporting_boundary(authority, connection, spec['company_ref'], table=table)
        digest = model_digest(spec, table, note_evidence_resolver=resolver,
                              financial_note_context=context, reporting_boundary=boundary)
        sources = coverage_sources(connection, spec, table, structure, None) or {}
        sources.update(boundary)
        try:
            sources['fcf_definition'] = load_company_fcf_definition(
                connection, spec['company_ref'], table, boundary.get('fiscal_year_end'))
        except (ValueError, OSError, sqlite3.Error):
            sources['fcf_definition'] = None
        sources['source_accuracy'] = prepare_accuracy(connection, state, spec, table)
        body = build_structured_forecast_model(spec, table, structure=structure, replay=replay,
            binding=binding, assumption_sources=sources,
            actor_ref=mission['autonomy']['automation_principal'], mission_version_ref=mission['id'])
        if time.monotonic() >= deadline:
            return None
        try:
            validate_execution_contract(body, publishing=True)
        except ForecastDriverExecutionRefused as exc:
            return table, digest, exc.driver_report, exc.contract_hash, f'ForecastDriverExecutionRefused: {exc}'
        return None


def _prove(state: Path, expected: Mapping[str, Any], *, deadline: float):
    from .readonly_sqlite import connect_read_only
    from .coverage_mission import CoverageMissionAuthority
    from .material_current_gap_priority import _checked_record
    from .material_requests import _view, ledger_path, request_digest, normalise_need
    from .forecast_material_gaps import gap_plans
    from .economic_invariants import FORECAST_INVARIANT_CONTRACT_HASH
    state = state.resolve()
    with closing(connect_read_only(state / 'core.sqlite')) as core:
        core.row_factory = sqlite3.Row
        core.execute('BEGIN')
        core.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
        row = core.execute('SELECT v.* FROM coverage_mission_pointer p JOIN coverage_mission_versions v '
            'ON v.mission_version_id=p.mission_version_id AND v.mission_ref=p.mission_ref '
            'ORDER BY p.mission_ref LIMIT 1').fetchone()
        if row is None:
            return None
        mission = _checked_record(row)
        if (mission['id'], mission['content_hash']) != (expected['mission_ref'], expected['mission_hash']):
            return None
        row = core.execute('SELECT * FROM coverage_mission_company_model_specs WHERE company_ref=? '
                           'ORDER BY created_at DESC,spec_id DESC LIMIT 1', (expected['company_ref'],)).fetchone()
        if row is None:
            return None
        spec = CoverageMissionAuthority._model_spec_row(row)
        fields = {'spec_id','mission_version_ref','model_profile_ref','work_order_ref','created_at','content_hash'}
        if (content_hash({k:v for k,v in spec.items() if k not in fields}) != spec['content_hash']
                or spec['spec_id'] != expected['spec_ref'] or spec['content_hash'] != expected['spec_hash']
                or spec['mission_version_ref'] != mission['id']
                or spec['company_ref'] not in {c['company_ref'] for c in mission.get('universe') or []}):
            return None
        # This first-forecast proof never replaces a current formal model.
        if core.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=\'forecast_model_versions\'').fetchone():
            if core.execute('SELECT 1 FROM forecast_model_versions WHERE company_ref=? LIMIT 1', (spec['company_ref'],)).fetchone():
                return None
        with closing(connect_read_only(ledger_path(state))) as ledger:
            ledger.row_factory = sqlite3.Row
            ledger.execute('BEGIN')
            ledger.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
            row = ledger.execute('SELECT * FROM material_requests WHERE request_id=?', (expected['request_ref'],)).fetchone()
            if row is None:
                return None
            request = _checked_record(row)
            view = _view(ledger, row, with_events=True)
            request['requesters'] = view['requesters']
            if (request['id'] != expected['request_ref'] or request['content_hash'] != expected['request_hash']
                    or request['subject'] != spec['company_ref'] or view['closed']
                    or view['status'] not in {'open','partial'}
                    or request['digest'] != request_digest(request['subject'], request['need'], request.get('period'))):
                return None
            replayed = _replay(core, state, spec, mission, deadline=deadline)
            if replayed is None:
                return None
            table, digest, report, execution_hash, failure_reason = replayed
            suffix = hashlib.sha256(('model-forecast-run|' + spec['company_ref'] + '|' + digest + '|'
                                     + FORECAST_INVARIANT_CONTRACT_HASH).encode()).hexdigest()[:24]
            directory = state / 'model-forecast-runs' / suffix
            if directory.is_symlink() or directory.parent.is_symlink():
                return None
            ticket, ticket_hash = _artifact(directory / 'ticket.json')
            summary, summary_hash = _artifact(directory / 'summary.json')
            if (ticket.get('id') != 'model-forecast-run:' + suffix or ticket.get('company_ref') != spec['company_ref']
                    or ticket.get('model_digest') != digest or ticket.get('validator_contract_hash') != FORECAST_INVARIANT_CONTRACT_HASH
                    or ticket.get('status') != 'succeeded' or ticket.get('exit_code') != 0
                    or summary.get('status') != 'succeeded' or summary.get('company_ref') != spec['company_ref']
                    or summary.get('spec_ref') != spec['spec_id'] or summary.get('digest') != digest
                    or summary.get('forecast_status') != 'refused:unexecuted_driver_plan'
                    or summary.get('driver_execution') != report or summary.get('execution_contract_hash') != execution_hash
                    or summary.get('failure_reason') != failure_reason):
                return None
            plan = next((p for p in gap_plans(spec, table, record={'execution_contract':{'drivers':report}})
                         if p['key'] == 'economic_driver_history'), None)
            if (plan is None or normalise_need(plan['need']) != request['need']
                    or plan['period'] != request.get('period') or plan['acceptance'] != request.get('acceptance')
                    or plan['source_priority'] != request.get('source_priority')
                    or not any(w.get('lane') == 'company_model_forecast' and w.get('product_ref') == spec['spec_id']
                               and w.get('section') == plan['key'] for w in request['requesters'])
                    or not any(m.get('id') == request['id'] and m.get('gap_key') == plan['key']
                               for m in summary.get('material_requests') or [])):
                return None
            if time.monotonic() >= deadline or core.total_changes or ledger.total_changes:
                return None
            body = {'contract_ref': expected['contract_ref'], 'basis': BASIS,
                'request_ref': request['id'], 'request_hash': request['content_hash'], 'company_ref': spec['company_ref'],
                'mission_version_ref': mission['id'], 'mission_hash': mission['content_hash'],
                'spec_ref': spec['spec_id'], 'spec_hash': spec['content_hash'], 'gap_key': plan['key'],
                'forecast_ref': None, 'forecast_hash': None, 'inputs_hash':content_hash(table), 'model_digest': digest,
                'validator_contract_hash': FORECAST_INVARIANT_CONTRACT_HASH,
                'failure_ticket_ref':ticket['id'], 'failure_ticket_hash':ticket_hash,'failure_summary_hash':summary_hash,
                'driver_report_hash':content_hash(report), 'execution_contract_hash':execution_hash,
                'failure_reason_hash':content_hash(failure_reason)}
            return {**body, 'content_hash':content_hash(body)}


def _bounded(state_dir, expected, *, deadline):
    remaining = deadline - time.monotonic()
    wire = canonical_json({'state_dir': str(Path(state_dir).resolve()), 'expected':dict(expected), 'deadline':deadline})
    if remaining <= 0 or len(wire.encode()) > MAX_REQUEST_BYTES:
        return None
    try:
        # Isolate the read-only worker and any SQLite header probe children.
        # This process group can contain no model/provider tasks.
        with subprocess.Popen([sys.executable, '-m', __name__, '--read-only-proof'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True) as process:
            try:
                stdout, _ = process.communicate(wire, timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                return None
            if process.returncode or len(stdout.encode()) > MAX_OUTPUT_BYTES or time.monotonic() >= deadline:
                return None
        proof = json.loads(stdout)
        if proof is None:
            return None
        if (proof.get('basis') != BASIS or proof.get('content_hash') != content_hash({k:v for k,v in proof.items() if k!='content_hash'})
                or any(proof.get(k) != v for k,v in expected.items() if k != 'mission_ref')
                or proof.get('mission_version_ref') != expected['mission_ref']):
            return None
        return proof
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


def failure_priority(state_dir, request, mission, spec, *, contract_ref, deadline, cache):
    if (request.get('acceptance',{}).get('model_gap_rule_ref') != 'rule:forecast-material-gaps:1'
            or not any(w.get('lane') == 'company_model_forecast' and w.get('product_ref') == spec['spec_id']
                       and w.get('section') == 'economic_driver_history' for w in request.get('requesters') or [])):
        return None
    expected = {'contract_ref':contract_ref,'request_ref':request['id'],'request_hash':request['content_hash'],
                'company_ref':spec['company_ref'],'mission_ref':mission['id'],'mission_hash':mission['content_hash'],
                'spec_ref':spec['spec_id'],'spec_hash':spec['content_hash']}
    key = canonical_json(expected)
    if key not in cache:
        cache[key] = _bounded(state_dir, expected, deadline=deadline)
    return cache[key]


def failure_still_current(state_dir, proof, *, deadline):
    expected = {k:proof[k] for k in ('contract_ref','request_ref','request_hash','company_ref','mission_hash','spec_ref','spec_hash')}
    expected['mission_ref'] = proof['mission_version_ref']
    return _bounded(state_dir, expected, deadline=deadline) == dict(proof)


if __name__ == '__main__':
    result = None
    try:
        raw = sys.stdin.read(MAX_REQUEST_BYTES + 1)
        if len(raw.encode()) <= MAX_REQUEST_BYTES and sys.argv[1:] == ['--read-only-proof']:
            payload = json.loads(raw)
            result = _prove(Path(payload['state_dir']), payload['expected'], deadline=payload['deadline'])
    except Exception:
        # Unavailable proof keeps ordinary service; no production authority changes.
        result = None
    sys.stdout.write(canonical_json(result))
