"""P13ao: the model input table -- the specification met with the filings.

The specification says how a company should be modelled. The series says what
each filed line actually did. This is the join, and it is the first artefact in
this system that looks like a model: one row per model line, one column per
quarter, every cell saying where it came from.

The join is not one to one, and pretending otherwise is the mistake this module
exists to avoid. IBM's specification splits ``us-gaap:CostOfRevenue`` into four
lines that behave differently -- consulting delivery staff, flexible delivery,
software operations, hardware inputs -- because that is what the business does,
while the filing reports a single figure called "Cost". The filed series is
real and the split is not filed anywhere.

So a filed line that several model rows draw on is reported as a **constraint**
rather than as four copies of one number: here is the total the company
reported, here are the rows that have to add up to it, and none of them has a
value yet. Handing back the same series four times would look like data and be
an assertion nobody made.

Three things a row can be, and the table says which for every row:

* **filed** -- one model row, one filed concept, the series is the history;
* **share of filed** -- several rows draw on one filed line, so each is an
  estimate inside a total that is known;
* **estimated** -- no filed counterpart at all. Billable headcount and
  utilisation are not in GAAP; the specification already said so by leaving
  ``basis_concept`` null, and this carries that through instead of quietly
  producing an empty row.

Nothing here is stored. The specification and the statements ledger determine
this table completely, so keeping a copy would only create something that can
drift from what it was derived from.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .company_model_series import quarterly_series, series_gaps

FILED = "filed"
SHARED = "share_of_filed"
ESTIMATED = "estimated"
NOT_FOUND = "concept_not_found"
AMBIGUOUS = "concept_in_several_statements"

# The specification's horizon says how many quarters the model rests on; this
# bounds one table regardless, so a specification asking for twenty years
# cannot produce a table nobody can read.
MAX_PERIODS = 24


class ModelInputError(ValueError):
    """The specification cannot be joined to this company's filings."""


def _rows_of(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in spec.get("revenue_drivers") or []:
        rows.append({
            "kind": "revenue_driver", "ref": item["ref"], "label": item["label"],
            "basis_concept": item.get("basis_concept"),
            "driver_kind": item.get("kind"), "unit": item.get("unit"),
            "because": item.get("because"),
        })
    for item in spec.get("expense_lines") or []:
        row = {
            "kind": "expense_line", "ref": item["ref"], "label": item["label"],
            "basis_concept": item.get("basis_concept"),
            "behaviour": item.get("behaviour"), "driver_ref": item.get("driver_ref"),
            "because": item.get("because"),
        }
        # These W5 fields are additive.  Do not synthesize null keys into a
        # legacy specification: its derived record remains byte-for-byte the
        # same until an analyst explicitly classifies the expense line.
        if "cost_driver_slot" in item:
            row["cost_driver_slot"] = item["cost_driver_slot"]
        if "cost_driver_unbound_reason" in item:
            row["cost_driver_unbound_reason"] = item["cost_driver_unbound_reason"]
        rows.append(row)
    return rows


def _series_for(missions: Any, company_ref: str, concept: str) -> dict[str, Any]:
    """The filed series for one concept, and which statement it came from.

    No statement is passed to the query. Across every company held, a concept
    appears in exactly one statement, so filtering by a guessed statement could
    only ever exclude the right answer; and when that stops being true the
    ambiguity is reported rather than resolved by preference.
    """

    lines = missions.statement_series_lines(company_ref, concept)
    if not lines:
        return {"status": NOT_FOUND, "statement": None, "series": None}
    statements = sorted({str(row["statement"]) for row in lines})
    if len(statements) > 1:
        return {"status": AMBIGUOUS, "statement": statements, "series": None}
    # The label must come from a row that is not a breakdown. Taking the last
    # row of any kind put "EMEA" on Accenture's total revenue -- the figures
    # were the totals and the name was a geography, which is worse than an
    # unlabelled row because a reader has no reason to doubt it.
    reported = [row for row in lines
                if not row.get("is_breakdown") and not row.get("dimension_axis")]
    return {
        "status": FILED, "statement": statements[0],
        "label": (reported[-1]["label"] if reported else concept),
        "series": quarterly_series(lines),
    }


# How many unused filed lines to name. Enough to notice a missing top line,
# not so many that the reviewer stops reading the list.
MAX_UNUSED_REPORTED = 12


def _unused_income_lines(
    missions: Any, company_ref: str, used: set[str],
) -> list[dict[str, Any]]:
    """Income-statement lines the company filed that no model row draws on.

    Live, IBM's specification bound every revenue driver to nothing filed --
    adoption, price mix, conversion, rate mix are genuinely not in GAAP -- with
    the result that the model had **no filed top line at all**, while the
    filings report ``us-gaap:Revenues`` plainly. Nothing was wrong with any
    single row; the omission only existed between them, which is exactly the
    kind of thing a reviewer cannot see and a derived list can.

    The income statement alone, because that is the one statement the frame
    always requires, and because listing every unused balance-sheet line would
    bury the signal in noise.
    """

    filings = missions.statement_filings(company_ref)
    if not filings:
        return []
    latest = sorted(filings, key=lambda item: (item["report_date"],
                                               item["accession"]))[-1]
    lines = missions.statement_lines(latest["ingest_id"], statement="income")
    # Earnings per share and share counts are filed on this statement and are
    # not model rows, and the data says so without a list of names to maintain:
    # they carry a different unit. The money unit is whichever one most of the
    # statement is in, so a filer reporting in something other than dollars is
    # read the same way.
    units: dict[str, int] = {}
    for row in lines:
        unit = str(row["unit"])
        units[unit] = units.get(unit, 0) + 1
    money = max(units, key=lambda unit: units[unit]) if units else None

    seen: dict[str, dict[str, Any]] = {}
    for row in lines:
        if row["is_breakdown"] or row["concept"] in used:
            continue
        if money is not None and str(row["unit"]) != money:
            continue
        seen.setdefault(str(row["concept"]), {
            "concept": str(row["concept"]),
            "label": str(row["label"]),
            "level": int(row["level"]),
        })
    return sorted(seen.values(),
                  key=lambda item: (item["level"], item["concept"]))[:MAX_UNUSED_REPORTED]


def build_model_inputs(
    missions: Any, spec: Mapping[str, Any], *, max_periods: int = MAX_PERIODS,
) -> dict[str, Any]:
    """One company's model rows, its filed history, and what still has to be estimated."""

    company_ref = spec.get("company_ref")
    if not isinstance(company_ref, str) or not company_ref:
        raise ModelInputError("specification carries no company_ref")
    horizon = spec.get("horizon") or {}
    wanted = horizon.get("historical_quarters")
    if isinstance(wanted, bool) or not isinstance(wanted, int) or wanted < 1:
        wanted = max_periods
    limit = max(1, min(int(wanted), int(max_periods)))

    rows = _rows_of(spec)
    concepts: dict[str, list[str]] = {}
    for row in rows:
        concept = row.get("basis_concept")
        if concept:
            concepts.setdefault(str(concept), []).append(row["ref"])

    filed: dict[str, dict[str, Any]] = {}
    for concept in sorted(concepts):
        filed[concept] = _series_for(missions, company_ref, concept)

    # The columns are the periods the filings actually cover, newest last, cut
    # to the horizon the specification asked for. A period no filed line
    # reaches is not a column: an all-empty column is not history.
    #
    # Both shapes count. A balance-sheet line has no quarters at all -- it is a
    # series of instants -- and taking columns from durations alone made IBM's
    # financing receivables come back as an empty row rather than as the
    # point-in-time series it is. An empty row that should have had values is
    # the worst of both: it looks like an answer and it is missing.
    ends: set[str] = set()
    for entry in filed.values():
        series = entry.get("series")
        if series:
            ends.update(str(item["period_end"]) for item in series["quarters"])
            ends.update(str(item["period_end"]) for item in series["instants"])
    periods = sorted(ends)[-limit:]

    filed_lines: list[dict[str, Any]] = []
    for concept, entry in filed.items():
        drawn_on_by = sorted(concepts[concept])
        line: dict[str, Any] = {
            "concept": concept,
            "statement": entry.get("statement"),
            "label": entry.get("label"),
            "status": entry["status"],
            "drawn_on_by": drawn_on_by,
            "is_split": len(drawn_on_by) > 1,
            "cells": {},
            "gaps": [],
        }
        series = entry.get("series")
        if series:
            # A line is one shape or the other, never both: a balance is an
            # instant and a flow is a duration. Prefer the durations when a
            # concept somehow has both, and say which shape was used, because
            # "18.7 billion in the quarter" and "18.7 billion on that day" are
            # different claims and a column header cannot tell them apart.
            quarters = {str(item["period_end"]): item for item in series["quarters"]}
            instants = {str(item["period_end"]): item for item in series["instants"]}
            kept = quarters or instants
            line["period_basis"] = "duration" if quarters else (
                "instant" if instants else None)
            line["cells"] = {
                end: {
                    "period_start": kept[end].get("period_start"),
                    "value": kept[end]["value"],
                    "unit": kept[end]["unit"],
                    "basis": kept[end]["basis"],
                    "source_accessions": kept[end]["source_accessions"],
                }
                for end in periods if end in kept
            }
            line["gaps"] = series_gaps(series) if quarters else []
            line["derived_count"] = series["derived_count"]
        filed_lines.append(line)

    for row in rows:
        concept = row.get("basis_concept")
        if not concept:
            row["status"] = ESTIMATED
            row["reason"] = "the specification names no filed counterpart for this line"
            continue
        entry = filed[str(concept)]
        if entry["status"] != FILED:
            row["status"] = entry["status"]
            row["reason"] = (
                "this concept is not in the statements held"
                if entry["status"] == NOT_FOUND
                else f"this concept appears in {entry['statement']}"
            )
            continue
        if len(concepts[str(concept)]) > 1:
            row["status"] = SHARED
            row["reason"] = (
                f"{len(concepts[str(concept)])} model lines split one filed line; "
                "the total is known and the split is not filed"
            )
        else:
            row["status"] = FILED
        row["statement"] = entry["statement"]

    return {
        "schema_version": "0.1",
        "company_ref": company_ref,
        "spec_ref": spec.get("spec_id"),
        "state_hash": spec.get("state_hash"),
        "periods": periods,
        "filed_lines": sorted(filed_lines, key=lambda item: item["concept"]),
        "rows": rows,
        "operating_metrics": [
            {
                "ref": item["ref"], "label": item["label"], "unit": item.get("unit"),
                "periodicity": item.get("periodicity"),
                "disclosed": item.get("disclosed"),
                # Even a disclosed metric is not in these statements: bookings
                # and utilisation live in the earnings materials, not in XBRL.
                "status": ESTIMATED,
                "reason": ("the company reports this, but not in the financial "
                           "statements held" if item.get("disclosed")
                           else "the company does not report this"),
            }
            for item in (spec.get("operating_metrics") or [])
        ],
        "readiness": {
            **readiness(rows, filed_lines, periods),
            "filed_income_lines_no_row_uses": _unused_income_lines(
                missions, company_ref, set(concepts)),
        },
    }


def readiness(
    rows: Sequence[Mapping[str, Any]],
    filed_lines: Sequence[Mapping[str, Any]],
    periods: Sequence[str],
) -> dict[str, Any]:
    """What the table has and what it is still missing, counted plainly.

    Deliberately not a score. "Sixty percent ready" invites a reader to accept
    a model that is missing the one line the thesis turns on; the counts and
    the names of the unmet rows do not.
    """

    by_status: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status"))
        by_status[status] = by_status.get(status, 0) + 1
    splits = [item for item in filed_lines if item.get("is_split")]
    empty = sorted(
        str(item["concept"]) for item in filed_lines
        if item.get("status") == FILED and not item.get("cells")
    )
    return {
        "period_count": len(periods),
        # A filed line that resolved but landed no value in any column. Named
        # rather than counted, because it means the concept exists in the
        # filings and something about its periods did not survive the join --
        # which is a bug to look at, not a fact about the company.
        "filed_lines_with_no_values": empty,
        "first_period": periods[0] if periods else None,
        "last_period": periods[-1] if periods else None,
        "rows_by_status": by_status,
        "filed_lines_needing_a_split": [
            {"concept": item["concept"], "into": item["drawn_on_by"]}
            for item in splits
        ],
        "rows_with_no_filed_history": sorted(
            str(row["ref"]) for row in rows
            if row.get("status") in (ESTIMATED, NOT_FOUND, AMBIGUOUS)
        ),
        "filed_lines_with_gaps": sorted(
            str(item["concept"]) for item in filed_lines if item.get("gaps")
        ),
        "derived_cells": sum(
            1 for item in filed_lines
            for cell in (item.get("cells") or {}).values()
            if cell.get("basis") != "reported"
        ),
    }


__all__ = [
    "AMBIGUOUS",
    "ESTIMATED",
    "FILED",
    "MAX_PERIODS",
    "NOT_FOUND",
    "SHARED",
    "ModelInputError",
    "build_model_inputs",
    "readiness",
]
