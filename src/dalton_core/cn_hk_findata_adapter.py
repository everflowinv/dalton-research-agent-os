"""S4: the six akshare calls, and the wires they normalise to.

Kept apart from the child that runs them so the normalisation can be tested
without a network, a store or a subprocess: a fixture in, a wire out.

**Canonicalisation happens before hashing, not after.** akshare hands back
pandas DataFrames whose cells are numpy scalars, ``pandas.Timestamp``,
``datetime.date``, ``NaN`` and occasionally a string with a comma in it. None
of that is JSON, and several of them serialise differently depending on the
version of numpy underneath. So every frame becomes ``{"columns": [...],
"rows": [{column: text-or-null}]}`` with the column order the library returned
and every cell rendered as text, *before* the artifact is hashed. Two runs that
saw the same numbers then produce the same hash, which is the only thing that
makes "this figure came from that call" checkable later.

**No floats reach the wire.** A price or a balance is text all the way through,
for the reason the rest of this system already has one: binary floating point
is not what a filing printed, and a series whose last bit moves reads as a
restatement to anything watching a version chain.

**Every row says who produced it.** ``source_vendor``, ``fallback_used`` and
``caliber_note`` are on every row of every operation. Today no operation has a
permitted fallback, so ``fallback_used`` is always false and the notes carry
the standing 口径 caveats -- that 东财's statements are a vendor normalisation
rather than the filed text, that Shenzhen calls 融券余额 what Shanghai calls
融券余量金额, that the AH premium is computed at the vendor's own exchange
rate. The day a fallback is approved, the label already travels with the
number.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from .cn_hk_findata_core import (
    ADAPTER_LIBRARY,
    ADAPTER_LIBRARY_VERSION,
    AH_PREMIUM_OPERATION,
    BUYBACKS_OPERATION,
    CALIBER_NOTES,
    FINANCIAL_STATEMENTS_OPERATION,
    MARGIN_BALANCE_OPERATION,
    NORTHBOUND_FLOW_OPERATION,
    SHAREHOLDERS_OPERATION,
    VENDORS_BY_OPERATION,
    refused_vendor_route,
)

RAW_SCHEMA_VERSION = "0.1"
WIRE_SCHEMA_VERSION = "0.1"

# Five years of quarters. An analyst opening a Chinese name wants a decade and
# the vendor will serve it, but the wire is what gets validated, stored and
# read, and twenty periods of a three-hundred-line statement is already a large
# object. Periods beyond the cap are counted, not silently lost.
MAX_PERIODS = 20
# One issuer's holder-count history. The vendor pages this 500 rows at a time
# and a long-listed company has two decades of them.
MAX_HOLDER_COUNT_ROWS = 200
MAX_ROWS = 20_000

# Columns of the A-share statement tables that describe the report rather than
# report a line item. Everything else in the frame is a concept.
_A_STATEMENT_METADATA = frozenset({
    "SECUCODE", "SECURITY_CODE", "SECURITY_NAME_ABBR", "ORG_CODE", "ORG_TYPE",
    "REPORT_DATE", "REPORT_TYPE", "REPORT_DATE_NAME", "SECURITY_TYPE_CODE",
    "NOTICE_DATE", "UPDATE_DATE", "CURRENCY", "START_DATE", "OPINION_TYPE",
    "OSOPINION_TYPE", "LISTING_STATE", "MINORITY_INTEREST",  # see below
})
# ``MINORITY_INTEREST`` is a real line item and is removed from the metadata
# set again here rather than being left out above, so the list above can stay a
# copy of the vendor's own envelope columns.
_A_STATEMENT_METADATA = _A_STATEMENT_METADATA - {"MINORITY_INTEREST"}

_STATEMENT_KINDS = ("income", "balance", "cash")
_A_FUNCTION_BY_STATEMENT = {
    "income": "stock_profit_sheet_by_report_em",
    "balance": "stock_balance_sheet_by_report_em",
    "cash": "stock_cash_flow_sheet_by_report_em",
}
_HK_SYMBOL_BY_STATEMENT = {
    "income": "利润表", "balance": "资产负债表", "cash": "现金流量表",
}
# 中国企业会计准则. The A-share feed does not name its standard anywhere, and
# leaving the field null would read as "the same as whatever the other row
# said" the first time an A row and an H row sit in one table.
CHINA_ACCOUNTING_STANDARD = "中国企业会计准则"


class CnHkFinDataAdapterError(RuntimeError):
    """The source did not return something this contract can describe."""


class CnHkFinDataVendorRefusal(CnHkFinDataAdapterError):
    """The declared vendor did not answer, and no substitute is permitted.

    Separate from the generic error because it is the one failure that must not
    be retried against another host. The skill this connector comes from learned
    that pressing 东方财富 after a refusal takes down the endpoints that were
    still working; and where an alternative vendor does exist, swapping to it
    silently would answer with a number measured a different way.
    """


# -- canonicalising --------------------------------------------------------


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise CnHkFinDataAdapterError("a figure was not finite")
    if value == 0:
        return "0"
    raw = format(value, "f")
    if "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    return raw or "0"


def cell_text(value: Any) -> str | None:
    """One DataFrame cell as canonical text, or null when the source had none.

    Text for everything, including numbers. The alternative -- keeping ints as
    ints and floats as floats -- means the artifact hash depends on how the
    numpy version underneath happened to widen a value, and the whole point of
    hashing the artifact is that the same call produces the same hash.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        text = value.strip()
        if text in {"", "nan", "NaN", "<NA>", "NaT", "None", "-", "--"}:
            return None
        return text
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10] if not isinstance(value, datetime) \
            else value.isoformat()
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        # ``repr`` gives the shortest decimal that round-trips, which is the
        # number the vendor's JSON printed; ``Decimal`` then removes the
        # exponent form, because ``1e+15`` is not a decimal string.
        return _decimal_text(Decimal(repr(float(value))))
    if isinstance(value, Decimal):
        return None if not value.is_finite() else _decimal_text(value)
    converter = getattr(value, "item", None)
    if callable(converter):
        try:
            return cell_text(converter())
        except (TypeError, ValueError):
            pass
    text = str(value).strip()
    if text in {"", "nan", "NaN", "<NA>", "NaT", "None"}:
        return None
    return text


def frame_to_raw(frame: Any, *, function: str, kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """A pandas frame as a canonical, JSON-safe, float-free record.

    Column order is the library's, kept rather than sorted: it is the vendor's
    own statement order, and a reader comparing two captures wants them to line
    up. The rows are ordered as returned for the same reason.
    """

    columns = [str(name) for name in list(getattr(frame, "columns", []))]
    if len(columns) != len(set(columns)):
        raise CnHkFinDataAdapterError(
            f"{function} returned duplicate column names, which cannot be "
            "canonicalised without choosing one of them arbitrarily"
        )
    rows: list[dict[str, str | None]] = []
    records = frame.to_dict("records") if hasattr(frame, "to_dict") else list(frame)
    if len(records) > MAX_ROWS:
        raise CnHkFinDataAdapterError(
            f"{function} returned {len(records)} rows; the ceiling is {MAX_ROWS}"
        )
    for record in records:
        rows.append({column: cell_text(record.get(column)) for column in columns})
    return {
        "function": function,
        "kwargs": {str(key): cell_text(value) for key, value in kwargs.items()},
        "columns": columns,
        "row_count": len(rows),
        "rows": rows,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _raw_envelope(operation: str, vendor: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": RAW_SCHEMA_VERSION,
        "library": ADAPTER_LIBRARY,
        "library_version": ADAPTER_LIBRARY_VERSION,
        "operation": operation,
        "vendor": vendor,
        "fallback_used": False,
        "parameters": {str(key): value for key, value in parameters.items()},
        "observed_on": _today(),
        "captured_at": _now(),
        "frames": {},
        "errors": {},
    }


# -- reading the raw capture ----------------------------------------------


def _frame(raw: Mapping[str, Any], name: str, *, required: bool = True) -> dict[str, Any]:
    frames = raw.get("frames")
    if not isinstance(frames, Mapping):
        raise CnHkFinDataAdapterError("the raw capture carries no frames")
    frame = frames.get(name)
    if frame is None:
        if required:
            error = (raw.get("errors") or {}).get(name)
            raise CnHkFinDataAdapterError(
                f"the raw capture has no {name!r} frame"
                + (f": {error}" if error else "")
            )
        return {"columns": [], "rows": [], "row_count": 0}
    if not isinstance(frame, Mapping) or not isinstance(frame.get("rows"), list):
        raise CnHkFinDataAdapterError(f"the {name!r} frame is not a frame")
    return dict(frame)


def _text(row: Mapping[str, Any], column: str) -> str | None:
    value = row.get(column)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CnHkFinDataAdapterError(
            f"column {column!r} is {type(value).__name__}; the capture was not "
            "canonicalised before it was written"
        )
    text = value.strip()
    return text or None


def _number(row: Mapping[str, Any], column: str) -> str | None:
    text = _text(row, column)
    if text is None:
        return None
    cleaned = text.replace(",", "").replace("%", "")
    try:
        return _decimal_text(Decimal(cleaned))
    except (InvalidOperation, ValueError):
        return None


def _iso_date(value: str | None) -> str | None:
    """The vendor's several date spellings, as one ISO date."""

    if value is None:
        return None
    text = value.strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        candidate = text[:10]
    elif len(text) == 8 and text.isdigit():
        candidate = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    else:
        return None
    try:
        date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate


def _provenance(
    operation: str, *, vendor: str, fallback_used: bool, caliber_note: str | None
) -> dict[str, Any]:
    """The three fields every row carries, checked rather than trusted.

    A vendor the approval does not cover is refused here as well as by the
    frozen schema. Both, because the schema catches a wire that was built wrong
    and this catches a capture that was taken wrong, and the difference between
    them is which error a person gets told.
    """

    permitted = VENDORS_BY_OPERATION[operation]
    if vendor not in permitted:
        refused = refused_vendor_route(operation, vendor)
        detail = f" {refused['reason']}" if refused else ""
        raise CnHkFinDataVendorRefusal(
            f"{operation} is approved for {'/'.join(permitted)} and this row "
            f"came from {vendor}.{detail}"
        )
    if fallback_used and not caliber_note:
        raise CnHkFinDataAdapterError(
            f"a {operation} row from {vendor} is marked as a fallback with no "
            "caliber note; a number measured a different way must say so"
        )
    return {
        "source_vendor": vendor,
        "fallback_used": bool(fallback_used),
        "caliber_note": caliber_note,
    }


def _capture_provenance(raw: Mapping[str, Any], operation: str) -> tuple[str, bool]:
    vendor = raw.get("vendor")
    if not isinstance(vendor, str) or not vendor:
        raise CnHkFinDataAdapterError("the raw capture names no vendor")
    fallback_used = bool(raw.get("fallback_used"))
    permitted = VENDORS_BY_OPERATION[operation]
    if vendor not in permitted:
        refused = refused_vendor_route(operation, vendor)
        detail = f" {refused['reason']}" if refused else ""
        raise CnHkFinDataVendorRefusal(
            f"this capture came from {vendor}; {operation} is approved for "
            f"{'/'.join(permitted)} only.{detail}"
        )
    if fallback_used:
        # There is no permitted fallback for any of the six today, so a capture
        # that claims one is either mislabelled or was taken by something that
        # switched vendors on its own. Both are refusals, not warnings.
        raise CnHkFinDataVendorRefusal(
            f"this {operation} capture is marked as a vendor fallback, and no "
            "fallback vendor is approved for it. The declared vendor was not "
            "reached, so there is no answer -- not a different answer."
        )
    return vendor, fallback_used


def _envelope(source_record_refs: Sequence[str]) -> dict[str, Any]:
    return {
        "source_record_refs": list(source_record_refs),
        "next_cursor": None,
        "provider_status": 200,
    }


# -- fetching --------------------------------------------------------------


def _library() -> Any:
    """Import lazily, so a Core without the extra refuses with a reason."""

    try:
        import akshare  # noqa: PLC0415 - deliberately lazy
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise CnHkFinDataAdapterError(
            "the China/Hong Kong data library is not installed; install this "
            "package with the [cn-hk-data] extra"
        ) from exc
    return akshare


def _call(akshare: Any, function: str, **kwargs: Any) -> Any:
    """One upstream call, with the vendor's failure turned into a refusal.

    No retry. The skill's own rule, learned on 2026-08-21: connection refused,
    reset, 502 and 403 are not transient against these hosts, and pressing them
    took down the endpoints that were still healthy.
    """

    handle = getattr(akshare, function, None)
    if not callable(handle):
        raise CnHkFinDataAdapterError(
            f"the installed {ADAPTER_LIBRARY} has no {function}; the frozen "
            "adapter names a function this library version does not provide"
        )
    try:
        return handle(**kwargs)
    except Exception as exc:  # noqa: BLE001 - one upstream failure, named
        raise CnHkFinDataVendorRefusal(
            f"{function} did not answer ({type(exc).__name__}: {exc}); no "
            "substitute vendor is approved for this operation, so there is no "
            "answer rather than a different one"
        ) from exc


def a_share_symbol(ticker: str) -> str:
    """``600519`` as the vendor writes it: ``SH600519``.

    Refused rather than guessed when the prefix is not one this maps: a wrong
    market prefix returns another company's statements, which is worse than an
    error.
    """

    code = str(ticker).strip()
    if len(code) != 6 or not code.isdigit():
        raise CnHkFinDataAdapterError(
            f"{ticker!r} is not a six-digit A-share code"
        )
    if code[0] in {"6", "9"}:
        return f"SH{code}"
    if code[0] in {"0", "2", "3"}:
        return f"SZ{code}"
    if code[0] in {"4", "8"}:
        return f"BJ{code}"
    raise CnHkFinDataAdapterError(
        f"{ticker!r} has no exchange this adapter can name with confidence"
    )


def hk_symbol(ticker: str) -> str:
    code = str(ticker).strip().upper().removesuffix(".HK")
    if not code.isdigit() or len(code) > 5:
        raise CnHkFinDataAdapterError(f"{ticker!r} is not a Hong Kong code")
    return code.zfill(5)


def fetch_financial_statements(
    *, market: str, ticker: str, statement_kind: str, period_type: str
) -> dict[str, Any]:
    """One company's one statement, by period, from 东方财富."""

    if statement_kind not in _STATEMENT_KINDS:
        raise CnHkFinDataAdapterError(f"{statement_kind!r} is not a statement")
    if period_type not in {"report", "annual"}:
        raise CnHkFinDataAdapterError(f"{period_type!r} is not a period type")
    akshare = _library()
    raw = _raw_envelope(
        FINANCIAL_STATEMENTS_OPERATION, "eastmoney",
        {"market": market, "ticker": ticker, "statement_kind": statement_kind,
         "period_type": period_type},
    )
    if market == "a":
        if period_type != "report":
            # The vendor has a by-year route, but it is a different function
            # with a different report-date type, and this contract names one
            # function per statement. Refusing is honest; quietly serving
            # report periods for an annual request is not.
            raise CnHkFinDataAdapterError(
                "the A-share route this adapter freezes is the by-report-period "
                "one; ask for period_type=report and select the annual periods"
            )
        function = _A_FUNCTION_BY_STATEMENT[statement_kind]
        symbol = a_share_symbol(ticker)
        frame = _call(akshare, function, symbol=symbol)
        raw["frames"]["statement"] = frame_to_raw(
            frame, function=function, kwargs={"symbol": symbol})
    elif market == "hk":
        symbol = hk_symbol(ticker)
        indicator = "年度" if period_type == "annual" else "报告期"
        frame = _call(
            akshare, "stock_financial_hk_report_em",
            stock=symbol, symbol=_HK_SYMBOL_BY_STATEMENT[statement_kind],
            indicator=indicator,
        )
        raw["frames"]["statement"] = frame_to_raw(
            frame, function="stock_financial_hk_report_em",
            kwargs={"stock": symbol,
                    "symbol": _HK_SYMBOL_BY_STATEMENT[statement_kind],
                    "indicator": indicator},
        )
    else:
        raise CnHkFinDataAdapterError(f"{market!r} is not a market")
    return raw


def fetch_shareholders(*, a_ticker: str, period_end: str) -> dict[str, Any]:
    """Who holds the free float at one report date, and how many holders there are."""

    akshare = _library()
    raw = _raw_envelope(
        SHAREHOLDERS_OPERATION, "eastmoney",
        {"a_ticker": a_ticker, "period_end": period_end},
    )
    symbol = a_share_symbol(a_ticker).lower()
    compact = period_end.replace("-", "")
    frame = _call(akshare, "stock_gdfx_free_top_10_em", symbol=symbol, date=compact)
    raw["frames"]["top_holders"] = frame_to_raw(
        frame, function="stock_gdfx_free_top_10_em",
        kwargs={"symbol": symbol, "date": compact})
    frame = _call(akshare, "stock_zh_a_gdhs_detail_em", symbol=a_ticker)
    raw["frames"]["holder_counts"] = frame_to_raw(
        frame, function="stock_zh_a_gdhs_detail_em", kwargs={"symbol": a_ticker})
    return raw


def fetch_buybacks(*, a_ticker: str) -> dict[str, Any]:
    """The market-wide buyback table, which is the only one the vendor has."""

    akshare = _library()
    raw = _raw_envelope(BUYBACKS_OPERATION, "eastmoney", {"a_ticker": a_ticker})
    frame = _call(akshare, "stock_repurchase_em")
    raw["frames"]["buybacks"] = frame_to_raw(
        frame, function="stock_repurchase_em", kwargs={})
    return raw


def fetch_margin_balance(*, exchange: str, start: str, end: str) -> dict[str, Any]:
    """融资融券 totals, from the exchange rather than from a vendor."""

    if exchange not in {"sse", "szse"}:
        raise CnHkFinDataAdapterError(f"{exchange!r} is not an exchange")
    akshare = _library()
    raw = _raw_envelope(
        MARGIN_BALANCE_OPERATION, exchange,
        {"exchange": exchange, "start": start, "end": end},
    )
    if exchange == "sse":
        begin, finish = start.replace("-", ""), end.replace("-", "")
        frame = _call(akshare, "stock_margin_sse", start_date=begin, end_date=finish)
        raw["frames"]["margin"] = frame_to_raw(
            frame, function="stock_margin_sse",
            kwargs={"start_date": begin, "end_date": finish})
    else:
        if start != end:
            # Shenzhen publishes one trading day per request and the window has
            # to be walked day by day. Walking it silently would turn one
            # governed call into as many as the window is long.
            raise CnHkFinDataAdapterError(
                "the Shenzhen route publishes one trading day at a time; ask "
                "for a single day (start == end) and walk the window in the "
                "caller, where each day is its own governed call"
            )
        compact = start.replace("-", "")
        frame = _call(akshare, "stock_margin_szse", date=compact)
        raw["frames"]["margin"] = frame_to_raw(
            frame, function="stock_margin_szse", kwargs={"date": compact})
    return raw


def fetch_northbound_flow(*, as_of: str) -> dict[str, Any]:
    """沪深港通 flow, both directions, as of the latest trading day."""

    akshare = _library()
    raw = _raw_envelope(NORTHBOUND_FLOW_OPERATION, "eastmoney", {"as_of": as_of})
    frame = _call(akshare, "stock_hsgt_fund_flow_summary_em")
    raw["frames"]["flow"] = frame_to_raw(
        frame, function="stock_hsgt_fund_flow_summary_em", kwargs={})
    return raw


def fetch_ah_premium(*, ticker: str) -> dict[str, Any]:
    """One A+H pair's premium, from the vendor that actually computes one.

    This is the single operation that has to touch 东财's quote cluster --
    the host the 2026-08 incident was about. One call, no retry, and a quota
    that makes probing arithmetically impossible.
    """

    akshare = _library()
    raw = _raw_envelope(AH_PREMIUM_OPERATION, "eastmoney", {"ticker": ticker})
    frame = _call(akshare, "stock_zh_ah_spot_em")
    raw["frames"]["ah"] = frame_to_raw(
        frame, function="stock_zh_ah_spot_em", kwargs={})
    return raw


# -- normalising -----------------------------------------------------------


def _parameters(raw: Mapping[str, Any], operation: str) -> dict[str, Any]:
    parameters = raw.get("parameters")
    if not isinstance(parameters, Mapping):
        raise CnHkFinDataAdapterError("the raw capture carries no parameters")
    if raw.get("operation") != operation:
        raise CnHkFinDataAdapterError(
            f"this capture is of {raw.get('operation')!r}, not {operation!r}; "
            "a wire built from the wrong call would validate and be wrong"
        )
    return dict(parameters)


def _captured_at(raw: Mapping[str, Any]) -> str:
    value = raw.get("captured_at")
    if isinstance(value, str) and value.strip():
        return value
    observed = raw.get("observed_on")
    if isinstance(observed, str) and len(observed) == 10:
        # An older capture that did not record the moment. Midnight is before
        # any close, which is the safe direction to be wrong in.
        return f"{observed}T00:00:00+00:00"
    raise CnHkFinDataAdapterError("the raw capture does not say when it was taken")


def financial_statements_wire(
    raw: Mapping[str, Any], *, source_record_refs: Sequence[str]
) -> dict[str, Any]:
    """One statement, long: one line per (period, concept).

    The vendor serves the A-share tables wide -- one column per line item -- and
    the Hong Kong tables long. A closed schema can describe neither while the
    column name is data, so both become the long shape, which loses nothing:
    the wide frame is in the artifact either way.

    A concept absent from ``lines`` is a concept the vendor reported nothing
    for. The wide A-share table fills those with nulls by the hundred, and
    carrying them would triple the object to say "no" three hundred times.
    """

    parameters = _parameters(raw, FINANCIAL_STATEMENTS_OPERATION)
    vendor, fallback_used = _capture_provenance(raw, FINANCIAL_STATEMENTS_OPERATION)
    market = str(parameters.get("market") or "").strip()
    if market not in {"a", "hk"}:
        raise CnHkFinDataAdapterError("the capture names no market")
    statement = str(parameters.get("statement_kind") or "").strip()
    if statement not in _STATEMENT_KINDS:
        raise CnHkFinDataAdapterError("the capture names no statement")
    period_type = str(parameters.get("period_type") or "").strip()
    if period_type not in {"report", "annual"}:
        raise CnHkFinDataAdapterError("the capture names no period type")
    ticker = str(parameters.get("ticker") or "").strip()
    if not ticker:
        raise CnHkFinDataAdapterError("the capture names no ticker")

    frame = _frame(raw, "statement")
    rows = frame["rows"]
    lines: list[dict[str, Any]] = []
    dropped = 0
    security_name: str | None = None
    currency: str | None = None
    account_standard: str | None = (
        CHINA_ACCOUNTING_STANDARD if market == "a" else None
    )

    if market == "a":
        note = CALIBER_NOTES["eastmoney_statements_a"]
        by_period: dict[str, dict[str, Any]] = {}
        for row in rows:
            period_end = _iso_date(_text(row, "REPORT_DATE"))
            if period_end is None:
                dropped += 1
                continue
            by_period.setdefault(period_end, row)
        for period_end in sorted(by_period, reverse=True)[:MAX_PERIODS]:
            row = by_period[period_end]
            security_name = security_name or _text(row, "SECURITY_NAME_ABBR")
            currency = currency or _text(row, "CURRENCY")
            report_type = _text(row, "REPORT_DATE_NAME") or _text(row, "REPORT_TYPE")
            # The A-share feed carries no start date at all. On this route --
            # 报告期 -- every interim figure is cumulative from the start of
            # the fiscal year, and a mainland fiscal year is the calendar year,
            # so the start is the first of January of the period's year. It is
            # written down rather than left null because a Q2 cumulative figure
            # and a Q2 single-quarter figure have the same period_end, and
            # without a start they are one number twice. Balance-sheet lines
            # are instants and keep a null.
            period_start = (
                None if statement == "balance" else f"{period_end[:4]}-01-01"
            )
            for column in frame["columns"]:
                if column in _A_STATEMENT_METADATA or column.startswith("-"):
                    continue
                # ``TOTAL_OPERATE_INCOME_YOY`` is a percentage the vendor
                # computed, not a line the company reported. Admitting it as a
                # statement line puts it one column away from figures that can
                # be added up, and somebody will add it up.
                if column.endswith(("_YOY", "_QOQ")):
                    continue
                value = _number(row, column)
                if value is None:
                    continue
                lines.append({
                    "statement": statement,
                    "period_end": period_end,
                    "period_start": period_start,
                    "fiscal_year": period_end[:4],
                    "report_type": report_type,
                    "concept": column,
                    "label": None,
                    "value": value,
                    "currency": _text(row, "CURRENCY"),
                    "account_standard": CHINA_ACCOUNTING_STANDARD,
                    **_provenance(
                        FINANCIAL_STATEMENTS_OPERATION, vendor=vendor,
                        fallback_used=fallback_used, caliber_note=note),
                })
        dropped += max(0, len(by_period) - MAX_PERIODS)
    else:
        note = CALIBER_NOTES["eastmoney_statements_hk"]
        # The Hong Kong three-table endpoint returns line items and no header:
        # no currency, no accounting standard. The main-indicator table on the
        # same host has a CURRENCY, and borrowing it was the obvious move and
        # is wrong. Measured on 腾讯 FY2025: 营业额 in the statement table is
        # 743,689,000,000; OPERATE_INCOME in the indicator table is
        # 751,766,000,000, with CURRENCY=HKD and IS_CNY_CODE=0. A 1.09% gap is
        # not an exchange rate, so the two tables are not the same measurement
        # and the label from one does not describe the other. Both fields stay
        # null, and the caliber note says why -- a wrong currency on a Hong
        # Kong revenue line is worse than an absent one, because absent stops
        # a reader and wrong does not.
        periods = sorted(
            {p for p in (_iso_date(_text(row, "REPORT_DATE")) for row in rows)
             if p is not None},
            reverse=True,
        )[:MAX_PERIODS]
        kept = set(periods)
        for row in rows:
            period_end = _iso_date(_text(row, "REPORT_DATE"))
            concept = _text(row, "STD_ITEM_CODE")
            if period_end is None or concept is None:
                dropped += 1
                continue
            if period_end not in kept:
                dropped += 1
                continue
            security_name = security_name or _text(row, "SECURITY_NAME_ABBR")
            lines.append({
                "statement": statement,
                "period_end": period_end,
                "period_start": _iso_date(_text(row, "START_DATE")),
                # The vendor's FISCAL_YEAR is "12-31" -- the fiscal year *end*,
                # not the year. Under a field called fiscal_year that reads as
                # a date somebody will sort on.
                "fiscal_year": period_end[:4],
                # DATE_TYPE_CODE is "001" and the vendor publishes no legend.
                # Prefixed so it cannot be mistaken for a label anyone has
                # verified; the code itself is in the artifact.
                "report_type": (
                    f"date_type_code:{_text(row, 'DATE_TYPE_CODE')}"
                    if _text(row, "DATE_TYPE_CODE") else None
                ),
                "concept": concept,
                "label": _text(row, "STD_ITEM_NAME"),
                "value": _number(row, "AMOUNT"),
                "currency": None,
                "account_standard": None,
                **_provenance(
                    FINANCIAL_STATEMENTS_OPERATION, vendor=vendor,
                    fallback_used=fallback_used, caliber_note=note),
            })
    lines.sort(key=lambda item: (item["period_end"], item["concept"]), reverse=True)
    return {
        "schema_version": WIRE_SCHEMA_VERSION,
        "market": market,
        "ticker": ticker,
        "security_name": security_name,
        "statement": statement,
        "period_type": period_type,
        "currency": currency,
        "account_standard": account_standard,
        "captured_at": _captured_at(raw),
        "lines": lines,
        "period_count": len({item["period_end"] for item in lines}),
        "dropped_row_count": dropped,
        **_envelope(source_record_refs),
    }


def shareholders_wire(
    raw: Mapping[str, Any], *, source_record_refs: Sequence[str]
) -> dict[str, Any]:
    """Two blocks: who holds the free float, and how many holders there are.

    Both, because either alone misleads. A top-ten table that has barely moved
    beside a holder count that fell a fifth is the 筹码集中 story; the top ten
    on its own is just a list of institutions.
    """

    parameters = _parameters(raw, SHAREHOLDERS_OPERATION)
    vendor, fallback_used = _capture_provenance(raw, SHAREHOLDERS_OPERATION)
    ticker = str(parameters.get("a_ticker") or "").strip()
    period_end = _iso_date(str(parameters.get("period_end") or ""))
    if not ticker or period_end is None:
        raise CnHkFinDataAdapterError("the capture names no ticker or period")

    dropped = 0
    holders: list[dict[str, Any]] = []
    holder_note = CALIBER_NOTES["eastmoney_top_holders"]
    for row in _frame(raw, "top_holders")["rows"]:
        rank = _number(row, "名次")
        name = _text(row, "股东名称")
        if rank is None or name is None:
            dropped += 1
            continue
        holders.append({
            "rank": int(Decimal(rank)),
            "holder_name": name,
            "holder_nature": _text(row, "股东性质"),
            "share_class": _text(row, "股份类型"),
            "shares": _number(row, "持股数"),
            "pct_of_float": _number(row, "占总流通股本持股比例"),
            # Free text on purpose: the vendor writes "不变", "新进" and signed
            # numbers in the same column, and coercing those to a number means
            # inventing a zero for two of the three.
            "change": _text(row, "增减"),
            "change_ratio": _number(row, "变动比率"),
            **_provenance(SHAREHOLDERS_OPERATION, vendor=vendor,
                          fallback_used=fallback_used, caliber_note=holder_note),
        })
    holders.sort(key=lambda item: item["rank"])

    counts: list[dict[str, Any]] = []
    for row in _frame(raw, "holder_counts", required=False)["rows"]:
        as_of = _iso_date(_text(row, "股东户数统计截止日"))
        if as_of is None:
            dropped += 1
            continue
        counts.append({
            "as_of": as_of,
            "announced_on": _iso_date(_text(row, "股东户数公告日期")),
            "holder_count": _number(row, "股东户数-本次"),
            "prior_holder_count": _number(row, "股东户数-上次"),
            "holder_count_change": _number(row, "股东户数-增减"),
            "holder_count_change_ratio": _number(row, "股东户数-增减比例"),
            "avg_shares_per_holder": _number(row, "户均持股数量"),
            "avg_value_per_holder": _number(row, "户均持股市值"),
            "total_shares": _number(row, "总股本"),
            "total_market_cap": _number(row, "总市值"),
            **_provenance(SHAREHOLDERS_OPERATION, vendor=vendor,
                          fallback_used=fallback_used, caliber_note=None),
        })
    counts.sort(key=lambda item: item["as_of"], reverse=True)
    if len(counts) > MAX_HOLDER_COUNT_ROWS:
        dropped += len(counts) - MAX_HOLDER_COUNT_ROWS
        counts = counts[:MAX_HOLDER_COUNT_ROWS]
    return {
        "schema_version": WIRE_SCHEMA_VERSION,
        "ticker": ticker,
        "security_name": next(
            (name for name in (_text(row, "名称") for row
                               in _frame(raw, "holder_counts", required=False)["rows"])
             if name), None),
        "period_end": period_end,
        "captured_at": _captured_at(raw),
        "top_holders": holders,
        "holder_counts": counts,
        "dropped_row_count": dropped,
        **_envelope(source_record_refs),
    }


def buybacks_wire(
    raw: Mapping[str, Any], *, source_record_refs: Sequence[str]
) -> dict[str, Any]:
    """One issuer's buyback programmes, filtered out of the market-wide table.

    ``universe_row_count`` is the number of rows the vendor's table actually
    had. Without it, "this company announced no buyback" and "the table came
    back short" are the same empty answer.
    """

    parameters = _parameters(raw, BUYBACKS_OPERATION)
    vendor, fallback_used = _capture_provenance(raw, BUYBACKS_OPERATION)
    ticker = str(parameters.get("a_ticker") or "").strip()
    if not ticker:
        raise CnHkFinDataAdapterError("the capture names no ticker")
    frame = _frame(raw, "buybacks")
    note = CALIBER_NOTES["eastmoney_buyback_market_table"]
    rows: list[dict[str, Any]] = []
    dropped = 0
    for row in frame["rows"]:
        code = _text(row, "股票代码")
        if code is None:
            dropped += 1
            continue
        if code != ticker:
            continue
        rows.append({
            "security_code": code,
            "security_name": _text(row, "股票简称"),
            "announced_on": _iso_date(_text(row, "最新公告日期")),
            "started_on": _iso_date(_text(row, "回购起始时间")),
            "progress": _text(row, "实施进度"),
            "planned_shares_low": _number(row, "计划回购数量区间-下限"),
            "planned_shares_high": _number(row, "计划回购数量区间-上限"),
            "planned_amount_low": _number(row, "计划回购金额区间-下限"),
            "planned_amount_high": _number(row, "计划回购金额区间-上限"),
            "planned_pct_low": _number(row, "占公告前一日总股本比例-下限"),
            "planned_pct_high": _number(row, "占公告前一日总股本比例-上限"),
            "price_ceiling": _number(row, "计划回购价格区间"),
            "repurchased_shares": _number(row, "已回购股份数量"),
            "repurchased_amount": _number(row, "已回购金额"),
            "repurchased_price_low": _number(row, "已回购股份价格区间-下限"),
            "repurchased_price_high": _number(row, "已回购股份价格区间-上限"),
            **_provenance(BUYBACKS_OPERATION, vendor=vendor,
                          fallback_used=fallback_used, caliber_note=note),
        })
    rows.sort(key=lambda item: (item["announced_on"] or "", item["started_on"] or ""),
              reverse=True)
    return {
        "schema_version": WIRE_SCHEMA_VERSION,
        "ticker": ticker,
        "captured_at": _captured_at(raw),
        "rows": rows,
        "universe_row_count": len(frame["rows"]),
        "dropped_row_count": dropped,
        **_envelope(source_record_refs),
    }


def margin_balance_wire(
    raw: Mapping[str, Any], *, source_record_refs: Sequence[str]
) -> dict[str, Any]:
    """融资融券 totals for one exchange over one window.

    The two exchanges publish the same six quantities under partly different
    names, and Shenzhen's response has no date column at all -- the date is the
    request. Both are reconciled onto one row shape, and the vendor's own name
    for the one genuinely ambiguous column travels with the row.
    """

    parameters = _parameters(raw, MARGIN_BALANCE_OPERATION)
    vendor, fallback_used = _capture_provenance(raw, MARGIN_BALANCE_OPERATION)
    exchange = str(parameters.get("exchange") or "").strip()
    if exchange not in {"sse", "szse"} or exchange != vendor:
        raise CnHkFinDataAdapterError(
            "the capture's exchange and its vendor disagree; one of them is "
            "describing a different response"
        )
    start = _iso_date(str(parameters.get("start") or ""))
    end = _iso_date(str(parameters.get("end") or ""))
    if start is None or end is None:
        raise CnHkFinDataAdapterError("the capture names no window")
    frame = _frame(raw, "margin")
    rows: list[dict[str, Any]] = []
    dropped = 0
    if exchange == "sse":
        note = CALIBER_NOTES["sse_short_balance"]
        for row in frame["rows"]:
            trade_date = _iso_date(_text(row, "信用交易日期"))
            if trade_date is None:
                dropped += 1
                continue
            rows.append({
                "trade_date": trade_date,
                "financing_balance": _number(row, "融资余额"),
                "financing_buy": _number(row, "融资买入额"),
                "short_selling_volume": _number(row, "融券卖出量"),
                "short_balance_volume": _number(row, "融券余量"),
                "short_balance_amount": _number(row, "融券余量金额"),
                "total_balance": _number(row, "融资融券余额"),
                "currency": "CNY",
                "amount_unit": "元",
                "volume_unit": "股",
                **_provenance(MARGIN_BALANCE_OPERATION, vendor=vendor,
                              fallback_used=fallback_used, caliber_note=note),
            })
    else:
        note = CALIBER_NOTES["szse_short_balance"]
        for row in frame["rows"]:
            # Shenzhen returns the totals for the requested day and nothing
            # that says which day that was. The date is the request, and
            # saying so here is the only place it can be said honestly.
            rows.append({
                "trade_date": start,
                "financing_balance": _number(row, "融资余额"),
                "financing_buy": _number(row, "融资买入额"),
                "short_selling_volume": _number(row, "融券卖出量"),
                "short_balance_volume": _number(row, "融券余量"),
                "short_balance_amount": _number(row, "融券余额"),
                "total_balance": _number(row, "融资融券余额"),
                "currency": "CNY",
                # Shenzhen publishes in 亿元 and 亿股 where Shanghai publishes
                # in 元 and 股. Neither exchange labels the unit in the payload;
                # this is read off the magnitudes -- 12,847.58 against
                # 1,350,016,680,402 for the same quantity on adjacent days --
                # and it is on every row because it is the difference between
                # a market total and a rounding error.
                "amount_unit": "亿元",
                "volume_unit": "亿股",
                **_provenance(MARGIN_BALANCE_OPERATION, vendor=vendor,
                              fallback_used=fallback_used, caliber_note=note),
            })
    rows.sort(key=lambda item: item["trade_date"])
    return {
        "schema_version": WIRE_SCHEMA_VERSION,
        "exchange": exchange,
        "requested_start": start,
        "requested_end": end,
        "captured_at": _captured_at(raw),
        "rows": rows,
        "dropped_row_count": dropped,
        **_envelope(source_record_refs),
    }


def northbound_flow_wire(
    raw: Mapping[str, Any], *, source_record_refs: Sequence[str]
) -> dict[str, Any]:
    """沪深港通 flow as of one trading day, checked against the day asked for.

    The endpoint is a snapshot: it answers with whatever day it currently holds
    and never with the day in the request, because there is nothing in the
    request. So the returned day is compared with the requested one and a
    mismatch is a refusal. A stale snapshot filed under today's date is the
    failure this operation exists to avoid.
    """

    parameters = _parameters(raw, NORTHBOUND_FLOW_OPERATION)
    vendor, fallback_used = _capture_provenance(raw, NORTHBOUND_FLOW_OPERATION)
    as_of = _iso_date(str(parameters.get("as_of") or ""))
    if as_of is None:
        raise CnHkFinDataAdapterError("the capture names no requested date")
    frame = _frame(raw, "flow")
    note = CALIBER_NOTES["eastmoney_hsgt_unit"]
    rows: list[dict[str, Any]] = []
    dropped = 0
    for row in frame["rows"]:
        trade_date = _iso_date(_text(row, "交易日"))
        mutual_type = _text(row, "类型")
        if trade_date is None or mutual_type is None:
            dropped += 1
            continue
        rows.append({
            "trade_date": trade_date,
            "mutual_type": mutual_type,
            "board": _text(row, "板块"),
            "funds_direction": _text(row, "资金方向"),
            "trade_status": _text(row, "交易状态"),
            "net_buy_amount": _number(row, "成交净买额"),
            "net_inflow": _number(row, "资金净流入"),
            "daily_quota_balance": _number(row, "当日资金余额"),
            "advancing": _optional_int(_number(row, "上涨数")),
            "unchanged": _optional_int(_number(row, "持平数")),
            "declining": _optional_int(_number(row, "下跌数")),
            "index_name": _text(row, "相关指数"),
            "index_change_percent": _number(row, "指数涨跌幅"),
            "amount_unit": "亿元",
            **_provenance(NORTHBOUND_FLOW_OPERATION, vendor=vendor,
                          fallback_used=fallback_used, caliber_note=note),
        })
    returned = {row["trade_date"] for row in rows}
    if rows and as_of not in returned:
        raise CnHkFinDataAdapterError(
            f"the snapshot is of {'/'.join(sorted(returned))} and {as_of} was "
            "asked for; filing it under the requested date would date a number "
            "to a day it does not describe"
        )
    rows.sort(key=lambda item: (item["trade_date"], item["mutual_type"],
                                item["board"] or ""))
    return {
        "schema_version": WIRE_SCHEMA_VERSION,
        "requested_as_of": as_of,
        "captured_at": _captured_at(raw),
        "rows": rows,
        "dropped_row_count": dropped,
        **_envelope(source_record_refs),
    }


def _optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(Decimal(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed >= 0 else None


def ah_premium_wire(
    raw: Mapping[str, Any], *, source_record_refs: Sequence[str]
) -> dict[str, Any]:
    """One A+H pair, out of the vendor's whole A+H universe.

    The premium is the vendor's own number and is not recomputed here. The two
    prices are in two currencies, the exchange rate the vendor used is not
    published, and a locally recomputed premium would be a different figure
    wearing the vendor's name.
    """

    parameters = _parameters(raw, AH_PREMIUM_OPERATION)
    vendor, fallback_used = _capture_provenance(raw, AH_PREMIUM_OPERATION)
    ticker = str(parameters.get("ticker") or "").strip()
    if not ticker:
        raise CnHkFinDataAdapterError("the capture names no ticker")
    wanted = {ticker, ticker.zfill(5), ticker.zfill(6)}
    frame = _frame(raw, "ah")
    note = CALIBER_NOTES["eastmoney_ah_premium"]
    rows: list[dict[str, Any]] = []
    dropped = 0
    for row in frame["rows"]:
        h_code = _text(row, "H股代码")
        a_code = _text(row, "A股代码")
        name = _text(row, "名称")
        if h_code is None or a_code is None or name is None:
            dropped += 1
            continue
        if not (wanted & {h_code, a_code}):
            continue
        rows.append({
            "name": name,
            "h_code": h_code,
            "a_code": a_code,
            "h_price": _number(row, "最新价-HKD"),
            "h_change_percent": _number(row, "H股-涨跌幅"),
            "a_price": _number(row, "最新价-RMB"),
            "a_change_percent": _number(row, "A股-涨跌幅"),
            "price_ratio": _number(row, "比价"),
            "premium_percent": _number(row, "溢价"),
            "h_currency": "HKD",
            "a_currency": "CNY",
            **_provenance(AH_PREMIUM_OPERATION, vendor=vendor,
                          fallback_used=fallback_used, caliber_note=note),
        })
    rows.sort(key=lambda item: item["h_code"])
    return {
        "schema_version": WIRE_SCHEMA_VERSION,
        "ticker": ticker,
        "captured_at": _captured_at(raw),
        "rows": rows,
        "universe_row_count": len(frame["rows"]),
        "dropped_row_count": dropped,
        **_envelope(source_record_refs),
    }


WIRE_BUILDERS: dict[str, Callable[..., dict[str, Any]]] = {
    FINANCIAL_STATEMENTS_OPERATION: financial_statements_wire,
    SHAREHOLDERS_OPERATION: shareholders_wire,
    BUYBACKS_OPERATION: buybacks_wire,
    MARGIN_BALANCE_OPERATION: margin_balance_wire,
    NORTHBOUND_FLOW_OPERATION: northbound_flow_wire,
    AH_PREMIUM_OPERATION: ah_premium_wire,
}


FETCHERS: dict[str, Callable[..., dict[str, Any]]] = {
    FINANCIAL_STATEMENTS_OPERATION: fetch_financial_statements,
    SHAREHOLDERS_OPERATION: fetch_shareholders,
    BUYBACKS_OPERATION: fetch_buybacks,
    MARGIN_BALANCE_OPERATION: fetch_margin_balance,
    NORTHBOUND_FLOW_OPERATION: fetch_northbound_flow,
    AH_PREMIUM_OPERATION: fetch_ah_premium,
}


__all__ = [
    "CHINA_ACCOUNTING_STANDARD",
    "CnHkFinDataAdapterError",
    "CnHkFinDataVendorRefusal",
    "FETCHERS",
    "MAX_HOLDER_COUNT_ROWS",
    "MAX_PERIODS",
    "MAX_ROWS",
    "RAW_SCHEMA_VERSION",
    "WIRE_BUILDERS",
    "WIRE_SCHEMA_VERSION",
    "a_share_symbol",
    "ah_premium_wire",
    "buybacks_wire",
    "cell_text",
    "fetch_ah_premium",
    "fetch_buybacks",
    "fetch_financial_statements",
    "fetch_margin_balance",
    "fetch_northbound_flow",
    "fetch_shareholders",
    "financial_statements_wire",
    "frame_to_raw",
    "hk_symbol",
    "margin_balance_wire",
    "northbound_flow_wire",
    "shareholders_wire",
]
