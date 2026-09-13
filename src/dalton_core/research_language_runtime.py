"""Budgeted publication-language review using two independently configured routes."""
from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Mapping

from .cockpit_model import CockpitModel, independent_model_call, unwrap_json_object
from .model_router import ModelRouter
from .model_fallback_chain import TIER_BRAIN, TIER_CHEAP, TIER_VERIFIER, register_purpose_tier
from .research_localization import build_verifier_prompt
from .research_language_review import (
    BRAIN_PURPOSE, CHECKER_MODEL, CHECKER_PURPOSE, ResearchLanguageReviewError,
    run_language_review,
)
from .store import canonical_json, content_hash

register_purpose_tier(CHECKER_PURPOSE, TIER_CHEAP)
register_purpose_tier(BRAIN_PURPOSE, TIER_BRAIN)
FIDELITY_PURPOSE=register_purpose_tier("research_localization_verifier",TIER_VERIFIER)


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
            "provider":str(profile["provider"]),"model":str(profile["model"])}


def _write_once(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        if path.read_bytes() != data:
            raise ResearchLanguageReviewError("语言审查证据文件已存在但内容不同")
        return
    with os.fdopen(fd,"wb") as stream: stream.write(data)


def run(product: Mapping[str, Any], *, mission: Mapping[str, Any], request_id: str,
        checker_config: Path, brain_config: Path, verifier_config: Path, scheduler_db: Path,
        producer_route_decision_ref: str, artifact_dir: Path | None=None,
        model_factory: Callable[[Mapping[str, Any]], Any] | None=None) -> dict[str, Any]:
    """Run exactly one checker and one brain call, persisting a closed receipt."""
    checker_raw=_load(checker_config); brain_raw=_load(brain_config); verifier_raw=_load(verifier_config)
    proof_key=sha256((request_id+":"+sha256((canonical_json(product)+"\n").encode()).hexdigest()).encode()).hexdigest()[:24]
    proof_base=None if artifact_dir is None else artifact_dir/proof_key
    if proof_base is not None and proof_base.with_suffix(".json").exists():
        payload=proof_base.with_suffix(".json").read_bytes(); prior=json.loads(payload)
        if (prior.get("status")!="ready_for_publication" or not isinstance(prior.get("fidelity_verification"),dict)
                or prior["fidelity_verification"].get("verdict")!="pass"
                or prior.get("content_hash")!=content_hash({k:v for k,v in prior.items() if k!="content_hash"})):
            raise ResearchLanguageReviewError("既有语言审查记录不完整或身份不匹配")
        return {**prior,"artifact_ref":proof_base.name,"artifact_sha256":_hash_bytes(payload)}
    def default_factory(cfg: Mapping[str,Any]):
        checker=cfg is checker_raw
        output=12_000 if checker else 16_000
        return CockpitModel(cfg,scheduler_db=scheduler_db,max_input_tokens=120_000,
                            max_output_tokens=output,
                            timeout_seconds=600,max_cost_usd=1.0)
    make=model_factory or default_factory
    calls: dict[str,dict[str,Any]]={}
    def invoke(label: str, purpose: str, cfg: Mapping[str,Any], prompt: str) -> Mapping[str,Any]:
        call=make(cfg).call(purpose=purpose,request_id=f"{request_id}:{label}",prompt=prompt,mission=mission)
        calls[label]=dict(call)
        parsed=unwrap_json_object(call["text"])
        if not isinstance(parsed,Mapping): raise ResearchLanguageReviewError("语言审查模型没有返回有效结构")
        return parsed
    # Identity passed into the pure review is validated again from persisted router records below.
    identity_failure: list[Exception] = []
    def brain_after_checker(prompt: str) -> Mapping[str,Any]:
        identity=_route_identity(checker_raw,calls["checker"])
        if identity["provider"]!="antigravity-cli-gateway" or identity["model"]!="gemini-3.8-flash":
            error=ResearchLanguageReviewError("语言检查模型身份不符合发布要求"); identity_failure.append(error); raise error
        return invoke("brain",BRAIN_PURPOSE,brain_raw,prompt)
    result=run_language_review(product,
        checker=lambda prompt:invoke("checker",CHECKER_PURPOSE,checker_raw,prompt),
        brain=brain_after_checker,
        checker_identity={"provider":"antigravity-cli-gateway","model":"antigravity-cli-gateway/gemini-3.8-flash"})
    if identity_failure: raise identity_failure[0]
    if result["status"]=="ready_for_publication":
        checker_identity=_route_identity(checker_raw,calls["checker"])
        if checker_identity["provider"]!="antigravity-cli-gateway" or checker_identity["model"]!="gemini-3.8-flash":
            raise ResearchLanguageReviewError("语言检查模型身份不符合发布要求")
        brain_identity=_route_identity(brain_raw,calls["brain"])
        verifier_model=make(verifier_raw)
        verifier_call=independent_model_call(verifier_model,producer_route_decision_refs=[producer_route_decision_ref,calls["brain"]["route_decision_ref"]],purpose=FIDELITY_PURPOSE,request_id=f"{request_id}:fidelity",prompt=build_verifier_prompt(product,{"sections":result["brain_revision"]["sections"]}),mission=mission)
        calls["fidelity"]=dict(verifier_call)
        verdict=unwrap_json_object(verifier_call["text"])
        expected={"verdict","faithful","no_new_facts","meaning_preserved","findings"}
        if not isinstance(verdict,Mapping) or set(verdict)!=expected or verdict.get("verdict")!="pass" or any(verdict.get(k) is not True for k in ("faithful","no_new_facts","meaning_preserved")) or not isinstance(verdict.get("findings"),list):
            raise ResearchLanguageReviewError("独立事实保真核验未通过")
        result["fidelity_verification"]={**dict(verdict),"route":_route_identity(verifier_raw,verifier_call)}
        result["runtime_identity"]={"checker":checker_identity,"brain":brain_identity,"fidelity":result["fidelity_verification"]["route"]}
        result["call_evidence"]={k:{x:v.get(x) for x in ("work_order_ref","result_envelope_ref","invocation_ref","cost_micros","replayed")} for k,v in calls.items()}
        result["content_hash"]=content_hash({k:v for k,v in result.items() if k!="content_hash"})
    if artifact_dir is not None:
        base=proof_base
        if result.get("suggestions_markdown") is not None:
            _write_once(base.with_suffix(".md"),result["suggestions_markdown"].encode())
        payload=(canonical_json(result)+"\n").encode();_write_once(base.with_suffix(".json"),payload)
        result={**result,"artifact_ref":base.name,"artifact_sha256":_hash_bytes(payload)}
    return result
