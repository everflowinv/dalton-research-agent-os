"""P12f: the table is computed, the style is a rule, and neither is an impression.

A model asked "is this management conservative?" will answer. It will not have
counted, and the answer will be a summary of the tone of the transcripts it was
shown rather than of the distance between what was guided and what arrived.
So the arithmetic is here, the rule is a stored sentence, and the model is only
ever handed the finished table.

The tests that matter are the ones about *refusing to compare*: a guide in
percent against an actual in dollars, a sentence no rule reads, a run of three
beats. Each of those is a way to manufacture a pattern out of nothing, and each
of them is what a plausible implementation would have done instead.
"""

from __future__ import annotations

import unittest

from dalton_core.guidance_profile import (
    GUIDANCE_STYLE_RULE,
    period_bounds,
    period_key,
    MIN_SETTLED_EVENTS,
    GuidanceProfileError,
    build_profile,
    classify,
    guidance_events,
    measure_of,
    read_range,
    render_profile_table,
    unit_class,
    validate_profile,
)

ACN = "company:sec-cik:0001467373"


def guide(period, text, ref=None, unit="percent"):
    return {"ref": ref or f"claim-version:{period}", "period": period, "text": text,
            "unit": unit}


def actual(period, value, ref=None, measure="revenue_growth", unit="percent"):
    return {"ref": ref or f"claim-version:actual-{period}", "period": period,
            "measure": measure, "value": value, "unit": unit}


class ReadingTests(unittest.TestCase):
    def test_the_shapes_a_guide_is_written_in(self):
        for text, low, high, rule in (
            ("we expect revenue growth of 5% to 7%", "5", "7", "x_to_y"),
            ("between 5 and 7 percent", "5", "7", "between_x_and_y"),
            ("revenue growth of 5-7%", "5", "7", "x_to_y"),
            ("revenue growth of about 6%", "6", "6", "point"),
        ):
            parsed = read_range(text)
            self.assertIsNotNone(parsed, text)
            self.assertEqual((str(parsed["low"]), str(parsed["high"])), (low, high), text)
            self.assertEqual(parsed["guide_basis"], rule, text)

    def test_a_sentence_no_rule_reads_is_not_guessed_at(self):
        self.assertIsNone(read_range("management expects modest improvement"))

    def test_a_dated_span_is_not_a_guidance_range(self):
        # Unanchored, the dash rule read "2025-09-01..2026-05-31" as a range
        # from 9 to 2026, and every period label in the ledger became a guide.
        for text in ("for the period 2025-09-01..2026-05-31",
                     "the quarter ended 2026-05-31",
                     "revenue of 18718144000 in Q2 2026"):
            self.assertIsNone(read_range(text), text)

    def test_a_dashed_range_still_reads_when_it_names_its_unit(self):
        parsed = read_range("我们预计收入增速为 5-7 个百分点")
        self.assertEqual((str(parsed["low"]), str(parsed["high"])), ("5", "7"))
        self.assertEqual(parsed["guide_basis"], "x_dash_y")

    def test_a_span_and_a_date_meet_on_the_period_end(self):
        self.assertEqual(period_key("2026-03-01..2026-05-31"), "2026-05-31")
        self.assertEqual(period_key("2026-05-31"), "2026-05-31")
        self.assertEqual(period_bounds("2026-03-01..2026-05-31"),
                         ("2026-03-01", "2026-05-31"))
        # A fiscal label is not resolved to a date: P12b refused to guess a
        # company's fiscal calendar, and a guide paired against the wrong
        # quarter is worse than a guide paired against nothing.
        self.assertEqual(period_key("Q2 FY2026"), "Q2 FY2026")
        self.assertEqual(period_bounds("Q2 FY2026"), (None, None))

    def test_the_measure_is_a_closed_word_or_nothing(self):
        self.assertEqual(measure_of("local-currency revenue growth"), "revenue_growth")
        self.assertEqual(measure_of("adjusted EPS for the year"), "eps")
        self.assertIsNone(measure_of("management tone on the call"))

    def test_percent_and_money_are_different_kinds_of_number(self):
        self.assertEqual(unit_class("percent"), "percent")
        self.assertEqual(unit_class("USD"), "amount")


class EventTests(unittest.TestCase):
    def test_beat_miss_and_inline(self):
        table = guidance_events(
            [guide("2025Q1", "revenue growth of 5% to 7%"),
             guide("2025Q2", "revenue growth of 5% to 7%"),
             guide("2025Q3", "revenue growth of 5% to 7%")],
            [actual("2025Q1", 8), actual("2025Q2", 4), actual("2025Q3", 6)],
        )
        self.assertEqual([item["deviation"]["verdict"] for item in table["events"]],
                         ["beat", "miss", "inline"])
        self.assertEqual([item["deviation"]["distance"] for item in table["events"]],
                         ["1", "1", "0"])

    def test_a_guide_with_no_actual_is_unknown_rather_than_dropped(self):
        table = guidance_events([guide("2025Q1", "revenue growth of 5% to 7%")], [])
        self.assertEqual(table["events"][0]["deviation"]["verdict"], "unknown")
        self.assertIsNone(table["events"][0]["actual"])

    def test_a_percent_guide_is_never_compared_with_a_dollar_actual(self):
        table = guidance_events(
            [guide("2025Q1", "revenue growth of 5% to 7%")],
            [actual("2025Q1", 18718144000, measure="revenue_growth", unit="USD")],
        )
        deviation = table["events"][0]["deviation"]
        self.assertEqual(deviation["verdict"], "unknown")
        self.assertIn("unit class", deviation["reason"])

    def test_a_quarterly_guide_meets_the_quarter_not_the_year_to_date(self):
        # Both rows end on the same day; only one of them is the three months
        # the guide was about.
        table = guidance_events(
            [{"ref": "claim-version:g", "period": "2026-03-01..2026-05-31",
              "text": "revenue growth of 5% to 7%", "unit": "percent"}],
            [{"ref": "statement-line:ytd", "measure": "revenue_growth",
              "period_start": "2025-09-01", "period_end": "2026-05-31",
              "value": 20, "unit": "percent"},
             {"ref": "statement-line:q", "measure": "revenue_growth",
              "period_start": "2026-03-01", "period_end": "2026-05-31",
              "value": 6, "unit": "percent"}],
        )
        event = table["events"][0]
        self.assertEqual(event["actual"]["refs"], ["statement-line:q"])
        self.assertEqual(event["deviation"]["verdict"], "inline")

    def test_an_unreadable_guide_is_counted_rather_than_invented(self):
        table = guidance_events(
            [guide("2025Q1", "management expects modest revenue growth improvement")], [])
        self.assertEqual(table["events"], [])
        self.assertEqual(table["unparsed"][0]["reason"],
                         "no rule read a range from the statement")

    def test_every_event_says_which_rule_read_it(self):
        table = guidance_events([guide("2025Q1", "revenue growth of 5% to 7%")], [])
        self.assertEqual(table["events"][0]["guide"]["guide_basis"], "x_to_y")


class ClassificationTests(unittest.TestCase):
    def events(self, pairs, guides=None):
        rows = []
        for index, (period, value) in enumerate(pairs):
            low, high = (guides or [(5, 7)] * len(pairs))[index]
            rows.append(guide(period, f"revenue growth of {low}% to {high}%"))
        return guidance_events(rows, [actual(period, value) for period, value in pairs])

    def test_three_beats_are_a_run_not_a_style(self):
        table = self.events([("2025Q1", 8), ("2025Q2", 8), ("2025Q3", 8)])
        verdict = classify(table["events"])
        self.assertEqual(verdict["classification"], "insufficient_data")
        self.assertIn(str(MIN_SETTLED_EVENTS), verdict["basis"])

    def test_beating_a_flat_guide_every_quarter_is_conservative(self):
        table = self.events([("2025Q1", 8), ("2025Q2", 8), ("2025Q3", 9),
                             ("2025Q4", 8)])
        verdict = classify(table["events"])
        self.assertEqual(verdict["classification"], "conservative")

    def test_beating_a_rising_guide_every_quarter_is_beat_and_raise(self):
        table = self.events(
            [("2025Q1", 8), ("2025Q2", 9), ("2025Q3", 10), ("2025Q4", 11)],
            guides=[(5, 7), (6, 8), (7, 9), (8, 10)])
        verdict = classify(table["events"])
        self.assertEqual(verdict["classification"], "beat_and_raise")
        self.assertIn("revised upward", verdict["basis"])

    def test_missing_half_the_time_is_aggressive(self):
        table = self.events([("2025Q1", 4), ("2025Q2", 4), ("2025Q3", 8),
                             ("2025Q4", 6)])
        verdict = classify(table["events"])
        self.assertEqual(verdict["classification"], "aggressive")

    def test_a_mixed_record_says_so_rather_than_choosing(self):
        # The fifth word (owner, 2026-09-09). Four settled events that show no
        # pattern is a finding about this management team; two settled events
        # is a gap in our evidence. Sharing one word for both would have lost
        # the difference.
        table = self.events([("2025Q1", 8), ("2025Q2", 6), ("2025Q3", 6),
                             ("2025Q4", 6)])
        verdict = classify(table["events"])
        self.assertEqual(verdict["classification"], "mixed")
        self.assertIn("no pattern reached its threshold", verdict["basis"])

    def test_mixed_and_insufficient_data_are_not_the_same_answer(self):
        thin = classify(self.events([("2025Q1", 8), ("2025Q2", 6)])["events"])
        self.assertEqual(thin["classification"], "insufficient_data")
        self.assertIn("the rule needs", thin["basis"])


class ProfileTests(unittest.TestCase):
    def test_the_profile_carries_its_own_rule_and_refuses_a_swapped_one(self):
        profile = build_profile(
            company_ref=ACN,
            guides=[guide("2025Q1", "revenue growth of 5% to 7%")],
            actuals=[actual("2025Q1", 8)])
        self.assertEqual(profile["rule"], GUIDANCE_STYLE_RULE)
        self.assertEqual(profile["refs"],
                         ["claim-version:2025Q1", "claim-version:actual-2025Q1"])
        with self.assertRaises(GuidanceProfileError):
            validate_profile({**profile, "rule": "whatever I felt like"})

    def test_a_classification_outside_the_four_words_is_refused(self):
        profile = build_profile(company_ref=ACN, guides=[], actuals=[])
        with self.assertRaises(GuidanceProfileError):
            validate_profile({**profile, "classification": "pretty conservative"})

    def test_the_table_the_model_is_shown_is_a_table(self):
        profile = build_profile(
            company_ref=ACN,
            guides=[guide("2025Q1", "revenue growth of 5% to 7%")],
            actuals=[actual("2025Q1", 8)])
        rendered = render_profile_table(profile)
        self.assertIn("2025Q1\trevenue_growth\t5\t7\tpercent\t8\tbeat", rendered)
        self.assertIn("classification\tinsufficient_data", rendered)

    def test_no_material_at_all_is_insufficient_data_and_not_an_error(self):
        profile = build_profile(company_ref=ACN, guides=[], actuals=[])
        self.assertEqual(profile["classification"], "insufficient_data")
        self.assertEqual(profile["events"], [])


if __name__ == "__main__":
    unittest.main()
