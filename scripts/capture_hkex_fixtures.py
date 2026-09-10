#!/usr/bin/env python3
"""Capture the HKEXnews / DI fixtures this repository tests against, once.

Run by hand, never by a test and never by the writer.  Every byte in
``tests/fixtures/hkex-filings`` came out of one invocation of this script on
2026-09-10, and the exact URLs it reaches are printed with each capture so that
a reader can fetch the same document and compare.

    PYTHONPATH=$PWD/src .venv/bin/python scripts/capture_hkex_fixtures.py \
        --ticker 00700 --as-of 2026-09-09 --since 2026-08-01 --until 2026-09-10

**Why a capture and not the served bytes.**  Three of the four operations are
served as text and could be replayed byte for byte; the fourth is a BIFF8
workbook that only ``xlrd`` can read.  Replaying that one from its bytes would
put an optional third-party library in the path of every offline test, so the
capture holds the *canonical grid* -- every cell as the text the sheet
contained, in row and column order -- together with the SHA-256 and byte length
of the workbook it came from.  The tests then need nothing installed, and the
one test that does open the workbook is skipped when ``xlrd`` is absent.

The capture is what the child hashes into an artifact, so it is canonical: the
two clock fields are recorded but excluded from that hash by the child (see
``hkex_filings_cli.CLOCK_FIELDS``), because when a document was read is not
part of what it said.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dalton_core.hkex_filings_core import (  # noqa: E402
    ANNOUNCEMENTS_INDEX_OPERATION,
    CAPTURE_SCHEMA_VERSION,
    DISCLOSURE_OF_INTERESTS_OPERATION,
    MONTHLY_RETURNS_OPERATION,
    NEXT_DAY_DISCLOSURE_OPERATION,
    TIER_ONE_ALL,
    TIER_ONE_MONTHLY_RETURNS,
    USER_AGENT,
    di_all_form_list_url,
    di_corp_list_url,
    di_form_detail_url,
    share_buyback_report_url,
    stock_prefix_url,
    title_search_url,
)

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "hkex-filings"
PAUSE_SECONDS = 2.0


def get(url: str) -> tuple[bytes, str]:
    print(f"  GET {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        raw = response.read(8 * 1024 * 1024)
        content_type = response.headers.get("Content-Type", "")
    time.sleep(PAUSE_SECONDS)
    return raw, content_type


def document(url: str, role: str, raw: bytes, content_type: str, **extra: object) -> dict:
    return {
        "role": role,
        "url": url,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_length": len(raw),
        "content_type": content_type.split(";")[0].strip() or "application/octet-stream",
        **extra,
    }


def grid_from_workbook(raw: bytes) -> list[list[str]]:
    """Every cell of the one sheet, as the text it holds.

    The share buy-back report is a sheet of strings -- the exchange formats the
    numbers before writing them -- so nothing here has to decide how to render
    a float, and a cell that is empty stays an empty string rather than
    becoming a zero.
    """

    import xlrd

    book = xlrd.open_workbook(file_contents=raw)
    if book.nsheets != 1:
        raise SystemExit(f"the buy-back report has {book.nsheets} sheets, not one")
    sheet = book.sheet_by_index(0)
    rows: list[list[str]] = []
    for index in range(sheet.nrows):
        row = []
        for cell in sheet.row(index):
            if cell.ctype == xlrd.XL_CELL_TEXT:
                row.append(str(cell.value))
            elif cell.ctype == xlrd.XL_CELL_EMPTY or cell.ctype == xlrd.XL_CELL_BLANK:
                row.append("")
            else:
                raise SystemExit(
                    f"row {index} holds a {cell.ctype} cell; this report has always "
                    "been text and a number here would need a rendering decision"
                )
        rows.append(row)
    return rows


def write(name: str, payload: dict) -> None:
    path = FIXTURES / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"  wrote {path.relative_to(Path(__file__).resolve().parents[1])}")


def envelope(operation: str, parameters: dict) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "operation": operation,
        "parameters": parameters,
        "captured_at": now.isoformat(timespec="microseconds"),
        "observed_on": now.date().isoformat(),
        "documents": [],
    }


def capture_buyback(ticker: str, as_of: str) -> None:
    print("next_day_disclosure_returns")
    url = share_buyback_report_url(as_of)
    raw, content_type = get(url)
    capture = envelope(NEXT_DAY_DISCLOSURE_OPERATION,
                       {"hk_ticker": ticker, "as_of": as_of})
    capture["documents"].append(
        document(url, "share_buyback_report", raw, content_type,
                 grid=grid_from_workbook(raw))
    )
    write(f"next-day-disclosure-{ticker}-{as_of.replace('-', '')}.json", capture)
    (FIXTURES / f"share-buyback-report-{as_of.replace('-', '')}.xls").write_bytes(raw)
    print(f"  wrote the workbook itself ({len(raw)} bytes)")


def capture_stock_id(ticker: str) -> tuple[int, dict]:
    url = stock_prefix_url(ticker)
    raw, content_type = get(url)
    text = raw.decode("utf-8", "replace")
    payload = json.loads(text[text.index("(") + 1: text.rindex(")")])
    stock_id = int(payload["stockInfo"][0]["stockId"])
    return stock_id, document(url, "stock_prefix", raw, content_type, text=text)


def capture_title_search(
    operation: str, ticker: str, since: str, until: str, tier_one: str, name: str
) -> None:
    print(operation)
    stock_id, prefix_doc = capture_stock_id(ticker)
    url = title_search_url(stock_id=stock_id, since=since, until=until, tier_one=tier_one)
    raw, content_type = get(url)
    capture = envelope(operation, {"hk_ticker": ticker, "since": since,
                                   "until": until, "tier_one": tier_one})
    capture["documents"].append(prefix_doc)
    capture["documents"].append(
        document(url, "title_search", raw, content_type,
                 text=raw.decode("utf-8", "replace"))
    )
    write(name, capture)


def capture_disclosure_of_interests(
    ticker: str, since: str, until: str, max_forms: int
) -> None:
    print(DISCLOSURE_OF_INTERESTS_OPERATION)
    from dalton_core.hkex_filings_adapter import (
        di_corporation_from_list,
        di_notice_rows,
    )

    corp_url = di_corp_list_url(ticker=ticker, since=since, until=until)
    corp_raw, corp_type = get(corp_url)
    corp_text = corp_raw.decode("utf-8", "replace")
    corporation = di_corporation_from_list(corp_text, ticker=ticker)

    list_url = di_all_form_list_url(
        sid=corporation["sid"], corporation_name=corporation["corporation_name"],
        ticker=ticker, since=since, until=until,
    )
    list_raw, list_type = get(list_url)
    list_text = list_raw.decode("utf-8", "replace")

    capture = envelope(DISCLOSURE_OF_INTERESTS_OPERATION,
                       {"hk_ticker": ticker, "since": since, "until": until})
    capture["documents"].append(document(corp_url, "di_corp_list", corp_raw, corp_type,
                                         text=corp_text))
    capture["documents"].append(document(list_url, "di_form_list", list_raw, list_type,
                                         text=list_text))
    for row in di_notice_rows(list_text)[:max_forms]:
        detail_url = di_form_detail_url(
            form_path=row["form_path"], sid=corporation["sid"],
            corporation_name=corporation["corporation_name"],
            ticker=ticker, since=since, until=until,
        )
        detail_raw, detail_type = get(detail_url)
        capture["documents"].append(document(
            detail_url, f"di_form:{row['form_serial_number']}", detail_raw, detail_type,
            text=detail_raw.decode("utf-8", "replace"),
        ))
    write(f"disclosure-of-interests-{ticker}.json", capture)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ticker", default="00700")
    parser.add_argument("--as-of", default="2026-09-09",
                        help="the day the share buy-back report was printed")
    parser.add_argument("--since", default="2026-08-01")
    parser.add_argument("--until", default="2026-09-10")
    parser.add_argument("--max-forms", type=int, default=3)
    parser.add_argument("--only", default=None, help="one operation, for a re-capture")
    args = parser.parse_args(list(argv) if argv is not None else None)

    wanted = args.only
    if wanted in (None, NEXT_DAY_DISCLOSURE_OPERATION):
        capture_buyback(args.ticker, args.as_of)
    if wanted in (None, MONTHLY_RETURNS_OPERATION):
        capture_title_search(
            MONTHLY_RETURNS_OPERATION, args.ticker, args.since, args.until,
            TIER_ONE_MONTHLY_RETURNS, f"monthly-returns-{args.ticker}.json",
        )
    if wanted in (None, ANNOUNCEMENTS_INDEX_OPERATION):
        capture_title_search(
            ANNOUNCEMENTS_INDEX_OPERATION, args.ticker, args.since, args.until,
            TIER_ONE_ALL, f"announcements-index-{args.ticker}.json",
        )
    if wanted in (None, DISCLOSURE_OF_INTERESTS_OPERATION):
        capture_disclosure_of_interests(
            args.ticker, args.since, args.until, args.max_forms
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
