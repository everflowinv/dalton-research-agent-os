"""Explicit identity scope for a model's filed segment-sum proof.

Amounts never select a partition. The executed specification names its exact
members; the complete current filing independently checks that membership.
Historical periods/accessions are the ones consumed by that execution plan.
Other disclosed partitions remain in the complete filing proof as source data.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence
from .store import content_hash

RULE_REF = "rule:forecast-statement-scope:1"


def statement_scope(record: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
                    connection: Any = None) -> tuple[list[Mapping[str, Any]], dict[str, Any] | None]:
    from .model_forecast_driver import ForecastModelValidationError
    declarations = {d["ref"]: d for d in (record.get("execution_contract") or {}).get("specification", {}).get("revenue_drivers") or []}
    plans = (record.get("execution_contract") or {}).get("specification", {}).get("driver_formulas") or []
    drivers = {d["ref"]: d for d in record.get("drivers") or []}
    selected = []
    manifests = []
    for plan in plans:
        members = [declarations.get(ref) for ref in plan.get("driver_refs") or []]
        if plan.get("operator") != "sum" or not members or not all(d and d.get("basis_identity") for d in members):
            continue
        identities = [d["basis_identity"] for d in members]
        concepts = {d["basis_concept"] for d in members}
        statements = {i["statement"] for i in identities}
        dimensions = [i["dimensions"] for i in identities]
        axes = set(dimensions[0])
        varying = [axis for axis in axes if len({d.get(axis) for d in dimensions}) > 1]
        if len(concepts) != 1 or len(statements) != 1 or any(set(d) != axes for d in dimensions) or len(varying) != 1:
            raise ForecastModelValidationError("segment scope requires one explicit partition axis and identical fixed dimensions")
        axis = varying[0]
        fixed = {a: v for a, v in dimensions[0].items() if a != axis}
        expected = {d[axis] for d in dimensions}
        if len(expected) != len(members):
            raise ForecastModelValidationError("segment scope repeats a member identity")
        concept, statement = next(iter(concepts)), next(iter(statements))
        def in_family(row):
            dims = row.get("dimensions") or {}
            return (row.get("concept") == concept and row.get("statement") == statement
                    and set(dims) == axes and all(dims[a] == v for a, v in fixed.items()))
        economic = [drivers.get("economic:" + d["ref"]) for d in members]
        if not all(economic):
            raise ForecastModelValidationError("segment scope is missing an executed identity driver")
        histories = [{c["period_end"]: c for c in d.get("history") or []} for d in economic]
        common = sorted(set.intersection(*(set(h) for h in histories)))
        if len(common) < 4:
            raise ForecastModelValidationError("segment scope lacks four common filed quarters")
        latest = common[-1]
        requested = {(str(accession), end) for h in histories for end in common
                     for accession in h[end].get("accessions") or []}
        # Resolve complete current authority independently of caller-supplied
        # rows. A caller cannot hide a member by omitting its proof row.
        full = list(rows)
        filing_map = {}
        if connection is not None and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='coverage_mission_statement_filings'").fetchone():
            from .statement_context import statement_rows
            for ingest in {str(r["ingest_id"]) for r in rows if r.get("ingest_id")}:
                filing = connection.execute("SELECT accession,report_date FROM coverage_mission_statement_filings WHERE ingest_id=?", (ingest,)).fetchone()
                if filing is None:
                    raise ForecastModelValidationError("segment scope names unknown filing")
                filing_map[ingest] = dict(filing)
            current_ingests = {ingest for ingest, f in filing_map.items()
                               if (str(f["accession"]), latest) in requested}
            full = []
            for ingest in current_ingests:
                bindings = {r.get("statement_context_ref") for r in rows if str(r.get("ingest_id")) == ingest}
                if len(bindings) != 1:
                    raise ForecastModelValidationError("segment scope mixes context versions")
                full.extend(statement_rows(connection, ingest, version_id=next(iter(bindings))))
        current = [r for r in full if in_family(r) and r.get("period_end") == latest]
        current_members = {(r.get("dimensions") or {})[axis] for r in current}
        if current_members != expected:
            raise ForecastModelValidationError("segment scope must declare every current filed member; missing=" + str(sorted(current_members - expected)) + "; unknown=" + str(sorted(expected - current_members)))
        scoped = [r for r in rows if in_family(r) and (r.get("dimensions") or {})[axis] in expected
                  and ((str((filing_map.get(str(r.get("ingest_id"))) or {}).get("accession") or r.get("accession")), str(r.get("period_end"))) in requested
                       if filing_map else r.get("period_end") in common)]
        groups = {}
        for r in scoped:
            key = (r.get("ingest_id"), r.get("period_start"), r.get("period_end"), r.get("unit"))
            groups.setdefault(key, set()).add(r["dimensions"][axis])
        if not groups or any(names != expected for names in groups.values()):
            raise ForecastModelValidationError("segment scope is missing a declared member in a consumed filing period")
        selected.extend(scoped)
        manifests.append({"output_ref": plan["output_ref"], "concept": concept, "statement": statement,
                          "axis": axis, "fixed_dimensions": fixed, "members": sorted(expected),
                          "scope_ref": "filed-partition:" + content_hash({"concept": concept, "statement": statement, "axis": axis, "fixed_dimensions": fixed, "members": sorted(expected)}),
                          "historical_quarters": common, "periods": [list(k) for k in sorted(groups, key=str)],
                          "context_refs": sorted({str(r["statement_context_ref"]) for r in scoped if r.get("statement_context_ref")}),
                          "statement_line_refs": sorted({str(r["line_id"]) for r in scoped if r.get("line_id")}),
                          "current_membership_rows_hash": content_hash(current)})
    if not manifests:
        return list(rows), None
    # Consolidated leaves are still checked. Dimensional data from other
    # concepts/partitions are preserved in the proof but not asserted by this
    # particular economic execution plan.
    scoped_rows = [r for r in rows if not r.get("dimension_axis") and not r.get("dimensions") and not r.get("is_breakdown")]
    scoped_rows.extend(selected)
    manifest = {"rule_ref": RULE_REF, "partitions": manifests,
                "retained_source_rows": len(rows), "asserted_rows": len(scoped_rows),
                "unmodeled_partitions_reason": "Other filed dimensions remain source data; only the specification's complete identity partitions enter this model's segment-sum assertion."}
    manifest["content_hash"] = content_hash(manifest)
    return scoped_rows, manifest
