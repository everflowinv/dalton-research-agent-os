"""Un-parking thesis-impact: a live verifier pin and an output that lands."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from dalton_core.model_deployment import (
    ADAPTER_REF,
    VERIFIER_POLICY_REF,
    VERIFIER_PROFILE_ID,
    openclaw_broker_profiles,
    openclaw_verifier_policy,
)
from dalton_core.model_fallback_chain import TIER_VERIFIER, tier_chain
from dalton_core.model_router import ModelRouter
from dalton_core.thesis_impact import IMPACTS
from dalton_core.thesis_impact_reopen import (
    BRAIN_CHAIN_FAMILIES,
    IMPACT_DECISIONS,
    PRODUCER_FAMILY,
    REOPENED_VERIFIER_POLICY_REF,
    REOPENED_VERIFIER_PROFILE_IDS,
    ThesisImpactReopenConflict,
    ThesisImpactReopenValidationError,
    decision_for_impact,
    ensure_reopened_verifier_policy,
    flag_state,
    gold_output_map,
    independence_report,
    mission_grants_candidate,
    openclaw_verifier_policy_v2,
    recheck_eligibility,
    route_impact_to_candidate,
)
from dalton_core.thesis_revision import ThesisRevisionAuthority
from tests.p14a_fixtures import ACN, AUTOMATION, OWNER, P14aHarness

CHECKED_AT = datetime(2026, 9, 9, tzinfo=timezone.utc)


class PolicyPinTests(unittest.TestCase):
    def test_v2_is_a_new_version_chained_to_the_pin_it_replaces(self):
        v1 = openclaw_verifier_policy(created_at=CHECKED_AT)
        v2 = openclaw_verifier_policy_v2(created_at=CHECKED_AT)
        self.assertEqual(v1["filters"]["allowed_profile_ids"], [VERIFIER_PROFILE_ID])
        self.assertEqual(v2["policy_version_ref"], REOPENED_VERIFIER_POLICY_REF)
        self.assertEqual(v2["prior_version_ref"], VERIFIER_POLICY_REF)
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["id"], v1["id"])
        # Only the profiles moved; the rest of the pin is the pin.
        self.assertEqual(v2["ordered_preferences"], v1["ordered_preferences"])
        self.assertEqual(v2["filters"]["family_independence_capabilities"],
                         v1["filters"]["family_independence_capabilities"])
        self.assertEqual(v2["filters"]["allowed_adapter_refs"], [ADAPTER_REF])

    def test_the_pin_is_the_verifier_chain_minus_the_link_the_producer_shares(self):
        chain = list(tier_chain(TIER_VERIFIER))
        self.assertEqual(chain[0], "profile:claude-fable-5-1")
        self.assertEqual(REOPENED_VERIFIER_PROFILE_IDS, tuple(chain[1:]))
        self.assertIn(PRODUCER_FAMILY, BRAIN_CHAIN_FAMILIES)


class IndependenceTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.router = ModelRouter(Path(self.temp.name) / "router.sqlite")
        self.addCleanup(self.router.close)
        self.profiles = {
            profile["id"]: profile
            for profile in openclaw_broker_profiles(
                checked_at=CHECKED_AT, availability_ttl=timedelta(days=7))
        }

    def register(self, *profile_ids, status=None):
        for profile_id in profile_ids:
            profile = dict(self.profiles[profile_id])
            if status is not None:
                profile["status"] = status
                profile["retirement"] = {
                    "reason": "not_offered_by_broker",
                    "retired_at": CHECKED_AT.isoformat(timespec="microseconds"),
                    "broker_catalog_hash": "d" * 64,
                }
            self.router.register_profile(profile)

    def test_every_pinned_profile_is_independent_of_every_brain_link(self):
        self.register(*REOPENED_VERIFIER_PROFILE_IDS, "profile:claude-fable-5-1")
        result = ensure_reopened_verifier_policy(self.router, created_at=CHECKED_AT)
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(
            ensure_reopened_verifier_policy(self.router, created_at=CHECKED_AT)["status"],
            "duplicate")
        report = independence_report(self.router)
        self.assertTrue(report["independent"])
        self.assertTrue(report["live"])
        self.assertEqual(report["reasons"], [])
        self.assertEqual(sorted(report["usable_profile_ids"]),
                         sorted(REOPENED_VERIFIER_PROFILE_IDS))
        families = {entry["profile_id"]: entry["family"] for entry in report["profiles"]}
        self.assertEqual(families["profile:zai-glm-5-3"], "zhipu-glm-5.3")
        self.assertEqual(families["profile:gemini-3-5-flash-lite"], "google-gemini-3")
        for family in families.values():
            self.assertNotIn(family, BRAIN_CHAIN_FAMILIES)

    def test_the_old_pin_is_not_independent_of_the_producer_it_would_check(self):
        self.register(VERIFIER_PROFILE_ID)
        self.router.register_policy(openclaw_verifier_policy(created_at=CHECKED_AT))
        report = independence_report(
            self.router, policy_version_ref=VERIFIER_POLICY_REF,
            producer_families=("google-gemini-3",),
        )
        self.assertFalse(report["independent"])
        self.assertIn("google-gemini-3", report["reasons"][0])

    def test_a_retired_or_unregistered_profile_makes_the_pin_dead(self):
        ensure_reopened_verifier_policy(self.router, created_at=CHECKED_AT)
        absent = independence_report(self.router)
        self.assertFalse(absent["live"])
        self.assertEqual(len(absent["reasons"]), 2)
        self.assertIn("not registered", absent["reasons"][0])

        self.register(*REOPENED_VERIFIER_PROFILE_IDS, status="retired")
        retired = independence_report(self.router)
        self.assertFalse(retired["live"])
        self.assertTrue(all("retired" in reason for reason in retired["reasons"]))


class EligibilityTests(unittest.TestCase):
    def test_the_observed_rerun_is_honest_about_its_one_case(self):
        report = recheck_eligibility(source="observed")
        self.assertEqual(report["case_count"], 30)
        self.assertEqual(report["scored_case_count"], 1)
        self.assertFalse(report["automation_eligible"])
        self.assertIn("calibration coverage is incomplete", report["reasons"])
        # And it says whose outputs those were, so nobody reads a clearance
        # for the model that is newly pinned into a re-run of an old one.
        self.assertEqual(report["observed_families"], ["deepseek-v4"])
        self.assertFalse(report["covers_the_new_pin"])

    def test_the_gold_rerun_shows_the_release_gate_can_still_unlock(self):
        report = recheck_eligibility(source="gold")
        self.assertEqual(report["scored_case_count"], 30)
        self.assertTrue(report["automation_eligible"])
        self.assertEqual(report["reasons"], [])
        self.assertEqual(report["score"]["detection_rate"], 1.0)
        self.assertEqual(report["score"]["high_severity_misses"], 0)
        self.assertFalse(report["covers_the_new_pin"])

    def test_the_thresholds_are_the_frozen_ones(self):
        from dalton_core.thesis_impact_calibration import load_frozen_calibration_corpus

        corpus = load_frozen_calibration_corpus()
        self.assertEqual(corpus["rubric"]["release_thresholds"], {
            "minimum_seeded_cases": 30,
            "minimum_detection_rate": 0.9,
            "high_severity_misses": 0,
        })
        self.assertEqual(len(gold_output_map(corpus)), 30)

    def test_an_unknown_source_is_refused(self):
        with self.assertRaises(ThesisImpactReopenValidationError):
            recheck_eligibility(source="live")


class FlagTests(unittest.TestCase):
    def mission(self, *, scopes=(), checkpoints=()):
        return {"id": "coverage-mission-version:us-it-services:14",
                "autonomy": {"may_write": list(scopes),
                             "human_checkpoints": list(checkpoints)}}

    def test_the_flag_is_on_only_when_the_pin_is_live_and_the_mission_grants(self):
        live = {"policy_version_ref": REOPENED_VERIFIER_POLICY_REF, "live": True,
                "reasons": []}
        granted = self.mission(scopes=("thesis_revision_candidate",),
                               checkpoints=("thesis_revision_candidate",))
        state = flag_state(independence=live, mission=granted)
        self.assertTrue(state["enabled"])
        self.assertEqual(state["flag"], "thesis_impact.enabled")
        self.assertEqual(state["reasons"], [])

        dead = {"policy_version_ref": REOPENED_VERIFIER_POLICY_REF, "live": False,
                "reasons": ["profile:zai-glm-5-3 is retired"]}
        self.assertFalse(flag_state(independence=dead, mission=granted)["enabled"])

        ungranted = flag_state(independence=live, mission=self.mission())
        self.assertFalse(ungranted["enabled"])
        self.assertEqual(len(ungranted["reasons"]), 2)
        self.assertTrue(all("ADR-0007" in reason for reason in ungranted["reasons"]))

    def test_the_checkpoint_alone_is_not_the_grant_and_neither_is_the_scope(self):
        for autonomy in (("thesis_revision_candidate",), ()), ((), ("thesis_revision_candidate",)):
            granted, reasons = mission_grants_candidate(
                self.mission(scopes=autonomy[0], checkpoints=autonomy[1]))
            self.assertFalse(granted)
            self.assertEqual(len(reasons), 1)


class RoutingTests(P14aHarness):
    grants = ("market_event", "thesis_revision_candidate", "observation",
              "stage_record", "deliverable")

    def setUp(self):
        super().setUp()
        self.grant("thesis_revision_candidate",
                   checkpoints=("thesis_revision_candidate",))
        self.claim_ref = self.claim(statement="Bookings fell 6% year on year.")
        self.claim_record = {
            "id": self.claim_ref, "claim_ref": "claim:test:1",
            "metric_or_aspect": "aspect:test", "period": "2026Q2",
            "normalized_statement": "Bookings fell 6% year on year.",
            "created_at": "2026-09-09T00:00:00+00:00",
        }
        self.thesis = {"id": "thesis-version:abc", "content_hash": "f" * 64,
                       "thesis_ref": "thesis:acn:ai-reinvention-growth"}
        self.revisions = ThesisRevisionAuthority(self.store)

    def assessment(self, impact, *, suffix="1"):
        return {
            "schema_version": "0.1", "id": f"thesis-impact:{suffix}",
            "created_at": "2026-09-09T01:00:00+00:00",
            "claim_version_ref": self.claim_ref,
            "thesis_version_ref": self.thesis["id"],
            "impact": impact,
            "driver_statement": "Bookings convert into revenue with a lag.",
            "rationale": "A six percent fall is the falsifier the thesis named.",
            "follow_up_question": ("What is the segment split?" if impact == "insufficient"
                                   else None),
            "producer_result_envelope_ref": f"result-envelope:{suffix}",
        }

    def verification(self, verdict="pass", *, suffix="1"):
        return {"id": f"thesis-impact-verification:{suffix}", "verdict": verdict,
                "findings": [], "independence": {"predicate": "model_family"}}

    def route(self, impact, *, verdict="pass", suffix="1", mission=None):
        return route_impact_to_candidate(
            self.store, assessment=self.assessment(impact, suffix=suffix),
            verification=self.verification(verdict, suffix=suffix),
            thesis=self.thesis, claim=self.claim_record, company_ref=ACN,
            mission=mission or self.mission, actor_ref=AUTOMATION,
        )

    def test_the_four_words_map_onto_the_playbook_s_five(self):
        self.assertEqual(set(IMPACTS),
                         {"supports", "weakens", "no_change", "insufficient"})
        self.assertEqual(decision_for_impact("weakens"), "THESIS_WEAKENED")
        self.assertEqual(decision_for_impact("supports"), "THESIS_STRENGTHENED")
        self.assertEqual(decision_for_impact("no_change"), "NO_CHANGE")
        self.assertEqual(set(IMPACT_DECISIONS), {"supports", "weakens", "no_change"})
        with self.assertRaisesRegex(ThesisImpactReopenConflict, "backlog"):
            decision_for_impact("insufficient")
        with self.assertRaises(ThesisImpactReopenValidationError):
            decision_for_impact("probably")

    def test_a_weakens_becomes_a_candidate_behind_an_event_and_a_judgement(self):
        result = self.route("weakens")
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["decision"], "THESIS_WEAKENED")
        self.assertEqual(result["event"]["kind"], "claim")
        self.assertEqual(result["event"]["payload"]["claim_version_ref"], self.claim_ref)
        self.assertEqual(result["judgement"]["action"], "revise_thesis")
        self.assertEqual(result["judgement"]["effect"]["assessment_ref"],
                         "thesis-impact:1")
        candidate = result["candidate"]
        self.assertEqual(candidate["checkpoint_kind"], "thesis_revision_candidate")
        self.assertEqual(candidate["thesis_version_ref"], self.thesis["id"])
        self.assertIn(self.claim_ref, candidate["evidence_refs"])
        self.assertIn("thesis-impact:1", candidate["evidence_refs"])
        self.assertIn("独立核验：pass", candidate["because"])
        # It arrives in the same queue the judgement lane's candidates do.
        self.assertEqual([item["id"] for item in self.revisions.undecided(ACN)],
                         [candidate["id"]])

    def test_the_same_assessment_routed_twice_writes_one_of_everything(self):
        first = self.route("supports")
        again = self.route("supports")
        self.assertEqual(again["event"]["id"], first["event"]["id"])
        self.assertEqual(again["judgement"]["id"], first["judgement"]["id"])
        self.assertEqual(again["judgement"]["status"], "duplicate")
        self.assertEqual(again["candidate"]["status"], "duplicate")
        self.assertEqual(len(self.revisions.undecided(ACN)), 1)

    def test_no_change_is_recorded_and_proposes_nothing(self):
        result = self.route("no_change")
        self.assertEqual(result["status"], "no_change")
        self.assertIsNone(result["candidate"])
        self.assertEqual(result["judgement"]["decision"], "NO_CHANGE")
        self.assertEqual(result["judgement"]["action"], "no_change")
        self.assertEqual(self.revisions.undecided(ACN), [])

    def test_insufficient_never_becomes_a_proposal(self):
        result = self.route("insufficient")
        self.assertEqual(result["status"], "skipped")
        self.assertIn("backlog", result["reason"])
        self.assertEqual(self.revisions.undecided(ACN), [])

    def test_an_unverified_assessment_is_refused(self):
        with self.assertRaisesRegex(ThesisImpactReopenConflict, "independently verified"):
            self.route("weakens", verdict="reject")

    def test_without_the_grant_the_judgement_is_kept_and_the_candidate_is_queued(self):
        harness = self.__class__("run")
        params = dict(self.params)
        autonomy = dict(params["autonomy"])
        autonomy["may_write"] = [s for s in autonomy["may_write"]
                                 if s != "thesis_revision_candidate"]
        autonomy["may_write"] = list(dict.fromkeys(autonomy["may_write"] + ["market_event"]))
        autonomy["human_checkpoints"] = [c for c in autonomy["human_checkpoints"]
                                         if c != "thesis_revision_candidate"]
        params["autonomy"] = autonomy
        params.update({"version_id": "coverage-mission-version:us-it-services:99",
                       "prior_version_ref": self.mission["id"],
                       "idempotency_key": "coverage-mission:us-it-services:99"})
        ungranted = self.missions.create_mission(self.mission_ref, **params)
        result = self.route("weakens", mission=ungranted)
        self.assertEqual(result["status"], "queued")
        self.assertIn("ADR-0007", result["reason"])
        self.assertIsNone(result["candidate"])
        self.assertIsNotNone(result["judgement"])
        self.assertEqual(self.revisions.undecided(ACN), [])
        del harness


if __name__ == "__main__":
    unittest.main()
