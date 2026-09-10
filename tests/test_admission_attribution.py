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

    def test_missing_earnings_issuer_metadata_refuses_before_qualitative_model(self):
        calls = []
        with patch.object(type(self.h.service), "_suggestions",
                          side_effect=lambda *a, **k: calls.append(a)):
            result = self.admit()
        self.assertEqual(result["status"], "not_attributed")
        self.assertEqual(calls, [])

    def test_missing_earnings_issuer_metadata_refuses_before_numeric_model(self):
        context = self.h.service.context(
            self.review["review_id"], content_hash(self.review), 0, self.actor,
            require_open=False)
        calls = []
        with patch.object(type(self.h.service), "numeric_slots",
                          side_effect=lambda *a, **k: calls.append(a)):
            result = self.h.service.generate_numeric(
                review_id=self.review["review_id"],
                expected_review_hash=content_hash(self.review), offset=0,
                expected_context_hash=context["content_hash"], actor_ref=self.actor)
        self.assertEqual(result["status"], "not_attributed")
        self.assertEqual(calls, [])

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

    def test_missing_earnings_issuer_metadata_refuses_before_metric_model(self):
        calls = []
        with patch.object(type(self.h.service), "_run_secondary",
                          side_effect=lambda *a, **k: calls.append(a) or (None, False)):
            result = self.discover()
        self.assertEqual(result["status"], "not_attributed")
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


class MultiSubjectAdmissionTests(unittest.TestCase):
    """P10x/S1: one window, a two-subject plan, through the real admission loop.

    The unit tests decide *who* a statement is about.  This one asserts what
    the loop does with that answer: two candidates rather than one, distinct
    pair keys so the second does not collide with the first, and a replay that
    is a duplicate rather than a second Claim.
    """

    def setUp(self):
        from dalton_core.extraction_backlog import DocumentProvenanceStore
        from dalton_core.research_review import HumanReviewAuthority
        from dalton_core.research_verification import CandidateStagingStore

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
        staging = str(Path(self.temp.name) / "staging.sqlite")
        self.h.writer._candidate_staging = CandidateStagingStore(staging)
        self.h.writer._candidate_review = HumanReviewAuthority(staging)
        self.addCleanup(self.h.writer._candidate_staging.close)
        # Point the harness at the v2 review, which is the active one, and
        # generate the one fixture suggestion this window will admit.
        self.h.params = {"review_id": self.review["review_id"],
                         "expected_review_hash": content_hash(self.review),
                         "offset": 0, "actor_ref": self.actor}
        self.h.enable_fixture()
        self.h.service.generate(**self.h.params,
                                expected_context_hash=self.h.context()["content_hash"])
        self._policy_with_document_rule()
        self.store = DocumentProvenanceStore(self.h.h.core.connection)
        self.store.record({
            "document_ref": self.review["document_ref"],
            "source_ref": "source:alphaengine",
            "spec_ref": "earnings-call-transcripts",
            "provenance_tier": "management",
            "title": "Accenture Q2 2026 Earnings Conference Call",
            "named_companies": ["Accenture"],
            "metadata_seen": True,
        })
        self.h.service._subject_cache = {}
        self.members = [m["company_ref"] for m in self.v2["universe"]]

    def _policy_with_document_rule(self):
        from dalton_core.research_auto_commit import DOCUMENT_QUALITATIVE_RULE_REF
        from tests.test_document_extraction import OWNER

        core = self.h.h.core
        core.create_policy(
            {**core.active_policy_version().policy,
             "research_candidate_auto_commit": {
                 "enabled": True, "max_records": 20,
                 "rules": [DOCUMENT_QUALITATIVE_RULE_REF]}},
            policy_version_id="policy:synthetic-document-qualitative:2", actor_ref=OWNER,
            change_reason="ADR-0005 fixture: list the mission document qualitative rule",
        )
        self.rule_ref = DOCUMENT_QUALITATIVE_RULE_REF

    def _plan(self, subjects):
        return patch.object(type(self.h.service), "statement_subjects",
                            return_value=lambda statement: {"subjects": list(subjects),
                                                            "basis": "test"})

    def admit(self):
        review = self.h.missions.document_review(self.review["review_id"])
        return self.h.service.admit_suggestions(
            review_id=review["review_id"], expected_review_hash=content_hash(review),
            offset=0, actor_ref=self.actor)

    def test_two_subjects_mint_two_candidates_with_distinct_keys(self):
        primary, other = self.review["company_ref"], next(
            ref for ref in self.members if ref != self.review["company_ref"])
        before = self.h.counts()
        with self._plan([primary, other]):
            result = self.admit()
        self.assertEqual(result["status"], "admitted", result)
        admitted = result["admitted"]
        self.assertEqual([entry["subject_ref"] for entry in admitted], [primary, other])
        self.assertEqual([entry["status"] for entry in admitted], ["admitted", "admitted"])
        # Distinct candidate identity, or the second collides with the first.
        self.assertEqual(len({entry["candidate_claim_ref"] for entry in admitted}), 2)
        self.assertEqual(len({entry["claim_version_ref"] for entry in admitted}), 2)
        after = self.h.counts()
        self.assertEqual(after["claim_versions"] - before["claim_versions"], 2)
        self.assertEqual(after["evidence_versions"] - before["evidence_versions"], 2)
        # One quote, so one correction set shared by both.
        self.assertEqual(
            after["transcript_correction_set_versions"]
            - before["transcript_correction_set_versions"], 1)
        subjects = {self.h.h.core.get_claim(entry["claim_version_ref"])["claim"]["subject_ref"]
                    for entry in admitted}
        self.assertEqual(subjects, {primary, other})

    def test_replaying_the_same_two_subject_window_writes_nothing_new(self):
        primary, other = self.review["company_ref"], next(
            ref for ref in self.members if ref != self.review["company_ref"])
        with self._plan([primary, other]):
            first = self.admit()
            settled = self.h.counts()
            again = self.admit()
        self.assertEqual([entry["status"] for entry in again["admitted"]],
                         ["duplicate", "duplicate"])
        self.assertEqual([entry["candidate_claim_ref"] for entry in again["admitted"]],
                         [entry["candidate_claim_ref"] for entry in first["admitted"]])
        self.assertEqual(self.h.counts(), settled)

    def test_the_single_subject_key_is_the_one_it_has_always_been(self):
        # B3: the review's own company must keep byte-identical identity, or
        # every Claim already admitted on live would be minted a second time.
        primary = self.review["company_ref"]
        with self._plan([primary]):
            alone = self.admit()
        settled = self.h.counts()
        with self._plan([primary, next(r for r in self.members if r != primary)]):
            widened = self.admit()
        self.assertEqual(widened["admitted"][0]["candidate_claim_ref"],
                         alone["admitted"][0]["candidate_claim_ref"])
        self.assertEqual(widened["admitted"][0]["status"], "duplicate")
        self.assertEqual(widened["admitted"][1]["status"], "admitted")
        after = self.h.counts()
        self.assertEqual(after["claim_versions"] - settled["claim_versions"], 1)
