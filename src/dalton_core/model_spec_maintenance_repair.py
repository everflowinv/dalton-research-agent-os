"""One explicit maintenance correction of a previously paid incomplete diagnosis.

This grant is separate from the original repair slot. Its key is the immutable
root Work, never a day, contract version, budget, model epoch or error string.
It does not grant financial writes or bypass normal model/budget admission.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from .store import canonical_json, content_hash
from .contracts import WorkOrder
from .model_spec_complete_diagnostics import diagnose, strong_result, _row

CONTRACT_REF = "model-spec-fixed-maintenance-correction:0.1"
TABLE = "company_model_spec_maintenance_grants"
CLAIMS = "company_model_spec_maintenance_claims"
MAX_RECORDS = 64
# Exact frozen first-error contract before complete independent diagnosis.
# Unknown contracts cannot create a new maintenance entitlement.
KNOWN_INCOMPLETE_CONTRACTS = {
    "contract:company-model-spec-structured-output-repair:0.12":
        "03fee2a3ba8923056af8c89f1e1d84a11c0a8c812eefb01d0a0e1f24fc8cb2e7",
}
PROOF_KEYS = ("work_order_ref", "work_order_hash", "result_envelope_ref",
              "result_envelope_hash", "invocation_ref", "route_decision_ref")


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _load(connection, ref):
    row = _row(connection, "SELECT * FROM scheduler_work_orders WHERE work_order_id=?", (ref,))
    if row is None:
        raise ValueError("maintenance Work missing")
    work = WorkOrder.from_dict(json.loads(row["work_order_json"])).to_dict()
    formal = strong_result(connection, ref)
    if (canonical_json(work) != row["work_order_json"] or content_hash(work) != row["work_order_hash"]
            or formal is None or formal["terminal_state"] != "succeeded"
            or work["metadata"].get("purpose") != "model_spec"
            or work["metadata"].get("control_plane") != "cockpit" or work["input_refs"]):
        raise ValueError("maintenance parent is not a strong succeeded model-spec Work")
    result = formal["result_envelope"]
    text = result.get("outputs", {}).get("text")
    if not isinstance(text, str) or not result.get("invocation_ref"):
        raise ValueError("maintenance result text/invocation missing")
    call = {"work_order_ref": ref, "work_order_hash": row["work_order_hash"],
            "result_envelope_ref": result["id"], "result_envelope_hash": formal["result_envelope_hash"],
            "invocation_ref": result["invocation_ref"],
            "route_decision_ref": result.get("metadata", {}).get("route_decision_ref"),
            "text": text, "replayed": True, "cost_micros": 0, "cost_status": "replayed"}
    if any(not isinstance(call[k], str) or not call[k] for k in PROOF_KEYS):
        raise ValueError("maintenance result authority incomplete")
    return work, call


def _proof(call):
    return {k: call[k] for k in PROOF_KEYS}


def _diagnostic_error(report):
    from .company_model_spec import CompanyModelSpecError
    from .model_spec_complete_diagnostics import ERROR_CODE
    return CompanyModelSpecError("independent settings errors: " + ", ".join(
        f["rule"] for f in report["findings"]), code=ERROR_CODE, proof=report)


def validate_report(body, proof):
    from .company_model_spec import CompanyModelSpecError
    actual = diagnose(body)
    if (actual["status"] != "complete" or not actual["findings"]
            or canonical_json(actual) != canonical_json(proof)):
        raise CompanyModelSpecError("maintenance diagnostic differs from its exact parent")


def assert_changed(original, repaired, proof):
    # Reuse the whole-body/alternatives guard with an explicit one-finding
    # threshold; ordinary multi-error repair still requires two findings.
    from .model_spec_complete_diagnostics import assert_only_allowed_changed
    validate_report(original, proof)
    assert_only_allowed_changed(original, repaired, proof, minimum_findings=1)


def _permission(missions, mission, company):
    current = missions._resolve_active_mission_for_company(company, purpose=CONTRACT_REF,
        mission_version_ref=mission["id"], mission_version_hash=mission["content_hash"])
    if (current != mission or "model_run" not in current["autonomy"]["may_write"]
            or "company_model" not in current["deliverables"]
            or company not in {v["company_ref"] for v in current["universe"]}):
        raise ValueError("maintenance current company/mission permission differs")
    cur = missions.connection.cursor()
    missions._validate_playbook_binding(cur, current["bindings"]["playbook_version"])
    missions._validate_constitution_binding(cur, current["bindings"]["constitution_version"], current["industry_ref"])
    missions._validate_mandate_binding(cur, current["bindings"]["mandate_version"], current["industry_ref"])
    return current


def qualify(connection, *, missions, model, state, mission, root_ref, parent_ref):
    """No edits/calls; exact old results, original mission and every fee close."""
    from .company_model_cli import TASK_HASH, REPAIR_CONTRACT_REF, build_prompt, validate_model_spec_request_identity, structured_output_repair_config, _repair_prompt
    from .company_model_spec import parse_response, spec_from_response, CompanyModelSpecError
    from .investment_memo_quiesce import settled_call
    if content_hash({k: v for k, v in state.items() if k != "state_hash"}) != state["state_hash"]:
        raise ValueError("maintenance full state hash differs")
    if structured_output_repair_config(model.config)["max_attempts"] < 1:
        raise ValueError("maintenance repair is disabled by the current configuration")
    current = _permission(missions, mission, state["company_ref"])
    root, root_call = _load(connection, root_ref)
    parent, parent_call = _load(connection, parent_ref)
    root_identity = validate_model_spec_request_identity(root["metadata"].get("model_spec_request_identity", {}))
    binding = parent["metadata"].get("structured_output_repair")
    root_proof = _proof(root_call)
    if (not isinstance(root_identity, Mapping) or root_identity.get("task_hash") != TASK_HASH
            or root_identity.get("state_hash") != state["state_hash"]
            or root_identity.get("structured_output_repair") != {"max_attempts": 1}
            or not root["metadata"].get("request_id", "").startswith(content_hash(root_identity)[:32])
            or root["question"] != build_prompt(state)
            or root["metadata"].get("structured_output_repair") is not None
            or not isinstance(binding, Mapping) or binding.get("maintenance_grant") is not None
            or binding.get("root_original") != root_proof or binding.get("repair_parent") != root_proof
            or binding.get("repair_number") != 1 or binding.get("repair_config") != {"max_attempts": 1}
            or binding.get("repair_contract_ref") == REPAIR_CONTRACT_REF
            or binding.get("diagnostic_bundle") is not None
            or binding.get("state_hash") != state["state_hash"] or binding.get("task_hash") != TASK_HASH
            or binding.get("original_text_sha256") != _sha(root_call["text"])
            or binding.get("parent_text_sha256") != _sha(root_call["text"])
            or binding.get("repair_prompt_sha256") != _sha(parent["question"])):
        raise ValueError("maintenance original root/used single repair binding differs")
    # The actual old Work freezes its contract; do not invent old fields or
    # recertify an arbitrary prompt by hashing it now.
    try:
        old_contract = json.JSONDecoder().raw_decode(parent["question"].split("REPAIR_CONTRACT:\n", 1)[1])[0]
        old_schema = json.JSONDecoder().raw_decode(parent["question"].split("OUTPUT_SCHEMA:\n", 1)[1])[0]
    except (IndexError, ValueError, TypeError) as exc:
        raise ValueError("maintenance frozen old contract unavailable") from exc
    if (KNOWN_INCOMPLETE_CONTRACTS.get(binding.get("repair_contract_ref")) != binding.get("repair_contract_hash")
            or old_contract.get("ref") != binding.get("repair_contract_ref")
            or content_hash(old_contract) != binding.get("repair_contract_hash")
            or not parent["metadata"].get("request_id", "").startswith("model-spec-repair:" + content_hash(binding)[:32])):
        raise ValueError("maintenance frozen old contract/request differs")
    siblings = connection.execute("SELECT work_order_id FROM scheduler_work_orders WHERE work_order_id >= 'work:cockpit-model_spec-' AND work_order_id < 'work:cockpit-model_spec.' AND "
        "json_extract(work_order_json,'$.metadata.structured_output_repair.root_original.work_order_ref')=? "
        "AND json_extract(work_order_json,'$.metadata.structured_output_repair.maintenance_grant') IS NULL "
        "LIMIT ?", (root_ref,MAX_RECORDS+1)).fetchall()
    if len(siblings)>MAX_RECORDS or {r[0] for r in siblings}!={parent_ref}:
        raise ValueError("maintenance original root has additional unproved repair consumption")
    original = parse_response(root_call["text"])
    repaired = parse_response(parent_call["text"])
    initial_report = diagnose(original)
    report = diagnose(repaired)
    if (initial_report["status"] != "complete" or len(initial_report["findings"]) < 2
            or report["status"] != "complete" or not report["findings"]):
        raise ValueError("maintenance requires independent omitted settings, not arbitrary financial failure")
    # An old paid correction may fix a subset only; everything else must be
    # exactly the original body and its choices must remain contract-legal.
    # Permit unchanged diagnosed paths, without accepting an invalid new value.
    changed_report = copy.deepcopy(initial_report)
    changed_report["findings"] = [f for f in initial_report["findings"] if any(
        _path(original, p) != _path(repaired, p) for p in f["paths"])]
    if not changed_report["findings"]:
        raise ValueError("maintenance old repair did not correct any diagnosed setting")
    _assert_subset_changed(original, repaired, initial_report, changed_report["findings"])
    try:
        spec_from_response(state, root_call["text"], decided_by=current["autonomy"]["automation_principal"])
    except CompanyModelSpecError as error:
        if (binding.get("validation_error") != {"code": error.code, "message": str(error)}
                or parent["question"] != _repair_prompt(root_call["text"],error,contract=old_contract,output_schema=old_schema)):
            raise ValueError("maintenance old first-error diagnosis differs") from error
    else:
        raise ValueError("maintenance original answer is already valid")
    # Original mission is strongly read, never synthesized from a hash.
    old_mission = missions._mission_record(root["metadata"]["mission_version_ref"])
    if (old_mission["content_hash"] != root["metadata"].get("mission_version_hash")
            or old_mission["mission_ref"] != current["mission_ref"]
            or state["company_ref"] not in {v["company_ref"] for v in old_mission["universe"]}
            or (parent["metadata"].get("mission_version_ref"), parent["metadata"].get("mission_version_hash"))
               != (old_mission["id"], old_mission["content_hash"])):
        raise ValueError("maintenance historical mission/company binding differs")
    fees = [settled_call(model, call, old_mission, purpose="model_spec") for call in (root_call, parent_call)]
    return {"contract_ref": CONTRACT_REF, "company_ref": state["company_ref"],
            "mission_ref": current["mission_ref"], "state_hash": state["state_hash"], "task_hash": TASK_HASH,
            "root_original": root_proof, "repair_parent": _proof(parent_call),
            "original_text_sha256": _sha(root_call["text"]), "parent_text_sha256": _sha(parent_call["text"]),
            "original_diagnostics": initial_report, "diagnostic_bundle": report, "paid_prefix": fees}, root_call, parent_call


def _path(body, path):
    for key in path:
        body = body[key]
    return body


def _assert_subset_changed(original, repaired, full_report, changed):
    from .company_model_spec import CompanyModelSpecError
    # Restore unmodified diagnosed settings to a legal choice in both temporary
    # bodies. This invokes the existing guard while preserving exact original
    # and repaired bodies, including every unrelated field.
    left, right = original, repaired
    for f in full_report["findings"]:
        if f in changed:
            continue
        for p in f["paths"]:
            if _path(left, p) != _path(right, p):
                raise CompanyModelSpecError("maintenance old repair changed an unproved field")
    # Guard independently all changed paths/choices, then whole-body equality.
    expected = copy.deepcopy(repaired)
    for f in changed:
        if f["rule"] not in {"shares_annual_semantics", "eps_annual_semantics"}:
            raise ValueError("maintenance old partial repair must be an annual setting correction")
        if _path(repaired, f["paths"][0]) not in f["choices"][0]:
            raise ValueError("maintenance old repair selected an invalid annual setting")
        for p, value in zip(f["paths"], f["original_values"], strict=True):
            node = expected
            for k in p[:-1]: node = node[k]
            node[p[-1]] = copy.deepcopy(value)
    if canonical_json(expected) != canonical_json(original):
        raise CompanyModelSpecError("maintenance old repair changed fields outside diagnosed paths")


class MaintenanceAuthority:
    """Live owner capability; JSON metadata alone cannot authorize a call."""
    def __init__(self, missions, model, state, mission):
        self.missions, self.model = missions, model
        self.state, self.mission = copy.deepcopy(state), copy.deepcopy(mission)

    def _schema(self):
        c = self.missions.connection
        c.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (root_ref TEXT PRIMARY KEY, record_json TEXT NOT NULL, content_hash TEXT NOT NULL)")
        c.execute(f"CREATE TABLE IF NOT EXISTS {CLAIMS} (sequence INTEGER PRIMARY KEY, root_ref TEXT NOT NULL, work_ref TEXT NOT NULL UNIQUE, work_hash TEXT NOT NULL, grant_hash TEXT NOT NULL)")
        for table in (TABLE, CLAIMS):
            for action in ("INSERT", "UPDATE", "DELETE"):
                c.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()} BEFORE {action} ON {table} WHEN dalton_coverage_mission_authorized()=0 BEGIN SELECT RAISE(ABORT,'maintenance owner required'); END")
            for action in ("UPDATE", "DELETE"):
                c.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'maintenance record immutable'); END")

    def issue(self, connection, root_ref, parent_ref):
        body, root_call, parent_call = qualify(connection, missions=self.missions, model=self.model,
            state=self.state, mission=self.mission, root_ref=root_ref, parent_ref=parent_ref)
        self._schema()
        with self.missions._transaction() as cur:
            row = cur.execute(f"SELECT * FROM {TABLE} WHERE root_ref=?", (root_ref,)).fetchone()
            if row is None:
                body = {**body, "authorization_at_issue": {
                    "mission_version_ref": self.mission["id"],
                    "mission_version_hash": self.mission["content_hash"],
                    "actor_ref": self.mission["autonomy"]["automation_principal"]}}
                cur.execute(f"INSERT INTO {TABLE} VALUES (?,?,?)", (root_ref, canonical_json(body), content_hash(body)))
            else:
                prior = json.loads(row["record_json"])
                if (canonical_json(prior) != row["record_json"] or content_hash(prior) != row["content_hash"]
                        or {k:v for k,v in prior.items() if k != "authorization_at_issue"} != body):
                    raise ValueError("fixed maintenance grant already exists with different evidence/diagnosis")
                body = prior
            self._issued_permission(body)
        return body, root_call, parent_call

    def _issued_permission(self, body):
        proof = body["authorization_at_issue"]
        issued = self.missions._mission_record(proof["mission_version_ref"])
        if (issued["content_hash"] != proof["mission_version_hash"]
                or issued["mission_ref"] != body["mission_ref"]
                or issued["autonomy"]["automation_principal"] != proof["actor_ref"]
                or "model_run" not in issued["autonomy"]["may_write"]
                or "company_model" not in issued["deliverables"]
                or body["company_ref"] not in {v["company_ref"] for v in issued["universe"]}):
            raise ValueError("maintenance original explicit grant permission differs")

    def find(self, connection):
        rows = connection.execute("SELECT work_order_id FROM scheduler_work_orders WHERE work_order_id >= 'work:cockpit-model_spec-' AND work_order_id < 'work:cockpit-model_spec.' AND "
            "json_extract(work_order_json,'$.metadata.model_spec_request_identity.state_hash')=? "
            "AND json_extract(work_order_json,'$.metadata.model_spec_request_identity.task_hash')=? "
            "ORDER BY created_at DESC,work_order_id DESC LIMIT ?", (self.state["state_hash"],
            __import__('dalton_core.company_model_cli', fromlist=['TASK_HASH']).TASK_HASH, MAX_RECORDS+1)).fetchall()
        if len(rows)>MAX_RECORDS: raise ValueError("maintenance exact root capacity exceeded")
        for row in rows:
            root_ref = row[0]
            repairs = connection.execute("SELECT work_order_id FROM scheduler_work_orders WHERE work_order_id >= 'work:cockpit-model_spec-' AND work_order_id < 'work:cockpit-model_spec.' AND "
                "json_extract(work_order_json,'$.metadata.structured_output_repair.root_original.work_order_ref')=? "
                "AND json_extract(work_order_json,'$.metadata.structured_output_repair.maintenance_grant') IS NULL "
                "ORDER BY created_at DESC,work_order_id DESC LIMIT ?", (root_ref, MAX_RECORDS+1)).fetchall()
            if len(repairs)>MAX_RECORDS: raise ValueError("maintenance exact repair capacity exceeded")
            if not repairs: return None
            # A later unknown/in-flight/paid failed correction is not an empty
            # slot. Never search backward past it for a nicer successful answer.
            parent_ref = repairs[0][0]
            try: return self.issue(connection, root_ref, parent_ref)
            except ValueError: return None
        return None

    def verify_child(self, connection, binding, child):
        from .company_model_cli import _repair_prompt
        from .company_model_spec import parse_response
        grant = binding["maintenance_grant"]
        row = self.missions.connection.execute(f"SELECT * FROM {TABLE} WHERE root_ref=?", (grant["root_ref"],)).fetchone()
        if row is None: raise ValueError("maintenance grant missing")
        body = json.loads(row["record_json"])
        if canonical_json(body)!=row["record_json"] or content_hash(body)!=row["content_hash"] or grant["content_hash"]!=row["content_hash"]:
            raise ValueError("maintenance grant hash differs")
        current, root, parent = qualify(connection, missions=self.missions, model=self.model,
            state=self.state, mission=self.mission, root_ref=grant["root_ref"], parent_ref=body["repair_parent"]["work_order_ref"])
        self._issued_permission(body)
        if (current != {k:v for k,v in body.items() if k != "authorization_at_issue"} or binding["root_original"] != body["root_original"]
                or binding["repair_parent"] != body["repair_parent"]
                or binding["state_hash"] != body["state_hash"] or binding["task_hash"] != body["task_hash"]
                or binding["diagnostic_bundle"] != body["diagnostic_bundle"]
                or binding["parent_text_sha256"] != body["parent_text_sha256"]
                or binding["original_text_sha256"] != body["original_text_sha256"]
                or binding["repair_config"] != {"max_attempts":1} or binding["repair_number"] != 2
                or child.metadata.get("mission_version_ref") != self.mission["id"]
                or child.metadata.get("mission_version_hash") != self.mission["content_hash"]
                or child.question != _repair_prompt(parent["text"], _diagnostic_error(body["diagnostic_bundle"]))
                or binding["validation_error"] != {"code": _diagnostic_error(body["diagnostic_bundle"]).code,
                    "message": str(_diagnostic_error(body["diagnostic_bundle"]))}):
            raise ValueError("maintenance child grant/diagnosis/current permission differs")
        # CAS before admission: a concurrent child with different config cannot
        # consume the same grant. Only exact proved zero-send refusals transfer
        # an unused grant; unknown/pending/paid states preserve their authority.
        with self.missions._transaction() as cur:
            claims = cur.execute(f"SELECT * FROM {CLAIMS} WHERE root_ref=? ORDER BY sequence", (grant["root_ref"],)).fetchall()
            if len(claims)>=MAX_RECORDS and not any(r["work_ref"]==child.id for r in claims):
                raise ValueError("maintenance no-send claim capacity exhausted")
            for claim in claims:
                if claim["grant_hash"]!=grant["content_hash"]: raise ValueError("maintenance claim grant differs")
                if claim["work_ref"]==child.id:
                    if claim["work_hash"]!=content_hash(child.to_dict()): raise ValueError("maintenance child Work changed")
                    continue
                if not _no_send(connection, self.model, claim):
                    raise ValueError("maintenance correction already consumed or in flight")
            if not any(r["work_ref"]==child.id for r in claims):
                cur.execute(f"INSERT INTO {CLAIMS}(root_ref,work_ref,work_hash,grant_hash) VALUES (?,?,?,?)",
                    (grant["root_ref"],child.id,content_hash(child.to_dict()),grant["content_hash"]))


def _no_send(connection, model, claim):
    from .planner_paid_output_recovery import recovery_readers
    from .cockpit_timeout_recovery import budget_refusal_proof
    from .model_spec_complete_diagnostics import _same_slot_no_send
    from .contracts import ResultEnvelope
    row = _row(connection, "SELECT * FROM scheduler_work_orders WHERE work_order_id=?", (claim["work_ref"],))
    if row is None or row["work_order_hash"]!=claim["work_hash"]: return False
    work = WorkOrder.from_dict(json.loads(row["work_order_json"]))
    if canonical_json(work.to_dict())!=row["work_order_json"] or content_hash(work.to_dict())!=claim["work_hash"]: return False
    if _same_slot_no_send(connection,work.to_dict(),model.config): return True
    formal = strong_result(connection,work.id)
    if formal is None or formal["terminal_state"]!='failed': return False
    result = ResultEnvelope.from_dict(formal["result_envelope"])
    with recovery_readers(model.config["model_router_db"],model.config["budget_db"],now=model.clock()) as (router,budget):
        if budget.connection.execute("SELECT 1 FROM thesis_impact_day_admissions WHERE work_order_ref=? LIMIT 1",(work.id,)).fetchone(): return False
        return budget_refusal_proof(budget,router,work,formal["attempt_number"],
            {"result":result,"invocation":None,"cost_micros":0,
             "pool_rejection":result.metadata.get("pool_rejection")}) is not None


def binding_for(body, parent_text):
    from .company_model_cli import REPAIR_CONTRACT_REF,REPAIR_CONTRACT_HASH,_repair_prompt
    error = _diagnostic_error(body["diagnostic_bundle"])
    prompt = _repair_prompt(parent_text,error)
    binding = {"schema_version":"company-model-spec-repair-binding-0.1",
        **{k:copy.deepcopy(body[k]) for k in ("root_original","repair_parent","original_text_sha256","parent_text_sha256","state_hash","task_hash","diagnostic_bundle")},
        "validation_error":{"code":error.code,"message":str(error)},
        "repair_contract_ref":REPAIR_CONTRACT_REF,"repair_contract_hash":REPAIR_CONTRACT_HASH,
        "repair_prompt_sha256":_sha(prompt),"repair_config":{"max_attempts":1},"repair_number":2,
        "maintenance_grant":{"contract_ref":CONTRACT_REF,"root_ref":body["root_original"]["work_order_ref"],"content_hash":content_hash(body)}}
    return binding,prompt


def execute(authority, candidate, *, decided_by, repairs, note_evidence_resolver=None, financial_validator=None):
    """Normal lane entry, with free strong-result replay before any new call."""
    from .company_model_spec import spec_from_response, parse_response, CompanyModelSpecError
    from .planner_paid_output_recovery import _authority_connection
    body, root, parent = candidate
    binding,prompt = binding_for(body,parent['text'])
    grant_hash = content_hash(body)
    call = None
    budget_defer = None
    with _authority_connection(authority.model.scheduler_db) as connection:
        fresh, _, _ = qualify(connection, missions=authority.missions, model=authority.model,
            state=authority.state, mission=authority.mission,
            root_ref=root['work_order_ref'],parent_ref=parent['work_order_ref'])
        row = authority.missions.connection.execute(f"SELECT * FROM {TABLE} WHERE root_ref=?",(root['work_order_ref'],)).fetchone()
        if (row is None or row['record_json']!=canonical_json(body) or row['content_hash']!=grant_hash
                or fresh != {k:v for k,v in body.items() if k!='authorization_at_issue'}):
            raise ValueError('maintenance durable grant/current qualification differs')
        authority._issued_permission(body)
        claims = authority.missions.connection.execute(f"SELECT * FROM {CLAIMS} WHERE root_ref=? ORDER BY sequence DESC LIMIT ?",
            (root['work_order_ref'],MAX_RECORDS+1)).fetchall()
        if len(claims)>MAX_RECORDS: raise CompanyModelSpecError('maintenance claim capacity exceeded')
        for claim in claims:
            if claim['grant_hash']!=grant_hash: raise CompanyModelSpecError('maintenance claim grant differs')
            result = strong_result(connection,claim['work_ref'])
            if result is not None and result['terminal_state']=='succeeded':
                work,call = _load(connection,claim['work_ref'])
                if (work['metadata'].get('structured_output_repair')!=binding
                        or work['question']!=prompt or content_hash(work)!=claim['work_hash']):
                    raise CompanyModelSpecError('maintenance paid answer differs from grant')
                from .investment_memo_quiesce import settled_call
                old = authority.missions._mission_record(work['metadata']['mission_version_ref'])
                if work['metadata']['mission_version_hash']!=old['content_hash'] or old['mission_ref']!=body['mission_ref']:
                    raise CompanyModelSpecError('maintenance replay mission differs')
                settled_call(authority.model,call,old,purpose='model_spec')
                break
            if (result is not None and result['terminal_state']=='failed'
                    and (result['result_envelope'].get('error') or {}).get('code') in {'BUDGET_REFUSED','POOL_EXHAUSTED'}
                    and _no_send(connection,authority.model,claim)):
                from datetime import datetime,timezone
                failed_day = datetime.fromisoformat(result['created_at']).astimezone(timezone.utc).date()
                now_day = authority.model.clock().astimezone(timezone.utc).date()
                if now_day > failed_day:
                    budget_defer = content_hash({'unused_grant_hash':grant_hash,
                        'no_send_work_hash':claim['work_hash'],'eligible_day':now_day.isoformat()})[:16]
            # The live CAS checks all OTHER immutable children immediately
            # before admission. The exact current child may resume after a
            # crash before enqueue without inventing another authorization.
    if call is None:
        authority.model._model_spec_maintenance_authority = authority
        try:
            request_id='model-spec-repair:'+content_hash(binding)[:32]
            if budget_defer is not None:
                # This is an unused grant's strong local no-send delay, never
                # a day-keyed new paid entitlement. CAS rejects ANY paid,
                # unknown or in-flight sibling under this same fixed grant.
                from .cockpit_model import _capacity_retry
                config=authority.model.config
                if 'capacity_retry' in config: request_id+=':capacity-policy:'+content_hash(_capacity_retry(config))[:16]
                if 'transport_retry' in config: request_id+=':transport-policy:'+content_hash(config['transport_retry'])[:16]
                request_id+=':operator-recovery:'+budget_defer
            call = authority.model.call(purpose='model_spec', mission=authority.mission,
                request_id=request_id,prompt=prompt,
                _structured_output_repair=binding)
        finally:
            del authority.model._model_spec_maintenance_authority
    repairs.append({**{k:call.get(k) for k in PROOF_KEYS},'repair_number':2,
        'maintenance_grant_hash':grant_hash,'replayed':bool(call.get('replayed')),
        'cost_micros':int(call.get('cost_micros') or 0),'binding_hash':content_hash(binding)})
    assert_changed(parse_response(parent['text']),parse_response(call['text']),body['diagnostic_bundle'])
    spec = spec_from_response(authority.state,call['text'],decided_by=decided_by,
        note_evidence_resolver=note_evidence_resolver)
    if financial_validator is not None: financial_validator(spec)
    return spec,call
