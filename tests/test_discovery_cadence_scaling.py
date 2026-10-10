"""2026-10-07: 「优先看这家」 re-times the discovery coordinators, per company.

ADR-0009's cadence versions used to change only the tracking lane's due
report and the cockpit.  The AlphaEngine / web / SEC coordinators now read the
same chain in ``_cadence_block``: an active priority divides the plan's
``rediscovery_interval_days`` / ``retry_interval_days`` by 4 (never under one
day), for that company only; ``mode=normal`` puts them back on the plan.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from dalton_core.alphaengine_query_forms import query_form_report
from dalton_core.mission_source_discovery import (
    MissionSourceDiscoveryCoordinator,
    build_discovery_plan,
)
from dalton_core.tracking_cadence import (
    MAX_DISCOVERY_DIVISOR,
    TrackingCadenceAuthority,
    discovery_cadence_divisor,
    load_policy,
    publish_priority,
    scaled_interval_days,
)
from tests.p14a_fixtures import ACN, CTSH, OWNER, P14aHarness

POLICY = Path(__file__).resolve().parents[1] / "deploy/phase9/p14a-tracking-policy-v2.json"
NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
SPEC = {"spec_ref": "earnings-call-transcripts", "rediscovery_interval_days": 7,
        "retry_interval_days": 2}


class ScaledIntervalTests(unittest.TestCase):
    def test_no_divisor_is_the_plan_value(self):
        for days in (1, 2, 7, 30):
            self.assertEqual(scaled_interval_days(days, 1), days)

    def test_priority_is_a_quarter_never_under_a_day(self):
        self.assertEqual(scaled_interval_days(30, 4), 7)
        self.assertEqual(scaled_interval_days(14, 4), 3)
        self.assertEqual(scaled_interval_days(7, 4), 1)
        self.assertEqual(scaled_interval_days(2, 4), 1)
        self.assertEqual(scaled_interval_days(1, 4), 1)

    def test_a_core_without_the_table_answers_one(self):
        connection = sqlite3.connect(":memory:")
        self.assertEqual(discovery_cadence_divisor(connection, ACN)["divisor"], 1)
        self.assertEqual(discovery_cadence_divisor(None, ACN)["divisor"], 1)


class CoordinatorCadenceTests(P14aHarness):
    def setUp(self):
        super().setUp()
        self.policy = load_policy(POLICY)
        self.cadences = TrackingCadenceAuthority(self.store)
        self.rows: dict[str, dict] = {}

    def lever(self, company_ref: str, mode: str) -> dict:
        return publish_priority(
            self.cadences, company_ref=company_ref, mode=mode,
            because=f"owner: {mode}", evidence_refs=[f"owner-lever-request:{mode}"],
            change_reason="human_revision", policy=self.policy, mission=self.mission,
            actor_ref=OWNER, decision="owner_prioritize" if mode == "prioritize" else "owner_normal")

    def last(self, company_ref: str, status: str, days_ago: float) -> None:
        self.rows[company_ref] = {
            "status": status,
            "created_at": (NOW - timedelta(days=days_ago)).isoformat(timespec="microseconds"),
        }

    def block(self, company_ref: str, *, source_ref: str = "source:alphaengine",
              use_retry_interval: bool = False) -> str | None:
        missions = SimpleNamespace(discovery_dispatches=lambda *_a, company_ref=None, **_k: (
            [self.rows[company_ref]] if company_ref in self.rows else []))
        lane = SimpleNamespace(missions=missions, clock=lambda: NOW, source_ref=source_ref,
                               search_launcher=None, store=self.store,
                               _query_changed_since=lambda *_a: False)
        return MissionSourceDiscoveryCoordinator._cadence_block(
            lane, self.mission["id"], company_ref, SPEC, use_retry_interval=use_retry_interval)

    def test_without_a_cadence_the_plan_intervals_apply_unchanged(self):
        self.last(ACN, "succeeded", 3)
        self.assertEqual(self.block(ACN), "rediscovered 3d ago; interval 7d")
        self.last(ACN, "failed", 1)
        self.assertEqual(self.block(ACN), "last attempt failed 1d ago; retry interval 2d")

    def test_a_priority_divides_the_intervals_for_that_company_only(self):
        self.assertTrue(self.lever(ACN, "prioritize")["published"])
        self.assertEqual(discovery_cadence_divisor(self.store, ACN)["divisor"],
                         MAX_DISCOVERY_DIVISOR)
        self.assertEqual(discovery_cadence_divisor(self.store, CTSH)["divisor"], 1)
        self.last(ACN, "succeeded", 3)
        self.last(CTSH, "succeeded", 3)
        # 7 // 4 = 1 day: three days after the last search ACN is due again.
        self.assertIsNone(self.block(ACN))
        self.assertEqual(self.block(CTSH), "rediscovered 3d ago; interval 7d")
        self.last(ACN, "succeeded", 0.5)
        self.assertIn("interval 1d (plan 7d ÷4, 优先看这家)", self.block(ACN))
        # The retry interval, floored at one day, and the SEC/web coordinators
        # read the same company-level priority.
        self.last(ACN, "failed", 1.5)
        for source in ("source:alphaengine", "source:sec-edgar", "source:web-search"):
            self.assertIsNone(self.block(ACN, source_ref=source), source)
        self.last(ACN, "failed", 0.5)
        self.assertIn("retry interval 1d (plan 2d ÷4", self.block(ACN))

    def test_back_to_normal_puts_the_plan_values_back(self):
        self.lever(ACN, "prioritize")
        self.lever(ACN, "normal")
        self.assertEqual(discovery_cadence_divisor(self.store, ACN)["divisor"], 1)
        self.last(ACN, "succeeded", 3)
        self.assertEqual(self.block(ACN), "rediscovered 3d ago; interval 7d")

    def test_a_slower_cadence_never_lengthens_discovery(self):
        self.cadences.propose(
            company_ref=ACN, source_key="alphaengine", interval_seconds=43200 * 6,
            because="nothing new three times", evidence_refs=["research-event:1"],
            change_reason="driver_event", policy=self.policy, mission=self.mission,
            actor_ref=OWNER)
        self.assertEqual(discovery_cadence_divisor(self.store, ACN)["divisor"], 1)
        self.last(ACN, "succeeded", 3)
        self.assertEqual(self.block(ACN), "rediscovered 3d ago; interval 7d")

    def test_a_partial_speed_up_divides_by_its_own_ratio(self):
        self.cadences.propose(
            company_ref=ACN, source_key="alphaengine", interval_seconds=21600,
            because="results thickening", evidence_refs=["research-event:2"],
            change_reason="evidence_thicker", policy=self.policy, mission=self.mission,
            actor_ref=OWNER)
        self.assertEqual(discovery_cadence_divisor(self.store, ACN)["divisor"], 2)
        self.last(ACN, "succeeded", 3.5)
        self.assertIsNone(self.block(ACN))


class PlanReportTests(unittest.TestCase):
    def test_the_plan_report_states_the_priority_intervals_without_changing_the_plan(self):
        plan = build_discovery_plan(
            plan_id="discovery-plan:x:alphaengine:1", created_at="2026-10-06T00:00:00.000000+00:00",
            mission_ref="coverage-mission:x", source_ref="source:alphaengine",
            max_calls_24h=10, companies={"company:ticker:googl": {
                "name": "Alphabet", "ticker": "GOOGL", "aliases": ["Alphabet"]}},
            specs=[{"spec_ref": "company-news", "document_type": "news",
                    "query_variants": [{"query_template": "{name} news", "filters": {}}],
                    "lookback_days": 90, "rediscovery_interval_days": 14,
                    "retry_interval_days": 2}])
        report = query_form_report(plan)
        self.assertEqual(report["cadence_scaling"]["max_divisor"], 4)
        self.assertIn("优先看这家", report["cadence_scaling"]["rule"])
        spec = report["specs"][0]
        self.assertEqual((spec["rediscovery_interval_days"], spec["retry_interval_days"]), (14, 2))
        self.assertEqual((spec["priority_rediscovery_interval_days"],
                          spec["priority_retry_interval_days"]), (3, 1))
        self.assertEqual(report["approved_plan_hash"], plan["content_hash"])


if __name__ == "__main__":
    unittest.main()
