"""P11g: which figures matter is learned from what the market cites."""

from __future__ import annotations

import unittest

from dalton_core.metric_discovery import (
    MetricDiscoveryError,
    establish_requirements,
    uncorroborated,
    validate_metric_proposal,
    verify_metric_proposal,
    verify_metric_proposals,
)

QUOTE = "quote:10:200:aaaaaaaaaaaaaaaa"
DOC_A = "alphaengine-doc:sell-side-1"
DOC_B = "alphaengine-doc:sell-side-2"
DOC_C = "public-web-url:sha256:press-release"


def proposal(**overrides):
    base = {
        "metric_ref": "metric:new-bookings",
        "label": "new bookings",
        "unit": "currency",
        "evidence_phrase": "new bookings",
        "quote_id": QUOTE,
        "document_ref": DOC_A,
    }
    base.update(overrides)
    return base


class ProposalShapeTests(unittest.TestCase):
    def test_the_shape_is_closed(self) -> None:
        with self.assertRaises(MetricDiscoveryError):
            validate_metric_proposal({**proposal(), "extra": 1})
        for missing in ("metric_ref", "label", "unit", "evidence_phrase", "document_ref"):
            body = proposal()
            body.pop(missing)
            with self.assertRaises(MetricDiscoveryError):
                validate_metric_proposal(body)

    def test_the_ref_must_be_a_stable_slug(self) -> None:
        # Free-text names would make the same figure two requirements.
        for bad in ("new bookings", "metric:New Bookings", "bookings", "metric:"):
            with self.assertRaises(MetricDiscoveryError):
                validate_metric_proposal(proposal(metric_ref=bad))

    def test_an_unknown_unit_is_refused(self) -> None:
        with self.assertRaises(MetricDiscoveryError):
            validate_metric_proposal(proposal(unit="bookings"))


class ProposalVerificationTests(unittest.TestCase):
    def test_wording_present_in_the_citation_is_accepted(self) -> None:
        quotes = {QUOTE: "Management highlighted new bookings of $21.3 billion."}
        out = verify_metric_proposal(proposal(), quotes)
        self.assertEqual(out["metric_ref"], "metric:new-bookings")
        self.assertEqual(out["citation_text"], quotes[QUOTE])

    def test_wording_absent_from_the_citation_is_refused(self) -> None:
        # A model may say where the market named a figure, never invent that
        # it did.
        quotes = {QUOTE: "Management discussed the demand environment."}
        with self.assertRaises(MetricDiscoveryError):
            verify_metric_proposal(proposal(), quotes)

    def test_wording_is_compared_the_way_a_reader_reads_it(self) -> None:
        for text in (
            "Growth in New Bookings was strong.",
            "growth in new-bookings was strong",
            "growth in  new   bookings was strong",
        ):
            self.assertTrue(verify_metric_proposal(proposal(), {QUOTE: text}))

    def test_one_unsupported_proposal_does_not_discard_the_rest(self) -> None:
        quotes = {QUOTE: "New bookings and free cash flow both improved."}
        verified, refused = verify_metric_proposals(
            [
                proposal(),
                proposal(metric_ref="metric:free-cash-flow", label="free cash flow",
                         evidence_phrase="free cash flow"),
                proposal(metric_ref="metric:same-store-sales", label="same store sales",
                         evidence_phrase="same store sales"),
            ],
            quotes,
        )
        self.assertEqual(len(verified), 2)
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["metric_ref"], "metric:same-store-sales")


class RequirementEstablishmentTests(unittest.TestCase):
    def test_one_document_is_an_anecdote(self) -> None:
        # A single stray sentence must not commit the system to hunting a
        # figure forever.
        self.assertEqual(establish_requirements([proposal()]), [])
        pending = uncorroborated([proposal()])
        self.assertEqual(pending[0]["metric_ref"], "metric:new-bookings")
        self.assertEqual(pending[0]["citation_count"], 1)

    def test_two_documents_establish_a_requirement_with_its_citations(self) -> None:
        established = establish_requirements(
            [proposal(document_ref=DOC_A), proposal(document_ref=DOC_B)]
        )
        self.assertEqual(len(established), 1)
        item = established[0]
        self.assertEqual(item["metric_ref"], "metric:new-bookings")
        self.assertEqual(item["citation_count"], 2)
        self.assertEqual(item["cited_by"], sorted([DOC_A, DOC_B]))
        # It carries what extraction needs to ask for it by name.
        self.assertEqual(item["periods"], 4)
        self.assertIn("new bookings", item["prompt"])

    def test_one_verbose_document_cannot_establish_a_requirement_alone(self) -> None:
        # Counting mentions rather than documents would let a single source
        # create requirements by repeating itself.
        repeated = [proposal(document_ref=DOC_A) for _ in range(6)]
        self.assertEqual(establish_requirements(repeated), [])

    def test_the_most_cited_metric_comes_first(self) -> None:
        proposals = [
            proposal(document_ref=DOC_A), proposal(document_ref=DOC_B),
            proposal(document_ref=DOC_C),
            proposal(metric_ref="metric:free-cash-flow", label="free cash flow",
                     evidence_phrase="free cash flow", document_ref=DOC_A),
            proposal(metric_ref="metric:free-cash-flow", label="free cash flow",
                     evidence_phrase="free cash flow", document_ref=DOC_B),
        ]
        established = establish_requirements(proposals)
        self.assertEqual(
            [item["metric_ref"] for item in established],
            ["metric:new-bookings", "metric:free-cash-flow"],
        )

    def test_the_same_name_in_two_units_is_refused_not_merged(self) -> None:
        # Revenue in currency and revenue in percent are different figures;
        # merging them would make the series meaningless.
        self.assertEqual(establish_requirements([
            proposal(document_ref=DOC_A),
            proposal(document_ref=DOC_B, unit="percent"),
        ]), [])

    def test_a_contested_metric_does_not_take_the_others_down_with_it(self) -> None:
        # P13y: this used to raise, and every caller read the exception as "no
        # requirements learned". IBM held 176 observations and got zero, in
        # silence, because two documents disagreed about one of them.
        established = establish_requirements([
            proposal(document_ref=DOC_A),
            proposal(document_ref=DOC_B, unit="percent"),
            proposal(metric_ref="metric:free-cash-flow", label="free cash flow",
                     evidence_phrase="free cash flow", document_ref=DOC_A),
            proposal(metric_ref="metric:free-cash-flow", label="free cash flow",
                     evidence_phrase="free cash flow", document_ref=DOC_B),
        ])
        self.assertEqual([item["metric_ref"] for item in established],
                         ["metric:free-cash-flow"])

    def test_a_metric_the_market_moves_to_is_visible_before_it_qualifies(self) -> None:
        proposals = [
            proposal(document_ref=DOC_A), proposal(document_ref=DOC_B),
            proposal(metric_ref="metric:ai-bookings", label="AI bookings",
                     evidence_phrase="AI bookings", document_ref=DOC_C),
        ]
        self.assertEqual(
            [item["metric_ref"] for item in establish_requirements(proposals)],
            ["metric:new-bookings"],
        )
        # Reported rather than dropped: today's single mention may be next
        # quarter's requirement.
        self.assertEqual(
            [item["metric_ref"] for item in uncorroborated(proposals)],
            ["metric:ai-bookings"],
        )


if __name__ == "__main__":
    unittest.main()
