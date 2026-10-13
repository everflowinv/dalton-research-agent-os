"""Owner-derived reads of material requested by an exact model source gap.

This is not a planner model result or numerical evidence.  The admission
owner must replay the original paid model, its state/diagnostic/settled fee
chain and the registered source before using a plan returned here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Mapping
from pathlib import Path
from contextlib import contextmanager, closing
import sqlite3

from .store import canonical_json, content_hash
from .operating_driver_units import CONTRACT_REF as NUMERIC_REF

TASK_REF = 'owner:model-operating-driver-source-plan:0.1'
SCHEMA_VERSION = 'model-operating-driver-source-plan-0.1'
MAX_READS_PER_TICK = 2


@contextmanager
def _material_reader(state_dir):
    from .material_requests import ledger_path
    from .readonly_sqlite import connect_read_only,connect_cold_wal_snapshot
    path=ledger_path(state_dir)
    if not path.is_file():
        yield None
        return
    try:
        connection=connect_read_only(path)
    except RuntimeError as exc:
        if 'read_only WAL requires existing WAL/SHM' not in str(exc):
            raise
        with connect_cold_wal_snapshot(path) as connection:
            connection.row_factory=sqlite3.Row
            yield connection
    else:
        with closing(connection):
            connection.row_factory=sqlite3.Row
            yield connection


def read_material_request(state_dir,ref):
    from .material_requests import _view
    with _material_reader(state_dir) as connection:
        if connection is None:return None
        row=connection.execute('SELECT * FROM material_requests WHERE request_id=?',(ref,)).fetchone()
        if row is None:return None
        raw=json.loads(row['record_json'])
        if canonical_json(raw)!=row['record_json'] or raw['content_hash']!=row['content_hash'] or content_hash({k:v for k,v in raw.items() if k!='content_hash'})!=row['content_hash']:
            raise ValueError('operating source material record hash differs')
        return _view(connection,row,with_events=True)


def derive_current_plan(authority: Any, mission: Mapping[str, Any], *, request_ref: str,
                        registration_ref: str, state_dir: str | Path,
                        model_config: Mapping[str, Any]) -> dict[str, Any]:
    """Strong owner replay, used before appending a plan and on every execution.

    Model/source/database handles are existing owner readers. This function
    neither constructs CockpitModel nor reserves a paid call.
    """
    from types import SimpleNamespace
    read_request = read_material_request
    from .company_model_state import (build_company_model_state,
        model_spec_numeric_context_config, model_spec_prompt_byte_limit)
    from .company_model_cli import filed_classifications, validate_model_spec_request_identity
    from .company_model_spec import build_prompt, TASK_HASH
    from .financial_note_context import FinancialNoteReadContext
    from .model_spec_maintenance_repair import _load, _proof
    from .model_spec_source_sign_projection import _permission, _settled_success_history
    from .document_research import (SEARCH_REQUEST_SCHEMA_VERSION, SEARCH_OPERATION)
    from .mission_document_research import PURPOSE
    root = Path(state_dir)
    request = read_request(root, request_ref)
    if request is None:
        raise ValueError('model source material request is unavailable')
    scope = (request.get('acceptance') or {}).get('model_driver_source_request')
    if not isinstance(scope, Mapping):
        raise ValueError('model source plan request scope differs')
    _permission(authority._missions, mission, scope['company_ref'])
    scheduler = authority.planner_scheduler_connection
    work, call = _load(scheduler, scope['root_original']['work_order_ref'])
    identity = validate_model_spec_request_identity(work['metadata'].get('model_spec_request_identity', {}))
    if (identity['task_hash'] != TASK_HASH or _proof(call) != scope['root_original']
            or work['metadata'].get('structured_output_repair') is not None
            or work['metadata'].get('mission_version_ref') != scope['mission_version_ref']
            or work['metadata'].get('mission_version_hash') != scope['mission_version_hash']):
        raise ValueError('model source original Work/mission is not its registered root')
    original_mission = authority._missions.mission(scope['mission_version_ref'])
    # Current permissions remain checked by _mission. Do not fabricate a new
    # hash for the old model's mission after the owner changes governance.
    from .document_mission_lineage import document_mission_versions
    if original_mission['id'] not in document_mission_versions(authority.connection, mission):
        raise ValueError('model source plan root is not in the current authorized mission ancestry')
    if original_mission['content_hash'] != scope['mission_version_hash']:
        raise ValueError('model source root mission hash differs')
    if scheduler.execute("SELECT 1 FROM scheduler_work_orders WHERE work_order_id >= 'work:cockpit-model_spec-' "
            "AND work_order_id < 'work:cockpit-model_spec.' AND "
            "json_extract(work_order_json,'$.metadata.structured_output_repair.root_original.work_order_ref')=? LIMIT 1",
            (work['id'],)).fetchone() is not None:
        raise ValueError('model source plan root has subsequently consumed a model repair')
    frozen_rows=authority.connection.execute(
        "SELECT plan_json,content_hash,state_hash,work_order_ref,model_profile_ref,decided_by FROM coverage_mission_research_plans "
        "WHERE json_extract(plan_json,'$.task_ref')=? AND "
        "json_extract(plan_json,'$.model_source_origin.anchor.root_original.work_order_ref')=? LIMIT 2",
        (TASK_REF,work['id'])).fetchall()
    state=None
    for row in frozen_rows:
        prior=json.loads(row['plan_json'])
        candidate=prior['model_source_origin']['frozen_model_state']
        if (canonical_json(prior)!=row['plan_json'] or prior['content_hash']!=row['content_hash']
                or content_hash({k:v for k,v in prior.items() if k!='content_hash'})!=row['content_hash']
                or row['state_hash']!=content_hash(prior['model_source_origin'])
                or row['work_order_ref'] is not None or row['model_profile_ref'] is not None
                or row['decided_by']!=original_mission['autonomy']['automation_principal']
                or prior['model_source_origin']['anchor']['root_original']!=_proof(call)
                or candidate['state_hash']!=identity['state_hash']
                or candidate['state_hash']!=content_hash({k:v for k,v in candidate.items() if k!='state_hash'})
                or build_prompt(candidate)!=work['question']):
            raise ValueError('operating sibling plan lost its complete frozen paid input')
        if state is not None and canonical_json(state)!=canonical_json(candidate):
            raise ValueError('operating root has ambiguous frozen input states')
        state=candidate
    if state is None:
        with FinancialNoteReadContext(store=authority.store, state_dir=root) as notes:
            state = build_company_model_state(authority._missions, scope['company_ref'],
                industry_classification=filed_classifications(authority.store).get(scope['company_ref']),
                numeric_context_policy=model_spec_numeric_context_config(model_config),
                prompt_byte_limit=model_spec_prompt_byte_limit(model_config),
                financial_note_context=notes.projection(scope['company_ref']),
                ticker=next((entry.get('ticker') for entry in original_mission['universe']
                             if entry['company_ref'] == scope['company_ref']), None))
        if state['state_hash'] != identity['state_hash']:
            # A later root may explicitly include previously admitted operating
            # evidence. Rebuild that exact context only when the original filed
            # frame alone is not its input; never strip it from a newer Work.
            from .operating_driver_context import OperatingDriverReadContext
            with OperatingDriverReadContext(store=authority.store,state_dir=root) as operating:
                projection=operating.projection(scope['company_ref'])
            if projection is not None:
                state={**state,'operating_driver_context':projection}
                state['state_hash']=content_hash({k:v for k,v in state.items() if k!='state_hash'})
    if state['state_hash'] != identity['state_hash'] or build_prompt(state) != work['question']:
        raise ValueError('model source original input is no longer the exact current filed state')
    database = scheduler.execute('PRAGMA database_list').fetchall()
    path = next(row[2] for row in database if row[1] == 'main')
    model = SimpleNamespace(config=dict(model_config), scheduler_db=path, clock=authority.clock)
    fees = _settled_success_history(model, scheduler, work, call, original_mission)
    registration = authority.registration_resolver(registration_ref)
    policy = authority.registry.policy
    from .operating_driver_retrieval import metric_terms
    search_request = {'schema_version': SEARCH_REQUEST_SCHEMA_VERSION, 'operation': SEARCH_OPERATION,
        'purpose': PURPOSE, 'research_question': request['need']['question'],
        'registration': registration, 'query_terms': metric_terms(scope['metric'])[:policy['max_query_terms']],
        'limits': {'max_results':policy['max_results'],'context_before_chars':policy['max_context_before_chars'],'context_after_chars':policy['max_context_after_chars']},
        'policy_ref': policy['policy_ref'], 'policy_hash': policy['content_hash']}
    proof = authority.registry.search(search_request)
    from .directed_numeric_executor import source_binding
    source_binding(authority.connection, registration, scope['company_ref'], strategy_version=NUMERIC_REF)
    from .operating_source_measurements import registered_measurements
    measures = registered_measurements(authority.registry, registration, purpose=PURPOSE, search_proof=proof,
        periods=[scope['period_start']+'/'+scope['period_end']])
    target = source_unit_target(scope, proof['matches'], measurement_proof=measures)
    expected = derive_plan(state=state, mission=mission, root_mission=original_mission, call=call, fees=fees,
                          request=request, registration=registration, search_proof=proof, target=target)
    return expected


def replay_plan(authority: Any, plan: Mapping[str, Any], mission: Mapping[str, Any], *,
                state_dir: str | Path, model_config: Mapping[str, Any]) -> dict[str, Any]:
    origin = plan.get('model_source_origin')
    if not isinstance(origin, Mapping) or origin.get('contract_ref') != TASK_REF:
        raise ValueError('model source plan has no exact owner origin')
    from types import SimpleNamespace
    read_request = read_material_request
    from .model_spec_maintenance_repair import _load, _proof
    from .model_spec_source_sign_projection import _permission, _settled_success_history
    from .company_model_spec import build_prompt
    from .company_model_cli import validate_model_spec_request_identity
    from .document_mission_lineage import document_mission_versions
    from .document_research import SEARCH_REQUEST_SCHEMA_VERSION, SEARCH_OPERATION
    from .mission_document_research import PURPOSE
    from .directed_numeric_executor import source_binding
    scope = origin['anchor']
    admitted_mission = authority._missions._mission_record(plan['mission_version_ref'])
    governing = authority._governing_mission(admitted_mission)
    if (mission['id'],mission['content_hash']) not in {(admitted_mission['id'],admitted_mission['content_hash']),
            (governing['id'],governing['content_hash'])}:
        raise ValueError('operating plan admitted/current mission differs from owner authority')
    current = authority._missions._mission_record(governing['id'])
    _permission(authority._missions,current,scope['company_ref'])
    if scope['mission_version_ref'] not in document_mission_versions(authority.connection,current):
        raise ValueError('operating plan historical root is not in the current authorized project')
    original_mission = authority._missions.mission(scope['mission_version_ref'])
    if original_mission['content_hash'] != scope['mission_version_hash']:
        raise ValueError('operating plan original mission hash differs')
    work,call = _load(authority.planner_scheduler_connection,scope['root_original']['work_order_ref'])
    identity = validate_model_spec_request_identity(work['metadata']['model_spec_request_identity'])
    state = origin['frozen_model_state']
    if (state['company_ref'] != scope['company_ref'] or state['state_hash'] != identity['state_hash']
            or state['state_hash'] != content_hash({k:v for k,v in state.items() if k!='state_hash'})
            or work['question'] != build_prompt(state) or _proof(call) != scope['root_original']
            or work['metadata'].get('structured_output_repair') is not None
            or work['metadata'].get('mission_version_ref') != original_mission['id']
            or work['metadata'].get('mission_version_hash') != original_mission['content_hash']):
        raise ValueError('operating plan frozen original input does not match its complete formal root')
    database=authority.planner_scheduler_connection.execute('PRAGMA database_list').fetchall()
    model=SimpleNamespace(config=dict(model_config),scheduler_db=next(r[2] for r in database if r[1]=='main'),clock=authority.clock)
    fees=_settled_success_history(model,authority.planner_scheduler_connection,work,call,original_mission)
    request=read_request(state_dir,origin['request_ref'])
    registration=authority.registration_resolver(origin['registration_ref'])
    source_binding(authority.connection,registration,scope['company_ref'],strategy_version=NUMERIC_REF)
    policy=authority.registry.policy
    from .operating_driver_retrieval import metric_terms
    search={'schema_version':SEARCH_REQUEST_SCHEMA_VERSION,'operation':SEARCH_OPERATION,'purpose':PURPOSE,
        'research_question':request['need']['question'],'registration':registration,
        'query_terms':metric_terms(scope['metric'])[:policy['max_query_terms']],
        'limits':{'max_results':policy['max_results'],'context_before_chars':policy['max_context_before_chars'],'context_after_chars':policy['max_context_after_chars']},
        'policy_ref':policy['policy_ref'],'policy_hash':policy['content_hash']}
    proof=authority.registry.search(search)
    from .operating_source_measurements import registered_measurements
    measures=registered_measurements(authority.registry,registration,purpose=PURPOSE,search_proof=proof,
        periods=[scope['period_start']+'/'+scope['period_end']])
    target=source_unit_target(scope,proof['matches'],measurement_proof=measures)
    expected=derive_plan(state=state,mission=admitted_mission,root_mission=original_mission,call=call,fees=fees,
        request=request,registration=registration,search_proof=proof,target=target)
    if canonical_json({k:v for k,v in plan.items() if k != 'plan_id'}) != canonical_json(expected):
        raise ValueError('model source owner plan does not rederive from its complete original proof')
    return expected


def derive_plan(*, state: Mapping[str, Any], mission: Mapping[str, Any],
                call: Mapping[str, Any], fees: Any, request: Mapping[str, Any],
                registration: Mapping[str, Any], search_proof: Mapping[str, Any],
                target: Mapping[str, Any], root_mission: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Pure deterministic proposal, requiring the host's strong readers first.

    Supplying matching self hashes here does not grant a write: the host
    rederives these inputs from durable authority on admission and execution.
    No normal model repair allowance is consumed or replenished by this plan.
    """
    from .model_spec_joint_field_diagnostics import source_request_plans, from_state
    from .model_spec_maintenance_repair import _proof
    from .company_model_spec import parse_response
    scope = (request.get('acceptance') or {}).get('model_driver_source_request')
    if not isinstance(scope, Mapping) or request.get('status') != 'found' or not request.get('closed'):
        raise ValueError('operating source plan needs the exact found model material request')
    body = parse_response(call['text'])
    anchor = {key: value for key, value in scope.items()
              if key not in {'metric', 'period_start', 'period_end'}}
    original_mission = mission if root_mission is None else root_mission
    expected = {'contract_ref': 'model-spec-joint-required-fields:0.1',
                'company_ref': state['company_ref'], 'state_hash': state['state_hash'],
                'root_original': _proof(call),
                'original_text_sha256': hashlib.sha256(call['text'].encode()).hexdigest(),
                'diagnostics_hash': content_hash(from_state(body, state)),
                'paid_history_hash': content_hash(fees),
                'mission_version_ref': original_mission['id'], 'mission_version_hash': original_mission['content_hash'],
                'financial_evidence': False, 'repair_authority': False}
    if canonical_json(anchor) != canonical_json(expected):
        raise ValueError('operating source plan original root/state/diagnostic/fees differ')
    candidates = source_request_plans(body, state, expected)
    matching = [p for p in candidates if p['acceptance']['model_driver_source_request'] == scope]
    from .material_requests import normalise_need
    if len(matching) != 1 or request['need'] != normalise_need(matching[0]['need']) or any(request.get(k) != matching[0][k]
                               for k in ('subject', 'period', 'source_priority', 'acceptance')):
        raise ValueError('operating source plan differs from the original exact metric-period need')
    if (registration['source_authority']['company_ref'] != state['company_ref']
            or search_proof['request']['registration'] != registration
            or registration['document_ref'] not in {
                item.get('document_ref') for event in request.get('events', ())
                if event.get('event_kind') == 'closed' and event.get('status') == 'found'
                for item in event.get('items', ())}):
        raise ValueError('operating source plan does not bind the found registered original')
    # An MR's lexical acceptance alone is insufficient. The new target must
    # name the exact requested metric, date window and a source-declared unit.
    from .directed_numeric_research import normalize_targets
    normalized = normalize_targets([dict(target)], contract_ref=NUMERIC_REF)[0]
    metric = scope['metric']
    if (normalized['metric_definition']['label'] != metric['label']
            or normalized['period'] != scope['period_start'] + '/' + scope['period_end']
            or normalized['basis'] not in {'management-reported', 'gaap-reported'}):
        raise ValueError('operating target changed metric, historical window or reported basis')
    inquiry = {'rank': 0, 'company_ref': state['company_ref'],
        'question': request['need']['question'],
        'wants': 'An independently verified source-reported operating quantity with exact period, unit, scope and canonical Claim; unavailable is an explicit result.',
        'because': 'Read the original source gap already requested by the exact model; this is an owner-derived task, not a new model decision.',
        'directed_document': {'strategy_version': NUMERIC_REF,
            'document_ref': registration['document_ref'],
            'document_version_hash': registration['content_hash'],
            'query_terms': search_proof['request']['query_terms'],
            'query_rationale': 'Read the exact requested company metric in its source-reported historical window and physical unit.',
            'numeric_targets': [normalized]}}
    origin = {'contract_ref': TASK_REF, 'request_ref': request['id'],
        'request_scope_hash': content_hash(matching[0]), 'anchor': copy.deepcopy(scope),
        'registration_ref': registration['id'], 'registration_hash': registration['content_hash'],
        'search_proof_ref': search_proof['id'], 'search_proof_hash': search_proof['content_hash'],
        'target_hash': content_hash(normalized), 'model_calls': 0, 'frozen_model_state':copy.deepcopy(dict(state))}
    wire = {'schema_version': SCHEMA_VERSION, 'task_ref': TASK_REF,
        'mission_version_ref': mission['id'], 'state_hash': content_hash(origin),
        'assessment': 'Owner-derived exact operating disclosure read; material found is not model input authority.',
        'directives': [], 'inquiries': [inquiry], 'sufficiency': [], 'model_source_origin': origin}
    return {**wire, 'content_hash': content_hash(wire)}


def source_unit_target(scope: Mapping[str, Any], matches: list[Mapping[str, Any]], *, measurement_proof=None) -> dict[str, Any]:
    """Conservative preflight: never infer a price unit from a dollar amount.

    This accepts a unique supported physical denomination from an exact raw
    column/clause proof or a literal label/unit annotation. Dimensional
    compatibility does not certify metric meaning. It selects no amount or
    economic formula; both paid reader stages must
    still prove the complete target and original quotation independently.
    """
    label = scope['metric']['label']
    definitions = []
    from .operating_driver_retrieval import metric_terms
    from .operating_source_measurements import matching_measurements
    native = matching_measurements(measurement_proof,
        period=scope['period_start']+'/'+scope['period_end'], terms=metric_terms(scope['metric']))
    # A declared quantity or percent cannot be satisfied by a price that
    # happens to appear in the same source paragraph. This is dimensional
    # compatibility only; neither the native metric meaning nor any amount
    # is selected here. Monetary declarations still need one explicit native
    # per-unit denomination, not an inferred denominator.
    declared=scope['metric']['unit']
    if declared in {'tons','metric_tons','short_tons','percent','count'}:
        native=[r for r in native if r['unit']==declared]
    elif declared in {'usd','cad','eur','gbp'}:
        native=[r for r in native if r['currency']==declared.upper()]
    definitions.extend((r['unit'], r['currency']) for r in native)
    patterns = (
        ('currency_per_mmbtu', r'(USD|CAD|EUR|GBP)\s*(?:per|/)\s*MMBtu\b', True),
        ('currency_per_metric_ton', r'(USD|CAD|EUR|GBP)\s*(?:per|/)\s*metric\s+(?:ton|tonne)', True),
        ('currency_per_short_ton', r'(USD|CAD|EUR|GBP)\s*(?:per|/)\s*short\s+ton', True),
        ('currency_per_ton', r'(USD|CAD|EUR|GBP)\s*(?:per|/)\s*ton', True),
        ('metric_tons', r'\bmetric\s+(?:tons?|tonnes?)\b', False),
        ('short_tons', r'\bshort\s+tons?\b', False),
        ('tons', r'(?<!metric )(?<!short )\btons?\b', False),
        ('percent', r'\bpercent(?:age)?\b|%', False),
        ('count', r'\(\s*counts?\s*\)', False),
        ('currency', r'\b(USD|CAD|EUR|GBP)\b', True),
    )
    for match in matches:
        from .operating_driver_units import verify_reported_period
        try:
            verify_reported_period(scope['period_start']+'/'+scope['period_end'],match['excerpt'])
        except ValueError:
            continue
        for named in re.finditer(re.escape(label) + r'\s*\(([^)]+)\)',match['excerpt'],re.I):
            row = named.group(0)
            present = []
            for unit, expression, monetary in patterns:
                found = re.search(expression, row, re.I)
                if found:
                    present.append((unit, found.group(1).upper() if monetary else None))
            # Specific ratios/quantity qualifiers supersede their literal
            # currency/ton components. Ambiguous currencies remain rejected.
            if any(u.startswith('currency_per_') for u, _ in present):
                present = [p for p in present if p[0].startswith('currency_per_')]
            elif any(u in {'metric_tons', 'short_tons'} for u, _ in present):
                present = [p for p in present if p[0] != 'tons']
            if any(u in {'tons', 'metric_tons', 'short_tons'} for u, _ in present):
                present = [p for p in present if p[0] != 'count']
            if re.search(r'\bper\b|/', named.group(1), re.I) and not any(u.startswith('currency_per_') for u, _ in present):
                raise ValueError('operating source declares an unsupported compound physical unit')
            definitions.extend(present)
    identities = set(definitions)
    if len(identities) != 1:
        raise ValueError('operating source label has no unique explicit supported physical unit')
    unit, currency = identities.pop()
    ref = 'metric:operating-' + content_hash({'metric': scope['metric'], 'unit': unit, 'currency': currency})[:24]
    target = {'target_ref': 'operating-target:' + content_hash(scope)[:32], 'metric_ref': ref,
        'metric_definition': {'label': label,
            'meaning': 'The company-reported ' + label + '; preserve exact disclosed scope and this explicit unit, without conversion or estimation.'},
        'period': scope['period_start'] + '/' + scope['period_end'],
        'unit': unit, 'currency': currency, 'basis': 'management-reported'}
    from .directed_numeric_research import normalize_targets
    return normalize_targets([target], contract_ref=NUMERIC_REF)[0]


def found_requests(state_dir, *, limit=80):
    """Bounded exact request PKs, not all corpus/history scanning."""
    with _material_reader(state_dir) as connection:
        if connection is None:
            return []
        refs = connection.execute("SELECT request_id FROM material_requests WHERE "
            "json_extract(record_json,'$.acceptance.model_driver_source_request.contract_ref')=? "
            "AND (SELECT e.status FROM material_request_events e WHERE e.request_id=material_requests.request_id "
            "ORDER BY e.event_id DESC LIMIT 1)='found' ORDER BY created_at,request_id LIMIT ?", ('model-spec-joint-required-fields:0.1',limit)).fetchall()
    return [r for row in refs if (r := read_material_request(state_dir,row[0])) is not None
            and r['status'] == 'found' and r['closed']]


def admit_found_materials(*, authority, registrations, backlog, mission, state_dir,
                          model_config, limit=MAX_READS_PER_TICK):
    """Normal admission owner, same source/mission/day budget gates and slots.

    The system appends a named deterministic plan; it never forges a planner
    Work or rewrites the material request. Failures remain explicit source
    needs, and a registered inquiry is idempotent on subsequent ticks.
    """
    from .mission_document_research_admission import admit_directed_inquiry
    requests = found_requests(state_dir)
    if requests:
        # No persistent grant/cursor reset. Rotate only the bounded pure
        # eligibility reads; exact plan/admission identities remain fixed.
        offset = int(authority.clock().timestamp() // 300) % len(requests)
        requests = requests[offset:] + requests[:offset]
    # Newly found gaps retain a share even when earlier reads are already
    # registered. Checking existence never substitutes for strong replay.
    def registered(request):
        return authority.connection.execute(
            "SELECT 1 FROM coverage_mission_research_plans p JOIN mission_document_research_admissions a "
            "ON a.plan_ref=p.plan_id WHERE json_extract(p.plan_json,'$.task_ref')=? AND "
            "json_extract(p.plan_json,'$.model_source_origin.request_ref')=? LIMIT 1",
            (TASK_REF,request['id'])).fetchone() is not None
    requests.sort(key=registered)
    results = []
    attempts = 0
    for request in requests:
        docs = {item.get('document_ref') for event in request['events']
                if event.get('event_kind') == 'closed' and event.get('status') == 'found'
                for item in event.get('items',())}
        chosen = sorted((r for r in registrations.values() if r['document_ref'] in docs),key=lambda r:r['id'])
        if not chosen:
            continue
        if attempts >= limit:
            break
        attempts += 1
        try:
            prior = authority.connection.execute(
                "SELECT plan_id,plan_json FROM coverage_mission_research_plans WHERE "
                "json_extract(plan_json,'$.task_ref')=? AND "
                "json_extract(plan_json,'$.model_source_origin.request_ref')=? LIMIT 2",
                (TASK_REF,request['id'])).fetchall()
            if len(prior)>1:
                raise ValueError('operating material has ambiguous historical owner plans')
            if prior:
                plan=json.loads(prior[0]['plan_json'])
                admitted_mission=authority._missions._mission_record(plan['mission_version_ref'])
                replay_plan(authority,plan,admitted_mission,state_dir=state_dir,model_config=model_config)
                exact={**plan,'plan_id':prior[0]['plan_id']}
                # Current _mission authorization is rechecked by the normal
                # admission owner; a new day/config/mission cannot buy a new read.
                entry_mission=admitted_mission
            else:
                failures=[]
                for registration in chosen[:5]:
                    try:
                        plan = derive_current_plan(authority,mission,request_ref=request['id'],
                            registration_ref=registration['id'],state_dir=state_dir,model_config=model_config)
                        break
                    except ValueError as exc:
                        failures.append(str(exc))
                else:
                    raise ValueError('no bounded held original satisfies this operating request: '+ '; '.join(failures))
                stored = authority._missions.record_research_plan(plan,
                    decided_by=mission['autonomy']['automation_principal'])
                exact = {**plan,'plan_id':stored['plan_id']}
                entry_mission=mission
            prior_admissions=authority.connection.execute(
                'SELECT admission_id FROM mission_document_research_admissions WHERE plan_ref=? LIMIT 2',
                (exact['plan_id'],)).fetchall()
            if len(prior_admissions)>1:
                raise ValueError('operating owner plan has ambiguous durable admissions')
            if prior_admissions:
                # A new mission/day must not call record_question with the
                # old inactive binding. Reuse only after complete current
                # permission/source/model authority validation.
                admitted=authority.resolve_for_execution(prior_admissions[0]['admission_id'])
                result={'kind':'mission_directed_document_research','admission_ref':admitted['id'],
                    'company_ref':admitted['company_ref'],'status':'duplicate'}
            else:
                result = admit_directed_inquiry(authority=authority,registrations=registrations,
                    backlog=backlog,mission=entry_mission,plan=exact,inquiry=plan['inquiries'][0])
            results.append({**result,'material_request_ref':request['id'], 'source_handoff_contract':TASK_REF})
        except ValueError as exc:
            results.append({'status':'refused','material_request_ref':request['id'],
                'reason':f'{type(exc).__name__}: {exc}','source_handoff_contract':TASK_REF})
    return results


def wake_signature(state_dir, connection=None, *, now=None):
    """MR arrival wakes a free owner pass even when the brain plan is unchanged."""
    found=found_requests(state_dir)
    if connection is not None:
        tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {'coverage_mission_research_plans','mission_document_research_admissions'} <= tables:
            found=[r for r in found if connection.execute(
                "SELECT 1 FROM coverage_mission_research_plans p JOIN mission_document_research_admissions a "
                "ON a.plan_ref=p.plan_id WHERE json_extract(p.plan_json,'$.task_ref')=? AND "
                "json_extract(p.plan_json,'$.model_source_origin.request_ref')=? LIMIT 1",
                (TASK_REF,r['id'])).fetchone() is None]
    if not found:
        return None
    return {'contract_ref':TASK_REF,
        **({'eligibility_epoch':int(now.timestamp()//300)} if now is not None else {}),'found':[
        {'ref':r['id'],'hash':r['content_hash'],'last_event_at':r['last_event_at']} for r in found]}
