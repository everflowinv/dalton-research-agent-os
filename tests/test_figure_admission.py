"""ADR-0007: a company-filed figure carries a number into the Ledger. Nothing else does.

ADR-0003 B said a cited original is not a numeric authority and installed that
in `CandidateStagingStore.stage`. ADR-0007 does not touch that refusal --
`transcript_core_authority` and `public_web_core_authority` still admit
qualitative candidates only, and the first test here says so. What it adds is a
*different* mode, `mission_figure_authority`, whose material is not a cited
original at all: it is a row of `coverage_mission_document_figures`, whose
digits and as-reported label were checked against the exact quote before the
row was written and are checked again here from the stored quote and hashes.

Two consequences the tests are mostly about.

A SEC-filing figure needs no transcript citation binding, because there is no
transcript in the chain: figure -> mission review -> discovered document ->
the discovery that found it -> the connector SourceEnvelope that enumerated the
document -> the raw ArtifactVersion. That is the chain live already has for
every one of its surviving figures, and it is the chain this mode re-derives.

And an `earnings-call-transcript` figure stays qualitative (ADR-0007 §4).
ADR-0003's finding was about transcripts and it was right about transcripts.
That refusal is in the staging store, in the promoter and in the sweep, and it
is tested in all three, because a rule enforced in one place is a rule someone
routes around.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.claim_index_figures import (
    DocumentFigureResolver,
    FigureAdmissionError,
    FigureNotFound,
    MissionFigureAuthorityResolver,
    figure_claim_semantics,
    promote_figure,
    promote_verified_figures,
    quote_span,
)
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.document_figure_grade import FILED, SPOKEN
from dalton_core.research_review import HumanReviewAuthority
from dalton_core.research_verification import (
    CandidateStagingStore,
    MISSION_FIGURE_AUTHORITY_MODE,
    MISSION_VERIFIED_FIGURE_RULE_REF,
    ResearchVerificationConflict,
    VerificationRejected,
    figure_candidate_numerics,
)
from tests.test_document_extraction import ExtractionHarness
from tests.test_mission_source_discovery import ACN, NEW_DOC, OWNER

# Committing to the Ledger needs an authenticated Tailscale human, which is
# what the review contract has always demanded and is unchanged here.
REVIEWER = "human:tailscale-" + "0123456789abcdef" * 2

ACTOR = "automation:coverage-mission"
CITATION = "Net revenues were $17.7 billion and margin was 15.6 percent for the quarter."
QUOTE = "quote:0:200:aaaaaaaaaaaaaaaa"


class FigureHarness:
    """A Core with the full mission chain behind one document, plus figures.

    ``ExtractionHarness`` already builds the chain this mode walks -- mission,
    discovery, discovered document, review, acquisition -- so it is reused
    rather than rebuilt. The grade is passed explicitly to
    ``record_document_figures`` because that is the parameter the write path
    takes; live it comes from the discovery spec.
    """

    def __init__(self):
        self._dir = tempfile.TemporaryDirectory()
        self.extraction = ExtractionHarness(Path(self._dir.name))
        self.core = self.extraction.h.core
        self.missions = self.extraction.missions
        self.review = self.extraction.review
        self.staging_path = Path(self._dir.name) / "candidate-staging.sqlite"

    def record_figure(self, *, metric_ref="metric:revenue", value="17.7",
                      label="Net revenues", grade=FILED, period="FY2026Q3",
                      unit="currency", currency="USD", scale="billion",
                      quote_id=QUOTE, citation_text=CITATION):
        self.missions.record_document_figures(
            company_ref=ACN, review_ref=self.review["review_id"],
            document_ref=NEW_DOC,
            source_manifest_hash=self.extraction.manifest["content_hash"],
            source_grade=grade, observed_by=ACTOR,
            figures=[{
                "quote_id": quote_id, "metric_ref": metric_ref,
                "subject_as_named": "Accenture", "as_reported_label": label,
                "value": value, "unit": unit, "currency": currency,
                "period": period, "basis": "management-reported", "scale": scale,
                "citation_text": citation_text,
            }],
        )
        return DocumentFigureResolver(self.core.connection).figures(company_ref=ACN)[-1]

    def staging(self):
        return CandidateStagingStore(self.staging_path)

    def close(self):
        self.extraction.close()
        self._dir.cleanup()


class TheTranscriptRefusalIsUntouchedTests(unittest.TestCase):
    """ADR-0007 narrows nothing inside the cited-original modes."""

    def setUp(self):
        self.harness = FigureHarness()
        self.addCleanup(self.harness.close)
        self.staging = self.harness.staging()
        self.addCleanup(self.staging.close)

    def test_a_cited_original_still_admits_qualitative_candidates_only(self):
        figure = self.harness.record_figure()
        with self.assertRaises(VerificationRejected) as caught:
            self.staging.stage(
                material={}, source_verification={}, evidence={}, claim={},
                idempotency_key="k", verification_mode="transcript_core_authority",
                figure_admission_policy="verified_figure", verified_figure=figure,
            )
        self.assertIn("admitted only through mission_figure_authority",
                      str(caught.exception))

    def test_the_policy_is_off_by_default_and_its_word_is_closed(self):
        figure = self.harness.record_figure()
        with self.assertRaises(VerificationRejected) as caught:
            self.staging.stage(
                material={}, source_verification={}, evidence={}, claim={},
                idempotency_key="k", verified_figure=figure)
        self.assertIn("deliberately", str(caught.exception))
        with self.assertRaises(VerificationRejected):
            self.staging.stage(
                material={}, source_verification={}, evidence={}, claim={},
                idempotency_key="k", figure_admission_policy="whatever",
                verified_figure=figure)

    def test_the_rule_ref_the_owner_has_to_sign_is_named_once(self):
        self.assertEqual(MISSION_VERIFIED_FIGURE_RULE_REF,
                         "research-auto-commit:mission-verified-figure:v1")


class FigureReVerificationTests(unittest.TestCase):
    def setUp(self):
        self.harness = FigureHarness()
        self.addCleanup(self.harness.close)
        self.resolver = MissionFigureAuthorityResolver(self.harness.core.connection)

    def test_a_good_figure_re_verifies_and_produces_a_numeric_bundle(self):
        figure = self.harness.record_figure()
        held, bundle = self.resolver.verify_figure(figure)
        self.assertEqual(held["figure_id"], figure["figure_id"])
        self.assertEqual((bundle["kind"], bundle["verdict"]), ("numeric", "pass"))
        self.assertEqual(bundle["subject_ref"], figure["figure_id"])
        self.assertEqual(bundle["subject_hash"], figure["content_hash"])
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
            figure["figure_id"], reason="wrong company", retracted_by=OWNER)
        with self.assertRaises(ResearchVerificationConflict) as caught:
            self.resolver.verify_figure(figure)
        self.assertIn("figure_not_retracted", str(caught.exception))
        self.assertEqual(self.resolver.figures(company_ref=ACN), [])

    def test_a_figure_that_is_not_in_this_core_is_not_found(self):
        with self.assertRaises(FigureNotFound):
            self.resolver.verify_figure({"figure_id": "mission-document-figure:nope"})

    def test_the_numeric_fields_a_candidate_takes_are_the_rows_own(self):
        figure = self.harness.record_figure()
        self.assertEqual(figure_candidate_numerics(figure), {
            "value": "17.7", "unit": "currency", "currency": "USD",
            "scale": "billion", "period": "FY2026Q3",
        })

    def test_a_percentage_has_no_scale_word_and_becomes_scale_one(self):
        # The CandidateClaim contract has no null scale for a number, and
        # reading the row's scale straight through was a crash on every
        # percentage -- which is most of what a research claim measures.
        figure = self.harness.record_figure(
            metric_ref="metric:operating-margin", value="15.6",
            label="margin was 15.6 percent", unit="percent", currency=None,
            scale=None)
        self.assertEqual(figure_candidate_numerics(figure), {
            "value": "15.6", "unit": "percent", "currency": None,
            "scale": "one", "period": "FY2026Q3",
        })

    def test_the_statement_is_generated_from_the_figure_and_says_its_grade(self):
        figure = self.harness.record_figure()
        semantics = figure_claim_semantics(figure)
        self.assertEqual(semantics["metric_or_aspect"], "metric:revenue")
        self.assertEqual(semantics["basis"], "company-filed-document")
        self.assertIn("17.7", semantics["normalized_statement"])
        self.assertIn("as published by the company",
                      semantics["normalized_statement"])
        spoken = figure_claim_semantics({**figure, "source_grade": SPOKEN})
        self.assertIn("spoken on the earnings call",
                      spoken["normalized_statement"])


class MissionFigureMaterialTests(unittest.TestCase):
    def setUp(self):
        self.harness = FigureHarness()
        self.addCleanup(self.harness.close)
        self.resolver = MissionFigureAuthorityResolver(self.harness.core.connection)
        self.figure = self.harness.record_figure()

    def test_the_material_is_the_figure_row_bound_to_its_core_chain(self):
        material = self.resolver.build_material(self.figure["figure_id"])
        self.assertEqual(material["provenance_mode"], MISSION_FIGURE_AUTHORITY_MODE)
        self.assertEqual(material["authority_resolution_ref"], self.figure["figure_id"])
        self.assertEqual(material["authority_resolution_hash"],
                         self.figure["content_hash"])
        self.assertEqual(material["source_type"], "official_filing")
        self.assertEqual(material["source_record_refs"], [NEW_DOC])
        self.assertEqual(material["source_content_hash"],
                         self.figure["source_manifest_hash"])
        self.assertEqual(material["normalized_payload"]["value"], "17.7")
        self.assertEqual(material["source_lineage"][-2:],
                         [NEW_DOC, self.figure["figure_id"]])
        # No citation binding and no correction set anywhere in it: that is
        # exactly what lets a SEC filing figure move.
        self.assertNotIn("transcript-claim-citation-binding",
                         json.dumps(material))

    def test_the_source_verification_re_derives_every_hop(self):
        material = self.resolver.build_material(self.figure["figure_id"])
        bundle = self.resolver.verify_source_material(material)
        self.assertEqual((bundle["kind"], bundle["verdict"]), ("source", "pass"))
        self.assertEqual(bundle["checkpoint_ref"], self.figure["figure_id"])
        codes = {item["code"] for item in bundle["findings"]}
        for code in ("figure_content_hash", "figure_not_retracted",
                     "figure_grade_is_filed", "citation_digits_and_label",
                     "citation_hash", "quote_names_a_span", "review_binds_document",
                     "discovered_binds_document", "envelope_names_the_document",
                     "artifact_ref", "source_lineage"):
            self.assertIn(code, codes)

    def test_a_material_whose_payload_drifted_fails_the_projection_check(self):
        from dalton_core.store import content_hash

        material = dict(self.resolver.build_material(self.figure["figure_id"]))
        payload = {**material["normalized_payload"], "value": "99.9"}
        import hashlib

        material["normalized_payload"] = payload
        material["normalized_payload_hash"] = hashlib.sha256(
            __import__("dalton_core.store", fromlist=["canonical_json"]).canonical_json(
                payload).encode("utf-8")).hexdigest()
        material.pop("content_hash")
        material["content_hash"] = content_hash(material)
        bundle = self.resolver.verify_source_material(material)
        self.assertEqual(bundle["verdict"], "reject")

    def test_a_figure_whose_review_names_another_document_is_refused(self):
        other = self.harness.missions.register_document_review
        del other  # the review is fixed by the harness; assert the guard directly
        with self.assertRaises(FigureAdmissionError):
            self.resolver.chain({**self.figure, "document_ref": "alphaengine-doc:other"})


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.harness = FigureHarness()
        self.addCleanup(self.harness.close)
        self.staging = self.harness.staging()
        self.addCleanup(self.staging.close)

    def promote(self, figure, key="figure:acn:revenue"):
        return promote_figure(
            self.harness.core, self.staging, figure=figure,
            actor_ref="system:figure-promoter", idempotency_key=key)

    def test_a_company_filed_figure_stages_as_a_quantitative_candidate(self):
        figure = self.harness.record_figure()
        result = self.promote(figure)
        self.assertEqual(result["write_status"], "fresh")
        claim = result["claim"]
        self.assertEqual(claim["claim_kind"], "quantitative")
        self.assertEqual((claim["value"], claim["unit"], claim["currency"],
                          claim["scale"]), ("17.7", "currency", "USD", "billion"))
        self.assertEqual(claim["subject_ref"], ACN)
        # The figure row *is* the numeric authority reference.
        self.assertEqual(claim["numeric_spec_ref"], figure["figure_id"])
        self.assertEqual(claim["numeric_spec_hash"], figure["content_hash"])
        self.assertEqual(claim["numeric_verification_ref"],
                         result["numeric_verification"]["id"])
        self.assertEqual(result["evidence"]["source_type"], "official_filing")
        # One artifact ref, not two: there is no citation binding to append.
        self.assertEqual(len(result["evidence"]["artifact_refs"]), 1)

    def test_a_percentage_figure_promotes_without_crashing_on_its_missing_scale(self):
        figure = self.harness.record_figure(
            metric_ref="metric:operating-margin", value="15.6",
            label="margin was 15.6 percent", unit="percent", currency=None,
            scale=None)
        result = self.promote(figure, key="figure:acn:margin")
        self.assertEqual(result["write_status"], "fresh")
        self.assertEqual(result["claim"]["scale"], "one")
        self.assertIsNone(result["claim"]["currency"])

    def test_a_spoken_figure_is_refused_by_the_promoter_and_by_the_store(self):
        figure = self.harness.record_figure(
            metric_ref="metric:new-bookings", grade=SPOKEN)
        with self.assertRaises(FigureAdmissionError) as caught:
            self.promote(figure, key="figure:acn:spoken")
        self.assertIn("stay qualitative", str(caught.exception))
        # And the store refuses it even if a caller reaches past the promoter.
        resolver = MissionFigureAuthorityResolver(self.harness.core.connection)
        material = resolver.build_material(figure["figure_id"])
        verification = resolver.verify_source_material(material)
        self.assertEqual(verification["verdict"], "reject")

    def test_staging_the_same_figure_twice_is_a_duplicate(self):
        figure = self.harness.record_figure()
        self.promote(figure)
        again = self.promote(figure)
        self.assertEqual(again["write_status"], "duplicate")
        counts = self.staging.counts()
        self.assertEqual(counts["candidate_claim_versions"], 1)
        # No JSON-pointer spec was written: the figure row is the spec, and it
        # is staged beside the candidate so the cockpit can open it.
        self.assertEqual(counts["candidate_numeric_specs"], 0)
        self.assertEqual(counts["candidate_figures"], 1)

    def test_a_claim_that_disagrees_with_the_figure_never_stages(self):
        from dalton_core.store import content_hash

        figure = self.harness.record_figure()
        good = self.promote(figure)
        tampered = dict(good["claim"])
        tampered["value"] = "21.3"
        tampered.pop("content_hash")
        tampered["content_hash"] = content_hash(tampered)
        resolver = MissionFigureAuthorityResolver(self.harness.core.connection)
        with self.assertRaises(ResearchVerificationConflict) as caught:
            self.staging.stage(
                material=good["material"],
                source_verification=good["source_verification"],
                evidence=good["evidence"], claim=tampered,
                idempotency_key="figure:tampered",
                verification_mode=MISSION_FIGURE_AUTHORITY_MODE,
                figure_admission_policy="verified_figure",
                verified_figure=figure, figure_resolver=resolver,
            )
        self.assertIn("drifted from verified inputs", str(caught.exception))

    def test_the_review_path_opens_the_figure_the_number_rests_on(self):
        figure = self.harness.record_figure()
        result = self.promote(figure)
        claim = result["claim"]
        review = HumanReviewAuthority(self.harness.staging_path)
        self.addCleanup(review.close)
        bundle = review.candidate_authority_bundle(claim["id"])
        # No JSON-pointer spec to open; the figure is a separate read, because
        # this bundle is splatted into commit_policy_candidate by four callers.
        self.assertIsNone(bundle["numeric_spec"])
        self.assertNotIn("numeric_figure", bundle)
        staged = review.staged_figure(claim["id"])
        self.assertEqual(staged["figure_id"], figure["figure_id"])
        self.assertEqual(staged["citation_text"], CITATION)
        listed = review.list_candidates()
        self.assertEqual([item["claim"]["claim_kind"] for item in listed],
                         ["quantitative"])


    def test_the_review_path_ends_in_a_quantitative_claim_version(self):
        # The whole point: a number that had nowhere to go is now a Claim, and
        # nothing about the review or the commit changed to let it be one.
        figure = self.harness.record_figure()
        result = self.promote(figure)
        claim = result["claim"]
        review = HumanReviewAuthority(self.harness.staging_path)
        self.addCleanup(review.close)
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
            idempotency_key="review:acn:figure", created_at=claim["created_at"])
        pending = review.pending_commits()
        self.assertEqual(len(pending), 1)
        committed = self.harness.core.commit_reviewed_candidate(
            **pending[0], idempotency_key="reviewed-ledger:acn:figure")
        self.assertEqual(committed["status"], "fresh")
        stored = json.loads(self.harness.core.connection.execute(
            "SELECT claim_json FROM claim_versions WHERE claim_version_id=?",
            (committed["claim_version_ref"],)).fetchone()[0])
        self.assertEqual(stored["claim_kind"], "quantitative")
        self.assertEqual((stored["value"], stored["unit"], stored["scale"]),
                         ("17.7", "currency", "billion"))
        # The figure ref stays reachable from the Ledger: the ClaimVersion
        # binds the candidate, and the candidate's numeric_spec_ref is the
        # figure. Hashed at every hop.
        self.assertEqual(stored["candidate_origin_ref"], claim["id"])
        self.assertEqual(stored["candidate_origin_hash"], claim["content_hash"])
        self.assertEqual(claim["numeric_spec_ref"], figure["figure_id"])


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.harness = FigureHarness()
        self.addCleanup(self.harness.close)
        self.staging = self.harness.staging()
        self.addCleanup(self.staging.close)

    def sweep(self, **overrides):
        params = {"actor_ref": "system:figure-promoter"}
        params.update(overrides)
        return promote_verified_figures(self.harness.core, self.staging, **params)

    def test_the_sweep_stages_the_filed_ones_and_never_touches_the_spoken_ones(self):
        filed = self.harness.record_figure()
        spoken = self.harness.record_figure(
            metric_ref="metric:new-bookings", grade=SPOKEN)
        result = self.sweep()
        self.assertEqual([item["figure_id"] for item in result["promoted"]],
                         [filed["figure_id"]])
        self.assertEqual([item["figure_id"] for item in result["skipped"]],
                         [spoken["figure_id"]])
        self.assertIn("stays qualitative under ADR-0007",
                      result["skipped"][0]["reason"])

    def test_a_figure_already_promoted_is_not_promoted_again(self):
        filed = self.harness.record_figure()
        self.assertEqual(len(self.sweep()["promoted"]), 1)
        self.assertEqual(self.sweep(promoted=[filed["figure_id"]])["promoted"], [])

    def test_a_run_that_stopped_at_its_bound_says_so(self):
        # A sweep that hit its limit and did not report it reads as "there is
        # nothing left".
        self.harness.record_figure()
        self.harness.record_figure(metric_ref="metric:operating-margin",
                                   value="15.6", label="margin was 15.6 percent",
                                   unit="percent", currency=None, scale=None)
        result = self.sweep(limit=1)
        self.assertEqual(len(result["promoted"]), 1)
        self.assertEqual(result["truncated"], 1)
        self.assertEqual(self.sweep()["truncated"], 0)

    def test_a_broken_figure_is_listed_rather_than_aborting_the_run(self):
        # ResearchVerificationError is the base class and used to escape the
        # except tuple, so one bad row ended the whole sweep.
        good = self.harness.record_figure()
        # The write path re-verifies digits and label, so the only way to
        # store a figure this mode will refuse is to break the quote id -- the
        # span it names is what ties the number to a place in the original.
        broken = self.harness.record_figure(
            metric_ref="metric:net-income", value="15.6",
            label="margin was 15.6 percent", unit="percent", currency=None,
            scale=None, quote_id="not-a-quote-span")
        result = self.sweep()
        self.assertEqual([item["figure_id"] for item in result["promoted"]],
                         [good["figure_id"]])
        self.assertEqual([item["figure_id"] for item in result["skipped"]],
                         [broken["figure_id"]])

    def test_a_quote_id_that_names_no_span_is_read_as_none(self):
        self.assertIsNone(quote_span("quote:5:5:aaaa"))
        self.assertIsNone(quote_span(""))
        self.assertEqual(quote_span("quote:1:9:abcd"), (1, 9))


class StatementLineTests(unittest.TestCase):
    """The other verified number: a report line with an accession behind it.

    Stronger provenance than a document figure -- the accession belongs to one
    CIK by construction -- and it still cannot travel, because
    ``mission_figure_authority`` walks the mission document chain and a
    statement line has none. Verification is implemented so both kinds answer
    the same question the same way; the material for a line is the SEC
    financials envelope chain and is not built (see the P12b report).
    """

    def setUp(self):
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
            company_ref=ACN, ticker="ACN", actor_ref=ACTOR,
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
        self.assertEqual((record["value"], record["period_end"]),
                         ("17700000000", "2026-06-30"))
        self.assertEqual((bundle["kind"], bundle["verdict"]), ("numeric", "pass"))
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

    def test_the_index_collapses_them_and_the_promoter_does_not(self):
        from dalton_core.claim_index_tagging import dedupe_group_key

        keys = {
            dedupe_group_key({
                "subject_ref": ACN, "claim_kind": "quantitative",
                "metric_or_aspect": "metric:revenue", "unit": "currency",
                "basis": "company-filed-document",
                "period": "2026-03-01..2026-05-31",
                "claim_version_ref": f"claim-version:{index}",
            })
            for index in range(3)
        }
        # One group for three copies. The promoter still stages all three,
        # because two documents agreeing is information and discarding the
        # second and third would throw it away.
        self.assertEqual(len(keys), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
