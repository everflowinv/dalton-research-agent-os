"""Budgeted publication-language review using two independently configured routes."""
from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Mapping

from .cockpit_model import CockpitModel, unwrap_json_object
from .model_router import ModelRouter
from .research_language_review import (
    BRAIN_PURPOSE, CHECKER_MODEL, CHECKER_PURPOSE, ResearchLanguageReviewError,
    run_language_review,
)
from .store import canonical_json


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
        checker_config: Path, brain_config: Path, scheduler_db: Path,
        artifact_dir: Path | None=None,
        model_factory: Callable[[Mapping[str, Any]], Any] | None=None) -> dict[str, Any]:
    """Run exactly one checker and one brain call, persisting a closed receipt."""
    checker_raw=_load(checker_config); brain_raw=_load(brain_config)
    make=model_factory or (lambda cfg:CockpitModel(cfg,scheduler_db=scheduler_db))
    calls: dict[str,dict[str,Any]]={}
    def invoke(label: str, purpose: str, cfg: Mapping[str,Any], prompt: str) -> Mapping[str,Any]:
        call=make(cfg).call(purpose=purpose,request_id=f"{request_id}:{label}",prompt=prompt,mission=mission)
        calls[label]=dict(call)
        parsed=unwrap_json_object(call["text"])
        if not isinstance(parsed,Mapping): raise ResearchLanguageReviewError("语言审查模型没有返回有效结构")
        return parsed
    # Identity passed into the pure review is validated again from persisted router records below.
    result=run_language_review(product,
        checker=lambda prompt:invoke("checker",CHECKER_PURPOSE,checker_raw,prompt),
        brain=lambda prompt:invoke("brain",BRAIN_PURPOSE,brain_raw,prompt),
        checker_identity={"provider":"antigravity-cli-gateway","model":"antigravity-cli-gateway/gemini-3.8-flash"})
    if result["status"]=="ready_for_publication":
        checker_identity=_route_identity(checker_raw,calls["checker"])
        if checker_identity["provider"]!="antigravity-cli-gateway" or checker_identity["model"]!="gemini-3.8-flash":
            raise ResearchLanguageReviewError("语言检查模型身份不符合发布要求")
        result["runtime_identity"]={"checker":checker_identity,"brain":_route_identity(brain_raw,calls["brain"])}
        result["call_evidence"]={k:{x:v.get(x) for x in ("work_order_ref","result_envelope_ref","invocation_ref","cost_micros","replayed")} for k,v in calls.items()}
    if artifact_dir is not None:
        key=sha256((request_id+":"+result["source_hash"]).encode()).hexdigest()[:24]
        base=artifact_dir/key
        if result.get("suggestions_markdown") is not None:
            _write_once(base.with_suffix(".md"),result["suggestions_markdown"].encode())
        payload=(canonical_json(result)+"\n").encode();_write_once(base.with_suffix(".json"),payload)
        result={**result,"artifact_ref":base.name,"artifact_sha256":_hash_bytes(payload)}
    return result
