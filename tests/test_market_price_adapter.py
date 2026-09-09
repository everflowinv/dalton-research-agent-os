"""P11a: the yfinance normaliser, against one captured Accenture call.

The fixture is a real ``yf.download`` for ACN over ten trading days in
September 2026, taken once with the network and replayed here. Everything below
runs offline.

Three of these assertions are the owner's own lessons from ``stock-move-analyzer``
and its siblings, written down as tests so the next person cannot undo them by
accident: no adjusted prices masquerading as closes, both columns always, and a
flattened MultiIndex.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from dalton_core.authority_resolver import _schema_matches
from dalton_core.market_price_adapter import (
    MarketDataAdapterError,
    _float_text,
    analyst_estimates_wire,
    daily_prices_wire,
    json_safe,
)
from dalton_core.yfinance_core import (
    ANALYST_ESTIMATES_OPERATION,
    DAILY_PRICES_OPERATION,
    yfinance_output_schema,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "market"
SINK = "raw-sink:" + "0" * 64


def prices_fixture(**overrides):
    raw = json.loads((FIXTURES / "acn-daily-prices.json").read_text(encoding="utf-8"))
    raw.update(overrides)
    return raw


def estimates_fixture(**overrides):
    raw = json.loads(
        (FIXTURES / "acn-analyst-estimates.json").read_text(encoding="utf-8"))
    raw.update(overrides)
    return raw


class CapturedCallTests(unittest.TestCase):
    def setUp(self):
        self.raw = prices_fixture()
        self.wire = daily_prices_wire(self.raw, source_record_refs=[SINK])

    def test_the_capture_is_a_real_unadjusted_accenture_call(self):
        self.assertEqual(self.raw["ticker"], "ACN")
        self.assertIs(self.raw["auto_adjust"], False)
        self.assertEqual(self.raw["library"], "yfinance")
        self.assertEqual(len(self.raw["rows"]), 10)

    def test_the_multiindex_is_flattened_to_field_names(self):
        # ``yf.download`` keys columns by (field, ticker) even for one ticker,
        # so ``row["Close"]`` is a KeyError and ``row[("Close", "ACN")]`` is a
        # shape that changes the moment two tickers are asked for.
        self.assertEqual(
            sorted(self.raw["columns"]),
            ["Adj Close", "Close", "High", "Low", "Open", "Volume"],
        )
        for row in self.raw["rows"]:
            self.assertIn("Close", row)

    def test_close_and_adj_close_are_separate_columns(self):
        for bar in self.wire["bars"]:
            self.assertIn("close", bar)
            self.assertIn("adj_close", bar)

    def test_the_wire_satisfies_the_frozen_contract(self):
        _schema_matches(
            self.wire, yfinance_output_schema(DAILY_PRICES_OPERATION), "output")

    def test_the_bars_are_the_prices_that_printed(self):
        # Yahoo widens single-precision numbers to doubles, so 183.78 arrives
        # as 183.77999877929688. Storing that would put a number no exchange
        # ever printed into every citation.
        first = self.wire["bars"][0]
        self.assertEqual(first["date"], "2026-08-25")
        self.assertEqual(first["open"], "183.78")
        self.assertEqual(first["close"], "186.93")
        self.assertEqual(first["adj_close"], "186.93")
        self.assertEqual(first["volume"], "4071800")

    def test_the_bars_come_back_in_date_order(self):
        dates = [bar["date"] for bar in self.wire["bars"]]
        self.assertEqual(dates, sorted(dates))

    def test_share_count_and_market_cap_are_dated_by_the_reading(self):
        observations = {
            item["observation"]: item for item in self.wire["observations"]
        }
        self.assertEqual(set(observations), {"shares_outstanding", "market_cap"})
        for item in observations.values():
            # Not a bar date: Yahoo reports the latest it knows with no history
            # behind it, so the honest date is the date of the reading.
            self.assertEqual(item["as_of"], self.raw["observed_on"])
        self.assertEqual(observations["shares_outstanding"]["unit"], "shares")
        self.assertEqual(observations["market_cap"]["unit"], "USD")


class RefusalTests(unittest.TestCase):
    def test_an_adjusted_capture_cannot_be_stored_as_a_close(self):
        with self.assertRaises(MarketDataAdapterError) as caught:
            daily_prices_wire(prices_fixture(auto_adjust=True),
                              source_record_refs=[SINK])
        self.assertIn("adjusted", str(caught.exception))

    def test_a_call_without_a_currency_is_not_a_price(self):
        raw = prices_fixture()
        raw["metadata"] = {**raw["metadata"], "currency": None}
        with self.assertRaises(MarketDataAdapterError):
            daily_prices_wire(raw, source_record_refs=[SINK])

    def test_a_call_without_an_observation_date_is_refused(self):
        with self.assertRaises(MarketDataAdapterError):
            daily_prices_wire(prices_fixture(observed_on=None),
                              source_record_refs=[SINK])

    def test_a_day_with_a_hole_in_it_is_dropped_rather_than_filled(self):
        raw = prices_fixture()
        raw["rows"] = copy.deepcopy(raw["rows"])
        raw["rows"][0]["Close"] = None
        wire = daily_prices_wire(raw, source_record_refs=[SINK])
        self.assertEqual(len(wire["bars"]), len(raw["rows"]) - 1)
        self.assertNotIn("2026-08-25", {bar["date"] for bar in wire["bars"]})

    def test_an_absent_share_count_is_absent_rather_than_zero(self):
        raw = prices_fixture()
        raw["metadata"] = {**raw["metadata"], "sharesOutstanding": None}
        wire = daily_prices_wire(raw, source_record_refs=[SINK])
        self.assertEqual(
            [item["observation"] for item in wire["observations"]], ["market_cap"])


class EstimateTests(unittest.TestCase):
    def setUp(self):
        self.raw = estimates_fixture()
        self.wire = analyst_estimates_wire(self.raw, source_record_refs=[SINK])

    def test_the_wire_satisfies_the_frozen_contract(self):
        _schema_matches(
            self.wire, yfinance_output_schema(ANALYST_ESTIMATES_OPERATION), "output")

    def test_it_carries_the_target_the_ratings_and_both_estimate_tables(self):
        self.assertEqual(self.wire["ticker"], "ACN")
        self.assertIsNotNone(self.wire["price_target"]["mean"])
        self.assertIsNotNone(self.wire["price_target"]["number_of_analysts"])
        self.assertTrue(self.wire["recommendations"])
        periods = {row["period"] for row in self.wire["eps_estimates"]}
        self.assertTrue({"0q", "+1q", "0y", "+1y"}.issubset(periods))
        self.assertTrue(self.wire["revenue_estimates"])

    def test_a_block_yahoo_dropped_comes_back_empty_not_invented(self):
        wire = analyst_estimates_wire(
            estimates_fixture(price_target=None, revenue_estimate=None),
            source_record_refs=[SINK],
        )
        self.assertIsNone(wire["price_target"]["mean"])
        self.assertEqual(wire["revenue_estimates"], [])
        # And the blocks that survived are untouched.
        self.assertTrue(wire["eps_estimates"])
        _schema_matches(
            wire, yfinance_output_schema(ANALYST_ESTIMATES_OPERATION), "output")


class NumberTests(unittest.TestCase):
    def test_a_double_that_is_exactly_a_float32_is_shortened(self):
        self.assertEqual(_float_text(183.77999877929688), "183.78")
        self.assertEqual(_float_text(186.92999267578125), "186.93")

    def test_a_double_that_is_not_a_float32_is_left_alone(self):
        # Genuine double precision is not rounded away by a rule meant for
        # Yahoo's widening.
        self.assertEqual(_float_text(0.1234567890123), "0.1234567890123")

    def test_nan_becomes_null_rather_than_a_number(self):
        self.assertIsNone(json_safe(float("nan")))
        self.assertIsNone(json_safe(float("inf")))


if __name__ == "__main__":
    unittest.main()
