"""Separate executed filing partitions from retained disclosure diagnostics.

All declared branches are roots, including unavailable branches. The complete
source-group proof remains untouched; only an unrelated disclosure's segment
arithmetic is not an assertion made by this execution plan.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .store import content_hash

RULE_REF = "rule:forecast-execution-statement-scope:2"
MAX_DIAGNOSTICS = 32


def _refuse(message):
    from .model_forecast_driver import ForecastModelValidationError
    raise ForecastModelValidationError("forecast execution scope: " + message)


def _key(row):
    return (row.get("ingest_id"), row.get("statement_context_ref"),
            row.get("concept"), row.get("period_start"), row.get("period_end"))


def _wire(rows):
    from .model_forecast_driver import _statement_line_proof_row
    return sorted((_statement_line_proof_row(r) for r in rows), key=str)


def _roots(record):
    """Validate references for every declared expression, not computed outputs."""
    structure = record.get("financial_statement_structure") or {}
    if not isinstance(structure, Mapping) or not structure.get("lines"):
        _refuse("declared financial structure is missing")
    lines = {line["ref"]: line for line in structure.get("lines") or []}
    if len(lines) != len(structure.get("lines") or []):
        _refuse("duplicate structure roots")
    for formula in structure.get("formulas") or []:
        if formula.get("output_ref") not in lines:
            _refuse("unknown structure output")
        for term in formula.get("terms") or []:
            if term.get("line_ref") not in lines:
                _refuse("unknown structure dependency")
    specification = (record.get("execution_contract") or {}).get("specification") or {}
    declarations = {d["ref"]: d for d in specification.get("revenue_drivers") or []}
    for formula in specification.get("driver_formulas") or []:
        if formula.get("output_ref") not in lines:
            _refuse("unknown economic output")
        if any(ref not in declarations for ref in formula.get("driver_refs") or []):
            _refuse("unknown economic dependency")
    drivers = {d["ref"]: d for d in record.get("drivers") or []}
    if len(drivers) != len(record.get("drivers") or []):
        _refuse("duplicate driver roots")
    for line in lines.values():
        if line.get("kind") == "filed":
            matches = [d for d in drivers.values() if d.get("structure_line_ref") == line["ref"]]
            if (len(matches) != 1 or matches[0].get("concept") != line.get("concept")
                    or matches[0].get("statement") != line.get("statement")):
                _refuse("declared filed root has no exact driver")
    # A declared dimensional basis must not disappear simply because its
    # execution failed. Unavailable consolidated leaves remain roots too.
    for ref, declaration in declarations.items():
        identity = declaration.get("basis_identity")
        if not isinstance(identity, Mapping) or not identity.get("dimensions"):
            continue
        driver = drivers.get(("fcf-input:" if ref.startswith("fcf-") else "economic:") + ref)
        if (driver is None or driver.get("statement_identity") != identity
                or driver.get("concept") != declaration.get("basis_concept")
                or not driver.get("history")):
            _refuse("declared dimensional root lacks exact executed history")
    return sorted(drivers), content_hash({
        "structure": structure, "specification": specification,
        "cash_flow_companion": record.get("cash_flow_companion"),
        "drivers": list(drivers.values()),
    })


def execution_scope(record, rows, source_groups, *, prior_partition_scope=None):
    from .forecast_consumed_source_groups import RULE_REF as SOURCE_RULE, _wire as source_wire
    from .economic_invariants import (ADDITIVE_SEGMENT_AXES, CONSOLIDATION_ITEMS_AXES,
                                     OPERATING_SEGMENT_MEMBERS, segment_groups, _segment_sum, FAIL)
    if (not isinstance(source_groups, Mapping)
            or source_groups.get("rule_ref") != SOURCE_RULE
            or source_groups.get("company_ref") != record.get("company_ref")
            or source_groups.get("content_hash") != content_hash({k: v for k, v in source_groups.items() if k != "content_hash"})):
        _refuse("source-group identity or hash differs")
    root_refs, roots_hash = _roots(record)
    by_key = {}
    for row in rows:
        by_key.setdefault(_key(row), []).append(row)
    entries = {_key(entry): entry for entry in source_groups["groups"]}
    if len(entries) != len(source_groups["groups"]) or set(entries) != set(by_key):
        _refuse("complete source-group set differs")
    families, selected = [], set()
    for key, group_rows in by_key.items():
        entry = entries[key]
        if (entry.get("source_rows_hash") != content_hash({"statement_rows": source_wire(group_rows)})
                or entry.get("statement_line_refs") != sorted(str(r["line_id"]) for r in group_rows)):
            _refuse("source-group rows differ")
        for request in entry.get("consumed_by") or []:
            if request.get("driver_ref") not in root_refs:
                _refuse("source consumer is not a declared execution root")
            dimensions = request.get("dimensions")
            if not isinstance(dimensions, Mapping):
                _refuse("source consumer lacks complete dimensions")
            driver = next(d for d in record["drivers"] if d["ref"] == request["driver_ref"])
            identity = driver.get("statement_identity") or {}
            if (dict(dimensions) != identity.get("dimensions", {})
                    or request.get("statement") != identity.get("statement", driver.get("statement"))
                    or request.get("concept") != key[2]):
                _refuse("source consumer identity differs from its driver")
            if not dimensions:
                continue
            axes = [a for a in dimensions if a in ADDITIVE_SEGMENT_AXES]
            # Unsupported multi-axis families must not silently become a
            # passing consolidated model. Retain the old conservative gate.
            if len(axes) != 1:
                _refuse("consumed dimensional root has no unique additive axis")
            axis = axes[0]
            fixed = {a: m for a, m in dimensions.items() if a != axis}
            if fixed and not all(a in CONSOLIDATION_ITEMS_AXES and m in OPERATING_SEGMENT_MEMBERS
                                 for a, m in fixed.items()):
                _refuse("consumed fixed dimensions have no supported partition semantics")
            statement = request.get("statement")
            family = [r for r in group_rows if r.get("statement") == statement
                      and set(r.get("dimensions") or {}) == set(dimensions)
                      and all((r.get("dimensions") or {}).get(a) == m for a, m in fixed.items())]
            if not family or not any((r.get("dimensions") or {}) == dict(dimensions) for r in family):
                _refuse("consumed member is absent from its complete family")
            # Include peers and disclosed corporate reconciling rows in this
            # exact source/statement/period, never just the requested member.
            related = [r for r in group_rows if r.get("statement") == statement and (
                not r.get("dimensions") and not r.get("dimension_axis") and not r.get("is_breakdown")
                or r in family
                or r.get("dimension_axis") in CONSOLIDATION_ITEMS_AXES)]
            from .model_forecast_driver import _filing_units_match
            if any(not _filing_units_match(r.get("unit"), driver.get("unit"), structured=True)
                   for r in related):
                _refuse("consumed complete family mixes units")
            for row in related:
                selected.add(str(row["line_id"]))
            families.append({"ingest_id": key[0], "statement_context_ref": key[1],
                "concept": key[2], "period_start": key[3], "period_end": key[4],
                "statement": statement, "axis": axis, "fixed_dimensions": fixed,
                "driver_ref": request["driver_ref"],
                "members": sorted({str((r.get("dimensions") or {})[axis]) for r in family}),
                "statement_line_refs": sorted(str(r["line_id"]) for r in related)})
    # All consolidated rows remain present for existing global/source checks.
    # No financial amounts, histories or result DAG are edited here.
    scoped = [r for r in rows if str(r["line_id"]) in selected or (
        not r.get("dimensions") and not r.get("dimension_axis") and not r.get("is_breakdown"))]
    grouped = {}
    for row in rows:
        grouped.setdefault((row.get("ingest_id"), row.get("statement_context_ref"), row.get("statement")), []).append(row)
    diagnostics = []
    for key, source_rows in sorted(grouped.items(), key=lambda item: str(item[0])):
        asserted_groups = {(f["concept"], f"{f['period_start']}..{f['period_end']}", f["axis"])
                           for f in families if (f["ingest_id"], f["statement_context_ref"], f["statement"]) == key}
        for group in segment_groups(source_rows):
            if (group["line"], group["period"], group["axis"]) in asserted_groups:
                continue
            verdict = _segment_sum([group])
            if verdict.status == FAIL:
                diagnostics.append({"ingest_id": key[0], "statement_context_ref": key[1],
                    "statement": key[2], "group": group, "findings": list(verdict.findings),
                    "scope": "retained_disclosure_not_consumed_partition", "financial_evidence": False,
                    "retry_authority": False})
    manifest = {"rule_ref": RULE_REF, "source_groups_hash": source_groups["content_hash"],
        "root_refs": root_refs, "roots_hash": roots_hash,
        "retained_source_rows": len(rows), "source_rows_hash": content_hash(_wire(rows)),
        "asserted_rows": len(scoped), "asserted_rows_hash": content_hash(_wire(scoped)),
        "consumed_families": sorted(families, key=str),
        "partitions": (prior_partition_scope or {}).get("partitions") or [],
        "supporting_partition_diagnostics": diagnostics[:MAX_DIAGNOSTICS],
        "omitted_diagnostic_count": max(0, len(diagnostics) - MAX_DIAGNOSTICS),
        "omitted_diagnostics_hash": content_hash(diagnostics[MAX_DIAGNOSTICS:]),
        "unmodeled_partitions_reason": "Unconsumed disclosures remain in the complete source proof and diagnostic; they are not asserted by this execution plan."}
    # Full rows also supply the filer's member declarations and explicit
    # corporate bridges. Only the arithmetic assertions are scoped; removing
    # peer rows here would hide members declared on another reported line.
    return list(rows), {**manifest, "content_hash": content_hash(manifest)}


def evaluate_execution(record, *, statement_rows=(), solver_results=(), scope):
    from .economic_invariants import forecast_subject, segment_groups, evaluate, FORECAST_MODEL
    if (scope.get("rule_ref") != RULE_REF
            or scope.get("content_hash") != content_hash({k: v for k, v in scope.items() if k != "content_hash"})
            or scope.get("source_rows_hash") != content_hash(_wire(statement_rows))
            or scope.get("roots_hash") != _roots(record)[1]):
        _refuse("execution scope does not bind this candidate and its full source rows")
    required = {(f["ingest_id"], f["statement_context_ref"], f["statement"],
                 f["concept"], f"{f['period_start']}..{f['period_end']}", f["axis"])
                for f in scope["consumed_families"]}
    grouped = {}
    for row in statement_rows:
        grouped.setdefault((row.get("ingest_id"), row.get("statement_context_ref"), row.get("statement")), []).append(row)
    selected = []
    seen = set()
    for key, source_rows in sorted(grouped.items(), key=lambda item: str(item[0])):
        for group in segment_groups(source_rows):
            family = (*key, group["line"], group["period"], group["axis"])
            if family in required:
                selected.append(group)
                seen.add(family)
    if seen != required:
        _refuse("a consumed complete family lacks its own consolidated partition")
    subject = forecast_subject(record, statement_rows=statement_rows, solver_results=solver_results)
    subject["segments"] = selected
    return evaluate(output_kind=FORECAST_MODEL,
        output_ref=str(record.get("id") or record.get("model_ref") or "forecast-model"),
        company_ref=str(record.get("company_ref")), subject=subject)
