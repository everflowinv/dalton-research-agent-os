"""Finite free revalidation of a paid plan; never a new model permission."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from .store import canonical_json, content_hash

CONTRACT = 'planner-structural-revalidation:0.1'
MAX_PARENT_WORKS = 32


def normalize_wrappers(parsed):
    """Remove one fully redundant container, preserving every inquiry field."""
    result = copy.deepcopy(parsed)
    required = {'strategy_version','document_ref','document_version_hash','query_terms','query_rationale'}
    proofs = []
    for ordinal, inquiry in enumerate(result['inquiries']):
        if not isinstance(inquiry, dict):
            continue
        outer = inquiry.get('directed_document')
        if not isinstance(outer, dict) or 'directed_document' not in outer:
            continue
        inner = outer['directed_document']
        # Do not turn conflicting, recursive or partially specified wrappers
        # into a different instruction. The ordinary closed validator refuses.
        if (not isinstance(inner, dict) or 'directed_document' in inner
                or not required <= set(outer) or not required <= set(inner)
                or set(outer) - {'directed_document'} - set(inner)
                or any(canonical_json(outer[k]) != canonical_json(inner[k])
                       for k in set(outer)-{'directed_document'})):
            continue
        inquiry['directed_document'] = copy.deepcopy(inner)
        proofs.append({'inquiry_index':ordinal, 'before_hash':content_hash(outer),
                       'after_hash':content_hash(inner)})
    return result, proofs


def current_numeric_canonical_binding(connection, mission, inquiry, state):
    """A displayed current backlog head plus an exact original owner binding."""
    from .canonical_document_questions import normalize_binding, resolve_binding
    from .mission_document_research_withdrawal import read_withdrawals
    binding = normalize_binding(inquiry.get('canonical_question'))
    strategy = inquiry.get('directed_document')
    if not isinstance(strategy, dict) or not strategy.get('numeric_targets'):
        raise ValueError('missing canonical source proof is not a typed inquiry')
    rows = (state.get('reflection_followup_context') or {}).get('backlog_questions') or []
    matches = [row for row in rows if row.get('state') == 'open'
        and (row.get('head') or {}).get('id') == binding['version_ref']
        and (row.get('head') or {}).get('content_hash') == binding['content_hash']
        and (row.get('head') or {}).get('company_ref') == inquiry.get('company_ref')]
    if len(matches) != 1:
        raise ValueError('canonical typed inquiry lacks one exact current backlog head')
    from .research_question_backlog import ResearchQuestionBacklog, read_exact_backlog_question_version
    version = read_exact_backlog_question_version(connection.cursor(), binding['version_ref'])
    backlog = ResearchQuestionBacklog.__new__(ResearchQuestionBacklog)
    backlog.connection = connection
    current = backlog.question(version['question_ref'])
    if (current['state'] != 'open' or current['head']['id'] != binding['version_ref']
            or current['head']['content_hash'] != binding['content_hash']):
        raise ValueError('canonical typed inquiry is no longer the current open owner head')
    admission = resolve_binding(connection, mission, company_ref=inquiry['company_ref'],
                                binding=binding, strategy=strategy)
    if (not admission.get('numeric_authority') or admission['id'] in read_withdrawals(connection)):
        raise ValueError('canonical typed original is not an active numeric source binding')
    # This names a question and its source, not equivalence of target metrics
    # and never a reuse of the admission's paid or recovery authority.
    return binding


def paid_parent(connection, router, budget, *, state, mission, validate):
    """Bounded immutable paid output over the exact current FULL state only."""
    from .contracts import WorkOrder
    from .model_spec_complete_diagnostics import strong_result
    from .planner_paid_output_recovery import _router_record
    from .research_state import state_content_hash
    if state.get('prompt_projection') or state_content_hash(state) != state.get('content_hash'):
        raise ValueError('planner revalidation requires the complete canonical current state')
    if budget is None:
        return None
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='scheduler_work_orders'").fetchone() is None:
        return None
    rows = connection.execute("SELECT * FROM scheduler_work_orders "
        "WHERE work_order_id GLOB 'work:cockpit-plan-*' ORDER BY rowid DESC LIMIT ?",
        (MAX_PARENT_WORKS,)).fetchall()
    for row in rows:
        wire = json.loads(row['work_order_json'])
        work = WorkOrder.from_dict(wire)
        meta = work.metadata
        if (meta.get('purpose') != 'plan' or meta.get('control_plane') != 'cockpit'
                or meta.get('mission_version_ref') != mission['id']
                or (meta.get('mission_version_hash') is not None
                    and meta['mission_version_hash'] != mission['content_hash'])):
            continue
        try:
            frozen = json.loads(work.question.rsplit('\nRESEARCH_STATE=',1)[1])
        except (IndexError, ValueError):
            continue
        if (frozen.get('content_hash') != state['content_hash']
                or (frozen.get('prompt_projection') or {}).get('full_state_hash',
                       frozen.get('content_hash')) != state['content_hash']
                or (frozen.get('goal') or {}).get('mission_version_ref') != mission['id']):
            continue
        if (canonical_json(wire) != row['work_order_json']
                or content_hash(wire) != row['work_order_hash'] or work.id != row['work_order_id']):
            raise ValueError('planner revalidation Work hash differs')
        formal = strong_result(connection, work.id)
        if formal is None or formal['terminal_state'] != 'succeeded':
            continue
        envelope = formal['result_envelope']; text = envelope['outputs'].get('text')
        if not isinstance(text, str):
            continue
        route_ref = envelope['metadata'].get('route_decision_ref')
        if not isinstance(route_ref, str):
            continue
        route = _router_record(router,'model_route_decisions','decision_id','decision_json','decision_hash',route_ref)
        profile = _router_record(router,'model_endpoint_profile_versions','profile_version_ref',
                                 'profile_json','profile_hash',route['selected_profile_version_ref'])
        policy = _router_record(router,'model_routing_policy_versions','policy_version_ref',
                                'policy_json','policy_hash',route['policy_version_ref'])
        if (route['work_order_ref'] != work.id or route['work_order_hash'] != row['work_order_hash']
                or route['attempt_number'] != formal['attempt_number'] or route['outcome'] != 'selected'
                or route['policy_hash'] != policy['content_hash']
                or envelope['metadata'].get('profile_version_ref') != profile['profile_version_ref']
                or envelope['metadata'].get('thinking_level') != route['thinking_level']):
            raise ValueError('planner revalidation route differs from its formal output')
        admitted = budget.connection.execute('SELECT attempt_number,phase FROM thesis_impact_day_admissions '
            'WHERE work_order_ref=? ORDER BY attempt_number,phase LIMIT 65',(work.id,)).fetchall()
        if not admitted or len(admitted) > 64:
            return None
        fees = []
        selected_fee = None
        for fee_row in admitted:
            paid = budget.admission(work_order_ref=work.id,attempt_number=fee_row['attempt_number'],phase=fee_row['phase'])
            if paid is None or paid['settlement'] is None or ((paid.get('mission_binding') or {}).get('mission_version_ref') != mission['id']
                        or (paid.get('mission_binding') or {}).get('mission_version_hash') != mission['content_hash']):
                return None  # Preserve unknown/in-flight reservations; no new call.
            accounted = paid['settlement'].get('actual_micros')
            if isinstance(accounted, bool) or not isinstance(accounted, int) or accounted < 0:
                return None
            # This is the unchanged owner's settled ledger amount, possibly
            # a conservative estimate; it does not certify supplier telemetry.
            entry = {'admission_ref':paid['admission']['admission_id'],
                'admission_hash':paid['admission']['content_hash'],
                'settlement_ref':paid['settlement']['settlement_id'],
                'settlement_hash':paid['settlement']['content_hash'],
                'accounted_micros':accounted}
            fees.append(entry)
            if fee_row['attempt_number']==formal['attempt_number'] and fee_row['phase']=='assessment':
                if paid['admission']['route_decision_ref'] != route_ref:
                    raise ValueError('planner revalidation fee route differs')
                selected_fee = entry
        if selected_fee is None:
            return None
        try:
            plan = validate(text)
        except ValueError:
            continue  # Genuine source/content errors remain refused whole.
        proof = {'contract_ref':CONTRACT, 'state_hash':state['content_hash'],
            'mission_ref':mission['id'], 'mission_hash':mission['content_hash'],
            'work_ref':work.id,'work_hash':row['work_order_hash'],
            'formal_hash':formal['content_hash'],'result_hash':formal['result_envelope_hash'],
            'route_ref':route_ref,'route_hash':route['content_hash'],'fees':fees,
            'plan_hash':plan['content_hash'],'model_calls_allowed':0}
        return {'plan':plan,'proof':{**proof,'content_hash':content_hash(proof)}}
    return None


def candidate(*, state_dir, scheduler_db, config, state, mission, validate):
    from .planner_paid_output_recovery import _authority_connection, recovery_readers
    now = datetime.now(timezone.utc)
    with _authority_connection(scheduler_db or Path(state_dir)/'scheduler.sqlite') as connection, \
            recovery_readers(config['model_router_db'],config['budget_db'],now=now) as (router,budget):
        import sqlite3
        connection.row_factory=sqlite3.Row
        return paid_parent(connection,router,budget,state=state,mission=mission,validate=validate)


def input_signature(connection):
    """Strong bounded current planner inputs; a wake hint grants no spending."""
    import sqlite3
    from .coverage_mission import CoverageMissionAuthority
    from .initial_screen_quality_feedback import read_feedback
    from .research_financial_state import _financial_state
    try:
        row = connection.execute('SELECT mission_version_id FROM coverage_mission_pointer '
            'ORDER BY mission_ref LIMIT 1').fetchone()
        if row is None:
            return content_hash({'status':'no_mission'})
        reader = CoverageMissionAuthority.__new__(CoverageMissionAuthority)
        reader.connection = connection
        mission = reader._mission_record(row[0])
        if len(mission['universe']) > 128:
            return content_hash({'status':'scope_exceeds_bound'})
        rows=[]
        for company in mission['universe']:
            ref=company['company_ref']
            try:
                quality=read_feedback(connection,company_ref=ref)
            except (ValueError,sqlite3.Error,KeyError):
                quality={'status':'unverified'}
            financial,_=_financial_state(connection,ref,mission)
            rows.append({'company_ref':ref,'quality':quality,
                'financial':{k:financial[k] for k in ('status','model_version_ref',
                    'model_content_hash','mission_version_ref','current_mission') if k in financial}})
        return content_hash({'mission_ref':mission['id'],'mission_hash':mission['content_hash'],'inputs':rows})
    except (ValueError,sqlite3.Error,KeyError):
        return content_hash({'status':'unavailable'})
