#!/usr/bin/env python3
"""Recover one failed Ask from an exact replayed producer result and formal brain envelope."""
import argparse,hashlib,json,sqlite3
from pathlib import Path
from dalton_core.cockpit_ask_recovery import failed_job_hash,recover_failed_ask_job
from dalton_core.research_gap_display import ask_gap_display_fields, display_metadata_text
from dalton_core.research_language_runtime import run
from dalton_core.cockpit_model import unwrap_json_object
from dalton_core.store import canonical_json

def load(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 ap=argparse.ArgumentParser();
 for n in ('journal-db','scheduler-db','producer-result','mission','checker-config','brain-config','verifier-config','artifact-dir','receipt','output'):ap.add_argument('--'+n,type=Path,required=True)
 for n in ('job-id','expected-failed-hash','expected-producer-sha256','producer-result-envelope-ref','producer-route-decision-ref','brain-result-envelope-ref','brain-raw-sha256','original-source-commit','successor-source-commit','recovered-at'):ap.add_argument('--'+n,required=True)
 ap.add_argument('--execute',action='store_true');a=ap.parse_args()
 producer=load(a.producer_result)
 if hashlib.sha256(canonical_json(producer).encode()).hexdigest()!=a.expected_producer_sha256 or producer.get('replayed') is not True:raise SystemExit('producer result is not the exact idempotent replay')
 scheduler=sqlite3.connect(f'file:{a.scheduler_db.resolve()}?mode=ro',uri=True);scheduler.execute('pragma query_only=on');scheduler.execute('begin')
 formal=scheduler.execute('SELECT result_envelope_hash,result_envelope_json,outcome,work_order_id FROM scheduler_result_envelopes WHERE result_envelope_id=?',(a.producer_result_envelope_ref,)).fetchone();scheduler.rollback();scheduler.close()
 if formal is None or formal[2]!='succeeded' or hashlib.sha256(formal[1].encode()).hexdigest()!=formal[0]:raise SystemExit('producer formal envelope is missing or invalid')
 envelope=json.loads(formal[1]);outputs=envelope.get('outputs') or {};raw=outputs.get('text')
 if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=outputs.get('content_hash'):raise SystemExit('producer formal output hash mismatch')
 raw_answer=unwrap_json_object(raw)
 if not isinstance(raw_answer,dict) or producer.get('answer')!=raw_answer.get('answer') or producer.get('gaps')!=raw_answer.get('gaps'):raise SystemExit('producer replay differs from formal model output')
 if (envelope.get('metadata') or {}).get('route_decision_ref')!=a.producer_route_decision_ref:raise SystemExit('producer route differs from formal envelope')
 ro=sqlite3.connect(f'file:{a.journal_db.resolve()}?mode=ro',uri=True);ro.row_factory=sqlite3.Row;ro.execute('pragma query_only=on');ro.execute('begin');row=ro.execute('select * from cockpit_jobs where job_id=?',(a.job_id,)).fetchone();ro.rollback();ro.close()
 if row is None or failed_job_hash(dict(row))!=a.expected_failed_hash:raise SystemExit('failed job identity mismatch')
 plan={'schema_version':'cockpit-ask-language-recovery-plan:0.1','status':'validated' if not a.execute else 'executing','job_id':a.job_id,'failed_job_hash':a.expected_failed_hash,'producer_result_sha256':a.expected_producer_sha256,'producer_result_envelope_ref':a.producer_result_envelope_ref,'brain_result_envelope_ref':a.brain_result_envelope_ref,'brain_raw_sha256':a.brain_raw_sha256,'original_source_commit':a.original_source_commit,'successor_source_commit':a.successor_source_commit,'model_calls':{'ask':0,'checker':0,'brain':0,'fidelity':0 if not a.execute else 1}}
 data=(canonical_json(plan)+'\n').encode();osmod=__import__('os');fd=osmod.open(a.output,osmod.O_WRONLY|osmod.O_CREAT|osmod.O_EXCL|osmod.O_NOFOLLOW,0o600);osmod.write(fd,data);osmod.fsync(fd);osmod.close(fd);parent=osmod.open(a.output.parent,osmod.O_RDONLY);osmod.fsync(parent);osmod.close(parent)
 if not a.execute:return
 mission=load(a.mission);product={'kind':'ask_answer','version_ref':'cockpit-ask:'+json.loads(row['request_json'])['request_id'],'sections':[{'title':'回答','body':producer['answer'],'gaps':producer['gaps']}]}
 review=run(product,mission=mission,request_id=json.loads(row['request_json'])['request_id'],checker_config=a.checker_config,brain_config=a.brain_config,verifier_config=a.verifier_config,scheduler_db=a.scheduler_db,producer_route_decision_ref=a.producer_route_decision_ref,artifact_dir=a.artifact_dir,brain_recovery={'result_envelope_ref':a.brain_result_envelope_ref,'raw_sha256':a.brain_raw_sha256})
 if review.get('status')!='ready_for_publication':raise SystemExit('recovered language review is not publishable')
 section=review['brain_revision']['sections'][0];gap_fields=ask_gap_display_fields(section['gaps']);result={**producer,'display_answer':display_metadata_text(section['body']),**gap_fields,'cost_usd':round(float(producer['cost_usd'])+int(review['review_cost_micros'])/1_000_000,4),'replayed':False,'language_review':{k:review.get(k) for k in ('status','source_hash','revision_hash','content_hash','artifact_ref','artifact_sha256')}}
 proof={'schema_version':'cockpit-ask-language-recovery:0.1','job_id':a.job_id,'failed_job_hash':a.expected_failed_hash,'original_source_commit':a.original_source_commit,'successor_source_commit':a.successor_source_commit,'language_artifact_ref':review['artifact_ref'],'language_artifact_sha256':review['artifact_sha256'],'producer_result_envelope_ref':a.producer_result_envelope_ref,'producer_result_sha256':hashlib.sha256(canonical_json(producer).encode()).hexdigest(),'checker_stage_sha256':review['brain_recovery']['checker_stage_sha256'],'brain_result_envelope_ref':a.brain_result_envelope_ref,'brain_raw_sha256':a.brain_raw_sha256,'brain_fixed_sha256':review['brain_recovery']['normalization']['fixed_sha256'],'brain_suffix':review['brain_recovery']['normalization']['suffix']}
 rw=sqlite3.connect(str(a.journal_db),isolation_level=None);out=recover_failed_ask_job(rw,job_id=a.job_id,expected_failed_hash=a.expected_failed_hash,producer_result=producer,result=result,recovery_proof=proof,artifact_dir=a.artifact_dir,receipt_path=a.receipt,recovered_at=a.recovered_at);rw.close();print(canonical_json({**plan,**out,'status':'complete'}))
if __name__=='__main__':main()
