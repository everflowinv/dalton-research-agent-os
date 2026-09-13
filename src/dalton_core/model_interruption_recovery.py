"""Operator recovery for an exact model call whose local owner was interrupted."""
from __future__ import annotations

import hashlib, json, os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .cockpit_model import CockpitModel, call_cost_micros
from .contracts import ResultEnvelope, WorkOrder
from .model_router import ModelRouter
from .scheduler import Scheduler, SchedulerConflict
from .store import canonical_json, content_hash
from .thesis_impact_budget import ThesisImpactBudgetStore


class ModelInterruptionRecoveryError(RuntimeError): pass


def _sha(data: bytes) -> str: return hashlib.sha256(data).hexdigest()
def _alive(pid: int) -> bool:
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    except PermissionError: return True
    return True


def recover(plan: Mapping[str, Any], *, execute: bool = False,
            process_is_alive=_alive) -> dict[str, Any]:
    required = {"schema_version","process_pid","process_start","scheduler_db",
                "router_db","budget_db","broker_journal","entries"}
    if not isinstance(plan, Mapping) or set(plan) != required or plan["schema_version"] != "dalton-model-interruption-recovery-plan:0.1":
        raise ModelInterruptionRecoveryError("recovery plan has an invalid closed shape")
    if not isinstance(plan["entries"],list) or not plan["entries"]:
        raise ModelInterruptionRecoveryError("recovery plan entries must be non-empty")
    pid=plan["process_pid"]
    if isinstance(pid,bool) or not isinstance(pid,int) or pid < 2 or process_is_alive(pid):
        raise ModelInterruptionRecoveryError("recovery process identity is not proved dead")
    journal_path=Path(plan["broker_journal"]); journal_bytes=journal_path.read_bytes()
    journal=json.loads(journal_bytes); records=journal.get("records")
    rows=list(records.values()) if isinstance(records,dict) else list(records or [])
    by_inv={r.get("invocationId"):r for r in rows if isinstance(r,dict)}
    outputs=[]
    with Scheduler(plan["scheduler_db"]) as scheduler, ModelRouter(plan["router_db"]) as router, ThesisImpactBudgetStore(plan["budget_db"]) as budget:
      for entry in plan["entries"]:
        keys={"work_order_ref","attempt_number","lease_revision_ref","lease_hash",
              "work_order_hash","owner_ref","disposition","model_config","model_config_sha256"}
        if not isinstance(entry,Mapping) or set(entry)!=keys:
            raise ModelInterruptionRecoveryError("recovery entry has an invalid closed shape")
        authority=scheduler.work_order_authority(entry["work_order_ref"])
        if authority is None or authority["work_order_hash"] != entry["work_order_hash"]:
            raise ModelInterruptionRecoveryError("work authority drifted")
        work=WorkOrder.from_dict(authority["work_order"])
        recovery_key="model-interruption:"+work.id+":"+str(entry["attempt_number"])
        common={"schema_version":"dalton-model-interruption-proof:0.1",
                "process_identity_sha256":_sha((str(pid)+"\0"+plan["process_start"]).encode())}
        if (execute and entry["disposition"]=="undispatched"
                and scheduler.interrupted_model_recovery_receipt(recovery_key) is not None):
            proof=common|{"route_absence_sha256":content_hash([]),
                          "journal_absence_sha256":content_hash([]),
                          "admission_absence_sha256":content_hash(None)}
            response=scheduler.reconcile_interrupted_model_attempt(work.id,entry["attempt_number"],entry["owner_ref"],
                lease_revision_ref=entry["lease_revision_ref"],lease_hash=entry["lease_hash"],work_order_hash=entry["work_order_hash"],
                process_pid=pid,process_start=plan["process_start"],process_is_alive=process_is_alive,
                disposition="undispatched",recovery_proof=proof,idempotency_key=recovery_key)
            if response["status"]!="duplicate":raise ModelInterruptionRecoveryError("prior recovery receipt did not replay")
            outputs.append({"work_order_ref":work.id,"disposition":"undispatched","proof_hash":content_hash(proof),"status":"duplicate"})
            continue
        already_formal=scheduler.formal_result(work.id)
        if already_formal is None and (not execute or entry["disposition"]!="undispatched"):
            scheduler.validate_interrupted_model_attempt(
                work.id,entry["attempt_number"],entry["owner_ref"],
                lease_revision_ref=entry["lease_revision_ref"],
                lease_hash=entry["lease_hash"],work_order_hash=entry["work_order_hash"])
        decisions=router.list_decisions(work_order_id=work.id)
        admission=budget.admission(work_order_ref=work.id,attempt_number=entry["attempt_number"],phase="assessment")
        result=None; result_hash=None
        if entry["disposition"]=="undispatched":
            if decisions or admission is not None or any((r.get("response") or {}).get("workOrderId")==work.id for r in rows):
                raise ModelInterruptionRecoveryError("undispatched authority is contradicted")
            proof=common|{"route_absence_sha256":content_hash([]),
                          "journal_absence_sha256":content_hash([]),
                          "admission_absence_sha256":content_hash(None)}
        elif entry["disposition"]=="durable_completion":
            if len(decisions)!=1 or admission is None:
                raise ModelInterruptionRecoveryError("durable recovery authorities are not singular")
            route=decisions[0]; profile=router.get_profile(route["selected_profile_version_ref"])
            if (route.get("work_order_ref") != work.id
                    or route.get("attempt_number") != entry["attempt_number"]
                    or admission["admission"]["route_decision_ref"] != route["id"]):
                raise ModelInterruptionRecoveryError("route/admission attempt binding drifted")
            config_path=Path(entry["model_config"])
            fd=os.open(config_path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
            try:
                import stat
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ModelInterruptionRecoveryError("model config is not a regular file")
                config_bytes=b""
                while True:
                    part=os.read(fd,1024*1024)
                    if not part:break
                    config_bytes+=part
            finally:os.close(fd)
            if _sha(config_bytes)!=entry["model_config_sha256"]:
                raise ModelInterruptionRecoveryError("model config hash drifted")
            model=CockpitModel(json.loads(config_bytes),scheduler_db=plan["scheduler_db"],
                max_input_tokens=int(work.budget["max_input_tokens"]),
                max_output_tokens=int(work.budget["max_output_tokens"]),
                max_cost_usd=float(work.budget["max_cost_usd"]),
                timeout_seconds=int(work.budget["max_seconds"]))
            purpose=(work.metadata or {}).get("purpose")
            if not isinstance(purpose,str) or not purpose:
                raise ModelInterruptionRecoveryError("work purpose authority is absent")
            matching=[r for r in rows if (r.get("response") or {}).get("workOrderId")==work.id]
            if len(matching)!=1:
                raise ModelInterruptionRecoveryError("durable broker record is not singular")
            record=matching[0]
            adapter=model._adapter(router,timeout_seconds=int(model.budget_for(purpose)["timeout_seconds"]))
            invocation,result_obj=adapter.replay(work,route,profile)
            record=by_inv.get(invocation.id)
            if record is None or record.get("state")!="completed" or (record.get("response") or {}).get("workOrderId")!=work.id:
                raise ModelInterruptionRecoveryError("exact durable broker completion is absent")
            lease_rows=scheduler.lease_history(record_lease_id(scheduler,entry["lease_revision_ref"]))
            exact=[x for x in lease_rows if x["lease_revision_id"]==entry["lease_revision_ref"]]
            if len(exact)!=1 or not (datetime.fromisoformat(exact[0]["issued_at"]).timestamp()*1000 <= record["createdAtMs"] < datetime.fromisoformat(exact[0]["expires_at"]).timestamp()*1000):
                raise ModelInterruptionRecoveryError("broker completion is outside the exact lease")
            actual,_=call_cost_micros(invocation,route,profile,int(admission["admission"]["reserved_micros"]))
            if actual > int(admission["admission"]["reserved_micros"]):
                raise ModelInterruptionRecoveryError("replayed cost exceeds original admission")
            existing_settlement=admission["settlement"]
            reservation_hash=content_hash({"work_order_ref":work.id,"attempt_number":entry["attempt_number"],
                "lease_revision_ref":entry["lease_revision_ref"],"invocation_ref":invocation.id,
                "broker_record_sha256":content_hash(record),"model_config_sha256":entry["model_config_sha256"]})
            if execute and already_formal is None:
                scheduler.reserve_interrupted_model_recovery(work.id,entry["attempt_number"],entry["owner_ref"],
                    lease_revision_ref=entry["lease_revision_ref"],lease_hash=entry["lease_hash"],
                    work_order_hash=entry["work_order_hash"],reservation_hash=reservation_hash)
            if existing_settlement is not None:
                if (existing_settlement["actual_micros"] != actual
                        or existing_settlement.get("usage_entry_ref") is not None):
                    raise ModelInterruptionRecoveryError("existing recovery settlement conflicts")
                settlement=existing_settlement
            elif not execute:
                settlement={"settlement_id":"pending","content_hash":"0"*64}
            else:
                settlement=budget.settle(admission["admission"]["admission_id"],actual_micros=actual)
            proof=common|{"route_decision_ref":route["id"],"profile_version_ref":profile["profile_version_ref"],
                "invocation_ref":invocation.id,"broker_record_sha256":content_hash(record),
                "budget_admission_id":admission["admission"]["admission_id"],
                "budget_settlement_id":settlement["settlement_id"],
                "budget_settlement_sha256":settlement["content_hash"]}
            raw=result_obj.to_dict(); raw["created_at"]=datetime.fromtimestamp(
                int(record["createdAtMs"])/1000,timezone.utc).isoformat(timespec="microseconds")
            result=ResultEnvelope.from_dict(raw).to_dict(); result_hash=content_hash(result)
        else: raise ModelInterruptionRecoveryError("recovery disposition is invalid")
        if execute:
            response=scheduler.reconcile_interrupted_model_attempt(work.id,entry["attempt_number"],entry["owner_ref"],
                lease_revision_ref=entry["lease_revision_ref"],lease_hash=entry["lease_hash"],work_order_hash=entry["work_order_hash"],
                process_pid=pid,process_start=plan["process_start"],process_is_alive=process_is_alive,
                disposition=entry["disposition"],recovery_proof=proof,idempotency_key=recovery_key,
                reservation_hash=(reservation_hash if entry["disposition"]=="durable_completion" else None),
                result_envelope=result,result_envelope_hash=result_hash)
        else: response={"status":"validated"}
        if response["status"]=="conflict":raise ModelInterruptionRecoveryError("model recovery idempotency conflicted")
        outputs.append({"work_order_ref":work.id,"disposition":entry["disposition"],"proof_hash":content_hash(proof),"status":response["status"]})
    return {"schema_version":"dalton-model-interruption-recovery-receipt:0.1","executed":execute,"process_pid":pid,
            "broker_journal_observed_sha256":_sha(journal_bytes),"entries":outputs}


def record_lease_id(scheduler: Scheduler, revision_ref: str) -> str:
    row=scheduler.connection.execute("SELECT lease_id FROM scheduler_leases WHERE lease_revision_id=?",(revision_ref,)).fetchone()
    if row is None: raise SchedulerConflict("lease revision is absent")
    return row["lease_id"]
