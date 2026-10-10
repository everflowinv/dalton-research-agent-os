"""Forward FCF in system, disclosed company and ex-one-off cash definitions."""
from __future__ import annotations
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

SCHEMA_VERSION = "forecast-company-fcf:1"
COMPONENT_ROLES = {"operating_cash_flow", "capital_expenditure", "finance_lease_principal",
                   "capitalized_software", "capitalized_contract_costs"}


def _number(value: Any) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("company FCF amount must be numeric") from exc
    if not number.is_finite(): raise ValueError("company FCF amount must be finite")
    return number


def validate_fcf_definition(value: Any, concepts: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - {"source_ref", "components", "one_off_items", "historical_bridge"} or not value.get("source_ref"):
        raise ValueError("company FCF definition must cite a filing/passage and its components")
    components = value.get("components")
    if not isinstance(components, list) or len(components) < 2:
        raise ValueError("company FCF definition requires CFO and capital expenditure")
    seen = set()
    roles = set()
    for item in components:
        if set(item) != {"role", "concept", "coefficient"} or item["role"] not in COMPONENT_ROLES:
            raise ValueError("unsupported company FCF component")
        if item["concept"] in seen or (concepts is not None and item["concept"] not in concepts):
            raise ValueError("company FCF components must be unique disclosed concepts")
        if _number(item["coefficient"]) != (1 if item["role"] == "operating_cash_flow" else -1):
            raise ValueError("company FCF must add CFO and deduct its capital cash outflows")
        seen.add(item["concept"]); roles.add(item["role"])
    if len(roles) != len(components):
        raise ValueError("company FCF cannot count the same cash component role twice")
    if not {"operating_cash_flow", "capital_expenditure"} <= roles:
        raise ValueError("company FCF definition requires CFO minus property/equipment")
    for item in value.get("one_off_items") or []:
        if set(item) != {"period_end", "value", "direction", "source_ref", "because"} or item["direction"] not in {"inflow", "outflow"} or not item["source_ref"]:
            raise ValueError("one-off cash adjustment needs amount, direction and filing source")
        from datetime import date
        date.fromisoformat(item["period_end"])
        if _number(item["value"]) < 0: raise ValueError("one-off cash adjustment amount cannot be negative")
    return dict(value)


def load_company_fcf_definition(connection: Any, company_ref: str, table: Mapping[str, Any],
                                fiscal_year_end: str | None) -> dict[str, Any] | None:
    """Read B's reconciled earnings-release table from held verified bytes."""
    from .earnings_release_intake import core_state_dir
    from .earnings_release_outlook import intake_reads, exhibit_bytes, plain_text, _release_period
    from .valuation_fcf_bridge import company_fcf_tables, fcf_bridge, one_off_items
    from .forecast_coverage import COVERAGE_CONCEPTS
    from .company_model_inputs import CASH_FLOW_ROLE_CONCEPTS
    candidates = {**COVERAGE_CONCEPTS, **CASH_FLOW_ROLE_CONCEPTS}
    available = {s["concept"] for s in table.get("filed_lines") or [] if s.get("status") == "filed"}
    available |= {s["concept"] for s in table.get("cash_flow_inputs") or [] if s.get("status") == "filed"}
    state_dir = core_state_dir(connection)
    if state_dir is None: return None
    financial_inputs = table
    releases = []
    for read in sorted(intake_reads(connection, company_ref), key=lambda r: str(r.get("report_date") or "")):
        raw, _reason = exhibit_bytes(state_dir, str(read["body_sha256"]))
        if raw is not None:
            releases.append({"text": plain_text(raw), "release_period": _release_period(read),
                             "accession": read.get("accession"), "report_date": read.get("report_date")})
    if not releases: return None
    latest = releases[-1]
    month = None if fiscal_year_end is None else int(fiscal_year_end[5:7])
    tables = company_fcf_tables(latest["text"], fye_month=month, release_period=latest["release_period"])
    table = next((t for t in reversed(tables) if t["reconciles"]), None)
    if table is None: return None
    components = []
    for item in table["components"]:
        label = item["label"].lower()
        role = ("finance_lease_principal" if re.search(r"financ(?:e|ing).*lease|lease.*principal", label) else
                "capitalized_software" if "software" in label else
                "capitalized_contract_costs" if re.search(r"contract|transformation", label) else
                "operating_cash_flow" if re.search(r"operating|cash.*operations", label) else
                "capital_expenditure" if re.search(r"capital expenditure|property|equipment", label) else None)
        concept = next((c for c in candidates.get(role, ()) if c in available), None)
        if role is None or concept is None:
            return None  # an ambiguous component never becomes a silent zero
        components.append({"role": role, "concept": concept, "coefficient": "1" if role == "operating_cash_flow" else "-1"})
    flows = {}
    for role in ("operating_cash_flow", "capital_expenditure"):
        component = next(c for c in components if c["role"] == role)
        src = next((l for l in financial_inputs.get("filed_lines") or [] if l["concept"] == component["concept"]), {})
        flows[role] = {end: Decimal(str(c["value"])) for end,c in (src.get("cells") or {}).items() if c.get("period_start")}
    common = sorted(set(flows["operating_cash_flow"]) & set(flows["capital_expenditure"]))[-4:]
    system = None
    if len(common) == 4:
        system = {"value": str(sum(flows["operating_cash_flow"][end] - flows["capital_expenditure"][end] for end in common)),
                  "through": common[-1], "formula": "TTM filed CFO - property/equipment"}
    source = f"filing:{latest['accession']}"
    definition = {"source_ref": source, "components": components,
                  # A historic cash event is removed from future forecasts by
                  # the one-off model. Do not deduct its amount every future q.
                  "one_off_items": [],
                  "historical_bridge": fcf_bridge(system=system, releases=releases, market_cap=None, fye_month=month)}
    try: return validate_fcf_definition(definition)
    except ValueError: return None


def append_company_fcf(body: dict[str, Any], definition: Mapping[str, Any] | None) -> dict[str, Any]:
    from .model_forecast_driver import _result, _computed_cell, _unavailable_cell, _finish, _cell_ref
    from .forecast_coverage import COVERAGE_CONCEPTS
    results = {r["ref"]: r for r in body["results"]}
    drivers = {d.get("concept"): d for d in body["drivers"] if d.get("concept")}
    periods = body["forecast_periods"]
    ends = [p["end"] for p in periods]
    def live(ref):
        return {c["period"]["end"]: Decimal(str(c["value"])) for c in results.get(ref, {}).get("cells") or []
                if c.get("status") == "computed" and not c.get("superseded_by")}
    values = {"operating_cash_flow": live("result:operating_cash_flow"), "capital_expenditure": live("result:capital_expenditure")}
    component_refs = {d.get("concept"): next((r["ref"] for r in body["results"] if r.get("driver_ref") == d["ref"]), None) for d in drivers.values()}
    report = {"schema_version": SCHEMA_VERSION, "source_ref": None if not definition else definition.get("source_ref"),
              "columns": [], "components": [], "historical_bridge": None if not definition else definition.get("historical_bridge")}
    if definition is not None: validate_fcf_definition(definition)
    company_values = {}
    company_capex = {}
    missing = []
    if definition is not None:
        for component in definition["components"]:
            role, concept = component["role"], component["concept"]
            source = drivers.get(concept)
            ref = component_refs.get(concept)
            observed = live(ref) if ref else {}
            if role in {"operating_cash_flow", "capital_expenditure"} and source and source.get("role") == role:
                observed = values[role]
            if not observed and source:
                history = [Decimal(str(c["value"])) for c in source["history"] if c.get("period_start")][-4:]
                if len(history) == 4:
                    observed = {end: sum(history) / 4 for end in ends}
                    ref = f"result:fcf-component:{role}"
                    line = _result(ref, None, role, source.get("unit") or "usd", "trailing four-quarter capital cash outflow carried forward", driver_ref=source["ref"])
                    for period in periods:
                        line["cells"].append(_computed_cell(ref, period, observed[period["end"]], inputs=[_cell_ref(c) for c in source["history"][-4:]]))
                    body["results"].append(_finish(line)); results[ref] = line
            if not set(ends) <= set(observed): missing.append(concept)
            values[concept] = observed
            report["components"].append({**component, "result_ref": ref,
                "status": "computed" if set(ends) <= set(observed) else "unavailable"})
        if not missing:
            for end in ends:
                company_values[end] = sum(Decimal(str(c["coefficient"])) * values[c["concept"]][end] for c in definition["components"])
                company_capex[end] = sum(values[c["concept"]][end] for c in definition["components"] if c["role"] != "operating_cash_flow")
    system_values = live("result:free_cash_flow")
    reason = "没有绑定公司的已披露FCF对账口径" if definition is None else "公司FCF组成项未采集/不可预测：" + ", ".join(missing)
    configurations = [("system", "result:free_cash_flow_system", "系统口径自由现金流", system_values),
                      ("company", "result:free_cash_flow_company", "公司口径自由现金流", company_values),
                      ("company_capex", "result:capital_expenditure_company", "公司口径资本开支（含租赁/软件/合同成本）", company_capex)]
    adjusted = dict(company_values)
    if definition:
        # Explicit forecast-quarter cash one-offs are handled by their own
        # source-bound amount; only a declared future item enters this bridge.
        for end in adjusted:
            for item in definition.get("one_off_items") or []:
                if item["period_end"] != end: continue
                value = Decimal(str(item["value"]))
                adjusted[end] += -value if item["direction"] == "inflow" else value
    configurations.append(("company_ex_one_offs", "result:free_cash_flow_company_ex_one_offs", "公司口径剔除一次性现金项目", adjusted))
    for basis, ref, label, amounts in configurations:
        line = _result(ref, None, label, body["unit"],
                       "CFO - property/equipment - finance lease principal - capitalized software - capitalized contract costs" if basis != "system" else "CFO - property/equipment")
        for period in periods:
            if period["end"] in amounts:
                refs = [{"ref": "result:free_cash_flow", "period_end": period["end"]}] if basis == "system" else [
                    {"ref": c["result_ref"], "period_end": period["end"]} for c in report["components"] if c["result_ref"]]
                line["cells"].append(_computed_cell(ref, period, amounts[period["end"]], results=refs))
            else: line["cells"].append(_unavailable_cell(ref, period, reason))
        body["results"] = [r for r in body["results"] if r["ref"] != ref] + [_finish(line)]
        report["columns"].append({"basis": basis, "result_ref": ref, "status": line["status"], "reason": line["reason"]})
    return report
