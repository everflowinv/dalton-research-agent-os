"""Closed US-GAAP revenue recognition timing, never a company-name heuristic.

The taxonomy's point-in-time / over-time classification is separate from
business, product and geography partitions. Only these exact standard members
on a pure standard axis enter the new source-bound execution rule. Every
consumed original duration still needs its own complete reported total.
"""
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, localcontext

CONTRACT_REF = "forecast-revenue-recognition-timing:0.1"
AXIS = "us-gaap:TimingOfTransferOfGoodOrServiceAxis"
MEMBERS = frozenset({"us-gaap:TransferredAtPointInTimeMember",
                     "us-gaap:TransferredOverTimeMember"})


def declared(spec):
    """Select the new free forecast epoch only for explicitly declared timing."""
    return any(AXIS in ((d.get("basis_identity") or {}).get("dimensions") or {})
               for d in spec.get("revenue_drivers") or [])


def consumed(record):
    return declared((record.get("execution_contract") or {}).get("specification") or {})


def _refuse(message):
    from .forecast_execution_statement_scope_legacy import _refuse
    _refuse("revenue recognition timing: " + message)


def group(rows, *, concept, statement, start, end):
    """Rows are already deduplicated by full original source identity."""
    from .model_forecast_driver import CONCEPT_ROLES, REVENUE
    if statement != "income" or CONCEPT_ROLES.get(concept) != REVENUE or not start or not end:
        _refuse("classification requires a reported duration revenue line")
    totals, parts = [], {}
    for row in rows:
        if (row.get("concept"), row.get("statement"), row.get("period_start"),
                row.get("period_end")) != (concept, statement, start, end):
            continue
        dims = row.get("dimensions")
        if not isinstance(dims, Mapping) or row.get("dimension_count") != len(dims):
            _refuse("full original dimensional metadata is missing")
        if not dims and not row.get("dimension_axis") and not row.get("is_breakdown"):
            totals.append(row)
        elif set(dims) == {AXIS}:
            member = dims[AXIS]
            if member not in MEMBERS or member in parts:
                _refuse("classification has an unsupported or duplicate standard member")
            parts[member] = row
    if len(totals) != 1 or set(parts) != MEMBERS:
        _refuse("each consumed source period requires both standard members and its own consolidated total")
    selected = [totals[0], *parts.values()]
    sources = {(r.get("ingest_id"), r.get("statement_context_ref")) for r in selected}
    if len(sources) != 1 or not all(next(iter(sources))):
        _refuse("classification mixes or lacks original source identities")
    units = {str(r.get("unit") or "").casefold() for r in selected}
    from .company_financial_statement_structure import _is_currency_unit
    if len(units) != 1 or not _is_currency_unit(next(iter(units))):
        _refuse("classification mixes or lacks original units")
    try:
        values = [Decimal(str(r["value"])) for r in selected]
        if not all(v.is_finite() and abs(v.adjusted()) <= 1000
                   and abs(v.as_tuple().exponent) <= 1000 for v in values):
            raise InvalidOperation
        with localcontext() as ctx:
            ctx.prec = max(80, max(len(v.as_tuple().digits) for v in values) + 2100)
            exact = sum(values[1:], Decimal(0)) == values[0]
    except (InvalidOperation, ValueError, KeyError):
        _refuse("classification contains an invalid original amount")
    if not exact:
        _refuse("both standard members do not equal this original consolidated total")
    return {"line": concept, "period": f"{start}..{end}", "axis": AXIS,
            "total": totals[0]["value"],
            "parts": [{"member": member, "value": parts[member]["value"]}
                      for member in sorted(MEMBERS)]}
