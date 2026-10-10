"""Consumer adapter: conviction uses current, recomputed EPS × P/E scenario payoffs."""
from .scenario_valuation import current


def conviction_scenarios(store,company_ref):
    valuation=current(store.connection,company_ref)
    if valuation["status"]!="available": return []
    return [{"ref":r["scenario_version_ref"],"name":r["name"],"probability":r["probability"],
        "payoff":r["return_percent"],"trigger":r["trigger"],"valuation_ref":valuation["id"],
        "model_binding":valuation["binding"],"window":valuation["window"]} for r in valuation["scenarios"]]


def validate_conviction_sources(store,company_ref,rows):
    held=conviction_scenarios(store,company_ref)
    if len(held)!=3 or len(rows)!=3: raise ValueError("conviction requires three current model-recomputed EPS × P/E scenarios")
    byref={r["ref"]:r for r in held}
    from decimal import Decimal
    for row in rows:
        source=byref.get(row.get("source_ref"))
        if source is None or any(Decimal(str(row[k]))!=Decimal(str(source[k])) for k in ("probability","payoff")):
            raise ValueError("conviction payoff/probability differs from its current held scenario valuation")
    return held
