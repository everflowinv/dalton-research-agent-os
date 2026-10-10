"""Independent annual diluted-share forecasts from direct annual filings.

This is an explicit forecast assumption, never a reconstruction of historical
annual shares from quarterly averages. Historical direct values retain priority.
"""
from __future__ import annotations
import copy
from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import Any, Mapping
from .store import content_hash

RULE_REF = "rule:annual-diluted-shares-filed-growth:1"
METHOD = "annual_growth_from_filed"


def annual_share_source(line: Mapping[str, Any]) -> dict[str, Any]:
    out = {"rule_ref": RULE_REF, "method": METHOD, "status": "unavailable",
           "observations": [], "reason": None}
    try:
        facts = []
        for item in line.get("duration_facts") or []:
            start, end = date.fromisoformat(item["period_start"]), date.fromisoformat(item["period_end"])
            if not 350 <= (end-start).days+1 <= 380:
                continue
            if not set(item.get("source_forms") or []) <= {"10-K", "10-K/A"} or not item.get("source_forms"):
                continue
            if item.get("basis") not in (None, "reported", "direct"):
                continue
            if not item.get("source_accessions") or item.get("unit") != "shares" or Decimal(str(item["value"])) <= 0:
                continue
            facts.append(copy.deepcopy(dict(item)))
        by_end = {}
        for fact in facts:
            key = fact["period_end"]
            if key in by_end and any(fact.get(k) != by_end[key].get(k) for k in ("period_start", "value", "unit")):
                raise ValueError("direct annual diluted shares have conflicting filed identities")
            by_end[key] = fact
        ordered = [by_end[end] for end in sorted(by_end)]
        if len(ordered) < 2:
            raise ValueError("fewer than two directly filed annual diluted-share observations")
        # Use the latest uninterrupted run; never compound over a missing year.
        run = [ordered[-1]]
        for prior in reversed(ordered[:-1]):
            current = run[0]
            p_end, c_end = date.fromisoformat(prior["period_end"]), date.fromisoformat(current["period_end"])
            if (c_end.year-p_end.year != 1 or not 350 <= (c_end-p_end).days <= 380
                    or date.fromisoformat(current["period_start"]) != p_end+timedelta(days=1)):
                break
            run.insert(0, prior)
        if len(run) < 2:
            raise ValueError("latest directly filed annual diluted shares lack a consecutive prior fiscal year")
        run = run[-4:]
        with localcontext() as ctx:
            ctx.prec = 60
            rates = [Decimal(str(c["value"]))/Decimal(str(p["value"]))-1 for p,c in zip(run, run[1:])]
            rate = sum(rates)/len(rates)
        out.update(status="validated", observations=run, reason=None,
                   concept=line.get("concept"), unit="shares", source_evidence=copy.deepcopy(line.get("source_evidence")),
                   as_of=run[-1]["period_end"], base_value=run[-1]["value"], growth_rate=str(rate),
                   historical_growth_band={"low":str(min(rates)),"high":str(max(rates))},
                   hypothesis="Future annual diluted shares continue the mean of the latest consecutive filed annual changes; annual dilution is modeled independently of quarterly averages.")
    except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
        out["reason"] = str(exc)
    out["content_hash"] = content_hash(out)
    return out


def annual_share_forecast(line: Mapping[str, Any], *, period_start: str, period_end: str) -> dict[str, Any]:
    source = annual_share_source(line)
    out = {"status":"unavailable", "value":None, "unit":"shares", "method":METHOD,
           "rule_ref":RULE_REF, "source":source, "as_of":source.get("as_of"), "steps":None,
           "growth_rate":source.get("growth_rate"), "reason":source.get("reason")}
    try:
        # A direct held annual value always wins, even when no growth history exists.
        direct = [f for f in line.get("duration_facts") or [] if f.get("period_start")==period_start and f.get("period_end")==period_end
                  and 350 <= (date.fromisoformat(period_end)-date.fromisoformat(period_start)).days+1 <= 380
                  and f.get("basis") in (None,"reported","direct")
                  and f.get("unit")=="shares" and f.get("source_accessions") and set(f.get("source_forms") or []) <= {"10-K","10-K/A"} and f.get("source_forms")]
        if direct:
            values={str(f["value"]) for f in direct}
            if len(values)!=1 or Decimal(next(iter(values)))<=0:
                raise ValueError("direct annual shares conflict or are not positive")
            out.update(status="computed", value=next(iter(values)), method="direct_annual_filing", steps=0, reason=None)
            return out
        if source["status"] != "validated":
            return out
        start, end = date.fromisoformat(period_start), date.fromisoformat(period_end)
        base_end = date.fromisoformat(source["as_of"])
        steps = end.year-base_end.year
        if (not 350 <= (end-start).days+1 <= 380 or not 1 <= steps <= 12
                or abs((end-base_end).days/steps-365.25)>15):
            raise ValueError("annual share forecast does not match a complete future fiscal-year calendar")
        with localcontext() as ctx:
            ctx.prec = 60
            rate = Decimal(source["growth_rate"])
            if not Decimal(source["historical_growth_band"]["low"]) <= rate <= Decimal(source["historical_growth_band"]["high"]):
                raise ValueError("annual share growth lies outside its own filed growth band")
            amount = Decimal(str(source["base_value"]))*(1+rate)**steps
        if amount <= 0:
            raise ValueError("annual forecast diluted shares must be positive")
        out.update(status="computed", value=str(amount), steps=steps, reason=None)
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        out["reason"]=str(exc)
    return out
