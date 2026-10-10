"""A new research environment inherits the mature environment's configuration.

Section 2 of the 2026-09-30 review: a workspace created on this machine came up
without the crowd and prior-research lanes, on the per-call budgets of the day
its template was exported, with a different day-budget policy in service.json
than in its lanes, and with nothing on the create page saying the template was
stale.  Each test here pins one of those down.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core.store import content_hash

OWNER = "human:owner"


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def make_host_sources(source_state: Path) -> None:
    """The host inputs the legacy install put inside the legacy state directory."""

    for tool in ("xueqiu", "xreach"):
        path = source_state / "host-tools" / tool
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(path, 0o755)
        _write(source_state / "credential-grants" / f"{tool}.json", {"grant": tool})
    (source_state / "feeds" / "prior-research" / "0000000001").mkdir(parents=True)


class HostLaneInputsTests(unittest.TestCase):
    """c2: crowd and prior-research are wired at creation from the legacy state."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "legacy" / "state" / "dalton-core"
        self.source.mkdir(parents=True)
        make_host_sources(self.source)
        self.state = self.root / "ws" / "state" / "dalton-core"
        self.state.mkdir(parents=True)
        environ = patch.dict(os.environ, {
            "DALTON_OPENCLAW_WORKSPACE": str(self.root / "no-openclaw")}, clear=False)
        environ.start(); self.addCleanup(environ.stop)
        os.environ.pop("DALTON_PRIOR_RESEARCH_DIR", None)

    def test_the_source_state_supplies_crowd_and_prior_research_is_our_own(self) -> None:
        from dalton_core.workspace_runtime_setup import _host_lane_inputs

        workspace = SimpleNamespace(state_dir=self.state)
        with_source = _host_lane_inputs(workspace, OWNER, None, True, self.source)
        for name in ("host-tools/xueqiu", "host-tools/xreach",
                     "credential-grants/xueqiu.json", "credential-grants/xreach.json"):
            self.assertIn(name, with_source["host_sources"])
            self.assertTrue((self.state / name).exists(), name)
        self.assertIn("source:xueqiu", with_source["source_refs"])
        # Prior research (owner instruction 2026-09-30): this environment's
        # own empty directory, never a link into the legacy state.
        prior = self.state / "feeds" / "prior-research"
        self.assertTrue(prior.is_dir())
        self.assertFalse(prior.is_symlink())
        self.assertEqual(list(prior.iterdir()), [])
        self.assertNotIn("feeds/prior-research", with_source["host_sources"])
        self.assertNotIn("feeds/prior-research", with_source["borrowed_from_source_state"])
        self.assertIn("source:prior-research", with_source["source_refs"])
        governance = {path.name for path in (self.state / "connector-governance").iterdir()}
        self.assertTrue(any(name.startswith("prior-research") for name in governance))


class CallBudgetAlignmentTests(unittest.TestCase):
    """a2: the per-call budgets follow the mature environment."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "legacy"; self.source.mkdir()
        self.state = self.root / "ws"; self.state.mkdir()

    def _config(self, state: Path, **extra) -> dict:
        return {"routing_policy_ref": "model-routing-policy-version:x:1",
                "credential_slot_refs": ["credential-slot:openclaw:openai"],
                "model_router_db": str(state / "model-router.sqlite"),
                "broker_socket": "/tmp/none.sock", "broker_auth_key": "/tmp/none.key",
                "broker_client_id": "client:dalton-core", "expected_agent_id": "chem",
                "budget_db": str(state / "thesis-impact-budget.sqlite"),
                "budget_policy_ref": "budget:1", **extra}

    def test_the_draft_output_limit_and_a_removed_override_follow_the_source(self) -> None:
        from dalton_core.workspace_host_scheme import (
            align_call_budgets, audit_host_scheme, call_budget_differences,
        )

        name = "mission-document-draft-model-config.json"
        _write(self.source / name, self._config(self.source, purpose_call_budgets={
            "mission_directed_document_draft": {"max_cost_usd": 1.0,
                                                "max_output_tokens": 16000}}))
        _write(self.state / name, self._config(self.state, purpose_call_budgets={
            "mission_directed_document_draft": {"max_cost_usd": 1.0}}))
        _write(self.source / "claim-index-model-config.json", self._config(self.source))
        _write(self.state / "claim-index-model-config.json", self._config(
            self.state, call_budget={"max_output_tokens": 999}))
        self.assertEqual(len(call_budget_differences(self.state, self.source)), 2)
        rows = {row["key"]: row for row in audit_host_scheme(
            self.state, source_state_dir=self.source)}
        self.assertEqual(rows["call_budgets"]["status"], "differs")

        result = align_call_budgets(self.state, source_state_dir=self.source)
        self.assertEqual(result["status"], "aligned")
        held = json.loads((self.state / name).read_text())
        self.assertEqual(held["purpose_call_budgets"]["mission_directed_document_draft"]
                         ["max_output_tokens"], 16000)
        # Routing and the machine-local paths are untouched.
        self.assertEqual(held["model_router_db"], str(self.state / "model-router.sqlite"))
        self.assertNotIn("call_budget", json.loads(
            (self.state / "claim-index-model-config.json").read_text()))
        self.assertEqual(align_call_budgets(self.state, source_state_dir=self.source)
                         ["status"], "unchanged")


class BudgetPolicyResolutionTests(unittest.TestCase):
    """b3: the day-budget pin is the head of the lineage the ledger holds."""

    def test_the_lineage_head_is_the_version_nobody_succeeds(self) -> None:
        from dalton_core.workspace_model_setup import latest_budget_policy_id

        lineage = [
            {"policy_version_id": "p:1", "prior_version_id": None, "created_at": "2026-08-01"},
            {"policy_version_id": "p:3", "prior_version_id": "p:2", "created_at": "2026-09-14"},
            {"policy_version_id": "p:2", "prior_version_id": "p:1", "created_at": "2026-09-09"},
            {"policy_version_id": "p:500", "prior_version_id": "p:3", "created_at": "2026-09-15"},
        ]
        self.assertEqual(latest_budget_policy_id(lineage), "p:500")
        self.assertIsNone(latest_budget_policy_id([]))
        self.assertIsNone(latest_budget_policy_id(None))

    def test_the_installed_ledger_answers_the_same(self) -> None:
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
        from dalton_core.workspace_model_setup import installed_budget_policy_id

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "thesis-impact-budget.sqlite"
            with ThesisImpactBudgetStore(path) as store:
                store.register_policy(policy_version_id="p:1", day_cap_micros=25_000_000)
                store.register_policy(policy_version_id="p:3", day_cap_micros=180_000_000,
                                      prior_version_id="p:1")
                store.register_policy(policy_version_id="p:500", day_cap_micros=500_000_000,
                                      prior_version_id="p:3")
            self.assertEqual(installed_budget_policy_id(path), "p:500")
            self.assertIsNone(installed_budget_policy_id(Path(directory) / "missing.sqlite"))


class ExistingWorkspaceBudgetRepairTests(unittest.TestCase):
    """The ws-e399 drift: $180 here, $500 on the host, 4096 vs 16000 output."""

    def setUp(self) -> None:
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore

        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "legacy" / "state" / "dalton-core"
        self.state = self.root / "ws" / "state" / "dalton-core"
        for state, ids in ((self.source, ("p:1", "p:3", "p:300", "p:500")),
                           (self.state, ("p:1", "p:3"))):
            state.mkdir(parents=True)
            caps = {"p:1": 25, "p:3": 180, "p:300": 300, "p:500": 500}
            with ThesisImpactBudgetStore(state / "thesis-impact-budget.sqlite") as budget:
                prior = None
                for policy in ids:
                    budget.register_policy(policy_version_id=policy,
                                           day_cap_micros=caps[policy] * 1_000_000,
                                           prior_version_id=prior)
                    prior = policy
            head = ids[-1]
            _write(state / "claim-index-model-config.json", {
                "routing_policy_ref": "model-routing-policy-version:x:1",
                "credential_slot_refs": ["credential-slot:openclaw:openai"],
                "model_router_db": str(state / "model-router.sqlite"),
                "broker_socket": "/tmp/none.sock", "broker_auth_key": "/tmp/none.key",
                "broker_client_id": "client:dalton-core", "expected_agent_id": "chem",
                "budget_db": str(state / "thesis-impact-budget.sqlite"),
                "budget_policy_ref": head})
            _write(state.parents[1] / "config" / "service.json", {
                "thesis_impact": {"enabled": False, "config": {
                    "budget_policy_version_id": head}}})

    def test_the_lineage_is_extended_and_every_pin_moves_to_the_host_head(self) -> None:
        from dalton_core.workspace_host_scheme import align_budget_policy, budget_policy_plan

        plan = budget_policy_plan(self.state, self.source)
        self.assertEqual(plan["status"], "extend")
        self.assertEqual([row["policy_version_id"] for row in plan["register"]],
                         ["p:300", "p:500"])
        result = align_budget_policy(self.state, source_state_dir=self.source)
        self.assertEqual(result["status"], "aligned")
        config = json.loads((self.state / "claim-index-model-config.json").read_text())
        self.assertEqual(config["budget_policy_ref"], "p:500")
        service = json.loads((self.state.parents[1] / "config" / "service.json").read_text())
        self.assertEqual(service["thesis_impact"]["config"]["budget_policy_version_id"], "p:500")
        self.assertEqual(budget_policy_plan(self.state, self.source)["status"], "ok")

    def test_an_environment_with_its_own_budget_save_is_reported_not_rewritten(self) -> None:
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
        from dalton_core.workspace_host_scheme import align_budget_policy, budget_policy_plan

        with ThesisImpactBudgetStore(self.state / "thesis-impact-budget.sqlite") as budget:
            budget.register_policy(policy_version_id="p:own", day_cap_micros=400_000_000,
                                   prior_version_id="p:3")
        self.assertEqual(budget_policy_plan(self.state, self.source)["status"], "diverged")
        self.assertEqual(align_budget_policy(self.state, source_state_dir=self.source)
                         ["updated"], [])

    def test_the_repair_planner_lists_the_three_host_scheme_actions(self) -> None:
        from dalton_core.workspace_lane_parity import apply_parity_actions, plan_parity_actions

        _write(self.source / "claim-support-verification.json", {"backfill_batches_per_run": 0})
        source_config = json.loads((self.source / "claim-index-model-config.json").read_text())
        source_config["call_budget"] = {"max_output_tokens": 16000}
        _write(self.source / "claim-index-model-config.json", source_config)
        actions = plan_parity_actions(self.state, actor_ref=OWNER, host_sources={},
                                      source_state_dir=self.source)
        kinds = {action.kind for action in actions}
        self.assertTrue({"call_budgets", "budget_policy", "claim_support_settings"} <= kinds)
        chosen = [action for action in actions if action.kind in
                  {"call_budgets", "budget_policy", "claim_support_settings"}]
        performed = apply_parity_actions(chosen, actor_ref=OWNER)
        self.assertEqual({row["result"] for row in performed}, {"aligned", "seeded"})
        self.assertEqual(json.loads((self.state / "claim-support-verification.json").read_text()),
                         {"backfill_batches_per_run": 0})
        again = {action.kind for action in plan_parity_actions(
            self.state, actor_ref=OWNER, host_sources={}, source_state_dir=self.source)}
        self.assertFalse({"call_budgets", "budget_policy", "claim_support_settings"} & again)


class ManagerConfigTests(unittest.TestCase):
    """Item 5: legacy_workspace may name its real paths, never the stale copy."""

    def test_state_dir_and_config_path_are_accepted_and_legacy_control_is_refused(self) -> None:
        from dalton_core.workspace import WorkspaceError
        from dalton_core.workspace_manager import _validate_legacy_workspace

        _validate_legacy_workspace({"name": "现有研究环境", "workspace_id": "legacy",
                                    "status": "running", "url": "https://x/",
                                    "state_dir": "/Volumes/x/legacy-state/dalton-core",
                                    "config_path": "/Users/x/Library/Application Support/"
                                                   "Dalton/config/service.json"})
        with self.assertRaises(WorkspaceError):
            _validate_legacy_workspace({"state_dir": "relative/path"})
        with self.assertRaises(WorkspaceError):
            _validate_legacy_workspace({"config_path": "/Users/x/.dalton/legacy-control/service.json"})
        with self.assertRaises(WorkspaceError):
            _validate_legacy_workspace({"unexpected": 1})

    def test_nothing_in_the_package_reads_the_legacy_control_copy(self) -> None:
        import dalton_core

        package = Path(dalton_core.__file__).parent
        readers = [path.name for path in package.rglob("*.py")
                   if "legacy-control/" in path.read_text(encoding="utf-8")
                   and path.name != "workspace_manager.py"]
        self.assertEqual(readers, [])


class TemplateFreshnessTests(unittest.TestCase):
    """Item 4: the create page knows when the pinned template is behind."""

    def setUp(self) -> None:
        from dalton_core.workspace_template_freshness import clear_cache

        clear_cache()
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "legacy" / "state" / "dalton-core"
        self.state.mkdir(parents=True)

    def _config(self, **extra) -> dict:
        return {"routing_policy_ref": "model-routing-policy-version:x:9",
                "credential_slot_refs": ["credential-slot:openclaw:openai"],
                "model_router_db": str(self.state / "model-router.sqlite"),
                "broker_socket": "/tmp/none.sock", "broker_auth_key": "/tmp/none.key",
                "broker_client_id": "client:dalton-core", "expected_agent_id": "chem",
                "budget_db": str(self.state / "thesis-impact-budget.sqlite"),
                "budget_policy_ref": "budget:500", **extra}

    def _template(self, configs: dict) -> Path:
        path = self.root / "model-template.json"
        _write(path, {"configs": configs, "budget_policies": [
            {"policy_version_id": "budget:1", "prior_version_id": None, "created_at": "1"},
            {"policy_version_id": "budget:500", "prior_version_id": "budget:1",
             "created_at": "2"}]})
        return path

    def test_a_missing_config_and_a_changed_retry_are_stale_realigned_fields_are_not(self) -> None:
        from dalton_core.workspace_template_freshness import model_template_freshness

        _write(self.state / "claim-index-model-config.json", self._config())
        template = self._template({"claim-index-model-config.json": {
            **self._config(), "routing_policy_ref": "model-routing-policy-version:x:1",
            "credential_slot_refs": [], "model_router_db": "/elsewhere"}})
        self.assertEqual(model_template_freshness(template, self.state)["status"], "fresh")

        _write(self.state / "quality-verifier-model-config.json", self._config())
        _write(self.state / "claim-index-model-config.json", self._config(
            provider_retry={"max_same_profile_retries": 2}))
        result = model_template_freshness(template, self.state)
        self.assertEqual(result["status"], "stale")
        self.assertNotEqual(result["template_hash"], result["source_hash"])
        self.assertTrue(any("quality-verifier" in item for item in result["differences"]))
        self.assertTrue(any("provider_retry" in item for item in result["differences"]))

    def test_the_manager_listing_carries_the_warning(self) -> None:
        from dalton_core.workspace_template_freshness import template_freshness

        _write(self.state / "claim-index-model-config.json", self._config())
        _write(self.state / "quality-verifier-model-config.json", self._config())
        template = self._template({"claim-index-model-config.json": self._config()})
        config = {"legacy_workspace": {"name": "legacy", "workspace_id": "legacy",
                                       "state_dir": str(self.state)},
                  "runtime_templates": {"model": {"path": str(template), "sha256": "a" * 64},
                                        "service": {"path": str(template), "sha256": "b" * 64}}}
        result = template_freshness(config)
        self.assertEqual(result["model"]["status"], "stale")
        self.assertEqual(result["status"], "stale")
        self.assertTrue(result["warnings"])
        self.assertIn("reexport-runtime-templates", result["warnings"][-1])


class HealthParityBeforeFirstMissionTests(unittest.TestCase):
    """d1: the nine rows signed at first publish are info on a blank workspace."""

    def test_blank_workspace_governance_rows_are_info_and_only_the_mission_is_pending(self) -> None:
        from dalton_core.store import DaltonStore
        from dalton_core.workspace_health_parity import Environment, check_governance

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            state = Path(directory) / "state" / "dalton-core"
            state.mkdir(parents=True)
            DaltonStore(str(state / "core.sqlite")).close()
            env = Environment("blank", state)
            try:
                rows = check_governance(env)
            finally:
                env.close()
        baseline = [row for row in rows if row["check"].startswith(
            ("policy.research_candidate_auto_commit", "policy.research_plan_auto_start",
             "policy.research_budget"))]
        self.assertEqual(len(baseline), 9)
        self.assertEqual({row["status"] for row in baseline}, {"info"})
        self.assertTrue(all("fix" not in row for row in baseline))
        pending = [row["check"] for row in rows if row["status"] == "pending"]
        self.assertEqual(pending, ["mission"])


if __name__ == "__main__":
    unittest.main()
