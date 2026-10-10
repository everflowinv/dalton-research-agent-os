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
    BRAIN_PURPOSE, CHECKER_MODEL, CHECKER_PROVIDER, CHECKER_PURPOSE, ResearchLanguageReviewError,
    build_checker_prompt, parse_stage_output_with_proof, run_language_review,
)
from .store import canonical_json, content_hash
from .research_publication_limits import GovernedPublicationModel, publication_scope, cache_binding
from .research_localization import source_content_hash,localization_blockers
from .research_source_projection import publication_source_hash
from .research_publication_repair import local_repair_prompt,expand_local_patch,local_patch_quotes

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
    root = artifact_dir or Path(scheduler_db).parent/'publication-language-work'
    blockers=localization_blockers(product)
    if blockers:return {'status':'skipped','pending_reason':'publication_source_substance_failed','blockers':blockers}
    with publication_scope(root/'publication-limits.sqlite3',publication_source_hash(product)):
        return _run(product,mission=mission,request_id=request_id,checker_config=checker_config,
                    brain_config=brain_config,verifier_config=verifier_config,scheduler_db=scheduler_db,
                    producer_route_decision_ref=producer_route_decision_ref,artifact_dir=artifact_dir,
                    model_factory=model_factory,brain_recovery=brain_recovery)


def _run(product: Mapping[str, Any], *, mission: Mapping[str, Any], request_id: str,
        checker_config: Path, brain_config: Path, verifier_config: Path, scheduler_db: Path,
        producer_route_decision_ref: str, artifact_dir: Path | None=None,
        model_factory: Callable[[Mapping[str, Any]], Any] | None=None,
        brain_recovery: Mapping[str, Any] | None=None) -> dict[str, Any]:
    """Resume completed stages and persist a final closed receipt after fidelity."""
    checker_raw,checker_hash=_config(checker_config)
    brain_raw,brain_hash=_config(brain_config)
    verifier_raw,verifier_hash=_config(verifier_config)
    source_hash=sha256((canonical_json(product)+"\n").encode()).hexdigest()
    proof_key=content_hash(cache_binding(product))[:24]
    proof_base=None if artifact_dir is None else artifact_dir/proof_key
    identities={"source_hash":source_hash,"checker_config_sha256":checker_hash,
                "brain_config_sha256":brain_hash,"verifier_config_sha256":verifier_hash,
                "producer_route_decision_ref":producer_route_decision_ref,"cache_binding":cache_binding(product)}
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
            replay=_independence(verifier_raw,draft_routes=prior.get('authored_route_refs') or [producer_route_decision_ref,
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
    factory=model_factory or default_factory
    make=lambda cfg:GovernedPublicationModel(factory(cfg))
    calls: dict[str,dict[str,Any]]={}
    def invoke(label: str, purpose: str, cfg: Mapping[str,Any], prompt: str) -> Mapping[str,Any]:
        call=make(cfg).call(purpose=purpose,request_id=f"{proof_key}:{label}:{content_hash(cfg)[:12]}",prompt=prompt,mission=mission)
        calls[label]=dict(call)
        if label in {"checker", "brain"}:
            parsed, normalization = parse_stage_output_with_proof(call["text"], stage=label)
            calls[label]["stage_output_normalization"] = normalization
        else:
            parsed = unwrap_json_object(call["text"])
        if not isinstance(parsed,Mapping): raise ResearchLanguageReviewError("语言审查模型没有返回有效结构")
        return parsed
    checker_binding={**cache_binding(product),"checker_config_sha256":checker_hash}
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
    terminal_path=(None if proof_base is None else proof_base.with_name(
        proof_base.name+'.fidelity-terminal-'+content_hash(semantic_binding)[:24]+'.json'))
    if terminal_path is not None and terminal_path.exists():
        terminal,_=_read_sealed(terminal_path)
        if terminal['binding']!=semantic_binding:raise ResearchLanguageReviewError('核验拒绝证据绑定漂移')
        return {**terminal['pending'],'artifact_replayed':True}
    semantic_attempt=(0 if artifact_dir is None else
        len(list((artifact_dir/"failures").glob(f"{proof_key}.fidelity.*.json")))
        if (artifact_dir/"failures").is_dir() else 0)
    authored_routes=[producer_route_decision_ref,brain_call['route_decision_ref']]
    review_history=[]
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
        if replay.get('independent') is not True:raise ResearchLanguageReviewError('独立事实保真核验模型不独立')
        if not valid:
            failure={'stage':'semantic','verifier':dict(verdict),'reason':'独立事实保真核验拒绝'}
            candidate=result['brain_revision']['sections']
            decisions=result['brain_revision']['decisions']
            repair_prompt,indices=local_repair_prompt(product,candidate,failure,decisions=decisions)
            repair_binding={'semantic_binding':semantic_binding,'feedback':verdict,'indices':indices}
            repair_path=(None if proof_base is None else proof_base.with_name(
                proof_base.name+'.repair-'+content_hash(repair_binding)[:24]+'.json'))
            if repair_path is not None and repair_path.exists():
                repair_stage,_=_read_sealed(repair_path)
                if repair_stage['binding']!=repair_binding:raise ResearchLanguageReviewError('局部修复缓存不匹配')
                repair_call=repair_stage['call_evidence']
            else:
                repair_call=make(brain_raw).call(purpose=BRAIN_PURPOSE,
                    request_id=proof_key+':local-repair:'+content_hash(repair_binding)[:16],prompt=repair_prompt,mission=mission)
                repair_stage=_sealed({'binding':repair_binding,'call_evidence':dict(repair_call)})
                if repair_path is not None:_write_once(repair_path,(canonical_json(repair_stage)+'\n').encode())
            patch,_=parse_stage_output_with_proof(repair_call['text'],stage='brain')
            expanded=expand_local_patch(patch,candidate=candidate,indices=indices,decisions=decisions,
                quotes=local_patch_quotes(candidate,failure,indices))
            repaired=run_language_review(product,checker=lambda _:checker_value,brain=lambda _:expanded,
                checker_identity={'provider':CHECKER_PROVIDER,'model':CHECKER_MODEL})
            if repaired.get('status')!='ready_for_publication':raise ResearchLanguageReviewError(repaired.get('reason') or '局部修复未通过')
            review_history=[{'stage':'semantic_rejection','verifier':dict(verdict),'call_evidence':_call_evidence(verifier_call)},
                            {'stage':'original_revision','call_evidence':brain_call}]
            brain_call=_call_evidence(repair_call);brain_identity=_route_identity(brain_raw,brain_call)
            authored_routes.append(brain_call['route_decision_ref'])
            result=repaired
            verifier_call=independent_model_call(make(verifier_raw),producer_route_decision_refs=authored_routes,
                purpose=FIDELITY_PURPOSE,request_id=proof_key+':fidelity-repair:'+content_hash(repair_binding)[:16],
                prompt=build_verifier_prompt(product,{'sections':expanded['sections']}),mission=mission)
            calls['fidelity']=dict(verifier_call);verdict=unwrap_json_object(verifier_call['text'])
            route=_route_identity(verifier_raw,verifier_call)
            replay=_independence(verifier_raw,draft_routes=authored_routes,verifier_route=verifier_call['route_decision_ref'])
            valid=(isinstance(verdict,Mapping) and set(verdict)==expected and verdict.get('verdict')=='pass'
                   and all(verdict.get(k) is True for k in ('faithful','no_new_facts','meaning_preserved')) and verdict.get('findings')==[])
            result['fidelity_verification']={**dict(verdict),'route':route,'independence':replay}
            if not valid or replay.get('independent') is not True:raise ResearchLanguageReviewError('两轮语言修订后事实保真核验仍未通过')
    except Exception as exc:
        pending={**result,"status":"pending_fidelity_review","pending_reason":str(exc),
                 "fidelity_verification":result.get("fidelity_verification",{"error_type":type(exc).__name__}),
                 "runtime_binding":identities,"runtime_identity":{"checker":checker_identity,
                 "brain":brain_identity},"call_evidence":{"checker":checker_call,"brain":brain_call}}
        if "fidelity" in calls:
            pending["fidelity_call_evidence"]=_call_evidence(calls["fidelity"])
            pending["call_evidence"]["fidelity"]=pending["fidelity_call_evidence"]
        pending["review_cost_micros"]=sum(int(row.get("cost_micros") or 0)
                                          for row in pending["call_evidence"].values())
        recorded=_record_failure(artifact_dir,proof_key,"fidelity",pending)
        if terminal_path is not None and 'fidelity' in calls:
            terminal=_sealed({'binding':semantic_binding,'pending':recorded})
            _write_once(terminal_path,(canonical_json(terminal)+'\n').encode())
        return recorded
    result["runtime_binding"]=identities
    result['authored_route_refs']=authored_routes
    if review_history:result['review_history']=review_history
    result["runtime_identity"]={"checker":checker_identity,"brain":brain_identity,"fidelity":route}
    calls={"checker":checker_call,"brain":brain_call,"fidelity":_call_evidence(calls["fidelity"])}
    result["call_evidence"]=calls
    if style_stage.get("recovery") is not None:
        result["brain_recovery"]={**style_stage["recovery"],
            "checker_stage_sha256":content_hash(checker_stage),
            "brain_result_envelope_ref":brain_call["result_envelope_ref"]}
    result["review_cost_micros"]=sum(int(v.get("cost_micros") or 0) for v in calls.values())+sum(
        int(row['call_evidence'].get('cost_micros') or 0) for row in review_history)
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
