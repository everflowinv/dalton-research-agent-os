"""W4: turn one HKEX document into rows, verbatim, and say what they imply.

Four readers and one context builder.  The readers never compute: every figure
they emit is the text the document held, with the thousands separators and the
trailing zeros the issuer chose, because ``HKD 438.2`` and ``438.20`` are two
different statements about how precisely a price was reported and a float
erases the difference.  The context builder is the only place arithmetic
happens, its formulas are frozen in this module, and everything it produces is
derived from figures that are in the same wire -- so a reader who disagrees
with a comparison can recompute it from the numbers beside it.

**Why the split matters here more than usual.**  The buy-back tape is the one
disclosure in this system that arrives every single day, and a daily number
with no context is noise: 231,000 shares means nothing until you know it is the
nineteenth consecutive day, that the pace has not changed, and that 0.49% of
the company has now been retired under this mandate.  Deciding whether that is
material is the brain's job.  Handing it a number with no denominator is how it
gets decided badly.
"""

from __future__ import annotations

import html as _html
import json
import re
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping, Sequence

from .hkex_filings_core import (
    ANNOUNCEMENTS_INDEX_OPERATION,
    CALIBER_NOTES,
    CAPACITY_CODE_MEANINGS,
    DISCLOSURE_OF_INTERESTS_OPERATION,
    HkexFilingsError,
    MONTHLY_RETURNS_OPERATION,
    NEXT_DAY_DISCLOSURE_OPERATION,
    REASON_CODE_MEANINGS,
    TRADE_REASON_CODES,
    normalise_ticker,
)
from .store import content_hash

WIRE_SCHEMA_VERSION = "0.1"
# One issuer files at most a handful of DI notices a week; a hundred in a
# window is a corporate action, and reading every one of them is not what a
# daily tick is for.
MAX_DI_NOTICES = 40
MAX_INDEX_ROWS = 200
MAX_BUYBACK_ROWS = 20
MAX_NOTES_CHARS = 4000
# How many prior disclosed days the pace comparison looks back over. Four weeks
# of trading days: long enough that one heavy day does not set the baseline,
# short enough that a mandate granted last quarter is not the comparison.
PACE_WINDOW_DAYS = 20
TRAILING_WINDOW_DAYS = 90


class HkexFilingsParseError(RuntimeError):
    """A captured HKEX document does not have the shape this reader knows."""


# -- small shared helpers ----------------------------------------------------

_NUMBER_RE = re.compile(r"^-?[0-9][0-9,]*(?:\.[0-9]+)?$")
_PERCENT_RE = re.compile(r"^-?[0-9][0-9,]*(?:\.[0-9]+)?\s*%?$")
_CURRENCY_AMOUNT_RE = re.compile(
    r"^(?P<currency>[A-Z]{3})\s*(?P<amount>-?[0-9][0-9,]*(?:\.[0-9]+)?)$"
)
_ISO_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_SLASH_DATE_RE = re.compile(r"^([0-9]{2})/([0-9]{2})/([0-9]{4})$")
_HK_DATE_RE = re.compile(r"^([0-9]{4})/([0-9]{2})/([0-9]{2})$")
_DI_SERIAL_RE = re.compile(r"^[A-Z]{2}[0-9]{8}[A-Z][0-9]{5}$")
_MONTH_ENDED_RE = re.compile(
    r"month ended\s+(?P<day>[0-9]{1,2})\s+(?P<month>[A-Za-z]+)\s+(?P<year>[0-9]{4})",
    re.IGNORECASE,
)
_MONTHS = {
    name.lower(): index for index, name in enumerate(
        ("January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"), start=1
    )
}


def _clean(value: Any) -> str:
    """One cell, with the non-breaking spaces and line breaks taken out."""

    text = _html.unescape(str(value)) if value is not None else ""
    text = text.replace("\xa0", " ").replace("​", "")
    return re.sub(r"\s+", " ", text).strip()


def _blank_to_none(value: str) -> str | None:
    return value or None


def _decimal(value: str | None, *, name: str) -> Decimal | None:
    """A figure as a number, for the derived block and for nothing else.

    Returns ``None`` rather than raising on an absent figure, because an absent
    figure is normal -- the Exchange leaves the lowest price blank when every
    purchase that day was at one price -- and refuses on a present figure it
    cannot read, because that is a shape nobody has seen.
    """

    if value is None or value == "":
        return None
    text = value.replace(",", "").replace("%", "").strip()
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise HkexFilingsParseError(f"{name}: {value!r} is not a figure") from exc


def _decimal_text(value: Decimal, places: int) -> str:
    quantised = value.quantize(Decimal(1).scaleb(-places))
    return format(quantised, "f")


def _split_currency(value: str, *, name: str) -> tuple[str | None, str | None]:
    """``HKD 438.2`` is two facts, and this is where they stop being one.

    A Hong Kong issuer may buy back in a currency that is not the Hong Kong
    dollar -- the same report carries GBP and USD rows -- so the currency
    travels as its own field rather than inside the amount, where a comparison
    would silently add two of them together.
    """

    text = _clean(value)
    if not text:
        return None, None
    match = _CURRENCY_AMOUNT_RE.fullmatch(text)
    if match is None:
        if _NUMBER_RE.fullmatch(text):
            return None, text
        raise HkexFilingsParseError(f"{name}: {value!r} is not a currency amount")
    return match.group("currency"), match.group("amount")


def _iso_from_slash(value: str, *, name: str) -> str | None:
    """``18/08/2026`` -> ``2026-08-18``; the DI database's date shape."""

    text = _clean(value)
    if not text:
        return None
    match = _SLASH_DATE_RE.fullmatch(text)
    if match is None:
        raise HkexFilingsParseError(f"{name}: {value!r} is not a dd/mm/yyyy date")
    day, month, year = match.groups()
    return f"{year}-{month}-{day}"


def _iso_from_hk(value: str, *, name: str) -> str | None:
    """``2026/09/08`` -> ``2026-09-08``; the Exchange report's date shape."""

    text = _clean(value)
    if not text:
        return None
    match = _HK_DATE_RE.fullmatch(text)
    if match is None:
        raise HkexFilingsParseError(f"{name}: {value!r} is not a yyyy/mm/dd date")
    return "-".join(match.groups())


def _percent(value: str, *, name: str) -> str | None:
    text = _clean(value).rstrip("%").strip()
    if not text:
        return None
    if not _PERCENT_RE.fullmatch(text):
        raise HkexFilingsParseError(f"{name}: {value!r} is not a percentage")
    return text


def _row_hash(row: Mapping[str, Any]) -> str:
    return content_hash(dict(row))


# -- (a) the Exchange's daily Share Repurchase Report ------------------------
#
# One workbook a trading day, every issuer that bought back on the previous
# one, every cell a string.  The capture holds it as a grid of that text.

BUYBACK_HEADER_MARKER = "Company"
BUYBACK_COLUMNS: tuple[str, ...] = (
    "company_name", "stock_code", "security_type", "trading_date",
    "shares_repurchased", "highest_price", "lowest_price", "aggregate_price_paid",
    "method_of_repurchase", "total_shares_repurchased",
    "shares_repurchased_for_cancellation", "shares_repurchased_for_treasury",
    "mandate_to_date_shares", "mandate_to_date_pct_of_issued",
)
_DATE_PRINTED_RE = re.compile(r"Date Printed\s*:\s*([0-9]{2}/[0-9]{2}/[0-9]{4})")


def _buyback_header_index(grid: Sequence[Sequence[str]]) -> int:
    for index, row in enumerate(grid):
        if row and _clean(row[0]) == BUYBACK_HEADER_MARKER:
            return index
    raise HkexFilingsParseError(
        "this workbook has no 'Company' header row; it is not the Exchange's "
        "share repurchase report"
    )


def buyback_report_printed_on(grid: Sequence[Sequence[str]]) -> str | None:
    """The date the Exchange printed this report, from the sheet itself.

    Read rather than taken from the request: the report printed on a day
    carries the *previous* trading day's purchases, and a wire that took the
    requested date as the trading date would file every purchase one day late.
    """

    for row in grid[:8]:
        for cell in row:
            match = _DATE_PRINTED_RE.search(_clean(cell))
            if match:
                return _iso_from_slash(match.group(1), name="date_printed")
    return None


def buyback_rows(
    grid: Sequence[Sequence[str]], *, ticker: str
) -> tuple[list[dict[str, Any]], int]:
    """Every row of the report for one stock code, and how many rows it held.

    The count is returned with the rows for the reason S4 wrote down about a
    market-wide table: without it, "this company bought nothing back" and "the
    table came back short" are the same empty answer.

    The Exchange writes the code unpadded (``700``) and HKEXnews writes it
    padded (``00700``); both are normalised before they are compared, so the
    match is between two companies rather than between two spellings.
    """

    wanted = normalise_ticker(ticker)
    header = _buyback_header_index(grid)
    rows: list[dict[str, Any]] = []
    universe = 0
    for raw in grid[header + 1:]:
        cells = [_clean(cell) for cell in raw]
        if len(cells) < len(BUYBACK_COLUMNS):
            cells = cells + [""] * (len(BUYBACK_COLUMNS) - len(cells))
        code = cells[1]
        if not code.isdigit():
            # A note, a footnote or the end-of-report banner. The report ends
            # with several paragraphs in column A and they are not rows.
            continue
        universe += 1
        if normalise_ticker(code) != wanted:
            continue
        high_currency, high = _split_currency(cells[5], name="highest_price")
        low_currency, low = _split_currency(cells[6], name="lowest_price")
        paid_currency, paid = _split_currency(cells[7], name="aggregate_price_paid")
        currencies = {value for value in (high_currency, low_currency, paid_currency)
                      if value}
        if len(currencies) > 1:
            raise HkexFilingsParseError(
                f"one report row mixes currencies {sorted(currencies)}; the "
                "Exchange aggregates one currency per row and a mixed row is a "
                "shape nobody has seen"
            )
        row = {
            "company_name": _blank_to_none(cells[0]),
            "stock_code": normalise_ticker(code),
            "security_type": _blank_to_none(cells[2]),
            "trading_date": _iso_from_hk(cells[3], name="trading_date"),
            "shares_repurchased": _blank_to_none(cells[4]),
            "highest_price": high,
            "lowest_price": low,
            "aggregate_price_paid": paid,
            "currency": next(iter(currencies)) if currencies else None,
            "method_of_repurchase": _blank_to_none(cells[8]),
            "total_shares_repurchased": _blank_to_none(cells[9]),
            "shares_repurchased_for_cancellation": _blank_to_none(cells[10]),
            "shares_repurchased_for_treasury": _blank_to_none(cells[11]),
            "mandate_to_date_shares": _blank_to_none(cells[12]),
            "mandate_to_date_pct_of_issued": _percent(
                cells[13], name="mandate_to_date_pct_of_issued"
            ),
            "caliber_note": CALIBER_NOTES["share_buyback_report"] + " "
                            + CALIBER_NOTES["mandate_to_date"],
        }
        row["record_hash"] = _row_hash(row)
        rows.append(row)
        if len(rows) >= MAX_BUYBACK_ROWS:
            break
    return rows, universe


def parse_next_day_disclosure(
    capture: Mapping[str, Any], *, ticker: str, artifact_hash: str,
    source_record_refs: Sequence[str] = (),
) -> dict[str, Any]:
    """The frozen wire for one issuer's day on the Exchange's buy-back tape."""

    document = _document(capture, "share_buyback_report")
    grid = document.get("grid")
    if not isinstance(grid, list) or not grid:
        raise HkexFilingsParseError("the buy-back capture holds no sheet grid")
    rows, universe = buyback_rows(grid, ticker=ticker)
    return {
        "schema_version": WIRE_SCHEMA_VERSION,
        "operation": NEXT_DAY_DISCLOSURE_OPERATION,
        "stock_code": normalise_ticker(ticker),
        "report_printed_on": buyback_report_printed_on(grid),
        "report_url": document["url"],
        "report_sha256": document["sha256"],
        "rows": rows,
        "row_count": len(rows),
        "universe_row_count": universe,
        "artifact_hash": artifact_hash,
        "source_record_refs": list(source_record_refs),
        "next_cursor": None,
        "provider_status": 200,
    }


# -- (b) and (d) the HKEXnews title search -----------------------------------

_INDEX_COLUMNS = ("NEWS_ID", "DATE_TIME", "STOCK_CODE", "STOCK_NAME", "TITLE",
                  "LONG_TEXT", "FILE_LINK", "FILE_TYPE", "FILE_INFO")


def title_search_payload(text: str) -> dict[str, Any]:
    """The servlet's envelope, whose ``result`` is JSON inside JSON."""

    try:
        envelope = json.loads(text)
    except ValueError as exc:
        raise HkexFilingsParseError("the title search body is not JSON") from exc
    if not isinstance(envelope, Mapping) or "result" not in envelope:
        raise HkexFilingsParseError("the title search body has no result field")
    raw = envelope["result"]
    try:
        rows = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as exc:
        raise HkexFilingsParseError("the title search result is not JSON") from exc
    if not isinstance(rows, list):
        raise HkexFilingsParseError("the title search result is not a list")
    return {"envelope": envelope, "rows": rows}


def stock_id_from_prefix(text: str, *, ticker: str) -> int:
    """The internal ``stockId`` HKEXnews keys its search by.

    Answered in JSONP, so the callback wrapper comes off first.  More than one
    match is a refusal: ``prefix.do`` is a *prefix* search and resolving to the
    first hit would search one company's announcements under another's code.
    """

    body = _clean(text)
    start, end = body.find("("), body.rfind(")")
    if start < 0 or end <= start:
        raise HkexFilingsParseError("the prefix response is not JSONP")
    try:
        payload = json.loads(body[start + 1:end])
    except ValueError as exc:
        raise HkexFilingsParseError("the prefix response is not JSON") from exc
    matches = [
        item for item in (payload.get("stockInfo") or [])
        if isinstance(item, Mapping)
        and normalise_ticker(str(item.get("code") or "0")) == normalise_ticker(ticker)
    ]
    if len(matches) != 1:
        raise HkexFilingsParseError(
            f"{ticker} resolved to {len(matches)} securities on HKEXnews; a "
            "search that cannot name one security must not be run under one "
            "company's name"
        )
    stock_id = matches[0].get("stockId")
    if not isinstance(stock_id, int) or isinstance(stock_id, bool) or stock_id <= 0:
        raise HkexFilingsParseError("the prefix response has no stockId")
    return stock_id


def _index_row(item: Mapping[str, Any], *, ticker: str) -> dict[str, Any] | None:
    """One search hit, with the ``<br/>``-joined multi-counter fields split.

    A Hong Kong issuer with a renminbi counter files one announcement under two
    codes, and HKEXnews writes them into one cell separated by ``<br/>``.  The
    row keeps every code it named and says which one was asked for, because
    dropping the second is how a document filed under 80700 stops being
    Tencent's.
    """

    wanted = normalise_ticker(ticker)
    codes = [
        normalise_ticker(part) for part in
        re.split(r"<br\s*/?>", str(item.get("STOCK_CODE") or ""))
        if _clean(part).isdigit()
    ]
    if wanted not in codes:
        return None
    names = [_clean(part) for part in
             re.split(r"<br\s*/?>", str(item.get("STOCK_NAME") or "")) if _clean(part)]
    filed_at = _clean(item.get("DATE_TIME"))
    match = re.fullmatch(r"([0-9]{2})/([0-9]{2})/([0-9]{4}) ([0-9]{2}:[0-9]{2})",
                         filed_at)
    if match is None:
        raise HkexFilingsParseError(f"{filed_at!r} is not an HKEXnews filing time")
    day, month, year, clock = match.groups()
    row = {
        "news_id": _clean(item.get("NEWS_ID")) or None,
        "filed_at": f"{year}-{month}-{day}T{clock}:00+08:00",
        "filed_on": f"{year}-{month}-{day}",
        "stock_codes": codes,
        "stock_names": names,
        "title": _clean(item.get("TITLE")) or None,
        "headline_category": _clean(item.get("LONG_TEXT")) or None,
        "file_link": _clean(item.get("FILE_LINK")) or None,
        "file_type": _clean(item.get("FILE_TYPE")) or None,
        "file_size": _clean(item.get("FILE_INFO")) or None,
    }
    row["record_hash"] = _row_hash(row)
    return row


def _title_search_rows(capture: Mapping[str, Any], *, ticker: str) -> tuple[
    list[dict[str, Any]], Mapping[str, Any], Mapping[str, Any]
]:
    document = _document(capture, "title_search")
    parsed = title_search_payload(document["text"])
    rows = []
    for item in parsed["rows"][:MAX_INDEX_ROWS]:
        if not isinstance(item, Mapping):
            continue
        row = _index_row(item, ticker=ticker)
        if row is not None:
            rows.append(row)
    return rows, parsed["envelope"], document


def parse_announcements_index(
    capture: Mapping[str, Any], *, ticker: str, artifact_hash: str,
    source_record_refs: Sequence[str] = (),
) -> dict[str, Any]:
    """Every announcement one stock code filed in the captured window."""

    rows, envelope, document = _title_search_rows(capture, ticker=ticker)
    return {
        "schema_version": WIRE_SCHEMA_VERSION,
        "operation": ANNOUNCEMENTS_INDEX_OPERATION,
        "stock_code": normalise_ticker(ticker),
        "since": _clean(capture["parameters"].get("since")) or None,
        "until": _clean(capture["parameters"].get("until")) or None,
        "headline_category": _clean(capture["parameters"].get("tier_one")) or None,
        "search_url": document["url"],
        "rows": rows,
        "row_count": len(rows),
        "record_count": int(envelope.get("recordCnt") or 0),
        "has_next_row": bool(envelope.get("hasNextRow")),
        "artifact_hash": artifact_hash,
        "source_record_refs": list(source_record_refs),
        "next_cursor": None,
        "provider_status": 200,
    }


def month_ended(title: str | None) -> str | None:
    """``... for the month ended 31 August 2026`` -> ``2026-08-31``."""

    if not title:
        return None
    match = _MONTH_ENDED_RE.search(title)
    if match is None:
        return None
    month = _MONTHS.get(match.group("month").lower())
    if month is None:
        return None
    return f"{match.group('year')}-{month:02d}-{int(match.group('day')):02d}"


def parse_monthly_returns(
    capture: Mapping[str, Any], *, ticker: str, artifact_hash: str,
    source_record_refs: Sequence[str] = (),
) -> dict[str, Any]:
    """The Monthly Returns one stock code filed, as an index and nothing more.

    Every row says which month it covers and where the document is.  None of
    them says what the issued-share count was: see
    ``hkex_filings_core.MONTHLY_RETURN_LIMIT_NOTE``.
    """

    rows, envelope, document = _title_search_rows(capture, ticker=ticker)
    for row in rows:
        row["period_end"] = month_ended(row["title"])
        row["caliber_note"] = CALIBER_NOTES["monthly_return_index_only"]
        row["record_hash"] = _row_hash(
            {key: value for key, value in row.items() if key != "record_hash"}
        )
    return {
        "schema_version": WIRE_SCHEMA_VERSION,
        "operation": MONTHLY_RETURNS_OPERATION,
        "stock_code": normalise_ticker(ticker),
        "since": _clean(capture["parameters"].get("since")) or None,
        "until": _clean(capture["parameters"].get("until")) or None,
        "search_url": document["url"],
        "rows": rows,
        "row_count": len(rows),
        "record_count": int(envelope.get("recordCnt") or 0),
        "figures_available": False,
        "artifact_hash": artifact_hash,
        "source_record_refs": list(source_record_refs),
        "next_cursor": None,
        "provider_status": 200,
    }


# -- (c) the Disclosure of Interests database --------------------------------


class _TableTextParser(HTMLParser):
    """Every visible string on an ASP.NET page, in document order.

    A structural parse of these pages is not worth having: the DI forms nest
    tables six deep, the nesting differs between Form 1 and Form 3A, and the
    labels -- which do not move -- are what the fields are anchored to.  So the
    page becomes a token stream and the readers below scan it for the label
    they need and take what follows.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._link_text: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
        if tag == "a" and self._href is not None:
            self.links.append((self._href, _clean("".join(self._link_text))))
            self._href = None
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = _clean(data)
        if self._href is not None:
            self._link_text.append(data)
        if text:
            self.tokens.append(text)


def _page(text: str) -> _TableTextParser:
    parser = _TableTextParser()
    parser.feed(text)
    parser.close()
    return parser


def di_corporation_from_list(text: str, *, ticker: str) -> dict[str, Any]:
    """The corporation's internal ``sid`` and printed name, from its own link.

    Never composed and never defaulted.  ``NSAllFormList`` answers with
    whatever corporation the ``sid`` names and ignores the stock code beside
    it, so a guessed ``sid`` returns a complete, well-formed page of another
    company's directors -- an answer nothing downstream could tell from the
    right one.
    """

    wanted = normalise_ticker(ticker)
    page = _page(text)
    found: dict[str, Any] | None = None
    for href, _label in page.links:
        if "NSAllFormList.aspx" not in href:
            continue
        query = dict(
            part.split("=", 1) for part in _html.unescape(href).split("?", 1)[-1].split("&")
            if "=" in part
        )
        sid, code = query.get("sid"), query.get("sc")
        if not sid or not sid.isdigit():
            continue
        if code and code.isdigit() and normalise_ticker(code) != wanted:
            continue
        from urllib.parse import unquote_plus

        candidate = {
            "sid": int(sid),
            "corporation_name": unquote_plus(query.get("corpn", "")).strip(),
            "stock_code": wanted,
        }
        if found is not None and found["sid"] != candidate["sid"]:
            raise HkexFilingsParseError(
                f"stock code {wanted} names more than one corporation in the DI "
                f"database ({found['sid']} and {candidate['sid']}); reading one "
                "of them would be reading a company nobody asked about"
            )
        found = candidate
    if found is None:
        raise HkexFilingsParseError(
            f"the DI corporation list holds no corporation for {wanted}"
        )
    return found


_DI_LIST_HEADER = "Form Serial Number"
# The list's own column headings, and the field each one becomes.  Matched by
# heading rather than by position: an empty cell is a real thing here -- a
# notice with no average price is the ordinary case for a code 1316 -- and a
# reader that counted non-empty cells would read the shares interested as the
# price, which is what the first version of this did.
_DI_LIST_FIELDS: tuple[tuple[str, str], ...] = (
    ("Form Serial Number", "form_serial_number"),
    ("Name of substantial shareholder", "person_name"),
    ("Reason for disclosure", "reason_code"),
    ("No. of shares bought", "shares_involved"),
    ("Average price per share", "average_price"),
    ("No. of shares interested", "shares_interested"),
    ("% of issued voting shares", "pct_of_issued_voting_shares"),
    ("Date of relevant event", "event_date"),
)


class _TableParser(HTMLParser):
    """Every HTML table on a page, as rows of cell text with the blanks kept.

    ``_TableTextParser`` throws empty cells away, which is right for the DI
    forms -- they are labels and values, not a grid -- and wrong for the notice
    list, which is a grid whose empty cells carry meaning.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._stack: list[list[list[str]]] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._stack[-1].append([])
        elif tag in {"td", "th"} and self._stack and self._stack[-1]:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._stack and self._stack[-1]:
            self._stack[-1][-1].append(_clean("".join(self._cell)))
            self._cell = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _di_list_table(text: str) -> tuple[list[str], list[list[str]]]:
    """The one table on the page whose header names the form serial number."""

    parser = _TableParser()
    parser.feed(text)
    parser.close()
    for table in parser.tables:
        for index, row in enumerate(table):
            if row and _clean(row[0]) == _DI_LIST_HEADER:
                return row, table[index + 1:]
    raise HkexFilingsParseError(
        "this page has no 'Form Serial Number' column; it is not a DI notice list"
    )


def _di_column_index(header: Sequence[str], prefix: str) -> int | None:
    for index, cell in enumerate(header):
        if cell.startswith(prefix):
            return index
    return None


def di_notice_rows(text: str) -> list[dict[str, Any]]:
    """One row per DI notice, from the all-notices list page.

    The list carries the person, the reason code, the size and the average
    price.  What it does not carry is the holding before the event, which is
    why each notice's own form is read as well.
    """

    header, body = _di_list_table(text)
    columns = {
        field: _di_column_index(header, prefix)
        for prefix, field in _DI_LIST_FIELDS
    }
    missing = sorted(field for field, index in columns.items() if index is None)
    if missing:
        raise HkexFilingsParseError(
            f"the DI notice list is missing the columns {missing}; its shape has "
            "changed and reading it by position would misfile every figure"
        )
    page = _page(text)
    by_serial: dict[str, str] = {}
    for href, label in page.links:
        if "NSForm" in href and _DI_SERIAL_RE.fullmatch(label or ""):
            by_serial.setdefault(label, _html.unescape(href).split("&", 1)[0])
    rows: list[dict[str, Any]] = []
    for cells in body:
        if len(cells) < len(header):
            continue
        serial = _clean(cells[columns["form_serial_number"]])
        if not _DI_SERIAL_RE.fullmatch(serial):
            continue
        reason_code = _long_value(cells[columns["reason_code"]]) or ""
        # ``HKD 503.0000`` in the average-price cell: the same two-facts-in-one
        # shape the buy-back report uses, split for the same reason.
        price_currency, price = _split_currency(
            _clean(cells[columns["average_price"]]), name="average_price",
        )
        row = {
            "form_serial_number": serial,
            "form_path": by_serial.get(serial),
            "person_name": _blank_to_none(_clean(cells[columns["person_name"]])),
            "reason_code": reason_code or None,
            "reason_meaning": REASON_CODE_MEANINGS.get(reason_code),
            "is_trade": reason_code in TRADE_REASON_CODES,
            "shares_involved": _long_value(cells[columns["shares_involved"]]),
            "average_price": price,
            "average_price_currency": price_currency,
            "shares_interested": _long_value(cells[columns["shares_interested"]]),
            "short_position_interested": positions(
                cells[columns["shares_interested"]]
            ).get("S"),
            "pct_of_issued_voting_shares": _long_value(
                cells[columns["pct_of_issued_voting_shares"]]
            ),
            "position_marker": _position_marker(cells[columns["shares_interested"]]),
            "event_date": _iso_from_slash(
                cells[columns["event_date"]], name="event_date"
            ),
        }
        row["record_hash"] = _row_hash(row)
        rows.append(row)
        if len(rows) >= MAX_DI_NOTICES:
            break
    return rows


_POSITION_WORDS = ("L", "S", "P")
# ``63,494(L)0(S)``: one cell, two positions, no separator between them. The
# DI list writes the long position, the short position and the lending pool
# into a single cell and marks each with a letter, so a reader that took the
# cell as a number would report a director's holding as "63,494(L)0(S)" and a
# reader that stripped the trailing marker would report it as 0.
_POSITION_RE = re.compile(r"(-?[0-9][0-9,]*(?:\.[0-9]+)?)\(([LSP])\)")


def positions(value: str | None) -> dict[str, str]:
    """Every position a DI cell holds, keyed by ``L``, ``S`` or ``P``.

    A cell with no marker at all -- which is how the average price is written
    -- comes back under ``L``, because a figure the database did not qualify is
    the one figure it is describing.
    """

    text = _clean(value or "")
    if not text:
        return {}
    found = {marker: figure for figure, marker in _POSITION_RE.findall(text)}
    if found:
        return found
    return {"L": text}


def _long_value(value: str | None) -> str | None:
    """The long position in a DI cell, which is the one these rows are about."""

    return positions(value).get("L")


def _position_marker(value: str | None) -> str | None:
    """``(L)``, ``(S)`` or ``(P)``, when the cell named exactly one of them.

    Kept because a short position and a holding are not the same fact, and a
    figure whose marker has been dropped reads as a holding whatever it was.
    """

    found = _POSITION_RE.findall(_clean(value or ""))
    markers = sorted({marker for _figure, marker in found})
    if len(markers) == 1:
        return f"({markers[0]})"
    return None


_BEFORE_LABEL = "Total shares in listed corporation immediately before the relevant event"
_AFTER_LABEL = "Total shares in listed corporation immediately after the relevant event"
_ISSUED_LABEL = "Number of issued shares in class"
_CLASS_LABEL = "Class of shares"
_FORM_TITLE_RE = re.compile(r"^FORM\s+([0-9][A-Z]?)\s*-\s*(.+)$", re.IGNORECASE)


def di_form_detail(text: str) -> dict[str, Any]:
    """The holding before and after one relevant event, and how it is held.

    Long positions only.  A short position and a lending pool are real and are
    not the same fact as a holding; they are carried into the notes text rather
    than folded into a number that would then be wrong in both directions.
    """

    page = _page(text)
    tokens = page.tokens
    detail: dict[str, Any] = {
        "form_type": None,
        "form_title": None,
        "class_of_shares": None,
        "issued_shares_in_class": None,
        "shares_before": None,
        "pct_before": None,
        "shares_after": None,
        "pct_after": None,
        "capacity_codes": [],
        "short_position_present": False,
    }
    for token in tokens[:40]:
        match = _FORM_TITLE_RE.match(token)
        if match:
            detail["form_type"] = f"Form {match.group(1).upper()}"
            detail["form_title"] = token
            break
    detail["class_of_shares"] = _value_after(tokens, _CLASS_LABEL)
    detail["issued_shares_in_class"] = _value_after(tokens, _ISSUED_LABEL, numeric=True)
    before = _position_totals(tokens, _BEFORE_LABEL)
    after = _position_totals(tokens, _AFTER_LABEL)
    detail["shares_before"], detail["pct_before"] = before
    detail["shares_after"], detail["pct_after"] = after
    detail["capacity_codes"] = sorted({
        token for token in tokens
        if re.fullmatch(r"21[0-9]{2}", token)
    })
    detail["short_position_present"] = any(
        token == "Short position" for token in tokens
    ) and _position_totals(tokens, _AFTER_LABEL, position="Short position")[0] is not None
    return detail


def _value_after(
    tokens: Sequence[str], label: str, *, numeric: bool = False
) -> str | None:
    try:
        index = tokens.index(label)
    except ValueError:
        return None
    for token in tokens[index + 1: index + 6]:
        if token in {":", "&nbsp;", ""}:
            continue
        if numeric and not _NUMBER_RE.fullmatch(token):
            continue
        return token
    return None


def _position_totals(
    tokens: Sequence[str], label: str, *, position: str = "Long position"
) -> tuple[str | None, str | None]:
    try:
        index = tokens.index(label)
    except ValueError:
        return None, None
    window = tokens[index: index + 24]
    try:
        anchor = window.index(position)
    except ValueError:
        return None, None
    figures = [value for value in window[anchor + 1: anchor + 5]
               if _NUMBER_RE.fullmatch(value)]
    if not figures:
        return None, None
    return figures[0], (figures[1] if len(figures) > 1 else None)


def di_notes_text(row: Mapping[str, Any], detail: Mapping[str, Any]) -> str:
    """Everything a DI notice said that the event payload has no field for.

    Hashed into the event and carried in full on the wire.  The alternative was
    to drop it, and what would be dropped is the SFC's capacity codes -- the
    difference between a director who owns shares and a director who is a
    trustee of somebody else's -- plus any short position.  Both change what
    the number means.
    """

    parts = [
        f"form={detail.get('form_type') or 'unknown'}",
        f"form_title={detail.get('form_title') or ''}",
        f"reason_code={row.get('reason_code')}",
        f"reason_meaning={row.get('reason_meaning') or 'not in the published code table'}",
        f"capacity_codes={','.join(detail.get('capacity_codes') or []) or 'none'}",
        "capacity_meanings=" + ",".join(
            CAPACITY_CODE_MEANINGS.get(code, f"{code} (not in the published table)")
            for code in (detail.get("capacity_codes") or [])
        ),
        f"class_of_shares={detail.get('class_of_shares') or ''}",
        f"issued_shares_in_class={detail.get('issued_shares_in_class') or ''}",
        f"pct_before={detail.get('pct_before') or ''}",
        f"pct_after={detail.get('pct_after') or ''}",
        f"short_position_interested={row.get('short_position_interested') or ''}",
        f"position_marker={row.get('position_marker') or ''}",
        f"short_position_present={bool(detail.get('short_position_present'))}",
        f"caliber={CALIBER_NOTES['di_capacity_codes']}",
    ]
    return "\n".join(parts)[:MAX_NOTES_CHARS]


DIRECTOR_FORMS = frozenset({"Form 3A", "Form 3B"})


def _direct_or_indirect(capacity_codes: Sequence[str]) -> str | None:
    """The SFC's capacity codes onto the one letter the payload field holds.

    ``2101`` is a beneficial owner and is the only code that means the person
    holds the shares themselves; every other capacity in the table -- trustee,
    investment manager, controlled corporation, spouse, child -- is somebody
    else's economic interest held or controlled by this person.  Which one it
    was is not thrown away: the code and its meaning are in the notes text this
    row's hash covers.
    """

    codes = [code for code in capacity_codes if code]
    if not codes:
        return None
    return "D" if codes == ["2101"] else "I"


def _acquired_disposed(before: str | None, after: str | None) -> str | None:
    """A or D, from two verbatim totals and nothing else.

    Derived rather than read, because the DI list has no such column and the
    reason code does not always carry the direction.  Two numbers that are
    equal say the holding did not change, which is what a code-1316 change in
    the nature of an interest is, and that is reported as neither.
    """

    left = _decimal(before, name="shares_before")
    right = _decimal(after, name="shares_after")
    if left is None or right is None:
        return None
    if right > left:
        return "A"
    if right < left:
        return "D"
    return None


def parse_disclosure_of_interests(
    capture: Mapping[str, Any], *, ticker: str, artifact_hash: str,
    source_record_refs: Sequence[str] = (),
) -> dict[str, Any]:
    """One issuer's DI notices, each joined to its own form."""

    corp_document = _document(capture, "di_corp_list")
    list_document = _document(capture, "di_form_list")
    corporation = di_corporation_from_list(corp_document["text"], ticker=ticker)
    rows = di_notice_rows(list_document["text"])
    details = {
        document["role"].split(":", 1)[1]: document
        for document in capture["documents"]
        if str(document.get("role", "")).startswith("di_form:")
    }
    joined: list[dict[str, Any]] = []
    for row in rows:
        detail_document = details.get(row["form_serial_number"])
        detail = (
            di_form_detail(detail_document["text"]) if detail_document
            else {"form_type": None, "form_title": None, "class_of_shares": None,
                  "issued_shares_in_class": None, "shares_before": None,
                  "pct_before": None, "shares_after": None, "pct_after": None,
                  "capacity_codes": [], "short_position_present": False}
        )
        notes = di_notes_text(row, detail)
        merged = {
            **{key: value for key, value in row.items() if key != "record_hash"},
            "form_type": detail["form_type"],
            "is_director_notice": (detail["form_type"] or "") in DIRECTOR_FORMS,
            "class_of_shares": detail["class_of_shares"],
            "issued_shares_in_class": detail["issued_shares_in_class"],
            "shares_before": detail["shares_before"],
            "pct_before": detail["pct_before"],
            "shares_after": detail["shares_after"] or row["shares_interested"],
            "pct_after": detail["pct_after"] or row["pct_of_issued_voting_shares"],
            "capacity_codes": list(detail["capacity_codes"]),
            "direct_or_indirect": _direct_or_indirect(detail["capacity_codes"]),
            "acquired_disposed": _acquired_disposed(
                detail["shares_before"], detail["shares_after"]
            ),
            "detail_read": detail_document is not None,
            "notes_text": notes,
            "notes_text_hash": content_hash({"notes": notes}),
            "caliber_note": CALIBER_NOTES["di_list_and_form"],
        }
        merged["record_hash"] = _row_hash(merged)
        joined.append(merged)
    return {
        "schema_version": WIRE_SCHEMA_VERSION,
        "operation": DISCLOSURE_OF_INTERESTS_OPERATION,
        "stock_code": normalise_ticker(ticker),
        "corporation_name": corporation["corporation_name"] or None,
        "corporation_sid": corporation["sid"],
        "since": _clean(capture["parameters"].get("since")) or None,
        "until": _clean(capture["parameters"].get("until")) or None,
        "list_url": list_document["url"],
        "rows": joined,
        "row_count": len(joined),
        "detail_read_count": sum(1 for row in joined if row["detail_read"]),
        "artifact_hash": artifact_hash,
        "source_record_refs": list(source_record_refs),
        "next_cursor": None,
        "provider_status": 200,
    }


# -- derived, replayable context ---------------------------------------------
#
# Everything below is arithmetic over figures that are in the same wire, with
# the formula written next to it.  Nothing here is fetched, nothing here is
# remembered between runs, and a reader who disagrees with a comparison can
# recompute it from the numbers beside it.  That is what makes it replayable:
# the same wire and the same handed-in history always give the same block.

UNAVAILABLE = "unavailable"


def _ratio(numerator: Decimal | None, denominator: Decimal | None,
           *, places: int = 6) -> str | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return _decimal_text(numerator / denominator, places)


def average_price_paid(row: Mapping[str, Any]) -> str | None:
    """Aggregate price paid / shares repurchased, to six decimal places.

    Derived rather than read because the Exchange's report does not carry an
    average: it carries the highest and the lowest.  Six places because a Hong
    Kong share can trade at 0.043 and a rounded average of a small-cap buy-back
    would be a different number from the one the issuer paid.
    """

    return _ratio(
        _decimal(row.get("aggregate_price_paid"), name="aggregate_price_paid"),
        _decimal(row.get("shares_repurchased"), name="shares_repurchased"),
    )


def buyback_cluster_key(row: Mapping[str, Any]) -> str | None:
    """The ISO week one day's buy-back belongs to.

    Hong Kong's daily tape produces one disclosure a trading day, and an issuer
    running a programme files twenty a month.  Twenty events that each say
    "bought some more" is not twenty pieces of news; the week is the unit a
    reader actually reasons in, so every row carries the key that rolls it up.
    """

    from datetime import date as _date

    trading_date = row.get("trading_date")
    if not trading_date or not _ISO_DATE_RE.fullmatch(str(trading_date)):
        return None
    year, week, _weekday = _date.fromisoformat(str(trading_date)).isocalendar()
    return f"{year}-W{week:02d}"


def buyback_pace(
    row: Mapping[str, Any], *, prior_rows: Sequence[Mapping[str, Any]] = ()
) -> dict[str, Any]:
    """This day against the pace of the days before it.

    ``prior_rows`` are earlier rows of this same report for this same issuer,
    handed in by the caller from what it already read.  The lane holds them;
    this function holds nothing, which is why running it twice on the same
    inputs gives the same answer.
    """

    window = [
        item for item in prior_rows
        if item.get("trading_date") and item.get("trading_date") != row.get("trading_date")
    ][-PACE_WINDOW_DAYS:]
    shares = [
        value for value in
        (_decimal(item.get("shares_repurchased"), name="shares_repurchased")
         for item in window)
        if value is not None
    ]
    today = _decimal(row.get("shares_repurchased"), name="shares_repurchased")
    if not shares or today is None:
        return {
            "status": UNAVAILABLE,
            "reason": (
                "no earlier disclosed day for this issuer is in hand; the first "
                "buy-back a Core sees has nothing to be faster or slower than"
            ),
            "window_days": len(window),
        }
    mean = sum(shares, Decimal(0)) / Decimal(len(shares))
    return {
        "status": "compared",
        "window_days": len(window),
        "mean_daily_shares": _decimal_text(mean, 0),
        "shares_today": row.get("shares_repurchased"),
        "vs_mean": _ratio(today, mean, places=4),
        "formula": "shares_repurchased / mean(shares_repurchased over the prior "
                   f"{PACE_WINDOW_DAYS} disclosed days, this day excluded)",
    }


def buyback_context(
    row: Mapping[str, Any],
    *,
    prior_rows: Sequence[Mapping[str, Any]] = (),
    current_price: str | None = None,
    current_price_ref: str | None = None,
) -> dict[str, Any]:
    """What one day's buy-back means beside the numbers that give it a size.

    ``current_price`` is handed in rather than fetched.  It is normally
    ``None``: see ``hkex_filings_core.PRICE_AUTHORITY_NOTE`` -- Dalton's price
    authority cannot hold a Hong Kong symbol today, so the comparison a reader
    most wants is reported ``unavailable`` with the reason rather than made
    against a series that is not there.
    """

    from .hkex_filings_core import PRICE_AUTHORITY_NOTE

    average = average_price_paid(row)
    price = _decimal(current_price, name="current_price")
    if price is None:
        versus: dict[str, Any] = {
            "status": UNAVAILABLE,
            "reason": PRICE_AUTHORITY_NOTE,
        }
    else:
        versus = {
            "status": "compared",
            "current_price": current_price,
            "current_price_ref": current_price_ref,
            "average_price_paid": average,
            "premium_to_current": _ratio(
                (_decimal(average, name="average_price_paid") or Decimal(0)) - price,
                price, places=6,
            ),
            "formula": "(average_price_paid - current_price) / current_price",
        }
    return {
        "schema_version": WIRE_SCHEMA_VERSION,
        "trading_date": row.get("trading_date"),
        "cluster_key": buyback_cluster_key(row),
        "average_price_paid": average,
        "average_price_formula": "aggregate_price_paid / shares_repurchased",
        "currency": row.get("currency"),
        "price_vs_current": versus,
        "pace": buyback_pace(row, prior_rows=prior_rows),
        "cumulative": {
            "basis": "since the resolution granting the current repurchase mandate",
            "shares": row.get("mandate_to_date_shares"),
            "pct_of_issued": row.get("mandate_to_date_pct_of_issued"),
            "caliber_note": CALIBER_NOTES["mandate_to_date"],
        },
    }


def di_context(
    row: Mapping[str, Any], *, prior_rows: Sequence[Mapping[str, Any]] = ()
) -> dict[str, Any]:
    """How big one person's trade was against what they hold, and the run of them.

    ``size_vs_holdings`` is the number that decides whether a director selling
    matters: five thousand shares is a rounding error for one holder and the
    whole position for another, and the notice reports both figures.
    """

    from datetime import date as _date, timedelta as _timedelta

    involved = _decimal(row.get("shares_involved"), name="shares_involved")
    before = _decimal(row.get("shares_before"), name="shares_before")
    after = _decimal(row.get("shares_after"), name="shares_after")
    event_date = row.get("event_date")
    trailing: dict[str, Any] = {"status": UNAVAILABLE,
                                "reason": "this notice carries no event date"}
    if event_date and _ISO_DATE_RE.fullmatch(str(event_date)):
        floor = (_date.fromisoformat(str(event_date))
                 - _timedelta(days=TRAILING_WINDOW_DAYS)).isoformat()
        window = [
            item for item in prior_rows
            if item.get("event_date") and floor <= str(item["event_date"]) <= str(event_date)
        ]
        totals = [
            _decimal(item.get("shares_involved"), name="shares_involved")
            for item in window
        ]
        trailing = {
            "status": "computed",
            "window_days": TRAILING_WINDOW_DAYS,
            "since": floor,
            "notice_count": len(window),
            "people": sorted({str(item.get("person_name") or "") for item in window
                              if item.get("person_name")}),
            "shares_involved_total": _decimal_text(
                sum((value for value in totals if value is not None), Decimal(0)), 0
            ),
            "disposals": sum(1 for item in window
                             if item.get("acquired_disposed") == "D"),
            "acquisitions": sum(1 for item in window
                                if item.get("acquired_disposed") == "A"),
        }
    return {
        "schema_version": WIRE_SCHEMA_VERSION,
        "event_date": event_date,
        "size_vs_holdings": {
            "status": "computed" if involved is not None and (before or after)
            else UNAVAILABLE,
            "shares_involved": row.get("shares_involved"),
            "shares_before": row.get("shares_before"),
            "shares_after": row.get("shares_after"),
            "share_of_prior_holding": _ratio(involved, before),
            "share_of_current_holding": _ratio(involved, after),
            "formula": "shares_involved / shares_before, and / shares_after",
        },
        "trailing_90d": trailing,
        "is_trade": bool(row.get("is_trade")),
        "caliber_note": CALIBER_NOTES["di_capacity_codes"],
    }


# -- events ------------------------------------------------------------------

BUYBACK_EVENT_KIND = "buyback_disclosure"
BUYBACK_FORM = "Next Day Disclosure Return"
MARKET = "HK"


def buyback_reference(wire: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    """The name of the one disclosure this row is: report, code, trading day.

    Hong Kong has no accession number, so the reference is built out of the
    three things that identify one row of one report and is stable across
    re-reads of the same report.
    """

    return (
        f"hkex:share-repurchase-report:{wire.get('report_printed_on')}"
        f":{wire.get('stock_code')}:{row.get('trading_date')}"
    )


def buyback_events(
    wire: Mapping[str, Any],
    *,
    company_ref: str,
    invocation_ref: str,
    artifact_hash: str,
    prior_rows: Sequence[Mapping[str, Any]] = (),
    current_price: str | None = None,
) -> list[dict[str, Any]]:
    """Map filed HK daily returns to the shared US/HK event contract.

    The exchange cumulative count is since the repurchase mandate, never YTD.
    The average remains derived in context rather than represented as filed.
    """

    from .research_event import validate_payload

    events: list[dict[str, Any]] = []
    for row in wire["rows"]:
        occurred = row.get("trading_date") or wire.get("report_printed_on")
        if not occurred:
            continue
        reference = buyback_reference(wire, row)
        payload = {
            "accession": reference,
            "form": BUYBACK_FORM,
            "market": MARKET,
            "filing_date": wire.get("report_printed_on"),
            "period_start": row.get("trading_date"),
            "period_end": row.get("trading_date"),
            "shares_purchased": row.get("shares_repurchased"),
            # Not disclosed. The report gives the highest and the lowest; the
            # average is arithmetic and lives in the derived context, where a
            # reader can see the formula that produced it.
            "average_price_paid": None,
            "price_low": row.get("lowest_price"),
            "price_high": row.get("highest_price"),
            "total_paid": row.get("aggregate_price_paid"),
            "currency": row.get("currency"),
            # Only the issuer's own return carries the mandate ceiling; the
            # Exchange's aggregation does not.
            "remaining_authorisation": None,
            "cumulative_shares": row.get("mandate_to_date_shares"),
            "cumulative_basis": "since_mandate" if row.get("mandate_to_date_shares") is not None else None,
            "cluster_key": f"{buyback_cluster_key(row)}:{wire.get('report_printed_on')}",
            "disclosure_kind": "next_day_disclosure_return",
            "pct_of_issued": row.get("mandate_to_date_pct_of_issued"),
            "invocation_ref": invocation_ref,
            "artifact_hash": artifact_hash,
            "event_key": content_hash({"row": row["record_hash"], "ref": reference}),
        }
        payload = validate_payload(BUYBACK_EVENT_KIND, payload)
        events.append({
            "kind": BUYBACK_EVENT_KIND,
            "company_ref": company_ref,
            "occurred_at": f"{occurred}T00:00:00+00:00",
            "payload": payload,
            "context": buyback_context(
                row, prior_rows=prior_rows, current_price=current_price
            ),
        })
    return events


DIRECTOR_ROLE = "director or chief executive"
SHAREHOLDER_ROLE = "substantial shareholder"


def di_events(
    wire: Mapping[str, Any],
    *,
    company_ref: str,
    invocation_ref: str,
    artifact_hash: str,
    prior_rows: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """One event per DI notice: a director's is a transaction, a holder's is not.

    A director or chief executive files Form 3A or 3B and what they filed is a
    person trading their own company, which is ``insider_transaction``.  A
    substantial shareholder files Form 1, 2 or 3 and what they filed is a
    change in who owns the company, which is ``ownership_change``.  Collapsing
    the two would lose the distinction the owner asked for by name.
    """

    events: list[dict[str, Any]] = []
    for row in wire["rows"]:
        occurred = row.get("event_date")
        if not occurred:
            continue
        key = content_hash({"row": row["record_hash"],
                            "serial": row["form_serial_number"]})
        common = {
            "invocation_ref": invocation_ref,
            "artifact_hash": artifact_hash,
            "event_key": key,
            "notes_text_hash": row.get("notes_text_hash"),
        }
        if row.get("is_director_notice"):
            kind = "insider_transaction"
            payload = {
                "accession": row["form_serial_number"],
                "form": row.get("form_type"),
                "owner_name": row.get("person_name"),
                # Hong Kong has no CIK and the DI database publishes no
                # person identifier at all; the serial number names the
                # notice, not the person.
                "owner_cik": None,
                "role": DIRECTOR_ROLE,
                "transaction_code": row.get("reason_code"),
                "transaction_meaning": row.get("reason_meaning"),
                "transaction_date": row.get("event_date"),
                "security_title": row.get("class_of_shares"),
                "shares": row.get("shares_involved"),
                "price_per_share": row.get("average_price"),
                "acquired_disposed": row.get("acquired_disposed"),
                "shares_owned_following": row.get("shares_after"),
                "direct_or_indirect": row.get("direct_or_indirect"),
                "issuer_name": wire.get("corporation_name"),
                **common,
            }
        else:
            kind = "ownership_change"
            payload = {
                "accession": row["form_serial_number"],
                "form": row.get("form_type"),
                "is_amendment": False,
                "amendment_no": None,
                "reporting_person": row.get("person_name"),
                "person_cik": None,
                "person_type": SHAREHOLDER_ROLE,
                "percent_of_class": row.get("pct_after"),
                "aggregate_shares": row.get("shares_after"),
                # Part XV discloses an interest in shares, not a voting-power
                # split; leaving these blank says so.
                "sole_voting_power": None,
                "shared_voting_power": None,
                "event_date": row.get("event_date"),
                "security_class": row.get("class_of_shares"),
                "cusip": None,
                "purpose_text_hash": None,
                **common,
            }
        events.append({
            "kind": kind,
            "company_ref": company_ref,
            "occurred_at": f"{occurred}T00:00:00+00:00",
            "payload": payload,
            "context": di_context(row, prior_rows=prior_rows),
        })
    return events


# -- the capture ------------------------------------------------------------


def _document(capture: Mapping[str, Any], role: str) -> Mapping[str, Any]:
    documents = capture.get("documents")
    if not isinstance(documents, list):
        raise HkexFilingsParseError("the capture holds no documents")
    for document in documents:
        if isinstance(document, Mapping) and document.get("role") == role:
            return document
    raise HkexFilingsParseError(f"the capture holds no {role!r} document")


PARSERS = {
    NEXT_DAY_DISCLOSURE_OPERATION: parse_next_day_disclosure,
    MONTHLY_RETURNS_OPERATION: parse_monthly_returns,
    DISCLOSURE_OF_INTERESTS_OPERATION: parse_disclosure_of_interests,
    ANNOUNCEMENTS_INDEX_OPERATION: parse_announcements_index,
}


def parse_capture(
    operation: str, capture: Mapping[str, Any], *, ticker: str, artifact_hash: str,
    source_record_refs: Sequence[str] = (),
) -> dict[str, Any]:
    if operation not in PARSERS:
        raise HkexFilingsError(f"{operation!r} is not an hkex-filings operation")
    return PARSERS[operation](
        capture, ticker=ticker, artifact_hash=artifact_hash,
        source_record_refs=list(source_record_refs),
    )


__all__ = [
    "BUYBACK_COLUMNS",
    "BUYBACK_EVENT_KIND",
    "BUYBACK_FORM",
    "DIRECTOR_ROLE",
    "MARKET",
    "SHAREHOLDER_ROLE",
    "UNAVAILABLE",
    "average_price_paid",
    "buyback_cluster_key",
    "buyback_context",
    "buyback_events",
    "buyback_pace",
    "buyback_reference",
    "di_context",
    "di_events",
    "positions",
    "DIRECTOR_FORMS",
    "MAX_BUYBACK_ROWS",
    "MAX_DI_NOTICES",
    "MAX_INDEX_ROWS",
    "MAX_NOTES_CHARS",
    "PACE_WINDOW_DAYS",
    "PARSERS",
    "TRAILING_WINDOW_DAYS",
    "WIRE_SCHEMA_VERSION",
    "HkexFilingsParseError",
    "buyback_report_printed_on",
    "buyback_rows",
    "di_corporation_from_list",
    "di_form_detail",
    "di_notes_text",
    "di_notice_rows",
    "month_ended",
    "parse_announcements_index",
    "parse_capture",
    "parse_disclosure_of_interests",
    "parse_monthly_returns",
    "parse_next_day_disclosure",
    "stock_id_from_prefix",
    "title_search_payload",
]
