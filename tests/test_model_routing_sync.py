"""One model configuration for every environment on the host.

Three things are pinned down here, and they are three because each is a way the
owner's instruction ("a save in any environment is a save in all of them")
could be true on paper and false on the machine.

*The fan-out reaches every other environment, as the owner, and no further.*
The environment that saved is not called again; the others are called once
each, with the same parameters and the same actor.

*A stopped environment costs the save nothing.*  A writer that refuses is named
in the result, in the owner's own language, and the local publication stands.

*It is idempotent.*  An environment already carrying that chain is not called
at all.

The alignment planner is here too, because it answers the same question from
the other end: given the legacy environment's selections, what does each
workspace still have to publish.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from dalton_core.model_fallback_chain import TIERS, tier_chain
from dalton_core.model_router import ModelRouter
from dalton_core.model_routing_sync import (
    LEGACY_ENVIRONMENT_ID,
    Environment,
    environment_for_state_dir,
    _selection_landed,
    fan_out_selection,
    host_environments,
    purpose_holders,
    selection_params,
    sync_note,
    tier_holders,
)
from dalton_core.model_selection import publish_tier_selection
from dalton_core.openclaw_catalog_reconcile import sync_openclaw_model_catalog
from dalton_core.research_planner_setup import ensure_planner_policy
from tests.test_model_selection import NOW, _allowing_config


def _environment(root: Path, name: str, environment_id: str) -> Environment:
    state = root / name / "state" / "dalton-core"
    state.mkdir(parents=True, exist_ok=True)
    return Environment(
        environment_id=environment_id, slug=name, name=name,
        state_dir=state, writer_socket=state / "run" / "writer.sock",
        token_config=state / "writer-tokens.json",
        router_db=state / "model-router.sqlite",
        budget_db=state / "thesis-impact-budget.sqlite",
        core_db=state / "core.sqlite",
    )


def _seed_router(environment: Environment, *, chain: list[str] | None = None) -> str:
    """One router with the fixture catalog, one policy, one tier chain."""

    with ModelRouter(environment.router_db) as router:
        sync_openclaw_model_catalog(router, _allowing_config(), checked_at=NOW,
                                    availability_ttl=timedelta(days=3650))
        ref = ensure_planner_policy(
            router, tier="brain", now=NOW,
            policy_id="model-routing-policy:sync-fixture",
        )["policy_version_ref"]
        if chain is not None:
            ref = publish_tier_selection(
                router, policy_version_ref=ref, tier="brain",
                mode="explicit", chain=chain, now=NOW,
            )["policy_version_ref"]
    return ref


class FanOutTests(unittest.TestCase):
    """The save the owner made here, made everywhere else."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.here = _environment(self.root, "here", LEGACY_ENVIRONMENT_ID)
        self.there = _environment(self.root, "there", "ws-one")
        self.elsewhere = _environment(self.root, "elsewhere", "ws-two")
        self.environments = [self.here, self.there, self.elsewhere]
        self.calls: list[tuple[str, str, dict]] = []

    def _writer(self, environment, *, actor_ref, params):
        self.calls.append((environment.environment_id, actor_ref, dict(params)))
        return {"status": "published"}

    def test_every_other_environment_is_called_once_and_the_origin_is_not(self) -> None:
        params = selection_params(tier="brain", mode="explicit",
                                  chain=["profile:claude-fable-5-1"])
        result = fan_out_selection(
            manager_config_path=self.root / "manager.json",
            origin_state_dir=self.here.state_dir,
            actor_ref="human:owner", params=params,
            environments=self.environments, writer_call=self._writer,
        )
        self.assertEqual([call[0] for call in self.calls], ["ws-one", "ws-two"])
        # The same parameters and the same actor, or the environments would be
        # "synchronised" onto different content.
        self.assertTrue(all(call[1] == "human:owner" for call in self.calls))
        self.assertTrue(all(call[2] == dict(params) for call in self.calls))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["environment_count"], 2)
        self.assertEqual(len(result["synced"]), 2)
        self.assertEqual(result["failed"], [])
        self.assertIn("已同步到 2 个环境", result["note"])

    def test_one_refusal_does_not_undo_the_save_and_is_named_in_chinese(self) -> None:
        def writer(environment, *, actor_ref, params):
            if environment.environment_id == "ws-two":
                raise RuntimeError("连接被拒绝")
            return self._writer(environment, actor_ref=actor_ref, params=params)

        result = fan_out_selection(
            manager_config_path=self.root / "manager.json",
            origin_state_dir=self.here.state_dir,
            actor_ref="human:owner",
            params=selection_params(tier="brain", mode="tier"),
            environments=self.environments, writer_call=writer,
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual([item["environment_id"] for item in result["synced"]], ["ws-one"])
        self.assertEqual([item["environment_id"] for item in result["failed"]], ["ws-two"])
        self.assertIn("已同步到 1 个环境", result["note"])
        self.assertIn("1 个环境未同步", result["note"])
        self.assertIn("连接被拒绝", result["note"])

    def test_an_environment_already_on_the_chain_is_not_called(self) -> None:
        chain = ["profile:claude-fable-5-1", "profile:deepseek-v4-flash"]
        _seed_router(self.there, chain=chain)
        _seed_router(self.elsewhere)
        result = fan_out_selection(
            manager_config_path=self.root / "manager.json",
            origin_state_dir=self.here.state_dir,
            actor_ref="human:owner",
            params=selection_params(tier="brain", mode="explicit", chain=chain),
            environments=self.environments, writer_call=self._writer,
        )
        self.assertEqual([call[0] for call in self.calls], ["ws-two"])
        self.assertEqual([item["environment_id"] for item in result["skipped"]], ["ws-one"])
        self.assertIn("1 个环境本来就是这个配置", result["note"])

    def test_a_writer_whose_answer_was_lost_is_not_reported_as_a_failure(self) -> None:
        chain = ["profile:claude-fable-5-1", "profile:deepseek-v4-flash"]
        # The publication landed; only the answer did not come back.
        _seed_router(self.there, chain=chain)

        def writer(environment, *, actor_ref, params):
            raise TimeoutError("writer 没有回答")

        result = fan_out_selection(
            manager_config_path=self.root / "manager.json",
            origin_state_dir=self.here.state_dir,
            actor_ref="human:owner",
            params=selection_params(purpose="draft", mode="explicit", chain=chain),
            environments=[self.here, self.there], writer_call=writer,
        )
        # ws-one has the chain as a *tier*, not as a draft override, so the
        # purpose selection genuinely has not landed and is a failure.
        self.assertEqual([item["environment_id"] for item in result["failed"]], ["ws-one"])

    def test_a_lone_environment_says_so_rather_than_nothing(self) -> None:
        result = fan_out_selection(
            manager_config_path=self.root / "manager.json",
            origin_state_dir=self.here.state_dir,
            actor_ref="human:owner",
            params=selection_params(tier="brain", mode="tier"),
            environments=[self.here], writer_call=self._writer,
        )
        self.assertEqual(self.calls, [])
        self.assertEqual(result["note"], "这台机器上只有这一个研究环境。")

    def test_selection_params_refuses_a_save_that_names_both_or_neither(self) -> None:
        with self.assertRaises(Exception):
            selection_params(mode="tier")
        with self.assertRaises(Exception):
            selection_params(tier="brain", purpose="draft", mode="tier")

    def test_the_note_never_claims_more_than_it_did(self) -> None:
        note = sync_note([], [], [{"name": "另一个环境", "reason": "writer 没在跑"}])
        self.assertIn("1 个环境未同步：另一个环境（writer 没在跑）", note)


class HostEnumerationTests(unittest.TestCase):
    """Which environments this machine has, read from files and nothing else."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.host = self.root / "host"
        (self.host / "workspaces" / "ws-alpha").mkdir(parents=True)
        legacy_state = self.root / "legacy" / "state" / "dalton-core"
        legacy_state.mkdir(parents=True)
        self.legacy_service = self.root / "legacy" / "config" / "service.json"
        self.legacy_service.parent.mkdir(parents=True)
        self.legacy_service.write_text(json.dumps({
            "control": {"config": {
                "writer_socket": str(legacy_state / "run" / "writer.sock"),
                "token_config": str(legacy_state / "writer-tokens.json"),
                "cockpit": {"state_dir": str(legacy_state)},
            }},
        }), encoding="utf-8")
        self.legacy_state = legacy_state
        workspace_state = self.host / "workspaces" / "ws-alpha" / "state" / "dalton-core"
        workspace_state.mkdir(parents=True)
        (self.host / "workspaces" / "ws-alpha" / "workspace.json").write_text(
            json.dumps({"slug": "ws-alpha", "workspace_id": "id-alpha",
                        "state_dir": str(workspace_state),
                        "writer_socket": str(workspace_state / "run" / "writer.sock")}),
            encoding="utf-8")
        (self.host / "workspaces" / "ws-alpha" / "display.json").write_text(
            json.dumps({"display_name": "美国 Hyperscaler 研究"}), encoding="utf-8")
        self.manager = self.host / "manager.json"
        self.manager.write_text(json.dumps({
            "host_root": str(self.host),
            "legacy_workspace": {"name": "现有研究环境", "workspace_id": "legacy"},
        }), encoding="utf-8")

    def test_the_legacy_environment_comes_from_its_own_service_json(self) -> None:
        found = host_environments(self.manager, legacy_service_path=self.legacy_service)
        self.assertEqual([item.environment_id for item in found],
                         [LEGACY_ENVIRONMENT_ID, "id-alpha"])
        legacy = found[0]
        self.assertEqual(legacy.state_dir, self.legacy_state)
        self.assertEqual(legacy.token_config, self.legacy_state / "writer-tokens.json")
        self.assertEqual(legacy.router_db, self.legacy_state / "model-router.sqlite")
        self.assertEqual(legacy.budget_db,
                         self.legacy_state / "thesis-impact-budget.sqlite")
        self.assertEqual(legacy.name, "现有研究环境")

    def test_a_workspace_is_named_the_way_the_owner_named_it(self) -> None:
        found = host_environments(self.manager, legacy_service_path=self.legacy_service)
        self.assertEqual(found[1].name, "美国 Hyperscaler 研究")

    def test_an_unreadable_manifest_is_skipped_not_fatal(self) -> None:
        broken = self.host / "workspaces" / "ws-broken"
        broken.mkdir()
        (broken / "workspace.json").write_text("{not json", encoding="utf-8")
        found = host_environments(self.manager, legacy_service_path=self.legacy_service)
        self.assertEqual([item.environment_id for item in found],
                         [LEGACY_ENVIRONMENT_ID, "id-alpha"])

    def test_the_calling_environment_is_matched_through_a_symlink(self) -> None:
        found = host_environments(self.manager, legacy_service_path=self.legacy_service)
        link = self.root / "legacy-link"
        link.symlink_to(self.legacy_state)
        self.assertEqual(
            environment_for_state_dir(found, link).environment_id,
            LEGACY_ENVIRONMENT_ID)


class AlignmentPlannerTests(unittest.TestCase):
    """What each workspace still has to publish to match the legacy environment."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = _environment(self.root, "legacy", LEGACY_ENVIRONMENT_ID)
        self.target = _environment(self.root, "workspace", "ws-one")
        self.source_chain = ["profile:claude-fable-5-1", "profile:deepseek-v4-flash"]
        _seed_router(self.source, chain=self.source_chain)

    def _plan(self):
        import align_model_routing

        selections = align_model_routing.source_selections(self.source)
        return align_model_routing, selections

    def test_a_workspace_on_a_different_chain_is_planned_with_before_and_after(self) -> None:
        _seed_router(self.target, chain=["profile:deepseek-v4-flash"])
        align, selections = self._plan()
        plan = align.plan_environment(selections, self.target)
        self.assertEqual(plan["status"], "differs")
        self.assertEqual(plan["publish"],
                         [{"kind": "tier", "tier": "brain", "chain": self.source_chain}])
        difference = plan["differences"][0]
        self.assertEqual(difference["before"], ["profile:deepseek-v4-flash"])
        self.assertEqual(difference["after"], self.source_chain)
        self.assertEqual(difference["policy_id"], "model-routing-policy:sync-fixture")

    def test_a_workspace_already_on_the_chain_is_aligned_and_publishes_nothing(self) -> None:
        _seed_router(self.target, chain=self.source_chain)
        align, selections = self._plan()
        plan = align.plan_environment(selections, self.target)
        self.assertEqual(plan["status"], "aligned")
        self.assertEqual(plan["publish"], [])

    def test_an_environment_with_no_router_is_reported_not_crashed(self) -> None:
        align, selections = self._plan()
        plan = align.plan_environment(selections, self.target)
        self.assertEqual(plan["status"], "unreadable")
        self.assertIn("模型路由数据库", plan["reason"])

    def test_the_source_is_read_for_every_tier_it_declares(self) -> None:
        align, selections = self._plan()
        self.assertEqual(selections["tiers"]["brain"], self.source_chain)
        self.assertEqual(selections["conflicts"], [])
        # Untouched tiers keep the fixture's own chains and are propagated too.
        for tier in TIERS:
            if tier in selections["tiers"] and tier != "brain":
                self.assertEqual(selections["tiers"][tier], list(tier_chain(tier)))

    def test_the_rendered_plan_is_the_owners_language(self) -> None:
        _seed_router(self.target, chain=["profile:deepseek-v4-flash"])
        align, selections = self._plan()
        rendered = align.render({
            "source": {"environment_id": LEGACY_ENVIRONMENT_ID, "name": "现有研究环境",
                       "state_dir": str(self.source.state_dir),
                       "tiers": selections["tiers"], "purposes": selections["purposes"]},
            "conflicts": [],
            "environments": [align.plan_environment(selections, self.target)],
            "environments_differing": 1,
        })
        self.assertIn("现在：", rendered)
        self.assertIn("将改为：", rendered)
        self.assertIn("共 1 个环境与源环境不同。", rendered)


def _add_lineage(environment: Environment, policy_id: str, *,
                 brain_chain: list[str] | None = None) -> str:
    """A second lineage in an already-seeded router.

    ``brain_chain=None`` makes a *bare* lineage -- one profile pinned, no
    ``fallback_chains`` at all -- the shape the service-pin lineages
    (``dalton-openclaw``, ``-assessment``, ...) have in a workspace.
    """

    with ModelRouter(environment.router_db) as router:
        if brain_chain is None:
            return ensure_planner_policy(
                router, profile_ids=["profile:deepseek-v4-flash"], now=NOW,
                policy_id=policy_id)["policy_version_ref"]
        ref = ensure_planner_policy(router, tier="brain", now=NOW,
                                    policy_id=policy_id)["policy_version_ref"]
        return publish_tier_selection(
            router, policy_version_ref=ref, tier="brain", mode="explicit",
            chain=brain_chain, now=NOW)["policy_version_ref"]


def _record_decision(environment: Environment, policy_version_ref: str,
                     created_at: str) -> None:
    """One route decision made under ``policy_version_ref`` (test-only write)."""

    import sqlite3
    import uuid

    connection = sqlite3.connect(environment.router_db)
    # The router's append-only triggers ask the router process whether this
    # write is authorised; a test fixture answers yes for itself.
    connection.create_function("dalton_model_router_authorized", -1, lambda *_: 1)
    try:
        token = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO model_route_decisions (decision_id, decision_kind, outcome, "
            "work_order_id, work_order_hash, attempt_number, capability, "
            "policy_version_ref, policy_hash, candidate_snapshot_hash, request_hash, "
            "decision_hash, decision_json, created_at) "
            "VALUES (?, 'initial', 'rejected', 'wo', 'h', 1, 'reasoning', ?, 'h', 'h', "
            "'h', ?, '{}', ?)",
            (f"decision:{token}", policy_version_ref, token, created_at))
        connection.commit()
    finally:
        connection.close()


class LineageParticipationTests(unittest.TestCase):
    """2026-10-02: a lineage that declares no chain for a tier is not behind on it.

    The source side always ignored such lineages; the target side counted them,
    so after every successful apply the residual still read "还有 2 个环境与源环境
    不同" -- for service-pin lineages no writer call can ever give that tier.
    """

    LANE = {"brain": ["a", "b"], "cheap": ["c"], "verifier": ["v1", "v2"]}

    def setUp(self) -> None:
        import align_model_routing

        self.align = align_model_routing
        self.target = _environment(Path(tempfile.mkdtemp()), "workspace", "ws-one")
        self.source = {"tiers": {"cheap": ["c"], "verifier": ["v1", "v2"]},
                       "purposes": {"research_language_check": ["x", "y"]}}

    def _plan(self, tier_chains, overrides):
        from unittest import mock

        held = {"tier_chains": tier_chains, "purpose_overrides": overrides}
        with mock.patch.object(self.align, "target_selections", return_value=held):
            return self.align.plan_environment(self.source, self.target)

    def test_holders_ignore_lineages_that_do_not_declare_the_tier(self) -> None:
        chains = {"lane": dict(self.LANE), "openclaw": {},
                  "verifier-pin": {"verifier": ["v1", "v2"]}}
        self.assertEqual(tier_holders(chains, "cheap"), {"lane"})
        self.assertEqual(tier_holders(chains, "verifier"), {"lane", "verifier-pin"})
        # Nobody declares it: the environment is missing it, everyone counts.
        self.assertEqual(tier_holders(chains, "deliverable"),
                         {"lane", "openclaw", "verifier-pin"})

    def test_a_stage_pin_counts_lineages_that_hold_it_or_declare_its_tier(self) -> None:
        chains = {"lane": dict(self.LANE), "pinless-lane": dict(self.LANE),
                  "openclaw": {}, "verifier-pin": {"verifier": ["v1"]}}
        overrides = {"lane": {"research_language_check": {"mode": "explicit",
                                                          "chain": ["x", "y"]}},
                     "pinless-lane": {}, "openclaw": {}, "verifier-pin": {}}
        # research_language_check is a cheap-tier stage: the lane that lost its
        # pin to a cheap-tier save still counts; the service pins do not.
        self.assertEqual(purpose_holders(chains, overrides, "research_language_check"),
                         {"lane", "pinless-lane"})

    def test_bare_service_pin_lineages_do_not_make_an_aligned_environment_differ(self) -> None:
        lane = {"brain": ["a", "b"], "cheap": ["c"], "verifier": ["v1", "v2"]}
        pin = {"research_language_check": {"mode": "explicit", "chain": ["x", "y"]}}
        plan = self._plan(
            {"model-routing-policy:dalton-openclaw-extraction": lane,
             "model-routing-policy:dalton-openclaw-claim-index": dict(lane),
             "model-routing-policy:dalton-openclaw": {},
             "model-routing-policy:dalton-openclaw-assessment": {"brain": ["a", "b"]},
             "model-routing-policy:dalton-openclaw-verifier": {"verifier": ["v1", "v2"]}},
            {"model-routing-policy:dalton-openclaw-extraction": dict(pin),
             "model-routing-policy:dalton-openclaw-claim-index": dict(pin),
             "model-routing-policy:dalton-openclaw": {},
             "model-routing-policy:dalton-openclaw-assessment": {},
             "model-routing-policy:dalton-openclaw-verifier": {}})
        self.assertEqual(plan["status"], "aligned")
        self.assertEqual(plan["differences"], [])

    def test_a_lane_that_is_really_behind_is_still_reported(self) -> None:
        plan = self._plan(
            {"lane": {"cheap": ["old"], "verifier": ["v1", "v2"]},
             "pinless-lane": {"cheap": ["c"], "verifier": ["v1", "v2"]},
             "openclaw": {}},
            {"lane": {"research_language_check": {"mode": "explicit", "chain": ["x", "y"]}},
             "pinless-lane": {}, "openclaw": {}})
        self.assertEqual(plan["status"], "differs")
        self.assertEqual(
            sorted((item["kind"], item["subject"], item["policy_id"])
                   for item in plan["differences"]),
            [("purpose", "research_language_check", "pinless-lane"),
             ("tier", "cheap", "lane")])

    def test_an_environment_missing_a_tier_everywhere_is_behind(self) -> None:
        plan = self._plan({"lane": {"cheap": ["c"]}, "openclaw": {}},
                          {"lane": {"research_language_check": {
                              "mode": "explicit", "chain": ["x", "y"]}}, "openclaw": {}})
        self.assertEqual(plan["status"], "differs")
        self.assertEqual(sorted(item["policy_id"] for item in plan["differences"]),
                         ["lane", "openclaw"])
        self.assertEqual(plan["publish"],
                         [{"kind": "tier", "tier": "verifier", "chain": ["v1", "v2"]}])

    def test_a_real_router_with_a_bare_lineage_reads_aligned(self) -> None:
        root = Path(tempfile.mkdtemp())
        source = _environment(root, "legacy", LEGACY_ENVIRONMENT_ID)
        target = _environment(root, "workspace", "ws-one")
        chain = ["profile:claude-fable-5-1", "profile:deepseek-v4-flash"]
        _seed_router(source, chain=chain)
        _add_lineage(source, "model-routing-policy:dalton-openclaw-smoke")
        _seed_router(target, chain=chain)
        _add_lineage(target, "model-routing-policy:dalton-openclaw")
        selections = self.align.source_selections(source)
        self.assertEqual(selections["conflicts"], [])
        plan = self.align.plan_environment(selections, target)
        self.assertEqual(plan["status"], "aligned", plan["differences"])
        # The fan-out's own "already there" check agrees, so the quiet
        # environment is not woken for a selection it already has.
        self.assertTrue(_selection_landed(
            target, selection_params(tier="brain", mode="explicit", chain=chain)))


class IdleSourceLineageTests(unittest.TestCase):
    """A source conflict caused only by lineages that never route."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = _environment(self.root, "legacy", LEGACY_ENVIRONMENT_ID)
        self.lane_chain = ["profile:claude-fable-5-1", "profile:deepseek-v4-flash"]
        self.old_chain = ["profile:deepseek-v4-flash"]
        self.lane_ref = _seed_router(self.source, chain=self.lane_chain)
        self.old_ref = _add_lineage(self.source, "model-routing-policy:dalton-openclaw",
                                    brain_chain=self.old_chain)
        self.now = NOW + timedelta(days=30)

    def _selections(self, **kwargs):
        import align_model_routing

        return align_model_routing.source_selections(self.source, now=self.now, **kwargs)

    def test_without_the_idle_rule_it_is_the_old_conflict_with_details(self) -> None:
        selections = self._selections(idle_days=0)
        self.assertNotIn("brain", selections["tiers"])
        self.assertTrue(any("brain" in item for item in selections["conflicts"]))
        detail = selections["conflict_details"][0]
        self.assertEqual(detail["subject"], "brain")
        self.assertEqual(
            sorted((tuple(item["chain"]), tuple(item["policy_ids"]))
                   for item in detail["values"]),
            [(tuple(self.lane_chain), ("model-routing-policy:sync-fixture",)),
             (tuple(self.old_chain), ("model-routing-policy:dalton-openclaw",))])
        self.assertIsNone(detail["values"][0]["routing"])

    def test_an_idle_lineage_does_not_block_the_routing_lineages_chain(self) -> None:
        recent = (self.now - timedelta(days=1)).isoformat(timespec="microseconds")
        stale = (self.now - timedelta(days=20)).isoformat(timespec="microseconds")
        _record_decision(self.source, self.lane_ref, recent)
        _record_decision(self.source, self.old_ref, stale)
        selections = self._selections(idle_days=14)
        self.assertEqual(selections["tiers"]["brain"], self.lane_chain)
        self.assertEqual(selections["conflicts"], [])
        self.assertEqual(selections["idle_ignored"], [{
            "kind": "tier", "subject": "brain", "chain": self.lane_chain,
            "ignored": [{"policy_id": "model-routing-policy:dalton-openclaw",
                         "chain": self.old_chain}]}])
        self.assertEqual(selections["activity"]["model-routing-policy:sync-fixture"]
                         ["decisions"], 1)

    def test_two_routing_lineages_that_disagree_stay_the_owners_decision(self) -> None:
        recent = (self.now - timedelta(days=1)).isoformat(timespec="microseconds")
        _record_decision(self.source, self.lane_ref, recent)
        _record_decision(self.source, self.old_ref, recent)
        selections = self._selections(idle_days=14)
        self.assertNotIn("brain", selections["tiers"])
        self.assertEqual(selections["idle_ignored"], [])
        routing = [item["routing"] for item in selections["conflict_details"][0]["values"]]
        self.assertTrue(all(len(item) == 1 for item in routing))

    def test_nobody_routing_resolves_nothing(self) -> None:
        selections = self._selections(idle_days=14)
        self.assertNotIn("brain", selections["tiers"])
        self.assertTrue(selections["conflicts"])
        self.assertEqual(selections["activity"], {})
        rendered = self._render(selections)
        self.assertIn("近 14 天都没有路由过", rendered)

    def test_the_report_says_which_lineages_were_set_aside(self) -> None:
        _record_decision(self.source, self.lane_ref,
                         (self.now - timedelta(days=1)).isoformat(timespec="microseconds"))
        rendered = self._render(self._selections(idle_days=14))
        self.assertIn("ℹ 类别「brain」按近 14 天在路由的策略取值", rendered)
        self.assertIn("dalton-openclaw", rendered)

    def _render(self, selections):
        import align_model_routing

        return align_model_routing.render({
            "source": {"environment_id": LEGACY_ENVIRONMENT_ID, "name": "现有研究环境",
                       "state_dir": str(self.source.state_dir),
                       "tiers": selections["tiers"], "purposes": selections["purposes"]},
            "conflicts": selections["conflicts"],
            "conflict_details": selections["conflict_details"],
            "idle_ignored": selections["idle_ignored"],
            "idle_days": 14,
            "environments": [],
            "environments_differing": 0,
        })


class CockpitSaveTests(unittest.TestCase):
    """The page the owner presses 保存 on has to say what happened elsewhere."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.host = self.root / "host"
        (self.host / "workspaces" / "ws-other").mkdir(parents=True)
        self.here = self.root / "here" / "state" / "dalton-core"
        self.here.mkdir(parents=True)
        other = self.host / "workspaces" / "ws-other" / "state" / "dalton-core"
        other.mkdir(parents=True)
        (self.host / "workspaces" / "ws-other" / "workspace.json").write_text(
            json.dumps({"slug": "ws-other", "workspace_id": "ws-other",
                        "state_dir": str(other)}), encoding="utf-8")
        self.manager = self.host / "manager.json"
        self.manager.write_text(json.dumps({"host_root": str(self.host)}),
                                encoding="utf-8")

    def _plane(self):
        from dalton_core.cockpit_plane import CockpitConfig, CockpitPlane

        (self.here / "run").mkdir(exist_ok=True)
        (self.here / "run" / "heartbeat.json").write_text("{}", encoding="utf-8")
        (self.here / "core.sqlite").write_bytes(b"")
        config = CockpitConfig.from_mapping({
            "core_db": str(self.here / "core.sqlite"),
            "state_dir": str(self.here),
            "heartbeat_path": str(self.here / "run" / "heartbeat.json"),
            "scheduler_db": str(self.here / "scheduler.sqlite"),
            "journal_path": str(self.here / "journal.sqlite"),
            "workspace_manager_config_path": str(self.manager),
        })
        plane = CockpitPlane(
            config, writer_socket=self.here / "w.sock",
            token_config=self.here / "t.json",
            governance_call=lambda *a, **k: {"status": "published",
                                             "reload_note": "不用重启。"},
        )
        self.addCleanup(plane.close)
        return plane

    def test_a_save_reports_the_environment_it_could_not_reach(self) -> None:
        plane = self._plane()
        # The other environment has no writer listening, which is the ordinary
        # case for a workspace the owner has not opened today.
        result = plane.select_model("owner@example.com", {
            "tier": "brain", "mode": "explicit",
            "chain": ["profile:claude-fable-5-1"]})
        self.assertEqual(result["sync"]["status"], "partial")
        self.assertIn("1 个环境未同步", result["reload_note"])
        # And the local save is still a save.
        self.assertEqual(result["status"], "published")


if __name__ == "__main__":
    unittest.main()
