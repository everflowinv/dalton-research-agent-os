"""New scenario contracts: observable partitions, source-bound probabilities and model inputs."""
from __future__ import annotations
import copy
import re
from datetime import date
from decimal import Decimal,InvalidOperation
from typing import Mapping,Any,Sequence
from .scenario_valuation import after_year

RULE_REF="rule:scenario-partition-12m:1"
CIRCULAR=re.compile(r"THESIS_[A-Z_]+|研判(?:加强|减弱|强化|弱化)|论点(?:加强|减弱|强化|弱化)")


def _number(value):
    try: out=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError) as exc: raise ValueError("scenario threshold/probability must be numeric") from exc
    if not out.is_finite(): raise ValueError("scenario threshold/probability must be finite")
    return out


def validate_probability_basis(value,probability):
    if not isinstance(value,Mapping) or set(value)!={"kind","because","source_refs","as_of"}:
        raise ValueError("scenario probability basis needs kind, because, source_refs and date")
    if value["kind"] not in {"template_prior","evidence_assessment","owner_assessment"} or not value["because"] or not value["source_refs"]:
        raise ValueError("probability basis must name its source and judgement")
    date.fromisoformat(value["as_of"])
    return copy.deepcopy(dict(value))


def validate_new_scenario_rules(scenarios,anchors,*,today):
    if not anchors.get("model_version_ref") or not anchors.get("model_version_hash") or not anchors.get("valuation_snapshot_ref"):
        raise ValueError("no model or valuation: scenario proposal is unavailable")
    if len(scenarios)!=3 or {s.get("key") for s in scenarios}!={"bear","base","bull"}:
        raise ValueError("new scenario set requires mutually exclusive bear/base/bull")
    partition=None
    for s in scenarios:
        if CIRCULAR.search(str(s.get("narrative") or "")):
            raise ValueError("scenario condition cannot refer to the system's own judgement")
        basis=validate_probability_basis(s.get("probability_basis"),s.get("probability"))
        if "driver_deltas" not in s or not isinstance(s["driver_deltas"],list):
            raise ValueError("scenario needs executable driver deltas, including an explicit empty baseline")
        for delta in s["driver_deltas"]:
            if set(delta)!={"driver_ref","mode","delta","because","refs"} or not delta["refs"] or not delta["because"]:
                raise ValueError("scenario driver delta must bind its mechanism and sources")
            if delta["mode"] not in {"relative_level","rate_shift"}: raise ValueError("driver delta mode is unsupported")
            _number(delta["delta"])
        p=s.get("partition")
        fields={"observed_by","metric_ref","transform","cutpoints","source_refs"}
        if not isinstance(p,Mapping) or set(p)!=fields or p["observed_by"] not in {"sec_actual","company_fcf","claim"} or not p["source_refs"]:
            raise ValueError("scenario partition must name an observable economic metric and one shared cutpoint set")
        if p["observed_by"]=="company_fcf" and p["metric_ref"]!="fcf:company":
            raise ValueError("cash conditions must use the company-defined FCF")
        if any("free_cash_flow" in str(t.get("metric_ref")) and t.get("metric_ref")!="fcf:company" for t in s.get("triggers") or []):
            raise ValueError("cash conditions cannot substitute system FCF for company FCF")
        if p["transform"] not in {"level","yoy_pct","qoq_pct"}: raise ValueError("scenario partition transform is unsupported")
        if len(p["cutpoints"])!=2 or not _number(p["cutpoints"][0])<_number(p["cutpoints"][1]):
            raise ValueError("scenario cutpoints must be two ordered distinct values")
        if partition is not None and partition!=p: raise ValueError("scenario partitions differ, leaving overlap or gaps")
        partition=p
        economic=[]
        for t in list(s.get("triggers") or [])+list(s.get("invalidators") or []):
            if t.get("observed_by")=="event_judgement" or CIRCULAR.search(str(t)):
                raise ValueError("THESIS_* or system judgement cannot be a scenario condition")
            if t.get("horizon")!=after_year(today.isoformat()): raise ValueError("scenario assessment horizon must be exactly twelve months")
            if t.get("observed_by")=="owner":
                rule=t.get("owner_rule")
                if not isinstance(rule,Mapping) or set(rule)!={"source_ref","as_of","rule"} or not all(rule.values()):
                    raise ValueError("owner qualitative condition needs source, time and adjudication rule")
                date.fromisoformat(rule["as_of"])
            if t in (s.get("triggers") or []) and t.get("observed_by")==p["observed_by"] and t.get("metric_ref")==p["metric_ref"]:
                economic.append((t["comparator"],_number(t["threshold"]),t.get("transform") or "level"))
        low,high=map(_number,p["cutpoints"]);transform=p["transform"]
        expected={"bear":[("lt",low,transform)],"base":[("gte",low,transform),("lt",high,transform)],"bull":[("gte",high,transform)]}[s["key"]]
        if sorted(economic)!=sorted(expected): raise ValueError("scenario thresholds overlap or leave a gap; generate all three from the shared cutpoints")
    total=sum(_number(s["probability"]) for s in scenarios)
    if total!=1: raise ValueError("scenario probabilities must sum exactly to one")
    template={s["key"]: _number(s["probability"]) for s in scenarios}
    if template=={"base":Decimal(".5"),"bear":Decimal(".25"),"bull":Decimal(".25")} and not all(s["probability_basis"]["kind"]=="template_prior" for s in scenarios):
        raise ValueError("0.5/0.25/0.25 template prior must be explicitly marked")
    return RULE_REF


def partition_conditions(key,partition,*,today,labels=None):
    """Generate one exhaustive ordered partition; no independently typed thresholds."""
    cuts=partition["cutpoints"]
    sides={"bear":[("lt",cuts[0])],"base":[("gte",cuts[0]),("lt",cuts[1])],"bull":[("gte",cuts[1])]}
    return [{"condition_id":f"t{i+1}","label":(labels or {}).get(key,key),"observed_by":partition["observed_by"],
        "metric_ref":partition["metric_ref"],"transform":partition["transform"],"comparator":comparison,"threshold":str(value),
        "horizon":after_year(today.isoformat()),"refs":partition["source_refs"]} for i,(comparison,value) in enumerate(sides[key])]
