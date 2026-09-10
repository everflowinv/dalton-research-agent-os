"""P14-M2: the owner picks the model for each stage, and the catalog follows.

Four things are being pinned down here, and they are four because each one is
a way the feature could be true on paper and false in practice.

*The selection is a policy version.*  Publishing one must leave every earlier
version byte-identical, must be in force on the next call rather than the next
restart, and must roll back by being published again.

*A selection cannot be dishonest.*  A model this Core has no profile for, an
unpriced model in front of a priced one, and a verifier pointed at the
producer's own family are each refused with one sentence.

*The catalog follows the gateway by itself.*  New models register, vanished
ones retire rather than disappear, and the whole thing is idempotent.

*A model vanishing is never silent.*  The chain falls to its next link on its
own, the owner is told once per (model, stage), and the one case where falling
through would be worse than stopping -- the last independent verifier going --
refuses instead.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.cockpit_plane import CockpitConfig, CockpitPlane
from dalton_core.model_fallback_chain import (
    FallbackChainError,
    TIERS,
    effective_chain,
    execute_chain,
    purpose_tiers,
    register_purpose_tier,
    routing_overview,
    tier_chain,
    validate_selection,
)
from dalton_core.model_router import ModelRouter, live_links, resolve_chain
from dalton_core.model_selection import (
    NOTIFICATION_CHANNEL,
    ModelSelectionError,
    current_selection,
    model_configs,
    publish_selection,
    record_retirement_notices,
    retirement_fallbacks,
    set_model_selection,
)
from dalton_core.openclaw_allow_patch import (
    AllowPatchError,
    apply_allow_patch,
    build_allow_patch,
    changed_paths,
    profile_id_for,
)
from dalton_core.openclaw_catalog_reconcile import (
    load_openclaw_config,
    sync_openclaw_model_catalog,
)
from dalton_core.openclaw_model_discovery import discover_models, summarise
from dalton_core.research_planner_setup import credential_slots_for, ensure_planner_policy
from tests.test_model_fallback_chain import FakeBroker, _work
from tests.test_openclaw_catalog_reconcile import _config

# In the past, deliberately: a profile registered "now" has an availability
# check the router reads as being in the future, and refuses.
NOW = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)
OWNER = "human:tailscale-owner"
# A verifier-tier purpose to select for. Nothing in the shipped tier map is a
# verifier -- the verifier chain is reached by capability today -- so the
# selection rules that only apply to verification need one to talk about.
VERIFY_PURPOSE = "p14m2_verify_probe"
BRAIN_PURPOSE = "plan"
CHEAP_PURPOSE = "claim_index"


def _allowing_config(**overrides) -> dict:
    """The fixture config, with the broker's allow-list filled in.

    ``_config()`` predates the allow-list -- it describes providers and broker
    profiles only -- and every question this module asks is about the gate
    between them, so the fixture has to have one.
    """

    config = _config()
    entry = config["plugins"]["entries"]["dalton-openclaw-model-broker"]
    entry["llm"] = {"allowedModels": sorted(
        {profile["model"] for profile in entry["config"]["profiles"]}
    )}
    for key, value in overrides.items():
        config[key] = value
    return config


def _drop_broker_profile(config: dict, profile_id: str) -> dict:
    entry = config["plugins"]["entries"]["dalton-openclaw-model-broker"]
    entry["config"]["profiles"] = [
        profile for profile in entry["config"]["profiles"]
        if profile["id"] != profile_id
    ]
    return config


def _unprice(config: dict, model_ref: str) -> dict:
    """Take the rate card off one provider model, as a gateway sometimes does."""

    provider, _, model = model_ref.partition("/")
    for item in config["models"]["providers"][provider]["models"]:
        if item["id"] == model:
            item.pop("cost", None)
    return config


class RouterCase(unittest.TestCase):
    """One router with the fixture catalog and one policy per tier."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.router = ModelRouter(self.root / "model-router.sqlite")
        self.addCleanup(self.router.close)
        sync_openclaw_model_catalog(self.router, self.catalog_config(), checked_at=NOW)
        register_purpose_tier(VERIFY_PURPOSE, "verifier")
        self.policies = {
            tier: ensure_planner_policy(
                self.router, tier=tier, now=NOW,
                policy_id=f"model-routing-policy:p14m2-{tier}",
            )["policy_version_ref"]
            for tier in TIERS
        }

    def catalog_config(self) -> dict:
        """What the gateway offers. Overridden where that matters."""

        return _allowing_config()

    def policy(self, tier: str = "brain") -> dict:
        return self.router.get_policy(self.policies[tier])


class SelectionResolutionTests(RouterCase):
    def test_following_the_tier_resolves_to_the_tiers_own_chain(self) -> None:
        checked = validate_selection(
            self.router, purpose=BRAIN_PURPOSE, mode="tier", chain=[]
        )
        self.assertEqual(checked["chain"], list(tier_chain("brain")))
        resolved = effective_chain(self.policy(), BRAIN_PURPOSE)
        self.assertEqual(resolved["mode"], "tier")
        self.assertEqual(resolved["chain"], list(tier_chain("brain")))

    def test_naming_a_chain_beats_the_tier_and_is_what_routing_reads(self) -> None:
        published = publish_selection(
            self.router, policy_version_ref=self.policies["brain"],
            purpose=BRAIN_PURPOSE, mode="explicit",
            chain=["profile:claude-fable-5-1", "profile:gpt-6-astra"], now=NOW,
        )
        policy = self.router.get_policy(published["policy_version_ref"])
        resolved = effective_chain(policy, BRAIN_PURPOSE)
        self.assertEqual(resolved["mode"], "explicit")
        self.assertEqual(
            resolved["chain"],
            ["profile:claude-fable-5-1", "profile:gpt-6-astra"],
        )
        # And the router filters candidates against the same answer, so the
        # selection cannot mean one thing to the walk and another to the filter.
        decision = self.router.route(
            _work("work:p14m2-explicit"), attempt_number=1, capability="research",
            policy_version_ref=published["policy_version_ref"],
            credential_slot_refs=credential_slots_for(
                self.router, ["profile:claude-fable-5-1", "profile:gpt-6-astra"]
            ),
            required_modalities=["text"], required_context_tokens=2_000,
            estimated_input_tokens=1_000, estimated_output_tokens=500,
            idempotency_key="p14m2-explicit:1", tier="brain", purpose=BRAIN_PURPOSE,
        )["decision"]
        profile = self.router.get_profile(decision["selected_profile_version_ref"])
        self.assertEqual(profile["id"], "profile:claude-fable-5-1")

    def test_a_model_this_core_has_no_profile_for_cannot_be_chosen(self) -> None:
        with self.assertRaisesRegex(FallbackChainError, "no profile on this machine"):
            validate_selection(
                self.router, purpose=BRAIN_PURPOSE, mode="explicit",
                chain=["profile:not-a-model-we-hold"],
            )

    def test_naming_the_same_model_twice_is_not_a_fallback(self) -> None:
        with self.assertRaisesRegex(FallbackChainError, "twice"):
            validate_selection(
                self.router, purpose=BRAIN_PURPOSE, mode="explicit",
                chain=["profile:gpt-6-astra", "profile:gpt-6-astra"],
            )

    def test_following_the_tier_and_naming_models_are_not_both(self) -> None:
        with self.assertRaisesRegex(FallbackChainError, "not naming models"):
            validate_selection(
                self.router, purpose=BRAIN_PURPOSE, mode="tier",
                chain=["profile:gpt-6-astra"],
            )


class UnpricedModelTests(RouterCase):
    unpriced = "profile:gemini-3-5-flash-lite"

    def catalog_config(self) -> dict:
        # The gateway offers gemini-3.5-flash-lite with no rate card. It is the
        # cheap tier's last link already, which is exactly where an unpriced
        # model is allowed to be.
        return _unprice(_allowing_config(), "google/gemini-3.5-flash-lite")

    def test_an_unpriced_model_registers_and_says_so(self) -> None:
        profile = next(
            item for item in self.router.latest_profiles()
            if item["id"] == self.unpriced
        )
        self.assertTrue(profile["unpriced"])
        self.assertEqual(profile["cost"]["input_per_million_usd"], 0.0)

    def test_an_unpriced_model_may_be_last_and_may_not_be_first(self) -> None:
        checked = validate_selection(
            self.router, purpose=CHEAP_PURPOSE, mode="explicit",
            chain=["profile:deepseek-v4-flash", self.unpriced],
        )
        self.assertEqual(checked["chain"][-1], self.unpriced)
        with self.assertRaisesRegex(FallbackChainError, "only be the last resort"):
            validate_selection(
                self.router, purpose=CHEAP_PURPOSE, mode="explicit",
                chain=[self.unpriced, "profile:deepseek-v4-flash"],
            )

    def test_routing_refuses_an_unpriced_model_anywhere_but_the_end(self) -> None:
        published = publish_selection(
            self.router, policy_version_ref=self.policies["cheap"],
            purpose=CHEAP_PURPOSE, mode="explicit",
            chain=["profile:deepseek-v4-flash", self.unpriced], now=NOW,
        )
        decision = self.router.route(
            _work("work:p14m2-unpriced"), attempt_number=1, capability="research",
            policy_version_ref=published["policy_version_ref"],
            credential_slot_refs=credential_slots_for(
                self.router, ["profile:deepseek-v4-flash", self.unpriced]
            ),
            required_modalities=["text"], required_context_tokens=2_000,
            estimated_input_tokens=1_000, estimated_output_tokens=500,
            idempotency_key="p14m2-unpriced:1", tier="cheap", purpose=CHEAP_PURPOSE,
        )["decision"]
        snapshot = {
            self.router.get_profile(item["profile_version_ref"])["id"]: item
            for item in decision["candidate_snapshot"]
        }
        self.assertNotIn(
            "unpriced_model_not_last_link",
            snapshot[self.unpriced]["rejection_reasons"],
        )
        # And with no chain at all it is refused outright: nothing to be last of.
        untiered = self.router.route(
            _work("work:p14m2-unpriced-single"), attempt_number=1,
            capability="research",
            policy_version_ref=self.policies["cheap"],
            credential_slot_refs=credential_slots_for(self.router, [self.unpriced]),
            required_modalities=["text"], required_context_tokens=2_000,
            estimated_input_tokens=1_000, estimated_output_tokens=500,
            idempotency_key="p14m2-unpriced-single:1",
        )["decision"]
        single = {
            self.router.get_profile(item["profile_version_ref"])["id"]: item
            for item in untiered["candidate_snapshot"]
        }
        self.assertIn(
            "unpriced_model_requires_chain",
            single[self.unpriced]["rejection_reasons"],
        )


class VerifierIndependenceTests(RouterCase):
    def test_a_verifier_cannot_be_pointed_at_the_producers_own_family(self) -> None:
        with self.assertRaisesRegex(FallbackChainError, "not an independent check"):
            validate_selection(
                self.router, purpose=VERIFY_PURPOSE, mode="explicit",
                chain=["profile:claude-fable-5-1", "profile:zai-glm-5-3"],
            )

    def test_a_verifier_from_a_different_family_is_accepted(self) -> None:
        checked = validate_selection(
            self.router, purpose=VERIFY_PURPOSE, mode="explicit",
            chain=["profile:zai-glm-5-3", "profile:gemini-3-5-flash-lite"],
        )
        self.assertEqual(checked["tier"], "verifier")

    def test_losing_the_last_independent_link_refuses_rather_than_falls_through(
        self,
    ) -> None:
        # A producer decision on the brain chain, so the verifier's family is
        # read from what actually ran rather than from what a caller says.
        producer = self.router.route(
            _work("work:p14m2-producer"), attempt_number=1, capability="research",
            policy_version_ref=self.policies["brain"],
            credential_slot_refs=credential_slots_for(
                self.router, list(tier_chain("brain"))
            ),
            required_modalities=["text"], required_context_tokens=2_000,
            estimated_input_tokens=1_000, estimated_output_tokens=500,
            idempotency_key="p14m2-producer:1", tier="brain", purpose=BRAIN_PURPOSE,
        )["decision"]
        self.assertEqual(producer["outcome"], "selected")
        producer_family = producer["selected_endpoint"]["family"]
        # Every verifier link that is *not* the producer's family disappears.
        config = _allowing_config()
        held = {item["id"]: item for item in self.router.latest_profiles()}
        for profile_id in tier_chain("verifier"):
            if held[profile_id]["family"] != producer_family:
                _drop_broker_profile(config, profile_id)
        sync_openclaw_model_catalog(self.router, config, checked_at=NOW)
        outcome = execute_chain(
            self.router, _work("work:p14m2-verify", capability="verify"),
            purpose=VERIFY_PURPOSE, tier="verifier", capability="verify",
            attempt_number=1, policy_version_ref=self.policies["verifier"],
            credential_slot_refs=credential_slots_for(
                self.router, list(tier_chain("verifier"))
            ),
            required_modalities=["text"], required_context_tokens=2_000,
            estimated_input_tokens=1_000, estimated_output_tokens=500,
            idempotency_prefix="p14m2-verify", call=FakeBroker({}),
            producer_decision_ref=producer["id"],
        )
        self.assertEqual(outcome["status"], "refused")
        self.assertEqual(outcome["reason"], "verifier_not_independent")
        self.assertIn(producer_family, outcome["message"])
        self.assertIn("different family", outcome["message"])
        self.assertTrue(outcome["retired_links"])


class PolicyVersionTests(RouterCase):
    def test_publishing_appends_and_leaves_the_previous_version_untouched(self) -> None:
        before = self.router.get_policy(self.policies["brain"])
        published = publish_selection(
            self.router, policy_version_ref=self.policies["brain"],
            purpose=BRAIN_PURPOSE, mode="explicit",
            chain=["profile:claude-fable-5-1"], now=NOW,
        )
        self.assertEqual(published["status"], "fresh")
        self.assertEqual(published["prior_version_ref"], self.policies["brain"])
        after = self.router.get_policy(self.policies["brain"])
        self.assertEqual(after["content_hash"], before["content_hash"])
        self.assertNotIn("purpose_overrides", after)
        published_policy = self.router.get_policy(published["policy_version_ref"])
        self.assertEqual(
            published_policy["purpose_overrides"][BRAIN_PURPOSE],
            {"mode": "explicit", "chain": ["profile:claude-fable-5-1"]},
        )

    def test_publishing_the_same_selection_again_appends_nothing(self) -> None:
        first = publish_selection(
            self.router, policy_version_ref=self.policies["brain"],
            purpose=BRAIN_PURPOSE, mode="explicit",
            chain=["profile:claude-fable-5-1"], now=NOW,
        )
        again = publish_selection(
            self.router, policy_version_ref=first["policy_version_ref"],
            purpose=BRAIN_PURPOSE, mode="explicit",
            chain=["profile:claude-fable-5-1"], now=NOW,
        )
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["policy_version_ref"], first["policy_version_ref"])

    def test_rolling_back_is_publishing_the_previous_choice_again(self) -> None:
        chosen = publish_selection(
            self.router, policy_version_ref=self.policies["brain"],
            purpose=BRAIN_PURPOSE, mode="explicit",
            chain=["profile:claude-fable-5-1"], now=NOW,
        )
        rolled = publish_selection(
            self.router, policy_version_ref=chosen["policy_version_ref"],
            purpose=BRAIN_PURPOSE, mode="tier", now=NOW,
        )
        self.assertEqual(rolled["status"], "fresh")
        policy = self.router.get_policy(rolled["policy_version_ref"])
        self.assertEqual(
            policy["purpose_overrides"][BRAIN_PURPOSE], {"mode": "tier"}
        )
        self.assertEqual(
            effective_chain(policy, BRAIN_PURPOSE)["chain"], list(tier_chain("brain"))
        )
        # Three versions, none of them rewritten.
        rows = self.router.connection.execute(
            "SELECT version FROM model_routing_policy_versions "
            "WHERE policy_id=? ORDER BY version",
            ("model-routing-policy:p14m2-brain",),
        ).fetchall()
        self.assertEqual([row["version"] for row in rows], [1, 2, 3])

    def test_a_selection_a_version_cannot_carry_is_refused_by_the_wire(self) -> None:
        policy = dict(self.router.get_policy(self.policies["brain"]))
        policy["purpose_overrides"] = {"plan": {"mode": "explicit"}}
        policy.pop("content_hash")
        policy["version"] = 99
        with self.assertRaises(Exception):
            self.router.register_policy(policy)


class StateDirectoryCase(RouterCase):
    """A state directory with one model configuration pinning a policy."""

    def setUp(self) -> None:
        super().setUp()
        self.config_path = self.root / "research-planner-model-config.json"
        self.model_config = {
            "routing_policy_ref": self.policies["brain"],
            "credential_slot_refs": credential_slots_for(
                self.router, list(tier_chain("brain"))
            ),
            "model_router_db": str(self.root / "model-router.sqlite"),
            "broker_socket": str(self.root / "broker.sock"),
            "broker_auth_key": str(self.root / "broker.key"),
            "broker_client_id": "client:dalton-core",
            "expected_agent_id": "dalton-model-broker",
            "budget_db": str(self.root / "budget.sqlite"),
            "budget_policy_ref": "thesis-impact-day-budget-policy:production:1",
        }
        self.config_path.write_text(
            json.dumps(self.model_config), encoding="utf-8"
        )

    def stored(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))


class SetSelectionTests(StateDirectoryCase):
    def test_every_pinned_configuration_moves_to_the_new_version(self) -> None:
        result = set_model_selection(
            self.root, purpose=BRAIN_PURPOSE, mode="explicit",
            chain=["profile:claude-fable-5-1"], now=NOW,
        )
        self.assertEqual(result["status"], "published")
        self.assertEqual(
            result["model_configs_repointed"], ["research-planner-model-config.json"]
        )
        moved = self.stored()["routing_policy_ref"]
        self.assertNotEqual(moved, self.policies["brain"])
        policy = self.router.get_policy(moved)
        self.assertEqual(
            policy["purpose_overrides"][BRAIN_PURPOSE]["chain"],
            ["profile:claude-fable-5-1"],
        )

    def test_the_next_call_runs_the_selected_model_with_no_restart(self) -> None:
        set_model_selection(
            self.root, purpose=BRAIN_PURPOSE, mode="explicit",
            chain=["profile:claude-fable-5-1"], now=NOW,
        )
        # A lane reads its configuration file, which now names the new version.
        config = self.stored()
        outcome = execute_chain(
            self.router, _work("work:p14m2-after-selection"),
            purpose=BRAIN_PURPOSE, tier="brain", capability="research",
            attempt_number=1, policy_version_ref=config["routing_policy_ref"],
            credential_slot_refs=config["credential_slot_refs"],
            required_modalities=["text"], required_context_tokens=2_000,
            estimated_input_tokens=1_000, estimated_output_tokens=500,
            idempotency_prefix="p14m2-after-selection", call=FakeBroker({}),
        )
        self.assertEqual(outcome["status"], "served")
        self.assertEqual(outcome["profile_id"], "profile:claude-fable-5-1")

    def test_the_change_is_in_the_routing_overview_immediately(self) -> None:
        set_model_selection(
            self.root, purpose=BRAIN_PURPOSE, mode="explicit",
            chain=["profile:claude-fable-5-1"], now=NOW,
        )
        overview = routing_overview(
            self.router, policy_version_ref=self.stored()["routing_policy_ref"]
        )
        row = next(
            item for item in overview["purposes"] if item["purpose"] == BRAIN_PURPOSE
        )
        self.assertEqual(row["mode"], "explicit")
        self.assertEqual(
            [link["profile_id"] for link in row["chain"]],
            ["profile:claude-fable-5-1"],
        )
        selection = current_selection(self.root)
        self.assertTrue(selection["available"])
        chosen = next(
            item for item in selection["purposes"] if item["purpose"] == BRAIN_PURPOSE
        )
        self.assertEqual(chosen["mode"], "explicit")

    def test_a_verifier_pointed_at_the_producer_family_is_refused_with_a_reason(
        self,
    ) -> None:
        with self.assertRaisesRegex(ModelSelectionError, "not an independent check"):
            set_model_selection(
                self.root, purpose=VERIFY_PURPOSE, mode="explicit",
                chain=["profile:claude-fable-5-1"], now=NOW,
            )
        # Nothing moved: a refused selection must not repoint anything.
        self.assertEqual(self.stored()["routing_policy_ref"], self.policies["brain"])

    def test_a_stage_this_core_does_not_have_is_refused(self) -> None:
        with self.assertRaisesRegex(ModelSelectionError, "not a calling stage"):
            set_model_selection(
                self.root, purpose="no_such_stage", mode="tier", now=NOW,
            )

    def test_the_configuration_registry_is_what_is_repointed(self) -> None:
        names = [item["name"] for item in model_configs(self.root)]
        self.assertEqual(names, ["research-planner-model-config.json"])


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.router = ModelRouter(self.root / "model-router.sqlite")
        self.addCleanup(self.router.close)

    def test_a_model_the_gateway_offers_and_the_broker_does_not_pass(self) -> None:
        config = _allowing_config()
        entry = config["plugins"]["entries"]["dalton-openclaw-model-broker"]
        held_back = entry["llm"]["allowedModels"].pop()
        discovery = discover_models(config)
        self.assertIn(held_back, discovery["in_openclaw_not_allowed"])
        self.assertNotIn(held_back, discovery["allowed_model_refs"])

    def test_a_model_the_broker_passes_that_this_core_has_no_profile_for(self) -> None:
        config = _allowing_config()
        discovery = discover_models(config, router=self.router, checked_at=NOW)
        # Nothing registered yet: every servable broker profile is missing here.
        self.assertEqual(
            set(discovery["allowed_not_in_dalton"]), set(discovery["broker_profile_ids"])
        )
        self.assertFalse(discovery["in_sync"])
        sync_openclaw_model_catalog(self.router, config, checked_at=NOW)
        after = discover_models(config, router=self.router, checked_at=NOW)
        self.assertEqual(after["allowed_not_in_dalton"], [])
        self.assertEqual(after["dalton_not_in_openclaw"], [])
        self.assertTrue(after["in_sync"])

    def test_a_profile_this_core_holds_that_the_gateway_has_dropped(self) -> None:
        config = _allowing_config()
        sync_openclaw_model_catalog(self.router, config, checked_at=NOW)
        dropped = copy.deepcopy(config)
        provider = dropped["models"]["providers"]["openai"]
        provider["models"] = [
            item for item in provider["models"] if item["id"] != "gpt-6-astra"
        ]
        discovery = discover_models(dropped, router=self.router, checked_at=NOW)
        self.assertIn("profile:gpt-6-astra", discovery["dalton_not_in_openclaw"])
        # And retiring it -- never deleting it -- takes it out of the set.
        _drop_broker_profile(dropped, "profile:gpt-6-astra")
        dropped["plugins"]["entries"]["dalton-openclaw-model-broker"]["llm"][
            "allowedModels"
        ] = [
            ref for ref in dropped["plugins"]["entries"][
                "dalton-openclaw-model-broker"]["llm"]["allowedModels"]
            if ref != "openai/gpt-6-astra"
        ]
        sync_openclaw_model_catalog(self.router, dropped, checked_at=NOW)
        after = discover_models(dropped, router=self.router, checked_at=NOW)
        self.assertEqual(after["dalton_not_in_openclaw"], [])
        self.assertIn("profile:gpt-6-astra", after["retired_profile_ids"])

    def test_an_unpriced_model_is_named_and_the_catalog_still_reads(self) -> None:
        config = _unprice(_allowing_config(), "google/gemini-3.5-flash-lite")
        discovery = discover_models(config)
        self.assertEqual(
            discovery["unpriced_model_refs"], ["google/gemini-3.5-flash-lite"]
        )

    def test_the_summary_is_counts_and_never_a_list_of_models(self) -> None:
        counts = summarise(discover_models(_allowing_config()))
        self.assertTrue(all(isinstance(value, int) for value in counts.values()))

    def test_no_secret_reaches_the_discovery_report(self) -> None:
        serialized = json.dumps(discover_models(_allowing_config()))
        self.assertNotIn("must-not-leak", serialized)
        self.assertNotIn("also-secret", serialized)


class AllowPatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "openclaw.json"
        config = _allowing_config()
        entry = config["plugins"]["entries"]["dalton-openclaw-model-broker"]
        # One model the gateway offers, the broker neither allows nor profiles.
        self.held_back = "openai/gpt-6-astra"
        entry["llm"]["allowedModels"] = [
            ref for ref in entry["llm"]["allowedModels"] if ref != self.held_back
        ]
        _drop_broker_profile(config, "profile:gpt-6-astra")
        self.original = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
        self.path.write_text(self.original, encoding="utf-8")

    def test_the_patch_names_exactly_the_two_keys_that_move(self) -> None:
        patch = build_allow_patch(load_openclaw_config(self.path), self.held_back)
        self.assertEqual(patch["status"], "planned")
        self.assertEqual(patch["allowed_models_add"], [self.held_back])
        self.assertEqual(patch["profiles_add"][0]["model"], self.held_back)
        self.assertEqual(patch["profile_id"], "profile:gpt-6-astra")

    def test_applying_backs_up_first_and_changes_only_the_broker_subtree(self) -> None:
        result = apply_allow_patch(self.path, self.held_back, now=NOW)
        self.assertEqual(result["status"], "applied")
        backup = Path(result["backup_path"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), self.original)
        self.assertEqual(
            result["changed_paths"],
            ["plugins.entries.dalton-openclaw-model-broker"],
        )
        self.assertIn("openclaw gateway restart", result["reload_instruction"])
        # Round trip: what landed parses, and differs from the backup only
        # inside the subtree the patch names.
        before = json.loads(backup.read_text(encoding="utf-8"))
        after = load_openclaw_config(self.path)
        self.assertEqual(
            changed_paths(before, after),
            ["plugins.entries.dalton-openclaw-model-broker"],
        )
        self.assertIn(
            self.held_back,
            after["plugins"]["entries"]["dalton-openclaw-model-broker"]["llm"][
                "allowedModels"
            ],
        )

    def test_pressing_it_twice_does_not_append_a_second_entry(self) -> None:
        apply_allow_patch(self.path, self.held_back, now=NOW)
        again = apply_allow_patch(
            self.path, self.held_back, now=NOW + timedelta(minutes=1)
        )
        self.assertEqual(again["status"], "already_allowed")
        self.assertIsNone(again["backup_path"])
        allowed = load_openclaw_config(self.path)["plugins"]["entries"][
            "dalton-openclaw-model-broker"]["llm"]["allowedModels"]
        self.assertEqual(allowed.count(self.held_back), 1)

    def test_a_model_the_gateway_does_not_have_is_refused(self) -> None:
        with self.assertRaisesRegex(AllowPatchError, "not in models.providers"):
            apply_allow_patch(self.path, "openai/not-a-model", now=NOW)
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)

    def test_the_profile_id_is_the_one_daltons_own_catalog_already_uses(self) -> None:
        config = load_openclaw_config(self.path)
        self.assertEqual(profile_id_for(config, self.held_back), "profile:gpt-6-astra")


class CatalogLaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.openclaw = self.root / "openclaw.json"
        self.openclaw.write_text(
            json.dumps(_allowing_config(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.router_db = self.root / "model-router.sqlite"
        with ModelRouter(self.router_db):
            pass
        self.switch = self.root / "model-catalog-sync.json"
        self.switch.write_text(json.dumps({
            "openclaw_config_path": str(self.openclaw),
            "model_router_db": str(self.router_db),
        }), encoding="utf-8")
        self.moment = NOW

    def coordinator(self, delivery=None):
        from dalton_core.mission_model_catalog_lane import ModelCatalogSyncCoordinator

        return ModelCatalogSyncCoordinator(
            config_path=self.switch, clock=lambda: self.moment, delivery=delivery
        )

    def test_the_lane_is_registered_where_a_lane_has_to_be(self) -> None:
        from dalton_core.budget_pools import pool_for_operation
        from dalton_core.cockpit_plane import REGISTRY_LANE_LABELS
        from dalton_core.lane_registry import LANE_MODULES, lane_for_operation

        self.assertIn("dalton_core.mission_model_catalog_lane", LANE_MODULES)
        spec = lane_for_operation("dispatch_catalog_sync")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.driver_key, "catalog_sync")
        self.assertEqual(pool_for_operation("dispatch_catalog_sync"), "maintenance")
        self.assertIn("catalog_sync", REGISTRY_LANE_LABELS)

    def test_the_switch_file_is_the_switch(self) -> None:
        from dalton_core.lane_registry import LaunchAgentContext
        from dalton_core.mission_model_catalog_lane import argv_fragment

        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(argv_fragment(LaunchAgentContext(state=Path(empty))), [])
        fragment = argv_fragment(LaunchAgentContext(state=self.root))
        self.assertEqual(fragment[0], "--model-catalog-config")

    def test_one_run_registers_the_catalog_and_the_second_writes_nothing(self) -> None:
        first = self.coordinator().run()
        self.assertEqual(first["status"], "changed")
        self.assertTrue(first["registered"])
        self.assertTrue(first["catalog_in_sync"])
        self.assertEqual(first["notification_channel"], NOTIFICATION_CHANNEL)
        second = self.coordinator().run()
        self.assertEqual(second["status"], "current")
        self.assertEqual(second["registered"], [])

    def test_the_hour_is_the_window(self) -> None:
        lane = self.coordinator()
        self.assertEqual(lane.dispatch_once()["status"], "changed")
        self.assertEqual(lane.dispatch_once()["status"], "idle")
        self.moment = NOW + timedelta(hours=1)
        self.assertEqual(lane.dispatch_once()["status"], "current")

    def test_the_lane_never_writes_the_gateways_configuration(self) -> None:
        before = self.openclaw.read_bytes()
        self.coordinator().run()
        self.assertEqual(self.openclaw.read_bytes(), before)

    def test_an_unreadable_host_file_does_not_fail_the_tick(self) -> None:
        self.openclaw.write_text("{ not json", encoding="utf-8")
        result = self.coordinator().dispatch_once()
        self.assertEqual(result["status"], "failed")
        self.assertIn("cannot load OpenClaw config", result["reason"])


class RetirementNoticeTests(StateDirectoryCase):
    def _drop(self, *profile_ids: str) -> dict:
        config = _allowing_config()
        for profile_id in profile_ids:
            _drop_broker_profile(config, profile_id)
        return sync_openclaw_model_catalog(self.router, config, checked_at=NOW)

    def test_a_retired_first_link_falls_to_the_next_one_by_itself(self) -> None:
        self._drop("profile:gpt-6-astra")
        outcome = execute_chain(
            self.router, _work("work:p14m2-fallthrough"),
            purpose=BRAIN_PURPOSE, tier="brain", capability="research",
            attempt_number=1, policy_version_ref=self.policies["brain"],
            credential_slot_refs=credential_slots_for(
                self.router, list(tier_chain("brain"))
            ),
            required_modalities=["text"], required_context_tokens=2_000,
            estimated_input_tokens=1_000, estimated_output_tokens=500,
            idempotency_prefix="p14m2-fallthrough", call=FakeBroker({}),
        )
        self.assertEqual(outcome["status"], "served")
        self.assertEqual(outcome["profile_id"], "profile:claude-fable-5-1")

    def test_an_explicit_chain_that_is_entirely_retired_falls_back_to_the_tier(
        self,
    ) -> None:
        published = publish_selection(
            self.router, policy_version_ref=self.policies["brain"],
            purpose=BRAIN_PURPOSE, mode="explicit",
            chain=["profile:gpt-6-astra"], now=NOW,
        )
        self._drop("profile:gpt-6-astra")
        policy = self.router.get_policy(published["policy_version_ref"])
        held = {item["id"]: item for item in self.router.latest_profiles()}
        resolved = resolve_chain(
            policy, tier="brain", purpose=BRAIN_PURPOSE, profiles=held
        )
        self.assertEqual(resolved["mode"], "tier_after_retirement")
        self.assertEqual(resolved["superseded_chain"], ["profile:gpt-6-astra"])
        self.assertEqual(live_links(resolved["chain"], held),
                         ["profile:claude-fable-5-1"])
        outcome = execute_chain(
            self.router, _work("work:p14m2-superseded"),
            purpose=BRAIN_PURPOSE, tier="brain", capability="research",
            attempt_number=1,
            policy_version_ref=published["policy_version_ref"],
            credential_slot_refs=credential_slots_for(
                self.router, list(tier_chain("brain"))
            ),
            required_modalities=["text"], required_context_tokens=2_000,
            estimated_input_tokens=1_000, estimated_output_tokens=500,
            idempotency_prefix="p14m2-superseded", call=FakeBroker({}),
        )
        self.assertEqual(outcome["status"], "served")
        self.assertEqual(outcome["profile_id"], "profile:claude-fable-5-1")
        # The selection itself is untouched: it is content of an immutable
        # version, and re-selecting is the owner's move.
        self.assertEqual(
            policy["purpose_overrides"][BRAIN_PURPOSE]["chain"],
            ["profile:gpt-6-astra"],
        )

    def test_the_owner_is_told_once_per_model_and_stage(self) -> None:
        sync = self._drop("profile:gpt-6-astra")
        delivered: list[dict] = []
        first = record_retirement_notices(
            self.router, state_dir=self.root,
            retired_profile_ids=sync["retired_profile_ids_this_run"],
            delivery=delivered.append,
        )
        self.assertEqual(first["notification_channel"], NOTIFICATION_CHANNEL)
        self.assertIn(BRAIN_PURPOSE, first["affected_purposes"])
        self.assertTrue(first["notices_written"])
        self.assertEqual(len(delivered), len(first["notices_written"]))
        message = first["messages"][0]
        self.assertIn("profile:gpt-6-astra", message)
        self.assertIn("已在 OpenClaw 消失", message)
        self.assertIn("profile:claude-fable-5-1", message)
        self.assertIn("cockpit 模型页", message)
        # The lane runs every hour and the model stays gone; it must not
        # become an hourly alarm.
        again = record_retirement_notices(
            self.router, state_dir=self.root,
            retired_profile_ids=sync["retired_profile_ids_this_run"],
            delivery=delivered.append,
        )
        self.assertEqual(again["notices_written"], [])
        self.assertEqual(len(delivered), len(first["notices_written"]))
        self.assertEqual(again["notices_repeated"], first["notices_written"])

    def test_a_stage_left_with_nothing_says_so_rather_than_naming_a_model(
        self,
    ) -> None:
        self._drop(*tier_chain("brain"))
        affected = retirement_fallbacks(
            self.router, policy=self.policy("brain"),
            retired_profile_ids=list(tier_chain("brain")),
        )
        row = next(item for item in affected if item["purpose"] == BRAIN_PURPOSE)
        self.assertIsNone(row["replacement_profile_id"])
        self.assertIn("没有可用的替代模型", row["message"])

    def test_acknowledging_closes_it_and_a_second_press_is_a_no_op(self) -> None:
        sync = self._drop("profile:gpt-6-astra")
        record_retirement_notices(
            self.router, state_dir=self.root,
            retired_profile_ids=sync["retired_profile_ids_this_run"],
        )
        # One retirement touches every stage that was pointed at the model, so
        # there is a notice per stage and acknowledging one closes one.
        notices = self.router.fallback_notices(open_only=True)
        self.assertGreater(len(notices), 1)
        notice = notices[0]
        first = self.router.acknowledge_fallback_notice(
            notice_id=notice["id"], actor_ref=OWNER
        )
        self.assertEqual(first["status"], "acknowledged")
        still_open = self.router.fallback_notices(open_only=True)
        self.assertEqual(len(still_open), len(notices) - 1)
        self.assertNotIn(notice["id"], [item["id"] for item in still_open])
        again = self.router.acknowledge_fallback_notice(
            notice_id=notice["id"], actor_ref=OWNER
        )
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["acknowledged_by"], OWNER)
        self.assertEqual(
            self.router.acknowledge_fallback_notice(
                notice_id="model-fallback-notice:nope", actor_ref=OWNER
            )["status"],
            "unknown",
        )

    def test_a_notice_cannot_be_edited_or_deleted(self) -> None:
        sync = self._drop("profile:gpt-6-astra")
        record_retirement_notices(
            self.router, state_dir=self.root,
            retired_profile_ids=sync["retired_profile_ids_this_run"],
        )
        with self.assertRaises(Exception):
            self.router.connection.execute(
                "UPDATE model_fallback_notices SET purpose='ask'"
            )
        with self.assertRaises(Exception):
            self.router.connection.execute("DELETE FROM model_fallback_notices")


class GovernanceOperationTests(unittest.TestCase):
    def test_the_three_model_operations_are_human_only_and_actor_bound(self) -> None:
        from dalton_core import writer_server

        for operation in ("set_model_selection", "allow_openclaw_model",
                          "acknowledge_model_fallback_notice"):
            with self.subTest(operation=operation):
                self.assertIn(operation, writer_server.OPERATION_FIELDS)
                self.assertIn(operation, writer_server.HUMAN_GOVERNANCE_OPERATIONS)
                self.assertEqual(
                    writer_server.OPERATION_ACTOR_FIELDS[operation], "actor_ref"
                )
                self.assertNotIn(operation, writer_server.MISSION_AUTOMATION_OPERATIONS)
                self.assertNotIn(operation, writer_server.CORE_OPERATIONS)
                self.assertTrue(
                    hasattr(writer_server.WriterServer, f"_op_{operation}")
                )

    def test_letting_a_model_through_takes_a_model_name_and_never_a_path(self) -> None:
        from dalton_core import writer_server

        self.assertEqual(
            writer_server.OPERATION_FIELDS["allow_openclaw_model"],
            frozenset({"model_ref", "actor_ref"}),
        )


class CockpitModelPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.openclaw = self.root / "openclaw.json"
        self.openclaw.write_text(
            json.dumps(_allowing_config(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.router_db = self.root / "model-router.sqlite"
        self.calls: list[tuple[str, dict]] = []

    def plane(self, *, with_model_config: bool) -> CockpitPlane:
        (self.root / "run").mkdir(exist_ok=True)
        heartbeat = self.root / "run" / "heartbeat.json"
        heartbeat.write_text("{}", encoding="utf-8")
        core = self.root / "core.sqlite"
        core.write_bytes(b"")
        raw = {
            "core_db": str(core), "state_dir": str(self.root),
            "heartbeat_path": str(heartbeat),
            "scheduler_db": str(self.root / "scheduler.sqlite"),
            "journal_path": str(self.root / "journal.sqlite"),
            "openclaw_config_path": str(self.openclaw),
        }
        if with_model_config:
            raw["model_config_path"] = str(
                self.root / "research-planner-model-config.json"
            )

        def governance(token_config, socket, *, actor_ref, operation, params):
            self.calls.append((operation, dict(params)))
            return {"status": "published", "actor": actor_ref}

        plane = CockpitPlane(
            CockpitConfig.from_mapping(raw),
            writer_socket=self.root / "w.sock", token_config=self.root / "t.json",
            governance_call=governance, clock=lambda: NOW,
        )
        self.addCleanup(plane.close)
        return plane

    def install(self) -> str:
        with ModelRouter(self.router_db) as router:
            sync_openclaw_model_catalog(router, _allowing_config(), checked_at=NOW)
            policy = ensure_planner_policy(
                router, tier="brain", now=NOW,
                policy_id="model-routing-policy:p14m2-cockpit",
            )["policy_version_ref"]
            slots = credential_slots_for(router, list(tier_chain("brain")))
        (self.root / "research-planner-model-config.json").write_text(
            json.dumps({
                "routing_policy_ref": policy, "credential_slot_refs": slots,
                "model_router_db": str(self.router_db),
                "broker_socket": str(self.root / "b.sock"),
                "broker_auth_key": str(self.root / "b.key"),
                "broker_client_id": "client:dalton-core",
                "expected_agent_id": "dalton-model-broker",
                "budget_db": str(self.root / "budget.sqlite"),
                "budget_policy_ref": "thesis-impact-day-budget-policy:production:1",
            }), encoding="utf-8")
        return policy

    def test_a_core_with_no_model_catalog_says_so_rather_than_failing(self) -> None:
        view = self.plane(with_model_config=False).models()
        self.assertFalse(view["available"])
        self.assertIn("模型路由库", view["reason"])

    def test_the_page_shows_every_stage_its_chain_and_the_three_diff_sets(self) -> None:
        self.install()
        view = self.plane(with_model_config=True).models()
        self.assertTrue(view["available"])
        row = next(
            item for item in view["purposes"] if item["purpose"] == BRAIN_PURPOSE
        )
        self.assertEqual(row["mode_label"], "跟随档位")
        self.assertEqual(
            [link["model"] for link in row["chain"]], list(tier_chain("brain"))
        )
        self.assertTrue(row["label"] and row["tier_label"])
        self.assertTrue(view["choices"])
        catalog = view["catalog"]
        self.assertTrue(catalog["available"])
        for key in ("in_openclaw_not_allowed", "allowed_not_in_dalton",
                    "dalton_not_in_openclaw"):
            self.assertIsInstance(catalog[key], list)
            self.assertTrue(catalog[f"{key}_note"])

    def test_the_page_reads_without_a_gateway_configuration(self) -> None:
        self.install()
        plane = self.plane(with_model_config=True)
        object.__setattr__(plane.config, "openclaw_config_path", None)
        view = plane.models()
        self.assertTrue(view["available"])
        self.assertFalse(view["catalog"]["available"])

    def test_the_three_buttons_leave_through_the_writer_as_the_owner(self) -> None:
        self.install()
        plane = self.plane(with_model_config=True)
        plane.select_model("owner@example.com", {
            "purpose": BRAIN_PURPOSE, "mode": "explicit",
            "chain": ["profile:claude-fable-5-1"],
        })
        plane.allow_model("owner@example.com", {"model_ref": "openai/gpt-6-astra"})
        plane.acknowledge_model_notice(
            "owner@example.com", {"ref": "model-fallback-notice:abc"}
        )
        self.assertEqual(
            [name for name, _ in self.calls],
            ["set_model_selection", "allow_openclaw_model",
             "acknowledge_model_fallback_notice"],
        )
        self.assertEqual(self.calls[1][1], {"model_ref": "openai/gpt-6-astra"})

    def test_an_impossible_selection_never_reaches_the_writer(self) -> None:
        self.install()
        plane = self.plane(with_model_config=True)
        from dalton_core.cockpit_plane import CockpitError

        with self.assertRaises(CockpitError):
            plane.select_model("owner@example.com", {
                "purpose": BRAIN_PURPOSE, "mode": "nonsense", "chain": [],
            })
        with self.assertRaises(CockpitError):
            plane.select_model("owner@example.com", {
                "purpose": BRAIN_PURPOSE, "mode": "explicit", "chain": [],
            })
        self.assertEqual(self.calls, [])

    def test_an_open_notice_is_waiting_on_the_approvals_page(self) -> None:
        self.install()
        with ModelRouter(self.router_db) as router:
            router.record_fallback_notice(
                profile_id="profile:gpt-6-astra", purpose=BRAIN_PURPOSE,
                tier="brain", replacement_profile_id="profile:claude-fable-5-1",
                reason="not_in_broker_catalog",
                message="模型 profile:gpt-6-astra 已在 OpenClaw 消失",
                detail={},
            )
        plane = self.plane(with_model_config=True)
        items = plane._model_fallback_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "model_fallback")
        self.assertEqual(
            [action["decision"] for action in items[0]["actions"]], ["acknowledge"]
        )
        self.assertFalse(items[0]["needs_rationale"])


class PurposeCoverageTests(unittest.TestCase):
    def test_every_stage_the_owner_can_select_has_a_name_in_their_language(
        self,
    ) -> None:
        from dalton_core.model_selection import PURPOSE_LABELS

        unnamed = sorted(set(purpose_tiers()) - set(PURPOSE_LABELS))
        # A probe purpose registered by a test is not the owner's business.
        unnamed = [name for name in unnamed if not name.startswith("p14m")]
        self.assertEqual(unnamed, [], "these stages appear on the page unnamed")


if __name__ == "__main__":
    unittest.main()
