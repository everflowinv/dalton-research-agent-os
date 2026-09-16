"""WP-F: the deterministic numbers this install already holds reach ``claim_versions``.

C2 built the promoter and stopped at the two doors it could not open: the
mission verified-figure rule was defined but never listed in ``KNOWN_RULE_REFS``
or evaluated, and a filed XBRL statement line had no staging chain at all.  Both
are here now, and both are governed: a candidate reaches the Ledger only when
the *active signed policy* names the rule that admits it, and only when this
Core rebuilds every byte of the candidate out of its own rows first.

The fixture rows are the live IBM 10-Q ingest (``0000051143-26-000078``, period
2025-01-01..2025-06-30), which is where the live install's revenue, gross
profit and margin actually come from.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.quantitative_claim_promotion import (
    QuantitativeClaimPromotionError,
    SecStatementLineAuthorityResolver,
    build_statement_line_candidate,
    stage_statement_line_candidate,
)
from dalton_core.research_auto_commit import (
    MISSION_VERIFIED_FIGURE_RULE_REF,
    SEC_STATEMENT_LINE_RULE_REF,
    KNOWN_RULE_REFS,
    ResearchAutoCommitRejected,
)
from dalton_core.research_verification import (
    CandidateStagingStore,
    ResearchVerificationConflict,
    SEC_STATEMENT_LINE_AUTHORITY_MODE,
    VerificationRejected,
)
from dalton_core.store import DaltonStore, content_hash

ACN = "company:sec-cik:0001467373"
ACTOR = "automation:coverage-mission"
OWNER = "human:coverage-owner"
ACCESSION = "0001467373-26-000031"
SINK = "raw-sink:" + "c" * 64


def filed_line(**overrides):
    base = {
        "statement": "income", "concept": "us-gaap:Revenues", "label": "Revenues",
        "level": 0, "parent_concept": None, "is_breakdown": False,
        "dimension_axis": None, "dimension_member": None,
        "period_start": "2026-04-01", "period_end": "2026-06-30",
        "value": "17700000000", "unit": "usd", "balance": "credit",
    }
    base.update(overrides)
    return base


LINES = [
    filed_line(),
    filed_line(concept="us-gaap:GrossProfit", label="Gross profit",
               value="5717000000"),
    filed_line(concept="us-gaap:EarningsPerShareDiluted", label="Diluted EPS",
               value="3.87", unit="usdPerShare", balance=None),
    filed_line(concept="acn:SomethingNobodyMapped", label="Other", value="12"),
]


class Harness:
    """One Core with a settled statement dispatch and one ingested 10-Q."""

    def __init__(self, lines=None):
        from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name)
        self.store = DaltonStore(str(self.path / "core.sqlite"))
        state = bootstrap_method_authorities(self.store)
        self.missions = CoverageMissionAuthority(self.store)
        params = mission_params(state)
        self.mission = self.missions.create_mission(params.pop("mission_ref"), **params)
        authorization = self.missions.authorize_sec_lane(
            company_ref=ACN, ticker="ACN", actor_ref=ACTOR,
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"])
        dispatch = self.missions.queue_statement_dispatch(authorization=authorization)
        self.dispatch_id = dispatch["dispatch_id"]
        self.ticket = "sec-financials-run:" + "1" * 24
        self.missions.mark_statement_dispatch_launched(self.dispatch_id, self.ticket)
        self.missions.record_statement_observation(
            dispatch_id=self.dispatch_id,
            observation={
                "schema_version": "0.1", "cik": "0001467373",
                "entity_name": "Accenture plc",
                "filings": [{
                    "accession": ACCESSION, "form": "10-Q", "filed": "2026-06-25",
                    "report_date": "2026-06-30",
                    "lines": list(LINES if lines is None else lines),
                }],
                "source_record_refs": [SINK],
                "next_cursor": None, "provider_status": 200,
            },
            governance_ref="connector-governance:sec-financial-statements:v2",
            governance_hash="b" * 64)
        self.missions.settle_statement_dispatch(self.dispatch_id, outcome="succeeded")
        self.staging = CandidateStagingStore(self.path / "candidate-staging.sqlite")

    # -- helpers ---------------------------------------------------------

    @property
    def ingest_id(self):
        return self.store.connection.execute(
            "SELECT ingest_id FROM coverage_mission_statement_filings").fetchone()[0]

    def proposals(self):
        return SecStatementLineAuthorityResolver(self.store.connection).proposals(
            self.ingest_id)

    def origin(self, metric):
        for ref, proposal in self.proposals().items():
            if proposal["metric_or_aspect"] == metric:
                return ref
        raise AssertionError(f"no proposal for {metric}")

    def sign(self, *rules):
        """Publish and activate one policy version that lists these rules."""

        current = self.store.active_policy_version().to_dict()
        body = dict(current["policy"])
        body["research_candidate_auto_commit"] = {
            "enabled": True, "rules": list(rules), "max_records": 20,
        }
        version = int(str(current["id"]).rsplit("-", 1)[1]) + 1
        return self.store.create_policy(
            body, policy_version_id=f"policy-{version}", version_number=version,
            activate=True, policy_ref=current.get("policy_ref", "commit-gate"),
            effective_from="2026-01-01T00:00:00+00:00", effective_until=None,
            actor_ref=OWNER, prior_version_ref=current["id"],
            change_reason="WP-F test: list the deterministic filed-number rules")

    def stage(self, metric):
        return stage_statement_line_candidate(
            self.store.connection, self.staging, ingest_id=self.ingest_id,
            origin_ref=self.origin(metric), actor_ref=ACTOR)

    def commit(self, bundle, *, key=None):
        return self.store.commit_policy_candidate(
            evidence=bundle["evidence"], claim=bundle["claim"],
            material=bundle["material"],
            numeric_spec=bundle["numeric_spec"],
            source_verification=bundle["source_verification"],
            numeric_verification=bundle["numeric_verification"],
            idempotency_key=key or ("policy-ledger:" + bundle["claim"]["id"]))

    def close(self):
        self.staging.close()
        self.store.close()
        self._dir.cleanup()


class TheRuleRegistryTests(unittest.TestCase):
    def test_both_deterministic_number_rules_are_known_to_the_evaluator(self):
        # C2 found MISSION_VERIFIED_FIGURE_RULE_REF defined but absent from the
        # registry, which made it unsignable: _policy_rule rejects any policy
        # listing a rule it does not know.
        self.assertIn(MISSION_VERIFIED_FIGURE_RULE_REF, KNOWN_RULE_REFS)
        self.assertIn(SEC_STATEMENT_LINE_RULE_REF, KNOWN_RULE_REFS)


class StatementLineStagingTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_a_filed_line_stages_as_a_quantitative_candidate(self):
        staged = self.h.stage("revenue")
        self.assertEqual(staged["write_status"], "fresh")
        claim = staged["claim"]
        self.assertEqual(claim["claim_kind"], "quantitative")
        self.assertEqual(claim["value"], "17700000000")
        self.assertEqual((claim["unit"], claim["currency"], claim["scale"]),
                         ("USD", "USD", "one"))
        self.assertEqual(claim["period"], "2026-04-01..2026-06-30")
        self.assertIn(ACCESSION, claim["normalized_statement"])

    def test_the_material_anchors_the_exact_filing_row_and_raw_artifact(self):
        material = self.h.stage("revenue")["material"]
        self.assertEqual(material["provenance_mode"], SEC_STATEMENT_LINE_AUTHORITY_MODE)
        self.assertEqual(material["source_envelope_ref"], self.h.ingest_id)
        self.assertEqual(material["artifact_ref"], SINK)
        self.assertEqual(material["artifact_hash"], "c" * 64)
        payload = material["normalized_payload"]
        self.assertEqual(payload["accession"], ACCESSION)
        self.assertEqual(payload["dispatch_id"], self.h.dispatch_id)
        self.assertEqual([item["concept"] for item in payload["lines"]],
                         ["us-gaap:Revenues"])
        self.assertEqual(material["source_lineage"][0], "source:sec-edgar")

    def test_an_unmapped_concept_is_never_a_candidate(self):
        concepts = {
            item["anchor"].get("concept") for item in self.h.proposals().values()
            if item["origin_kind"] == "statement_line"
        }
        self.assertNotIn("acn:SomethingNobodyMapped", concepts)

    def test_a_derived_margin_divides_two_filed_rows_of_one_filing(self):
        staged = self.h.stage("gross margin")
        claim = staged["claim"]
        expected = (Decimal("5717000000") / Decimal("17700000000")).quantize(
            Decimal("0.000001"))
        self.assertEqual(Decimal(claim["value"]), expected)
        # A margin enters as a ratio, because ``ratio`` is the only two-input
        # derivation the Ledger's numeric verifier can recompute.
        self.assertEqual((claim["unit"], claim["currency"], claim["scale"]),
                         ("ratio", None, "one"))
        spec = staged["numeric_spec"]
        self.assertEqual(spec["operator"], "ratio")
        self.assertEqual([item["json_pointer"] for item in spec["inputs"]],
                         ["/lines/0/value", "/lines/1/value"])
        payload = staged["material"]["normalized_payload"]
        self.assertEqual(payload["derivation"]["operator"], "ratio")
        self.assertEqual(len(payload["lines"]), 2)

    def test_staging_the_same_row_twice_writes_one_candidate(self):
        first = self.h.stage("revenue")
        second = self.h.stage("revenue")
        self.assertEqual(
            (first["write_status"], second["write_status"]), ("fresh", "duplicate"))
        self.assertEqual(self.h.staging.counts()["candidate_claim_versions"], 1)

    def test_a_retyped_digit_is_refused_by_the_staging_store(self):
        bundle = build_statement_line_candidate(
            self.h.store.connection, ingest_id=self.h.ingest_id,
            origin_ref=self.h.origin("revenue"), actor_ref=ACTOR)
        claim = dict(bundle["claim"])
        claim["value"] = "17800000000"
        claim.pop("content_hash")
        claim["content_hash"] = content_hash(claim)
        with self.assertRaises(ResearchVerificationConflict):
            self.h.staging.stage(
                material=bundle["material"],
                source_verification=bundle["source_verification"],
                evidence=bundle["evidence"], claim=claim,
                idempotency_key="tampered",
                verification_mode=SEC_STATEMENT_LINE_AUTHORITY_MODE,
                verified_statement_line=bundle["verified_statement_line"],
                statement_resolver=bundle["resolver"])

    def test_a_filed_line_cannot_borrow_another_mode_to_carry_its_number(self):
        bundle = build_statement_line_candidate(
            self.h.store.connection, ingest_id=self.h.ingest_id,
            origin_ref=self.h.origin("revenue"), actor_ref=ACTOR)
        with self.assertRaises(VerificationRejected):
            self.h.staging.stage(
                material=bundle["material"],
                source_verification=bundle["source_verification"],
                evidence=bundle["evidence"], claim=bundle["claim"],
                idempotency_key="wrong-mode",
                verification_mode="transcript_core_authority",
                verified_statement_line=bundle["verified_statement_line"],
                statement_resolver=bundle["resolver"])

    def test_a_human_typed_actor_cannot_stand_in_for_the_mission_automation(self):
        with self.assertRaises(QuantitativeClaimPromotionError):
            build_statement_line_candidate(
                self.h.store.connection, ingest_id=self.h.ingest_id,
                origin_ref=self.h.origin("revenue"), actor_ref=OWNER)


class StatementLineAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def claims(self):
        return [
            json.loads(row[0]) for row in self.h.store.connection.execute(
                "SELECT claim_json FROM claim_versions ORDER BY created_at").fetchall()
        ]

    def test_an_unsigned_policy_leaves_the_number_staged(self):
        # The bootstrap policy carries no auto-commit block at all, which is
        # the fail-closed default: nothing automated enters the Ledger.
        bundle = self.h.stage("revenue")
        with self.assertRaises(ResearchAutoCommitRejected) as caught:
            self.h.commit(bundle)
        self.assertIn("auto-commit", str(caught.exception))
        self.assertEqual(self.claims(), [])

    def test_a_signed_rule_admits_the_filed_line_with_its_whole_provenance(self):
        self.h.sign(SEC_STATEMENT_LINE_RULE_REF)
        bundle = self.h.stage("revenue")
        result = self.h.commit(bundle)
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(result["authorization"]["rule_ref"], SEC_STATEMENT_LINE_RULE_REF)
        claims = self.claims()
        self.assertEqual(len(claims), 1)
        claim = claims[0]
        self.assertEqual(claim["claim_kind"], "quantitative")
        self.assertEqual(claim["value"], "17700000000")
        self.assertEqual(claim["subject_ref"], ACN)
        self.assertEqual(claim["producer_execution_refs"], [self.h.ticket])
        evidence = json.loads(self.h.store.connection.execute(
            "SELECT evidence_json FROM evidence_versions").fetchone()[0])
        self.assertEqual(evidence["source_envelope_ref"], self.h.ingest_id)
        self.assertEqual(evidence["source_ref"], "source:sec-edgar")
        self.assertEqual(evidence["artifact_refs"], [{"ref": SINK, "hash": "c" * 64}])

    def test_a_signed_rule_admits_the_derived_margin_with_both_rows(self):
        self.h.sign(SEC_STATEMENT_LINE_RULE_REF)
        bundle = self.h.stage("gross margin")
        self.h.commit(bundle)
        claim = self.claims()[0]
        self.assertEqual(claim["unit"], "ratio")
        self.assertIn("除以", claim["normalized_statement"])
        payload = bundle["material"]["normalized_payload"]
        self.assertEqual(
            [item["line_id"] for item in payload["lines"]],
            [payload["derivation"]["numerator_line_id"],
             payload["derivation"]["denominator_line_id"]])

    def test_committing_twice_writes_one_claim_version(self):
        self.h.sign(SEC_STATEMENT_LINE_RULE_REF)
        bundle = self.h.stage("revenue")
        first = self.h.commit(bundle)
        second = self.h.commit(bundle)
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(self.claims()), 1)

    def test_an_edited_sentence_is_refused_by_the_replay(self):
        self.h.sign(SEC_STATEMENT_LINE_RULE_REF)
        bundle = self.h.stage("revenue")
        claim = dict(bundle["claim"])
        claim["normalized_statement"] = claim["normalized_statement"] + "（据信）"
        claim.pop("content_hash")
        claim["content_hash"] = content_hash(claim)
        with self.assertRaises(ResearchAutoCommitRejected) as caught:
            self.h.store.commit_policy_candidate(
                evidence=bundle["evidence"], claim=claim,
                material=bundle["material"], numeric_spec=bundle["numeric_spec"],
                source_verification=bundle["source_verification"],
                numeric_verification=bundle["numeric_verification"],
                idempotency_key="edited")
        self.assertIn("normalized_statement", str(caught.exception))
        self.assertEqual(self.claims(), [])

    def test_a_swapped_material_payload_is_refused_by_the_replay(self):
        self.h.sign(SEC_STATEMENT_LINE_RULE_REF)
        bundle = self.h.stage("revenue")
        material = json.loads(json.dumps(bundle["material"]))
        material["normalized_payload"]["value"] = "99000000000"
        with self.assertRaises(ResearchAutoCommitRejected):
            self.h.store.commit_policy_candidate(
                evidence=bundle["evidence"], claim=bundle["claim"],
                material=material, numeric_spec=bundle["numeric_spec"],
                source_verification=bundle["source_verification"],
                numeric_verification=bundle["numeric_verification"],
                idempotency_key="swapped")

    def test_a_policy_that_lists_only_the_figure_rule_does_not_admit_a_line(self):
        self.h.sign(MISSION_VERIFIED_FIGURE_RULE_REF)
        bundle = self.h.stage("revenue")
        with self.assertRaises(ResearchAutoCommitRejected) as caught:
            self.h.commit(bundle)
        self.assertIn(SEC_STATEMENT_LINE_RULE_REF, str(caught.exception))

    def test_the_commit_gate_refuses_a_filing_that_is_not_core_authority(self):
        self.h.sign(SEC_STATEMENT_LINE_RULE_REF)
        bundle = self.h.stage("revenue")
        # The evaluator passes (it replays from Core); the commit boundary is
        # asked about an envelope Core does not hold.
        from dalton_core.store import GateRejected

        evidence = dict(bundle["evidence"])
        evidence["source_envelope_ref"] = "statement-ingest:nothing"
        evidence.pop("content_hash")
        evidence["content_hash"] = content_hash(evidence)
        with self.assertRaises((GateRejected, ResearchAutoCommitRejected)):
            self.h.store.commit_policy_candidate(
                evidence=evidence, claim=bundle["claim"],
                material=bundle["material"], numeric_spec=bundle["numeric_spec"],
                source_verification=bundle["source_verification"],
                numeric_verification=bundle["numeric_verification"],
                idempotency_key="not-core")


class TheReviewInboxCanOpenTheNumberTests(unittest.TestCase):
    """A staged number the cockpit cannot open is a number nobody can review."""

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def review(self):
        from dalton_core.research_review import HumanReviewAuthority

        authority = HumanReviewAuthority(self.h.path / "candidate-staging.sqlite")
        self.addCleanup(authority.close)
        return authority

    def test_a_filed_line_candidate_opens_with_its_row_and_no_spec(self):
        staged = self.h.stage("revenue")
        review = self.review()
        bundle = review.candidate_authority_bundle(staged["claim"]["id"])
        self.assertIsNone(bundle["numeric_spec"])
        self.assertEqual(bundle["material"]["id"], staged["material"]["id"])
        line = review.staged_statement_line(staged["claim"]["id"])
        self.assertEqual(line["figure_kind"], "statement_line")
        self.assertEqual(line["accession"], ACCESSION)
        self.assertEqual(line["value"], "17700000000")

    def test_a_derived_margin_candidate_opens_with_the_spec_that_recomputes_it(self):
        staged = self.h.stage("gross margin")
        bundle = self.review().candidate_authority_bundle(staged["claim"]["id"])
        self.assertEqual(bundle["numeric_spec"]["operator"], "ratio")
        self.assertEqual(bundle["material"]["id"], staged["material"]["id"])


class VerifiedFigureAdmissionTests(unittest.TestCase):
    """ADR-0007 step 1: the figure rule is now evaluated, not just named."""

    def setUp(self):
        from tests.test_figure_admission import FigureHarness

        self.harness = FigureHarness()
        self.addCleanup(self.harness.close)
        self.staging = self.harness.staging()
        self.addCleanup(self.staging.close)
        self.core = self.harness.core

    def claims(self):
        return [
            json.loads(row[0]) for row in self.core.connection.execute(
                "SELECT claim_json FROM claim_versions").fetchall()
        ]

    def sign(self, *rules):
        current = self.core.active_policy_version().to_dict()
        body = dict(current["policy"])
        body["research_candidate_auto_commit"] = {
            "enabled": True, "rules": list(rules), "max_records": 20,
        }
        version = int(str(current["id"]).rsplit("-", 1)[1]) + 1
        return self.core.create_policy(
            body, policy_version_id=f"policy-{version}", version_number=version,
            activate=True, policy_ref=current.get("policy_ref", "commit-gate"),
            effective_from="2026-01-01T00:00:00+00:00", effective_until=None,
            actor_ref=OWNER, prior_version_ref=current["id"],
            change_reason="WP-F test: list the verified figure rule")

    def promote(self):
        from dalton_core.claim_index_figures import promote_figure

        figure = self.harness.record_figure()
        return promote_figure(
            self.core, self.staging, figure=figure, actor_ref=ACTOR,
            idempotency_key="figure-promotion:" + figure["figure_id"])

    def commit(self, promoted):
        return self.core.commit_policy_candidate(
            evidence=promoted["evidence"], claim=promoted["claim"],
            material=promoted["material"],
            source_verification=promoted["source_verification"],
            numeric_verification=promoted["numeric_verification"],
            idempotency_key="policy-ledger:" + promoted["claim"]["id"])

    def test_an_unsigned_figure_rule_leaves_the_figure_staged(self):
        promoted = self.promote()
        self.assertEqual(promoted["write_status"], "fresh")
        with self.assertRaises(ResearchAutoCommitRejected):
            self.commit(promoted)
        self.assertEqual(self.claims(), [])

    def test_a_signed_figure_rule_admits_the_number(self):
        self.sign(MISSION_VERIFIED_FIGURE_RULE_REF)
        promoted = self.promote()
        result = self.commit(promoted)
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(result["authorization"]["rule_ref"],
                         MISSION_VERIFIED_FIGURE_RULE_REF)
        claims = self.claims()
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["claim_kind"], "quantitative")
        self.assertEqual(claims[0]["value"], "17.7")

    def test_a_policy_listing_only_the_line_rule_does_not_admit_a_figure(self):
        self.sign(SEC_STATEMENT_LINE_RULE_REF)
        promoted = self.promote()
        with self.assertRaises(ResearchAutoCommitRejected) as caught:
            self.commit(promoted)
        self.assertIn(MISSION_VERIFIED_FIGURE_RULE_REF, str(caught.exception))

    def test_a_retyped_figure_value_is_refused_by_the_replay(self):
        self.sign(MISSION_VERIFIED_FIGURE_RULE_REF)
        promoted = self.promote()
        claim = dict(promoted["claim"])
        claim["value"] = "19.9"
        claim.pop("content_hash")
        claim["content_hash"] = content_hash(claim)
        with self.assertRaises(ResearchAutoCommitRejected) as caught:
            self.core.commit_policy_candidate(
                evidence=promoted["evidence"], claim=claim,
                material=promoted["material"],
                source_verification=promoted["source_verification"],
                numeric_verification=promoted["numeric_verification"],
                idempotency_key="tampered-figure")
        self.assertIn("value", str(caught.exception))
        self.assertEqual(self.claims(), [])

    def test_committing_one_figure_twice_writes_one_claim_version(self):
        self.sign(MISSION_VERIFIED_FIGURE_RULE_REF)
        promoted = self.promote()
        self.assertEqual(self.commit(promoted)["status"], "fresh")
        self.assertEqual(self.commit(promoted)["status"], "duplicate")
        self.assertEqual(len(self.claims()), 1)


class TheLaneRunTests(unittest.TestCase):
    """The child the coordinator launches, end to end, with and without a signature."""

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.h.staging.close()
        self.h.store.close()

    def run_lane(self, **kwargs):
        from dalton_core.quantitative_claim_promotion_cli import run_promotion

        return run_promotion(
            state_dir=self.h.path, summary_dir=self.h.path / "run",
            staging_db=self.h.path / "candidate-staging.sqlite", **kwargs)

    def claims(self):
        connection = sqlite3.connect(
            f"file:{self.h.path / 'core.sqlite'}?mode=ro", uri=True)
        try:
            return connection.execute("SELECT COUNT(*) FROM claim_versions").fetchone()[0]
        finally:
            connection.close()

    def test_without_a_signature_the_numbers_stage_and_say_which_rule_is_missing(self):
        summary = self.run_lane()
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["admitted"], 0)
        self.assertGreater(summary["staged"], 0)
        self.assertEqual(summary["formal_authority_writes"], 0)
        self.assertIn(SEC_STATEMENT_LINE_RULE_REF, summary["unsigned_rules"])
        self.assertTrue(any(
            SEC_STATEMENT_LINE_RULE_REF in str(item.get("reason"))
            for item in summary["staged_candidates"]))
        self.assertEqual(self.claims(), 0)

    def test_with_a_signature_the_same_run_admits_them_and_a_replay_writes_nothing(self):
        store = DaltonStore(str(self.h.path / "core.sqlite"))
        self.h.store = store
        self.h.sign(SEC_STATEMENT_LINE_RULE_REF, MISSION_VERIFIED_FIGURE_RULE_REF)
        store.close()
        first = self.run_lane()
        self.assertGreater(first["admitted"], 0)
        self.assertEqual(first["formal_authority_writes"], first["admitted"])
        written = self.claims()
        self.assertEqual(written, first["admitted"])
        second = self.run_lane()
        self.assertEqual(second["admitted"], first["admitted"])
        self.assertEqual(second["formal_authority_writes"], 0)
        self.assertEqual(second["promoted"],
                         {"statement_line": 0, "derived_ratio": 0, "document_figure": 0})
        self.assertEqual(self.claims(), written)

    def test_stage_only_never_asks_the_ledger_however_the_policy_reads(self):
        store = DaltonStore(str(self.h.path / "core.sqlite"))
        self.h.store = store
        self.h.sign(SEC_STATEMENT_LINE_RULE_REF)
        store.close()
        summary = self.run_lane(admit=False)
        self.assertEqual((summary["admitted"], summary["formal_authority_writes"]), (0, 0))
        self.assertEqual(self.claims(), 0)


class TheFiledRowIsAppendOnlyTests(unittest.TestCase):
    def test_a_filed_line_cannot_be_edited_under_a_staged_candidate(self):
        h = Harness()
        self.addCleanup(h.close)
        h.stage("revenue")
        with self.assertRaises(sqlite3.IntegrityError):
            h.store.connection.execute(
                "UPDATE coverage_mission_statement_lines SET value='1'")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
