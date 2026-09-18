"""A research environment created today is already inside this machine's scheme.

Three things used to be true of a new environment until somebody ran two
scripts by hand, and each one is a way for a workspace to look healthy while
being wrong:

*It was outside the host's daily cap*, so two environments could spend twice
the day's money with both pages reporting that no cap had been passed.

*It ran the packaged model chains* rather than the ones the owner has been
choosing, so a new Cockpit started life on models that had been taken out of
service in the environment next to it.

*It listed fewer broker credential slots than the host has*, which turns a
perfectly good chain into ``credential_slot_unavailable`` on every link -- a
lane reporting an exhausted chain at a cost of zero.

What is pinned down here is that creation does all three, that it does none of
them destructively, and that the audit page says so afterwards.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.model_router import ModelRouter
from dalton_core.model_routing_sync import agreed_selections
from dalton_core.model_selection import publish_selection
from dalton_core.openclaw_catalog_reconcile import sync_openclaw_model_catalog
from dalton_core.research_planner_setup import ensure_planner_policy
from dalton_core.shared_daily_budget import (
    BINDING_FILENAME,
    load_shared_daily_budget_binding,
    policy_wire,
    write_owner_only,
)
from dalton_core.store import content_hash
from dalton_core.workspace_creation import create_blank_workspace
from dalton_core.workspace_host_scheme import (
    adopt_host_scheme,
    align_credential_slots,
    align_model_routing,
    audit_host_scheme,
    source_credential_slots,
)
from dalton_core.workspace_runtime_setup import install
from tests.test_model_selection import NOW, _allowing_config, _controls

POLICY_ID = "model-routing-policy:host-scheme-fixture"
HOST_SLOTS = ["credential-slot:openclaw:openai", "credential-slot:openclaw:claude-cli"]
WORKSPACE_SLOTS = ["credential-slot:openclaw:openai"]
#: The chain the owner chose in the legacy environment. Its first link is
#: deliberately a model a workspace created from an older template does not
#: have a profile for.
CHOSEN_BRAIN_CHAIN = ["profile:claude-fable-5-1", "profile:deepseek-v4-flash"]


def _catalog(*, without: str | None = None) -> dict:
    """What the gateway offers, with verification controls where a tier needs them."""

    config = _allowing_config()
    entry = config["plugins"]["entries"]["dalton-openclaw-model-broker"]
    profiles = entry["config"]["profiles"]
    for profile in profiles:
        profile["providerControls"] = _controls(profile["model"])
    if without is not None:
        entry["config"]["profiles"] = [item for item in profiles if item["id"] != without]
    return config


class HostSchemeCase(unittest.TestCase):
    """One source environment, one freshly templated workspace beside it."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self._environment("legacy", slots=HOST_SLOTS)
        self.workspace = self._environment(
            "ws-one", slots=WORKSPACE_SLOTS, without=CHOSEN_BRAIN_CHAIN[0])
        self._choose(self.source)

    def _environment(self, name: str, *, slots: list[str],
                     without: str | None = None) -> Path:
        state = self.root / name / "state" / "dalton-core"
        state.mkdir(parents=True)
        router_db = state / "model-router.sqlite"
        with ModelRouter(router_db) as router:
            sync_openclaw_model_catalog(
                router, _catalog(without=without), checked_at=NOW,
                availability_ttl=timedelta(days=3650))
            reference = ensure_planner_policy(
                router, tier="brain", now=NOW, policy_id=POLICY_ID,
            )["policy_version_ref"]
        (state / "research-planner-model-config.json").write_text(json.dumps({
            "routing_policy_ref": reference,
            "credential_slot_refs": list(slots),
            "model_router_db": str(router_db),
        }), encoding="utf-8")
        service = state.parents[1] / "config" / "service.json"
        service.parent.mkdir(parents=True, exist_ok=True)
        service.write_text(json.dumps({
            "thesis_impact": {"config": {"credential_slot_refs": list(slots)}},
        }), encoding="utf-8")
        return state

    def _choose(self, state: Path) -> None:
        """The owner's selections in the source environment: one tier, one stage."""

        with ModelRouter(state / "model-router.sqlite") as router:
            latest = self._head(router)
            reference = self._publish_tier(router, latest)
            publish_selection(
                router, policy_version_ref=reference, purpose="claim_index",
                mode="explicit", chain=["profile:zai-glm-5-3-flash"],
                actor_ref="human:owner", now=NOW)

    @staticmethod
    def _head(router: ModelRouter) -> str:
        row = router.connection.execute(
            "SELECT policy_version_ref FROM model_routing_policy_versions "
            "WHERE policy_id=? ORDER BY version DESC LIMIT 1", (POLICY_ID,),
        ).fetchone()
        return row["policy_version_ref"]

    @staticmethod
    def _publish_tier(router: ModelRouter, reference: str) -> str:
        from dalton_core.model_selection import publish_tier_selection

        return publish_tier_selection(
            router, policy_version_ref=reference, tier="brain", mode="explicit",
            chain=CHOSEN_BRAIN_CHAIN, actor_ref="human:owner", now=NOW,
        )["policy_version_ref"]


class RoutingAlignmentTests(HostSchemeCase):
    """A new environment starts on the selections the owner has already made."""

    def test_a_new_workspace_ends_on_the_source_chains_and_gets_the_profile(self) -> None:
        held = agreed_selections(self.workspace / "model-router.sqlite")
        self.assertNotEqual(held["tiers"]["brain"], CHOSEN_BRAIN_CHAIN)

        result = align_model_routing(
            self.workspace, source_state_dir=self.source,
            actor_ref="human:owner", now=NOW)

        self.assertEqual(result["status"], "aligned", result.get("failed"))
        # The model the owner chose was not in this workspace's catalog, so the
        # selection could not have been saved without bringing its profile
        # over; the copy is named in the record rather than implied.
        self.assertEqual(result["profiles_copied"], [CHOSEN_BRAIN_CHAIN[0]])
        after = agreed_selections(self.workspace / "model-router.sqlite")
        self.assertEqual(after["tiers"], agreed_selections(
            self.source / "model-router.sqlite")["tiers"])
        self.assertEqual(after["purposes"]["claim_index"],
                         ["profile:zai-glm-5-3-flash"])
        # Published, not inserted: the lane configuration follows the new
        # policy version, which is what makes the next call use it.
        config = json.loads((self.workspace / "research-planner-model-config.json"
                             ).read_text(encoding="utf-8"))
        self.assertNotEqual(config["routing_policy_ref"], "")
        with ModelRouter(self.workspace / "model-router.sqlite") as router:
            policy = router.get_policy(config["routing_policy_ref"])
        self.assertEqual(policy["fallback_chains"]["tiers"]["brain"],
                         CHOSEN_BRAIN_CHAIN)

    def test_running_it_twice_publishes_nothing_the_second_time(self) -> None:
        align_model_routing(self.workspace, source_state_dir=self.source,
                            actor_ref="human:owner", now=NOW)
        with ModelRouter(self.workspace / "model-router.sqlite") as router:
            before = router.connection.execute(
                "SELECT count(*) FROM model_routing_policy_versions").fetchone()[0]
        again = align_model_routing(self.workspace, source_state_dir=self.source,
                                    actor_ref="human:owner", now=NOW)
        with ModelRouter(self.workspace / "model-router.sqlite") as router:
            after = router.connection.execute(
                "SELECT count(*) FROM model_routing_policy_versions").fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(again["status"], "aligned")

    def test_an_unreadable_source_leaves_the_packaged_defaults_and_says_why(self) -> None:
        result = align_model_routing(
            self.workspace, source_state_dir=self.root / "not-an-environment",
            actor_ref="human:owner", now=NOW)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("模型路由", result["note"])
        self.assertIn("align_model_routing.py", result["note"])
        held = agreed_selections(self.workspace / "model-router.sqlite")
        self.assertNotEqual(held["tiers"]["brain"], CHOSEN_BRAIN_CHAIN)


class CredentialSlotTests(HostSchemeCase):
    """A chain is only as good as the credential list the config names."""

    def test_the_workspace_ends_with_the_hosts_slots_everywhere_it_lists_them(self) -> None:
        self.assertEqual(source_credential_slots(self.source), HOST_SLOTS)

        result = align_credential_slots(self.workspace, source_state_dir=self.source)

        self.assertEqual(result["status"], "aligned")
        config = json.loads((self.workspace / "research-planner-model-config.json"
                             ).read_text(encoding="utf-8"))
        self.assertEqual(config["credential_slot_refs"], HOST_SLOTS)
        service = json.loads((self.workspace.parents[1] / "config" / "service.json"
                              ).read_text(encoding="utf-8"))
        self.assertEqual(service["thesis_impact"]["config"]["credential_slot_refs"],
                         HOST_SLOTS)
        self.assertIn("service.json#thesis_impact.config.credential_slot_refs",
                      result["updated"])

    def test_a_slot_this_environment_has_and_the_host_does_not_is_kept(self) -> None:
        path = self.workspace / "research-planner-model-config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["credential_slot_refs"] = ["credential-slot:openclaw:local-only"]
        path.write_text(json.dumps(config), encoding="utf-8")

        align_credential_slots(self.workspace, source_state_dir=self.source)

        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["credential_slot_refs"],
            ["credential-slot:openclaw:local-only", *HOST_SLOTS])

    def test_a_second_run_changes_nothing(self) -> None:
        align_credential_slots(self.workspace, source_state_dir=self.source)
        again = align_credential_slots(self.workspace, source_state_dir=self.source)
        self.assertEqual(again["status"], "unchanged")
        self.assertEqual(again["updated"], [])


class AuditTests(HostSchemeCase):
    """The page an owner opens to ask why this environment is not the other one."""

    def test_before_and_after_adoption_the_three_rows_say_so(self) -> None:
        rows = {row["key"]: row for row in audit_host_scheme(
            self.workspace, source_state_dir=self.source)}
        self.assertEqual(sorted(rows), ["credential_slots", "model_routing_alignment",
                                        "shared_daily_budget"])
        self.assertEqual(rows["shared_daily_budget"]["status"], "missing")
        self.assertEqual(rows["model_routing_alignment"]["status"], "differs")
        self.assertEqual(rows["credential_slots"]["status"], "differs")

        result = adopt_host_scheme(
            self.workspace, manager_config_path=None, actor_ref="human:owner",
            source_state_dir=self.source, now=NOW)
        self.assertEqual(result["model_routing"]["status"], "aligned")
        self.assertEqual(result["credential_slots"]["status"], "aligned")

        rows = {row["key"]: row for row in audit_host_scheme(
            self.workspace, source_state_dir=self.source)}
        self.assertEqual(rows["model_routing_alignment"]["status"], "ok")
        self.assertEqual(rows["credential_slots"]["status"], "ok")

    def test_the_lane_audit_carries_the_rows_and_prints_them(self) -> None:
        from dalton_core.workspace_lane_parity import audit_lanes, render_audit

        report = audit_lanes(self.workspace, host_sources={},
                             source_state_dir=self.source)
        self.assertEqual([row["key"] for row in report["host_scheme"]],
                         ["shared_daily_budget", "model_routing_alignment",
                          "credential_slots"])
        rendered = render_audit(report)
        self.assertIn("共享每日预算", rendered)
        self.assertIn("模型凭证槽位", rendered)

    def test_an_existing_workspace_is_repaired_by_the_parity_plan(self) -> None:
        from dalton_core.workspace_lane_parity import (
            apply_parity_actions, plan_parity_actions,
        )

        actions = [action for action in plan_parity_actions(
            self.workspace, actor_ref="human:owner", host_sources={},
            source_state_dir=self.source) if action.kind == "credential_slots"]
        self.assertEqual(len(actions), 1)
        self.assertIn("credential_slot_unavailable", actions[0].reason)

        performed = apply_parity_actions(actions, actor_ref="human:owner")

        self.assertEqual(performed[0]["result"], "aligned")
        config = json.loads((self.workspace / "research-planner-model-config.json"
                             ).read_text(encoding="utf-8"))
        self.assertEqual(config["credential_slot_refs"], HOST_SLOTS)
        self.assertEqual(
            [action for action in plan_parity_actions(
                self.workspace, actor_ref="human:owner", host_sources={},
                source_state_dir=self.source) if action.kind == "credential_slots"],
            [])


class BlankWorkspaceCase(unittest.TestCase):
    """One freshly created, un-templated workspace and a host policy beside it."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.release = self.root / "release"
        self.release.mkdir()
        self.manager = self.root / "manager.json"
        self.manager.write_text(json.dumps({"host_root": str(self.root)}),
                                encoding="utf-8")
        body = {"schema_version": "dalton-shared-connection-catalog-0.1",
                "models": [], "sources": []}
        self.catalog = self.root / "connections.json"
        self.catalog.write_text(json.dumps({**body, "content_hash": content_hash(body)}),
                                encoding="utf-8")
        receipt = create_blank_workspace(
            self.root / "fleet", "blank", 18892,
            "release:sha256:" + "a" * 64, self.release,
            request_id="request-123", display_name="Blank",
            shared_readonly_paths=(self.catalog, self.release),
        )
        self.manifest = Path(receipt["manifest_path"])
        self.state = Path(receipt["config_path"]).parent.parent / "state/dalton-core"
        self.policy_path = self.root / "shared-daily-budget.json"

    def _policy(self) -> None:
        write_owner_only(self.policy_path, policy_wire(
            max_daily_cost_usd=500.0, max_daily_paid_calls=100000,
            max_alphaengine_calls_24h=130, revision=1, prior_hash=None,
            updated_at=datetime(2026, 9, 18, tzinfo=timezone.utc).isoformat(
                timespec="microseconds"),
            actor_ref="human:owner"))


class SharedDailyBudgetBindingTests(BlankWorkspaceCase):
    """A workspace is inside the host's daily cap from its first call."""

    def test_a_fresh_install_binds_to_the_host_policy(self) -> None:
        self._policy()
        result = install(self.manifest, actor_ref="human:owner@example.com",
                         stage_host_lanes=False, manager_config_path=self.manager,
                         shared_daily_budget_policy_path=self.policy_path)
        self.assertEqual(result["shared_daily_budget"]["status"], "bound")
        binding = load_shared_daily_budget_binding(self.state / BINDING_FILENAME)
        self.assertEqual(binding["policy_path"], str(self.policy_path))
        self.assertEqual(binding["manager_config_path"], str(self.manager.resolve()))
        foundation = json.loads(
            (self.state / "research-foundation.json").read_text(encoding="utf-8"))
        self.assertEqual(foundation["shared_daily_budget"]["policy_revision"], 1)
        # The gate the admission authority consults finds it without being told.
        from dalton_core.shared_daily_budget import resolve_shared_gate

        gate = resolve_shared_gate(self.state / "thesis-impact-budget.sqlite")
        self.assertEqual(gate["max_daily_paid_calls"], 100000)
        self.assertEqual(gate["environment_id"], binding["environment_id"])
        # The audit reads the same thing from the other end.
        rows = {row["key"]: row for row in audit_host_scheme(self.state)}
        self.assertEqual(rows["shared_daily_budget"]["status"], "ok")

    def test_a_host_without_a_policy_is_recorded_as_skipped_and_not_an_error(self) -> None:
        result = install(self.manifest, actor_ref="human:owner@example.com",
                         stage_host_lanes=False, manager_config_path=self.manager,
                         shared_daily_budget_policy_path=self.policy_path)
        self.assertEqual(result["shared_daily_budget"]["status"], "skipped")
        self.assertIn("bind_shared_daily_budget.py",
                      result["shared_daily_budget"]["note"])
        self.assertFalse((self.state / BINDING_FILENAME).exists())
        foundation = json.loads(
            (self.state / "research-foundation.json").read_text(encoding="utf-8"))
        self.assertEqual(foundation["shared_daily_budget"]["status"], "skipped")

    def test_without_a_host_manifest_nothing_is_bound_and_the_reason_is_kept(self) -> None:
        result = install(self.manifest, actor_ref="human:owner@example.com",
                         stage_host_lanes=False)
        self.assertEqual(result["shared_daily_budget"]["status"], "skipped")
        self.assertFalse((self.state / BINDING_FILENAME).exists())


class ProvisionWiringTests(BlankWorkspaceCase):
    """The creation flow is what has to carry all of this, not a later script."""

    def test_provisioning_binds_the_budget_and_records_the_scheme_in_the_receipt(self) -> None:
        from unittest.mock import patch

        from dalton_core.workspace_manager import provision_runtime

        self._policy()
        template = self.root / "template.json"
        template.write_text("{}", encoding="utf-8")
        config = {
            "tailscale_host": "dalton.example.ts.net",
            "tailscale_executable": "/usr/bin/tailscale",
            "shared_daily_budget_policy_path": str(self.policy_path),
            "runtime_templates": {
                "model": {"path": str(template), "sha256": "a" * 64},
                "service": {"path": str(template), "sha256": "b" * 64},
            },
        }
        with patch("dalton_core.workspace_manager._runtime_templates",
                   return_value={"model": template, "service": template}), \
                patch("dalton_core.workspace_model_setup.install_runtime_template",
                      return_value={"status": "stubbed"}), \
                patch("dalton_core.workspace_service_setup.install_service_template",
                      return_value={"status": "stubbed"}), \
                patch("dalton_core.workspace_control_setup.configure_workspace_control",
                      return_value=None), \
                patch("dalton_core.workspace_lane_parity.resolve_host_sources",
                      return_value={}):
            receipt = provision_runtime(
                config, self.manifest, login="owner@example.com",
                manager_config_path=self.manager)

        scheme = receipt["host_scheme"]
        self.assertEqual(scheme["shared_daily_budget"]["status"], "bound")
        self.assertTrue((self.state / BINDING_FILENAME).is_file())
        self.assertEqual(
            json.loads((self.state / "runtime-ready.json").read_text(
                encoding="utf-8"))["host_scheme"]["shared_daily_budget"]["status"],
            "bound")
        # This host declares no other environment, so there is nothing to copy
        # the model configuration from -- said out loud rather than silently
        # leaving the owner to wonder which chains the workspace started on.
        self.assertEqual(scheme["model_routing"]["status"], "skipped")
        self.assertIn("默认模型配置", scheme["model_routing"]["note"])


if __name__ == "__main__":
    unittest.main()
