"""Company-specific financial statement lines and replayable formula topology.

This is the authority boundary between filed presentation and forecast math.
It does not choose a structure.  It verifies that a proposed structure names
lines this company actually filed, cites held statement or note evidence, and
replays its equations against historical periods before a forecast may use it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from .store import content_hash


SCHEMA_VERSION = "0.1"
STRUCTURE_AUTHORITY_REF = "company-financial-statement-structure:0.1"

STATEMENTS = ("income", "balance", "cash")
PERIOD_KINDS = ("duration", "instant")
LINE_KINDS = ("filed", "derived")
ANNUAL_SEMANTICS = (
    "sum_quarters", "direct_annual", "annual_ratio", "not_applicable",
)
ROLES = (
    "revenue", "cost_of_revenue", "gross_profit", "operating_expense",
    "operating_income", "nonoperating_income_expense", "interest_income",
    "interest_expense", "pretax_income", "income_tax_expense",
    "income_from_continuing_operations", "discontinued_operations",
    "net_income", "noncontrolling_interest", "parent_net_income",
    "diluted_weighted_average_shares", "diluted_eps", "other",
)
FORMULA_OPERATORS = ("sum", "divide")
_LINE_FIELDS = {
    "ref", "role", "label", "kind", "concept", "statement", "unit",
    "period_kind", "annual_semantics",
}
_SUM_FIELDS = {"output_ref", "operator", "terms", "tie_out_concept", "evidence_refs"}
_DIVIDE_FIELDS = {
    "output_ref", "operator", "numerator_ref", "denominator_ref",
    "tie_out_concept", "evidence_refs",
}
_TERM_FIELDS = {"line_ref", "coefficient"}
_NOTE_FIELDS = {"ref", "content_hash", "source_content_hash"}


class FinancialStatementStructureError(ValueError):
    pass


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise FinancialStatementStructureError(f"{name} has an invalid closed shape")
    return dict(value)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FinancialStatementStructureError(f"{name} must be non-empty text")
    return value.strip()


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise FinancialStatementStructureError(f"{name} must be a decimal string")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise FinancialStatementStructureError(f"{name} must be a decimal string") from exc
    if not result.is_finite():
        raise FinancialStatementStructureError(f"{name} must be finite")
    return result


def financial_input_authority(value: Mapping[str, Any]) -> dict[str, Any]:
    """Stable financial-only projection, excluding unrelated company state."""

    if not isinstance(value, Mapping):
        raise FinancialStatementStructureError("financial inputs must be an object")
    company_ref = _text(value.get("company_ref"), "financial inputs company_ref")
    lines = value.get("filed_lines")
    if not isinstance(lines, list):
        raise FinancialStatementStructureError("financial inputs filed_lines must be a list")
    projection = {
        "schema_version": "financial-input-authority-0.1",
        "company_ref": company_ref,
        "periods": list(value.get("periods") or []),
        "filed_lines": lines,
        "cash_flow_inputs": list(value.get("cash_flow_inputs") or []),
    }
    return {**projection, "content_hash": content_hash(projection)}


def _filed_index(inputs: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for raw in inputs.get("filed_lines") or []:
        if not isinstance(raw, Mapping):
            raise FinancialStatementStructureError("financial input line must be an object")
        concept = _text(raw.get("concept"), "financial input concept")
        if concept in indexed:
            raise FinancialStatementStructureError(f"financial input concept {concept} is duplicated")
        indexed[concept] = dict(raw)
    return indexed


def _units(line: Mapping[str, Any]) -> set[str]:
    return {
        str(cell.get("unit")).casefold()
        for cell in (line.get("cells") or {}).values()
        if isinstance(cell, Mapping) and cell.get("unit")
    }


def _statement_refs(inputs: Mapping[str, Any]) -> set[str]:
    return {
        str(ref)
        for line in inputs.get("filed_lines") or []
        if isinstance(line, Mapping)
        for cell in (line.get("cells") or {}).values()
        if isinstance(cell, Mapping)
        for ref in (cell.get("source_accessions") or [])
        if isinstance(ref, str) and ref
    }


def _note_refs(note_evidence: Sequence[Mapping[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for index, raw in enumerate(note_evidence):
        wire = _closed(raw, _NOTE_FIELDS, f"note_evidence[{index}]")
        ref = _text(wire["ref"], f"note_evidence[{index}].ref")
        for field in ("content_hash", "source_content_hash"):
            digest = _text(wire[field], f"note_evidence[{index}].{field}")
            if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise FinancialStatementStructureError(
                    f"note_evidence[{index}].{field} must be a lowercase SHA-256"
                )
        if ref in refs:
            raise FinancialStatementStructureError("note evidence ref is duplicated")
        refs.add(ref)
    return refs


def _normalized_note_evidence(
    note_evidence: Sequence[Mapping[str, Any]],
    resolver: Callable[[str], Mapping[str, Any] | None] | None,
) -> list[dict[str, Any]]:
    # _note_refs performs the closed/hash validation. Keep the complete proof
    # in the normalized authority so a note ref cannot later resolve to new bytes.
    refs = _note_refs(note_evidence)
    if refs and resolver is None:
        raise FinancialStatementStructureError(
            "note evidence requires an authoritative resolver"
        )
    normalized = sorted((dict(item) for item in note_evidence), key=lambda item: item["ref"])
    for item in normalized:
        held = None if resolver is None else resolver(item["ref"])
        if not isinstance(held, Mapping) or dict(held) != item:
            raise FinancialStatementStructureError("note evidence authority differs")
    return normalized


def _spec_authority(spec: Mapping[str, Any]) -> dict[str, str]:
    company_ref = _text(spec.get("company_ref"), "company spec company_ref")
    spec_ref = _text(spec.get("spec_id"), "company spec spec_id")
    digest = _text(spec.get("content_hash"), "company spec content_hash")
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise FinancialStatementStructureError(
            "company spec content_hash must be a lowercase SHA-256"
        )
    return {"company_ref": company_ref, "spec_ref": spec_ref, "spec_hash": digest}


def _validate_spec_alignment(
    spec: Mapping[str, Any], lines: Sequence[Mapping[str, Any]],
) -> None:
    """Keep the extension attached to concepts selected by the current spec."""

    revenue = [line for line in lines if line["role"] == "revenue"]
    if (
        len(revenue) != 1
        or revenue[0]["kind"] != "filed"
        or revenue[0]["concept"] != spec.get("revenue_anchor_concept")
    ):
        raise FinancialStatementStructureError(
            "structure revenue must be the company spec's exact filed anchor"
        )
    held_concepts = {
        line["concept"] for line in lines if line["kind"] == "filed"
    }
    selected_expenses = {
        item.get("basis_concept")
        for item in (spec.get("expense_lines") or [])
        if isinstance(item, Mapping) and item.get("basis_concept")
    }
    missing = sorted(selected_expenses - held_concepts)
    if missing:
        raise FinancialStatementStructureError(
            "structure omits expense concepts selected by the company spec: "
            + ", ".join(str(item) for item in missing)
        )


def _normalize_line(
    raw: Any, index: int, filed: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    wire = _closed(raw, _LINE_FIELDS, f"lines[{index}]")
    line = {
        "ref": _text(wire["ref"], f"lines[{index}].ref"),
        "role": _text(wire["role"], f"lines[{index}].role"),
        "label": _text(wire["label"], f"lines[{index}].label"),
        "kind": _text(wire["kind"], f"lines[{index}].kind"),
        "concept": wire["concept"],
        "statement": _text(wire["statement"], f"lines[{index}].statement"),
        "unit": _text(wire["unit"], f"lines[{index}].unit").casefold(),
        "period_kind": _text(wire["period_kind"], f"lines[{index}].period_kind"),
        "annual_semantics": _text(
            wire["annual_semantics"], f"lines[{index}].annual_semantics"
        ),
    }
    for field, allowed in (
        ("role", ROLES), ("kind", LINE_KINDS), ("statement", STATEMENTS),
        ("period_kind", PERIOD_KINDS), ("annual_semantics", ANNUAL_SEMANTICS),
    ):
        if line[field] not in allowed:
            raise FinancialStatementStructureError(f"lines[{index}].{field} is invalid")
    concept = line["concept"]
    if line["kind"] == "derived":
        if concept is not None:
            raise FinancialStatementStructureError("a derived line cannot claim a filed concept")
    else:
        concept = _text(concept, f"lines[{index}].concept")
        source = filed.get(concept)
        if source is None or source.get("status") != "filed":
            raise FinancialStatementStructureError(
                f"lines[{index}].concept is not a filed line in this input authority"
            )
        if source.get("statement") != line["statement"]:
            raise FinancialStatementStructureError("filed line statement differs from authority")
        if source.get("period_basis") != line["period_kind"]:
            raise FinancialStatementStructureError("filed line period kind differs from authority")
        units = _units(source)
        if units != {line["unit"]}:
            raise FinancialStatementStructureError("filed line unit differs or is ambiguous")
        line["concept"] = concept
    if line["role"] == "diluted_weighted_average_shares":
        if line["annual_semantics"] != "direct_annual" or line["unit"] != "shares":
            raise FinancialStatementStructureError(
                "diluted weighted-average shares require direct_annual shares"
            )
    if line["role"] == "diluted_eps" and line["annual_semantics"] not in {
        "direct_annual", "annual_ratio",
    }:
        raise FinancialStatementStructureError(
            "diluted EPS cannot be aggregated from quarterly EPS"
        )
    return line


def _normalize_formula(
    raw: Any, index: int, lines: Mapping[str, Mapping[str, Any]],
    filed: Mapping[str, Mapping[str, Any]], allowed_evidence: set[str],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise FinancialStatementStructureError(f"formulas[{index}] must be an object")
    operator = raw.get("operator")
    fields = _SUM_FIELDS if operator == "sum" else _DIVIDE_FIELDS
    wire = _closed(raw, fields, f"formulas[{index}]")
    output = _text(wire["output_ref"], f"formulas[{index}].output_ref")
    if output not in lines or lines[output]["kind"] != "derived":
        raise FinancialStatementStructureError("formula output must name a derived line")
    refs = wire["evidence_refs"]
    if (
        not isinstance(refs, list) or not refs
        or any(not isinstance(ref, str) or ref not in allowed_evidence for ref in refs)
        or len(refs) != len(set(refs))
    ):
        raise FinancialStatementStructureError(
            "formula evidence_refs must name held statement or note evidence"
        )
    tie = wire["tie_out_concept"]
    if tie is not None:
        tie = _text(tie, f"formulas[{index}].tie_out_concept")
        target = filed.get(tie)
        if target is None or target.get("status") != "filed":
            raise FinancialStatementStructureError("formula tie-out concept is not filed")
        if target.get("statement") != lines[output]["statement"]:
            raise FinancialStatementStructureError("formula tie-out statement differs")
        if _units(target) != {lines[output]["unit"]}:
            raise FinancialStatementStructureError("formula tie-out unit differs")
        if target.get("period_basis") != lines[output]["period_kind"]:
            raise FinancialStatementStructureError("formula tie-out period kind differs")
    result: dict[str, Any] = {
        "output_ref": output, "operator": operator,
        "tie_out_concept": tie, "evidence_refs": list(refs),
    }
    if operator == "sum":
        terms = wire["terms"]
        if not isinstance(terms, list) or not terms:
            raise FinancialStatementStructureError("sum formula needs terms")
        normalized = []
        for number, raw_term in enumerate(terms):
            term = _closed(raw_term, _TERM_FIELDS, f"formulas[{index}].terms[{number}]")
            ref = _text(term["line_ref"], "formula term line_ref")
            if ref not in lines or ref == output:
                raise FinancialStatementStructureError("formula term line_ref is invalid")
            coefficient = _decimal(term["coefficient"], "formula term coefficient")
            if coefficient not in {Decimal(-1), Decimal(1)}:
                raise FinancialStatementStructureError("sum coefficient must be -1 or 1")
            if lines[ref]["unit"] != lines[output]["unit"]:
                raise FinancialStatementStructureError("sum terms must use the output unit")
            if lines[ref]["period_kind"] != lines[output]["period_kind"]:
                raise FinancialStatementStructureError(
                    "sum terms must use the output period kind"
                )
            normalized.append({"line_ref": ref, "coefficient": str(int(coefficient))})
        if len({term["line_ref"] for term in normalized}) != len(normalized):
            raise FinancialStatementStructureError("sum formula repeats a term")
        result["terms"] = normalized
    elif operator == "divide":
        numerator = _text(wire["numerator_ref"], "formula numerator_ref")
        denominator = _text(wire["denominator_ref"], "formula denominator_ref")
        if numerator not in lines or denominator not in lines:
            raise FinancialStatementStructureError("divide formula names an unknown line")
        if lines[output]["role"] != "diluted_eps":
            raise FinancialStatementStructureError("divide is reserved for diluted EPS")
        if lines[numerator]["role"] != "parent_net_income":
            raise FinancialStatementStructureError("EPS numerator must be parent net income")
        if lines[denominator]["role"] != "diluted_weighted_average_shares":
            raise FinancialStatementStructureError(
                "EPS denominator must be diluted weighted-average shares"
            )
        if (
            lines[numerator]["period_kind"] != lines[output]["period_kind"]
            or lines[denominator]["period_kind"] != lines[output]["period_kind"]
        ):
            raise FinancialStatementStructureError(
                "EPS operands must use the output period kind"
            )
        result.update({"numerator_ref": numerator, "denominator_ref": denominator})
    else:
        raise FinancialStatementStructureError("formula operator is invalid")
    return result


def _period_cells(line: Mapping[str, Any]) -> dict[tuple[str | None, str], Decimal]:
    return {
        (cell.get("period_start"), str(end)): _decimal(cell.get("value"), "filed cell value")
        for end, cell in (line.get("cells") or {}).items()
        if isinstance(cell, Mapping)
    }


def _validate_formula_evidence(
    formulas: Sequence[Mapping[str, Any]], lines: Mapping[str, Mapping[str, Any]],
    filed: Mapping[str, Mapping[str, Any]], note_refs: set[str],
) -> None:
    by_output = {formula["output_ref"]: formula for formula in formulas}
    visiting: set[str] = set()
    memo: dict[str, set[str]] = {}

    def sources(ref: str) -> set[str]:
        if ref in memo:
            return memo[ref]
        if ref in visiting:
            raise FinancialStatementStructureError("formula graph contains a cycle")
        line = lines[ref]
        if line["kind"] == "filed":
            result = {
                str(source_ref)
                for cell in (filed[line["concept"]].get("cells") or {}).values()
                if isinstance(cell, Mapping)
                for source_ref in (cell.get("source_accessions") or [])
            }
        else:
            visiting.add(ref)
            formula = by_output.get(ref)
            if formula is None:
                raise FinancialStatementStructureError(
                    "derived line has no formula evidence authority"
                )
            dependencies = (
                [term["line_ref"] for term in formula["terms"]]
                if formula["operator"] == "sum"
                else [formula["numerator_ref"], formula["denominator_ref"]]
            )
            result = set().union(*(sources(dependency) for dependency in dependencies))
            result.update(ref for ref in formula["evidence_refs"] if ref in note_refs)
            visiting.remove(ref)
        memo[ref] = result
        return result

    for formula in formulas:
        dependencies = (
            [term["line_ref"] for term in formula["terms"]]
            if formula["operator"] == "sum"
            else [formula["numerator_ref"], formula["denominator_ref"]]
        )
        allowed = note_refs | set().union(*(sources(ref) for ref in dependencies))
        if not set(formula["evidence_refs"]).issubset(allowed):
            raise FinancialStatementStructureError(
                f"formula {formula['output_ref']} cites evidence outside its operands"
            )


def replay_historical_structure(
    structure: Mapping[str, Any], financial_inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Replay every equation on complete periods; never fill a missing term."""

    filed = _filed_index(financial_inputs)
    lines = {line["ref"]: line for line in structure["lines"]}
    values: dict[str, dict[tuple[str | None, str], Decimal]] = {
        ref: _period_cells(filed[line["concept"]])
        for ref, line in lines.items() if line["kind"] == "filed"
    }
    reports: list[dict[str, Any]] = []
    pending = {formula["output_ref"]: formula for formula in structure["formulas"]}
    while pending:
        progressed = False
        for output, formula in list(pending.items()):
            dependencies = (
                [term["line_ref"] for term in formula["terms"]]
                if formula["operator"] == "sum"
                else [formula["numerator_ref"], formula["denominator_ref"]]
            )
            if any(ref not in values for ref in dependencies):
                continue
            periods = set(values[dependencies[0]])
            for ref in dependencies[1:]:
                periods &= set(values[ref])
            calculated: dict[tuple[str | None, str], Decimal] = {}
            for period in sorted(periods, key=lambda item: (item[1], item[0] or "")):
                if formula["operator"] == "sum":
                    calculated[period] = sum(
                        (_decimal(term["coefficient"], "coefficient")
                         * values[term["line_ref"]][period]
                         for term in formula["terms"]),
                        Decimal(0),
                    )
                else:
                    divisor = values[formula["denominator_ref"]][period]
                    if divisor == 0:
                        continue
                    calculated[period] = values[formula["numerator_ref"]][period] / divisor
            values[output] = calculated
            tie = formula["tie_out_concept"]
            tied = {} if tie is None else _period_cells(filed[tie])
            tested = sorted(set(calculated) & set(tied), key=lambda item: item[1])
            def matches(period: tuple[str | None, str]) -> bool:
                if formula["operator"] != "divide":
                    return calculated[period] == tied[period]
                # Filed per-share values are rounded disclosures.  Use the
                # exponent the company actually filed for that period rather
                # than inventing one global EPS precision.
                quantum = Decimal(1).scaleb(tied[period].as_tuple().exponent)
                return calculated[period].quantize(quantum) == tied[period]
            mismatches = [
                {"period_start": period[0], "period_end": period[1],
                 "calculated": str(calculated[period]), "filed": str(tied[period])}
                for period in tested if not matches(period)
            ]
            if mismatches:
                raise FinancialStatementStructureError(
                    f"formula {output} does not tie to filed history"
                )
            reports.append({
                "output_ref": output,
                "status": "validated" if tested else "unavailable",
                "tested_periods": [
                    {"period_start": period[0], "period_end": period[1]}
                    for period in tested
                ],
                "reason": None if tested else (
                    "no complete historical period has every formula term and filed tie-out"
                ),
            })
            del pending[output]
            progressed = True
        if not progressed:
            raise FinancialStatementStructureError("formula graph contains a cycle")
    return {
        "schema_version": "financial-statement-structure-replay-0.1",
        "structure_hash": structure["content_hash"],
        "formulas": sorted(reports, key=lambda item: item["output_ref"]),
        "ready_for_forecast": bool(reports) and all(
            report["status"] == "validated" for report in reports
        ),
    }


def validate_financial_statement_structure(
    proposal: Mapping[str, Any], company_spec: Mapping[str, Any],
    financial_inputs: Mapping[str, Any], *,
    note_evidence: Sequence[Mapping[str, Any]] = (),
    note_evidence_resolver: Callable[[str], Mapping[str, Any] | None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate a company-spec extension and return it with replay proof."""

    fields = {
        "schema_version", "structure_ref", "company_ref", "spec_ref", "spec_hash",
        "financial_input_hash", "lines", "formulas", "created_by",
    }
    body = _closed(proposal, fields, "financial statement structure")
    authority = financial_input_authority(financial_inputs)
    spec_authority = _spec_authority(company_spec)
    if body["schema_version"] != SCHEMA_VERSION:
        raise FinancialStatementStructureError("unsupported structure schema_version")
    if body["company_ref"] != authority["company_ref"]:
        raise FinancialStatementStructureError("structure company differs from financial inputs")
    if body["company_ref"] != spec_authority["company_ref"]:
        raise FinancialStatementStructureError("structure company differs from company spec")
    if (
        body["spec_ref"] != spec_authority["spec_ref"]
        or body["spec_hash"] != spec_authority["spec_hash"]
        or financial_inputs.get("spec_ref") != spec_authority["spec_ref"]
    ):
        raise FinancialStatementStructureError("structure company spec authority drifted")
    if body["financial_input_hash"] != authority["content_hash"]:
        raise FinancialStatementStructureError("structure financial input authority drifted")
    _text(body["structure_ref"], "structure_ref")
    actor = _text(body["created_by"], "created_by")
    if not actor.startswith(("human:", "automation:")):
        raise FinancialStatementStructureError("created_by must use human: or automation:")
    filed = _filed_index(financial_inputs)
    raw_lines = body["lines"]
    if not isinstance(raw_lines, list) or not raw_lines:
        raise FinancialStatementStructureError("structure needs at least one line")
    lines = [_normalize_line(raw, index, filed) for index, raw in enumerate(raw_lines)]
    by_ref = {line["ref"]: line for line in lines}
    if len(by_ref) != len(lines):
        raise FinancialStatementStructureError("structure line ref is duplicated")
    _validate_spec_alignment(company_spec, lines)
    notes = _normalized_note_evidence(note_evidence, note_evidence_resolver)
    note_refs = {item["ref"] for item in notes}
    allowed_evidence = _statement_refs(financial_inputs) | note_refs
    raw_formulas = body["formulas"]
    if not isinstance(raw_formulas, list):
        raise FinancialStatementStructureError("formulas must be a list")
    formulas = [
        _normalize_formula(raw, index, by_ref, filed, allowed_evidence)
        for index, raw in enumerate(raw_formulas)
    ]
    outputs = [formula["output_ref"] for formula in formulas]
    derived = sorted(ref for ref, line in by_ref.items() if line["kind"] == "derived")
    if sorted(outputs) != derived or len(outputs) != len(set(outputs)):
        raise FinancialStatementStructureError(
            "every derived line must have exactly one formula"
        )
    _validate_formula_evidence(formulas, by_ref, filed, note_refs)
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "authority_ref": STRUCTURE_AUTHORITY_REF,
        "structure_ref": body["structure_ref"],
        "company_ref": body["company_ref"],
        "spec_ref": body["spec_ref"],
        "spec_hash": body["spec_hash"],
        "financial_input_hash": body["financial_input_hash"],
        "lines": lines,
        "formulas": formulas,
        "note_evidence": notes,
        "created_by": actor,
    }
    normalized["content_hash"] = content_hash(normalized)
    replay = replay_historical_structure(normalized, financial_inputs)
    return normalized, replay


def _quarter_gap(left: str, right: str) -> int:
    return (date.fromisoformat(right) - date.fromisoformat(left)).days


def aggregate_fiscal_year(
    cells: Sequence[Mapping[str, Any]], *, semantic: str, fiscal_year: str,
) -> dict[str, Any]:
    """Apply closed annual semantics without inventing missing quarters/shares."""

    selected = [dict(cell) for cell in cells if cell.get("fiscal_year") == fiscal_year]
    if semantic == "sum_quarters":
        quarters = sorted(
            (cell for cell in selected if cell.get("period_kind") == "quarter"),
            key=lambda cell: str(cell.get("period_end")),
        )
        units = {str(cell.get("unit")).casefold() for cell in quarters}
        calendars = {str(cell.get("calendar") or "") for cell in quarters}
        definitions = {str(cell.get("definition_ref") or "") for cell in quarters}
        complete_periods = all(
            isinstance(cell.get("period_start"), str)
            and isinstance(cell.get("period_end"), str)
            and 80 <= _quarter_gap(
                str(cell["period_start"]), str(cell["period_end"])
            ) <= 100
            for cell in quarters
        )
        contiguous = all(
            _quarter_gap(str(left["period_end"]), str(right["period_start"])) == 1
            for left, right in zip(quarters, quarters[1:])
        )
        if (
            len(quarters) != 4 or len(units) != 1
            or calendars == {""} or len(calendars) != 1
            or definitions == {""} or len(definitions) != 1
            or not complete_periods or not contiguous
        ):
            return {"status": "unavailable", "value": None,
                    "reason": ("a fiscal-year amount needs four contiguous quarters "
                               "with one calendar, definition, and unit")}
        return {"status": "computed", "value": str(sum(
                    (_decimal(cell.get("value"), "quarter value") for cell in quarters),
                    Decimal(0))), "unit": units.pop(), "source_periods": quarters}
    if semantic == "direct_annual":
        annual = [cell for cell in selected if cell.get("period_kind") == "annual"]
        if (
            len(annual) != 1
            or not isinstance(annual[0].get("calendar"), str)
            or not annual[0].get("calendar")
            or not isinstance(annual[0].get("definition_ref"), str)
            or not annual[0].get("definition_ref")
        ):
            return {"status": "unavailable", "value": None,
                    "reason": ("this line requires one directly filed annual value "
                               "with an exact calendar and definition")}
        return {"status": "computed", "value": str(_decimal(annual[0].get("value"),
                                                               "annual value")),
                "unit": str(annual[0].get("unit")).casefold(),
                "source_periods": annual}
    raise FinancialStatementStructureError("annual semantic is invalid for aggregation")


def annual_diluted_eps(
    *, parent_net_income_cells: Sequence[Mapping[str, Any]],
    diluted_weighted_share_cells: Sequence[Mapping[str, Any]], fiscal_year: str,
) -> dict[str, Any]:
    """Annual parent earnings divided by directly filed annual diluted shares."""

    income = aggregate_fiscal_year(
        parent_net_income_cells, semantic="sum_quarters", fiscal_year=fiscal_year,
    )
    shares = aggregate_fiscal_year(
        diluted_weighted_share_cells, semantic="direct_annual", fiscal_year=fiscal_year,
    )
    if income["status"] != "computed" or shares["status"] != "computed":
        return {"status": "unavailable", "value": None,
                "reason": "annual diluted EPS needs complete parent income and direct annual weighted shares"}
    if income.get("unit") not in {"usd", "eur", "gbp", "jpy", "cny"}:
        return {"status": "unavailable", "value": None,
                "reason": "annual parent income must use one currency unit"}
    if shares.get("unit") != "shares":
        return {"status": "unavailable", "value": None,
                "reason": "annual diluted weighted shares must use shares"}
    denominator = _decimal(shares["value"], "annual diluted weighted shares")
    income_calendars = {cell["calendar"] for cell in income["source_periods"]}
    share_calendar = shares["source_periods"][0]["calendar"]
    if income_calendars != {share_calendar}:
        return {"status": "unavailable", "value": None,
                "reason": "annual parent income and weighted shares use different calendars"}
    if denominator == 0:
        return {"status": "unavailable", "value": None,
                "reason": "annual diluted weighted shares are zero"}
    return {"status": "computed",
            "value": str(_decimal(income["value"], "annual parent income") / denominator),
            "unit": f"{income['unit']}_per_share",
            "source_periods": income["source_periods"] + shares["source_periods"]}


def forecast_structure_binding(
    structure: Mapping[str, Any], replay: Mapping[str, Any],
    financial_inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Closed handoff consumed by the future structure-aware forecast formula."""

    held = dict(structure)
    held_hash = held.pop("content_hash", None)
    if (
        structure.get("schema_version") != SCHEMA_VERSION
        or structure.get("authority_ref") != STRUCTURE_AUTHORITY_REF
        or not isinstance(held_hash, str)
        or content_hash(held) != held_hash
        or financial_input_authority(financial_inputs)["content_hash"]
        != structure.get("financial_input_hash")
    ):
        raise FinancialStatementStructureError("statement structure authority is invalid")
    expected_replay = replay_historical_structure(structure, financial_inputs)
    if dict(replay) != expected_replay:
        raise FinancialStatementStructureError("historical replay authority differs")
    if replay.get("structure_hash") != structure.get("content_hash"):
        raise FinancialStatementStructureError("historical replay is for another structure")
    if replay.get("ready_for_forecast") is not True:
        raise FinancialStatementStructureError("statement structure is not ready for forecast")
    projection = {
        "schema_version": "forecast-statement-structure-binding-0.1",
        "authority_ref": structure.get("authority_ref"),
        "structure_ref": structure.get("structure_ref"),
        "structure_hash": structure.get("content_hash"),
        "company_ref": structure.get("company_ref"),
        "spec_ref": structure.get("spec_ref"),
        "spec_hash": structure.get("spec_hash"),
        "financial_input_hash": structure.get("financial_input_hash"),
        "historical_replay_hash": content_hash(expected_replay),
    }
    for field, value in projection.items():
        _text(value, field)
    return {**projection, "content_hash": content_hash(projection)}


__all__ = [
    "ANNUAL_SEMANTICS", "FinancialStatementStructureError", "LINE_KINDS",
    "ROLES", "SCHEMA_VERSION", "STRUCTURE_AUTHORITY_REF",
    "aggregate_fiscal_year", "annual_diluted_eps", "financial_input_authority",
    "forecast_structure_binding", "replay_historical_structure",
    "validate_financial_statement_structure",
]
