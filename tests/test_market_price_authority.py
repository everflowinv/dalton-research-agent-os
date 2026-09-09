"""P11a: the price history is versioned, not maintained.

A price moves under you. A split restates every bar before it, a dividend moves
the adjusted series, Yahoo corrects a bad print. If a bar could be updated, the
only evidence any of that happened would be gone and last week's valuation
could never be reproduced.

So: append-only, one version per company that has something new in it,
``duplicate`` otherwise, and every bar bound to the exact call that fetched it.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.market_price import (
    MarketPriceConflict,
    MarketPriceNotFound,
    MarketPriceSeriesAuthority,
    MarketPriceValidationError,
    series_ref_for,
)
from dalton_core.store import DaltonStore, canonical_json

ACN = "company:sec-cik:0001467373"
INVOCATION = "connector-invocation:yfinance:" + "a" * 32
ARTIFACT = "1" * 64
GOVERNANCE = "connector-governance:yfinance-daily-prices:v1"
GOVERNANCE_HASH = "2" * 64


def bar(date, close="100", **overrides):
    row = {
        "date": date, "open": "99", "high": "101", "low": "98",
        "close": close, "adj_close": close, "volume": "1000000",
    }
    row.update(overrides)
    return row


class AuthorityTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.authority = MarketPriceSeriesAuthority(self.store)

    def publish(self, bars, *, observations=(), invocation=INVOCATION,
                artifact=ARTIFACT, company_ref=ACN, ticker="ACN",
                currency="USD", start="2026-09-01", end="2026-09-10"):
        return self.authority.publish_series(
            company_ref=company_ref, ticker=ticker, currency=currency,
            bars=bars, observations=observations,
            invocation_ref=invocation, artifact_hash=artifact,
            governance_ref=GOVERNANCE, governance_hash=GOVERNANCE_HASH,
            requested_start=start, requested_end=end,
        )


class VersionChainTests(AuthorityTestCase):
    def test_a_first_publish_is_version_one_and_reads_back(self):
        published = self.publish([bar("2026-09-01"), bar("2026-09-02", "101")])
        self.assertEqual(published["status"], "fresh")
        self.assertEqual(published["version"], 1)
        self.assertIsNone(published["prior_version_ref"])
        self.assertEqual(published["bar_count"], 2)
        self.assertEqual(published["first_bar_date"], "2026-09-01")
        self.assertEqual(published["last_bar_date"], "2026-09-02")
        self.assertEqual(published["series_ref"], series_ref_for(ACN))
        stored = self.authority.version(published["id"])
        self.assertEqual(stored["content_hash"], published["content_hash"])

    def test_every_bar_names_the_call_that_fetched_it(self):
        published = self.publish([bar("2026-09-01")])
        for row in published["bars"]:
            self.assertEqual(row["invocation_ref"], INVOCATION)
            self.assertEqual(row["artifact_hash"], ARTIFACT)

    def test_a_window_with_nothing_new_is_a_duplicate(self):
        first = self.publish([bar("2026-09-01")])
        again = self.publish([bar("2026-09-01")], invocation="connector-invocation:yfinance:" + "b" * 32,
                             artifact="3" * 64)
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(self.authority.versions(ACN)), 1)

    def test_a_new_trading_day_publishes_a_new_version_carrying_the_whole_series(self):
        first = self.publish([bar("2026-09-01")])
        second = self.publish([bar("2026-09-02", "101")])
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["prior_version_ref"], first["id"])
        self.assertEqual(second["added_bar_dates"], ["2026-09-02"])
        # A version carries the full history, so a reader never has to
        # assemble one from a chain.
        self.assertEqual(
            [row["date"] for row in second["bars"]], ["2026-09-01", "2026-09-02"])

    def test_a_restated_bar_is_a_new_version_and_is_named(self):
        # The split case. Nothing is edited; the chain says which day moved,
        # because that is the only evidence the split happened.
        first = self.publish([bar("2026-09-01", "100")])
        second = self.publish([bar(
            "2026-09-01", "50", open="49.5", high="50.5", low="49",
            adj_close="50", volume="2000000")])
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(second["restated_bar_dates"], ["2026-09-01"])
        self.assertEqual(second["added_bar_dates"], [])
        self.assertEqual(second["bars"][0]["close"], "50")
        # And version one still says 100.
        self.assertEqual(
            self.authority.version(first["id"])["bars"][0]["close"], "100")

    def test_a_second_reading_of_the_market_cap_does_not_publish_a_version(self):
        # Market capitalisation moves every second the market is open. A tick
        # that found no new trading day but a market cap $6m lighter is not a
        # new fact about the company.
        self.publish([bar("2026-09-01")], observations=[{
            "observation": "market_cap", "as_of": "2026-09-01",
            "value": "108000000000", "unit": "USD"}])
        again = self.publish([bar("2026-09-01")], observations=[{
            "observation": "market_cap", "as_of": "2026-09-01",
            "value": "107994000000", "unit": "USD"}])
        self.assertEqual(again["status"], "duplicate")

    def test_the_newest_reading_of_a_day_supersedes_the_earlier_one(self):
        self.publish([bar("2026-09-01")], observations=[{
            "observation": "shares_outstanding", "as_of": "2026-09-01",
            "value": "611942109", "unit": "shares"}])
        second = self.publish([bar("2026-09-02", "101")], observations=[{
            "observation": "shares_outstanding", "as_of": "2026-09-01",
            "value": "611000000", "unit": "shares"}])
        rows = [row for row in second["observations"]
                if row["observation"] == "shares_outstanding"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], "611000000")


class ReaderTests(AuthorityTestCase):
    def test_a_company_with_no_history_reads_as_none_rather_than_raising(self):
        self.assertIsNone(self.authority.latest_version(ACN))
        self.assertIsNone(self.authority.series(ACN))
        self.assertIsNone(self.authority.latest_close(ACN))
        self.assertIsNone(self.authority.latest_observation(ACN, "market_cap"))

    def test_the_latest_close_carries_both_closes_and_its_as_of(self):
        self.publish([
            bar("2026-09-01", "100"),
            bar("2026-09-02", "101", adj_close="100.4"),
        ])
        latest = self.authority.latest_close(ACN)
        self.assertEqual(latest["as_of"], "2026-09-02")
        self.assertEqual(latest["close"], "101")
        # Both, always: a reader quoting "the shares closed at" wants one and a
        # total-return reader wants the other, and dividends must never be
        # added on top of the adjusted one.
        self.assertEqual(latest["adj_close"], "100.4")
        self.assertEqual(latest["invocation_ref"], INVOCATION)
        self.assertEqual(latest["artifact_hash"], ARTIFACT)

    def test_the_series_reader_hands_back_the_version_it_came_from(self):
        published = self.publish([bar("2026-09-01")])
        series = self.authority.series(ACN)
        self.assertEqual(series["version_ref"], published["id"])
        self.assertEqual(series["version_hash"], published["content_hash"])
        self.assertEqual(series["source_ref"], "source:yahoo-finance")

    def test_the_newest_share_count_is_the_one_with_the_latest_as_of(self):
        self.publish([bar("2026-09-01")], observations=[{
            "observation": "shares_outstanding", "as_of": "2026-09-01",
            "value": "612000000", "unit": "shares"}])
        self.publish([bar("2026-09-02", "101")], observations=[{
            "observation": "shares_outstanding", "as_of": "2026-09-02",
            "value": "611000000", "unit": "shares"}])
        newest = self.authority.latest_observation(ACN, "shares_outstanding")
        self.assertEqual(newest["as_of"], "2026-09-02")
        self.assertEqual(newest["value"], "611000000")

    def test_an_unknown_version_ref_is_not_found(self):
        with self.assertRaises(MarketPriceNotFound):
            self.authority.version("market-price-series-version:nope")


class RefusalTests(AuthorityTestCase):
    def test_a_float_is_not_a_price(self):
        # A binary float is not the number that printed, and two runs that
        # parsed the same quote can disagree in the last bits -- which would
        # make a restatement out of nothing.
        with self.assertRaises(MarketPriceValidationError):
            self.publish([bar("2026-09-01", close=100.0)])

    def test_a_bar_whose_close_is_outside_its_own_range_is_refused(self):
        with self.assertRaises(MarketPriceConflict):
            self.publish([bar("2026-09-01", close="500")])

    def test_a_bar_whose_low_is_above_its_high_is_refused(self):
        with self.assertRaises(MarketPriceConflict):
            self.publish([bar("2026-09-01", low="200", high="150")])

    def test_the_same_day_twice_in_one_window_is_refused(self):
        with self.assertRaises(MarketPriceValidationError):
            self.publish([bar("2026-09-01"), bar("2026-09-01", "101")])

    def test_a_bar_missing_a_column_is_refused_rather_than_filled(self):
        row = bar("2026-09-01")
        del row["adj_close"]
        with self.assertRaises(MarketPriceValidationError):
            self.publish([row])

    def test_a_series_cannot_change_ticker_or_currency_underneath_itself(self):
        self.publish([bar("2026-09-01")])
        with self.assertRaises(MarketPriceConflict):
            self.publish([bar("2026-09-02", "101")], ticker="XYZ")
        with self.assertRaises(MarketPriceConflict):
            self.publish([bar("2026-09-02", "101")], currency="EUR")

    def test_a_first_version_needs_at_least_one_bar(self):
        with self.assertRaises(MarketPriceValidationError):
            self.publish([])

    def test_a_non_hash_artifact_is_refused(self):
        with self.assertRaises(MarketPriceValidationError):
            self.publish([bar("2026-09-01")], artifact="not-a-hash")


class ImmutabilityTests(AuthorityTestCase):
    def test_an_unauthorised_connection_cannot_insert(self):
        published = self.publish([bar("2026-09-01")])
        raw = sqlite3.connect(self.store.path)
        raw.create_function("dalton_authorized", 0, lambda: 0)
        self.addCleanup(raw.close)
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT INTO market_price_series_versions "
                "(version_id,series_ref,version_number,prior_version_id,company_ref,"
                "ticker,source_ref,currency,first_bar_date,last_bar_date,bar_count,"
                "added_bar_count,restated_bar_count,record_json,content_hash,"
                "actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("forged", series_ref_for(ACN), 99, None, ACN, "ACN",
                 "source:yahoo-finance", "USD", "2026-09-01", "2026-09-01", 1,
                 1, 0, canonical_json({}), "0" * 64, "human:me", "now"),
            )
        self.assertEqual(len(self.authority.versions(ACN)), 1)
        self.assertEqual(self.authority.versions(ACN)[0]["id"], published["id"])

    def test_a_published_version_cannot_be_updated_or_deleted(self):
        published = self.publish([bar("2026-09-01")])
        with self.store._transaction() as cur:
            with self.assertRaises(sqlite3.IntegrityError):
                cur.execute(
                    "UPDATE market_price_series_versions SET ticker='XYZ' "
                    "WHERE version_id=?", (published["id"],))
        with self.store._transaction() as cur:
            with self.assertRaises(sqlite3.IntegrityError):
                cur.execute(
                    "DELETE FROM market_price_series_versions WHERE version_id=?",
                    (published["id"],))

    def test_a_tampered_record_is_refused_on_read(self):
        published = self.publish([bar("2026-09-01")])
        raw = sqlite3.connect(self.store.path)
        raw.create_function("dalton_authorized", 0, lambda: 1)
        self.addCleanup(raw.close)
        # The no-update trigger stands in the way of an honest edit, so the
        # forgery has to go around it: drop the trigger, rewrite the row, and
        # check that the reader still catches it by hash.
        raw.execute("DROP TRIGGER market_price_series_no_update")
        raw.execute(
            "UPDATE market_price_series_versions SET record_json=? WHERE version_id=?",
            (canonical_json({**published, "ticker": "XYZ"}), published["id"]))
        raw.commit()
        with self.assertRaises(MarketPriceConflict):
            self.authority.version(published["id"])


if __name__ == "__main__":
    unittest.main()
