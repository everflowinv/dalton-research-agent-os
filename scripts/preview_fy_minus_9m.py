#!/usr/bin/env python3
"""Read-only: which fourth quarters the FY − 9M rule would derive in one environment.

Opens ``<state-dir>/core.sqlite`` with ``mode=ro`` and, for every 10-K the
financial-statements lane has recorded, runs the same derivation the quarter
lane runs (``dalton_core.sec_fy_minus_9m.derive``).  Prints one JSON row per
10-K: the derived quarter, the prior-year one, the growth and its precision,
or the reason the rule refuses.  Also says whether the quarter is already held
as a ``quarterly_revenue_yoy_growth`` Claim, and cross-checks the components
against what the Ledger already holds: the filed quarters plus the derived
fourth quarter against the fiscal year, and each quarter against the
quarterly Claims admitted for it.

Nothing is written; no network.

    .venv/bin/python scripts/preview_fy_minus_9m.py --state-dir <state>
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.sec_fy_minus_9m import FyMinus9mRefused, derive  # noqa: E402

# The same-accession rules' sentence: "... of USD <current> for <period>, up
# 1.2% year over year from USD <prior> in the comparable quarter."
_GROWTH_SENTENCE = re.compile(
    r"of USD (-?\d+) for \S+, (?:up|down) [\d.]+% year over year from USD (-?\d+)")


def _ledger(connection: sqlite3.Connection, company_ref: str) -> dict[str, Any]:
    growth: dict[str, dict[str, Any]] = {}
    revenue: dict[str, set[Decimal]] = {}
    for (raw,) in connection.execute(
            "SELECT claim_json FROM claim_versions WHERE json_extract(claim_json,'$.subject_ref')=? "
            "AND json_extract(claim_json,'$.metric_or_aspect') IN "
            "('quarterly_revenue_yoy_growth','revenue')", (company_ref,)):
        claim = json.loads(raw)
        if claim["metric_or_aspect"] == "revenue":
            if claim.get("value") is not None:
                revenue.setdefault(claim["period"], set()).add(Decimal(claim["value"]))
            continue
        match = _GROWTH_SENTENCE.search(claim.get("normalized_statement") or "")
        growth[claim["period"]] = {
            "value": claim.get("value"), "basis": claim.get("basis"),
            "current": Decimal(match.group(1)) if match else None,
            "prior": Decimal(match.group(2)) if match else None,
        }
    return {"growth": growth, "revenue": revenue}


def cross_check(derivation: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any]:
    """Four quarters against the year, and each quarter against the Ledger."""

    components = {item["role"]: item for item in derivation["components"]}
    result: dict[str, Any] = {}
    for year in ("current", "prior"):
        fy = Decimal(components[f"{year}.fiscal_year"]["value"])
        q4 = Decimal(derivation[year]["q4"]["value"])
        quarters = [components[f"{year}.q{index}"] for index in (1, 2, 3)]
        filed = sum((Decimal(item["value"]) for item in quarters), Decimal(0))
        mismatches: list[dict[str, str]] = []
        held: list[Decimal] = []
        for index, item in enumerate(quarters, start=1):
            period = f"{item['period_start']}..{item['period_end']}"
            seen: list[Decimal] = sorted(ledger["revenue"].get(period, ()))
            current = components[f"current.q{index}"]
            growth = ledger["growth"].get(f"{current['period_start']}..{current['period_end']}")
            if growth is not None and growth.get(year) is not None:
                seen.append(growth[year])
            for value in seen:
                if value != Decimal(item["value"]):
                    mismatches.append({"period": period, "ledger": str(value),
                                       "component": item["value"]})
            if seen:
                held.append(seen[0])
        result[year] = {
            "filed_quarters_plus_q4_minus_fy": str(filed + q4 - fy),
            "ledger_quarters_held": len(held),
            "ledger_quarters_plus_q4_minus_fy": (
                str(sum(held, Decimal(0)) + q4 - fy) if len(held) == 3 else None),
            "ledger_mismatches": mismatches,
        }
    return result


def preview(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for filing in connection.execute(
            "SELECT ingest_id, company_ref, accession, report_date "
            "FROM coverage_mission_statement_filings WHERE form='10-K' "
            "ORDER BY company_ref, report_date").fetchall():
        row: dict[str, Any] = {"company_ref": filing["company_ref"],
                               "accession": filing["accession"],
                               "fiscal_year_end": filing["report_date"]}
        try:
            derivation = derive(connection, filing["ingest_id"])
        except FyMinus9mRefused as exc:
            rows.append({**row, "status": "refused", "reason": str(exc)})
            continue
        ledger = _ledger(connection, filing["company_ref"])
        period = derivation["current"]["q4"]["period"]
        rows.append({
            **row, "status": "derivable", "concept": derivation["concept"],
            "period": period, "growth_percent": derivation["growth"],
            "q4": derivation["current"]["q4"], "prior_q4": derivation["prior"]["q4"],
            "nine_months": derivation["current"]["nine_months"]["basis"],
            "precision": derivation["precision"],
            "already_held": ledger["growth"].get(period),
            "cross_check": cross_check(derivation, ledger),
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    core = Path(args.state_dir).expanduser().resolve() / "core.sqlite"
    if not core.is_file():
        raise SystemExit(f"no Core at {core}")
    connection = sqlite3.connect(f"file:{core}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        print(json.dumps(preview(connection), ensure_ascii=False, indent=1, default=str))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - an owner-run script
    sys.exit(main())
