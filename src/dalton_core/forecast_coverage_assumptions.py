"""Assumptions for a coverage (0.6) statement structure, with their sources.

One assumption per forecast leaf per quarter, chosen in a fixed order:

1. the company's own preliminary actual for that quarter (ADR-0015, the 8-K
   earnings release) -- it is what happened, not a forecast, until the 10-Q or
   10-K replaces it;
2. company guidance;
3. street consensus;
4. analyst judgement already entered on this model (a person's, or an event
   judgement's, revision of a driver assumption), carried into the new version;
5. trailing history (growth or share carried forward, a trailing-four-quarter
   mean, a one-off at zero, a balance held, a rate on a balance).

Every assumption names its source in ``provenance.rule_ref`` and cites it in
``refs``.  The model only picks the structure's key driver lines; the numbers
here are deterministic rules over held records.  Guidance and consensus are
applied to revenue, the line they speak to.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Any, Mapping, Sequence

from .forecast_seasonality import seasonal_profile, year_ago, RULE_YEAR_OVER_YEAR, RULE_SEASONAL_FACTOR

RULE_PRELIMINARY = "rule:preliminary-actual-8k:1"
RULE_GUIDANCE = "rule:company-guidance:1"
RULE_CONSENSUS = "rule:consensus-estimate:1"
RULE_JUDGEMENT = "rule:analyst-judgement-carried:1"
RULE_TRAILING = "rule:trailing-carry-forward:1"
RULE_MEAN = "rule:trailing-mean:1"
RULE_ONE_OFF = "rule:one-off-zero:1"
RULE_HOLD = "rule:hold-last:1"
RULE_RATE = "rule:rate-times-balance:1"
# 2026-10-09 (P0, research artifact quality review: META v3 compounded G&A
# +30.6%/q and R&D +13.95%/q from a quarter holding a $2.4B legal charge and
# ~$1.18B of severance, which its own spec called one-off).
RULE_NORMALISED = "rule:one-off-normalised-base:1"
RULE_GROWTH_CAP = "rule:opex-growth-invariant:1"
RULE_GUIDANCE_BOUND = "rule:company-guidance-bound:1"
# P1 (same review: CTSH shrank its share count 2.31% a quarter with no buyback
# budget behind it): a falling share count is bounded by what the forecast
# buyback line can retire at the company's own trailing shares-per-dollar.
RULE_SHARE_BOUND = "rule:share-count-buyback-bound:1"
#: An operating cost line may grow faster than revenue by at most this much a
#: quarter, for at most ``OPEX_EXCESS_QUARTERS`` forecast quarters, unless an
#: analyst assumption says otherwise for that quarter.
OPEX_EXCESS_PP = Decimal("0.02")
OPEX_EXCESS_QUARTERS = 2
#: Roles the growth invariant governs.
OPEX_ROLES = frozenset({"cost_of_revenue", "operating_expense", "company_presented_component"})
#: A quarter is a spike when it exceeds the median of the four before it by
#: this factor; with the spec flagging the line one-off and no explicit
#: amount, the excess is the one-off.
SPIKE_FACTOR = Decimal("1.5")

SOURCE_LABELS: dict[str, str] = {
    RULE_PRELIMINARY: "8-K 初步实际",
    RULE_YEAR_OVER_YEAR: "上年同季×同比增长", RULE_SEASONAL_FACTOR: "季度季节因子",
    RULE_GUIDANCE: "公司指引",
    RULE_CONSENSUS: "一致预期",
    RULE_JUDGEMENT: "分析师判断",
    RULE_TRAILING: "历史外推",
    RULE_MEAN: "历史均值延续",
    RULE_ONE_OFF: "一次性归零",
    RULE_HOLD: "最近余额持有",
    RULE_RATE: "历史利率×余额",
    RULE_NORMALISED: "基期剔除一次性项目",
    RULE_GROWTH_CAP: "费用增速上限（不超过收入增速＋2个百分点）",
    RULE_GUIDANCE_BOUND: "公司指引区间约束",
    RULE_SHARE_BOUND: "回购预算约束股数",
}
SOURCE_PRIORITY = ("preliminary", "guidance", "consensus", "judgement", "trailing")

_RATE_QUANT = Decimal("0.000000000001")
_TRAILING = 4
_PRECISION = 60


def source_label(rule_ref: Any, kind: Any = None) -> str:
    if kind == "human":
        return "分析师判断"
    if kind == "actual":
        return "申报实际"
    return SOURCE_LABELS.get(str(rule_ref or ""), "事件或人工修订" if not rule_ref else "历史外推")


def _q(value: Decimal) -> Decimal:
    return value.quantize(_RATE_QUANT, ROUND_HALF_UP)


def _pct(value: Decimal) -> str:
    return format((value * 100).quantize(Decimal("0.01"), ROUND_HALF_UP), "f")


def coverage_structure_assumptions(
    drivers: Sequence[Mapping[str, Any]], periods: Sequence[Mapping[str, Any]],
    structure: Mapping[str, Any], *, sources: Mapping[str, Any] | None = None,
    decided_by: str,
) -> list[dict[str, Any]]:
    from .model_forecast_driver import (
        ForecastModelValidationError, _assumption, _cell_ref, _decimal, _structure_historical_values,
        positive_trailing_growth, quarterly_history,
    )

    sources = dict(sources or {})
    historical, historical_refs = _structure_historical_values(drivers, structure)
    lines = {str(item["ref"]): item for item in structure.get("lines") or []}
    by_line = {str(item.get("structure_line_ref")): item for item in drivers}
    preliminary = sources.get("preliminary") or {}
    pre_end = str(preliminary.get("period_end") or "")
    pre_values = {key: _decimal(value, "preliminary value")
                  for key, value in (preliminary.get("values") or {}).items()
                  if value is not None}
    pre_ref = None
    if preliminary.get("accession"):
        pre_ref = {"kind": "filing", "ref": str(preliminary["accession"]),
                   "concept": None, "period_end": pre_end or None,
                   "accession": str(preliminary["accession"])}
    revenue_targets = {str(end): item for end, item in
                       (sources.get("revenue_targets") or {}).items()}
    judgement = {(str(item["driver_ref"]), str(item["period_end"]), str(item["measure"])): item
                 for item in sources.get("judgement") or []}
    role_count: dict[str, int] = {}
    for line in lines.values():
        role_count[str(line.get("role"))] = role_count.get(str(line.get("role")), 0) + 1
    # 2026-10-09: one-off items in the base, the revenue guidance range, and
    # the revenue growth path the opex invariant compares against.
    one_offs = sources.get("one_offs") or {}
    flagged_concepts = {str(item) for item in one_offs.get("concepts") or ()}
    explicit_one_offs = [dict(item) for item in one_offs.get("items") or ()
                         if isinstance(item, Mapping)]
    revenue_bounds = {str(end): dict(item) for end, item in
                      (sources.get("revenue_bounds") or {}).items()}
    revenue_growth: dict[str, Decimal] = {}
    buyback = buyback_funding(lines, historical)
    out: list[dict[str, Any]] = []

    def refs_of(line_ref: str, ends: Sequence[str]) -> list[dict[str, Any]]:
        seen: set[tuple[Any, ...]] = set()
        found: list[dict[str, Any]] = []
        for end in ends:
            for item in historical_refs.get(line_ref, {}).get(end, []):
                key = tuple(item.get(name) for name in ("kind", "concept", "period_end",
                                                        "accession"))
                if key not in seen:
                    seen.add(key)
                    found.append(dict(item))
        return found

    def add(driver: Mapping[str, Any], period: Mapping[str, Any], measure: str,
            value: Decimal, unit: str, because: str, refs: Sequence[Mapping[str, Any]],
            rule: str) -> None:
        out.append(_assumption(
            driver_ref=str(driver["ref"]), period=period, measure=measure,
            value=_q(value), unit=unit, because=because, refs=list(refs),
            decided_by=decided_by, rule_ref=rule))

    def judged(driver: Mapping[str, Any], end: str, measure: str) -> Mapping[str, Any] | None:
        return judgement.get((str(driver["ref"]), end, measure))

    with localcontext() as ctx:
        ctx.prec = _PRECISION
        # Revenue first: the opex growth invariant compares against its path.
        ordered = sorted(lines.items(), key=lambda item: item[1].get("role") != "revenue")
        for line_ref, line in ordered:
            if line.get("kind") != "filed":
                continue
            driver = by_line.get(line_ref)
            if driver is None:
                continue
            method = str(line.get("forecast_method"))
            role = str(line.get("role"))
            unit = str(line.get("unit") or "usd")
            values = historical.get(line_ref, {})
            ends = sorted(values)
            concept = str(line.get("concept") or "")
            line_items = [item for item in explicit_one_offs
                          if str(item.get("concept") or "") == concept
                          or str(item.get("line_ref") or "") == line_ref]
            normalised: list[dict[str, Any]] = []
            if (concept in flagged_concepts or line_items) and method != "zero_one_off" \
                    and role not in ("revenue",):
                values, normalised = normalise_base(
                    values, flagged=concept in flagged_concepts, explicit=line_items)
            norm_text = "".join(
                f"; base normalised ({RULE_NORMALISED}): {item['period_end']} filed {item['filed']} "
                f"-> {item['normalised']} (one-off {item['removed']} removed, {item['basis']}"
                + (f", {item['because']}" if item.get("because") else "") + ")"
                for item in normalised)
            norm_refs = [dict(item["ref"]) for item in normalised
                         if isinstance(item.get("ref"), Mapping)]
            if method in {"year_over_year_growth", "seasonal_factor"}:
                profile = seasonal_profile(quarterly_history(driver))
                if profile is None:
                    continue
                growth = profile["year_over_year_growth"]
                projected = dict(values)
                used = sorted(values)[-8:]
                for period in periods:
                    end = str(period["end"])
                    prior = year_ago(end, projected)
                    if prior is None:
                        continue
                    held = judged(driver, end, method)
                    rule = RULE_YEAR_OVER_YEAR if method == "year_over_year_growth" else RULE_SEASONAL_FACTOR
                    rate = growth if held is None else _decimal(held["value"], "judgement")
                    level = projected[prior] * (1 + rate)
                    refs = refs_of(line_ref, used)
                    because = f"same fiscal quarter {prior} × (1 + year-over-year growth {_pct(rate)}%); seasonality retained"
                    if method == "seasonal_factor":
                        # Estimate the underlying trend after removing the
                        # annual growth; align its factor to the target date.
                        trend = (1 + growth) ** (Decimal(1) / 4)
                        raw = [Decimal(str(c["value"])) for c in profile["rows"]]
                        n = len(raw)
                        baseline = sum(raw[i] / trend ** i for i in range(n - 4, n)) / 4
                        # Forecast quarters can start after a preliminary
                        # quarter, so phase/step follow actual dated quarters.
                        last = profile["rows"][-1]["period_end"]
                        from datetime import date
                        step = round((date.fromisoformat(end) - date.fromisoformat(last)).days / 91)
                        factor = profile["factors"][(step - 1) % 4]
                        level = baseline * trend ** (n - 1 + step) * factor
                        because += f"; trend level × fiscal-quarter seasonal factor {factor}"
                    target = revenue_targets.get(end) if role == "revenue" else None
                    if target:
                        level = _decimal(target["value"], "target")
                        rate = level / projected[prior] - 1
                        rule = RULE_GUIDANCE if target.get("source") == "guidance" else RULE_CONSENSUS
                        refs = [dict(target["ref"])]
                        because = str(target.get("because"))
                    if held is not None and target is None:
                        if method == "seasonal_factor":
                            level = _decimal(held["value"], "judgement amount")
                        rule, because, refs = RULE_JUDGEMENT, str(held["because"]), list(held["refs"])
                    add(driver, period, method, rate if method == "year_over_year_growth" else level,
                        "ratio" if method == "year_over_year_growth" else unit, because, refs,
                        rule)
                    projected[end] = level
                    previous = max((key for key in projected if key < end), default=None)
                    if role == "revenue" and previous and projected[previous] != 0:
                        revenue_growth[end] = level / projected[previous] - 1
                continue
            if method == "quarterly_growth":
                profile = seasonal_profile(quarterly_history(driver))
                if profile and profile["significant"]:
                    raise ForecastModelValidationError(f"seasonal line {line_ref} cannot use average quarterly growth")
                cells = quarterly_history(driver)
                if normalised:
                    fixed = {item["period_end"]: item["normalised"] for item in normalised}
                    cells = [{**cell, "value": fixed.get(str(cell["period_end"]), cell["value"])}
                             for cell in cells]
                trailing = _ttm_growth(cells, _cell_ref) or positive_trailing_growth(cells)
                prev = _decimal(cells[-1]["value"], "history value") if cells else None
                # The model compounds from the last *filed* value; a
                # normalised base is reached by the first forecast quarter's
                # rate stepping down from it (said in that quarter's because).
                filed_last = (_decimal(quarterly_history(driver)[-1]["value"], "history value")
                              if normalised and quarterly_history(driver) else None)
                if pre_ref and pre_end and (not cells or pre_end > str(cells[-1]["period_end"])):
                    metric = "revenue" if role == "revenue" else "shares_diluted" if role == "diluted_weighted_average_shares" else None
                    if metric in pre_values:
                        prev = pre_values[metric]
                excess_quarters = 0
                for period in periods:
                    end = str(period["end"])
                    target = None
                    if role == "revenue" and end == pre_end and "revenue" in pre_values:
                        target = (pre_values["revenue"], RULE_PRELIMINARY, [pre_ref],
                                  f"the company's 8-K earnings release reports revenue of "
                                  f"{pre_values['revenue']} for the quarter ended {end} "
                                  "(preliminary until the 10-Q/10-K)")
                    elif (role == "diluted_weighted_average_shares" and end == pre_end
                          and "shares_diluted" in pre_values):
                        target = (pre_values["shares_diluted"], RULE_PRELIMINARY, [pre_ref],
                                  f"the 8-K earnings release reports {pre_values['shares_diluted']}"
                                  f" diluted shares for the quarter ended {end}")
                    elif role == "revenue" and end in revenue_targets:
                        item = revenue_targets[end]
                        rule = RULE_GUIDANCE if item.get("source") == "guidance" else RULE_CONSENSUS
                        target = (_decimal(item["value"], "target"), rule,
                                  [dict(item["ref"])], str(item.get("because")))
                    bound = revenue_bounds.get(end) if role == "revenue" else None
                    if target is not None and bound is not None and target[1] != RULE_PRELIMINARY:
                        low = _decimal(bound["low"], "guidance low")
                        high = _decimal(bound["high"], "guidance high")
                        if not low <= target[0] <= high:
                            # Guidance outranks consensus: a target outside
                            # the company's own range is held at its edge.
                            clamped = min(max(target[0], low), high)
                            target = (clamped, RULE_GUIDANCE_BOUND,
                                      [dict(bound["ref"]), *target[2]],
                                      f"{target[3]}, outside the company's revenue guidance "
                                      f"{low} to {high}, so held at {clamped}")
                    if (target is not None or judged(driver, end, "quarterly_growth") is not None) \
                            and filed_last is not None:
                        # A target or a judgement compounds from the filed value.
                        prev, filed_last = filed_last, None
                    if target is not None and prev is not None and prev != 0:
                        growth = target[0] / prev - 1
                        add(driver, period, "quarterly_growth", growth, "ratio",
                            f"{target[3]}; growth from the prior quarter {_pct(growth)}%",
                            [item for item in target[2] if item], target[1])
                        if role == "revenue":
                            revenue_growth[end] = growth
                        prev = target[0]
                        continue
                    held = judged(driver, end, "quarterly_growth")
                    if held is not None and prev is not None:
                        growth = _decimal(held["value"], "judgement")
                        add(driver, period, "quarterly_growth", growth, "ratio",
                            str(held["because"]), held["refs"], RULE_JUDGEMENT)
                        if role == "revenue":
                            revenue_growth[end] = growth
                        prev = prev * (1 + growth)
                        continue
                    if trailing is None or prev is None:
                        prev = None
                        continue
                    growth = trailing["value"]
                    rule = RULE_NORMALISED if normalised else RULE_TRAILING
                    because = (f"the trailing filed history through {trailing['last_period']} for "
                               f"structure line {line_ref} is carried forward unchanged "
                               f"({_pct(trailing['value'])}% a quarter){norm_text}")
                    refs = list(trailing["refs"]) + norm_refs
                    bound = revenue_bounds.get(end) if role == "revenue" else None
                    if bound is not None and prev != 0:
                        low = _decimal(bound["low"], "guidance low")
                        high = _decimal(bound["high"], "guidance high")
                        level = prev * (1 + growth)
                        if level < low or level > high:
                            clamped = min(max(level, low), high)
                            growth = clamped / prev - 1
                            rule = RULE_GUIDANCE_BOUND
                            because = (f"{bound.get('because') or 'company guidance'}: revenue "
                                       f"guidance {low} to {high} for the quarter ended {end}; the "
                                       f"trailing rate would give {_q(level)}, so the forecast is "
                                       f"held at the {'top' if level > high else 'bottom'} of the "
                                       f"range ({_pct(growth)}% from the prior quarter)")
                            refs = [dict(bound["ref"])] + list(trailing["refs"])
                    if role == "diluted_weighted_average_shares" and growth < 0:
                        funded = None if buyback is None else (
                            buyback["shares_per_dollar"] * buyback["quarterly_buyback"] / prev)
                        if funded is None or funded > growth:
                            floor = Decimal(0) if funded is None else min(Decimal(0), funded)
                            because = (
                                f"the trailing share count change ({_pct(growth)}% a quarter) is "
                                + ("not backed by any buyback line in the cash flow, so the count is "
                                   "held flat" if buyback is None else
                                   f"bounded by the buyback the cash flow forecasts: "
                                   f"{_q(buyback['quarterly_buyback'])} a quarter at the company's "
                                   f"own {_q(buyback['shares_per_dollar'] * -1000000)} shares retired "
                                   f"per million dollars ({buyback['first']} to {buyback['last']}) "
                                   f"retires {_pct(-floor)}% a quarter")
                                + f" ({RULE_SHARE_BOUND})")
                            growth, rule = floor, RULE_SHARE_BOUND
                            if buyback is not None:
                                refs = list(refs) + list(buyback["refs"])
                    if role in OPEX_ROLES and revenue_growth.get(end) is not None:
                        ceiling = revenue_growth[end] + OPEX_EXCESS_PP
                        held_any = judged(driver, end, "quarterly_growth") is not None
                        if growth > ceiling and not held_any:
                            excess_quarters += 1
                            if excess_quarters > OPEX_EXCESS_QUARTERS:
                                because = (f"{line_ref} would grow {_pct(growth)}% a quarter against "
                                           f"revenue {_pct(revenue_growth[end])}%; an operating cost "
                                           f"may outgrow revenue by at most "
                                           f"{_pct(OPEX_EXCESS_PP)} points a quarter for "
                                           f"{OPEX_EXCESS_QUARTERS} quarters without an analyst "
                                           f"assumption, so it is capped at {_pct(ceiling)}%"
                                           f"{norm_text}")
                                growth, rule = ceiling, RULE_GROWTH_CAP
                    applied = growth
                    if filed_last is not None and filed_last != 0:
                        applied = prev * (1 + growth) / filed_last - 1
                        because += (f"; this first quarter steps from the filed {filed_last} to the "
                                    f"normalised base {_q(prev)} grown {_pct(growth)}%, so its rate "
                                    f"is {_pct(applied)}%")
                        filed_last = None
                    add(driver, period, "quarterly_growth", applied, "ratio", because, refs, rule)
                    if role == "revenue":
                        revenue_growth[end] = growth
                    prev = prev * (1 + growth)
                continue
            if method == "share_of_line":
                base_ref = str(line.get("forecast_base_ref"))
                base_values = historical.get(base_ref, {})
                samples = [(values[end] / base_values[end], end) for end in ends
                           if base_values.get(end) not in (None, Decimal(0))]
                trailing_value = None
                used: list[str] = []
                if samples:
                    window = samples[-_TRAILING:]
                    used = [end for _value, end in window]
                    # Tax has its own bounded, discrete-item-aware rule. A
                    # loss quarter must not prevent that rule from inspecting
                    # the complete held history. Other shares keep their sign gate.
                    if role == "income_tax_expense" or len({base_values[end] > 0 for end in used}) == 1:
                        if role == "income_tax_expense":
                            trailing_value, used = _effective_rate(values, base_values, ends)
                            skipped = [end for end in discrete_tax_quarters(
                                values, base_values, ends) if end not in used]
                            if skipped and trailing_value is not None:
                                norm_text += (f"; quarters with a discrete tax item "
                                              f"({', '.join(skipped)}) are left out of the rate "
                                              f"({RULE_NORMALISED})")
                        else:
                            trailing_value = sum((value for value, _ in window),
                                                 Decimal(0)) / Decimal(len(window))
                pre_share = None
                if pre_values.get("revenue") and role == "cost_of_revenue" \
                        and role_count.get(role) == 1 and "cost_of_revenue" in pre_values \
                        and lines.get(base_ref, {}).get("role") == "revenue":
                    pre_share = pre_values["cost_of_revenue"] / pre_values["revenue"]
                if role == "income_tax_expense" and pre_values.get("pretax_income") \
                        and "income_tax" in pre_values:
                    rate = pre_values["income_tax"] / pre_values["pretax_income"]
                    pre_share = rate if Decimal(0) <= rate <= Decimal(1) else None
                for period in periods:
                    end = str(period["end"])
                    if pre_share is not None and end == pre_end:
                        add(driver, period, "share_of_line", pre_share, "ratio",
                            f"the 8-K earnings release for the quarter ended {end} implies "
                            f"{_pct(pre_share)}% of {base_ref}", [pre_ref], RULE_PRELIMINARY)
                        continue
                    held = judged(driver, end, "share_of_line")
                    if held is not None:
                        add(driver, period, "share_of_line", _decimal(held["value"], "j"),
                            "ratio", str(held["because"]), held["refs"], RULE_JUDGEMENT)
                        continue
                    if trailing_value is None:
                        continue
                    add(driver, period, "share_of_line", trailing_value, "ratio",
                        (f"the trailing filed history through {used[-1]} for structure line "
                         f"{line_ref} is carried forward unchanged ({_pct(trailing_value)}% "
                         f"of {base_ref}){norm_text}"),
                        refs_of(line_ref, used) + refs_of(base_ref, used) + norm_refs,
                        RULE_NORMALISED if normalised else RULE_TRAILING)
                continue
            if method in {"carry_forward_mean", "zero_one_off", "hold_last",
                          "rate_times_balance"}:
                value: Decimal | None = None
                used = ends[-_TRAILING:]
                rule = RULE_MEAN
                because = ""
                if method == "carry_forward_mean" and used:
                    value = sum((values[end] for end in used), Decimal(0)) / Decimal(len(used))
                    because = (f"延续: the mean of the {len(used)} filed quarters through "
                               f"{used[-1]} is carried forward")
                elif method == "zero_one_off":
                    value, rule = Decimal(0), RULE_ONE_OFF
                    used = ends[-1:]
                    because = ("the spec marks this line one-off, so the forecast holds it at "
                               "zero; the last filed value is cited")
                elif method == "hold_last" and ends:
                    used = ends[-1:]
                    value, rule = values[used[-1]], RULE_HOLD
                    because = f"the balance filed at {used[-1]} is carried forward (held)"
                elif method == "rate_times_balance":
                    base_ref = str(line.get("forecast_base_ref"))
                    base_values = historical.get(base_ref, {})
                    paired = [end for end in ends if base_values.get(end)][-_TRAILING:]
                    total_base = sum((base_values[end] for end in paired), Decimal(0))
                    if paired and total_base != 0:
                        value = sum((values[end] for end in paired), Decimal(0)) / total_base
                        used, rule = paired, RULE_RATE
                        because = (f"the trailing rate on {base_ref} over the {len(paired)} "
                                   f"quarters through {paired[-1]} ({_pct(value)}% a quarter) "
                                   "is carried forward")
                if value is None:
                    continue
                refs = refs_of(line_ref, used)
                if method == "rate_times_balance":
                    refs += refs_of(str(line.get("forecast_base_ref")), used)
                if not refs and ends:
                    refs = refs_of(line_ref, ends[-1:])
                for period in periods:
                    end = str(period["end"])
                    held = judged(driver, end, method)
                    if held is not None:
                        add(driver, period, method, _decimal(held["value"], "j"),
                            unit if method != "rate_times_balance" else "ratio",
                            str(held["because"]), held["refs"], RULE_JUDGEMENT)
                        continue
                    add(driver, period, method, value,
                        "ratio" if method == "rate_times_balance" else unit,
                        because if refs else because + " (carried forward)", refs, rule)
    if pre_ref and pre_end:
        from .model_forecast_driver import quarter_after
        for line_ref, line in lines.items():
            driver = by_line.get(line_ref)
            if driver is None:
                continue
            metric = "revenue" if line.get("role") == "revenue" else "shares_diluted" if line.get("role") == "diluted_weighted_average_shares" else None
            if metric not in pre_values:
                continue
            history = quarterly_history(driver)
            if not history or pre_end <= str(history[-1]["period_end"]):
                continue
            period = quarter_after({"end": history[-1]["period_end"], "calendar": "company:fiscal"})
            if period["end"] != pre_end:
                continue
            out.append(_assumption(driver_ref=str(driver["ref"]), period=period,
                measure="reported_actual", value=pre_values[metric], unit=str(line.get("unit") or "usd"),
                kind="actual", because="8-K preliminary actual anchors the unreported forecast quarters",
                refs=[pre_ref], decided_by=decided_by, rule_ref=RULE_PRELIMINARY))
    return sorted(out, key=lambda item: (item["driver_ref"], item["period"]["end"]))


def buyback_funding(lines: Mapping[str, Mapping[str, Any]],
                    historical: Mapping[str, Mapping[str, Decimal]]) -> dict[str, Any] | None:
    """Shares retired per dollar of buyback, and the buyback the forecast carries.

    Over the filed quarters where both the diluted share count and the
    buyback line are held: the net change in shares (dilution from share
    awards included) over the dollars spent.  The forecast buyback is the
    line's own ``carry_forward_mean`` amount (its trailing four-quarter mean).
    None without a buyback line, without four quarters, or when the count did
    not fall while buying back.
    """

    shares_ref = next((ref for ref, line in lines.items()
                       if line.get("role") == "diluted_weighted_average_shares"), None)
    buyback_ref = next((ref for ref, line in lines.items()
                        if line.get("role") == "share_repurchase"), None)
    if shares_ref is None or buyback_ref is None:
        return None
    shares, spent = historical.get(shares_ref) or {}, historical.get(buyback_ref) or {}
    share_ends = sorted(shares)
    spend_ends = sorted(spent)
    if len(share_ends) < 2 or len(spend_ends) < 4:
        return None
    first, last = share_ends[max(0, len(share_ends) - 8)], share_ends[-1]
    dollars = sum((spent[end] for end in spend_ends if first < end <= last), Decimal(0))
    change = shares[last] - shares[first]
    if dollars <= 0 or change >= 0:
        return None
    quarterly = sum((spent[end] for end in spend_ends[-4:]), Decimal(0)) / Decimal(4)
    return {"shares_per_dollar": change / dollars, "quarterly_buyback": quarterly,
            "first": first, "last": last, "refs": []}


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def normalise_base(
    values: Mapping[str, Decimal], *, flagged: bool,
    explicit: Sequence[Mapping[str, Any]] = (), window: int = 8,
) -> tuple[dict[str, Decimal], list[dict[str, Any]]]:
    """Filed quarter values with one-off items taken out of the base window.

    ``explicit`` items (``period_end``, ``amount``, ``ref``, ``because``) are a
    filed or released one-off (a legal charge, severance) and are subtracted
    from their quarter.  Without one, a line the spec flags one-off has every
    spike in the last ``window`` quarters -- a quarter above
    ``SPIKE_FACTOR`` x the median of the four before it -- replaced by that
    median.  Each change is returned as a ``normalisation`` record; nothing is
    changed for a line that is neither flagged nor given an explicit item.
    """

    out = {end: Decimal(value) for end, value in values.items()}
    records: list[dict[str, Any]] = []
    ends = sorted(out)
    recent = set(ends[-window:])
    for item in explicit:
        end = str(item.get("period_end") or "")
        try:
            amount = Decimal(str(item.get("amount")))
        except Exception:
            continue
        if end in out and end in recent and amount and abs(amount) < abs(out[end]):
            filed = out[end]
            out[end] = filed - amount
            records.append({"period_end": end, "filed": str(filed), "normalised": str(out[end]),
                            "removed": str(amount), "basis": "explicit_one_off",
                            "ref": item.get("ref"), "because": str(item.get("because") or "")})
    if flagged and not records:
        for index, end in enumerate(ends):
            if end not in recent or index < 4:
                continue
            prior = [out[e] for e in ends[index - 4:index]]
            if any(value <= 0 for value in prior):
                continue
            typical = _median(prior)
            if out[end] > typical * SPIKE_FACTOR:
                filed = out[end]
                out[end] = typical
                records.append({
                    "period_end": end, "filed": str(filed), "normalised": str(typical),
                    "removed": str(filed - typical), "basis": "spike_over_trailing_median",
                    "ref": None,
                    "because": (f"规格把这一行标为一次性（含一次性费用）；{end} 的 {filed} 高于此前四季"
                                f"中位数 {typical} 的 {SPIKE_FACTOR} 倍，超出部分视为一次性，基期按中位数正常化")})
    return out, records


def _ttm_growth(cells: Sequence[Mapping[str, Any]], cell_ref: Any) -> dict[str, Any] | None:
    """Quarterly growth implied by the last four quarters over the four before.

    ``(sum of the last 4 / sum of the 4 before) ** (1/4) - 1``: a year of
    seasonality on both sides, so one spike quarter (a legal accrual, a
    restructuring) does not become a rate compounded for two years.  Needs
    eight consecutive positive quarters; otherwise the caller falls back to
    the mean of quarter-on-quarter changes.
    """

    from datetime import date

    window = list(cells[-8:])
    if len(window) < 8:
        return None
    for prior, current in zip(window, window[1:]):
        gap = (date.fromisoformat(str(current["period_end"]))
               - date.fromisoformat(str(prior["period_end"]))).days
        if not 80 <= gap <= 100:
            return None
    values = [Decimal(str(cell["value"])) for cell in window]
    if any(value <= 0 for value in values):
        return None
    earlier, later = sum(values[:4], Decimal(0)), sum(values[4:], Decimal(0))
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        ratio = later / earlier
        growth = (ratio.ln() / 4).exp() - 1
    return {
        "value": _q(growth), "count": 8,
        "first_period": str(window[0]["period_end"]),
        "last_period": str(window[-1]["period_end"]),
        "refs": [cell_ref(cell) for cell in window],
    }


DISCRETE_TAX_PP = Decimal("0.15")


def discrete_tax_quarters(tax: Mapping[str, Decimal], pretax: Mapping[str, Decimal],
                          ends: Sequence[str]) -> list[str]:
    """Quarters of the last eight whose tax is a discrete item, not the rate."""

    window = [end for end in ends if end in tax and pretax.get(end, Decimal(0)) > 0][-8:]
    if len(window) < 4:
        return []
    rates = {end: tax[end] / pretax[end] for end in window}
    middle = _median(list(rates.values()))
    return [end for end, rate in rates.items()
            if rate < 0 or abs(rate - middle) > DISCRETE_TAX_PP]


def _effective_rate(tax: Mapping[str, Decimal], pretax: Mapping[str, Decimal],
                    ends: Sequence[str]) -> tuple[Decimal | None, list[str]]:
    """Sum of tax over sum of pretax income, inside [0, 1], or nothing.

    A benefit quarter is a real thing; a benefit carried forward as a rate is
    not.  Four quarters first, then eight; a rate outside [0, 1] is refused.
    """

    paired = [end for end in ends if end in pretax and pretax[end] != 0]
    # 2026-10-09 (P0, one-off normalisation): a quarter whose rate is a
    # discrete tax item -- a net benefit on positive pretax income (IBM's
    # -$1.4B in 2025 Q4 made its trailing rate 0.8%) or a rate more than
    # ``DISCRETE_TAX_PP`` from the median of the last eight quarters (META's
    # $19B one-time charge in 2025 Q3) -- is left out of the rate window.
    discrete = discrete_tax_quarters(tax, pretax, paired)
    if discrete and len(paired) - len(discrete) >= 4:
        paired = [end for end in paired if end not in discrete]
    for window in (4, 8):
        used = paired[-window:]
        total = sum((pretax[end] for end in used), Decimal(0))
        if used and total > 0:
            rate = sum((tax[end] for end in used), Decimal(0)) / total
            if Decimal(0) <= rate <= Decimal(1):
                return rate, used
    rates = sorted(tax[end] / pretax[end] for end in paired[-8:]
                   if pretax[end] > 0 and Decimal(0) <= tax[end] / pretax[end] <= 1)
    if rates:
        middle = rates[len(rates) // 2]
        return middle, paired[-8:]
    return None, []


def mark_outside_band(record: Mapping[str, Any],
                      assumptions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Set ``outside_band`` exactly where the invariant gate's band says so.

    The band is the gate's own (``economic_invariants.measure_band``), so a
    target from an 8-K, guidance or consensus that leaves the filed range says
    why, and a value inside the range never claims otherwise.
    """

    from .economic_invariants import measure_band

    cache: dict[tuple[str, str], Mapping[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for item in assumptions:
        item = dict(item)
        if item.get("kind") == "actual" or item.get("superseded_by"):
            out.append(item)
            continue
        key = (str(item["driver_ref"]), str(item["measure"]))
        if key not in cache:
            cache[key] = measure_band(record, key[0], key[1])
        band = cache[key]
        if band.get("status") != "available":
            out.append(item)
            continue
        value = Decimal(str(item["value"]))
        low, high = Decimal(str(band["min"])), Decimal(str(band["max"]))
        if low <= value <= high:
            item["outside_band"] = None
        elif not item.get("outside_band"):
            label = source_label((item.get("provenance") or {}).get("rule_ref"),
                                 item.get("kind"))
            item["outside_band"] = {"reason": (
                f"{label} sets {value} for this quarter, outside the filed range "
                f"{low} to {high}; the source is cited in refs")}
        out.append(item)
    return out


__all__ = [
    "RULE_CONSENSUS", "RULE_GUIDANCE", "RULE_HOLD", "RULE_JUDGEMENT", "RULE_MEAN",
    "OPEX_EXCESS_PP", "OPEX_EXCESS_QUARTERS", "RULE_GROWTH_CAP", "RULE_GUIDANCE_BOUND",
    "RULE_NORMALISED", "RULE_ONE_OFF", "RULE_PRELIMINARY", "RULE_RATE", "RULE_SHARE_BOUND",
    "RULE_TRAILING", "buyback_funding", "normalise_base",
    "SOURCE_LABELS", "SOURCE_PRIORITY", "coverage_structure_assumptions",
    "mark_outside_band", "source_label",
]
