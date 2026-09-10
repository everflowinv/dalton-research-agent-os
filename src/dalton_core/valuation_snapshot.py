"""P11c: what a company trades at, computed rather than asserted.

``derived_deterministic``: nothing in a snapshot is a judgement. Given the same
price version, the same share observation and the same filed figures, the same
formula version produces the same *metrics* -- every figure below is replayable
from the refs beside it. Two records of the same inputs are not byte-identical,
because ``created_at`` sits inside the hashed body and no two publications share
a timestamp; what is identical is ``binding_hash``, which is what the duplicate
rule compares and what a replay should be checked against. That is also the
whole reason the formula string is stored in every record rather than living
only in this file: a snapshot published today has to stay reproducible after
this module changes, and a reader has to be able to see *which* arithmetic
produced the number they are looking at.

Three rules this module exists to enforce.

**Every input is a ref.** A price is a version ref plus a bar date. A share
count is a version ref plus the date the observation was made. A filed figure
is a concept, a period and the accession it came from. There is no field in
here that holds a number whose origin is "the caller said so".

**A missing input is a missing metric, never a guess.** If a company does not
report a concept some metric needs, that metric comes back ``unavailable`` with
the reason spelled out. It does not fall back to a peer, an average, or a prior
period, and the reason is a sentence a reader can act on rather than a null.

**Fundamentals come from filings, full stop.** Yahoo serves income statements
too, and this module refuses them by source ref. A P/E built on a scraped
earnings number would look exactly like a P/E built on a filed one, which is
precisely why it can never be allowed to exist: the moment one is admitted,
"every number goes back to a filing" is decoration.

**On the percentile.** A multiple's percentile is only as honest as the inputs
behind the history, and there are two of them. This computes each metric at
every stored bar using the fundamental window that was known as of that bar --
so with one window, the percentile is driven entirely by price, and the record
says ``basis: price_only``. Share count is the second: without a dated share
history, every historical market cap is built from *today's* share count, which
for a company that has been buying back stock overstates the past. That is
recorded as ``shares_basis: current_shares_applied_to_history``, and passing
``share_history`` in turns it into ``dated_shares``. Both limitations are
stated in the record rather than hidden by it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any

from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
ACTOR_REF = "core:valuation-snapshot-worker"

# The frozen arithmetic. This string is written into every record: a snapshot
# published under 0.1 stays readable as 0.1 arithmetic no matter what this
# module does later, and two snapshots under different formula versions are
# never compared as if they were the same measurement.
FORMULA_VERSION = "valuation-formula:p11c:0.1"

# Where a filed figure may come from. Yahoo is not on this list and will not be:
# see the module docstring.
ALLOWED_FUNDAMENTAL_SOURCES = frozenset({"source:sec-edgar"})

# How many quarters make a trailing-twelve-month figure. Not four "or so": a
# window with three quarters in it is a nine-month figure being read as a year,
# which understates every multiple built on it.
TRAILING_QUARTERS = 4

# What each role is, and how its components combine. A flow is summed over the
# trailing year; a balance is a single instant. Reading one as the other is the
# most common way a multiple comes out wrong by a factor of four.
ROLE_AGGREGATION: Mapping[str, str] = {
    "net_income": "trailing_sum",
    "revenue": "trailing_sum",
    "operating_income": "trailing_sum",
    "depreciation_amortisation": "trailing_sum",
    "operating_cash_flow": "trailing_sum",
    "capital_expenditure": "trailing_sum",
    "total_debt": "instant",
    "cash_and_equivalents": "instant",
}
# Capital expenditure is filed as a positive outflow (``PaymentsTo...``) and is
# subtracted by the formula. A caller that hands it over already negated would
# double the free cash flow, so the sign convention is checked rather than
# assumed.
NON_NEGATIVE_ROLES = frozenset({
    "revenue", "total_debt", "cash_and_equivalents", "capital_expenditure",
})

METRIC_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "metric": "trailing_pe",
        "label": "trailing price / earnings",
        "unit": "ratio",
        "roles": ("net_income",),
        "formula": "market_cap / net_income_ttm",
        "quantum": "0.0001",
    },
    {
        "metric": "price_to_sales",
        "label": "price / sales",
        "unit": "ratio",
        "roles": ("revenue",),
        "formula": "market_cap / revenue_ttm",
        "quantum": "0.0001",
    },
    {
        "metric": "ev_to_ebitda",
        "label": "enterprise value / EBITDA",
        "unit": "ratio",
        "roles": (
            "operating_income", "depreciation_amortisation",
            "total_debt", "cash_and_equivalents",
        ),
        "formula": (
            "(market_cap + total_debt - cash_and_equivalents) / "
            "(operating_income_ttm + depreciation_amortisation_ttm)"
        ),
        "quantum": "0.0001",
    },
    {
        "metric": "fcf_yield",
        "label": "free cash flow yield",
        "unit": "fraction",
        "roles": ("operating_cash_flow", "capital_expenditure"),
        "formula": "(operating_cash_flow_ttm - capital_expenditure_ttm) / market_cap",
        "quantum": "0.000001",
    },
)
METRICS = tuple(item["metric"] for item in METRIC_DEFINITIONS)
# Metrics whose denominator has to be positive for the ratio to mean anything.
# A negative P/E is not a cheap company; it is a loss-making one, and printing
# "-14.2x" invites a reader to compare it with 14.2x.
_POSITIVE_DENOMINATOR_REASON = {
    "trailing_pe": "trailing net income is not positive, so a P/E would not be a multiple",
    "price_to_sales": "trailing revenue is not positive",
    "ev_to_ebitda": "trailing EBITDA is not positive, so EV/EBITDA would not be a multiple",
}

# The percentile, frozen. "What share of the stored history sat at or below
# today" -- one definition, written into every record, so two snapshots are
# never compared under two different meanings of the same word.
PERCENTILE_METHOD = "share_of_history_at_or_below"
# Below this many usable bars a percentile is noise wearing a number's clothes.
MIN_PERCENTILE_SAMPLE = 30

MAX_METRIC_INPUT_COMPONENTS = 8
MAX_FUNDAMENTAL_WINDOWS = 40

_SCHEMA_PATH = Path(__file__).with_name("valuation_snapshot_schema.sql")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_ACCESSION_RE = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")


class ValuationSnapshotError(RuntimeError):
    """Base error for the valuation snapshot authority."""


class ValuationSnapshotValidationError(ValuationSnapshotError, ValueError):
    """A request does not satisfy the closed contract."""


class ValuationSnapshotConflict(ValuationSnapshotError):
    """A request conflicts with the immutable chain."""


class ValuationSnapshotNotFound(ValuationSnapshotError):
    """No such snapshot or version."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValuationSnapshotValidationError(f"{name} must be non-empty text")
    return value.strip()


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise ValuationSnapshotValidationError(f"{name} must be lowercase SHA-256")
    return value


def _iso_date(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValuationSnapshotValidationError(f"{name} must be YYYY-MM-DD") from exc
    return value


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValuationSnapshotValidationError(
            f"{name} must be a canonical decimal string"
        )
    if _DECIMAL_RE.fullmatch(value) is None:
        raise ValuationSnapshotValidationError(
            f"{name} must be a canonical decimal string"
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValuationSnapshotValidationError(f"{name} must be Decimal-compatible") from exc
    if not parsed.is_finite():
        raise ValuationSnapshotValidationError(f"{name} must be finite")
    return parsed


def _format(value: Decimal) -> str:
    if value == 0:
        return "0"
    raw = format(value, "f")
    if "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    return raw or "0"


def _quantise(value: Decimal, quantum: str) -> str:
    return _format(value.quantize(Decimal(quantum), rounding=ROUND_HALF_EVEN))


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValuationSnapshotValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) != fields:
        raise ValuationSnapshotValidationError(
            f"{name} has invalid closed shape; "
            f"missing={sorted(fields - set(wire))}, unknown={sorted(set(wire) - fields)}"
        )
    return wire


def _record(base: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(base)
    wire["content_hash"] = content_hash(wire)
    return wire


def _ref(prefix: str, identity: Mapping[str, Any]) -> str:
    return f"{prefix}:{content_hash(identity)[:32]}"


def snapshot_ref_for(company_ref: str) -> str:
    return f"valuation-snapshot:{_text(company_ref, 'company_ref')}"


def _decode(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
    if row is None:
        raise ValuationSnapshotNotFound(name)
    try:
        wire = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValuationSnapshotConflict(f"{name} record_json is invalid") from exc
    if not isinstance(wire, dict) or canonical_json(wire) != row["record_json"]:
        raise ValuationSnapshotConflict(f"{name} record_json is not canonical")
    body = dict(wire)
    asserted = body.pop("content_hash", None)
    if asserted != content_hash(body) or asserted != row["content_hash"]:
        raise ValuationSnapshotConflict(f"{name} content hash drifted")
    if wire.get("id") != row["version_id"]:
        raise ValuationSnapshotConflict(f"{name} identity column drifted")
    checks = {
        "snapshot_ref": wire.get("snapshot_ref"),
        "version_number": wire.get("version"),
        "prior_version_id": wire.get("prior_version_ref"),
        "company_ref": wire.get("company_ref"),
        "formula_version": wire.get("formula_version"),
        "as_of": wire.get("as_of"),
        "actor_ref": wire.get("actor_ref"),
        "created_at": wire.get("created_at"),
    }
    for column, expected in checks.items():
        if column in row.keys() and row[column] != expected:
            raise ValuationSnapshotConflict(f"{name} {column} drifted")
    return wire


# -- inputs ----------------------------------------------------------------


def _price_binding(value: Any) -> tuple[dict[str, Any], Decimal]:
    wire = _closed(
        value,
        {
            "version_ref", "version_hash", "bar_date", "close", "currency",
            "invocation_ref", "artifact_hash",
        },
        "price",
    )
    wire["version_ref"] = _text(wire["version_ref"], "price.version_ref")
    wire["version_hash"] = _hash(wire["version_hash"], "price.version_hash")
    wire["bar_date"] = _iso_date(wire["bar_date"], "price.bar_date")
    close = _decimal(wire["close"], "price.close")
    if close <= 0:
        raise ValuationSnapshotConflict("a close of zero or less is not a price")
    wire["currency"] = _text(wire["currency"], "price.currency").upper()
    wire["invocation_ref"] = _text(wire["invocation_ref"], "price.invocation_ref")
    wire["artifact_hash"] = _hash(wire["artifact_hash"], "price.artifact_hash")
    return wire, close


def _shares_binding(value: Any) -> tuple[dict[str, Any], Decimal]:
    wire = _closed(
        value,
        {
            "version_ref", "version_hash", "as_of", "shares_outstanding",
            "invocation_ref", "artifact_hash",
        },
        "shares",
    )
    wire["version_ref"] = _text(wire["version_ref"], "shares.version_ref")
    wire["version_hash"] = _hash(wire["version_hash"], "shares.version_hash")
    wire["as_of"] = _iso_date(wire["as_of"], "shares.as_of")
    shares = _decimal(wire["shares_outstanding"], "shares.shares_outstanding")
    if shares <= 0:
        raise ValuationSnapshotConflict("a share count of zero or less is not a count")
    wire["invocation_ref"] = _text(wire["invocation_ref"], "shares.invocation_ref")
    wire["artifact_hash"] = _hash(wire["artifact_hash"], "shares.artifact_hash")
    return wire, shares


def _role_input(
    role: str, value: Any, name: str, *, currency: str
) -> tuple[dict[str, Any], Decimal]:
    wire = _closed(
        value,
        {"concept", "statement", "unit", "source_ref", "components"},
        name,
    )
    aggregation = ROLE_AGGREGATION[role]
    # Every role here is money, and the market capitalisation it will be
    # divided into is in the price's currency. A filer reporting in thousands,
    # or a foreign issuer reporting in euros, would otherwise produce a
    # price/sales a thousand times too small with nothing on the record to say
    # so -- the units are checked because a wrong multiple is worse than an
    # unavailable one.
    unit = _text(wire["unit"], f"{name}.unit")
    if unit != currency:
        raise ValuationSnapshotConflict(
            f"{name} is reported in {unit} and the price is in {currency}; "
            "a multiple across two units is not a multiple"
        )
    source_ref = _text(wire["source_ref"], f"{name}.source_ref")
    if source_ref not in ALLOWED_FUNDAMENTAL_SOURCES:
        # The one refusal this whole module is built around.
        raise ValuationSnapshotConflict(
            f"{name} comes from {source_ref}; a filed figure has one primary "
            "source and a market-data feed is not it"
        )
    components = wire["components"]
    if not isinstance(components, (list, tuple)) or not components:
        raise ValuationSnapshotValidationError(f"{name}.components must not be empty")
    if len(components) > MAX_METRIC_INPUT_COMPONENTS:
        raise ValuationSnapshotValidationError(f"{name}.components is too long")
    expected = TRAILING_QUARTERS if aggregation == "trailing_sum" else 1
    if len(components) != expected:
        raise ValuationSnapshotConflict(
            f"{name} is a {aggregation} role and needs exactly {expected} "
            f"component(s), not {len(components)}"
        )
    rows: list[dict[str, Any]] = []
    total = Decimal(0)
    seen: set[tuple[str | None, str]] = set()
    for index, raw in enumerate(components):
        component = _closed(
            raw,
            {"period_start", "period_end", "value", "accession"},
            f"{name}.components[{index}]",
        )
        start = component["period_start"]
        if start is not None:
            start = _iso_date(start, f"{name}.components[{index}].period_start")
        end = _iso_date(component["period_end"], f"{name}.components[{index}].period_end")
        if aggregation == "trailing_sum" and start is None:
            raise ValuationSnapshotConflict(
                f"{name} is a flow; a component with no period_start is an "
                "instant and cannot be summed into a trailing year"
            )
        if aggregation == "instant" and start is not None:
            raise ValuationSnapshotConflict(
                f"{name} is a balance; a component with a period_start is a "
                "flow and is not a point in time"
            )
        if (start, end) in seen:
            raise ValuationSnapshotConflict(
                f"{name} counts the same period twice"
            )
        seen.add((start, end))
        accession = _text(component["accession"], f"{name}.components[{index}].accession")
        if _ACCESSION_RE.fullmatch(accession) is None:
            raise ValuationSnapshotValidationError(
                f"{name}.components[{index}].accession must be a canonical SEC accession"
            )
        value_decimal = _decimal(
            component["value"], f"{name}.components[{index}].value"
        )
        total += value_decimal
        rows.append({
            "period_start": start,
            "period_end": end,
            "value": component["value"],
            "accession": accession,
        })
    if role in NON_NEGATIVE_ROLES and total < 0:
        raise ValuationSnapshotConflict(
            f"{name} totals below zero; check the sign convention for {role}"
        )
    rows.sort(key=lambda item: (item["period_end"], item["period_start"] or ""))
    return {
        "role": role,
        "aggregation": aggregation,
        "concept": _text(wire["concept"], f"{name}.concept"),
        "statement": _text(wire["statement"], f"{name}.statement"),
        "unit": unit,
        "source_ref": source_ref,
        "components": rows,
        "value": _format(total),
    }, total


def _fundamental_windows(value: Any, *, currency: str) -> list[dict[str, Any]]:
    """Filed figures, dated by when they became knowable.

    ``as_of`` is the filing date, not the period end: a March quarter is not
    something the market knew in March. Using the period end here is how a
    percentile ends up quietly forward-looking.
    """

    if not isinstance(value, (list, tuple)) or not value:
        raise ValuationSnapshotValidationError(
            "fundamental_windows must carry at least one window"
        )
    if len(value) > MAX_FUNDAMENTAL_WINDOWS:
        raise ValuationSnapshotValidationError("fundamental_windows is too long")
    windows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        window = _closed(raw, {"as_of", "roles"}, f"fundamental_windows[{index}]")
        as_of = _iso_date(window["as_of"], f"fundamental_windows[{index}].as_of")
        if as_of in seen:
            raise ValuationSnapshotConflict(
                f"two fundamental windows are dated {as_of}"
            )
        seen.add(as_of)
        roles = window["roles"]
        if not isinstance(roles, Mapping):
            raise ValuationSnapshotValidationError(
                f"fundamental_windows[{index}].roles must be an object"
            )
        unknown = sorted(set(roles) - set(ROLE_AGGREGATION))
        if unknown:
            raise ValuationSnapshotValidationError(
                f"fundamental_windows[{index}].roles names roles no metric uses: {unknown}"
            )
        normalised: dict[str, Any] = {}
        totals: dict[str, Decimal] = {}
        for role in sorted(roles):
            normalised[role], totals[role] = _role_input(
                role, roles[role], f"fundamental_windows[{index}].roles.{role}",
                currency=currency,
            )
        windows.append({"as_of": as_of, "roles": normalised, "_totals": totals})
    windows.sort(key=lambda item: item["as_of"])
    return windows


# -- arithmetic ------------------------------------------------------------


def _metric_value(
    definition: Mapping[str, Any], market_cap: Decimal, totals: Mapping[str, Decimal]
) -> tuple[Decimal | None, str | None]:
    """One metric under the frozen formula, or why it could not be computed."""

    metric = definition["metric"]
    if metric == "trailing_pe":
        denominator = totals["net_income"]
    elif metric == "price_to_sales":
        denominator = totals["revenue"]
    elif metric == "ev_to_ebitda":
        denominator = totals["operating_income"] + totals["depreciation_amortisation"]
    elif metric == "fcf_yield":
        numerator = totals["operating_cash_flow"] - totals["capital_expenditure"]
        return numerator / market_cap, None
    else:  # pragma: no cover - METRIC_DEFINITIONS is closed
        raise ValuationSnapshotError(f"no arithmetic for {metric}")
    if denominator <= 0:
        return None, _POSITIVE_DENOMINATOR_REASON[metric]
    if metric == "ev_to_ebitda":
        numerator = market_cap + totals["total_debt"] - totals["cash_and_equivalents"]
    else:
        numerator = market_cap
    return numerator / denominator, None


def _percentile(sample: Sequence[Decimal], current: Decimal) -> str:
    at_or_below = sum(1 for value in sample if value <= current)
    return _quantise(
        Decimal(at_or_below) * Decimal(100) / Decimal(len(sample)), "0.01"
    )


def compute_metrics(
    *,
    close: Decimal,
    shares: Decimal,
    as_of: str,
    windows: Sequence[Mapping[str, Any]],
    bars: Sequence[Mapping[str, Any]],
    share_history: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every metric at ``as_of``, and where each sits in its own history.

    The history is built the only honest way available: at each stored bar, the
    metric is recomputed with the fundamental window -- and, when one is given,
    the share observation -- that was already known on that date. A bar earlier
    than the first window has no fundamentals behind it and is left out rather
    than back-filled with figures nobody had yet.
    """

    market_cap = close * shares
    dated_shares = sorted(
        ((row["as_of"], Decimal(row["shares_outstanding"])) for row in share_history),
        key=lambda item: item[0],
    )

    def window_for(bar_date: str) -> Mapping[str, Decimal] | None:
        chosen: Mapping[str, Decimal] | None = None
        for window in windows:
            if window["as_of"] <= bar_date:
                chosen = window["_totals"]
        return chosen

    def shares_for(bar_date: str) -> Decimal:
        """The share count that was known on that day, or today's if none is.

        Falling back to today's count is a real distortion for a company that
        has been retiring stock -- its past market caps come out too large and
        its past multiples too high. It is done anyway because the alternative
        is no history at all, and it is labelled rather than absorbed.
        """

        chosen = shares
        for observed_on, count in dated_shares:
            if observed_on <= bar_date:
                chosen = count
        return chosen

    shares_basis = "dated_shares" if dated_shares else "current_shares_applied_to_history"

    # Every window dated after the bar being valued means nothing was filed
    # yet. Valuing today's price against next quarter's filing would be the
    # same mistake as a forward-looking percentile, so it is refused rather
    # than reached for.
    current_totals = window_for(as_of)
    no_window_yet = current_totals is None
    if current_totals is None:
        current_totals = {}

    metrics: list[dict[str, Any]] = []
    for definition in METRIC_DEFINITIONS:
        roles = definition["roles"]
        missing = [role for role in roles if role not in current_totals]
        if missing:
            metrics.append({
                "metric": definition["metric"],
                "label": definition["label"],
                "unit": definition["unit"],
                "formula": definition["formula"],
                "status": "unavailable",
                "value": None,
                "reason": (
                    "no filing was on hand as of "
                    f"{as_of}; the earliest fundamentals held are dated "
                    f"{windows[0]['as_of']}"
                    if no_window_yet else
                    "the filings held carry no "
                    + ", ".join(role.replace("_", " ") for role in missing)
                    + " for this company"
                ),
                "inputs": [],
                "percentile": None,
            })
            continue
        value, reason = _metric_value(definition, market_cap, current_totals)
        if value is None:
            metrics.append({
                "metric": definition["metric"],
                "label": definition["label"],
                "unit": definition["unit"],
                "formula": definition["formula"],
                "status": "unavailable",
                "value": None,
                "reason": reason,
                "inputs": list(roles),
                "percentile": None,
            })
            continue
        sample: list[Decimal] = []
        # Which windows the sample actually rested on, not how many were
        # supplied. Handing in eight quarters and then valuing a history that
        # only reaches back into the newest of them is a price-driven
        # percentile wearing the other label.
        windows_used: set[str] = set()
        for bar in bars:
            totals = window_for(bar["date"])
            if totals is None or any(role not in totals for role in roles):
                continue
            for window in windows:
                if window["_totals"] is totals:
                    windows_used.add(window["as_of"])
            bar_cap = Decimal(bar["close"]) * shares_for(bar["date"])
            historical, _reason = _metric_value(definition, bar_cap, totals)
            if historical is not None:
                sample.append(historical)
        percentile: dict[str, Any] | None
        if len(sample) < MIN_PERCENTILE_SAMPLE:
            percentile = {
                "value": None,
                "sample_size": len(sample),
                "method": PERCENTILE_METHOD,
                "basis": None,
                "shares_basis": None,
                "windows_used": len(windows_used),
                "reason": (
                    f"{len(sample)} bars of history behind this metric; a "
                    f"percentile needs at least {MIN_PERCENTILE_SAMPLE}"
                ),
            }
        else:
            percentile = {
                "value": _percentile(sample, value),
                "sample_size": len(sample),
                "method": PERCENTILE_METHOD,
                # The honesty fields. One window means the fundamentals never
                # moved across the history, so the percentile is the price's
                # percentile in multiple clothing; and without dated share
                # observations every past market cap carries today's count.
                "basis": (
                    "price_only" if len(windows_used) < 2
                    else "price_and_filed_fundamentals"
                ),
                "shares_basis": shares_basis,
                "windows_used": len(windows_used),
                "reason": None,
            }
        metrics.append({
            "metric": definition["metric"],
            "label": definition["label"],
            "unit": definition["unit"],
            "formula": definition["formula"],
            "status": "available",
            "value": _quantise(value, definition["quantum"]),
            "reason": None,
            "inputs": list(roles),
            "percentile": percentile,
        })
    basis = {
        "market_cap": _quantise(market_cap, "0.01"),
        "market_cap_formula": "close * shares_outstanding",
        "fundamental_window_count": len(windows),
        "fundamental_window_as_ofs": [window["as_of"] for window in windows],
        "share_observation_count": len(dated_shares),
        "shares_basis": shares_basis,
        "price_history_bars": len(bars),
        "percentile_method": PERCENTILE_METHOD,
        "min_percentile_sample": MIN_PERCENTILE_SAMPLE,
    }
    return metrics, basis


class ValuationSnapshotAuthority:
    """Append-only derived-deterministic valuation snapshots, one chain per company."""

    def __init__(self, store: Any):
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("ValuationSnapshotAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def version(self, version_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM valuation_snapshot_versions WHERE version_id=?",
            (_text(version_ref, "version_ref"),),
        ).fetchone()
        return _decode(row, f"ValuationSnapshotVersion {version_ref}")

    def latest_version(self, company_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM valuation_snapshot_versions WHERE snapshot_ref=? "
            "ORDER BY version_number DESC LIMIT 1",
            (snapshot_ref_for(company_ref),),
        ).fetchone()
        if row is None:
            return None
        return _decode(row, f"latest ValuationSnapshotVersion for {company_ref}")

    def publish_snapshot(
        self,
        *,
        company_ref: str,
        price: Mapping[str, Any],
        shares: Mapping[str, Any],
        fundamental_windows: Sequence[Mapping[str, Any]],
        price_history: Sequence[Mapping[str, Any]] = (),
        share_history: Sequence[Mapping[str, Any]] = (),
        statement_rows: Sequence[Mapping[str, Any]] = (),
        solver_results: Sequence[Mapping[str, Any]] = (),
        actor_ref: str = ACTOR_REF,
    ) -> dict[str, Any]:
        """Compute and store one snapshot, or say it is the same as the last."""

        company_ref = _text(company_ref, "company_ref")
        actor_ref = _text(actor_ref, "actor_ref")
        price_wire, close = _price_binding(price)
        shares_wire, share_count = _shares_binding(shares)
        windows = _fundamental_windows(
            fundamental_windows, currency=price_wire["currency"])

        dated_shares: list[dict[str, str]] = []
        seen_share_dates: set[str] = set()
        for index, raw in enumerate(share_history):
            row = _closed(
                raw, {"as_of", "shares_outstanding"}, f"share_history[{index}]")
            observed_on = _iso_date(row["as_of"], f"share_history[{index}].as_of")
            if observed_on in seen_share_dates:
                raise ValuationSnapshotConflict(
                    f"share_history carries {observed_on} twice")
            seen_share_dates.add(observed_on)
            count = _decimal(
                row["shares_outstanding"],
                f"share_history[{index}].shares_outstanding")
            if count <= 0:
                raise ValuationSnapshotConflict(
                    f"share_history[{index}] is not a positive count")
            dated_shares.append(
                {"as_of": observed_on, "shares_outstanding": _format(count)})
        dated_shares.sort(key=lambda row: row["as_of"])

        bars: list[dict[str, str]] = []
        seen_dates: set[str] = set()
        for index, raw in enumerate(price_history):
            if not isinstance(raw, Mapping):
                raise ValuationSnapshotValidationError(
                    f"price_history[{index}] must be an object"
                )
            bar_date = _iso_date(raw.get("date"), f"price_history[{index}].date")
            if bar_date in seen_dates:
                raise ValuationSnapshotConflict(
                    f"price_history carries {bar_date} twice"
                )
            seen_dates.add(bar_date)
            bar_close = _decimal(raw.get("close"), f"price_history[{index}].close")
            if bar_close <= 0:
                raise ValuationSnapshotConflict(
                    f"price_history[{index}] has a close of zero or less"
                )
            bars.append({"date": bar_date, "close": _format(bar_close)})
        bars.sort(key=lambda item: item["date"])
        if bars and price_wire["bar_date"] > bars[-1]["date"]:
            raise ValuationSnapshotConflict(
                "the priced bar is newer than every bar in the history it is "
                "being ranked against"
            )

        metrics, basis = compute_metrics(
            close=close, shares=share_count, as_of=price_wire["bar_date"],
            windows=windows, bars=bars, share_history=dated_shares,
        )
        public_windows = [
            {"as_of": window["as_of"], "roles": window["roles"]} for window in windows
        ]
        binding = {
            "formula_version": FORMULA_VERSION,
            "price": price_wire,
            "shares": shares_wire,
            "fundamental_windows": public_windows,
            "price_history_dates": [bar["date"] for bar in bars],
            "share_history": dated_shares,
        }
        binding_hash = content_hash(binding)

        snapshot_ref = snapshot_ref_for(company_ref)
        latest = self.latest_version(company_ref)
        if latest is not None and latest["binding_hash"] == binding_hash:
            # Same price, same shares, same filings, same formula: the same
            # snapshot. Publishing it again would grow the chain without
            # recording that anything changed.
            return {"status": "duplicate", **latest}
        version = 1 if latest is None else latest["version"] + 1
        prior_version_ref = None if latest is None else latest["id"]
        identity = {
            "snapshot_ref": snapshot_ref,
            "version": version,
            "prior_version_ref": prior_version_ref,
            "company_ref": company_ref,
            "binding_hash": binding_hash,
        }
        version_id = _ref("valuation-snapshot-version", identity)
        wire = _record({
            "schema_version": SCHEMA_VERSION,
            "id": version_id,
            "created_at": _now(),
            "snapshot_ref": snapshot_ref,
            "version": version,
            "prior_version_ref": prior_version_ref,
            "company_ref": company_ref,
            "kind": "derived_deterministic",
            "formula_version": FORMULA_VERSION,
            "as_of": price_wire["bar_date"],
            "currency": price_wire["currency"],
            "price": price_wire,
            "shares": shares_wire,
            "share_history": dated_shares,
            "fundamental_windows": public_windows,
            "basis": basis,
            "metrics": metrics,
            "binding_hash": binding_hash,
            "actor_ref": actor_ref,
        })
        available = sum(1 for item in metrics if item["status"] == "available")
        # P17b. ``_role_input`` checks that a trailing-year role has four
        # components and that none repeats; it does not check that each of them
        # is a *quarter*. Four figures that each end on a quarter end and
        # include a nine-month year-to-date among them pass every check above
        # and produce a price/sales wrong by the overlap. That is what this
        # gate is for, and a failure refuses the whole snapshot.
        from .economic_invariants import evaluate_valuation, gate

        gate(self.store, evaluate_valuation(
            wire, statement_rows=statement_rows, solver_results=solver_results))
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO valuation_snapshot_versions "
                "(version_id,snapshot_ref,version_number,prior_version_id,company_ref,"
                "formula_version,as_of,price_version_ref,price_version_hash,"
                "binding_hash,available_metric_count,record_json,content_hash,"
                "actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id, snapshot_ref, version, prior_version_ref, company_ref,
                    FORMULA_VERSION, wire["as_of"], price_wire["version_ref"],
                    price_wire["version_hash"], binding_hash, available,
                    canonical_json(wire), wire["content_hash"], actor_ref,
                    wire["created_at"],
                ),
            )
        stored = self.version(version_id)
        if stored != wire:
            raise ValuationSnapshotConflict(
                "stored valuation snapshot does not read back"
            )
        return {"status": "fresh", **stored}


__all__ = [
    "ACTOR_REF",
    "ALLOWED_FUNDAMENTAL_SOURCES",
    "FORMULA_VERSION",
    "METRICS",
    "METRIC_DEFINITIONS",
    "MIN_PERCENTILE_SAMPLE",
    "PERCENTILE_METHOD",
    "ROLE_AGGREGATION",
    "SCHEMA_VERSION",
    "TRAILING_QUARTERS",
    "ValuationSnapshotAuthority",
    "ValuationSnapshotConflict",
    "ValuationSnapshotError",
    "ValuationSnapshotNotFound",
    "ValuationSnapshotValidationError",
    "compute_metrics",
    "snapshot_ref_for",
]
