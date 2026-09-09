"""P11d: a move is only news if the rest of the universe did not do it too."""

from __future__ import annotations

import unittest
from decimal import Decimal

from dalton_core.market_event import (
    basket_return,
    cumulative_return,
    detect_price_divergences,
    daily_return,
    detect_abnormal_moves,
    latest_settled_date,
    recent_settled_dates,
)
from tests.p14a_fixtures import ACN, CTSH, DXC, EPAM, IBM, bar

THRESHOLDS = {
    "threshold_ref": "abnormal-move:test:v1",
    "absolute_move_percent": "3.0",
    "excess_vs_basket_percent": "2.5",
    "excess_vs_benchmark_percent": "3.0",
    "min_basket_members": 3,
}
SETTLED = "2026-09-02T23:30:00+00:00"
SETTLED_LATE = "2026-09-03T23:30:00+00:00"
MID_SESSION = "2026-09-02T18:00:00+00:00"


def series(closes, *, captured_at=SETTLED, version="market-price-series-version:1"):
    return {
        "version_ref": version,
        "bars": [
            {**bar(date, close), "captured_at": captured_at,
             "invocation_ref": "connector-invocation:yfinance:x",
             "artifact_hash": "0" * 64}
            for date, close in closes
        ],
    }


def flat(prefix="100"):
    return series([("2026-09-01", prefix), ("2026-09-02", prefix)])


class DailyReturnTests(unittest.TestCase):
    def test_the_first_bar_of_a_series_has_no_return(self):
        self.assertIsNone(daily_return(series([("2026-09-02", "100")]), "2026-09-02"))

    def test_a_provisional_bar_never_produces_a_return(self):
        # Its close is the last trade of an afternoon wearing a close's shape.
        mid = series([("2026-09-01", "100"), ("2026-09-02", "110")],
                     captured_at=MID_SESSION)
        self.assertIsNone(daily_return(mid, "2026-09-02"))

    def test_a_settled_bar_produces_an_exact_decimal_return(self):
        row = daily_return(series([("2026-09-01", "100"), ("2026-09-02", "104")]),
                           "2026-09-02")
        self.assertEqual(row["return_percent"], Decimal("4"))
        self.assertEqual(row["close"], "104")

    def test_an_absent_day_is_absent(self):
        self.assertIsNone(daily_return(flat(), "2026-09-03"))
        self.assertIsNone(daily_return(None, "2026-09-02"))


class BasketTests(unittest.TestCase):
    def test_the_subject_is_excluded_from_its_own_basket(self):
        returns = {
            ACN: {"return_percent": Decimal("5")},
            CTSH: {"return_percent": Decimal("0")},
            EPAM: {"return_percent": Decimal("0")},
        }
        value, members = basket_return(returns, exclude=ACN)
        self.assertEqual(value, Decimal("0"))
        self.assertEqual(members, 2)

    def test_a_basket_of_nobody_is_nothing_not_zero(self):
        self.assertEqual(basket_return({ACN: {"return_percent": Decimal("5")}},
                                       exclude=ACN), (None, 0))


class DetectionTests(unittest.TestCase):
    def universe(self, acn_close, peers="100"):
        return {
            ACN: series([("2026-09-01", "100"), ("2026-09-02", acn_close)]),
            CTSH: flat(peers), EPAM: flat(peers), IBM: flat(peers), DXC: flat(peers),
        }

    def test_a_sector_wide_move_fires_on_nobody(self):
        # Every name down four percent is one fact about rates and zero facts
        # about any of them: no excess, and the absolute threshold is the only
        # thing that would fire, so it is checked against a universe that all
        # moved the same amount.
        moved = {
            ref: series([("2026-09-01", "100"), ("2026-09-02", "98")])
            for ref in (ACN, CTSH, EPAM, IBM, DXC)
        }
        events = detect_abnormal_moves(
            series_by_company=moved, as_of="2026-09-02",
            thresholds={**THRESHOLDS, "absolute_move_percent": "3.0"},
        )
        self.assertEqual(events, [])

    def test_one_name_moving_against_flat_peers_fires(self):
        events = detect_abnormal_moves(
            series_by_company=self.universe("104"), as_of="2026-09-02",
            thresholds=THRESHOLDS,
        )
        self.assertEqual([event["company_ref"] for event in events], [ACN])
        payload = events[0]["payload"]
        self.assertEqual(payload["return_percent"], "4.0000")
        self.assertEqual(payload["basket_return_percent"], "0.0000")
        self.assertEqual(payload["excess_vs_basket_percent"], "4.0000")
        self.assertEqual(payload["basket_members"], 4)
        self.assertEqual(payload["direction"], "up")
        self.assertIn("absolute", payload["trigger"])
        self.assertIn("excess_vs_basket", payload["trigger"])

    def test_a_provisional_bar_never_triggers(self):
        moved = self.universe("104")
        moved[ACN] = series([("2026-09-01", "100"), ("2026-09-02", "104")],
                            captured_at=MID_SESSION)
        self.assertEqual(
            detect_abnormal_moves(series_by_company=moved, as_of="2026-09-02",
                                  thresholds=THRESHOLDS),
            [],
        )

    def test_a_basket_below_the_minimum_does_not_produce_an_excess(self):
        two = {
            ACN: series([("2026-09-01", "100"), ("2026-09-02", "104")]),
            CTSH: flat(),
        }
        events = detect_abnormal_moves(
            series_by_company=two, as_of="2026-09-02", thresholds=THRESHOLDS,
        )
        # The absolute threshold still fires; the basket fields say nothing was
        # comparable rather than reporting a one-name basket as a sector.
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]["payload"]["excess_vs_basket_percent"])
        self.assertIsNone(events[0]["payload"]["basket_return_percent"])

    def test_a_missing_benchmark_is_recorded_not_treated_as_zero(self):
        events = detect_abnormal_moves(
            series_by_company=self.universe("104"), as_of="2026-09-02",
            thresholds=THRESHOLDS, benchmark_series=None,
            benchmark_ref="company:benchmark:SPY",
        )
        payload = events[0]["payload"]
        self.assertEqual(payload["benchmark_ref"], "company:benchmark:SPY")
        self.assertIsNone(payload["benchmark_return_percent"])
        self.assertIsNone(payload["excess_vs_benchmark_percent"])

    def test_a_present_benchmark_is_subtracted(self):
        events = detect_abnormal_moves(
            series_by_company=self.universe("104"), as_of="2026-09-02",
            thresholds=THRESHOLDS,
            benchmark_series=series([("2026-09-01", "100"), ("2026-09-02", "101")]),
            benchmark_ref="company:benchmark:SPY",
        )
        payload = events[0]["payload"]
        self.assertEqual(payload["benchmark_return_percent"], "1.0000")
        self.assertEqual(payload["excess_vs_benchmark_percent"], "3.0000")

    def test_an_event_body_carries_the_price_version_and_the_invocation(self):
        events = detect_abnormal_moves(
            series_by_company=self.universe("104"), as_of="2026-09-02",
            thresholds=THRESHOLDS,
        )
        self.assertEqual(events[0]["kind"], "price_move")
        self.assertEqual(events[0]["evidence_tier"], "market_price")
        self.assertEqual(events[0]["occurred_at"], "2026-09-02T00:00:00+00:00")
        self.assertIn("market-price-series-version:1", events[0]["source_refs"])
        self.assertEqual(events[0]["payload"]["price_version_ref"],
                         "market-price-series-version:1")


class SettledDateTests(unittest.TestCase):
    def test_the_shared_newest_settled_day_is_used(self):
        # One company's fetch ran first and has an extra day; comparing on it
        # would make the basket one name wide.
        universe = {
            ACN: series([("2026-09-01", "100"), ("2026-09-02", "101"),
                         ("2026-09-03", "102")]),
            CTSH: series([("2026-09-01", "100"), ("2026-09-02", "100")]),
        }
        self.assertEqual(latest_settled_date(universe), "2026-09-02")
        self.assertEqual(recent_settled_dates(universe, limit=5),
                         ["2026-09-01", "2026-09-02"])

    def test_a_provisional_day_is_not_a_settled_day(self):
        # Read at 18:00Z on the 2nd: the 1st had closed hours earlier and is
        # settled; the 2nd is an afternoon's last trade and is not.
        universe = {
            ACN: series([("2026-09-01", "100"), ("2026-09-02", "101")],
                        captured_at=MID_SESSION),
        }
        self.assertEqual(latest_settled_date(universe), "2026-09-01")
        self.assertEqual(recent_settled_dates(universe, limit=5), ["2026-09-01"])

    def test_an_empty_universe_has_no_day(self):
        self.assertIsNone(latest_settled_date({ACN: None}))
        self.assertEqual(recent_settled_dates({ACN: None}, limit=3), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class DivergenceTests(unittest.TestCase):
    """The owner's third instruction: agreeing with the market is worth nothing."""

    THRESHOLDS = {**THRESHOLDS, "window_trading_days": 10,
                  "divergence_vs_basket_percent": "6.0", "min_basket_members": 2}
    DATES = ["2026-09-01", "2026-09-02", "2026-09-03"]

    def universe(self, acn_last, peers_last="100", captured_at=SETTLED_LATE):
        def run(last):
            return series([("2026-09-01", "100"), ("2026-09-02", "100"),
                           ("2026-09-03", last)], captured_at=captured_at)

        return {ACN: run(acn_last), CTSH: run(peers_last), EPAM: run(peers_last),
                IBM: run(peers_last)}

    def stances(self, stance="long"):
        return {ACN: {"thesis_ref": "thesis-version:acn", "stance": stance}}

    def test_a_long_thesis_diverges_when_the_price_runs_down_against_flat_peers(self):
        events = detect_price_divergences(
            series_by_company=self.universe("92"), dates=self.DATES,
            thresholds=self.THRESHOLDS, stances=self.stances(),
        )
        self.assertEqual([event["company_ref"] for event in events], [ACN])
        payload = events[0]["payload"]
        self.assertEqual(payload["cumulative_return_percent"], "-8.0000")
        self.assertEqual(payload["excess_vs_basket_percent"], "-8.0000")
        self.assertEqual(payload["divergence_percent"], "8.0000")
        self.assertEqual(payload["thesis_stance"], "long")
        self.assertEqual(payload["thesis_ref"], "thesis-version:acn")
        self.assertEqual(payload["from_date"], "2026-09-01")
        self.assertEqual(payload["as_of"], "2026-09-03")
        self.assertEqual(events[0]["kind"], "price_divergence")

    def test_a_long_thesis_going_the_right_way_produces_nothing(self):
        # Being right is not an event. Only the disagreement is.
        self.assertEqual(
            detect_price_divergences(
                series_by_company=self.universe("110"), dates=self.DATES,
                thresholds=self.THRESHOLDS, stances=self.stances(),
            ),
            [],
        )

    def test_the_same_move_under_a_short_thesis_is_the_other_way_round(self):
        against_short = detect_price_divergences(
            series_by_company=self.universe("110"), dates=self.DATES,
            thresholds=self.THRESHOLDS, stances=self.stances("short"),
        )
        self.assertEqual(len(against_short), 1)
        self.assertEqual(against_short[0]["payload"]["divergence_percent"], "10.0000")
        self.assertEqual(
            detect_price_divergences(
                series_by_company=self.universe("92"), dates=self.DATES,
                thresholds=self.THRESHOLDS, stances=self.stances("short"),
            ),
            [],
        )

    def test_a_move_below_the_threshold_is_not_a_divergence(self):
        self.assertEqual(
            detect_price_divergences(
                series_by_company=self.universe("97"), dates=self.DATES,
                thresholds=self.THRESHOLDS, stances=self.stances(),
            ),
            [],
        )

    def test_a_sector_wide_fall_is_not_a_divergence(self):
        self.assertEqual(
            detect_price_divergences(
                series_by_company=self.universe("92", peers_last="92"),
                dates=self.DATES, thresholds=self.THRESHOLDS, stances=self.stances(),
            ),
            [],
        )

    def test_a_provisional_end_of_window_never_fires(self):
        self.assertEqual(
            detect_price_divergences(
                series_by_company=self.universe("92", captured_at=MID_SESSION),
                dates=self.DATES, thresholds=self.THRESHOLDS, stances=self.stances(),
            ),
            [],
        )

    def test_a_company_with_no_thesis_diverges_from_nothing(self):
        self.assertEqual(
            detect_price_divergences(
                series_by_company=self.universe("92"), dates=self.DATES,
                thresholds=self.THRESHOLDS, stances={},
            ),
            [],
        )

    def test_a_suppressed_company_does_not_re_fire_inside_its_own_window(self):
        # A divergence that persists for a fortnight would otherwise produce a
        # fresh event every day, each with a different end date and therefore a
        # different hash.
        self.assertEqual(
            detect_price_divergences(
                series_by_company=self.universe("92"), dates=self.DATES,
                thresholds=self.THRESHOLDS, stances=self.stances(),
                suppress={ACN: True},
            ),
            [],
        )

    def test_a_window_of_one_day_is_not_a_window(self):
        self.assertEqual(
            detect_price_divergences(
                series_by_company=self.universe("92"), dates=["2026-09-03"],
                thresholds=self.THRESHOLDS, stances=self.stances(),
            ),
            [],
        )

    def test_a_cumulative_return_needs_both_ends(self):
        run = series([("2026-09-01", "100"), ("2026-09-03", "92")],
                     captured_at=SETTLED_LATE)
        self.assertIsNone(cumulative_return(run, from_date="2026-09-02",
                                            as_of="2026-09-03"))
        self.assertEqual(cumulative_return(run, from_date="2026-09-01",
                                           as_of="2026-09-03"), Decimal("-8"))
