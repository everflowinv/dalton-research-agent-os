#!/usr/bin/env python3
"""Recover one failed Ask from its immutable formal prompt and result envelopes."""
import argparse,hashlib,json,sqlite3
from pathlib import Path
from dalton_core.cockpit_ask_recovery import failed_job_hash,recover_failed_ask_job
from dalton_core.research_gap_display import ask_gap_display_fields, display_metadata_text
from dalton_core.research_language_runtime import run
from dalton_core.cockpit_model import unwrap_json_object
from dalton_core.ask_answer import parse_answer
from dalton_core.ask_context import BLOCK_LABELS, BLOCK_TAGS
from dalton_core.store import canonical_json

def shown_from_prompt(prompt):
 import re
 blocks={letter:name for name,letter in BLOCK_TAGS.items()}
 letters=re.escape(''.join(sorted(blocks)))
 return [{'tag':tag,'statement':statement,'ref':None,'period':period or None,'company':'','at':'','block':blocks[tag[0]],'block_label':BLOCK_LABELS[blocks[tag[0]]],'recovered_prompt_detail':detail or None}
         for tag,period,detail,statement in re.findall(rf'^([{letters}]\d+)(?: \[([^]]+)\])?(?: （([^）]*)）)? (.*)$',prompt,re.M)]

def load(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 ap=argparse.ArgumentParser();
 for n in ('journal-db','scheduler-db','mission','checker-config','brain-config','verifier-config','artifact-dir','receipt','output'):ap.add_argument('--'+n,type=Path,required=True)
 for n in ('job-id','expected-failed-hash','expected-checker-source-hash','producer-result-envelope-ref','producer-route-decision-ref','brain-result-envelope-ref','brain-raw-sha256','original-source-commit','successor-source-commit','recovered-at'):ap.add_argument('--'+n,required=True)
 ap.add_argument('--execute',action='store_true');a=ap.parse_args()
 scheduler=sqlite3.connect(f'file:{a.scheduler_db.resolve()}?mode=ro',uri=True);scheduler.execute('pragma query_only=on');scheduler.execute('begin')
 formal=scheduler.execute('SELECT result_envelope_hash,result_envelope_json,outcome,work_order_id FROM scheduler_result_envelopes WHERE result_envelope_id=?',(a.producer_result_envelope_ref,)).fetchone();scheduler.rollback();scheduler.close()
 if formal is None or formal[2]!='succeeded' or hashlib.sha256(formal[1].encode()).hexdigest()!=formal[0]:raise SystemExit('producer formal envelope is missing or invalid')
 envelope=json.loads(formal[1]);outputs=envelope.get('outputs') or {};raw=outputs.get('text')
 if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=outputs.get('content_hash'):raise SystemExit('producer formal output hash mismatch')
 raw_answer=unwrap_json_object(raw)
 if not isinstance(raw_answer,dict):raise SystemExit('producer formal output is not an answer object')
 if (envelope.get('metadata') or {}).get('route_decision_ref')!=a.producer_route_decision_ref:raise SystemExit('producer route differs from formal envelope')
 ro=sqlite3.connect(f'file:{a.journal_db.resolve()}?mode=ro',uri=True);ro.row_factory=sqlite3.Row;ro.execute('pragma query_only=on');ro.execute('begin');row=ro.execute('select * from cockpit_jobs where job_id=?',(a.job_id,)).fetchone();ro.rollback();ro.close()
 if row is None:raise SystemExit('failed job is missing')
 row_is_failed=(row['status']=='failed' and failed_job_hash(dict(row))==a.expected_failed_hash)
 row_is_recovered=(row['status']=='done' and row['result_json'] is not None)
 if not row_is_failed and not row_is_recovered:raise SystemExit('failed job identity mismatch')
 request=json.loads(row['request_json']); work_db=sqlite3.connect(f'file:{a.scheduler_db.resolve()}?mode=ro',uri=True); wr=work_db.execute('select work_order_hash,work_order_json from scheduler_work_orders where work_order_id=?',(formal[3],)).fetchone();work_db.close()
 if wr is None or hashlib.sha256(wr[1].encode()).hexdigest()!=wr[0]:raise SystemExit('producer work order is missing or invalid')
 work=json.loads(wr[1]); prompt=work.get('question')
 work_request=(work.get('metadata') or {}).get('request_id')
 if not isinstance(work_request,str) or not work_request.startswith(request.get('request_id','')) or not isinstance(prompt,str) or not prompt.endswith('问题：'+request.get('question','')):raise SystemExit('producer work/request/question identity mismatch')
 # Reconstruct only fields that the immutable prompt actually proves.  Tags and
 # their displayed text are authority; historical refs/counts/policy state were
 # never persisted and are deliberately marked unavailable.
 shown=shown_from_prompt(prompt)
 context={'shown':shown,'wants_market_vs_us':'「看法」类' in prompt}
 parsed=parse_answer(raw_answer,context=context)
 budget_db=a.scheduler_db.with_name('thesis-impact-budget.sqlite'); bc=sqlite3.connect(f'file:{budget_db.resolve()}?mode=ro',uri=True); cost=bc.execute('SELECT s.actual_micros FROM thesis_impact_day_settlements s JOIN thesis_impact_day_admissions a ON a.admission_id=s.admission_id WHERE a.work_order_ref=?',(formal[3],)).fetchall();bc.close()
 if len(cost)!=1:raise SystemExit('producer cost settlement is missing or ambiguous')
 producer={'question':request['question'],'answer':parsed['answer'],'sentences':parsed['sentences'],'citations':parsed['citations'],'gaps':parsed['gaps'],'unknowns':parsed['unknowns'],'confidence':parsed['confidence'],'refused':parsed['refused'],'refusal_reason':parsed['refusal_reason'],'refusal_label':parsed['refusal_label'],'refusal_detail':parsed['refusal_detail'],'market_vs_us':parsed['market_vs_us'],'verification':parsed['verification'],'context':None,'answer_policy':None,'refresh':{'ran':False,'status':'recovered_unavailable'},'refreshed_with':[],'refresh_note':'该回答从原始正式请求与结果恢复；未执行补搜。','claims_considered':None,'claims_total':None,'duplicates_dropped':None,'cost_usd':round(cost[0][0]/1_000_000,4),'replayed':True,'answered_at':None,'recovery_provenance':{'status':'recovered_from_formal_envelopes','work_order_ref':formal[3],'work_order_sha256':wr[0],'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),'result_envelope_ref':a.producer_result_envelope_ref,'result_envelope_sha256':formal[0],'unavailable_fields':['historical_context_summary','policy_state','claim_counts','original_answered_at']}}
 producer_sha=hashlib.sha256(canonical_json(producer).encode()).hexdigest()
 product={'kind':'ask_answer','version_ref':'cockpit-ask:'+request['request_id'],'sections':[{'title':'回答','body':producer['answer'],'gaps':producer['gaps']}]}
 product_source_hash=hashlib.sha256((canonical_json(product)+'\n').encode()).hexdigest()
 if product_source_hash!=a.expected_checker_source_hash:raise SystemExit('reconstructed product does not match the completed checker source')
 plan={'schema_version':'cockpit-ask-language-recovery-plan:0.2','status':'reserved','job_id':a.job_id,'failed_job_hash':a.expected_failed_hash,'producer_result_sha256':producer_sha,'product_source_hash':product_source_hash,'producer_result_envelope_ref':a.producer_result_envelope_ref,'producer_work_order_ref':formal[3],'producer_work_order_sha256':wr[0],'brain_result_envelope_ref':a.brain_result_envelope_ref,'brain_raw_sha256':a.brain_raw_sha256,'original_source_commit':a.original_source_commit,'successor_source_commit':a.successor_source_commit,'planned_model_calls':{'ask':0,'checker':0,'brain':0,'fidelity':1}}
 data=(canonical_json(plan)+'\n').encode();osmod=__import__('os');a.output.parent.mkdir(parents=True,exist_ok=True)
 try:
  fd=osmod.open(a.output,osmod.O_WRONLY|osmod.O_CREAT|osmod.O_EXCL|osmod.O_NOFOLLOW,0o600);osmod.write(fd,data);osmod.fsync(fd);osmod.close(fd);parent=osmod.open(a.output.parent,osmod.O_RDONLY);osmod.fsync(parent);osmod.close(parent)
 except FileExistsError:
  existing=load(a.output)
  if existing.get('status')=='complete':
   core={k:v for k,v in existing.items() if k not in ('result_sha256','recovery_receipt_sha256')};core['status']='reserved'
   if core!=plan:raise SystemExit('recovery output is occupied by different bytes')
   if not a.execute:print(canonical_json(existing));return
  elif existing!=plan:raise SystemExit('recovery output is occupied by different bytes')
 if not a.execute:return
 def finalized(row_json):
  if not a.receipt.is_file() or a.receipt.is_symlink():raise SystemExit('recovered job has no exact durable recovery receipt')
  receipt=load(a.receipt); result_sha=hashlib.sha256(row_json.encode()).hexdigest()
  if receipt.get('job_id')!=a.job_id or receipt.get('failed_job_hash')!=a.expected_failed_hash or receipt.get('result_sha256')!=result_sha:raise SystemExit('recovered job differs from recovery receipt')
  complete={**plan,'status':'complete','result_sha256':result_sha,'recovery_receipt_sha256':sha(a.receipt)};tmp=a.output.with_suffix(a.output.suffix+'.tmp');tmp.write_text(canonical_json(complete)+'\n');osmod.chmod(tmp,0o600);osmod.replace(tmp,a.output);parent=osmod.open(a.output.parent,osmod.O_RDONLY);osmod.fsync(parent);osmod.close(parent);print(canonical_json(complete))
 if row_is_recovered:
  finalized(row['result_json']);return
 mission=load(a.mission)
 review=run(product,mission=mission,request_id=json.loads(row['request_json'])['request_id'],checker_config=a.checker_config,brain_config=a.brain_config,verifier_config=a.verifier_config,scheduler_db=a.scheduler_db,producer_route_decision_ref=a.producer_route_decision_ref,artifact_dir=a.artifact_dir,brain_recovery={'result_envelope_ref':a.brain_result_envelope_ref,'raw_sha256':a.brain_raw_sha256})
 if review.get('status')!='ready_for_publication':raise SystemExit('recovered language review is not publishable')
 section=review['brain_revision']['sections'][0];gap_fields=ask_gap_display_fields(section['gaps']);result={**producer,'display_answer':display_metadata_text(section['body']),**gap_fields,'cost_usd':round(float(producer['cost_usd'])+int(review['review_cost_micros'])/1_000_000,4),'replayed':False,'language_review':{k:review.get(k) for k in ('status','source_hash','revision_hash','content_hash','artifact_ref','artifact_sha256')}}
 proof={'schema_version':'cockpit-ask-language-recovery:0.1','job_id':a.job_id,'failed_job_hash':a.expected_failed_hash,'original_source_commit':a.original_source_commit,'successor_source_commit':a.successor_source_commit,'language_artifact_ref':review['artifact_ref'],'language_artifact_sha256':review['artifact_sha256'],'producer_result_envelope_ref':a.producer_result_envelope_ref,'producer_result_sha256':producer_sha,'checker_stage_sha256':review['brain_recovery']['checker_stage_sha256'],'brain_result_envelope_ref':a.brain_result_envelope_ref,'brain_raw_sha256':a.brain_raw_sha256,'brain_fixed_sha256':review['brain_recovery']['normalization']['fixed_sha256'],'brain_suffix':review['brain_recovery']['normalization']['suffix']}
 rw=sqlite3.connect(str(a.journal_db),isolation_level=None)
 try: out=recover_failed_ask_job(rw,job_id=a.job_id,expected_failed_hash=a.expected_failed_hash,producer_result=producer,result=result,recovery_proof=proof,artifact_dir=a.artifact_dir,receipt_path=a.receipt,recovered_at=a.recovered_at)
 except Exception:
  done=rw.execute("select result_json from cockpit_jobs where job_id=? and status='done'",(a.job_id,)).fetchone()
  if not done or not a.receipt.exists(): rw.close();raise
  finalized(done[0]);rw.close();return
 rw.close();complete={**plan,**out,'status':'complete'};tmp=a.output.with_suffix(a.output.suffix+'.tmp');tmp.write_text(canonical_json(complete)+'\n');osmod.chmod(tmp,0o600);osmod.replace(tmp,a.output);parent=osmod.open(a.output.parent,osmod.O_RDONLY);osmod.fsync(parent);osmod.close(parent);print(canonical_json(complete))
if __name__=='__main__':main()
