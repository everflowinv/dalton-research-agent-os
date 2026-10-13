"""Arithmetic views of exact source families; original evidence stays intact."""
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from .store import content_hash


def _refuse(message):
    from .forecast_execution_statement_scope_legacy import _refuse as refuse
    refuse(message)


def partitions(record, rows, families):
    from .economic_invariants import (segment_groups, ADDITIVE_SEGMENT_AXES,
        CONSOLIDATION_ITEMS_AXES, OPERATING_SEGMENT_MEMBERS, RECONCILING_MEMBERS)
    drivers = {d["ref"]: d for d in record.get("drivers") or []}
    requests = {}
    for family in families:
        axis, fixed = family["axis"], family["fixed_dimensions"]
        driver = drivers.get(family["driver_ref"])
        identity = (driver or {}).get("statement_identity") or {}
        dims = identity.get("dimensions") or {}
        if (axis not in ADDITIVE_SEGMENT_AXES or axis not in dims
                or {a: m for a, m in dims.items() if a != axis} != fixed
                or not all(a in CONSOLIDATION_ITEMS_AXES and m in OPERATING_SEGMENT_MEMBERS
                           for a, m in fixed.items())
                or identity.get("statement", (driver or {}).get("statement")) != family["statement"]
                or (driver or {}).get("concept") != family["concept"]):
            _refuse("family does not bind its complete executed driver identity")
        key = tuple(family[k] for k in ("ingest_id", "statement_context_ref", "statement",
                    "concept", "period_start", "period_end", "axis")) + (tuple(sorted(fixed.items())),)
        requests.setdefault(key, []).append(family)
    out = []
    for key, consumers in sorted(requests.items(), key=lambda item: str(item[0])):
        ingest, context, statement, concept, start, end, axis, fixed_items = key
        fixed = dict(fixed_items)
        eligible = []
        for row in rows:
            if (row.get("ingest_id"), row.get("statement_context_ref"), row.get("statement"),
                    row.get("period_start"), row.get("period_end")) != (ingest, context, statement, start, end):
                continue
            dims = row.get("dimensions")
            if not isinstance(dims, Mapping) or row.get("dimension_count") != len(dims):
                _refuse("source family lacks complete dimensional metadata")
            total = not dims and not row.get("dimension_axis") and not row.get("is_breakdown")
            peer = (set(dims) == {axis, *fixed} and axis in dims
                    and all(dims.get(a) == m for a, m in fixed.items()))
            bridge = (len(dims) == 1 and any(a in CONSOLIDATION_ITEMS_AXES and m in RECONCILING_MEMBERS
                                          for a, m in dims.items()))
            if total or peer or bridge:
                eligible.append((row, peer))
        # Match full identity before comparing amounts. Conflicts cannot choose
        # a convenient rendering or borrow a matching amount from another source.
        atomic = {}
        for row, peer in sorted(eligible, key=lambda item: str(item[0]["line_id"])):
            identity = (row["concept"], tuple(sorted(row["dimensions"].items())))
            try:
                value = Decimal(str(row["value"]))
                if not value.is_finite():
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                _refuse("source family contains a non-numeric amount")
            unit = str(row.get("unit") or "").casefold()
            if not unit:
                _refuse("source family lacks its unit")
            if identity in atomic:
                previous, _, refs = atomic[identity]
                if Decimal(str(previous["value"])) != value or str(previous["unit"]).casefold() != unit:
                    _refuse("duplicate complete source identity has conflicting value or unit")
                refs.append(str(row["line_id"]))
            else:
                atomic[identity] = (row, peer, [str(row["line_id"])])
        arithmetic, origins = [], []
        for row, peer, refs in atomic.values():
            projected = dict(row)
            if peer:
                # An arithmetic-only view of a supported fixed marker. Actual
                # source dimensions remain untouched in rows and origins.
                projected.update(dimensions={axis: row["dimensions"][axis]}, dimension_count=1,
                                 dimension_axis=axis, dimension_member=row["dimensions"][axis], is_breakdown=True)
            arithmetic.append(projected)
            origins.append({"concept": row["concept"], "dimensions": dict(row["dimensions"]),
                "value": row["value"], "unit": row["unit"], "statement_line_refs": sorted(refs)})
        matches = [g for g in segment_groups(arithmetic) if g["line"] == concept
                   and g["period"] == f"{start}..{end}" and g["axis"] == axis]
        if len(matches) != 1:
            _refuse("a consumed complete family lacks its own consolidated partition")
        group = matches[0]
        members = sorted(p["member"] for p in group["parts"])
        if any(f["members"] != members for f in consumers):
            _refuse("complete family members differ from the retained source scope")
        entry = {"source_identity": [ingest, context, statement], "concept": concept,
                 "period_start": start, "period_end": end, "axis": axis,
                 "fixed_dimensions": fixed, "driver_refs": sorted(f["driver_ref"] for f in consumers),
                 "origins": sorted(origins, key=str), "group": group}
        out.append({**entry, "content_hash": content_hash(entry)})
    return out
