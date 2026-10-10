"""One source-bound target revision after a formally verified zero-answer read.

The admission writer derives this proof; a planner cannot declare that a read
was incomplete or grant itself a further attempt. This is not a retry of the
old task: its complete targets and result remain unchanged.
"""
from __future__ import annotations

from typing import Any, Mapping
from .directed_numeric_research import CONTRACT_REF, normalize_targets, pending_numeric_result
from .document_inquiry_equivalence import inquiry_signature_for, signatures_equivalent
from .store import content_hash

RULE_REF = 'mission-document-numeric:zero-answer-target-revision:0.1'
PROOF_FIELDS = frozenset({'rule_ref', 'admission_ref', 'admission_hash',
                         'numeric_result_hash', 'old_target_scope_hash', 'new_target_scope_hash'})


def target_scope(targets: Any) -> list[list[Any]]:
    """Operative dimensions, excluding identifiers and definition wording.

    A renamed target or rephrased question is not a new executable scope.
    Empty/duplicate/invalid targets still fail the original closed validator.
    """
    rows = [[t[k] for k in ('metric_ref', 'period', 'unit', 'currency', 'basis')]
            for t in normalize_targets(targets)]
    return sorted(rows, key=lambda row: tuple('' if v is None else v for v in row))


def _zero_answer_parent(parent: Mapping[str, Any], result: Mapping[str, Any]) -> bool:
    """Shared prerequisite, not permission to change an inquiry or call a model."""
    old_authority = parent.get('numeric_authority')
    return bool(old_authority and result.get('numeric_authority') == old_authority
        and not result.get('completed_targets') and not result.get('canonical_claims')
        and {t['target_ref'] for t in result['pending_targets']}
            == {t['target_ref'] for t in old_authority['targets']}
        and old_authority.get('target_revision_proof') is None)


def _revision_consumers(connection, *, company_ref, admission_ref, numeric_result_hash):
    # Withdrawn consumers remain present and still spend the one revision.
    return connection.execute(
        "SELECT admission_id FROM mission_document_research_admissions WHERE company_ref=? "
        "AND json_extract(record_json,'$.numeric_authority.target_revision_proof.admission_ref')=? "
        "AND json_extract(record_json,'$.numeric_authority.target_revision_proof.numeric_result_hash')=? "
        "ORDER BY created_at, admission_id",
        (company_ref, admission_ref, numeric_result_hash)).fetchall()


def revision_source_candidate(connection, parent, result, numeric_result_hash) -> bool:
    """Planning hint only, after the caller's strong admission/final-result replay.

    A new inquiry still needs _proof_for's mission/company/question/source and
    operative-target checks, and the writer's transaction-time consumption CAS.
    """
    return bool(result.get('status') == 'waiting_evidence'
        and _zero_answer_parent(parent, result)
        and not _revision_consumers(connection, company_ref=parent['company_ref'],
            admission_ref=parent['id'], numeric_result_hash=numeric_result_hash))

def _proof_for(authority, mission_ref: str, inquiry: Mapping[str, Any], admission_ref: str):
    strategy = inquiry.get('directed_document') or {}
    if strategy.get('strategy_version') != CONTRACT_REF:
        return None
    parent = authority.admission(admission_ref)
    if (parent['mission_ref'] != mission_ref or parent['company_ref'] != inquiry['company_ref']
            or signatures_equivalent(inquiry_signature_for(inquiry['company_ref'], inquiry),
                inquiry_signature_for(parent['company_ref'], parent['planner_inquiry'])) is None):
        return None
    original = parent['request']['registration']
    if (strategy.get('document_ref') != original['document_ref']
            or strategy.get('document_version_hash') != original['content_hash']):
        return None
    pending = pending_numeric_result(authority.connection, admission_ref)
    if pending is None:
        return None
    result = pending['result']
    if not _zero_answer_parent(parent, result):
        return None
    old_authority = parent['numeric_authority']
    old_scope, new_scope = target_scope(old_authority['targets']), target_scope(strategy['numeric_targets'])
    if not ({tuple(row[1:]) for row in new_scope} - {tuple(row[1:]) for row in old_scope}):
        return None  # deleting unresolved targets is not a revision either.
    return {'rule_ref': RULE_REF, 'admission_ref': admission_ref,
            'admission_hash': parent['content_hash'],
            'numeric_result_hash': pending['numeric_result_hash'],
            'old_target_scope_hash': content_hash(old_scope),
            'new_target_scope_hash': content_hash(new_scope)}


def derive_revision_proof(authority, mission_ref: str, inquiry: Mapping[str, Any]):
    if (inquiry.get('directed_document') or {}).get('strategy_version') != CONTRACT_REF:
        return None
    from .research_task import inquiry_content_hash, inquiry_ref_for
    strategy = inquiry['directed_document']
    # Exact historical admission replay must not acquire a new identity field.
    # An already-marked admission reuses its immutable verified marker instead.
    existing = authority.connection.execute(
        "SELECT admission_id FROM mission_document_research_admissions WHERE company_ref=? "
        "AND inquiry_ref=? AND document_authority_hash=? "
        "AND json_extract(record_json,'$.mission_ref')=? ORDER BY created_at,admission_id",
        (inquiry['company_ref'], inquiry_ref_for(inquiry_content_hash(inquiry)),
         strategy['document_version_hash'], mission_ref)).fetchall()
    for row in existing:
        record = authority.admission(row[0])
        if record['request']['registration']['document_ref'] != strategy['document_ref']:
            continue
        marker = record.get('numeric_authority', {}).get('target_revision_proof')
        return None if marker is None else verify_revision_proof(authority, mission_ref, inquiry, marker)
    if authority.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mission_document_numeric_results'").fetchone() is None:
        return None  # Before the first executor opens there is no answer proof.
    # Only source-bound zero-answer numeric receipts can be candidates. These
    # SQL predicates are a prefilter, never proof: every selected row still
    # replays its exact admission and successful formal result below.
    rows = authority.connection.execute(
        "SELECT a.admission_id FROM mission_document_research_admissions a "
        "JOIN mission_document_numeric_results n ON n.admission_ref=a.admission_id "
        "WHERE a.company_ref=? AND a.document_authority_hash=? "
        "AND json_extract(a.record_json,'$.mission_ref')=? "
        "AND json_extract(a.record_json,'$.request.registration.document_ref')=? "
        "AND json_extract(n.record_json,'$.status')='waiting_evidence' "
        "AND json_array_length(n.record_json,'$.completed_targets')=0 "
        "AND json_array_length(n.record_json,'$.canonical_claims')=0 "
        "ORDER BY a.created_at,a.admission_id",
        (inquiry['company_ref'], strategy['document_version_hash'], mission_ref,
         strategy['document_ref'])).fetchall()
    for row in rows:
        proof = _proof_for(authority, mission_ref, inquiry, row[0])
        if proof is not None:
            return proof
    return None


def verify_revision_proof(authority, mission_ref, inquiry, proof):
    from .mission_document_research import MissionDocumentResearchError
    if (not isinstance(proof, Mapping) or set(proof) != PROOF_FIELDS
            or proof.get('rule_ref') != RULE_REF
            or _proof_for(authority, mission_ref, inquiry, proof['admission_ref']) != dict(proof)):
        raise MissionDocumentResearchError('numeric target revision proof differs')
    return dict(proof)


def revision_consumer(authority, proof, *, company_ref):
    """Must also run under the admission writer's BEGIN IMMEDIATE before INSERT.

    All admissions count, including withdrawn ones. A withdrawal must not
    silently replenish the same source-result's paid revision allowance.
    """
    rows = _revision_consumers(authority.connection, company_ref=company_ref,
        admission_ref=proof['admission_ref'], numeric_result_hash=proof['numeric_result_hash'])
    if not rows:
        return None
    record = authority.admission(rows[0][0])
    marker = record['numeric_authority']['target_revision_proof']
    verify_revision_proof(authority, record['mission_ref'], record['planner_inquiry'], marker)
    return record
