"""Budgeted publication-language review using two independently configured routes."""
from __future__ import annotations

import json
import os
import sqlite3
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Mapping

from .cockpit_model import CockpitModel, independent_model_call, unwrap_json_object
from .company_dossier_draft import independence
from .model_router import ModelRouter
from .model_fallback_chain import TIER_BRAIN, TIER_CHEAP, TIER_VERIFIER, register_purpose_tier
from .research_localization import build_verifier_prompt
from .research_language_review import (
    BRAIN_PURPOSE, CHECKER_MODEL, CHECKER_PURPOSE, ResearchLanguageReviewError,
    build_checker_prompt, parse_stage_output_with_proof, run_language_review,
)
from .store import canonical_json, content_hash

register_purpose_tier(CHECKER_PURPOSE, TIER_CHEAP)
register_purpose_tier(BRAIN_PURPOSE, TIER_BRAIN)
register_purpose_tier("research_localization_verifier", TIER_VERIFIER)
FIDELITY_PURPOSE = "research_localization_verifier"


def _hash_bytes(data: bytes) -> str:
    return sha256(data).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    value=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value,dict): raise ResearchLanguageReviewError("语言审查配置格式无效")
    return value


def _route_identity(config: Mapping[str, Any], call: Mapping[str, Any]) -> dict[str, str]:
    ref=call.get("route_decision_ref")
    if not isinstance(ref,str): raise ResearchLanguageReviewError("语言审查没有模型路由记录")
    router=ModelRouter(config["model_router_db"])
    try:
        decision=router.get_decision(ref); profile=router.get_profile(decision["selected_profile_version_ref"])
    finally: router.close()
    return {"route_decision_ref":ref,"profile_version_ref":decision["selected_profile_version_ref"],
            "provider":str(profile["provider"]),"model":str(profile["model"]),
            "family":str(profile["family"])}


def _independence(config: Mapping[str, Any], *, draft_routes: list[str],
                  verifier_route: str) -> dict[str, Any]:
    router=ModelRouter(config["model_router_db"])
    def resolve(ref: str | None) -> str | None:
        if not isinstance(ref,str) or not ref: return None
        try:
            decision=router.get_decision(ref)
            family=router.get_profile(decision["selected_profile_version_ref"]).get("family")
            return family if isinstance(family,str) and family else None
        except Exception:
            return None
    try:
        return independence(draft_routes=draft_routes,verifier_route=verifier_route,resolve=resolve)
    finally:
        router.close()


def _write_once(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        if path.read_bytes() != data:
            raise ResearchLanguageReviewError("语言审查证据文件已存在但内容不同")
        return
    with os.fdopen(fd,"wb") as stream: stream.write(data)


def _config(path: Path) -> tuple[dict[str, Any], str]:
    data=path.read_bytes(); value=json.loads(data)
    if not isinstance(value,dict): raise ResearchLanguageReviewError("语言审查配置格式无效")
    return value,_hash_bytes(data)


def _call_evidence(call: Mapping[str,Any]) -> dict[str,Any]:
    return {key:call.get(key) for key in ("route_decision_ref","work_order_ref",
        "result_envelope_ref","invocation_ref","cost_micros","replayed",
        "stage_output_normalization")}


def _sealed(value: Mapping[str,Any]) -> dict[str,Any]:
    row=dict(value);row["content_hash"]=content_hash(row);return row


def _read_sealed(path: Path) -> tuple[dict[str,Any],bytes]:
    payload=path.read_bytes();value=json.loads(payload)
    if (not isinstance(value,dict) or value.get("content_hash") !=
            content_hash({k:v for k,v in value.items() if k!="content_hash"})):
        raise ResearchLanguageReviewError("既有语言审查记录不完整或身份不匹配")
    return value,payload


def _record_failure(root: Path | None, key: str, stage: str,
                    value: Mapping[str,Any]) -> dict[str,Any]:
    if root is None:return dict(value)
    directory=root/"failures";directory.mkdir(parents=True,exist_ok=True)
    payload=(canonical_json(_sealed(value))+"\n").encode()
    for number in range(10_000):
        path=directory/f"{key}.{stage}.{number:04d}.json"
        try:
            _write_once(path,payload)
            if value.get("suggestions_markdown") is not None:
                _write_once(path.with_suffix(".md"),str(value["suggestions_markdown"]).encode())
            return {**dict(value),"artifact_ref":path.relative_to(root).with_suffix("").as_posix(),
                    "artifact_sha256":_hash_bytes(payload)}
        except ResearchLanguageReviewError:continue
    raise ResearchLanguageReviewError("语言审查失败记录数量超出限制")


def run(product: Mapping[str, Any], *, mission: Mapping[str, Any], request_id: str,
        checker_config: Path, brain_config: Path, verifier_config: Path, scheduler_db: Path,
        producer_route_decision_ref: str, artifact_dir: Path | None=None,
        model_factory: Callable[[Mapping[str, Any]], Any] | None=None,
        brain_recovery: Mapping[str, Any] | None=None) -> dict[str, Any]:
    """Resume completed stages and persist a final closed receipt after fidelity."""
    checker_raw,checker_hash=_config(checker_config)
    brain_raw,brain_hash=_config(brain_config)
    verifier_raw,verifier_hash=_config(verifier_config)
    source_hash=sha256((canonical_json(product)+"\n").encode()).hexdigest()
    proof_key=sha256((request_id+":"+source_hash).encode()).hexdigest()[:24]
    proof_base=None if artifact_dir is None else artifact_dir/proof_key
    identities={"source_hash":source_hash,"checker_config_sha256":checker_hash,
                "brain_config_sha256":brain_hash,"verifier_config_sha256":verifier_hash,
                "producer_route_decision_ref":producer_route_decision_ref}
    final_path=None if proof_base is None else proof_base.with_suffix(".json")
    identity_final=(None if proof_base is None else proof_base.with_name(
        proof_base.name+".final-"+content_hash(identities)[:24]+".json"))
    replay_path=identity_final
    if replay_path is not None and not replay_path.exists():replay_path=final_path
    if replay_path is not None and replay_path.exists():
        prior,payload=_read_sealed(replay_path)
        if prior.get("status")=="ready_for_publication" and prior.get("runtime_binding")==identities:
            runtime=prior.get("runtime_identity") or {};calls=prior.get("call_evidence") or {}
            for label,cfg in (("checker",checker_raw),("brain",brain_raw),("fidelity",verifier_raw)):
                if _route_identity(cfg,calls[label]) != runtime[label]:
                    raise ResearchLanguageReviewError("既有语言审查模型身份无法重放")
            replay=_independence(verifier_raw,draft_routes=[producer_route_decision_ref,
                calls["brain"]["route_decision_ref"]],verifier_route=calls["fidelity"]["route_decision_ref"])
            if replay.get("independent") is not True:
                raise ResearchLanguageReviewError("既有事实保真核验独立性无法重放")
            return {**prior,"artifact_ref":replay_path.relative_to(artifact_dir).with_suffix("").as_posix(),"artifact_sha256":_hash_bytes(payload),
                    "artifact_replayed":True}
        # Old pending receipts remain immutable evidence but no longer block a
        # new staged attempt; their result is never promoted into a stage.
    def default_factory(cfg: Mapping[str,Any]):
        output=16_000 if cfg is brain_raw else 12_000
        return CockpitModel(cfg,scheduler_db=scheduler_db,max_input_tokens=120_000,
                            max_output_tokens=output,
                            timeout_seconds=600,max_cost_usd=1.0)
    make=model_factory or default_factory
    calls: dict[str,dict[str,Any]]={}
    def invoke(label: str, purpose: str, cfg: Mapping[str,Any], prompt: str) -> Mapping[str,Any]:
        call=make(cfg).call(purpose=purpose,request_id=f"{request_id}:{label}",prompt=prompt,mission=mission)
        calls[label]=dict(call)
        if label in {"checker", "brain"}:
            parsed, normalization = parse_stage_output_with_proof(call["text"], stage=label)
            calls[label]["stage_output_normalization"] = normalization
        else:
            parsed = unwrap_json_object(call["text"])
        if not isinstance(parsed,Mapping): raise ResearchLanguageReviewError("语言审查模型没有返回有效结构")
        return parsed
    checker_binding={"source_hash":source_hash,"checker_config_sha256":checker_hash}
    checker_path=(None if proof_base is None else proof_base.with_name(
        proof_base.name+".checker-"+content_hash(checker_binding)[:24]+".json"))
    if checker_path is not None and checker_path.exists():
        checker_stage,_=_read_sealed(checker_path)
        if checker_stage.get("binding")!=checker_binding:raise ResearchLanguageReviewError("语言检查缓存身份不匹配")
        checker_value=checker_stage["value"];checker_call=checker_stage["call_evidence"]
        checker_identity=_route_identity(checker_raw,checker_call)
        if checker_identity!=checker_stage["route_identity"]:raise ResearchLanguageReviewError("语言检查缓存路由无法重放")
    else:
        try: checker_value=invoke("checker",CHECKER_PURPOSE,checker_raw,
                                  build_checker_prompt(product))
        except Exception as exc:
            _record_failure(artifact_dir,proof_key,"checker",{"status":"pending_language_review","binding":checker_binding,"error_type":type(exc).__name__})
            raise
        checker_call=_call_evidence(calls["checker"]);checker_identity=_route_identity(checker_raw,checker_call)
        if checker_identity["provider"]!="antigravity-cli-gateway" or checker_identity["model"]!="gemini-3.8-flash":
            raise ResearchLanguageReviewError("语言检查模型身份不符合发布要求")
        checker_stage=_sealed({"schema_version":"research-language-checker-stage:0.1","binding":checker_binding,
            "value":checker_value,"call_evidence":checker_call,"route_identity":checker_identity})
        if checker_path is not None:_write_once(checker_path,(canonical_json(checker_stage)+"\n").encode())
    style_binding={**checker_binding,"brain_config_sha256":brain_hash,
                   "checker_stage_sha256":content_hash(checker_stage)}
    style_path=(None if proof_base is None else proof_base.with_name(
        proof_base.name+".style-"+content_hash(style_binding)[:24]+".json"))
    brain_terminal_path=(None if proof_base is None else
        proof_base.with_name(proof_base.name+".brain-terminal-"+
                             content_hash(style_binding)[:24]+".json"))
    recovered_brain = None
    recovered_brain_proof = None
    recovered_brain_call = None
    if brain_terminal_path is not None and brain_terminal_path.exists():
        terminal,_=_read_sealed(brain_terminal_path)
        if terminal.get("binding")!=style_binding:
            raise ResearchLanguageReviewError("语言修订失败记录身份不匹配")
        if brain_recovery is None:
            return terminal["review"]
        recovered_brain_call = terminal.get("call_evidence")
        required = {"result_envelope_ref", "raw_sha256"}
        if not isinstance(brain_recovery, Mapping) or set(brain_recovery) != required:
            raise ResearchLanguageReviewError("语言修订恢复输入格式无效")
        if (not isinstance(recovered_brain_call, Mapping)
                or brain_recovery["result_envelope_ref"] != recovered_brain_call.get("result_envelope_ref")):
            raise ResearchLanguageReviewError("语言修订恢复结果身份不匹配")
        envelope_ref = brain_recovery["result_envelope_ref"]
        try:
            database = sqlite3.connect(f"file:{Path(scheduler_db).resolve()}?mode=ro", uri=True)
            database.execute("PRAGMA query_only=ON"); database.execute("BEGIN")
            row = database.execute(
                "SELECT result_envelope_hash,result_envelope_json,outcome,work_order_id "
                "FROM scheduler_result_envelopes WHERE result_envelope_id=?", (envelope_ref,)).fetchone()
            database.rollback(); database.close()
        except sqlite3.Error as exc:
            raise ResearchLanguageReviewError("语言修订恢复无法读取正式结果") from exc
        if row is None or row[2] != "succeeded" or row[3] != recovered_brain_call.get("work_order_ref"):
            raise ResearchLanguageReviewError("语言修订恢复正式结果身份不匹配")
        envelope_json = row[1]
        if _hash_bytes(envelope_json.encode()) != row[0]:
            raise ResearchLanguageReviewError("语言修订恢复正式结果哈希不匹配")
        envelope = json.loads(envelope_json)
        raw_text = (envelope.get("outputs") or {}).get("text")
        raw_hash = (envelope.get("outputs") or {}).get("content_hash")
        if (not isinstance(raw_text, str) or not isinstance(brain_recovery["raw_sha256"], str)
                or _hash_bytes(raw_text.encode()) != brain_recovery["raw_sha256"]
                or raw_hash != brain_recovery["raw_sha256"]):
            raise ResearchLanguageReviewError("语言修订恢复原文哈希不匹配")
        recovered_brain, recovered_brain_proof = parse_stage_output_with_proof(raw_text, stage="brain")
        if recovered_brain_proof["mode"] != "eof_container_closure":
            raise ResearchLanguageReviewError("语言修订恢复只接受EOF容器闭合")
    if style_path is not None and style_path.exists():
        style_stage,_=_read_sealed(style_path)
        if style_stage.get("binding")!=style_binding:raise ResearchLanguageReviewError("语言修订缓存身份不匹配")
        result=style_stage["review"];brain_call=style_stage["call_evidence"]
        brain_identity=_route_identity(brain_raw,brain_call)
        if brain_identity!=style_stage["route_identity"]:raise ResearchLanguageReviewError("语言修订缓存路由无法重放")
    else:
        try:
            result=run_language_review(product,checker=lambda _:checker_value,
                brain=((lambda prompt: recovered_brain) if recovered_brain is not None else
                       (lambda prompt:invoke("brain",BRAIN_PURPOSE,brain_raw,prompt))),
                checker_identity={"provider":"antigravity-cli-gateway","model":"antigravity-cli-gateway/gemini-3.8-flash"})
        except Exception as exc:
            _record_failure(artifact_dir,proof_key,"brain",{"status":"pending_brain_revision","binding":style_binding,"error_type":type(exc).__name__});raise
        if result["status"]!="ready_for_publication":
            evidence={"checker":checker_call}
            if "brain" in calls:evidence["brain"]=_call_evidence(calls["brain"])
            pending=_record_failure(artifact_dir,proof_key,"brain",{**result,
                "binding":style_binding,"runtime_identity":{"checker":checker_identity},
                "call_evidence":evidence,
                "review_cost_micros":sum(int(row.get("cost_micros") or 0)
                                         for row in evidence.values())})
            # A returned brain envelope followed by structural rejection is a
            # completed one-time revision, not transient infrastructure work.
            if "brain" in calls and brain_terminal_path is not None:
                terminal=_sealed({"schema_version":"research-language-brain-terminal:0.1",
                    "binding":style_binding,"review":pending,
                    "call_evidence":_call_evidence(calls["brain"])})
                _write_once(brain_terminal_path,(canonical_json(terminal)+"\n").encode())
            return pending
        brain_call=(dict(recovered_brain_call) if recovered_brain_call is not None
                    else _call_evidence(calls["brain"]))
        if recovered_brain_proof is not None:
            brain_call["stage_output_normalization"] = recovered_brain_proof
        brain_identity=_route_identity(brain_raw,brain_call)
        style_stage=_sealed({"schema_version":"research-language-style-stage:0.1","binding":style_binding,
            "review":result,"call_evidence":brain_call,"route_identity":brain_identity,
            **({"recovery": {"kind": "eof_container_closure",
                "terminal_sha256": _hash_bytes(brain_terminal_path.read_bytes()),
                "normalization": recovered_brain_proof}}
               if recovered_brain_proof is not None else {})})
        if style_path is not None:_write_once(style_path,(canonical_json(style_stage)+"\n").encode())
    semantic_binding={"style_stage_sha256":content_hash(style_stage),
        "verifier_config_sha256":verifier_hash,"producer_route_decision_ref":producer_route_decision_ref}
    semantic_attempt=(0 if artifact_dir is None else
        len(list((artifact_dir/"failures").glob(f"{proof_key}.fidelity.*.json")))
        if (artifact_dir/"failures").is_dir() else 0)
    try:
        verifier_model=make(verifier_raw)
        verifier_call=independent_model_call(verifier_model,producer_route_decision_refs=[producer_route_decision_ref,brain_call["route_decision_ref"]],purpose=FIDELITY_PURPOSE,request_id=f"{request_id}:fidelity:{content_hash(semantic_binding)[:16]}:{semantic_attempt}",prompt=build_verifier_prompt(product,{"sections":result["brain_revision"]["sections"]}),mission=mission)
        calls["fidelity"]=dict(verifier_call);verdict=unwrap_json_object(verifier_call["text"])
        expected={"verdict","faithful","no_new_facts","meaning_preserved","findings"}
        valid=(isinstance(verdict,Mapping) and set(verdict)==expected and verdict.get("verdict")=="pass"
               and all(verdict.get(k) is True for k in ("faithful","no_new_facts","meaning_preserved")) and verdict.get("findings")==[])
        route=_route_identity(verifier_raw,verifier_call)
        replay=_independence(verifier_raw,draft_routes=[producer_route_decision_ref,brain_call["route_decision_ref"]],verifier_route=verifier_call["route_decision_ref"])
        result["fidelity_verification"]={**(dict(verdict) if isinstance(verdict,Mapping) else {"raw_structure_valid":False}),"route":route,"independence":replay}
        if not valid or replay.get("independent") is not True:raise ResearchLanguageReviewError("独立事实保真核验未通过")
    except Exception as exc:
        pending={**result,"status":"pending_fidelity_review","pending_reason":"独立事实保真核验未完成",
                 "fidelity_verification":result.get("fidelity_verification",{"error_type":type(exc).__name__}),
                 "runtime_binding":identities,"runtime_identity":{"checker":checker_identity,
                 "brain":brain_identity},"call_evidence":{"checker":checker_call,"brain":brain_call}}
        if "fidelity" in calls:
            pending["fidelity_call_evidence"]=_call_evidence(calls["fidelity"])
            pending["call_evidence"]["fidelity"]=pending["fidelity_call_evidence"]
        pending["review_cost_micros"]=sum(int(row.get("cost_micros") or 0)
                                          for row in pending["call_evidence"].values())
        return _record_failure(artifact_dir,proof_key,"fidelity",pending)
    result["runtime_binding"]=identities
    result["runtime_identity"]={"checker":checker_identity,"brain":brain_identity,"fidelity":route}
    calls={"checker":checker_call,"brain":brain_call,"fidelity":_call_evidence(calls["fidelity"])}
    result["call_evidence"]=calls
    if style_stage.get("recovery") is not None:
        result["brain_recovery"]={**style_stage["recovery"],
            "checker_stage_sha256":content_hash(checker_stage),
            "brain_result_envelope_ref":brain_call["result_envelope_ref"]}
    result["review_cost_micros"]=sum(int(v.get("cost_micros") or 0) for v in calls.values())
    result["replayed"]=bool(calls) and all(v.get("replayed") is True for v in calls.values())
    result["content_hash"]=content_hash({k:v for k,v in result.items() if k!="content_hash"})
    if artifact_dir is not None:
        base=proof_base
        if result.get("suggestions_markdown") is not None:
            _write_once(base.with_suffix(".md"),result["suggestions_markdown"].encode())
        payload=(canonical_json(result)+"\n").encode()
        target=base.with_suffix(".json")
        if target.exists(): target=identity_final
        _write_once(target,payload)
        base=target.with_suffix("")
        result={**result,"artifact_ref":base.name,"artifact_sha256":_hash_bytes(payload)}
    return result
