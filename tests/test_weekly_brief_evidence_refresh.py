"""A 0.2 weekly brief plan rebuilds its evidence pack from the Ledger each issue.

Live W36-W38 each cited the same five Claims because plan v3 pinned one pack
version registered once on 08-27.  These tests pin the replacement: the plan
names the pack ref, a deterministic lane re-selects current Claims before the
cycle is admitted, and the admission freezes whatever version that produced.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from dalton_core.claim_retirement import ClaimRetirementAuthority
from dalton_core.industry_evidence_refresh import period_end
from dalton_core.industry_research import (
    EVIDENCE_REFRESH_ACTOR,
    IndustryResearchValidationError,
)
from dalton_core.weekly_brief_coordinator import (
    WeeklyBriefCoordinatorError,
    WeeklyBriefSchedulePlan,
)
from tests import test_industry_research as industry_fixture
from tests import test_weekly_brief_coordinator as coordinator_fixture


# Thursdays after the fixture Claims are written (they carry the real clock,
# and FY-style periods anchor on that write date).
WEEK_1 = "2026-12-03T12:30:00+00:00"
WEEK_2 = "2026-12-10T12:30:00+00:00"
WEEK_3 = "2026-12-17T12:30:00+00:00"


class WeeklyBriefEvidenceRefreshTests(coordinator_fixture.WeeklyBriefCoordinatorTests):
    def refresh_plan(self, **changes) -> dict:
        value = {
            "schema_version": "0.2",
            "plan_ref": "weekly-brief-plan:us-it-services:v4",
            "brief_ref": "weekly-brief:us-it-services",
            "timezone": "America/New_York",
            "weekday": 3, "hour": 7, "minute": 0,
            "effective_from": "2026-11-26T00:00:00+00:00",
            "evidence_refresh": {
                "evidence_pack_ref": "industry-evidence-pack:us-it-services",
                "company_overlay_refs": ["company-overlay:acn"],
                "claim_window_days": 365,
            },
            "company_thesis_refs": {},
            "destination_ref": "openclaw:discord:test:channel:weekly",
        }
        value.update(changes)
        return value

    def new_bookings_claim(self, *, period: str = "2026-09-01..2026-11-30") -> dict:
        claim = self.fixture.store.register_claim({
            "claim_ref": "claim:acn:q4fy26:new-bookings",
            "subject_ref": industry_fixture.ACN,
            "metric_or_aspect": "metric:new-bookings", "period": period,
            "basis": "issuer-reported",
            "normalized_statement": "Q4 FY2026 new bookings were USD 21.3 billion.",
            "claim_kind": "quantitative", "value": 21.3, "unit": "USD_billion",
            "producer_invocation_refs": ["invocation:industry-research"],
            "actor_ref": "automation:researcher",
        })
        self.fixture.store.relate_evidence({
            "id": "relation:acn:q4fy26:bookings",
            "evidence_version_ref": self.fixture.evidence["evidence_version_id"],
            "claim_version_ref": claim["claim_version_id"], "relation": "supports",
        })
        return claim

    def pack_versions(self) -> list[dict]:
        connection = self.fixture.store.connection
        return [
            self.fixture.authority.evidence_pack(row[0]) for row in connection.execute(
                "SELECT version_id FROM industry_evidence_pack_versions ORDER BY version_number"
            )
        ]

    def run_refresh(self, plan: dict, as_of: str) -> dict:
        return self.execute(plan, as_of=as_of)

    # -- plan shape ------------------------------------------------------------

    def test_pinned_plan_serializes_exactly_as_before(self) -> None:
        plan = self.plan()
        parsed = WeeklyBriefSchedulePlan.from_mapping(plan)
        self.assertEqual(plan["evidence_pack_version_id"], parsed.to_dict()["evidence_pack_version_id"])
        self.assertEqual(set(plan), set(parsed.to_dict()))
        self.assertIsNone(parsed.evidence_refresh)

    def test_refresh_plan_is_closed_and_round_trips(self) -> None:
        plan = self.refresh_plan()
        parsed = WeeklyBriefSchedulePlan.from_mapping(plan)
        plan["effective_from"] = "2026-11-26T00:00:00.000000+00:00"
        self.assertEqual(plan, parsed.to_dict())
        mixed = dict(plan, evidence_pack_version_id=self.pack["id"])
        with self.assertRaises(WeeklyBriefCoordinatorError):
            WeeklyBriefSchedulePlan.from_mapping(mixed)
        bad_window = self.refresh_plan()
        bad_window["evidence_refresh"] = dict(
            bad_window["evidence_refresh"], claim_window_days=0
        )
        with self.assertRaises(WeeklyBriefCoordinatorError):
            WeeklyBriefSchedulePlan.from_mapping(bad_window)

    def test_period_end_reads_iso_ranges_only(self) -> None:
        self.assertEqual("2026-05-31", period_end("2026-03-01..2026-05-31").isoformat())
        self.assertEqual("2026-05-31", period_end("2026-05-31").isoformat())
        self.assertIsNone(period_end("FY2026Q3"))
        self.assertIsNone(period_end("session as of the desk note"))

    # -- refresh behaviour -----------------------------------------------------

    def test_quiet_week_reuses_the_latest_pack_instead_of_minting_a_copy(self) -> None:
        plan = self.refresh_plan()
        self.authorize(plan)
        result = self.run_refresh(plan, WEEK_1)
        self.assertEqual("ready", result["status"])
        self.assertEqual("unchanged", result["evidence_refresh"]["status"])
        self.assertEqual(self.pack["id"], result["evidence_pack_version_ref"])
        self.assertEqual(1, len(self.pack_versions()))

    def test_new_claim_publishes_a_refreshed_pack_that_the_cycle_freezes(self) -> None:
        plan = self.refresh_plan()
        self.authorize(plan)
        first = self.run_refresh(plan, WEEK_1)
        claim = self.new_bookings_claim()
        second = self.run_refresh(plan, WEEK_2)

        refresh = second["evidence_refresh"]
        self.assertEqual("refreshed", refresh["status"])
        self.assertEqual("fresh", refresh["evidence_pack_status"])
        pack = self.fixture.authority.evidence_pack(second["evidence_pack_version_ref"])
        self.assertEqual(EVIDENCE_REFRESH_ACTOR, pack["actor_ref"])
        self.assertEqual(self.pack["id"], pack["prior_version_ref"])
        self.assertEqual(2, pack["version"])
        authority = pack["refresh_authority"]
        self.assertEqual(plan["plan_ref"], authority["plan_ref"])
        self.assertEqual(
            WeeklyBriefSchedulePlan.from_mapping(plan).content_hash, authority["plan_hash"]
        )
        self.assertEqual(self.pack["id"], authority["template_evidence_pack_version_ref"])
        bound = {item["claim_version_ref"] for item in pack["evidence_bindings"]}
        self.assertIn(claim["claim_version_id"], bound)
        self.assertNotIn(self.fixture.bookings["claim_version_id"], bound)
        self.assertIn(self.fixture.demand["claim_version_id"], bound)

        # The human "conversion still needs proof" position cited the replaced
        # bookings Claim, so it is not re-argued; the new Claim is shown as an
        # unreviewed qualification next to the untouched demand position.
        positions = pack["debates"][0]["positions"]
        self.assertEqual("demand is visible", positions[0]["label"])
        self.assertEqual("qualifies", positions[1]["stance"])
        self.assertIn("stance not yet reviewed", positions[1]["label"])
        self.assertEqual([claim["claim_version_id"]], positions[1]["claim_version_refs"])

        overlay = self.fixture.authority.company_overlay(
            refresh["company_overlay_version_refs"][0]
        )
        self.assertEqual(EVIDENCE_REFRESH_ACTOR, overlay["actor_ref"])
        self.assertEqual(self.overlay["id"], overlay["prior_version_ref"])
        self.assertEqual("unknown", overlay["driver_views"][0]["stance"])
        self.assertEqual(self.overlay["key_differences"], overlay["key_differences"])

        admission = self.weekly.cycle_admission(second["cycle_ref"])
        self.assertEqual(pack["id"], admission["evidence_pack_version_ref"])
        issue = self.weekly.issue(second["issue_version_ref"])
        self.assertEqual(first["issue_version_ref"], issue["prior_version_ref"])
        self.assertEqual(
            [claim["claim_version_id"]], issue["change_summary"]["new_claim_version_refs"]
        )
        self.assertEqual(
            [self.fixture.bookings["claim_version_id"]],
            issue["change_summary"]["removed_claim_version_refs"],
        )
        self.assertTrue(self.fixture.authority.integrity_report()["ok"])

        replay = self.run_refresh(plan, WEEK_2)
        self.assertEqual("duplicate", replay["admission_status"])
        self.assertEqual(2, len(self.pack_versions()))

    def test_crash_before_admission_replays_into_the_same_versions(self) -> None:
        plan = self.refresh_plan()
        self.authorize(plan)
        self.new_bookings_claim()
        with patch.object(
            self.weekly, "admit_scheduled_cycle",
            side_effect=RuntimeError("simulated crash before admission"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.run_refresh(plan, WEEK_1)
        versions_after_crash = [item["id"] for item in self.pack_versions()]
        result = self.run_refresh(plan, WEEK_1)
        self.assertEqual("unchanged", result["evidence_refresh"]["status"])
        self.assertEqual(versions_after_crash, [item["id"] for item in self.pack_versions()])
        self.assertEqual(versions_after_crash[-1], result["evidence_pack_version_ref"])
        self.assertEqual(
            {"company-overlay:acn": "reused"},
            result["evidence_refresh"]["company_overlay_statuses"],
        )

    def test_retired_claims_are_not_selected(self) -> None:
        plan = self.refresh_plan()
        self.authorize(plan)
        claim = self.new_bookings_claim()
        retirement = ClaimRetirementAuthority(self.fixture.store)
        challenge = retirement.challenge(
            claim_version_ref=claim["claim_version_id"],
            claim_version_hash=claim["content_hash"],
            reason_code="human_judgment",
            rationale="Bookings figure belongs to another issuer.",
            actor_ref="human:analyst",
        )
        retirement.decide(
            challenge_ref=challenge["id"],
            challenge_hash=challenge["content_hash"], decision="retired",
            actor_ref="human:analyst", rationale="Confirmed wrong issuer.",
        )
        result = self.run_refresh(plan, WEEK_1)
        self.assertEqual("unchanged", result["evidence_refresh"]["status"])
        pack = self.fixture.authority.evidence_pack(result["evidence_pack_version_ref"])
        self.assertNotIn(
            claim["claim_version_id"],
            {item["claim_version_ref"] for item in pack["evidence_bindings"]},
        )

    def test_empty_window_falls_back_to_the_current_pack_and_says_so(self) -> None:
        plan = self.refresh_plan()
        plan["evidence_refresh"] = dict(plan["evidence_refresh"], claim_window_days=1)
        self.authorize(plan)
        result = self.run_refresh(plan, WEEK_3)
        self.assertEqual("ready", result["status"])
        self.assertEqual("fallback", result["evidence_refresh"]["status"])
        self.assertIn("window", result["evidence_refresh"]["reason"])
        self.assertEqual(self.pack["id"], result["evidence_pack_version_ref"])
        self.assertEqual(1, len(self.pack_versions()))

    def test_switching_plans_at_an_issued_slot_does_not_publish_twice(self) -> None:
        pinned = self.plan()
        refreshing = self.refresh_plan()
        active = self.fixture.store.active_policy()
        policy = dict(active["policy"])
        policy["weekly_brief_auto_publish"] = {
            "enabled": True,
            "rule_ref": coordinator_fixture.WEEKLY_BRIEF_AUTO_PUBLISH_RULE_REF,
            "allowed_plan_bindings": [
                {"plan_ref": item["plan_ref"],
                 "plan_hash": WeeklyBriefSchedulePlan.from_mapping(item).content_hash}
                for item in (pinned, refreshing)
            ],
            "max_issues_per_week": 1,
        }
        self.fixture.store.create_policy(
            policy, policy_version_id="policy:weekly-brief-test:v2", version_number=2,
            prior_version_ref=active["policy_version_id"], actor_ref="human:test-owner",
            effective_from="2026-08-20T00:00:00+00:00",
            change_reason="authorize both plans across a switch",
        )
        issued = self.run_refresh(pinned, WEEK_1)
        switched = self.run_refresh(refreshing, WEEK_1)
        self.assertEqual("already_issued", switched["status"])
        self.assertEqual(issued["issue_version_ref"], switched["issue_version_ref"])
        self.assertEqual(1, self.fixture.store.connection.execute(
            "SELECT COUNT(*) FROM weekly_brief_issue_versions"
        ).fetchone()[0])
        self.assertEqual(1, len(self.agenda.pending_outbox()))
        following = self.run_refresh(refreshing, WEEK_2)
        self.assertEqual("ready", following["status"])
        self.assertEqual(issued["issue_version_ref"], self.weekly.issue(
            following["issue_version_ref"]
        )["prior_version_ref"])

    def test_only_the_in_process_refresh_can_publish_as_the_refresh_actor(self) -> None:
        params = self.fixture.pack_params()
        params.update(
            actor_ref=EVIDENCE_REFRESH_ACTOR, prior_version_ref=self.pack["id"],
            version_id="industry-evidence-pack-version:forged",
            idempotency_key="forged",
        )
        with self.assertRaises(IndustryResearchValidationError):
            self.fixture.authority.register_evidence_pack(
                "industry-evidence-pack:us-it-services", **params
            )
        params["actor_ref"] = "human:coverage-owner"
        with self.assertRaises(IndustryResearchValidationError):
            self.fixture.authority.register_evidence_pack(
                "industry-evidence-pack:us-it-services", **params,
                refresh_authority={"plan_ref": "x"},
            )
        from dalton_core.writer_server import OPERATION_FIELDS
        self.assertNotIn(
            "refresh_authority", OPERATION_FIELDS["register_industry_evidence_pack"]
        )
        self.assertNotIn("refresh_authority", OPERATION_FIELDS["register_company_overlay"])


# The inherited coordinator tests already run in their own module.
for _name in [name for name in dir(coordinator_fixture.WeeklyBriefCoordinatorTests) if name.startswith("test_")]:
    setattr(WeeklyBriefEvidenceRefreshTests, _name, None)


if __name__ == "__main__":
    unittest.main()
