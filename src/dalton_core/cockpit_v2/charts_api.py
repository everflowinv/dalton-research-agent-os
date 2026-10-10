"""Cockpit v2: the financial chart and table payloads.

``GET /v2/data/charts/{price,model,scenarios,returns,bridge,comparison,numbers}?company=…``

Every payload here is computed on the server, deterministically, from the same
authorities the existing cockpit reads -- nothing is re-estimated in a browser
and no number is invented to fill a chart:

* ``price``      -- the market price series (``market_price_series_versions``),
  the latest-close summary through ``CockpitPlane._market`` (so 近5日/近1月/近3月
  match the old cockpit to the digit) and the valuation snapshot's multiples
  through ``CockpitPlane._valuation``; filings as event markers.
* ``model``      -- the newest ``ForecastModelVersion``: filed history from its
  drivers, the forecast from its results, the boundary between them, and a
  margin series derived only by the model's own validated structure formulas.
* ``scenarios``  -- ADR-0012 ``company_scenario.company_view``.
* ``returns``    -- ADR-0014 holding-period return bridge, as waterfall steps.
* ``bridge``     -- ADR-0014 income bridges, each line bound to its filed
  statement line (concept, label as filed, accession, form, filing date).
* ``comparison`` -- every covered company on one basis per column; a value on
  another basis is labelled 不可比较 and is not ranked.
* ``numbers``    -- batch B: ``numbers_pack`` (consensus with corrected fiscal
  labels, broker estimate tables, release outlook, forward valuation, market
  data, cross-checks) as JSON plus its compact text.  Additive.

A workspace without the data answers ``status: "empty"`` with the reason taken
from the stage ledger (``coverage_mission_stage_records``), never a 5xx.  Only
an unusable request (unknown kind, missing ``company``) is refused, with
:class:`ChartRequestError` -> HTTP 400.

Amounts use batch v/z's formatters (``numeric_display``, ``financial_display``,
``return_attribution.amount_scale``) so the figures read like the rest of the
cockpit.  Read-only: the Core is opened ``mode=ro`` and nothing here writes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from ..terminology import label as term_label

SCHEMA_VERSION = "cockpit-v2-charts-0.1"
KINDS = ("price", "model", "scenarios", "returns", "bridge", "comparison", "numbers")
COMPANY_KINDS = frozenset(KINDS) - {"comparison"}
PRICE_RANGES = {"3m": 63, "6m": 126, "1y": 252, "3y": 756}
DEFAULT_PRICE_RANGE = "1y"
MAX_COMPANY_REF = 512


class ChartRequestError(ValueError):
    """The request itself is unusable (HTTP 400); data gaps are never this."""


# ---------------------------------------------------------------------------
# small, deterministic helpers
# ---------------------------------------------------------------------------


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _q(value: Decimal | None, quantum: str = "0.01") -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal(quantum), rounding=ROUND_HALF_UP))


def _signed(value: Decimal | None, quantum: str = "0.01", suffix: str = "") -> str | None:
    """``+1.97`` / ``−24.39`` / ``0.00``: the sign is always printed."""

    if value is None:
        return None
    shown = value.quantize(Decimal(quantum), rounding=ROUND_HALF_UP)
    if shown > 0:
        text = f"+{shown:,}"
    elif shown < 0:
        text = f"−{abs(shown):,}"
    else:
        text = f"{abs(shown):,}"
    return text + suffix


def _plain(value: Decimal) -> str:
    text = format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _table_exists(core: sqlite3.Connection, name: str) -> bool:
    return core.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _cn_date(value: Any) -> str | None:
    try:
        day = date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
    return f"{day.year}年{day.month}月{day.day}日"


def _period_text(start: Any, end: Any) -> str:
    a, b = _cn_date(start), _cn_date(end)
    if a and b:
        return f"{a}至{b}"
    return b or a or "期间未记录"


def _amount_text(value: Any) -> str | None:
    from ..numeric_display import format_display_number

    number = _dec(value)
    if number is None:
        return None
    return format_display_number(number, kind="amount_usd")


# ---------------------------------------------------------------------------
# the reader: one read-only Core, the active mission, company names
# ---------------------------------------------------------------------------


class _LatestBy:
    """Lets ``CockpitPlane._market``/``_valuation`` run on any read-only Core."""

    @staticmethod
    def _latest_by(core: Any, table: str, key: str, order: str) -> list[sqlite3.Row]:
        from ..cockpit_plane import CockpitPlane

        return CockpitPlane._latest_by(core, table, key, order)


class ChartReader:
    """What the chart payloads read: a Core opened ``mode=ro`` and its mission."""

    def __init__(self, core_db: str | Path, *, mission_ref: str | None = None,
                 clock: Callable[[], datetime] | None = None,
                 names: Callable[[], Mapping[str, str]] | None = None) -> None:
        self.core_db = Path(core_db)
        self.mission_ref = mission_ref
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._names = names or (lambda: {})

    @classmethod
    def from_plane(cls, plane: Any) -> "ChartReader":
        config = plane.config
        names = getattr(plane, "_mission_company_names", None)
        return cls(config.core_db, mission_ref=getattr(config, "mission_ref", None),
                   clock=getattr(plane, "clock", None), names=names)

    @contextmanager
    def core(self) -> Iterator[sqlite3.Connection | None]:
        """The Core read-only, or ``None`` when this host has no Core file yet."""

        if not self.core_db.exists():
            yield None
            return
        connection = sqlite3.connect(f"file:{self.core_db}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        finally:
            connection.close()

    def mission(self, core: sqlite3.Connection | None) -> dict[str, Any] | None:
        if core is None or not (_table_exists(core, "coverage_mission_versions")
                                and _table_exists(core, "coverage_mission_pointer")):
            return None
        sql = ("SELECT v.record_json FROM coverage_mission_versions v JOIN coverage_mission_pointer p "
               "ON p.mission_version_id=v.mission_version_id ")
        if self.mission_ref is not None:
            row = core.execute(sql + "WHERE p.mission_ref=?", (self.mission_ref,)).fetchone()
        else:
            row = core.execute(sql + "ORDER BY p.updated_at DESC LIMIT 1").fetchone()
        return None if row is None else json.loads(row["record_json"])

    def members(self, mission: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
        from ..cockpit_plane import COMPANY_NAMES

        if mission is None:
            return {}
        try:
            names = dict(self._names() or {})
        except Exception:  # noqa: BLE001 - a missing feed plan costs the name only
            names = {}
        members: dict[str, dict[str, Any]] = {}
        for member in mission.get("universe") or ():
            item = dict(member)
            if not item.get("name"):
                name = names.get(item.get("company_ref")) or COMPANY_NAMES.get(
                    str(item.get("ticker") or ""))
                if name:
                    item["name"] = name
            members[item["company_ref"]] = item
        return members

    @staticmethod
    def label(members: Mapping[str, Mapping[str, Any]], company_ref: str) -> str:
        """``Accenture ACN`` -- the name the company page header, the list and
        every chart card share (batch ah); never a CIK."""

        from ..financial_display import company_display_name

        if not company_ref or (company_ref not in members and str(company_ref).startswith("industry:")):
            return "行业"
        return company_display_name(members.get(company_ref), company_ref)


# ---------------------------------------------------------------------------
# the stage ledger: why something is not there yet
# ---------------------------------------------------------------------------

_WHAT = {
    "numbers": "还没有一致预期或行情数据",
    "price": "还没有行情数据",
    "model": "还没有预测模型",
    "scenarios": "还没有情景假设",
    "returns": "还没有回报归因",
    "bridge": "还没有利润桥",
}


def stage_position(core: sqlite3.Connection | None, mission: Mapping[str, Any] | None,
                   company_ref: str) -> dict[str, Any]:
    """The company's newest stage record in this mission, in the owner's words."""

    from ..mission_stage import STAGE_LABELS, STAGE_STATUS_LABELS

    if core is None or mission is None or not _table_exists(core, "coverage_mission_stage_records"):
        return {"stage_ref": None, "stage_label": None, "status": None, "status_label": None,
                "at": None, "text": "研究还没有开始"}
    row = core.execute(
        "SELECT stage_ref, status, created_at FROM coverage_mission_stage_records "
        "WHERE company_ref=? AND mission_version_ref IN (SELECT mission_version_id FROM "
        "coverage_mission_versions WHERE mission_ref=?) ORDER BY created_at DESC, record_id DESC LIMIT 1",
        (company_ref, mission.get("mission_ref")),
    ).fetchone()
    if row is None:
        return {"stage_ref": None, "stage_label": None, "status": None, "status_label": None,
                "at": None, "text": "研究还没有开始"}
    stage_label = STAGE_LABELS.get(row["stage_ref"], "研究")
    status_label = STAGE_STATUS_LABELS.get(row["status"])
    text = f"目前处于「{stage_label}」阶段" + (f"（{status_label}）" if status_label else "")
    return {"stage_ref": row["stage_ref"], "stage_label": stage_label, "status": row["status"],
            "status_label": status_label, "at": row["created_at"], "text": text}


def empty_reason(core: sqlite3.Connection | None, mission: Mapping[str, Any] | None,
                 company_ref: str, kind: str, detail: str | None = None) -> str:
    if core is None:
        return "这个研究环境还没有数据库，研究还没有开始。"
    if mission is None:
        return "这个研究环境还没有发布研究目标，因此没有覆盖公司。"
    where = stage_position(core, mission, company_ref)["text"]
    what = _WHAT.get(kind, "还没有数据")
    return f"该公司{what}：{detail + '；' if detail else ''}{where}。"


# ---------------------------------------------------------------------------
# envelope
# ---------------------------------------------------------------------------


def _envelope(kind: str, reader: ChartReader, *, company_ref: str | None,
              company: str | None) -> dict[str, Any]:
    return {"kind": kind, "schema_version": SCHEMA_VERSION, "generated_at": _iso(reader.clock()),
            "company_ref": company_ref, "company": company, "status": "ok", "empty_reason": None,
            "data_as_of": None, "currency": None, "unit": None, "period": None, "notes": []}


def _empty(payload: dict[str, Any], reason: str) -> dict[str, Any]:
    payload["status"] = "empty"
    payload["empty_reason"] = reason
    return payload


def _company_ref(query: Mapping[str, Any]) -> str:
    raw = query.get("company")
    if not isinstance(raw, str) or not raw.strip():
        raise ChartRequestError("缺少参数 company：要看哪一家公司？")
    if len(raw) > MAX_COMPANY_REF:
        raise ChartRequestError("参数 company 太长")
    return raw.strip()


# ---------------------------------------------------------------------------
# price: sparkline, latest close, changes, multiples
# ---------------------------------------------------------------------------

VALUATION_METRIC_LABELS = {
    "trailing_pe": ("GAAP市盈率（过去四季）", "倍"),
    "core_pe": ("核心营业税后市盈率（不含融资成本）", "倍"),
    "forward_pe": ("预期市盈率", "倍"),
    "price_to_sales": ("市销率（过去四季）", "倍"),
    "ev_to_ebitda": ("企业价值 / EBITDA", "倍"),
    "ev_to_sales": ("企业价值 / 收入", "倍"),
    "price_to_book": ("市净率", "倍"),
    "fcf_yield": ("自由现金流收益率", "%"),
}
CHANGE_LABELS = {5: "近 5 个交易日", 30: "近 30 个交易日", 90: "近 90 个交易日"}


def _valuation_reason(text: Any) -> str | None:
    if not text:
        return None
    raw = str(text)
    found = re.fullmatch(r"the filings held carry no (.+?) for this company", raw)
    if found:
        names = {"depreciation amortisation": "折旧与摊销", "total debt": "总债务",
                 "cash and equivalents": "现金及等价物", "operating income": "营业利润",
                 "net income": "净利润", "revenue": "营业收入"}
        parts = [names.get(part.strip(), part.strip()) for part in found.group(1).split(",")]
        return f"已取得的申报文件里没有{'、'.join(parts)}，无法计算"
    if "not positive" in raw:
        return "过去四季盈利不为正，这个倍数没有意义"
    return "暂时无法计算（原因见技术详情）"


def _metric_view(metric: Mapping[str, Any]) -> dict[str, Any]:
    label, unit = VALUATION_METRIC_LABELS.get(
        str(metric.get("metric")), (str(metric.get("label") or metric.get("metric")), ""))
    value = _dec(metric.get("value"))
    raw_unit = str(metric.get("unit") or "")
    if value is None or metric.get("status") != "available":
        display = None
    elif raw_unit == "fraction":
        display = f"{_q(value * 100, '0.1')}%"
        unit = "%"
    elif raw_unit == "percent":
        display = f"{_q(value, '0.1')}%"
        unit = "%"
    else:
        display = f"{_q(value, '0.1')} 倍"
        unit = unit or "倍"
    percentile = _dec(metric.get("percentile"))
    return {"metric": metric.get("metric"), "label": label, "unit": unit,
            "value": metric.get("value"), "display": display,
            "status": "ok" if display is not None else "missing",
            "missing_reason": None if display is not None else (
                _valuation_reason(metric.get("reason")) or "暂时无法计算"),
            "percentile": metric.get("percentile"),
            "percentile_display": None if percentile is None else f"{_q(percentile, '1')}%",
            "percentile_basis_label": metric.get("percentile_basis_label"),
            "sample_size": metric.get("sample_size"),
            "earnings_basis": metric.get("earnings_basis"), "core_method": metric.get("core_method"),
            "tax_basis": metric.get("tax_basis"), "limitations": metric.get("limitations"),
            "sample_window": metric.get("sample_window"),
            "technical_reason": metric.get("reason")}


def price_payload(reader: ChartReader, query: Mapping[str, Any]) -> dict[str, Any]:
    from ..cockpit_plane import CockpitPlane
    from ..market_price import bar_is_provisional

    company_ref = _company_ref(query)
    span = str(query.get("range") or DEFAULT_PRICE_RANGE)
    if span not in PRICE_RANGES:
        raise ChartRequestError("参数 range 只能是 3m、6m、1y 或 3y")
    with reader.core() as core:
        mission = reader.mission(core)
        members = reader.members(mission)
        payload = _envelope("price", reader, company_ref=company_ref,
                            company=reader.label(members, company_ref))
        payload.update({"range": span, "range_options": [
            {"value": key, "label": {"3m": "近 3 月", "6m": "近 6 月", "1y": "近 1 年",
                                     "3y": "近 3 年"}[key]} for key in PRICE_RANGES]})
        if core is None or not _table_exists(core, "market_price_series_versions"):
            return _empty(payload, empty_reason(core, mission, company_ref, "price",
                                                "这个环境还没有运行过行情同步"))
        row = core.execute(
            "SELECT record_json FROM market_price_series_versions WHERE company_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (company_ref,)).fetchone()
        record = None if row is None else json.loads(row["record_json"])
        bars = [bar for bar in (record or {}).get("bars") or () if _dec(bar.get("close")) is not None]
        if not bars:
            return _empty(payload, empty_reason(core, mission, company_ref, "price",
                                                "行情同步还没有为这家公司取到收盘价"))
        summary = CockpitPlane._market(_LatestBy(), core).get(company_ref) or {}
        valuation = CockpitPlane._valuation(_LatestBy(), core).get(company_ref)
        filings = []
        if _table_exists(core, "coverage_mission_statement_filings"):
            filings = [dict(item) for item in core.execute(
                "SELECT accession, form, filed, report_date FROM coverage_mission_statement_filings "
                "WHERE company_ref=? ORDER BY filed", (company_ref,))]
    window = bars[-PRICE_RANGES[span]:]
    newest = bars[-1]
    provisional = bar_is_provisional(newest)
    currency = record.get("currency") or "USD"
    points = [{"date": bar["date"], "close": str(bar["close"]),
               "provisional": bool(bar is newest and provisional)} for bar in window]
    first_day = window[0]["date"]
    events = []
    for filing in filings:
        filed = str(filing.get("filed") or "")[:10]
        if filed and first_day <= filed <= newest["date"]:
            form = str(filing.get("form") or "申报")
            events.append({"date": filed, "kind": "filing", "form": form,
                           "label": f"{form} 申报", "accession": filing.get("accession"),
                           "report_date": filing.get("report_date")})
    changes = []
    for item in summary.get("changes") or ():
        percent = _dec(item.get("percent"))
        changes.append({"days": item.get("days"),
                        "label": CHANGE_LABELS.get(item.get("days"), item.get("label")),
                        "basis": f"{item.get('days')} 个交易日收盘对收盘",
                        "percent": item.get("percent"),
                        "display": _signed(percent, "0.1", "%"),
                        "since": item.get("since"),
                        "status": "ok" if percent is not None else "missing",
                        "missing_reason": None if percent is not None else item.get("reason")})
    lows = [_dec(point["close"]) for point in points]
    metrics = [] if valuation is None else [_metric_view(item) for item in valuation.get("metrics") or ()]
    market_cap = None if valuation is None else (valuation.get("basis") or {}).get("market_cap")
    if market_cap:
        metrics.insert(0, {"metric": "market_cap", "label": "总市值", "unit": "美元",
                           "value": market_cap, "display": _amount_text(market_cap),
                           "status": "ok", "missing_reason": None, "percentile": None,
                           "percentile_display": None, "percentile_basis_label": None,
                           "sample_size": None,
                           "technical_reason": None,
                           "note": (valuation.get("basis") or {}).get("shares_note")})
    payload.update({
        "data_as_of": newest["date"], "currency": currency, "unit": f"{currency}/股",
        "period": {"start": first_day, "end": newest["date"], "bars": len(points),
                   "label": f"{_period_text(first_day, newest['date'])}，每日收盘价"},
        "latest": {"date": newest["date"], "close": str(newest["close"]),
                   "display": f"{_q(_dec(newest['close']))} {'美元' if currency == 'USD' else currency}",
                   "provisional": provisional,
                   "basis_label": "盘中价（当天还没有收盘定价）" if provisional else "收盘价",
                   "captured_at": newest.get("captured_at")},
        "points": points, "events": events, "changes": changes,
        "range_low": _q(min(lows)), "range_high": _q(max(lows)),
        "source_label": "Yahoo Finance 每日行情（由行情同步流程记录）",
        "version_ref": record.get("id"),
        "metrics": metrics,
        "metrics_as_of": None if valuation is None else valuation.get("as_of"),
        "metrics_missing_reason": (None if valuation is not None else
                                   "还没有估值快照：估值测算尚未为这家公司算出倍数"),
    })
    payload["notes"].append("价格未复权（收盘价）；涨跌幅按交易日收盘对收盘计算，与驾驶舱其他页面一致。")
    if provisional:
        payload["notes"].append("最新一根是盘中价，不是收盘价。")
    return payload


# ---------------------------------------------------------------------------
# model: history | forecast table, revenue and margin series
# ---------------------------------------------------------------------------

AMOUNT_UNITS = {"usd", "eur", "gbp", "jpy", "cny"}
SHARE_UNITS = {"shares", "share"}
PER_SHARE_UNITS = {"usd_per_share", "usdpershare", "per_share"}
RATIO_UNITS = {"ratio", "percent", "percentage", "pure", "fraction"}
SECTION_LABELS = {"income": "利润表", "cash": "现金流量表", "balance": "资产负债表", "other": "其他"}
CASH_ROLES = {"operating_cash_flow", "capital_expenditure", "free_cash_flow"}


def _unit_kind(unit: Any) -> str:
    text = str(unit or "").casefold().replace("-", "_")
    if text in AMOUNT_UNITS:
        return "amount"
    if text in SHARE_UNITS:
        return "shares"
    if "per_share" in text or text in PER_SHARE_UNITS:
        return "per_share"
    if text in {"count", "units", "unit", "number"}:
        return "count"
    if text in {"usd_per_unit", "usd/unit", "usdperunit"}:
        return "per_unit"
    if text in RATIO_UNITS:
        return "ratio"
    return "other"


def _reason_text(raw: Any, line_names: Mapping[str, str]) -> str:
    from ..company_model_report import _forecast_reason_text

    text = str(raw or "").strip()
    if not text:
        return "模型没有算出这一格，也没有记录原因"
    shown = _forecast_reason_text(text, lambda value: "模型没有算出这一格（原因见技术详情）")
    found = re.fullmatch(r"formula terms unavailable for this quarter: (.+)", text)
    if found:
        names = [line_names.get(part.strip(), part.strip()) for part in found.group(1).split(",")]
        shown = f"{shown}：{'、'.join(names)}"
    found = re.fullmatch(r"(?:statement line|forecast base) (\S+) is (?:explicitly )?unavailable.*", text)
    if found and found.group(1) in line_names:
        shown = f"{shown}（{line_names[found.group(1)]}）"
    return shown


def _live_cells(cells: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for cell in cells:
        if cell.get("superseded_by"):
            continue
        out[str((cell.get("period") or {}).get("end"))] = cell
    return out


def _superseded(cells: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str((cell.get("period") or {}).get("end")): cell for cell in cells
            if cell.get("superseded_by") and cell.get("value") is not None}


def _model_line_label(row: Mapping[str, Any]) -> str:
    """A grid row's name: Chinese, else the humanized concept; never a filler
    sentence or a raw ``us-gaap:`` id (financial review 痛点 4)."""

    from ..cockpit_model_display import native_chinese_model_text, statement_line_label
    from ..financial_display import model_line_display

    label = model_line_display(statement_line_label(row, native_chinese_model_text), row.get("concept"))
    if label.casefold() in {"cost", "opex", "tax"}:
        from ..cockpit_model_display import ROLE_LABELS
        return ROLE_LABELS.get(row.get("role"), {"cost":"营业成本", "opex":"营业费用", "tax":"所得税"}[label.casefold()])
    return label


def _match_result(driver: Mapping[str, Any], results: Mapping[str, Mapping[str, Any]],
                  role_counts: Mapping[str, int]) -> Mapping[str, Any] | None:
    role = driver.get("role")
    exact = results.get("result:" + str(driver.get("ref")))
    if exact is not None and exact.get("driver_ref") == driver.get("ref") and exact.get("unit") == driver.get("unit"):
        return exact
    if not role:
        return None
    for key in (driver.get("structure_line_ref"), driver.get("concept")):
        if key and f"result:{role}:{key}" in results:
            return results[f"result:{role}:{key}"]
    if role_counts.get(role) == 1 and f"result:{role}" in results:
        return results[f"result:{role}"]
    return None


def _cell_display(value: Decimal | None, kind: str, divisor: Decimal) -> str | None:
    from ..return_attribution import amount_in

    if value is None:
        return None
    if kind == "amount":
        return amount_in(value, divisor)
    if kind == "shares":
        return f"{(value / Decimal(1_000_000)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,}"
    if kind == "per_share":
        return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,}"
    if kind == "ratio":
        return f"{(value * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP):,}%"
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,}"


def _formula_history(structure: Mapping[str, Any], replay: Mapping[str, Any],
                     history: Mapping[str, Mapping[str, Mapping[str, Any]]]
                     ) -> dict[str, dict[str, dict[str, Any]]]:
    """Subtotals the filed history does not list, by the model's own validated formulas.

    Only a formula whose replay against the filings validated is used, and
    only in a period where every term is present.  ``basis`` is ``derived``.
    """

    validated = {item.get("output_ref") for item in (replay or {}).get("formulas") or ()
                 if item.get("status") == "validated"}
    out: dict[str, dict[str, dict[str, Any]]] = {}
    known: dict[str, Mapping[str, Mapping[str, Any]]] = dict(history)
    formulas = list((structure or {}).get("formulas") or ())
    # Formulas are not stored in dependency order (a completed structure
    # appends the subtotals it derived), so evaluate until nothing moves.
    ready = [formula for formula in formulas
             if formula.get("output_ref") in validated and formula.get("output_ref") not in known]
    ordered: list[Mapping[str, Any]] = []
    placed: set[str] = set(known)
    while ready:
        progressed = False
        for formula in list(ready):
            needs = ([t.get("line_ref") for t in formula.get("terms") or ()]
                     if formula.get("operator") == "sum" else
                     [formula.get("numerator_ref"), formula.get("denominator_ref")])
            if all(item in placed or item not in {f.get("output_ref") for f in ready}
                   for item in needs):
                ordered.append(formula)
                placed.add(formula.get("output_ref"))
                ready.remove(formula)
                progressed = True
        if not progressed:
            ordered.extend(ready)
            break
    for formula in ordered:
        output = formula.get("output_ref")
        if output not in validated or output in known:
            continue
        if formula.get("operator") == "divide":
            numerator = known.get(formula.get("numerator_ref")) or {}
            denominator = known.get(formula.get("denominator_ref")) or {}
            cells = {}
            for end in sorted(set(numerator) & set(denominator)):
                top, bottom = _dec(numerator[end].get("value")), _dec(denominator[end].get("value"))
                if top is not None and bottom:
                    cells[end] = {"value": str(top / bottom), "basis": "derived_formula"}
            if cells:
                out[output] = cells
                known[output] = cells
            continue
        if formula.get("operator") != "sum":
            continue
        terms = formula.get("terms") or ()
        periods = set.intersection(*(set(known.get(t.get("line_ref"), {})) for t in terms)) if terms else set()
        cells: dict[str, dict[str, Any]] = {}
        for end in sorted(periods):
            total = Decimal(0)
            for term in terms:
                value = _dec(known[term["line_ref"]][end].get("value"))
                coefficient = _dec(term.get("coefficient"))
                if value is None or coefficient is None:
                    break
                total += value * coefficient
            else:
                cells[end] = {"value": str(total), "basis": "derived_formula"}
        if cells:
            out[output] = cells
            known[output] = cells
    return out


def model_payload(reader: ChartReader, query: Mapping[str, Any]) -> dict[str, Any]:
    from ..return_attribution import amount_scale

    company_ref = _company_ref(query)
    with reader.core() as core:
        mission = reader.mission(core)
        members = reader.members(mission)
        payload = _envelope("model", reader, company_ref=company_ref,
                            company=reader.label(members, company_ref))
        if core is None or not _table_exists(core, "forecast_model_versions"):
            return _empty(payload, empty_reason(core, mission, company_ref, "model",
                                                "这个环境还没有开始建预测模型"))
        row = core.execute(
            "SELECT record_json FROM forecast_model_versions WHERE company_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (company_ref,)).fetchone()
        if row is None:
            has_spec = (_table_exists(core, "coverage_mission_company_model_specs") and core.execute(
                "SELECT 1 FROM coverage_mission_company_model_specs WHERE company_ref=? LIMIT 1",
                (company_ref,)).fetchone() is not None)
            detail = ("模型规格已经确定，预测数字还没有算出来" if has_spec
                      else "还没有确定模型规格（预测哪些报表行、用哪些驱动因素）")
            return _empty(payload, empty_reason(core, mission, company_ref, "model", detail))
        record = json.loads(row["record_json"])
        preliminary = _model_preliminary(core, company_ref, record)
        projection = None
        if _table_exists(core, "forecast_model_annual_projections"):
            from ..model_forecast_driver import ForecastModelAuthority
            authority = object.__new__(ForecastModelAuthority)
            authority.connection = core
            projection = authority.annual_projection(record["id"])
    result = _model_from_record(payload, record, amount_scale, preliminary=preliminary)
    if projection is not None:
        result["annual_projection"] = projection
    return result


def _model_preliminary(core: sqlite3.Connection, company_ref: str,
                       record: Mapping[str, Any]) -> dict[str, Any] | None:
    """ADR-0015 preliminary actuals for one of the model's forecast quarters."""

    try:
        from ..forecast_assumption_sources import preliminary_quarter

        held = preliminary_quarter(core, company_ref,
                                   reporting_unit=str(record.get("unit") or "usd").casefold())
    except Exception:
        return None
    if not held:
        return None
    ends = {str(item.get("end")) for item in record.get("forecast_periods") or ()}
    return held if str(held.get("period_end")) in ends else None


def _period_header(period: Mapping[str, Any] | str, kind: str) -> dict[str, Any]:
    if isinstance(period, Mapping):
        start, end = period.get("start"), str(period.get("end"))
    else:
        start, end = None, str(period)
    short = f"{end[2:4]}/{end[5:7]}" if len(end) >= 7 else end
    return {"end": end, "start": start, "kind": kind, "label": short,
            "title": f"{_period_text(start, end) if start else '截至 ' + (_cn_date(end) or end)}"
                     f"（{'预测' if kind == 'forecast' else '已披露'}）"}


# Preliminary (8-K) metrics by the model role they report.
PRELIMINARY_ROLES = {
    "revenue": "revenue", "cost_of_revenue": "cost_of_revenue",
    "operating_income": "operating_income", "pretax_income": "pretax_income",
    "income_tax": "income_tax_expense", "net_income": "net_income",
    "net_income_attributable": "parent_net_income", "eps_diluted": "diluted_eps",
    "shares_diluted": "diluted_weighted_average_shares",
}
STATEMENT_COMPLETENESS = (("income", "利润表"), ("cash", "现金流"), ("balance", "资产负债表"))
CASH_RESULT_LINES = {"net-cash-change", "net_cash_change"}


def _model_methods(record: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """方法 per structure line (and per legacy driver), and 来源 per driver."""

    from ..forecast_coverage import method_label
    from ..forecast_coverage_assumptions import source_label

    structure = record.get("financial_statement_structure") or {}
    lines = {str(item.get("ref")): item for item in structure.get("lines") or ()}
    reasons = {str(item.get("line_ref")): item.get("reason")
               for item in structure.get("coverage") or ()}
    computability = (record.get("financial_statement_structure_replay") or {}).get(
        "forecast_computability") or {}
    methods: dict[str, dict[str, Any]] = {}
    for ref, line in lines.items():
        method = str(line.get("forecast_method"))
        base = lines.get(str(line.get("forecast_base_ref"))) or {}
        reason = reasons.get(ref)
        if method == "unavailable" and not reason:
            reason = "模型规格没有为这一行选择预测方法"
        if method == "formula" and computability.get(ref):
            reason = f"公式项不可预测：{computability[ref]}"
        methods[ref] = {"code": method, "label": method_label(method, base_role=base.get("role")),
                        "reason": reason}
    for line in (record.get("cash_flow_companion") or {}).get("lines") or ():
        method = str(line.get("forecast_method"))
        base = lines.get(str(line.get("forecast_base_ref"))) or {}
        methods[str(line.get("ref"))] = {
            "code": method, "label": method_label(method, base_role=base.get("role")),
            "reason": line.get("reason") if method == "unavailable" else None}
    sources: dict[str, str] = {}
    forecast_ends = {str(item.get("end")) for item in record.get("forecast_periods") or ()}
    for item in record.get("assumptions") or ():
        if item.get("superseded_by") or str((item.get("period") or {}).get("end")) not in forecast_ends:
            continue
        label = source_label((item.get("provenance") or {}).get("rule_ref"), item.get("kind"))
        held = sources.get(str(item.get("driver_ref")))
        if held is None:
            sources[str(item.get("driver_ref"))] = label
        elif label not in held.split("、"):
            sources[str(item.get("driver_ref"))] = f"{held}、{label}"
        if not lines and str(item.get("driver_ref")) not in methods:
            measure = str(item.get("measure"))
            methods[str(item.get("driver_ref"))] = {
                "code": measure,
                "label": {"quarterly_growth": "驱动", "revenue_share": "占收入比",
                          "operating_income_share": "比率"}.get(measure, method_label(measure)),
                "reason": None}
    return methods, sources


def _legacy_formula_history(drivers: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """History for a pre-structure model's computed lines (IBM's v4).

    The legacy chain is fixed: gross profit = revenue − cost of revenue,
    operating income = gross profit − operating expenses, free cash flow =
    operating cash flow − capital expenditure.  A period appears only when
    every filed component is present.
    """

    by_role: dict[str, list[dict[str, Any]]] = {}
    for driver in drivers:
        history = {str(c.get("period_end")): c for c in driver.get("history") or ()
                   if c.get("period_start")}
        if driver.get("role") and history:
            by_role.setdefault(str(driver["role"]), []).append(history)

    def total(roles: Sequence[tuple[str, int]]) -> dict[str, Decimal]:
        series = [(hist, sign) for role, sign in roles for hist in by_role.get(role, [])]
        if not series or any(role not in by_role for role, _ in roles):
            return {}
        ends = set.intersection(*(set(hist) for hist, _ in series))
        out: dict[str, Decimal] = {}
        for end in ends:
            values = [(_dec(hist[end].get("value")), sign) for hist, sign in series]
            if all(value is not None for value, _ in values):
                out[end] = sum((value * sign for value, sign in values), Decimal(0))
        return out

    gross = total([("revenue", 1), ("cost_of_revenue", -1)])
    opex = total([("operating_expense", 1)])
    operating = {end: gross[end] - opex[end] for end in set(gross) & set(opex)}
    fcf = total([("operating_cash_flow", 1), ("capital_expenditure", -1)])
    return {key: {end: {"value": str(value), "basis": "derived_formula"}
                  for end, value in values.items()}
            for key, values in (("gross-profit", gross), ("operating-income", operating),
                                ("free-cash-flow", fcf)) if values}


def _model_from_record(payload: dict[str, Any], record: Mapping[str, Any],
                       amount_scale: Callable[[Sequence[Any]], tuple[Decimal, str]],
                       *, preliminary: Mapping[str, Any] | None = None) -> dict[str, Any]:
    drivers = list(record.get("drivers") or ())
    results = {str(item.get("ref")): item for item in record.get("results") or ()}
    role_counts: dict[str, int] = {}
    for driver in drivers:
        if driver.get("role"):
            role_counts[driver["role"]] = role_counts.get(driver["role"], 0) + 1
    history_ends = [str(item) for item in record.get("history_periods") or ()]
    realised = [dict(item) for item in record.get("realised_periods") or ()]
    forecast = [dict(item) for item in record.get("forecast_periods") or ()]
    realised_ends = {str(item.get("end")) for item in realised}
    forecast_ends = [str(item.get("end")) for item in forecast]
    history_starts: dict[str, Any] = {}
    for driver in drivers:
        for cell in driver.get("history") or ():
            history_starts.setdefault(str(cell.get("period_end")), cell.get("period_start"))
    preliminary = preliminary or (record.get("forecast_policy") or {}).get("preliminary")
    preliminary_end = None if not preliminary else str(preliminary.get("period_end"))
    columns = ([_period_header({"start": history_starts.get(end), "end": end}, "history")
                for end in history_ends if end not in realised_ends]
               + [_period_header(item, "history") for item in realised]
               + [_period_header(item, "forecast") for item in forecast])
    if preliminary_end and preliminary_end not in {column["end"] for column in columns}:
        columns.append(_period_header({"start": None, "end": preliminary_end}, "history"))
        columns.sort(key=lambda column: column["end"])
    for column in columns:
        if column["end"] == preliminary_end:
            # ADR-0015: the company's own 8-K numbers stand in this column
            # until the 10-Q/10-K lands; the realised period advances to it.
            column.update({"kind": "preliminary", "label": f"{column['label']} 初步",
                           "title": column["title"].replace("（预测）", "（初步实际（8-K），待 10-Q/10-K 核对）")})
    boundary = sum(1 for column in columns if column["kind"] in {"history", "preliminary"})
    structure = record.get("financial_statement_structure") or {}
    line_names: dict[str, str] = {}
    for line in structure.get("lines") or ():
        line_names[str(line.get("ref"))] = _model_line_label(line)
    for driver in drivers:
        if driver.get("structure_line_ref"):
            line_names.setdefault(str(driver["structure_line_ref"]), _model_line_label(driver))

    # history by structure line ref, for the formula subtotals and the margins
    history_by_line: dict[str, dict[str, dict[str, Any]]] = {}
    for driver in drivers:
        key = driver.get("structure_line_ref")
        if key:
            history_by_line[str(key)] = {str(c.get("period_end")): dict(c)
                                         for c in driver.get("history") or ()}
    derived = _formula_history(structure, record.get("financial_statement_structure_replay") or {},
                               history_by_line)
    if not structure:
        derived = _legacy_formula_history(drivers)
    elif "cash-flow:operating-cash-flow" in history_by_line and \
            "cash-flow:capital-expenditure" in history_by_line:
        ocf = history_by_line["cash-flow:operating-cash-flow"]
        capex = history_by_line["cash-flow:capital-expenditure"]
        fcf = {}
        for end in sorted(set(ocf) & set(capex)):
            a, b = _dec(ocf[end].get("value")), _dec(capex[end].get("value"))
            if a is not None and b is not None:
                fcf[end] = {"value": str(a - b), "basis": "derived_formula"}
        if fcf:
            derived["free-cash-flow"] = fcf
    methods, sources = _model_methods(record)
    preliminary_values = {
        PRELIMINARY_ROLES[key]: value for key, value in ((preliminary or {}).get("values") or {}).items()
        if key in PRELIMINARY_ROLES
    }
    role_rows: dict[str, int] = {}

    amounts: list[Any] = []
    rows: list[dict[str, Any]] = []
    used_results: set[str] = set()
    from ..model_forecast_driver import structure_result_refs

    structure_refs = structure_result_refs(structure) if structure else {}
    structure_lines = {str(item.get("ref")): item for item in structure.get("lines") or ()}

    def section(role: Any, statement: Any, line: Any = None) -> str:
        if (role in CASH_ROLES or statement == "cash"
                or str(line or "").replace("-", "_") in CASH_ROLES
                or str(line or "") in CASH_RESULT_LINES):
            return "cash"
        return str(statement) if statement in SECTION_LABELS else "income"

    def forecast_cells(result: Mapping[str, Any] | None, kind: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        if result is None:
            return out
        live = _live_cells(result.get("cells") or ())
        before = _superseded(result.get("cells") or ())
        for end in [*realised_ends, *forecast_ends]:
            cell = live.get(end)
            if cell is None:
                continue
            value = _dec(cell.get("value")) if cell.get("status") == "computed" else None
            entry = {"value": None if value is None else str(value),
                     "basis": "actual" if cell.get("kind") == "actual" else "estimate",
                     "reason": None if value is not None else _reason_text(
                         cell.get("reason") or result.get("reason"), line_names),
                     "technical_reason": cell.get("reason") or result.get("reason")}
            if end in before:
                entry["prior_estimate"] = str(before[end].get("value"))
            out[end] = entry
        return out

    for driver in drivers:
        history = {str(c.get("period_end")): c for c in driver.get("history") or ()}
        result = _match_result(driver, results, role_counts)
        if not history and result is None:
            continue
        if result is not None:
            used_results.add(str(result.get("ref")))
        kind = _unit_kind(driver.get("unit") or (result or {}).get("unit"))
        cells: dict[str, dict[str, Any]] = {}
        for end, cell in history.items():
            value = _dec(cell.get("value"))
            cells[end] = {"value": None if value is None else str(value),
                          "basis": "reported" if cell.get("basis") == "reported" else "derived",
                          "reason": None, "accessions": list(cell.get("accessions") or ())}
            if kind == "amount" and value is not None:
                amounts.append(value)
        cells.update(forecast_cells(result, kind))
        line_key = str(driver.get("structure_line_ref") or "")
        method = methods.get(line_key) or methods.get(str(driver.get("ref"))) or (
            None if result is None else {"code": "formula", "label": "公式", "reason": None})
        rows.append({"key": str(driver.get("ref")), "label": _model_line_label(driver),
                     "role": driver.get("role"), "line_ref": driver.get("structure_line_ref"),
                     "section": section(driver.get("role"), driver.get("statement")),
                     "unit_kind": kind, "cells": cells, "concept": driver.get("concept"),
                     "method": method or {"code": "unavailable", "label": "不可预测",
                                          "reason": "模型规格没有为这一行选择预测方法"},
                     "source": sources.get(str(driver.get("ref"))),
                     "has_forecast": result is not None and result.get("status") == "computed",
                     "forecast_reason": (None if result is not None else
                                         "模型规格没有为这一行选择预测方法，只有已披露历史")})
    for ref, result in results.items():
        if ref in used_results:
            continue
        kind = _unit_kind(result.get("unit"))
        cells = {}
        line = ref.split(":", 1)[1].split(":")[0].replace("_", "-") if ":" in ref else ref
        structure_line = next((key for key, value in structure_refs.items() if value == ref), None)
        for end, cell in (derived.get(str(structure_line or "")) or derived.get(line) or {}).items():
            cells[end] = {"value": cell["value"], "basis": "derived_formula", "reason": None}
            if kind == "amount":
                amounts.append(_dec(cell["value"]))
        cells.update(forecast_cells(result, kind))
        method = methods.get(str(structure_line or "")) or {
            "code": "formula", "label": "公式", "reason": None}
        statement = (structure_lines.get(str(structure_line or "")) or {}).get("statement")
        rows.append({"key": ref, "label": _model_line_label(result), "role": result.get("role"),
                     "line_ref": structure_line or line,
                     "section": section(result.get("role"), statement, line),
                     "unit_kind": kind, "cells": cells, "concept": None,
                     "method": method, "source": None,
                     "has_forecast": result.get("status") == "computed",
                     "forecast_reason": None})
    if preliminary_end and preliminary_values:
        role_counts_rows: dict[str, int] = {}
        for row in rows:
            if row.get("role"):
                role_counts_rows[str(row["role"])] = role_counts_rows.get(str(row["role"]), 0) + 1
        for row in rows:
            role = str(row.get("role") or "")
            value = preliminary_values.get(role)
            if value is None or role_counts_rows.get(role) != 1:
                continue
            held = row["cells"].get(preliminary_end) or {}
            row["cells"][preliminary_end] = {
                "value": value, "basis": "preliminary", "reason": None,
                **({"model_estimate": held.get("value")} if held.get("value") is not None else {}),
            }
            if row["unit_kind"] == "amount":
                amounts.append(_dec(value))
    divisor, amount_unit = amount_scale([value for value in amounts if value is not None])
    ends = [column["end"] for column in columns]
    missing_cells = 0
    for row in rows:
        shown = []
        for end in ends:
            cell = row["cells"].get(end)
            if cell is None:
                is_forecast = end in forecast_ends
                reason = (row.get("forecast_reason") or "模型没有这一期") if is_forecast \
                    else "这一期没有已披露数据"
                shown.append({"end": end, "value": None, "display": None, "basis": None,
                              "status": "missing", "reason": reason})
                missing_cells += 1
                continue
            value = _dec(cell.get("value"))
            entry = {"end": end, "value": cell.get("value"),
                     "display": _cell_display(value, row["unit_kind"], divisor),
                     "basis": cell.get("basis"),
                     "status": "ok" if value is not None else "missing",
                     "reason": cell.get("reason")}
            if cell.get("prior_estimate") is not None:
                entry["prior_estimate_display"] = _cell_display(
                    _dec(cell["prior_estimate"]), row["unit_kind"], divisor)
            if cell.get("model_estimate") is not None:
                entry["model_estimate_display"] = _cell_display(
                    _dec(cell["model_estimate"]), row["unit_kind"], divisor)
            if cell.get("technical_reason"):
                entry["technical_reason"] = cell["technical_reason"]
            if value is None:
                missing_cells += 1
            shown.append(entry)
        row["cells"] = shown
        row["divisor"] = str(divisor) if row["unit_kind"] == "amount" else (
            "1000000" if row["unit_kind"] == "shares" else "1")
        row["unit_label"] = {"amount": amount_unit, "shares": "百万股", "per_share": "美元/股",
                             "count": "个", "per_unit": "美元/个", "ratio": "%", "other": "原单位"}[row["unit_kind"]]
        row.pop("forecast_reason", None)
    order = {"income": 0, "cash": 1, "balance": 2, "other": 3}
    rows.sort(key=lambda row: order.get(row["section"], 9))

    series = _model_series(rows, columns)
    computed = sum(1 for item in results.values() if item.get("status") == "computed")
    blocked = [{"label": _model_line_label(item),
                "reason": _reason_text(item.get("reason"), line_names)}
               for item in results.values() if item.get("status") != "computed"]
    last_history = next((column for column in reversed(columns)
                         if column["kind"] in {"history", "preliminary"}), None)
    by_statement = {}
    for key, label in STATEMENT_COMPLETENESS:
        members = [row for row in rows if row["section"] == key]
        if members:
            by_statement[key] = {"label": label, "total": len(members),
                                 "computed": sum(1 for row in members if row.get("has_forecast"))}
    statement_label = "、".join(f"{item['label']} {item['computed']}/{item['total']}"
                                for item in by_statement.values())
    payload.update({
        "data_as_of": None if last_history is None else last_history["end"],
        "currency": record.get("currency") or "USD", "unit": amount_unit,
        "period": {"kind": "quarter", "label": "单季（公司财年口径）",
                   "history": [c["end"] for c in columns if c["kind"] == "history"],
                   "forecast": [c["end"] for c in columns if c["kind"] == "forecast"],
                   "preliminary": [c["end"] for c in columns if c["kind"] == "preliminary"]},
        "version": record.get("version"), "version_ref": record.get("id"),
        "model_created_at": record.get("created_at"),
        "columns": columns, "boundary_index": boundary, "rows": rows,
        "sections": [{"key": key, "label": label} for key, label in SECTION_LABELS.items()
                     if any(row["section"] == key for row in rows)],
        "series": series,
        "completeness": {"results_total": len(results), "results_computed": computed,
                         "results_blocked": blocked, "missing_cells": missing_cells,
                         "by_statement": by_statement,
                         "label": statement_label or f"预测结果 {computed}/{len(results)} 行已算出"},
        "preliminary": None if not preliminary_end else {
            "period": (preliminary or {}).get("period"), "period_end": preliminary_end,
            "accession": (preliminary or {}).get("accession"),
            "report_date": (preliminary or {}).get("report_date"),
            "exhibit_url": (preliminary or {}).get("exhibit_url"),
            "label": "初步实际（8-K）", "note": "初步，待 10-K/10-Q 核对",
            "rows": sorted(preliminary_values)},
        "legend": {"reported": "已披露（申报原值）", "derived": "推导（由累计披露值相减）",
                   "derived_formula": "推导（按模型已校验的结构公式由已披露行计算）",
                   "estimate": "预测（估算）", "actual": "预测期已有实际披露",
                   "preliminary": "初步实际（8-K），待 10-Q/10-K 核对"},
    })
    payload["unit_header"] = f"金额：{amount_unit}"
    payload["notes"].append(
        f"金额行统一为{amount_unit}；股数、数量、单价及每股收益按各行标注的单位显示。“—”为缺失，悬停或展开可见原因。")
    if realised:
        payload["notes"].append("部分预测期已经披露实际数，表中显示实际值并保留当时预测。")
    if preliminary_end:
        payload["notes"].append(
            f"{preliminary_end} 一列为公司 8-K 业绩发布的初步实际（{(preliminary or {}).get('accession')}），"
            "待 10-Q/10-K 核对；8-K 未给出的行仍为模型估计。")
    return payload


def _model_series(rows: Sequence[Mapping[str, Any]], columns: Sequence[Mapping[str, Any]]
                  ) -> dict[str, Any]:
    by_role: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        role = row.get("role")
        line = row.get("line_ref")
        key = role or line
        if key and key not in by_role:
            by_role[str(key)] = row
        if line and line not in by_role:
            by_role[str(line)] = row
    kinds = {column["end"]: column["kind"] for column in columns}

    def points(row: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        if row is None:
            return []
        divisor = _dec(row.get("divisor")) or Decimal(1)
        return [{"end": cell["end"], "value": cell["value"], "display": cell["display"],
                 "plot_value": (None if _dec(cell["value"]) is None else
                                _q(_dec(cell["value"]) / divisor, "0.0001")),
                 "basis": cell["basis"], "kind": kinds[cell["end"]],
                 "status": cell["status"], "reason": cell["reason"]} for cell in row["cells"]]

    revenue = by_role.get("revenue")
    out: dict[str, Any] = {
        "revenue": {"label": "营业收入", "unit": None if revenue is None else revenue["unit_label"],
                    "points": points(revenue),
                    "missing_reason": None if revenue is not None else "模型没有营业收入行"},
        "margins": [],
    }

    def margin(key: str, label: str, formula: str, numerator: Callable[[str], Decimal | None],
               basis_of: Callable[[str], list[Any]]) -> dict[str, Any]:
        result = []
        for cell in (revenue or {}).get("cells") or ():
            end = cell["end"]
            base = _dec(cell["value"])
            top = numerator(end)
            if base is None or top is None or base == 0:
                result.append({"end": end, "value": None, "display": None, "kind": kinds[end],
                               "basis": None, "status": "missing",
                               "reason": "缺少计算所需的收入或利润数据"})
                continue
            value = top / base * 100
            bases = basis_of(end) + [cell["basis"]]
            if kinds[end] == "forecast":
                basis = "estimate"
            elif kinds[end] == "preliminary":
                basis = "preliminary"
            elif all(item == "reported" for item in bases):
                basis = "reported"
            else:
                basis = "derived"
            result.append({"end": end, "value": _q(value, "0.01"),
                           "display": f"{_q(value, '0.1')}%", "kind": kinds[end],
                           "basis": basis, "status": "ok", "reason": None})
        available = sum(1 for item in result if item["status"] == "ok")
        return {"key": key, "label": label, "formula": formula, "unit": "%", "points": result,
                "history_points": sum(1 for item in result if item["status"] == "ok"
                                      and item["kind"] == "history"),
                "forecast_points": sum(1 for item in result if item["status"] == "ok"
                                       and item["kind"] == "forecast"),
                "available_points": available}

    def cell_of(row: Mapping[str, Any] | None, end: str) -> Mapping[str, Any] | None:
        if row is None:
            return None
        return next((cell for cell in row["cells"] if cell["end"] == end), None)

    operating = by_role.get("operating_income") or by_role.get("operating-income")
    if revenue is not None and operating is not None:
        out["margins"].append(margin(
            "operating_margin", "营业利润率", "营业利润 ÷ 营业收入",
            lambda end: _dec((cell_of(operating, end) or {}).get("value")),
            lambda end: [(cell_of(operating, end) or {}).get("basis")]))
    cost = by_role.get("cost_of_revenue")
    if revenue is not None and cost is not None:
        def gross(end: str) -> Decimal | None:
            base = _dec((cell_of(revenue, end) or {}).get("value"))
            spend = _dec((cell_of(cost, end) or {}).get("value"))
            return None if base is None or spend is None else base - spend
        out["margins"].append(margin(
            "gross_margin", "毛利率（按公司列报的营业成本）", "（营业收入 − 营业成本）÷ 营业收入",
            gross, lambda end: [(cell_of(cost, end) or {}).get("basis")]))
    # The margin with history *and* forecast leads; otherwise the most points.
    out["margins"].sort(key=lambda item: (not (item["history_points"] and item["forecast_points"]),
                                          -item["available_points"]))
    if not out["margins"]:
        out["margins_missing_reason"] = "模型里没有可以计算利润率的利润行"
    return out


# ---------------------------------------------------------------------------
# scenarios: bear / base / bull, triggers
# ---------------------------------------------------------------------------

SCENARIO_ORDER = {"bear": 0, "base": 1, "bull": 2}
SCENARIO_KEY_LABELS = {"bear": "悲观", "base": "基准", "bull": "乐观"}
COMPARATOR_DIRECTIONS = {"gt": 1, "gte": 1, "lt": -1, "lte": -1}
STRICT_COMPARATORS = {"gt": "严格大于", "lt": "严格小于"}


def _distance(condition: Mapping[str, Any], raw: Mapping[str, Any] | None) -> str | None:
    """「距阈值还差 …」 in the condition's own unit, or ``None``."""

    from ..company_scenario import condition_value_text

    if raw is None or condition.get("status") == "triggered":
        return None
    value, threshold = _dec(condition.get("value")), _dec(condition.get("threshold"))
    direction = COMPARATOR_DIRECTIONS.get(str(raw.get("comparator")))
    if value is None or threshold is None or direction is None:
        return None
    gap = (threshold - value) * direction
    if gap < 0:
        return None
    if gap == 0:
        strict = STRICT_COMPARATORS.get(str(raw.get("comparator")))
        return f"最新值正好等于阈值，规则要求{strict}" if strict else None
    if (raw.get("transform") or "level") != "level":
        return f"距阈值还差 {_plain(gap)} 个百分点"
    shown = condition_value_text(raw, str(gap)) or str(gap)
    if shown.startswith("0 ") or shown in {"0", "0%"}:
        shown = f"{gap.normalize():f}{'' if raw.get('transform', 'level') == 'level' else '%'}"
    return f"距阈值还差 {shown}"


def scenarios_payload(reader: ChartReader, query: Mapping[str, Any]) -> dict[str, Any]:
    from ..company_scenario import company_view, in_force, read_set_versions

    company_ref = _company_ref(query)
    with reader.core() as core:
        mission = reader.mission(core)
        members = reader.members(mission)
        payload = _envelope("scenarios", reader, company_ref=company_ref,
                            company=reader.label(members, company_ref))
        if core is None or not _table_exists(core, "company_scenario_sets"):
            return _empty(payload, empty_reason(core, mission, company_ref, "scenarios",
                                                "这个研究项目还没有生成过情景分析"))
        view = company_view(core, company_ref)
        if view is None:
            return _empty(payload, empty_reason(core, mission, company_ref, "scenarios",
                                                "还没有人提出过这家公司的情景"))
        raw: dict[tuple[str, str], Mapping[str, Any]] = {}
        versions = list(in_force(core, company_ref))
        if view.get("pending"):
            versions += read_set_versions(core, view["pending"]["set_ref"])
        for version in versions:
            for item in list(version.get("triggers") or ()) + list(version.get("invalidators") or ()):
                raw[(version["id"], item["condition_id"])] = item
    live = view.get("in_force") or []
    source = live or ((view.get("pending") or {}).get("scenarios") or [])
    state = "in_force" if live else ("pending" if source else "none")
    scenarios = []
    for item in sorted(source, key=lambda s: (SCENARIO_ORDER.get(s.get("key"), 9), s.get("name") or "")):
        probability = _dec(item.get("probability")) or Decimal(0)
        conditions = []
        for condition in [*item.get("triggers", ()), *item.get("invalidators", ())]:
            source_raw = raw.get((item["version_ref"], condition["condition_id"]))
            conditions.append({
                "condition_id": condition["condition_id"], "kind": condition["kind"],
                "kind_label": term_label("scenario", "trigger" if condition["kind"] == "trigger" else "invalidator"),
                "label": condition.get("display_label") or condition.get("label"),
                "rule": condition.get("rule"), "metric_label": condition.get("metric_display_label"),
                "threshold_label": condition.get("threshold_label"),
                "value_label": condition.get("value_label"), "value_as_of": condition.get("as_of"),
                "horizon": condition.get("horizon"), "observer_label": condition.get("observer_label"),
                "status": condition.get("status") or "unobserved",
                "status_label": condition.get("status_label"),
                "reason_label": condition.get("reason_label"),
                "distance_label": _distance(condition, source_raw),
                "source_ref": condition.get("source_ref"),
                "observed_at": condition.get("observed_at"),
                "rule_technical": condition.get("rule_technical"),
            })
        horizons = sorted({c["horizon"] for c in conditions if c.get("horizon")})
        scenarios.append({
            "key": item.get("key"), "key_label": SCENARIO_KEY_LABELS.get(item.get("key"), "其他"),
            "name": item.get("name"), "narrative": item.get("narrative"),
            "probability": item.get("probability"),
            "probability_display": f"{_q(probability * 100, '1')}%",
            "probability_pp": _q(probability * 100, "0.1"),
            "linked_drivers": [_model_line_label({"ref": ref}) for ref in item.get("linked_driver_refs") or ()],
            "horizon": horizons[-1] if horizons else None,
            "horizon_label": (f"观察至 {horizons[-1]}" if horizons else "未写观察期限"),
            "target_label": None,
            "target_missing_reason": "情景估值待补充：情景记录里尚无目标价或回报区间",
            "conditions": conditions, "version_ref": item.get("version_ref"),
            "created_at": item.get("created_at"),
        })
    total = sum((_dec(s["probability"]) or Decimal(0) for s in scenarios), Decimal(0))
    stamps = [c.get("observed_at") for s in scenarios for c in s["conditions"] if c.get("observed_at")]
    payload.update({
        "data_as_of": max(stamps)[:10] if stamps else (max(s["created_at"] for s in scenarios)[:10]
                                                       if scenarios else None),
        "unit": "概率（%）", "currency": None,
        "period": {"label": "截至各条件的观察期限"},
        "state": state,
        "state_label": {"in_force": "已采纳、正在跟踪", "pending": "提案等你确认（尚未采纳）",
                        "none": "没有情景"}[state],
        "headline": view.get("headline"),
        "scenarios": scenarios,
        "probability_sum": _q(total * 100, "0.1"),
        "probability_sum_display": f"{_q(total * 100, '1')}%",
        "probability_sum_ok": total == Decimal(1),
        "triggered": view.get("triggered"), "invalidated": view.get("invalidated"),
    })
    if total != Decimal(1) and scenarios:
        payload["notes"].append(f"概率合计 {_q(total * 100, '1')}%，不等于 100%；系统没有替你归一化。")
    if state == "pending":
        payload["notes"].append("这组情景还没有被采纳：在采纳之前只记录观察，不触发任何动作。")
    return payload


# ---------------------------------------------------------------------------
# returns: waterfall per leg, in percentage points
# ---------------------------------------------------------------------------

LEG_BASIS = {
    "realised_trailing_year": "事后财务归因：窗口两端的 EPS 按财务期间对齐，不是当时可得的数据",
    "expected_driver_model": "粗算估算",
    "expected_consensus": "一致预期目标价隐含回报 = 目标价隐含 + 股息（12 个月）",
    "expected_sell_side": "卖方目标价隐含回报 = 目标价隐含 + 股息（12 个月）",
}
EARNINGS_BASIS_LABELS = {
    "driver_model_parent_net_income": "模型预测的归母净利润",
    "driver_model_net_income": "模型预测的净利润",
    "driver_model_revenue_x_trailing_net_margin": "模型收入 × 过去四季净利率（代理口径，已停用）",
    "driver_model_core_earnings": "旧模型营业税后口径（未经本轮核心税额分配校验）",
    "driver_model_operating_tax_proxy": "营业税后近似（经异常筛查的GAAP税率；不是披露的核心EPS）",
}
SHARE_BASIS_LABELS = {"driver_model_diluted_shares": "模型预测的稀释股数",
                      "held_flat": "股数按最近一期持平", "missing_diluted_shares": "缺稀释股数"}


def _leg(leg: Mapping[str, Any], as_of: Any = None, generated: Any = None) -> dict[str, Any]:
    from ..return_attribution import COMPONENT_LABELS, COMPONENTS, EXPECTATION_SOURCE_LABELS

    kind = leg.get("leg")
    expected = str(kind).startswith("expected")
    base = {"leg": kind, "label": leg.get("label"), "group": "expected" if expected else "realised",
            "group_label": "预期（未来）" if expected else "已实现（事后）"}
    if leg.get("status") != "available":
        return {**base, "status": "missing", "missing_reason": leg.get("reason") or "这一段没有算出来"}
    contributions = leg.get("contributions_pp") or {}
    steps: list[dict[str, Any]] = []
    running = Decimal(0)
    price_components = [name for name in COMPONENTS if name != "dividends"]
    for name in price_components:
        value = _dec(contributions.get(name))
        start = running
        if value is not None:
            running += value
        steps.append({"key": name, "label": COMPONENT_LABELS[name], "type": "step",
                      "value_pp": _q(value), "display": _signed(value, "0.01", " pp"),
                      "start_pp": _q(start), "end_pp": _q(running),
                      "status": "ok" if value is not None else "missing",
                      "missing_reason": None if value is not None else "缺少这一项的输入，未计入"})
    price_return = _dec(leg.get("price_return_percent"))
    residual = None if price_return is None else price_return - running
    steps.append({"key": "price_return", "label": "价格回报", "type": "subtotal",
                  "value_pp": _q(price_return), "display": _signed(price_return, "0.01", "%"),
                  "start_pp": "0.00", "end_pp": _q(price_return), "status": "ok"})
    dividends = _dec(contributions.get("dividends"))
    after = price_return if price_return is not None else running
    steps.append({"key": "dividends", "label": COMPONENT_LABELS["dividends"], "type": "step",
                  "value_pp": _q(dividends), "display": _signed(dividends, "0.01", " pp"),
                  "start_pp": _q(after), "end_pp": _q(after + (dividends or Decimal(0))),
                  "status": "ok" if dividends is not None else "missing",
                  "missing_reason": None if dividends is not None else "缺股息数据，未计入"})
    total = _dec(leg.get("total_return_percent"))
    steps.append({"key": "total_return", "label": "总回报", "type": "total",
                  "value_pp": _q(total), "display": _signed(total, "0.01", "%"),
                  "start_pp": "0.00", "end_pp": _q(total), "status": "ok"})
    window = leg.get("window") or {}
    source = leg.get("expectation_source_kind")
    basis = LEG_BASIS.get(str(kind), "")
    notes = list(leg.get("notes") or ())
    if kind == "expected_driver_model":
        parts = [EARNINGS_BASIS_LABELS.get(str(leg.get("earnings_basis")), "模型预测"),
                 SHARE_BASIS_LABELS.get(str(leg.get("share_basis")), "股数口径未记录"),
                 "估值倍数不变"]
        basis = "粗算估算：" + "；".join(parts)
    out = {**base, "status": "ok", "steps": steps,
           "window": {"start": window.get("start"), "end": window.get("end"),
                      "label": _period_text(window.get("start"), window.get("end"))},
           "basis_label": basis,
           "estimated": expected,
           "source_label": None if source is None else EXPECTATION_SOURCE_LABELS.get(source),
           "price_start": _q(_dec(leg.get("price_start"))), "price_end": _q(_dec(leg.get("price_end"))),
           "price_target": _q(_dec(leg.get("price_target"))),
           "eps_start": _q(_dec((leg.get("eps_start") or {}).get("value"))),
           "eps_end": _q(_dec((leg.get("eps_end") or {}).get("value"))),
           "total_return_percent": leg.get("total_return_percent"),
           "total_display": _signed(total, "0.01", "%"),
           "rounding_residual_pp": (None if residual is None or abs(residual) < Decimal("0.015")
                                    else _q(residual)),
           "contradiction": leg.get("contradiction"),
           "target_implied_percent": leg.get("target_implied_percent"),
           "dividend_yield_percent": leg.get("dividend_yield_percent"),
           "notes": notes}
    if expected:
        flags = _forecast_flags(leg, as_of, generated)
        out.update({"badges": flags["badges"], "generated_label": flags["generated_label"],
                    "simplified_label": flags["simplified_label"], "expired": flags["expired"],
                    "started": flags["started"]})
    return out


def returns_payload(reader: ChartReader, query: Mapping[str, Any]) -> dict[str, Any]:
    from ..return_attribution import latest_return_bridge

    company_ref = _company_ref(query)
    with reader.core() as core:
        mission = reader.mission(core)
        members = reader.members(mission)
        payload = _envelope("returns", reader, company_ref=company_ref,
                            company=reader.label(members, company_ref))
        if core is None or not _table_exists(core, "holding_period_return_bridges"):
            return _empty(payload, empty_reason(core, mission, company_ref, "returns",
                                                "这个研究项目还没有生成过回报归因"))
        record = latest_return_bridge(core, company_ref)
        if record is None:
            return _empty(payload, empty_reason(core, mission, company_ref, "returns",
                                                "回报归因尚未为这家公司测算"))
        generated = {leg.get("leg"): _model_created(core, (leg.get("inputs") or {}).get("forecast_model_version_ref"))
                     for leg in record.get("legs") or ()}
    as_of = record.get("as_of")
    legs = [_leg(leg, as_of, generated.get(leg.get("leg"))) for leg in record.get("legs") or ()]
    payload.update({
        "data_as_of": record.get("as_of"), "currency": "USD", "unit": "百分点（pp）",
        "period": {"label": "各段窗口见每张图"},
        "legs": legs, "ref": record.get("id"),
        "legend": {"step_positive": "正贡献（+）", "step_negative": "负贡献（−）",
                   "subtotal": "价格回报（小计）", "total": "总回报（含股息）"},
    })
    payload["notes"].append("贡献按对数权重拆分，各项相加等于价格回报（四舍五入）；总回报 = 价格回报 + 股息收益率。")
    if not any(leg["status"] == "ok" for leg in legs):
        payload["status"] = "empty"
        payload["empty_reason"] = "回报归因的每一段都还缺输入：" + "；".join(
            f"{leg['label']}：{leg['missing_reason']}" for leg in legs)
    return payload


# ---------------------------------------------------------------------------
# bridge: revenue -> operating income -> pre-tax -> net income to common
# ---------------------------------------------------------------------------

ANCHOR_CLASSES = ("revenue", "operating_income", "pretax_income", "net_income", "net_income_to_common")
SEMANTICS_PREFERENCE = ("quarterly", "annual", "year_to_date")


def _statement_lines(core: sqlite3.Connection, refs: Sequence[str]) -> dict[str, dict[str, Any]]:
    if not refs or not _table_exists(core, "coverage_mission_statement_lines"):
        return {}
    out: dict[str, dict[str, Any]] = {}
    has_filings = _table_exists(core, "coverage_mission_statement_filings")
    for ref in refs:
        line_id = ref.split("statement-line:", 1)[-1]
        if has_filings:
            row = core.execute(
                "SELECT l.line_id, l.concept, l.label, l.period_start, l.period_end, l.value, l.unit, "
                "l.statement, f.accession, f.form, f.filed FROM coverage_mission_statement_lines l "
                "LEFT JOIN coverage_mission_statement_filings f ON f.ingest_id=l.ingest_id "
                "WHERE l.line_id=?", (line_id,)).fetchone()
        else:
            row = core.execute(
                "SELECT line_id, concept, label, period_start, period_end, value, unit, statement, "
                "NULL AS accession, NULL AS form, NULL AS filed FROM coverage_mission_statement_lines "
                "WHERE line_id=?", (line_id,)).fetchone()
        if row is not None:
            out[ref] = dict(row)
    return out


def _bridge_view(bridge: Mapping[str, Any], lines: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    from ..financial_display import concept_label, model_line_display
    from ..return_attribution import (
        LINE_CLASS_LABELS, SEMANTICS_LABELS, STATUS_LABELS, amount_in, amount_scale)

    period = bridge.get("period") or {}
    rows = [dict(line) for line in bridge.get("lines") or ()]
    values = [line.get("value") for line in rows]
    divisor, unit = amount_scale(values)
    steps: list[dict[str, Any]] = []
    running: Decimal | None = None
    previous_anchor: tuple[str, Decimal] | None = None

    def statement(line: Mapping[str, Any]) -> dict[str, Any]:
        filed = lines.get(str(line.get("ref"))) or {}
        ref = str(line.get("ref") or "")
        return {"ref": line.get("ref"), "concept": line.get("concept"),
                "line_id": ref.split("statement-line:", 1)[-1] if ref else None,
                "concept_label": (concept_label(line.get("concept"))
                                  or model_line_display(filed.get("label"), line.get("concept"))),
                "label_as_filed": filed.get("label"), "filed_value": filed.get("value"),
                "filed_value_display": _amount_text(filed.get("value")),
                "period": _period_text(filed.get("period_start") or period.get("start"),
                                       filed.get("period_end") or period.get("end")),
                "accession": filed.get("accession") or (bridge.get("source") or {}).get("accession"),
                "form": filed.get("form"), "filed": filed.get("filed"),
                "statement": filed.get("statement")}

    for line in rows:
        cls = line.get("classification")
        if line.get("step") is None or cls in ANCHOR_CLASSES:
            value = _dec(line.get("value"))
            if value is None:
                continue
            if previous_anchor is not None and previous_anchor[0] == "revenue" and cls == "operating_income":
                gap = value - previous_anchor[1]
                steps.append({"key": "operating_costs", "label": "营业成本及费用（收入 − 营业利润）",
                              "short_label": "营业成本及费用",
                              "derived_from": f"{LINE_CLASS_LABELS.get('revenue', '营业收入')} − "
                                              f"{LINE_CLASS_LABELS.get(cls, '营业利润')}",
                              "type": "step", "value": str(gap),
                              "display": _signed(gap / divisor, "0.01", f" {unit}"),
                              "start": str(previous_anchor[1]), "end": str(value),
                              "derived": True, "statement": None})
            steps.append({"key": cls, "label": LINE_CLASS_LABELS.get(cls, cls),
                          "short_label": LINE_CLASS_LABELS.get(cls, cls), "type": "anchor",
                          "value": str(value), "display": f"{amount_in(value, divisor)} {unit}",
                          "start": "0", "end": str(value), "derived": False,
                          "statement": statement(line)})
            running = value
            previous_anchor = (str(cls), value)
            continue
        value = _dec(line.get("signed_value"))
        if value is None:
            continue
        start = running if running is not None else Decimal(0)
        running = start + value
        shown = (LINE_CLASS_LABELS.get(cls) or concept_label(line.get("concept"))
                 or model_line_display((lines.get(str(line.get("ref"))) or {}).get("label"), line.get("concept")))
        steps.append({"key": f"{cls}:{line.get('ref')}", "label": shown, "short_label": shown,
                      "type": "step", "value": str(value),
                      "display": _signed(value / divisor, "0.01", f" {unit}"),
                      "start": str(start), "end": str(running), "derived": False,
                      "statement": statement(line)})
    checks = []
    from ..return_attribution import STEP_LABELS
    for step in bridge.get("steps") or ():
        residual = _dec(step.get("residual"))
        checks.append({"label": STEP_LABELS.get(step.get("step"), step.get("step")),
                       "status": step.get("status"),
                       "status_label": {"complete": "勾稽一致", "residual": "有差额",
                                        "missing_anchor": "缺报表行"}.get(step.get("status"), "未知"),
                       "residual_display": (None if residual is None
                                            else _signed(residual / divisor, "0.01", f" {unit}")),
                       "missing": [LINE_CLASS_LABELS.get(item, item) for item in step.get("missing") or ()]})
    return {"ref": bridge.get("id"), "fiscal_label": period.get("fiscal_label"),
            "semantics": period.get("semantics"),
            "semantics_label": SEMANTICS_LABELS.get(period.get("semantics"), "期间"),
            "period": {"start": period.get("start"), "end": period.get("end"),
                       "label": _period_text(period.get("start"), period.get("end"))},
            "accession": (bridge.get("source") or {}).get("accession"),
            "status": bridge.get("status"), "status_label": STATUS_LABELS.get(bridge.get("status")),
            "unit": unit, "divisor": str(divisor), "steps": steps, "checks": checks,
            "missing": [LINE_CLASS_LABELS.get(item, item) for item in bridge.get("missing_rows") or ()]}


def bridge_payload(reader: ChartReader, query: Mapping[str, Any]) -> dict[str, Any]:
    from ..return_attribution import SEMANTICS_LABELS, latest_bridges

    company_ref = _company_ref(query)
    wanted = query.get("bridge") or None
    with reader.core() as core:
        mission = reader.mission(core)
        members = reader.members(mission)
        payload = _envelope("bridge", reader, company_ref=company_ref,
                            company=reader.label(members, company_ref))
        if core is None or not _table_exists(core, "income_bridge_versions"):
            return _empty(payload, empty_reason(core, mission, company_ref, "bridge",
                                                "这个研究项目还没有生成过利润桥"))
        bridges = latest_bridges(core, company_ref)
        if not bridges:
            return _empty(payload, empty_reason(core, mission, company_ref, "bridge",
                                                "还没有可以对账的申报期"))
        options = []
        for bridge in sorted(bridges, key=lambda b: (b["period"]["end"], b["period"]["semantics"]),
                             reverse=True):
            options.append({"ref": bridge["id"], "semantics": bridge["period"]["semantics"],
                            "semantics_label": SEMANTICS_LABELS.get(bridge["period"]["semantics"]),
                            "fiscal_label": bridge["period"].get("fiscal_label"),
                            "period": _period_text(bridge["period"]["start"], bridge["period"]["end"]),
                            "status": bridge.get("status")})
        selected = next((b for b in bridges if b["id"] == wanted), None)
        if selected is None:
            for semantics in SEMANTICS_PREFERENCE:
                same = [b for b in bridges if b["period"]["semantics"] == semantics]
                if same:
                    selected = max(same, key=lambda b: b["period"]["end"])
                    break
        lines = _statement_lines(core, [str(line.get("ref")) for line in selected.get("lines") or ()])
    view = _bridge_view(selected, lines)
    payload.update({"data_as_of": selected["period"]["end"], "currency": "USD",
                    "unit": view["unit"],
                    "period": {"label": f"{view['fiscal_label'] or ''} {view['semantics_label']}"
                                        f"（{view['period']['label']}）".strip(),
                               "semantics": view["semantics"]},
                    "bridge": view, "options": options,
                    "semantics_options": [{"value": key, "label": SEMANTICS_LABELS[key]}
                                          for key in SEMANTICS_PREFERENCE
                                          if any(o["semantics"] == key for o in options)]})
    payload["unit_header"] = f"金额：{view['unit']}"
    payload["notes"].append("每一行都是申报原值（带正负号）；带下划线的柱子可点击，查看对应的报表行和原始申报。只在同一期间、同一口径内相加，不混合单季/累计/年度。")
    return payload


# ---------------------------------------------------------------------------
# comparison: one basis per column, otherwise 不可比较
# ---------------------------------------------------------------------------


#: ONE comparison rule for every company-comparison surface (review O01/O13):
#: the overview widget, the shell's fallback table and the company list all
#: read this payload and rank with the same function.
COMPARISON_RULE = {
    "id": "same-basis-same-window-v1",
    "label": "统计窗口一致才排名",
    "text": (term_label("comparison", "rule") + "；缺数的标的写明缺什么，"
             "与口径暂不可比的标的一起排在最后。"),
    "reference": ("每列的对比期间取覆盖公司中最多家共用的那一个；家数相同时取起点最新的期间。"),
}
SORT_COLUMNS = {"return": "expected", "change": "change", "risk": "risk"}


def _window_key(basis_key: str) -> tuple[str, str]:
    start, _, end = str(basis_key).partition("..")
    return start, end


def _basis_column(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    """The reference basis of one column (:data:`COMPARISON_RULE`); a value on
    another basis is not comparable and says which basis it is on."""

    counts: dict[str, int] = {}
    for row in rows:
        cell = row["cells"].get(key)
        if cell and cell.get("value") is not None:
            counts[cell["basis_key"]] = counts.get(cell["basis_key"], 0) + 1
    if not counts:
        return None
    # Most companies first; on a tie the latest start (the most current window).
    common = max(counts, key=lambda key_: (counts[key_], _window_key(key_)[0], key_))
    sample = next(row["cells"][key] for row in rows
                  if row["cells"].get(key) and row["cells"][key].get("basis_key") == common)
    for row in rows:
        cell = row["cells"].get(key)
        if cell is None or cell.get("value") is None:
            continue
        cell["comparable"] = cell["basis_key"] == common
        if not cell["comparable"]:
            cell["incomparable_reason"] = (f"{term_label('comparison', 'not_comparable')}：本行{cell['basis_label']}，"
                                           f"本列{sample['basis_label']}；不参与排名")
    return {"basis_key": common, "basis_label": sample["basis_label"], "companies": counts[common]}


def rank_rows(rows: Sequence[Mapping[str, Any]], sort: str, direction: str = "desc") -> list[Mapping[str, Any]]:
    """The one ordering (mirrored by ``charts/comparison.mjs`` ``sortRows``):
    comparable values by value, then 不可比较, then missing, each by ticker."""

    key = SORT_COLUMNS.get(sort, "expected")
    sign = Decimal(1) if direction == "asc" else Decimal(-1)

    def order(row: Mapping[str, Any]) -> tuple:
        cell = row["cells"].get(key) or {}
        value = _dec(cell.get("value"))
        if value is not None and cell.get("comparable"):
            return (0, value * sign, row.get("ticker") or "")
        return (1 if value is not None else 2, Decimal(0), row.get("ticker") or "")

    return sorted(rows, key=order)


def _forecast_flags(leg: Mapping[str, Any], as_of: Any, generated_at: Any) -> dict[str, Any]:
    """Labels a forward return carries next to its number (C07/C08, G11)."""

    window = leg.get("window") or {}
    start, end = str(window.get("start") or ""), str(window.get("end") or "")
    today = str(as_of or "")[:10]
    flags: dict[str, Any] = {"generated_at": generated_at,
                             "generated_label": (f"预测生成于{_cn_date(generated_at)}"
                                                 if _cn_date(generated_at) else None),
                             "expired": bool(end and today and end < today),
                             "started": bool(start and today and start < today),
                             "simplified": False, "simplified_label": None, "badges": []}
    if flags["expired"]:
        flags["badges"].append({"text": "已到期未更新", "tone": "warn",
                                "detail": f"预测期已于{_cn_date(end)}结束，还没有新的预测"})
    elif flags["started"]:
        flags["badges"].append({"text": "预测期已开始", "tone": "warn",
                                "detail": (f"预测期从{_cn_date(start)}起算，早于当前价格日期{_cn_date(today)}；"
                                           "部分期间已经过去，不是从今天起的未来一年")})
    if leg.get("leg") == "expected_driver_model":
        parts = [EARNINGS_BASIS_LABELS.get(str(leg.get("earnings_basis")), "模型预测"),
                 SHARE_BASIS_LABELS.get(str(leg.get("share_basis")), "股数口径未记录"),
                 "估值倍数不变"]
        simplified = leg.get("earnings_basis") == "driver_model_revenue_x_trailing_net_margin" \
            or leg.get("share_basis") != "driver_model_diluted_shares"
        flags["simplified"] = simplified
        flags["simplified_label"] = ("粗算估算（假设：" + "；".join(parts) + "）") if simplified else None
        if simplified:
            flags["badges"].append({"text": "粗算估算", "tone": "warn", "detail": "假设：" + "；".join(parts)})
    if leg.get("contradiction"):
        # 2026-10-09: a return next to a loss-making model says so.
        flags["badges"].append({"text": "与模型亏损矛盾", "tone": "warn",
                                "detail": str(leg["contradiction"])})
    if flags["generated_label"]:
        flags["badges"].append({"text": flags["generated_label"], "tone": "info", "detail": None})
    return flags


def _model_created(core: sqlite3.Connection, model_ref: Any) -> str | None:
    if not model_ref or not _table_exists(core, "forecast_model_versions"):
        return None
    row = core.execute("SELECT created_at FROM forecast_model_versions WHERE version_id=?",
                       (str(model_ref),)).fetchone()
    return None if row is None else row["created_at"]


def comparison_payload(reader: ChartReader, query: Mapping[str, Any]) -> dict[str, Any]:
    from ..cockpit_plane import CockpitPlane
    from ..company_scenario import company_view as scenario_view
    from ..return_attribution import latest_return_bridge

    sort = str(query.get("sort") or "return")
    if sort not in SORT_COLUMNS:
        raise ChartRequestError("参数 sort 只能是 return、change 或 risk")
    direction = str(query.get("dir") or "desc")
    if direction not in {"asc", "desc"}:
        raise ChartRequestError("参数 dir 只能是 asc 或 desc")
    with reader.core() as core:
        mission = reader.mission(core)
        members = reader.members(mission)
        payload = _envelope("comparison", reader, company_ref=None, company=None)
        payload.update({"sort": sort, "dir": direction, "rule": dict(COMPARISON_RULE)})
        if core is None or mission is None:
            return _empty(payload, empty_reason(core, mission, "", "comparison"))
        market = (CockpitPlane._market(_LatestBy(), core)
                  if _table_exists(core, "market_price_series_versions") else {})
        has_returns = _table_exists(core, "holding_period_return_bridges")
        has_scenarios = _table_exists(core, "company_scenario_sets")
        rows = []
        for company_ref, member in members.items():
            price = market.get(company_ref) or {}
            month = next((c for c in price.get("changes") or () if c.get("days") == 30), None)
            record = latest_return_bridge(core, company_ref) if has_returns else None
            legs = {leg.get("leg"): leg for leg in (record or {}).get("legs") or ()}
            scen = scenario_view(core, company_ref) if has_scenarios else None
            from ..scenario_valuation import current as current_scenario_valuation,valuation_view
            scenario_value=(scen or {}).get("valuation") or valuation_view(current_scenario_valuation(core,company_ref))
            cells: dict[str, dict[str, Any]] = {}
            close = _dec(price.get("close"))
            cells["close"] = {"value": None if close is None else str(close),
                              "display": None if close is None else f"{close.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,}",
                              "basis_key": f"{price.get('as_of')}|{price.get('currency')}",
                              "basis_label": (f"{_cn_date(price.get('as_of')) or '日期未记录'}"
                                              f"{'收盘' if not price.get('provisional') else '盘中临时价'}"),
                              "missing_reason": None if close is not None else "还没有行情数据"}
            change = _dec((month or {}).get("percent"))
            cells["change"] = {"value": None if change is None else str(change),
                               "display": _signed(change, "0.1", "%"),
                               "basis_key": f"30d|{price.get('as_of')}",
                               "basis_label": f"截至{_cn_date(price.get('as_of')) or '日期未记录'}的近 30 个交易日",
                               "missing_reason": None if change is not None else (
                                   (month or {}).get("reason") or "还没有行情数据")}
            for key, leg_kind, what in (("realised", "realised_trailing_year", "过去 12 个月总回报"),
                                        ("expected", "expected_driver_model", "模型预期总回报")):
                leg = legs.get(leg_kind) or {}
                value = _dec(leg.get("total_return_percent")) if leg.get("status") == "available" else None
                window = leg.get("window") or {}
                cell = {"value": None if value is None else str(value),
                        "display": _signed(value, "0.01", "%"),
                        "basis_key": f"{window.get('start')}..{window.get('end')}",
                        "basis_label": _period_text(window.get("start"), window.get("end")),
                        "estimated": key == "expected",
                        "missing_reason": None if value is not None else (
                            leg.get("reason") or "回报归因待测算")}
                if key == "expected":
                    summary=(scenario_value or {}).get("summary") or {}
                    value=_dec(summary.get("expected_return_percent")) if scenario_value.get("status")=="available" else None
                    window=scenario_value.get("window") or {}
                    cell={"value":None if value is None else str(value),"display":_signed(value,"0.01","%"),
                        "basis_key":f"{window.get('start')}..{window.get('end')}",
                        "basis_label":_period_text(window.get("start"),window.get("end")),"estimated":True,
                        "valuation_ref":scenario_value.get("id"),"method":"概率加权：重算EPS×历史PE＋股息，价格日到12个月后",
                        "missing_reason":None if value is not None else scenario_value.get("reason") or "尚无完整且当前的三情景估值",
                        "technical_details":scenario_value.get("technical_details")}
                cells[key] = cell
            in_force = (scen or {}).get("in_force") or []
            bear = next((s for s in in_force if s.get("key") == "bear"), None)
            bear_p = _dec((bear or {}).get("probability"))
            # The bear scenario's probability AND its potential drawdown, never a
            # "risk score" (am, report 4.1): the drawdown is the bear target's
            # return when the scenario carries one, else said to be pending.
            valued_bear=next((r for r in scenario_value.get("scenarios") or [] if r.get("key")=="bear"),{})
            drawdown = _dec(valued_bear.get("return_percent"))
            downside = (term_label("comparison", "bear_drawdown", d=_signed(drawdown, "0.1", "%"))
                        if drawdown is not None else term_label("comparison", "bear_drawdown_pending"))
            cells["risk"] = {"value": None if bear_p is None else str(bear_p * 100),
                             "display": None if bear_p is None else f"{_q(bear_p * 100, '1')}%",
                             "upside_downside_ratio":(scenario_value.get("summary") or {}).get("upside_downside_ratio"),
                             "target_price_bounds":None if scenario_value.get("summary") is None else {k:scenario_value["summary"][k] for k in ("bear_target_price","bull_target_price")},
                             "basis_key": "bear_probability",
                             "basis_label": "悲观情景发生概率（研究员主观权重）",
                             "downside_label": downside,
                             "invalidated": (scen or {}).get("invalidated") or 0,
                             "invalidated_label": (f"{term_label('scenario', 'invalidated')}：{(scen or {}).get('invalidated')} 条"
                                                   if (scen or {}).get("invalidated") else None),
                             "missing_reason": None if bear_p is not None else (
                                 "没有已采纳的悲观情景" if scen else "还没有情景假设")}
            for cell in cells.values():
                cell.setdefault("comparable", cell.get("value") is not None)
            rows.append({"company_ref": company_ref, "ticker": member.get("ticker"),
                         "company": reader.label(members, company_ref),
                         "stage": stage_position(core, mission, company_ref)["text"],
                         "cells": cells})
    columns = [
        {"key": "close", "label": "最新价（美元）", "sort": None, "unit": "美元"},
        {"key": "change", "label": "近 30 个交易日涨跌", "sort": "change", "unit": "%"},
        {"key": "realised", "label": "过去 12 个月总回报（已实现）", "sort": None, "unit": "%"},
        {"key": "expected", "label": term_label("comparison", "expected_return"), "sort": "return", "unit": "%"},
        {"key": "risk", "label": term_label("comparison", "bear"), "sort": "risk", "unit": "%"},
    ]
    for column in columns:
        column["basis"] = _basis_column(rows, column["key"])
    rows = rank_rows(rows, sort, direction)
    sort_key = SORT_COLUMNS[sort]
    rank = 0
    for row in rows:
        cell = row["cells"][sort_key]
        if cell.get("value") is not None and cell.get("comparable"):
            rank += 1
            row["rank"] = rank
        else:
            row["rank"] = None
    stamps = [row["cells"]["close"]["basis_key"].split("|")[0] for row in rows
              if row["cells"]["close"].get("value") is not None]
    payload.update({
        "data_as_of": max(stamps) if stamps else None, "currency": "USD",
        "unit": "%（涨跌、回报、概率）", "period": {"label": "每列的对比期间写在列名下方"},
        "columns": columns, "rows": rows,
        "sort_options": [{"value": "return", "label": f"按{term_label('comparison', 'expected_return')}"},
                         {"value": "change", "label": "按近 30 个交易日涨跌"},
                         {"value": "risk", "label": "按悲观情景发生概率"}],
    })
    payload["notes"].append(COMPARISON_RULE["text"])
    if not rows:
        return _empty(payload, "研究目标里还没有覆盖公司。")
    return payload


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def numbers_payload(reader: ChartReader, query: Mapping[str, Any]) -> dict[str, Any]:
    """Batch B: the numbers pack for one company (read-only; stored figures only)."""

    from ..numbers_pack import numbers_pack

    company_ref = _company_ref(query)
    with reader.core() as core:
        mission = reader.mission(core)
        members = reader.members(mission)
        payload = _envelope("numbers", reader, company_ref=company_ref,
                            company=reader.label(members, company_ref))
        if core is None or mission is None:
            return _empty(payload, empty_reason(core, mission, company_ref, "numbers"))
        stage_reason = empty_reason(core, mission, company_ref, "numbers")
    as_of = query.get("as_of")
    as_of = as_of if isinstance(as_of, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of) else None
    pack = numbers_pack(reader.core_db.parent, company_ref, as_of or reader.clock().date().isoformat(),
                        parse_unstored=False, include_peers=query.get("peers") not in ("0", "false"),
                        core_db=reader.core_db)
    body = pack["json"]
    if (body.get("consensus") or {}).get("status") != "ok" and (body.get("market") or {}).get("status") != "ok":
        return _empty(payload, stage_reason)
    payload.update({"data_as_of": body.get("as_of"), "currency": (body.get("market") or {}).get("currency"),
                    "numbers": body, "text": pack["text"]})
    return payload


BUILDERS: dict[str, Callable[[ChartReader, Mapping[str, Any]], dict[str, Any]]] = {
    "price": price_payload, "model": model_payload, "scenarios": scenarios_payload,
    "returns": returns_payload, "bridge": bridge_payload, "comparison": comparison_payload,
    "numbers": numbers_payload,
}


def _reader(context: Any) -> ChartReader:
    if isinstance(context, ChartReader):
        return context
    if isinstance(context, Mapping):
        if context.get("reader") is not None:
            return _reader(context["reader"])
        if context.get("plane") is not None:
            return ChartReader.from_plane(context["plane"])
        if context.get("core_db") is not None:
            return ChartReader(context["core_db"], mission_ref=context.get("mission_ref"))
    if getattr(context, "config", None) is not None:
        return ChartReader.from_plane(context)
    raise ChartRequestError("这台机器还没有配置研究环境，图表没有数据来源")


def chart_payload(kind: str, query: Mapping[str, Any], context: Any) -> dict[str, Any]:
    """One chart payload.  ``context``: a CockpitPlane, a ChartReader, or a mapping."""

    if kind not in BUILDERS:
        raise ChartRequestError("没有这种图表数据")
    reader = _reader(context)
    try:
        return BUILDERS[kind](reader, query)
    except ChartRequestError:
        raise
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
        # The Core changed under us or a record is not what this reader knows:
        # an honest empty state, never a 5xx.
        payload = _envelope(kind, reader, company_ref=query.get("company"), company=None)
        payload["technical_reason"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        return _empty(payload, "暂时读不出这张图的数据，请稍后刷新；如果一直如此，请在管理页查看。")


__all__ = ["BUILDERS", "COMPANY_KINDS", "ChartReader", "ChartRequestError", "KINDS",
           "SCHEMA_VERSION", "chart_payload", "empty_reason", "stage_position"]
