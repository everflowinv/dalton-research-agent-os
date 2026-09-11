"""Execute one mission-bound targeted annual-report repair admission.

This is deliberately separate from the human-approved ResearchPlan executor.
It consumes only the narrow authority recorded by MissionAnnualResearchAuthority
and runs the existing registered local retrieval, draft, independent verifier,
and candidate-staging implementations one durable node at a time.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .annual_report_qualitative import (
    AnnualReportQualitativeError,
    RegisteredAnnualReportDraftWorker,
    RegisteredAnnualReportVerifierWorker,
    build_annual_report_candidate_bundle,
    stage_annual_report_candidate,
    qualitative_router_capability,
    validate_model_proof,
)
from .contracts import ResultEnvelope, WorkOrder
from .mission_annual_research import (
    MissionAnnualResearchAuthority, MissionAnnualResearchError,
)
from .registered_annual_report import (
    RegisteredAnnualReportError, RegisteredAnnualReportRegistry,
    validate_retrieval_proof,
)
from .research_plan import (
    _REGISTERED_ANNUAL_REPORT_BUDGET,
    _REGISTERED_ANNUAL_REPORT_DOWNSTREAM_STEP_SPECS,
    _REGISTERED_ANNUAL_REPORT_STEP_SPEC,
    _resolve_qualitative_child_work_order,
)
from .research_verification import CandidateStagingStore, ResearchVerificationError
from .scheduler import Scheduler
from .store import (
    authorization_flag, authorized_flag, canonical_json, content_hash,
)


SCHEMA_VERSION = "0.1"
AUTHORITY_KIND = "mission_annual_research_admission"
_SCHEMA_PATH = Path(__file__).with_name("mission_annual_research_executor_schema.sql")


class MissionAnnualResearchExecutorError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _wire_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise MissionAnnualResearchExecutorError("executor clock must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _ref(prefix: str, value: Any) -> str:
    return f"{prefix}:{content_hash(value)[:32]}"


def _mission_annual_steps(admission: Mapping[str, Any]) -> list[dict[str, Any]]:
    specs = (_REGISTERED_ANNUAL_REPORT_STEP_SPEC, *_REGISTERED_ANNUAL_REPORT_DOWNSTREAM_STEP_SPECS)
    run_id = _ref("mission-annual-research-run", {
        "admission_ref": admission["id"], "admission_hash": admission["content_hash"],
    })
    result = []
    for ordinal, spec in enumerate(specs, 1):
        body = {
            "schema_version": SCHEMA_VERSION,
            "id": f"mission-annual-research-step:{run_id.rsplit(':', 1)[-1]}:{ordinal}",
            "ordinal": ordinal, **dict(spec),
            "max_attempts": (
                1 if ordinal == 1
                else sum(
                    admission["request"]["model_execution"][stage]["max_attempts"]
                    for stage in ("draft", "verifier")
                ) if ordinal == 4
                else admission["request"]["model_execution"][
                    "draft" if ordinal == 2 else "verifier"
                ]["max_attempts"]
            ),
        }
        body["content_hash"] = content_hash(body)
        result.append(body)
    return result


def _mission_annual_common_metadata(
    admission: Mapping[str, Any], step: Mapping[str, Any],
    work_ref: str, upstream_ref: str | None,
) -> dict[str, Any]:
    binding = {
        "admission_ref": admission["id"],
        "admission_hash": admission["content_hash"],
        "work_order_ref": work_ref,
        "stage": step["stage"], "step_ref": step["id"],
        "upstream_work_order_ref": upstream_ref,
    }
    return {
        "authority_kind": AUTHORITY_KIND,
        "mission_annual_research_admission_ref": admission["id"],
        "mission_annual_research_admission_hash": admission["content_hash"],
        "mission_annual_work_binding_hash": content_hash(binding),
        "mission_version_ref": admission["mission_version_ref"],
        "mission_version_hash": admission["mission_version_hash"],
        "repair_feedback_ref": admission["repair_feedback_ref"],
        "repair_feedback_hash": admission["repair_feedback_hash"],
        "repair_target_ref": admission["repair_target_ref"],
        "repair_target_hash": admission["repair_target_hash"],
        "source_content_hash": admission["request"]["source_content_hash"],
        "step_ref": step["id"], "step_hash": step["content_hash"],
        "stage": step["stage"], "operation": step["operation"],
        "permission_scope": "registered_annual_report_read",
        "upstream_work_order_ref": upstream_ref,
    }


def _mission_annual_blueprints(admission: Mapping[str, Any]) -> list[dict[str, Any]]:
    steps = _mission_annual_steps(admission)
    works: list[dict[str, Any]] = []
    for step in steps:
        ordinal = step["ordinal"]
        work_ref = "work:mission-annual-research-" + content_hash({
            "admission_identity_hash": admission["identity_hash"],
            "ordinal": ordinal,
        })[:32]
        prior_ref = works[-1]["id"] if works else None
        model_key = "draft" if ordinal == 2 else "verifier" if ordinal == 3 else None
        execution = None if model_key is None else admission["request"]["model_execution"][model_key]
        if ordinal == 1:
            question = (
                "Search registered SEC annual report for CIK "
                f"{admission['request']['issuer_cik']}, accession "
                f"{admission['request']['accession']}, source SHA-256 "
                f"{admission['request']['source_content_hash']}, terms "
                f"{','.join(admission['request']['query_terms'])}"
            )
        else:
            question = f"Mission annual research stage {ordinal}: {step['operation']}"
        metadata = _mission_annual_common_metadata(admission, step, work_ref, prior_ref)
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
            budget = {
                **_REGISTERED_ANNUAL_REPORT_BUDGET,
                "step_max_attempts": step["max_attempts"],
            }
        requested_capabilities = list(step["requested_capabilities"])
        if model_key is not None:
            requested_capabilities.append(
                qualitative_router_capability(requested_capabilities[0])
            )
        works.append(WorkOrder.from_dict({
            "schema_version": SCHEMA_VERSION, "id": work_ref,
            "created_at": admission["created_at"], "updated_at": admission["created_at"],
            "question": question,
            "requested_capabilities": requested_capabilities,
            "runtime_profile_ref": step["runtime_profile_ref"],
            "budget": budget,
            "idempotency_key": f"mission-annual-research-work:{admission['id']}:{ordinal}",
            "declared_side_effects": list(step["declared_side_effects"]),
            "status": "ready",
            "input_refs": [admission["id"], admission["repair_target_ref"], step["id"], *([prior_ref] if prior_ref else [])],
            "metadata": metadata,
        }).to_dict())
    return works


def _derive_mission_annual_work(
    admission: Mapping[str, Any], scheduler: Scheduler,
    blueprints: Sequence[Mapping[str, Any]], index: int,
) -> dict[str, Any]:
    if index == 0:
        return dict(blueprints[0])
    upstream = scheduler.work_order_authority(blueprints[index - 1]["id"])
    formal = scheduler.formal_result(blueprints[index - 1]["id"])
    if upstream is None or formal is None or formal["terminal_state"] != "succeeded":
        raise MissionAnnualResearchExecutorError("child requires exact succeeded upstream")
    context = {
        "id": _ref("mission-annual-research-run", {
            "admission_ref": admission["id"], "admission_hash": admission["content_hash"],
        }),
        "execution_scope": {"parameters": admission["request"]},
    }
    return _resolve_qualitative_child_work_order(
        context, blueprints[index], upstream["work_order"], formal,
        question=admission["planner_inquiry"]["question"],
    )


def validate_mission_annual_work_authority(
    authority: MissionAnnualResearchAuthority, scheduler: Scheduler,
    work: WorkOrder | Mapping[str, Any],
) -> dict[str, Any]:
    """Rebuild and compare the complete immutable WorkOrder from Core authority."""

    wire = work.to_dict() if isinstance(work, WorkOrder) else WorkOrder.from_dict(work).to_dict()
    metadata = wire["metadata"]
    if metadata.get("authority_kind") != AUTHORITY_KIND:
        raise MissionAnnualResearchExecutorError("WorkOrder lacks mission annual authority")
    admission = authority.resolve_for_execution(
        metadata.get("mission_annual_research_admission_ref")
    )
    blueprints = _mission_annual_blueprints(admission)
    indexes = [
        index for index, blueprint in enumerate(blueprints)
        if blueprint["id"] == wire["id"]
    ]
    if len(indexes) != 1 or indexes[0] not in (1, 2):
        raise MissionAnnualResearchExecutorError("WorkOrder is not an admitted mission model stage")
    expected = _derive_mission_annual_work(admission, scheduler, blueprints, indexes[0])
    stored = scheduler.work_order_authority(wire["id"])
    if (
        canonical_json(wire) != canonical_json(expected)
        or stored is None
        or stored["work_order_hash"] != content_hash(expected)
        or canonical_json(stored["work_order"]) != canonical_json(expected)
    ):
        raise MissionAnnualResearchExecutorError("WorkOrder drifted from exact mission derivation")
    return admission


class MissionAnnualResearchExecutor:
    """Crash-safe one-node-at-a-time consumer of exact mission admissions."""

    _authorized = authorized_flag()

    def __init__(
        self, *, authority: MissionAnnualResearchAuthority, scheduler: Scheduler,
        registry: RegisteredAnnualReportRegistry,
        draft_worker: RegisteredAnnualReportDraftWorker,
        verifier_worker: RegisteredAnnualReportVerifierWorker,
        staging: CandidateStagingStore, actor_ref: str,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        if not isinstance(authority, MissionAnnualResearchAuthority):
            raise TypeError("authority must be MissionAnnualResearchAuthority")
        if not isinstance(scheduler, Scheduler):
            raise TypeError("scheduler must be Scheduler")
        if not isinstance(registry, RegisteredAnnualReportRegistry):
            raise TypeError("registry must be RegisteredAnnualReportRegistry")
        if not isinstance(draft_worker, RegisteredAnnualReportDraftWorker):
            raise TypeError("draft_worker has the wrong type")
        if not isinstance(verifier_worker, RegisteredAnnualReportVerifierWorker):
            raise TypeError("verifier_worker has the wrong type")
        if not isinstance(staging, CandidateStagingStore):
            raise TypeError("staging must be CandidateStagingStore")
        if any(item.connection is not authority.connection for item in (scheduler, registry)):
            raise TypeError("authority, registry and Scheduler must share Core authority")
        if draft_worker.scheduler is not scheduler or verifier_worker.scheduler is not scheduler:
            raise TypeError("annual model workers must share Scheduler")
        if (
            draft_worker.mission_annual_research_authority is not authority
            or verifier_worker.mission_annual_research_authority is not authority
        ):
            raise TypeError("annual model workers must share mission research authority")
        if not isinstance(actor_ref, str) or not actor_ref:
            raise TypeError("actor_ref must be non-empty text")
        self.authority = authority
        self.connection = authority.connection
        self.scheduler = scheduler
        self.registry = registry
        self.draft_worker = draft_worker
        self.verifier_worker = verifier_worker
        self.staging = staging
        self.actor_ref = actor_ref
        self.clock = clock
        self._authorization_flag = authorization_flag(
            self.connection, "dalton_mission_annual_research_executor_authorized"
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("mission annual research executor operation cannot be nested")
        self._authorized = True
        try:
            with self.authority.store._transaction() as cursor:
                yield cursor
        finally:
            self._authorized = False

    @staticmethod
    def _steps(admission: Mapping[str, Any]) -> list[dict[str, Any]]:
        return _mission_annual_steps(admission)

    @staticmethod
    def _common_metadata(
        admission: Mapping[str, Any], step: Mapping[str, Any],
        work_ref: str, upstream_ref: str | None,
    ) -> dict[str, Any]:
        return _mission_annual_common_metadata(admission, step, work_ref, upstream_ref)

    def _blueprints(self, admission: Mapping[str, Any]) -> list[dict[str, Any]]:
        return _mission_annual_blueprints(admission)

    def _derive_work(
        self, admission: Mapping[str, Any], blueprints: Sequence[Mapping[str, Any]],
        index: int,
    ) -> dict[str, Any]:
        return _derive_mission_annual_work(
            admission, self.scheduler, blueprints, index
        )

    @staticmethod
    def _run_id(admission: Mapping[str, Any]) -> str:
        return _ref("mission-annual-research-run", {
            "admission_ref": admission["id"], "admission_hash": admission["content_hash"],
        })

    def _start(self, admission: Mapping[str, Any], root: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._run_id(admission)
        start_id = _ref("mission-annual-research-start", run_id)
        row = self.connection.execute(
            "SELECT * FROM mission_annual_research_starts WHERE start_id=?", (start_id,)
        ).fetchone()
        body = {
            "schema_version": SCHEMA_VERSION, "id": start_id, "run_id": run_id,
            "admission_ref": admission["id"], "admission_hash": admission["content_hash"],
            "root_work_order_ref": root["id"], "root_work_order_hash": content_hash(root),
            "created_at": admission["created_at"],
        }
        body["content_hash"] = content_hash(body)
        if row is None:
            with self._transaction() as cursor:
                cursor.execute(
                    "INSERT INTO mission_annual_research_starts VALUES(?,?,?,?,?,?,?,?,?)",
                    (start_id, admission["id"], admission["content_hash"], run_id,
                     root["id"], content_hash(root), canonical_json(body),
                     body["content_hash"], admission["created_at"]),
                )
            return body
        try:
            stored = json.loads(row["record_json"])
        except (TypeError, ValueError, RecursionError) as exc:
            raise MissionAnnualResearchExecutorError("stored annual research start is invalid") from exc
        if canonical_json(stored) != canonical_json(body) or row["content_hash"] != body["content_hash"]:
            raise MissionAnnualResearchExecutorError("stored annual research start drifted")
        return body

    def _enqueue(self, work: Mapping[str, Any]) -> dict[str, Any]:
        result = self.scheduler.enqueue(work)
        if result["status"] not in {"fresh", "duplicate"}:
            raise MissionAnnualResearchExecutorError("Scheduler WorkOrder admission conflicted")
        authority = self.scheduler.work_order_authority(work["id"])
        if authority is None or authority["work_order_hash"] != content_hash(work):
            raise MissionAnnualResearchExecutorError("Scheduler WorkOrder authority drifted")
        return {"status": "admitted", "work_order_ref": work["id"], "stage": work["metadata"]["stage"]}

    def _complete_retrieval(self, admission: Mapping[str, Any], work: Mapping[str, Any]) -> dict[str, Any]:
        try:
            proof = self.registry.search(admission["request"])
        except RegisteredAnnualReportError as exc:
            raise MissionAnnualResearchExecutorError(str(exc)) from exc
        claim = self.scheduler.claim(self.actor_ref, work_order_id=work["id"])
        if claim is None:
            return {"status": "pending", "work_order_ref": work["id"]}
        wire = ResultEnvelope(
            schema_version=SCHEMA_VERSION,
            id=_ref("result-envelope:mission-annual-retrieval", {"run": self._run_id(admission), "proof": proof["content_hash"]}),
            created_at=_wire_time(self.clock()), work_order_ref=work["id"],
            invocation_ref=_ref("execution:mission-annual-retrieval", self._run_id(admission)),
            status="succeeded", outputs=proof, actual_side_effects=(), usage_refs=(),
            artifact_refs=(), error=None,
            metadata={"operation": admission["operation"], "authority_ref": admission["id"]},
        ).to_dict()
        completed = self.scheduler.complete(
            work["id"], claim["attempt"]["attempt_number"], self.actor_ref,
            claim["lease_token"], wire,
            idempotency_key=f"mission-annual-research:{admission['id']}:1:complete",
            result_envelope_hash=content_hash(wire),
        )
        if completed["status"] != "fresh":
            raise MissionAnnualResearchExecutorError("retrieval completion did not converge")
        return {"status": "succeeded", "work_order_ref": work["id"]}

    def _complete_staging(
        self, admission: Mapping[str, Any], works: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        work, upstream = works[3], works[2]
        formal = self.scheduler.formal_result(upstream["id"])
        if formal is None or formal["terminal_state"] != "succeeded":
            raise MissionAnnualResearchExecutorError("staging requires succeeded verifier")
        claim = self.scheduler.claim(self.actor_ref, work_order_id=work["id"])
        if claim is None:
            return {"status": "pending", "work_order_ref": work["id"]}
        try:
            verifier = validate_model_proof(
                formal["result_envelope"]["outputs"],
                stage="independent_qualitative_verifier", work=upstream,
            )
            staged = stage_annual_report_candidate(
                self.staging, question_ref=admission["repair_target_ref"],
                question=admission["planner_inquiry"]["question"],
                proof=work["metadata"]["retrieval_proof"],
                draft_proof=work["metadata"]["draft_proof"],
                verifier_proof=verifier, draft_work=works[1], verifier_work=upstream,
                actor_ref=admission["actor_ref"], created_at=work["created_at"],
                idempotency_key=f"mission-annual-research-candidate:{admission['id']}",
                source_authority=self.registry.candidate_source_authority(
                    admission["request"]
                ),
                mission_admission=admission,
            )
        except (AnnualReportQualitativeError, ResearchVerificationError) as exc:
            raise MissionAnnualResearchExecutorError(str(exc)) from exc
        records = {
            "authority_ref": admission["id"], "repair_target_ref": admission["repair_target_ref"],
            "repair_target_hash": admission["repair_target_hash"],
            # The retrieval/model workflow has finished, but the Dossier gap
            # remains open until a separate canonical Evidence/Claim
            # admission consumes this staged candidate.
            "research_status": "candidate_staged",
            "candidate_evidence_ref": staged["evidence"]["id"],
            "candidate_evidence_hash": staged["evidence"]["content_hash"],
            "candidate_claim_ref": staged["claim"]["id"],
            "candidate_claim_hash": staged["claim"]["content_hash"],
        }
        self._validate_staged_records(admission, works, records)
        envelope = ResultEnvelope(
            schema_version=SCHEMA_VERSION,
            id=_ref("result-envelope:mission-annual-staging", records),
            created_at=_wire_time(self.clock()), work_order_ref=work["id"],
            invocation_ref=_ref("execution:mission-annual-staging", self._run_id(admission)),
            status="succeeded", outputs=records, actual_side_effects=(), usage_refs=(),
            artifact_refs=(), error=None, metadata={"authority_ref": admission["id"]},
        ).to_dict()
        completed = self.scheduler.complete(
            work["id"], claim["attempt"]["attempt_number"], self.actor_ref,
            claim["lease_token"], envelope,
            idempotency_key=f"mission-annual-research:{admission['id']}:4:complete",
            result_envelope_hash=content_hash(envelope),
        )
        if completed["status"] != "fresh":
            raise MissionAnnualResearchExecutorError("staging completion did not converge")
        outcome = self._store_outcome(admission, works, records)
        return {
            "status": "complete", **records,
            "outcome_ref": outcome["id"], "outcome_hash": outcome["content_hash"],
        }

    def _validate_staged_records(
        self, admission: Mapping[str, Any], works: Sequence[Mapping[str, Any]],
        records: Mapping[str, Any],
    ) -> dict[str, Any]:
        expected_keys = {
            "authority_ref", "repair_target_ref", "repair_target_hash",
            "research_status", "candidate_evidence_ref", "candidate_evidence_hash",
            "candidate_claim_ref", "candidate_claim_hash",
        }
        if not isinstance(records, Mapping) or set(records) != expected_keys:
            raise MissionAnnualResearchExecutorError("staging result has an open or invalid shape")
        if (
            records["authority_ref"] != admission["id"]
            or records["repair_target_ref"] != admission["repair_target_ref"]
            or records["repair_target_hash"] != admission["repair_target_hash"]
            or records["research_status"] != "candidate_staged"
        ):
            raise MissionAnnualResearchExecutorError("staging result drifted from admission")
        try:
            bundle = self.staging.exact_candidate_bundle(
                evidence_ref=records["candidate_evidence_ref"],
                claim_ref=records["candidate_claim_ref"],
                idempotency_key=f"mission-annual-research-candidate:{admission['id']}",
            )
            formal = self.scheduler.formal_result(works[2]["id"])
            if formal is None or formal["terminal_state"] != "succeeded":
                raise MissionAnnualResearchExecutorError("staged candidate lost verifier authority")
            expected = build_annual_report_candidate_bundle(
                question_ref=admission["repair_target_ref"],
                question=admission["planner_inquiry"]["question"],
                proof=works[3]["metadata"]["retrieval_proof"],
                draft_proof=works[3]["metadata"]["draft_proof"],
                verifier_proof=validate_model_proof(
                    formal["result_envelope"]["outputs"],
                    stage="independent_qualitative_verifier", work=works[2],
                ),
                draft_work=works[1], verifier_work=works[2],
                actor_ref=admission["actor_ref"], created_at=works[3]["created_at"],
                source_authority=self.registry.candidate_source_authority(
                    admission["request"]
                ),
                mission_admission=admission,
            )
        except (AnnualReportQualitativeError, ResearchVerificationError) as exc:
            raise MissionAnnualResearchExecutorError(str(exc)) from exc
        for key in ("material", "source_verification", "evidence", "claim"):
            if canonical_json(bundle[key]) != canonical_json(expected[key]):
                raise MissionAnnualResearchExecutorError(
                    f"staged candidate {key} drifted from exact annual authority"
                )
        if (
            records["candidate_evidence_hash"] != bundle["evidence"]["content_hash"]
            or records["candidate_claim_hash"] != bundle["claim"]["content_hash"]
        ):
            raise MissionAnnualResearchExecutorError("staging result hashes drifted")
        return dict(records)

    def _stage_formal_owned_by_executor(
        self, work: Mapping[str, Any], formal: Mapping[str, Any],
    ) -> None:
        row = self.connection.execute(
            "SELECT l.owner_ref,e.result_envelope_id,e.result_envelope_hash "
            "FROM scheduler_attempt_events e "
            "JOIN scheduler_leases l ON l.lease_revision_id=e.lease_revision_id "
            "WHERE e.work_order_id=? AND e.attempt_number=? AND e.state='succeeded' "
            "ORDER BY e.event_seq DESC LIMIT 1",
            (work["id"], formal["attempt_number"]),
        ).fetchone()
        if (
            row is None or row["owner_ref"] != self.actor_ref
            or row["result_envelope_id"] != formal["result_envelope_id"]
            or row["result_envelope_hash"] != formal["result_envelope_hash"]
        ):
            raise MissionAnnualResearchExecutorError(
                "staging formal result was not completed by the mission executor"
            )

    def _store_outcome(
        self, admission: Mapping[str, Any], works: Sequence[Mapping[str, Any]],
        records: Mapping[str, Any],
    ) -> dict[str, Any]:
        records = self._validate_staged_records(admission, works, records)
        start_ref = _ref("mission-annual-research-start", self._run_id(admission))
        outcome_id = _ref("mission-annual-research-outcome", {"start_ref": start_ref, **dict(records)})
        body = {
            "schema_version": SCHEMA_VERSION, "id": outcome_id,
            "start_ref": start_ref, "admission_ref": admission["id"],
            **dict(records), "created_at": admission["created_at"],
        }
        body["content_hash"] = content_hash(body)
        row = self.connection.execute(
            "SELECT record_json,content_hash FROM mission_annual_research_outcomes WHERE outcome_id=?",
            (outcome_id,),
        ).fetchone()
        if row is None:
            with self._transaction() as cursor:
                cursor.execute(
                    "INSERT INTO mission_annual_research_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (outcome_id, start_ref, admission["id"], admission["repair_target_ref"],
                     admission["repair_target_hash"], records["candidate_evidence_ref"],
                     records["candidate_evidence_hash"], records["candidate_claim_ref"],
                     records["candidate_claim_hash"], canonical_json(body), body["content_hash"],
                     admission["created_at"]),
                )
        elif row["record_json"] != canonical_json(body) or row["content_hash"] != body["content_hash"]:
            raise MissionAnnualResearchExecutorError("stored annual research outcome drifted")
        return body

    def _promote(
        self, admission: Mapping[str, Any], records: Mapping[str, Any],
        outcome: Mapping[str, Any],
    ) -> dict[str, Any]:
        existing = self.connection.execute(
            "SELECT * FROM mission_annual_research_promotions WHERE admission_ref=?",
            (admission["id"],),
        ).fetchone()
        if existing is not None:
            try:
                promotion = json.loads(existing["record_json"])
            except (TypeError, ValueError, RecursionError) as exc:
                raise MissionAnnualResearchExecutorError(
                    "stored annual research promotion is invalid"
                ) from exc
            base = dict(promotion) if isinstance(promotion, Mapping) else {}
            asserted = base.pop("content_hash", None)
            columns = {
                "id": existing["promotion_id"],
                "outcome_ref": existing["outcome_ref"],
                "admission_ref": existing["admission_ref"],
                "evidence_version_ref": existing["evidence_version_ref"],
                "evidence_version_hash": existing["evidence_version_hash"],
                "claim_version_ref": existing["claim_version_ref"],
                "claim_version_hash": existing["claim_version_hash"],
                "policy_authorization_ref": existing["policy_authorization_ref"],
                "policy_authorization_hash": existing["policy_authorization_hash"],
                "created_at": existing["created_at"],
            }
            evidence = self.connection.execute(
                "SELECT content_hash FROM evidence_versions WHERE evidence_version_id=?",
                (existing["evidence_version_ref"],),
            ).fetchone()
            claim = self.authority.store.get_claim(existing["claim_version_ref"])
            decision_row = self.connection.execute(
                "SELECT decision_json FROM reviewed_candidate_commits "
                "WHERE review_decision_ref=?",
                (existing["policy_authorization_ref"],),
            ).fetchone()
            try:
                from .research_auto_commit import validate_policy_commit_decision

                decision = validate_policy_commit_decision(
                    json.loads(decision_row["decision_json"])
                    if decision_row is not None else {}
                )
            except (TypeError, ValueError, RecursionError) as exc:
                raise MissionAnnualResearchExecutorError(
                    "stored annual research promotion authorization is invalid"
                ) from exc
            if (
                not isinstance(promotion, Mapping)
                or canonical_json(promotion) != existing["record_json"]
                or asserted != existing["content_hash"]
                or asserted != content_hash(base)
                or any(promotion.get(key) != value for key, value in columns.items())
                or promotion.get("research_status") != "canonical_claim_promoted"
                or promotion.get("outcome_ref") != outcome["id"]
                or evidence is None
                or evidence["content_hash"] != promotion.get("evidence_version_hash")
                or claim is None
                or claim["content_hash"] != promotion.get("claim_version_hash")
                or decision["content_hash"]
                != promotion.get("policy_authorization_hash")
                or canonical_json(decision) != decision_row["decision_json"]
            ):
                raise MissionAnnualResearchExecutorError(
                    "stored annual research promotion drifted"
                )
            return {
                "status": "complete", **dict(records),
                "research_status": "canonical_claim_promoted",
                "outcome_ref": outcome["id"], "outcome_hash": outcome["content_hash"],
                "promotion_ref": promotion["id"],
                "promotion_hash": promotion["content_hash"],
                "evidence_version_ref": promotion["evidence_version_ref"],
                "claim_version_ref": promotion["claim_version_ref"],
            }
        from .research_auto_commit import policy_lists_document_rule

        if not policy_lists_document_rule(self.authority.store.active_policy()):
            return {
                "status": "complete", **dict(records),
                "outcome_ref": outcome["id"], "outcome_hash": outcome["content_hash"],
            }
        # Re-resolve immediately before the formal Ledger boundary. The
        # policy evaluator independently re-reads the immutable admission,
        # model invocations, Scheduler WorkOrders and Core source authority.
        self.authority.resolve_for_execution(admission["id"])
        staged = self.staging.exact_candidate_bundle(
            evidence_ref=records["candidate_evidence_ref"],
            claim_ref=records["candidate_claim_ref"],
            idempotency_key=f"mission-annual-research-candidate:{admission['id']}",
        )
        promoted = self.authority.store.commit_policy_candidate(
            evidence=staged["evidence"], claim=staged["claim"],
            material=staged["material"],
            source_verification=staged["source_verification"],
            numeric_spec=None, numeric_verification=None,
            idempotency_key=f"policy-ledger:mission-annual-research:{admission['id']}",
        )
        evidence = self.authority.store.connection.execute(
            "SELECT content_hash FROM evidence_versions WHERE evidence_version_id=?",
            (promoted["evidence_version_ref"],),
        ).fetchone()
        claim = self.authority.store.get_claim(promoted["claim_version_ref"])
        authorization = promoted["authorization"]
        if evidence is None or claim is None:
            raise MissionAnnualResearchExecutorError(
                "policy promotion did not persist canonical Evidence/Claim"
            )
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": _ref("mission-annual-research-promotion", {
                "outcome_ref": outcome["id"],
                "claim_version_ref": promoted["claim_version_ref"],
                "authorization_hash": authorization["content_hash"],
            }),
            "outcome_ref": outcome["id"], "admission_ref": admission["id"],
            "research_status": "canonical_claim_promoted",
            "evidence_version_ref": promoted["evidence_version_ref"],
            "evidence_version_hash": evidence["content_hash"],
            "claim_version_ref": promoted["claim_version_ref"],
            "claim_version_hash": claim["content_hash"],
            "policy_authorization_ref": authorization["id"],
            "policy_authorization_hash": authorization["content_hash"],
            "created_at": authorization["created_at"],
        }
        record["content_hash"] = content_hash(record)
        row = self.connection.execute(
            "SELECT record_json,content_hash FROM mission_annual_research_promotions "
            "WHERE promotion_id=?", (record["id"],),
        ).fetchone()
        if row is None:
            with self._transaction() as cursor:
                cursor.execute(
                    "INSERT INTO mission_annual_research_promotions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (record["id"], outcome["id"], admission["id"],
                     record["evidence_version_ref"], record["evidence_version_hash"],
                     record["claim_version_ref"], record["claim_version_hash"],
                     record["policy_authorization_ref"], record["policy_authorization_hash"],
                     canonical_json(record), record["content_hash"], record["created_at"]),
                )
        elif row["record_json"] != canonical_json(record) or row["content_hash"] != record["content_hash"]:
            raise MissionAnnualResearchExecutorError("stored annual research promotion drifted")
        return {
            "status": "complete", **dict(records),
            "research_status": "canonical_claim_promoted",
            "outcome_ref": outcome["id"], "outcome_hash": outcome["content_hash"],
            "promotion_ref": record["id"], "promotion_hash": record["content_hash"],
            "evidence_version_ref": record["evidence_version_ref"],
            "claim_version_ref": record["claim_version_ref"],
        }

    def run_once(self, admission_ref: str) -> dict[str, Any]:
        try:
            admission = self.authority.resolve_for_execution(admission_ref)
        except MissionAnnualResearchError as exc:
            raise MissionAnnualResearchExecutorError(str(exc)) from exc
        blueprints = self._blueprints(admission)
        self._start(admission, blueprints[0])
        for index in range(4):
            work = self._derive_work(admission, blueprints, index)
            stored = self.scheduler.work_order_authority(work["id"])
            if stored is None:
                return self._enqueue(work)
            if stored["work_order_hash"] != content_hash(work):
                raise MissionAnnualResearchExecutorError("stored WorkOrder drifted from admission")
            formal = self.scheduler.formal_result(work["id"])
            if formal is None:
                if index == 0:
                    return self._complete_retrieval(admission, work)
                if index in (1, 2):
                    validate_mission_annual_work_authority(
                        self.authority, self.scheduler, work
                    )
                    worker = self.draft_worker if index == 1 else self.verifier_worker
                    try:
                        result = worker.run_once(work)
                    except AnnualReportQualitativeError as exc:
                        raise MissionAnnualResearchExecutorError(str(exc)) from exc
                    return {**result, "work_order_ref": work["id"], "stage": work["metadata"]["stage"]}
                return self._complete_staging(admission, [self._derive_work(admission, blueprints, i) for i in range(4)])
            if formal["terminal_state"] != "succeeded":
                return {"status": "blocked", "work_order_ref": work["id"], "stage": work["metadata"]["stage"]}
            if index == 0:
                proof = validate_retrieval_proof(formal["result_envelope"]["outputs"], expected_request=admission["request"])
                if canonical_json(proof) != canonical_json(self.registry.search(admission["request"])):
                    raise MissionAnnualResearchExecutorError("stored retrieval proof drifted")
            elif index in (1, 2):
                validate_mission_annual_work_authority(
                    self.authority, self.scheduler, work
                )
                validate_model_proof(formal["result_envelope"]["outputs"], stage=work["metadata"]["stage"], work=work)
            else:
                records = formal["result_envelope"]["outputs"]
                self._stage_formal_owned_by_executor(work, formal)
                resolved_works = [
                    self._derive_work(admission, blueprints, i) for i in range(4)
                ]
                outcome = self._store_outcome(admission, resolved_works, records)
                return self._promote(admission, records, outcome)
        raise MissionAnnualResearchExecutorError("annual research run has invalid shape")


__all__ = [
    "AUTHORITY_KIND", "MissionAnnualResearchExecutor",
    "MissionAnnualResearchExecutorError", "validate_mission_annual_work_authority",
]
