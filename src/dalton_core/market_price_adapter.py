"""P11a: the two yfinance calls, and the wires they normalise to.

Kept apart from the child that runs them so the normalisation can be tested
without a network, a store or a subprocess: a fixture in, a wire out.

Three things here exist because the owner's other tooling learned them the hard
way, and they are not preferences:

``auto_adjust=False``, always. Yahoo's default silently replaces Close with the
split-and-dividend-adjusted series and drops Adj Close entirely, so a later
reader holds one column and cannot tell which one it is. Both are fetched and
both are stored under their own names.

**Never add dividends on top of an adjusted price.** Adj Close already contains
them. Nothing here computes a total return, and the two columns are separated
precisely so that whoever eventually does cannot do it twice by accident.

**Flatten the MultiIndex.** ``yf.download`` returns columns keyed by
``(field, ticker)`` even for a single ticker, so ``row["Close"]`` is a
``KeyError`` and ``row[("Close", "ACN")]`` is a shape that changes the moment
someone requests two tickers. The frame is flattened to field names once, here,
rather than at every use site.

The raw library output is kept whole and hashed before any of this runs; see
``market_price_cli``. What follows only decides which parts of it enter the
frozen contract.
"""

from __future__ import annotations

import math
import struct
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .yfinance_core import (
    ANALYST_ESTIMATES_OPERATION,
    DAILY_PRICES_OPERATION,
    ADAPTER_LIBRARY,
)

RAW_SCHEMA_VERSION = "0.1"
WIRE_SCHEMA_VERSION = "0.1"
# Yahoo's own column names, in the order the library returns them.
PRICE_COLUMNS = ("Open", "High", "Low", "Close", "Adj Close", "Volume")
_WIRE_BY_COLUMN = {
    "Open": "open", "High": "high", "Low": "low",
    "Close": "close", "Adj Close": "adj_close", "Volume": "volume",
}
# Yahoo publishes these under one name in ``info`` and nowhere else.
SHARES_FIELD = "sharesOutstanding"
MARKET_CAP_FIELD = "marketCap"
CURRENCY_FIELD = "currency"
ANALYST_COUNT_FIELD = "numberOfAnalystOpinions"
_ESTIMATE_COLUMNS = {
    "avg": "avg", "low": "low", "high": "high",
    "numberOfAnalysts": "number_of_analysts", "growth": "growth",
    "currency": "currency",
}
_RECOMMENDATION_COLUMNS = {
    "strongBuy": "strong_buy", "buy": "buy", "hold": "hold",
    "sell": "sell", "strongSell": "strong_sell",
}
MAX_BARS = 6000


class MarketDataAdapterError(RuntimeError):
    """The source did not return something this contract can describe."""


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def json_safe(value: Any) -> Any:
    """Plain JSON for the artifact, including whatever the library hands back.

    Dataframe cells arrive as numpy scalars and absent ones as float NaN. NaN
    is not JSON and is not a number: it is the library's way of saying Yahoo
    had nothing there, so it becomes null. Anything else unrecognised is
    stringified rather than dropped -- the artifact is the call, and losing part
    of it would defeat keeping it.
    """

    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, float):
        # ``float(...)`` rather than the value itself: numpy's float64 passes
        # this test and reprs as ``np.float64(186.93)``, which is not a number
        # a Decimal will accept. One coercion here beats finding out later.
        return None if math.isnan(value) or math.isinf(value) else float(value)
    if isinstance(value, int):
        return int(value)
    converter = getattr(value, "item", None)
    if callable(converter):
        try:
            return json_safe(converter())
        except (TypeError, ValueError):
            pass
    text = str(value)
    return None if text in {"nan", "NaN", "<NA>", "NaT", "None"} else text


def _decimal_text(value: Any, name: str) -> str:
    """A price as text, so the number stored is the number that printed.

    A float round-trips differently on different machines and different library
    versions, and a series whose last bit moves is a restatement as far as the
    version chain is concerned. Text once, here.
    """

    if isinstance(value, bool) or value is None:
        raise MarketDataAdapterError(f"{name} is absent")
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise MarketDataAdapterError(f"{name} is not a finite number")
        parsed = Decimal(_float_text(float(value)))
    elif isinstance(value, str):
        try:
            parsed = Decimal(value.strip())
        except Exception as exc:  # noqa: BLE001 - InvalidOperation and friends
            raise MarketDataAdapterError(f"{name} is not a number") from exc
    else:
        raise MarketDataAdapterError(f"{name} is not a number")
    if not parsed.is_finite():
        raise MarketDataAdapterError(f"{name} is not a finite number")
    return _format(parsed)


def _float_text(value: float) -> str:
    """The decimal that printed, not the double that carries it.

    Yahoo serves single-precision numbers widened to doubles, so Accenture
    opening at 183.78 arrives as ``183.77999877929688``. Storing that verbatim
    would put a number no exchange ever printed into every citation, and would
    make two library versions that widen differently look like a restatement.

    So: when the double is *exactly* a float32 -- which is how the widening is
    recognised, not guessed at -- the shortest decimal that round-trips to that
    same float32 is the number. When it is not exactly a float32, the source
    genuinely had double precision and nothing is touched. No rounding rule, no
    decimal-places constant to be wrong about a sub-penny quote or a
    split-adjusted price from 1998.
    """

    if struct.unpack("f", struct.pack("f", value))[0] != value:
        return repr(value)
    for digits in range(1, 10):
        candidate = f"{value:.{digits}g}"
        if struct.unpack("f", struct.pack("f", float(candidate)))[0] == value:
            return candidate
    return repr(value)


def _format(value: Decimal) -> str:
    if value == 0:
        return "0"
    raw = format(value, "f")
    if "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    return raw or "0"


def _optional_decimal(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return _decimal_text(value, "estimate")
    except MarketDataAdapterError:
        return None


def _optional_count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value) or value < 0:
            return None
        return int(value)
    if isinstance(value, int):
        return value if value >= 0 else None
    return None


# -- fetching --------------------------------------------------------------


def _library() -> Any:
    """Import lazily, so a Core without the extra refuses with a reason.

    The same shape as the SEC statements parser: a missing optional dependency
    is a run that says what is missing, not a module that will not import.
    """

    try:
        import yfinance  # noqa: PLC0415 - deliberately lazy
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise MarketDataAdapterError(
            "the market-data library is not installed; "
            "install this package with the [market-data] extra"
        ) from exc
    return yfinance


def fetch_daily_prices(ticker: str, *, start: str, end: str) -> dict[str, Any]:
    """One company's daily bars, plus the share count and market cap Yahoo knows.

    Two physical calls, which is what the quota policy budgets: the price
    download, and the metadata read that carries the share count. ``end`` is
    exclusive in Yahoo's window, and is passed through unchanged rather than
    quietly nudged, so the artifact records the window that was actually asked
    for.
    """

    yfinance = _library()
    frame = yfinance.download(
        ticker, start=start, end=end, auto_adjust=False,
        progress=False, actions=False,
    )
    columns = list(getattr(frame.columns, "get_level_values", lambda _level: frame.columns)(0))
    frame = frame.copy()
    frame.columns = columns
    rows: list[dict[str, Any]] = []
    for stamp, row in frame.iterrows():
        record: dict[str, Any] = {"date": str(stamp)[:10]}
        for column in columns:
            record[column] = json_safe(row[column])
        rows.append(record)
    metadata: dict[str, Any] = {}
    try:
        info = yfinance.Ticker(ticker).info or {}
    except Exception as exc:  # noqa: BLE001 - metadata is not the bars
        info = {}
        metadata["info_error"] = f"{type(exc).__name__}: {exc}"
    for field in (
        CURRENCY_FIELD, SHARES_FIELD, MARKET_CAP_FIELD, "quoteType",
        "shortName", "exchange", "regularMarketTime", ANALYST_COUNT_FIELD,
    ):
        metadata[field] = json_safe(info.get(field))
    return {
        "schema_version": RAW_SCHEMA_VERSION,
        "library": ADAPTER_LIBRARY,
        "library_version": str(getattr(yfinance, "__version__", "unknown")),
        "operation": DAILY_PRICES_OPERATION,
        "ticker": ticker,
        "requested_start": start,
        "requested_end": end,
        "auto_adjust": False,
        "observed_on": _today(),
        "columns": columns,
        "rows": rows,
        "metadata": metadata,
    }


def fetch_analyst_estimates(ticker: str) -> dict[str, Any]:
    """What the sell side says: targets, ratings, and forward EPS and revenue.

    Four physical calls against four separate Yahoo endpoints, which is what
    the quota budgets. Each block is captured independently, because Yahoo
    drops one without dropping the others and a missing target should not cost
    the recommendations.
    """

    yfinance = _library()
    handle = yfinance.Ticker(ticker)
    raw: dict[str, Any] = {
        "schema_version": RAW_SCHEMA_VERSION,
        "library": ADAPTER_LIBRARY,
        "library_version": str(getattr(yfinance, "__version__", "unknown")),
        "operation": ANALYST_ESTIMATES_OPERATION,
        "ticker": ticker,
        "observed_on": _today(),
        "errors": {},
    }
    blocks: dict[str, Any] = {
        "price_target": lambda: handle.analyst_price_targets,
        "recommendations": lambda: handle.recommendations,
        "earnings_estimate": lambda: handle.earnings_estimate,
        "revenue_estimate": lambda: handle.revenue_estimate,
    }
    for name, accessor in blocks.items():
        try:
            value = accessor()
        except Exception as exc:  # noqa: BLE001 - one absent block, not a failed run
            raw[name] = None
            raw["errors"][name] = f"{type(exc).__name__}: {exc}"
            continue
        if value is None:
            raw[name] = None
        elif hasattr(value, "to_dict"):
            raw[name] = json_safe(
                value.to_dict("index") if name != "recommendations"
                else value.to_dict("records")
            )
        else:
            raw[name] = json_safe(value)
    try:
        info = handle.info or {}
    except Exception as exc:  # noqa: BLE001
        info = {}
        raw["errors"]["info"] = f"{type(exc).__name__}: {exc}"
    raw["metadata"] = {
        ANALYST_COUNT_FIELD: json_safe(info.get(ANALYST_COUNT_FIELD)),
        CURRENCY_FIELD: json_safe(info.get(CURRENCY_FIELD)),
    }
    return raw


# -- normalising -----------------------------------------------------------


def daily_prices_wire(
    raw: dict[str, Any], *, source_record_refs: list[str]
) -> dict[str, Any]:
    """The bars and the dated observations, in the shape the contract froze."""

    ticker = str(raw.get("ticker") or "").strip()
    if not ticker:
        raise MarketDataAdapterError("the raw output names no ticker")
    if raw.get("auto_adjust") is not False:
        # Belt and braces: a captured artifact that was taken with adjustment
        # on cannot be replayed into this contract, because its Close is not a
        # Close.
        raise MarketDataAdapterError(
            "this artifact was taken with auto_adjust on; its Close is the "
            "adjusted series and cannot be stored as a Close"
        )
    metadata = raw.get("metadata") or {}
    currency = metadata.get(CURRENCY_FIELD)
    if not isinstance(currency, str) or len(currency.strip()) != 3:
        raise MarketDataAdapterError(
            "Yahoo reported no currency for this ticker; a price without one "
            "is not a price"
        )
    observed_on = str(raw.get("observed_on") or "").strip()
    if len(observed_on) != 10:
        raise MarketDataAdapterError("the raw output carries no observation date")
    rows = raw.get("rows")
    if not isinstance(rows, list):
        raise MarketDataAdapterError("the raw output carries no rows")
    if len(rows) > MAX_BARS:
        raise MarketDataAdapterError(f"a run may carry at most {MAX_BARS} bars")
    bars: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise MarketDataAdapterError(f"rows[{index}] is not a row")
        missing = [name for name in PRICE_COLUMNS if row.get(name) is None]
        if missing:
            # A day Yahoo has partial data for is a day this contract cannot
            # describe. Dropping it keeps the series honest; filling it would
            # invent a price.
            continue
        bars.append({
            "date": str(row.get("date"))[:10],
            **{
                _WIRE_BY_COLUMN[name]: _decimal_text(row[name], f"rows[{index}].{name}")
                for name in PRICE_COLUMNS
            },
        })
    bars.sort(key=lambda item: item["date"])

    observations: list[dict[str, str]] = []
    for field, name, unit in (
        (SHARES_FIELD, "shares_outstanding", "shares"),
        (MARKET_CAP_FIELD, "market_cap", currency.upper()),
    ):
        value = metadata.get(field)
        if value is None:
            continue
        try:
            text = _decimal_text(value, field)
        except MarketDataAdapterError:
            continue
        if Decimal(text) <= 0:
            continue
        observations.append({
            "observation": name,
            # Dated by when it was read, not by any bar: Yahoo reports the
            # latest it knows and nothing behind it, so the honest date is the
            # date of the reading.
            "as_of": observed_on,
            "value": text,
            "unit": unit,
        })
    return {
        "schema_version": WIRE_SCHEMA_VERSION,
        "ticker": ticker.upper(),
        "currency": currency.strip().upper(),
        "requested_start": str(raw.get("requested_start"))[:10],
        "requested_end": str(raw.get("requested_end"))[:10],
        "auto_adjust": False,
        "bars": bars,
        "observations": observations,
        "source_record_refs": list(source_record_refs),
        "next_cursor": None,
        "provider_status": 200,
    }


def _estimate_rows(block: Any) -> list[dict[str, Any]]:
    if not isinstance(block, dict):
        return []
    rows: list[dict[str, Any]] = []
    for period in sorted(block):
        values = block[period]
        if not isinstance(values, dict):
            continue
        year_ago = values.get("yearAgoEps")
        if year_ago is None:
            year_ago = values.get("yearAgoRevenue")
        currency = values.get("currency")
        rows.append({
            "period": str(period),
            "avg": _optional_decimal(values.get("avg")),
            "low": _optional_decimal(values.get("low")),
            "high": _optional_decimal(values.get("high")),
            "year_ago": _optional_decimal(year_ago),
            "growth": _optional_decimal(values.get("growth")),
            "number_of_analysts": _optional_count(values.get("numberOfAnalysts")),
            "currency": currency if isinstance(currency, str) and currency else None,
        })
    return rows


def analyst_estimates_wire(
    raw: dict[str, Any], *, source_record_refs: list[str]
) -> dict[str, Any]:
    """What the street said, dated, with every figure allowed to be absent.

    Nothing here is a fundamental and nothing here may be read as one. An
    analyst's revenue estimate is an opinion about the future with a name and a
    date on it; the filed revenue it will eventually be compared against comes
    from SEC and only from SEC.
    """

    ticker = str(raw.get("ticker") or "").strip()
    if not ticker:
        raise MarketDataAdapterError("the raw output names no ticker")
    observed_on = str(raw.get("observed_on") or "").strip()
    if len(observed_on) != 10:
        raise MarketDataAdapterError("the raw output carries no observation date")
    target = raw.get("price_target")
    target = target if isinstance(target, dict) else {}
    metadata = raw.get("metadata") or {}
    recommendations: list[dict[str, Any]] = []
    for row in raw.get("recommendations") or []:
        if not isinstance(row, dict):
            continue
        period = row.get("period")
        if not isinstance(period, str) or not period:
            continue
        recommendations.append({
            "period": period,
            **{
                wire: (_optional_count(row.get(column)) or 0)
                for column, wire in _RECOMMENDATION_COLUMNS.items()
            },
        })
    return {
        "schema_version": WIRE_SCHEMA_VERSION,
        "ticker": ticker.upper(),
        "as_of": observed_on,
        "price_target": {
            "current": _optional_decimal(target.get("current")),
            "high": _optional_decimal(target.get("high")),
            "low": _optional_decimal(target.get("low")),
            "mean": _optional_decimal(target.get("mean")),
            "median": _optional_decimal(target.get("median")),
            "number_of_analysts": _optional_count(metadata.get(ANALYST_COUNT_FIELD)),
        },
        "recommendations": recommendations,
        "eps_estimates": _estimate_rows(raw.get("earnings_estimate")),
        "revenue_estimates": _estimate_rows(raw.get("revenue_estimate")),
        "source_record_refs": list(source_record_refs),
        "next_cursor": None,
        "provider_status": 200,
    }


__all__ = [
    "MAX_BARS",
    "MarketDataAdapterError",
    "PRICE_COLUMNS",
    "RAW_SCHEMA_VERSION",
    "WIRE_SCHEMA_VERSION",
    "analyst_estimates_wire",
    "daily_prices_wire",
    "fetch_analyst_estimates",
    "fetch_daily_prices",
    "json_safe",
]
