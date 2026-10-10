"""A workspace owner can schedule a weekly brief, and it can land without Discord.

Review 2026-09-30, gap d2: legacy's weekly brief was a hand-made plan file, a
policy edit, a constitution and mission cascade, a ``service.json`` edit and
a controller restart.  A workspace had no entry point at all.  Pinned here:
the plan is derived from the mission, published through the real governance
cascade in one call, idempotent and CAS-guarded; the controller reads it from
a file (no restart) and needs no outbox for a local destination; and a local
delivery writes the exact Markdown and records the delivery in Core.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.research_constitution import ResearchConstitutionAuthority
from dalton_core.store import DaltonStore, content_hash
from dalton_core.weekly_brief_coordinator import (
    WEEKLY_BRIEF_AUTO_PUBLISH_RULE_REF,
    WeeklyBriefCoordinator,
    WeeklyBriefCoordinatorConfig,
    WeeklyBriefSchedulePlan,
    _validate_policy,
)
from dalton_core.weekly_brief_workspace_plan import (
    LocalWeeklyBriefDelivery,
    WorkspaceWeeklyBriefConflict,
    WorkspaceWeeklyBriefError,
    build_workspace_weekly_brief_plan,
    is_local_destination,
    local_briefs,
    local_destination_ref,
    publish_workspace_weekly_brief_plan,
    read_plan,
)
from tests.p9a_fixtures import OWNER, bootstrap_method_authorities, mission_params

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
SCHEDULE = {"weekday": 2, "hour": 7, "minute": 0, "timezone": "America/New_York"}


class PlanTests(unittest.TestCase):
    def test_the_plan_is_named_after_the_mission_and_parses_as_a_refresh_plan(self):
        mission = {"mission_ref": "coverage-mission:ws-abc",
                   "universe": [{"company_ref": "company:ticker:wdgt", "ticker": "WDGT"},
                                {"company_ref": "company:ticker:gzmo", "ticker": "GZMO"}]}
        plan = build_workspace_weekly_brief_plan(
            mission, brief_slug="ws-abc", version=1, **SCHEDULE,
            destination_ref=local_destination_ref("ws-abc"),
            effective_from=NOW.isoformat())
        self.assertEqual(plan["plan_ref"], "weekly-brief-plan:ws-abc:v1")
        self.assertEqual(plan["brief_ref"], "weekly-brief:ws-abc")
        self.assertEqual(plan["evidence_refresh"]["evidence_pack_ref"],
                         "industry-evidence-pack:ws-abc")
        self.assertEqual(plan["evidence_refresh"]["company_overlay_refs"],
                         ["company-overlay:wdgt", "company-overlay:gzmo"])
        self.assertTrue(is_local_destination(plan["destination_ref"]))
        WeeklyBriefSchedulePlan.from_mapping(plan)

    def test_a_bad_schedule_is_refused(self):
        mission = {"mission_ref": "coverage-mission:x", "universe": [{"ticker": "A"}]}
        with self.assertRaises(WorkspaceWeeklyBriefError):
            build_workspace_weekly_brief_plan(
                mission, brief_slug="x", version=1, weekday=9, hour=7, minute=0,
                timezone="UTC", destination_ref="cockpit:weekly-brief:x",
                effective_from=NOW.isoformat())


class PublishTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / "state"
        self.state.mkdir()
        self.store = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(self.store.close)
        method = bootstrap_method_authorities(self.store)
        params = mission_params(method)
        self.mission = CoverageMissionAuthority(self.store).create_mission(
            params.pop("mission_ref"), **params)

    def publish(self, expected=None, **overrides):
        values = {**SCHEDULE, "destination_ref": None, **overrides}
        return publish_workspace_weekly_brief_plan(
            self.store, self.state, expected_plan_hash=expected, actor_ref=OWNER,
            now=NOW, **values)

    def test_publishing_authorizes_the_exact_plan_and_cascades(self):
        predicates = self.store.active_policy_version().to_dict()["independence_predicates"]
        result = self.publish()
        self.assertEqual(result["status"], "published")
        plan = read_plan(self.state)
        self.assertEqual(plan, result["plan"])
        self.assertTrue(result["local"])
        self.assertTrue((self.state / "weekly-brief-plans" /
                         f"{content_hash(plan)}.json").is_file())
        schedule = WeeklyBriefSchedulePlan.from_mapping(plan)
        active = self.store.active_policy()
        # The coordinator's own gate passes on the published policy.
        _validate_policy(active, schedule, NOW)
        rule = active["policy"]["weekly_brief_auto_publish"]
        self.assertEqual(rule["rule_ref"], WEEKLY_BRIEF_AUTO_PUBLISH_RULE_REF)
        self.assertEqual(rule["max_issues_per_week"], 1)
        missions = CoverageMissionAuthority(self.store)
        mission = missions.active_mission(self.mission["mission_ref"])
        self.assertNotEqual(mission["id"], self.mission["id"])
        constitution = ResearchConstitutionAuthority(self.store).constitution(
            mission["bindings"]["constitution_version"]["ref"])
        self.assertEqual(constitution["bindings"]["weekly_brief_plan"],
                         {"ref": schedule.plan_ref, "hash": schedule.content_hash})
        self.assertEqual(constitution["bindings"]["governance_policy_version"]["ref"],
                         active["policy_version_id"])
        # The independence predicates survive the policy edit.
        self.assertEqual(self.store.active_policy_version().to_dict()["independence_predicates"],
                         predicates)

    def test_the_same_schedule_again_changes_nothing(self):
        first = self.publish()
        policy = self.store.active_policy()["policy_version_id"]
        again = self.publish(expected=first["plan_hash"])
        self.assertEqual(again["status"], "unchanged")
        self.assertEqual(self.store.active_policy()["policy_version_id"], policy)

    def test_a_new_time_is_the_next_version_and_keeps_the_old_binding(self):
        first = self.publish()
        second = self.publish(expected=first["plan_hash"], hour=9)
        self.assertEqual(second["plan"]["plan_ref"].rsplit(":", 1)[1], "v2")
        bindings = self.store.active_policy()["policy"][
            "weekly_brief_auto_publish"]["allowed_plan_bindings"]
        self.assertEqual(len(bindings), 2)

    def test_a_stale_page_cannot_overwrite(self):
        self.publish()
        with self.assertRaises(WorkspaceWeeklyBriefConflict):
            self.publish(expected=None, hour=10)

    def test_the_writer_operation_is_a_human_governance_door(self):
        from dalton_core.writer_server import (
            HUMAN_GOVERNANCE_OPERATIONS, OPERATION_ACTOR_FIELDS, OPERATION_FIELDS,
        )

        self.assertIn("publish_workspace_weekly_brief_plan", HUMAN_GOVERNANCE_OPERATIONS)
        self.assertEqual(OPERATION_ACTOR_FIELDS["publish_workspace_weekly_brief_plan"],
                         "actor_ref")
        self.assertEqual(OPERATION_FIELDS["publish_workspace_weekly_brief_plan"], frozenset({
            "weekday", "hour", "minute", "timezone", "destination_ref",
            "expected_plan_hash", "actor_ref"}))


class FakeClient:
    def __init__(self, claims=(), cycle=None):
        self.claims = list(claims)
        self.cycle = cycle or {"status": "ready"}
        self.calls: list[tuple[str, dict]] = []

    def run_weekly_brief_cycle(self, **params):
        self.calls.append(("run_weekly_brief_cycle", params))
        return dict(self.cycle)

    def claim_agenda_outbox(self, **params):
        self.calls.append(("claim_agenda_outbox", params))
        claims, self.claims = self.claims, []
        return {"claims": claims}

    def record_scheduled_weekly_brief_delivery(self, **params):
        self.calls.append(("record_scheduled_weekly_brief_delivery", params))
        return {"status": "fresh"}

    def record_agenda_delivery(self, **params):
        self.calls.append(("record_agenda_delivery", params))
        return {"status": "recorded"}


def _claim(body: str, destination: str) -> dict:
    return {"message_id": "m1", "delivery_attempt_id": "a1", "payload": {
        "schema_version": "0.1", "kind": "weekly_research_brief", "cycle_ref": "c1",
        "issue_version_ref": "weekly-brief-version:1", "issue_version_hash": "b" * 64,
        "brief_ref": "weekly-brief:x", "industry_ref": "industry:x", "period": {},
        "destination_ref": destination,
        "artifact_sha256": hashlib.sha256(body.encode()).hexdigest(), "body": body,
        "created_at": "2026-09-30T11:00:00.000000+00:00"}}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)

    def config(self):
        return WeeklyBriefCoordinatorConfig.from_mapping({
            "writer_socket": str(self.state / "run" / "writer.sock"),
            "token_config": str(self.state / "writer-tokens.json"),
            "plan_path": str(self.state / "weekly-brief-plan.json")})

    def test_no_plan_file_is_a_quiet_no_plan(self):
        client = FakeClient()
        result = WeeklyBriefCoordinator(self.config(), client=client).run_once(now=NOW)
        self.assertEqual(result["status"], "no_plan")
        self.assertEqual(client.calls, [])

    def test_a_local_plan_runs_and_delivers_into_the_state_directory(self):
        destination = local_destination_ref("x")
        plan = build_workspace_weekly_brief_plan(
            {"mission_ref": "coverage-mission:x", "universe": [{"ticker": "A"}]},
            brief_slug="x", version=1, **SCHEDULE, destination_ref=destination,
            effective_from="2026-09-01T00:00:00+00:00")
        (self.state / "weekly-brief-plan.json").write_text(json.dumps(plan))
        body = "# Weekly brief\n\nhermetic fixture\n"
        client = FakeClient(claims=[_claim(body, destination)])
        result = WeeklyBriefCoordinator(self.config(), client=client).run_once(now=NOW)
        self.assertEqual(result["local_delivery"]["delivered"], 1)
        files = list((self.state / "weekly-briefs").glob("*.md"))
        self.assertEqual([path.read_text() for path in files], [body])
        self.assertEqual(len(local_briefs(self.state)), 1)
        names = [name for name, _ in client.calls]
        self.assertEqual(names, ["run_weekly_brief_cycle", "claim_agenda_outbox",
                                 "record_scheduled_weekly_brief_delivery",
                                 "record_agenda_delivery"])
        recorded = dict(client.calls[2][1])
        self.assertTrue(recorded["external_message_ref"].startswith("file:"))
        self.assertEqual(recorded["delivered_at"], "2026-09-30T11:00:00.000000+00:00")
        self.assertEqual(client.calls[3][1]["state"], "delivered")

    def test_a_tampered_body_is_not_written_and_is_recorded_failed(self):
        destination = local_destination_ref("x")
        claim = _claim("real", destination)
        claim["payload"]["body"] = "tampered"
        client = FakeClient(claims=[claim])
        result = LocalWeeklyBriefDelivery(
            client, endpoint_ref=destination, directory=self.state / "weekly-briefs",
        ).run_once(now=NOW)
        self.assertEqual(result["failed"], 1)
        self.assertFalse((self.state / "weekly-briefs").exists())
        self.assertEqual(client.calls[-1][1]["state"], "failed")


class ServiceConfigTests(unittest.TestCase):
    def test_a_plan_file_needs_no_outbox(self):
        from dalton_core.bootstrap import bootstrap
        from dalton_core.service import ServiceConfig

        with tempfile.TemporaryDirectory() as temp:
            state, config_path = Path(temp) / "state", Path(temp) / "service.json"
            bootstrap(state, config_path)
            raw = json.loads(config_path.read_text())
            raw["weekly_brief"] = {"enabled": True, "interval_seconds": 300, "config": {
                "writer_socket": str(state / "run" / "writer.sock"),
                "token_config": str(state / "writer-tokens.json"),
                "plan_path": str(state / "weekly-brief-plan.json")}}
            config = ServiceConfig.from_mapping(raw)
            self.assertIsNone(config.weekly_brief.plan)
            self.assertEqual(config.weekly_brief.plan_path, state / "weekly-brief-plan.json")
            # The inline, non-local form still requires the outbox bridge.
            plan = build_workspace_weekly_brief_plan(
                {"mission_ref": "coverage-mission:x", "universe": [{"ticker": "A"}]},
                brief_slug="x", version=1, **SCHEDULE,
                destination_ref="openclaw:discord:default:channel:1",
                effective_from=NOW.isoformat())
            raw["weekly_brief"]["config"] = {
                "writer_socket": str(state / "run" / "writer.sock"),
                "token_config": str(state / "writer-tokens.json"), "plan": plan}
            with self.assertRaisesRegex(Exception, "outbox"):
                ServiceConfig.from_mapping(raw)


class RouteTests(unittest.TestCase):
    def test_view_and_publish_routes_go_through_the_plane(self):
        from types import SimpleNamespace

        from dalton_core.agenda_control import AgendaControlApplication, _Session

        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            DaltonStore(str(state / "core.sqlite")).close()
            calls = []

            class Plane:
                config = SimpleNamespace(state_dir=state, core_db=state / "core.sqlite")

                def _governance(self, login, operation, params, *, failure):
                    calls.append((login, operation, params))
                    return {"status": "published"}

            application = AgendaControlApplication(None, None, cockpit_plane=Plane())
            view = application.cockpit_view("/v1/cockpit/weekly-brief", OWNER, {})
            self.assertIsNone(view["plan"])
            self.assertEqual(view["local_briefs"], [])
            session = _Session(login=OWNER, csrf="token", expires_at=9e18)
            result = application.post_cockpit(
                "weekly_brief_plan", OWNER, session, "token",
                json.dumps({**SCHEDULE, "destination_ref": "  "}).encode())
            self.assertEqual(result["status"], "published")
            self.assertEqual(calls[0][1], "publish_workspace_weekly_brief_plan")
            self.assertIsNone(calls[0][2]["destination_ref"])

    def test_the_page_ships_with_the_package(self):
        import dalton_core

        page = Path(dalton_core.__file__).with_name("cockpit_weekly_brief.html")
        self.assertIn("/v1/cockpit/weekly_brief_plan", page.read_text(encoding="utf-8"))
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        self.assertIn('"cockpit_weekly_brief.html"', pyproject.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
