"""S2: the Guidepoint plan, its cadence and budget, the child and the launcher.

``coverage_mission.DISCOVERY_SOURCES`` does not yet list ``source:guidepoint``
-- that file belongs to another agent this wave -- so the tests that exercise
the discovery ledger patch the one entry the lane needs and say so.  The entry
itself is in the S2 report as an integration line; patching it here is what
proves the rest of the lane is already correct behind it.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from dalton_core import coverage_mission
from dalton_core.connector_governance import build_governance_record
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.guidepoint_cli import run_acquisition, run_discovery
from dalton_core.guidepoint_core import SEARCH_KIND
from dalton_core.guidepoint_launcher import (
    GuidepointLaunchRejected,
    GuidepointSearchLauncher,
)
from dalton_core.guidepoint_search import (
    FakeGuidepointHandle,
    GuidepointSearchGovernance,
    guidepoint_daily_call_ceiling,
)
from dalton_core.mission_guidepoint_lane import (
    GuidepointLaneCoordinator,
    GuidepointPlanError,
    build_guidepoint_discovery_plan,
    build_guidepoint_parameters,
    guidepoint_plan_queries,
    load_guidepoint_discovery_plan,
    validate_guidepoint_discovery_plan,
)
from dalton_core.store import canonical_json
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_guidepoint_search_lane import ROWS, Clock, Harness

ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "deploy/phase9/p9-us-it-services-guidepoint-v1.json"
FIXTURE = ROOT / "tests/fixtures/guidepoint_search_library_synthetic.json"
ACN = "company:sec-cik:0001467373"
CTSH = "company:sec-cik:0001058290"
AUTOMATION = "automation:coverage-mission"
GUIDEPOINT = "source:guidepoint"

# The one vocabulary entry `coverage_mission` needs; the report asks for it
# verbatim.
GUIDEPOINT_DISCOVERY_SOURCE = MappingProxyType(
    {
        "connector_source_ref": "source:guidepoint",
        "operation": "search_library",
        "document_ref_prefix": "guidepoint-excerpt:",
    }
)


def patched_discovery_sources():
    return mock.patch.object(
        coverage_mission,
        "DISCOVERY_SOURCES",
        MappingProxyType(
            {**dict(coverage_mission.DISCOVERY_SOURCES), GUIDEPOINT: GUIDEPOINT_DISCOVERY_SOURCE}
        ),
    )


def approved_governance() -> GuidepointSearchGovernance:
    return GuidepointSearchGovernance(
        build_governance_record(SEARCH_KIND, approved_by="human:lumos", status="approved")
    )


def small_plan(**overrides):
    base = dict(
        plan_id="discovery-plan:us-it-services:guidepoint:test",
        created_at="2026-09-09T00:00:00+00:00",
        mission_ref="coverage-mission:us-it-services",
        companies={ACN: "Accenture ACN", CTSH: "Cognizant CTSH"},
        specs=[
            {
                "spec_ref": "client-demand-and-budgets",
                "document_type": "expert_call_transcript",
                "query_template": "What are clients saying about {terms} budgets",
                "lookback_days": 400, "rediscovery_interval_days": 14,
                "retry_interval_days": 2, "max_excerpts": 12,
            }
        ],
        industry_specs=[
            {
                "spec_ref": "it-services-demand",
                "document_type": "expert_call_transcript",
                "query": "What are operators seeing in US IT services demand",
                "industry": "IT Services", "lookback_days": 400,
                "rediscovery_interval_days": 14, "retry_interval_days": 2,
                "max_excerpts": 12,
            }
        ],
        industry_anchor_company_ref=ACN,
        max_calls_24h=20,
        max_calls_per_tick=3,
    )
    base.update(overrides)
    return build_guidepoint_discovery_plan(**base)


class PlanTests(unittest.TestCase):
    def test_the_committed_plan_loads_and_binds_its_hash(self) -> None:
        plan = load_guidepoint_discovery_plan(PLAN_PATH)
        self.assertEqual(plan["mission_ref"], "coverage-mission:us-it-services")
        self.assertEqual(plan["source_ref"], GUIDEPOINT)
        self.assertEqual(len(plan["companies"]), 5)
        self.assertEqual(
            [spec["spec_ref"] for spec in plan["specs"]],
            ["client-demand-and-budgets", "competitive-wins-and-losses"],
        )
        self.assertEqual(
            [spec["spec_ref"] for spec in plan["industry_specs"]],
            [
                "it-services-demand", "genai-billable-hours-deflation",
                "offshore-pricing-and-wages", "large-deal-bookings-and-renewals",
            ],
        )
        self.assertEqual(plan["industry_anchor_company_ref"], ACN)
        # Ten company queries plus four industry queries.
        self.assertEqual(len(guidepoint_plan_queries(plan, as_of=date(2026, 9, 9))), 14)
        # Every spec carries its own per-query excerpt budget.
        for spec in plan["specs"] + plan["industry_specs"]:
            self.assertEqual(spec["max_excerpts"], 12)
        tampered = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
        tampered["companies"][ACN]["search_terms"] = "Something Else"
        with self.assertRaises(GuidepointPlanError):
            validate_guidepoint_discovery_plan(tampered)

    def test_a_company_query_names_the_issuer_and_an_industry_query_does_not(self) -> None:
        plan = small_plan()
        company = build_guidepoint_parameters(
            plan, spec_ref="client-demand-and-budgets", company_ref=CTSH,
            as_of=date(2026, 9, 9),
        )
        self.assertEqual(
            company["query"], "What are clients saying about Cognizant CTSH budgets"
        )
        self.assertEqual(company["filters"]["company"], "Cognizant CTSH")
        self.assertNotIn("industry", company["filters"])
        industry = build_guidepoint_parameters(
            plan, spec_ref="it-services-demand", company_ref=ACN, as_of=date(2026, 9, 9),
        )
        self.assertNotIn("company", industry["filters"])
        self.assertEqual(industry["filters"]["industry"], "IT Services")
        # An industry query runs under the anchor company only, because the
        # mission grant it needs is that company's.
        with self.assertRaises(GuidepointPlanError):
            build_guidepoint_parameters(
                plan, spec_ref="it-services-demand", company_ref=CTSH, as_of=date(2026, 9, 9),
            )

    def test_the_plan_refuses_the_shapes_that_would_mislead(self) -> None:
        with self.assertRaises(GuidepointPlanError):
            small_plan(industry_anchor_company_ref="company:sec-cik:9999999999")
        with self.assertRaises(GuidepointPlanError):
            small_plan(
                industry_specs=[
                    {
                        "spec_ref": "bad", "document_type": "expert_call_transcript",
                        "query": "{terms} demand", "industry": "IT Services",
                        "lookback_days": 1, "rediscovery_interval_days": 1,
                        "retry_interval_days": 1, "max_excerpts": 1,
                    }
                ]
            )
        with self.assertRaises(GuidepointPlanError):
            small_plan(
                specs=[
                    {
                        "spec_ref": "bad", "document_type": "sell_side_report",
                        "query_template": "{terms}", "lookback_days": 1,
                        "rediscovery_interval_days": 1, "retry_interval_days": 1,
                        "max_excerpts": 1,
                    }
                ]
            )
        with self.assertRaises(GuidepointPlanError):
            small_plan(max_calls_24h=0)
        with self.assertRaises(GuidepointPlanError):
            small_plan(
                specs=[
                    {
                        "spec_ref": "bad", "document_type": "expert_call_transcript",
                        "query_template": "{terms}", "lookback_days": 1,
                        "rediscovery_interval_days": 1, "retry_interval_days": 1,
                        "max_excerpts": 40,
                    }
                ]
            )


class LaneHarness:
    """A Core whose active mission version does or does not connect Guidepoint.

    Only one version is published, because ``authorize_source_discovery``
    resolves the *active* version: a superseded version that still says
    ``not_connected`` is refused for binding the wrong version, which is a
    different refusal from the one under test.
    """

    def __init__(self, root: Path, clock: Clock, *, connected: bool = True) -> None:
        self.root = root
        self.clock = clock
        self.h = Harness(root, FakeGuidepointHandle(ROWS), clock=clock)
        self.method = bootstrap_method_authorities(self.h.core)
        self.missions = CoverageMissionAuthority(self.h.core)
        params = mission_params(self.method)
        if connected:
            params["autonomy"]["may_write"] = sorted(
                set(params["autonomy"]["may_write"]) | {"source_discovery", "observation"}
            )
            for item in params["source_plan"]:
                if item["source_ref"] == GUIDEPOINT:
                    item["status"] = "connected"
        ref = params.pop("mission_ref")
        self.mission = self.missions.create_mission(ref, **params)

    def close(self) -> None:
        self.h.close()


class ChildTests(unittest.TestCase):
    """The child the launcher spawns, run in process."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.clock = Clock()
        self.plan = small_plan()
        self.plan_path = self.root / "plan.json"
        self.plan_path.write_text(canonical_json(self.plan) + "\n", encoding="utf-8")

    def core(self, *, connected: bool = True):
        lane = LaneHarness(self.state, self.clock, connected=connected)
        self.addCleanup(lane.close)
        return lane

    def discover(self, lane, *, spec_ref="it-services-demand", company_ref=ACN,
                 requested_by=AUTOMATION):
        mission = lane.mission
        lane.close()  # the child opens its own connection on the same file
        summary = run_discovery(
            state_dir=self.state,
            governance=approved_governance(),
            plan=self.plan,
            company_ref=company_ref,
            spec_ref=spec_ref,
            requested_by=requested_by,
            mission_version_ref=mission["id"],
            mission_version_hash=mission["content_hash"],
            as_of=date(2026, 9, 9),
            handle=FakeGuidepointHandle(ROWS),
            transport="fixture",
            summary_dir=self.root / "summary",
            spool_dir=self.state / "connector-spool",
        )
        return summary

    def test_the_child_records_a_discovery_and_queues_every_excerpt(self) -> None:
        lane = self.core()
        with patched_discovery_sources():
            summary = self.discover(lane)
        self.assertEqual(summary["status"], "succeeded", summary["failure_reason"])
        self.assertEqual(summary["source_ref"], GUIDEPOINT)
        self.assertEqual(summary["document_count"], len(ROWS))
        self.assertEqual(summary["new_document_count"], len(ROWS))
        self.assertEqual(summary["provider_calls"], 1)
        self.assertEqual(summary["quote_policy"], {"max_verbatim_words": 20})
        self.assertTrue(summary["discovery_ref"])
        written = json.loads((self.root / "summary" / "summary.json").read_text())
        self.assertEqual(written["discovery_ref"], summary["discovery_ref"])
        # And the acquisition of one of them spends nothing further.
        envelope_ref = summary["search"]["source_envelope_ref"]
        acquired = run_acquisition(
            state_dir=self.state,
            source_envelope_ref=envelope_ref,
            document_ref=summary["search"]["document_refs"][0],
            summary_dir=self.root / "acq",
            spool_dir=self.state / "connector-spool",
        )
        self.assertEqual(acquired["status"], "succeeded", acquired["failure_reason"])
        self.assertEqual(acquired["provider_calls"], 0)
        manifest = json.loads((self.root / "acq" / "manifest.json").read_text())
        self.assertEqual(manifest["document_ref"], summary["search"]["document_refs"][0])

    def test_a_mission_that_has_not_connected_guidepoint_refuses_the_child(self) -> None:
        lane = self.core(connected=False)
        with patched_discovery_sources():
            summary = self.discover(lane)
        self.assertEqual(summary["status"], "failed")
        self.assertIn("not_connected", summary["failure_reason"])
        self.assertIsNone(summary["search"])

    def test_the_child_refuses_an_unapproved_record_before_the_call(self) -> None:
        lane = self.core()
        lane.close()
        proposed = GuidepointSearchGovernance(
            build_governance_record(SEARCH_KIND, approved_by="human:lumos")
        )
        handle = FakeGuidepointHandle(ROWS)
        with patched_discovery_sources():
            summary = run_discovery(
                state_dir=self.state, governance=proposed, plan=self.plan,
                company_ref=ACN, spec_ref="it-services-demand", requested_by=AUTOMATION,
                mission_version_ref=lane.mission["id"],
                mission_version_hash=lane.mission["content_hash"],
                as_of=date(2026, 9, 9), handle=handle, transport="fixture",
                summary_dir=self.root / "summary",
                spool_dir=self.state / "connector-spool",
            )
        self.assertEqual(summary["status"], "failed")
        self.assertIn("owner approval is required", summary["failure_reason"])
        self.assertEqual(handle.calls, [])


class CoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.clock = Clock()
        self.plan = small_plan()
        self.plan_path = self.root / "plan.json"
        self.plan_path.write_text(canonical_json(self.plan) + "\n", encoding="utf-8")
        self.governance_path = self.root / "governance.json"
        self.governance_path.write_text(
            canonical_json(
                build_governance_record(
                    SEARCH_KIND, approved_by="human:lumos", status="approved"
                )
            )
            + "\n",
            encoding="utf-8",
        )
        self.lane = LaneHarness(self.state, self.clock)
        self.addCleanup(self.lane.close)
        self.unconnected_root = self.root / "unconnected"

    def launcher(self, **overrides):
        kwargs = dict(
            state_dir=self.state,
            governance_path=self.governance_path,
            plan_path=self.plan_path,
            mode_args=(),
            fake_search_file=FIXTURE,
            clock=self.clock,
        )
        kwargs.update(overrides)
        launcher = GuidepointSearchLauncher(**kwargs)
        self.addCleanup(launcher.close)
        return launcher

    def coordinator(self, launcher, *, requested_by=AUTOMATION, lane=None):
        lane = lane or self.lane
        mission = lane.mission
        return GuidepointLaneCoordinator(
            missions=lane.missions,
            connection=lane.h.core.connection,
            launcher=launcher,
            plan=self.plan,
            mission_version_ref=mission["id"],
            mission_version_hash=mission["content_hash"],
            requested_by=requested_by,
            clock=self.clock,
        )

    def test_the_budget_is_the_smallest_of_three_ceilings(self) -> None:
        budget = self.coordinator(self.launcher()).budget()
        self.assertEqual(budget["governed_daily_limit"], guidepoint_daily_call_ceiling())
        self.assertEqual(budget["governed_daily_limit"], 25)
        self.assertEqual(budget["plan_daily_limit"], 20)
        self.assertEqual(budget["spent_24h"], 0)
        self.assertEqual(budget["remaining_24h"], 20)
        self.assertEqual(budget["launchable"], 3)

    def test_a_spent_call_lowers_the_remaining_allowance(self) -> None:
        request = self.lane.h.search.build_request(
            build_guidepoint_parameters(
                self.plan, spec_ref="it-services-demand", company_ref=ACN,
                as_of=date(2026, 9, 9),
            )
        )
        self.lane.h.search.search(request)
        budget = self.coordinator(self.launcher()).budget()
        self.assertEqual(budget["spent_24h"], 1)
        self.assertEqual(budget["remaining_24h"], 19)

    def test_an_exhausted_quota_launches_nothing_and_says_why(self) -> None:
        coordinator = self.coordinator(self.launcher())
        with mock.patch(
            "dalton_core.mission_guidepoint_lane.count_recent_guidepoint_search_calls",
            return_value=20,
        ):
            result = coordinator.launch_discovery(as_of=date(2026, 9, 9))
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["reason"], "quota_exhausted")
        self.assertEqual(result["launched"], [])
        self.assertEqual(result["budget"]["remaining_24h"], 0)

    def test_a_mission_that_refuses_the_grant_is_recorded_not_raised(self) -> None:
        self.lane.close()
        other = self.root / "unconnected-state"
        other.mkdir()
        lane = LaneHarness(other, self.clock, connected=False)
        self.addCleanup(lane.close)
        launcher = self.launcher(state_dir=other)
        with patched_discovery_sources():
            result = self.coordinator(launcher, lane=lane).launch_discovery(
                as_of=date(2026, 9, 9)
            )
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["reason"], "all_grants_refused")
        self.assertEqual(len(result["skipped"]), 3)
        for item in result["skipped"]:
            self.assertIn("not_connected", item["reason"])

    def test_every_query_is_due_before_it_has_ever_run(self) -> None:
        coordinator = self.coordinator(self.launcher())
        due = coordinator.due_queries(as_of=date(2026, 9, 9))
        # One company spec across two issuers, plus one industry query.
        self.assertEqual(len(due), 3)
        self.assertEqual({item["reason"] for item in due}, {"never_run"})
        self.assertEqual(
            [(item["kind"], item["company_ref"]) for item in due],
            [("company", CTSH), ("company", ACN), ("industry", ACN)],
        )

    def test_the_launcher_refuses_an_unapproved_record_and_a_stale_plan(self) -> None:
        proposed = self.root / "proposed.json"
        proposed.write_text(
            canonical_json(build_governance_record(SEARCH_KIND, approved_by="human:lumos"))
            + "\n",
            encoding="utf-8",
        )
        launcher = self.launcher(governance_path=proposed)
        with self.assertRaises(GuidepointLaunchRejected):
            launcher.load_governance()
        missing = self.launcher(governance_path=self.root / "absent.json")
        with self.assertRaises(GuidepointLaunchRejected):
            missing.load_governance()
        # A plan the coordinator decided from that is not the plan on disk.
        stale = self.launcher()
        with self.assertRaises(GuidepointLaunchRejected):
            stale.start(
                plan={**self.plan, "content_hash": "0" * 64},
                spec_ref="it-services-demand", company_ref=ACN,
                parameters={}, query_hash="0" * 64, requested_by=AUTOMATION,
                mission_version_ref=self.lane.mission["id"],
                mission_version_hash=self.lane.mission["content_hash"],
                as_of=date(2026, 9, 9),
            )

    def test_a_networked_launcher_cannot_also_serve_a_fixture(self) -> None:
        from dalton_core.lane_child_launcher import LaneChildError

        with self.assertRaises(LaneChildError):
            GuidepointSearchLauncher(
                state_dir=self.state, governance_path=self.governance_path,
                plan_path=self.plan_path, mode_args=("--allow-network",),
                fake_search_file=FIXTURE,
            )
        with self.assertRaises(LaneChildError):
            GuidepointSearchLauncher(
                state_dir=self.state, governance_path=self.governance_path,
                plan_path=self.plan_path, mode_args=(),
            )

    def test_one_tick_spawns_a_child_that_reaches_the_one_missing_vocabulary_entry(self) -> None:
        """The tick is real: a ticket, a process, a summary, an honest stop.

        The child is a separate process, so it cannot see the patched
        ``DISCOVERY_SOURCES`` the in-process tests use. That makes this the
        sharpest statement of what integration still owes the lane: everything
        up to and including the mission grant is wired, and the single thing
        standing between this ticket and a recorded discovery is one entry in
        ``coverage_mission.DISCOVERY_SOURCES``.
        """

        launcher = self.launcher()
        coordinator = self.coordinator(launcher)
        with patched_discovery_sources(), mock.patch.dict(
            os.environ, {"PYTHONPATH": str(ROOT / "src")}
        ):
            result = coordinator.launch_discovery(as_of=date(2026, 9, 9))
            self.assertEqual(result["status"], "launched")
            # One child at a time is the lane's contract: the first launch
            # takes the slot and the rest of the tick's queries wait.
            self.assertEqual(len(result["launched"]), 1)
            self.assertEqual(result["skipped"], [
                {
                    "spec_ref": "client-demand-and-budgets",
                    "company_ref": ACN,
                    "reason": "child_slot_busy",
                },
            ])
            ticket_ref = result["launched"][0]["ticket_ref"]
            launcher.wait(timeout=180)
            status = launcher.status(ticket_ref)
        self.assertEqual(status["transport"], "fixture")
        self.assertEqual(status["source_ref"], GUIDEPOINT)
        self.assertEqual(status["governance_ref"],
                         "connector-governance:guidepoint-search-library:v1")
        self.assertEqual(status["status"], "failed")
        self.assertEqual(
            status["summary"]["failure_reason"],
            "CoverageMissionConflict: source:guidepoint is not a search-driven "
            "discovery source",
        )
        self.assertEqual(status["summary"]["governance_status"], "approved")
        self.assertEqual(status["summary"]["provider_calls"], 0)


if __name__ == "__main__":
    unittest.main()
