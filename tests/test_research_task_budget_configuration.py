"""Configured task bounds reach admission, tickets and the actual planner reserve."""
import json
from decimal import Decimal
from unittest.mock import patch

from dalton_core import research_task as rt
from dalton_core.mission_research_task_lane import lane_configuration
from dalton_core.research_task_cli import run_admissions
from dalton_core.research_task_launcher import ResearchTaskLauncher
from tests.test_research_task import ResearchTaskFixture, inquiry


class ConfiguredTaskTests(ResearchTaskFixture):
    daily_cost_usd = 100.0

    def service_budget(self, cost):
        # Use an actual installed-shaped directory, confined to this fixture.
        path = self.state_dir / "config" / "service.json"
        path.parent.mkdir(exist_ok=True)
        raw = dict.fromkeys((
            "observation_mandate_version_ref", "doctrine_pack_version_ref",
            "doctrine_pack_version_hash", "planner_routing_policy_ref",
            "planner_credential_slot_refs", "planner_model_router_db",
            "planner_broker_socket", "planner_broker_auth_key",
        ))
        raw.update({
            "writer_socket": str(self.state_dir / "writer.sock"),
            "token_config": str(self.state_dir / "tokens.json"),
            "scheduler_db": str(self.state_dir / "scheduler.sqlite"),
            "user_agent": "Dalton test", "max_response_bytes": 1000,
            "timeout_seconds": 5, "max_probes_per_tick": 1,
            "filed_window_days": 400, "planner_broker_client_id": "client:dalton-core",
            "planner_expected_agent_id": "chem", "planner_max_cost_usd": 0.5,
            "planner_call_budget": {"max_cost_usd": cost},
        })
        path.write_text(json.dumps({"bounded_planner": {"config": raw}}))
        return path

    def test_more_than_three_admissions_and_four_rounds_are_configurable(self):
        config = self.state_dir / "research-task-lane.json"
        config.write_text(json.dumps({"max_admissions_per_tick": 8,
                                      "task_budget": {"max_rounds": 6}}))
        settings = lane_configuration(config)
        self.assertEqual(settings["max_admissions_per_tick"], 8)
        plan = self.record_plan([inquiry(question=f"Reconcile metric {i}", rank=i)
                                 for i in range(5)])
        entries = rt.plan_admissions(self.authority, mission=self.mission, plan=plan,
                                    limit=8, budget_overrides=settings["task_budget"])
        self.assertEqual(sum(e["admissible"] for e in entries), 5)
        self.assertEqual(entries[0]["budget"], {
            "max_rounds": 6, "max_cost_units": 6, "max_seconds": 720})

    def test_configured_planner_cost_reprices_admission_and_cockpit_pool(self):
        service = self.service_budget(2)
        nested = self.state_dir / "state" / "dalton-core"
        nested.mkdir(parents=True)
        self.assertEqual(rt.default_planner_cost_usd(nested), Decimal("2"))
        plan = self.record_plan([inquiry(question="Check revenue")])
        with patch.object(self.store, "path", str(nested / "core.sqlite")):
            entry = rt.plan_admissions(self.authority, mission=self.mission, plan=plan)[0]
            self.assertEqual(entry["estimated_micros"], 4_000_000)
            self.admit(plan, entry, plan["inquiries"][0])
            view = rt.research_task_view(self.store, mission=self.mission)
            self.assertEqual(view["pool"]["reserved_micros"], 4_000_000)
            self.assertEqual(view["companies"][0]["tasks"][0]["estimated_micros"], 4_000_000)
            service.write_text("{malformed")
            with self.assertRaisesRegex(rt.ResearchTaskError, "configured planner budget"):
                rt.plan_admissions(self.authority, mission=self.mission, plan=plan)

    def test_readonly_ask_pool_uses_the_same_installed_cost(self):
        import sqlite3
        from dalton_core.ask_refresh import pool_balance
        self.service_budget(2)
        nested = self.state_dir / "state" / "dalton-core"
        nested.mkdir(parents=True)
        plan = self.record_plan([inquiry(question="Check revenue")])
        entry = self.admissions(plan)[0]
        self.admit(plan, entry, plan["inquiries"][0])
        target = nested / "core.sqlite"
        writer = sqlite3.connect(target)
        self.store.connection.backup(writer)
        writer.close()
        reader = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)
        reader.row_factory = sqlite3.Row
        try:
            from datetime import datetime, timezone
            balance = pool_balance(reader, self.mission,
                day=datetime.now(timezone.utc).date().isoformat())
            self.assertEqual(balance["reserved_micros"], 4_000_000)
        finally:
            reader.close()

    def test_configuration_change_rekeys_ticket_and_command(self):
        config = self.state_dir / "research-task-lane.json"
        config.write_text(json.dumps({"max_admissions_per_tick": 5}))
        launcher = ResearchTaskLauncher(state_dir=self.state_dir, config_path=config)
        with patch.object(launcher, "spawn", side_effect=lambda **kwargs: kwargs):
            first = launcher.start(plan_ref="plan:one", signature="same")
            self.assertEqual(first, launcher.start(plan_ref="plan:one", signature="same"))
            config.write_text(json.dumps({"max_admissions_per_tick": 8,
                                          "task_budget": {"max_rounds": 7}}))
            second = launcher.start(plan_ref="plan:one", signature="same")
        self.assertNotEqual(first["digest"], second["digest"])
        command = launcher._command(ticket_dir=self.state_dir,
                                    configuration=second["configuration"])
        self.assertEqual(command[command.index("--max-admissions") + 1], "8")
        self.assertEqual(json.loads(command[command.index("--task-budget") + 1]),
                         {"max_rounds": 7})

    def test_invalid_config_does_not_silently_restore_defaults(self):
        config = self.state_dir / "research-task-lane.json"
        for wire in ("broken", "[]", '{"max_admissions_per_tick":0}',
                     '{"retired_templates":false}', '{"task_budget":{"max_rounds":0}}'):
            with self.subTest(wire=wire):
                config.write_text(wire)
                with self.assertRaises(ValueError if 'max_rounds' in wire else rt.ResearchTaskError):
                    lane_configuration(config)

    def test_child_persists_the_selected_round_time_and_unit_bounds(self):
        self.record_plan([inquiry(question="Check revenue")])
        self.store.close()
        selected = {"max_rounds": 7, "max_cost_units": 5, "max_seconds": 350}
        result = run_admissions(state_dir=self.state_dir, summary_dir=self.state_dir,
                                max_admissions=8, task_budget=selected)
        self.assertEqual(result["admitted"], 1)
        self.assertEqual(result["tasks"][0]["budget"], selected)
        self.assertEqual(result["pool"]["reserved_micros"], 3_500_000)
