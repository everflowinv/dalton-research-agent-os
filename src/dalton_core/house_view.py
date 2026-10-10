"""A deterministic first-screen house view, with dated authority links and gaps."""
from __future__ import annotations
from datetime import date
from .store import content_hash

RULE_REF="rule:house-view-current-authorities:1"


def build_house_view(*,company_ref,theses,claims,judgements,scenario_valuation,catalyst,today,ticker=None):
    latest={}
    for row in theses:
        ref=row.get("thesis_ref") or row.get("ref")
        if ref is None: continue
        if ref not in latest or (int(row.get("version") or 0),str(row.get("created_at") or ""))>(int(latest[ref].get("version") or 0),str(latest[ref].get("created_at") or "")):
            latest[ref]=row
    def mine(row):
        return row.get("company_ref")==company_ref or (ticker and str(row.get("thesis_ref") or row.get("ref") or "").lower().startswith("thesis:"+ticker.lower()+":"))
    active=[r for r in latest.values() if mine(r) and r.get("stance")=="house" and r.get("view_status")=="active"]
    active.sort(key=lambda r:(str(r.get("created_at") or ""),int(r.get("version") or 0)),reverse=True)
    chosen=active[0] if active else None
    claim_byref={r.get("claim_version_id") or r.get("ref") or r.get("id"):r for r in claims}
    reasons=[];seen=set()
    for row in ([] if chosen is None else [chosen]):
        for ref in row.get("claim_refs") or []:
            claim=claim_byref.get(ref)
            text=None if claim is None else claim.get("statement")
            if text and ref not in seen: reasons.append({"text":text,"claim_ref":ref,"passage_ref":None});seen.add(ref)
        for ref in row.get("passage_refs") or []:
            if ref not in seen:
                text=row.get("mechanism") or row.get("implied_expectation")
                if text:reasons.append({"text":text,"claim_ref":None,"passage_ref":ref});seen.add(ref)
    reasons=reasons[:3]
    changes=[condition_view(x) for x in (chosen or {}).get("falsification_conditions") or []]
    ordered=sorted(judgements,key=lambda r:str(r.get("at") or r.get("created_at") or ""),reverse=True)
    segment=[]
    for row in ordered:
        if str(row.get("decision") or "") not in {"NO_CHANGE","THESIS_NO_CHANGE"}: break
        segment.append(row)
    unchanged=len(segment)
    dates=[str(r.get("at") or r.get("created_at") or "")[:10] for r in segment if r.get("at") or r.get("created_at")]
    start=min(dates) if dates else None;end=max(dates) if dates else None
    unchanged_window=None if not dates else {"start":start,"end":end,"days":(date.fromisoformat(end)-date.fromisoformat(start)).days+1}
    change_word=(str((chosen or {}).get("change_reason") or "").split("｜",1)[0]).strip()
    labels={"THESIS_STRENGTHENED":"依据加强","THESIS_WEAKENED":"依据减弱","THESIS_BROKEN":"观点失效","NO_CHANGE":"维持","THESIS_NO_CHANGE":"维持"}
    change_label=labels.get(change_word,"首次建立" if chosen and chosen.get("version")==1 else None)
    if unchanged and chosen:change_label=f"维持（最近连续{unchanged}条动态未改变观点"+(f"，{start}至{end}，共{unchanged_window['days']}天）" if unchanged_window else "）")
    value={"rule_ref":RULE_REF,"statement":None if chosen is None else chosen.get("summary") or chosen.get("statement"),
        "as_of":None if chosen is None else chosen.get("created_at"),"version":None if chosen is None else chosen.get("version"),
        "thesis_version_ref":None if chosen is None else chosen.get("id"),"change_label":change_label,
        "reasons":reasons,"reasons_status":"available" if len(reasons)==3 else "incomplete",
        "risk_reward":None if (scenario_valuation or {}).get("status")!="available" else {**scenario_valuation["summary"],
            "valuation_ref":scenario_valuation.get("id"),"window":scenario_valuation["window"]},
        "risk_reward_missing_reason":None if (scenario_valuation or {}).get("status")=="available" else (scenario_valuation or {}).get("reason") or "模型、情景或估值依据尚不完整",
        "next_catalyst":None,"what_changes_view":changes,"unchanged_updates":unchanged,
        "unchanged_window":unchanged_window,"status":"available" if chosen else "missing","missing_reason":None if chosen else "尚无已登记且生效的本所观点"}
    if catalyst:
        next_event=catalyst.get("next_catalyst") or catalyst
        # No ex-dividend date substitutes for a research event.
        if next_event.get("kind") not in {"ex_dividend","dividend_ex_date"}:
            observations=next_event.get("key_observations") or []
            value["next_catalyst"]={"date":next_event.get("date") or next_event.get("event_date"),
                "label":next_event.get("label") or next_event.get("kind_label"),"what_to_watch":observations,
                "observations_status":next_event.get("observations_status") or "missing",
                "observations_reason":next_event.get("observations_reason") or (None if observations else "尚无当前且已发布的业绩前瞻观察项")}
    value["content_hash"]=content_hash(value)
    return value


def stage_note(*,stage_ref,deliverable,model_coverage,current_note):
    if "等着起草初步研究" not in current_note:return current_note
    if deliverable:
        if (model_coverage or {}).get("status")!="available":return (model_coverage or {}).get("label") or "初步研究已形成；盈利预测仍有资料缺口"
        return "研究底稿已形成；按当前模型和情景继续跟踪"
    if stage_ref and stage_ref not in {"initial_screen","source_readiness","company_dossier"}:
        return "本阶段资料已齐；继续完成当前研究任务"
    return current_note


def condition_view(value):
    metric=str(value.get("metric_ref") or "")
    labels={"revenue":"营业收入","operating_income":"营业利润","operating_margin":"营业利润率","margin":"利润率","diluted_eps":"稀释每股收益","eps":"每股收益","fcf:company":"公司口径自由现金流","book_to_bill":"订单收入比","bookings":"签约额"}
    label=labels.get(metric.removeprefix("metric:").removeprefix("concept:").replace("-","_"))
    if label is None and metric.startswith("concept:"):
        from .quantitative_claim_promotion import CONCEPT_METRICS
        known=CONCEPT_METRICS.get(metric.split(":",1)[1]);label=None if known is None else known[1]
    comparison={"lt":"低于","lte":"不高于","gt":"高于","gte":"不低于","eq":"等于","ne":"不等于"}.get(value.get("operator"))
    units={"ratio":"比例","percent":"%","usd":"美元","USD":"美元","usd_per_share":"美元／股","USD/share":"美元／股","count":"个","shares":"股"}
    return {**value,"metric_display_label":label,"comparison_label":comparison,"unit_display_label":units.get(value.get("unit")),
        "display_status":"available" if label and comparison and units.get(value.get("unit")) else "missing"}
