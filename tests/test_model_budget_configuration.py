import json
import unittest
import threading
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from dalton_core.call_budget import CallBudgetError
from dalton_core.cockpit_model import CockpitModel
from dalton_core.model_budget_configuration import call_budget_view, set_model_call_budget
from dalton_core.model_selection import ModelSelectionError
from tests import test_model_selection as fixtures
OWNER = fixtures.OWNER


class BudgetConfigurationTests(fixtures.StateDirectoryCase):
    def setUp(self):
        super().setUp()
        self.event = self.root / "event-judgement-model-config.json"
        self.event.write_text(json.dumps(self.model_config))

    def save(self, budget, *, purpose="event_judgement", expected=None, actor=OWNER):
        view = call_budget_view(self.root, purpose)
        return set_model_call_budget(self.root, purpose=purpose, budget=budget,
                                     expected_config_hash=expected or view["config_hash"], actor_ref=actor)

    def test_budget_change_is_purpose_scoped_audited_and_route_preserving(self):
        original = json.loads(self.event.read_text())
        result = self.save({"max_cost_usd": 1.5, "max_output_tokens": 2200})
        self.assertEqual(result["effective"]["max_cost_usd"], 1.5)
        self.assertEqual(result["effective"]["max_output_tokens"], 2200)
        self.assertEqual(call_budget_view(self.root, "thesis_reflection")["effective"]["max_cost_usd"], 1.0)
        updated = json.loads(self.event.read_text())
        self.assertEqual({k: updated[k] for k in original}, original)
        receipt = json.loads(next((self.root / "model-budget-revisions").glob("*.json")).read_text())
        self.assertEqual(receipt["actor_ref"], OWNER)
        self.assertEqual(receipt["budget"], {"max_cost_usd": 1.5, "max_output_tokens": 2200})
        self.assertEqual(self.save({"max_cost_usd": 1.5, "max_output_tokens": 2200})["status"], "unchanged")
        self.assertEqual(len(list((self.root / "model-budget-revisions").glob("*.json"))), 1)

    def test_stale_configuration_refuses_without_overwriting_new_route(self):
        prior = call_budget_view(self.root, "event_judgement")["config_hash"]
        raw = json.loads(self.event.read_text()); raw["routing_policy_ref"] = "policy:new"
        self.event.write_text(json.dumps(raw))
        with self.assertRaisesRegex(ModelSelectionError, "changed"):
            self.save({"max_cost_usd": 1.0}, expected=prior)
        self.assertEqual(json.loads(self.event.read_text()), raw)

    def test_concurrent_shared_config_edits_cannot_erase_another_purpose(self):
        original_hash = call_budget_view(self.root, "event_judgement")["config_hash"]
        ready = threading.Barrier(2)
        def save(purpose):
            ready.wait()
            try:
                return self.save({"max_cost_usd": 2}, purpose=purpose, expected=original_hash)
            except ModelSelectionError:
                return {"status": "conflict", "purpose": purpose}
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(save, ["event_judgement", "thesis_reflection"]))
        self.assertEqual(sorted(x["status"] for x in results), ["conflict", "updated"])
        failed = next(x["purpose"] for x in results if x["status"] == "conflict")
        self.save({"max_cost_usd": 2}, purpose=failed)
        stored = json.loads(self.event.read_text())["purpose_call_budgets"]
        self.assertEqual(stored["event_judgement"]["max_cost_usd"], 2)
        self.assertEqual(stored["thesis_reflection"]["max_cost_usd"], 2)

    def test_lost_success_can_retry_same_budget_with_old_hash(self):
        prior = call_budget_view(self.root, "event_judgement")["config_hash"]
        self.save({"max_cost_usd": 2}, expected=prior)
        self.assertEqual(self.save({"max_cost_usd": 2}, expected=prior)["status"], "unchanged")
        self.assertEqual(len(list((self.root / "model-budget-revisions").glob("*.json"))), 1)

    def test_invalid_or_automated_budget_cannot_write(self):
        original = self.event.read_bytes()
        for value in ({"max_cost_usd": -1}, {"max_input_tokens": True}, {"oops": 1}, {"max_cost_usd": float("nan")}):
            with self.subTest(value=value), self.assertRaises(CallBudgetError):
                self.save(value)
        with self.assertRaises(CallBudgetError):
            self.save({"max_cost_usd": 1}, actor="automation:coverage-mission")
        self.assertEqual(original, self.event.read_bytes())

    def test_run_budget_is_separate_and_rejects_fields_the_stage_does_not_consume(self):
        view = call_budget_view(self.root, "event_judgement", kind="run")
        result = set_model_call_budget(self.root, purpose="event_judgement", kind="run",
            budget={"max_events": 12, "max_events_per_company": 4},
            expected_config_hash=view["config_hash"], actor_ref=OWNER)
        self.assertEqual(result["effective"]["max_events"], 12)
        self.assertEqual(call_budget_view(self.root, "event_judgement")["effective"]["max_cost_usd"], 1)
        with self.assertRaises(CallBudgetError):
            set_model_call_budget(self.root, purpose="event_judgement", kind="run",
                budget={"max_units": 5}, expected_config_hash=result["config_hash"], actor_ref=OWNER)

    def test_invalid_budget_does_not_break_the_model_page(self):
        raw = json.loads(self.event.read_text()); raw["call_budget"] = {"max_cost_usd": -1}
        self.event.write_text(json.dumps(raw))
        view = call_budget_view(self.root, "event_judgement")
        self.assertFalse(view["editable"])
        self.assertIn("预算配置无法读取", view["reason"])

    def test_reset_returns_to_declared_defaults_and_write_failure_keeps_history_honest(self):
        self.save({"max_cost_usd": 2})
        self.assertEqual(self.save({})["effective"]["max_cost_usd"], 1.0)
        before = self.event.read_bytes()
        history = list((self.root / "model-budget-revisions").glob("*.json"))
        with patch("dalton_core.model_budget_configuration._write_configs_atomically", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.save({"max_cost_usd": 3})
        self.assertEqual(before, self.event.read_bytes())
        self.assertEqual(history, list((self.root / "model-budget-revisions").glob("*.json")))

    def test_five_uncatalogued_purposes_save_and_reach_their_consumer(self):
        """An absent central default is a placeholder, not a disabled editor."""
        base = self.root / "installed"
        state = base / "state" / "dalton-core"
        state.mkdir(parents=True)
        config_dir = base / "config"
        config_dir.mkdir()
        initial = state / "initial-screen-model-config.json"
        extraction = state / "document-extraction-model-config.json"
        initial.write_text(json.dumps(self.model_config))
        extraction.write_text(json.dumps(self.model_config))
        (config_dir / "service.json").write_text(json.dumps({
            "control": {"config": {"cockpit": {
                "model_config_path": str(initial),
            }}},
        }))

        for purpose, path in (
            ("ask", initial), ("goal", initial), ("steer", initial),
            ("draft", initial), ("document_extraction", extraction),
        ):
            with self.subTest(purpose=purpose):
                before = json.loads(path.read_text())
                view = call_budget_view(state, purpose)
                self.assertTrue(view["editable"], view)
                if purpose == "document_extraction":
                    from dalton_core.document_extraction import LEGACY_CALL_BUDGET
                    self.assertEqual(view["effective"], LEGACY_CALL_BUDGET)
                else:
                    self.assertIsNone(view["effective"])
                result = set_model_call_budget(
                    state, purpose=purpose, budget={"max_cost_usd": 0.42},
                    expected_config_hash=view["config_hash"], actor_ref=OWNER,
                )
                self.assertEqual(result["status"], "updated")
                stored = json.loads(path.read_text())
                self.assertEqual(stored["routing_policy_ref"],
                                 before["routing_policy_ref"])
                consumer = CockpitModel(stored, scheduler_db=base / "scheduler.sqlite")
                self.assertEqual(consumer.budget_for(purpose)["max_cost_usd"], 0.42)

        from dalton_core.call_budget import resolve_call_budget
        from dalton_core.document_extraction import LEGACY_CALL_BUDGET
        configured = json.loads(extraction.read_text())
        effective = resolve_call_budget(
            configured, "document_extraction", defaults=LEGACY_CALL_BUDGET)
        self.assertEqual(effective["max_input_tokens"],
                         LEGACY_CALL_BUDGET["max_input_tokens"])
        self.assertEqual(effective["max_output_tokens"],
                         LEGACY_CALL_BUDGET["max_output_tokens"])
        self.assertEqual(effective["timeout_seconds"],
                         LEGACY_CALL_BUDGET["timeout_seconds"])


class BudgetGovernanceTests(unittest.TestCase):
    def test_invalid_direct_writer_budget_maps_to_contract_rejection(self):
        from dalton_core.writer_server import WriterServer
        server = object.__new__(WriterServer)
        from unittest.mock import PropertyMock
        with patch.object(WriterServer, "state_dir", new_callable=PropertyMock, return_value="/unused"), patch("dalton_core.model_budget_configuration.set_model_call_budget", side_effect=CallBudgetError("invalid")):
            try:
                server._op_set_model_call_budget({"purpose": "event_judgement", "budget": {},
                    "expected_config_hash": "a" * 64, "actor_ref": OWNER})
            except Exception as exc:
                self.assertEqual(server._error_code(exc), "rejected")
            else:
                self.fail("invalid budget was accepted")

    def test_writer_operation_is_human_only_and_actor_bound(self):
        from dalton_core import writer_server as w
        operation = "set_model_call_budget"
        self.assertIn(operation, w.HUMAN_GOVERNANCE_OPERATIONS)
        self.assertNotIn(operation, w.MISSION_AUTOMATION_OPERATIONS)
        self.assertNotIn(operation, w.CORE_OPERATIONS)
        self.assertEqual(w.OPERATION_ACTOR_FIELDS[operation], "actor_ref")
        self.assertNotIn("path", w.OPERATION_FIELDS[operation])

    def test_cockpit_forwards_budget_with_authenticated_actor(self):
        fixture = fixtures.CockpitModelPageTests()
        fixture.setUp()
        try:
            plane = fixture.plane(with_model_config=False)
            plane.set_call_budget("owner@example.test", {"purpose": "event_judgement",
                "budget": {"max_cost_usd": 1}, "expected_config_hash": "a" * 64})
            self.assertEqual(fixture.calls[-1][0], "set_model_call_budget")
            self.assertEqual(fixture.calls[-1][1]["budget"], {"max_cost_usd": 1})
        finally:
            fixture.doCleanups()


class ServiceBudgetTests(unittest.TestCase):
    def test_planner_budget_edit_preserves_service_and_requires_restart(self):
        import tempfile
        from pathlib import Path
        from tests.test_bounded_planner_driver import StalledLoopTests
        from dalton_core.bounded_planner_driver import BoundedPlannerDriverConfig
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            state = root / "state" / "dalton-core"; state.mkdir(parents=True)
            path = root / "config" / "service.json"; path.parent.mkdir()
            raw = json.loads(json.dumps(asdict(StalledLoopTests()._config(state)), default=str))
            raw.pop("planner_call_budget")
            raw.update(planner_routing_policy_ref="policy:planner", planner_credential_slot_refs=["slot:test"],
                       planner_model_router_db=str(state / "router.sqlite"),
                       planner_broker_socket=str(state / "broker.sock"), planner_broker_auth_key=str(state / "broker.key"),
                       planner_max_cost_usd=0.37)
            original = {"bounded_planner": {"enabled": True, "config": raw}, "unrelated": "preserved"}
            path.write_text(json.dumps(original))
            view = call_budget_view(state, "plan")
            self.assertTrue(view["editable"], view)
            self.assertEqual(view["effective"]["max_cost_usd"], 0.37)
            result = set_model_call_budget(state, purpose="plan", budget={"max_cost_usd": 0.9, "max_output_tokens": 1800},
                expected_config_hash=view["config_hash"], actor_ref=OWNER)
            self.assertTrue(result["requires_restart"])
            stored = json.loads(path.read_text())
            self.assertEqual(stored["unrelated"], "preserved")
            actual = BoundedPlannerDriverConfig.from_mapping(stored["bounded_planner"]["config"])
            self.assertEqual(actual.planner_call_budget["max_cost_usd"], 0.9)
            self.assertEqual(actual.planner_call_budget["max_output_tokens"], 1800)

    def test_thesis_budget_first_save_creates_only_budget_overlay(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            state = root / "state" / "dalton-core"; state.mkdir(parents=True)
            path = root / "config" / "service.json"; path.parent.mkdir()
            path.write_text(json.dumps({"thesis_impact": {"config": {
                "assessment_routing_policy_ref": "policy:assess", "verifier_routing_policy_ref": "policy:verify",
                "model_router_db": str(state / "router.sqlite")}}}))
            original = path.read_bytes()
            view = call_budget_view(state, "thesis_impact_assessment")
            self.assertTrue(view["editable"], view)
            result = set_model_call_budget(state, purpose="thesis_impact_assessment",
                budget={"max_cost_usd": 1}, expected_config_hash=view["config_hash"], actor_ref=OWNER)
            self.assertEqual(result["effective"]["max_cost_usd"], 1)
            self.assertEqual(call_budget_view(state, "thesis_impact_verifier")["effective"]["max_cost_usd"], .25)
            self.assertEqual(path.read_bytes(), original)
            stored = json.loads((state / "thesis-impact-budget-config.json").read_text())
            self.assertEqual(set(stored), {"purpose_call_budgets"})
