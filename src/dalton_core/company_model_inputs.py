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

from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping, Sequence

from .company_model_series import quarterly_series, series_gaps
from .store import content_hash
from .driver_template import COST_REGISTRY_HASH, COST_REGISTRY_REF, cost_slot_ids

FILED = "filed"
SHARED = "share_of_filed"
ESTIMATED = "estimated"
NOT_FOUND = "concept_not_found"
AMBIGUOUS = "concept_in_several_statements"
INCOMPLETE = "incomplete_quarter_series"

# Closed, role-specific candidates. These are SEC concepts whose semantics are
# stable enough to enter arithmetic without a model guessing from a label.
CASH_FLOW_ROLE_CONCEPTS: Mapping[str, tuple[str, ...]] = {
    "operating_cash_flow": (
        "us-gaap:NetCashProvidedByUsedInOperatingActivities",
        "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    # Ordered: when a filer tags more than one, the narrowest wins. Amazon
    # files its capital expenditure only as ``PaymentsToAcquireProductiveAssets``
    # ("Purchases of property and equipment"), so with the first concept alone
    # its free cash flow had no capex and the specification was refused.
    "capital_expenditure": (
        "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
        "us-gaap:PaymentsToAcquireProductiveAssets",
    ),
}
# Roles whose candidates are a preference order rather than a set: several
# qualifying is not ambiguity, it is the same outflow tagged at two widths.
PREFERENCE_ORDERED_CASH_ROLES = frozenset({"capital_expenditure"})
# The concepts that *are* consolidated net income. The XBRL fact is one fact
# whichever statement a parser files it under: edgartools puts Amazon's
# ``NetIncomeLoss`` only on the cash-flow statement, where it opens the
# operating section, and the income statement then looks as if it had no
# bottom line. Only these exact concepts are carried across -- never a label.
NET_INCOME_CONCEPTS: tuple[str, ...] = (
    "us-gaap:NetIncomeLoss",
    "us-gaap:ProfitLoss",
)

# The specification's horizon says how many quarters the model rests on; this
# bounds one table regardless, so a specification asking for twenty years
# cannot produce a table nobody can read.
MAX_PERIODS = 24
MIN_CASH_FLOW_QUARTERS = 4
STATEMENT_IDENTITY_RULE_REF = "filed-input:declared-statement-source-identity:0.1"


class ModelInputError(ValueError):
    """The specification cannot be joined to this company's filings."""


def _model_unit(value: Any) -> str:
    """Normalize the SEC currency-per-share spelling used by model formulas."""

    unit = str(value)
    match = re.fullmatch(r"([A-Za-z]{3})PerShare", unit)
    return f"{match.group(1).lower()}_per_share" if match else unit


def cash_quarter_windows_are_unique(quarters: Sequence[Mapping[str, Any]]) -> bool:
    """Whether each held cash quarter has one non-overlapping fiscal window."""

    windows = sorted(
        (str(item.get("period_start") or ""), str(item.get("period_end") or ""))
        for item in quarters
    )
    if any(not start or not end or start > end for start, end in windows):
        return False
    if len({end for _start, end in windows}) != len(windows):
        return False
    return all(
        current_start > previous_end
        for (_previous_start, previous_end), (current_start, _current_end)
        in zip(windows, windows[1:])
    )


def _rows_of(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    anchor = spec.get("revenue_anchor_concept")
    driver_concepts = {
        str(item.get("basis_concept"))
        for item in (spec.get("revenue_drivers") or [])
        if item.get("basis_concept")
    }
    if anchor and str(anchor) not in driver_concepts:
        rows.append({
            "kind": "revenue_anchor",
            "ref": "filed-revenue-anchor",
            "label": "Filed revenue calculation anchor",
            "basis_concept": str(anchor),
            "unit": "USD",
            "because": "Bound separately from the economic revenue drivers.",
        })
    for item in spec.get("revenue_drivers") or []:
        rows.append({
            "kind": "revenue_driver", "ref": item["ref"], "label": item["label"],
            "basis_concept": item.get("basis_concept"),
            "driver_kind": item.get("kind"), "unit": item.get("unit"),
            "because": item.get("because"),
            **({k:item[k] for k in ("basis_operating_metric_ref","basis_operating_context_hash","basis_operating_context_evidence_refs")} if "basis_operating_metric_ref" in item else {}),
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


def _statement_structure_concepts(spec: Mapping[str, Any]) -> set[str]:
    """Exact filed/tie concepts the persisted structure needs for replay."""

    structure = spec.get("financial_statement_structure")
    if structure is None:
        return set()
    if not isinstance(structure, Mapping):
        raise ModelInputError("financial statement structure is not an object")
    lines = structure.get("lines")
    formulas = structure.get("formulas")
    if not isinstance(lines, list) or not isinstance(formulas, list):
        raise ModelInputError("financial statement structure has invalid lines or formulas")
    concepts: set[str] = set()
    for line in lines:
        if not isinstance(line, Mapping):
            raise ModelInputError("financial statement structure line is not an object")
        concept = line.get("concept")
        if line.get("kind") == "filed":
            if not isinstance(concept, str) or not concept:
                raise ModelInputError("filed statement structure line has no concept")
            concepts.add(concept)
        elif concept is not None:
            raise ModelInputError("derived statement structure line claims a filed concept")
    for formula in formulas:
        if not isinstance(formula, Mapping):
            raise ModelInputError("financial statement structure formula is not an object")
        tie = formula.get("tie_out_concept")
        if tie is not None:
            if not isinstance(tie, str) or not tie:
                raise ModelInputError("statement structure tie-out concept is invalid")
            concepts.add(tie)
    if not concepts:
        raise ModelInputError("financial statement structure has no filed authority")
    return concepts


def _declared_statement_hints(spec: Mapping[str, Any]) -> dict[str, str]:
    """Only an exact, unambiguous statement already declared by the spec.

    A financial concept may be separately disclosed on income and cash. Do
    not choose by its label, value, source age or financial role. Conflicting
    declarations retain the old ambiguity rather than silently select a table.
    """
    structure = spec.get("financial_statement_structure")
    if not isinstance(structure, Mapping):
        return {}
    by_ref = {line.get("ref"): line for line in structure.get("lines") or []
              if isinstance(line, Mapping)}
    declarations: dict[str, set[str]] = {}
    for line in by_ref.values():
        concept, statement = line.get("concept"), line.get("statement")
        if line.get("kind") == "filed" and concept and statement in {"income", "cash", "balance"}:
            declarations.setdefault(str(concept), set()).add(statement)
    for formula in structure.get("formulas") or []:
        if not isinstance(formula, Mapping):
            continue
        concept = formula.get("tie_out_concept")
        output = by_ref.get(formula.get("output_ref")) or {}
        statement = output.get("statement")
        if concept and statement in {"income", "cash", "balance"}:
            declarations.setdefault(str(concept), set()).add(statement)
    return {concept: next(iter(statements)) for concept, statements in declarations.items()
            if len(statements) == 1}


def _series_for(
    missions: Any, company_ref: str, concept: str, *, legacy_replay: bool = False,
    non_additive_duration_policy: bool = True,
    ifrs_non_additive_duration_policy: bool = False,
    statement_hint: str | None = None,
) -> dict[str, Any]:
    """The filed series for one concept, and which statement it came from.

    Legacy callers retain cross-statement ambiguity. A new calculation may
    use the exact unique statement declared by its already verified structure;
    every original line/context/value/unit binding and series gate still runs.
    """

    lines = missions.statement_series_lines(company_ref, concept)
    if not lines:
        return {"status": NOT_FOUND, "statement": None, "series": None}
    statements = sorted({str(row["statement"]) for row in lines})
    statement_scope = None
    if len(statements) > 1:
        if statement_hint not in statements:
            return {"status": AMBIGUOUS, "statement": statements, "series": None}
        statement_scope = {"rule_ref": STATEMENT_IDENTITY_RULE_REF,
                           "statement": statement_hint, "observed_statements": statements}
        lines = [line for line in lines if line["statement"] == statement_hint]
        statements = [statement_hint]
    # The label must come from a row that is not a breakdown. Taking the last
    # row of any kind put "EMEA" on Accenture's total revenue -- the figures
    # were the totals and the name was a geography, which is worse than an
    # unlabelled row because a reader has no reason to doubt it.
    reported = [row for row in lines
                if not row.get("is_breakdown") and not row.get("dimension_axis")]
    unit_for_model = str if legacy_replay else _model_unit
    source_units = sorted({unit_for_model(row.get("unit"))
                           for row in reported if row.get("unit")})
    series = quarterly_series(
        lines, legacy_replay=legacy_replay,
        non_additive_duration_policy=non_additive_duration_policy,
        ifrs_non_additive_duration_policy=ifrs_non_additive_duration_policy,
    )
    if not legacy_replay:
        for kind in ("quarters", "instants", "durations"):
            for cell in series.get(kind) or []:
                if cell.get("unit") is not None:
                    cell["unit"] = _model_unit(cell["unit"])
    source_evidence = None
    if not legacy_replay and any("dimensions" in row or row.get("statement_context_ref") for row in reported):
        fields = ("line_id", "ingest_id", "ordinal", "statement", "concept", "period_start", "period_end",
                  "value", "unit", "accession", "filed", "dimensions", "statement_context_ref", "source_unit_ref", "unit_definition_hash")
        evidence_rows = [{key: row[key] for key in fields if key in row} for row in reported]
        source_evidence = {"rule_ref": "filed-input-evidence:exact-context-lines:1", "rows": evidence_rows,
                           "content_hash": content_hash(evidence_rows)}
    return {
        **({"source_evidence": source_evidence} if source_evidence is not None else {}),
        **({"statement_scope": statement_scope} if statement_scope is not None else {}),
        "status": FILED, "statement": statements[0],
        "label": (reported[-1]["label"] if reported else concept),
        "series": series,
        "source_units": source_units,
    }


def _cash_flow_inputs(
    missions: Any, company_ref: str, spec: Mapping[str, Any], *,
    legacy_replay: bool = False,
) -> list[dict[str, Any]]:
    """Select exact filed cash-flow concepts, or retain a typed reason not to."""

    definition = spec.get("cash_flow_companion")
    selected_by_role = None
    if spec.get("schema_version") == "0.4" and isinstance(definition, Mapping):
        raw_lines = definition.get("lines")
        if not isinstance(raw_lines, list):
            raise ModelInputError("cash-flow companion has no selected lines")
        selected_by_role = {
            str(item.get("role")): item.get("concept")
            for item in raw_lines if isinstance(item, Mapping)
        }
        if set(selected_by_role) != set(CASH_FLOW_ROLE_CONCEPTS):
            raise ModelInputError("cash-flow companion selected roles differ")
        for role, selected_concept in selected_by_role.items():
            conflicting_roles = [
                other_role for other_role, known_concepts in
                CASH_FLOW_ROLE_CONCEPTS.items()
                if other_role != role and selected_concept in known_concepts
            ]
            if conflicting_roles:
                raise ModelInputError(
                    f"cash-flow companion {role} uses a concept with known "
                    f"{conflicting_roles[0]} semantics")
    selected: list[dict[str, Any]] = []
    for role, concepts in CASH_FLOW_ROLE_CONCEPTS.items():
        if selected_by_role is not None:
            selected_concept = selected_by_role[role]
            if selected_concept is None:
                selected.append({
                    "role": role, "status": NOT_FOUND,
                    "reason": f"the company specification marks {role} unavailable",
                })
                continue
            if not isinstance(selected_concept, str) or not selected_concept:
                raise ModelInputError(f"cash-flow companion {role} concept is invalid")
            concepts = (selected_concept,)
        candidates: list[tuple[str, dict[str, Any]]] = []
        rejected: list[str] = []
        for concept in concepts:
            entry = _series_for(
                missions, company_ref, concept, legacy_replay=legacy_replay,
            )
            series = entry.get("series") or {}
            quarters = list(series.get("quarters") or [])
            units = set(entry.get("source_units") or [])
            windows_valid = (
                True if legacy_replay else cash_quarter_windows_are_unique(quarters)
            )
            sign_valid = True
            if role == "capital_expenditure":
                try:
                    sign_valid = all(Decimal(str(item["value"])) >= 0 for item in quarters)
                except (InvalidOperation, ValueError):
                    sign_valid = False
            if entry.get("status") == FILED and entry.get("statement") == "cash" \
                    and quarters and len(units) == 1 and sign_valid and windows_valid:
                candidates.append((concept, entry))
            elif entry.get("status") != NOT_FOUND:
                rejected.append(
                    f"{concept} has duplicate or overlapping cash quarter windows"
                    if not windows_valid else
                    f"{concept} is not a dimension-free, single-unit cash statement "
                    "series with the filed outflow sign convention"
                )
        if len(candidates) > 1 and role in PREFERENCE_ORDERED_CASH_ROLES:
            candidates = candidates[:1]
        if len(candidates) != 1:
            reason = (
                f"{len(candidates)} frozen filed concepts qualify for {role}"
                if candidates else
                ("; ".join(rejected) or f"no frozen filed concept qualifies for {role}")
            )
            selected.append({"role": role, "status": AMBIGUOUS if candidates else NOT_FOUND,
                             "reason": reason})
            continue
        concept, entry = candidates[0]
        series = entry["series"]
        gaps = series_gaps(series)
        status = (
            FILED if not gaps and len(series["quarters"]) >= MIN_CASH_FLOW_QUARTERS
            else INCOMPLETE
        )
        item = {
            "role": role, "status": status, "concept": concept,
            "statement": "cash", "unit": series["quarters"][0]["unit"],
            "series": series, "gaps": gaps,
        }
        if status == INCOMPLETE:
            item["reason"] = (
                "filed cumulative series does not provide four consecutive quarters"
            )
        selected.append(item)
    return selected


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


class _StatementInputRead:
    """Reuse exact strong statement reads only inside one input-table build.

    The authority still selects and validates each filing/context. No cache is
    retained by the authority, shared across companies, or used on a later tick.
    Existing connection/transaction ownership is unchanged.
    """

    def __init__(self, source: Any, series_reader: Any):
        self._source = source
        self._series_reader = series_reader
        self._filings: dict[Any, list[dict[str, Any]]] = {}
        self._lines: dict[Any, list[dict[str, Any]]] = {}

    def statement_filings(self, company_ref: str | None = None) -> list[dict[str, Any]]:
        if company_ref not in self._filings:
            self._filings[company_ref] = self._source.statement_filings(company_ref)
        return self._filings[company_ref]

    def statement_lines(self, ingest_id: str, *, statement: str | None = None,
                        context_version_ref: str | None = None,
                        latest_context: bool = True) -> list[dict[str, Any]]:
        key = (ingest_id, context_version_ref, latest_context)
        if key not in self._lines:
            self._lines[key] = self._source.statement_lines(
                ingest_id, context_version_ref=context_version_ref,
                latest_context=latest_context,
            )
        rows = self._lines[key]
        if statement is not None:
            from .coverage_mission import _vocabulary

            wanted = _vocabulary(statement, ("income", "balance", "cash"), "statement")
            return [row for row in rows if row["statement"] == wanted]
        return rows

    def statement_series_lines(self, company_ref: str, concept: str, *,
                               statement: str | None = None) -> list[dict[str, Any]]:
        return self._series_reader(self, company_ref, concept, statement=statement)


def _input_statement_reader(missions: Any) -> Any:
    # Custom readers retain their own semantics. Only the real authority's
    # known series projection is routed through this request-scoped adapter.
    from .coverage_mission import CoverageMissionAuthority

    reader = getattr(getattr(missions, "statement_series_lines", None), "__func__", None)
    if reader is not CoverageMissionAuthority.statement_series_lines:
        return missions
    return _StatementInputRead(missions, reader)


def build_model_inputs(
    missions: Any, spec: Mapping[str, Any], *, max_periods: int = MAX_PERIODS,
    operating_driver_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One company's model rows, its filed history, and what still has to be estimated."""

    source_authority = missions
    missions = _input_statement_reader(missions)
    company_ref = spec.get("company_ref")
    if not isinstance(company_ref, str) or not company_ref:
        raise ModelInputError("specification carries no company_ref")
    horizon = spec.get("horizon") or {}
    wanted = horizon.get("historical_quarters")
    if isinstance(wanted, bool) or not isinstance(wanted, int) or wanted < 1:
        wanted = max_periods
    limit = max(1, min(int(wanted), int(max_periods)))

    rows = _rows_of(spec)
    cost_rows = [row for row in rows if "cost_driver_slot" in row]
    cost_template = spec.get("cost_driver_template")
    if cost_rows:
        if not isinstance(cost_template, Mapping):
            raise ModelInputError("cost-bound specification carries no cost template metadata")
        if (cost_template.get("registry_ref") != COST_REGISTRY_REF
                or cost_template.get("registry_hash") != COST_REGISTRY_HASH):
            raise ModelInputError("cost-bound specification carries stale cost template metadata")
        classification = cost_template.get("classification")
        allowed = set(cost_slot_ids(classification))
        for row in cost_rows:
            slot = row.get("cost_driver_slot")
            if slot is not None and slot not in allowed:
                raise ModelInputError(
                    f"expense row {row['ref']} binds {slot!r} outside its cost template")
            if slot is None and not row.get("cost_driver_unbound_reason"):
                raise ModelInputError(
                    f"expense row {row['ref']} is unbound without a reason")
    elif cost_template is not None:
        raise ModelInputError("cost template metadata has no classified expense rows")
    concepts: dict[str, list[str]] = {}
    for row in rows:
        concept = row.get("basis_concept")
        if concept:
            concepts.setdefault(str(concept), []).append(row["ref"])
    # The spec's economic rows are only a subset of the filed arithmetic. A
    # company-specific net-income/EPS bridge also needs non-operating, tax,
    # attribution, share and filed subtotal concepts even when none is a
    # revenue driver or expense row. Keep them as source authority lines; do
    # not manufacture economic model rows for them.
    statement_structure_bound = spec.get("financial_statement_structure") is not None
    legacy_series = (
        not statement_structure_bound
        and spec.get("schema_version") in {None, "0.1", "0.2"}
    )
    statement_structure_concepts = _statement_structure_concepts(spec)
    structure_definition = spec.get("financial_statement_structure")
    non_additive_duration_policy = (
        isinstance(structure_definition, Mapping)
        and structure_definition.get("schema_version") in {"0.4", "0.5", "0.6", "0.7"}
    )
    for concept in statement_structure_concepts:
        concepts.setdefault(concept, [])
    for component in (spec.get("fcf_definition") or {}).get("components") or []:
        if isinstance(component, Mapping) and component.get("concept"):
            concepts.setdefault(str(component["concept"]), [])
    # Forecast coverage (structure 0.6) may add the diluted share count, a
    # derived diluted EPS and the first-version cash-flow and balance-sheet
    # leaves.  Their exact concepts come from a closed table and enter the
    # table only when filed; an unfiled one is simply not offered.
    coverage_candidates: set[str] = set()
    if isinstance(structure_definition, Mapping):
        from .forecast_coverage import UPGRADEABLE_STRUCTURE_VERSIONS, coverage_concepts

        if structure_definition.get("schema_version") in UPGRADEABLE_STRUCTURE_VERSIONS:
            coverage_candidates = set(coverage_concepts()) - set(concepts)

    statement_hints = _declared_statement_hints(spec)
    filed: dict[str, dict[str, Any]] = {}
    for concept in sorted(concepts):
        filed[concept] = _series_for(
            missions, company_ref, concept,
            legacy_replay=legacy_series,
            # The inner structure is the persisted numeric replay contract.
            # Outer company specs already reached 0.4 while their structures
            # were still 0.3, so only inner 0.4 and later opt into this correction.
            non_additive_duration_policy=non_additive_duration_policy,
            ifrs_non_additive_duration_policy=(isinstance(structure_definition, Mapping) and structure_definition.get("schema_version") == "0.7"),
            statement_hint=statement_hints.get(concept),
        )
    for concept in sorted(coverage_candidates):
        entry = _series_for(
            missions, company_ref, concept, legacy_replay=legacy_series,
            non_additive_duration_policy=non_additive_duration_policy,
            ifrs_non_additive_duration_policy=(isinstance(structure_definition, Mapping) and structure_definition.get("schema_version") == "0.7"),
        )
        if entry["status"] == FILED and entry.get("series"):
            filed[concept] = entry
            concepts[concept] = []

    cash_required = any(
        item.get("statement") == "cash" and item.get("importance") != "not_material"
        for item in (spec.get("forecast_statements") or [])
    )
    cash_flow_inputs = (
        _cash_flow_inputs(
            missions, company_ref, spec,
            legacy_replay=legacy_series,
        ) if cash_required else []
    )

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
    for item in cash_flow_inputs:
        if item.get("status") == FILED:
            ends.update(str(cell["period_end"]) for cell in item["series"]["quarters"])
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
        if entry.get("statement_scope") is not None:
            line["statement_scope"] = entry["statement_scope"]
        if statement_structure_bound:
            line.update({"duration_facts": [], "ambiguous_periods": []})
            if entry.get("source_evidence") is not None:
                line["source_evidence"] = entry["source_evidence"]
                line["source_quarter_ends"] = sorted({str(cell["period_end"]) for cell in (entry.get("series") or {}).get("quarters", [])})
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
            line["period_basis"] = "duration" if (
                quarters or (statement_structure_bound and series.get("durations"))
            ) else (
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
            if statement_structure_bound:
                line["duration_facts"] = list(series.get("durations") or [])
                line["ambiguous_periods"] = list(series.get("ambiguous_periods") or [])
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

    result = {
        "schema_version": "0.3" if statement_structure_bound else "0.2",
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
        "cash_flow_inputs": cash_flow_inputs,
        "readiness": {
            **readiness(rows, filed_lines, periods),
            "filed_income_lines_no_row_uses": _unused_income_lines(
                missions, company_ref, set(concepts)),
        },
    }
    operating_rows = [r for r in rows if r.get('basis_operating_metric_ref')]
    if operating_rows:
        from .operating_driver_context import OperatingDriverReadContext, metric_history
        from pathlib import Path
        if operating_driver_context is None:
            database = source_authority.connection.execute('PRAGMA database_list').fetchall()
            core_path = next(r[2] for r in database if r[1] == 'main')
            with OperatingDriverReadContext(store=source_authority.store, state_dir=Path(core_path).parent) as reader:
                bindings={r['ref']:r for row in operating_rows for r in row['basis_operating_context_evidence_refs']}
                operating_driver_context = reader.projection(company_ref,evidence_bindings=list(bindings.values()))
        lines = []
        for row in operating_rows:
            from .operating_driver_context import selected_context
            selected=selected_context(operating_driver_context,row['basis_operating_context_evidence_refs'],row['basis_operating_context_hash'])
            series = metric_history(selected, company_ref=company_ref, metric_ref=row['basis_operating_metric_ref'], expected_hash=row['basis_operating_context_hash'])
            if series['unit'] != row['unit']:
                raise ModelInputError('operating model unit differs from source history')
            row.update(status='verified_reported', reason='Independent reported operating source; not a filed statement line.')
            lines.append({**series,'driver_ref':row['ref'],'status':'verified_reported'})
        result['operating_driver_lines'] = lines
        result['readiness'].update(readiness(rows, filed_lines, periods))
    if any(line.get("statement_scope") for line in filed_lines):
        result["statement_identity_rule_ref"] = STATEMENT_IDENTITY_RULE_REF
    if cost_template is not None:
        result["cost_driver_template"] = dict(cost_template)
    identities=identity_input_lines(missions,spec)
    if identities: result["filed_identity_lines"]=identities
    from .filed_period_composition import collect
    composition = collect(source_authority, spec, result)
    if composition is not None:
        result["historical_income_composition"] = composition
    return result


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
    "CASH_FLOW_ROLE_CONCEPTS",
    "NET_INCOME_CONCEPTS",
    "PREFERENCE_ORDERED_CASH_ROLES",
    "cash_quarter_windows_are_unique",
    "ESTIMATED",
    "FILED",
    "INCOMPLETE",
    "MIN_CASH_FLOW_QUARTERS",
    "MAX_PERIODS",
    "NOT_FOUND",
    "SHARED",
    "ModelInputError",
    "build_model_inputs",
    "readiness",
]


def validate_statement_identity(value):
    if not isinstance(value,Mapping) or set(value)!={"statement","dimensions"} or value["statement"] not in {"income","balance","cash"}:
        raise ModelInputError("statement identity needs exact statement and dimensions")
    dims=value["dimensions"]
    if not isinstance(dims,Mapping) or not dims or not all(isinstance(k,str) and k and isinstance(v,str) and v for k,v in dims.items()):
        raise ModelInputError("statement identity needs full named axis/member dimensions")
    return {"statement":value["statement"],"dimensions":dict(sorted(dims.items()))}


def identity_input_lines(missions,spec):
    """Independent dimension-specific series; never reuse the consolidated selector."""
    result=[]
    period_originals = {}
    for driver in spec.get("revenue_drivers") or []:
        if driver.get("basis_identity") is None:continue
        identity=validate_statement_identity(driver["basis_identity"]);concept=driver.get("basis_concept")
        if not concept:continue
        period_proof = driver.get("basis_period_family_bindings")
        if period_proof is None:
            rows=[r for r in missions.statement_series_lines(spec["company_ref"],concept,statement=identity["statement"])
                if r.get("dimensions")==identity["dimensions"]]
        else:
            from .period_source_family import verify_driver_bindings, identity_for_row
            executed = {"statement_identity": identity, "statement_period_family_bindings": period_proof}
            try:
                identity_for_row(executed, {})
            except ValueError as exc:
                raise ModelInputError(str(exc)) from exc
            filings = missions.statement_filings(spec["company_ref"])
            scopes = period_proof["source_read_scopes"]
            required = {s["ingest_id"]: s["context_refs"] for s in scopes}
            if len(required) != len(scopes):
                raise ModelInputError("period-family original read scope repeats a filing")
            filing_map = {f["ingest_id"]: f for f in filings}
            if not set(required).issubset(filing_map):
                raise ModelInputError("period-family original filing authority is missing")
            held = [filing_map[s["ingest_id"]] for s in scopes]
            originals = []
            for filing in held:
                ingest = filing["ingest_id"]
                for context_ref in required[ingest]:
                    key = (ingest, context_ref)
                    if key not in period_originals:
                        period_originals[key] = missions.statement_lines(ingest,
                            context_version_ref=context_ref, latest_context=False)
                    originals.extend(period_originals[key])
            try:
                verify_driver_bindings(period_proof, driver=driver, company_ref=spec["company_ref"],
                                       filings=held, rows=originals)
            except ValueError as exc:
                raise ModelInputError(str(exc)) from exc
            filing_by_ingest = {f["ingest_id"]: f for f in held}
            rows=[]
            for row in originals:
                selected = identity_for_row(executed, row)
                if (row.get("concept") != concept or selected is None
                        or row.get("statement") != selected["statement"]
                        or row.get("dimensions") != selected["dimensions"]):
                    continue
                filing=filing_by_ingest[row["ingest_id"]]
                rows.append({**row,"accession":filing["accession"],"filed":filing["filed"],
                    "report_date":filing["report_date"],"filing_form":filing["form"]})
        evidence_fields=("line_id","ingest_id","ordinal","statement","concept","period_start","period_end","value","unit","accession","filed","dimensions","statement_context_ref")
        evidence=[{k:r[k] for k in evidence_fields if k in r} for r in rows]
        # Once exact identity is selected, the quarterly arithmetic has no mix of segments.
        series=quarterly_series([{**r,"is_breakdown":False,"dimension_axis":None,"dimension_member":None} for r in rows])
        units={_model_unit(c.get("unit")) for c in series["quarters"] if c.get("unit")}
        cells={c["period_end"]:{**c,"unit":_model_unit(c["unit"])} for c in series["quarters"]}
        result.append({"driver_ref":driver["ref"],"concept":concept,"statement":identity["statement"],"statement_identity":identity,
            **({"statement_period_family_bindings":period_proof} if period_proof is not None else {}),
            "label":driver["label"],"status":FILED if cells and len(units)==1 else NOT_FOUND,"cells":cells,
            "source_evidence":{"rule_ref":("filed-input-evidence:period-source-family:1" if period_proof is not None
                else "filed-input-evidence:exact-dimensional-context:1"),"rows":evidence,"content_hash":content_hash(evidence)},
            "gaps":series.get("ambiguous_periods") or []})
    return result
