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
from datetime import date, timedelta
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from dalton_core import coverage_mission
from dalton_core.connector_governance import build_governance_record
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.guidepoint_cli import run_acquisition, run_discovery
from dalton_core.guidepoint_core import SEARCH_KIND
from dalton_core.guidepoint_launcher import (
    GuidepointAcquisitionLauncher,
    GuidepointLaunchRejected,
    GuidepointSearchLauncher,
)
from dalton_core.lane_child_launcher import (
    LaneChildConflict,
    LaneChildTicketNotFound,
)
from dalton_core.guidepoint_search import (
    FakeGuidepointHandle,
    GuidepointSearchGovernance,
    guidepoint_daily_call_ceiling,
)
from dalton_core.mission_guidepoint_lane import (
    GuidepointLaneCoordinator,
    dispatch as lane_dispatch,
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

    def test_one_tick_spawns_a_child_that_records_a_discovery_end_to_end(self) -> None:
        """The tick is real: a ticket, a process, a summary, a recorded discovery.

        The child is a separate process, so it cannot see any patched
        ``DISCOVERY_SOURCES``; it reads the real vocabulary. Integration added
        ``source:guidepoint`` to ``coverage_mission.DISCOVERY_SOURCES``, so the
        child now runs through the mission grant to a recorded discovery.
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
        self.assertEqual(status["status"], "succeeded")
        self.assertIsNone(status["summary"]["failure_reason"])
        self.assertEqual(status["summary"]["discovery_status"], "fresh")
        self.assertEqual(status["summary"]["new_document_count"], 3)
        self.assertTrue(status["summary"]["discovery_ref"].startswith("mission-source-discovery:"))
        self.assertEqual(status["summary"]["governance_status"], "approved")
        self.assertEqual(status["summary"]["provider_calls"], 1)


class StubLauncher:
    """A launcher whose children have already finished."""

    def __init__(self, tickets):
        self.tickets = dict(tickets)

    def status(self, ticket_ref):
        try:
            return self.tickets[ticket_ref]
        except KeyError as exc:
            raise LaneChildTicketNotFound(ticket_ref) from exc


class RefusingLauncher(StubLauncher):
    def start(self, **kwargs):
        raise GuidepointLaunchRejected(
            "Guidepoint connector governance record is not approved"
        )


class ReconciliationTests(unittest.TestCase):
    """What last tick launched has to reach the ledger this tick."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.clock = Clock()
        self.plan = small_plan()
        self.lane = LaneHarness(self.state, self.clock)
        self.addCleanup(self.lane.close)

    def coordinator(self, launcher, **overrides):
        kwargs = dict(
            missions=self.lane.missions,
            connection=self.lane.h.core.connection,
            launcher=launcher,
            plan=self.plan,
            clock=self.clock,
        )
        kwargs.update(overrides)
        return GuidepointLaneCoordinator(**kwargs)

    def dispatch_row(self, ticket_ref="guidepoint-search-run:" + "a" * 24):
        authorization = self.lane.missions.authorize_source_discovery(
            company_ref=ACN, source_ref=GUIDEPOINT, requested_by=AUTOMATION,
            mission_version_ref=self.lane.mission["id"],
            mission_version_hash=self.lane.mission["content_hash"],
        )
        return self.lane.missions.record_discovery_dispatch(
            authorization=authorization,
            discovery_plan_ref=self.plan["id"],
            discovery_plan_hash=self.plan["content_hash"],
            spec_ref="it-services-demand",
            query_hash="b" * 64,
            ticket_ref=ticket_ref,
        )

    def test_an_orphaned_child_is_settled_failed_not_left_launched(self) -> None:
        # A ticket that still says running with no live process is the shape
        # that used to sit in the ledger forever after a writer restart.
        with patched_discovery_sources():
            dispatch = self.dispatch_row()
            launcher = StubLauncher({
                dispatch["ticket_ref"]: {
                    "status": "orphaned", "exit_code": None, "summary": None,
                },
            })
            settled = self.coordinator(launcher).settle_dispatches()
            self.assertEqual(len(settled), 1)
            self.assertEqual(settled[0]["status"], "failed")
            self.assertEqual(
                self.lane.missions.open_discovery_dispatches(source_ref=GUIDEPOINT), []
            )

    def test_a_missing_ticket_is_settled_rather_than_ignored(self) -> None:
        with patched_discovery_sources():
            self.dispatch_row()
            settled = self.coordinator(StubLauncher({})).settle_dispatches()
            self.assertEqual([item["status"] for item in settled], ["failed"])

    def test_a_running_child_is_left_alone(self) -> None:
        with patched_discovery_sources():
            dispatch = self.dispatch_row()
            launcher = StubLauncher({dispatch["ticket_ref"]: {"status": "running"}})
            self.assertEqual(self.coordinator(launcher).settle_dispatches(), [])
            self.assertEqual(
                len(self.lane.missions.open_discovery_dispatches(source_ref=GUIDEPOINT)), 1
            )

    def test_a_child_that_exited_zero_without_a_discovery_is_a_failure(self) -> None:
        # Exit code alone does not prove the mission ledger accepted anything.
        with patched_discovery_sources():
            dispatch = self.dispatch_row()
            launcher = StubLauncher({
                dispatch["ticket_ref"]: {
                    "status": "succeeded", "exit_code": 0,
                    "summary": {"failure_reason": "CoverageMissionConflict: nope",
                                "discovery_ref": None},
                },
            })
            settled = self.coordinator(launcher).settle_dispatches()
            self.assertEqual(settled[0]["status"], "failed")

    def test_a_child_that_recorded_a_discovery_is_settled_succeeded(self) -> None:
        with patched_discovery_sources():
            dispatch = self.dispatch_row()
            launcher = StubLauncher({
                dispatch["ticket_ref"]: {
                    "status": "succeeded", "exit_code": 0,
                    "summary": {"discovery_ref": "mission-source-discovery:x",
                                "new_document_count": 3},
                },
            })
            settled = self.coordinator(launcher).settle_dispatches()
            self.assertEqual(settled[0]["status"], "succeeded")
            self.assertEqual(settled[0]["new_document_count"], 3)

    def test_a_launch_refusal_stops_the_tick_and_is_not_an_exception(self) -> None:
        with patched_discovery_sources():
            result = self.coordinator(RefusingLauncher({})).launch_discovery(
                as_of=date(2026, 9, 9)
            )
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["reason"], "not_approved")
        self.assertEqual(len(result["skipped"]), 1)
        self.assertIn("not approved", result["skipped"][0]["reason"])

    def test_a_launch_that_records_a_dispatch_reports_its_ref(self) -> None:
        class OneShot(StubLauncher):
            def __init__(self):
                super().__init__({})
                self.calls = 0

            def start(self, **kwargs):
                self.calls += 1
                if self.calls > 1:
                    raise LaneChildConflict("busy")
                return {"id": "guidepoint-search-run:" + "c" * 24}

        launcher = OneShot()
        with patched_discovery_sources():
            result = self.coordinator(launcher).launch_discovery(as_of=date(2026, 9, 9))
        self.assertEqual(result["status"], "launched")
        self.assertEqual(len(result["launched"]), 1)
        self.assertTrue(result["launched"][0]["dispatch_ref"])
        self.assertEqual(result["skipped"][0]["reason"], "child_slot_busy")
        with patched_discovery_sources():
            open_rows = self.lane.missions.open_discovery_dispatches(source_ref=GUIDEPOINT)
        self.assertEqual(len(open_rows), 1)

    def test_a_tick_whose_only_skip_is_the_child_slot_says_so(self) -> None:
        class Busy(StubLauncher):
            def start(self, **kwargs):
                raise LaneChildConflict("busy")

        with patched_discovery_sources():
            result = self.coordinator(Busy({})).launch_discovery(as_of=date(2026, 9, 9))
        self.assertEqual(result["reason"], "child_slot_busy")


class CadenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.clock = Clock()
        self.plan = small_plan()
        self.lane = LaneHarness(self.state, self.clock)
        self.addCleanup(self.lane.close)

    def coordinator(self, records):
        class Missions:
            def __init__(self, inner, records):
                self.inner = inner
                self.records = records

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def source_discoveries(self, *args, **kwargs):
                return [
                    record for record in self.records
                    if record["company_ref"] == kwargs.get("company_ref")
                    and record["spec_ref"] == kwargs.get("spec_ref")
                ]

        return GuidepointLaneCoordinator(
            missions=Missions(self.lane.missions, records),
            connection=self.lane.h.core.connection,
            launcher=StubLauncher({}),
            plan=self.plan,
            mission_version_ref=self.lane.mission["id"],
            mission_version_hash=self.lane.mission["content_hash"],
            requested_by=AUTOMATION,
            clock=self.clock,
        )

    def record(self, *, spec_ref, company_ref, days_ago, refs):
        when = self.clock() - timedelta(days=days_ago)
        return {
            "company_ref": company_ref, "spec_ref": spec_ref,
            "source_ref": GUIDEPOINT, "document_refs": refs,
            "created_at": when.isoformat(timespec="microseconds"),
        }

    def test_a_query_that_found_nothing_retries_on_the_short_interval(self) -> None:
        # rediscovery is 14 days, retry is 2. An empty page three days ago is
        # due again; a page that found something is not.
        empty = self.record(spec_ref="it-services-demand", company_ref=ACN,
                            days_ago=3, refs=[])
        found = self.record(spec_ref="client-demand-and-budgets", company_ref=ACN,
                            days_ago=3, refs=["guidepoint-excerpt:sha256:" + "0" * 64])
        due = self.coordinator([empty, found]).due_queries(as_of=date(2026, 9, 9))
        by_spec = {(item["spec_ref"], item["company_ref"]): item["reason"] for item in due}
        self.assertEqual(by_spec.get(("it-services-demand", ACN)), "retry_due")
        self.assertNotIn(("client-demand-and-budgets", ACN), by_spec)
        # CTSH never ran, so it is due whatever the intervals say.
        self.assertEqual(by_spec.get(("client-demand-and-budgets", CTSH)), "never_run")

    def test_an_empty_page_inside_the_retry_interval_is_not_due(self) -> None:
        empty = self.record(spec_ref="it-services-demand", company_ref=ACN,
                            days_ago=1, refs=[])
        due = self.coordinator([empty]).due_queries(as_of=date(2026, 9, 9))
        self.assertNotIn(("it-services-demand", ACN),
                         {(item["spec_ref"], item["company_ref"]) for item in due})

    def test_a_productive_page_past_the_long_interval_is_due_again(self) -> None:
        found = self.record(spec_ref="it-services-demand", company_ref=ACN,
                            days_ago=15,
                            refs=["guidepoint-excerpt:sha256:" + "0" * 64])
        due = self.coordinator([found]).due_queries(as_of=date(2026, 9, 9))
        reasons = {(item["spec_ref"], item["company_ref"]): item["reason"] for item in due}
        self.assertEqual(reasons.get(("it-services-demand", ACN)), "cadence_due")


class DispatchTests(unittest.TestCase):
    """The controller tick reports; it never raises."""

    class Server:
        def __init__(self, launcher, missions=None, connection=None):
            self._launcher = launcher
            self.coverage_mission = missions
            self.store = type("S", (), {"connection": connection})()

        def lane_launcher(self, kwarg):
            return self._launcher

    def test_a_writer_without_the_lane_is_unconfigured(self) -> None:
        result = lane_dispatch(self.Server(None), {})
        self.assertEqual(result["status"], "unconfigured")

    def test_a_core_with_no_mission_is_unconfigured_not_an_exception(self) -> None:
        class NoMission:
            def active_mission(self, mission_ref):
                raise LookupError("no active mission for this ref")

        class Launcher(StubLauncher):
            plan = small_plan()
            acquisition_launcher = None

        result = lane_dispatch(
            self.Server(Launcher({}), missions=NoMission(), connection=None), {}
        )
        self.assertEqual(result["status"], "unconfigured")
        self.assertIn("LookupError", result["reason"])
        self.assertEqual(result["source_ref"], GUIDEPOINT)


class AcquisitionLauncherTests(unittest.TestCase):
    """What extraction reads has to exist and has to agree with itself."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.launcher = GuidepointAcquisitionLauncher(state_dir=self.state)
        self.addCleanup(self.launcher.close)
        self.document_ref = "guidepoint-excerpt:sha256:" + "1" * 64

    def write_ticket(self, digest, *, status="succeeded", manifest=None, summary=None):
        directory = self.launcher.tickets_dir / digest
        directory.mkdir(parents=True, exist_ok=True)
        ticket = {
            "schema_version": "0.1",
            "id": f"guidepoint-acquire-run:{digest}",
            "document_ref": self.document_ref,
            "started_at": "2026-09-09T00:00:00.000000+00:00",
            "status": status,
        }
        manifest = manifest if manifest is not None else {
            "id": "guidepoint-excerpt-acquisition:x",
            "content_hash": "d" * 64,
            "document_ref": self.document_ref,
            "status": "complete",
            "content_chars": 42,
        }
        summary = summary if summary is not None else {
            "document_ref": self.document_ref,
            "status": "succeeded",
            "manifest_ref": manifest["id"],
            "manifest_hash": manifest["content_hash"],
            "content_chars": manifest["content_chars"],
        }
        for name, value in (("ticket.json", ticket), ("summary.json", summary),
                            ("manifest.json", manifest)):
            path = directory / name
            path.write_text(json.dumps(value), encoding="utf-8")
            os.chmod(path, 0o600)
        return ticket["id"]

    def test_a_settled_acquisition_is_readable_by_ticket_and_by_document(self) -> None:
        ticket_ref = self.write_ticket("e" * 24)
        manifest = self.launcher.read_completed_manifest(ticket_ref, self.document_ref)
        self.assertEqual(manifest["document_ref"], self.document_ref)
        self.assertEqual(
            self.launcher.locate_completed_manifest(self.document_ref), manifest
        )

    def test_files_that_disagree_are_refused(self) -> None:
        # The summary says the excerpt is 42 characters; the manifest says 43.
        ticket_ref = self.write_ticket(
            "f" * 24,
            manifest={"id": "guidepoint-excerpt-acquisition:x",
                      "content_hash": "d" * 64,
                      "document_ref": self.document_ref,
                      "status": "complete", "content_chars": 43},
            summary={"document_ref": self.document_ref, "status": "succeeded",
                     "manifest_ref": "guidepoint-excerpt-acquisition:x",
                     "manifest_hash": "d" * 64, "content_chars": 42},
        )
        with self.assertRaises(GuidepointLaunchRejected):
            self.launcher.read_completed_manifest(ticket_ref, self.document_ref)

    def test_an_unfinished_or_unknown_ticket_is_refused(self) -> None:
        running = self.write_ticket("0" * 24, status="running")
        with self.assertRaises(GuidepointLaunchRejected):
            self.launcher.read_completed_manifest(running, self.document_ref)
        with self.assertRaises(GuidepointLaunchRejected):
            self.launcher.read_completed_manifest("not-a-ticket", self.document_ref)
        with self.assertRaises(GuidepointLaunchRejected):
            self.launcher.locate_completed_manifest("guidepoint-excerpt:sha256:" + "9" * 64)

    def test_a_world_readable_file_is_refused(self) -> None:
        ticket_ref = self.write_ticket("1" * 24)
        os.chmod(self.launcher.tickets_dir / ("1" * 24) / "manifest.json", 0o644)
        with self.assertRaises(GuidepointLaunchRejected):
            self.launcher.read_completed_manifest(ticket_ref, self.document_ref)


if __name__ == "__main__":
    unittest.main()
