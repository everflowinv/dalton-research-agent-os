"""P11a: one company's daily price history, as an append-only version chain.

A price looks like the easiest number in this system and is one of the most
dangerous, because it moves under you. Yesterday's close is not a fixed fact:
a split restates every bar before it, a dividend moves the adjusted series, and
Yahoo silently corrects bad prints. A store that let a bar be *updated* would
lose the only evidence that any of that happened, and a valuation computed last
week could never be reproduced.

So the history is versioned rather than maintained. Every published version
carries the whole series it knows about; a new version is published only when
it has something the previous one did not -- a trading day it had never seen,
or a day whose numbers have moved -- and otherwise the run is a ``duplicate``
that costs a read. Restated history is a new version, never an edit, and the
version says which bars it added and which it restated so that "the split
happened" is visible in the chain rather than inferred from its absence.

Every bar names two things: the connector invocation that fetched it and the
sha256 of the raw library output that call produced. That is the whole
provenance promise for the market layer -- any number in a valuation goes back
to one exact, replayable call rather than to "Yahoo, some time".

Close and Adj Close are separate columns and both are always stored. See
``yfinance_core`` for why that is not a preference.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
SOURCE_REF = "source:yahoo-finance"
ACTOR_REF = "core:market-price-worker"
# The columns one bar has. Close and Adj Close are both here, on purpose.
BAR_FIELDS = ("open", "high", "low", "close", "adj_close", "volume")
OBSERVATION_KINDS = ("shares_outstanding", "market_cap")
# A ceiling, not an expectation: twenty years of daily bars for one company.
MAX_BARS = 6000
MAX_OBSERVATIONS = 4000

_SCHEMA_PATH = Path(__file__).with_name("market_price_schema.sql")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,15}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class MarketPriceError(RuntimeError):
    """Base error for the market price authority."""


class MarketPriceValidationError(MarketPriceError, ValueError):
    """A request does not satisfy the closed contract."""


class MarketPriceConflict(MarketPriceError):
    """A request conflicts with the immutable chain."""


class MarketPriceNotFound(MarketPriceError):
    """No such series or version."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MarketPriceValidationError(f"{name} must be non-empty text")
    return value.strip()


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise MarketPriceValidationError(f"{name} must be lowercase SHA-256")
    return value


def _iso_date(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise MarketPriceValidationError(f"{name} must be YYYY-MM-DD") from exc
    return value


def _decimal_string(value: Any, name: str) -> str:
    """A figure is text here for the same reason it is everywhere in this system.

    A binary float is not the price that printed, and two runs that parsed the
    same quote can disagree in the last bits -- which would make a restatement
    out of nothing and publish a version every tick.
    """

    if isinstance(value, bool) or not isinstance(value, str):
        raise MarketPriceValidationError(f"{name} must be a canonical decimal string")
    if _DECIMAL_RE.fullmatch(value) is None:
        raise MarketPriceValidationError(f"{name} must be a canonical decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise MarketPriceValidationError(f"{name} must be Decimal-compatible") from exc
    if not parsed.is_finite():
        raise MarketPriceValidationError(f"{name} must be finite")
    return value


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MarketPriceValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) != fields:
        raise MarketPriceValidationError(
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


def series_ref_for(company_ref: str) -> str:
    """One company, one price series. Named by the company, not the ticker.

    A ticker is a thing an exchange lends a company and can take back; the
    company ref is what the rest of the ledger keys on. A company that changes
    ticker keeps its history, which is the point.
    """

    return f"market-price-series:{_text(company_ref, 'company_ref')}"


def _decode(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
    if row is None:
        raise MarketPriceNotFound(name)
    try:
        wire = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise MarketPriceConflict(f"{name} record_json is invalid") from exc
    if not isinstance(wire, dict) or canonical_json(wire) != row["record_json"]:
        raise MarketPriceConflict(f"{name} record_json is not canonical")
    body = dict(wire)
    asserted = body.pop("content_hash", None)
    if asserted != content_hash(body) or asserted != row["content_hash"]:
        raise MarketPriceConflict(f"{name} content hash drifted")
    if wire.get("id") != row["version_id"]:
        raise MarketPriceConflict(f"{name} identity column drifted")
    checks = {
        "series_ref": wire.get("series_ref"),
        "version_number": wire.get("version"),
        "prior_version_id": wire.get("prior_version_ref"),
        "company_ref": wire.get("company_ref"),
        "ticker": wire.get("ticker"),
        "currency": wire.get("currency"),
        "first_bar_date": wire.get("first_bar_date"),
        "last_bar_date": wire.get("last_bar_date"),
        "bar_count": len(wire.get("bars") or []),
        "actor_ref": wire.get("actor_ref"),
        "created_at": wire.get("created_at"),
    }
    for column, expected in checks.items():
        if column in row.keys() and row[column] != expected:
            raise MarketPriceConflict(f"{name} {column} drifted")
    return wire


def _normalise_bars(
    bars: Sequence[Mapping[str, Any]],
    *,
    invocation_ref: str,
    artifact_hash: str,
) -> list[dict[str, Any]]:
    """One row per trading day, each bound to the call that produced it."""

    if not isinstance(bars, (list, tuple)):
        raise MarketPriceValidationError("bars must be an array")
    if len(bars) > MAX_BARS:
        raise MarketPriceValidationError(f"a run may carry at most {MAX_BARS} bars")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(bars):
        item = _closed(raw, {"date", *BAR_FIELDS}, f"bars[{index}]")
        bar_date = _iso_date(item["date"], f"bars[{index}].date")
        if bar_date in seen:
            raise MarketPriceValidationError(f"bars carry {bar_date} twice")
        seen.add(bar_date)
        row: dict[str, Any] = {"date": bar_date}
        for field in BAR_FIELDS:
            row[field] = _decimal_string(item[field], f"bars[{index}].{field}")
        low, high = Decimal(row["low"]), Decimal(row["high"])
        if low > high:
            raise MarketPriceConflict(f"bar {bar_date} has a low above its high")
        for field in ("open", "close"):
            value = Decimal(row[field])
            if value < low or value > high:
                raise MarketPriceConflict(
                    f"bar {bar_date} has a {field} outside its own high/low range"
                )
        if Decimal(row["volume"]) < 0:
            raise MarketPriceConflict(f"bar {bar_date} has negative volume")
        row["invocation_ref"] = invocation_ref
        row["artifact_hash"] = artifact_hash
        rows.append(row)
    return rows


def _normalise_observations(
    observations: Sequence[Mapping[str, Any]],
    *,
    invocation_ref: str,
    artifact_hash: str,
) -> list[dict[str, Any]]:
    """Share count and market capitalisation, each with its own date.

    Yahoo reports the latest it knows and nothing behind it, so these are not
    properties of a trading day and are not stored as if they were. A valuation
    that uses a share count says which observation it used and when that
    observation was made.
    """

    if not isinstance(observations, (list, tuple)):
        raise MarketPriceValidationError("observations must be an array")
    if len(observations) > MAX_OBSERVATIONS:
        raise MarketPriceValidationError(
            f"a run may carry at most {MAX_OBSERVATIONS} observations"
        )
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(observations):
        item = _closed(
            raw, {"observation", "as_of", "value", "unit"}, f"observations[{index}]"
        )
        kind = _text(item["observation"], f"observations[{index}].observation")
        if kind not in OBSERVATION_KINDS:
            raise MarketPriceValidationError(
                f"observations[{index}].observation is not a known observation"
            )
        as_of = _iso_date(item["as_of"], f"observations[{index}].as_of")
        if (kind, as_of) in seen:
            raise MarketPriceValidationError(
                f"observations carry {kind} on {as_of} twice"
            )
        seen.add((kind, as_of))
        value = _decimal_string(item["value"], f"observations[{index}].value")
        if Decimal(value) <= 0:
            raise MarketPriceConflict(
                f"{kind} on {as_of} is not a positive quantity"
            )
        rows.append({
            "observation": kind,
            "as_of": as_of,
            "value": value,
            "unit": _text(item["unit"], f"observations[{index}].unit"),
            "invocation_ref": invocation_ref,
            "artifact_hash": artifact_hash,
        })
    return sorted(rows, key=lambda row: (row["observation"], row["as_of"]))


def _bar_body(row: Mapping[str, Any]) -> dict[str, str]:
    """The part of a bar that being different makes a restatement.

    Not the invocation and not the artifact hash: refetching the same window
    tomorrow produces a different call for identical numbers, and calling that
    a restatement would publish a version a day forever.
    """

    return {field: str(row[field]) for field in BAR_FIELDS}


class MarketPriceSeriesAuthority:
    """Append-only daily price history, one version chain per company."""

    def __init__(self, store: Any):
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("MarketPriceSeriesAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    # -- reading -----------------------------------------------------------

    def version(self, version_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM market_price_series_versions WHERE version_id=?",
            (_text(version_ref, "version_ref"),),
        ).fetchone()
        return _decode(row, f"MarketPriceSeriesVersion {version_ref}")

    def latest_version(self, company_ref: str) -> dict[str, Any] | None:
        """The current series for one company, or None if it has no history."""

        row = self.connection.execute(
            "SELECT * FROM market_price_series_versions WHERE series_ref=? "
            "ORDER BY version_number DESC LIMIT 1",
            (series_ref_for(company_ref),),
        ).fetchone()
        if row is None:
            return None
        return _decode(row, f"latest MarketPriceSeriesVersion for {company_ref}")

    def versions(self, company_ref: str) -> list[dict[str, Any]]:
        """The whole chain, oldest first. A split is visible here or nowhere."""

        rows = self.connection.execute(
            "SELECT * FROM market_price_series_versions WHERE series_ref=? "
            "ORDER BY version_number ASC",
            (series_ref_for(company_ref),),
        ).fetchall()
        return [
            _decode(row, f"MarketPriceSeriesVersion for {company_ref}") for row in rows
        ]

    def series(self, company_ref: str) -> dict[str, Any] | None:
        """The bars and observations a reader should use, with their version ref."""

        latest = self.latest_version(company_ref)
        if latest is None:
            return None
        return {
            "company_ref": latest["company_ref"],
            "ticker": latest["ticker"],
            "currency": latest["currency"],
            "version_ref": latest["id"],
            "version_hash": latest["content_hash"],
            "version": latest["version"],
            "source_ref": latest["source_ref"],
            "bars": [dict(bar) for bar in latest["bars"]],
            "observations": [dict(row) for row in latest["observations"]],
            "first_bar_date": latest["first_bar_date"],
            "last_bar_date": latest["last_bar_date"],
        }

    def latest_close(self, company_ref: str) -> dict[str, Any] | None:
        """The newest close, and the exact thing a citation would point at.

        Both closes come back. A reader that wants a total-return series wants
        ``adj_close``; a reader quoting "the shares closed at" wants ``close``,
        and dividends must never be added on top of the adjusted one.
        """

        latest = self.latest_version(company_ref)
        if latest is None:
            return None
        bar = latest["bars"][-1]
        return {
            "company_ref": latest["company_ref"],
            "ticker": latest["ticker"],
            "currency": latest["currency"],
            "as_of": bar["date"],
            "close": bar["close"],
            "adj_close": bar["adj_close"],
            "version_ref": latest["id"],
            "version_hash": latest["content_hash"],
            "invocation_ref": bar["invocation_ref"],
            "artifact_hash": bar["artifact_hash"],
        }

    def latest_observation(
        self, company_ref: str, observation: str
    ) -> dict[str, Any] | None:
        """The newest share count or market capitalisation this series holds."""

        if observation not in OBSERVATION_KINDS:
            raise MarketPriceValidationError("observation is not a known observation")
        latest = self.latest_version(company_ref)
        if latest is None:
            return None
        rows = [row for row in latest["observations"]
                if row["observation"] == observation]
        if not rows:
            return None
        newest = max(rows, key=lambda row: row["as_of"])
        return {
            **dict(newest),
            "version_ref": latest["id"],
            "version_hash": latest["content_hash"],
        }

    # -- publishing --------------------------------------------------------

    def publish_series(
        self,
        *,
        company_ref: str,
        ticker: str,
        currency: str,
        bars: Sequence[Mapping[str, Any]],
        observations: Sequence[Mapping[str, Any]] = (),
        invocation_ref: str,
        artifact_hash: str,
        governance_ref: str,
        governance_hash: str,
        requested_start: str,
        requested_end: str,
        actor_ref: str = ACTOR_REF,
    ) -> dict[str, Any]:
        """Merge one fetched window into the chain, or say it added nothing.

        The merge is a union over bar dates, newest fetch winning on a date
        both know about -- which is how a restatement lands. Nothing is
        dropped: a version always carries the full history, so a reader never
        has to assemble one from a chain.
        """

        company_ref = _text(company_ref, "company_ref")
        ticker = _text(ticker, "ticker").upper()
        if _TICKER_RE.fullmatch(ticker) is None:
            raise MarketPriceValidationError("ticker is not a market symbol")
        currency = _text(currency, "currency").upper()
        if _CURRENCY_RE.fullmatch(currency) is None:
            raise MarketPriceValidationError("currency must be an ISO 4217 code")
        invocation_ref = _text(invocation_ref, "invocation_ref")
        artifact_hash = _hash(artifact_hash, "artifact_hash")
        governance_ref = _text(governance_ref, "governance_ref")
        governance_hash = _hash(governance_hash, "governance_hash")
        requested_start = _iso_date(requested_start, "requested_start")
        requested_end = _iso_date(requested_end, "requested_end")
        if requested_end < requested_start:
            raise MarketPriceValidationError("requested_end precedes requested_start")
        actor_ref = _text(actor_ref, "actor_ref")
        fetched = _normalise_bars(
            bars, invocation_ref=invocation_ref, artifact_hash=artifact_hash
        )
        fetched_observations = _normalise_observations(
            observations, invocation_ref=invocation_ref, artifact_hash=artifact_hash
        )

        series_ref = series_ref_for(company_ref)
        latest = self.latest_version(company_ref)
        prior_bars: dict[str, dict[str, Any]] = {}
        prior_observations: list[dict[str, Any]] = []
        if latest is not None:
            if latest["ticker"] != ticker:
                raise MarketPriceConflict(
                    "this company's series is held under a different ticker"
                )
            if latest["currency"] != currency:
                raise MarketPriceConflict(
                    "this company's series is held in a different currency"
                )
            prior_bars = {bar["date"]: dict(bar) for bar in latest["bars"]}
            prior_observations = [dict(row) for row in latest["observations"]]

        added = sorted(row["date"] for row in fetched if row["date"] not in prior_bars)
        restated = sorted(
            row["date"] for row in fetched
            if row["date"] in prior_bars
            and _bar_body(row) != _bar_body(prior_bars[row["date"]])
        )
        if latest is not None and not added and not restated:
            # Nothing this run saw was new. A weekend tick, or a window that
            # had already been fetched: the correct answer is to publish
            # nothing rather than to write a version identical to the last.
            #
            # Deliberately not gated on the observations. Market capitalisation
            # moves every second the market is open, so a run that found no new
            # trading day but a market cap $6m lighter is not a new fact about
            # the company -- and letting that publish would put a version on
            # the chain every tick all afternoon.
            return {"status": "duplicate", **latest}
        if not fetched and latest is None:
            raise MarketPriceValidationError(
                "a first version needs at least one bar"
            )

        merged = dict(prior_bars)
        for row in fetched:
            merged[row["date"]] = row
        ordered = [merged[key] for key in sorted(merged)]
        if len(ordered) > MAX_BARS:
            raise MarketPriceConflict(
                f"this series would exceed {MAX_BARS} bars; it needs its own retention rule"
            )
        # One reading per observation per day, the newest winning. Yahoo
        # reports the latest it knows with no history behind it, so a second
        # reading on the same day supersedes the first rather than sitting
        # beside it as if the share count had changed twice before lunch.
        by_day = {
            (row["observation"], row["as_of"]): row
            for row in [*prior_observations, *fetched_observations]
        }
        observation_rows = [by_day[key] for key in sorted(by_day)]

        version = 1 if latest is None else latest["version"] + 1
        prior_version_ref = None if latest is None else latest["id"]
        identity = {
            "series_ref": series_ref,
            "version": version,
            "prior_version_ref": prior_version_ref,
            "company_ref": company_ref,
            "ticker": ticker,
            "currency": currency,
            "source_ref": SOURCE_REF,
            "bars": ordered,
            "observations": observation_rows,
        }
        version_id = _ref("market-price-series-version", identity)
        wire = _record({
            "schema_version": SCHEMA_VERSION,
            "id": version_id,
            "created_at": _now(),
            **identity,
            "first_bar_date": ordered[0]["date"],
            "last_bar_date": ordered[-1]["date"],
            "bar_count": len(ordered),
            "added_bar_dates": added,
            # Named rather than counted: which day was restated is the whole
            # evidence that a split or a correction happened, and a reader who
            # has to diff two versions to find out will not.
            "restated_bar_dates": restated,
            "fetch": {
                "invocation_ref": invocation_ref,
                "artifact_hash": artifact_hash,
                "governance_ref": governance_ref,
                "governance_hash": governance_hash,
                "requested_start": requested_start,
                "requested_end": requested_end,
                "fetched_bar_count": len(fetched),
            },
            "actor_ref": actor_ref,
        })
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO market_price_series_versions "
                "(version_id,series_ref,version_number,prior_version_id,company_ref,"
                "ticker,source_ref,currency,first_bar_date,last_bar_date,bar_count,"
                "added_bar_count,restated_bar_count,record_json,content_hash,"
                "actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id, series_ref, version, prior_version_ref, company_ref,
                    ticker, SOURCE_REF, currency, wire["first_bar_date"],
                    wire["last_bar_date"], wire["bar_count"], len(added),
                    len(restated), canonical_json(wire), wire["content_hash"],
                    actor_ref, wire["created_at"],
                ),
            )
        # Read back rather than trust the write: the row that answers every
        # later question is the one that has to be correct, not the dict that
        # was handed to sqlite.
        stored = self.version(version_id)
        if stored != wire:
            raise MarketPriceConflict("stored market price version does not read back")
        return {"status": "fresh", **stored}


__all__ = [
    "ACTOR_REF",
    "BAR_FIELDS",
    "MAX_BARS",
    "MAX_OBSERVATIONS",
    "OBSERVATION_KINDS",
    "SCHEMA_VERSION",
    "SOURCE_REF",
    "MarketPriceConflict",
    "MarketPriceError",
    "MarketPriceNotFound",
    "MarketPriceSeriesAuthority",
    "MarketPriceValidationError",
    "series_ref_for",
]
