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
        table = self.events([("2025Q1", 8), ("2025Q2", 6), ("2025Q3", 6),
                             ("2025Q4", 6)])
        verdict = classify(table["events"])
        self.assertEqual(verdict["classification"], "insufficient_data")
        self.assertIn("no pattern reached its threshold", verdict["basis"])


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
