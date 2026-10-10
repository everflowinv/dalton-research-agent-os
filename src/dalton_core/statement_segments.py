"""Operating-segment figures the filed statements hold, per company (2026-10-09).

Two sources, the ledger first:

* ``coverage_mission_statement_lines`` on the segment axis -- what the
  statements lane recorded.  EPAM presents its segments on the face of the
  income statement, so its ledger has them.
* For a held filing whose ledger has no income-statement segment line, the
  statements lane's own raw parse (the ``raw-sink`` object its ingest names,
  re-hashed before it is read) re-normalised with
  :func:`sec_financials_normalise.segment_lines`.  IBM keeps its segment
  table in the notes; until 2026-10-09 the normaliser dropped those facts, and
  the ledger is append-only and ingests a filing once, so the filings already
  held are read back from the parse they were recorded from.

Read-only: no write, no network, no model.  Nothing here decides anything; it
is what the planner and the model readers can see instead of "文件数字为零".
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .sec_financials_normalise import SEGMENT_AXIS, segment_lines

#: Income-statement concepts a segment table is read for, by role.
_REVENUE = ("us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax")
_PROFIT = (
    "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "us-gaap:OperatingIncomeLoss", "us-gaap:SegmentReportingInformationProfitLoss",
)


def _ledger_lines(connection: sqlite3.Connection, ingest_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(
        "SELECT statement,concept,label,dimension_member,dimension_count,period_start,"
        "period_end,value,unit FROM coverage_mission_statement_lines "
        "WHERE ingest_id=? AND dimension_axis=? ORDER BY ordinal",
        (ingest_id, SEGMENT_AXIS)).fetchall()]


def _raw_parse(state_dir: Path | None, refs_json: str, accession: str) -> tuple[Any, str | None]:
    if state_dir is None:
        return None, "no state directory to read the recorded parse from"
    from .acquired_sec_filings import _spool_bytes

    try:
        refs = json.loads(refs_json)
    except (TypeError, ValueError):
        return None, "ingest names no readable raw parse"
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, str) or not ref.startswith("raw-sink:"):
            continue
        raw, problem = _spool_bytes(Path(state_dir), ref.removeprefix("raw-sink:"))
        if raw is None:
            return None, problem
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, "recorded parse is not JSON"
        for filing in payload.get("filings") or ():
            if isinstance(filing, Mapping) and filing.get("accession") == accession:
                return filing, None
        return None, "recorded parse does not hold this accession"
    return None, "ingest names no raw parse"


def statement_segment_figures(
    connection: sqlite3.Connection, company_ref: str, *, state_dir: str | Path | None,
) -> dict[str, Any]:
    """Every held filing's operating-segment lines, with where each came from."""

    try:
        filings = connection.execute(
            "SELECT ingest_id,accession,form,report_date,source_record_refs_json "
            "FROM coverage_mission_statement_filings WHERE company_ref=? "
            "ORDER BY report_date DESC, accession", (company_ref,)).fetchall()
    except sqlite3.OperationalError:
        return {"status": "unavailable", "reason": "statements ledger not installed", "filings": []}
    out: list[dict[str, Any]] = []
    for filing in filings:
        lines = [line for line in _ledger_lines(connection, filing["ingest_id"])]
        income = [line for line in lines if line["statement"] == "income"]
        basis, problem = "ledger", None
        if not income:
            parsed, problem = _raw_parse(
                None if state_dir is None else Path(state_dir),
                filing["source_record_refs_json"], filing["accession"])
            if parsed is not None:
                statements = parsed.get("statements") or {}
                income = [line for name in sorted(statements)
                          for line in segment_lines(
                              statement=name,
                              structure=(statements[name] or {}).get("structure") or (),
                              facts=(statements[name] or {}).get("facts") or ())
                          if line["statement"] == "income"]
                basis = "recorded_parse"
        out.append({
            "accession": filing["accession"], "form": filing["form"],
            "report_date": filing["report_date"], "basis": basis,
            "unreadable": problem if not income else None,
            "lines": [{key: line.get(key) for key in (
                "concept", "label", "dimension_member", "period_start", "period_end",
                "value", "unit")} for line in income],
        })
    return {"status": "available", "filings": out}


def segment_summary(figures: Mapping[str, Any], *, limit: int = 8) -> dict[str, Any]:
    """The planner-sized view: which segments, and the latest quarter's revenue and profit."""

    filings = list(figures.get("filings") or ())
    with_lines = [item for item in filings if item.get("lines")]
    segments: dict[str, str] = {}
    latest: dict[str, Any] | None = None
    for filing in with_lines:
        for line in filing["lines"]:
            label = str(line.get("label") or "")
            segments.setdefault(str(line.get("dimension_member")),
                                label.split(" — ", 1)[-1] if " — " in label else label)
    for filing in with_lines:
        quarter = [line for line in filing["lines"]
                   if line.get("period_start") and line.get("period_end") == filing["report_date"]]
        if not quarter:
            continue
        starts = sorted({line["period_start"] for line in quarter}, reverse=True)
        quarter = [line for line in quarter if line["period_start"] == starts[0]]
        rows: dict[str, dict[str, Any]] = {}
        for line in quarter:
            role = ("revenue" if line["concept"] in _REVENUE
                    else "profit" if line["concept"] in _PROFIT else None)
            if role is None:
                continue
            entry = rows.setdefault(str(line["dimension_member"]), {
                "segment": segments.get(str(line["dimension_member"])),
                "member": line["dimension_member"]})
            entry.setdefault(role, {"concept": line["concept"], "value": line["value"],
                                    "unit": line["unit"]})
        latest = {"accession": filing["accession"], "form": filing["form"],
                  "period_start": starts[0], "period_end": filing["report_date"],
                  "basis": filing["basis"], "segments": list(rows.values())[:limit]}
        break
    return {
        "filings_held": len(filings),
        "filings_with_segment_lines": len(with_lines),
        "segment_lines": sum(len(item["lines"]) for item in filings),
        "segments": [{"member": key, "label": value} for key, value in list(segments.items())[:limit]],
        "read_from_recorded_parse": sum(1 for item in with_lines if item["basis"] == "recorded_parse"),
        "latest_quarter": latest,
        "unreadable": [{"accession": item["accession"], "reason": item["unreadable"]}
                       for item in filings if item.get("unreadable")][:3],
    }


__all__ = ["segment_summary", "statement_segment_figures"]
