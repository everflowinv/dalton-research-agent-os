from __future__ import annotations

import unittest
from unittest.mock import patch

from dalton_core.agenda import AgendaStore
from dalton_core.industry_research import REQUIRED_INDUSTRY_BRIEF_SECTIONS
from dalton_core.weekly_brief import WeeklyBriefAuthority
from dalton_core.weekly_brief_coordinator import (
    WEEKLY_BRIEF_AUTO_PUBLISH_RULE_REF,
    WeeklyBriefCoordinatorPrecondition,
    WeeklyBriefSchedulePlan,
    run_weekly_brief_cycle,
)
from tests import test_industry_research as industry_fixture


AS_OF = "2026-08-27T12:00:00+00:00"


class WeeklyBriefCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = industry_fixture.IndustryResearchAuthorityTests(
            methodName=(
                "test_snapshot_is_self_contained_and_renderer_requires_complete_contract"
            )
        )
        fixture.setUp()
        self.fixture = fixture
        self.addCleanup(fixture.doCleanups)
        params = fixture.pack_params()
        params["coverage_universe"] = [params["coverage_universe"][0]]
        params["report_contract"]["industry_brief_sections"] = list(
            REQUIRED_INDUSTRY_BRIEF_SECTIONS
        )
        self.pack = fixture.authority.register_evidence_pack(
            "industry-evidence-pack:us-it-services", **params
        )
        self.overlay = fixture.authority.register_company_overlay(
            "company-overlay:acn", **fixture.overlay_params(self.pack)
        )
        self.weekly = WeeklyBriefAuthority(fixture.store, fixture.authority)
        self.agenda = AgendaStore(fixture.store)

    def plan(self, **changes) -> dict:
        value = {
            "schema_version": "0.1",
            "plan_ref": "weekly-brief-plan:us-it-services:v1",
            "brief_ref": "weekly-brief:us-it-services",
            "timezone": "America/New_York",
            "weekday": 3,
            "hour": 7,
            "minute": 0,
            "effective_from": "2026-08-20T00:00:00+00:00",
            "evidence_pack_version_id": self.pack["id"],
            "company_overlay_version_ids": [self.overlay["id"]],
            "company_thesis_refs": {},
            "destination_ref": "openclaw:discord:test:channel:weekly",
        }
        value.update(changes)
        return value

    def authorize(self, plan: dict) -> dict:
        active = self.fixture.store.active_policy()
        policy = dict(active["policy"])
        policy["weekly_brief_auto_publish"] = {
            "enabled": True,
            "rule_ref": WEEKLY_BRIEF_AUTO_PUBLISH_RULE_REF,
            "allowed_plan_bindings": [{
                "plan_ref": plan["plan_ref"],
                "plan_hash": WeeklyBriefSchedulePlan.from_mapping(plan).content_hash,
            }],
            "max_issues_per_week": 1,
        }
        return self.fixture.store.create_policy(
            policy,
            policy_version_id="policy:weekly-brief-test:v2",
            version_number=2,
            prior_version_ref=active["policy_version_id"],
            actor_ref="human:test-owner",
            effective_from="2026-08-20T00:00:00+00:00",
            change_reason="authorize exact scheduled weekly brief test plan",
        )

    def execute(self, plan: dict | None = None, *, as_of: str = AS_OF) -> dict:
        return run_weekly_brief_cycle(
            self.fixture.store, self.weekly, self.agenda,
            plan=plan or self.plan(), as_of=as_of, actor_ref="core",
        )

    def test_policy_absence_fails_before_admission_issue_or_outbox(self) -> None:
        with self.assertRaises(WeeklyBriefCoordinatorPrecondition):
            self.execute()
        connection = self.fixture.store.connection
        self.assertEqual(0, connection.execute(
            "SELECT COUNT(*) FROM weekly_brief_cycle_admissions"
        ).fetchone()[0])
        self.assertEqual(0, connection.execute(
            "SELECT COUNT(*) FROM weekly_brief_issue_versions"
        ).fetchone()[0])
        self.assertEqual(0, len(self.agenda.pending_outbox()))

    def test_exact_plan_publishes_and_replays_without_duplicates(self) -> None:
        plan = self.plan()
        self.authorize(plan)
        first = self.execute(plan)
        replay = self.execute(plan)
        self.assertEqual("ready", first["status"])
        self.assertEqual("fresh", first["admission_status"])
        self.assertEqual("fresh", first["issue_status"])
        self.assertEqual("fresh", first["outbox_status"])
        self.assertEqual("duplicate", replay["admission_status"])
        self.assertEqual("duplicate", replay["issue_status"])
        self.assertEqual("duplicate", replay["outbox_status"])
        pending = self.agenda.pending_outbox()
        self.assertEqual(1, len(pending))
        self.assertEqual("weekly_brief.issue", pending[0]["topic"])
        self.assertEqual("weekly_research_brief", pending[0]["payload"]["kind"])
        self.assertEqual(first["issue_version_ref"], pending[0]["payload"]["issue_version_ref"])
        report = self.weekly.integrity_report()
        self.assertTrue(report["ok"], report)
        self.assertEqual(1, report["cycle_admissions"])

    def test_plan_hash_drift_is_denied_before_a_second_cycle(self) -> None:
        plan = self.plan()
        self.authorize(plan)
        self.execute(plan)
        drifted = self.plan(minute=15)
        with self.assertRaises(WeeklyBriefCoordinatorPrecondition):
            self.execute(drifted)
        self.assertEqual(1, self.fixture.store.connection.execute(
            "SELECT COUNT(*) FROM weekly_brief_cycle_admissions"
        ).fetchone()[0])

    def test_crash_after_issue_resumes_from_frozen_admission(self) -> None:
        plan = self.plan()
        self.authorize(plan)
        with patch.object(
            self.agenda, "enqueue_weekly_brief",
            side_effect=RuntimeError("simulated crash before outbox commit"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.execute(plan)
        connection = self.fixture.store.connection
        self.assertEqual(1, connection.execute(
            "SELECT COUNT(*) FROM weekly_brief_cycle_admissions"
        ).fetchone()[0])
        self.assertEqual(1, connection.execute(
            "SELECT COUNT(*) FROM weekly_brief_issue_versions"
        ).fetchone()[0])
        self.assertEqual(0, len(self.agenda.pending_outbox()))
        active = self.fixture.store.active_policy()
        disabled = dict(active["policy"])
        disabled["weekly_brief_auto_publish"] = {
            **disabled["weekly_brief_auto_publish"], "enabled": False,
        }
        self.fixture.store.create_policy(
            disabled,
            policy_version_id="policy:weekly-brief-test:v3",
            version_number=3,
            prior_version_ref=active["policy_version_id"],
            actor_ref="human:test-owner",
            effective_from="2026-08-27T12:01:00+00:00",
            change_reason="test that an admitted cycle keeps frozen authority",
        )
        resumed = self.execute(plan, as_of="2026-08-27T12:02:00+00:00")
        self.assertEqual("duplicate", resumed["admission_status"])
        self.assertEqual("duplicate", resumed["issue_status"])
        self.assertEqual("fresh", resumed["outbox_status"])
        self.assertEqual(1, len(self.agenda.pending_outbox()))

    def test_effective_from_prevents_backfill(self) -> None:
        plan = self.plan(effective_from="2026-08-27T12:30:00+00:00")
        result = self.execute(plan)
        self.assertEqual("waiting", result["status"])
        self.assertEqual(0, self.fixture.store.connection.execute(
            "SELECT COUNT(*) FROM weekly_brief_cycle_admissions"
        ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
