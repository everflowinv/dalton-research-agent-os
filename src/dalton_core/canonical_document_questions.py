"""Explicit, source-bound problem identities for follow-up original reading.

The model may reference a verified question; this module never guesses that
similar sentences or two questions about one document mean the same thing.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping

from .store import canonical_json, content_hash

FIELDS = {'version_ref', 'content_hash'}
MAX_QUESTIONS_PER_COMPANY = 4


def normalize_binding(value):
    if (not isinstance(value, Mapping) or set(value) != FIELDS
            or not isinstance(value['version_ref'], str)
            or not value['version_ref'].startswith('research-question-version:')
            or not isinstance(value['content_hash'], str)
            or re.fullmatch('[a-f0-9]{64}', value['content_hash']) is None):
        raise ValueError('canonical question requires an exact version ref/hash')
    return dict(value)


def binding_for(admission):
    declared = admission['planner_inquiry'].get('canonical_question')
    if declared is not None:
        return normalize_binding(declared)
    return {'version_ref': admission['question_version_ref'],
            'content_hash': admission['question_version_hash']}


def _read(connection, row):
    from .mission_document_research import MissionDocumentResearchAuthority
    wire = MissionDocumentResearchAuthority._read_row(None, row)
    if canonical_json(wire) != row["record_json"]:
        raise ValueError("canonical question admission bytes are not canonical")
    return wire


def resolve_binding(connection, mission, *, company_ref, binding, strategy):
    """Re-read the question and a same-lineage admission that owns its source."""
    from .document_mission_lineage import document_mission_versions
    from .research_question_backlog import read_exact_backlog_question_version
    binding = normalize_binding(binding)
    versions = set(document_mission_versions(connection, mission))
    if company_ref not in {item['company_ref'] for item in mission['universe']}:
        raise ValueError('canonical question is outside the mission company scope')
    question = read_exact_backlog_question_version(connection.cursor(), binding['version_ref'])
    if (question['content_hash'] != binding['content_hash']
            or question['company_ref'] != company_ref
            or question['actor_ref'] != mission['autonomy']['automation_principal']):
        raise ValueError('canonical question company/hash/principal differs')
    rows = connection.execute(
        'SELECT * FROM mission_document_research_admissions WHERE company_ref=? '
        'AND question_version_ref=? ORDER BY created_at DESC,admission_id',
        (company_ref, binding['version_ref'])).fetchall()
    from .coverage_mission import CoverageMissionAuthority
    missions = CoverageMissionAuthority.__new__(CoverageMissionAuthority)
    missions.connection = connection
    for row in rows:
        admission = _read(connection, row)
        if admission['mission_version_ref'] not in versions:
            continue
        original_mission = missions._mission_record(admission['mission_version_ref'])
        if original_mission['content_hash'] != admission['mission_version_hash']:
            raise ValueError('canonical admission mission hash differs')
        if (admission['question_version_hash'] != binding['content_hash']
                or admission['planner_inquiry']['question'] != question['question']
                or admission['planner_inquiry']['wants'] != question['answer_criteria']
                or question['source_refs'] != [admission['source_ref']]):
            raise ValueError('canonical question differs from its original admission')
        original = admission['planner_inquiry']['directed_document']
        same_source = all(strategy.get(k) == original.get(k)
                          for k in ('document_ref', 'document_version_hash'))
        resume = strategy.get('resumes_numeric_result') or {}
        if same_source or (resume.get('admission_ref') == admission['id']
                           and resume.get('admission_hash') == admission['content_hash']):
            return admission
    raise ValueError('canonical question lacks a same-mission exact original binding')


def validate_state_binding(inquiry, state):
    if 'canonical_question' not in inquiry:
        return None
    binding = normalize_binding(inquiry['canonical_question'])
    strategy = inquiry.get('directed_document')
    if not isinstance(strategy, Mapping):
        raise ValueError('canonical question requires directed original reading')
    company = next((c for c in state.get('companies', ())
                    if c.get('company_ref') == inquiry.get('company_ref')), None)
    rows = () if company is None else (company.get('canonical_document_questions') or {}).get('questions', ())
    for row in rows:
        if row.get('canonical_question') != binding:
            continue
        if (strategy.get('document_ref') == row.get('document_ref')
                and strategy.get('document_version_hash') == row.get('document_version_hash')):
            return binding
        resume = strategy.get('resumes_numeric_result') or {}
        if (resume.get('admission_ref') == row.get('admission_ref')
                and resume.get('admission_hash') == row.get('admission_hash')):
            return binding
    raise ValueError('canonical question/source binding is absent from verified planner state')


def read_questions(connection: Any, mission: Mapping[str, Any], *, now=None):
    """Bounded exact rows plus honest omissions; no constructors or writes."""
    from .document_mission_lineage import document_mission_versions
    from .mission_document_research import _settlement_instants
    from .mission_document_research_withdrawal import read_withdrawals
    from .document_inquiry_equivalence import ANSWERED_COOLDOWN
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mission_document_research_admissions'").fetchone() is None:
        return {}
    versions = set(document_mission_versions(connection, mission))
    withdrawals = read_withdrawals(connection)
    now = now or datetime.now(timezone.utc)
    result = {}
    from .mission_document_research import _exact_plan
    from .document_inquiry_equivalence import inquiry_signature_for, signatures_equivalent
    latest = connection.execute('SELECT plan_id FROM coverage_mission_research_plans WHERE mission_version_ref=? ORDER BY rowid DESC LIMIT 1', (mission['id'],)).fetchone()
    latest_inquiries = [] if latest is None else _exact_plan(connection, latest[0])[0]['inquiries']
    for company in mission['universe']:
        company_ref = company['company_ref']
        rows = connection.execute(
            'SELECT * FROM mission_document_research_admissions WHERE company_ref=? '
            'ORDER BY created_at DESC,admission_id DESC', (company_ref,)).fetchall()
        admissions = [_read(connection, row) for row in rows if row['mission_version_ref'] in versions]
        settled = _settlement_instants(connection, [a['id'] for a in admissions])
        projected, seen = [], set()
        from .directed_numeric_research import numeric_rejection
        for admission in admissions:
            if admission.get('numeric_authority') is not None or numeric_rejection(connection, admission['id']) is not None:
                continue # Their explicit typed replacement/resume bindings already travel separately.
            if admission['id'] in withdrawals:
                continue # Reopened tasks already have their separate explicit projection.
            binding = binding_for(admission)
            # Replayed canonical descendants retain the original question's identity.
            signature = (binding['version_ref'], binding['content_hash'])
            if signature in seen:
                continue
            at = settled.get(admission['id'])
            if at and admission['id'] not in withdrawals and now-datetime.fromisoformat(at) >= ANSWERED_COOLDOWN:
                continue
            strategy = admission['planner_inquiry'].get('directed_document')
            if not isinstance(strategy, Mapping):
                continue
            anchor = resolve_binding(connection, mission, company_ref=company_ref,
                                     binding=binding, strategy=strategy)
            seen.add(signature)
            projected.append({'canonical_question': binding,
                'question': anchor['planner_inquiry']['question'],
                'document_ref': strategy['document_ref'],
                'document_version_hash': strategy['document_version_hash'],
                'state': 'recently_settled' if at else 'in_flight',
                'referenced_by_latest_plan': any(i.get('company_ref') == company_ref
                    and (i.get('canonical_question') == binding or signatures_equivalent(
                        inquiry_signature_for(company_ref, i),
                        inquiry_signature_for(company_ref, anchor['planner_inquiry'])) is not None)
                    for i in latest_inquiries)})
        # Pending source-bound problems take precedence over old settled reads.
        # Never let recently-created completed detail crowd out an active question.
        projected.sort(key=lambda row: (row['state'] != 'in_flight',
                                        not row['referenced_by_latest_plan']))
        if projected:
            result[company_ref] = {'questions': projected[:MAX_QUESTIONS_PER_COMPANY],
                'omitted_count': max(0,len(projected)-MAX_QUESTIONS_PER_COMPANY),
                'omitted_hash': content_hash(projected[MAX_QUESTIONS_PER_COMPANY:])}
    return result
