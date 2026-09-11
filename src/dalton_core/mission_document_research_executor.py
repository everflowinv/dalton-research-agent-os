"""Durable four-stage executor for one source-neutral directed-document admission."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .annual_report_qualitative import (
    AnnualReportQualitativeError, VERIFIER_PROVIDER_SCHEMA_HASH,
    qualitative_router_capability, validate_model_proof,
)
from .contracts import ModelInvocation, ResultEnvelope, WorkOrder
from .document_research import DocumentResearchError, DocumentResearchRegistry
from .document_research_qualitative import (
    DRAFT_PURPOSE, VERIFIER_PURPOSE, MissionDocumentDraftWorker,
    MissionDocumentVerifierWorker, _MissionDocumentCandidateAuthority,
    _build_candidate_bundle, draft_prompt, verifier_prompt,
)
from .mission_document_research import (
    MissionDocumentResearchAuthority, MissionDocumentResearchError,
)
from .research_verification import (
    MISSION_DOCUMENT_AUTHORITY_MODE, CandidateStagingStore, ResearchVerificationError,
)
from .scheduler import LeaseRejected, Scheduler
from .store import authorization_flag, authorized_flag, canonical_json, content_hash

SCHEMA_VERSION = "0.1"
AUTHORITY_KIND = "mission_document_research_admission"
_SCHEMA_PATH = Path(__file__).with_name("mission_document_research_executor_schema.sql")


class MissionDocumentResearchExecutorError(RuntimeError):
    pass


def read_mission_document_research_observations(
    connection: Any, *, mission_version_ref: str | None = None,
) -> list[dict[str, Any]]:
    """Read canonical immutable query outcomes for planner state projection."""

    query = "SELECT * FROM mission_document_research_observations"
    params: tuple[Any, ...] = ()
    if mission_version_ref is not None:
        query += " WHERE mission_version_ref=?"
        params = (mission_version_ref,)
    query += " ORDER BY created_at,observation_id"
    try:
        rows = connection.execute(query, params).fetchall()
    except Exception as exc:
        # Older copied states have no feedback table and therefore no outcomes.
        if "no such table" in str(exc).lower():
            return []
        raise
    result = []
    fields = {
        "schema_version", "id", "admission_ref", "admission_hash",
        "mission_version_ref", "company_ref", "plan_ref", "plan_hash",
        "inquiry_ref", "inquiry_hash", "question_version_ref",
        "question_version_hash", "question", "wants", "document_ref",
        "document_authority_ref",
        "document_authority_hash", "search_proof_ref", "search_proof_hash",
        "draft_proof_ref", "draft_proof_hash", "missing_evidence",
        "stage", "work_order_ref", "work_order_hash",
        "result_envelope_ref", "result_envelope_hash",
        "candidate_evidence_ref", "candidate_evidence_hash",
        "candidate_claim_ref", "candidate_claim_hash", "recovery",
        "outcome", "producer_ref", "tried_query_terms", "meaning", "suggested_actions",
        "created_at", "content_hash",
    }
    for row in rows:
        try:
            wire = json.loads(row["record_json"])
        except (TypeError, ValueError, RecursionError) as exc:
            raise MissionDocumentResearchExecutorError(
                "stored directed-document feedback is invalid") from exc
        body = dict(wire) if isinstance(wire, Mapping) else {}
        asserted = body.pop("content_hash", None)
        columns = {
            "id": row["observation_id"], "admission_ref": row["admission_ref"],
            "admission_hash": row["admission_hash"],
            "mission_version_ref": row["mission_version_ref"],
            "company_ref": row["company_ref"], "inquiry_ref": row["inquiry_ref"],
            "inquiry_hash": row["inquiry_hash"], "outcome": row["outcome"],
            "created_at": row["created_at"],
        }
        if (not isinstance(wire, Mapping) or set(wire) != fields
                or wire.get("schema_version") != SCHEMA_VERSION
                or wire.get("outcome") not in {
                    "query_miss", "no_verified_claim", "recovery_required", "candidate_staged"}
                or any(wire.get(key) != value for key, value in columns.items())
                or asserted != row["content_hash"] or asserted != content_hash(body)
                or canonical_json(wire) != row["record_json"]
                or not isinstance(wire.get("tried_query_terms"), list)
                or wire.get("suggested_actions") != (
                    ["revise_query_terms", "bounded_document_read"]
                    if wire.get("outcome") == "query_miss" else
                    ["revise_query_terms", "bounded_document_read", "acquire_another_document"]
                    if wire.get("outcome") == "no_verified_claim" else []
                )
                or ((wire.get("draft_proof_ref") is None)
                    != (wire.get("draft_proof_hash") is None))
                or (wire.get("outcome") == "query_miss"
                    and wire.get("draft_proof_ref") is not None)
                or (wire.get("outcome") in {"no_verified_claim", "candidate_staged"}
                    and wire.get("draft_proof_ref") is None)
                or not isinstance(wire.get("missing_evidence"), list)):
            raise MissionDocumentResearchExecutorError(
                "stored directed-document feedback drifted")
        admission_row = connection.execute(
            "SELECT record_json,content_hash,actor_ref FROM mission_document_research_admissions "
            "WHERE admission_id=?", (wire["admission_ref"],)).fetchone()
        work_row = connection.execute(
            "SELECT work_order_json,work_order_hash FROM scheduler_work_orders "
            "WHERE work_order_id=?", (wire["work_order_ref"],)).fetchone()
        result_row = connection.execute(
            "SELECT * FROM scheduler_result_envelopes WHERE result_envelope_id=?",
            (wire["result_envelope_ref"],)).fetchone()
        if admission_row is None or work_row is None or result_row is None:
            raise MissionDocumentResearchExecutorError(
                "stored directed-document feedback lost execution authority")
        try:
            admission = json.loads(admission_row["record_json"])
            work = WorkOrder.from_dict(json.loads(work_row["work_order_json"])).to_dict()
            envelope = ResultEnvelope.from_dict(
                json.loads(result_row["result_envelope_json"])).to_dict()
        except Exception as exc:
            raise MissionDocumentResearchExecutorError(
                "stored directed-document execution authority is invalid") from exc
        event_owner = connection.execute(
            "SELECT l.owner_ref,e.state FROM scheduler_attempt_events e "
            "JOIN scheduler_leases l ON l.lease_revision_id=e.lease_revision_id "
            "WHERE e.work_order_id=? AND e.result_envelope_id=? "
            "AND e.result_envelope_hash=? ORDER BY e.event_seq DESC LIMIT 1",
            (work["id"], envelope["id"], content_hash(envelope)),
        ).fetchone()
        admission_body = dict(admission)
        admission_hash = admission_body.pop("content_hash", None)
        exact_admission = {
            "mission_version_ref": admission.get("mission_version_ref"),
            "company_ref": admission.get("company_ref"),
            "plan_ref": admission.get("plan_ref"),
            "plan_hash": admission.get("plan_hash"),
            "inquiry_ref": admission.get("inquiry_ref"),
            "inquiry_hash": admission.get("inquiry_hash"),
            "question_version_ref": admission.get("question_version_ref"),
            "question_version_hash": admission.get("question_version_hash"),
            "question": admission.get("planner_inquiry", {}).get("question"),
            "wants": admission.get("planner_inquiry", {}).get("wants"),
            "document_ref": admission.get("request", {}).get(
                "registration", {}).get("document_ref"),
            "document_authority_ref": admission.get("document_authority_ref"),
            "document_authority_hash": admission.get("document_authority_hash"),
            "tried_query_terms": admission.get("request", {}).get("query_terms"),
        }
        if (canonical_json(admission) != admission_row["record_json"]
                or admission_hash != admission_row["content_hash"]
                or admission_hash != content_hash(admission_body)
                or admission_hash != wire["admission_hash"]
                or canonical_json(work) != work_row["work_order_json"]
                or content_hash(work) != work_row["work_order_hash"]
                or work_row["work_order_hash"] != wire["work_order_hash"]
                or work["metadata"].get("stage") != wire["stage"]
                or result_row["work_order_id"] != work["id"]
                or result_row["result_envelope_json"] != canonical_json(envelope)
                or result_row["result_envelope_hash"] != content_hash(envelope)
                or result_row["result_envelope_hash"] != wire["result_envelope_hash"]
                or envelope["id"] != wire["result_envelope_ref"]
                or envelope["work_order_ref"] != work["id"]
                or work["metadata"].get("mission_document_research_admission_ref")
                    != admission["id"]
                or work["metadata"].get("mission_document_research_admission_hash")
                    != admission["content_hash"]
                or any(wire.get(key) != value for key, value in exact_admission.items())
                or event_owner is None):
            raise MissionDocumentResearchExecutorError(
                "stored directed-document feedback execution drifted")
        if (not isinstance(wire.get("producer_ref"), str) or not wire["producer_ref"]
                or event_owner["owner_ref"] != wire["producer_ref"]):
            raise MissionDocumentResearchExecutorError(
                "stored directed-document feedback has a foreign completion")
        if ((wire["outcome"] == "recovery_required"
             and event_owner["state"] not in {"failed", "retryable"})
                or (wire["outcome"] != "recovery_required"
                    and event_owner["state"] != "succeeded")):
            raise MissionDocumentResearchExecutorError(
                "stored directed-document feedback has the wrong completion state")
        outputs = envelope["outputs"]
        retrieval = (outputs if wire["outcome"] == "query_miss"
                     else work["metadata"].get("retrieval_proof"))
        expected_draft = (outputs if wire["outcome"] == "no_verified_claim"
                          else work["metadata"].get("draft_proof"))
        if (not isinstance(retrieval, Mapping)
                or retrieval.get("id") != wire["search_proof_ref"]
                or retrieval.get("content_hash") != wire["search_proof_hash"]
                or ((expected_draft is None) != (wire["draft_proof_ref"] is None))
                or (expected_draft is not None and (
                    not isinstance(expected_draft, Mapping)
                    or expected_draft.get("id") != wire["draft_proof_ref"]
                    or expected_draft.get("content_hash") != wire["draft_proof_hash"]))):
            raise MissionDocumentResearchExecutorError(
                "stored directed-document feedback provenance drifted")
        if wire["outcome"] == "query_miss":
            if (not isinstance(outputs, Mapping) or outputs.get("id") != wire["search_proof_ref"]
                    or outputs.get("content_hash") != wire["search_proof_hash"]
                    or outputs.get("matches") != []):
                raise MissionDocumentResearchExecutorError("query-miss proof drifted")
        elif wire["outcome"] == "no_verified_claim":
            if (not isinstance(outputs, Mapping) or outputs.get("id") != wire["draft_proof_ref"]
                    or outputs.get("content_hash") != wire["draft_proof_hash"]
                    or outputs.get("output", {}).get("status") != "insufficient_evidence"):
                raise MissionDocumentResearchExecutorError(
                    "insufficient-evidence proof drifted")
        elif wire["outcome"] == "candidate_staged":
            if (not isinstance(outputs, Mapping)
                    or outputs.get("candidate_evidence_ref") != wire["candidate_evidence_ref"]
                    or outputs.get("candidate_evidence_hash") != wire["candidate_evidence_hash"]
                    or outputs.get("candidate_claim_ref") != wire["candidate_claim_ref"]
                    or outputs.get("candidate_claim_hash") != wire["candidate_claim_hash"]):
                raise MissionDocumentResearchExecutorError("staged feedback proof drifted")
        else:
            recovery = wire.get("recovery")
            proof = recovery.get("proof") if isinstance(recovery, Mapping) else None
            if (proof is not None and (
                    proof.get("result_envelope_ref") != envelope["id"]
                    or proof.get("result_envelope_hash") != content_hash(envelope))):
                raise MissionDocumentResearchExecutorError("recovery feedback proof drifted")
        result.append(dict(wire))
    return result


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise MissionDocumentResearchExecutorError("executor clock must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _ref(prefix: str, value: Any) -> str:
    return prefix + ":" + content_hash(value)[:32]


def _steps(admission: Mapping[str, Any]) -> list[dict[str, Any]]:
    specs = (
        ("registered_document_retrieval", "search_registered_document", [], []),
        ("qualitative_model_draft", "mission_directed_document_draft",
         ["capability:dalton:model:qualitative-research", "research"], []),
        ("independent_qualitative_verifier", "mission_directed_document_verifier",
         ["capability:dalton:model:qualitative-verifier", "verify"], []),
        ("qualitative_candidate_staging", "stage_mission_document_candidate", [], ["candidate_staging"]),
    )
    run = _ref("mission-document-research-run", {
        "admission_ref": admission["id"], "admission_hash": admission["content_hash"]})
    result = []
    for ordinal, (stage, operation, capabilities, side_effects) in enumerate(specs, 1):
        model_key = "draft" if ordinal == 2 else "verifier" if ordinal == 3 else None
        attempts = 1 if model_key is None else admission["model_execution"][model_key]["max_attempts"]
        body = {
            "schema_version": SCHEMA_VERSION,
            "id": f"mission-document-research-step:{run.rsplit(':', 1)[-1]}:{ordinal}",
            "ordinal": ordinal, "stage": stage, "operation": operation,
            "requested_capabilities": capabilities,
            "runtime_profile_ref": (
                "runtime-profile:dalton-model-broker:0.1" if model_key
                else "runtime-profile:dalton-local-python:0.1"),
            "declared_side_effects": side_effects, "max_attempts": attempts,
        }
        body["content_hash"] = content_hash(body)
        result.append(body)
    return result


def _blueprints(admission: Mapping[str, Any]) -> list[dict[str, Any]]:
    works = []
    for step in _steps(admission):
        ordinal = step["ordinal"]
        ref = "work:mission-document-research-" + content_hash({
            "admission_identity_hash": admission["identity_hash"], "ordinal": ordinal})[:32]
        prior = works[-1]["id"] if works else None
        model_key = "draft" if ordinal == 2 else "verifier" if ordinal == 3 else None
        execution = None if model_key is None else admission["model_execution"][model_key]
        requested_capabilities = list(step["requested_capabilities"])
        if model_key is not None:
            requested_capabilities.append(
                qualitative_router_capability(requested_capabilities[0])
            )
        metadata = {
            "authority_kind": AUTHORITY_KIND,
            "mission_document_research_admission_ref": admission["id"],
            "mission_document_research_admission_hash": admission["content_hash"],
            "mission_document_work_binding_hash": content_hash({
                "admission_ref": admission["id"], "admission_hash": admission["content_hash"],
                "work_order_ref": ref, "stage": step["stage"], "step_ref": step["id"],
                "upstream_work_order_ref": prior,
            }),
            "mission_version_ref": admission["mission_version_ref"],
            "mission_version_hash": admission["mission_version_hash"],
            "plan_ref": admission["plan_ref"], "plan_hash": admission["plan_hash"],
            "inquiry_ref": admission["inquiry_ref"], "inquiry_hash": admission["inquiry_hash"],
            "question_version_ref": admission["question_version_ref"],
            "question_version_hash": admission["question_version_hash"],
            "question": admission["planner_inquiry"]["question"],
            "wants": admission["planner_inquiry"]["wants"],
            "document_ref": admission["request"]["registration"]["document_ref"],
            "document_authority_ref": admission["document_authority_ref"],
            "document_authority_hash": admission["document_authority_hash"],
            "step_ref": step["id"], "step_hash": step["content_hash"],
            "stage": step["stage"], "operation": step["operation"],
            "upstream_work_order_ref": prior,
        }
        if execution is not None:
            metadata.update({
                "routing_policy_ref": execution["routing_policy_ref"],
                "credential_slot_refs": list(execution["credential_slot_refs"]),
                "budget_db": execution["budget_db"],
                "budget_policy_ref": execution["budget_policy_ref"],
                "provider_retry": execution["provider_retry"],
                "transport_retry": execution["transport_retry"],
            })
            budget = {
                "max_attempts": execution["max_attempts"],
                "max_input_tokens": execution["max_input_tokens"],
                "max_output_tokens": execution["max_output_tokens"],
                "max_total_tokens": execution["max_input_tokens"] + execution["max_output_tokens"],
                "max_cost_usd": execution["max_cost_usd"],
                "max_seconds": execution["max_seconds"],
                "max_elapsed_seconds": execution["max_elapsed_seconds"],
                "step_max_attempts": step["max_attempts"],
            }
        else:
            budget = {"max_attempts": 1, "max_input_tokens": 1, "max_output_tokens": 1,
                      "max_total_tokens": 2, "max_cost_usd": 0,
                      "max_seconds": 60, "step_max_attempts": 1}
        question = (
            canonical_json(admission["request"]) if ordinal == 1
            else f"Mission directed-document stage {ordinal}: {step['operation']}"
        )
        works.append(WorkOrder.from_dict({
            "schema_version": SCHEMA_VERSION, "id": ref,
            "created_at": admission["created_at"], "updated_at": admission["created_at"],
            "question": question, "requested_capabilities": requested_capabilities,
            "runtime_profile_ref": step["runtime_profile_ref"], "budget": budget,
            "idempotency_key": f"mission-document-research-work:{admission['id']}:{ordinal}",
            "declared_side_effects": step["declared_side_effects"], "status": "ready",
            "input_refs": [admission["id"], admission["question_version_ref"], step["id"],
                           *([prior] if prior else [])], "metadata": metadata,
        }).to_dict())
    return works


def _derive(admission: Mapping[str, Any], scheduler: Scheduler,
            registry: DocumentResearchRegistry, blueprints: Sequence[Mapping[str, Any]],
            index: int) -> dict[str, Any]:
    if index == 0:
        return dict(blueprints[0])
    upstream = scheduler.work_order_authority(blueprints[index - 1]["id"])
    formal = scheduler.formal_result(blueprints[index - 1]["id"])
    if upstream is None or formal is None or formal["terminal_state"] != "succeeded":
        raise MissionDocumentResearchExecutorError("child requires exact succeeded upstream")
    upstream_work = upstream["work_order"]
    envelope = formal["result_envelope"]
    blueprint = dict(blueprints[index])
    metadata = dict(blueprint["metadata"])
    refs = list(blueprint["input_refs"])
    planned_upstream = metadata["upstream_work_order_ref"]
    if planned_upstream != upstream_work["id"]:
        refs = [upstream_work["id"] if item == planned_upstream else item for item in refs]
        metadata["upstream_work_order_ref"] = upstream_work["id"]
    question = admission["planner_inquiry"]["question"]
    stage = metadata["stage"]
    if index == 1:
        try:
            proof = registry.verify_search_proof(envelope["outputs"])
        except DocumentResearchError as exc:
            raise MissionDocumentResearchExecutorError(str(exc)) from exc
        prompt = draft_prompt(question=question, search_proof=proof)
        context_hash = content_hash(proof["matches"])
        binding = {
            "prompt_hash": content_hash(prompt), "search_proof_hash": proof["content_hash"],
            "registration_hash": admission["document_authority_hash"],
            "context_hash": context_hash, "routing_policy_ref": metadata["routing_policy_ref"],
            "credential_slot_refs": metadata["credential_slot_refs"],
            "provider_retry": metadata["provider_retry"], "purpose": DRAFT_PURPOSE,
        }
        metadata.update({
            "question": question, "prompt_hash": content_hash(prompt),
            "model_request_binding_hash": content_hash(binding), "retrieval_proof": proof,
            "retrieval_match_count": len(proof["matches"]), "context_hash": context_hash,
            "purpose": DRAFT_PURPOSE, "upstream_result_ref": envelope["id"],
            "upstream_result_hash": formal["result_envelope_hash"],
        })
        refs.extend([proof["id"], envelope["id"]])
    elif index == 2:
        draft = validate_model_proof(envelope["outputs"], stage="qualitative_model_draft",
                                     work=upstream_work)
        proof = upstream_work["metadata"]["retrieval_proof"]
        producer = {key: draft[key] for key in (
            "route_decision_ref", "model_invocation_ref", "model_family", "request_binding_hash")}
        prompt = verifier_prompt(question=question, search_proof=proof,
                                 draft=draft["output"], producer_proof=producer)
        binding = {
            "prompt_hash": content_hash(prompt), "draft_hash": draft["output_hash"],
            "search_proof_hash": proof["content_hash"],
            "registration_hash": admission["document_authority_hash"],
            "context_hash": upstream_work["metadata"]["context_hash"],
            "producer_route_decision_ref": draft["route_decision_ref"],
            "purpose": VERIFIER_PURPOSE, "routing_policy_ref": metadata["routing_policy_ref"],
            "credential_slot_refs": metadata["credential_slot_refs"],
            "provider_retry": metadata["provider_retry"],
        }
        metadata.update({
            "question": question, "prompt_hash": content_hash(prompt),
            "model_request_binding_hash": content_hash(binding), "retrieval_proof": proof,
            "retrieval_match_count": len(proof["matches"]), "draft": draft["output"],
            "draft_proof": draft, "producer_model_family": draft["model_family"],
            "producer_route_decision_ref": draft["route_decision_ref"],
            "purpose": VERIFIER_PURPOSE, "verifier_output_schema_version": "0.1",
            "verifier_provider_contract": "annual-report-verifier-provider-output-0.1",
            "verifier_provider_schema_hash": VERIFIER_PROVIDER_SCHEMA_HASH,
            "context_hash": upstream_work["metadata"]["context_hash"],
            "upstream_result_ref": envelope["id"],
            "upstream_result_hash": formal["result_envelope_hash"],
        })
        refs.extend([draft["id"], proof["id"], envelope["id"]])
    elif index == 3:
        verifier = validate_model_proof(
            envelope["outputs"], stage="independent_qualitative_verifier", work=upstream_work)
        proof = upstream_work["metadata"]["retrieval_proof"]
        draft = upstream_work["metadata"]["draft_proof"]
        request_body = {
            "question": question, "search_proof_hash": proof["content_hash"],
            "draft_proof_hash": draft["content_hash"],
            "verifier_proof_hash": verifier["content_hash"],
            "registration_hash": admission["document_authority_hash"],
        }
        prompt = canonical_json({"task": "Stage exact directed-document candidate", **request_body})
        metadata.update({
            "question": question, "prompt_hash": content_hash(prompt),
            "model_request_binding_hash": content_hash(request_body), "retrieval_proof": proof,
            "draft_proof": draft, "verifier_proof": verifier,
            "producer_route_decision_ref": draft["route_decision_ref"],
            "verifier_route_decision_ref": verifier["route_decision_ref"],
            "upstream_result_ref": envelope["id"],
            "upstream_result_hash": formal["result_envelope_hash"],
        })
        refs.extend([verifier["id"], draft["id"], proof["id"], envelope["id"]])
    else:
        raise MissionDocumentResearchExecutorError("unsupported directed-document stage")
    return WorkOrder.from_dict({**blueprint, "question": prompt,
                                "input_refs": list(dict.fromkeys(refs)),
                                "metadata": metadata}).to_dict()


def _recovery_policy(admission: Mapping[str, Any], index: int) -> dict[str, int]:
    stage = "draft" if index == 1 else "verifier"
    provider = admission["model_execution"][stage].get("provider_retry")
    value = None if provider is None else provider.get("unknown_recovery")
    if value is None:
        return {"max_fresh_work_orders": 0, "retry_backoff_seconds": 0,
                "max_elapsed_seconds": 1}
    return dict(value)


def _parse_time(value: Any, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise MissionDocumentResearchExecutorError(f"{name} is not RFC3339") from exc
    if parsed.tzinfo is None:
        raise MissionDocumentResearchExecutorError(f"{name} lacks timezone")
    return parsed.astimezone(timezone.utc)


def _formal_hash(formal: Mapping[str, Any]) -> str:
    return content_hash({
        "id": formal.get("id", formal.get("result_record_id")),
        **{key: formal[key] for key in (
            "work_order_id", "attempt_number", "result_envelope_id",
            "result_envelope_hash", "terminal_state", "created_at")},
    })


def _formal_ref(formal: Mapping[str, Any]) -> str:
    value = formal.get("id", formal.get("result_record_id"))
    if not isinstance(value, str) or not value:
        raise MissionDocumentResearchExecutorError("formal result ref is invalid")
    return value


def _terminal_model_failure(scheduler: Scheduler, work: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return exact terminal authority, including retry-exhaustion's last receipt."""

    formal = scheduler.formal_result(work["id"])
    if formal is not None:
        return formal if formal["terminal_state"] == "failed" else None
    status = scheduler.status(work["id"])
    if status["state"] != "failed":
        return None
    rows = scheduler.connection.execute(
        "SELECT * FROM scheduler_attempt_events WHERE work_order_id=? ORDER BY event_seq",
        (work["id"],),
    ).fetchall()
    if len(rows) < 2:
        return None
    terminal, receipt = rows[-1], rows[-2]
    if (terminal["state"] != "failed"
            or terminal["reason"] != "retry_exhausted_after_retryable_result"
            or terminal["prior_event_id"] != receipt["event_id"]
            or receipt["state"] != "retryable"
            or terminal["attempt_number"] != receipt["attempt_number"]
            or receipt["result_envelope_id"] is None):
        return None
    for row in (receipt, terminal):
        wire = {
            "schema_version": SCHEMA_VERSION, "wire_version": row["wire_version"],
            "id": row["event_id"], "created_at": row["created_at"],
            "work_order_ref": row["work_order_id"],
            "attempt_number": row["attempt_number"], "state": row["state"],
            "lease_ref": row["lease_revision_id"],
            "result_envelope_ref": row["result_envelope_id"],
            "result_envelope_hash": row["result_envelope_hash"],
            "reason": row["reason"], "not_before": row["not_before"],
            "prior_event_ref": row["prior_event_id"],
        }
        wire["content_hash"] = content_hash(wire)
        if wire["content_hash"] != row["content_hash"]:
            raise MissionDocumentResearchExecutorError("retry exhaustion authority drifted")
    envelope_row = scheduler.connection.execute(
        "SELECT * FROM scheduler_result_envelopes WHERE result_envelope_id=?",
        (receipt["result_envelope_id"],),
    ).fetchone()
    if envelope_row is None:
        raise MissionDocumentResearchExecutorError("retry exhaustion receipt is missing")
    try:
        envelope = ResultEnvelope.from_dict(
            json.loads(envelope_row["result_envelope_json"])).to_dict()
    except Exception as exc:
        raise MissionDocumentResearchExecutorError(
            "retry exhaustion receipt is invalid") from exc
    receipt_hash = content_hash(envelope)
    if (envelope_row["work_order_id"] != work["id"]
            or envelope_row["attempt_number"] != receipt["attempt_number"]
            or envelope_row["result_envelope_hash"] != receipt_hash
            or envelope_row["result_envelope_hash"] != receipt["result_envelope_hash"]
            or envelope_row["result_envelope_json"] != canonical_json(envelope)
            or envelope_row["outcome"] != "retryable"):
        raise MissionDocumentResearchExecutorError("retry exhaustion receipt drifted")
    return {
        "id": terminal["event_id"], "work_order_id": work["id"],
        "attempt_number": receipt["attempt_number"],
        "result_envelope_id": envelope["id"], "result_envelope_hash": receipt_hash,
        "result_envelope": envelope, "terminal_state": "failed",
        "created_at": terminal["created_at"], "retry_exhausted": True,
    }


def _exact_model_result(work: Mapping[str, Any], formal: Mapping[str, Any], worker: Any):
    if formal is None or formal.get("terminal_state") != "succeeded":
        raise MissionDocumentResearchExecutorError("model stage lacks succeeded formal authority")
    try:
        envelope = ResultEnvelope.from_dict(formal["result_envelope"]).to_dict()
        proof = validate_model_proof(
            envelope["outputs"], stage=work["metadata"]["stage"], work=work)
        route = worker.router.get_decision(proof["route_decision_ref"])
    except Exception as exc:
        raise MissionDocumentResearchExecutorError("model formal authority is invalid") from exc
    if (formal["work_order_id"] != work["id"]
            or formal["result_envelope_id"] != envelope["id"]
            or formal["result_envelope_hash"] != content_hash(envelope)
            or envelope["work_order_ref"] != work["id"]
            or envelope["invocation_ref"] != proof["model_invocation_ref"]
            or envelope["status"] != "succeeded"
            or canonical_json(envelope["outputs"]) != canonical_json(proof)
            or route.get("id") != proof["route_decision_ref"]
            or route.get("outcome") != "selected"
            or route.get("work_order_ref") != work["id"]
            or route.get("work_order_hash") != content_hash(work)
            or route.get("attempt_number") != formal["attempt_number"]):
        raise MissionDocumentResearchExecutorError("model formal binding drifted")
    completion = worker.scheduler.connection.execute(
        "SELECT l.owner_ref FROM scheduler_attempt_events e "
        "JOIN scheduler_leases l ON l.lease_revision_id=e.lease_revision_id "
        "WHERE e.work_order_id=? AND e.attempt_number=? AND e.state='succeeded' "
        "AND e.result_envelope_id=? AND e.result_envelope_hash=? "
        "ORDER BY e.event_seq DESC LIMIT 1",
        (work["id"], formal["attempt_number"], envelope["id"], content_hash(envelope)),
    ).fetchone()
    if completion is None or completion["owner_ref"] != worker.worker_ref:
        raise MissionDocumentResearchExecutorError("model completion authority is foreign")
    row = worker.store.connection.execute(
        "SELECT * FROM model_invocations WHERE invocation_id=?",
        (proof["model_invocation_ref"],),
    ).fetchone()
    if row is None:
        raise MissionDocumentResearchExecutorError("model invocation authority is unavailable")
    try:
        saved = json.loads(row["invocation_json"])
        alias = saved.pop("invocation_id", None)
        invocation = ModelInvocation.from_dict(saved).to_dict()
    except Exception as exc:
        raise MissionDocumentResearchExecutorError("model invocation authority is invalid") from exc
    columns = {
        "profile_ref": row["profile_ref"], "provider": row["provider"],
        "model": row["model"], "capability": row["capability"],
        "runtime_ref": row["runtime_ref"], "actor_ref": row["actor_ref"],
        "environment_hash": row["environment_hash"], "granularity": row["granularity"],
        "work_order_ref": row["work_order_ref"], "model_family": row["model_family"],
    }
    if (alias != proof["model_invocation_ref"]
            or canonical_json({**invocation, "invocation_id": alias}) != row["invocation_json"]
            or any(invocation.get(key) != value for key, value in columns.items())
            or invocation["id"] != alias or invocation["work_order_ref"] != work["id"]
            or invocation["parent_ref"] != route["id"]
            or invocation["profile_ref"] != route["selected_profile_version_ref"]
            or invocation["model_family"] != proof["model_family"]
            or invocation["completed_at"] is None):
        raise MissionDocumentResearchExecutorError("model invocation binding drifted")
    endpoint = route.get("selected_endpoint")
    expected_capability = (
        "research" if work["metadata"]["stage"] == "qualitative_model_draft"
        else "verify"
    )
    if (not isinstance(endpoint, Mapping)
            or invocation["provider"] != endpoint.get("provider")
            or invocation["model"] != endpoint.get("model")
            or invocation["model_family"] != endpoint.get("family")
            or invocation["runtime_ref"] != endpoint.get("adapter_ref")
            or invocation["capability"] != route.get("capability")
            or invocation["capability"] != expected_capability):
        raise MissionDocumentResearchExecutorError(
            "model invocation differs from selected route")
    authority = getattr(worker, "mission_document_research_authority", None)
    budget_store = getattr(worker, "budget_store", None)
    if authority is None or budget_store is None:
        raise MissionDocumentResearchExecutorError("model budget authority is unavailable")
    admission = authority.resolve_for_execution(
        work["metadata"].get("mission_document_research_admission_ref"))
    index = 1 if work["metadata"]["stage"] == "qualitative_model_draft" else 2
    phase = "assessment" if index == 1 else "verification"
    exact_budget = budget_store.admission(
        work_order_ref=work["id"], attempt_number=formal["attempt_number"], phase=phase)
    ceiling = int(Decimal(str(work["budget"]["max_cost_usd"])) * 1_000_000)
    expected_binding = _expected_budget_binding(authority, admission, index)
    if (exact_budget is None
            or exact_budget["admission"].get("route_decision_ref") != route["id"]
            or exact_budget["admission"].get("policy_version_id")
            != work["metadata"]["budget_policy_ref"]
            or exact_budget["admission"].get("reserved_micros") != ceiling
            or canonical_json(exact_budget["mission_binding"])
            != canonical_json(expected_binding)):
        raise MissionDocumentResearchExecutorError("model budget binding drifted")
    settlement = exact_budget["settlement"]
    if settlement is None or settlement.get("usage_entry_ref") is None:
        raise MissionDocumentResearchExecutorError(
            "model budget settlement is unavailable")
    try:
        usage = worker.observability.get_usage(settlement["usage_entry_ref"])
        cost_row = worker.observability.connection.execute(
            "SELECT cost_entry_id FROM observability_cost_entries "
            "WHERE usage_entry_ref=? AND revision_number=1",
            (settlement["usage_entry_ref"],),
        ).fetchone()
        cost = (None if cost_row is None else
                worker.observability.get_cost(cost_row["cost_entry_id"]))
    except Exception as exc:
        raise MissionDocumentResearchExecutorError(
            "model budget accounting authority is unavailable") from exc
    if (usage.get("invocation_ref") != invocation["id"]
            or usage.get("work_order_ref") != work["id"]
            or usage.get("profile_ref") != invocation["profile_ref"]
            or usage.get("provider") != invocation["provider"]
            or usage.get("model") != invocation["model"]
            or usage.get("model_family") != invocation["model_family"]
            or usage.get("runtime_ref") != invocation["runtime_ref"]
            or usage.get("capability") != invocation["capability"]
            or not isinstance(cost, Mapping)
            or cost.get("usage_entry_ref") != usage["id"]
            or cost.get("cost_status") != "actual"
            or cost.get("amount_micros") != settlement.get("actual_micros")):
        raise MissionDocumentResearchExecutorError(
            "model budget settlement accounting drifted")
    return proof


def _expected_budget_binding(authority: Any, admission: Mapping[str, Any], index: int):
    try:
        mission = authority.active_budget_mission(admission["id"])
        from .budget_pools import mission_pool_scope
        return {
            "mission_ref": mission["mission_ref"],
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "max_daily_paid_calls": mission["budget"]["max_daily_paid_calls"],
            "max_daily_cost_micros": int(
                Decimal(str(mission["budget"]["max_daily_cost_usd"])) * 1_000_000),
            "outer_budget": dict(mission["outer_budget"]),
            **mission_pool_scope(
                mission, purpose=DRAFT_PURPOSE if index == 1 else VERIFIER_PURPOSE),
        }
    except Exception as exc:
        raise MissionDocumentResearchExecutorError(
            "recovery mission/budget authority is unavailable") from exc


def _no_send_receipt(envelope: Mapping[str, Any], work: Mapping[str, Any],
                     route_ref: str) -> str | None:
    receipt = envelope.get("metadata", {}).get("mission_document_no_send")
    if not isinstance(receipt, Mapping):
        return None
    body = dict(receipt)
    asserted = body.pop("content_hash", None)
    if (asserted != content_hash(body)
            or receipt.get("schema_version") != SCHEMA_VERSION
            or receipt.get("state") != "definitely_not_sent"
            or receipt.get("work_order_ref") != work["id"]
            or receipt.get("route_decision_ref") != route_ref):
        return None
    if receipt.get("kind") == "typed_transport_exception":
        if (set(receipt) != {"schema_version", "kind", "authority", "state",
                            "work_order_ref", "route_decision_ref",
                            "transport_error_type", "content_hash"}
                or receipt.get("authority") != "mission-document-model-worker"
                or receipt.get("transport_error_type") != "BrokerDefinitelyNotSent"):
            return None
        return "typed_transport_exception"
    if receipt.get("kind") != "adapter_result" or set(receipt) != {
        "schema_version", "kind", "authority", "state", "work_order_ref",
        "route_decision_ref", "adapter_result", "adapter_result_hash", "content_hash",
    } or receipt.get("authority") != "openclaw-model-adapter":
        return None
    try:
        result = ResultEnvelope.from_dict(receipt["adapter_result"]).to_dict()
    except Exception:
        return None
    if (receipt.get("adapter_result_hash") != content_hash(result)
            or result["work_order_ref"] != work["id"]
            or result["status"] != "failed"
            or result.get("metadata", {}).get("route_decision_ref") != route_ref
            or result.get("metadata", {}).get("dispatch_proof") != {
                "authority": "openclaw-model-adapter", "state": "definitely_not_sent",
                "version": "0.1",
            }
            or str((result.get("error") or {}).get("code", "")).upper() not in {
                "BUSY", "CONCURRENCY_LIMIT", "BROKER_CONCURRENCY_LIMIT",
                "QUEUE_TIMEOUT", "BROKER_CLOSED",
            }):
        return None
    return "adapter_result"


def _failure_execution_authority(scheduler: Scheduler, work: Mapping[str, Any],
                                 formal: Mapping[str, Any], worker: Any) -> None:
    envelope = ResultEnvelope.from_dict(formal["result_envelope"]).to_dict()
    route_ref = envelope.get("metadata", {}).get("route_decision_ref")
    event = scheduler.connection.execute(
        "SELECT l.owner_ref,e.attempt_number FROM scheduler_attempt_events e "
        "JOIN scheduler_leases l ON l.lease_revision_id=e.lease_revision_id "
        "WHERE e.work_order_id=? AND e.result_envelope_id=? "
        "AND e.result_envelope_hash=? ORDER BY e.event_seq DESC LIMIT 1",
        (work["id"], envelope["id"], content_hash(envelope)),
    ).fetchone()
    row = worker.router.connection.execute(
        "SELECT * FROM model_route_decisions WHERE decision_id=?", (route_ref,)
    ).fetchone()
    if event is None or event["owner_ref"] != worker.worker_ref or row is None:
        raise MissionDocumentResearchExecutorError("failed model execution authority is foreign")
    try:
        route = json.loads(row["decision_json"])
    except (TypeError, ValueError, RecursionError) as exc:
        raise MissionDocumentResearchExecutorError("failed route authority is invalid") from exc
    route_body = dict(route)
    asserted = route_body.pop("content_hash", None)
    if (canonical_json(route) != row["decision_json"]
            or asserted != row["decision_hash"] or asserted != content_hash(route_body)
            or route.get("id") != route_ref or route.get("outcome") != "selected"
            or route.get("work_order_ref") != work["id"]
            or route.get("work_order_hash") != content_hash(work)
            or route.get("attempt_number") != formal["attempt_number"]
            or event["attempt_number"] != formal["attempt_number"]
            or route.get("policy_version_ref") != work["metadata"]["routing_policy_ref"]
            or route.get("selected_endpoint", {}).get("credential_slot_ref")
            not in work["metadata"]["credential_slot_refs"]):
        raise MissionDocumentResearchExecutorError("failed route authority drifted")


def _verify_recovery_failure_proof(authority: Any, scheduler: Scheduler,
                                   admission: Mapping[str, Any], index: int,
                                   failed: Mapping[str, Any], link: Mapping[str, Any],
                                   worker: Any) -> None:
    proof = link.get("failure_proof")
    formal = _terminal_model_failure(scheduler, failed)
    if not isinstance(proof, Mapping) or formal is None:
        raise MissionDocumentResearchExecutorError("recovery failure proof is unavailable")
    envelope = ResultEnvelope.from_dict(formal["result_envelope"]).to_dict()
    _failure_execution_authority(scheduler, failed, formal, worker)
    common = {
        "failed_at": formal["created_at"],
        "formal_result_ref": _formal_ref(formal), "formal_result_hash": _formal_hash(formal),
        "result_envelope_ref": envelope["id"],
        "result_envelope_hash": formal["result_envelope_hash"],
        "route_decision_ref": envelope.get("metadata", {}).get("route_decision_ref"),
    }
    if any(proof.get(key) != value for key, value in common.items()):
        raise MissionDocumentResearchExecutorError("recovery formal proof drifted")
    if authority.store.connection.execute(
        "SELECT 1 FROM model_invocations WHERE work_order_ref=? LIMIT 1", (failed["id"],)
    ).fetchone() is not None:
        raise MissionDocumentResearchExecutorError("recovery Work gained a model invocation")
    budget_store = getattr(worker, "budget_store", None)
    if budget_store is None:
        raise MissionDocumentResearchExecutorError("recovery budget authority is unavailable")
    phase = "assessment" if index == 1 else "verification"
    binding = _expected_budget_binding(authority, admission, index)
    if proof.get("mission_binding_hash") != content_hash(binding):
        raise MissionDocumentResearchExecutorError("recovery mission binding drifted")
    classification = proof.get("classification")
    if classification == "adapter_proved_definitely_not_sent":
        if (_no_send_receipt(envelope, failed, common["route_decision_ref"])
                != "typed_transport_exception"
                or (envelope.get("error") or {}).get("code") not in {
                    "MODEL_CHAIN_EXHAUSTED", "MODEL_ADAPTER_UNAVAILABLE",
                }
                or proof.get("budget_authority_ref") is not None
                or proof.get("budget_settlement_ref") is not None):
            raise MissionDocumentResearchExecutorError("adapter no-send proof drifted")
        return
    if classification == "proved_zero_cost_no_send":
        exact = budget_store.admission(
            work_order_ref=failed["id"], attempt_number=formal["attempt_number"], phase=phase)
        if (_no_send_receipt(envelope, failed, common["route_decision_ref"])
                not in {"adapter_result", "typed_transport_exception"}
                or exact is None or exact["admission"].get("admission_id")
                != proof.get("budget_authority_ref")
                or exact["admission"].get("content_hash")
                != proof.get("budget_authority_hash")
                or exact["settlement"] is None
                or exact["settlement"].get("settlement_id")
                != proof.get("budget_settlement_ref")
                or exact["settlement"].get("content_hash")
                != proof.get("budget_settlement_hash")
                or exact["settlement"].get("actual_micros") != 0
                or exact["settlement"].get("usage_entry_ref") is not None
                or canonical_json(exact["mission_binding"]) != canonical_json(binding)):
            raise MissionDocumentResearchExecutorError("zero-cost recovery proof drifted")
        return
    table = ("thesis_impact_day_rejections" if classification == "atomic_day_budget_refusal"
             else "model_budget_pool_rejections" if classification == "atomic_pool_budget_refusal"
             else None)
    if table is None:
        raise MissionDocumentResearchExecutorError("recovery classification is invalid")
    row = budget_store.connection.execute(
        f"SELECT record_json,content_hash FROM {table} WHERE rejection_id=?",
        (proof.get("budget_authority_ref"),),
    ).fetchone()
    if row is None:
        raise MissionDocumentResearchExecutorError("recovery refusal proof is unavailable")
    try:
        refusal = json.loads(row["record_json"])
    except (TypeError, ValueError, RecursionError) as exc:
        raise MissionDocumentResearchExecutorError("recovery refusal proof is invalid") from exc
    body = dict(refusal)
    asserted = body.pop("content_hash", None)
    if (canonical_json(refusal) != row["record_json"]
            or asserted != row["content_hash"] or asserted != content_hash(body)
            or asserted != proof.get("budget_authority_hash")
            or refusal.get("work_order_ref") != failed["id"]
            or refusal.get("attempt_number") != formal["attempt_number"]
            or refusal.get("phase") != phase
            or refusal.get("route_decision_ref", common["route_decision_ref"])
            != common["route_decision_ref"]
            or refusal.get("day") != proof.get("refusal_day")):
        raise MissionDocumentResearchExecutorError("recovery refusal proof drifted")
    if classification == "atomic_day_budget_refusal":
        if (refusal.get("mission_binding") != binding
                or refusal.get("policy_version_id") != failed["metadata"]["budget_policy_ref"]):
            raise MissionDocumentResearchExecutorError("day-budget recovery proof drifted")
    elif (refusal.get("reason") != "pool_exhausted"
          or refusal.get("mission_ref") != binding["mission_ref"]
          or refusal.get("pool") != binding["pool"]
          or refusal.get("pool_lane") != binding.get("pool_lane")):
        raise MissionDocumentResearchExecutorError("pool-budget recovery proof drifted")


def _recovery_work(base: Mapping[str, Any], link: Mapping[str, Any]) -> dict[str, Any]:
    metadata = dict(base["metadata"])
    metadata["mission_document_recovery"] = {
        "schema_version": SCHEMA_VERSION,
        "recovery_link_ref": link["id"],
        "recovery_link_hash": link["content_hash"],
        "recovery_number": link["recovery_number"],
        "policy_hash": link["policy_hash"],
        "window_started_at": link["window_started_at"],
    }
    return WorkOrder.from_dict({
        **dict(base), "id": link["recovery_work_order_ref"],
        "idempotency_key": "mission-document-recovery:" + link["id"],
        "input_refs": list(dict.fromkeys([
            *base["input_refs"], link["failed_work_order_ref"], link["id"],
        ])),
        "metadata": metadata,
    }).to_dict()


def _recovery_rows(connection: Any, admission: Mapping[str, Any], index: int) -> list[Any]:
    return connection.execute(
        "SELECT * FROM mission_document_research_recovery_links "
        "WHERE admission_ref=? AND stage_ordinal=? ORDER BY recovery_number",
        (admission["id"], index + 1),
    ).fetchall()


def _read_recovery_link(
    connection: Any, admission: Mapping[str, Any], index: int, row: Any,
    base: Mapping[str, Any], prior: Mapping[str, Any] | None,
) -> dict[str, Any]:
    try:
        wire = json.loads(row["record_json"])
    except (TypeError, ValueError, RecursionError) as exc:
        raise MissionDocumentResearchExecutorError("recovery link is invalid") from exc
    number = 1 if prior is None else prior["recovery_number"] + 1
    policy = _recovery_policy(admission, index)
    identity = {
        "admission_ref": admission["id"], "admission_hash": admission["content_hash"],
        "stage_ordinal": index + 1, "recovery_number": number,
        "failed_work_order_ref": base["id"],
        "failed_work_order_hash": content_hash(base),
        "prior_recovery_link_ref": None if prior is None else prior["id"],
        "prior_recovery_link_hash": None if prior is None else prior["content_hash"],
        "policy_hash": content_hash(policy),
        "window_started_at": wire.get("window_started_at"),
        "failure_proof": wire.get("failure_proof"),
    }
    link_ref = _ref("mission-document-research-recovery-link", identity)
    recovery_ref = "work:mission-document-recovery-" + content_hash(identity)[:32]
    expected = {
        "schema_version": SCHEMA_VERSION, "id": link_ref, **identity,
        "recovery_work_order_ref": recovery_ref,
        "created_at": wire.get("created_at"),
    }
    expected["content_hash"] = content_hash(expected)
    columns = {
        "id": row["recovery_link_id"], "admission_ref": row["admission_ref"],
        "stage_ordinal": row["stage_ordinal"], "recovery_number": row["recovery_number"],
        "failed_work_order_ref": row["failed_work_order_ref"],
        "recovery_work_order_ref": row["recovery_work_order_ref"],
        "content_hash": row["content_hash"], "created_at": row["created_at"],
    }
    started = _parse_time(expected["window_started_at"], "recovery window start")
    created = _parse_time(expected["created_at"], "recovery eligibility time")
    proof = wire.get("failure_proof") if isinstance(wire, Mapping) else None
    failed_formal_time = (
        _parse_time(wire["failure_proof"].get("failed_at"), "failed formal time")
        if isinstance(proof, Mapping) and proof.get("failed_at") is not None else None
    )
    if (canonical_json(wire) != row["record_json"]
            or canonical_json(wire) != canonical_json(expected)
            or any(wire.get(key) != value for key, value in columns.items())
            or number > policy["max_fresh_work_orders"]
            or (prior is None and failed_formal_time is not None
                and started != failed_formal_time)
            or (prior is not None and wire.get("window_started_at")
                != prior.get("window_started_at"))
            or (failed_formal_time is not None and created != max(
                failed_formal_time + timedelta(seconds=policy["retry_backoff_seconds"]),
                (datetime.fromisoformat(proof["refusal_day"]).replace(tzinfo=timezone.utc)
                 + timedelta(days=1)) if proof.get("refusal_day") is not None
                else failed_formal_time,
            ))
            or created < started
            or created >= started + timedelta(seconds=policy["max_elapsed_seconds"])):
        raise MissionDocumentResearchExecutorError("recovery link authority drifted")
    recovered = _recovery_work(base, wire)
    stored = connection.execute(
        "SELECT work_order_json,work_order_hash FROM scheduler_work_orders WHERE work_order_id=?",
        (recovered["id"],),
    ).fetchone()
    if (stored is None or stored["work_order_json"] != canonical_json(recovered)
            or stored["work_order_hash"] != content_hash(recovered)):
        raise MissionDocumentResearchExecutorError("recovery Work authority drifted")
    return wire


def _effective_stage(authority: Any, scheduler: Scheduler,
                     admission: Mapping[str, Any], index: int, *, worker: Any = None
                     ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    originals = _blueprints(admission)
    effective = [originals[0]]
    selected_links: list[dict[str, Any]] = []
    for current in range(1, index + 1):
        base = _derive(admission, scheduler, authority.registry,
                       [*effective, originals[current]], current)
        prior = None
        for row in _recovery_rows(authority.connection, admission, current):
            link = _read_recovery_link(
                authority.connection, admission, current, row, base, prior)
            if worker is not None:
                _verify_recovery_failure_proof(
                    authority, scheduler, admission, current, base, link, worker)
            base = _recovery_work(base, link)
            prior = link
            if current == index:
                selected_links.append(link)
        effective.append(base)
    return effective[index], selected_links


def validate_mission_document_work_authority(authority, scheduler, work, *, worker=None):
    wire = WorkOrder.from_dict(work.to_dict() if isinstance(work, WorkOrder) else work).to_dict()
    if wire["metadata"].get("authority_kind") != AUTHORITY_KIND:
        raise MissionDocumentResearchExecutorError("Work lacks directed-document authority")
    admission = authority.resolve_for_execution(
        wire["metadata"].get("mission_document_research_admission_ref"))
    stage = wire["metadata"].get("stage")
    indexes = {"qualitative_model_draft": 1, "independent_qualitative_verifier": 2}
    if stage not in indexes:
        raise MissionDocumentResearchExecutorError("Work is not an admitted model stage")
    expected, _links = _effective_stage(
        authority, scheduler, admission, indexes[stage], worker=worker)
    if indexes[stage] == 2:
        if worker is None:
            raise MissionDocumentResearchExecutorError("verifier Work lacks model authority")
        upstream = scheduler.work_order_authority(expected["metadata"]["upstream_work_order_ref"])
        upstream_formal = scheduler.formal_result(expected["metadata"]["upstream_work_order_ref"])
        if upstream is None:
            raise MissionDocumentResearchExecutorError("draft Work authority is unavailable")
        _exact_model_result(upstream["work_order"], upstream_formal, worker)
    stored = scheduler.work_order_authority(wire["id"])
    if (canonical_json(wire) != canonical_json(expected) or stored is None
            or stored["work_order_hash"] != content_hash(expected)
            or canonical_json(stored["work_order"]) != canonical_json(expected)):
        raise MissionDocumentResearchExecutorError("Work drifted from exact derivation")
    return admission


def effective_mission_document_work_orders(
    authority: MissionDocumentResearchAuthority, scheduler: Scheduler,
    admission_ref: str, *, draft_worker: MissionDocumentDraftWorker,
    verifier_worker: MissionDocumentVerifierWorker,
) -> list[dict[str, Any]]:
    """Strictly read and reverify the currently derivable effective Work prefix."""

    admission = authority.resolve_for_execution(admission_ref)
    originals = _blueprints(admission)
    effective = [originals[0]]
    for index in range(1, 4):
        prior = scheduler.formal_result(effective[index - 1]["id"])
        if prior is None or prior["terminal_state"] != "succeeded":
            break
        if index in (1, 2):
            worker = draft_worker if index == 1 else verifier_worker
            work, _links = _effective_stage(
                authority, scheduler, admission, index, worker=worker)
        else:
            work = _derive(
                admission, scheduler, authority.registry,
                [*effective, originals[index]], index)
        stored = scheduler.work_order_authority(work["id"])
        if stored is not None and (stored["work_order_hash"] != content_hash(work)
                                   or canonical_json(stored["work_order"])
                                   != canonical_json(work)):
            raise MissionDocumentResearchExecutorError("effective Work authority drifted")
        effective.append(work)
    return effective


class MissionDocumentResearchExecutor:
    _authorized = authorized_flag()

    def __init__(self, *, authority: MissionDocumentResearchAuthority, scheduler: Scheduler,
                 registry: DocumentResearchRegistry, draft_worker: MissionDocumentDraftWorker,
                 verifier_worker: MissionDocumentVerifierWorker, staging: CandidateStagingStore,
                 actor_ref: str, clock: Callable[[], datetime] = _now,
                 fault_injector: Callable[[str], None] | None = None):
        if not isinstance(authority, MissionDocumentResearchAuthority):
            raise TypeError("authority has the wrong type")
        if not isinstance(scheduler, Scheduler) or not isinstance(registry, DocumentResearchRegistry):
            raise TypeError("executor requires Scheduler and DocumentResearchRegistry")
        if registry is not authority.registry:
            raise TypeError("executor registry must be the admission authority registry")
        if not isinstance(draft_worker, MissionDocumentDraftWorker) \
                or not isinstance(verifier_worker, MissionDocumentVerifierWorker):
            raise TypeError("executor model workers have the wrong type")
        if draft_worker.scheduler is not scheduler or verifier_worker.scheduler is not scheduler:
            raise TypeError("model workers must share Scheduler")
        if (draft_worker.mission_document_research_authority is not authority
                or verifier_worker.mission_document_research_authority is not authority):
            raise TypeError("model workers must share directed-document authority")
        self.authority, self.connection, self.scheduler = authority, authority.connection, scheduler
        self.registry, self.draft_worker, self.verifier_worker = registry, draft_worker, verifier_worker
        self.staging, self.actor_ref, self.clock = staging, actor_ref, clock
        self.fault_injector = fault_injector
        self._authorization_flag = authorization_flag(
            self.connection, "dalton_mission_document_research_executor_authorized")
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        if self._authorized:
            raise RuntimeError("directed-document executor operation cannot be nested")
        self._authorized = True
        try:
            with self.authority.store._transaction() as cursor:
                yield cursor
        finally:
            self._authorized = False

    def _run_id(self, admission):
        return _ref("mission-document-research-run", {
            "admission_ref": admission["id"], "admission_hash": admission["content_hash"]})

    def _blueprints(self, admission):
        return _blueprints(admission)

    def _derive_work(self, admission, blueprints, index):
        return _derive(admission, self.scheduler, self.registry, blueprints, index)

    @staticmethod
    def _phase(index):
        if index == 1:
            return "assessment"
        if index == 2:
            return "verification"
        raise MissionDocumentResearchExecutorError("only model stages can recover")

    @staticmethod
    def _canonical_rejection(row, fields):
        try:
            wire = json.loads(row["record_json"])
        except (TypeError, ValueError, RecursionError) as exc:
            raise MissionDocumentResearchExecutorError(
                "recovery budget rejection is invalid") from exc
        body = dict(wire) if isinstance(wire, Mapping) else {}
        asserted = body.pop("content_hash", None)
        if (not isinstance(wire, Mapping) or canonical_json(wire) != row["record_json"]
                or asserted != row["content_hash"] or asserted != content_hash(body)
                or any(wire.get(key) != row[column]
                       for key, column in dict(fields).items())):
            raise MissionDocumentResearchExecutorError(
                "recovery budget rejection authority drifted")
        return dict(wire)

    def _safe_failure_proof(self, admission, work, formal, index):
        if formal["terminal_state"] != "failed":
            return None
        try:
            envelope = ResultEnvelope.from_dict(formal["result_envelope"]).to_dict()
        except Exception as exc:
            raise MissionDocumentResearchExecutorError(
                "failed model ResultEnvelope is invalid") from exc
        if (formal["work_order_id"] != work["id"]
                or formal["result_envelope_id"] != envelope["id"]
                or formal["result_envelope_hash"] != content_hash(envelope)
                or envelope["work_order_ref"] != work["id"]
                or envelope["status"] != (
                    "retryable" if formal.get("retry_exhausted") else "failed")):
            raise MissionDocumentResearchExecutorError("failed model authority drifted")
        # A persisted invocation means a provider call existed. Generic recovery
        # never guesses whether that request was charged or completed.
        invocation_rows = self.authority.store.connection.execute(
            "SELECT invocation_id FROM model_invocations WHERE work_order_ref=?", (work["id"],)
        ).fetchall()
        if invocation_rows:
            return None
        worker = self.draft_worker if index == 1 else self.verifier_worker
        budget_store = worker.budget_store
        if budget_store is None:
            return None
        phase = self._phase(index)
        route_ref = envelope.get("metadata", {}).get("route_decision_ref")
        if not isinstance(route_ref, str) or not route_ref:
            return None
        ceiling = int(Decimal(str(work["budget"]["max_cost_usd"])) * 1_000_000)
        expected_binding = _expected_budget_binding(self.authority, admission, index)
        _failure_execution_authority(self.scheduler, work, formal, worker)
        try:
            budget = budget_store.admission(
                work_order_ref=work["id"], attempt_number=formal["attempt_number"], phase=phase)
        except Exception as exc:
            raise MissionDocumentResearchExecutorError(
                "recovery budget admission authority drifted") from exc
        receipt_kind = _no_send_receipt(envelope, work, route_ref)
        if (budget is None
                and receipt_kind == "typed_transport_exception"):
            if (envelope.get("error") or {}).get("code") not in {
                    "MODEL_CHAIN_EXHAUSTED", "MODEL_ADAPTER_UNAVAILABLE"}:
                return None
            return {
                "classification": "adapter_proved_definitely_not_sent",
                "failed_at": formal["created_at"],
                "formal_result_ref": _formal_ref(formal),
                "formal_result_hash": _formal_hash(formal),
                "result_envelope_ref": envelope["id"],
                "result_envelope_hash": formal["result_envelope_hash"],
                "route_decision_ref": route_ref,
                "budget_authority_ref": None, "budget_authority_hash": None,
                "budget_settlement_ref": None, "budget_settlement_hash": None,
                "mission_binding_hash": content_hash(expected_binding),
                "refusal_day": None,
            }
        if budget is not None:
            ledger = budget.get("admission")
            settlement = budget.get("settlement")
            binding = budget.get("mission_binding")
            if (not isinstance(ledger, Mapping) or not isinstance(settlement, Mapping)
                    or ledger.get("policy_version_id") != work["metadata"]["budget_policy_ref"]
                    or ledger.get("route_decision_ref") != route_ref
                    or ledger.get("reserved_micros") != ceiling
                    or receipt_kind not in {"adapter_result", "typed_transport_exception"}
                    or settlement.get("actual_micros") != 0
                    or settlement.get("usage_entry_ref") is not None
                    or canonical_json(binding) != canonical_json(expected_binding)):
                return None
            return {
                "classification": "proved_zero_cost_no_send",
                "failed_at": formal["created_at"],
                "formal_result_ref": _formal_ref(formal), "formal_result_hash": _formal_hash(formal),
                "result_envelope_ref": envelope["id"],
                "result_envelope_hash": formal["result_envelope_hash"],
                "route_decision_ref": route_ref,
                "budget_authority_ref": ledger["admission_id"],
                "budget_authority_hash": ledger["content_hash"],
                "budget_settlement_ref": settlement["settlement_id"],
                "budget_settlement_hash": settlement["content_hash"],
                "mission_binding_hash": content_hash(binding),
                "refusal_day": None,
            }
        params = (work["id"], formal["attempt_number"], phase)
        day_row = budget_store.connection.execute(
            "SELECT * FROM thesis_impact_day_rejections WHERE work_order_ref=? "
            "AND attempt_number=? AND phase=?", params).fetchone()
        if day_row is not None:
            refusal = self._canonical_rejection(day_row, {
                key: key for key in (
                    "rejection_id", "policy_version_id", "day", "work_order_ref",
                    "attempt_number", "phase", "route_decision_ref", "reserved_micros",
                    "day_committed_micros", "day_cap_micros", "created_at")})
            if (refusal.get("policy_version_id") != work["metadata"]["budget_policy_ref"]
                    or refusal.get("route_decision_ref") != route_ref
                    or refusal.get("reserved_micros") != ceiling
                    or canonical_json(refusal.get("mission_binding"))
                    != canonical_json(expected_binding)):
                return None
            return {
                "classification": "atomic_day_budget_refusal",
                "failed_at": formal["created_at"],
                "formal_result_ref": _formal_ref(formal), "formal_result_hash": _formal_hash(formal),
                "result_envelope_ref": envelope["id"],
                "result_envelope_hash": formal["result_envelope_hash"],
                "route_decision_ref": route_ref,
                "budget_authority_ref": refusal["rejection_id"],
                "budget_authority_hash": refusal["content_hash"],
                "budget_settlement_ref": None, "budget_settlement_hash": None,
                "mission_binding_hash": content_hash(expected_binding),
                "refusal_day": refusal["day"],
            }
        pool_row = budget_store.connection.execute(
            "SELECT * FROM model_budget_pool_rejections WHERE work_order_ref=? "
            "AND attempt_number=? AND phase=? ORDER BY created_at DESC LIMIT 1", params).fetchone()
        if pool_row is None:
            return None
        refusal = self._canonical_rejection(pool_row, {
            "rejection_id": "rejection_id", "day": "day", "mission_ref": "mission_ref",
            "pool": "pool", "pool_lane": "pool_lane", "work_order_ref": "work_order_ref",
            "attempt_number": "attempt_number", "phase": "phase",
            "reserved_micros": "reserved_micros", "spent": "pool_spent_micros",
            "cap": "pool_cap_micros", "borrowable_micros": "borrowable_micros",
            "created_at": "created_at"})
        if (refusal.get("reason") != "pool_exhausted"
                or refusal.get("mission_ref") != expected_binding["mission_ref"]
                or refusal.get("pool") != expected_binding["pool"]
                or refusal.get("pool_lane") != expected_binding.get("pool_lane")
                or refusal.get("reserved_micros") != ceiling):
            return None
        return {
            "classification": "atomic_pool_budget_refusal",
            "failed_at": formal["created_at"],
            "formal_result_ref": _formal_ref(formal), "formal_result_hash": _formal_hash(formal),
            "result_envelope_ref": envelope["id"],
            "result_envelope_hash": formal["result_envelope_hash"],
            "route_decision_ref": route_ref,
            "budget_authority_ref": refusal["rejection_id"],
            "budget_authority_hash": refusal["content_hash"],
            "budget_settlement_ref": None, "budget_settlement_hash": None,
            "mission_binding_hash": content_hash(expected_binding),
            "refusal_day": refusal["day"],
        }

    def _start(self, admission, root):
        start_id = _ref("mission-document-research-start", self._run_id(admission))
        body = {"schema_version": SCHEMA_VERSION, "id": start_id,
                "run_id": self._run_id(admission), "admission_ref": admission["id"],
                "admission_hash": admission["content_hash"], "root_work_order_ref": root["id"],
                "root_work_order_hash": content_hash(root), "created_at": admission["created_at"]}
        body["content_hash"] = content_hash(body)
        row = self.connection.execute(
            "SELECT record_json,content_hash FROM mission_document_research_starts WHERE start_id=?",
            (start_id,)).fetchone()
        if row is None:
            with self._transaction() as cur:
                cur.execute("INSERT INTO mission_document_research_starts VALUES(?,?,?,?,?,?,?,?,?)",
                            (start_id, admission["id"], admission["content_hash"], body["run_id"],
                             root["id"], content_hash(root), canonical_json(body),
                             body["content_hash"], admission["created_at"]))
        elif row["record_json"] != canonical_json(body) or row["content_hash"] != body["content_hash"]:
            raise MissionDocumentResearchExecutorError("stored start drifted")

    def _enqueue(self, work):
        result = self.scheduler.enqueue(work)
        if result["status"] not in {"fresh", "duplicate"}:
            raise MissionDocumentResearchExecutorError("Scheduler admission conflicted")
        return {"status": "admitted", "work_order_ref": work["id"],
                "stage": work["metadata"]["stage"]}

    def _complete_retrieval(self, admission, work):
        try:
            proof = self.registry.search(admission["request"])
        except DocumentResearchError as exc:
            raise MissionDocumentResearchExecutorError(str(exc)) from exc
        claim = self.scheduler.claim(self.actor_ref, work_order_id=work["id"])
        if claim is None:
            return {"status": "pending", "work_order_ref": work["id"]}
        envelope = ResultEnvelope(
            schema_version=SCHEMA_VERSION,
            id=_ref("result-envelope:mission-document-search", proof["content_hash"]),
            created_at=_time(self.clock()), work_order_ref=work["id"],
            invocation_ref=_ref("execution:mission-document-search", self._run_id(admission)),
            status="succeeded", outputs=proof, actual_side_effects=(), usage_refs=(),
            artifact_refs=(), error=None, metadata={"authority_ref": admission["id"]}).to_dict()
        completed = self.scheduler.complete(
            work["id"], claim["attempt"]["attempt_number"], self.actor_ref,
            claim["lease_token"], envelope,
            idempotency_key=f"mission-document-research:{admission['id']}:1:complete",
            result_envelope_hash=content_hash(envelope))
        if completed["status"] != "fresh":
            raise MissionDocumentResearchExecutorError("retrieval completion did not converge")
        return {"status": "succeeded", "work_order_ref": work["id"]}

    def _write_observation(self, body):
        body["content_hash"] = content_hash(body)
        existing = self.connection.execute(
            "SELECT record_json,content_hash FROM mission_document_research_observations "
            "WHERE observation_id=?", (body["id"],)).fetchone()
        if existing is None:
            with self._transaction() as cur:
                cur.execute(
                    "INSERT INTO mission_document_research_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (body["id"], body["admission_ref"], body["admission_hash"],
                     body["mission_version_ref"], body["company_ref"],
                     body["inquiry_ref"], body["inquiry_hash"], body["outcome"],
                     canonical_json(body), body["content_hash"], body["created_at"]),
                )
        elif (existing["record_json"] != canonical_json(body)
              or existing["content_hash"] != body["content_hash"]):
            raise MissionDocumentResearchExecutorError("stored observation drifted")
        exact = [item for item in read_mission_document_research_observations(
            self.connection, mission_version_ref=body["mission_version_ref"]
        ) if item["id"] == body["id"]]
        if len(exact) != 1:
            raise MissionDocumentResearchExecutorError("observation was not persisted exactly")
        return exact[0]

    def _recovery_observation(self, admission, work, formal, index, recovery):
        proof = work["metadata"]["retrieval_proof"]
        draft = work["metadata"].get("draft_proof")
        status = recovery["status"]
        reason = recovery["reason"]
        body = {
            "schema_version": SCHEMA_VERSION,
            "id": _ref("mission-document-research-recovery-observation", {
                "admission_ref": admission["id"], "work_order_ref": work["id"],
                "reason": reason, "status": status,
            }),
            "admission_ref": admission["id"], "admission_hash": admission["content_hash"],
            "mission_version_ref": admission["mission_version_ref"],
            "company_ref": admission["company_ref"], "plan_ref": admission["plan_ref"],
            "plan_hash": admission["plan_hash"], "inquiry_ref": admission["inquiry_ref"],
            "inquiry_hash": admission["inquiry_hash"],
            "question_version_ref": admission["question_version_ref"],
            "question_version_hash": admission["question_version_hash"],
            "question": admission["planner_inquiry"]["question"],
            "wants": admission["planner_inquiry"]["wants"],
            "document_ref": admission["request"]["registration"]["document_ref"],
            "document_authority_ref": admission["document_authority_ref"],
            "document_authority_hash": admission["document_authority_hash"],
            "search_proof_ref": proof["id"], "search_proof_hash": proof["content_hash"],
            "draft_proof_ref": None if draft is None else draft["id"],
            "draft_proof_hash": None if draft is None else draft["content_hash"],
            "missing_evidence": [], "outcome": "recovery_required",
            "producer_ref": (self.draft_worker.worker_ref if index == 1
                             else self.verifier_worker.worker_ref),
            "stage": work["metadata"]["stage"], "work_order_ref": work["id"],
            "work_order_hash": content_hash(work),
            "result_envelope_ref": formal["result_envelope_id"],
            "result_envelope_hash": formal["result_envelope_hash"],
            "candidate_evidence_ref": None, "candidate_evidence_hash": None,
            "candidate_claim_ref": None, "candidate_claim_hash": None,
            "recovery": dict(recovery),
            "tried_query_terms": list(admission["request"]["query_terms"]),
            "meaning": (
                "The model stage failed without authority for a safe replay; its send and "
                "charging state remains frozen for review."
                if reason == "send_state_unproved" else
                "The model stage did not send a chargeable request and may use a fresh "
                "bounded WorkOrder when its recorded retry conditions permit."
            ),
            "suggested_actions": [],
            "created_at": formal["created_at"],
        }
        return self._write_observation(body)

    def _recover_failed_model(self, admission, work, formal, index):
        policy = _recovery_policy(admission, index)
        worker = self.draft_worker if index == 1 else self.verifier_worker
        current, links = _effective_stage(
            self.authority, self.scheduler, admission, index, worker=worker)
        if canonical_json(current) != canonical_json(work):
            raise MissionDocumentResearchExecutorError("effective recovery Work drifted")
        proof = self._safe_failure_proof(admission, work, formal, index)
        now = self.clock().astimezone(timezone.utc)
        started = (_parse_time(formal["created_at"], "failed formal time") if not links
                   else _parse_time(links[0]["window_started_at"], "recovery window start"))
        deadline = started + timedelta(seconds=policy["max_elapsed_seconds"])
        if proof is None:
            recovery = {
                "status": "stopped", "reason": "send_state_unproved", "eligible": False,
                "used_fresh_work_orders": len(links),
                "max_fresh_work_orders": policy["max_fresh_work_orders"],
                "retry_at": None, "deadline": _time(deadline), "proof": None,
            }
        else:
            failed_at = _parse_time(formal["created_at"], "failed formal time")
            eligible_at = failed_at + timedelta(seconds=policy["retry_backoff_seconds"])
            if proof["refusal_day"] is not None:
                day_after = datetime.fromisoformat(proof["refusal_day"]).replace(
                    tzinfo=timezone.utc) + timedelta(days=1)
                eligible_at = max(eligible_at, day_after)
            if policy["max_fresh_work_orders"] == 0:
                state, reason = "stopped", "fresh_work_recovery_disabled"
            elif len(links) >= policy["max_fresh_work_orders"]:
                state, reason = "stopped", "fresh_work_recovery_exhausted"
            elif now >= deadline or eligible_at >= deadline:
                state, reason = "stopped", "fresh_work_recovery_deadline_exceeded"
            elif now < eligible_at:
                state, reason = "waiting", "fresh_work_recovery_backoff"
            else:
                state, reason = "eligible", "proved_no_send_or_budget_refusal"
            recovery = {
                "status": state, "reason": reason, "eligible": state == "eligible",
                "used_fresh_work_orders": len(links),
                "max_fresh_work_orders": policy["max_fresh_work_orders"],
                "retry_at": _time(eligible_at), "deadline": _time(deadline),
                "proof": proof,
            }
        self._recovery_observation(admission, work, formal, index, recovery)
        if not recovery["eligible"]:
            return {"status": recovery["status"], "reason": recovery["reason"],
                    "work_order_ref": work["id"], "stage": work["metadata"]["stage"]}
        number = len(links) + 1
        prior = None if not links else links[-1]
        identity = {
            "admission_ref": admission["id"], "admission_hash": admission["content_hash"],
            "stage_ordinal": index + 1, "recovery_number": number,
            "failed_work_order_ref": work["id"],
            "failed_work_order_hash": content_hash(work),
            "prior_recovery_link_ref": None if prior is None else prior["id"],
            "prior_recovery_link_hash": None if prior is None else prior["content_hash"],
            "policy_hash": content_hash(policy), "window_started_at": _time(started),
            "failure_proof": proof,
        }
        link_ref = _ref("mission-document-research-recovery-link", identity)
        link = {
            "schema_version": SCHEMA_VERSION, "id": link_ref, **identity,
            "recovery_work_order_ref": (
                "work:mission-document-recovery-" + content_hash(identity)[:32]),
            # This is the deterministic first eligible instant, so enqueue/link
            # convergence survives a crash between their two databases.
            "created_at": recovery["retry_at"],
        }
        link["content_hash"] = content_hash(link)
        recovered = _recovery_work(work, link)
        enqueued = self.scheduler.enqueue(recovered)
        if enqueued["status"] not in {"fresh", "duplicate"}:
            raise MissionDocumentResearchExecutorError("recovery Work enqueue conflicted")
        if self.fault_injector is not None:
            self.fault_injector("after_recovery_enqueue")
        existing = self.connection.execute(
            "SELECT record_json,content_hash FROM mission_document_research_recovery_links "
            "WHERE recovery_link_id=?", (link["id"],)).fetchone()
        if existing is None:
            with self._transaction() as cur:
                cur.execute(
                    "INSERT INTO mission_document_research_recovery_links VALUES(?,?,?,?,?,?,?,?,?)",
                    (link["id"], admission["id"], index + 1, number, work["id"],
                     recovered["id"], canonical_json(link), link["content_hash"],
                     link["created_at"]),
                )
        elif (existing["record_json"] != canonical_json(link)
              or existing["content_hash"] != link["content_hash"]):
            raise MissionDocumentResearchExecutorError("recovery link conflicted")
        checked, checked_links = _effective_stage(
            self.authority, self.scheduler, admission, index, worker=worker)
        if (canonical_json(checked) != canonical_json(recovered)
                or len(checked_links) != number):
            raise MissionDocumentResearchExecutorError("recovery did not converge")
        return {"status": "admitted", "reason": "fresh_work_recovery",
                "work_order_ref": recovered["id"], "stage": recovered["metadata"]["stage"]}

    def _research_feedback(self, admission, proof, work, formal, *, outcome, draft_proof=None):
        if outcome not in {"query_miss", "no_verified_claim"}:
            raise MissionDocumentResearchExecutorError("unsupported research feedback outcome")
        missing = [] if draft_proof is None else list(draft_proof["output"]["missing"])
        body = {
            "schema_version": SCHEMA_VERSION,
            "id": _ref("mission-document-research-feedback", {
                "admission_ref": admission["id"], "outcome": outcome}),
            "admission_ref": admission["id"], "admission_hash": admission["content_hash"],
            "mission_version_ref": admission["mission_version_ref"],
            "company_ref": admission["company_ref"],
            "plan_ref": admission["plan_ref"], "plan_hash": admission["plan_hash"],
            "inquiry_ref": admission["inquiry_ref"], "inquiry_hash": admission["inquiry_hash"],
            "question_version_ref": admission["question_version_ref"],
            "question_version_hash": admission["question_version_hash"],
            "question": admission["planner_inquiry"]["question"],
            "wants": admission["planner_inquiry"]["wants"],
            "document_ref": admission["request"]["registration"]["document_ref"],
            "document_authority_ref": admission["document_authority_ref"],
            "document_authority_hash": admission["document_authority_hash"],
            "search_proof_ref": proof["id"], "search_proof_hash": proof["content_hash"],
            "draft_proof_ref": None if draft_proof is None else draft_proof["id"],
            "draft_proof_hash": None if draft_proof is None else draft_proof["content_hash"],
            "missing_evidence": missing, "outcome": outcome,
            "producer_ref": (self.actor_ref if outcome == "query_miss"
                             else self.draft_worker.worker_ref),
            "stage": work["metadata"]["stage"], "work_order_ref": work["id"],
            "work_order_hash": content_hash(work),
            "result_envelope_ref": formal["result_envelope_id"],
            "result_envelope_hash": formal["result_envelope_hash"],
            "candidate_evidence_ref": None, "candidate_evidence_hash": None,
            "candidate_claim_ref": None, "candidate_claim_hash": None,
            "recovery": None,
            "tried_query_terms": list(admission["request"]["query_terms"]),
            "meaning": (
                "The bounded query found no matching excerpt; this does not prove the "
                "registered full text lacks an answer."
                if outcome == "query_miss" else
                "The retrieved excerpts were readable but did not support a verified answer; "
                "this does not prove the registered full text or another document lacks one."
            ),
            "suggested_actions": (
                ["revise_query_terms", "bounded_document_read"]
                if outcome == "query_miss" else
                ["revise_query_terms", "bounded_document_read", "acquire_another_document"]
            ),
            "created_at": admission["created_at"],
        }
        exact = self._write_observation(body)
        return {"status": "complete", "research_status": outcome,
                "feedback_ref": exact["id"], "feedback_hash": exact["content_hash"]}

    def _candidate_observation(self, admission, work, formal, records):
        proof = work["metadata"]["retrieval_proof"]
        draft = work["metadata"]["draft_proof"]
        body = {
            "schema_version": SCHEMA_VERSION,
            "id": _ref("mission-document-research-observation", {
                "admission_ref": admission["id"], "outcome": "candidate_staged",
                "work_order_ref": work["id"]}),
            "admission_ref": admission["id"], "admission_hash": admission["content_hash"],
            "mission_version_ref": admission["mission_version_ref"],
            "company_ref": admission["company_ref"], "plan_ref": admission["plan_ref"],
            "plan_hash": admission["plan_hash"], "inquiry_ref": admission["inquiry_ref"],
            "inquiry_hash": admission["inquiry_hash"],
            "question_version_ref": admission["question_version_ref"],
            "question_version_hash": admission["question_version_hash"],
            "question": admission["planner_inquiry"]["question"],
            "wants": admission["planner_inquiry"]["wants"],
            "document_ref": admission["request"]["registration"]["document_ref"],
            "document_authority_ref": admission["document_authority_ref"],
            "document_authority_hash": admission["document_authority_hash"],
            "search_proof_ref": proof["id"], "search_proof_hash": proof["content_hash"],
            "draft_proof_ref": draft["id"], "draft_proof_hash": draft["content_hash"],
            "missing_evidence": [], "outcome": "candidate_staged",
            "producer_ref": self.actor_ref,
            "stage": work["metadata"]["stage"], "work_order_ref": work["id"],
            "work_order_hash": content_hash(work),
            "result_envelope_ref": formal["result_envelope_id"],
            "result_envelope_hash": formal["result_envelope_hash"],
            "candidate_evidence_ref": records["candidate_evidence_ref"],
            "candidate_evidence_hash": records["candidate_evidence_hash"],
            "candidate_claim_ref": records["candidate_claim_ref"],
            "candidate_claim_hash": records["candidate_claim_hash"],
            "tried_query_terms": list(admission["request"]["query_terms"]),
            "meaning": "An independently verified draft-only candidate was staged.",
            "suggested_actions": [], "recovery": None,
            "created_at": admission["created_at"],
        }
        return self._write_observation(body)

    def _staging_records(self, admission, works, verifier):
        bundle = _build_candidate_bundle(
            admission=admission,
            proof=works[3]["metadata"]["retrieval_proof"],
            draft_proof=works[3]["metadata"]["draft_proof"], verifier_proof=verifier,
            draft_work=works[1], verifier_work=works[2], created_at=works[3]["created_at"],
        )
        resolver = _MissionDocumentCandidateAuthority(
            question=admission["planner_inquiry"]["question"],
            proof=works[3]["metadata"]["retrieval_proof"],
            draft_proof=bundle["material"]["normalized_payload"]["draft_proof"],
            verifier_proof=bundle["material"]["normalized_payload"]["verifier_proof"],
            admission=admission,
        )
        staged_result = self.staging.stage(
            material=bundle["material"],
            source_verification=bundle["source_verification"],
            evidence=bundle["evidence"], claim=bundle["claim"],
            idempotency_key=f"mission-document-research-candidate:{admission['id']}",
            verification_mode=MISSION_DOCUMENT_AUTHORITY_MODE,
            authority_resolver=resolver,
        )
        staged = {"staging": staged_result, **bundle}
        return {"authority_ref": admission["id"],
                "question_version_ref": admission["question_version_ref"],
                "question_version_hash": admission["question_version_hash"],
                "research_status": "candidate_staged",
                "candidate_evidence_ref": staged["evidence"]["id"],
                "candidate_evidence_hash": staged["evidence"]["content_hash"],
                "candidate_claim_ref": staged["claim"]["id"],
                "candidate_claim_hash": staged["claim"]["content_hash"]}

    def _validate_records(self, admission, works, records):
        keys = {"authority_ref", "question_version_ref", "question_version_hash",
                "research_status", "candidate_evidence_ref", "candidate_evidence_hash",
                "candidate_claim_ref", "candidate_claim_hash"}
        if not isinstance(records, Mapping) or set(records) != keys or (
            records["authority_ref"] != admission["id"]
            or records["question_version_ref"] != admission["question_version_ref"]
            or records["question_version_hash"] != admission["question_version_hash"]
            or records["research_status"] != "candidate_staged"):
            raise MissionDocumentResearchExecutorError("staging result drifted")
        bundle = self.staging.exact_candidate_bundle(
            evidence_ref=records["candidate_evidence_ref"],
            claim_ref=records["candidate_claim_ref"],
            idempotency_key=f"mission-document-research-candidate:{admission['id']}")
        formal = self.scheduler.formal_result(works[2]["id"])
        if formal is None or formal["terminal_state"] != "succeeded":
            raise MissionDocumentResearchExecutorError("staging lost verifier authority")
        exact_verifier = _exact_model_result(works[2], formal, self.verifier_worker)
        expected = _build_candidate_bundle(
            admission=admission, proof=works[3]["metadata"]["retrieval_proof"],
            draft_proof=works[3]["metadata"]["draft_proof"],
            verifier_proof=exact_verifier,
            draft_work=works[1], verifier_work=works[2], created_at=works[3]["created_at"])
        for key in ("material", "source_verification", "evidence", "claim"):
            if canonical_json(bundle[key]) != canonical_json(expected[key]):
                raise MissionDocumentResearchExecutorError(f"staged {key} drifted")
        if (records["candidate_evidence_hash"] != bundle["evidence"]["content_hash"]
                or records["candidate_claim_hash"] != bundle["claim"]["content_hash"]):
            raise MissionDocumentResearchExecutorError("staging hashes drifted")
        return dict(records)

    def _complete_staging(self, admission, works):
        work = works[3]
        formal = self.scheduler.formal_result(works[2]["id"])
        if formal is None or formal["terminal_state"] != "succeeded":
            raise MissionDocumentResearchExecutorError("staging requires verifier")
        claim = self.scheduler.claim(self.actor_ref, work_order_id=work["id"])
        if claim is None:
            return {"status": "pending", "work_order_ref": work["id"]}
        try:
            verifier = _exact_model_result(works[2], formal, self.verifier_worker)
            if self.fault_injector is not None:
                self.fault_injector("before_staging_lease_validation")
            self.scheduler.validate_lease_for_use(
                work["id"], claim["attempt"]["attempt_number"], self.actor_ref,
                claim["lease_token"], lease_revision_ref=claim["lease"]["id"],
                lease_hash=claim["lease"]["content_hash"],
                work_order_hash=claim["work_order_hash"],
            )
            records = self._staging_records(admission, works, verifier)
            self._validate_records(admission, works, records)
        except LeaseRejected:
            return {"status": "pending", "work_order_ref": work["id"]}
        except (AnnualReportQualitativeError, ResearchVerificationError) as exc:
            raise MissionDocumentResearchExecutorError(str(exc)) from exc
        envelope = ResultEnvelope(
            schema_version=SCHEMA_VERSION,
            id=_ref("result-envelope:mission-document-staging", records),
            created_at=_time(self.clock()), work_order_ref=work["id"],
            invocation_ref=_ref("execution:mission-document-staging", self._run_id(admission)),
            status="succeeded", outputs=records, actual_side_effects=(), usage_refs=(),
            artifact_refs=(), error=None, metadata={"authority_ref": admission["id"]}).to_dict()
        completed = self.scheduler.complete(
            work["id"], claim["attempt"]["attempt_number"], self.actor_ref,
            claim["lease_token"], envelope,
            idempotency_key=f"mission-document-research:{admission['id']}:4:complete",
            result_envelope_hash=content_hash(envelope))
        if completed["status"] != "fresh":
            raise MissionDocumentResearchExecutorError("staging completion did not converge")
        stage_formal = self.scheduler.formal_result(work["id"])
        if stage_formal is None:
            raise MissionDocumentResearchExecutorError("staging formal result is unavailable")
        self._candidate_observation(admission, work, stage_formal, records)
        return self._outcome(admission, works, records)

    def _stage_owned(self, work, formal):
        row = self.connection.execute(
            "SELECT l.owner_ref,e.result_envelope_id,e.result_envelope_hash "
            "FROM scheduler_attempt_events e JOIN scheduler_leases l "
            "ON l.lease_revision_id=e.lease_revision_id WHERE e.work_order_id=? "
            "AND e.attempt_number=? AND e.state='succeeded' ORDER BY e.event_seq DESC LIMIT 1",
            (work["id"], formal["attempt_number"])).fetchone()
        if (row is None or row["owner_ref"] != self.actor_ref
                or row["result_envelope_id"] != formal["result_envelope_id"]
                or row["result_envelope_hash"] != formal["result_envelope_hash"]):
            raise MissionDocumentResearchExecutorError("stage was not completed by executor")

    def _outcome(self, admission, works, records):
        records = self._validate_records(admission, works, records)
        start_ref = _ref("mission-document-research-start", self._run_id(admission))
        body = {"schema_version": SCHEMA_VERSION,
                "id": _ref("mission-document-research-outcome", {"start": start_ref, **records}),
                "start_ref": start_ref, "admission_ref": admission["id"], **records,
                "created_at": admission["created_at"]}
        body["content_hash"] = content_hash(body)
        row = self.connection.execute(
            "SELECT record_json,content_hash FROM mission_document_research_outcomes WHERE outcome_id=?",
            (body["id"],)).fetchone()
        if row is None:
            with self._transaction() as cur:
                cur.execute("INSERT INTO mission_document_research_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (body["id"], start_ref, admission["id"],
                             admission["question_version_ref"], admission["question_version_hash"],
                             records["candidate_evidence_ref"], records["candidate_evidence_hash"],
                             records["candidate_claim_ref"], records["candidate_claim_hash"],
                             canonical_json(body), body["content_hash"], admission["created_at"]))
        elif row["record_json"] != canonical_json(body) or row["content_hash"] != body["content_hash"]:
            raise MissionDocumentResearchExecutorError("stored outcome drifted")
        return {"status": "complete", **records, "outcome_ref": body["id"],
                "outcome_hash": body["content_hash"]}

    def run_once(self, admission_ref: str):
        try:
            admission = self.authority.resolve_for_execution(admission_ref)
        except MissionDocumentResearchError as exc:
            raise MissionDocumentResearchExecutorError(str(exc)) from exc
        originals = _blueprints(admission)
        self._start(admission, originals[0])
        effective: list[dict[str, Any]] = []
        for index in range(4):
            if index == 0:
                work = originals[0]
            elif index in (1, 2):
                stage_worker = self.draft_worker if index == 1 else self.verifier_worker
                work, _links = _effective_stage(
                    self.authority, self.scheduler, admission, index, worker=stage_worker)
            else:
                work = _derive(
                    admission, self.scheduler, self.registry,
                    [*effective, originals[index]], index)
            effective.append(work)
            stored = self.scheduler.work_order_authority(work["id"])
            if stored is None:
                return self._enqueue(work)
            if stored["work_order_hash"] != content_hash(work):
                raise MissionDocumentResearchExecutorError("stored Work drifted")
            formal = self.scheduler.formal_result(work["id"])
            if formal is None:
                terminal = (
                    _terminal_model_failure(self.scheduler, work)
                    if index in (1, 2) else None
                )
                if terminal is not None:
                    return self._recover_failed_model(
                        admission, work, terminal, index)
                if index == 0:
                    return self._complete_retrieval(admission, work)
                if index in (1, 2):
                    worker = self.draft_worker if index == 1 else self.verifier_worker
                    validate_mission_document_work_authority(
                        self.authority, self.scheduler, work, worker=worker)
                    try:
                        result = worker.run_once(work)
                    except AnnualReportQualitativeError as exc:
                        raise MissionDocumentResearchExecutorError(str(exc)) from exc
                    return {**result, "work_order_ref": work["id"], "stage": work["metadata"]["stage"]}
                return self._complete_staging(admission, effective)
            if formal["terminal_state"] != "succeeded":
                if index in (1, 2):
                    return self._recover_failed_model(admission, work, formal, index)
                return {"status": "blocked", "work_order_ref": work["id"],
                        "stage": work["metadata"]["stage"]}
            if index == 0:
                proof = self.registry.verify_search_proof(
                    formal["result_envelope"]["outputs"])
                if not proof["matches"]:
                    return self._research_feedback(
                        admission, proof, work, formal, outcome="query_miss")
            elif index in (1, 2):
                worker = self.draft_worker if index == 1 else self.verifier_worker
                validate_mission_document_work_authority(
                    self.authority, self.scheduler, work, worker=worker)
                model_proof = _exact_model_result(work, formal, worker)
                if index == 1 and model_proof["output"]["status"] == "insufficient_evidence":
                    return self._research_feedback(
                        admission, work["metadata"]["retrieval_proof"],
                        work, formal, outcome="no_verified_claim",
                        draft_proof=model_proof)
            else:
                self._stage_owned(work, formal)
                self._candidate_observation(
                    admission, work, formal, formal["result_envelope"]["outputs"])
                return self._outcome(
                    admission, effective, formal["result_envelope"]["outputs"])
        raise MissionDocumentResearchExecutorError("directed-document run has invalid shape")


__all__ = ["AUTHORITY_KIND", "MissionDocumentResearchExecutor",
           "MissionDocumentResearchExecutorError",
           "effective_mission_document_work_orders",
           "read_mission_document_research_observations",
           "validate_mission_document_work_authority"]
