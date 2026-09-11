"""Authoritative reader for failed extraction windows eligible for human re-read."""
from __future__ import annotations

from typing import Any

from .contracts import ResultEnvelope
from .store import content_hash


class FailedDocumentWindowReader:
    def __init__(self, scheduler: Any):
        self.scheduler = scheduler

    def read_failed_window(self, *, work_order_ref: str, result_envelope_ref: str,
                           review_id: str, prior_review_hash: str) -> dict[str, str]:
        formal = self.scheduler.formal_result(work_order_ref)
        authority = self.scheduler.work_order_authority(work_order_ref)
        if formal is None or authority is None or formal.get("terminal_state") != "failed":
            raise ValueError("formal failed extraction result is unavailable")
        work = authority["work_order"]
        if authority.get("work_order_hash") != content_hash(work):
            raise ValueError("failed extraction WorkOrder drifted")
        context = (work.get("metadata") or {}).get("context") or {}
        if context.get("review_id") != review_id:
            raise ValueError("failed extraction WorkOrder belongs to another review")
        envelope = formal.get("result_envelope")
        if not isinstance(envelope, dict) or formal.get("result_envelope_hash") != content_hash(envelope):
            raise ValueError("failed extraction ResultEnvelope drifted")
        result = ResultEnvelope.from_dict(envelope)
        if result.id != result_envelope_ref or result.work_order_ref != work_order_ref or result.status != "failed":
            raise ValueError("failed extraction result binding drifted")
        return {"work_order_ref": work_order_ref,
                "work_order_hash": authority["work_order_hash"],
                "result_envelope_ref": result.id,
                "result_envelope_hash": formal["result_envelope_hash"],
                "error_code": str((result.error or {}).get("code") or "MODEL_FAILED")}
