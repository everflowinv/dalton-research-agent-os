"""Exact-CAS publication of a fully verified recovered Cockpit Ask job."""
from __future__ import annotations
import json, os, sqlite3
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any
from .research_gap_display import display_metadata_text
from .store import canonical_json, content_hash
class AskRecoveryError(ValueError): pass

def _job_wire(row: Mapping[str, Any]) -> dict[str, Any]:
    return {k: row[k] for k in ("job_id","kind","login","status","request_json","result_json","error","created_at","updated_at")}
def failed_job_hash(row: Mapping[str, Any]) -> str:return content_hash(_job_wire(row))
def _sealed(value: Mapping[str,Any])->dict[str,Any]:
    row=dict(value);row['content_hash']=content_hash(row);return row
def _write_once_fsync(path:Path,data:bytes)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    try:fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes()!=data:raise AskRecoveryError('recovery receipt already exists with different bytes')
        return
    with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
    parent=os.open(path.parent,os.O_RDONLY);os.fsync(parent);os.close(parent)

def recover_failed_ask_job(connection: sqlite3.Connection, *, job_id: str,
        expected_failed_hash: str, producer_result: Mapping[str,Any], result: Mapping[str, Any],
        recovery_proof: Mapping[str, Any], artifact_dir: Path, receipt_path: Path,
        recovered_at: str) -> dict[str, Any]:
    """Publish one reviewed result into the same failed job, under durable CAS."""
    hex64=lambda x:isinstance(x,str) and len(x)==64 and all(c in '0123456789abcdef' for c in x)
    required={"schema_version","job_id","failed_job_hash","original_source_commit","successor_source_commit",
        "language_artifact_ref","language_artifact_sha256","producer_result_envelope_ref","producer_result_sha256",
        "checker_stage_sha256","brain_result_envelope_ref","brain_raw_sha256","brain_fixed_sha256","brain_suffix"}
    if not isinstance(result,Mapping) or not isinstance(producer_result,Mapping) or not isinstance(recovery_proof,Mapping) or set(recovery_proof)!=required:raise AskRecoveryError('recovery inputs have an invalid shape')
    if (recovery_proof['schema_version']!='cockpit-ask-language-recovery:0.1' or recovery_proof['job_id']!=job_id or recovery_proof['failed_job_hash']!=expected_failed_hash or recovery_proof['brain_suffix']!=']}'):raise AskRecoveryError('recovery proof binding is invalid')
    for key in ('language_artifact_sha256','producer_result_sha256','checker_stage_sha256','brain_raw_sha256','brain_fixed_sha256'):
        if not hex64(recovery_proof[key]):raise AskRecoveryError('recovery proof hash is invalid')
    if any(not isinstance(recovery_proof[k],str) or len(recovery_proof[k])!=40 for k in ('original_source_commit','successor_source_commit')):raise AskRecoveryError('recovery source identity is invalid')
    producer_wire=canonical_json(dict(producer_result))
    if sha256(producer_wire.encode()).hexdigest()!=recovery_proof['producer_result_sha256']:raise AskRecoveryError('producer result hash differs')
    ref=recovery_proof['language_artifact_ref']; artifact=(artifact_dir/(ref+'.json')).resolve()
    if artifact_dir.resolve() not in artifact.parents or not artifact.is_file() or artifact.is_symlink():raise AskRecoveryError('language artifact path is invalid')
    payload=artifact.read_bytes()
    if sha256(payload).hexdigest()!=recovery_proof['language_artifact_sha256']:raise AskRecoveryError('language artifact hash differs')
    review=json.loads(payload)
    if not isinstance(review,dict) or review.get('content_hash')!=content_hash({k:v for k,v in review.items() if k!='content_hash'}) or review.get('status')!='ready_for_publication':raise AskRecoveryError('language artifact is not sealed and ready')
    recovered=review.get('brain_recovery') or {}; normalization=recovered.get('normalization') or {}
    if (recovered.get('checker_stage_sha256')!=recovery_proof['checker_stage_sha256']
            or recovered.get('brain_result_envelope_ref')!=recovery_proof['brain_result_envelope_ref']
            or normalization.get('raw_sha256')!=recovery_proof['brain_raw_sha256']
            or normalization.get('fixed_sha256')!=recovery_proof['brain_fixed_sha256']
            or normalization.get('suffix')!=recovery_proof['brain_suffix']):
        raise AskRecoveryError('recovery proof differs from sealed language artifact')
    section=(review.get('brain_revision') or {}).get('sections')
    if not isinstance(section,list) or len(section)!=1:raise AskRecoveryError('language artifact section shape differs')
    section=section[0]; expected_answer=display_metadata_text(section.get('body'));expected_gaps=[display_metadata_text(x) for x in section.get('gaps',[])]
    immutable=('question','answer','sentences','citations','gaps','unknowns','confidence','refused','refusal_reason','refusal_label','refusal_detail','market_vs_us','verification','context','answer_policy','refresh','refreshed_with','refresh_note','claims_considered','claims_total','duplicates_dropped')
    if any(result.get(k)!=producer_result.get(k) for k in immutable):raise AskRecoveryError('recovered result changed producer facts or citations')
    if result.get('display_answer')!=expected_answer or result.get('display_gaps')!=expected_gaps:raise AskRecoveryError('recovered display differs from reviewed section')
    language=result.get('language_review') or {}
    if language.get('status')!='ready_for_publication' or language.get('artifact_ref')!=ref or language.get('artifact_sha256')!=recovery_proof['language_artifact_sha256']:raise AskRecoveryError('result language proof differs')
    expected_cost=round(float(producer_result.get('cost_usd') or 0)+int(review.get('review_cost_micros') or 0)/1_000_000,4)
    if result.get('cost_usd')!=expected_cost:raise AskRecoveryError('recovered cost ledger differs')
    connection.row_factory=sqlite3.Row
    row=connection.execute('SELECT * FROM cockpit_jobs WHERE job_id=?',(job_id,)).fetchone()
    if row is None or row['kind']!='ask' or row['status']!='failed' or row['result_json'] is not None or failed_job_hash(dict(row))!=expected_failed_hash:raise AskRecoveryError('target failed Ask job differs')
    request=json.loads(row['request_json'])
    if result.get('question')!=request.get('question'):raise AskRecoveryError('recovered answer belongs to another question')
    result_json=canonical_json(dict(result));proof_hash=content_hash(dict(recovery_proof))
    receipt=_sealed({'schema_version':'cockpit-ask-language-recovery-receipt:0.1','status':'reserved','job_id':job_id,'failed_job_hash':expected_failed_hash,'result_sha256':sha256(result_json.encode()).hexdigest(),'recovery_proof':dict(recovery_proof),'recovered_at':recovered_at})
    receipt_bytes=(canonical_json(receipt)+'\n').encode();_write_once_fsync(receipt_path,receipt_bytes);receipt_sha=sha256(receipt_bytes).hexdigest()
    connection.execute('BEGIN IMMEDIATE')
    try:
        current=connection.execute('SELECT * FROM cockpit_jobs WHERE job_id=?',(job_id,)).fetchone()
        if current is None or failed_job_hash(dict(current))!=expected_failed_hash:raise AskRecoveryError('failed Ask job changed before CAS')
        changed=connection.execute("UPDATE cockpit_jobs SET status='done',result_json=?,error=NULL,updated_at=? WHERE job_id=? AND status='failed' AND result_json IS NULL AND error=? AND updated_at=?",(result_json,recovered_at,job_id,current['error'],current['updated_at'])).rowcount
        if changed!=1:raise AskRecoveryError('failed Ask job CAS changed during recovery')
        connection.execute('INSERT INTO cockpit_events(at,kind,title,detail,login,refs_json) VALUES(?,?,?,?,?,?)',(recovered_at,'question','语言审查恢复后，原问题已发布',None,current['login'],canonical_json({'job_id':job_id,'recovery_receipt_path':str(receipt_path),'recovery_receipt_sha256':receipt_sha,'recovery_proof_hash':proof_hash})))
        connection.commit()
    except Exception:connection.rollback();raise
    return {'status':'done','job_id':job_id,'result_sha256':receipt['result_sha256'],'recovery_receipt_sha256':receipt_sha}
