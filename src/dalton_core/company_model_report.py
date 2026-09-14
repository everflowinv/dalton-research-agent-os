"""P13ao: print one company's model input table so a person can judge it.

Everything upstream of this is structure a machine reads. At some point a human
has to look at what the system thinks a company's model is and say whether it
is right, and a JSON blob is not that.

Two decisions in the layout, both about not flattering the work:

* the filed history and the model rows are shown **separately**, because they
  are not the same thing. The filed lines are what the company reported; the
  model rows are what this system proposes to model, and several of them
  usually have no history of their own. Interleaving them would let a reader
  skim past the difference.
* every row that has to be estimated is listed **by name**, not summarised as
  a count. "Seven rows need estimates" reads like progress; "billable
  capacity, bookings conversion, currency translation, acquired revenue ..."
  reads like the work it actually is.

Currency totals and counts print in millions for width. Per-share and ratio
figures retain their declared unit. The underlying value is never touched --
the table is a view, and the ledger keeps what was filed.
"""

from __future__ import annotations

import argparse
import re
import json
import sys
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .company_model_inputs import ESTIMATED, FILED, SHARED, build_model_inputs

LABEL_WIDTH = 46
CELL_WIDTH = 12
_CHINESE_MODEL_LABELS = {
    "Revenue": "营业收入",
    "Revenues": "营业收入",
    "Revenues（营业收入）": "营业收入",
    "营业收入（Revenues）": "营业收入",
    "Gross Profit": "毛利润",
    "Operating Income": "营业利润",
    "Net Income": "净利润",
}


def _model_text(value: str, fallback: Callable[[str], str]) -> str:
    """Translate only exact model field labels; prose and quotes stay intact."""

    mapped = _CHINESE_MODEL_LABELS.get(value.strip())
    return mapped if mapped is not None else fallback(value)


def _forecast_reason_text(value: str, fallback: Callable[[str], str]) -> str:
    """Translate closed forecast-engine reasons without changing the record."""
    exact = {
        "formula terms unavailable for this quarter": "本季度缺少公式所需项目",
        "revenue or cost of revenue is not available for this quarter": "本季度缺少营业收入或营业成本",
        "gross profit is not available for this quarter": "本季度缺少毛利润",
        "operating income or income tax is not available for this quarter": "本季度缺少营业利润或所得税",
        "operating cash flow or capital expenditure is not available for this quarter": "本季度缺少经营现金流或资本支出",
        "operating cash flow or capital expenditure is unavailable for this quarter": "本季度缺少经营现金流或资本支出",
        "the specification binds no filed cost-of-revenue concept": "模型规则未绑定已披露的营业成本项目",
        "the specification binds no filed income-tax or net-income concept": "模型规则未绑定已披露的所得税或净利润项目",
        "the specification binds no single filed operating-cash-flow concept": "模型规则未绑定唯一的已披露经营现金流项目",
        "the specification binds no single filed capital-expenditure concept": "模型规则未绑定唯一的已披露资本支出项目",
        "the specification marks the cash flow statement not_material": "模型规则将现金流量表标为非重大项目",
    }
    text = value.strip()
    if text in exact:
        return exact[text]
    operating_expenses = re.fullmatch(
        r"(\d+) operating expense lines are not available for this quarter", text)
    if operating_expenses:
        return f"本季度缺少 {operating_expenses.group(1)} 项营业费用"
    patterns = (
        (r"statement line .+ is explicitly unavailable for forecast", "该报表项目未提供预测值"),
        (r"forecast base .+ is unavailable for this quarter", "本季度缺少预测基准"),
        (r"formula terms unavailable for this quarter: .+", "本季度缺少公式所需项目"),
        (r"no growth assumption for .+ in this quarter", "本季度缺少增长假设"),
        (r"no share assumption for .+ in this quarter; its trailing history gives no usable rate", "本季度缺少占比假设，历史数据也无法提供可用比例"),
        (r"no cash-flow share assumption for .+ in this quarter", "本季度缺少现金流占比假设"),
        (r"no supported exact (operating_cash_flow|capital_expenditure) forecast basis is available", "缺少可核验的现金流预测基准"),
        (r".+ is not available for this quarter", "本季度缺少计算基准"),
        (r"more than one filed concept claims the cost-of-revenue role: .+", "多个已披露项目同时被标为营业成本，无法唯一确定"),
        (r"more than one filed concept claims the income-tax or net-income role", "多个已披露项目同时被标为所得税或净利润，无法唯一确定"),
        (r"structured base .+ is unavailable for this quarter", "本季度缺少结构化预测基准"),
        (r"the forecast base would make positive-outflow capital expenditure negative", "预测基准会使正向列示的资本支出变为负数，因此未计算"),
    )
    for pattern, translated in patterns:
        if re.fullmatch(pattern, text):
            return translated
    return fallback(value)


def _millions(value: Any) -> str:
    try:
        number = Decimal(str(value)) / Decimal(1_000_000)
    except (InvalidOperation, ValueError, TypeError):
        return "?"
    return f"{number:,.1f}"


def _display_value(value: Any, unit: Any) -> str:
    """Scale totals and counts, while preserving per-share and ratio values."""
    normalized = str(unit or "").casefold().replace("-", "_")
    if "per_share" in normalized or "pershare" in normalized or normalized in {
        "ratio", "percent", "percentage", "pure",
    }:
        try:
            return f"{Decimal(str(value)):,.2f}"
        except (InvalidOperation, ValueError, TypeError):
            return "?"
    if normalized in {
        "usd", "eur", "gbp", "jpy", "cny", "shares", "share", "headcount",
        "count", "employees",
    }:
        return _millions(value)
    # An unknown or absent unit is not evidence that the value is currency.
    try:
        return f"{Decimal(str(value)):,.2f}"
    except (InvalidOperation, ValueError, TypeError):
        return "?"


def _mark(cell: Mapping[str, Any]) -> str:
    # A derived figure is marked wherever it is shown. A reader who does not
    # look up the legend still sees that this one is not like the others.
    return "" if cell.get("basis") == "reported" else "*"


def render_model_inputs(table: Mapping[str, Any], *, entity_name: str | None = None) -> str:
    periods: Sequence[str] = table.get("periods") or []
    readiness = table.get("readiness") or {}
    out: list[str] = []
    title = entity_name or table.get("company_ref") or "company"
    out.append(f"模型输入  {title}")
    out.append(f"模型规格 {table.get('spec_ref')}")
    out.append(
        f"{readiness.get('period_count', 0)} 个季度 "
        f"{readiness.get('first_period')} .. {readiness.get('last_period')}"
        "；总额/数量单位为百万；每股/比率沿用标注单位；* 表示累计值推导"
    )
    out.append("")

    header = "已披露历史".ljust(LABEL_WIDTH) + "".join(
        end[2:].rjust(CELL_WIDTH) for end in periods)
    out.append(header)
    out.append("-" * len(header))
    for line in table.get("filed_lines") or []:
        label = str(line.get("label") or line["concept"])[:LABEL_WIDTH - 2]
        if line.get("is_split"):
            label = f"{label} [拆分项]"
        row = label[:LABEL_WIDTH].ljust(LABEL_WIDTH)
        if line.get("status") != FILED:
            row += f"  ({line.get('status')})"
            out.append(row)
            continue
        cells = line.get("cells") or {}
        for end in periods:
            cell = cells.get(end)
            row += ("--" if cell is None
                    else _display_value(cell["value"], cell.get("unit")) + _mark(cell)).rjust(CELL_WIDTH)
        out.append(row)

    out.append("")
    out.append("模型科目")
    out.append("-" * LABEL_WIDTH)
    for row in table.get("rows") or []:
        status = row.get("status")
        note = {
            FILED: "已披露",
            SHARED: "已披露科目的拆分项",
            ESTIMATED: "待估算：无对应披露科目",
        }.get(str(status), str(status))
        out.append(f"  {str(row['ref'])[:34]:34} {row['kind'][:14]:14} {note}")

    metrics = table.get("operating_metrics") or []
    if metrics:
        out.append("")
        out.append("经营指标（不属于财务报表科目）")
        out.append("-" * LABEL_WIDTH)
        for item in metrics:
            disclosed = "公司已披露" if item.get("disclosed") else "公司未披露"
            out.append(f"  {str(item['ref'])[:34]:34} {str(item.get('unit'))[:10]:10} {disclosed}")

    out.append("")
    out.append("仍待补齐")
    out.append("-" * LABEL_WIDTH)
    splits = readiness.get("filed_lines_needing_a_split") or []
    for item in splits:
        out.append(f"  将 {item['concept']} 拆分为：{', '.join(item['into'])}")
    unmet = readiness.get("rows_with_no_filed_history") or []
    if unmet:
        out.append(f"  无披露历史（{len(unmet)}项）：{', '.join(unmet)}")
    gaps = readiness.get("filed_lines_with_gaps") or []
    if gaps:
        out.append(f"  {len(gaps)} 个披露科目存在季度缺口 "
                   "（10-Q 不包含第四季度全年补差）")
    empty = readiness.get("filed_lines_with_no_values") or []
    if empty:
        out.append(f"  resolved but empty (look at this): {', '.join(empty)}")
    unused = readiness.get("filed_income_lines_no_row_uses") or []
    if unused:
        out.append("  已披露但尚未被模型使用的科目：")
        for item in unused:
            out.append(f"      {item['label'][:40]:40} {item['concept']}")
    if not (splits or unmet or gaps or empty or unused):
        out.append("  无")
    return "\n".join(out)


# -- P13-M2: the driver model ----------------------------------------------

DRIVER_LABEL_WIDTH = 42
# How much history to print beside a forecast. Enough to see the trend the
# assumption was taken from; the whole table is what ``render_model_inputs``
# is for, and a page that needs sideways scrolling gets read as a picture.
HISTORY_COLUMNS = 4


def _percent(value: Any, *, decimals: int = 2) -> str:
    try:
        number = Decimal(str(value)) * Decimal(100)
    except (InvalidOperation, ValueError, TypeError):
        return "?"
    return f"{number:,.{decimals}f}%"


def _visual_width(value: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
               for char in value)


def _visual_ljust(value: str, width: int) -> str:
    return value + " " * max(0, width - _visual_width(value))


def _row(label: str, history: Sequence[str], forecast: Sequence[str]) -> str:
    prefix = ""
    if _visual_width(label) > DRIVER_LABEL_WIDTH:
        prefix, label = label + "\n", ""
    return prefix + (
        _visual_ljust(label, DRIVER_LABEL_WIDTH)
        + "".join(item.rjust(CELL_WIDTH) for item in history)
        + " |"
        + "".join(item.rjust(CELL_WIDTH) for item in forecast)
    )


def render_forecast_model(
    record: Mapping[str, Any], *, entity_name: str | None = None,
    history_columns: int = HISTORY_COLUMNS,
    annual_projection: Mapping[str, Any] | None = None,
    display_text: Callable[[str], str] | None = None,
    include_technical: bool = True,
) -> str:
    """Print one ForecastModelVersion so a person can argue with it.

    The layout is the argument. Filed history and the model's own columns are
    separated by a bar, because the reader has to know at a glance which side
    of it a number came from. Every assumption prints its ``because``
    underneath itself rather than in a footnote, because an assumption whose
    reason is somewhere else is an assumption nobody checks. An unavailable
    result prints its reason where its number would be, because a blank reads
    as a zero. And a quarter that has been filed prints the actual **with what
    we had estimated underneath it** -- the estimate is not deleted when it
    turns out to be wrong, which is the only way anyone ever finds out how
    wrong this system usually is.
    """

    supplied_show = display_text or (lambda value: value)
    from .cockpit_model_display import field_label
    show = lambda value: _forecast_reason_text(
        value, lambda raw: field_label(raw, lambda item: _model_text(item, supplied_show)))
    history = [str(item) for item in (record.get("history_periods") or [])]
    history = history[-max(0, int(history_columns)):] if history_columns else []
    realised = [str(item["end"]) for item in (record.get("realised_periods") or [])]
    forecast = [str(item["end"]) for item in (record.get("forecast_periods") or [])]
    columns = realised + forecast
    history = [end for end in history if end not in set(columns)]
    drivers = list(record.get("drivers") or [])
    out: list[str] = []
    title = entity_name or record.get("company_ref") or "company"
    out.append(f"驱动模型  {title}")
    out.append(f"版本 {record.get('version')} · 更新原因：{show(str(record.get('change_reason') or '未记录'))}"
               + (f" · 决策：{show(str(record['decision']))}" if record.get("decision") else ""))
    out.append("金额单位为百万美元；股数单位为百万股；每股数据以美元/股显示；"
               "百分比显示到1位小数；"
               "* 表示由累计披露值推导")
    if realised:
        out.append(f"模型生成后新增披露：{', '.join(realised)}")
    out.append("")
    out.append(_row("", [end[2:] for end in history], [end[2:] for end in columns]))
    out.append("-" * (DRIVER_LABEL_WIDTH + CELL_WIDTH * (len(history) + len(columns)) + 2))

    out.append("驱动因素（已披露历史）")
    for driver in drivers:
        cells = {str(cell["period_end"]): cell for cell in (driver.get("history") or [])}
        label = f"  {show(str(driver.get('label') or driver.get('ref')))}"
        if driver.get("role"):
            role={"revenue":"收入驱动","cost":"成本驱动","margin":"利润率驱动"}.get(str(driver["role"]),show(str(driver["role"])))
            label = f"{label} [{role}]"
        if not cells:
            shown_label = str(driver.get('ref')) if include_technical else label.strip()
            shown_note = show(str(driver.get('note') or driver.get('status')))
            if not include_technical and _visual_width(shown_label) > DRIVER_LABEL_WIDTH - 2:
                out.append(f"  {shown_label}\n{'':42}{shown_note}")
            else:
                out.append(f"  {shown_label[:DRIVER_LABEL_WIDTH - 2]:40} {shown_note}")
            continue
        out.append(_row(
            label,
            [(_display_value(cells[end]["value"], cells[end].get("unit") or driver.get("unit"))
              + _mark(cells[end])) if end in cells else "--"
             for end in history],
            ["" for _ in columns]))
        if driver.get("note"):
            out.append(f"        {show(str(driver['note']))}")

    assumptions: dict[str, list[Mapping[str, Any]]] = {}
    for item in record.get("assumptions") or []:
        assumptions.setdefault(str(item["driver_ref"]), []).append(item)
    out.append("")
    out.append("预测假设")
    if not assumptions:
        out.append("  暂无：披露历史不足，不能外推")
    for driver in drivers:
        rows = assumptions.get(str(driver.get("ref")))
        if not rows:
            continue
        live = _live(rows)
        measure = {"share":"占比","growth":"增速","margin":"利润率"}.get(str(rows[0].get("measure")),show(str(rows[0].get("measure"))))
        kinds = sorted({{"estimate":"预测","actual":"实际","scenario":"情景"}.get(str(item.get("kind")),show(str(item.get("kind")))) for item in rows})
        out.append(_row(
            f"  {show(str(driver.get('label') or driver.get('ref')))}  [{measure}, {'/'.join(kinds)}]",
            ["" for _ in history],
            [_percent(live[end]["value"], decimals=1) if end in live else "--" for end in columns]))
        for because in dict.fromkeys(
            str(item.get("because")) for item in rows
            if not item.get("superseded_by") and item.get("kind") != "actual"
        ):
            out.append(f"        依据：{show(because)}")
        for item in rows:
            if item.get("superseded_by"):
                out.append(f"        当时假设 {_percent(item['value'], decimals=1)}，期间 "
                           f"{item['period']['end']}：{show(str(item.get('because') or '未记录依据'))}")

    out.append("")
    out.append("预测结果")
    for result in record.get("results") or []:
        live = _live(result.get("cells") or [])
        out.append(_row(
            f"  {show(str(result.get('label') or result.get('ref')))}",
            ["" for _ in history],
            [(_display_value(live[end]["value"], live[end].get("unit") or result.get("unit"))
              if live.get(end, {}).get("status") == "computed"
              else "--") if end in live else "--" for end in columns]))
        for cell in result.get("cells") or []:
            if cell.get("superseded_by") and cell.get("value") is not None:
                actual = live.get(str(cell["period"]["end"]), {})
                out.append(
                    f"        当时预测 {_display_value(cell['value'], cell.get('unit') or result.get('unit'))}，期间 "
                    f"{cell['period']['end']}；实际披露 "
                    f"{_display_value(actual.get('value'), actual.get('unit') or result.get('unit'))}")
        if result.get("status") != "computed":
            raw_reason = str(result.get('reason') or '未记录原因')
            shown_reason = show(raw_reason)
            out.append(f"        状态：{show(str(result.get('status') or '未记录'))}；"
                       f"原因：{shown_reason}")
            if include_technical and shown_reason != raw_reason:
                out.append(f"        技术原因：{raw_reason}")
        elif include_technical:
            out.append(f"        {result.get('formula')}")
    if annual_projection is not None:
        from .company_model_annual_projection import validate_projection_record

        projection = validate_projection_record(annual_projection, model=record)
        result_labels = {str(item["ref"]): str(item["label"])
                         for item in record.get("results") or []}
        out.append("")
        out.append("年度结构化财务预测")
        out.append("-" * DRIVER_LABEL_WIDTH)
        for period in projection["periods"]:
            out.append(f"  {period['label']}")
            for result_ref, outcome in period["line_outcomes"].items():
                label = show(result_labels.get(result_ref, result_ref))
                if outcome.get("status") == "computed":
                    out.append(
                        f"    {label}: {outcome.get('value')} {outcome.get('unit')}"
                    )
                else:
                    out.append(
                        f"    {label}：暂不可得："
                        f"{show(str(outcome.get('reason') or '未记录原因'))}"
                    )
        out.append("")
        out.append("年度摊薄每股收益")
        out.append("-" * DRIVER_LABEL_WIDTH)
        out.append(f"  权威记录 {projection['projection_ref']}  "
                   f"{projection['content_hash']}")
        rows = [item for item in projection["periods"]
                if item.get("historical_eps") is not None
                or item.get("forecast_eps") is not None]
        if not rows:
            out.append("  暂不可得：尚未绑定完整财年")
        for item in rows:
            outcome = item.get("historical_eps") or item.get("forecast_eps") or {}
            if outcome.get("status") == "computed":
                try:
                    shown = format(Decimal(str(outcome["value"])), ",.2f")
                except (InvalidOperation, ValueError, TypeError):
                    shown = "?"
                out.append(f"  {item['label']:16} {shown:>12}  {outcome.get('unit')}")
            else:
                out.append(f"  {item['label']:16} 暂不可得："
                           f"{show(str(outcome.get('reason') or '未记录原因'))}")
    # What the chain does not account for, named. A reader looking at operating
    # income has to be able to see which filed lines are not inside it; the
    # formula above says what was subtracted, and this says what was not.
    outside = sorted(
        str(item["concept"]) for item in drivers
        if item.get("role") is None and item.get("history") and item.get("concept"))
    if outside:
        out.append("")
        out.append("  当前计算链未覆盖的披露科目：")
        for concept in outside:
            name = next((str(item.get("label") or concept) for item in drivers if str(item.get("concept")) == concept), concept)
            out.append(f"      {concept if include_technical else show(name)}")
    out.append("")
    out.append("阅读说明")
    out.append("-" * DRIVER_LABEL_WIDTH)
    out.append("  预测依据列在对应假设下方，可据此检查和调整。")
    out.append("  “--”表示没有完成计算，下方会说明缺少什么。")
    out.append("  已披露季度显示实际值，并保留当时预测供对照。")
    out.append("")
    if include_technical:
        out.append("技术信息")
        out.append(f"  模型记录 {record.get('id')}")
        out.append(f"  模型规格 {record.get('spec_ref')} · 公式 {record.get('formula_ref')}")
        out.append(f"  生成规则 {record.get('generator_ref')}")
    out.append("")
    return "\n".join(out)


SCENARIO_WIDTH = 14
SENSITIVITY_LABEL_WIDTH = 22
# How many filed cells a scenario names before the view says "and N more".
# A mean over eleven quarters cites twenty-two of them, and printed in full
# they bury the two lines that carry the argument.
MAX_SHOWN_REFS = 3


def _where(ours: Any, band: Mapping[str, Any]) -> str:
    """Where our estimate sits in the range the company has actually lived in.

    A sentence rather than a number, and it is the point of the whole table.
    The reader who takes away one thing should take away "we are forecasting a
    margin the company has never printed" or "we are sitting on the mean".
    """

    try:
        value = Decimal(str(ours))
        low = Decimal(str(band["trough"]["value"]))
        mean = Decimal(str(band["mean"]["value"]))
        high = Decimal(str(band["peak"]["value"]))
    except (InvalidOperation, ValueError, TypeError, KeyError):
        return ""
    if value < low:
        return f"低于该历史窗口内所有披露值（低点 {_percent(low)}）"
    if value > high:
        return f"高于该历史窗口内所有披露值（高点 {_percent(high)}）"
    if value == mean:
        return "与历史均值一致"
    side = "低于" if value < mean else "高于"
    span = high - low
    if span == 0:
        return "位于没有宽度的历史区间内"
    position = (value - low) / span * Decimal(100)
    return (f"{side}历史均值，位于低点至高点区间的 "
            f"{position.quantize(Decimal('1'))}% 位置")


def render_sensitivity(
    record: Mapping[str, Any], *, entity_name: str | None = None,
    display_text: Callable[[str], str] | None = None,
) -> str:
    """Print one SensitivityProjection so a person can argue with the ranking.

    Three decisions in the layout, each about not flattering the work:

    * the band prints **with the quarter each extreme happened in**, because
      "peak 70.1%" is a number and "70.1%, in the February 2025 quarter" is a
      fact somebody can go and check;
    * our own estimate prints as a column of the what-if table rather than
      above it, so it is read as one scenario among four rather than as the
      answer with three decorations beside it;
    * an unavailable line prints its reason where its number would be. A blank
      column in a sensitivity table reads as "no sensitivity", which is the
      opposite of what a missing line means.
    """

    supplied_show = display_text or (lambda value: value)
    show = lambda value: _forecast_reason_text(value, lambda item: _model_text(item, supplied_show))
    scenario_label = lambda value: {"trough":"历史低点","mean":"历史均值","ours":"本模型","peak":"历史高点","latest":"最新值"}.get(str(value),show(str(value)))
    out: list[str] = []
    title = entity_name or record.get("company_ref") or "company"
    metric = record.get("impact_metric") or {}
    horizon = record.get("horizon") or []
    window = record.get("history_window") or {}
    selection = record.get("selection") or {}
    out.append(f"敏感性分析  {title}")
    out.append(f"版本 {record.get('version')} · "
               f"按 {show(str(metric.get('label') or metric.get('result_ref')))} 排序，"
               f"覆盖 {len(horizon)} 个季度"
               + (f"，截至 {horizon[-1]['end']}" if horizon else ""))
    out.append(f"历史区间 {window.get('first')} .. {window.get('last')} "
               f"（{window.get('quarters')} 个季度）；数值单位为百万")
    if selection.get("status") != "available":
        out.append(f"驱动选择暂不可得（{selection.get('status')}），原因见模型记录")
    out.append("")

    drivers = list(record.get("drivers") or [])
    for driver in drivers:
        band = driver.get("band") or {}
        impact = driver.get("impact") or {}
        swing = driver.get("swing") or {}
        ours = driver.get("ours") or {}
        out.append(f"#{driver.get('rank')}  {show(str(driver.get('label') or driver.get('driver_ref')))}"
                   f"  [{show(str(driver.get('measure')))}]")
        if band.get("status") == "available":
            out.append(
                f"      低点 {_percent(band['trough']['value'])} "
                f"({band['trough']['period_end']})"
                f"   均值 {_percent(band['mean']['value'])}"
                f"   高点 {_percent(band['peak']['value'])} "
                f"({band['peak']['period_end']})"
                f"   最新值 {_percent(band['latest']['value'])} "
                f"({band['latest']['period_end']})")
            out.append(f"      覆盖 {band.get('count')} 个已披露季度 "
                       f"{band.get('first_period')} .. {band.get('last_period')}")
            # The next observation in from each end, printed where a reader
            # looking at the extreme will see it. A peak far above its own
            # runner-up was one quarter and probably one event, and a scenario
            # run at it is a scenario about that event.
            for edge in ("trough", "peak"):
                runner = (band.get(edge) or {}).get("runner_up")
                if runner is not None:
                    out.append(
                        f"      次{scenario_label(edge)}：{_percent(runner['value'])} "
                        f"({runner['period_end']})")
        else:
            out.append("      历史区间暂不可得，原因见模型记录")
        if ours.get("value") is not None:
            out.append(f"      本模型 {_percent(ours['value'])}"
                       + (f" -- {_where(ours['value'], band)}"
                          if band.get("status") == "available" else ""))
        else:
            out.append("      当前假设：预测期内并非固定值")
        if impact.get("status") == "computed":
            out.append(f"      该假设变动 1 个百分点时，"
                       f"{show(str(metric.get('label')))}变动 "
                       f"{_millions(impact['delta'])} 百万（基准值的 {impact.get('percent_of_base')}%）")
        else:
            out.append("      弹性暂不可得，原因见模型记录")
        if swing.get("status") == "computed":
            out.append(f"      在自身历史区间内，{show(str(metric.get('label')))}变动 "
                       f"{_millions(swing['swing'])} 百万（基准值的 {swing.get('percent_of_base')}%）；"
                       "驱动因素按此排序")
        else:
            out.append("      区间影响暂不可得，原因见模型记录")

        rows = list(driver.get("what_if") or [])
        header = "".ljust(SENSITIVITY_LABEL_WIDTH) + "".join(
            scenario_label(row["scenario"]).rjust(SCENARIO_WIDTH) for row in rows)
        out.append("      " + header)
        out.append("      " + "".ljust(SENSITIVITY_LABEL_WIDTH)
                   + "".join(
                       (_percent(row["assumption_value"])
                        if row.get("assumption_value") is not None else "--"
                        ).rjust(SCENARIO_WIDTH) for row in rows))
        line_refs: list[tuple[str, str]] = []
        for row in rows:
            for line in row.get("lines") or []:
                key = (str(line["ref"]), str(line.get("label") or line["ref"]))
                if key not in line_refs:
                    line_refs.append(key)
        for ref, label in line_refs:
            cells = []
            for row in rows:
                line = next((item for item in (row.get("lines") or [])
                             if str(item["ref"]) == ref), None)
                cells.append(_millions(line["total"])
                             if line is not None and line.get("total") is not None
                             else "--")
            out.append("      " + label[:SENSITIVITY_LABEL_WIDTH].ljust(
                SENSITIVITY_LABEL_WIDTH) + "".join(
                    item.rjust(SCENARIO_WIDTH) for item in cells))
        for ref, label in line_refs:
            reasons = {str(line.get("reason")) for row in rows
                       for line in (row.get("lines") or [])
                       if str(line["ref"]) == ref and line.get("reason")}
            for reason in sorted(reasons):
                out.append(f"        {show(label)}：{show(reason)}")
        for row in rows:
            if row.get("status") != "computed":
                out.append(f"        {scenario_label(row['scenario'])}：{show(str(row.get('reason') or '未记录原因'))}")
                continue
            # Capped, and the cap is stated. A mean over eleven quarters cites
            # twenty-two filed cells; printed in full it buries the two lines
            # above it, which are the ones that carry the argument. The record
            # keeps every ref -- this is the view, and it says how many it left.
            named = sorted({
                f"{item.get('concept')}@{item.get('period_end')}"
                + (f" ({item['accession']})" if item.get("accession") else "")
                for item in (row.get("input_refs") or [])})
            if not named:
                continue
            shown = ", ".join(named[:MAX_SHOWN_REFS])
            if len(named) > MAX_SHOWN_REFS:
                shown += f"，另有 {len(named) - MAX_SHOWN_REFS} 个披露单元格"
            out.append(f"        {scenario_label(row['scenario'])}，依据：{shown}")
        out.append("")

    bridge = record.get("consensus_bridge") or {}
    out.append("一致预期对照")
    out.append("-" * DRIVER_LABEL_WIDTH)
    if bridge.get("status") != "available":
        out.append("  暂无可用的一致预期对照，原因见模型记录")
    else:
        detail = {(str(item["metric"]), str(item["period"])): item
                  for item in (record.get("bridge_detail") or [])}
        out.append("  " + "指标".ljust(16) + "期间".ljust(14)
                   + "本模型".rjust(16) + "一致预期".rjust(16) + "差额".rjust(16)
                   + "差幅 %".rjust(10))
        for row in bridge.get("metrics") or []:
            extra = detail.get((str(row["metric"]), str(row["period"])))
            out.append(
                "  " + str(row["metric"]).ljust(16)
                + str(row["period"]).ljust(14)
                + _millions(row["ours"]).rjust(16)
                + _millions(row["consensus"]).rjust(16)
                + (_millions(extra["gap_abs"]) if extra else "--").rjust(16)
                + str(row["gap_percent"]).rjust(10))
        for row in record.get("bridge_detail") or []:
            out.append(f"    {show(str(row['metric']))} {row['period']}："
                       f"{show(str(row.get('basis') or '未记录依据'))}")

    out.append("")
    out.append("阅读说明")
    out.append("-" * DRIVER_LABEL_WIDTH)
    out.append("  历史区间来自公司披露，并非人工挑选；")
    out.append("  驱动因素按目标指标在历史区间内的变化幅度排序。")
    out.append("  所有份额假设使用同一收入基数，可比较1个百分点的变化。")
    out.append("  每列均由模型重新计算，未作为正式预测发布；")
    out.append("  当前假设列代表模型自身。")
    out.append("")
    out.append("")
    out.append("  每个情景列在所示全部季度中保持同一假设水平。")
    out.append("  情景列不是路径；低点列表示整个预测期均处于低点。")
    out.append("")
    out.append("  若极值与次高或次低值相距较远，极值可能只对应单一季度或事件；")
    out.append("  表中同时列出次高或次低值，便于判断这种差距。")
    out.append("")
    out.append("技术信息")
    out.append(f"  敏感性记录 {record.get('id')} · 数值类型 {record.get('value_kind')}")
    out.append(f"  模型 {record.get('model_version_ref')} · 选择规则 {record.get('selection_rule_ref')}")
    out.append("")
    return "\n".join(out)


def _live(items: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """What each column currently says: the actual where there is one."""

    out: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if item.get("superseded_by"):
            continue
        period = item.get("period") or {}
        out[str(period.get("end"))] = item
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--company-ref", help="default: every company with a specification")
    parser.add_argument("--json", action="store_true", help="the table itself, not the view")
    parser.add_argument("--forecast", action="store_true",
                        help="the latest driver model rather than the input table")
    parser.add_argument("--sensitivity", action="store_true",
                        help="the latest sensitivity table and consensus bridge")
    args = parser.parse_args(argv)

    from .coverage_mission import CoverageMissionAuthority
    from .model_forecast_driver import ForecastModelAuthority
    from .store import DaltonStore

    store = DaltonStore(str(Path(args.state_dir).expanduser().resolve() / "core.sqlite"))
    try:
        missions = CoverageMissionAuthority(store)
        if args.company_ref:
            refs = [args.company_ref]
        else:
            refs = sorted({item["company_ref"]
                           for item in missions.company_model_specs()})
        if not refs:
            print("no company has a model specification yet")
            return 0
        if args.sensitivity:
            from .forecast_sensitivity import SensitivityProjectionAuthority

            projections = SensitivityProjectionAuthority(store)
            for index, ref in enumerate(refs):
                record = projections.latest(ref)
                if record is None:
                    print(f"{ref}: no sensitivity table yet")
                    continue
                if args.json:
                    print(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                     indent=1))
                    continue
                held = missions.statement_filings(ref)
                if index:
                    print("\n")
                print(render_sensitivity(
                    record, entity_name=held[-1]["entity_name"] if held else None))
            return 0
        if args.forecast:
            models = ForecastModelAuthority(store)
            for index, ref in enumerate(refs):
                record = models.latest(ref)
                if record is None:
                    print(f"{ref}: no driver model yet")
                    continue
                if args.json:
                    print(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=1))
                    continue
                held = missions.statement_filings(ref)
                if index:
                    print("\n")
                print(render_forecast_model(
                    record, entity_name=held[-1]["entity_name"] if held else None,
                    annual_projection=models.annual_projection(record["id"])))
            return 0
        for index, ref in enumerate(refs):
            spec = missions.latest_company_model_spec(ref)
            if spec is None:
                continue
            table = build_model_inputs(missions, spec)
            if args.json:
                print(json.dumps(table, ensure_ascii=False, sort_keys=True, indent=1))
                continue
            held = missions.statement_filings(ref)
            if index:
                print("\n")
            print(render_model_inputs(
                table, entity_name=held[-1]["entity_name"] if held else None))
        return 0
    finally:
        store.close()


if __name__ == "__main__":  # pragma: no cover - a reading tool
    sys.exit(main())


__all__ = ["main", "render_forecast_model", "render_model_inputs",
           "render_sensitivity"]
