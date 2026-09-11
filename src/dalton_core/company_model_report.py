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
    annual_projection: Mapping[str, Any] | None = None,
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
    if annual_projection is not None:
        from .company_model_annual_projection import validate_projection_record

        projection = validate_projection_record(annual_projection, model=record)
        result_labels = {str(item["ref"]): str(item["label"])
                         for item in record.get("results") or []}
        out.append("")
        out.append("ANNUAL STRUCTURED FINANCIALS")
        out.append("-" * DRIVER_LABEL_WIDTH)
        for period in projection["periods"]:
            out.append(f"  {period['label']}")
            for result_ref, outcome in period["line_outcomes"].items():
                label = result_labels.get(result_ref, result_ref)
                if outcome.get("status") == "computed":
                    out.append(
                        f"    {label}: {outcome.get('value')} {outcome.get('unit')}"
                    )
                else:
                    out.append(
                        f"    {label}: unavailable: "
                        f"{outcome.get('reason') or 'no reason recorded'}"
                    )
        out.append("")
        out.append("ANNUAL DILUTED EPS")
        out.append("-" * DRIVER_LABEL_WIDTH)
        out.append(f"  authority {projection['projection_ref']}  "
                   f"{projection['content_hash']}")
        rows = [item for item in projection["periods"]
                if item.get("historical_eps") is not None
                or item.get("forecast_eps") is not None]
        if not rows:
            out.append("  unavailable: no complete fiscal year is bound")
        for item in rows:
            outcome = item.get("historical_eps") or item.get("forecast_eps") or {}
            if outcome.get("status") == "computed":
                try:
                    shown = format(Decimal(str(outcome["value"])), ",.2f")
                except (InvalidOperation, ValueError, TypeError):
                    shown = "?"
                out.append(f"  {item['label']:16} {shown:>12}  {outcome.get('unit')}")
            else:
                out.append(f"  {item['label']:16} unavailable: "
                           f"{outcome.get('reason') or 'no reason recorded'}")
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
        return f"below anything filed in this window (trough {_percent(low)})"
    if value > high:
        return f"above anything filed in this window (peak {_percent(high)})"
    if value == mean:
        return "exactly on the historical mean"
    side = "below" if value < mean else "above"
    span = high - low
    if span == 0:
        return "inside a band with no width"
    position = (value - low) / span * Decimal(100)
    return (f"{side} the historical mean, "
            f"{position.quantize(Decimal('1'))}% of the way from trough to peak")


def render_sensitivity(
    record: Mapping[str, Any], *, entity_name: str | None = None,
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

    out: list[str] = []
    title = entity_name or record.get("company_ref") or "company"
    metric = record.get("impact_metric") or {}
    horizon = record.get("horizon") or []
    window = record.get("history_window") or {}
    selection = record.get("selection") or {}
    out.append(f"SENSITIVITY  {title}")
    out.append(f"{record.get('id')}  version {record.get('version')}  "
               f"({record.get('value_kind')})")
    out.append(f"model {record.get('model_version_ref')}")
    out.append(f"rule {record.get('selection_rule_ref')}   "
               f"ranked on {metric.get('label') or metric.get('result_ref')} "
               f"over {len(horizon)} quarters"
               + (f" to {horizon[-1]['end']}" if horizon else ""))
    out.append(f"history {window.get('first')} .. {window.get('last')} "
               f"({window.get('quarters')} quarters)   figures in millions")
    if selection.get("status") != "available":
        out.append(f"selection {selection.get('status')}: {selection.get('reason')}")
    out.append("")

    drivers = list(record.get("drivers") or [])
    for driver in drivers:
        band = driver.get("band") or {}
        impact = driver.get("impact") or {}
        swing = driver.get("swing") or {}
        ours = driver.get("ours") or {}
        out.append(f"#{driver.get('rank')}  {driver.get('label') or driver.get('driver_ref')}"
                   f"  [{driver.get('measure')}]")
        if band.get("status") == "available":
            out.append(
                f"      trough {_percent(band['trough']['value'])} "
                f"({band['trough']['period_end']})"
                f"   mean {_percent(band['mean']['value'])}"
                f"   peak {_percent(band['peak']['value'])} "
                f"({band['peak']['period_end']})"
                f"   latest {_percent(band['latest']['value'])} "
                f"({band['latest']['period_end']})")
            out.append(f"      over {band.get('count')} filed quarters "
                       f"{band.get('first_period')} .. {band.get('last_period')}")
            # The next observation in from each end, printed where a reader
            # looking at the extreme will see it. A peak far above its own
            # runner-up was one quarter and probably one event, and a scenario
            # run at it is a scenario about that event.
            for edge in ("trough", "peak"):
                runner = (band.get(edge) or {}).get("runner_up")
                if runner is not None:
                    out.append(
                        f"      next {edge} in: {_percent(runner['value'])} "
                        f"({runner['period_end']})")
        else:
            out.append(f"      band unavailable: {band.get('reason')}")
        if ours.get("value") is not None:
            out.append(f"      ours {_percent(ours['value'])}"
                       + (f" -- {_where(ours['value'], band)}"
                          if band.get("status") == "available" else ""))
        else:
            out.append("      ours: not one number across the horizon")
        if impact.get("status") == "computed":
            out.append(f"      one point on this assumption moves "
                       f"{metric.get('label')} by "
                       f"{_millions(impact['delta'])}m ({impact.get('percent_of_base')}%)")
        else:
            out.append(f"      elasticity unavailable: {impact.get('reason')}")
        if swing.get("status") == "computed":
            out.append(f"      across its own historical range "
                       f"{metric.get('label')} moves "
                       f"{_millions(swing['swing'])}m ({swing.get('percent_of_base')}%)"
                       "  <- this is what ranks it")
        else:
            out.append(f"      range swing unavailable: {swing.get('reason')}")

        rows = list(driver.get("what_if") or [])
        header = "".ljust(SENSITIVITY_LABEL_WIDTH) + "".join(
            str(row["scenario"]).rjust(SCENARIO_WIDTH) for row in rows)
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
                out.append(f"        {label}: {reason}")
        for row in rows:
            if row.get("status") != "computed":
                out.append(f"        {row['scenario']}: {row.get('reason')}")
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
                shown += f", and {len(named) - MAX_SHOWN_REFS} more filed cells"
            out.append(f"        {row['scenario']} from {shown}")
        out.append("")

    bridge = record.get("consensus_bridge") or {}
    out.append("CONSENSUS BRIDGE")
    out.append("-" * DRIVER_LABEL_WIDTH)
    if bridge.get("status") != "available":
        out.append(f"  unavailable: {bridge.get('reason')}")
    else:
        detail = {(str(item["metric"]), str(item["period"])): item
                  for item in (record.get("bridge_detail") or [])}
        out.append("  " + "metric".ljust(16) + "period".ljust(14)
                   + "ours".rjust(16) + "street".rjust(16) + "gap".rjust(16)
                   + "gap %".rjust(10))
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
            out.append(f"    {row['metric']} {row['period']}: {row.get('basis')}")

    out.append("")
    out.append("HOW TO READ IT")
    out.append("-" * DRIVER_LABEL_WIDTH)
    out.append("  the band is what the company has filed, not a range anyone chose;")
    out.append("  the drivers are ranked by how far the metric moves across that")
    out.append("  band, because every share assumption here is a share of the same")
    out.append("  revenue and a one-point move in each is the same number. Every")
    out.append("  column is the model recomputed in memory: none of them was ever")
    out.append("  published as a forecast, and our own column is the model itself.")
    out.append("")
    out.append("  EVERY COLUMN HOLDS ITS LEVEL FLAT ACROSS ALL THE QUARTERS SHOWN.")
    out.append("  None of them is a path. The trough column is not the trough")
    out.append("  quarter happening once -- it is the whole horizon spent there.")
    out.append("  Where an extreme sits far from the next observation, that peak")
    out.append("  or trough was one quarter and probably one event; the runner-up")
    out.append("  is printed beside it so you can see the gap and judge it.")
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
