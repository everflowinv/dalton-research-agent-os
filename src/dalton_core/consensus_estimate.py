"""P11b: what the street expects of one company, versioned like a price.

Two routes answer the same question and both land here.

*Route 2, the vendor observation.* yfinance's ``analyst_estimates`` operation
returns a price target, recommendation counts and forward EPS and revenue
estimates. Every one of those is an **observation of what a vendor said the
street thinks on one day** -- not a fact about the company, not a figure anyone
filed -- and it is labelled that way in the record. The whole version is bound
to one connector invocation and the sha256 of the raw library output that call
produced, which is the same provenance promise the price series makes.

*Route 1, the reports already in the ledger.* Two independent brokers who
published a target within a window are a consensus *range*; one broker alone is
one broker's opinion and never becomes one. That block arrives through
``publish_report_consensus`` carrying the ``StreetEstimate`` records it was
computed from, so a reader can walk back to the exact quotes.

**The fiscal period mapping is the part that can be silently wrong.** Yahoo
labels its estimates ``0q``, ``+1q``, ``0y``, ``+1y`` and never says which
quarter that is. Accenture's fiscal year ends in August and IBM's in December,
so the same four keys mean different things for two companies in the same
industry on the same afternoon, and a consensus EPS filed against the wrong
quarter is worse than no consensus at all: it will be compared against an
actual it was never about.

The rule, written down once here and recorded on every version:

    ``0`` is *the next period to be reported*.

So ``0q`` is the first fiscal quarter whose end falls after the last period the
company has actually reported, and ``0y`` is the first fiscal year whose end
does. That is what makes Accenture's ``0q`` on 9 September 2026 its fiscal Q4
ending 31 August -- already over, not yet reported -- while IBM's is the
September quarter still in progress. Both need two facts this module refuses to
guess: the fiscal year end and the last reported period end. Absent either, the
mapping raises and no version is published, because an unlabelled estimate is
not a smaller version of a labelled one.
"""

from __future__ import annotations

import calendar
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .model_forecast_driver import CELL_KINDS, CHANGE_REASONS
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
SOURCE_REF = "source:yahoo-finance"
REPORT_SOURCE_REF = "source:alphaengine"
ACTOR_REF = "core:consensus-estimate-worker"
# What a version's numbers are: a vendor's report of what the street thinks,
# read on a day. Recorded on the record rather than left to be inferred from
# the source ref, because the distinction between "observed" and "filed" is the
# one thing a reader must not have to look up.
OBSERVATION_BASIS = "vendor-observed"
REPORT_BASIS = "sell-side-report-corroborated"
VENDOR = "vendor_observation"
REPORTS = "report_consensus"
SOURCE_KINDS = (VENDOR, REPORTS)
# The mapping rule above, named so a stored version says which rule produced
# its labels and a later change to the rule is visible rather than silent.
FISCAL_MAPPING_VERSION = "consensus-fiscal-mapping:p11b:0.1"
# Yahoo's four estimate keys, and nothing else. A fifth key appearing one day
# is refused rather than guessed at.
ESTIMATE_PERIOD_KEYS = ("0q", "+1q", "0y", "+1y")
# Its recommendation keys: this month and the three before it.
RECOMMENDATION_PERIOD_KEYS = ("0m", "-1m", "-2m", "-3m")
RECOMMENDATION_FIELDS = ("strong_buy", "buy", "hold", "sell", "strong_sell")
ESTIMATE_FIELDS = ("avg", "low", "high", "year_ago", "growth")
TARGET_FIELDS = ("current", "high", "low", "mean", "median")
MAX_ESTIMATE_ROWS = 16
MAX_RECOMMENDATION_ROWS = 16

_SCHEMA_PATH = Path(__file__).with_name("consensus_estimate_schema.sql")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,15}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_MONTH_DAY_RE = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")


class ConsensusEstimateError(RuntimeError):
    """Base error for the consensus estimate authority."""


class ConsensusEstimateValidationError(ConsensusEstimateError, ValueError):
    """A request does not satisfy the closed contract."""


class ConsensusEstimateConflict(ConsensusEstimateError):
    """A request conflicts with the immutable chain."""


class ConsensusEstimateNotFound(ConsensusEstimateError):
    """No such chain or version."""


class FiscalMappingError(ConsensusEstimateError, ValueError):
    """The vendor's relative period cannot be placed on a fiscal calendar."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConsensusEstimateValidationError(f"{name} must be non-empty text")
    return value.strip()


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise ConsensusEstimateValidationError(f"{name} must be lowercase SHA-256")
    return value


def _iso_date(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ConsensusEstimateValidationError(f"{name} must be YYYY-MM-DD") from exc
    return value


def _rfc3339(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConsensusEstimateValidationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ConsensusEstimateValidationError(f"{name} must carry a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _optional_decimal(value: Any, name: str) -> str | None:
    """A figure as text, or absent.

    Absent is not zero and is not an error. Yahoo drops a whole block without
    warning, and "no analyst rates it a sell" and "nobody said how many do" are
    different facts that a zero would merge.
    """

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise ConsensusEstimateValidationError(
            f"{name} must be a canonical decimal string or null"
        )
    if _DECIMAL_RE.fullmatch(value) is None:
        raise ConsensusEstimateValidationError(
            f"{name} must be a canonical decimal string or null"
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ConsensusEstimateValidationError(f"{name} must be Decimal-compatible") from exc
    if not parsed.is_finite():
        raise ConsensusEstimateValidationError(f"{name} must be finite")
    return value


def _optional_count(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConsensusEstimateValidationError(f"{name} must be a non-negative integer or null")
    return value


def _optional_currency(value: Any, name: str) -> str | None:
    if value is None:
        return None
    value = _text(value, name).upper()
    if _CURRENCY_RE.fullmatch(value) is None:
        raise ConsensusEstimateValidationError(f"{name} must be an ISO 4217 code")
    return value


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConsensusEstimateValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) != fields:
        raise ConsensusEstimateValidationError(
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


def consensus_ref_for(company_ref: str) -> str:
    """One company, one consensus chain, named by the company."""

    return f"consensus-estimate:{_text(company_ref, 'company_ref')}"


# -- the fiscal calendar ----------------------------------------------------


def _month_end(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _shift(year: int, month: int, months: int) -> tuple[int, int]:
    index = (year * 12 + month - 1) + months
    return index // 12, index % 12 + 1


def fiscal_calendar(
    *, fiscal_year_end: Any, last_reported_period_end: Any
) -> dict[str, Any]:
    """The two facts the vendor's relative labels cannot be read without.

    ``fiscal_year_end`` is ``MM-DD`` -- Accenture's ``08-31``, IBM's ``12-31``.
    ``last_reported_period_end`` is the period end of the newest filing this
    system holds for the company; it is what makes ``0`` mean "next to be
    reported" rather than "whatever quarter today happens to fall in".

    Both are required. A caller that has neither has no business labelling a
    consensus estimate, and this raising is the whole point: the alternative is
    a number filed against a quarter it was never about.
    """

    if not isinstance(fiscal_year_end, str) or _MONTH_DAY_RE.fullmatch(
        fiscal_year_end.strip()
    ) is None:
        raise FiscalMappingError(
            "fiscal_year_end must be MM-DD; this company's fiscal calendar is "
            "not known, so the vendor's 0q/+1q/0y/+1y cannot be placed"
        )
    fiscal_year_end = fiscal_year_end.strip()
    try:
        anchor = date.fromisoformat(_text(last_reported_period_end, "last_reported_period_end"))
    except (ValueError, ConsensusEstimateValidationError) as exc:
        raise FiscalMappingError(
            "last_reported_period_end must be YYYY-MM-DD; without the last "
            "period this company actually reported, 0q means nothing"
        ) from exc
    month, day = (int(part) for part in fiscal_year_end.split("-"))
    # A filer whose year ends on the last day of a month ends every quarter on
    # the last day of a month, whatever length it happens to be. One whose year
    # ends on the 15th keeps the 15th. Both are expressed by the same rule.
    ends_on_month_end = day == _month_end(2001, month)
    return {
        "fiscal_year_end": fiscal_year_end,
        "fiscal_year_end_month": month,
        "fiscal_year_end_day": day,
        "ends_on_month_end": ends_on_month_end,
        "last_reported_period_end": anchor.isoformat(),
        "mapping_version": FISCAL_MAPPING_VERSION,
        # FY2026 is the fiscal year that *ends* in calendar 2026. That is how
        # both Accenture and IBM label theirs, and how every filing this system
        # holds is labelled.
        "label_basis": "fiscal_year_ends_in_calendar_year",
    }


def fiscal_year_end_from_quarters(report_dates: Sequence[Any]) -> str | None:
    """The fiscal year end a company's quarterly filings give away.

    A filer reports three quarters on a 10-Q and the fourth inside the 10-K, so
    the quarter it *never* files a 10-Q for is the quarter its fiscal year ends
    in. Three distinct quarter months, each three apart, name the fourth
    exactly, and no guess is involved: it is arithmetic on the company's own
    filing dates.

    This exists because the live Core holds thirty-three 10-Q rows and no 10-K
    at all, so the annual-report route -- the direct one, and still the one
    preferred when it is available -- answers ``unknown`` for every covered
    company. On that data this derives 12-31 for Cognizant and EPAM, 03-31 for
    DXC and 08-31 for Accenture, the last of which a Citi note independently
    prints as "Fiscal year end 31-Aug" on its first page.

    ``None`` rather than a guess when the filings do not settle it: fewer than
    three distinct quarters (IBM, with one filing), months that are not on a
    three-month grid, or period ends that are not month ends -- a 52/53-week
    filer's year end moves by a few days each year and is not an ``MM-DD`` at
    all, so it must not be written as one.
    """

    months: dict[int, list[date]] = {}
    for value in report_dates or ():
        try:
            parsed = date.fromisoformat(str(value))
        except (TypeError, ValueError):
            continue
        months.setdefault(parsed.month, []).append(parsed)
    if len(months) != 3:
        return None
    if any(
        moment.day != _month_end(moment.year, moment.month)
        for group in months.values() for moment in group
    ):
        # Not month ends. A 52/53-week filer, or a date this rule cannot read.
        return None
    present = sorted(months)
    # The four slots of the grid the three observed months belong to.
    for start in present:
        grid = sorted(((start - 1 + step) % 12) + 1 for step in (0, 3, 6, 9))
        if set(present) <= set(grid):
            missing = [month for month in grid if month not in months]
            if len(missing) != 1:
                return None
            month = missing[0]
            return f"{month:02d}-{_month_end(2001, month):02d}"
    return None


def _period_end(cal: Mapping[str, Any], year: int, month: int) -> date:
    if cal["ends_on_month_end"]:
        return date(year, month, _month_end(year, month))
    return date(year, month, min(int(cal["fiscal_year_end_day"]), _month_end(year, month)))


def _fiscal_year_end_on_or_after(cal: Mapping[str, Any], moment: date) -> date:
    """The fiscal year this date falls in, for finding its quarters."""

    month = int(cal["fiscal_year_end_month"])
    candidate = _period_end(cal, moment.year, month)
    if candidate < moment:
        candidate = _period_end(cal, moment.year + 1, month)
    return candidate


def _fiscal_year_end_after(cal: Mapping[str, Any], moment: date) -> date:
    """The first fiscal year that ends *strictly* after this date.

    Strict, and the day it matters is the day a 10-K lands. Then the last
    reported period end is exactly the fiscal year end, and an on-or-after
    rule answers with the year that was just reported -- which is not a year
    anyone is still estimating. Refusing there, as this did until B1, took the
    whole version down and left the company unmapped for the two months
    between the annual filing and the first quarter after it: a dead zone once
    a year, in the window where the street's next-year number is most worth
    having. The quarter branch has always used a strict comparison; this is
    the same rule, written once more.
    """

    month = int(cal["fiscal_year_end_month"])
    candidate = _period_end(cal, moment.year, month)
    if candidate <= moment:
        candidate = _period_end(cal, moment.year + 1, month)
    return candidate


def _quarter_ends_of(cal: Mapping[str, Any], fiscal_year_end: date) -> list[date]:
    """The four quarter ends of the fiscal year that closes on this date."""

    ends: list[date] = []
    for back in (9, 6, 3, 0):
        year, month = _shift(fiscal_year_end.year, fiscal_year_end.month, -back)
        ends.append(_period_end(cal, year, month))
    return ends


def _next_quarter_end_after(cal: Mapping[str, Any], moment: date) -> tuple[date, int, int]:
    """The first fiscal quarter that ends after ``moment``: end, FY label, Q."""

    year_end = _fiscal_year_end_on_or_after(cal, moment)
    for _ in range(3):
        for index, end in enumerate(_quarter_ends_of(cal, year_end), start=1):
            if end > moment:
                return end, year_end.year, index
        year, month = _shift(year_end.year, year_end.month, 12)
        year_end = _period_end(cal, year, month)
    raise FiscalMappingError("no fiscal quarter follows the last reported period")


def map_estimate_period(key: str, cal: Mapping[str, Any]) -> dict[str, Any]:
    """Turn one of Yahoo's four relative keys into a named fiscal period.

    ``0`` is the next period to be reported; ``+1`` is the one after it. See
    the module docstring for why that rule and not "the period we are in".
    """

    if key not in ESTIMATE_PERIOD_KEYS:
        raise FiscalMappingError(
            f"{key!r} is not one of the vendor's estimate periods "
            f"{list(ESTIMATE_PERIOD_KEYS)}"
        )
    anchor = date.fromisoformat(str(cal["last_reported_period_end"]))
    if key.endswith("q"):
        end, fiscal_year, quarter = _next_quarter_end_after(cal, anchor)
        if key == "+1q":
            end, fiscal_year, quarter = _next_quarter_end_after(cal, end)
        return {
            "period_kind": "quarter",
            "fiscal_year": fiscal_year,
            "fiscal_quarter": quarter,
            "period_end": end.isoformat(),
            "label": f"FY{fiscal_year}Q{quarter}",
        }
    year_end = _fiscal_year_end_after(cal, anchor)
    if key == "+1y":
        year, month = _shift(year_end.year, year_end.month, 12)
        year_end = _period_end(cal, year, month)
    return {
        "period_kind": "fiscal_year",
        "fiscal_year": year_end.year,
        "fiscal_quarter": None,
        "period_end": year_end.isoformat(),
        "label": f"FY{year_end.year}",
    }


def map_recommendation_period(key: str, as_of: str) -> dict[str, Any]:
    """Yahoo's ``0m``/``-1m``/... against the calendar month they were read in.

    These are not fiscal periods and are not mapped as if they were: a rating
    count is about the month a vendor aggregated it in, and that month is a
    calendar month wherever the company's fiscal year ends.
    """

    if key not in RECOMMENDATION_PERIOD_KEYS:
        raise FiscalMappingError(
            f"{key!r} is not one of the vendor's recommendation periods "
            f"{list(RECOMMENDATION_PERIOD_KEYS)}"
        )
    read_on = date.fromisoformat(_iso_date(as_of, "as_of"))
    back = 0 if key == "0m" else int(key[:-1])
    year, month = _shift(read_on.year, read_on.month, back)
    return {"month": f"{year:04d}-{month:02d}"}


# -- normalising the wire ---------------------------------------------------


def _estimate_rows(
    rows: Any, cal: Mapping[str, Any], name: str
) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if not isinstance(rows, (list, tuple)):
        raise ConsensusEstimateValidationError(f"{name} must be an array")
    if len(rows) > MAX_ESTIMATE_ROWS:
        raise ConsensusEstimateValidationError(
            f"{name} may carry at most {MAX_ESTIMATE_ROWS} periods"
        )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        item = _closed(
            raw,
            {"period", *ESTIMATE_FIELDS, "number_of_analysts", "currency"},
            f"{name}[{index}]",
        )
        key = _text(item["period"], f"{name}[{index}].period")
        if key in seen:
            raise ConsensusEstimateValidationError(f"{name} carries {key} twice")
        seen.add(key)
        mapped = map_estimate_period(key, cal)
        row: dict[str, Any] = {"vendor_period": key, **mapped}
        for field in ESTIMATE_FIELDS:
            row[field] = _optional_decimal(item[field], f"{name}[{index}].{field}")
        row["number_of_analysts"] = _optional_count(
            item["number_of_analysts"], f"{name}[{index}].number_of_analysts"
        )
        row["currency"] = _optional_currency(item["currency"], f"{name}[{index}].currency")
        low, high = row["low"], row["high"]
        if low is not None and high is not None and Decimal(low) > Decimal(high):
            raise ConsensusEstimateConflict(
                f"{name} for {row['label']} has a low estimate above its high"
            )
        out.append(row)
    out.sort(key=lambda row: (row["period_end"], row["vendor_period"]))
    return out


def _recommendation_rows(rows: Any, as_of: str, name: str) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if not isinstance(rows, (list, tuple)):
        raise ConsensusEstimateValidationError(f"{name} must be an array")
    if len(rows) > MAX_RECOMMENDATION_ROWS:
        raise ConsensusEstimateValidationError(
            f"{name} may carry at most {MAX_RECOMMENDATION_ROWS} periods"
        )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        item = _closed(raw, {"period", *RECOMMENDATION_FIELDS}, f"{name}[{index}]")
        key = _text(item["period"], f"{name}[{index}].period")
        if key in seen:
            raise ConsensusEstimateValidationError(f"{name} carries {key} twice")
        seen.add(key)
        row = {"vendor_period": key, **map_recommendation_period(key, as_of)}
        for field in RECOMMENDATION_FIELDS:
            row[field] = _optional_count(item[field], f"{name}[{index}].{field}")
        out.append(row)
    out.sort(key=lambda row: row["month"], reverse=True)
    return out


def _target_block(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {field: None for field in TARGET_FIELDS} | {"number_of_analysts": None}
    item = _closed(value, {*TARGET_FIELDS, "number_of_analysts"}, name)
    block = {
        field: _optional_decimal(item[field], f"{name}.{field}")
        for field in TARGET_FIELDS
    }
    block["number_of_analysts"] = _optional_count(
        item["number_of_analysts"], f"{name}.number_of_analysts"
    )
    low, high = block["low"], block["high"]
    if low is not None and high is not None and Decimal(low) > Decimal(high):
        raise ConsensusEstimateConflict("the target price low is above its high")
    for field in ("mean", "median", "current"):
        value = block[field]
        if value is not None and Decimal(value) <= 0:
            raise ConsensusEstimateConflict(f"the target price {field} is not positive")
    return block


def _comparable(wire: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a version that being different makes it a new version.

    Not the fetch block and not the timestamps: refetching the same numbers
    tomorrow is a different invocation of the same call, and treating that as
    a change would publish a version a day forever.
    """

    return {
        "ticker": wire["ticker"],
        "currency": wire["currency"],
        "price_target": wire["price_target"],
        "recommendations": wire["recommendations"],
        "eps_estimates": wire["eps_estimates"],
        "revenue_estimates": wire["revenue_estimates"],
        "report_consensus": wire["report_consensus"],
        "fiscal_calendar": {
            key: wire["fiscal_calendar"][key]
            for key in ("fiscal_year_end", "last_reported_period_end", "mapping_version")
        },
    }


def _changed_fields(
    before: Mapping[str, Any] | None, after: Mapping[str, Any]
) -> list[str]:
    if before is None:
        return ["*"]
    old, new = _comparable(before), _comparable(after)
    return sorted(key for key in new if old.get(key) != new[key])


def _decode(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
    if row is None:
        raise ConsensusEstimateNotFound(name)
    try:
        wire = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConsensusEstimateConflict(f"{name} record_json is invalid") from exc
    if not isinstance(wire, dict) or canonical_json(wire) != row["record_json"]:
        raise ConsensusEstimateConflict(f"{name} record_json is not canonical")
    body = dict(wire)
    asserted = body.pop("content_hash", None)
    if asserted != content_hash(body) or asserted != row["content_hash"]:
        raise ConsensusEstimateConflict(f"{name} content hash drifted")
    if wire.get("id") != row["version_id"]:
        raise ConsensusEstimateConflict(f"{name} identity column drifted")
    checks = {
        "consensus_ref": wire.get("consensus_ref"),
        "version_number": wire.get("version"),
        "prior_version_id": wire.get("prior_version_ref"),
        "company_ref": wire.get("company_ref"),
        "ticker": wire.get("ticker"),
        "source_kind": wire.get("source_kind"),
        "change_reason": wire.get("change_reason"),
        "currency": wire.get("currency"),
        "as_of": wire.get("as_of"),
        "actor_ref": wire.get("actor_ref"),
        "created_at": wire.get("created_at"),
    }
    for column, expected in checks.items():
        if column in row.keys() and row[column] != expected:
            raise ConsensusEstimateConflict(f"{name} {column} drifted")
    return wire


class ConsensusEstimateAuthority:
    """Append-only street expectations, one version chain per company."""

    def __init__(self, store: Any):
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("ConsensusEstimateAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    # -- reading -----------------------------------------------------------

    def version(self, version_ref: str) -> dict[str, Any]:
        return self._decode_version(self.connection, _text(version_ref, "version_ref"))

    @staticmethod
    def _decode_version(cursor: Any, version_ref: str) -> dict[str, Any]:
        row = cursor.execute(
            "SELECT * FROM consensus_estimate_versions WHERE version_id=?",
            (version_ref,),
        ).fetchone()
        return _decode(row, f"ConsensusEstimateVersion {version_ref}")

    def latest_version(self, company_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM consensus_estimate_versions WHERE consensus_ref=? "
            "ORDER BY version_number DESC LIMIT 1",
            (consensus_ref_for(company_ref),),
        ).fetchone()
        if row is None:
            return None
        return _decode(row, f"latest ConsensusEstimateVersion for {company_ref}")

    def versions(self, company_ref: str) -> list[dict[str, Any]]:
        """The whole chain, oldest first: how the street changed its mind."""

        rows = self.connection.execute(
            "SELECT * FROM consensus_estimate_versions WHERE consensus_ref=? "
            "ORDER BY version_number ASC",
            (consensus_ref_for(company_ref),),
        ).fetchall()
        return [
            _decode(row, f"ConsensusEstimateVersion for {company_ref}") for row in rows
        ]

    def latest_consensus(self, company_ref: str) -> dict[str, Any] | None:
        """What the street currently expects, with the ref that says so.

        Everything a citation needs is on the returned dict: the version, its
        hash, the day it was observed, the invocation that fetched it and the
        hash of the raw bytes that call returned.
        """

        latest = self.latest_version(company_ref)
        if latest is None:
            return None
        fetch = latest["fetch"]
        return {
            "company_ref": latest["company_ref"],
            "ticker": latest["ticker"],
            "currency": latest["currency"],
            "as_of": latest["as_of"],
            "version_ref": latest["id"],
            "version_hash": latest["content_hash"],
            "version": latest["version"],
            "source_ref": latest["source_ref"],
            "source_kind": latest["source_kind"],
            "observation_basis": latest["observation_basis"],
            "price_target": dict(latest["price_target"]),
            "recommendations": [dict(row) for row in latest["recommendations"]],
            "eps_estimates": [dict(row) for row in latest["eps_estimates"]],
            "revenue_estimates": [dict(row) for row in latest["revenue_estimates"]],
            "report_consensus": (
                None if latest["report_consensus"] is None
                else dict(latest["report_consensus"])
            ),
            "fiscal_calendar": dict(latest["fiscal_calendar"]),
            "invocation_ref": fetch.get("invocation_ref"),
            "artifact_hash": fetch.get("artifact_hash"),
        }

    def consensus_for_period(
        self, company_ref: str, period: str
    ) -> dict[str, Any] | None:
        """The EPS and revenue the street expects for one named fiscal period.

        ``period`` is the mapped label -- ``FY2027`` or ``FY2026Q4`` -- never
        the vendor's ``0q``. A caller that asks in the vendor's own words is
        asking a question whose answer changes every quarter without the
        question changing, which is how a consensus ends up beside the wrong
        actual.
        """

        period = _text(period, "period")
        latest = self.latest_version(company_ref)
        if latest is None:
            return None
        eps = next((row for row in latest["eps_estimates"] if row["label"] == period), None)
        revenue = next(
            (row for row in latest["revenue_estimates"] if row["label"] == period), None
        )
        if eps is None and revenue is None:
            return None
        anchor = eps or revenue
        fetch = latest["fetch"]
        return {
            "company_ref": latest["company_ref"],
            "period": period,
            "period_kind": anchor["period_kind"],
            "fiscal_year": anchor["fiscal_year"],
            "fiscal_quarter": anchor["fiscal_quarter"],
            "period_end": anchor["period_end"],
            "eps": None if eps is None else dict(eps),
            "revenue": None if revenue is None else dict(revenue),
            "as_of": latest["as_of"],
            "version_ref": latest["id"],
            "version_hash": latest["content_hash"],
            "observation_basis": latest["observation_basis"],
            "mapping_version": latest["fiscal_calendar"]["mapping_version"],
            "invocation_ref": fetch.get("invocation_ref"),
            "artifact_hash": fetch.get("artifact_hash"),
        }

    # -- publishing --------------------------------------------------------

    def publish_consensus(
        self,
        *,
        company_ref: str,
        wire: Mapping[str, Any],
        fiscal_year_end: Any,
        last_reported_period_end: Any,
        invocation_ref: str,
        artifact_hash: str,
        governance_ref: str,
        governance_hash: str,
        captured_at: str,
        actor_ref: str = ACTOR_REF,
    ) -> dict[str, Any]:
        """Record one ``analyst_estimates`` call, or say it changed nothing.

        ``wire`` is exactly what ``market_price_adapter.analyst_estimates_wire``
        produces, already checked against the frozen output schema. The mapping
        from its four relative period keys to this company's fiscal periods
        happens here, so a version cannot exist without one.
        """

        company_ref = _text(company_ref, "company_ref")
        cal = fiscal_calendar(
            fiscal_year_end=fiscal_year_end,
            last_reported_period_end=last_reported_period_end,
        )
        if not isinstance(wire, Mapping):
            raise ConsensusEstimateValidationError("wire must be an object")
        ticker = _text(wire.get("ticker"), "ticker").upper()
        if _TICKER_RE.fullmatch(ticker) is None:
            raise ConsensusEstimateValidationError("ticker is not a market symbol")
        as_of = _iso_date(wire.get("as_of"), "as_of")
        eps = _estimate_rows(wire.get("eps_estimates"), cal, "eps_estimates")
        revenue = _estimate_rows(wire.get("revenue_estimates"), cal, "revenue_estimates")
        recommendations = _recommendation_rows(
            wire.get("recommendations"), as_of, "recommendations"
        )
        target = _target_block(wire.get("price_target"), "price_target")
        currencies = {
            row["currency"] for row in (*eps, *revenue) if row["currency"] is not None
        }
        if len(currencies) > 1:
            raise ConsensusEstimateConflict(
                "this vendor reported estimates in more than one currency; a "
                "consensus that mixes them is not a consensus"
            )
        currency = next(iter(currencies), None)
        if not eps and not revenue and not recommendations and all(
            value is None for value in target.values()
        ):
            # Yahoo can drop every block at once. That is a failed read, not a
            # company with no coverage, and publishing it would erase a real
            # consensus behind an empty version.
            raise ConsensusEstimateValidationError(
                "the vendor returned no target, no ratings and no estimates; "
                "there is nothing to publish"
            )
        return self._publish(
            company_ref=company_ref,
            source_kind=VENDOR,
            source_ref=SOURCE_REF,
            observation_basis=OBSERVATION_BASIS,
            ticker=ticker,
            currency=currency,
            as_of=as_of,
            cal=cal,
            price_target=target,
            recommendations=recommendations,
            eps_estimates=eps,
            revenue_estimates=revenue,
            report_consensus=None,
            carry_report_consensus=True,
            fetch={
                "invocation_ref": _text(invocation_ref, "invocation_ref"),
                "artifact_hash": _hash(artifact_hash, "artifact_hash"),
                "governance_ref": _text(governance_ref, "governance_ref"),
                "governance_hash": _hash(governance_hash, "governance_hash"),
                "captured_at": _rfc3339(captured_at, "captured_at"),
                "observed_on": as_of,
            },
            actor_ref=_text(actor_ref, "actor_ref"),
        )

    def publish_report_consensus(
        self,
        *,
        company_ref: str,
        report_consensus: Mapping[str, Any],
        actor_ref: str = ACTOR_REF,
    ) -> dict[str, Any]:
        """Attach a corroborated sell-side range to this company's chain.

        The block itself is built by ``street_estimate.report_consensus``,
        which is where the two-independent-brokers rule lives. This only refuses
        to store one that does not carry its own evidence, and refuses to open a
        chain with it: a range of two brokers' targets is a fact about the
        street's spread, and a company whose consensus chain begins with it
        would have no vendor observation to read it against.
        """

        company_ref = _text(company_ref, "company_ref")
        block = validate_report_consensus(report_consensus)
        latest = self.latest_version(company_ref)
        if latest is None:
            raise ConsensusEstimateConflict(
                "a report consensus attaches to an existing consensus chain; "
                "this company has no vendor observation yet"
            )
        return self._publish(
            company_ref=company_ref,
            source_kind=REPORTS,
            source_ref=REPORT_SOURCE_REF,
            observation_basis=REPORT_BASIS,
            ticker=latest["ticker"],
            currency=latest["currency"],
            as_of=block["as_of"],
            cal=latest["fiscal_calendar"],
            price_target=dict(latest["price_target"]),
            recommendations=[dict(row) for row in latest["recommendations"]],
            eps_estimates=[dict(row) for row in latest["eps_estimates"]],
            revenue_estimates=[dict(row) for row in latest["revenue_estimates"]],
            report_consensus=block,
            carry_report_consensus=False,
            fetch=dict(latest["fetch"]),
            actor_ref=_text(actor_ref, "actor_ref"),
        )

    def _publish(self, **kwargs: Any) -> dict[str, Any]:
        consensus_ref = consensus_ref_for(kwargs["company_ref"])
        with self.store._transaction() as cur:
            return self._merge_and_insert(cur, consensus_ref=consensus_ref, **kwargs)

    def _merge_and_insert(
        self, cur: sqlite3.Cursor, *, consensus_ref: str, company_ref: str,
        source_kind: str, source_ref: str, observation_basis: str, ticker: str,
        currency: str | None, as_of: str, cal: Mapping[str, Any],
        price_target: Mapping[str, Any], recommendations: list[dict[str, Any]],
        eps_estimates: list[dict[str, Any]], revenue_estimates: list[dict[str, Any]],
        report_consensus: dict[str, Any] | None, carry_report_consensus: bool,
        fetch: Mapping[str, Any], actor_ref: str,
    ) -> dict[str, Any]:
        row = cur.execute(
            "SELECT * FROM consensus_estimate_versions WHERE consensus_ref=? "
            "ORDER BY version_number DESC LIMIT 1",
            (consensus_ref,),
        ).fetchone()
        latest = None if row is None else _decode(
            row, f"latest ConsensusEstimateVersion for {company_ref}")
        if latest is not None:
            if latest["ticker"] != ticker:
                raise ConsensusEstimateConflict(
                    "this company's consensus is held under a different ticker"
                )
            if carry_report_consensus:
                # A vendor observation does not erase a report range it knows
                # nothing about; it carries it forward untouched.
                report_consensus = latest["report_consensus"]
        identity = {
            "consensus_ref": consensus_ref,
            "company_ref": company_ref,
            "ticker": ticker,
            "source_ref": source_ref,
            "source_kind": source_kind,
            "currency": currency,
            "as_of": as_of,
            "fiscal_calendar": dict(cal),
            "price_target": dict(price_target),
            "recommendations": recommendations,
            "eps_estimates": eps_estimates,
            "revenue_estimates": revenue_estimates,
            "report_consensus": report_consensus,
        }
        changed = _changed_fields(latest, identity)
        if latest is not None and not changed:
            # The street said the same thing it said yesterday. That is the
            # common case and it is not a new fact; writing a version for it
            # would make the chain a log of ticks rather than of revisions.
            return {"status": "duplicate", **latest}
        version = 1 if latest is None else latest["version"] + 1
        prior_version_ref = None if latest is None else latest["id"]
        identity = {
            **identity,
            "version": version,
            "prior_version_ref": prior_version_ref,
        }
        version_id = _ref("consensus-estimate-version", identity)
        wire = _record({
            "schema_version": SCHEMA_VERSION,
            "id": version_id,
            "created_at": _now(),
            **identity,
            # More evidence about the same periods. The closed vocabulary is
            # the forecast driver's, so a reader of any versioned product in
            # this system meets the same five reasons.
            "change_reason": "evidence_thicker",
            "changed_fields": changed,
            # What these numbers are. A vendor's report of what the street
            # thinks, read on a day -- not a figure the company published.
            "observation_basis": observation_basis,
            "fetch": dict(fetch),
            "actor_ref": actor_ref,
        })
        if wire["change_reason"] not in CHANGE_REASONS:  # pragma: no cover - constant
            raise ConsensusEstimateValidationError("change_reason is not in the vocabulary")
        try:
            cur.execute(
                "INSERT INTO consensus_estimate_versions "
                "(version_id,consensus_ref,version_number,prior_version_id,company_ref,"
                "ticker,source_ref,source_kind,change_reason,currency,as_of,"
                "target_price_mean,analyst_count,eps_period_count,revenue_period_count,"
                "record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id, consensus_ref, version, prior_version_ref, company_ref,
                    ticker, source_ref, source_kind, wire["change_reason"], currency,
                    as_of, wire["price_target"]["mean"],
                    wire["price_target"]["number_of_analysts"],
                    len(eps_estimates), len(revenue_estimates),
                    canonical_json(wire), wire["content_hash"], actor_ref,
                    wire["created_at"],
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConsensusEstimateConflict(
                "another run published this company's next consensus version first"
            ) from exc
        stored = self._decode_version(cur, version_id)
        if stored != wire:
            raise ConsensusEstimateConflict(
                "stored consensus estimate version does not read back"
            )
        return {"status": "fresh", **stored}


# -- the reader P15d resolves by name ---------------------------------------
#
# ``conviction_call_cli.consensus_gap`` looks this module up at call time and
# asks for a module-level ``latest_consensus(store, company_ref)``. It was
# written while this authority was still Wave 2 work, deliberately by name
# rather than by import, so that the conviction lane would start working the
# day the authority landed. This is that day, so the function it names exists
# here rather than the lane being edited.
#
# It answers with *our forecast against the street's*, which needs both sides.
# The street's side is this authority. Ours is the forecast model, joined on
# the one thing the two can agree about without a shared vocabulary: the fiscal
# period **end date**, which this authority computes from the company's own
# filings and the forecast model carries on every cell. Label matching would
# not do -- "FY2027" means one thing here and is not what the forecast model
# calls its columns -- and a join on a guess is how a gap gets computed against
# the wrong year.
#
# No forecast, or no overlapping period, means no metrics and no gap. That is
# reported as an absence rather than as agreement: a conviction call whose
# consensus section quietly vanished would read as "we agree with the street",
# which is the one thing it must never accidentally say.
_EPS_WORDS = ("eps", "earnings per share")
_REVENUE_WORDS = ("revenue", "revenues", "net revenue", "sales")
MAX_GAP_METRICS = 12
# Changing the join changes the answer even when neither source version did.
# It is returned beside every gap and folded into the sensitivity rule hash,
# so projections made by the old end-date-only join become visibly stale.
CONSENSUS_GAP_RULE_REF = "rule:consensus-fiscal-period-join:1"


def _forecast_cells(store: Any, company_ref: str) -> tuple[dict[Any, Any], str | None]:
    """Our quarterly estimates keyed by metric and exact fiscal window.

    A fiscal Q4 and its fiscal year end on the same day. End date alone loses
    the fact that prevents a quarterly value from being compared with an
    annual consensus value, so cells without a typed company-fiscal quarter
    identity are unavailable to this join.
    """

    try:
        from .model_forecast_driver import ForecastModelAuthority

        latest = ForecastModelAuthority(store).latest(company_ref)
    except Exception:  # noqa: BLE001 - no model here is no gap, not an outage
        return {}, None
    if not latest:
        return {}, None
    found: dict[str, Any] = {}
    for line in latest.get("results") or ():
        if not isinstance(line, Mapping):
            continue
        name = f"{line.get('role') or ''} {line.get('label') or ''}".lower()
        if any(word in name for word in _EPS_WORDS):
            metric = "eps"
        elif any(word in name for word in _REVENUE_WORDS):
            metric = "revenue"
        else:
            continue
        for cell in line.get("cells") or ():
            if not isinstance(cell, Mapping):
                continue
            # A cell that could not be computed is not a number.
            if cell.get("status") != "computed":
                continue
            # D: a superseded cell is the estimate an actual replaced. The
            # owner's versioning rule keeps it rather than overwriting it, so
            # it is still on the model and still looks like a forecast; reading
            # it would compare the street to a number we ourselves no longer
            # hold.
            if cell.get("superseded_by"):
                continue
            period = cell.get("period")
            if not isinstance(period, Mapping):
                continue
            start = period.get("start")
            end = period.get("end")
            value = cell.get("value")
            kind = cell.get("kind")
            if (
                period.get("calendar") != "company:fiscal"
                or period.get("kind") != "quarter"
                or not isinstance(start, str)
                or not isinstance(end, str)
                or value is None
                or kind not in CELL_KINDS
            ):
                continue
            try:
                if date.fromisoformat(start) > date.fromisoformat(end):
                    continue
            except ValueError:
                continue
            key = (metric, "quarter", start, end)
            standing = found.get(key)
            # D: prefer the actualised cell. Once a period has been reported,
            # our number for it is what happened, not what we expected -- and
            # the street's estimate beside it is then a record of what the
            # street expected, which is the more interesting row of the two.
            if standing is not None and not (
                kind == "actual" and standing["kind"] == "estimate"
            ):
                continue
            found[key] = {
                "value": str(value),
                "unit": str(line.get("unit") or ""),
                "label": str(line.get("label") or metric),
                "kind": kind,
                "period": dict(period),
            }
    return found, str(latest.get("id") or latest.get("version_ref") or "") or None


def _gap_percent(ours: str, street: str) -> str | None:
    try:
        mine, theirs = Decimal(ours), Decimal(street)
    except (InvalidOperation, ValueError):
        return None
    if not theirs.is_finite() or not mine.is_finite() or theirs == 0:
        # A gap against zero is not a percentage of anything.
        return None
    # Divided by the magnitude, not the signed value, so a positive gap always
    # means "we are above the street" -- including where both are losses, where
    # dividing by a negative would flip the sign and read as the opposite.
    gap = ((mine - theirs) / abs(theirs) * 100).quantize(Decimal("0.01"))
    return f"{gap:f}"


def _quarter_window(cal: Mapping[str, Any], end: date) -> tuple[str, str]:
    """Return the exact company-fiscal quarter ending on ``end``."""

    year, month = _shift(end.year, end.month, -3)
    previous_end = _period_end(cal, year, month)
    return (previous_end + timedelta(days=1)).isoformat(), end.isoformat()


def _ours_for_street_period(
    ours: Mapping[Any, Any], metric: str, street: Mapping[str, Any],
    cal: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve one street period without crossing quarter/year boundaries."""

    try:
        mapped = map_estimate_period(str(street["vendor_period"]), cal)
    except (KeyError, FiscalMappingError, ValueError):
        return None, "street_period_mapping_unavailable"
    for field in ("period_kind", "fiscal_year", "fiscal_quarter", "period_end", "label"):
        if street.get(field) != mapped[field]:
            return None, "street_period_mapping_drifted"
    try:
        end = date.fromisoformat(str(mapped["period_end"]))
    except ValueError:
        return None, "street_period_end_invalid"

    if mapped["period_kind"] == "quarter":
        start, end_text = _quarter_window(cal, end)
        found = ours.get((metric, "quarter", start, end_text))
        return (None, "forecast_quarter_unavailable") if found is None else (dict(found), None)

    # The workbook's established annual-model convention is a sum of four
    # typed duration quarters for non-ratio flow lines. The current formal
    # model has such a line for revenue but has no share-count/EPS formula;
    # quarterly EPS can be compared directly, while annual EPS remains
    # unavailable until that exact definition exists. A single Q4 is never an
    # annual proxy.
    if metric != "revenue":
        return None, "forecast_fiscal_year_aggregation_unavailable"
    quarter_ends = _quarter_ends_of(cal, end)
    quarters: list[dict[str, Any]] = []
    for quarter_end in quarter_ends:
        start, end_text = _quarter_window(cal, quarter_end)
        found = ours.get((metric, "quarter", start, end_text))
        if found is None:
            return None, "forecast_fiscal_year_incomplete"
        quarters.append(dict(found))
    units = {str(item.get("unit") or "") for item in quarters}
    labels = {str(item.get("label") or metric) for item in quarters}
    if len(units) != 1 or len(labels) != 1:
        return None, "forecast_fiscal_year_components_conflict"
    unit = next(iter(units))
    if unit == "ratio":
        return None, "forecast_fiscal_year_ratio_unavailable"
    try:
        total = sum((Decimal(str(item["value"])) for item in quarters), Decimal(0))
    except (InvalidOperation, ValueError):
        return None, "forecast_fiscal_year_value_invalid"
    if not total.is_finite():
        return None, "forecast_fiscal_year_value_invalid"
    return {
        "value": format(total, "f"), "unit": unit,
        "label": next(iter(labels)), "kind": "fiscal_year_sum",
        "period": {
            "calendar": "company:fiscal", "kind": "fiscal_year",
            "start": _quarter_window(cal, quarter_ends[0])[0],
            "end": end.isoformat(),
        },
    }, None


def latest_consensus(store: Any, company_ref: str) -> dict[str, Any] | None:
    """Our forecast against the street's, for one company. See the note above."""

    try:
        held = ConsensusEstimateAuthority(store).latest_consensus(company_ref)
    except Exception:  # noqa: BLE001 - an unreadable street is no street
        return None
    if not held:
        return None
    ours, forecast_ref = _forecast_cells(store, company_ref)
    if not ours:
        return {"metrics": []}
    refs = [held["version_ref"]] + ([forecast_ref] if forecast_ref else [])
    rows: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    cal = held["fiscal_calendar"]
    for metric, key in (("eps", "eps_estimates"), ("revenue", "revenue_estimates")):
        for street in held[key]:
            mine, reason = _ours_for_street_period(ours, metric, street, cal)
            if mine is None:
                unavailable.append({
                    "metric": metric, "period": street["label"],
                    "period_kind": street["period_kind"], "reason": reason,
                    "refs": list(refs),
                })
                continue
            if street["avg"] is None:
                unavailable.append({
                    "metric": metric, "period": street["label"],
                    "period_kind": street["period_kind"],
                    "reason": "street_estimate_unavailable", "refs": list(refs),
                })
                continue
            percent = _gap_percent(mine["value"], street["avg"])
            if percent is None:
                continue
            rows.append({
                "metric": mine["label"],
                "period": street["label"],
                "ours": mine["value"],
                "consensus": street["avg"],
                "unit": mine["unit"] or (street["currency"] or "unit"),
                "gap_percent": percent,
                "refs": list(refs),
            })
    rows.sort(key=lambda row: (row["period"], row["metric"]))
    unavailable.sort(key=lambda row: (row["period"], row["metric"]))
    return {
        "rule_ref": CONSENSUS_GAP_RULE_REF,
        "metrics": rows[:MAX_GAP_METRICS],
        "unavailable_periods": unavailable,
    }


def report_consensus(
    store: Any, company_ref: str, *, as_of: str | None = None
) -> list[dict[str, Any]]:
    """The sell-side range as one row per house, or nothing when there is none.

    The second reader resolved by name against this module -- P13-M3's
    sensitivity work asks for ``{broker, value, refs}`` per house and expects
    two distinct houses or nothing. It is a thin adapter over
    ``street_estimate.report_consensus``, which is where the rule lives and
    where it stays: corroboration is counted in distinct houses, never in
    notes, so two notes from one house come back as nothing at all rather than
    as a range of one opinion with itself.

    Exactly three keys per row, because a caller that closed the shape would
    refuse a fourth. The currency is not among them and does not need to be:
    the rule below refuses a range that mixes two, so every row here is in the
    same one.

    An empty list is the honest answer for a company the street has published
    one view on, and for a Core with no street estimates at all. Nothing here
    raises: an unreadable street is no street, not an outage.
    """

    try:
        from .street_estimate import (
            StreetEstimateStore,
            report_consensus as street_range,
        )

        held = StreetEstimateStore(store).estimates(_text(company_ref, "company_ref"))
    except Exception:  # noqa: BLE001 - no notes here is no range
        return []
    if not held:
        return []
    day = as_of or datetime.now(timezone.utc).date().isoformat()
    try:
        block = street_range(held, as_of=day)
    except Exception:  # noqa: BLE001
        return []
    if not block:
        return []
    counted = set(block["brokers"])
    newest: dict[str, Mapping[str, Any]] = {}
    for item in held:
        broker = str(item.get("broker") or "")
        target = item.get("target_price")
        if broker not in counted or not isinstance(target, Mapping):
            continue
        # The same house inside the window contributes its newest note, which
        # is the rule the range was built under; picking a different one here
        # would make the rows disagree with the range they came from.
        standing = newest.get(broker)
        # Strict: two notes from one house on one day keep the first the store
        # returned, which is a stable order rather than an arbitrary one.
        if standing is None or str(standing["published_on"]) < str(item["published_on"]):
            newest[broker] = item
    rows = [
        {
            "broker": broker,
            "value": str(item["target_price"]["value"]),
            "refs": [str(item["id"]), str(item["document_ref"])],
        }
        for broker, item in newest.items()
    ]
    rows.sort(key=lambda row: row["broker"])
    return rows


def validate_report_consensus(value: Any) -> dict[str, Any]:
    """The closed shape of a range computed from sell-side reports."""

    block = _closed(value, {
        "metric", "period", "as_of", "window_days", "broker_count", "brokers",
        "low", "high", "mean", "currency", "estimate_refs", "document_refs",
        "policy_ref", "policy_hash",
    }, "report_consensus")
    block["metric"] = _text(block["metric"], "report_consensus.metric")
    block["period"] = _text(block["period"], "report_consensus.period")
    block["as_of"] = _iso_date(block["as_of"], "report_consensus.as_of")
    window = block["window_days"]
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ConsensusEstimateValidationError(
            "report_consensus.window_days must be a positive integer"
        )
    brokers = block["brokers"]
    if not isinstance(brokers, Sequence) or isinstance(brokers, (str, bytes)):
        raise ConsensusEstimateValidationError("report_consensus.brokers must be an array")
    brokers = sorted({_text(item, "report_consensus.brokers[]") for item in brokers})
    if len(brokers) < 2:
        raise ConsensusEstimateValidationError(
            "a report consensus needs two independent brokers; one broker's "
            "target is one broker's opinion, whatever it is called"
        )
    if block["broker_count"] != len(brokers):
        raise ConsensusEstimateValidationError(
            "report_consensus.broker_count must count the brokers it names"
        )
    block["brokers"] = brokers
    for field in ("low", "high", "mean"):
        block[field] = _optional_decimal(block[field], f"report_consensus.{field}")
        if block[field] is None:
            raise ConsensusEstimateValidationError(
                f"report_consensus.{field} is required"
            )
    if Decimal(block["low"]) > Decimal(block["high"]):
        raise ConsensusEstimateConflict("report_consensus low is above its high")
    block["currency"] = _optional_currency(
        block["currency"], "report_consensus.currency"
    )
    for field in ("estimate_refs", "document_refs"):
        refs = block[field]
        if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)):
            raise ConsensusEstimateValidationError(
                f"report_consensus.{field} must be an array"
            )
        block[field] = sorted({_text(item, f"report_consensus.{field}[]") for item in refs})
        if len(block[field]) < 2:
            raise ConsensusEstimateValidationError(
                f"report_consensus.{field} must name one per broker at least"
            )
    block["policy_ref"] = _text(block["policy_ref"], "report_consensus.policy_ref")
    block["policy_hash"] = _hash(block["policy_hash"], "report_consensus.policy_hash")
    return block


__all__ = [
    "ACTOR_REF",
    "ESTIMATE_FIELDS",
    "ESTIMATE_PERIOD_KEYS",
    "FISCAL_MAPPING_VERSION",
    "MAX_ESTIMATE_ROWS",
    "MAX_RECOMMENDATION_ROWS",
    "OBSERVATION_BASIS",
    "RECOMMENDATION_FIELDS",
    "RECOMMENDATION_PERIOD_KEYS",
    "REPORTS",
    "REPORT_BASIS",
    "REPORT_SOURCE_REF",
    "SCHEMA_VERSION",
    "SOURCE_KINDS",
    "SOURCE_REF",
    "TARGET_FIELDS",
    "VENDOR",
    "ConsensusEstimateAuthority",
    "ConsensusEstimateConflict",
    "ConsensusEstimateError",
    "ConsensusEstimateNotFound",
    "ConsensusEstimateValidationError",
    "FiscalMappingError",
    "consensus_ref_for",
    "MAX_GAP_METRICS",
    "fiscal_calendar",
    "fiscal_year_end_from_quarters",
    "latest_consensus",
    "map_estimate_period",
    "report_consensus",
    "map_recommendation_period",
    "validate_report_consensus",
]
