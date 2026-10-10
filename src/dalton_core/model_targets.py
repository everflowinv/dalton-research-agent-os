"""Batch B: revenue targets for the coverage model, from the numbers lane.

``forecast_assumption_sources.TARGET_PROVIDER_MODULES`` names this module;
the coverage model ranks *guidance > consensus > judgement > trailing* and
asks here for the first two, per forecast quarter:

* **guidance** -- the company's own next-quarter revenue range from its newest
  earnings release (``earnings_release_outlook``), midpoint, with the quoted
  outlook line;
* **consensus** -- the vendor's quarterly revenue consensus on the
  *re-derived* fiscal calendar (``consensus_table`` / ``financial_period``:
  8-K releases advance it, year-ago and broker-median evidence place it), never
  a row for a quarter already reported.

Read-only; any failure yields fewer targets, never an exception.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any

_SCALE = {"one": Decimal(1), "thousand": Decimal(1000), "million": Decimal(1_000_000),
          "billion": Decimal(1_000_000_000)}


def _dec(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def forecast_model_targets(connection: sqlite3.Connection, company_ref: str) -> list[dict[str, Any]]:
    """``[{source, metric, period_end, value, ref, because}]`` for revenue."""

    out: list[dict[str, Any]] = []
    try:
        from .earnings_release_intake import core_state_dir
        from .earnings_release_outlook import latest_outlook

        outlook = latest_outlook(connection, company_ref, state_dir=core_state_dir(connection))
    except Exception:  # noqa: BLE001
        outlook = None
    for item in (outlook or {}).get("items") or ():
        if (item.get("kind") != "guidance" or item.get("metric") != "revenue"
                or item.get("period_kind") != "quarter" or not item.get("period_end")):
            continue
        low, high = _dec(item.get("low")), _dec(item.get("high"))
        scale = _SCALE.get(str(item.get("scale") or "one"))
        if low is None or high is None or scale is None or item.get("unit") != "currency":
            continue
        mid = (low + high) / 2 * scale
        out.append({
            "source": "guidance", "metric": "revenue", "period_end": item["period_end"],
            "value": format(mid.quantize(Decimal(1)), "f"),
            "ref": f"sec:filing:{outlook.get('accession')}",
            "because": (f"公司指引 {item.get('period')} 收入 {item.get('low')}–{item.get('high')} "
                        f"{item.get('scale')}，取中值；原文：{item.get('heading_as_printed') or ''} "
                        f"{str(item.get('quote') or '')[:160]}"),
        })
    try:
        from .consensus_table import consensus_table_from

        table = consensus_table_from(connection, company_ref, None, state_dir=None)
    except Exception:  # noqa: BLE001
        table = {}
    version = (table or {}).get("version") or {}
    for quarter in (table or {}).get("quarters") or ():
        cell = quarter.get("revenue") or {}
        value = _dec(cell.get("mean"))
        if value is None or value <= 0 or not cell.get("period_end"):
            continue
        out.append({
            "source": "consensus", "metric": "revenue", "period_end": cell["period_end"],
            "value": format(value, "f"), "ref": str(version.get("ref") or "consensus"),
            "because": (f"一致预期 {quarter['label']} 收入 {cell.get('mean')}（{cell.get('count')} 家，"
                        f"{version.get('as_of')}；财期依据 {cell.get('label_basis')}，原标签 "
                        f"{cell.get('stored_label')}）"),
        })
    return out


__all__ = ["forecast_model_targets"]
