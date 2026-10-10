"""Twelve-month EPS × historical P/E scenario valuations, append-only and replayable."""
from __future__ import annotations
import calendar
import copy
import json
import sqlite3
from datetime import date,datetime,timezone
from decimal import Decimal,InvalidOperation,localcontext
from typing import Any,Mapping,Sequence
from pathlib import Path
from .store import content_hash,canonical_json
from .forecast_sensitivity import recompute_linked,SensitivityUnavailable

SCHEMA_VERSION="scenario-valuation:1"
LEGACY_RULE_REF="rule:scenario-eps-pe-12m:1"
RULE_REF="rule:scenario-eps-pe-12m:2"


class ScenarioValuationError(ValueError): pass


def number(value):
    try: out=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError) as exc: raise ScenarioValuationError("valuation value must be numeric") from exc
    if not out.is_finite(): raise ScenarioValuationError("valuation value must be finite")
    return out


def after_year(start):
    day=date.fromisoformat(start)
    return day.replace(year=day.year+1,day=min(day.day,calendar.monthrange(day.year+1,day.month)[1])).isoformat()


def _live(model,role):
    line=next((r for r in model.get("results") or [] if r.get("role")==role),{})
    return {c["period"]["end"]:c for c in line.get("cells") or [] if c.get("status")=="computed" and not c.get("superseded_by")}


def forward_eps(model,start,end,*,prefer_annual=True):
    """Four complete open fiscal quarters in this exact twelve-month window."""
    from .model_forecast import quarter_after
    periods=[p for p in model.get("forecast_periods") or [] if start<p["end"]<=end]
    if len(periods)!=4: raise ScenarioValuationError("model lacks four forecast quarters within twelve months")
    if any(quarter_after(a)["end"]!=b["end"] for a,b in zip(periods,periods[1:])):
        raise ScenarioValuationError("forward EPS quarter sequence is incomplete")
    eps=_live(model,"diluted_eps")
    if any(p["end"] not in eps for p in periods): raise ScenarioValuationError("model has no complete forecast EPS; no profit proxy is permitted")
    numerator=_live(model,"diluted_eps_numerator");shares=_live(model,"diluted_weighted_average_shares")
    annual = None
    fiscal_end = (model.get("forecast_policy") or {}).get("fiscal_year_end")
    if prefer_annual and fiscal_end and periods[-1]["end"][5:] == fiscal_end[5:]:
        method = next((r for r in (model.get("financial_statement_structure_replay") or {}).get("forecast_methods") or []
                       if r.get("annual_forecast_source", {}).get("method") == "annual_growth_from_filed"), None)
        if method is not None:
            if not all(p["end"] in numerator for p in periods):
                raise ScenarioValuationError("annual forecast EPS needs four complete common-income quarters")
            from .annual_share_forecast import annual_share_forecast, annual_share_source
            source = method["annual_forecast_source"]
            line = {"concept":source.get("concept"), "source_evidence":source.get("source_evidence"),
                    "duration_facts":source.get("observations") or []}
            if method.get("annual_status") != "validated" or annual_share_source(line) != source:
                raise ScenarioValuationError("annual diluted-share forecast source does not replay")
            annual = annual_share_forecast(line, period_start=periods[0]["start"], period_end=periods[-1]["end"])
            if annual["status"] != "computed":
                raise ScenarioValuationError("annual diluted-share forecast is unavailable: "+str(annual.get("reason")))
    if annual is not None:
        value=sum(number(numerator[p["end"]]["value"]) for p in periods)/number(annual["value"])
        basis="sum forecast common income / independently forecast annual diluted shares"
    elif all(p["end"] in numerator and p["end"] in shares for p in periods):
        weights=[(date.fromisoformat(p["end"])-date.fromisoformat(p["start"])).days+1 for p in periods]
        mean_shares=sum(number(shares[p["end"]]["value"])*w for p,w in zip(periods,weights))/sum(weights)
        if mean_shares<=0: raise ScenarioValuationError("forecast diluted shares are not positive")
        value=sum(number(numerator[p["end"]]["value"]) for p in periods)/mean_shares
        basis="sum forecast diluted earnings numerator / day-weighted diluted shares"
    else:
        value=sum(number(eps[p["end"]]["value"]) for p in periods)
        basis="sum four model-recomputed filed-definition diluted EPS quarters"
    if value<=0: raise ScenarioValuationError("forecast GAAP EPS is nonpositive; P/E valuation is unavailable")
    return {"value":str(value),"basis":basis,"period_ends":[p["end"] for p in periods],"result_refs":[eps[p["end"]]["ref"] for p in periods],
            **({"annual_shares":{"method":annual["method"], "value":annual["value"], "as_of":annual["as_of"],
               "steps":annual["steps"], "growth_rate":annual["growth_rate"], "source_hash":annual["source"]["content_hash"]}} if annual is not None else {})}


def pe_quantiles(history,start,*,minimum=20):
    rows=[r for r in {r.get("as_of"):r for r in history}.values() if r.get("as_of") and r["as_of"]<=start and r.get("value") is not None and number(r["value"])>0
          and r.get("basis", "gaap")=="gaap"]
    if len(rows)<minimum: raise ScenarioValuationError(f"historical rolling GAAP P/E sample is insufficient ({len(rows)}/{minimum})")
    values=sorted(number(r["value"]) for r in rows)
    def quantile(p):
        index=Decimal(len(values)-1)*p;lo=int(index);hi=min(lo+1,len(values)-1)
        return values[lo]+(values[hi]-values[lo])*(index-lo)
    return {"bear":str(quantile(Decimal(".2"))),"base":str(quantile(Decimal(".5"))),"bull":str(quantile(Decimal(".8"))),
        "sample_count":len(rows),"window":{"start":min(r["as_of"] for r in rows),"end":max(r["as_of"] for r in rows)},
        "source_refs":sorted({str(r["source_ref"]) for r in rows if r.get("source_ref")})}


def build_valuation(*,company_ref,model,scenarios,price,pe_history,dividends,street=None,rule_ref=RULE_REF):
    """Pure evaluation. Missing inputs yield an honest unavailable whole record."""
    sources={"model":copy.deepcopy(model),"scenarios":copy.deepcopy(list(scenarios)),"price":copy.deepcopy(price),
             "pe_history":copy.deepcopy(list(pe_history)),"dividends":copy.deepcopy(dividends),"street":copy.deepcopy(street)}
    binding={"model_version_ref":None if not model else model.get("id"),"model_version_hash":None if not model else model.get("content_hash"),
        "scenario_version_refs":[s.get("id") for s in scenarios],"scenario_version_hashes":[s.get("content_hash") for s in scenarios],
        "price_version_ref":None if not price else price.get("id"),"price_version_hash":None if not price else price.get("content_hash")}
    if rule_ref not in {RULE_REF,LEGACY_RULE_REF}: raise ScenarioValuationError("unknown scenario valuation rule")
    out={"schema_version":SCHEMA_VERSION,"rule_ref":rule_ref,"company_ref":company_ref,"binding":binding,
         "input_hash":content_hash(sources),"inputs":sources,"status":"unavailable","reason":None,"window":None,"scenarios":[],"summary":None,"street":street}
    try:
        if model is None or price is None: raise ScenarioValuationError("no current model or market price")
        if model.get("company_ref")!=company_ref or price.get("company_ref") not in {None,company_ref}: raise ScenarioValuationError("valuation inputs belong to another company")
        start=price.get("as_of");date.fromisoformat(start);end=after_year(start);close=number(price.get("close"))
        if close<=0: raise ScenarioValuationError("current price must be positive")
        out["window"]={"start":start,"end":end,"label":"今天到12个月后"}
        if len(scenarios)!=3 or {s.get("key") for s in scenarios}!={"bear","base","bull"}: raise ScenarioValuationError("valuation requires bear/base/bull scenarios")
        total=sum(number(s.get("probability")) for s in scenarios)
        if total!=1: raise ScenarioValuationError("scenario probabilities must sum exactly to one")
        if any(not 0<number(s["probability"])<=1 for s in scenarios): raise ScenarioValuationError("probability is missing or outside (0,1]")
        forward_eps(model,start,end,prefer_annual=rule_ref==RULE_REF)  # a missing model EPS cannot be repaired with a proxy
        multiples=pe_quantiles(pe_history,start)
        if dividends is None or dividends.get("status") not in {"collected","confirmed_none"}:
            raise ScenarioValuationError("twelve-month dividends are uncollected; missing cash is not zero")
        dividend=number(dividends.get("amount"))
        if dividend<0 or not dividends.get("source_ref"): raise ScenarioValuationError("dividend amount requires a source and cannot be negative")
        if dividends.get("window")!={"start":start,"end":end}: raise ScenarioValuationError("dividend projection does not use the same twelve-month window")
        if model.get("execution_contract",{}).get("drivers",{}).get("status")=="unexecuted": raise ScenarioValuationError("economic driver plan is unexecuted")
        rows=[]
        with localcontext() as ctx:
            ctx.prec=60
            for s in sorted(scenarios,key=lambda s:{"bear":0,"base":1,"bull":2}[s["key"]]):
                held=s.get("model_binding") or {}
                if held!={"model_version_ref":model.get("id"),"model_version_hash":model.get("content_hash")}:
                    raise ScenarioValuationError("scenario baseline is stale or unbound to current model")
                if "driver_deltas" not in s: raise ScenarioValuationError("scenario lacks executable driver deltas")
                from .scenario_rules import validate_probability_basis
                validate_probability_basis(s.get("probability_basis"),s.get("probability"))
                projected=recompute_linked(model,s["driver_deltas"])
                eps=forward_eps(projected,start,end,prefer_annual=rule_ref==RULE_REF)
                multiple=number(multiples[s["key"]]);override=s.get("pe_override")
                if override is not None:
                    if set(override)!={"value","because","source_ref"} or not override["because"] or not override["source_ref"]:
                        raise ScenarioValuationError("P/E override requires reason and source")
                    multiple=number(override["value"])
                if multiple<=0: raise ScenarioValuationError("scenario P/E is not positive")
                target=number(eps["value"])*multiple+dividend
                payoff=(target/close-1)*100
                rows.append({"scenario_version_ref":s.get("id"),"key":s["key"],"name":s["name"],"probability":str(number(s["probability"])),
                    "eps":eps,"pe":str(multiple),"pe_basis":multiples,"pe_override":override,"dividends":str(dividend),
                    "target_price":str(target),"return_percent":str(payoff),"driver_deltas":s["driver_deltas"],
                    "trigger":"; ".join(t.get("label","") for t in s.get("triggers") or [])})
            bykey={r["key"]:r for r in rows};down=number(bykey["bear"]["return_percent"]);up=number(bykey["bull"]["return_percent"])
            if not number(bykey["bear"]["target_price"])<=number(bykey["base"]["target_price"])<=number(bykey["bull"]["target_price"]):
                raise ScenarioValuationError("scenario targets are not ordered bear ≤ base ≤ bull")
            summary={"expected_return_percent":str(sum(number(r["probability"])*number(r["return_percent"]) for r in rows)),
                "upside_downside_ratio":None if down>=0 or up<=0 else str(up/abs(down)),
                "bear_target_price":bykey["bear"]["target_price"],"base_target_price":bykey["base"]["target_price"],"bull_target_price":bykey["bull"]["target_price"],
                "current_price":str(close),"currency":price.get("currency"),"bear_probability":bykey["bear"]["probability"]}
        out.update(status="available",scenarios=rows,summary=summary)
    except (ScenarioValuationError,SensitivityUnavailable,ValueError,TypeError) as exc:
        out.update(status="unavailable",reason=str(exc),scenarios=[],summary=None)
    out["content_hash"]=content_hash(out)
    return out


def replay(value):
    body=dict(value);asserted=body.pop("content_hash",None)
    if content_hash(body)!=asserted: raise ScenarioValuationError("scenario valuation hash differs")
    stored="body_hash" in body
    if stored:
        for key in ("id","version","prior_version_ref","created_at","body_hash"): body.pop(key,None)
        body["content_hash"]=value["body_hash"]
    else: body["content_hash"]=asserted
    sources=value["inputs"]
    expected=build_valuation(company_ref=value["company_ref"],model=sources["model"],scenarios=sources["scenarios"],price=sources["price"],
        pe_history=sources["pe_history"],dividends=sources["dividends"],street=sources["street"],rule_ref=value["rule_ref"])
    if expected!=body: raise ScenarioValuationError("scenario valuation does not replay")
    return expected


def latest(connection,company_ref):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scenario_valuation_versions'").fetchone(): return None
    row=connection.execute("SELECT record_json FROM scenario_valuation_versions WHERE company_ref=? ORDER BY version_number DESC LIMIT 1",(company_ref,)).fetchone()
    if row is None: return None
    value=json.loads(row[0]);replay(value);return value


class ScenarioValuationAuthority:
    def __init__(self,store):
        self.store=store;self.connection=store.connection
        self.connection.execute("CREATE TABLE IF NOT EXISTS scenario_valuation_versions(version_id TEXT PRIMARY KEY,company_ref TEXT NOT NULL,version_number INTEGER NOT NULL,prior_version_id TEXT,body_hash TEXT NOT NULL,record_json TEXT NOT NULL,content_hash TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(company_ref,version_number))")
        self.connection.executescript("CREATE TRIGGER IF NOT EXISTS scenario_valuation_no_update BEFORE UPDATE ON scenario_valuation_versions BEGIN SELECT RAISE(ABORT, 'append-only scenario valuation'); END; CREATE TRIGGER IF NOT EXISTS scenario_valuation_no_delete BEFORE DELETE ON scenario_valuation_versions BEGIN SELECT RAISE(ABORT, 'append-only scenario valuation'); END;")
    def publish(self,body):
        replay(body)
        _assert_current_bindings(self.connection,body)
        prior=latest(self.connection,body["company_ref"])
        if prior and prior["body_hash"]==body["content_hash"]: return {**prior,"status_marker":"duplicate"}
        number=1 if prior is None else prior["version"]+1
        record={**body,"body_hash":body["content_hash"],"id":"scenario-valuation-version:"+content_hash({"company":body["company_ref"],"version":number})[:32],
            "version":number,"prior_version_ref":None if prior is None else prior["id"],"created_at":datetime.now(timezone.utc).isoformat()}
        record.pop("content_hash");record["content_hash"]=content_hash(record)
        with self.store._transaction() as cur:
            cur.execute("INSERT INTO scenario_valuation_versions VALUES(?,?,?,?,?,?,?,?)",(record["id"],record["company_ref"],number,record["prior_version_ref"],body["content_hash"],canonical_json(record),record["content_hash"],record["created_at"]))
        return {**record,"status_marker":"fresh"}


def _latest_json(connection,table,company_ref,order="version_number"):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone(): return None
    columns={r[1] for r in connection.execute(f"PRAGMA table_info({table})")}
    order=order if order in columns else "created_at"
    row=connection.execute(f"SELECT record_json FROM {table} WHERE company_ref=? ORDER BY {order} DESC LIMIT 1",(company_ref,)).fetchone()
    return None if row is None else json.loads(row[0])


def held_inputs(connection,company_ref,*,state_dir=None):
    """Read held authorities only. A missing dividend projection remains missing."""
    from .company_scenario import in_force
    from .valuation_history import company_facts,eps_points,ttm_eps
    from .return_attribution import dividend_cash_window
    model=_latest_json(connection,"forecast_model_versions",company_ref)
    price_record=_latest_json(connection,"market_price_series_versions",company_ref)
    bars=(price_record or {}).get("bars") or []
    price=None if not bars else {"id":price_record["id"],"content_hash":price_record.get("content_hash"),"company_ref":company_ref,
        "as_of":bars[-1]["date"],"close":bars[-1].get("close"),"currency":price_record.get("currency")}
    if state_dir is None:
        db=next((r[2] for r in connection.execute("PRAGMA database_list") if r[1]=="main"),"")
        state_dir=Path(db).parent if db else None
    history=[]
    facts=None if state_dir is None else company_facts(connection,Path(state_dir),company_ref)
    if facts:
        ttm=ttm_eps(eps_points(facts["facts"]))
        for bar in bars:
            day=str(bar["date"]);known=[r for r in ttm if r["effective"]<=day]
            if not known or bar.get("close") is None: continue
            point=max(known,key=lambda r:(r["period_end"],r["effective"]))
            if number(point["value"])<=0: continue
            history.append({"as_of":day,"value":str(number(bar["close"])/number(point["value"])),"basis":"gaap",
                "source_ref":price_record["id"],"eps_source_hash":facts["raw_hash"],"eps_period_end":point["period_end"],
                "eps_available_at":point["effective"],"eps_accessions":point.get("source_accessions") or []})
    dividends=None
    evidence=_latest_json(connection,"dividend_payment_evidence_versions",company_ref)
    if evidence and price:
        start=price["as_of"];end=after_year(start)
        exact={k:v for k,v in evidence.items() if k not in {"id","version","created_at","content_hash","body_hash","prior_version_ref","schema_version"}}
        payments=dividend_cash_window(exact,start,end)
        if payments.get("cash_per_share") is not None and exact.get("currency")==price.get("currency"):
            dividends={"status":"confirmed_none" if payments["cash_per_share"]=="0" else "collected",
                "amount":payments["cash_per_share"],"source_ref":exact["source_ref"],"source_hash":exact["source_hash"],
                "share_class_ref":exact["share_class_ref"],"window":{"start":start,"end":end}}
    snapshot=_latest_json(connection,"valuation_snapshot_versions",company_ref)
    street=None if not snapshot else {"snapshot_ref":snapshot.get("id"),"targets":snapshot.get("street_targets") or [],
        "limitations":"Only held target observations; target horizon and currency must be checked before comparison."}
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='consensus_estimate_versions'").fetchone():
        from .consensus_estimate import ConsensusEstimateAuthority
        authority=ConsensusEstimateAuthority.__new__(ConsensusEstimateAuthority);authority.connection=connection
        consensus=authority.latest_version(company_ref)
        if consensus is not None:
            targets=consensus.get("price_target") or {}
            street={"consensus_ref":consensus["id"],"consensus_hash":consensus["content_hash"],
                "as_of":consensus.get("as_of"),"currency":consensus.get("currency"),"targets":[copy.deepcopy(targets)],
                "comparable":False,"limitations":"已持有卖方目标价均值及高低值；来源未明确同一12个月截止日，不将其视为同窗口可比估值。"}
    return {"company_ref":company_ref,"model":model,"scenarios":in_force(connection,company_ref),"price":price,
        "pe_history":history,"dividends":dividends,"street":street}


def current(connection,company_ref,*,state_dir=None):
    """Current deterministic result, never a write from a read-only consumer."""
    inputs=held_inputs(connection,company_ref,state_dir=state_dir)
    held=latest(connection,company_ref)
    if held and held.get("rule_ref")==RULE_REF and held.get("input_hash")==content_hash({k:v for k,v in inputs.items() if k!="company_ref"}):
        return held
    computed=build_valuation(**inputs)
    computed["id"]="scenario-valuation-calculation:"+computed["content_hash"][:32]
    return computed


def refresh_company(store,company_ref,*,state_dir=None):
    return ScenarioValuationAuthority(store).publish(build_valuation(**held_inputs(store.connection,company_ref,state_dir=state_dir)))


def _assert_current_bindings(connection,body):
    company=body["company_ref"];sources=body["inputs"]
    for table,key in (("forecast_model_versions","model"),("market_price_series_versions","price")):
        held=_latest_json(connection,table,company)
        if held and (sources.get(key) is None or sources[key].get("id")!=held.get("id") or sources[key].get("content_hash")!=held.get("content_hash")):
            raise ScenarioValuationError("scenario valuation baseline is stale: "+key)
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_scenario_sets'").fetchone():
        from .company_scenario import in_force
        held=in_force(connection,company)
        if held and [(s["id"],s["content_hash"]) for s in held]!=[(s.get("id"),s.get("content_hash")) for s in sources["scenarios"]]:
            raise ScenarioValuationError("scenario valuation set is stale")


def valuation_view(value):
    """Viewer reasons are actionable Chinese; exact diagnostic remains technical."""
    result={k:value.get(k) for k in ("id","status","binding","window","scenarios","summary","street")}
    reason=value.get("reason")
    mappings={"no current model or market price":"尚无当前盈利预测或行情；先补齐模型与最新价格。",
        "valuation requires bear/base/bull scenarios":"尚无已采纳的完整悲观、基准、乐观三情景；先完成情景裁决。",
        "model lacks four forecast quarters within twelve months":"未来12个月的四个完整预测季度尚未齐全；需更新滚动预测。",
        "model has no complete forecast EPS; no profit proxy is permitted":"预测每股收益尚未完整计算；先补齐净利润和股份口径。",
        "twelve-month dividends are uncollected; missing cash is not zero":"未来12个月股息尚未收齐；需要有来源的股息金额或明确无股息依据。",
        "economic driver plan is unexecuted":"经济驱动方案尚未执行；先补齐量、价、分部或合同基础。",
        "scenario baseline is stale or unbound to current model":"情景基准已过期或未绑定当前模型；需按最新模型重草。",
        "scenario lacks executable driver deltas":"情景缺少可执行的驱动变化；需补齐数量、价格或费用率假设。",
        "forecast GAAP EPS is nonpositive; P/E valuation is unavailable":"预测会计准则每股收益非正；市盈率估值暂不可用。",
        "scenario probabilities must sum exactly to one":"情景概率合计未达到100%；需重新裁决整组概率。"}
    shown=mappings.get(reason)
    if reason and reason.startswith("historical rolling GAAP P/E sample is insufficient"):
        shown="已持有的历史滚动市盈率样本不足；先补齐历史价格与同口径每股收益。"
    if reason and not shown:shown="情景估值尚未通过输入或计算校验；请查看技术详情定位并补齐依据。"
    result["reason"]=shown
    result["technical_details"]={"reason":reason}
    return result
