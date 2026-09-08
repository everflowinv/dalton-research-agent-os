"""P11h: resolve a metric to the concept an issuer actually uses."""

from __future__ import annotations

import unittest

from dalton_core.xbrl_concept_resolver import (
    ConceptResolutionError,
    resolve_concept,
    resolve_metric,
)


def facts(*concepts: tuple[str, int, str | None]) -> dict:
    return {
        "facts": {
            "us-gaap": {
                name: {
                    "label": label,
                    "units": {"USD": [{"val": 1} for _ in range(count)]},
                }
                for name, count, label in concepts
            }
        }
    }


class ConceptResolutionTests(unittest.TestCase):
    def test_the_issuers_own_naming_is_what_is_searched(self) -> None:
        # Three filers, three names for the same line, none of them enumerable
        # in advance. Each resolves against its own taxonomy.
        for name in ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                     "SalesRevenueServicesNet"):
            resolved = resolve_metric(facts((name, 100, name)), "metric:revenue")
            self.assertEqual(resolved["chosen_concept"], name)

    def test_a_qualified_variant_is_not_the_figure_being_asked_for(self) -> None:
        """The failure that matters: real, cited, and about something else.

        Live, IBM does not tag OperatingIncomeLoss at all. Without this the
        resolver returned DisposalGroupIncludingDiscontinuedOperationOperating
        IncomeLoss -- its discontinued operations -- and nothing downstream
        would have looked wrong.
        """

        ibm_like = facts(
            ("DisposalGroupIncludingDiscontinuedOperationOperatingIncomeLoss", 36, "Disposal"),
            ("OtherNonoperatingIncomeExpense", 48, "Other"),
        )
        with self.assertRaises(ConceptResolutionError):
            resolve_metric(ibm_like, "metric:operating-income")

    def test_things_that_merely_mention_revenue_are_refused(self) -> None:
        # Every one of these is in ACN's own taxonomy.
        noise = facts(
            ("CostOfRevenue", 211, "Cost of Revenue"),
            ("DeferredRevenueCurrent", 141, "Deferred Revenue, Current"),
            ("RevenueRemainingPerformanceObligation", 57, "RPO"),
            ("OperatingLeasesIncomeStatementSubleaseRevenue", 30, "Sublease"),
        )
        with self.assertRaises(ConceptResolutionError):
            resolve_metric(noise, "metric:revenue")

    def test_a_deprecated_concept_is_not_chosen_over_a_live_one(self) -> None:
        # A filer keeps history under a retired tag, which is how a resolver
        # picks a concept that stopped being updated years ago.
        payload = facts(
            ("ReimbursementRevenue", 900, "Reimbursement Revenue (Deprecated 2018-01-31)"),
            ("Revenues", 10, "Revenues"),
        )
        self.assertEqual(
            resolve_metric(payload, "metric:revenue")["chosen_concept"], "Revenues"
        )

    def test_an_exact_preference_beats_a_longer_name_with_more_facts(self) -> None:
        payload = facts(
            ("Revenues", 10, "Revenues"),
            ("RevenueFromContractWithCustomerExcludingAssessedTax", 900, "ASC 606"),
        )
        self.assertEqual(
            resolve_metric(payload, "metric:revenue")["chosen_concept"], "Revenues"
        )

    def test_the_working_is_shown_so_a_wrong_pick_is_visible(self) -> None:
        payload = facts(
            ("Revenues", 100, "Revenues"),
            ("RevenueFromContractWithCustomerExcludingAssessedTax", 50, "ASC 606"),
            ("CostOfRevenue", 200, "Cost of Revenue"),
        )
        resolved = resolve_metric(payload, "metric:revenue")
        self.assertEqual(resolved["chosen_concept"], "Revenues")
        self.assertIn(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            [item["concept"] for item in resolved["alternatives"]],
        )
        rejected = {item["concept"]: item["reason"] for item in resolved["rejected"]}
        self.assertIn("CostOfRevenue", rejected)
        self.assertIn("costof", rejected["CostOfRevenue"])

    def test_a_concept_with_no_reported_facts_is_not_chosen(self) -> None:
        payload = {"facts": {"us-gaap": {
            "Revenues": {"label": "Revenues", "units": {}},
            "SalesRevenueServicesNet": {
                "label": "Sales", "units": {"USD": [{"val": 1}]},
            },
        }}}
        self.assertEqual(
            resolve_metric(payload, "metric:revenue")["chosen_concept"],
            "SalesRevenueServicesNet",
        )

    def test_an_untagged_figure_says_so_rather_than_guessing(self) -> None:
        # Bookings and utilisation are not in anyone's us-gaap taxonomy; those
        # come out of prose, and pretending otherwise would invent a concept.
        with self.assertRaises(ConceptResolutionError):
            resolve_metric(facts(("Revenues", 10, "Revenues")), "metric:new-bookings")

    def test_an_empty_or_malformed_payload_is_refused(self) -> None:
        for payload in ({}, {"facts": {}}, {"facts": {"us-gaap": {}}}):
            with self.assertRaises(ConceptResolutionError):
                resolve_metric(payload, "metric:revenue")

    def test_a_metric_must_name_what_it_prefers(self) -> None:
        with self.assertRaises(ConceptResolutionError):
            resolve_concept(facts(("Revenues", 10, "Revenues")), prefer=())


if __name__ == "__main__":
    unittest.main()
