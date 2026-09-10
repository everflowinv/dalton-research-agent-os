"""P13-M3: our numbers against the street's, in the shape the conviction call reads.

The bridge is three lines of arithmetic and one hard rule. The arithmetic is
ours minus theirs, per metric per period. The rule is that **a street number is
never invented**: when nobody has told us where consensus is, the bridge says
``unavailable`` with the reason, and it says it loudly, because a section that
quietly disappeared when the source was missing would read as "we agree with
the street" -- which is the one thing this object must never accidentally say.

Three things follow from that rule and they are the whole design:

* **The reader is resolved by name, not imported.** ``latest_consensus`` and
  ``report_consensus`` are looked up on ``consensus_estimate`` at call time.
  That was written while P11b was on another branch; it has since landed, and
  the lookup is still the right shape -- an older Core without the module gets
  an honest ``unavailable`` rather than an ImportError at start-up, and this
  module does not need to be edited when that one changes its internals. The
  same trick P15d already plays, for the same reason.
* **Where the authority has already done the comparison, it is not done
  twice.** P11b's ``latest_consensus`` returns the finished gap row with
  ``ours`` on it, read from the same ``ForecastModelAuthority`` this projection
  is a projection of. Those rows are validated and passed through. Computing
  ``ours`` again here would be a second implementation of one number that
  agrees today -- exactly what this file refuses to do about the validator.
* **The shape is P15d's, exactly.** ``conviction_call.validate_consensus_gap``
  is the validator, imported rather than re-implemented, so there is one
  definition of what a bridge is. A second validator that agreed today would
  disagree in six months and the disagreement would surface as a refused
  conviction call after two model calls had been paid for.
* **Two brokers or nothing.** When the vendor line has no number for a metric,
  ``report_consensus`` may stand in -- but only from at least two brokers, as a
  range with its midpoint. One broker is not consensus, it is one broker, and
  calling it "the street" is how a variant view gets manufactured out of a
  single note.

The gap row carries ``gap_percent`` because that is what the conviction call's
risk/reward standards are written in. The absolute gap is kept too, in the
projection's ``bridge_detail``, because a two percent gap on revenue and a two
percent gap on EPS are not the same amount of disagreement -- but it is kept
*beside* the validated rows rather than inside them, so the validated shape
stays byte-for-byte what P15d closed over.
"""

from __future__ import annotations

import inspect
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Any, Mapping, Sequence

from .conviction_call import (
    ConvictionCallValidationError,
    validate_consensus_gap,
)
from .store import content_hash

#: The metrics a bridge may carry, in the order they are built. Closed, because
#: "our number against theirs" is only meaningful where both sides mean the
#: same thing by the word, and an open list invites a row comparing our revenue
#: with their bookings.
BRIDGE_METRICS: tuple[str, ...] = ("revenue", "eps", "target_price")

#: Which result line of the driver model each metric is ours from. ``eps`` and
#: ``target_price`` are absent on purpose and say so: the driver model has no
#: share count (P13-M2 open question 5), and a price target is the valuation
#: layer's, not the model's.
OURS_FROM: dict[str, str | None] = {
    "revenue": "result:revenue",
    "eps": None,
    "target_price": None,
}

MIN_BROKERS = 2
#: The conviction call refuses a bridge with more rows than this, so a bridge
#: that would be refused is trimmed here -- deterministically, best-covered
#: metric first and then by period -- rather than built and thrown away.
MAX_METRICS = 12

_PERCENT_QUANT = Decimal("0.0001")
_PRECISION = 60


class ConsensusBridgeError(RuntimeError):
    """The bridge could not be built, and why."""


def validate_bridge(value: Any, name: str = "consensus_bridge") -> dict[str, Any]:
    """The one validator, shared with P15d.

    Re-exported rather than reimplemented. Both sides of this contract are in
    this repository and there is no version of "two validators that agree" that
    stays true.
    """

    try:
        return validate_consensus_gap(value, name)
    except ConvictionCallValidationError as exc:
        raise ConsensusBridgeError(str(exc)) from exc


def unavailable(reason: str) -> dict[str, Any]:
    """The honest answer, in the validated shape."""

    return {"status": "unavailable", "reason": str(reason), "metrics": []}


# ---------------------------------------------------------------------------
# reading the street
# ---------------------------------------------------------------------------


def _wants_store(reader: Any) -> bool:
    """Whether this reader takes the store as its first argument.

    Decided by looking at the signature rather than by calling and catching
    ``TypeError``. A ``TypeError`` raised *inside* the reader -- comparing a
    string with a Decimal, say -- is indistinguishable from an arity mismatch
    at the call site, and the retry would then call a working reader a second
    time with the wrong arguments and report the second failure. Signatures are
    a fact; exceptions from a call are a guess.

    Unreadable signatures (a C function, a mock) default to taking the store,
    which is what P11b's ``latest_consensus(store, company_ref)`` and
    ``report_consensus(store, company)`` both do.
    """

    try:
        names = list(inspect.signature(reader).parameters)
    except (TypeError, ValueError):
        return True
    if not names:
        return False
    first = names[0]
    if first in ("self", "cls") and len(names) > 1:
        first = names[1]
    return first in ("store", "core", "connection", "db")


def _module() -> Any | None:
    try:
        from . import consensus_estimate  # type: ignore[attr-defined]
    except ImportError:
        return None
    return consensus_estimate


def read_consensus(store: Any, company_ref: str) -> dict[str, Any]:
    """Whatever the consensus authority holds for this company, or why not.

    Every failure degrades to ``unavailable`` with a reason rather than
    raising. A street we cannot read and a street that does not exist are the
    same fact for a reader, and both are better than a lane that dies on a
    module it was designed to work without.
    """

    module = _module()
    if module is None:
        return {"status": "unavailable", "payload": None,
                "reason": "this Core carries no consensus authority module, "
                          "so there is nothing to bridge to"}
    reader = getattr(module, "latest_consensus", None)
    if reader is None:
        return {"status": "unavailable", "payload": None,
                "reason": "the consensus module on this Core exposes no "
                          "latest_consensus reader"}
    # The blueprint names this reader ``latest_consensus(company)``; P11b and
    # the conviction call both spell it ``(store, company_ref)``. Read off the
    # signature, so a reader written either way works and neither is guessed.
    try:
        found = (reader(store, company_ref) if _wants_store(reader)
                 else reader(company_ref))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "payload": None,
                "reason": f"the consensus authority could not be read: "
                          f"{type(exc).__name__}: {exc}"}
    if not found:
        return {"status": "unavailable", "payload": None,
                "reason": f"no consensus estimate is held for {company_ref}"}
    return {"status": "available", "payload": found, "reason": None}


def report_consensus(store: Any, company_ref: str, metric: str,
                     period: str) -> dict[str, Any]:
    """The two-broker range, for when the vendor line has no number.

    A range and its midpoint, with both brokers' refs. Fewer than
    ``MIN_BROKERS`` distinct brokers is refused: one note is not the street,
    and a variant view built against one analyst is a disagreement with a
    person rather than with a market. P11b enforces the same rule one layer
    down -- ``street_estimate.report_consensus`` counts distinct houses, never
    notes -- so this is a second lock on the same door rather than the only
    one, which is how it should be for the number that decides whether we hold
    a variant view.

    ``metric`` and ``period`` are the caller's question, not the reader's
    arguments: P11b's reader is ``report_consensus(store, company_ref, *,
    as_of)`` and answers for target price only. So the question is checked
    here, and a caller asking about anything else is told this reader cannot
    answer it rather than being handed a target price under another name.
    """

    module = _module()
    reader = None if module is None else getattr(module, "report_consensus", None)
    if reader is None:
        return {"status": "unavailable", "value": None, "refs": [],
                "reason": "no broker-note consensus reader is installed on this Core"}
    if metric != "target_price":
        return {"status": "unavailable", "value": None, "refs": [],
                "reason": f"the broker-note reader publishes target prices, not "
                          f"{metric}, so it cannot stand in for the missing "
                          f"{metric} number"}
    try:
        points = (reader(store, company_ref) if _wants_store(reader)
                  else reader(company_ref))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "value": None, "refs": [],
                "reason": f"the broker-note reader could not be read: "
                          f"{type(exc).__name__}: {exc}"}
    rows = [dict(item) for item in (points or []) if isinstance(item, Mapping)]
    # One list, and the count is taken off it. Counting houses over every row
    # and numbers over the valued ones was two counts that could both pass on
    # different rows: two notes from Alpha plus a Beta row with no number gave
    # "two brokers, two numbers" and published a range whose ends were both
    # Alpha's. The only rows that count are the ones that carry a house *and* a
    # number, because that pair is what a broker point is.
    numbered = [item for item in rows
                if item.get("value") is not None and item.get("broker")]
    brokers = {str(item["broker"]) for item in numbered}
    if len(brokers) < MIN_BROKERS:
        return {"status": "unavailable", "value": None, "refs": [],
                "reason": f"only {len(brokers)} broker(s) carry a {metric} number "
                          f"for {period}; {MIN_BROKERS} are needed before a range "
                          "may be called consensus"}
    try:
        with localcontext() as ctx:
            ctx.prec = _PRECISION
            values = sorted(Decimal(str(item["value"])) for item in numbered)
            low, high = values[0], values[-1]
            if not (low.is_finite() and high.is_finite()):
                raise ArithmeticError("a broker number is not finite")
            mid = (low + high) / Decimal(2)
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "value": None, "refs": [],
                "reason": f"the broker numbers could not be read as decimals: "
                          f"{type(exc).__name__}: {exc}"}
    refs = sorted({str(ref) for item in numbered for ref in (item.get("refs") or [])})
    return {
        "status": "available", "reason": None,
        "value": format(mid, "f"), "low": format(low, "f"), "high": format(high, "f"),
        "brokers": sorted(brokers), "basis": "report_consensus",
        "refs": refs,
    }


def consensus_fingerprint(consensus: Mapping[str, Any]) -> str | None:
    """What the street currently says, hashed, so the lane can see it move.

    ``None`` when there is no street: a company with no consensus has nothing
    that could change, and a fingerprint of the *reason* would make an
    unreadable authority look like news every time its error message differed.
    """

    if consensus.get("status") != "available":
        return None
    return content_hash({"consensus": consensus.get("payload")})


# ---------------------------------------------------------------------------
# building the bridge
# ---------------------------------------------------------------------------


def _ours_revenue(record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Our revenue estimate per open quarter, with the cell it came from."""

    from .forecast_sensitivity import open_periods

    ends = {str(item["end"]) for item in open_periods(record)}
    line = next((item for item in (record.get("results") or [])
                 if str(item["ref"]) == OURS_FROM["revenue"]), None)
    if line is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for cell in line.get("cells") or []:
        end = str(cell["period"]["end"])
        if end not in ends or cell.get("kind") != "estimate":
            continue
        if cell.get("superseded_by") or cell.get("status") != "computed":
            continue
        out[end] = {"value": str(cell["value"]), "ref": str(cell["ref"]),
                    "unit": str(record.get("currency") or "USD")}
    return out


def _rows_of(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, Mapping):
        rows = payload.get("metrics")
    else:
        rows = payload
    if not isinstance(rows, (list, tuple)):
        return []
    return [dict(item) for item in rows if isinstance(item, Mapping)]


def _already_gap_rows(rows: Sequence[Mapping[str, Any]]) -> bool:
    """Whether the authority already did the comparison.

    P11b's ``latest_consensus`` does not return "the street's number"; it
    returns the finished gap row, ``ours`` included, and its ``ours`` comes
    from ``ForecastModelAuthority.latest`` -- the same driver model this
    projection is a projection of. There is nothing left for this module to
    compute, and recomputing it would be a second implementation of one number
    that agrees today: precisely the thing this file refuses to do about the
    validator, and there is no reason to do it about the arithmetic either.

    A pre-P11b feed that hands over ``{metric, period, value}`` is still
    understood and still gets ``ours`` attached here. The two shapes are told
    apart by whether every row already carries both sides.
    """

    return bool(rows) and all(
        row.get("ours") is not None and row.get("gap_percent") is not None
        for row in rows)


def _pass_through(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The authority's own gap rows, validated and given their absolute gaps.

    Validated rather than trusted, because the shape arrives across a name
    lookup from a module this one does not import; a bad row discovered here is
    an ``unavailable`` with a reason, and discovered later is a conviction call
    refused after two model calls were paid for.
    """

    kept = [{"metric": str(row.get("metric")), "period": str(row.get("period")),
             "ours": str(row.get("ours")), "consensus": str(row.get("consensus")),
             "unit": str(row.get("unit") or "unit"),
             "gap_percent": str(row.get("gap_percent")),
             "refs": [str(item) for item in (row.get("refs") or [])]}
            for row in rows][:MAX_METRICS]
    try:
        bridge = validate_bridge({"status": "available", "reason": None,
                                  "metrics": kept})
    except ConsensusBridgeError as exc:
        return {"bridge": unavailable(
            f"the consensus authority returned a shape this Core cannot read: "
            f"{exc}"), "detail": []}
    detail = []
    for row in bridge["metrics"]:
        try:
            with localcontext() as ctx:
                ctx.prec = _PRECISION
                absolute = Decimal(row["ours"]) - Decimal(row["consensus"])
            gap_abs = format(absolute, "f")
        except Exception:  # noqa: BLE001 - a row we cannot subtract keeps no gap_abs
            gap_abs = None
        detail.append({**{key: row[key] for key in
                          ("metric", "period", "ours", "consensus", "unit",
                           "gap_percent", "refs")},
                       "basis": "consensus_authority", "gap_abs": gap_abs})
    return {"bridge": bridge, "detail": detail}


def _gap(ours: Decimal, theirs: Decimal) -> tuple[Decimal, Decimal | None]:
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        absolute = ours - theirs
        percent = None if theirs == 0 else absolute / abs(theirs) * Decimal(100)
    return absolute, percent


def build_bridge(
    record: Mapping[str, Any],
    consensus: Mapping[str, Any],
    *,
    store: Any = None,
    valuation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Our estimate against the street's, per metric and period.

    Returns the validated ``consensus_gap`` block and, beside it, the detail
    rows that carry the absolute gap and the source of each street number. The
    two are built together and returned together so they cannot drift: a detail
    row with no validated row beside it would be a number in the report that
    the conviction call never saw.
    """

    detail: list[dict[str, Any]] = []
    if consensus.get("status") != "available":
        return {"bridge": unavailable(str(consensus.get("reason") or
                                          "no consensus was read")),
                "detail": detail}
    rows = _rows_of(consensus.get("payload"))
    if not rows:
        return {"bridge": unavailable(
            f"the consensus authority holds no metric rows for "
            f"{record.get('company_ref')}"), "detail": detail}
    if _already_gap_rows(rows):
        return _pass_through(rows)

    ours_revenue = _ours_revenue(record)
    model_ref = str(record.get("id"))
    metrics: list[dict[str, Any]] = []
    skipped: list[str] = []
    for row in rows:
        metric = str(row.get("metric") or "")
        period = str(row.get("period") or "")
        if metric not in BRIDGE_METRICS:
            skipped.append(f"{metric or '?'}: not a metric this bridge compares")
            continue
        theirs_raw = row.get("value")
        street_refs = [str(item) for item in (row.get("refs") or []) if str(item).strip()]
        basis = "vendor"
        if theirs_raw is None and store is not None:
            fallback = report_consensus(store, str(record.get("company_ref")),
                                        metric, period)
            if fallback["status"] == "available":
                theirs_raw = fallback["value"]
                street_refs = list(fallback["refs"])
                basis = "report_consensus"
            else:
                skipped.append(f"{metric} {period}: {fallback['reason']}")
                continue
        if theirs_raw is None:
            skipped.append(f"{metric} {period}: the consensus row carries no number")
            continue
        ours_row = None
        if metric == "revenue":
            ours_row = ours_revenue.get(period)
            if ours_row is None:
                skipped.append(
                    f"revenue {period}: this model has no computed estimate for "
                    "that quarter")
                continue
        elif metric == "eps":
            # Stated rather than skipped silently: the driver model has no
            # share count, so there is no EPS to compare. P13-M2 open question
            # 5 is the fix, and it is a driver, not a division.
            skipped.append(
                "eps: the driver model carries no diluted share count, so it "
                "has no EPS of its own to compare (P13-M2 open question 5)")
            continue
        else:  # target_price
            if valuation is None:
                skipped.append(
                    "target_price: no valuation snapshot is linked to this "
                    "company, so we hold no price target to compare")
                continue
            ours_row = _valuation_target(valuation)
            if ours_row is None:
                skipped.append(
                    "target_price: the valuation snapshot holds no target price")
                continue
        try:
            ours = Decimal(str(ours_row["value"]))
            theirs = Decimal(str(theirs_raw))
        except Exception:  # noqa: BLE001
            skipped.append(f"{metric} {period}: a number could not be read")
            continue
        if not (ours.is_finite() and theirs.is_finite()):
            skipped.append(f"{metric} {period}: a number was not finite")
            continue
        absolute, percent = _gap(ours, theirs)
        if percent is None:
            skipped.append(
                f"{metric} {period}: consensus is zero, so there is no "
                "percentage gap to state")
            continue
        refs = list(dict.fromkeys(
            [model_ref, str(ours_row["ref"])] + street_refs))
        unit = str(row.get("unit") or ours_row.get("unit") or "USD")
        metrics.append({
            "metric": metric, "period": period,
            "ours": format(ours, "f"), "consensus": format(theirs, "f"),
            "unit": unit,
            "gap_percent": format(percent.quantize(_PERCENT_QUANT, ROUND_HALF_UP), "f"),
            "refs": refs,
        })
        detail.append({
            "metric": metric, "period": period, "basis": basis,
            "ours": format(ours, "f"), "consensus": format(theirs, "f"),
            "unit": unit,
            "gap_abs": format(absolute, "f"),
            "gap_percent": format(percent.quantize(_PERCENT_QUANT, ROUND_HALF_UP), "f"),
            "refs": refs,
        })
    if not metrics:
        return {"bridge": unavailable(
            "no metric could be bridged: " + "; ".join(skipped or ["no reason recorded"])),
            "detail": []}
    metrics.sort(key=lambda item: (BRIDGE_METRICS.index(item["metric"]),
                                   item["period"]))
    detail.sort(key=lambda item: (BRIDGE_METRICS.index(item["metric"]),
                                  item["period"]))
    metrics, detail = metrics[:MAX_METRICS], detail[:MAX_METRICS]
    bridge = validate_bridge({"status": "available", "reason": None,
                              "metrics": metrics})
    return {"bridge": bridge, "detail": detail}


def _valuation_target(valuation: Mapping[str, Any]) -> dict[str, Any] | None:
    """Our price target out of a valuation snapshot, if it holds one."""

    for row in valuation.get("metrics") or []:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("metric")) not in ("target_price", "price_target"):
            continue
        if row.get("value") is None or str(row.get("status") or "computed") != "computed":
            continue
        return {"value": str(row["value"]),
                "ref": str(valuation.get("id") or valuation.get("version_ref")),
                "unit": str(row.get("unit") or valuation.get("currency") or "USD")}
    return None


__all__ = [
    "BRIDGE_METRICS",
    "MAX_METRICS",
    "MIN_BROKERS",
    "OURS_FROM",
    "ConsensusBridgeError",
    "build_bridge",
    "consensus_fingerprint",
    "read_consensus",
    "report_consensus",
    "unavailable",
    "validate_bridge",
]
