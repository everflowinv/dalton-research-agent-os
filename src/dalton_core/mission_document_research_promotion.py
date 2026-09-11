"""Canonical promotion of an exactly executed directed-document candidate.

The live executor is an in-process capability, never a JSON argument accepted
from a candidate submitter. All stored source, model, budget and staging
authority is replayed; the existing governance rule remains the write gate.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .store import canonical_json, content_hash

_SCHEMA_PATH = Path(__file__).with_name("mission_document_research_promotion_schema.sql")
_RATIONALE = "Exact mission-directed original-document research with independent model verification, source replay and accounted execution (ADR-0005)."


def _need(condition: Any, reason: str) -> None:
    from .research_auto_commit import ResearchAutoCommitRejected
    if not condition:
        raise ResearchAutoCommitRejected(reason)


def _record(row: Any, label: str) -> dict[str, Any]:
    from .research_auto_commit import _record as exact_record
    return exact_record(row, label)


def _context_proof(context: Any, *, store: Any, connection: Any,
                   admission_ref: str) -> tuple[dict, list, dict, dict, dict]:
    from .mission_document_research_executor import (
        MissionDocumentResearchExecutor, exact_mission_document_model_execution_authority,
        effective_mission_document_work_orders, _ref,
    )
    from .research_auto_commit import _ReadOnlySchedulerAuthority
    from .model_router import independent_families
    from .research_verification import CandidateStagingStore

    _need(type(context) is MissionDocumentResearchExecutor,
          "directed document promotion requires the live executor capability")
    _need(type(context.staging) is CandidateStagingStore,
          "directed document staging is not the trusted staging store")
    _need(context.authority.store is store and store.connection is connection
          and context.connection is connection and context.scheduler.connection is connection
          and context.registry is context.authority.registry,
          "directed document promotion has a different Core or source authority")
    for worker in (context.draft_worker, context.verifier_worker):
        _need(worker.store is store and worker.scheduler is context.scheduler
              and worker.mission_document_research_authority is context.authority,
              "directed document model worker authority differs")
    admission = context.authority.resolve_for_execution(admission_ref)
    mission = context.authority.active_budget_mission(admission_ref)
    _need({"claim", "evidence", "research_task"}.issubset(mission["autonomy"]["may_write"]),
          "mission does not grant canonical directed research writes")
    works = effective_mission_document_work_orders(
        context.authority, context.scheduler, admission_ref,
        draft_worker=context.draft_worker, verifier_worker=context.verifier_worker)
    _need(len(works) == 4, "directed document execution has not completed all four stages")
    scheduler = _ReadOnlySchedulerAuthority(connection)
    formals = [scheduler.formal_result(work["id"]) for work in works]
    _need(all(formal is not None and formal["terminal_state"] == "succeeded"
              for formal in formals), "directed document stage is not formally complete")
    for index in (0, 3):
        context._stage_owned(works[index], formals[index])
    draft_authority = exact_mission_document_model_execution_authority(works[1], formals[1], context.draft_worker)
    verifier_authority = exact_mission_document_model_execution_authority(works[2], formals[2], context.verifier_worker)
    draft, verifier = draft_authority["model_result"], verifier_authority["model_result"]
    _need(independent_families(verifier["model_family"], draft["model_family"]),
          "directed document verifier is not independent")
    source_proof = context.registry.verify_search_proof(works[3]["metadata"]["retrieval_proof"])
    _need(canonical_json(source_proof["request"]) == canonical_json(admission["request"]),
          "directed document source replay differs from admission")
    records = context._validate_records(admission, works, formals[3]["result_envelope"]["outputs"])
    bundle = context.staging.exact_candidate_bundle(
        evidence_ref=records["candidate_evidence_ref"], claim_ref=records["candidate_claim_ref"],
        idempotency_key=f"mission-document-research-candidate:{admission_ref}")
    outcome_row = connection.execute(
        "SELECT * FROM mission_document_research_outcomes WHERE admission_ref=?",
        (admission_ref,)).fetchone()
    outcome = _record(outcome_row, "directed document staged outcome")
    start_ref = _ref("mission-document-research-start", context._run_id(admission))
    expected_outcome = {"schema_version": "0.1",
        "id": _ref("mission-document-research-outcome", {"start": start_ref, **records}),
        "start_ref": start_ref, "admission_ref": admission_ref, **records,
        "created_at": admission["created_at"]}
    expected_outcome["content_hash"] = content_hash(expected_outcome)
    _need(canonical_json(outcome) == canonical_json(expected_outcome)
          and outcome_row["outcome_id"] == outcome["id"]
          and all(outcome_row[key] == outcome[key] for key in outcome_row.keys()
                  if key in outcome),
          "directed document outcome differs from formal staging")
    proof = {"schema_version": "0.1", "admission_ref": admission_ref,
        "admission_hash": admission["content_hash"], "mission_ref": mission["mission_ref"],
        "mission_version_ref": mission["id"], "mission_version_hash": mission["content_hash"], "outcome_ref": outcome["id"],
        "outcome_hash": outcome["content_hash"],
        "stages": [{"work_ref": work["id"], "work_hash": content_hash(work),
            "formal_ref": formal["result_record_id"], "formal_hash": formal["content_hash"],
            "result_ref": formal["result_envelope_id"], "result_hash": formal["result_envelope_hash"]}
            for work, formal in zip(works, formals, strict=True)],
        "model_proofs": [{"proof_ref": value["id"], "proof_hash": value["content_hash"],
                          "route_ref": value["route_decision_ref"],
                          "invocation_ref": value["model_invocation_ref"]} for value in (draft, verifier)],
        "accounting_proofs": [value["execution_proof"] for value in (draft_authority, verifier_authority)],
        "registration_hash": source_proof["request"]["registration"]["content_hash"],
        "source_proof_ref": source_proof["id"], "source_proof_hash": source_proof["content_hash"],
        "bundle_hashes": {key: bundle[key]["content_hash"]
                          for key in ("evidence", "claim", "material", "source_verification")}}
    proof["content_hash"] = content_hash(proof)
    return admission, works, bundle, outcome, proof


def authorize_document_candidate(*, connection, store, context, policy_version,
                                 evidence, claim, material, source_verification):
    from .research_auto_commit import _decision, DOCUMENT_QUALITATIVE_RULE_REF

    _need(isinstance(material, Mapping), "directed document material is unavailable")
    payload = material.get("normalized_payload", {})
    binding = payload.get("mission_document_admission", {}) if isinstance(payload, Mapping) else {}
    _need(isinstance(binding, Mapping) and isinstance(binding.get("ref"), str),
          "directed document candidate has no exact admission binding")
    _admission, _works, bundle, _outcome, _proof = _context_proof(
        context, store=store, connection=connection, admission_ref=binding["ref"])
    supplied = {"evidence": evidence, "claim": claim, "material": material,
                "source_verification": source_verification}
    _need(all(canonical_json(value) == canonical_json(bundle[key]) for key, value in supplied.items()),
          "directed document candidate differs from its exact executed staging bundle")
    return _decision(policy_version, claim_wire=claim, evidence_wire=evidence,
        rule_ref=DOCUMENT_QUALITATIVE_RULE_REF,
        rationale=_RATIONALE)


def persist_document_promotion(cursor, executor, decision, evidence, claim, material):
    """Persist proof in the same Core transaction as Evidence, Claim and decision."""
    _need(cursor.connection is executor.connection and executor.connection.in_transaction,
          "document promotion requires the active Core Ledger transaction")
    admission_ref = material["normalized_payload"]["mission_document_admission"]["ref"]
    admission, _works, bundle, outcome, proof = _context_proof(
        executor, store=executor.authority.store, connection=executor.connection,
        admission_ref=admission_ref)
    from .research_auto_commit import _decision, DOCUMENT_QUALITATIVE_RULE_REF

    expected_decision = _decision(
        executor.authority.store.active_policy(),
        claim_wire=bundle["claim"], evidence_wire=bundle["evidence"],
        rule_ref=DOCUMENT_QUALITATIVE_RULE_REF, rationale=_RATIONALE,
    )
    _need(canonical_json(material) == canonical_json(bundle["material"]),
          "document promotion material differs from exact staging")
    _need(canonical_json(decision) == canonical_json(expected_decision),
          "document promotion decision differs from exact authorization")
    _need(
        evidence.get("candidate_origin_ref") == bundle["evidence"]["id"]
        and evidence.get("candidate_origin_hash") == bundle["evidence"]["content_hash"]
        and evidence.get("review_decision_ref") == decision["id"]
        and evidence.get("review_decision_hash") == decision["content_hash"]
        and all(evidence.get(key) == bundle["evidence"].get(key) for key in (
            "source_type", "source_ref", "source_envelope_ref", "source_envelope_hash",
            "retrieved_at", "valid_until", "artifact_refs", "source_lineage",
            "independence_group", "source_verification_ref", "source_verification_hash",
        )),
        "document promotion Evidence differs from exact candidate",
    )
    _need(
        claim.get("candidate_origin_ref") == bundle["claim"]["id"]
        and claim.get("candidate_origin_hash") == bundle["claim"]["content_hash"]
        and claim.get("semantic_review_ref") == decision["id"]
        and claim.get("semantic_review_hash") == decision["content_hash"]
        and claim.get("producer_execution_refs") == [
            bundle["material"]["normalized_payload"]["draft_proof"]["model_invocation_ref"]
        ]
        and all(claim.get(key) == bundle["claim"].get(key) for key in (
            "subject_ref", "metric_or_aspect", "period", "basis", "normalized_statement",
            "claim_kind", "value", "unit", "currency", "scale",
        )),
        "document promotion Claim differs from exact candidate",
    )

    stored_evidence = _record(cursor.execute(
        "SELECT evidence_json AS record_json,content_hash FROM evidence_versions "
        "WHERE evidence_version_id=?", (evidence["id"],),
    ).fetchone(), "document promotion Evidence")
    stored_claim = _record(cursor.execute(
        "SELECT claim_json AS record_json,content_hash FROM claim_versions "
        "WHERE claim_version_id=?", (claim["id"],),
    ).fetchone(), "document promotion Claim")
    receipt = cursor.execute(
        "SELECT * FROM reviewed_candidate_commits WHERE review_decision_ref=?",
        (decision["id"],),
    ).fetchone()
    _need(receipt is not None, "document promotion Ledger receipt is unavailable")
    result = json.loads(receipt["result_json"])
    _need(
        canonical_json(stored_evidence) == canonical_json(evidence)
        and canonical_json(stored_claim) == canonical_json(claim)
        and receipt["decision_json"] == canonical_json(decision)
        and receipt["candidate_evidence_ref"] == bundle["evidence"]["id"]
        and receipt["candidate_claim_ref"] == bundle["claim"]["id"]
        and receipt["request_hash"] == content_hash({
            "decision_hash": decision["content_hash"],
            "evidence_hash": bundle["evidence"]["content_hash"],
            "claim_hash": bundle["claim"]["content_hash"],
        })
        and result.get("review_decision_ref") == decision["id"]
        and result.get("evidence_version_ref") == evidence["id"]
        and result.get("claim_version_ref") == claim["id"],
        "document promotion inputs are not the exact active Ledger transaction",
    )
    saved = {"schema_version": "0.1",
        "id": "mission-document-research-promotion:" + content_hash({
            "outcome_ref": outcome["id"], "authorization_hash": decision["content_hash"]})[:32],
        "admission_ref": admission["id"], "outcome_ref": outcome["id"],
        "research_status": "canonical_claim_promoted", "execution_proof": proof,
        "evidence_version_ref": evidence["id"], "evidence_version_hash": evidence["content_hash"],
        "claim_version_ref": claim["id"], "claim_version_hash": claim["content_hash"],
        "policy_authorization_ref": decision["id"], "policy_authorization_hash": decision["content_hash"],
        "created_at": decision["created_at"]}
    saved["content_hash"] = content_hash(saved)
    _need(not executor._authorized, "document executor authority is already in use")
    executor._authorized = True
    try:
        cursor.execute("INSERT INTO mission_document_research_promotions VALUES(?,?,?,?,?,?)",
            (saved["id"], saved["admission_ref"], saved["outcome_ref"], canonical_json(saved),
             saved["content_hash"], saved["created_at"]))
    finally:
        executor._authorized = False


def promote_document_candidate(executor, admission, works, records, outcome):
    from .research_auto_commit import policy_lists_document_rule, validate_policy_commit_decision

    if not policy_lists_document_rule(executor.authority.store.active_policy()):
        return outcome
    connection = executor.connection
    _need(not connection.in_transaction, "document promotion cannot nest an open transaction")
    connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    exact_admission, exact_works, bundle, exact_outcome, proof = _context_proof(
        executor, store=executor.authority.store, connection=connection, admission_ref=admission["id"])
    _need(canonical_json(admission) == canonical_json(exact_admission)
          and canonical_json(works) == canonical_json(exact_works)
          and outcome.get("outcome_ref") == exact_outcome["id"]
          and outcome.get("outcome_hash") == exact_outcome["content_hash"]
          and all(exact_outcome.get(key) == value for key, value in records.items()),
          "document promotion input differs from exact staged execution")
    existing_row = connection.execute(
        "SELECT * FROM mission_document_research_promotions WHERE admission_ref=?", (admission["id"],)).fetchone()
    if existing_row is not None:
        saved = _record(existing_row, "directed document promotion")
        _need(saved["id"] == existing_row["promotion_id"]
              and saved["admission_ref"] == existing_row["admission_ref"]
              and saved["outcome_ref"] == existing_row["outcome_ref"]
              and saved["created_at"] == existing_row["created_at"]
              and saved["execution_proof"] == proof, "stored directed document promotion drifted")
    else:
        result = executor.authority.store.commit_policy_candidate(
            evidence=bundle["evidence"], claim=bundle["claim"], material=bundle["material"],
            source_verification=bundle["source_verification"], numeric_spec=None, numeric_verification=None,
            idempotency_key=f"policy-ledger:mission-document-research:{admission['id']}",
            document_execution_context=executor)
        _need(result["status"] in {"fresh", "duplicate"}, "document Ledger commit did not converge")
        saved_row = connection.execute(
            "SELECT * FROM mission_document_research_promotions WHERE admission_ref=?",
            (admission["id"],)).fetchone()
        saved = _record(saved_row, "atomic directed document promotion")

    evidence_row = connection.execute(
        "SELECT evidence_json AS record_json,content_hash FROM evidence_versions WHERE evidence_version_id=?", (saved["evidence_version_ref"],)).fetchone()
    evidence = _record(evidence_row, "canonical document Evidence")
    claim = _record(connection.execute(
        "SELECT claim_json AS record_json,content_hash FROM claim_versions WHERE claim_version_id=?",
        (saved["claim_version_ref"],)).fetchone(), "canonical document Claim")
    decision_row = connection.execute(
        "SELECT * FROM reviewed_candidate_commits WHERE review_decision_ref=?",
        (saved["policy_authorization_ref"],)).fetchone()
    decision = validate_policy_commit_decision(json.loads(decision_row["decision_json"]) if decision_row else {})
    from .research_auto_commit import _decision, DOCUMENT_QUALITATIVE_RULE_REF
    expected_decision = _decision(executor.authority.store.active_policy(),
        claim_wire=bundle["claim"], evidence_wire=bundle["evidence"],
        rule_ref=DOCUMENT_QUALITATIVE_RULE_REF, rationale=_RATIONALE)
    result = json.loads(decision_row["result_json"])
    _need(canonical_json(decision) == decision_row["decision_json"]
        and canonical_json(result) == decision_row["result_json"]
        and decision_row["request_hash"] == content_hash({
            "decision_hash": decision["content_hash"], "evidence_hash": bundle["evidence"]["content_hash"],
            "claim_hash": bundle["claim"]["content_hash"]})
        and decision_row["candidate_evidence_ref"] == bundle["evidence"]["id"]
        and decision_row["candidate_claim_ref"] == bundle["claim"]["id"]
        and decision_row["created_at"] == decision["created_at"]
        and decision_row["idempotency_key"] == f"policy-ledger:mission-document-research:{admission['id']}"
        and result.get("idempotency_key") == decision_row["idempotency_key"]
        and result.get("review_decision_ref") == decision["id"]
        and result.get("evidence_version_ref") == evidence["id"] == saved["evidence_version_ref"]
        and result.get("claim_version_ref") == claim["id"] == saved["claim_version_ref"]
        and saved["execution_proof"] == proof,
        "document Ledger receipt differs from exact candidate transaction")
    _need(set(saved) == {"schema_version", "id", "admission_ref", "outcome_ref",
        "research_status", "execution_proof", "evidence_version_ref", "evidence_version_hash",
        "claim_version_ref", "claim_version_hash", "policy_authorization_ref",
        "policy_authorization_hash", "created_at", "content_hash"}
        and saved["schema_version"] == "0.1"
        and saved["research_status"] == "canonical_claim_promoted"
        and saved["id"] == "mission-document-research-promotion:" + content_hash({
            "outcome_ref": exact_outcome["id"], "authorization_hash": decision["content_hash"]})[:32]
        and canonical_json(decision) == canonical_json(expected_decision)
        and saved["policy_authorization_ref"] == decision["id"]
        and saved["created_at"] == decision["created_at"]
        and saved["evidence_version_ref"] == "evidence-version:" + content_hash({
            "candidate": bundle["evidence"]["id"], "review": decision["id"]})
        and saved["claim_version_ref"] == "claim-version:" + content_hash({
            "candidate": bundle["claim"]["id"], "review": decision["id"]}),
        "document promotion receipt differs from exact authorization")
    _need(evidence["content_hash"] == saved["evidence_version_hash"]
          and claim["content_hash"] == saved["claim_version_hash"]
          and decision["content_hash"] == saved["policy_authorization_hash"],
          "canonical document promotion authority drifted")
    return {**outcome, "research_status": "canonical_claim_promoted",
        "promotion_ref": saved["id"], "promotion_hash": saved["content_hash"],
        "evidence_version_ref": saved["evidence_version_ref"], "claim_version_ref": saved["claim_version_ref"]}
