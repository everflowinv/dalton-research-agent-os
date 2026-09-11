"""Durable four-stage executor for one source-neutral directed-document admission."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .annual_report_qualitative import (
    AnnualReportQualitativeError, VERIFIER_PROVIDER_SCHEMA_HASH,
    validate_model_proof,
)
from .contracts import ResultEnvelope, WorkOrder
from .document_research import DocumentResearchError, DocumentResearchRegistry
from .document_research_qualitative import (
    DRAFT_PURPOSE, VERIFIER_PURPOSE, MissionDocumentDraftWorker,
    MissionDocumentVerifierWorker, build_candidate_bundle, draft_prompt,
    stage_candidate, verifier_prompt,
)
from .mission_document_research import (
    MissionDocumentResearchAuthority, MissionDocumentResearchError,
)
from .research_verification import CandidateStagingStore, ResearchVerificationError
from .scheduler import Scheduler
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
        "outcome", "tried_query_terms", "meaning", "suggested_actions",
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
                or (wire.get("draft_proof_ref") is None)
                != (wire.get("outcome") in {"query_miss", "recovery_required"})
                or not isinstance(wire.get("missing_evidence"), list)):
            raise MissionDocumentResearchExecutorError(
                "stored directed-document feedback drifted")
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
         ["capability:dalton:model:qualitative-research"], []),
        ("independent_qualitative_verifier", "mission_directed_document_verifier",
         ["capability:dalton:model:qualitative-verifier"], []),
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
            "question": question, "requested_capabilities": step["requested_capabilities"],
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


def validate_mission_document_work_authority(authority, scheduler, work):
    wire = WorkOrder.from_dict(work.to_dict() if isinstance(work, WorkOrder) else work).to_dict()
    if wire["metadata"].get("authority_kind") != AUTHORITY_KIND:
        raise MissionDocumentResearchExecutorError("Work lacks directed-document authority")
    admission = authority.resolve_for_execution(
        wire["metadata"].get("mission_document_research_admission_ref"))
    blueprints = _blueprints(admission)
    indexes = [i for i, item in enumerate(blueprints) if item["id"] == wire["id"]]
    if len(indexes) != 1 or indexes[0] not in (1, 2):
        raise MissionDocumentResearchExecutorError("Work is not an admitted model stage")
    expected = _derive(admission, scheduler, authority.registry, blueprints, indexes[0])
    stored = scheduler.work_order_authority(wire["id"])
    if (canonical_json(wire) != canonical_json(expected) or stored is None
            or stored["work_order_hash"] != content_hash(expected)
            or canonical_json(stored["work_order"]) != canonical_json(expected)):
        raise MissionDocumentResearchExecutorError("Work drifted from exact derivation")
    return admission


class MissionDocumentResearchExecutor:
    _authorized = authorized_flag()

    def __init__(self, *, authority: MissionDocumentResearchAuthority, scheduler: Scheduler,
                 registry: DocumentResearchRegistry, draft_worker: MissionDocumentDraftWorker,
                 verifier_worker: MissionDocumentVerifierWorker, staging: CandidateStagingStore,
                 actor_ref: str, clock: Callable[[], datetime] = _now):
        if not isinstance(authority, MissionDocumentResearchAuthority):
            raise TypeError("authority has the wrong type")
        if not isinstance(scheduler, Scheduler) or not isinstance(registry, DocumentResearchRegistry):
            raise TypeError("executor requires Scheduler and DocumentResearchRegistry")
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
        staged = stage_candidate(
            self.staging, admission=admission,
            proof=works[3]["metadata"]["retrieval_proof"],
            draft_proof=works[3]["metadata"]["draft_proof"], verifier_proof=verifier,
            draft_work=works[1], verifier_work=works[2], created_at=works[3]["created_at"],
            idempotency_key=f"mission-document-research-candidate:{admission['id']}")
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
        expected = build_candidate_bundle(
            admission=admission, proof=works[3]["metadata"]["retrieval_proof"],
            draft_proof=works[3]["metadata"]["draft_proof"],
            verifier_proof=validate_model_proof(
                formal["result_envelope"]["outputs"],
                stage="independent_qualitative_verifier", work=works[2]),
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
            verifier = validate_model_proof(formal["result_envelope"]["outputs"],
                stage="independent_qualitative_verifier", work=works[2])
            records = self._staging_records(admission, works, verifier)
            self._validate_records(admission, works, records)
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
        blueprints = _blueprints(admission)
        self._start(admission, blueprints[0])
        for index in range(4):
            work = _derive(admission, self.scheduler, self.registry, blueprints, index)
            stored = self.scheduler.work_order_authority(work["id"])
            if stored is None:
                return self._enqueue(work)
            if stored["work_order_hash"] != content_hash(work):
                raise MissionDocumentResearchExecutorError("stored Work drifted")
            formal = self.scheduler.formal_result(work["id"])
            if formal is None:
                if index == 0:
                    return self._complete_retrieval(admission, work)
                if index in (1, 2):
                    validate_mission_document_work_authority(self.authority, self.scheduler, work)
                    worker = self.draft_worker if index == 1 else self.verifier_worker
                    try:
                        result = worker.run_once(work)
                    except AnnualReportQualitativeError as exc:
                        raise MissionDocumentResearchExecutorError(str(exc)) from exc
                    return {**result, "work_order_ref": work["id"], "stage": work["metadata"]["stage"]}
                works = [_derive(admission, self.scheduler, self.registry, blueprints, i)
                         for i in range(4)]
                return self._complete_staging(admission, works)
            if formal["terminal_state"] != "succeeded":
                return {"status": "blocked", "work_order_ref": work["id"],
                        "stage": work["metadata"]["stage"]}
            if index == 0:
                proof = self.registry.verify_search_proof(
                    formal["result_envelope"]["outputs"])
                if not proof["matches"]:
                    return self._research_feedback(
                        admission, proof, work, formal, outcome="query_miss")
            elif index in (1, 2):
                validate_mission_document_work_authority(self.authority, self.scheduler, work)
                model_proof = validate_model_proof(
                    formal["result_envelope"]["outputs"],
                    stage=work["metadata"]["stage"], work=work)
                if index == 1 and model_proof["output"]["status"] == "insufficient_evidence":
                    return self._research_feedback(
                        admission, work["metadata"]["retrieval_proof"],
                        work, formal, outcome="no_verified_claim",
                        draft_proof=model_proof)
            else:
                self._stage_owned(work, formal)
                works = [_derive(admission, self.scheduler, self.registry, blueprints, i)
                         for i in range(4)]
                self._candidate_observation(
                    admission, work, formal, formal["result_envelope"]["outputs"])
                return self._outcome(admission, works, formal["result_envelope"]["outputs"])
        raise MissionDocumentResearchExecutorError("directed-document run has invalid shape")


__all__ = ["AUTHORITY_KIND", "MissionDocumentResearchExecutor",
           "MissionDocumentResearchExecutorError",
           "read_mission_document_research_observations",
           "validate_mission_document_work_authority"]
