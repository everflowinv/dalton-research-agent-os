"""Read the held records a coverage model's assumptions may stand on.

Readers only: this module never writes and never calls out.  It gathers, for
one company, what ``forecast_coverage_assumptions`` ranks above trailing
history:

* **preliminary** -- the newest ADR-0015 earnings-release read whose quarter
  is not yet filed (``earnings_release_intake.preliminary_actuals``);
* **guidance / consensus** -- revenue targets per forecast quarter.  The
  readers belong to the consensus/street lane; they are imported by name and a
  missing module is a note, not an error.  A module listed in
  ``TARGET_PROVIDER_MODULES`` that exposes
  ``forecast_model_targets(connection, company_ref) -> list[dict]`` is used;
  each dict is ``{"source": "guidance"|"consensus", "metric": "revenue",
  "period_end": "YYYY-MM-DD", "value": "<decimal>", "ref": "<record ref>",
  "because": "<sentence>"}``.  Without a provider the held
  ``consensus_estimate_versions`` row is read directly, and used only when its
  fiscal mapping agrees with the filed revenue a year earlier;
* **judgement** -- live human or event-decided driver assumptions of the
  company's current model version, carried into a rebuilt version.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any, Mapping, Sequence

TARGET_PROVIDER_MODULES = (
    "dalton_core.model_targets",
    "dalton_core.guidance_records",
    "dalton_core.company_guidance",
    "dalton_core.consensus_render",
    "dalton_core.street_consensus",
)
#: 2026-10-09 (P0): revenue guidance *ranges* bound the first forecast
#: quarter, and filed/released one-off items normalise the base quarter.  The
#: earnings-release outlook reader belongs to the numbers lane; it is probed by
#: name, each function in order, and a missing module or function is a flag in
#: the notes, never an error.
GUIDANCE_RANGE_PROVIDERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("dalton_core.earnings_release_outlook", ("revenue_guidance_ranges", "guidance_ranges",
                                               "forecast_model_targets")),
    ("dalton_core.company_guidance", ("revenue_guidance_ranges", "guidance_ranges")),
    ("dalton_core.guidance_records", ("revenue_guidance_ranges", "guidance_ranges")),
)
ONE_OFF_PROVIDERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("dalton_core.earnings_release_outlook", ("one_off_items", "nonrecurring_items")),
    ("dalton_core.earnings_release_intake", ("one_off_items",)),
)
_SCALES = {"one": Decimal(1), "thousand": Decimal(1000), "million": Decimal(1_000_000),
           "billion": Decimal(1_000_000_000)}
_PRELIMINARY_METRICS = {
    "revenue", "cost_of_revenue", "operating_income", "pretax_income", "income_tax",
    "net_income", "net_income_attributable", "eps_diluted", "shares_diluted",
}


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() else None


def preliminary_quarter(connection: sqlite3.Connection, company_ref: str,
                        *, reporting_unit: str = "usd") -> dict[str, Any] | None:
    """The newest unfiled quarter's preliminary rows, scaled to units of one."""

    try:
        from .earnings_release_intake import preliminary_actuals
        rows = preliminary_actuals(connection, company_ref, include_superseded=False)
    except Exception:
        return None
    rows = [row for row in rows if row.get("status") == "preliminary"
            and row.get("period_end")]
    if not rows:
        return None
    newest = max(str(row["period_end"]) for row in rows)
    same = [row for row in rows if str(row["period_end"]) == newest]
    values: dict[str, str] = {}
    for row in same:
        metric = str(row.get("metric"))
        if metric not in _PRELIMINARY_METRICS:
            continue
        value = _decimal(row.get("value"))
        scale = _SCALES.get(str(row.get("scale") or "one"))
        unit = str(row.get("unit") or "").casefold()
        if value is None or scale is None:
            continue
        if metric not in {"eps_diluted", "shares_diluted"} and unit != reporting_unit:
            continue
        values[metric] = format(value * scale, "f")
    if not values:
        return None
    return {
        "period_end": newest,
        "period": same[0].get("period"),
        "accession": same[0].get("accession"),
        "report_date": same[0].get("report_date"),
        "exhibit_url": same[0].get("exhibit_url"),
        "values": values,
    }


def _provider_targets(connection: sqlite3.Connection, company_ref: str
                      ) -> tuple[list[dict[str, Any]], str | None]:
    for name in TARGET_PROVIDER_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        reader = getattr(module, "forecast_model_targets", None)
        if not callable(reader):
            continue
        try:
            return [dict(item) for item in reader(connection, company_ref) or []], name
        except Exception as exc:  # a provider failure is a note, never a crash
            return [], f"{name} failed: {type(exc).__name__}"
    return [], None


def _year_ago_end(end: str) -> date:
    value = date.fromisoformat(end)
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)


def _probe(connection: sqlite3.Connection, company_ref: str,
           providers: Sequence[tuple[str, Sequence[str]]]) -> tuple[list[dict[str, Any]], str | None]:
    for module_name, functions in providers:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        for function in functions:
            reader = getattr(module, function, None)
            if not callable(reader):
                continue
            try:
                return ([dict(item) for item in reader(connection, company_ref) or []
                         if isinstance(item, Mapping)], f"{module_name}.{function}")
            except Exception as exc:  # a reader failure is a note, never a crash
                return [], f"{module_name}.{function} failed: {type(exc).__name__}"
    return [], None


def outlook_guidance_ranges(connection: sqlite3.Connection, company_ref: str
                            ) -> list[dict[str, Any]]:
    """Quarterly revenue guidance ranges from the numbers lane's outlook records, by name.

    ``earnings_release_outlook.latest_outlook`` (batch B): the newest release's
    outlook items; a ``guidance`` item for revenue over a quarter with a low
    and a high in currency is a range.  Nothing when the module, the record or
    the item is not there.
    """

    try:
        from .earnings_release_intake import core_state_dir
        from .earnings_release_outlook import latest_outlook  # type: ignore[import-not-found]

        outlook = latest_outlook(connection, company_ref, state_dir=core_state_dir(connection))
    except Exception:  # noqa: BLE001 - absent or failing reader: no ranges
        return []
    out = []
    for item in (outlook or {}).get("items") or ():
        if (item.get("kind") != "guidance" or item.get("metric") != "revenue"
                or item.get("period_kind") != "quarter" or not item.get("period_end")
                or item.get("unit") not in (None, "currency")):
            continue
        if item.get("low") is None or item.get("high") is None:
            continue
        out.append({"metric": "revenue", "source": "guidance", "period_end": item["period_end"],
                    "low": item["low"], "high": item["high"], "scale": item.get("scale") or "one",
                    "ref": str((outlook or {}).get("id") or ""),
                    "because": (f"company revenue guidance {item.get('low')}-{item.get('high')} "
                                f"{item.get('scale') or ''} for {item.get('period') or item['period_end']} "
                                "(earnings release outlook)")})
    return out


def revenue_guidance_bounds(connection: sqlite3.Connection, company_ref: str,
                            provided: Sequence[Mapping[str, Any]] = (),
                            ) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Revenue guidance ranges per quarter end: ``{end: {low, high, ref, because}}``."""

    items, source = _probe(connection, company_ref, GUIDANCE_RANGE_PROVIDERS)
    if not items:
        items = outlook_guidance_ranges(connection, company_ref)
        source = "dalton_core.earnings_release_outlook.latest_outlook" if items else source
    items = list(items) + [dict(item) for item in provided]
    bounds: dict[str, dict[str, Any]] = {}
    for item in items:
        if item.get("metric", "revenue") != "revenue" or not item.get("period_end"):
            continue
        if item.get("source", "guidance") != "guidance":
            continue
        low, high = _decimal(item.get("low")), _decimal(item.get("high"))
        scale = _SCALES.get(str(item.get("scale") or "one"), Decimal(1))
        if low is None or high is None or low <= 0 or high < low:
            continue
        end = str(item["period_end"])
        bounds[end] = {
            "low": format(low * scale, "f"), "high": format(high * scale, "f"),
            "ref": {"kind": "figure", "ref": str(item.get("ref") or source or "guidance"),
                    "concept": None, "period_end": end, "accession": item.get("accession")},
            "because": str(item.get("because") or "company revenue guidance"),
        }
    notes = []
    if bounds:
        notes.append(f"公司指引区间：读取自 {source or '指引记录'}，首个预测季收入受区间约束")
    else:
        notes.append("公司指引区间：没有可读的收入指引区间记录，首个预测季未按指引约束"
                     "（flag: guidance_bound_unavailable）")
    return bounds, notes


def one_off_items(connection: sqlite3.Connection, company_ref: str
                  ) -> tuple[list[dict[str, Any]], list[str]]:
    """Released/filed one-off items (legal, severance) by quarter, when a reader holds them.

    ``earnings_release_outlook.one_off_items`` (batch B follow-up) is probed
    first: each item carries its amount at a printed scale, the quarter it
    belongs to, the statement concept it sits in (stated in the sentence, or
    the line its kind normally lands in) and the sentence it was read from.
    Amounts are scaled to units of one here; a full-year mention is not a
    quarter's one-off and is left out (subtracting it from the fourth quarter
    would remove a year's charge from one quarter).
    """

    items, source = _probe(connection, company_ref, ONE_OFF_PROVIDERS)
    out = []
    skipped_annual = 0
    for item in items:
        amount = _decimal(item.get("amount"))
        scale = _SCALES.get(str(item.get("scale") or "one"), Decimal(1))
        if amount is None or not item.get("period_end"):
            continue
        if item.get("period_kind") not in (None, "quarter"):
            skipped_annual += 1
            continue
        ref = item.get("ref")
        if not isinstance(ref, Mapping):
            ref = {"kind": "figure", "ref": str(ref or source or "earnings-release"),
                   "concept": item.get("concept"), "period_end": str(item["period_end"]),
                   "accession": item.get("accession")}
        because = str(item.get("because") or "")
        if item.get("sentence") and item.get("sentence") not in because:
            because = (because + "：" if because else "") + str(item["sentence"])[:240]
        out.append({**item, "amount": format(amount * scale, "f"), "scale": "one",
                    "ref": dict(ref), "because": because})
    notes = []
    if out:
        notes.append(f"一次性项目：读取自 {source}，{len(out)} 项从基期剔除")
    else:
        notes.append("一次性项目：没有可读的一次性项目金额记录；规格标注为一次性的行按尖峰规则正常化基期")
    if skipped_annual:
        notes.append(f"一次性项目：{skipped_annual} 项是全年口径，未计入单季基期")
    return out, notes


def relabelled_consensus(connection: sqlite3.Connection, company_ref: str,
                         record: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """The newest held consensus version with its fiscal periods re-derived.

    Batch B's ``financial_period.relabel_consensus`` over the stored row:
    the calendar an 8-K release advanced, year-ago checks against filed and
    released actuals, broker medians when held.  Read-only.  ``None`` when
    nothing is held or the re-derivation cannot run -- callers then fall back
    to the stored labels and say so.  ACN on 2026-10-09: the stored
    ``FY2026Q4`` row is ``FY2027Q1``.
    """

    try:
        if record is None:
            if not _table_exists(connection, "consensus_estimate_versions"):
                return None
            from .consensus_estimate import consensus_ref_for

            row = connection.execute(
                "SELECT record_json FROM consensus_estimate_versions WHERE consensus_ref=? "
                "ORDER BY version_number DESC LIMIT 1", (consensus_ref_for(company_ref),)).fetchone()
            if row is None:
                return None
            record = json.loads(row[0])
        from .financial_period import relabel_consensus

        extra: list[dict[str, Any]] = []
        try:
            from .earnings_release_intake import core_state_dir
            from .earnings_release_outlook import actuals_for_calendar, latest_outlook

            outlook = latest_outlook(connection, str(record.get("company_ref") or company_ref),
                                     state_dir=core_state_dir(connection))
            if (outlook and outlook.get("items")
                    and str(outlook.get("report_date") or "") <= str(record.get("as_of") or "")):
                extra = actuals_for_calendar(outlook["items"], f"sec:filing:{outlook.get('accession')}")
        except Exception:  # noqa: BLE001 - the outlook only sharpens the year-ago check
            extra = []
        anchors = None
        try:
            from .consensus_table import street_anchors
            from .street_estimate_tables import street_table_from

            anchors = street_anchors(street_table_from(
                connection, str(record.get("company_ref") or company_ref), as_of=record.get("as_of"))) or None
        except Exception:  # noqa: BLE001
            anchors = None
        derived = relabel_consensus(connection, {**dict(record), "company_ref": record.get("company_ref")
                                                 or company_ref}, extra, anchors)
    except Exception:  # noqa: BLE001 - any failure leaves the stored labels in force
        return None
    if derived is None:
        return None
    return {"record": dict(record), "relabel": derived}


def held_consensus_targets(
    connection: sqlite3.Connection, company_ref: str,
    revenue_history: Mapping[str, Decimal],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Quarterly revenue consensus on batch B's relabelled fiscal periods.

    The stored vendor labels are re-derived first (``relabelled_consensus``);
    a row the relabel marks ``reported`` (a pre-report consensus for a quarter
    already announced) or ``duplicate`` is not a target.  A row is adopted when
    its year-ago revenue matches what was filed a year before its (corrected)
    period, or when the relabel itself confirmed it against held actuals.
    Without a relabel the stored labels are used and the note says so.
    """

    notes: list[str] = []
    if not _table_exists(connection, "consensus_estimate_versions"):
        return {}, ["一致预期：本环境没有一致预期记录"]
    try:
        from .consensus_estimate import consensus_ref_for
        consensus_ref = consensus_ref_for(company_ref)
    except Exception:
        return {}, ["一致预期：无法确定一致预期记录"]
    row = connection.execute(
        "SELECT record_json FROM consensus_estimate_versions WHERE consensus_ref=? "
        "ORDER BY version_number DESC LIMIT 1", (consensus_ref,)).fetchone()
    if row is None:
        return {}, ["一致预期：这家公司还没有一致预期记录"]
    record = json.loads(row[0])
    relabelled = relabelled_consensus(connection, company_ref, record)
    if relabelled is not None:
        rows = list(relabelled["relabel"].get("revenue_estimates") or [])
        changed = relabelled["relabel"].get("changed_labels") or []
        notes.append("一致预期：财期按最新已报告季度重新标注"
                     + (f"（{'、'.join(changed[:4])}）" if changed else "（标签未变）"))
    else:
        rows = list(record.get("revenue_estimates") or [])
        notes.append("一致预期：无法重新核对财期，沿用存储的财期标签")
    targets: dict[str, dict[str, Any]] = {}
    for item in rows:
        if item.get("period_kind") != "quarter" or not item.get("period_end"):
            continue
        if item.get("duplicate"):
            continue
        label = item.get("label")
        if item.get("reported"):
            notes.append(f"一致预期 {label}：该季度已公布，属于公布前的一致预期，不作为预测目标")
            continue
        end = str(item["period_end"])
        value = _decimal(item.get("avg"))
        year_ago = _decimal(item.get("year_ago"))
        if value is None or value <= 0:
            continue
        prior_end = _year_ago_end(end)
        filed_prior = next((amount for held_end, amount in revenue_history.items()
                            if abs((date.fromisoformat(held_end) - prior_end).days) <= 7),
                           None)
        confirmed = item.get("year_ago_check") == "match"
        if filed_prior is None or year_ago is None:
            if not confirmed:
                notes.append(f"一致预期 {label}：无法用一年前的申报收入核对财期映射，未采用")
                continue
        elif abs(filed_prior - year_ago) > filed_prior * Decimal("0.005"):
            notes.append(
                f"一致预期 {label}：上年同期值与申报收入不符（财期映射可能错位），未采用")
            continue
        original = item.get("original_label")
        relabel_text = (f", relabelled from {original} ({item.get('label_basis')})"
                        if original and original != label else "")
        targets[end] = {
            "source": "consensus", "value": format(value, "f"),
            "ref": {"kind": "figure", "ref": str(record.get("id")), "concept": None,
                    "period_end": end, "accession": None},
            "because": (f"street consensus revenue of {value} for {label}{relabel_text} "
                        f"({item.get('number_of_analysts')} analysts, as of "
                        f"{record.get('as_of')})"),
            "label": label, "original_label": original,
        }
    return targets, notes


def prior_judgements(prior: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Live analyst or event-decided assumptions of the current version."""

    if not isinstance(prior, Mapping):
        return []
    forecast = {str(item.get("end")) for item in prior.get("forecast_periods") or []}
    out = []
    for item in prior.get("assumptions") or []:
        if item.get("superseded_by") or str(item["period"]["end"]) not in forecast:
            continue
        provenance = item.get("provenance") or {}
        decided = str(provenance.get("decided_by") or "")
        is_judgement = item.get("kind") == "human" or (
            item.get("kind") == "estimate" and provenance.get("rule_ref") is None)
        if not is_judgement:
            continue
        out.append({
            "driver_ref": str(item["driver_ref"]),
            "period_end": str(item["period"]["end"]),
            "measure": str(item["measure"]),
            "value": str(item["value"]),
            "because": (f"carried from {prior.get('id')}: {item.get('because')} "
                        f"(decided by {decided})"),
            "refs": [{"kind": "human_decision", "ref": str(item["ref"]), "concept": None,
                      "period_end": str(item["period"]["end"]), "accession": None}],
        })
    return out


def load_assumption_sources(
    connection: sqlite3.Connection, company_ref: str, *,
    revenue_history: Mapping[str, Decimal], forecast_ends: Sequence[str] = (),
    prior: Mapping[str, Any] | None = None, reporting_unit: str = "usd",
) -> dict[str, Any]:
    notes: list[str] = []
    preliminary = preliminary_quarter(connection, company_ref, reporting_unit=reporting_unit)
    provided, provider = _provider_targets(connection, company_ref)
    targets: dict[str, dict[str, Any]] = {}
    if provider is not None and provided:
        for item in provided:
            if item.get("metric") != "revenue" or not item.get("period_end"):
                continue
            value = _decimal(item.get("value"))
            if value is None:
                continue
            end = str(item["period_end"])
            held = targets.get(end)
            # guidance outranks consensus for the same quarter
            if held is not None and held["source"] == "guidance":
                continue
            targets[end] = {
                "source": "guidance" if item.get("source") == "guidance" else "consensus",
                "value": format(value, "f"),
                "ref": {"kind": "figure", "ref": str(item.get("ref") or provider),
                        "concept": None, "period_end": end, "accession": None},
                "because": str(item.get("because") or f"{item.get('source')} revenue {value}"),
            }
        notes.append(f"指引/一致预期：读取自 {provider}")
    else:
        if provider is not None:
            notes.append(f"指引/一致预期：{provider} 没有给出目标")
        notes.append("公司指引：暂无可读的指引记录，收入不按指引调整")
        consensus, consensus_notes = held_consensus_targets(
            connection, company_ref, revenue_history)
        targets.update(consensus)
        notes.extend(consensus_notes)
    if preliminary is not None and preliminary["period_end"] in targets:
        targets.pop(preliminary["period_end"])
    ranged = [item for item in provided if item.get("low") is not None
              and item.get("high") is not None] if provider is not None else []
    bounds, bound_notes = revenue_guidance_bounds(connection, company_ref, ranged)
    notes.extend(bound_notes)
    items, item_notes = one_off_items(connection, company_ref)
    notes.extend(item_notes)
    if forecast_ends:
        targets = {end: item for end, item in targets.items() if end in set(forecast_ends)}
    if forecast_ends:
        bounds = {end: item for end, item in bounds.items() if end in set(forecast_ends)}
    return {
        "preliminary": preliminary,
        "revenue_targets": targets,
        "revenue_bounds": bounds,
        "one_offs": {"concepts": [], "items": items},
        "judgement": prior_judgements(prior),
        "notes": notes,
    }


__all__ = [
    "GUIDANCE_RANGE_PROVIDERS", "ONE_OFF_PROVIDERS", "TARGET_PROVIDER_MODULES",
    "held_consensus_targets", "load_assumption_sources", "one_off_items", "relabelled_consensus",
    "preliminary_quarter", "prior_judgements", "revenue_guidance_bounds",
]
