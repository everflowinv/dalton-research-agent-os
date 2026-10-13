"""Source-bound economic drivers and financial mechanisms for new forecasts.

The statement DAG keeps the company's historical presentation. This execution
layer forecasts its leaves from explicit economic formulas, then replays that
DAG. A declaration without an executed formula is a publication blocker.
"""
from __future__ import annotations

import copy
from decimal import Decimal, localcontext
import re
from typing import Any, Mapping, Sequence

from .store import content_hash
from .forecast_seasonality import seasonal_profile, year_ago

RULE_REF = "rule:financial-driver-execution:1"
SCHEMA_VERSION = "financial-execution:1"
ASSUMPTION_EXECUTION_REF = "rule:filed-tax-recurring-execution:0.1"
DERIVED_HISTORY_UNIT_RULE_REF = "rule:filed-derived-history-unit-case:0.1"


def execution_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(spec.get(key) or []) for key in
            ("revenue_drivers", "expense_lines", "driver_formulas")}


def validate_driver_formulas(formulas: Any, drivers: Sequence[Mapping[str, Any]],
                             structure: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(formulas, list):
        raise ValueError("driver_formulas must be a list")
    refs = {d["ref"] for d in drivers}
    outputs = {l["ref"] for l in structure.get("lines") or [] if l.get("kind") == "filed"}
    seen = set()
    out = []
    for f in formulas:
        if not isinstance(f, Mapping) or set(f) != {"output_ref", "operator", "driver_refs", "because"}:
            raise ValueError("driver formula must carry output_ref, operator, driver_refs and because")
        if f["output_ref"] not in outputs or f["output_ref"] in seen:
            raise ValueError("driver formula must target one unique filed statement leaf")
        inputs = f["driver_refs"]
        if not isinstance(inputs, list) or not inputs or len(set(inputs)) != len(inputs) or not set(inputs) <= refs:
            raise ValueError("driver formula references unknown or duplicate economic drivers")
        if f["operator"] not in {"sum", "product", "contract_conversion"}:
            raise ValueError("driver formula operator is unsupported")
        if f["operator"] == "product" and len(inputs) != 2:
            raise ValueError("volume × price needs exactly two drivers")
        if f["operator"] == "contract_conversion" and len(inputs) != 1:
            raise ValueError("contract conversion needs one contract book")
        if not isinstance(f["because"], str) or not f["because"].strip():
            raise ValueError("driver formula needs its company-specific reason")
        seen.add(f["output_ref"])
        out.append(copy.deepcopy(dict(f)))
    return out


def additional_drivers(spec: Mapping[str, Any], table: Mapping[str, Any],
                       existing: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    from .model_forecast_driver import _cells_of, is_structured_schema
    sources = {s["concept"]: s for s in table.get("filed_lines") or [] if s.get("status") == "filed"}
    concepts = {d.get("concept") for d in existing}
    out = []
    declared_rows = list(spec.get("revenue_drivers") or [])
    declared_rows += [{"ref": "fcf-" + c["role"], "basis_concept": c["concept"], "label": c["role"]}
                      for c in (spec.get("fcf_definition") or {}).get("components") or []
                      if c["role"] not in {"operating_cash_flow", "capital_expenditure"}]
    for declared in declared_rows:
        concept = declared.get("basis_concept")
        source = next((s for s in table.get("filed_identity_lines") or [] if s["driver_ref"]==declared["ref"] and s.get("status")=="filed"),None) if declared.get("basis_identity") else sources.get(concept)
        if source is None or (concept in concepts and (not spec.get("driver_formulas") or declared["ref"].startswith("fcf-"))):
            continue
        history = _cells_of(source, str(concept))
        units = {str(c.get("unit")).casefold() for c in (source.get("cells") or {}).values() if c.get("unit")}
        if not history or len(units) != 1:
            continue
        unit = next(iter(units))
        # The driver already uses the case-folded filed unit. Its cumulative
        # operands must use that same spelling, without changing the held
        # source, amount, period or evidence. Other units remain mismatches
        # for the strict arithmetic/source validator; this converts no units.
        for cell in history:
            for operand in cell.get("derived_from") or []:
                source_unit = operand.get("unit")
                if isinstance(source_unit, str) and source_unit.casefold() == unit:
                    operand["unit"] = unit
        driver_ref = ("fcf-input:" if declared["ref"].startswith("fcf-") else "economic:") + declared["ref"]
        out.append({"ref": driver_ref, "kind": "statement_line",
                    "concept": concept, "statement": source.get("statement"), "unit": unit,
                    "status": "filed", "role": None, "spec_rows": [declared["ref"]], "note": None,
                    "label": declared["label"], "history": history,
                    **({"statement_identity":source["statement_identity"]} if source.get("statement_identity") is not None else {}),
                    **({"statement_period_family_bindings":source["statement_period_family_bindings"]}
                       if source.get("statement_period_family_bindings") is not None else {}),
                    "structure_line_ref": driver_ref,
                    "forecast_method": "year_over_year_growth" if (seasonal_profile(history) or {}).get("significant") else "quarterly_growth", "forecast_base_ref": None})
        concepts.add(concept)
    return out


def _history(driver: Mapping[str, Any]) -> dict[str, Decimal]:
    return {str(c["period_end"]): Decimal(str(c["value"])) for c in driver.get("history") or []}


def _live(results: Sequence[Mapping[str, Any]], ref: str) -> dict[str, Decimal]:
    line = next((r for r in results if r["ref"] == ref), {})
    return {c["period"]["end"]: Decimal(str(c["value"])) for c in line.get("cells") or []
            if c.get("status") == "computed" and not c.get("superseded_by")}


def _project(driver: Mapping[str, Any], periods: Sequence[Mapping[str, Any]], assumptions: Sequence[Mapping[str, Any]] = ()) -> dict[str, Decimal]:
    from .model_forecast_driver import positive_trailing_growth
    cells = list(driver.get("history") or [])
    if not cells:
        return {}
    history = _history(driver)
    profile = seasonal_profile(cells)
    growth = positive_trailing_growth(cells)
    values = dict(history)
    out = {}
    prev = history[max(history)]
    levels = {a["period"]["end"]: Decimal(str(a["value"])) for a in assumptions if a["driver_ref"] == driver["ref"] and a["measure"] == "economic_driver_level" and not a.get("superseded_by")}
    from .model_forecast import quarter_after
    requested={p["end"] for p in periods}
    cursor={"start":max(history),"end":max(history),"calendar":"company:fiscal","kind":"quarter"}
    complete=[]
    while periods and cursor["end"]<periods[-1]["end"]:
        cursor=quarter_after(cursor);complete.append(cursor)
        if len(complete)>48: return {}
    for period in complete:
        end = period["end"]
        if end in levels:
            value = levels[end]
        elif profile and profile["significant"]:
            prior = year_ago(end, values)
            if prior is None:
                continue
            value = values[prior] * (1 + profile["year_over_year_growth"])
        elif growth is not None:
            value = prev * (1 + growth["value"])
        elif driver.get("statement") == "balance":
            value = prev  # a stock can explicitly be held; never invent a flow
        else:
            continue
        value = value.quantize(Decimal("0.000000000001"))
        prev = value
        if end in requested:out[end] = value
        values[end] = value
    return out


def _recurring_expense_proof(body, expense, source, assumptions):
    """Recheck the completion's filed recurring classification, never its prose."""
    from .forecast_coverage import ONE_OFF_CONCEPTS, MIN_HISTORY_QUARTERS
    from .model_forecast_driver import quarterly_history
    structure = body.get("financial_statement_structure") or {}
    line = next((l for l in structure.get("lines") or []
                 if source and l["ref"] == source.get("structure_line_ref")), None)
    if (source is None or line is None or source.get("status") != "filed"
            or line.get("kind") != "filed" or line.get("statement") != "income"
            or source.get("statement") != "income"
            or line.get("concept") != expense.get("basis_concept")
            or source.get("concept") != line.get("concept")
            or line["concept"] in ONE_OFF_CONCEPTS
            or line.get("forecast_method") not in {"share_of_line", "carry_forward_mean"}
            or (source.get("forecast_method"),source.get("forecast_base_ref")) !=
               (line["forecast_method"],line.get("forecast_base_ref"))):
        return None
    history = quarterly_history(source)
    if (len(history) < MIN_HISTORY_QUARTERS
            or len({c["period_end"] for c in history}) != len(history)
            or sum(Decimal(c["value"]) != 0 for c in history) * 4 < len(history) * 3):
        return None
    active = [a for a in assumptions if not a.get("superseded_by")]
    ends = {p["end"] for p in body["forecast_periods"]}
    if (len(active) != len(ends) or {a["period"]["end"] for a in active} != ends
            or any(a.get("measure") != line["forecast_method"] or not a.get("refs") for a in active)):
        return None
    from .model_forecast_driver import _structure_result_refs
    result_ref = _structure_result_refs(structure)[line["ref"]]
    if set(_live(body["results"],result_ref)) != ends:
        return None
    proof = {"contract_ref":ASSUMPTION_EXECUTION_REF,"expense_ref":expense["ref"],
             "concept":line["concept"],"driver_ref":source["ref"],"line_ref":line["ref"],
             "method":line["forecast_method"],"forecast_base_ref":line.get("forecast_base_ref"),
             "source_history_hash":content_hash(history),
             "assumption_refs":sorted(a["ref"] for a in active)}
    return {**proof,"content_hash":content_hash(proof)}


def driver_execution(body: Mapping[str, Any], spec: Mapping[str, Any], *, assumption_policy=None, source_accuracy=None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from .model_forecast_driver import _assumption, _cell_ref, _structure_historical_values
    if source_accuracy is None:
        source_accuracy = (body.get("execution_contract") or {}).get("source_accuracy")
    if source_accuracy is not None:
        from .forecast_source_accuracy import validate_shape
        validate_shape(source_accuracy, body["company_ref"])
    structure = body.get("financial_statement_structure") or {}
    lines = {l["ref"]: l for l in structure.get("lines") or []}
    drivers = list(body["drivers"])
    declarations = {d["ref"]: d for d in spec.get("revenue_drivers") or []}
    # A cash historical denominator is evidence, not a new economic driver.
    # In particular it must not replace a declared filed leaf with the same
    # concept when resolving an executed revenue formula.
    from .cash_flow_filed_tie_base import drivers as filed_cash_history_drivers
    historical_only = {d["ref"] for d in filed_cash_history_drivers(body.get("cash_flow_companion") or {})}
    by_concept = {d.get("concept"): d for d in drivers
                  if d.get("concept") and d.get("ref") not in historical_only}
    by_line = {d.get("structure_line_ref"): d for d in drivers}
    plans = copy.deepcopy(spec.get("driver_formulas") or [])
    # A legacy specification with exactly one total/mix identity has no
    # disaggregated economic claim. Its existing direct identity is executable.
    automatic_identity = False
    if not plans and len(declarations) == 1:
        declared = next(iter(declarations.values()))
        revenue = next((d for d in drivers if d.get("role") == "revenue"), None)
        if declared.get("kind") == "mix" and revenue and declared.get("basis_concept") == revenue["concept"]:
            automatic_identity = True
            plans = [{"output_ref": revenue.get("structure_line_ref") or "revenue", "operator": "sum",
                      "driver_refs": [declared["ref"]], "because": "explicit total/mix identity"}]
    historical = _structure_historical_values(drivers, structure)[0] if structure else {}
    statuses = {ref: {"ref": ref, "status": "unexecuted", "reason": "驱动计划未执行：未进入预测公式"}
                for ref in declarations}
    overrides = []
    executed = []
    with localcontext() as ctx:
        ctx.prec = 60
        for plan in plans:
            refs = list(plan.get("driver_refs") or [])
            inputs = [next((d for d in drivers if d["ref"] == f"economic:{ref}"), None if declarations.get(ref,{}).get("basis_identity") else by_concept.get(declarations.get(ref, {}).get("basis_concept"))) for ref in refs]
            target = by_line.get(plan["output_ref"])
            if target is None and not structure and plan["output_ref"] == "revenue":
                target = next((d for d in drivers if d.get("role") == "revenue"), None)
            reason = None
            if not inputs or any(d is None or not d.get("history") for d in inputs) or target is None:
                reason = "驱动计划未执行：公式缺少已申报的量/价/分部/合同基底"
            elif len(inputs) > 1 and len({(d['concept'],content_hash(d.get('statement_identity'))) for d in inputs}) != len(inputs):
                reason = "驱动计划未执行：多个经济驱动错误绑定同一集团总额"
            elif plan["operator"] == "product" and {declarations[r]["kind"] for r in refs} != {"volume", "price"}:
                reason = "驱动计划未执行：乘法必须是数量×价格"
            elif plan["operator"] == "contract_conversion" and declarations[refs[0]]["kind"] != "contract_book":
                reason = "驱动计划未执行：合同转化必须绑定合同余额"
            elif plan["operator"] in {"sum", "contract_conversion"} and any(d.get("unit") != target.get("unit") for d in inputs):
                reason = "驱动计划未执行：分部/合同基底与收入的申报单位不一致"
            elif plan["operator"] == "product":
                quantity = next(d for r,d in zip(refs,inputs) if declarations[r]["kind"] == "volume")
                price = next(d for r,d in zip(refs,inputs) if declarations[r]["kind"] == "price")
                price_unit = str(price.get("unit") or "").casefold()
                currency = str(target.get("unit") or "").casefold()
                denominator = re.sub("^" + re.escape(currency) + r"(?:_per_|per)", "", price_unit)
                quantity_unit = str(quantity.get("unit") or "").casefold().rstrip("s")
                compatible = denominator != price_unit and (denominator.rstrip("s") == quantity_unit or {denominator.rstrip("s"),quantity_unit} <= {"count","unit"})
                if not compatible: reason = "驱动计划未执行：数量×单价的单位不能还原收入货币"
            if reason:
                for ref in refs:
                    if ref in statuses: statuses[ref].update(reason=reason)
                executed.append({**plan, "status": "unexecuted", "reason": reason})
                continue
            histories = [_history(d) for d in inputs]
            target_history = historical.get(plan["output_ref"], _history(target))
            common = sorted(set(target_history).intersection(*(set(h) for h in histories)))
            conversion = None
            if plan["operator"] == "contract_conversion":
                samples = [target_history[end] / histories[0][end] for end in common if histories[0][end] != 0]
                if samples: conversion = sum(samples[-4:]) / len(samples[-4:])
            def calculate(values):
                if plan["operator"] == "sum": return sum(values)
                if plan["operator"] == "product": return values[0] * values[1]
                return None if conversion is None else values[0] * conversion
            accuracy_checks = []
            def period_ties(end):
                delta = calculate([h[end] for h in histories]) - target_history[end]
                if abs(delta) <= max(Decimal("0.01"), abs(target_history[end]) * Decimal("0.000001")):
                    return True
                if plan["operator"] != "sum" or source_accuracy is None:
                    return False
                from .forecast_source_accuracy import history_assessment
                assessment = history_assessment(source_accuracy, inputs, target, end)
                if assessment is not None:
                    # The proof must compare the exact output used by this DAG,
                    # not a different reported output from the same period.
                    if Decimal(assessment["reported_delta"]) != delta:
                        return False
                    accuracy_checks.append(assessment)
                return assessment is not None and assessment["status"] == "within_disclosed_accuracy"
            tied = (len(common) >= 4 and (plan["operator"] == "contract_conversion" and conversion is not None
                    or all(period_ties(end) for end in common)))
            projected = [_project(d, body["forecast_periods"], body["assumptions"]) for d in inputs]
            ends = [p["end"] for p in body["forecast_periods"]]
            if not tied or any(not set(ends) <= set(v) for v in projected):
                reason = "驱动计划未执行：历史公式未核对闭合或未来驱动缺少可复算基底"
                for ref in refs: statuses[ref].update(reason=reason)
                executed.append({**plan, "status": "unexecuted", "reason": reason})
                continue
            evidence = [_cell_ref(c) for d in inputs for c in d["history"][-8:]]
            if conversion is not None: evidence += [_cell_ref(c) for c in target["history"][-4:]]
            # Freeze each independent economic quantity as a revisable level.
            # The output formula reads these same levels, including human edits.
            for source, values in zip(inputs, projected):
                if not source["ref"].startswith("economic:"): continue
                for period in body["forecast_periods"]:
                    held = next((a for a in body["assumptions"] if a["driver_ref"] == source["ref"] and a["period"]["end"] == period["end"] and a["measure"] == "economic_driver_level" and not a.get("superseded_by")), None)
                    if held is None:
                        level_assumption=_assumption(driver_ref=source["ref"], period=period,
                            measure="economic_driver_level", value=values[period["end"]], unit=source["unit"],
                            because="filed economic driver level projected from its own history", refs=[_cell_ref(c) for c in source["history"][-8:]],
                            decided_by=body["actor_ref"], rule_ref=RULE_REF)
                        historic_levels=[Decimal(str(c["value"])) for c in source["history"]]
                        if not min(historic_levels)<=Decimal(str(values[period["end"]]))<=max(historic_levels):
                            level_assumption["outside_band"]={"reason":"The filed own-history growth mechanism projects the dated quantity or price level beyond its historical absolute level range."}
                            level_assumption["because"]+="; outside the filed level range because the own-history growth path extrapolates the dated source level"
                        overrides.append(level_assumption)
            for period in ([] if automatic_identity else body["forecast_periods"]):
                value = calculate([v[period["end"]] for v in projected])
                overrides.append(_assumption(driver_ref=target["ref"], period=period,
                    measure="mechanism_amount", value=value, unit=target.get("unit") or "usd",
                    because=f"{plan['operator']}({', '.join(refs)}): {plan['because']}" +
                            (f"; filed quarterly conversion rate {conversion}" if conversion is not None else ""),
                    refs=evidence, decided_by=body["actor_ref"], rule_ref=RULE_REF))
            for ref in refs: statuses[ref].update(status="executed", reason=None)
            executed.append({**plan, "status": "executed", "reason": None,
                             "historical_periods": common, "conversion_rate": None if conversion is None else str(conversion),
                             **({"reported_accuracy_checks": accuracy_checks} if accuracy_checks else {})})
    if assumption_policy is None:
        assumption_policy = (body.get("execution_contract") or {}).get("assumption_execution_ref")
    if assumption_policy not in (None, ASSUMPTION_EXECUTION_REF):
        raise ValueError("forecast assumption execution contract is unsupported")
    one_offs = []
    for expense in spec.get("expense_lines") or []:
        if expense.get("behaviour") != "one_off": continue
        source = by_concept.get(expense.get("basis_concept"))
        assumptions = [a for a in body["assumptions"] if source and a["driver_ref"] == source["ref"] and a.get("kind") != "actual"]
        executed_one_off = bool(source and assumptions and all(
            a.get("measure") == "zero_one_off" or "normalis" in a.get("because", "").lower() for a in assumptions))
        recurring = (_recurring_expense_proof(body,expense,source,assumptions)
                     if assumption_policy == ASSUMPTION_EXECUTION_REF else None)
        executed_one_off |= recurring is not None
        one_offs.append({**({"recurring_source_proof":recurring} if recurring is not None else {}),
                        "ref": expense["ref"], "status": "executed" if executed_one_off else "unexecuted",
                        "reason": None if executed_one_off else "驱动计划未执行：一次性项未归零或剔除基期"})
    rows = list(statuses.values()) + one_offs
    return {"rule_ref": RULE_REF, "drivers": rows, "formulas": executed,
            "status": "executed" if all(r["status"] == "executed" for r in rows) else "unexecuted",
            "publication_blockers": [r["reason"] for r in rows if r["status"] != "executed"]}, overrides


def _income_depreciation_policy(body: Mapping[str, Any]) -> Mapping[str, Any] | None:
    policy = (body.get("financial_statement_structure") or {}).get("income_depreciation_forecast_policy")
    if policy is None:
        return None
    from .forecast_coverage import INCOME_DEPRECIATION_POLICY_REF, INCOME_DEPRECIATION_METHODS, COVERAGE_CONCEPTS, MIN_HISTORY_QUARTERS
    from .model_forecast_driver import ForecastModelValidationError
    base = {k:v for k,v in policy.items() if k!="content_hash"}
    if (policy.get("contract_ref") != INCOME_DEPRECIATION_POLICY_REF
            or policy.get("content_hash") != content_hash(base)):
        raise ForecastModelValidationError("income depreciation policy hash/contract differs")
    seen = set()
    for held in policy.get("lines", []):
        driver = next((d for d in body["drivers"] if d.get("structure_line_ref") == held["line_ref"]), None)
        if (held["line_ref"] in seen or driver is None or driver.get("statement") != "income"
                or driver.get("concept") != held["concept"]
                or held["concept"] not in COVERAGE_CONCEPTS["depreciation_amortization"]
                or held["method"] not in INCOME_DEPRECIATION_METHODS
                or driver.get("forecast_method") != held["method"]
                or driver.get("forecast_base_ref") != held["forecast_base_ref"]
                or len(_history(driver)) < MIN_HISTORY_QUARTERS
                or content_hash({end:str(value) for end,value in _history(driver).items()}) != held["source_history_hash"]):
            raise ForecastModelValidationError("income depreciation policy differs from filed driver history")
        seen.add(held["line_ref"])
    if not seen:
        raise ForecastModelValidationError("income depreciation policy contains no exact income line")
    return policy


def financial_mechanisms(body: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from .forecast_coverage import COVERAGE_CONCEPTS
    from .model_forecast_driver import _assumption, _cell_ref, _structure_result_refs
    drivers = list(body["drivers"])
    structure = body.get("financial_statement_structure") or {}
    result_refs = _structure_result_refs(structure) if structure else {}
    periods = body["forecast_periods"]
    out = []
    reports = []
    assets = [d for d in drivers if d.get("role") in {"depreciable_assets", "amortizable_assets"}]
    debts = [d for d in drivers if d.get("role") in {"debt_current", "debt_noncurrent"}]
    da = [d for d in drivers if d.get("concept") in COVERAGE_CONCEPTS["depreciation_amortization"]]
    primary_da = next((d for d in da if d.get("role") == "depreciation_amortization"), da[0] if da else None)
    closing_assets = {}
    income_da_policy = _income_depreciation_policy(body)
    def amount(driver, period, value, because, refs):
        out.append(_assumption(driver_ref=driver["ref"], period=period, measure="mechanism_amount",
            value=value, unit=driver.get("unit") or "usd", because=because, refs=refs,
            decided_by=body["actor_ref"], rule_ref=RULE_REF))
    with localcontext() as ctx:
        ctx.prec = 60
        for driver in da:
            policy = income_da_policy
            if policy is not None:
                from .forecast_coverage import INCOME_DEPRECIATION_POLICY_REF
                held = next((x for x in policy["lines"] if x["line_ref"] == driver.get("structure_line_ref")), None)
                if (policy.get("contract_ref") == INCOME_DEPRECIATION_POLICY_REF and held is not None
                        and driver.get("statement") == "income" and driver.get("forecast_method") == held["method"]):
                    projected = _live(body["results"], result_refs.get(driver.get("structure_line_ref"), ""))
                    ready = set(p["end"] for p in periods) <= set(projected)
                    reports.append({"line_ref":driver.get("structure_line_ref"), "method":held["method"],
                        "status":"computed" if ready else "unavailable",
                        "reason":None if ready else "利润表折旧的明确预测假设缺少可执行历史或收入基底",
                        "periods":[], "policy_hash":policy["content_hash"]})
                    continue
            common = set(_history(driver))
            for asset in assets: common &= set(_history(asset))
            common = sorted(common)[-4:]
            capex = _live(body["results"], "result:capital_expenditure")
            # Lease principal is a financing payment, not a new asset. Software
            # and capitalized contract cost additions enter the asset roll.
            for role in ("capitalized_software", "capitalized_contract_costs"):
                additions = _live(body["results"], "result:fcf-component:" + role)
                if additions and set(capex) <= set(additions):
                    capex = {end: value + additions[end] for end, value in capex.items()}
            reason = ("缺少可折旧资产余额" if not assets else "缺少至少四季同期资产/折旧历史" if len(common) < 4
                      else "缺少可复算资本开支，资产滚动表不可执行" if not set(p["end"] for p in periods) <= set(capex) else None)
            actual_end = (body.get("forecast_policy") or {}).get("latest_actual_end")
            if reason is None and actual_end and any(max(_history(a)) < actual_end for a in assets):
                reason = "新初步实际已前移预测，但缺少该季资产/新增资本支出桥；禁止跳过已报告季滚一次资产"
            denominator = sum((sum(_history(a)[end] for a in assets) for end in common), Decimal(0))
            if reason is None and denominator <= 0: reason = "可折旧资产基底不为正"
            if reason:
                reports.append({"line_ref": driver.get("structure_line_ref"), "method": "roll_forward",
                                "status": "unavailable", "reason": reason, "periods": []})
                continue
            rate = sum((_history(driver)[end] for end in common), Decimal(0)) / denominator
            if not 0 <= rate <= 1:
                reports.append({"line_ref": driver.get("structure_line_ref"), "method": "roll_forward",
                                "status": "unavailable", "reason": "申报折旧率不在0到1内", "periods": []})
                continue
            level = sum(_history(a)[max(_history(a))] for a in assets)
            evidence = [_cell_ref(c) for d in [driver, *assets] for c in d["history"][-4:]]
            trace = []
            for period in periods:
                end = period["end"]
                # Mid-quarter commissioning: additions depreciate for half
                # of the quarter. The same rate drives both D&A and closing.
                charge = min(level + capex[end], (level + capex[end] / 2) * rate)
                closing = level + capex[end] - charge
                amount(driver, period, charge,
                       f"asset roll_forward: D&A=(opening assets {level}+capex {capex[end]}/2)×quarterly rate {rate}; closing assets={closing}", evidence)
                trace.append({"period_end": end, "opening": str(level), "capital_expenditure": str(capex[end]),
                              "depreciation_amortization": str(charge), "closing": str(closing)})
                if driver is primary_da: closing_assets[end] = closing
                level = closing
            reports.append({"line_ref": driver.get("structure_line_ref"), "method": "roll_forward",
                            "status": "computed", "reason": None, "asset_concepts": [a["concept"] for a in assets],
                            "quarterly_rate": str(rate), "periods": trace})
        # Distribute the combined roll by disclosed asset shares. Each partition
        # absorbs its share of D&A; holding intangibles while deducting all D&A
        # from PP&E could otherwise produce a fictitious negative PP&E balance.
        if closing_assets:
            total = sum(_history(a)[max(_history(a))] for a in assets)
            for asset in assets:
                share = _history(asset)[max(_history(asset))] / total
                for period in periods:
                    amount(asset, period, closing_assets[period["end"]] * share,
                           "asset roll_forward closing balance allocated by disclosed net-asset share",
                           [_cell_ref(c) for a in assets for c in a["history"][-1:]])
        for driver in [d for d in drivers if d.get("role") == "interest_expense"]:
            common = set(_history(driver))
            for debt in debts: common &= set(_history(debt))
            common = sorted(common)[-4:]
            denominator = sum((sum(_history(d)[end] for d in debts) for end in common), Decimal(0))
            if not debts or len(common) < 4 or denominator <= 0:
                reports.append({"line_ref": driver.get("structure_line_ref"), "method": "rate_times_balance",
                                "status": "unavailable", "reason": "缺少已申报债务余额/同季利息，禁止环比或均值替代", "periods": []})
                continue
            rate = sum((_history(driver)[end] for end in common), Decimal(0)) / denominator
            if not 0 <= rate <= 1:
                reports.append({"line_ref": driver.get("structure_line_ref"), "method": "rate_times_balance", "status": "unavailable", "reason": "申报利息与债务隐含季度利率不在0到1内，不能把净利息收益作融资费用", "periods": []})
                continue
            projected = [_live(body["results"], result_refs.get(d.get("structure_line_ref"), "")) for d in debts]
            trace = []
            for period in periods:
                end = period["end"]
                if any(end not in values for values in projected): continue
                balance = sum(values[end] for values in projected)
                value = balance * rate
                amount(driver, period, value, f"interest=total current+noncurrent debt {balance}×filed quarterly interest rate {rate}",
                       [_cell_ref(c) for d in [driver, *debts] for c in d["history"][-4:]])
                trace.append({"period_end": end, "debt_balance": str(balance), "quarterly_rate": str(rate), "interest": str(value)})
            reports.append({"line_ref": driver.get("structure_line_ref"), "method": "rate_times_balance",
                            "status": "computed" if len(trace) == len(periods) else "unavailable",
                            "reason": None if len(trace) == len(periods) else "预测债务余额不完整", "periods": trace})
    return {"lines": reports}, out


def _apply_overrides(body: dict[str, Any], overrides: Sequence[Mapping[str, Any]]) -> None:
    from .model_forecast_driver import compute_structured_model_results, compute_results, chain_base, revenue_anchor
    index = {(a["driver_ref"], a["period"]["end"]): dict(a) for a in overrides}
    assumptions = [a for a in body["assumptions"] if a.get("kind") == "actual" or a.get("superseded_by") or (a["driver_ref"], a["period"]["end"]) not in index]
    assumptions += list(index.values())
    body["assumptions"] = sorted(assumptions, key=lambda a: (a["driver_ref"], a["period"]["end"], a["kind"]))
    structure = body.get("financial_statement_structure")
    old = {r["ref"]: r for r in body["results"]}
    if structure:
        fresh = compute_structured_model_results(body["drivers"], body["assumptions"], body["forecast_periods"], structure, body.get("cash_flow_companion"))
    else:
        fresh = compute_results(body["drivers"], body["assumptions"], body["forecast_periods"], statements=body["statements"],
            base=chain_base(revenue_anchor(body["drivers"]), body, body["forecast_periods"][0]["end"]))
    ends = {p["end"] for p in body["forecast_periods"]}
    for r in fresh:
        carried = [c for c in old.get(r["ref"], {}).get("cells") or [] if c["period"]["end"] not in ends]
        r["cells"] = carried + r["cells"]
    body["results"] = fresh


def execute_financial_model(body: dict[str, Any], spec: Mapping[str, Any]) -> dict[str, Any]:
    from .forecast_company_fcf import append_company_fcf
    from .model_forecast_driver import _structure_result_refs
    # The stored contract freezes the exact declarations for pure replay.
    declaration = execution_spec(spec)
    prior_contract = body.get("execution_contract")
    assumption_policy = (prior_contract.get("assumption_execution_ref")
                         if isinstance(prior_contract, Mapping) else ASSUMPTION_EXECUTION_REF)
    source_accuracy = (prior_contract or {}).get("source_accuracy") if isinstance(prior_contract, Mapping) else None
    if source_accuracy is None:
        source_accuracy = spec.get("source_accuracy")
    report, overrides = driver_execution(body, declaration, assumption_policy=assumption_policy, source_accuracy=source_accuracy)
    if overrides: _apply_overrides(body, overrides)
    append_company_fcf(body, spec.get("fcf_definition"))
    mechanisms, financial_overrides = financial_mechanisms(body)
    if financial_overrides: _apply_overrides(body, financial_overrides)
    # Explicit quantity/price/segment levels are displayed and remain revisable.
    from .model_forecast_driver import _result, _computed_cell, _finish
    for driver in body["drivers"]:
        if not driver["ref"].startswith("economic:"): continue
        levels = {a["period"]["end"]: a for a in body["assumptions"] if a["driver_ref"] == driver["ref"] and a["measure"] == "economic_driver_level" and not a.get("superseded_by")}
        ref = "result:" + driver["ref"]
        line = _result(ref, None, driver["label"], driver["unit"], "economic driver level", driver_ref=driver["ref"])
        for period in body["forecast_periods"]:
            a = levels.get(period["end"])
            if a: line["cells"].append(_computed_cell(ref, period, Decimal(a["value"]), assumptions=[a["ref"]], inputs=a["refs"]))
        body["results"] = [r for r in body["results"] if r["ref"] != ref] + [_finish(line)]
    fcf = append_company_fcf(body, spec.get("fcf_definition"))
    contract = {"schema_version": SCHEMA_VERSION, "specification": declaration,
                "fcf_definition": copy.deepcopy(spec.get("fcf_definition")),
                "drivers": report, "financial_mechanisms": mechanisms, "fcf_bridge": fcf}
    if assumption_policy is not None:
        contract["assumption_execution_ref"] = assumption_policy
    if source_accuracy is not None:
        contract["source_accuracy"] = copy.deepcopy(source_accuracy)
    contract["content_hash"] = content_hash(contract)
    body["execution_contract"] = contract
    return body


def validate_execution_contract(record: Mapping[str, Any], *, publishing: bool = False) -> None:
    from .model_forecast_driver import ForecastModelValidationError
    contract = record.get("execution_contract")
    if contract is None:
        if publishing: raise ForecastModelValidationError("驱动计划未执行：新模型缺少执行合同")
        return
    if not isinstance(contract, Mapping) or contract.get("schema_version") != SCHEMA_VERSION:
        raise ForecastModelValidationError("financial execution contract is unsupported")
    original = dict(contract)
    asserted = original.pop("content_hash", None)
    if content_hash(original) != asserted:
        raise ForecastModelValidationError("financial execution contract hash differs")
    if contract.get("source_accuracy") is not None:
        from .forecast_source_accuracy import validate_shape
        validate_shape(contract["source_accuracy"], record["company_ref"])
    if contract.get("assumption_execution_ref") not in (None, ASSUMPTION_EXECUTION_REF):
        raise ForecastModelValidationError("forecast assumption execution rule is unsupported")
    if publishing:
        # A caller cannot forge an executed report and re-hash it. Re-run the
        # frozen specification and compare every formula amount with its cell.
        expected, overrides = driver_execution(record, contract["specification"])
        if expected != contract["drivers"]:
            raise ForecastModelValidationError("driver execution report does not replay")
        mechanisms, finance = financial_mechanisms(record)
        if mechanisms != contract["financial_mechanisms"]:
            raise ForecastModelValidationError("financial mechanisms do not replay")
        live = {(a["driver_ref"], a["period"]["end"]): a for a in record["assumptions"] if not a.get("superseded_by") and a.get("kind") != "actual"}
        for a in overrides + finance:
            held = live.get((a["driver_ref"], a["period"]["end"]))
            if held is None or held["measure"] != a["measure"] or Decimal(held["value"]) != Decimal(a["value"]):
                raise ForecastModelValidationError("executed formula amount differs from its forecast assumption")
        replay = copy.deepcopy(dict(record))
        execute_financial_model(replay, {**contract["specification"], "fcf_definition": contract["fcf_definition"]})
        if replay["results"] != record["results"] or replay["execution_contract"] != contract:
            raise ForecastModelValidationError("financial forecast results do not replay")
        for driver in record["drivers"]:
            from .forecast_coverage import COVERAGE_CONCEPTS
            policy = _income_depreciation_policy(record)
            income_policy = policy is not None and driver.get("statement") == "income" and any(
                x["line_ref"] == driver.get("structure_line_ref") and x["concept"] == driver.get("concept")
                and x["method"] == driver.get("forecast_method") for x in policy.get("lines", []))
            if driver.get("concept") in COVERAGE_CONCEPTS["depreciation_amortization"] and driver.get("forecast_method") != "roll_forward" and not income_policy and not (driver.get("forecast_method") == "unavailable" and any(r["line_ref"] == driver.get("structure_line_ref") and r["status"] == "unavailable" for r in mechanisms["lines"])):
                raise ForecastModelValidationError("depreciation requires asset roll_forward")
            if driver.get("role") == "interest_expense" and driver.get("forecast_method") != "rate_times_balance" and not (driver.get("forecast_method") == "unavailable" and any(r["line_ref"] == driver.get("structure_line_ref") and r["status"] == "unavailable" for r in mechanisms["lines"])):
                raise ForecastModelValidationError("interest requires debt balance × rate")
    if publishing and contract["drivers"]["status"] != "executed":
        from .model_forecast_driver import ForecastDriverExecutionRefused
        raise ForecastDriverExecutionRefused(
            "驱动计划未执行：" + "; ".join(contract["drivers"]["publication_blockers"]),
            driver_report=contract["drivers"], contract_hash=contract["content_hash"],
        )


def validate_assumption_sources(record: Mapping[str, Any], *, connection: Any | None = None) -> None:
    """New discretionary assumptions must cite a passage or held target record."""
    from .model_forecast_driver import ForecastModelValidationError
    target_prefixes = ("guidance:", "consensus:", "consensus-version:", "consensus-estimate-version:", "street-consensus:",
                       "earnings-release-outlook:")
    external_rules = {"rule:company-guidance:1", "rule:consensus-estimate:1", "rule:analyst-judgement-carried:1"}
    def admitted(ref):
        from .evidence_index import is_passage_ref, index_path, passage, EvidenceIndexError
        token = str(ref.get("ref") or "")
        if token.startswith("doc-passage:"):
            if not is_passage_ref(token): return False
            if connection is not None:
                from .earnings_release_intake import core_state_dir
                state = core_state_dir(connection)
                if state is not None and index_path(state).exists():
                    try: return passage(state, token) is not None
                    except EvidenceIndexError: return False
            return True
        if ref.get("kind") != "figure" or not token.startswith(target_prefixes): return False
        if connection is not None:
            bindings = (("consensus-estimate-version:", "consensus_estimate_versions", "version_id"),
                        ("earnings-release-outlook:", "earnings_release_outlooks", "outlook_id"))
            for prefix, table, column in bindings:
                if token.startswith(prefix) and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    return connection.execute(f"SELECT 1 FROM {table} WHERE {column}=? AND company_ref=?", (token, record["company_ref"])).fetchone() is not None
        return True
    for a in record["assumptions"]:
        if a.get("kind") == "actual" or a.get("superseded_by"): continue
        rule = (a.get("provenance") or {}).get("rule_ref")
        if a.get("kind") != "human" and rule is not None and rule not in external_rules: continue
        if not any(admitted(r) for r in a.get("refs") or []):
            raise ForecastModelValidationError("discretionary assumption must cite doc-passage or guidance/consensus record: " + a["ref"])


def validate_stored_execution_spec(connection: Any, record: Mapping[str, Any]) -> None:
    """Bind the frozen driver declaration to an admitted spec when available."""
    from .model_forecast_driver import ForecastModelValidationError
    import json
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='coverage_mission_company_model_specs'").fetchone(): return
    row = connection.execute("SELECT * FROM coverage_mission_company_model_specs WHERE spec_id=?", (record["spec_ref"],)).fetchone()
    if row is None: return  # standalone in-memory authority has no spec ledger
    metadata = json.loads(row["metadata_json"] or "{}")
    expected = {"revenue_drivers": json.loads(row["revenue_drivers_json"]),
                "expense_lines": json.loads(row["expense_lines_json"]),
                "driver_formulas": metadata.get("driver_formulas") or []}
    if row["content_hash"] != record["spec_hash"] or expected != record["execution_contract"]["specification"]:
        raise ForecastModelValidationError("executed driver specification differs from admitted company spec")
    definition = record["execution_contract"].get("fcf_definition")
    if metadata.get("fcf_definition") is not None:
        if metadata["fcf_definition"] != definition:
            raise ForecastModelValidationError("company FCF definition differs from admitted company spec")
    elif definition is not None:
        # An automatically found definition is bound to the held release, not
        # to an arbitrary source string supplied by the publishing caller.
        from .forecast_company_fcf import load_company_fcf_definition
        table = {"filed_lines": [{"concept": d["concept"], "statement": d.get("statement"), "status": "filed",
            "cells": {c["period_end"]: {"period_start": c.get("period_start"), "value": c["value"], "unit": d.get("unit")}
                      for c in d.get("history") or []}} for d in record["drivers"] if d.get("concept")]}
        held = load_company_fcf_definition(connection, record["company_ref"], table, (record.get("forecast_policy") or {}).get("fiscal_year_end"))
        fields = ("source_ref", "components", "one_off_items")
        if held is None or {k:held.get(k) for k in fields} != {k:definition.get(k) for k in fields}:
            raise ForecastModelValidationError("company FCF definition is not bound to its held release")
