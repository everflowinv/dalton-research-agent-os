"""P13i: don't mint a claim against a company the document is not about.

A free-text search filed a Haier European-business call and an EOS call under
EPAM. The figures pass refuses them now, but qualitative claims were still
being minted from them and cleaned up afterwards -- claim_retirement has
already retired 69 claims for exactly this reason. Retiring after the fact is
a worse version of not admitting in the first place: the wrong claim exists,
is read, and is only withdrawn if the retirement lane happens to get to it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.store import content_hash
from tests.p9a_fixtures import mission_params
from tests.test_document_extraction import ExtractionHarness, NEW_DOC


class AdmissionAttributionTests(unittest.TestCase):
    """Admission needs a connected source and the mission's own automation
    principal -- exactly the path that was minting claims from another
    company's earnings call."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.h = ExtractionHarness(Path(self.temp.name))
        self.addCleanup(self.h.close)
        # A mission version with AlphaEngine connected and source_discovery
        # granted; the acquired document carries forward with its ticket.
        params = mission_params(self.h.state)
        params["autonomy"]["may_write"] = list(params["autonomy"]["may_write"]) + ["source_discovery"]
        for item in params["source_plan"]:
            if item["source_ref"] == "source:alphaengine":
                item["status"] = "connected"
        params.update({"version_id": "coverage-mission-version:us-it-services:2",
                       "prior_version_ref": self.h.mission["id"],
                       "idempotency_key": "coverage-mission:us-it-services:2"})
        ref = params.pop("mission_ref")
        self.v2 = self.h.missions.create_mission(ref, **params)
        self.h.missions.carry_forward_superseded_documents(ref)
        self.h.missions.backfill_document_reviews(ref)
        self.review = self.h.missions.document_reviews(
            self.v2["id"], state="awaiting_human_extraction", limit=1)[0]
        self.actor = self.v2["autonomy"]["automation_principal"]

    def admit(self):
        return self.h.service.admit_suggestions(
            review_id=self.review["review_id"],
            expected_review_hash=content_hash(self.review),
            offset=0, actor_ref=self.actor,
        )

    def test_a_document_that_never_names_the_company_admits_nothing(self):
        with patch.object(type(self.h.service), "document_names_subject",
                          return_value={"checked": True, "names_subject": False,
                                        "matched": []}):
            result = self.admit()
        self.assertEqual(result["status"], "not_attributed")
        self.assertEqual(result["admitted"], [])
        self.assertIn("never names this company", result["reason"])

    def test_a_document_that_names_the_company_is_not_blocked_here(self):
        # The harness transcript says "Accenture"; admission proceeds to its
        # own gates rather than being refused for attribution.
        with patch.object(type(self.h.service), "document_names_subject",
                          return_value={"checked": True, "names_subject": True,
                                        "matched": ["Accenture"]}):
            result = self.admit()
        self.assertNotEqual(result["status"], "not_attributed")

    def test_a_subject_nobody_configured_is_not_treated_as_evidence(self):
        # "checked: false" means there was nothing to check against, which is
        # a configuration gap, not a fact about the document.
        with patch.object(type(self.h.service), "document_names_subject",
                          return_value={"checked": False, "names_subject": False,
                                        "matched": []}):
            result = self.admit()
        self.assertNotEqual(result["status"], "not_attributed")

    def test_a_filing_is_attributed_by_its_accession_without_a_text_check(self):
        # The accession belongs to one CIK; asking the text as well would
        # refuse a filing whose rendering happens not to repeat the name.
        calls = []

        def never(_self, context):
            calls.append(context)
            return {"checked": True, "names_subject": False, "matched": []}

        with patch.object(type(self.h.service), "_document_spec_ref",
                          return_value="annual-report-10k"), \
             patch.object(type(self.h.service), "document_names_subject", never):
            result = self.admit()
        self.assertNotEqual(result["status"], "not_attributed")
        self.assertEqual(calls, [])

    def test_a_human_cannot_reach_this_path_at_all(self):
        from dalton_core.research_verification import ResearchVerificationError

        with self.assertRaises(ResearchVerificationError):
            self.h.service.admit_suggestions(
                review_id=self.review["review_id"],
                expected_review_hash=content_hash(self.review),
                offset=0, actor_ref="human:lumos",
            )


class MetricDiscoveryAttributionTests(unittest.TestCase):
    """P13y: the third pass, which the gate was never added to.

    It writes no claim, which is why it was missed, and why it was worth
    finding: two observations make a requirement, and a requirement is what the
    numeric pass then hunts in this company's own filings.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.h = ExtractionHarness(Path(self.temp.name))
        self.addCleanup(self.h.close)
        params = mission_params(self.h.state)
        params["autonomy"]["may_write"] = list(params["autonomy"]["may_write"]) + ["source_discovery"]
        for item in params["source_plan"]:
            if item["source_ref"] == "source:alphaengine":
                item["status"] = "connected"
        params.update({"version_id": "coverage-mission-version:us-it-services:2",
                       "prior_version_ref": self.h.mission["id"],
                       "idempotency_key": "coverage-mission:us-it-services:2"})
        ref = params.pop("mission_ref")
        self.v2 = self.h.missions.create_mission(ref, **params)
        self.h.missions.carry_forward_superseded_documents(ref)
        self.h.missions.backfill_document_reviews(ref)
        self.review = self.h.missions.document_reviews(
            self.v2["id"], state="awaiting_human_extraction", limit=1)[0]
        self.actor = self.v2["autonomy"]["automation_principal"]

    def discover(self):
        context = self.h.service.context(
            self.review["review_id"], content_hash(self.review), 0, self.actor,
            require_open=False)
        return self.h.service.generate_metric_discovery(
            review_id=self.review["review_id"],
            expected_review_hash=content_hash(self.review),
            offset=0, expected_context_hash=context["content_hash"],
            actor_ref=self.actor,
        )

    def test_a_document_that_never_names_the_company_teaches_nothing(self):
        with patch.object(type(self.h.service), "document_names_subject",
                          return_value={"checked": True, "names_subject": False,
                                        "matched": []}):
            result = self.discover()
        self.assertEqual(result["status"], "not_attributed")
        self.assertEqual(result["recorded"], [])
        self.assertEqual(result["proposals"], [])

    def test_the_refusal_comes_before_the_model_is_paid_for(self):
        # A pass that refuses after the call has already spent the money on a
        # window it was never going to learn from.
        calls = []
        with patch.object(type(self.h.service), "document_names_subject",
                          return_value={"checked": True, "names_subject": False,
                                        "matched": []}), \
             patch.object(type(self.h.service), "_run_secondary",
                          side_effect=lambda *a, **k: calls.append(a) or (None, False)):
            self.discover()
        self.assertEqual(calls, [])

    def test_a_document_that_names_the_company_is_not_blocked_here(self):
        with patch.object(type(self.h.service), "document_names_subject",
                          return_value={"checked": True, "names_subject": True,
                                        "matched": ["Accenture"]}):
            result = self.discover()
        self.assertNotEqual(result["status"], "not_attributed")

    def test_a_subject_nobody_configured_is_not_treated_as_evidence(self):
        with patch.object(type(self.h.service), "document_names_subject",
                          return_value={"checked": False, "names_subject": False,
                                        "matched": []}):
            result = self.discover()
        self.assertNotEqual(result["status"], "not_attributed")

    def test_a_filing_is_attributed_by_its_accession_without_a_text_check(self):
        calls = []

        def never(_self, context):
            calls.append(context)
            return {"checked": True, "names_subject": False, "matched": []}

        with patch.object(type(self.h.service), "_document_spec_ref",
                          return_value="annual-report-10k"), \
             patch.object(type(self.h.service), "document_names_subject", never):
            result = self.discover()
        self.assertNotEqual(result["status"], "not_attributed")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
