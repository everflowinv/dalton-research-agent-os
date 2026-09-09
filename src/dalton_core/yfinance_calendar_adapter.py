"""C1: the yfinance calendar call, and the calendar entries it becomes.

Kept apart from the child that runs it so the normalisation can be tested
without a network, a store or a subprocess: a fixture in, a wire out, entries
after that.

Three things this deliberately does not do.

**It does not carry consensus figures.** ``Ticker.calendar`` returns the
earnings and revenue estimate beside the dates, and ``analyst_estimates``
already carries both. Two operations claiming the same number is how the two of
them come to disagree, and the calendar's job is dates.

**It does not use ``Ticker.earnings_dates``.** That accessor scrapes
``finance.yahoo.com``, which is not on this connector's host allowlist --
``query1`` and ``query2`` are. Reaching a host the approval does not name is
not a thing to do quietly because the data is convenient.

**It does not treat a past date as a forthcoming event.** Yahoo's
``Ex-Dividend Date`` and ``Dividend Date`` are the *last* ones it knows, not
the next: DXC still reports an ex-date of 2020-03-23, six years after it
stopped paying a dividend, and Accenture's was two months ago. A calendar that
put those on the strip as expected events would be inventing a future out of a
past, so a dividend date that has already passed produces no entry at all. The
wire keeps the date verbatim -- the artifact is the call -- and this is the
layer that decides what it means.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from .market_price_adapter import MarketDataAdapterError, json_safe
from .yfinance_core import ADAPTER_LIBRARY, CALENDAR_OPERATION, SOURCE_REF

RAW_SCHEMA_VERSION = "0.1"
WIRE_SCHEMA_VERSION = "0.1"

# Yahoo's own keys in the calendar dict, which the library builds by hand.
EARNINGS_FIELD = "Earnings Date"
DIVIDEND_FIELD = "Dividend Date"
EX_DIVIDEND_FIELD = "Ex-Dividend Date"
# One date, or the two ends of a window Yahoo could not narrow. Three would be
# a shape this contract does not describe, and guessing which two to keep is
# how a wrong date gets stored quietly.
MAX_EARNINGS_DATES = 2


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _captured_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _library() -> Any:
    """Import lazily, so a Core without the extra refuses with a reason."""

    try:
        import yfinance  # noqa: PLC0415 - deliberately lazy
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise MarketDataAdapterError(
            "the market-data library is not installed; "
            "install this package with the [market-data] extra"
        ) from exc
    return yfinance


# -- fetching --------------------------------------------------------------


def fetch_calendar(ticker: str) -> dict[str, Any]:
    """One company's dated corporate events, as Yahoo currently has them.

    One physical call, which is what the quota policy budgets:
    ``Ticker.calendar`` is a single ``quoteSummary`` request for the
    ``calendarEvents`` module against the hosts this connector declares.
    """

    yfinance = _library()
    handle = yfinance.Ticker(ticker)
    raw: dict[str, Any] = {
        "schema_version": RAW_SCHEMA_VERSION,
        "library": ADAPTER_LIBRARY,
        "library_version": str(getattr(yfinance, "__version__", "unknown")),
        "operation": CALENDAR_OPERATION,
        "ticker": ticker,
        "observed_on": _today(),
        "captured_at": _captured_at(),
        "errors": {},
    }
    try:
        raw["calendar"] = json_safe(handle.calendar)
    except Exception as exc:  # noqa: BLE001 - one absent block, not a crash
        raw["calendar"] = None
        raw["errors"]["calendar"] = f"{type(exc).__name__}: {exc}"
    return raw


# -- normalising -----------------------------------------------------------


def _iso_date(value: Any, name: str) -> str | None:
    """A date Yahoo printed, or nothing. Never a date this code decided.

    The zone is resolved before the day is taken, not after. A stamp of
    ``2026-10-01T23:30:00-07:00`` is the second of October in UTC, and slicing
    the first ten characters off it would file an earnings date a day early --
    which, near a month boundary, also moves the quarter the whole thing gets
    labelled with.
    """

    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise MarketDataAdapterError(f"{name} is not a date")
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # Not a timestamp. A bare day, then, or nothing this can describe.
        try:
            date.fromisoformat(text[:10])
        except ValueError as exc:
            raise MarketDataAdapterError(
                f"{name} is not a YYYY-MM-DD date"
            ) from exc
        return text[:10]
    if parsed.tzinfo is None:
        # A naive stamp is a day in whatever Yahoo was thinking in. Taken as
        # written rather than assumed to be UTC: shifting it would be inventing
        # a zone, and the day it printed is the honest reading.
        return parsed.date().isoformat()
    return parsed.astimezone(timezone.utc).date().isoformat()


def calendar_wire(
    raw: dict[str, Any], *, source_record_refs: list[str]
) -> dict[str, Any]:
    """The dates, in the shape the frozen contract froze."""

    ticker = str(raw.get("ticker") or "").strip()
    if not ticker:
        raise MarketDataAdapterError("the raw output names no ticker")
    observed_on = str(raw.get("observed_on") or "").strip()
    if len(observed_on) != 10:
        raise MarketDataAdapterError("the raw output carries no observation date")
    captured_at = raw.get("captured_at")
    if not isinstance(captured_at, str) or not captured_at.strip():
        captured_at = f"{observed_on}T00:00:00+00:00"
    calendar = raw.get("calendar")
    if calendar is None:
        # Yahoo knowing nothing about a company's diary is a real answer and
        # not a failed run. It produces a wire with no dates in it, and a
        # publish that adds nothing.
        calendar = {}
    if not isinstance(calendar, dict):
        raise MarketDataAdapterError("the raw output's calendar is not an object")

    earnings_raw = calendar.get(EARNINGS_FIELD)
    if earnings_raw is None:
        earnings_raw = []
    elif not isinstance(earnings_raw, (list, tuple)):
        earnings_raw = [earnings_raw]
    if len(earnings_raw) > MAX_EARNINGS_DATES:
        raise MarketDataAdapterError(
            f"Yahoo named {len(earnings_raw)} earnings dates; this contract "
            f"describes at most {MAX_EARNINGS_DATES}"
        )
    earnings: list[str] = []
    for index, item in enumerate(earnings_raw):
        parsed = _iso_date(item, f"{EARNINGS_FIELD}[{index}]")
        if parsed is not None and parsed not in earnings:
            earnings.append(parsed)
    earnings.sort()

    return {
        "schema_version": WIRE_SCHEMA_VERSION,
        "ticker": ticker.upper(),
        "as_of": observed_on,
        "captured_at": captured_at,
        "earnings_dates": earnings,
        "dividend_date": _iso_date(calendar.get(DIVIDEND_FIELD), DIVIDEND_FIELD),
        "ex_dividend_date": _iso_date(
            calendar.get(EX_DIVIDEND_FIELD), EX_DIVIDEND_FIELD
        ),
        "source_record_refs": list(source_record_refs),
        "next_cursor": None,
        "provider_status": 200,
    }


# -- becoming calendar entries ---------------------------------------------


def calendar_entries(
    wire: dict[str, Any], *, invocation_ref: str, note: str = ""
) -> list[dict[str, Any]]:
    """What this reading of Yahoo contributes to the calendar.

    Everything here is ``estimated``: Yahoo does not say where its date came
    from, and a date whose provenance is "a vendor had it" is not a date the
    company has committed to.

    **A window is one event, not two.** When Yahoo gives two dates it is naming
    the ends of a range it could not narrow -- one earnings call, somewhere in
    there. This used to emit an entry per date, which was wrong twice over: it
    put two results announcements on the strip where the company will make one,
    and because both carried the same vendor as their source, the two of them
    folded back into a single occurrence carrying that source twice, which the
    authority refuses. A company whose date Yahoo could not pin down therefore
    failed the run every day until the lane gave up on it.

    So one entry, dated at the **earlier** end -- the calendar is about when to
    be ready and being ready early costs nothing -- and the range said out loud
    in the note, where a person and the preview both see it.
    """

    observed_at = wire["captured_at"]
    entries: list[dict[str, Any]] = []

    def source(day: str) -> dict[str, Any]:
        return {
            "kind": "connector_invocation",
            "ref": invocation_ref,
            "source_ref": SOURCE_REF,
            "observed_date": day,
            "observed_at": observed_at,
            "confidence": "estimated",
            "note": note,
        }

    days = wire["earnings_dates"]
    if days:
        entries.append({
            "event_kind": "earnings",
            "sources": [source(days[0])],
            "notes": (
                f"Yahoo gave a window rather than a day: {days[0]} to "
                f"{days[-1]}. The earlier end is the date carried."
                if len(days) > 1 else ""
            ),
        })

    ex_dividend = wire["ex_dividend_date"]
    if ex_dividend is not None and ex_dividend > wire["as_of"]:
        # Only a *forthcoming* ex-date. Yahoo reports the last one it knows,
        # which for a company that stopped paying is years in the past; putting
        # that on a calendar of expected events would be a fabricated future.
        entries.append({
            "event_kind": "ex_dividend",
            "sources": [source(ex_dividend)],
            "notes": "",
        })

    return entries


__all__ = [
    "DIVIDEND_FIELD",
    "EARNINGS_FIELD",
    "EX_DIVIDEND_FIELD",
    "MAX_EARNINGS_DATES",
    "RAW_SCHEMA_VERSION",
    "WIRE_SCHEMA_VERSION",
    "calendar_entries",
    "calendar_wire",
    "fetch_calendar",
]
