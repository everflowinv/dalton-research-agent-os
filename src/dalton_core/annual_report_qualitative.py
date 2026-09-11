"""Typed model and candidate contracts for registered annual-report plans."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import timedelta, timezone
from decimal import Decimal
from importlib import resources
from typing import Any

from .contracts import ResultEnvelope, WorkOrder
from .registered_annual_report import validate_retrieval_proof
from .research_verification import (
    REGISTERED_ANNUAL_REPORT_AUTHORITY_MODE,
    REGISTERED_ANNUAL_REPORT_SOURCE_VERIFIER_HASH,
    REGISTERED_ANNUAL_REPORT_SOURCE_VERIFIER_REF,
    ResearchVerificationConflict,
    ResearchVerificationError,
    VerificationRejected,
    _canonical_raw_bytes,
    _finding_wire,
    _sha256_bytes,
    build_candidate_evidence,
    validate_candidate_claim,
    validate_candidate_evidence,
    validate_source_verification_material,
    validate_verification_bundle,
)
from .store import canonical_json, content_hash
from .transcript_polish_model_worker import RoutedTranscriptPolishModelWorker


DRAFT_OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "answer", "candidate"],
    "properties": {
        "schema_version": {"const": "0.1"},
        "answer": {"type": "string", "minLength": 1},
        "candidate": {
            "type": "object", "additionalProperties": False,
            "required": [
                "normalized_statement", "metric_or_aspect", "period", "basis",
                "cited_match_indexes",
            ],
            "properties": {
                "normalized_statement": {"type": "string", "minLength": 1},
                "metric_or_aspect": {"type": "string", "minLength": 1},
                "period": {"type": "string", "minLength": 1},
                "basis": {"type": "string", "minLength": 1},
                "cited_match_indexes": {
                    "type": "array", "minItems": 1, "uniqueItems": True,
                    "items": {"type": "integer", "minimum": 0},
                },
            },
        },
    },
}

VERIFIER_OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "verdict", "verified_statement", "findings"],
    "properties": {
        "schema_version": {"const": "0.1"},
        "verdict": {"enum": ["pass", "reject"]},
        "verified_statement": {"type": ["string", "null"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["code", "severity", "detail"],
                "properties": {
                    "code": {"type": "string", "minLength": 1},
                    "severity": {"enum": ["info", "warning", "error"]},
                    "detail": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}

VERIFIER_PROVIDER_CONTRACT_REF = "annual-report-verifier-provider-output-0.1"
_VERIFIER_PROVIDER_SCHEMA_RESOURCE = (
    "annual-report-verifier-provider-output-v0.1.schema.json"
)
VERIFIER_PROVIDER_SCHEMA_HASH = content_hash(json.loads(
    resources.files("dalton_core")
    .joinpath(_VERIFIER_PROVIDER_SCHEMA_RESOURCE)
    .read_text(encoding="utf-8")
))


class AnnualReportQualitativeError(ResearchVerificationError):
    pass


def _strict_json(text: str, name: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise AnnualReportQualitativeError(f"{name} must be JSON text")
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AnnualReportQualitativeError(f"{name} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise AnnualReportQualitativeError(f"{name} must be an object")
    return value


def validate_draft_output(value: Any, *, match_count: int) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"schema_version", "answer", "candidate"}:
        raise AnnualReportQualitativeError("draft output has an invalid closed shape")
    if value.get("schema_version") != "0.1":
        raise AnnualReportQualitativeError("draft schema_version is unsupported")
    answer = value.get("answer")
    candidate = value.get("candidate")
    if not isinstance(answer, str) or not answer.strip() or not isinstance(candidate, Mapping):
        raise AnnualReportQualitativeError("draft answer/candidate is invalid")
    fields = {
        "normalized_statement", "metric_or_aspect", "period", "basis",
        "cited_match_indexes",
    }
    if set(candidate) != fields:
        raise AnnualReportQualitativeError("draft candidate has an invalid closed shape")
    for name in fields - {"cited_match_indexes"}:
        if not isinstance(candidate.get(name), str) or not candidate[name].strip():
            raise AnnualReportQualitativeError(f"draft candidate {name} is invalid")
    indexes = candidate.get("cited_match_indexes")
    if (not isinstance(indexes, list) or not indexes
            or any(isinstance(item, bool) or not isinstance(item, int)
                   or item < 0 or item >= match_count for item in indexes)
            or len(indexes) != len(set(indexes))):
        raise AnnualReportQualitativeError("draft citations do not bind retrieval matches")
    return json.loads(canonical_json(value))


def validate_verifier_output(value: Any, *, draft: Mapping[str, Any]) -> dict[str, Any]:
    fields = {"schema_version", "verdict", "verified_statement", "findings"}
    if not isinstance(value, Mapping) or set(value) != fields or value.get("schema_version") != "0.1":
        raise AnnualReportQualitativeError("verifier output has an invalid closed shape")
    verdict = value.get("verdict")
    statement = value.get("verified_statement")
    findings = value.get("findings")
    if verdict not in {"pass", "reject"} or not isinstance(findings, list):
        raise AnnualReportQualitativeError("verifier verdict/findings are invalid")
    for finding in findings:
        if (not isinstance(finding, Mapping)
                or set(finding) != {"code", "severity", "detail"}
                or finding.get("severity") not in {"info", "warning", "error"}
                or any(not isinstance(finding.get(key), str) or not finding[key]
                       for key in ("code", "detail"))):
            raise AnnualReportQualitativeError("verifier finding is invalid")
    has_error = any(item["severity"] == "error" for item in findings)
    if verdict == "pass":
        expected = draft["candidate"]["normalized_statement"]
        if statement != expected or has_error:
            raise AnnualReportQualitativeError(
                "passing verifier must preserve the exact candidate statement and have no error"
            )
    elif statement is not None or not has_error:
        raise AnnualReportQualitativeError(
            "rejecting verifier must return null statement and an error finding"
        )
    return json.loads(canonical_json(value))


def parse_draft_text(text: str, *, match_count: int) -> dict[str, Any]:
    return validate_draft_output(_strict_json(text, "draft output"), match_count=match_count)


def parse_verifier_text(text: str, *, draft: Mapping[str, Any]) -> dict[str, Any]:
    return validate_verifier_output(_strict_json(text, "verifier output"), draft=draft)


def draft_prompt(*, question: str, retrieval_proof: Mapping[str, Any]) -> str:
    return canonical_json({
        "task": "Answer the complete research question using only the registered SEC annual-report matches. Return one draft-only qualitative candidate; do not assert numeric authority.",
        "question": question,
        "retrieval_proof": retrieval_proof,
        "output_schema": DRAFT_OUTPUT_SCHEMA,
    })


def verifier_prompt(
    *, question: str, retrieval_proof: Mapping[str, Any], draft: Mapping[str, Any],
    producer_proof: Mapping[str, Any],
) -> str:
    return canonical_json({
        "task": "Independently verify that the draft answers the complete question and every claim is supported by its exact registered SEC annual-report excerpts. Reject unsupported inference. Return JSON only.",
        "question": question,
        "retrieval_proof": retrieval_proof,
        "draft": draft,
        "producer_proof": producer_proof,
        "output_schema": VERIFIER_OUTPUT_SCHEMA,
    })


def validate_model_proof(
    value: Any, *, stage: str, work: WorkOrder | Mapping[str, Any]
) -> dict[str, Any]:
    work_wire = work.to_dict() if isinstance(work, WorkOrder) else dict(work)
    fields = {
        "schema_version", "id", "stage", "work_order_ref", "work_order_hash",
        "prompt_hash", "request_binding_hash", "route_decision_ref",
        "model_invocation_ref", "model_family", "output", "output_hash",
        "content_hash",
    }
    if not isinstance(value, Mapping) or set(value) != fields or value.get("schema_version") != "0.1":
        raise AnnualReportQualitativeError("model proof has an invalid closed shape")
    body = dict(value)
    asserted = body.pop("content_hash", None)
    if asserted != content_hash(body):
        raise AnnualReportQualitativeError("model proof content_hash mismatch")
    if (value.get("stage") != stage or value.get("work_order_ref") != work_wire["id"]
            or value.get("work_order_hash") != content_hash(work_wire)
            or value.get("prompt_hash") != content_hash(work_wire["question"])
            or value.get("request_binding_hash")
            != work_wire["metadata"].get("model_request_binding_hash")):
        raise AnnualReportQualitativeError("model proof drifted from exact WorkOrder request")
    if not isinstance(value.get("route_decision_ref"), str) or not value["route_decision_ref"]:
        raise AnnualReportQualitativeError("model proof lacks a route decision")
    if not isinstance(value.get("model_invocation_ref"), str) or not value["model_invocation_ref"]:
        raise AnnualReportQualitativeError("model proof lacks an invocation")
    if not isinstance(value.get("model_family"), str) or not value["model_family"]:
        raise AnnualReportQualitativeError("model proof lacks model family")
    if value.get("output_hash") != content_hash(value.get("output")):
        raise AnnualReportQualitativeError("model proof output hash drifted")
    return dict(value)


class RegisteredAnnualReportModelWorker(RoutedTranscriptPolishModelWorker):
    """The existing router/adapter/accounting worker with annual-report schemas."""

    worker_ref = "worker:registered-annual-report-model"
    namespace = "registered-annual-report-model"
    purpose = "registered_annual_report_draft"
    expected_stage: str | None = None

    def __init__(self, *, budget_store=None, budget_policy_ref=None,
                 mission_resolver: Callable[[str, str], Mapping[str, Any]] | None = None,
                 mission_annual_research_authority=None,
                 transport_retry: Mapping[str, Any] | None = None,
                 **kwargs):
        """Bind production annual calls to the same durable mission budget.

        Fixture adapters remain usable without a budget authority.  A real
        OpenClaw adapter is refused unless all three authority inputs exist;
        an annual call must never silently become an unbudgeted model call.
        """
        from .openclaw_model_adapter import OpenClawModelAdapter
        from .thesis_impact_budget import ThesisImpactBudgetStore

        adapter = kwargs.get("adapter")
        production = isinstance(adapter, OpenClawModelAdapter)
        if production and not (
            isinstance(budget_store, ThesisImpactBudgetStore)
            and isinstance(budget_policy_ref, str) and budget_policy_ref
            and callable(mission_resolver)
        ):
            raise AnnualReportQualitativeError(
                "annual-report broker execution requires mission budget authority"
            )
        self.budget_store = budget_store
        self.budget_policy_ref = budget_policy_ref
        self.mission_resolver = mission_resolver
        self.mission_annual_research_authority = mission_annual_research_authority
        if transport_retry is None:
            self.transport_retry = None
        else:
            from .annual_report_runtime import validate_annual_transport_retry

            self.transport_retry = validate_annual_transport_retry(transport_retry)
        self._production_adapter = production
        self.admission = None
        self._admission_identity = None
        super().__init__(**kwargs)

    def _before_model_call(self, work, route, profile, replayed):
        if self.budget_store is None:
            return
        from .openclaw_model_adapter import OpenClawModelAdapterError
        try:
            self._admit_model_call(work, route, profile, replayed)
        except OpenClawModelAdapterError:
            raise
        except Exception as exc:
            raise OpenClawModelAdapterError(
                "annual-report budget/mission admission rejected"
            ) from exc

    def _before_transport_send(self, work, route, profile):
        """Recheck the Work deadline at the last boundary before broker send."""

        from .openclaw_model_adapter import OpenClawModelAdapterError

        deadline = self._work_deadline(work)
        if deadline is not None:
            from .annual_report_runtime import (
                ANNUAL_LEASE_COMPLETION_GRACE_SECONDS,
            )

            queue_seconds = int((self.transport_retry or {}).get(
                "queue_wait_seconds", 0
            ))
            required = (
                int(work.budget["max_seconds"])
                + queue_seconds
                + ANNUAL_LEASE_COMPLETION_GRACE_SECONDS
            )
            if (self.clock().astimezone(timezone.utc)
                    + timedelta(seconds=required)
                    > deadline):
                raise OpenClawModelAdapterError(
                    "annual-report Work has insufficient elapsed budget for "
                    "another broker send"
                )
        self._before_model_call(work, route, profile, False)

    def _execute_model(self, work, route, profile):
        """Retry only adapter-proved pre-send failures within the frozen bound."""

        if not self._production_adapter:
            return super()._execute_model(work, route, profile)
        from .openclaw_model_adapter import BrokerDefinitelyNotSent

        retry = self.transport_retry or {
            "max_definitely_not_sent_retries": 0,
            "queue_wait_seconds": 0,
            "retry_backoff_seconds": 0,
        }
        maximum = retry["max_definitely_not_sent_retries"]
        for retry_number in range(maximum + 1):
            try:
                return self.adapter.execute(
                    work, route, profile,
                    before_send=lambda: self._before_transport_send(
                        work, route, profile
                    ),
                )
            except BrokerDefinitelyNotSent:
                if retry_number >= maximum:
                    raise
                if retry["retry_backoff_seconds"]:
                    import time

                    time.sleep(retry["retry_backoff_seconds"])

    def _admit_model_call(self, work, route, profile, replayed):
        if (work.metadata.get("budget_db") != self.budget_store.path
                or work.metadata.get("budget_policy_ref") != self.budget_policy_ref):
            raise AnnualReportQualitativeError("annual-report budget binding drifted")
        registration = work.metadata["retrieval_proof"]["registration"]
        mission_ref = registration["mission_version_ref"]
        company_ref = registration["company_ref"]
        mission = self.mission_resolver(mission_ref, company_ref)
        if (mission.get("id") != mission_ref
                or not isinstance(mission.get("outer_budget"), Mapping)):
            raise AnnualReportQualitativeError("annual-report mission authority drifted")
        phase = (
            "verification"
            if work.metadata["stage"] == "independent_qualitative_verifier"
            else "assessment"
        )
        identity = (work.id, int(route["attempt_number"]))
        if self._admission_identity != identity:
            self.admission = None
            self._admission_identity = identity
        if self.admission is not None:
            return
        ceiling = int(Decimal(str(work.budget["max_cost_usd"])) * 1_000_000)
        from .budget_pools import mission_pool_scope
        scope = {
            "mission_ref": mission["mission_ref"],
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "max_daily_paid_calls": mission["budget"]["max_daily_paid_calls"],
            "max_daily_cost_micros": int(
                Decimal(str(mission["budget"]["max_daily_cost_usd"])) * 1_000_000
            ),
            "outer_budget": dict(mission["outer_budget"]),
            **mission_pool_scope(mission, purpose=self.purpose),
        }
        prior = self.budget_store.admission(
            work_order_ref=work.id,
            attempt_number=int(route["attempt_number"]),
            phase=phase,
        )
        if replayed and prior is None:
            raise AnnualReportQualitativeError(
                "annual-report recovery has no original budget admission"
            )
        day = (
            prior["admission"]["day"] if prior is not None
            else self.clock().astimezone(timezone.utc).date().isoformat()
        )
        self.admission = self.budget_store.admit(
            policy_version_id=self.budget_policy_ref,
            day=day,
            work_order_ref=work.id,
            attempt_number=route["attempt_number"], phase=phase,
            route_decision_ref=route["id"], reserved_micros=ceiling,
            mission_binding=scope,
        )
        if self.admission.get("status") == "rejected":
            raise AnnualReportQualitativeError("annual-report mission budget refused the call")

    def _after_accounting(self, work, route, accounting):
        if self.budget_store is None:
            return None
        cost = accounting["cost"]
        if cost["amount_micros"] > self.admission["reserved_micros"]:
            phase = (
                "verification"
                if work.metadata["stage"] == "independent_qualitative_verifier"
                else "assessment"
            )
            self.budget_store.record_alert(
                alert_id="annual-report-overrun:" + content_hash({
                    "work_order_ref": work.id,
                    "attempt_number": route["attempt_number"],
                    "cost_entry_ref": cost["id"],
                }),
                kind="work_order_failed", severity="high",
                work_order_ref=work.id, phase=phase,
                detail={
                    "reason": "model_reservation_overrun",
                    "cost_entry_ref": cost["id"],
                    "amount_micros": cost["amount_micros"],
                    "cost_status": cost["cost_status"],
                    "reserved_micros": self.admission["reserved_micros"],
                },
            )
            return "MODEL_COST_EXCEEDED_RESERVATION"
        # Estimated/unknown usage retains the full reservation.  It is not a
        # zero-cost failure and is the only safe basis for later redrive.
        if cost["cost_status"] == "actual":
            self.budget_store.settle(
                self.admission["admission_id"],
                actual_micros=cost["amount_micros"],
                usage_entry_ref=accounting["usage"]["id"],
            )
        return None

    def _after_capacity_deferred(self, work, route, adapter_result):
        if self.budget_store is not None and self.admission is not None:
            self.budget_store.settle(self.admission["admission_id"], actual_micros=0)

    @staticmethod
    def _validate_candidate_sink(_sink: Any) -> None:
        return None

    def _work(self, value: WorkOrder | Mapping[str, Any]) -> WorkOrder:
        work = WorkOrder.from_dict(value.to_dict() if isinstance(value, WorkOrder) else value)
        metadata = work.metadata
        if metadata.get("stage") not in {"qualitative_model_draft", "independent_qualitative_verifier"}:
            raise AnnualReportQualitativeError("model WorkOrder stage is not qualitative")
        if self.expected_stage is not None and metadata["stage"] != self.expected_stage:
            raise AnnualReportQualitativeError(
                "annual-report model worker received the wrong qualitative stage"
            )
        if (metadata.get("routing_policy_ref") != self.routing_policy_ref
                or metadata.get("credential_slot_refs") != list(self.credential_slot_refs)
                or metadata.get("transport_retry") != self.transport_retry
                or metadata.get("prompt_hash") != content_hash(work.question)):
            raise AnnualReportQualitativeError("model WorkOrder routing/prompt binding drifted")
        if metadata.get("authority_kind") == "mission_annual_research_admission":
            if self.mission_annual_research_authority is None:
                raise AnnualReportQualitativeError(
                    "mission annual WorkOrder has no execution authority resolver"
                )
            try:
                from .mission_annual_research_executor import (
                    validate_mission_annual_work_authority,
                )

                validate_mission_annual_work_authority(
                    self.mission_annual_research_authority, work
                )
            except Exception as exc:
                raise AnnualReportQualitativeError(
                    "mission annual WorkOrder authority is invalid"
                ) from exc
        return work

    def _parse_candidate(self, text: str, work: WorkOrder) -> None:
        stage = work.metadata["stage"]
        if stage == "qualitative_model_draft":
            parse_draft_text(text, match_count=work.metadata["retrieval_match_count"])
        else:
            parse_verifier_text(text, draft=work.metadata["draft"])

    @staticmethod
    def _admit_candidate(_work: WorkOrder, _candidate_text: str) -> None:
        return None

    def _producer_route(self, work: WorkOrder) -> dict[str, Any] | None:
        if work.metadata["stage"] == "qualitative_model_draft":
            return None
        draft_proof = work.metadata.get("draft_proof")
        route_ref = work.metadata.get("producer_route_decision_ref")
        family = work.metadata.get("producer_model_family")
        if (
            not isinstance(draft_proof, Mapping)
            or route_ref != draft_proof.get("route_decision_ref")
            or family != draft_proof.get("model_family")
        ):
            raise AnnualReportQualitativeError(
                "verifier producer authority drifted from the draft proof"
            )
        try:
            route = self.router.get_decision(route_ref)
        except Exception as exc:
            raise AnnualReportQualitativeError(
                "verifier producer route decision is unavailable"
            ) from exc
        endpoint = route.get("selected_endpoint")
        if (
            route.get("outcome") != "selected"
            or route.get("work_order_ref") != draft_proof.get("work_order_ref")
            or not isinstance(endpoint, Mapping)
            or endpoint.get("family") != family
        ):
            raise AnnualReportQualitativeError(
                "verifier producer family is not proved by its route decision"
            )
        return route

    def _producer_family(self, work: WorkOrder):
        route = self._producer_route(work)
        return None if route is None else route["selected_endpoint"]["family"]

    def _producer_decision_ref(self, work: WorkOrder):
        route = self._producer_route(work)
        return None if route is None else route["id"]

    def _route_capability(self, work: WorkOrder) -> str:
        if len(work.requested_capabilities) != 1:
            raise AnnualReportQualitativeError(
                "qualitative model WorkOrder must request one capability"
            )
        return work.requested_capabilities[0]

    def _successful_result(self, work, route, invocation, result, candidate_text):
        stage = work.metadata["stage"]
        output = (
            parse_draft_text(candidate_text, match_count=work.metadata["retrieval_match_count"])
            if stage == "qualitative_model_draft"
            else parse_verifier_text(candidate_text, draft=work.metadata["draft"])
        )
        body = {
            "schema_version": "0.1",
            "id": "annual-report-model-proof:" + content_hash({
                "work_order_ref": work.id, "route": route["id"],
                "invocation": invocation.id, "output": output,
            })[:32],
            "stage": stage, "work_order_ref": work.id,
            "work_order_hash": content_hash(work.to_dict()),
            "prompt_hash": content_hash(work.question),
            "request_binding_hash": work.metadata["model_request_binding_hash"],
            "route_decision_ref": route["id"], "model_invocation_ref": invocation.id,
            "model_family": invocation.model_family, "output": output,
            "output_hash": content_hash(output),
        }
        body["content_hash"] = content_hash(body)
        validate_model_proof(body, stage=stage, work=work)
        return ResultEnvelope(
            schema_version=result.schema_version, id=result.id,
            created_at=result.created_at, work_order_ref=result.work_order_ref,
            invocation_ref=result.invocation_ref, status=result.status, outputs=body,
            actual_side_effects=result.actual_side_effects,
            usage_refs=result.usage_refs, artifact_refs=result.artifact_refs,
            error=result.error, metadata={
                **dict(result.metadata), "route_decision_ref": route["id"],
                "model_request_binding_hash": work.metadata["model_request_binding_hash"],
            },
        )


class RegisteredAnnualReportDraftWorker(RegisteredAnnualReportModelWorker):
    purpose = "registered_annual_report_draft"
    expected_stage = "qualitative_model_draft"


class RegisteredAnnualReportVerifierWorker(RegisteredAnnualReportModelWorker):
    purpose = "registered_annual_report_verifier"
    expected_stage = "independent_qualitative_verifier"


class AnnualReportCandidateAuthority:
    """Rebuild CandidateStaging provenance from exact formal plan outputs."""

    def __init__(self, *, question: str, proof: Mapping[str, Any], draft_proof: Mapping[str, Any], verifier_proof: Mapping[str, Any]):
        self.question = question
        self.proof = dict(proof)
        self.draft_proof = dict(draft_proof)
        self.verifier_proof = dict(verifier_proof)

    def build_material(self, created_at: str) -> dict[str, Any]:
        registration = self.proof["registration"]
        payload = {
            "question": self.question,
            "retrieval_proof": self.proof,
            "draft_proof": self.draft_proof,
            "verifier_proof": self.verifier_proof,
        }
        locations = list(dict.fromkeys(
            item["source_location"] for item in self.proof["matches"]
        ))
        base = {
            "schema_version": "0.2",
            "id": "source-material:registered-annual-report:" + self.proof["content_hash"],
            "created_at": created_at,
            "source_envelope_ref": registration["id"],
            "source_envelope_hash": content_hash(registration),
            "artifact_ref": registration["source_manifest_ref"],
            "artifact_hash": registration["source_manifest_hash"],
            "source_ref": "source:sec-edgar", "source_type": "official_filing",
            "operation": "search_registered_annual_report",
            "provenance_mode": REGISTERED_ANNUAL_REPORT_AUTHORITY_MODE,
            "authority_resolution_ref": self.verifier_proof["id"],
            "authority_resolution_hash": self.verifier_proof["content_hash"],
            "source_record_refs": [f"sec:filing:{registration['accession']}", *locations],
            "next_cursor": None, "normalized_payload": payload,
            "normalized_payload_hash": _sha256_bytes(
                _canonical_raw_bytes(payload, "annual report candidate payload"),
                "annual report candidate payload",
            ),
            "source_schema_hash": content_hash({
                "retrieval": "schema:registered-annual-report-retrieval-proof:0.2",
                "draft": DRAFT_OUTPUT_SCHEMA, "verifier": VERIFIER_OUTPUT_SCHEMA,
            }),
            "source_content_hash": registration["source_content_hash"],
            "source_lineage": [
                "source:sec-edgar", registration["company_ref"],
                f"sec:filing:{registration['accession']}",
                registration["source_manifest_ref"], self.proof["id"],
                self.draft_proof["id"], self.verifier_proof["id"],
            ],
            "published_at": None, "updated_at": None, "as_of": None,
            "retrieved_at": created_at, "completeness": "partial", "status": "partial",
        }
        base["content_hash"] = content_hash(base)
        return validate_source_verification_material(base)

    def verify_source_material(self, material: Mapping[str, Any]) -> dict[str, Any]:
        material_wire = validate_source_verification_material(material)
        recomputed = self.build_material(material_wire["created_at"])
        if canonical_json(recomputed) != canonical_json(material_wire):
            raise ResearchVerificationConflict("annual report candidate material drifted")
        findings = [
            _finding_wire(
                "independent_model_verdict", "info", "pass",
                "verifier.verdict", "pass", "pass",
                "independent routed verifier passed the exact draft and filing proof",
            ),
            _finding_wire(
                "registered_sec_source", "info", "pass",
                "retrieval.registration", "Core registered SEC 10-K", "Core registered SEC 10-K",
                "retrieval proof binds statement issuer and acquired document authority",
            ),
        ]
        body = {
            "schema_version": "0.1",
            "id": "verification-bundle:registered-annual-report-source:" + content_hash({
                "material": material_wire["content_hash"],
                "verifier": self.verifier_proof["content_hash"],
            }),
            "created_at": material_wire["retrieved_at"], "kind": "source",
            "subject_ref": material_wire["id"], "subject_hash": material_wire["content_hash"],
            "verdict": "pass", "checkpoint_ref": self.verifier_proof["id"],
            "checkpoint_hash": self.verifier_proof["content_hash"], "findings": findings,
            "verifier_ref": REGISTERED_ANNUAL_REPORT_SOURCE_VERIFIER_REF,
            "verifier_hash": REGISTERED_ANNUAL_REPORT_SOURCE_VERIFIER_HASH,
        }
        body["content_hash"] = content_hash(body)
        return validate_verification_bundle(body)


def build_annual_report_qualitative_candidate(
    evidence: Mapping[str, Any], source_verification: Mapping[str, Any], *,
    candidate_claim_ref: str, subject_ref: str, candidate: Mapping[str, Any],
    actor_ref: str, created_at: str,
) -> dict[str, Any]:
    evidence_wire = validate_candidate_evidence(evidence)
    source_wire = validate_verification_bundle(source_verification)
    if source_wire["kind"] != "source" or source_wire["verdict"] != "pass":
        raise VerificationRejected("annual report qualitative candidate needs passing source verification")
    if evidence_wire["source_type"] != "official_filing":
        raise VerificationRejected("annual report evidence must be an official filing")
    base = {
        "schema_version": "0.1",
        "id": "candidate-claim-version:" + content_hash({
            "candidate_claim_ref": candidate_claim_ref, "version": 1,
        }),
        "created_at": created_at, "candidate_claim_ref": candidate_claim_ref,
        "version": 1, "subject_ref": subject_ref,
        "metric_or_aspect": candidate["metric_or_aspect"], "period": candidate["period"],
        "basis": candidate["basis"],
        "normalized_statement": candidate["normalized_statement"],
        "semantic_verification_status": "unverified", "claim_kind": "qualitative",
        "value": None, "unit": None, "currency": None, "scale": None,
        "candidate_evidence_refs": [{"ref": evidence_wire["id"], "hash": evidence_wire["content_hash"]}],
        "source_verification_ref": source_wire["id"],
        "source_verification_hash": source_wire["content_hash"],
        "numeric_spec_ref": None, "numeric_spec_hash": None,
        "numeric_verification_ref": None, "numeric_verification_hash": None,
        "actor_ref": actor_ref, "prior_version_ref": None,
    }
    base["content_hash"] = content_hash(base)
    return validate_candidate_claim(base)


def stage_annual_report_candidate(
    staging: Any, *, question_ref: str, question: str, proof: Mapping[str, Any],
    draft_proof: Mapping[str, Any], verifier_proof: Mapping[str, Any],
    draft_work: WorkOrder | Mapping[str, Any],
    verifier_work: WorkOrder | Mapping[str, Any],
    actor_ref: str, created_at: str, idempotency_key: str,
) -> dict[str, Any]:
    draft_proof = validate_model_proof(
        draft_proof, stage="qualitative_model_draft", work=draft_work
    )
    verifier_proof = validate_model_proof(
        verifier_proof, stage="independent_qualitative_verifier", work=verifier_work
    )
    if verifier_proof["output"]["verdict"] != "pass":
        raise VerificationRejected("independent qualitative verifier rejected the draft")
    authority = AnnualReportCandidateAuthority(
        question=question, proof=proof, draft_proof=draft_proof,
        verifier_proof=verifier_proof,
    )
    material = authority.build_material(created_at)
    source_verification = authority.verify_source_material(material)
    evidence = build_candidate_evidence(
        material, source_verification,
        candidate_evidence_ref="candidate-evidence:annual-report:" + content_hash({
            "proof": proof["content_hash"], "draft": draft_proof["content_hash"],
        })[:32],
        actor_ref=actor_ref, created_at=created_at,
        verification_mode=REGISTERED_ANNUAL_REPORT_AUTHORITY_MODE,
    )
    candidate = draft_proof["output"]["candidate"]
    claim = build_annual_report_qualitative_candidate(
        evidence, source_verification,
        candidate_claim_ref="candidate-claim:annual-report:" + content_hash({
            "question_ref": question_ref, "statement": candidate["normalized_statement"],
            "proof": verifier_proof["content_hash"],
        })[:32],
        subject_ref=proof["registration"]["company_ref"], candidate=candidate,
        actor_ref=actor_ref, created_at=created_at,
    )
    staged = staging.stage(
        material=material, source_verification=source_verification,
        evidence=evidence, claim=claim, idempotency_key=idempotency_key,
        verification_mode=REGISTERED_ANNUAL_REPORT_AUTHORITY_MODE,
        authority_resolver=authority,
    )
    return {
        "staging": staged, "material": material,
        "source_verification": source_verification,
        "evidence": evidence, "claim": claim,
    }


__all__ = [
    "DRAFT_OUTPUT_SCHEMA", "VERIFIER_OUTPUT_SCHEMA", "AnnualReportCandidateAuthority",
    "AnnualReportQualitativeError", "RegisteredAnnualReportDraftWorker",
    "RegisteredAnnualReportModelWorker", "RegisteredAnnualReportVerifierWorker",
    "build_annual_report_qualitative_candidate", "draft_prompt", "parse_draft_text",
    "parse_verifier_text", "stage_annual_report_candidate", "validate_draft_output",
    "validate_model_proof", "validate_verifier_output", "verifier_prompt",
]
