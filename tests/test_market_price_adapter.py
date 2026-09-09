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
    PRICE_COLUMNS,
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

    def test_the_capture_records_when_the_source_was_read(self):
        # A window that includes today returns the last trade so far in the
        # same shape as a settled close; this is the only thing that can tell
        # a later reader which one it is holding.
        self.assertRegex(
            self.wire["captured_at"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertEqual(self.wire["captured_at"], self.raw["captured_at"])

    def test_a_capture_that_recorded_no_time_reads_as_provisional(self):
        # The safe direction: midnight is before any settlement hour, so an
        # undated capture is re-requested rather than frozen.
        raw = prices_fixture()
        del raw["captured_at"]
        wire = daily_prices_wire(raw, source_record_refs=[SINK])
        self.assertEqual(wire["captured_at"], f"{raw['observed_on']}T00:00:00+00:00")

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

    def test_a_day_with_a_hole_in_it_is_dropped_and_counted(self):
        raw = prices_fixture()
        raw["rows"] = copy.deepcopy(raw["rows"])
        raw["rows"][0]["Close"] = None
        wire = daily_prices_wire(raw, source_record_refs=[SINK])
        self.assertEqual(len(wire["bars"]), len(raw["rows"]) - 1)
        self.assertNotIn("2026-08-25", {bar["date"] for bar in wire["bars"]})
        # Counted, not silently swallowed: a frame that arrives entirely as
        # NaN must not be indistinguishable from a genuinely quiet window.
        self.assertEqual(wire["dropped_row_count"], 1)

    def test_a_clean_window_drops_nothing(self):
        self.assertEqual(
            daily_prices_wire(prices_fixture(),
                              source_record_refs=[SINK])["dropped_row_count"], 0)

    def test_an_all_nan_frame_produces_no_bars_and_a_full_drop_count(self):
        raw = prices_fixture()
        raw["rows"] = [{"date": row["date"], **{name: None for name in PRICE_COLUMNS}}
                       for row in raw["rows"]]
        wire = daily_prices_wire(raw, source_record_refs=[SINK])
        self.assertEqual(wire["bars"], [])
        self.assertEqual(wire["dropped_row_count"], 10)

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

    def test_an_absent_rating_count_stays_absent_rather_than_becoming_zero(self):
        # "No analyst rates it a sell" and "Yahoo did not say how many rate it
        # a sell" are different facts, and zero can only express one of them.
        raw = estimates_fixture()
        raw["recommendations"] = [
            {**raw["recommendations"][0], "strongSell": None}]
        wire = analyst_estimates_wire(raw, source_record_refs=[SINK])
        self.assertIsNone(wire["recommendations"][0]["strong_sell"])
        self.assertIsNotNone(wire["recommendations"][0]["hold"])
        _schema_matches(
            wire, yfinance_output_schema(ANALYST_ESTIMATES_OPERATION), "output")

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


class FetchTests(unittest.TestCase):
    """The two calls themselves, against a stubbed library.

    Everything above tests the normaliser on a captured payload, which leaves
    the part that talks to the library untested -- and that part is where the
    MultiIndex and the timezone live. So a stub stands in for ``yfinance`` with
    a frame of the shape the real one returns: two-level columns keyed by
    (field, ticker), and a tz-aware index in the exchange's own timezone, which
    is what makes "which day is this bar" a question with a wrong answer.
    """

    @classmethod
    def setUpClass(cls):
        try:
            import pandas  # noqa: F401
        except ImportError:  # pragma: no cover - depends on the extra
            raise unittest.SkipTest(
                "the market-data extra is not installed in this environment")

    def frame(self):
        import pandas

        index = pandas.DatetimeIndex(
            ["2026-08-25 00:00:00", "2026-08-26 00:00:00"], name="Date",
        ).tz_localize("America/New_York")
        columns = pandas.MultiIndex.from_product(
            [["Adj Close", "Close", "High", "Low", "Open", "Volume"], ["ACN"]],
            names=["Price", "Ticker"],
        )
        return pandas.DataFrame(
            [
                [186.93, 186.93, 188.0, 182.33, 183.78, 4071800],
                [181.38, 181.38, 185.84, 180.79, 182.35, 4376500],
            ],
            index=index, columns=columns,
        )

    def stub(self, *, frame=None, info=None, raises=None):
        import sys
        import types

        module = types.ModuleType("yfinance")
        module.__version__ = "1.7.0"
        calls: dict[str, object] = {}

        def download(ticker, **kwargs):
            calls["download"] = {"ticker": ticker, **kwargs}
            return self.frame() if frame is None else frame

        class Ticker:
            def __init__(self, symbol):
                calls["ticker"] = symbol

            @property
            def info(self):
                if raises is not None:
                    raise raises
                return {
                    "currency": "USD", "sharesOutstanding": 611942109,
                    "marketCap": 108000000000, "numberOfAnalystOpinions": 25,
                } if info is None else info

            analyst_price_targets = {"current": 177.03, "mean": 184.19}
            recommendations = None
            earnings_estimate = None
            revenue_estimate = None

        module.download = download
        module.Ticker = Ticker
        original = sys.modules.get("yfinance")
        sys.modules["yfinance"] = module

        def restore():
            if original is None:
                sys.modules.pop("yfinance", None)
            else:
                sys.modules["yfinance"] = original

        self.addCleanup(restore)
        return calls

    def test_the_download_is_never_auto_adjusted(self):
        from dalton_core.market_price_adapter import fetch_daily_prices

        calls = self.stub()
        fetch_daily_prices("ACN", start="2026-08-25", end="2026-08-27")
        # The single most important argument in this module.
        self.assertIs(calls["download"]["auto_adjust"], False)
        self.assertEqual(calls["download"]["start"], "2026-08-25")
        self.assertEqual(calls["download"]["end"], "2026-08-27")

    def test_the_multiindex_is_flattened_to_field_names(self):
        from dalton_core.market_price_adapter import fetch_daily_prices

        self.stub()
        raw = fetch_daily_prices("ACN", start="2026-08-25", end="2026-08-27")
        self.assertEqual(
            raw["columns"],
            ["Adj Close", "Close", "High", "Low", "Open", "Volume"])
        for row in raw["rows"]:
            # Not ("Close", "ACN"), which is a KeyError to every reader and a
            # shape that changes the moment two tickers are asked for.
            self.assertIn("Close", row)
            self.assertNotIn(("Close", "ACN"), row)

    def test_the_exchange_day_survives_the_timezone(self):
        from dalton_core.market_price_adapter import fetch_daily_prices

        self.stub()
        raw = fetch_daily_prices("ACN", start="2026-08-25", end="2026-08-27")
        # New York midnight is the previous day in UTC. The bar's date is the
        # exchange's, not a UTC conversion of it.
        self.assertEqual([row["date"] for row in raw["rows"]],
                         ["2026-08-25", "2026-08-26"])

    def test_the_whole_call_normalises_end_to_end(self):
        from dalton_core.market_price_adapter import fetch_daily_prices

        self.stub()
        raw = fetch_daily_prices("ACN", start="2026-08-25", end="2026-08-27")
        wire = daily_prices_wire(raw, source_record_refs=[SINK])
        _schema_matches(
            wire, yfinance_output_schema(DAILY_PRICES_OPERATION), "output")
        self.assertEqual(wire["bars"][0]["close"], "186.93")
        self.assertEqual(wire["dropped_row_count"], 0)
        self.assertEqual(
            {item["observation"] for item in wire["observations"]},
            {"shares_outstanding", "market_cap"})

    def test_metadata_that_fails_does_not_cost_the_bars(self):
        from dalton_core.market_price_adapter import fetch_daily_prices

        self.stub(raises=RuntimeError("Yahoo said no"))
        raw = fetch_daily_prices("ACN", start="2026-08-25", end="2026-08-27")
        self.assertEqual(len(raw["rows"]), 2)
        self.assertIn("info_error", raw["metadata"])
        # But without a currency the wire refuses, rather than guessing one.
        with self.assertRaises(MarketDataAdapterError):
            daily_prices_wire(raw, source_record_refs=[SINK])

    def test_an_estimates_block_that_raises_is_recorded_not_fatal(self):
        from dalton_core.market_price_adapter import fetch_analyst_estimates

        self.stub()
        raw = fetch_analyst_estimates("ACN")
        self.assertEqual(raw["ticker"], "ACN")
        self.assertIsNone(raw["recommendations"])
        self.assertEqual(raw["price_target"], {"current": 177.03, "mean": 184.19})
        wire = analyst_estimates_wire(raw, source_record_refs=[SINK])
        _schema_matches(
            wire, yfinance_output_schema(ANALYST_ESTIMATES_OPERATION), "output")
        self.assertEqual(wire["price_target"]["current"], "177.03")
        self.assertIsNone(wire["price_target"]["high"])

    def test_a_core_without_the_library_refuses_with_a_reason(self):
        import sys

        original = sys.modules.get("yfinance")
        sys.modules["yfinance"] = None  # import yfinance -> ImportError
        try:
            from dalton_core.market_price_adapter import fetch_daily_prices

            with self.assertRaises(MarketDataAdapterError) as caught:
                fetch_daily_prices("ACN", start="2026-08-25", end="2026-08-27")
            self.assertIn("market-data", str(caught.exception))
        finally:
            if original is None:
                sys.modules.pop("yfinance", None)
            else:
                sys.modules["yfinance"] = original


if __name__ == "__main__":
    unittest.main()
