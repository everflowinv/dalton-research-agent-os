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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--company-ref", help="default: every company with a specification")
    parser.add_argument("--json", action="store_true", help="the table itself, not the view")
    args = parser.parse_args(argv)

    from .coverage_mission import CoverageMissionAuthority
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


__all__ = ["main", "render_model_inputs"]
