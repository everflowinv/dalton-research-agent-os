"""Explicit period-specific choices from complete, already verified originals.

This module never changes an original dimension or selects a family for the
model. Callers must supply the full source rows and independently verified
filing authorities again when verifying a saved selection.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from .store import content_hash

CONTRACT_REF = "model-period-source-family:0.1"
INPUT_EVIDENCE_REF = "filed-input-evidence:period-source-family:1"
MAX_SOURCE_ROWS = 50000
SOURCE_FIELDS = ("ingest_id", "statement_context_ref", "statement", "concept",
                 "period_start", "period_end", "unit")


class PeriodSourceFamilyError(ValueError):
    pass


def _amount(row):
    raw = "" if row.get("value") is None else str(row["value"])
    if len(raw) > 128:
        raise PeriodSourceFamilyError("source amount exceeds the bounded decimal representation")
    try:
        value = Decimal(raw)
        if not value.is_finite() or abs(value.as_tuple().exponent) > 1000:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise PeriodSourceFamilyError("source amount is not a bounded finite decimal") from None
    return value


def source_key(value):
    """The unit and full original duration are part of the source identity."""
    return tuple(value.get(field) for field in SOURCE_FIELDS)


def catalog(company_ref: str, filings: Sequence[Mapping], rows: Sequence[Mapping]):
    from .economic_invariants import (
        ADDITIVE_SEGMENT_AXES, CONSOLIDATION_ITEMS_AXES, OPERATING_SEGMENT_MEMBERS,
    )
    from .model_forecast_driver import CONCEPT_ROLES, REVENUE
    from .company_financial_statement_structure import presentation_rounding_ties

    if not company_ref or len(rows) > MAX_SOURCE_ROWS:
        raise PeriodSourceFamilyError("complete source catalog exceeds its bounded scope")
    authorities = {}
    for filing in filings:
        ingest = filing.get("ingest_id")
        if (not ingest or ingest in authorities or filing.get("company_ref") != company_ref
                or not filing.get("accession") or not filing.get("content_hash")):
            raise PeriodSourceFamilyError("filing identity is duplicate, foreign or incomplete")
        authorities[ingest] = filing
    buckets = defaultdict(list)
    refs = set()
    for row in rows:
        if row.get("ingest_id") not in authorities:
            raise PeriodSourceFamilyError("source row belongs to an unknown filing")
        if not row.get("line_id") or row["line_id"] in refs:
            raise PeriodSourceFamilyError("complete source row refs are not unique")
        refs.add(row["line_id"])
        if row.get("statement") != "income" or CONCEPT_ROLES.get(row.get("concept")) != REVENUE:
            continue
        key = source_key(row)
        dims = row.get("dimensions")
        if (not all(key) or not isinstance(dims, Mapping)
                or row.get("dimension_count") != len(dims)
                or not all(isinstance(a, str) and a and isinstance(m, str) and m for a, m in dims.items())):
            raise PeriodSourceFamilyError("revenue source lacks its complete period/unit/dimensional identity")
        buckets[key].append(row)
    groups, gaps = [], []
    for key, source_rows in sorted(buckets.items(), key=lambda item: str(item[0])):
        source = dict(zip(SOURCE_FIELDS, key))
        filing = authorities[source["ingest_id"]]
        atomic = {}
        for row in sorted(source_rows, key=lambda r: r["line_id"]):
            dims = tuple(sorted(row["dimensions"].items()))
            amount = _amount(row)
            if dims in atomic:
                held = atomic[dims]
                if held["amount"] != amount:
                    raise PeriodSourceFamilyError("same complete source identity has conflicting amounts")
                held["origin_refs"].append(row["line_id"])
            else:
                atomic[dims] = {"amount": amount, "value": row["value"],
                                "origin_refs": [row["line_id"]]}
        families = defaultdict(dict)
        for dims, value in atomic.items():
            for axis, member in dims:
                if axis not in ADDITIVE_SEGMENT_AXES:
                    continue
                fixed = tuple((a, m) for a, m in dims if a != axis)
                if not all(a in CONSOLIDATION_ITEMS_AXES and m in OPERATING_SEGMENT_MEMBERS
                           for a, m in fixed):
                    continue  # Other axes are not silently collapsed into one partition.
                families[(axis, fixed)][member] = value
        for (axis, fixed), members in sorted(families.items(), key=lambda item: str(item[0])):
            base = {**source, "company_ref": company_ref, "accession": filing["accession"],
                    "filing_hash": filing["content_hash"], "axis": axis,
                    "fixed_dimensions": dict(fixed), "members": sorted(members)}
            parent, consolidated = atomic.get(fixed), atomic.get(())
            if len(members) < 2 or parent is None or consolidated is None:
                gaps.append({**base, "reason": "same_original_complete_family_or_total_missing"})
                continue
            amounts = [members[m]["amount"] for m in sorted(members)]
            total = sum(amounts, Decimal(0))
            def tie(calculated, reported, leaves):
                if calculated == reported:
                    return "exact"
                if presentation_rounding_ties(calculated, reported, leaves):
                    return "presentation_rounding"
                return None
            member_mode = tie(total, parent["amount"], amounts)
            company_mode = tie(parent["amount"], consolidated["amount"], [parent["amount"]])
            if member_mode is None or company_mode is None:
                gaps.append({**base, "reason": "same_original_family_does_not_close",
                             "member_sum": str(total), "reported_parent": str(parent["amount"]),
                             "reported_consolidated": str(consolidated["amount"])})
                continue
            body = {**base, "contract_ref": CONTRACT_REF,
                    "member_sources": [{"member": m, "dimensions": {**dict(fixed), axis: m},
                                        "value": members[m]["value"],
                                        "origin_refs": sorted(members[m]["origin_refs"])}
                                       for m in sorted(members)],
                    "parent_source": {"dimensions": dict(fixed), "value": parent["value"],
                                      "origin_refs": sorted(parent["origin_refs"])},
                    "consolidated_source": {"dimensions": {}, "value": consolidated["value"],
                                            "origin_refs": sorted(consolidated["origin_refs"])},
                    "members_to_parent": member_mode, "parent_to_consolidated": company_mode,
                    "complete_source_rows_hash": content_hash(source_rows)}
            groups.append({**body, "family_ref": "period-source-family:" + content_hash(body)})
    body = {"contract_ref": CONTRACT_REF, "company_ref": company_ref,
            "filing_authorities_hash": content_hash(list(filings)),
            "source_read_scopes": [{"ingest_id": filing["ingest_id"],
                "context_refs": sorted({row["statement_context_ref"] for row in rows
                    if row["ingest_id"] == filing["ingest_id"]})}
                for filing in filings],
            "complete_source_rows_hash": content_hash(list(rows)),
            "source_scopes": [dict(zip(SOURCE_FIELDS, key)) for key in sorted(buckets, key=str)],
            "groups": groups, "gaps": gaps,
            "scope": "explicit_source_choices_not_automatic_selection_or_forecast_approval"}
    return {**body, "content_hash": content_hash(body)}


def _binding(group, member):
    source = next((m for m in group["member_sources"] if m["member"] == member), None)
    if source is None:
        raise PeriodSourceFamilyError("explicit source family does not contain this declared member")
    return {**{field: group[field] for field in SOURCE_FIELDS},
            "family_ref": group["family_ref"], "filing_hash": group["filing_hash"],
            "accession": group["accession"], "axis": group["axis"],
            "member": member, "members": group["members"],
            "dimensions": source["dimensions"], "origin_refs": source["origin_refs"]}


def needs_period_choices(admitted):
    """Source-only eligibility, stable after a model is published.

    Do not wake static complete companies. For one monetary concept the full
    duration set must have a constant axis/member composition and unit, with
    at least one complete original family per duration, while no one static
    fixed-dimensional family covers that same whole history.
    """
    scopes = defaultdict(set)
    for scope in admitted["source_scopes"]:
        scopes[(scope["concept"], scope["statement"])].add(source_key(scope))
    candidates = defaultdict(lambda: defaultdict(set))
    for group in admitted["groups"]:
        composition = (group["concept"], group["statement"], group["unit"],
                       group["axis"], tuple(group["members"]))
        static = tuple(sorted(group["fixed_dimensions"].items()))
        candidates[composition][static].add(source_key(group))
    for composition, static_scopes in candidates.items():
        required = scopes[composition[:2]]
        if (required and set().union(*static_scopes.values()) == required
                and not any(covered == required for covered in static_scopes.values())):
            return True
    return False


def driver_bindings(context, driver):
    """Resolve only the model's explicit choices in its admitted input."""
    from .economic_invariants import ADDITIVE_SEGMENT_AXES
    choices = driver.get("basis_period_family_refs")
    if "basis_period_family_refs" not in driver:
        return None
    admitted = (context or {}).get("period_source_family_catalog")
    if (not isinstance(admitted, Mapping) or admitted.get("contract_ref") != CONTRACT_REF
            or admitted.get("content_hash") != content_hash({k: v for k, v in admitted.items() if k != "content_hash"})
            or not isinstance(choices, list) or not choices or len(choices) > 256
            or not all(isinstance(ref, str) for ref in choices) or len(choices) != len(set(choices))):
        raise PeriodSourceFamilyError("explicit period-family choices require the exact admitted catalog")
    identity = driver.get("basis_identity") or {}
    dimensions = identity.get("dimensions") or {}
    axes = [a for a in dimensions if a in ADDITIVE_SEGMENT_AXES]
    if len(axes) != 1:
        raise PeriodSourceFamilyError("period-family driver requires one explicitly declared additive axis")
    axis, member = axes[0], dimensions[axes[0]]
    by_ref = {g["family_ref"]: g for g in admitted["groups"]}
    selected = []
    for ref in choices:
        group = by_ref.get(ref)
        if group is None or group["family_ref"] != "period-source-family:" + content_hash({
                k: v for k, v in group.items() if k != "family_ref"}):
            raise PeriodSourceFamilyError("period-family ref is stale or outside this admitted source")
        if (group["concept"] != driver.get("basis_concept")
                or group["statement"] != identity.get("statement") or group["axis"] != axis):
            raise PeriodSourceFamilyError("period-family choice differs from the declared concept/statement/axis")
        selected.append(group)
    expected = set(tuple(source_key(scope)) for scope in admitted["source_scopes"]
                   if scope["concept"] == driver.get("basis_concept") and scope["statement"] == identity.get("statement"))
    actual = [source_key(group) for group in selected]
    if set(actual) != expected or len(actual) != len(set(actual)):
        raise PeriodSourceFamilyError("period-family choices must cover every admitted original duration exactly once")
    bindings = [_binding(group, member) for group in sorted(selected, key=lambda g: str(source_key(g)))]
    if len({tuple(b["members"]) for b in bindings}) != 1 or len({b["unit"] for b in bindings}) != 1:
        raise PeriodSourceFamilyError("period-family choices change member composition or monetary unit")
    latest = max(bindings, key=lambda b: (b["period_end"], b["accession"]))
    if latest["dimensions"] != dimensions:
        raise PeriodSourceFamilyError("current driver identity differs from its latest explicit family")
    body = {"contract_ref": CONTRACT_REF, "catalog_hash": admitted["content_hash"],
            "company_ref": admitted["company_ref"], "current_identity": identity,
            "source_read_scopes": admitted["source_read_scopes"],
            "bindings": bindings}
    return {**body, "content_hash": content_hash(body)}


def verify_driver_bindings(proof, *, driver, company_ref, filings, rows):
    """Independently recover every chosen source family before history use."""
    if (not isinstance(proof, Mapping) or proof.get("contract_ref") != CONTRACT_REF
            or proof.get("company_ref") != company_ref
            or proof.get("current_identity") != driver.get("basis_identity")
            or proof.get("content_hash") != content_hash({k: v for k, v in proof.items() if k != "content_hash"})):
        raise PeriodSourceFamilyError("saved period-family driver proof differs")
    rebuilt = catalog(company_ref, filings, rows)
    expected = driver_bindings({"period_source_family_catalog": rebuilt}, driver)
    if proof != expected:
        raise PeriodSourceFamilyError("saved period-family binding does not replay its complete original source scope")
    return proof


def identity_for_row(driver, row):
    """No row is matched by value, by label, or by a missing-axis heuristic."""
    proof = driver.get("statement_period_family_bindings")
    if proof is None:
        return driver.get("statement_identity")
    if (not isinstance(proof, Mapping)
            or set(proof) != {"contract_ref", "catalog_hash", "company_ref", "current_identity",
                              "source_read_scopes", "bindings", "content_hash"}
            or not isinstance(proof.get("bindings"), list) or not proof["bindings"]
            or len(proof["bindings"]) > 256
            or not all(isinstance(b, Mapping) and set(b) == set(SOURCE_FIELDS) | {
                "family_ref", "filing_hash", "accession", "axis", "member", "members",
                "dimensions", "origin_refs"}
                and all(isinstance(b[k], str) and b[k] for k in SOURCE_FIELDS)
                and isinstance(b["dimensions"], Mapping)
                and all(isinstance(a, str) and a and isinstance(m, str) and m
                        for a, m in b["dimensions"].items())
                and all(isinstance(b[k], list) and b[k]
                    and all(isinstance(v, str) and v for v in b[k])
                    and len(b[k]) == len(set(b[k])) for k in ("members", "origin_refs"))
                and b["member"] in b["members"] and b["dimensions"].get(b["axis"]) == b["member"]
                for b in proof["bindings"])
            or not isinstance(proof.get("source_read_scopes"), list)
            or not 1 <= len(proof["source_read_scopes"]) <= 64
            or not all(isinstance(s, Mapping) and set(s) == {"ingest_id", "context_refs"}
                and isinstance(s["ingest_id"], str) and s["ingest_id"]
                and isinstance(s["context_refs"], list) and len(s["context_refs"]) <= 64
                and all(isinstance(r, str) and r for r in s["context_refs"])
                and len(s["context_refs"]) == len(set(s["context_refs"]))
                for s in proof["source_read_scopes"])
            or len({source_key(b) for b in proof["bindings"]}) != len(proof["bindings"])
            or proof.get("contract_ref") != CONTRACT_REF
            or proof.get("content_hash") != content_hash({k: v for k, v in proof.items() if k != "content_hash"})
            or proof.get("current_identity") != driver.get("statement_identity")):
        raise PeriodSourceFamilyError("executed period-family identity proof differs")
    matches = [b for b in proof["bindings"] if
               (source_key(b) == source_key(row) if row.get("unit") is not None
                else source_key(b)[:-1] == source_key(row)[:-1])]
    if len(matches) != 1:
        return None
    binding = matches[0]
    # Family peers use the same source period, but only exact member originals
    # can supply this driver's history value.
    return {"statement": binding["statement"], "dimensions": binding["dimensions"]}


def validate_sum_bindings(drivers, formulas):
    by_ref = {d["ref"]: d for d in drivers}
    seen = set()
    for formula in formulas:
        declared = [by_ref[ref] for ref in formula["driver_refs"]]
        if not any(d.get("basis_period_family_bindings") for d in declared):
            continue
        if formula["operator"] != "sum" or not all(d.get("basis_period_family_bindings") for d in declared):
            raise PeriodSourceFamilyError("every member of a period-bound revenue sum needs explicit complete families")
        maps = [{source_key(b): b for b in d["basis_period_family_bindings"]["bindings"]} for d in declared]
        if any(set(m) != set(maps[0]) for m in maps):
            raise PeriodSourceFamilyError("period-bound revenue sum members select different original periods")
        for key in maps[0]:
            choices = [m[key] for m in maps]
            if (len({b["family_ref"] for b in choices}) != 1
                    or sorted(b["member"] for b in choices) != choices[0]["members"]):
                raise PeriodSourceFamilyError("period-bound revenue sum must select every member of one complete original family")
        seen.update(d["ref"] for d in declared)
    if any(d.get("basis_period_family_bindings") and d["ref"] not in seen for d in drivers):
        raise PeriodSourceFamilyError("period-bound family selection has no executable complete revenue sum")


def validate_executed_binding(driver, declaration):
    proof = driver.get("statement_period_family_bindings")
    expected = declaration.get("basis_period_family_bindings")
    if proof != expected:
        raise PeriodSourceFamilyError("executed period-family proof differs from its immutable specification")
    if proof is None:
        return
    if (proof.get("contract_ref") != CONTRACT_REF
            or proof.get("current_identity") != driver.get("statement_identity")
            or proof.get("content_hash") != content_hash({k: v for k, v in proof.items() if k != "content_hash"})
            or set(declaration.get("basis_period_family_refs") or []) != {b["family_ref"] for b in proof["bindings"]}):
        raise PeriodSourceFamilyError("executed period-family proof loses its original source choices")


def prompt_catalog(admitted):
    """Small explicit choices; full evidence remains in the admitted state."""
    sources, compositions, groups = [], [], []
    source_map, composition_map = {}, {}
    for g in admitted["groups"]:
        source = [g[k] for k in ("ingest_id", "statement_context_ref", "statement", "accession", "filing_hash")]
        skey = tuple(source)
        if skey not in source_map:
            source_map[skey] = len(sources); sources.append(source)
        composition = [g["axis"], g["fixed_dimensions"], g["members"]]
        ckey = content_hash(composition)
        if ckey not in composition_map:
            composition_map[ckey] = len(compositions); compositions.append(composition)
        groups.append([g["family_ref"], source_map[skey], g["concept"], g["period_start"],
                       g["period_end"], g["unit"], composition_map[ckey],
                       g["members_to_parent"], g["parent_to_consolidated"]])
    # Source-only eligibility proves these complete groups cover the full
    # duration scope. Omitted non-complete disclosures remain diagnostics,
    # never implied selectable sources or a reason to splice members.
    return {"contract_ref": CONTRACT_REF, "company_ref": admitted["company_ref"],
            "content_hash": admitted["content_hash"],
            "source_fields": ["ingest_id", "statement_context_ref", "statement", "accession", "filing_hash"],
            "sources": sources,
            "composition_fields": ["axis", "fixed_dimensions", "members"], "compositions": compositions,
            "group_fields": ["family_ref", "source_index", "concept", "period_start", "period_end", "unit",
                             "composition_index", "members_to_parent", "parent_to_consolidated"], "groups": groups,
            "complete_original_duration_scope_hash": content_hash(admitted["source_scopes"]),
            "non_complete_disclosure_count": len(admitted["gaps"]),
            "non_complete_disclosures_hash": content_hash(admitted["gaps"])}


def preflight(admitted, selections, requirements, *, company_ref, filings, rows):
    """Rebuild the complete catalog, then require every exact operand period.

    Requirements come from the caller's fixed historical scope, not from the
    selection. Omitting a cumulative operand or a recent quarter is a failure.
    """
    rebuilt = catalog(company_ref, filings, rows)
    if admitted != rebuilt:
        raise PeriodSourceFamilyError("admitted period-family catalog differs from complete original sources")
    by_ref = {g["family_ref"]: g for g in rebuilt["groups"]}
    if (not isinstance(selections, list) or not selections
            or len(selections) != len(set(selections)) or not isinstance(requirements, list)
            or not requirements):
        raise PeriodSourceFamilyError("period-family selection and complete history requirements are required")
    selected = []
    for ref in selections:
        if ref not in by_ref:
            raise PeriodSourceFamilyError("selected family is foreign, stale or not complete")
        selected.append(by_ref[ref])
    required = {}
    for r in requirements:
        if (set(r) != set(SOURCE_FIELDS) | {"axis", "members"}
                or not all(source_key(r)) or not isinstance(r["members"], list)
                or len(r["members"]) < 2 or len(r["members"]) != len(set(r["members"]))):
            raise PeriodSourceFamilyError("required period-family scope is incomplete")
        key = source_key(r) + (r["axis"], tuple(sorted(r["members"])))
        if key in required:
            raise PeriodSourceFamilyError("required period-family scope repeats an original duration")
        required[key] = r
    chosen = {}
    for group in selected:
        key = source_key(group) + (group["axis"], tuple(group["members"]))
        if key not in required or key in chosen:
            raise PeriodSourceFamilyError("selected family changes history scope or ambiguously repeats a period")
        chosen[key] = group
    if set(chosen) != set(required):
        missing = [required[k] for k in sorted(set(required) - set(chosen), key=str)]
        raise PeriodSourceFamilyError("complete historical operand periods missing: " + str(missing))
    body = {"contract_ref": CONTRACT_REF, "company_ref": company_ref,
            "catalog_hash": admitted["content_hash"], "requirements_hash": content_hash(requirements),
            "selections": list(selections), "selected_families": selected}
    return {**body, "content_hash": content_hash(body)}
