"""Bridge one formal routed model candidate back into the local probe."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import ResultEnvelope, WorkOrder
from .scheduler import Scheduler
from .store import canonical_json, content_hash
from .transcript_polish import (
    TranscriptPolishAuthority,
    TranscriptPolishWorker,
)
from .transcript_polish_model import (
    TRANSCRIPT_POLISH_ROUTED_WORKER_REF,
    TranscriptPolishModelConflict,
    TranscriptPolishModelRejected,
    build_transcript_polish_model_work_order,
    candidate_from_formal_model_result,
)


SCHEMA_VERSION = "0.1"


class RoutedTranscriptPolishCoordinator:
    """Prepare a model call, then close the exact local polish WorkOrder."""

    def __init__(
        self,
        *,
        authority: TranscriptPolishAuthority,
        scheduler: Scheduler,
    ) -> None:
        if authority.connection is not scheduler.connection:
            raise TypeError(
                "transcript authority and Scheduler must share one Core connection"
            )
        self.authority = authority
        self.local_worker = TranscriptPolishWorker(authority)
        self.scheduler = scheduler

    def _probe(
        self, probe_work_order: WorkOrder | Mapping[str, Any]
    ) -> tuple[WorkOrder, dict[str, Any]]:
        probe, parameters = self.local_worker.admitted_parameters(
            probe_work_order
        )
        status = self.scheduler.status(probe.id)
        if status["work_order_hash"] != content_hash(probe.to_dict()):
            raise TranscriptPolishModelConflict(
                "Scheduler retains a different transcript probe WorkOrder"
            )
        return probe, parameters

    def prepare(
        self,
        probe_work_order: WorkOrder | Mapping[str, Any],
        **work_budget: Any,
    ) -> dict[str, Any]:
        probe, parameters = self._probe(probe_work_order)
        if self.scheduler.formal_result(probe.id) is not None:
            return {
                "status": "probe_terminal",
                "formal_result": self.scheduler.formal_result(probe.id),
            }
        source = self.authority.model_source_context(
            source_manifest_ref=parameters["source_manifest_ref"],
            source_manifest_hash=parameters["source_manifest_hash"],
            source_content_hash=parameters["source_content_hash"],
            correction_set_version_ref=parameters[
                "correction_set_version_ref"
            ],
            correction_set_version_hash=parameters[
                "correction_set_version_hash"
            ],
        )
        work = build_transcript_polish_model_work_order(
            probe, source, **work_budget
        )
        enqueued = self.scheduler.enqueue(work)
        if enqueued["status"] not in {"fresh", "duplicate"}:
            raise TranscriptPolishModelRejected(
                "transcript model WorkOrder did not converge"
            )
        return {
            "status": "model_work_ready",
            "source_binding": {
                key: value for key, value in source.items()
                if key != "resolved_source_text"
            },
            "work_order": work.to_dict(),
            "enqueue": enqueued,
        }

    def _expected_model_work(
        self,
        probe: WorkOrder,
        parameters: Mapping[str, Any],
        model_work: WorkOrder,
    ) -> WorkOrder:
        source = self.authority.model_source_context(
            source_manifest_ref=parameters["source_manifest_ref"],
            source_manifest_hash=parameters["source_manifest_hash"],
            source_content_hash=parameters["source_content_hash"],
            correction_set_version_ref=parameters[
                "correction_set_version_ref"
            ],
            correction_set_version_hash=parameters[
                "correction_set_version_hash"
            ],
        )
        budget = model_work.budget
        expected = build_transcript_polish_model_work_order(
            probe,
            source,
            max_input_tokens=budget["max_input_tokens"],
            max_output_tokens=budget["max_output_tokens"],
            max_cost_usd=budget["max_cost_usd"],
            max_seconds=budget["max_seconds"],
        )
        if canonical_json(expected.to_dict()) != canonical_json(
            model_work.to_dict()
        ):
            raise TranscriptPolishModelConflict(
                "transcript model WorkOrder drifted from exact source authority"
            )
        return expected

    def advance(
        self,
        probe_work_order: WorkOrder | Mapping[str, Any],
        model_work_order: WorkOrder | Mapping[str, Any],
    ) -> dict[str, Any]:
        probe, parameters = self._probe(probe_work_order)
        existing = self.scheduler.formal_result(probe.id)
        if existing is not None:
            return {
                "status": existing["terminal_state"],
                "formal_result": existing,
                "replayed": True,
            }
        try:
            model_work = (
                model_work_order
                if isinstance(model_work_order, WorkOrder)
                else WorkOrder.from_dict(model_work_order)
            )
        except Exception as exc:
            raise TranscriptPolishModelConflict(
                "transcript model WorkOrder is invalid"
            ) from exc
        self._expected_model_work(probe, parameters, model_work)
        candidate, model_result = candidate_from_formal_model_result(
            self.scheduler, model_work
        )
        lease = self.scheduler.claim(
            TRANSCRIPT_POLISH_ROUTED_WORKER_REF,
            work_order_id=probe.id,
        )
        if lease is None:
            return {"status": "waiting", "work_order_ref": probe.id}
        if (
            canonical_json(lease["work_order"])
            != canonical_json(probe.to_dict())
            or lease["work_order_hash"] != content_hash(probe.to_dict())
        ):
            raise TranscriptPolishModelConflict(
                "transcript probe lease drifted"
            )
        outputs = self.local_worker.execute(
            probe.to_dict(), canonical_json(candidate)
        )
        match = outputs["matches"][0]
        identity = {
            "probe_work_order_ref": probe.id,
            "model_work_order_ref": model_work.id,
            "model_result_ref": model_result["id"],
            "polished_artifact_version_ref": match[
                "polished_artifact_version_ref"
            ],
        }
        digest = content_hash(identity)[:32]
        result = ResultEnvelope(
            schema_version=SCHEMA_VERSION,
            id="result:transcript-polish-routed-" + digest,
            created_at=model_result["created_at"],
            work_order_ref=probe.id,
            invocation_ref="invocation:transcript-polish-core-" + digest,
            status="succeeded",
            outputs=outputs,
            actual_side_effects=(),
            usage_refs=tuple(model_result["usage_refs"]),
            artifact_refs=(match["polished_artifact_version_ref"],),
            error=None,
            metadata={
                "model_work_order_ref": model_work.id,
                "model_work_order_hash": content_hash(model_work.to_dict()),
                "model_result_ref": model_result["id"],
                "model_result_hash": content_hash(model_result),
                "model_invocation_ref": model_result["invocation_ref"],
                "route_decision_ref": model_result["metadata"][
                    "route_decision_ref"
                ],
                "profile_version_ref": model_result["metadata"][
                    "profile_version_ref"
                ],
                "candidate_hash": content_hash(candidate),
                "verification": "core:transcript-polish-conservation",
            },
        )
        completion = self.scheduler.complete(
            probe.id,
            lease["attempt"]["attempt_number"],
            TRANSCRIPT_POLISH_ROUTED_WORKER_REF,
            lease["lease_token"],
            result,
            idempotency_key=f"transcript-polish-routed-complete:{probe.id}",
        )
        return {
            "status": "succeeded",
            "result": result.to_dict(),
            "completion": completion,
            "replayed": False,
        }


__all__ = ["RoutedTranscriptPolishCoordinator"]
