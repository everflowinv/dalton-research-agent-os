"""Read-only authority for one promoted, typed financial-note result.

This module deliberately does not interpret note text as a number.  It binds
the exact passages cited by a promoted directed-document result to the exact
SEC statement filing and period selected before admission.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from typing import Any

from .company_model_series import (
    ANNUAL_MAX_DAYS, NINE_MONTH_MAX_DAYS, QUARTER_MAX_DAYS, QUARTER_MIN_DAYS,
)
from .contracts import ModelInvocation, ResultEnvelope, WorkOrder
from .document_research import DocumentResearchRegistry, validate_registration
from .document_research_inventory import financial_note_targets_for_registration
from .document_research_strategy import (
    FINANCIAL_NOTE_TARGET_REF, FINANCIAL_NOTE_TARGET_SCHEMA_VERSION,
    validate_financial_note_target as _validate_financial_note_target,
)
from .research_auto_commit import validate_policy_commit_decision
from .research_verification import CandidateStagingStore
from .store import canonical_json, content_hash

TARGET_SCHEMA_VERSION = FINANCIAL_NOTE_TARGET_SCHEMA_VERSION
TARGET_REF = FINANCIAL_NOTE_TARGET_REF
EVIDENCE_SCHEMA_VERSION = "financial-note-evidence-authority-0.1"
BINDING_SCHEMA_VERSION = "financial-note-evidence-binding-0.1"


class FinancialNoteEvidenceError(RuntimeError):
    pass


def _need(condition: Any, reason: str) -> None:
    if not condition:
        raise FinancialNoteEvidenceError(reason)


def _text(value: Any, name: str) -> str:
    _need(isinstance(value, str) and bool(value), f"{name} is invalid")
    return value


def validate_financial_note_target(value: Mapping[str, Any]) -> dict[str, Any]:
    """Translate the shared planner validator to this authority's error type."""

    try:
        return _validate_financial_note_target(value)
    except Exception as exc:
        raise FinancialNoteEvidenceError(str(exc)) from exc


def _record(row: Any, *, json_column: str, hash_column: str, label: str) -> dict[str, Any]:
    _need(row is not None, f"{label} is unavailable")
    try:
        wire = json.loads(row[json_column])
    except (TypeError, ValueError, RecursionError) as exc:
        raise FinancialNoteEvidenceError(f"{label} is invalid") from exc
    _need(isinstance(wire, Mapping)
          and canonical_json(wire) == row[json_column]
          and wire.get("content_hash") == row[hash_column]
          and content_hash({key: item for key, item in wire.items()
                            if key != "content_hash"}) == row[hash_column],
          f"{label} drifted")
    return dict(wire)


class _ReadOnlyCandidateView:
    """Reuse the canonical bundle validator without running its DDL constructor."""

    exact_candidate_bundle = CandidateStagingStore.exact_candidate_bundle

    def __init__(self, connection: Any) -> None:
        self.connection = connection


def _period_kind(period_start: str, period_end: str) -> str | None:
    """Classify an exact duration using the shared inclusive-day convention."""

    elapsed = (date.fromisoformat(period_end) - date.fromisoformat(period_start)).days + 1
    if NINE_MONTH_MAX_DAYS < elapsed <= ANNUAL_MAX_DAYS:
        return "annual"
    if QUARTER_MIN_DAYS <= elapsed <= QUARTER_MAX_DAYS:
        return "quarter"
    return None


def _statement_filing(connection: Any, target: Mapping[str, Any],
                      *, company_ref: str, registration: Mapping[str, Any]) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM coverage_mission_statement_filings WHERE ingest_id=?",
        (target["statement_ingest_ref"],),
    ).fetchone()
    _need(row is not None, "statement filing authority is unavailable")
    source_refs = json.loads(row["source_record_refs_json"])
    from .company_model_annual_projection import verify_statement_filing
    try:
        verify_statement_filing(connection, dict(row))
    except Exception as exc:
        raise FinancialNoteEvidenceError(
            "statement filing authority drifted") from exc
    _need(row["company_ref"] == company_ref
          and row["content_hash"] == target["statement_filing_hash"]
          and row["accession"] == target["accession"]
          and row["form"] == target["form"]
          and registration["document_ref"] == f"sec:filing:{row['accession']}",
          "statement filing and registered original disagree")
    lines = connection.execute(
        "SELECT * FROM coverage_mission_statement_lines WHERE ingest_id=?",
        (row["ingest_id"],),
    ).fetchall()
    for item in target["periods"]:
        selected = [line for line in lines
                    if line["period_start"] == item["period_start"]
                    and line["period_end"] == item["period_end"]]
        eps_rows = [line for line in selected if not bool(line["is_breakdown"])
                    and str(line["concept"]).split(":")[-1]
                    == "EarningsPerShareDiluted"]
        share_rows = [line for line in selected if not bool(line["is_breakdown"])
                      and str(line["concept"]).split(":")[-1]
                      == "WeightedAverageNumberOfDilutedSharesOutstanding"]
        _need(len(eps_rows) == 1 and len(share_rows) == 1,
              "financial note period has ambiguous diluted EPS authority")
        eps_unit = str(eps_rows[0]["unit"]).casefold()
        normalized_eps_unit = "".join(character for character in eps_unit
                                      if character.isalnum())
        _need(normalized_eps_unit.endswith("pershare")
              and normalized_eps_unit != "pershare",
              "financial note period lacks filed diluted EPS authority")
        _need(str(share_rows[0]["unit"]).casefold() == "shares",
              "financial note period lacks filed diluted share authority")
        _need(_period_kind(item["period_start"], item["period_end"])
              == target["applicability_kind"],
              "financial note applicability differs from its exact duration")
    return {
        "ref": row["ingest_id"], "hash": row["content_hash"],
        "company_ref": row["company_ref"], "cik": row["cik"],
        "accession": row["accession"], "form": row["form"],
        "filed": row["filed"], "report_date": row["report_date"],
        "source_record_refs": source_refs,
    }


def _execution_checkpoint(core: Any, router: Any, proof: Mapping[str, Any],
                          *, material: Mapping[str, Any]) -> dict[str, Any]:
    proof_body = dict(proof)
    asserted_proof_hash = proof_body.pop("content_hash", None)
    _need(asserted_proof_hash == content_hash(proof_body),
          "promotion execution proof drifted")
    stages = proof.get("stages")
    _need(isinstance(stages, list) and len(stages) == 4,
          "promotion execution stages are invalid")
    expected_stages = [
        "registered_document_retrieval", "qualitative_model_draft",
        "independent_qualitative_verifier", "qualitative_candidate_staging",
    ]
    admission_ref = material.get("normalized_payload", {}).get(
        "mission_document_admission", {}).get("ref")
    accounting_proofs = proof.get("accounting_proofs")
    _need(isinstance(accounting_proofs, list) and len(accounting_proofs) == 2
          and all(isinstance(item, Mapping) for item in accounting_proofs)
          and len({item.get("work_order_ref") for item in accounting_proofs}) == 2,
          "promotion accounting checkpoints are invalid")
    model_by_work = {item.get("work_order_ref"): item for item in accounting_proofs}
    _need(len({stage.get("work_ref") for stage in stages}) == 4
          and len({stage.get("formal_ref") for stage in stages}) == 4
          and len({stage.get("result_ref") for stage in stages}) == 4,
          "promotion execution stages are not unique")
    checked = []
    for index, stage in enumerate(stages):
        _need(isinstance(stage, Mapping), "promotion execution stage is invalid")
        work_row = core.execute(
            "SELECT * FROM scheduler_work_orders WHERE work_order_id=?",
            (stage.get("work_ref"),),
        ).fetchone()
        formal_row = core.execute(
            "SELECT * FROM scheduler_formal_results WHERE result_record_id=?",
            (stage.get("formal_ref"),),
        ).fetchone()
        result_row = core.execute(
            "SELECT * FROM scheduler_result_envelopes WHERE result_envelope_id=?",
            (stage.get("result_ref"),),
        ).fetchone()
        _need(work_row is not None and formal_row is not None and result_row is not None,
              "promotion execution authority is unavailable")
        try:
            work = WorkOrder.from_dict(json.loads(work_row["work_order_json"])).to_dict()
            formal_envelope = ResultEnvelope.from_dict(
                json.loads(formal_row["result_envelope_json"])).to_dict()
            envelope = ResultEnvelope.from_dict(
                json.loads(result_row["result_envelope_json"])).to_dict()
        except Exception as exc:
            raise FinancialNoteEvidenceError("promotion execution authority is invalid") from exc
        formal_hash = content_hash({
            "id": formal_row["result_record_id"],
            **{key: formal_row[key] for key in (
                "work_order_id", "attempt_number", "result_envelope_id",
                "result_envelope_hash", "terminal_state", "created_at")},
        })
        receipt_hash = content_hash({
            "result_envelope_id": result_row["result_envelope_id"],
            "work_order_id": result_row["work_order_id"],
            "attempt_number": result_row["attempt_number"],
            "result_envelope_hash": result_row["result_envelope_hash"],
            "outcome": result_row["outcome"],
            "created_at": result_row["created_at"],
        })
        _need(canonical_json(work) == work_row["work_order_json"]
              and content_hash(work) == work_row["work_order_hash"] == stage.get("work_hash")
              and formal_hash == formal_row["content_hash"] == stage.get("formal_hash")
              and formal_row["work_order_id"] == work["id"]
              and formal_row["terminal_state"] == "succeeded"
              and canonical_json(formal_envelope) == formal_row["result_envelope_json"]
              and formal_envelope == envelope
              and formal_row["result_envelope_id"] == envelope["id"]
              and formal_row["result_envelope_hash"] == content_hash(envelope)
              and result_row["work_order_id"] == work["id"]
              and result_row["attempt_number"] == formal_row["attempt_number"]
              and result_row["outcome"] == "succeeded"
              and result_row["content_hash"] == receipt_hash
              and canonical_json(envelope) == result_row["result_envelope_json"]
              and result_row["result_envelope_hash"] == content_hash(envelope)
              and stage.get("result_hash") == content_hash(envelope)
              and envelope["work_order_ref"] == work["id"]
              and work["metadata"].get("authority_kind")
                  == "mission_document_research_admission"
              and work["metadata"].get("mission_document_research_admission_ref")
                  == admission_ref
              and work["metadata"].get("stage") == expected_stages[index]
              and (index == 0 or work["metadata"].get("upstream_work_order_ref")
                   == checked[index - 1]["work_ref"])
              and (index == 0 or work["metadata"].get("upstream_result_ref")
                   == checked[index - 1]["result_ref"])
              and (index == 0 or work["metadata"].get("upstream_result_hash")
                   == checked[index - 1]["result_hash"]),
              "promotion execution stage drifted")
        accounting = model_by_work.get(work["id"])
        is_model_stage = work["metadata"].get("stage") in {
            "qualitative_model_draft", "independent_qualitative_verifier"}
        _need((accounting is not None) == is_model_stage,
              "promotion model stage accounting checkpoint differs")
        if accounting is not None:
            body = dict(accounting)
            asserted = body.pop("content_hash", None)
            _need(asserted == content_hash(body)
                  and accounting.get("formal_result_ref") == formal_row["result_record_id"]
                  and accounting.get("formal_result_hash") == formal_hash
                  and accounting.get("result_envelope_ref") == envelope["id"]
                  and accounting.get("result_envelope_hash") == content_hash(envelope),
                  "promotion accounting checkpoint drifted")
            model_proof = envelope["outputs"]
            _need(isinstance(model_proof, Mapping)
                  and model_proof.get("id") == accounting.get("model_proof_ref")
                  and model_proof.get("content_hash") == accounting.get("model_proof_hash")
                  and content_hash({key: item for key, item in model_proof.items()
                                    if key != "content_hash"}) == model_proof.get("content_hash"),
                  "promotion model proof drifted")
            route_row = router.execute(
                "SELECT * FROM model_route_decisions WHERE decision_id=?",
                (accounting.get("route_decision_ref"),),
            ).fetchone()
            invocation_row = core.execute(
                "SELECT * FROM model_invocations WHERE invocation_id=?",
                (accounting.get("model_invocation_ref"),),
            ).fetchone()
            _need(route_row is not None and invocation_row is not None,
                  "promotion model route authority is unavailable")
            route = json.loads(route_row["decision_json"])
            saved_invocation = json.loads(invocation_row["invocation_json"])
            alias = saved_invocation.pop("invocation_id", None)
            invocation = ModelInvocation.from_dict(saved_invocation).to_dict()
            invocation_columns = {
                "id": invocation_row["invocation_id"],
                "profile_ref": invocation_row["profile_ref"],
                "provider": invocation_row["provider"],
                "model": invocation_row["model"],
                "capability": invocation_row["capability"],
                "runtime_ref": invocation_row["runtime_ref"],
                "actor_ref": invocation_row["actor_ref"],
                "environment_hash": invocation_row["environment_hash"],
                "granularity": invocation_row["granularity"],
                "work_order_ref": invocation_row["work_order_ref"],
                "model_family": invocation_row["model_family"],
            }
            endpoint = route.get("selected_endpoint")
            expected_capability = "research" if index == 1 else "verify"
            _need(canonical_json(route) == route_row["decision_json"]
                  and route.get("content_hash") == route_row["decision_hash"]
                  and content_hash({key: item for key, item in route.items()
                                    if key != "content_hash"}) == route_row["decision_hash"]
                  and route.get("work_order_ref") == work["id"]
                  and route.get("work_order_hash") == content_hash(work)
                  and route.get("outcome") == "selected"
                  and route.get("attempt_number") == formal_row["attempt_number"]
                  and route.get("id") == accounting.get("route_decision_ref")
                  and route.get("content_hash") == accounting.get("route_decision_hash")
                  and alias == invocation["id"] == accounting.get("model_invocation_ref")
                  and canonical_json({**invocation, "invocation_id": alias})
                      == invocation_row["invocation_json"]
                  and all(invocation.get(key) == value
                          for key, value in invocation_columns.items())
                  and content_hash(invocation) == accounting.get("model_invocation_hash")
                  and invocation["work_order_ref"] == work["id"]
                  and invocation["parent_ref"] == route["id"]
                  and invocation["profile_ref"] == route.get(
                      "selected_profile_version_ref")
                  and invocation["completed_at"] is not None
                  and envelope["invocation_ref"] == invocation["id"]
                  and isinstance(endpoint, Mapping)
                  and invocation["provider"] == endpoint.get("provider")
                  and invocation["model"] == endpoint.get("model")
                  and invocation["model_family"] == endpoint.get("family")
                  and invocation["runtime_ref"] == endpoint.get("adapter_ref")
                  and invocation["capability"] == route.get("capability")
                  and invocation["capability"] == expected_capability,
                  "promotion model execution identity drifted")
        checked.append({"work_ref": work["id"], "work_hash": content_hash(work),
                        "formal_ref": formal_row["result_record_id"],
                        "formal_hash": formal_hash, "result_ref": envelope["id"],
                        "result_hash": content_hash(envelope)})
    draft = material["normalized_payload"]["draft_proof"]
    verifier = material["normalized_payload"]["verifier_proof"]
    model_proofs = proof.get("model_proofs")
    _need(isinstance(model_proofs, list) and len(model_proofs) == 2
          and all(model_proofs[index].get("proof_ref") == item["id"]
                  and model_proofs[index].get("proof_hash") == item["content_hash"]
                  and model_proofs[index].get("route_ref") == item["route_decision_ref"]
                  and model_proofs[index].get("invocation_ref") == item["model_invocation_ref"]
                  for index, item in enumerate((draft, verifier))),
          "promotion model proof checkpoint differs from staged material")
    return {"stages": checked, "accounting_proof_hashes": [
        item["content_hash"] for item in proof["accounting_proofs"]]}


def resolve_financial_note_evidence(
    *, core_connection: Any, router_connection: Any, staging_connection: Any,
    registry: DocumentResearchRegistry, admission_ref: str,
) -> dict[str, Any]:
    """Rebuild one typed text authority without opening writable services."""

    _need(type(registry) is DocumentResearchRegistry,
          "financial note resolver requires the exact document registry")
    admission_row = core_connection.execute(
        "SELECT * FROM mission_document_research_admissions WHERE admission_id=?",
        (_text(admission_ref, "admission_ref"),),
    ).fetchone()
    admission = _record(admission_row, json_column="record_json",
                        hash_column="content_hash", label="directed admission")
    _need(admission_row["admission_id"] == admission["id"]
          and admission_row["mission_version_ref"] == admission["mission_version_ref"]
          and admission_row["company_ref"] == admission["company_ref"],
          "directed admission columns drifted")
    target = validate_financial_note_target(
        admission.get("planner_inquiry", {}).get("directed_document", {}).get(
            "evidence_target"))
    registration = validate_registration(admission["request"]["registration"])
    _need(registration["id"] == admission["document_authority_ref"]
          and registration["content_hash"] == admission["document_authority_hash"]
          and registration["source_authority"]["kind"]
              == "coverage-mission-acquired-document"
          and registration["source_authority"]["mission_version_ref"]
              == admission["mission_version_ref"]
          and registration["source_authority"]["company_ref"]
              == admission["company_ref"],
          "directed admission registration authority disagrees")
    mission_row = core_connection.execute(
        "SELECT * FROM coverage_mission_versions WHERE mission_version_id=?",
        (admission["mission_version_ref"],),
    ).fetchone()
    mission = _record(mission_row, json_column="record_json",
                      hash_column="content_hash", label="coverage mission")
    _need(mission_row["mission_version_id"] == mission["id"]
          and mission["content_hash"] == admission["mission_version_hash"],
          "coverage mission differs from the directed admission")
    derived_targets = financial_note_targets_for_registration(
        connection=core_connection, mission=mission,
        company_ref=admission["company_ref"], registration=registration,
    )
    _need(sum(item == target for item in derived_targets) == 1,
          "financial note target differs from current filing authority")
    filing = _statement_filing(core_connection, target,
                               company_ref=admission["company_ref"],
                               registration=registration)

    promotion_row = core_connection.execute(
        "SELECT * FROM mission_document_research_promotions WHERE admission_ref=?",
        (admission["id"],),
    ).fetchone()
    promotion = _record(promotion_row, json_column="record_json",
                        hash_column="content_hash", label="directed promotion")
    _need(set(promotion) == {
              "schema_version", "id", "admission_ref", "outcome_ref",
              "research_status", "execution_proof", "evidence_version_ref",
              "evidence_version_hash", "claim_version_ref", "claim_version_hash",
              "policy_authorization_ref", "policy_authorization_hash", "created_at",
              "content_hash",
          }
          and promotion["schema_version"] == "0.1"
          and promotion_row["promotion_id"] == promotion["id"]
          and promotion_row["admission_ref"] == promotion["admission_ref"]
          and promotion_row["outcome_ref"] == promotion["outcome_ref"]
          and promotion_row["created_at"] == promotion["created_at"]
          and promotion["admission_ref"] == admission["id"]
          and promotion["research_status"] == "canonical_claim_promoted"
          and promotion["execution_proof"]["admission_ref"] == admission["id"]
          and promotion["execution_proof"]["admission_hash"] == admission["content_hash"]
          and promotion["execution_proof"]["mission_version_ref"]
              == admission["mission_version_ref"]
          and promotion["execution_proof"]["mission_version_hash"]
              == admission["mission_version_hash"],
          "directed promotion differs from the typed admission")
    evidence = _record(core_connection.execute(
        "SELECT evidence_json,content_hash FROM evidence_versions WHERE evidence_version_id=?",
        (promotion["evidence_version_ref"],)).fetchone(),
        json_column="evidence_json", hash_column="content_hash",
        label="canonical directed Evidence")
    claim = _record(core_connection.execute(
        "SELECT claim_json,content_hash FROM claim_versions WHERE claim_version_id=?",
        (promotion["claim_version_ref"],)).fetchone(),
        json_column="claim_json", hash_column="content_hash",
        label="canonical directed Claim")
    _need(evidence["id"] == promotion["evidence_version_ref"]
          and evidence["content_hash"] == promotion["evidence_version_hash"]
          and claim["id"] == promotion["claim_version_ref"]
          and claim["content_hash"] == promotion["claim_version_hash"]
          and claim["subject_ref"] == admission["company_ref"]
          and claim["claim_kind"] == "qualitative" and claim["value"] is None,
          "canonical directed result is not qualitative authority for this company")
    receipt = core_connection.execute(
        "SELECT * FROM reviewed_candidate_commits WHERE review_decision_ref=?",
        (promotion["policy_authorization_ref"],),
    ).fetchone()
    _need(receipt is not None
          and receipt["idempotency_key"]
              == f"policy-ledger:mission-document-research:{admission['id']}",
          "directed promotion Ledger receipt is unavailable")
    try:
        decision = validate_policy_commit_decision(json.loads(receipt["decision_json"]))
        result = json.loads(receipt["result_json"])
    except Exception as exc:
        raise FinancialNoteEvidenceError(
            "directed promotion Ledger receipt is invalid") from exc
    _need(canonical_json(decision) == receipt["decision_json"]
          and decision.get("id") == promotion["policy_authorization_ref"]
          and decision.get("content_hash") == promotion["policy_authorization_hash"]
          and decision.get("candidate_evidence_ref") == receipt["candidate_evidence_ref"]
          and decision.get("candidate_claim_ref") == receipt["candidate_claim_ref"]
          and receipt["created_at"] == decision["created_at"] == promotion["created_at"]
          and receipt["request_hash"] == content_hash({
              "decision_hash": decision["content_hash"],
              "evidence_hash": decision["candidate_evidence_hash"],
              "claim_hash": decision["candidate_claim_hash"],
          })
          and canonical_json(result) == receipt["result_json"]
          and set(result) == {
              "status", "idempotency_key", "review_decision_ref",
              "evidence_version_ref", "claim_version_ref", "relation_ref",
              "claim_status", "event_refs",
          }
          and result.get("idempotency_key") == receipt["idempotency_key"]
          and result.get("review_decision_ref") == decision["id"]
          and result.get("evidence_version_ref") == evidence["id"]
          and result.get("claim_version_ref") == claim["id"],
          "directed promotion Ledger receipt drifted")
    bundle = _ReadOnlyCandidateView(staging_connection).exact_candidate_bundle(
        evidence_ref=receipt["candidate_evidence_ref"],
        claim_ref=receipt["candidate_claim_ref"],
        idempotency_key=f"mission-document-research-candidate:{admission['id']}",
    )
    hashes = promotion["execution_proof"]["bundle_hashes"]
    _need(all(bundle[key]["content_hash"] == hashes[key]
              for key in ("evidence", "claim", "material", "source_verification"))
          and receipt["candidate_evidence_ref"] == bundle["evidence"]["id"]
          and receipt["candidate_claim_ref"] == bundle["claim"]["id"]
          and decision["candidate_evidence_hash"] == bundle["evidence"]["content_hash"]
          and decision["candidate_claim_hash"] == bundle["claim"]["content_hash"]
          and evidence.get("candidate_origin_ref") == bundle["evidence"]["id"]
          and evidence.get("candidate_origin_hash") == bundle["evidence"]["content_hash"]
          and claim.get("candidate_origin_ref") == bundle["claim"]["id"]
          and claim.get("candidate_origin_hash") == bundle["claim"]["content_hash"],
          "canonical result differs from staged directed candidate")
    relation_row = core_connection.execute(
        "SELECT * FROM evidence_relations WHERE relation_id=?",
        (result["relation_ref"],)).fetchone()
    relation = _record(relation_row, json_column="relation_json",
                       hash_column="content_hash", label="canonical directed relation")
    _need(relation_row["relation_id"] == relation.get("id") == result["relation_ref"]
          and relation_row["evidence_ref"] == relation.get("evidence_ref")
          and relation_row["evidence_version_id"] == relation.get("evidence_version_ref")
          and relation_row["claim_ref"] == relation.get("claim_ref")
          and relation_row["claim_version_id"] == relation.get("claim_version_ref")
          and relation_row["relation"] == relation.get("relation")
          and relation_row["created_at"] == relation.get("created_at")
          and relation.get("relation") == "supports"
          and relation.get("evidence_ref") == evidence["evidence_ref"]
          and relation.get("evidence_version_ref") == evidence["id"]
          and relation.get("claim_ref") == claim["claim_ref"]
          and relation.get("claim_version_ref") == claim["id"]
          and relation.get("actor_ref") == decision["reviewer_ref"],
          "canonical directed relation differs from Ledger receipt")
    payload = bundle["material"]["normalized_payload"]
    _need(payload["mission_document_admission"] == {
        "ref": admission["id"], "hash": admission["content_hash"],
        "plan_ref": admission["plan_ref"], "plan_hash": admission["plan_hash"],
        "inquiry_ref": admission["inquiry_ref"], "inquiry_hash": admission["inquiry_hash"],
        "question_version_ref": admission["question_version_ref"],
        "question_version_hash": admission["question_version_hash"],
    }, "staged material binds another inquiry")
    proof = registry.verify_search_proof(payload["search_proof"])
    _need(canonical_json(proof["request"]) == canonical_json(admission["request"])
          and proof["id"] == promotion["execution_proof"]["source_proof_ref"]
          and proof["content_hash"] == promotion["execution_proof"]["source_proof_hash"],
          "registered note passage proof differs from the promoted execution")
    candidate = payload["draft_proof"]["output"]["candidate"]
    _need(isinstance(candidate, Mapping), "promoted financial note has no candidate")
    indexes = candidate.get("cited_match_indexes")
    _need(isinstance(indexes, list) and indexes,
          "promoted financial note has no cited passages")
    _need(all(isinstance(index, int) and not isinstance(index, bool)
              and 0 <= index < len(proof["matches"]) for index in indexes),
          "promoted financial note cites an invalid passage")
    passages = [proof["matches"][index] for index in indexes]
    checkpoint = _execution_checkpoint(
        core_connection, router_connection, promotion["execution_proof"],
        material=bundle["material"])
    body = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "target_ref": target["target_ref"], "company_ref": admission["company_ref"],
        "mission_version_ref": admission["mission_version_ref"],
        "mission_version_hash": admission["mission_version_hash"],
        "admission_ref": admission["id"], "admission_hash": admission["content_hash"],
        "promotion_ref": promotion["id"], "promotion_hash": promotion["content_hash"],
        "evidence_version_ref": evidence["id"],
        "evidence_version_hash": evidence["content_hash"],
        "claim_version_ref": claim["id"], "claim_version_hash": claim["content_hash"],
        "statement_ingest_ref": filing["ref"], "statement_filing_hash": filing["hash"],
        "accession": filing["accession"], "form": filing["form"],
        "applicability_kind": target["applicability_kind"],
        "periods": target["periods"],
        "registration_ref": registration["id"],
        "registration_hash": registration["content_hash"],
        "source_authority_ref": registration["source_authority"]["ref"],
        "source_authority_hash": registration["source_authority"]["hash"],
        "source_content_hash": registration["normalized_text"]["text_sha256"],
        "search_proof_ref": proof["id"], "search_proof_hash": proof["content_hash"],
        "passages": passages,
        "normalized_statement": claim["normalized_statement"],
        "execution_checkpoint": {
            "promotion_execution_proof_hash": promotion["execution_proof"]["content_hash"],
            **checkpoint,
            "accounting_replay": "promotion_checkpoint_only",
        },
    }
    reference = "financial-note-evidence:" + admission["id"]
    result = {**body, "ref": reference}
    result["content_hash"] = content_hash(result)
    return result


def financial_note_evidence_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the small exact projection stored by a company structure spec."""

    _need(isinstance(value, Mapping)
          and value.get("schema_version") == EVIDENCE_SCHEMA_VERSION
          and value.get("ref") == "financial-note-evidence:" + value.get("admission_ref", "")
          and value.get("content_hash") == content_hash({
              key: item for key, item in value.items() if key != "content_hash"
          }), "financial note evidence record drifted")
    return {
        "schema_version": BINDING_SCHEMA_VERSION,
        "ref": value["ref"], "content_hash": value["content_hash"],
        "target_ref": value["target_ref"], "company_ref": value["company_ref"],
        "statement_ingest_ref": value["statement_ingest_ref"],
        "statement_filing_hash": value["statement_filing_hash"],
        "accession": value["accession"], "form": value["form"],
        "applicability_kind": value["applicability_kind"],
        "periods": json.loads(canonical_json(value["periods"])),
    }


def resolve_financial_note_evidence_ref(
    *, core_connection: Any, router_connection: Any, staging_connection: Any,
    registry: DocumentResearchRegistry, evidence_ref: str,
) -> dict[str, Any]:
    """Resolve the stable reference stored by a company structure spec."""

    prefix = "financial-note-evidence:"
    evidence_ref = _text(evidence_ref, "evidence_ref")
    _need(evidence_ref.startswith(prefix), "financial note evidence ref is invalid")
    admission_ref = evidence_ref[len(prefix):]
    _need(admission_ref.startswith("mission-document-research-admission:"),
          "financial note evidence ref names another authority")
    result = resolve_financial_note_evidence(
        core_connection=core_connection, router_connection=router_connection,
        staging_connection=staging_connection, registry=registry,
        admission_ref=admission_ref,
    )
    _need(result["ref"] == evidence_ref, "financial note evidence ref drifted")
    return result


__all__ = [
    "BINDING_SCHEMA_VERSION", "EVIDENCE_SCHEMA_VERSION", "TARGET_REF",
    "TARGET_SCHEMA_VERSION", "FinancialNoteEvidenceError",
    "financial_note_evidence_binding", "resolve_financial_note_evidence",
    "resolve_financial_note_evidence_ref", "validate_financial_note_target",
]
