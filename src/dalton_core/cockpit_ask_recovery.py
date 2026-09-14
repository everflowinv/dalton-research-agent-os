"""Exact-CAS publication of a recovered Cockpit Ask job."""
from __future__ import annotations
import json, sqlite3
from collections.abc import Mapping
from hashlib import sha256
from typing import Any, Callable
from .store import canonical_json, content_hash

class AskRecoveryError(ValueError): pass

def _job_wire(row: Mapping[str, Any]) -> dict[str, Any]:
    return {k: row[k] for k in ("job_id","kind","login","status","request_json",
                                 "result_json","error","created_at","updated_at")}

def failed_job_hash(row: Mapping[str, Any]) -> str:
    return content_hash(_job_wire(row))

def recover_failed_ask_job(connection: sqlite3.Connection, *, job_id: str,
        expected_failed_hash: str, result: Mapping[str, Any],
        recovery_proof: Mapping[str, Any], recovered_at: str) -> dict[str, Any]:
    """Publish one fully reviewed result into the same failed job, under CAS."""
    if not isinstance(result, Mapping) or not isinstance(recovery_proof, Mapping):
        raise AskRecoveryError("recovery result or proof is invalid")
    if set(recovery_proof) != {"schema_version","job_id","failed_job_hash",
            "original_source_commit","successor_source_commit","language_artifact_ref",
            "language_artifact_sha256","producer_result_envelope_ref",
            "checker_stage_sha256","brain_result_envelope_ref","brain_raw_sha256",
            "brain_fixed_sha256","brain_suffix"}:
        raise AskRecoveryError("recovery proof has an invalid shape")
    if (recovery_proof.get("schema_version") != "cockpit-ask-language-recovery:0.1"
            or recovery_proof.get("job_id") != job_id
            or recovery_proof.get("failed_job_hash") != expected_failed_hash
            or recovery_proof.get("brain_suffix") != "]}"):
        raise AskRecoveryError("recovery proof binding is invalid")
    if result.get("language_review",{}).get("status") != "ready_for_publication":
        raise AskRecoveryError("recovered answer did not pass language publication review")
    if not isinstance(result.get("answer"),str) or not isinstance(result.get("citations"),list):
        raise AskRecoveryError("recovered answer shape is invalid")
    connection.row_factory=sqlite3.Row
    connection.execute("BEGIN IMMEDIATE")
    try:
        row=connection.execute("SELECT * FROM cockpit_jobs WHERE job_id=?",(job_id,)).fetchone()
        if row is None or row["kind"]!="ask" or row["status"]!="failed" or row["result_json"] is not None:
            raise AskRecoveryError("target is not the original failed Ask job")
        wire=dict(row)
        if failed_job_hash(wire)!=expected_failed_hash:
            raise AskRecoveryError("failed Ask job changed before recovery")
        request=json.loads(row["request_json"])
        if result.get("question") != request.get("question"):
            raise AskRecoveryError("recovered answer belongs to another question")
        result_json=canonical_json(dict(result))
        proof_hash=content_hash(dict(recovery_proof))
        changed=connection.execute(
            "UPDATE cockpit_jobs SET status='done',result_json=?,error=NULL,updated_at=? "
            "WHERE job_id=? AND status='failed' AND result_json IS NULL AND error=? AND updated_at=?",
            (result_json,recovered_at,job_id,row["error"],row["updated_at"])).rowcount
        if changed!=1: raise AskRecoveryError("failed Ask job CAS changed during recovery")
        connection.execute("INSERT INTO cockpit_events(at,kind,title,detail,login,refs_json) VALUES(?,?,?,?,?,?)",
            (recovered_at,"question","语言审查恢复后，原问题已发布",None,row["login"],
             canonical_json({"job_id":job_id,"recovery_proof_hash":proof_hash,
                 "language_artifact_ref":recovery_proof["language_artifact_ref"]})))
        connection.commit()
    except Exception:
        connection.rollback();raise
    return {"status":"done","job_id":job_id,"failed_job_hash":expected_failed_hash,
            "result_sha256":sha256(result_json.encode()).hexdigest(),
            "recovery_proof_hash":proof_hash,"recovered_at":recovered_at}
