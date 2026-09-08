"""P11f/P11g: the figures a stage needs -- a floor plus what the market cites."""

from __future__ import annotations

import unittest

from dalton_core.metric_base import (
    METRIC_UNITS,
    STAGE_SPINE,
    MetricBaseError,
    extraction_requests,
    metric_spec,
    metrics_for,
    missing_metrics,
)

ACN = "company:sec-cik:0001467373"
IBM = "company:sec-cik:0000051143"


def claim(metric_ref: str, period: str) -> dict:
    return {"metric_ref": metric_ref, "period": period}


class MetricBaseDeclarationTests(unittest.TestCase):
    def test_every_declared_metric_is_well_formed(self) -> None:
        seen = set()
        for stage in STAGE_SPINE:
            for item in metrics_for(stage, ACN):
                self.assertEqual(
                    set(item), {"metric_ref", "label", "unit", "periods", "prompt"}
                )
                self.assertIn(item["unit"], METRIC_UNITS)
                self.assertGreaterEqual(item["periods"], 1)
                self.assertNotIn((stage, item["metric_ref"]), seen)
                seen.add((stage, item["metric_ref"]))

    def test_the_spine_is_only_what_is_true_of_any_company(self) -> None:
        # P11g: the screen-wide list P11f hardcoded is gone. What is left is a
        # floor that holds for anything with an income statement; everything
        # else is discovered from what the market cites.
        refs = {item["metric_ref"] for item in metrics_for("initial_screen", ACN)}
        self.assertEqual(refs, {"metric:revenue", "metric:net-income"})

    def test_a_discovered_metric_becomes_a_requirement(self) -> None:
        discovered = [{
            "metric_ref": "metric:new-bookings", "label": "new bookings",
            "unit": "currency", "periods": 4,
            "prompt": "new bookings for the period",
        }]
        refs = [item["metric_ref"] for item in
                metrics_for("initial_screen", ACN, discovered)]
        self.assertIn("metric:new-bookings", refs)
        # And it is per company: IBM was told nothing, so it asks for nothing.
        self.assertNotIn(
            "metric:new-bookings",
            [item["metric_ref"] for item in metrics_for("initial_screen", IBM)],
        )

    def test_discovering_a_spine_metric_does_not_duplicate_it(self) -> None:
        discovered = [{
            "metric_ref": "metric:revenue", "label": "revenue", "unit": "currency",
            "periods": 8, "prompt": "revenue",
        }]
        refs = [item["metric_ref"] for item in
                metrics_for("initial_screen", ACN, discovered)]
        self.assertEqual(refs.count("metric:revenue"), 1)

    def test_a_malformed_discovered_metric_is_refused(self) -> None:
        with self.assertRaises(MetricBaseError):
            metrics_for("initial_screen", ACN, [{"metric_ref": "metric:x"}])

    def test_an_undeclared_stage_or_metric_is_refused_not_invented(self) -> None:
        with self.assertRaises(MetricBaseError):
            metrics_for("valuation", ACN)
        with self.assertRaises(MetricBaseError):
            metric_spec("initial_screen", ACN, "metric:ebitda")

    def test_deeper_stages_declare_nothing_until_they_are_built(self) -> None:
        # An unbuilt stage asking for figures nobody serves would put a
        # permanent gap on the cockpit, and discovery answers the screen's
        # question rather than a deeper stage's.
        self.assertEqual(metrics_for("company_model", ACN), [])
        discovered = [{"metric_ref": "metric:new-bookings", "label": "b",
                       "unit": "currency", "periods": 4, "prompt": "b"}]
        self.assertEqual(metrics_for("company_model", ACN, discovered), [])


class MissingMetricTests(unittest.TestCase):
    def test_a_metric_is_satisfied_by_distinct_periods_not_by_claim_count(self) -> None:
        # Four claims about one quarter is one quarter of revenue.
        held = [claim("metric:revenue", "FY2026Q3")] * 4
        missing = {item["metric_ref"]: item for item in
                   missing_metrics(held, stage="initial_screen", company_ref=ACN)}
        self.assertEqual(missing["metric:revenue"]["have"], 1)
        self.assertEqual(missing["metric:revenue"]["still_needed"], 3)

    def test_a_fully_covered_metric_drops_off(self) -> None:
        held = [claim("metric:revenue", f"FY2026Q{n}") for n in range(1, 5)]
        refs = {item["metric_ref"] for item in
                missing_metrics(held, stage="initial_screen", company_ref=ACN)}
        self.assertNotIn("metric:revenue", refs)
        self.assertIn("metric:net-income", refs)

    def test_nothing_held_means_everything_is_owed(self) -> None:
        missing = missing_metrics([], stage="initial_screen", company_ref=ACN)
        self.assertEqual(len(missing), len(metrics_for("initial_screen", ACN)))
        self.assertTrue(all(item["have"] == 0 for item in missing))


DISCOVERED = [
    {"metric_ref": "metric:new-bookings", "label": "new bookings", "unit": "currency",
     "periods": 4, "prompt": "new bookings for the period"},
    {"metric_ref": "metric:free-cash-flow", "label": "free cash flow", "unit": "currency",
     "periods": 4, "prompt": "free cash flow for the period"},
]


class ExtractionRequestTests(unittest.TestCase):
    def test_requests_name_the_metric_so_the_model_fills_a_slot(self) -> None:
        requests = extraction_requests(
            [], stage="initial_screen", company_ref=ACN, discovered=DISCOVERED, limit=3
        )
        self.assertEqual(len(requests), 3)
        for item in requests:
            self.assertEqual(
                set(item), {"metric_ref", "label", "unit", "prompt", "still_needed"}
            )

    def test_the_most_owed_metric_is_asked_for_first(self) -> None:
        # One quarter short must not wait behind something with nothing at all.
        held = [claim("metric:revenue", f"FY2026Q{n}") for n in range(1, 4)]
        requests = extraction_requests(
            held, stage="initial_screen", company_ref=ACN, discovered=DISCOVERED, limit=2
        )
        self.assertTrue(all(item["still_needed"] == 4 for item in requests))
        self.assertNotIn("metric:revenue", [item["metric_ref"] for item in requests])

    def test_a_document_is_never_asked_for_everything_at_once(self) -> None:
        requests = extraction_requests([], stage="initial_screen", company_ref=ACN, limit=2)
        self.assertEqual(len(requests), 2)
        with self.assertRaises(MetricBaseError):
            extraction_requests([], stage="initial_screen", company_ref=ACN, limit=0)

    def test_a_company_that_owes_nothing_is_asked_for_nothing(self) -> None:
        held = [
            claim(item["metric_ref"], f"FY2026Q{n}")
            for item in metrics_for("initial_screen", ACN, DISCOVERED)
            for n in range(1, item["periods"] + 1)
        ]
        self.assertEqual(
            extraction_requests(
                held, stage="initial_screen", company_ref=ACN, discovered=DISCOVERED
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
