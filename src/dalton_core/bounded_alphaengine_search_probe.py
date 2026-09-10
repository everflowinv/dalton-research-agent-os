"""Execute one inquiry-bound search through the approved discovery launcher."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Mapping

from .store import content_hash

PROBE_OPERATION = "alphaengine_discovery_refresh"
PROBE_PERMISSION_SCOPE = "alphaengine_read"
WORKER_REF = "worker:bounded-probe-alphaengine-search"


class BoundedAlphaEngineSearchProbeError(RuntimeError):
    pass


class BoundedAlphaEngineSearchProbePending(RuntimeError):
    """The authorized child still owns this WorkOrder; retry only to poll it."""


def execute_alphaengine_search_probe(work_order: Mapping[str, Any], *, client: Any,
                                     timeout_seconds: float = 120,
                                     poll_seconds: float = 0.1) -> dict[str, Any]:
    metadata = work_order.get("metadata") or {}
    if metadata.get("operation") != PROBE_OPERATION:
        raise BoundedAlphaEngineSearchProbeError("probe operation is not AlphaEngine search")
    if metadata.get("permission_scope") != PROBE_PERMISSION_SCOPE:
        raise BoundedAlphaEngineSearchProbeError("probe is outside AlphaEngine read scope")
    parameters = metadata.get("parameters")
    if not isinstance(parameters, Mapping) or set(parameters) != {"source_ref", "spec_ref", "inquiry_hash", "company_ref", "as_of", "company_scope_refs", "discovery_plan_ref", "discovery_plan_hash"}:
        raise BoundedAlphaEngineSearchProbeError("search probe parameters have an invalid closed shape")
    if parameters["source_ref"] != "source:alphaengine" or parameters["company_ref"] not in parameters["company_scope_refs"] or parameters["spec_ref"] not in {
        "earnings-call-transcripts", "sell-side-reports"}:
        raise BoundedAlphaEngineSearchProbeError("search probe is outside the approved discovery specs")
    for field in ("mission_version_ref", "mission_version_hash"):
        if not isinstance(metadata.get(field), str) or not metadata[field]:
            raise BoundedAlphaEngineSearchProbeError(f"search probe has no exact {field}")
    ticket = client.call("start_bounded_source_discovery", {"work_order": dict(work_order)})
    deadline = time.monotonic() + float(timeout_seconds)
    record = ticket
    while record.get("status") in {"running", "duplicate"} and time.monotonic() < deadline:
        if poll_seconds: time.sleep(poll_seconds)
        record = client.call("mission_source_discovery_status", {"ticket_ref": ticket["id"]})
    if record.get("status") in {"running", "duplicate"}:
        raise BoundedAlphaEngineSearchProbePending(
            "authorized AlphaEngine discovery child is still running")
    summary = record.get("summary") or {}
    search = summary.get("search") or {}
    documents = search.get("document_refs") or []
    identity = {"work_order_ref": work_order.get("id"), "ticket_ref": ticket.get("id"),
                "query_hash": summary.get("query_hash")}
    ref_fields = ("connector_invocation_ref", "source_envelope_ref")
    hash_fields = ("connector_invocation_hash", "source_envelope_hash")
    refs_valid = all(isinstance(search.get(field), str) and search[field].strip() for field in ref_fields)
    hashes_valid = all(isinstance(search.get(field), str) and len(search[field]) == 64
                       and all(c in "0123456789abcdef" for c in search[field]) for field in hash_fields)
    succeeded = (record.get("status") == "succeeded"
                 and summary.get("status") == "succeeded"
                 and search.get("outcome") == "succeeded"
                 and refs_valid and hashes_valid
                 and summary.get("query_hash") == ticket.get("query_hash"))
    return {
        "schema_version": "0.1", "id": "result:bounded-alphaengine-search:" + content_hash(identity)[:32],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "work_order_ref": work_order.get("id"),
        "invocation_ref": search.get("connector_invocation_ref"),
        "status": "succeeded" if succeeded else "failed",
        "outputs": {"matches": [{"source_location": f"alphaengine:{ref}"} for ref in documents]},
        "actual_side_effects": ["read:alphaengine"], "usage_refs": [],
        "artifact_refs": [ref for ref in (search.get("raw_artifact_version_ref"),) if ref],
        "error": None if succeeded else {"code": "SOURCE_UNAVAILABLE",
            "message": str(summary.get("failure_reason") or f"search ended {record.get('status')}")},
        "metadata": {"probe": "alphaengine-search-library", "query_hash": summary.get("query_hash"),
                     "spec_ref": parameters["spec_ref"], "inquiry_hash": parameters["inquiry_hash"],
                     "provider_calls": int(summary.get("provider_calls") or 0)},
    }


__all__ = ["BoundedAlphaEngineSearchProbeError", "BoundedAlphaEngineSearchProbePending", "PROBE_OPERATION", "PROBE_PERMISSION_SCOPE",
           "WORKER_REF", "execute_alphaengine_search_probe"]
