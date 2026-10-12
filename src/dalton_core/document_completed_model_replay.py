"""A single local completion after the historical Figure binding defect.

Successful paid stages are reused. The child is explicitly forbidden to enqueue,
recover, or execute a model stage; this is not another model retry allowance.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from .store import canonical_json, content_hash

CONTRACT = "document-completed-model-postprocessing:0.1"
ERROR = "DirectedNumericError: existing Figure conflicts with exact verified numeric answer"
CHAIN_CONTRACT = "document-completed-model-postprocessing:0.2"
CHAIN_ERROR = "ResearchVerificationConflict: candidate version chain mismatch"
from .directed_numeric_promotion_reconciliation import REPLAY_CONTRACT as PROMOTION_CONTRACT
CONTRACTS = frozenset({CONTRACT,CHAIN_CONTRACT,PROMOTION_CONTRACT})


def candidate(coordinator, admission, ticket_ref):
    from .directed_numeric_research import CONTRACT_REF
    from .scheduler import Scheduler
    from .contracts import ResultEnvelope
    from .mission_document_research_executor import _formal_hash
    if admission.get("numeric_authority", {}).get("contract_ref") != CONTRACT_REF:
        return None
    ticket = coordinator.launcher.status(ticket_ref)
    summary = ticket.get("summary")
    if (ticket.get("status") not in {"failed", "orphaned"} or not isinstance(summary, Mapping)
            or summary.get("error") not in {ERROR,CHAIN_ERROR} or summary.get("status") != "failed"
            or summary.get("admission_ref") != admission["id"]
            or summary.get("admission_hash") != admission["content_hash"]
            or ticket.get("admission_ref") != admission["id"]
            or ticket.get("admission_hash") != admission["content_hash"]
            or summary.get("content_hash") != content_hash({k:v for k,v in summary.items() if k != "content_hash"})):
        return None
    path = coordinator.launcher._ticket_path(ticket_ref).with_name("summary.json")
    if path.is_symlink():
        return None
    raw = path.read_bytes()
    if json.loads(raw) != summary:
        return None
    scheduler = object.__new__(Scheduler)
    scheduler.connection = coordinator.store.connection
    proofs = []
    for ordinal, ref in enumerate(coordinator._effective_work_hints(admission)[:3], 1):
        stored = scheduler.work_order_authority(ref)
        formal = scheduler.formal_result(ref)
        if stored is None or formal is None or formal["terminal_state"] != "succeeded":
            return None
        work = stored["work_order"]
        envelope = ResultEnvelope.from_dict(formal["result_envelope"]).to_dict()
        raw_formal = scheduler.connection.execute(
            "SELECT result_envelope_json FROM scheduler_formal_results WHERE work_order_id=?", (ref,)).fetchone()
        if (formal["content_hash"] != _formal_hash(formal)
                or envelope["id"] != formal["result_envelope_id"]
                or envelope["work_order_ref"] != ref
                or content_hash(envelope) != formal["result_envelope_hash"]
                or canonical_json(envelope) != raw_formal[0]):
            raise ValueError("completed model replay formal authority differs")
        if (work["metadata"].get("mission_document_research_admission_ref") != admission["id"]
                or work["metadata"].get("mission_document_research_admission_hash") != admission["content_hash"]):
            return None
        proofs.append({"ordinal": ordinal, "work_ref": ref,
            "work_hash": stored["work_order_hash"], "formal_hash": _formal_hash(formal)})
    if len(proofs) != 3:
        return None
    body = {"contract_ref": CONTRACT if summary['error']==ERROR else CHAIN_CONTRACT, "admission_ref": admission["id"],
        "admission_hash": admission["content_hash"], "source_ticket_ref": ticket_ref,
        "source_summary_sha256": hashlib.sha256(raw).hexdigest(), "completed_stages": proofs,
        "model_calls_allowed": 0}
    return {**body, "content_hash": content_hash(body)}
