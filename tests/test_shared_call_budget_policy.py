import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.call_budget import resolve_call_budget
from dalton_core.shared_call_budget_policy import (SCHEMA_VERSION,
    SharedCallBudgetPolicyError, load_shared_call_budget_policy)
from dalton_core.store import content_hash


class SharedCallBudgetPolicyTests(unittest.TestCase):
    def policy(self, **changes):
        body = {"schema_version": SCHEMA_VERSION, "default_max_cost_usd": 1.0,
                "purpose_max_cost_usd": {"draft": 0.75}, "revision": 1,
                "prior_hash": None, "updated_at": "2026-09-14T00:00:00+00:00",
                "actor_ref": "human:owner"}
        body.update(changes)
        return {**body, "content_hash": content_hash(body)}

    def test_dynamic_shared_policy_overrides_only_cost(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "policy.json"
            path.write_text(json.dumps(self.policy()))
            config = {"shared_call_budget_policy_path": str(path),
                      "purpose_call_budgets": {"draft": {
                          "max_output_tokens": 77, "max_cost_usd": 9}}}
            defaults = {"max_input_tokens": 100, "max_output_tokens": 20,
                        "max_cost_usd": 8, "timeout_seconds": 30}
            first = resolve_call_budget(config, "draft", defaults=defaults)
            self.assertEqual(first, {"max_input_tokens": 100,
                "max_output_tokens": 77, "max_cost_usd": 0.75,
                "timeout_seconds": 30})
            path.write_text(json.dumps(self.policy(default_max_cost_usd=0.8,
                                                   purpose_max_cost_usd={})))
            second = resolve_call_budget(config, "draft", defaults=defaults)
            self.assertEqual(second["max_cost_usd"], 0.8)
            self.assertEqual(second["max_output_tokens"], 77)

    def test_hash_tamper_and_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); path = root / "policy.json"
            bad = self.policy(); bad["default_max_cost_usd"] = 5
            path.write_text(json.dumps(bad))
            with self.assertRaises(SharedCallBudgetPolicyError):
                load_shared_call_budget_policy(path)
            real = root / "real.json"; real.write_text(json.dumps(self.policy()))
            link = root / "link.json"; link.symlink_to(real)
            with self.assertRaises(SharedCallBudgetPolicyError):
                load_shared_call_budget_policy(link)

    def test_integer_cost_keeps_valid_hash_across_repeated_validation(self):
        from dalton_core.shared_call_budget_policy import effective_shared_max_cost
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "policy.json"
            original = self.policy(default_max_cost_usd=1, purpose_max_cost_usd={"draft": 2})
            path.write_text(json.dumps(original))
            loaded = load_shared_call_budget_policy(path)
            self.assertEqual(loaded, original)
            self.assertEqual(effective_shared_max_cost(loaded, "draft"), 2.0)
            self.assertEqual(effective_shared_max_cost(loaded, "plan"), 1.0)

    def test_bounded_planner_reads_the_same_shared_policy(self):
        from dataclasses import asdict
        from dalton_core.bounded_planner_driver import BoundedPlannerDriverConfig
        from tests.test_bounded_planner_driver import StalledLoopTests
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); path = root / "policy.json"
            path.write_text(json.dumps(self.policy(purpose_max_cost_usd={"plan": 0.7})))
            raw = json.loads(json.dumps(asdict(StalledLoopTests()._config(root)), default=str))
            raw["shared_call_budget_policy_path"] = str(path)
            configured = BoundedPlannerDriverConfig.from_mapping(raw)
            self.assertEqual(configured.planner_call_budget["max_cost_usd"], 0.7)

    def test_each_extraction_window_hot_refreshes_shared_cost_only(self):
        from dalton_core.document_extraction import build_work as prose_work
        from dalton_core.document_numeric_extraction import build_work as numeric_work
        from dalton_core.metric_discovery_extraction import build_work as discovery_work
        from tests.test_document_numeric_extraction import CONTEXT as NUMERIC_CONTEXT, SLOTS
        from tests.test_metric_discovery_extraction import CONTEXT as DISCOVERY_CONTEXT

        prose_context = {**DISCOVERY_CONTEXT, "offset": 0, "end": 400}
        numeric_context = {**DISCOVERY_CONTEXT, **NUMERIC_CONTEXT}
        builders = (
            ("document_extraction", lambda config: prose_work(prose_context, model_config=config)),
            ("document_numeric_extraction",
             lambda config: numeric_work(numeric_context, SLOTS, model_config=config)),
            ("metric_discovery_extraction",
             lambda config: discovery_work(DISCOVERY_CONTEXT, model_config=config)),
        )
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "policy.json"
            config = {"shared_call_budget_policy_path": str(path)}

            def install(cost, revision):
                purposes = {purpose: cost for purpose, _ in builders}
                path.write_text(json.dumps(self.policy(
                    default_max_cost_usd=cost, purpose_max_cost_usd=purposes,
                    revision=revision, updated_at=f"2026-09-14T00:00:0{revision}+00:00",
                )))

            install(1.0, 1)
            first = [build(config) for _, build in builders]
            install(2.0, 2)
            raised = [build(config) for _, build in builders]
            install(0.5, 3)
            reduced = [build(config) for _, build in builders]

            for before, higher, lower in zip(first, raised, reduced):
                self.assertEqual(before.budget["max_cost_usd"], 1.0)
                self.assertEqual(higher.budget["max_cost_usd"], 2.0)
                self.assertEqual(lower.budget["max_cost_usd"], 0.5)
                for work in (before, higher, lower):
                    self.assertEqual(work.metadata["call_budget"]["max_cost_usd"],
                                     work.budget["max_cost_usd"])
                self.assertEqual(
                    {key: before.budget[key] for key in before.budget if key != "max_cost_usd"},
                    {key: higher.budget[key] for key in higher.budget if key != "max_cost_usd"},
                )
                self.assertNotEqual(before.id, higher.id)
                self.assertNotEqual(higher.id, lower.id)
                # Work already constructed for a prior reservation remains frozen.
                self.assertEqual(before.budget["max_cost_usd"], 1.0)


if __name__ == "__main__": unittest.main()
