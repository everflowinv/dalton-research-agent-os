"""Authoritative reader for failed extraction windows eligible for human re-read."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping

from .contracts import ResultEnvelope, WorkOrder
from .readonly_sqlite import connect_read_only
from .store import content_hash
from .document_extraction import OUTPUT_SCHEMA, QUOTE_CHARS, TASK_HASH, TASK_REF, WINDOW_CHARS


# Failed windows are immutable and remain eligible after the qualitative prompt
# contract is revised.  Keep the exact hashes of published task contracts here;
# never accept a merely well-formed digest supplied by a caller.
LEGACY_TASK_HASH = content_hash({
    "task": TASK_REF,
    "output": OUTPUT_SCHEMA,
    "window_chars": WINDOW_CHARS,
    "quote_chars": QUOTE_CHARS,
    "authority": "suggestions_only_human_citation_and_accept",
})
PUBLISHED_TASK_HASHES = frozenset({LEGACY_TASK_HASH, TASK_HASH})


class FailedDocumentWindowReader:
    def __init__(self, scheduler: Any):
        self.scheduler = scheduler
        self.prior_review: dict[str, Any] | None = None

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
        metadata = work.get("metadata") or {}
        prior_review = getattr(self, "prior_review", None)
        if (metadata.get("control_plane") != "mission-document-extraction"
                or metadata.get("task_ref") != TASK_REF
                or metadata.get("task_hash") not in PUBLISHED_TASK_HASHES):
            raise ValueError("failed WorkOrder is not qualitative document extraction")
        if (not isinstance(context, dict) or content_hash(
                {key: value for key, value in context.items() if key != "content_hash"}
            ) != context.get("content_hash")):
            raise ValueError("failed extraction context drifted")
        if (context.get("review_id") != review_id or not isinstance(prior_review, dict)
                or any(context.get(context_key) != prior_review.get(review_key) for context_key, review_key in (
                    ("document_ref", "document_ref"), ("company_ref", "company_ref"),
                    ("source_ref", "source_ref"), ("mission_version_ref", "mission_version_ref")))):
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


class _ReadOnlyScheduler:
    def __init__(self, connection): self.connection = connection
    def work_order_authority(self, ref):
        row = self.connection.execute(
            "SELECT work_order_json,work_order_hash FROM scheduler_work_orders WHERE work_order_id=?", (ref,)
        ).fetchone()
        if row is None: return None
        work = WorkOrder.from_dict(json.loads(row["work_order_json"])).to_dict()
        if content_hash(work) != row["work_order_hash"]: raise ValueError("WorkOrder drifted")
        return {"work_order": work, "work_order_hash": row["work_order_hash"]}
    def formal_result(self, ref):
        row = self.connection.execute(
            "SELECT * FROM scheduler_formal_results WHERE work_order_id=?", (ref,)
        ).fetchone()
        if row is None: return None
        out = dict(row); out["result_envelope"] = json.loads(out.pop("result_envelope_json")); return out


def review_reopen_candidate(*, core_db: str | Path, scheduler_db: str | Path,
                            candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Strict no-write review used before the human governance RPC."""
    if (not isinstance(candidate, Mapping) or candidate.get("schema_version") != "0.1"
            or candidate.get("action") != "authorize_supplemental_document_reads"
            or candidate.get("signed_decision") is not None
            or candidate.get("human_signature_required") is not True):
        raise ValueError("supplemental review candidate has invalid closed controls")
    claimed_hash = candidate.get("candidate_hash")
    if content_hash({k: v for k, v in candidate.items() if k != "candidate_hash"}) != claimed_hash:
        raise ValueError("supplemental review candidate hash drifted")
    ready = []
    with closing(connect_read_only(core_db)) as core, closing(connect_read_only(scheduler_db)) as scheduler:
        core.row_factory = scheduler.row_factory = __import__("sqlite3").Row
        reader = FailedDocumentWindowReader(_ReadOnlyScheduler(scheduler))
        for item in candidate.get("items") or ():
            row = core.execute("SELECT * FROM coverage_mission_document_reviews WHERE review_id=?",
                               (item["review_id"],)).fetchone()
            if row is None or row["state"] != "dismissed": raise ValueError("candidate review is not dismissed")
            from .document_read_completion import review_wire
            prior = review_wire(row)
            if content_hash(prior) != item["prior_review_hash"]: raise ValueError("candidate review drifted")
            pointer = core.execute("SELECT 1 FROM coverage_mission_pointer WHERE mission_version_id=?",
                                   (row["mission_version_ref"],)).fetchone()
            if pointer is None: raise ValueError("candidate mission is not current")
            accession = row["document_ref"].removeprefix("sec:filing:")
            filing = core.execute("SELECT form FROM coverage_mission_statement_filings WHERE company_ref=? AND accession=?",
                                  (row["company_ref"], accession)).fetchone()
            if filing is None or filing["form"] != "10-K": raise ValueError("candidate lacks issuer 10-K authority")
            reader.prior_review = prior
            for receipt in item["failed_windows"]:
                if reader.read_failed_window(
                    work_order_ref=receipt["work_order_ref"], result_envelope_ref=receipt["result_envelope_ref"],
                    review_id=row["review_id"], prior_review_hash=item["prior_review_hash"]) != receipt:
                    raise ValueError("candidate failed-window receipt drifted")
            ready.append(item["review_id"])
    return {"status": "reviewed", "candidate_hash": claimed_hash, "ready_review_ids": ready,
            "writes": 0}
