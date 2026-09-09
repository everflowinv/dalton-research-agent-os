"""S3: the crowd-source launchers and the lane that drives them.

The launcher tests prove the two refusals that are worth having early -- an
unapproved record and a request the same as one already made -- and that the
argv a child would receive is the argv a child can parse. One test really
runs it, because an argv nobody has run is an argv nobody has checked.

The lane tests prove the four things the coordinator exists for: it refuses
when the mission has not granted the write, it holds when a source is not
connected in the mission, it records at most one child per source per tick, and
it does not record the same post twice.
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping

from dalton_core.connector_governance import build_governance_record
from dalton_core.crowd_source_launcher import (
    EmployeeReviewsLauncher,
    XreachLauncher,
    XueqiuLauncher,
)
from dalton_core.lane_child_launcher import LaneChildRejected
from dalton_core.mission_crowd_source_lane import (
    CROWD_GRADE,
    CROWD_IMPORTANCE,
    LANE_GRANTS,
    CrowdObservationLedger,
    CrowdSourceLaneError,
    MissionCrowdSourceLaneCoordinator,
    load_crowd_source_map,
    observation_entries,
)
from tests.crowd_fixtures import blind_page, write_json

ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = ROOT / "deploy" / "phase9" / "p9-us-it-services-crowd-sources-v1.json"


def mission(*, may_write=None, connected=("source:xueqiu", "source:x",
                                          "source:blind")) -> dict[str, Any]:
    return {
        "autonomy": {"may_write": list(
            may_write if may_write is not None else sorted(LANE_GRANTS))},
        "source_plan": [{"source_ref": ref, "status": "connected"}
                        for ref in connected],
    }


class FakeLauncher:
    """A launcher that records what it was asked for and answers on demand."""

    def __init__(self, source_ref: str) -> None:
        self.SOURCE_REF = source_ref
        self.started: list[dict[str, Any]] = []
        self._tickets: dict[str, dict[str, Any]] = {}
        self.reject: str | None = None

    def start(self, *, operation: str, actor_ref: str, **params: Any) -> dict[str, Any]:
        if self.reject:
            raise LaneChildRejected(self.reject)
        ticket_ref = f"ticket-{len(self.started)}"
        self.started.append({"operation": operation, **params})
        self._tickets[ticket_ref] = {"status": "running", "summary": None}
        return {"id": ticket_ref}

    def finish(self, ticket_ref: str, summary: Mapping[str, Any] | None,
               status: str = "succeeded") -> None:
        self._tickets[ticket_ref] = {"status": status, "summary": summary}

    def status(self, ticket_ref: str) -> dict[str, Any]:
        return self._tickets[ticket_ref]


def summary_with(posts: int, *, first_id: int = 1) -> dict[str, Any]:
    return {
        "governance_ref": "connector-governance:xueqiu-search-posts:v1",
        "governance_hash": "c" * 64,
        "artifact": {"content_hash": "d" * 64, "size_bytes": 10,
                     "storage_locator": "spool:objects/dd/x"},
        "observation": {
            "schema_version": "0.1", "operation": "search_posts",
            "posts": [{"post_id": str(first_id + index),
                       "created_at": "2026-09-01 10:00:00"}
                      for index in range(posts)],
            "source_record_refs": ["raw-sink:" + "d" * 64],
            "next_cursor": None, "provider_status": 200,
        },
    }


class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.state = self.root / "state"
        self.state.mkdir()

    def governance(self, kind: str, *, status: str = "approved") -> Path:
        record = build_governance_record(kind, approved_by="human:tester",
                                         status=status)
        path = self.root / f"{kind}.json"
        path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        return path

    def xueqiu(self, *, status: str = "approved", **kwargs: Any) -> XueqiuLauncher:
        launcher = XueqiuLauncher(
            state_dir=self.state,
            governance_paths={
                "search_posts": self.governance("xueqiu-search-posts",
                                                status=status),
                "get_post": self.governance("xueqiu-get-post", status=status),
                "hot_rank": self.governance("xueqiu-hot-rank", status=status),
            },
            **kwargs,
        )
        self.addCleanup(launcher.close)
        return launcher

    def test_an_unapproved_record_is_refused_before_a_process_exists(self):
        launcher = self.xueqiu(status="proposed")
        with self.assertRaises(LaneChildRejected) as caught:
            launcher.start(operation="search_posts", query="x",
                           actor_ref="automation:coverage-mission")
        self.assertIn("not approved", str(caught.exception))
        self.assertFalse(list(self.state.glob("xueqiu-runs/*")))

    def test_an_operation_with_no_record_is_refused_by_name(self):
        launcher = XueqiuLauncher(state_dir=self.state, governance_paths={})
        self.addCleanup(launcher.close)
        with self.assertRaises(LaneChildRejected) as caught:
            launcher.start(operation="get_post", post_ref="1",
                           actor_ref="automation:coverage-mission")
        self.assertIn("get_post", str(caught.exception))

    def test_an_operation_this_connector_does_not_have_is_refused(self):
        with self.assertRaises(LaneChildRejected):
            self.xueqiu().start(operation="semantic_search",
                                actor_ref="automation:coverage-mission")

    def test_an_actor_outside_the_two_namespaces_is_refused(self):
        with self.assertRaises(LaneChildRejected):
            self.xueqiu().start(operation="search_posts", query="x",
                                actor_ref="someone")

    def test_a_malformed_since_is_refused_rather_than_passed_on(self):
        with self.assertRaises(LaneChildRejected):
            self.xueqiu().start(operation="search_posts", query="x",
                                since="last tuesday",
                                actor_ref="automation:coverage-mission")

    def test_the_argv_a_child_receives_is_one_a_child_can_parse(self):
        launcher = self.xueqiu()
        command = launcher._command(
            ticket_dir=self.root, operation="search_posts",
            params={"query": "synthetic", "since": "2026-09-01"})
        from dalton_core.xueqiu_cli import build_parser

        parsed = build_parser().parse_args(command[3:])
        self.assertEqual(parsed.operation, "search_posts")
        self.assertEqual(parsed.query, "synthetic")
        self.assertEqual(parsed.since, "2026-09-01")

    def test_the_review_child_is_never_handed_a_credential_grant(self):
        launcher = EmployeeReviewsLauncher(
            state_dir=self.state,
            governance_paths={"blind_reviews": self.governance(
                "employee-reviews-blind")},
            credential_grant_path=self.root / "grant.json",
        )
        self.addCleanup(launcher.close)
        command = launcher._command(
            ticket_dir=self.root, operation="blind_reviews",
            params={"employer_slug": "SyntheticCo", "pages": 2, "since": ""})
        self.assertNotIn("--credential-grant", command)

    def test_the_x_launcher_strips_an_at_sign_from_a_handle(self):
        launcher = XreachLauncher(
            state_dir=self.state,
            governance_paths={"user_timeline": self.governance(
                "x-xreach-user-timeline")},
        )
        self.addCleanup(launcher.close)
        cleaned = launcher._validate("user_timeline", {"handle": "@SyntheticCo"})
        self.assertEqual(cleaned["handle"], "SyntheticCo")

    def test_the_command_a_launcher_builds_really_runs_a_child(self):
        """An argv nobody has run is an argv nobody has checked.

        Run here rather than through ``spawn`` because the launcher runs its
        child with the state directory as the working directory, and a Core
        that is on the path only relatively -- which is how this suite is run
        and not how a deployment is -- would not be found from there. What is
        under test is the command, so the command is what is run.
        """

        page = self.root / "page.html"
        page.write_bytes(blind_page(unlocked=1, locked=1))
        launcher = EmployeeReviewsLauncher(
            state_dir=self.state,
            governance_paths={"blind_reviews": self.governance(
                "employee-reviews-blind")},
            mode_args=("--fixture-file", str(page)),
        )
        self.addCleanup(launcher.close)
        out = self.root / "out"
        out.mkdir()
        command = launcher._command(
            ticket_dir=out, operation="blind_reviews",
            params={"employer_slug": "SyntheticCo", "pages": 2, "since": ""})
        environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        finished = subprocess.run(command, cwd=str(ROOT), env=environment,
                                  capture_output=True, timeout=120, check=False)
        self.assertEqual(finished.returncode, 0, finished.stderr.decode())
        summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "succeeded", summary)
        self.assertEqual(summary["record_count"], 2)
        self.assertEqual(summary["body_locked_count"], 1)

    def test_the_same_request_is_the_same_ticket(self):
        launcher = self.xueqiu()
        first = launcher._command(ticket_dir=self.root, operation="search_posts",
                                  params={"query": "a", "since": ""})
        second = launcher._command(ticket_dir=self.root, operation="search_posts",
                                   params={"query": "a", "since": ""})
        self.assertEqual(first, second)


class MapTests(unittest.TestCase):
    def test_the_committed_map_covers_the_five_mission_companies(self):
        loaded = load_crowd_source_map(MAP_PATH)
        self.assertEqual([item["ticker"] for item in loaded["companies"]],
                         ["ACN", "CTSH", "EPAM", "IBM", "DXC"])
        for company in loaded["companies"]:
            self.assertTrue(company["company_ref"].startswith("company:"))
            self.assertTrue(company["employer_slug"])
            self.assertTrue(company["x_handles"])

    def test_a_map_of_the_wrong_version_is_refused(self):
        with TemporaryDirectory() as temp:
            path = write_json(Path(temp) / "map.json",
                              {"schema_version": "9.9", "companies": []})
            with self.assertRaises(CrowdSourceLaneError):
                load_crowd_source_map(path)

    def test_a_company_without_a_ticker_is_refused(self):
        with TemporaryDirectory() as temp:
            path = write_json(Path(temp) / "map.json", {
                "schema_version": "0.1",
                "companies": [{"company_ref": "company:x"}]})
            with self.assertRaises(CrowdSourceLaneError):
                load_crowd_source_map(path)


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = Path(self._temp.name) / "crowd-observations.jsonl"

    def entries(self, posts: int, first_id: int = 1) -> list[dict[str, Any]]:
        return observation_entries(
            source_ref="source:xueqiu", company_ref="company:x",
            operation="search_posts",
            summary=summary_with(posts, first_id=first_id),
            observed_at="2026-09-09T00:00:00.000000+00:00")

    def test_every_entry_carries_the_crowd_grade_and_its_importance(self):
        for entry in self.entries(2):
            self.assertEqual(entry["grade"], CROWD_GRADE)
            self.assertEqual(entry["importance"], CROWD_IMPORTANCE)
            self.assertEqual(entry["spec_ref"], "xueqiu-post")

    def test_an_entry_names_the_artifact_and_the_approval_it_came_under(self):
        entry = self.entries(1)[0]
        self.assertEqual(entry["artifact_hash"], "d" * 64)
        self.assertTrue(entry["governance_ref"])

    def test_the_same_post_read_twice_is_recorded_once(self):
        ledger = CrowdObservationLedger(self.path)
        first = ledger.record(self.entries(2))
        second = ledger.record(self.entries(3))
        self.assertEqual(len(first["recorded"]), 2)
        self.assertEqual(len(second["recorded"]), 1)
        self.assertEqual(len(second["duplicates"]), 2)

    def test_the_dedupe_survives_a_restart(self):
        CrowdObservationLedger(self.path).record(self.entries(2))
        reopened = CrowdObservationLedger(self.path)
        self.assertTrue(reopened.known("source:xueqiu", "1"))
        self.assertEqual(reopened.record(self.entries(2))["recorded"], [])

    def test_the_ledger_is_owner_only(self):
        CrowdObservationLedger(self.path).record(self.entries(1))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_a_run_without_an_observation_is_refused(self):
        with self.assertRaises(CrowdSourceLaneError):
            observation_entries(source_ref="source:x", company_ref="company:x",
                                operation="search", summary={"observation": None},
                                observed_at="2026-09-09T00:00:00.000000+00:00")

    def test_a_locked_review_body_travels_as_a_flag(self):
        summary = {
            "governance_ref": "g", "governance_hash": "h", "artifact": {},
            "observation": {"reviews": [
                {"review_id": "l0", "created_at": "2026-08-01T00:00:00Z",
                 "body_locked": True},
                {"review_id": "r0", "created_at": "2026-09-01T00:00:00Z",
                 "body_locked": False}]},
        }
        entries = observation_entries(
            source_ref="source:blind", company_ref="company:x",
            operation="blind_reviews", summary=summary,
            observed_at="2026-09-09T00:00:00.000000+00:00")
        self.assertEqual([item["body_locked"] for item in entries], [True, False])
        self.assertEqual(entries[0]["spec_ref"], "blind-employee-review")


class CoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.ledger = CrowdObservationLedger(self.root / "ledger.jsonl")
        self.launchers = {
            "xueqiu": FakeLauncher("source:xueqiu"),
            "x": FakeLauncher("source:x"),
            "employee-reviews": FakeLauncher("source:blind"),
        }
        self.source_map = load_crowd_source_map(MAP_PATH)

    def coordinator(self, mission_value: Any) -> MissionCrowdSourceLaneCoordinator:
        return MissionCrowdSourceLaneCoordinator(
            mission=lambda: mission_value, runners=self.launchers,
            source_map=self.source_map, ledger=self.ledger,
            clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc))

    def test_the_lane_needs_both_write_scopes(self):
        self.assertEqual(LANE_GRANTS, {"observation", "source_discovery"})
        result = self.coordinator(mission(may_write=["observation"])).dispatch_once()
        self.assertEqual(result["status"], "gated")
        self.assertIn("source_discovery", result["reason"])

    def test_no_mission_is_unconfigured_rather_than_a_crash(self):
        self.assertEqual(self.coordinator(None).dispatch_once()["status"],
                         "unconfigured")

    def test_a_source_the_mission_has_not_connected_is_held(self):
        result = self.coordinator(
            mission(connected=("source:xueqiu",))).dispatch_once()
        held = {item["source"] for item in result["sources"]
                if item["status"] == "held"}
        self.assertEqual(held, {"x", "employee-reviews"})

    def test_one_child_per_source_per_tick(self):
        coordinator = self.coordinator(mission())
        result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(
            sorted(item["source"] for item in result["sources"]
                   if item["status"] == "launched"),
            ["employee-reviews", "x", "xueqiu"])
        for launcher in self.launchers.values():
            self.assertEqual(len(launcher.started), 1)

    def test_a_source_with_a_child_in_flight_is_busy_not_doubled(self):
        coordinator = self.coordinator(mission())
        coordinator.dispatch_once()
        again = coordinator.dispatch_once()
        self.assertTrue(all(item["status"] == "busy" for item in again["sources"]))
        self.assertEqual(len(self.launchers["xueqiu"].started), 1)

    def test_a_finished_child_is_recorded_with_the_crowd_grade(self):
        coordinator = self.coordinator(mission())
        coordinator.dispatch_once()
        self.launchers["xueqiu"].finish("ticket-0", summary_with(2))
        settled = coordinator.dispatch_once()["settled"]
        recorded = [item for item in settled if item["source"] == "xueqiu"]
        self.assertEqual(recorded[0]["outcome"], "succeeded")
        self.assertEqual(recorded[0]["recorded"], 2)
        self.assertEqual(recorded[0]["grade"], CROWD_GRADE)

    def test_the_same_posts_on_a_later_tick_are_duplicates(self):
        coordinator = self.coordinator(mission())
        coordinator.dispatch_once()
        self.launchers["xueqiu"].finish("ticket-0", summary_with(2))
        coordinator.dispatch_once()
        self.launchers["xueqiu"].finish("ticket-1", summary_with(2))
        settled = coordinator.dispatch_once()["settled"]
        again = [item for item in settled if item["source"] == "xueqiu"][0]
        self.assertEqual(again["recorded"], 0)
        self.assertEqual(again["duplicates"], 2)

    def test_a_failed_child_stops_that_company_being_retried_forever(self):
        coordinator = self.coordinator(mission())
        coordinator.dispatch_once()
        first_company = self.launchers["xueqiu"].started[0]
        self.launchers["xueqiu"].finish(
            "ticket-0", {"failure_reason": "XueqiuRunError: nothing there"},
            status="failed")
        settled = coordinator.dispatch_once()["settled"]
        failed = [item for item in settled if item["source"] == "xueqiu"][0]
        self.assertEqual(failed["outcome"], "failed")
        self.assertIn("nothing there", failed["failure_reason"])
        # The next tick moves on to a different company rather than retrying.
        self.assertNotEqual(self.launchers["xueqiu"].started[-1], first_company)

    def test_a_rejected_launch_does_not_break_the_tick(self):
        self.launchers["x"].reject = "the record is not approved"
        result = self.coordinator(mission()).dispatch_once()
        statuses = {item["source"]: item["status"] for item in result["sources"]}
        self.assertEqual(statuses["x"], "rejected")
        self.assertEqual(statuses["xueqiu"], "launched")

    def test_a_held_company_is_retried_after_the_cool_off(self):
        """A failure is a pause, not a verdict.

        The first version never cleared the failure set, so one pass over five
        companies on a Core with no credential bound left the lane reporting
        `idle` for the life of the process -- the same word it uses when there
        is genuinely nothing to do.
        """

        from dalton_core.mission_crowd_source_lane import FAILURE_COOL_OFF_TICKS

        coordinator = self.coordinator(mission())
        self.launchers["xueqiu"].reject = "the record is not approved"
        for _ in range(len(self.source_map["companies"])):
            coordinator.dispatch_once()
        self.assertEqual(len(coordinator.held()), 5)
        self.launchers["xueqiu"].reject = None
        for _ in range(FAILURE_COOL_OFF_TICKS):
            coordinator.dispatch_once()
        self.assertEqual(coordinator.held(), {})
        self.assertTrue(self.launchers["xueqiu"].started)

    def test_a_tick_says_what_is_held_rather_than_only_that_it_is_idle(self):
        coordinator = self.coordinator(mission())
        self.launchers["xueqiu"].reject = "the record is not approved"
        result = coordinator.dispatch_once()
        self.assertTrue(any(key.startswith("xueqiu|") for key in result["held"]))

    def test_an_approval_clears_the_holds_immediately(self):
        coordinator = self.coordinator(mission())
        self.launchers["xueqiu"].reject = "the record is not approved"
        coordinator.dispatch_once()
        self.assertTrue(coordinator.held())
        # A record whose hash moved is a different record; whatever the last
        # failure was, it was about the old one.
        coordinator._configuration = "something else entirely"
        coordinator.dispatch_once()
        self.assertNotIn("xueqiu|company:sec-cik:0001467373", coordinator.held())

    def test_each_source_asks_for_the_thing_the_map_gave_it(self):
        self.coordinator(mission()).dispatch_once()
        self.assertEqual(self.launchers["xueqiu"].started[0]["operation"],
                         "search_posts")
        self.assertEqual(self.launchers["x"].started[0]["handle"], "Accenture")
        self.assertEqual(
            self.launchers["employee-reviews"].started[0]["employer_slug"],
            "Accenture")


class LaneRegistrationTests(unittest.TestCase):
    """The lane is registered, and it is off on a Core that has not approved.

    All seven governance records ship `proposed`, so a deployment picks this
    lane up only once the owner has approved at least one of them and the
    per-mission map is on disk. Until then `argv_fragment` returns nothing and
    the writer never learns the lane exists, which is the correct behaviour for
    a connector nobody has agreed to yet.
    """

    def setUp(self) -> None:
        from dalton_core import lane_registry

        lane_registry.load_lanes()
        self.registry = lane_registry

    def test_the_lane_is_registered_last_in_the_tick(self):
        from dalton_core.mission_crowd_source_lane import LANE

        self.assertEqual(LANE.operation, "dispatch_mission_crowd_sources")
        self.assertEqual(self.registry.tick_lanes()[-1].operation, LANE.operation)

    def test_an_empty_state_directory_leaves_the_lane_off(self):
        from dalton_core.mission_crowd_source_lane import argv_fragment

        with TemporaryDirectory() as temp:
            context = type("Context", (), {"state": Path(temp)})()
            self.assertEqual(argv_fragment(context), [])

    def seeded(self, temp: str, *, status: str) -> Any:
        from dalton_core.connector_governance import build_governance_record
        from dalton_core.mission_crowd_source_lane import CROWD_SOURCE_MAP

        state = Path(temp)
        (state / "phase9").mkdir()
        (state / "phase9" / CROWD_SOURCE_MAP).write_text("{}", encoding="utf-8")
        (state / "connector-governance").mkdir()
        record = build_governance_record("employee-reviews-blind",
                                         approved_by="human:tester", status=status)
        (state / "connector-governance" / "employee-reviews-blind-v1.json").write_text(
            json.dumps(record, sort_keys=True), encoding="utf-8")
        return type("Context", (), {"state": state})()

    def test_a_proposed_record_on_disk_does_not_turn_the_lane_on(self):
        """The installer copies the records; the owner approves them.

        A check for the file existing turns the lane on for records nobody has
        agreed to, and then every child refuses once a tick forever.
        """

        from dalton_core.mission_crowd_source_lane import argv_fragment

        with TemporaryDirectory() as temp:
            self.assertEqual(
                argv_fragment(self.seeded(temp, status="proposed")), [])

    def test_an_approved_record_and_a_map_turn_it_on(self):
        from dalton_core.mission_crowd_source_lane import argv_fragment

        with TemporaryDirectory() as temp:
            fragment = argv_fragment(self.seeded(temp, status="approved"))
            self.assertIn("--crowd-source-map", fragment)

    def test_a_writer_without_the_lane_says_so_rather_than_crashing(self):
        from dalton_core.mission_crowd_source_lane import dispatch

        server = type("Server", (), {"lane_launcher": lambda self, key: None})()
        self.assertEqual(dispatch(server, {})["status"], "unconfigured")
