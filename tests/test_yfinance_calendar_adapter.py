"""C1: Yahoo's diary, normalised -- and the three things it must not become."""

from __future__ import annotations

import json
import sys
import types
import unittest
from datetime import date
from pathlib import Path

from dalton_core.authority_resolver import _schema_matches
from dalton_core.market_price_adapter import MarketDataAdapterError
from dalton_core.yfinance_calendar_adapter import (
    MAX_EARNINGS_DATES,
    calendar_entries,
    calendar_wire,
    fetch_calendar,
    quarter_subject,
)
from dalton_core.yfinance_core import CALENDAR_OPERATION, yfinance_output_schema

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "market" / "acn-calendar.json"
INVOCATION = "connector-invocation:yfinance:aaaa"


def raw(**overrides):
    body = {
        "schema_version": "0.1", "library": "yfinance", "library_version": "1.7.0",
        "operation": "calendar", "ticker": "ACN", "observed_on": "2026-09-09",
        "captured_at": "2026-09-09T15:00:00+00:00", "errors": {},
        "calendar": {
            "Dividend Date": "2026-11-13",
            "Ex-Dividend Date": "2026-10-08",
            "Earnings Date": ["2026-10-01"],
            "Earnings Average": 3.17886,
        },
    }
    body.update(overrides)
    return body


def wire_of(body):
    return calendar_wire(body, source_record_refs=["raw-sink:" + "0" * 64])


class WireTests(unittest.TestCase):
    def test_the_wire_satisfies_the_frozen_contract(self):
        wire = wire_of(raw())
        _schema_matches(wire, yfinance_output_schema(CALENDAR_OPERATION), "output")
        self.assertEqual(wire["earnings_dates"], ["2026-10-01"])
        self.assertEqual(wire["ex_dividend_date"], "2026-10-08")

    def test_the_real_accenture_capture_satisfies_it_too(self):
        wire = wire_of(json.loads(FIXTURE.read_text(encoding="utf-8")))
        _schema_matches(wire, yfinance_output_schema(CALENDAR_OPERATION), "output")
        self.assertEqual(wire["ticker"], "ACN")
        self.assertEqual(wire["earnings_dates"], ["2026-10-01"])

    def test_no_consensus_figure_reaches_the_wire(self):
        # Yahoo serves the EPS and revenue estimate in the same block.
        # `analyst_estimates` already carries both, and two operations claiming
        # one number is how the two of them come to disagree.
        wire = wire_of(raw())
        self.assertNotIn("earnings_average", wire)
        for value in wire.values():
            self.assertNotIn("3.17886", str(value))

    def test_a_company_yahoo_knows_nothing_about_is_an_answer_not_a_failure(self):
        wire = wire_of(raw(calendar=None))
        self.assertEqual(wire["earnings_dates"], [])
        self.assertIsNone(wire["ex_dividend_date"])
        _schema_matches(wire, yfinance_output_schema(CALENDAR_OPERATION), "output")

    def test_a_window_yahoo_could_not_narrow_keeps_both_ends(self):
        wire = wire_of(raw(calendar={
            "Earnings Date": ["2026-10-01", "2026-10-05"]}))
        self.assertEqual(wire["earnings_dates"], ["2026-10-01", "2026-10-05"])

    def test_more_dates_than_the_contract_describes_are_refused(self):
        with self.assertRaises(MarketDataAdapterError):
            wire_of(raw(calendar={
                "Earnings Date": ["2026-10-01", "2026-10-05", "2026-10-09"]}))
        self.assertEqual(MAX_EARNINGS_DATES, 2)

    def test_something_that_is_not_a_date_is_refused_rather_than_coerced(self):
        with self.assertRaises(MarketDataAdapterError):
            wire_of(raw(calendar={"Earnings Date": ["next quarter"]}))

    def test_a_capture_with_no_ticker_is_refused(self):
        with self.assertRaises(MarketDataAdapterError):
            wire_of(raw(ticker=""))


class EntryTests(unittest.TestCase):
    def test_every_yahoo_date_is_estimated(self):
        entries = calendar_entries(wire_of(raw()), invocation_ref=INVOCATION)
        for entry in entries:
            for source in entry["sources"]:
                self.assertEqual(source["confidence"], "estimated")
                self.assertEqual(source["kind"], "connector_invocation")
                self.assertEqual(source["ref"], INVOCATION)

    def test_a_forthcoming_ex_dividend_is_on_the_calendar(self):
        entries = calendar_entries(wire_of(raw()), invocation_ref=INVOCATION)
        kinds = {entry["event_kind"] for entry in entries}
        self.assertEqual(kinds, {"earnings", "ex_dividend"})

    def test_a_past_ex_dividend_is_not_a_forthcoming_event(self):
        # Live: DXC still reports an ex-date of 2020-03-23, six years after it
        # stopped paying, and Accenture's was two months ago. Putting those on
        # a calendar of expected events would invent a future out of a past.
        entries = calendar_entries(
            wire_of(raw(calendar={
                "Earnings Date": ["2026-10-01"],
                "Ex-Dividend Date": "2020-03-23",
            })),
            invocation_ref=INVOCATION,
        )
        self.assertEqual([entry["event_kind"] for entry in entries], ["earnings"])

    def test_a_window_becomes_two_entries_that_say_they_are_a_window(self):
        entries = calendar_entries(
            wire_of(raw(calendar={"Earnings Date": ["2026-09-30", "2026-10-01"]})),
            invocation_ref=INVOCATION,
        )
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertIn("window", entry["notes"])
        # Different quarters, so different occurrences: two visible entries
        # rather than one silently merged.
        self.assertEqual({entry["subject"] for entry in entries},
                         {"2026q3", "2026q4"})

    def test_the_occurrence_key_is_the_calendar_quarter_of_the_date(self):
        self.assertEqual(quarter_subject("2026-10-01"), "2026q4")
        self.assertEqual(quarter_subject("2026-09-30"), "2026q3")
        self.assertEqual(quarter_subject("2026-01-01"), "2026q1")


class FetchTests(unittest.TestCase):
    """The one call, against a stub. The suite never reaches Yahoo."""

    def stub(self, calendar, *, raises=None):
        module = types.ModuleType("yfinance")
        module.__version__ = "1.7.0"

        class Handle:
            def __init__(self, symbol):
                self.symbol = symbol

            @property
            def calendar(self):
                if raises is not None:
                    raise raises
                return calendar

        module.Ticker = Handle
        self.addCleanup(sys.modules.pop, "yfinance", None)
        original = sys.modules.get("yfinance")
        if original is not None:
            self.addCleanup(sys.modules.__setitem__, "yfinance", original)
        sys.modules["yfinance"] = module

    def test_the_library_output_is_kept_whole_and_json_safe(self):
        self.stub({
            "Earnings Date": [date(2026, 10, 1)],
            "Ex-Dividend Date": date(2026, 10, 8),
            "Earnings Average": 3.17886,
        })
        body = fetch_calendar("ACN")
        self.assertEqual(body["operation"], "calendar")
        self.assertEqual(body["ticker"], "ACN")
        # Dates arrive as `datetime.date` and have to survive canonical JSON.
        self.assertEqual(body["calendar"]["Earnings Date"], ["2026-10-01"])
        self.assertEqual(body["errors"], {})
        json.dumps(body)

    def test_a_source_that_refuses_is_a_reported_absence_not_a_crash(self):
        self.stub(None, raises=RuntimeError("Yahoo said no"))
        body = fetch_calendar("ACN")
        self.assertIsNone(body["calendar"])
        self.assertIn("Yahoo said no", body["errors"]["calendar"])
        # And it still normalises, to a calendar with nothing in it.
        wire = wire_of(body)
        self.assertEqual(wire["earnings_dates"], [])


if __name__ == "__main__":
    unittest.main()
