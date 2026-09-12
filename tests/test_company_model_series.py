"""P13an: filed lines into a quarterly series, without booking anything twice.

The trap this module exists for is real and live: EPAM's Q2 2026 revenue is
1,414,767 thousand and its first half is 2,814,828 thousand, both ending
2026-06-30, and only ``period_start`` tells them apart. Everything here is
about not confusing those two, not inventing the ones that were never filed,
and saying plainly which figures were computed rather than reported.
"""

from __future__ import annotations

import unittest

from dalton_core.company_model_series import (
    CUMULATIVE,
    DERIVED,
    INSTANT,
    QUARTER,
    REPORTED,
    UNKNOWN,
    period_kind,
    quarterly_series,
    series_gaps,
)


def _row(start, end, value, *, filed="2026-07-01", accession="0001352010-26-000046",
         breakdown=False, axis=None, unit="USD", concept=None):
    return {
        "period_start": start, "period_end": end, "value": value,
        "filed": filed, "accession": accession, "unit": unit,
        "is_breakdown": breakdown, "dimension_axis": axis, "concept": concept,
    }


class PeriodKindTests(unittest.TestCase):
    def test_a_missing_start_is_an_instant(self):
        self.assertEqual(period_kind(None, "2026-06-30"), INSTANT)

    def test_a_calendar_quarter_and_a_fiscal_one_are_both_quarters(self):
        self.assertEqual(period_kind("2026-04-01", "2026-06-30"), QUARTER)
        # Accenture's fiscal third quarter, 92 days, ending on a month that is
        # nobody's calendar quarter end.
        self.assertEqual(period_kind("2026-03-01", "2026-05-31"), QUARTER)
        # A 13-week retail quarter.
        self.assertEqual(period_kind("2026-02-02", "2026-05-03"), QUARTER)

    def test_halves_and_nine_months_and_years_are_cumulative(self):
        for start, end in (("2026-01-01", "2026-06-30"),
                           ("2026-01-01", "2026-09-30"),
                           ("2025-07-01", "2026-06-30")):
            with self.subTest(end=end):
                self.assertEqual(period_kind(start, end), CUMULATIVE)

    def test_nonsense_is_not_forced_into_a_shape(self):
        self.assertEqual(period_kind("2026-06-30", "2026-01-01"), UNKNOWN)
        self.assertEqual(period_kind("2020-01-01", "2026-06-30"), UNKNOWN)
        self.assertEqual(period_kind("2026-04-01", "not-a-date"), UNKNOWN)


class QuarterlySeriesTests(unittest.TestCase):
    def test_weighted_average_shares_keep_filed_durations_without_subtracting_them(self):
        concept = "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding"
        series = quarterly_series([
            _row("2025-01-01", "2025-03-31", "100", concept=concept),
            _row("2025-04-01", "2025-06-30", "104", concept=concept,
                 accession="0000000000-25-000002"),
            _row("2025-07-01", "2025-09-30", "106", concept=concept,
                 accession="0000000000-25-000003"),
            _row("2025-01-01", "2025-09-30", "105", concept=concept,
                 accession="0000000000-25-000003"),
            # Subtraction would invent a negative Q4 even though both filed
            # weighted averages are positive.
            _row("2025-01-01", "2025-12-31", "104", concept=concept,
                 accession="0000000000-26-000001"),
            _row("2026-01-01", "2026-03-31", "103", concept=concept,
                 accession="0000000000-26-000002"),
        ])
        self.assertEqual(
            [(item["period_end"], item["value"], item["basis"])
             for item in series["quarters"]],
            [("2025-03-31", "100", REPORTED),
             ("2025-06-30", "104", REPORTED),
             ("2025-09-30", "106", REPORTED),
             ("2026-03-31", "103", REPORTED)],
        )
        self.assertEqual(series["derived_count"], 0)
        self.assertEqual(
            [(item["period_end"], item["value"], item["period_kind"])
             for item in series["durations"]],
            [("2025-03-31", "100", QUARTER),
             ("2025-06-30", "104", QUARTER),
             ("2025-09-30", "105", CUMULATIVE),
             ("2025-09-30", "106", QUARTER),
             ("2025-12-31", "104", CUMULATIVE),
             ("2026-03-31", "103", QUARTER)],
        )
        self.assertEqual(series_gaps(series), [{
            "after": "2025-09-30",
            "before": "2026-01-01",
            "missing_from": "2025-10-01",
            "missing_to": "2025-12-31",
            "days": 92,
        }])

    def test_basic_weighted_average_shares_also_keep_exact_reported_quarters(self):
        concept = "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic"
        series = quarterly_series([
            _row("2026-01-01", "2026-03-31", "90", concept=concept),
            _row("2026-04-01", "2026-06-30", "91", concept=concept),
            _row("2026-01-01", "2026-06-30", "90.5", concept=concept),
        ])
        self.assertEqual(
            [(item["period_end"], item["value"], item["basis"])
             for item in series["quarters"]],
            [("2026-03-31", "90", REPORTED),
             ("2026-06-30", "91", REPORTED)],
        )
        self.assertEqual(series["derived_count"], 0)

    def test_additive_flow_with_a_shares_unit_still_derives_missing_quarter(self):
        series = quarterly_series([
            _row("2026-01-01", "2026-03-31", "100", unit="shares",
                 concept="example:SharesIssued"),
            _row("2026-01-01", "2026-06-30", "250", unit="shares",
                 concept="example:SharesIssued"),
        ])
        self.assertEqual(
            [(item["period_end"], item["value"], item["basis"])
             for item in series["quarters"]],
            [("2026-03-31", "100", REPORTED),
             ("2026-06-30", "150", DERIVED)],
        )

    def test_weighted_average_legacy_replay_keeps_prior_derivation(self):
        concept = "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding"
        series = quarterly_series([
            _row("2025-01-01", "2025-09-30", "105", concept=concept),
            _row("2025-01-01", "2025-12-31", "104", concept=concept),
        ], legacy_replay=True)
        self.assertEqual(series["quarters"][0]["value"], "-1")
        self.assertEqual(series["quarters"][0]["basis"], DERIVED)

    def test_non_additive_policy_requires_every_row_to_name_the_exact_concept(self):
        concept = "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding"
        for adversarial in (None, "", "example:OtherConcept"):
            with self.subTest(adversarial=adversarial):
                series = quarterly_series([
                    _row("2025-01-01", "2025-09-30", "105", concept=concept),
                    _row("2025-01-01", "2025-12-31", "110",
                         concept=adversarial),
                ])
                self.assertEqual(series["derived_count"], 1)
                self.assertEqual(series["quarters"][0]["value"], "5")

    def test_the_quarter_is_kept_and_the_year_to_date_is_not_added_to_it(self):
        # The live trap, exactly as filed.
        series = quarterly_series([
            _row("2026-01-01", "2026-03-31", "1400061000"),
            _row("2026-04-01", "2026-06-30", "1414767000"),
            _row("2026-01-01", "2026-06-30", "2814828000"),
        ])
        self.assertEqual([item["value"] for item in series["quarters"]],
                         ["1400061000", "1414767000"])
        self.assertEqual(series["cumulative_used"], 1)
        self.assertEqual(series["derived_count"], 0)
        self.assertEqual(
            [(item["period_end"], item["period_kind"])
             for item in series["durations"]],
            [("2026-03-31", QUARTER), ("2026-06-30", CUMULATIVE),
             ("2026-06-30", QUARTER)],
        )

    def test_a_quarter_nobody_reported_is_derived_and_says_so(self):
        # A filer that reports only cumulative figures: the third quarter is
        # nine months minus six, and it is not a filed number.
        series = quarterly_series([
            _row("2026-01-01", "2026-03-31", "100"),
            _row("2026-01-01", "2026-06-30", "250", accession="0000000000-26-000002"),
            _row("2026-01-01", "2026-09-30", "420", accession="0000000000-26-000003"),
        ])
        by_end = {item["period_end"]: item for item in series["quarters"]}
        self.assertEqual(by_end["2026-06-30"]["value"], "150")
        self.assertEqual(by_end["2026-06-30"]["basis"], DERIVED)
        self.assertEqual(by_end["2026-09-30"]["value"], "170")
        self.assertEqual(series["derived_count"], 2)
        # And it names both filings the arithmetic rests on.
        self.assertEqual(len(by_end["2026-09-30"]["source_accessions"]), 2)
        self.assertEqual(len(by_end["2026-09-30"]["derived_from"]), 2)

    def test_a_reported_quarter_is_never_replaced_by_a_derived_one(self):
        series = quarterly_series([
            _row("2026-01-01", "2026-03-31", "100"),
            _row("2026-04-01", "2026-06-30", "149"),
            _row("2026-01-01", "2026-06-30", "250"),
        ])
        by_end = {item["period_end"]: item for item in series["quarters"]}
        self.assertEqual(by_end["2026-06-30"]["value"], "149")
        self.assertEqual(by_end["2026-06-30"]["basis"], REPORTED)
        self.assertEqual(series["derived_count"], 0)

    def test_cumulative_figures_from_different_years_are_not_subtracted(self):
        # Only figures sharing a start date can be differenced. Nine months of
        # this year minus six months of last year is not a quarter.
        series = quarterly_series([
            _row("2025-01-01", "2025-06-30", "250"),
            _row("2026-01-01", "2026-09-30", "420"),
        ])
        self.assertEqual(series["quarters"], [])
        self.assertEqual(series["derived_count"], 0)

    def test_cumulative_figures_with_different_units_are_not_subtracted(self):
        series = quarterly_series([
            _row("2026-01-01", "2026-03-31", "100", unit="USD"),
            _row("2026-01-01", "2026-06-30", "250", unit="EUR"),
        ])
        self.assertEqual([item["period_end"] for item in series["quarters"]],
                         ["2026-03-31"])
        self.assertEqual(series["derived_count"], 0)

    def test_direct_annual_duration_stays_separate_from_same_end_quarter(self):
        series = quarterly_series([
            _row("2025-01-01", "2025-12-31", "400"),
            _row("2025-10-01", "2025-12-31", "110"),
        ])
        self.assertEqual([item["value"] for item in series["quarters"]], ["110"])
        self.assertEqual(
            [(item["period_start"], item["period_kind"], item["value"])
             for item in series["durations"]],
            [("2025-01-01", CUMULATIVE, "400"),
             ("2025-10-01", QUARTER, "110")],
        )

    def test_the_most_recently_filed_statement_of_a_quarter_wins(self):
        series = quarterly_series([
            _row("2026-01-01", "2026-03-31", "100", filed="2026-04-30",
                 accession="0000000000-26-000001"),
            _row("2026-01-01", "2026-03-31", "104", filed="2026-07-31",
                 accession="0000000000-26-000002"),
        ])
        self.assertEqual(series["quarters"][0]["value"], "104")
        self.assertEqual(series["quarters"][0]["source_accessions"],
                         ["0000000000-26-000002"])

    def test_conflicting_values_in_one_filing_are_ambiguous_not_parse_order(self):
        series = quarterly_series([
            _row("2026-04-01", "2026-06-30", "953300000",
                 accession="0000051143-26-000078"),
            _row("2026-04-01", "2026-06-30", "953263534",
                 accession="0000051143-26-000078"),
        ])
        self.assertEqual(series["quarters"], [])
        self.assertEqual(series["durations"], [])
        self.assertEqual(series["ambiguous_periods"][0]["values"], [
            {"value": "953263534", "unit": "usd"},
            {"value": "953300000", "unit": "usd"},
        ])

    def test_legacy_replay_keeps_the_original_shape_and_parse_order_tie(self):
        series = quarterly_series([
            _row("2026-04-01", "2026-06-30", "953300000",
                 accession="0000051143-26-000078"),
            _row("2026-04-01", "2026-06-30", "953263534",
                 accession="0000051143-26-000078"),
        ], legacy_replay=True)
        self.assertEqual(series["quarters"][0]["value"], "953263534")
        self.assertNotIn("durations", series)
        self.assertNotIn("ambiguous_periods", series)

    def test_legacy_replay_keeps_cross_unit_cumulative_arithmetic(self):
        series = quarterly_series([
            _row("2026-01-01", "2026-03-31", "100", unit="USD"),
            _row("2026-01-01", "2026-06-30", "250", unit="EUR"),
        ], legacy_replay=True)
        self.assertEqual(
            [(item["period_end"], item["value"], item["unit"])
             for item in series["quarters"]],
            [("2026-03-31", "100", "USD"),
             ("2026-06-30", "150", "EUR")],
        )
        self.assertEqual(series["derived_count"], 1)

    def test_a_breakdown_is_never_mistaken_for_the_total(self):
        # Live: Accenture reports Consulting and Managed Services against the
        # same concept and period as the total, and the parser left the
        # breakdown flag clear on both. The dimension is what gives them away.
        series = quarterly_series([
            _row("2026-03-01", "2026-05-31", "18718144000"),
            _row("2026-03-01", "2026-05-31", "9328494000",
                 axis="srt:ProductOrServiceAxis"),
            _row("2026-03-01", "2026-05-31", "9389650000", breakdown=True),
        ])
        self.assertEqual([item["value"] for item in series["quarters"]],
                         ["18718144000"])

    def test_balance_sheet_instants_are_kept_apart_from_durations(self):
        series = quarterly_series([
            _row(None, "2026-06-30", "55000000000"),
            _row("2026-04-01", "2026-06-30", "1414767000"),
        ])
        self.assertEqual(len(series["quarters"]), 1)
        self.assertEqual(len(series["instants"]), 1)
        self.assertEqual(series["instants"][0]["value"], "55000000000")

    def test_a_period_nobody_can_classify_is_counted_not_guessed(self):
        series = quarterly_series([_row("2020-01-01", "2026-06-30", "1")])
        self.assertEqual(series["quarters"], [])
        self.assertEqual(series["unclassified_periods"], 1)

    def test_rows_without_a_usable_figure_are_dropped(self):
        series = quarterly_series([
            _row("2026-04-01", "2026-06-30", None),
            _row("2026-04-01", "2026-06-30", "not a number"),
            _row("2026-04-01", None, "100"),
        ])
        self.assertEqual(series["quarters"], [])

    def test_negatives_and_decimals_survive_intact(self):
        series = quarterly_series([
            _row("2026-01-01", "2026-03-31", "-49630000"),
            _row("2026-04-01", "2026-06-30", "10.01"),
        ])
        self.assertEqual([item["value"] for item in series["quarters"]],
                         ["-49630000", "10.01"])

    def test_an_empty_input_is_an_empty_series(self):
        series = quarterly_series([])
        self.assertEqual(series["quarters"], [])
        self.assertEqual(series["instants"], [])


class SeriesGapTests(unittest.TestCase):
    def test_the_hole_a_ten_q_only_history_leaves_is_named(self):
        # EPAM, live: 10-Qs never cover the fourth quarter, so a series built
        # from them alone has one hole a year and this says which days.
        series = quarterly_series([
            _row("2025-07-01", "2025-09-30", "1394373000"),
            _row("2026-01-01", "2026-03-31", "1400061000"),
            _row("2026-04-01", "2026-06-30", "1414767000"),
        ])
        gaps = series_gaps(series)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["missing_from"], "2025-10-01")
        self.assertEqual(gaps[0]["missing_to"], "2025-12-31")
        self.assertEqual(gaps[0]["days"], 92)

    def test_a_continuous_series_has_no_gaps(self):
        series = quarterly_series([
            _row("2026-01-01", "2026-03-31", "100"),
            _row("2026-04-01", "2026-06-30", "110"),
        ])
        self.assertEqual(series_gaps(series), [])

    def test_a_series_of_one_cannot_have_a_gap(self):
        series = quarterly_series([_row("2026-04-01", "2026-06-30", "110")])
        self.assertEqual(series_gaps(series), [])


if __name__ == "__main__":
    unittest.main()
