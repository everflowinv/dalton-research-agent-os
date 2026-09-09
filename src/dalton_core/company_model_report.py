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

Figures print in millions for width, and the underlying value is never touched
-- the table is a view, and the ledger keeps what was filed.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from .company_model_inputs import ESTIMATED, FILED, SHARED, build_model_inputs

LABEL_WIDTH = 46
CELL_WIDTH = 12


def _millions(value: Any) -> str:
    try:
        number = Decimal(str(value)) / Decimal(1_000_000)
    except (InvalidOperation, ValueError, TypeError):
        return "?"
    return f"{number:,.1f}"


def _mark(cell: Mapping[str, Any]) -> str:
    # A derived figure is marked wherever it is shown. A reader who does not
    # look up the legend still sees that this one is not like the others.
    return "" if cell.get("basis") == "reported" else "*"


def render_model_inputs(table: Mapping[str, Any], *, entity_name: str | None = None) -> str:
    periods: Sequence[str] = table.get("periods") or []
    readiness = table.get("readiness") or {}
    out: list[str] = []
    title = entity_name or table.get("company_ref") or "company"
    out.append(f"MODEL INPUTS  {title}")
    out.append(f"specification {table.get('spec_ref')}")
    out.append(
        f"{readiness.get('period_count', 0)} quarters "
        f"{readiness.get('first_period')} .. {readiness.get('last_period')}"
        "   figures in millions, * = derived from cumulative"
    )
    out.append("")

    header = "FILED HISTORY".ljust(LABEL_WIDTH) + "".join(
        end[2:].rjust(CELL_WIDTH) for end in periods)
    out.append(header)
    out.append("-" * len(header))
    for line in table.get("filed_lines") or []:
        label = str(line.get("label") or line["concept"])[:LABEL_WIDTH - 2]
        if line.get("is_split"):
            label = f"{label} [split]"
        row = label[:LABEL_WIDTH].ljust(LABEL_WIDTH)
        if line.get("status") != FILED:
            row += f"  ({line.get('status')})"
            out.append(row)
            continue
        cells = line.get("cells") or {}
        for end in periods:
            cell = cells.get(end)
            row += ("--" if cell is None
                    else _millions(cell["value"]) + _mark(cell)).rjust(CELL_WIDTH)
        out.append(row)

    out.append("")
    out.append("MODEL ROWS")
    out.append("-" * LABEL_WIDTH)
    for row in table.get("rows") or []:
        status = row.get("status")
        note = {
            FILED: "filed",
            SHARED: "share of a filed line",
            ESTIMATED: "estimated -- no filed counterpart",
        }.get(str(status), str(status))
        out.append(f"  {str(row['ref'])[:34]:34} {row['kind'][:14]:14} {note}")

    metrics = table.get("operating_metrics") or []
    if metrics:
        out.append("")
        out.append("OPERATING METRICS (never in the statements)")
        out.append("-" * LABEL_WIDTH)
        for item in metrics:
            disclosed = "company reports it" if item.get("disclosed") else "not reported"
            out.append(f"  {str(item['ref'])[:34]:34} {str(item.get('unit'))[:10]:10} {disclosed}")

    out.append("")
    out.append("WHAT IS STILL MISSING")
    out.append("-" * LABEL_WIDTH)
    splits = readiness.get("filed_lines_needing_a_split") or []
    for item in splits:
        out.append(f"  split {item['concept']} into: {', '.join(item['into'])}")
    unmet = readiness.get("rows_with_no_filed_history") or []
    if unmet:
        out.append(f"  no filed history ({len(unmet)}): {', '.join(unmet)}")
    gaps = readiness.get("filed_lines_with_gaps") or []
    if gaps:
        out.append(f"  quarters missing from ({len(gaps)}) filed lines "
                   "-- a 10-Q never covers the fourth quarter")
    empty = readiness.get("filed_lines_with_no_values") or []
    if empty:
        out.append(f"  resolved but empty (look at this): {', '.join(empty)}")
    unused = readiness.get("filed_income_lines_no_row_uses") or []
    if unused:
        out.append("  filed but no model row uses it:")
        for item in unused:
            out.append(f"      {item['label'][:40]:40} {item['concept']}")
    if not (splits or unmet or gaps or empty or unused):
        out.append("  nothing")
    return "\n".join(out)


# -- P13-M2: the driver model ----------------------------------------------

DRIVER_LABEL_WIDTH = 42
# How much history to print beside a forecast. Enough to see the trend the
# assumption was taken from; the whole table is what ``render_model_inputs``
# is for, and a page that needs sideways scrolling gets read as a picture.
HISTORY_COLUMNS = 4


def _percent(value: Any) -> str:
    try:
        number = Decimal(str(value)) * Decimal(100)
    except (InvalidOperation, ValueError, TypeError):
        return "?"
    return f"{number:,.2f}%"


def _row(label: str, history: Sequence[str], forecast: Sequence[str]) -> str:
    return (
        label[:DRIVER_LABEL_WIDTH].ljust(DRIVER_LABEL_WIDTH)
        + "".join(item.rjust(CELL_WIDTH) for item in history)
        + " |"
        + "".join(item.rjust(CELL_WIDTH) for item in forecast)
    )


def render_forecast_model(
    record: Mapping[str, Any], *, entity_name: str | None = None,
    history_columns: int = HISTORY_COLUMNS,
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

    history = [str(item) for item in (record.get("history_periods") or [])]
    history = history[-max(0, int(history_columns)):] if history_columns else []
    realised = [str(item["end"]) for item in (record.get("realised_periods") or [])]
    forecast = [str(item["end"]) for item in (record.get("forecast_periods") or [])]
    columns = realised + forecast
    history = [end for end in history if end not in set(columns)]
    drivers = list(record.get("drivers") or [])
    out: list[str] = []
    title = entity_name or record.get("company_ref") or "company"
    out.append(f"DRIVER MODEL  {title}")
    out.append(f"{record.get('id')}  version {record.get('version')}  "
               f"({record.get('change_reason')}"
               + (f", decided: {record['decision']}" if record.get("decision") else "")
               + ")")
    out.append(f"specification {record.get('spec_ref')}   "
               f"formula {record.get('formula_ref')}")
    out.append(f"generator {record.get('generator_ref')}   "
               "figures in millions, * = derived from cumulative")
    if realised:
        out.append(f"filed since this model was made: {', '.join(realised)}")
    out.append("")
    out.append(_row("", [end[2:] for end in history], [end[2:] for end in columns]))
    out.append("-" * (DRIVER_LABEL_WIDTH + CELL_WIDTH * (len(history) + len(columns)) + 2))

    out.append("DRIVERS (filed history)")
    for driver in drivers:
        cells = {str(cell["period_end"]): cell for cell in (driver.get("history") or [])}
        label = f"  {driver.get('label') or driver.get('ref')}"
        if driver.get("role"):
            label = f"{label} [{driver['role']}]"
        if not cells:
            out.append(f"  {str(driver.get('ref'))[:DRIVER_LABEL_WIDTH - 2]:40} "
                       f"{driver.get('note') or driver.get('status')}")
            continue
        out.append(_row(
            label,
            [(_millions(cells[end]["value"]) + _mark(cells[end])) if end in cells else "--"
             for end in history],
            ["" for _ in columns]))
        if driver.get("note"):
            out.append(f"        {driver['note']}")

    assumptions: dict[str, list[Mapping[str, Any]]] = {}
    for item in record.get("assumptions") or []:
        assumptions.setdefault(str(item["driver_ref"]), []).append(item)
    out.append("")
    out.append("ASSUMPTIONS")
    if not assumptions:
        out.append("  none -- no driver has enough filed history to carry forward")
    for driver in drivers:
        rows = assumptions.get(str(driver.get("ref")))
        if not rows:
            continue
        live = _live(rows)
        measure = str(rows[0].get("measure"))
        kinds = sorted({str(item.get("kind")) for item in rows})
        out.append(_row(
            f"  {driver.get('label') or driver.get('ref')}  [{measure}, {'/'.join(kinds)}]",
            ["" for _ in history],
            [_percent(live[end]["value"]) if end in live else "--" for end in columns]))
        for because in dict.fromkeys(
            str(item.get("because")) for item in rows
            if not item.get("superseded_by") and item.get("kind") != "actual"
        ):
            out.append(f"        because {because}")
        for item in rows:
            if item.get("superseded_by"):
                out.append(f"        we assumed {_percent(item['value'])} for "
                           f"{item['period']['end']}: {item.get('because')}")

    out.append("")
    out.append("RESULTS")
    for result in record.get("results") or []:
        live = _live(result.get("cells") or [])
        out.append(_row(
            f"  {result.get('label')}",
            ["" for _ in history],
            [(_millions(live[end]["value"]) if live.get(end, {}).get("status") == "computed"
              else "--") if end in live else "--" for end in columns]))
        for cell in result.get("cells") or []:
            if cell.get("superseded_by") and cell.get("value") is not None:
                actual = live.get(str(cell["period"]["end"]), {})
                out.append(
                    f"        we estimated {_millions(cell['value'])} for "
                    f"{cell['period']['end']}; filed "
                    f"{_millions(actual.get('value'))}")
        if result.get("status") != "computed":
            out.append(f"        {result.get('status')}: "
                       f"{result.get('reason') or 'no reason recorded'}")
        else:
            out.append(f"        {result.get('formula')}")
    # What the chain does not account for, named. A reader looking at operating
    # income has to be able to see which filed lines are not inside it; the
    # formula above says what was subtracted, and this says what was not.
    outside = sorted(
        str(item["concept"]) for item in drivers
        if item.get("role") is None and item.get("history") and item.get("concept"))
    if outside:
        out.append("")
        out.append("  filed lines this chain does not account for:")
        for concept in outside:
            out.append(f"      {concept}")
    out.append("")
    out.append("HOW TO READ IT")
    out.append("-" * DRIVER_LABEL_WIDTH)
    out.append("  every assumption is an estimate carried forward from the filings")
    out.append("  named in its refs; none of them is a view, and all of them are")
    out.append("  meant to be argued with. A result printed as -- was not computed,")
    out.append("  and the line under it says what was missing. A quarter that has")
    out.append("  been filed shows the actual, with what we estimated underneath.")
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
                    record, entity_name=held[-1]["entity_name"] if held else None))
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


__all__ = ["main", "render_forecast_model", "render_model_inputs"]
