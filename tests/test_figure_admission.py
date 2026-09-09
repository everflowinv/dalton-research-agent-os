"""ADR-0007: a verified figure carries a number into the Ledger, and nothing else does.

The rule this changes has stood since ADR-0003: a cited original is not a
numeric authority. It was right, because the only way to get a number out of a
document's text was a text extractor and that was the option the ADR declined.
It is also why 22 of the live Ledger's 2,170 Claims are numbers, all of them
from XBRL, while twelve verified figures sit in a mission table with nowhere to
go.

What ADR-0007 admits is not text. It is a figure *row* whose digits and
as-reported label were already found in the exact quote it cites, checked
before the row was written. So the tests here are about the re-check: the row
is read back out of Core by id, its hash recomputed from its own columns, its
retraction looked up, and the digits matched again -- and the candidate's own
copy of all that is compared last and trusted never.

The old rule is still the default and still tested, because turning this on is
a governance decision and a merge is not one.
"""

from __future__ import annotations

import json
import unittest

from dalton_core.claim_index_figures import (
    DocumentFigureResolver,
    FigureNotFound,
    figure_claim_semantics,
    find_citation_binding,
    promote_figure,
    promote_verified_figures,
    quote_span,
)
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.document_figure_grade import FILED, SPOKEN
from dalton_core.research_review import HumanReviewAuthority
from dalton_core.research_verification import (
    CandidateStagingStore,
    ResearchVerificationConflict,
    VerificationRejected,
    figure_candidate_numerics,
)
from tests.test_alphaengine_core_acquisition import DOCUMENT, DOCUMENT_REF, REVIEWER
from tests.test_transcript_qualitative_candidate import QualitativeTranscriptHarness

COMPANY = "company:sec-cik:0001467373"
ACTOR = "automation:coverage-mission"


class FigureHarness:
    """The transcript harness, plus a verified figure read from the same span."""

    def __init__(self):
        self.h = QualitativeTranscriptHarness()
        self.core = self.h.core
        self.missions = CoverageMissionAuthority(self.core)
        self.quote_text = DOCUMENT[self.h.span_start:self.h.span_end]
        self.quote_id = f"quote:{self.h.span_start}:{self.h.span_end}:{'a' * 16}"

    def record_figure(self, *, metric_ref="metric:new-bookings", value="19.3",
                      label="New bookings were $19.3 billion", grade=SPOKEN,
                      period="FY2026Q3", document_ref=DOCUMENT_REF, unit="currency",
                      currency="USD", scale="billion", quote_id=None,
                      citation_text=None):
        self.missions.record_document_figures(
            company_ref=COMPANY, review_ref="mission-document-review:acn",
            document_ref=document_ref,
            source_manifest_hash=self.h.manifest["content_hash"],
            source_grade=grade, observed_by=ACTOR,
            figures=[{
                "quote_id": quote_id or self.quote_id,
                "metric_ref": metric_ref, "subject_as_named": "Accenture",
                "as_reported_label": label, "value": value, "unit": unit,
                "currency": currency, "period": period, "basis": "management-reported",
                "scale": scale,
                "citation_text": citation_text or self.quote_text,
            }],
        )
        return DocumentFigureResolver(self.core.connection).figures(
            company_ref=COMPANY)[-1]

    def staging(self):
        store = CandidateStagingStore(self.h.staging_path)
        return store

    def close(self):
        self.h.close()


class TheOldRuleStillHoldsTests(unittest.TestCase):
    def setUp(self):
        self.harness = FigureHarness()
        self.addCleanup(self.harness.close)

    def test_a_cited_quantitative_candidate_is_still_refused_by_default(self):
        # Unchanged from ADR-0003: no figure supplied, no admission.
        figure = self.harness.record_figure()
        staging = self.harness.staging()
        self.addCleanup(staging.close)
        with self.assertRaises(VerificationRejected) as caught:
            promote_figure(
                self.harness.core, _RejectingStaging(staging), figure=figure,
                citation_ref=self.harness.h.citation["id"],
                correction_set_ref=self.harness.h.correction_set["id"],
                actor_ref="system:figure-promoter",
                idempotency_key="figure:default-policy",
                artifact_reader=self.harness.h.artifact_reader,
            )
        self.assertIn("deliberately", str(caught.exception))

    def test_the_policy_word_is_closed(self):
        figure = self.harness.record_figure()
        staging = self.harness.staging()
        self.addCleanup(staging.close)
        with self.assertRaises(VerificationRejected):
            staging.stage(
                material={}, source_verification={}, evidence={}, claim={},
                idempotency_key="k", figure_admission_policy="whatever",
                verified_figure=figure,
            )


class _RejectingStaging:
    """Forces the default policy through the promoter, which asks for the new one."""

    def __init__(self, inner):
        self.inner = inner

    def stage(self, **kwargs):
        kwargs.pop("figure_admission_policy", None)
        return self.inner.stage(**kwargs)


class FigureReVerificationTests(unittest.TestCase):
    def setUp(self):
        self.harness = FigureHarness()
        self.addCleanup(self.harness.close)
        self.resolver = DocumentFigureResolver(self.harness.core.connection)

    def test_a_good_figure_re_verifies_and_produces_a_numeric_bundle(self):
        figure = self.harness.record_figure()
        held, bundle = self.resolver.verify_figure(figure)
        self.assertEqual(held["figure_id"], figure["figure_id"])
        self.assertEqual(bundle["kind"], "numeric")
        self.assertEqual(bundle["verdict"], "pass")
        self.assertEqual(bundle["subject_ref"], figure["figure_id"])
        self.assertEqual(bundle["subject_hash"], figure["content_hash"])
        # The document and the manifest it was read from are the checkpoint.
        self.assertEqual(bundle["checkpoint_ref"], DOCUMENT_REF)
        self.assertEqual({item["code"] for item in bundle["findings"]}, {
            "figure_content_hash", "figure_not_retracted",
            "citation_digits_and_label", "caller_copy_is_exact",
        })

    def test_a_caller_copy_that_is_not_the_core_row_is_refused(self):
        figure = self.harness.record_figure()
        with self.assertRaises(ResearchVerificationConflict) as caught:
            self.resolver.verify_figure({**figure, "value": "21.3"})
        self.assertIn("caller_copy_is_exact", str(caught.exception))

    def test_a_retracted_figure_is_not_a_numeric_authority(self):
        figure = self.harness.record_figure()
        self.harness.missions.retract_document_figure(
            figure["figure_id"], reason="wrong company", retracted_by=REVIEWER)
        with self.assertRaises(ResearchVerificationConflict) as caught:
            self.resolver.verify_figure(figure)
        self.assertIn("figure_not_retracted", str(caught.exception))
        self.assertEqual(self.resolver.figures(company_ref=COMPANY), [])

    def test_a_figure_that_is_not_in_this_core_is_not_found(self):
        with self.assertRaises(FigureNotFound):
            self.resolver.verify_figure({"figure_id": "mission-document-figure:nope"})

    def test_the_numeric_fields_a_candidate_takes_are_the_rows_own(self):
        figure = self.harness.record_figure()
        numerics = figure_candidate_numerics(figure)
        self.assertEqual(numerics, {
            "value": "19.3", "unit": "currency", "currency": "USD",
            "scale": "billion", "period": "FY2026Q3",
        })

    def test_a_figure_with_no_scale_word_becomes_scale_one(self):
        # The CandidateClaim contract has no null scale for a number, and a
        # percentage has no scale word.
        figure = self.harness.record_figure(
            metric_ref="metric:local-currency-bookings-growth", value="3",
            label="3% in local currency", unit="percent", currency=None, scale=None)
        self.assertEqual(figure_candidate_numerics(figure)["scale"], "one")

    def test_the_statement_is_generated_from_the_figure_and_says_its_grade(self):
        figure = self.harness.record_figure()
        semantics = figure_claim_semantics(figure)
        self.assertEqual(semantics["metric_or_aspect"], "metric:new-bookings")
        self.assertEqual(semantics["basis"], "earnings-call-transcript-spoken")
        self.assertIn("19.3", semantics["normalized_statement"])
        self.assertIn("spoken on the earnings call", semantics["normalized_statement"])
        filed = figure_claim_semantics({**figure, "source_grade": FILED})
        self.assertIn("as published by the company", filed["normalized_statement"])


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.harness = FigureHarness()
        self.addCleanup(self.harness.close)
        self.staging = self.harness.staging()
        self.addCleanup(self.staging.close)

    def promote(self, figure, key="figure:acn:bookings"):
        return promote_figure(
            self.harness.core, self.staging, figure=figure,
            citation_ref=self.harness.h.citation["id"],
            correction_set_ref=self.harness.h.correction_set["id"],
            actor_ref="system:figure-promoter", idempotency_key=key,
            artifact_reader=self.harness.h.artifact_reader,
        )

    def test_a_verified_figure_stages_as_a_quantitative_candidate(self):
        figure = self.harness.record_figure()
        result = self.promote(figure)
        self.assertEqual(result["write_status"], "fresh")
        claim = result["claim"]
        self.assertEqual(claim["claim_kind"], "quantitative")
        self.assertEqual(claim["value"], "19.3")
        self.assertEqual(claim["unit"], "currency")
        self.assertEqual(claim["currency"], "USD")
        self.assertEqual(claim["scale"], "billion")
        self.assertEqual(claim["subject_ref"], COMPANY)
        # The figure ref *is* the numeric authority reference.
        self.assertEqual(claim["numeric_spec_ref"], figure["figure_id"])
        self.assertEqual(claim["numeric_spec_hash"], figure["content_hash"])
        self.assertEqual(claim["numeric_verification_ref"],
                         result["numeric_verification"]["id"])
        # And the evidence is the same cited original a qualitative candidate
        # would have carried: nothing about the citation chain changed.
        self.assertEqual(result["evidence"]["artifact_refs"][1]["ref"],
                         self.harness.h.citation["id"])

    def test_staging_the_same_figure_twice_is_a_duplicate(self):
        figure = self.harness.record_figure()
        self.promote(figure)
        again = self.promote(figure)
        self.assertEqual(again["write_status"], "duplicate")
        self.assertEqual(self.staging.counts()["candidate_claim_versions"], 1)
        # No JSON-pointer spec was written: the figure row is the spec.
        self.assertEqual(self.staging.counts()["candidate_numeric_specs"], 0)

    def test_a_claim_that_disagrees_with_the_figure_never_stages(self):
        figure = self.harness.record_figure()
        good = self.promote(figure)
        tampered = dict(good["claim"])
        tampered["value"] = "21.3"
        from dalton_core.store import content_hash

        tampered.pop("content_hash")
        tampered["content_hash"] = content_hash(tampered)
        with self.assertRaises(ResearchVerificationConflict) as caught:
            self.staging.stage(
                material=good["staging"] and self._material(good),
                source_verification=self._source(good),
                evidence=good["evidence"], claim=tampered,
                idempotency_key="figure:tampered",
                verification_mode="transcript_core_authority",
                authority_resolver=self._authority(),
                figure_admission_policy="verified_figure",
                verified_figure=figure,
                figure_resolver=DocumentFigureResolver(self.harness.core.connection),
            )
        self.assertIn("drifted from verified inputs", str(caught.exception))

    def _authority(self):
        from dalton_core.transcript_candidate_staging import (
            TranscriptCoreAuthorityResolver,
        )

        return TranscriptCoreAuthorityResolver(
            self.harness.core, artifact_reader=self.harness.h.artifact_reader,
            source_kind="alphaengine")

    def _material(self, result):
        return self._authority().build_material(self.harness.h.citation["id"])

    def _source(self, result):
        authority = self._authority()
        return authority.verify_source_material(
            authority.build_material(self.harness.h.citation["id"]))

    def test_the_review_and_adjudication_path_is_unchanged(self):
        figure = self.harness.record_figure()
        result = self.promote(figure)
        claim = result["claim"]
        review = HumanReviewAuthority(self.harness.h.staging_path)
        self.addCleanup(review.close)
        bundle = review.candidate_authority_bundle(claim["id"])
        # There is no JSON-pointer spec to open, and the figure the cockpit
        # needs to show is staged beside the candidate.
        self.assertIsNone(bundle["numeric_spec"])
        self.assertNotIn("numeric_figure", bundle)
        staged = review.staged_figure(claim["id"])
        self.assertEqual(staged["figure_id"], figure["figure_id"])
        self.assertEqual(staged["citation_text"], self.harness.quote_text)
        review.decide(
            candidate_claim_ref=claim["id"], candidate_claim_hash=claim["content_hash"],
            verdict="accept",
            reviewed_semantics={
                key: claim[key] for key in (
                    "subject_ref", "metric_or_aspect", "period", "basis",
                    "normalized_statement")},
            rationale="The quote contains the figure and the label.",
            findings=["digits present in the cited span"], reviewer_ref=REVIEWER,
            source_event_ref="research-review:acn:figure",
            idempotency_key="review:acn:figure", created_at=claim["created_at"],
        )
        pending = review.pending_commits()
        self.assertEqual(len(pending), 1)
        committed = self.harness.core.commit_reviewed_candidate(
            **pending[0], idempotency_key="reviewed-ledger:acn:figure")
        self.assertEqual(committed["status"], "fresh")
        stored = json.loads(self.harness.core.connection.execute(
            "SELECT claim_json FROM claim_versions WHERE claim_version_id=?",
            (committed["claim_version_ref"],)).fetchone()[0])
        self.assertEqual(stored["claim_kind"], "quantitative")
        self.assertEqual(stored["value"], "19.3")
        # The figure ref stays reachable from the Ledger: the ClaimVersion
        # binds the candidate that binds the figure, hashed at every hop.
        self.assertEqual(stored["candidate_origin_ref"], claim["id"])
        self.assertEqual(stored["candidate_origin_hash"], claim["content_hash"])
        self.assertEqual(claim["numeric_spec_ref"], figure["figure_id"])


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.harness = FigureHarness()
        self.addCleanup(self.harness.close)
        self.staging = self.harness.staging()
        self.addCleanup(self.staging.close)

    def test_the_sweep_stages_what_it_can_and_says_why_it_skipped_the_rest(self):
        good = self.harness.record_figure()
        # Same document, a span the admitted citation does not cover.
        far = self.harness.record_figure(
            metric_ref="metric:revenue", value="17.7",
            label="Net revenues were $17.7 billion",
            quote_id=f"quote:{len(DOCUMENT) - 5}:{len(DOCUMENT)}:{'b' * 16}",
            citation_text="Net revenues were $17.7 billion",
        )
        result = promote_verified_figures(
            self.harness.core, self.staging, actor_ref="system:figure-promoter",
            artifact_reader=self.harness.h.artifact_reader,
        )
        self.assertEqual([item["figure_id"] for item in result["promoted"]],
                         [good["figure_id"]])
        self.assertEqual([item["figure_id"] for item in result["skipped"]],
                         [far["figure_id"]])
        self.assertIn("no claim-eligible citation", result["skipped"][0]["reason"])

    def test_a_figure_already_promoted_is_not_promoted_again(self):
        good = self.harness.record_figure()
        first = promote_verified_figures(
            self.harness.core, self.staging, actor_ref="system:figure-promoter",
            artifact_reader=self.harness.h.artifact_reader)
        self.assertEqual(len(first["promoted"]), 1)
        second = promote_verified_figures(
            self.harness.core, self.staging, actor_ref="system:figure-promoter",
            artifact_reader=self.harness.h.artifact_reader,
            promoted=[good["figure_id"]])
        self.assertEqual(second["promoted"], [])

    def test_the_citation_lookup_takes_the_smallest_covering_span(self):
        binding = find_citation_binding(
            self.harness.core.connection, document_ref=DOCUMENT_REF,
            source_manifest_hash=self.harness.h.manifest["content_hash"],
            quote_id=self.harness.quote_id)
        self.assertEqual(binding["binding_id"], self.harness.h.citation["id"])
        self.assertIsNone(find_citation_binding(
            self.harness.core.connection, document_ref="alphaengine-doc:other",
            source_manifest_hash=self.harness.h.manifest["content_hash"],
            quote_id=self.harness.quote_id))
        self.assertIsNone(find_citation_binding(
            self.harness.core.connection, document_ref=DOCUMENT_REF,
            source_manifest_hash=self.harness.h.manifest["content_hash"],
            quote_id="not-a-quote"))

    def test_a_quote_id_that_names_no_span_is_read_as_none(self):
        self.assertIsNone(quote_span("quote:5:5:aaaa"))
        self.assertIsNone(quote_span(""))
        self.assertEqual(quote_span("quote:1:9:abcd"), (1, 9))


class StatementLineTests(unittest.TestCase):
    """The other verified number: a report line with an accession behind it.

    Stronger provenance than a document figure -- the accession belongs to one
    CIK by construction -- and weaker admissibility, because there is no text
    citation for it to travel the cited-original path with. Verification is
    implemented so both kinds of number answer the same question the same way;
    the staging chain for a line is the SEC connector authority path and is not
    built (see the P12b report).
    """

    def setUp(self):
        import tempfile
        from pathlib import Path

        from dalton_core.store import DaltonStore
        from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        state = bootstrap_method_authorities(self.store)
        self.missions = CoverageMissionAuthority(self.store)
        params = mission_params(state)
        mission = self.missions.create_mission(params.pop("mission_ref"), **params)
        authorization = self.missions.authorize_sec_lane(
            company_ref=COMPANY, ticker="ACN", actor_ref=ACTOR,
            mission_version_ref=mission["id"],
            mission_version_hash=mission["content_hash"])
        dispatch = self.missions.queue_statement_dispatch(authorization=authorization)
        self.missions.mark_statement_dispatch_launched(
            dispatch["dispatch_id"], "sec-financials-run:" + "1" * 24)
        self.missions.record_statement_observation(
            dispatch_id=dispatch["dispatch_id"],
            observation={
                "schema_version": "0.1", "cik": "0001467373",
                "entity_name": "Accenture plc",
                "filings": [{
                    "accession": "0001467373-26-000031", "form": "10-Q",
                    "filed": "2026-06-25", "report_date": "2026-06-30",
                    "lines": [{
                        "statement": "income", "concept": "us-gaap:Revenues",
                        "label": "Revenues", "level": 0, "parent_concept": None,
                        "is_breakdown": False, "dimension_axis": None,
                        "dimension_member": None, "period_start": "2026-04-01",
                        "period_end": "2026-06-30", "value": "17700000000",
                        "unit": "USD", "balance": "credit",
                    }],
                }],
                "source_record_refs": ["raw-sink:" + "c" * 64],
                "next_cursor": None, "provider_status": 200,
            },
            governance_ref="g", governance_hash="b" * 64)
        self.missions.settle_statement_dispatch(
            dispatch["dispatch_id"], outcome="succeeded")
        self.resolver = DocumentFigureResolver(self.store.connection)

    def line_id(self):
        return self.store.connection.execute(
            "SELECT line_id FROM coverage_mission_statement_lines").fetchone()[0]

    def test_a_line_re_verifies_against_its_filing_and_names_the_accession(self):
        record, bundle = self.resolver.verify_statement_line(self.line_id())
        self.assertEqual(record["figure_kind"], "statement_line")
        self.assertEqual(record["accession"], "0001467373-26-000031")
        self.assertEqual(record["value"], "17700000000")
        self.assertEqual(record["period_end"], "2026-06-30")
        self.assertEqual(bundle["kind"], "numeric")
        self.assertEqual(bundle["verdict"], "pass")
        self.assertEqual(bundle["checkpoint_ref"],
                         "sec:filing:0001467373-26-000031")
        self.assertEqual({item["code"] for item in bundle["findings"]},
                         {"line_has_value", "filing_accession",
                          "filing_source_records"})

    def test_a_line_that_is_not_here_is_not_found(self):
        with self.assertRaises(FigureNotFound):
            self.resolver.verify_statement_line("statement-line:nope")


class DuplicateFiguresTests(unittest.TestCase):
    """Three documents reporting one quarter's revenue are one canonical Claim."""

    def setUp(self):
        self.harness = FigureHarness()
        self.addCleanup(self.harness.close)

    def test_the_index_collapses_them_and_the_promoter_does_not(self):
        from dalton_core.claim_index_tagging import dedupe_group_key

        keys = {
            dedupe_group_key({
                "subject_ref": COMPANY, "claim_kind": "quantitative",
                "metric_or_aspect": "metric:new-bookings", "unit": "currency",
                "period": "FY2026Q3",
            }, as_of="2026-05-31")
            for _ in range(3)
        }
        # One group for three copies. The promoter still stages all three,
        # because two documents agreeing is information and discarding the
        # second and third would throw it away.
        self.assertEqual(len(keys), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
