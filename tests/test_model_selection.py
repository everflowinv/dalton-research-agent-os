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
from unittest import mock
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
from dalton_core.model_configurations import model_config_names
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


UNPRICED_MODEL_REF = "zai/glm-6-preview"
UNPRICED_PROFILE_ID = "profile:glm-6-preview"


def _with_unpriced_model(config: dict) -> dict:
    """Add a model the gateway offers with no rate card and Dalton never curated.

    It has to be a model with no curated profile: a curated one carries a rate
    card of Dalton's own, and the gateway dropping its published price does not
    make that card unknown.
    """

    provider, _, model = UNPRICED_MODEL_REF.partition("/")
    config["models"]["providers"][provider]["models"].append({
        "id": model, "contextWindow": 1_000_000, "maxTokens": 131_072,
    })
    entry = config["plugins"]["entries"]["dalton-openclaw-model-broker"]
    entry["config"]["profiles"].append({
        "id": UNPRICED_PROFILE_ID, "model": UNPRICED_MODEL_REF, "maxTokens": 131_072,
    })
    entry["llm"]["allowedModels"] = sorted(
        set(entry["llm"]["allowedModels"]) | {UNPRICED_MODEL_REF}
    )
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
        config = _allowing_config()
        profiles = config["plugins"]["entries"]["dalton-openclaw-model-broker"][
            "config"
        ]["profiles"]
        for profile in profiles:
            if profile["id"] in {
                "profile:gemini-3-8-flash",
                "profile:gemini-3-1-pro-preview",
            }:
                profile["providerControls"] = {
                    "mode": "google-generative-ai-count-tokens-v1",
                    "rateCard": {"inputPerMillionUsd": "1",
                                 "outputPerMillionUsd": "2",
                                 "validUntil": "2026-09-10T09:00:00.000000+00:00"},
                }
        return config

    def policy(self, tier: str = "brain") -> dict:
        return self.router.get_policy(self.policies[tier])


class SelectionResolutionTests(RouterCase):
    def test_explicit_then_tier_on_legacy_pin_displays_the_actual_pin(self) -> None:
        pinned = ensure_planner_policy(
            self.router, profile_ids=["profile:gpt-6-astra"], now=NOW,
            policy_id="model-routing-policy:legacy-display",
        )["policy_version_ref"]
        explicit = publish_selection(
            self.router, policy_version_ref=pinned, purpose=BRAIN_PURPOSE,
            mode="explicit", chain=["profile:claude-fable-5-1"], now=NOW,
        )["policy_version_ref"]
        rolled = publish_selection(
            self.router, policy_version_ref=explicit, purpose=BRAIN_PURPOSE,
            mode="tier", now=NOW,
        )["policy_version_ref"]
        shown = effective_chain(self.router.get_policy(rolled), BRAIN_PURPOSE)
        self.assertEqual(shown["mode"], "legacy_pin")
        self.assertEqual(shown["chain"], ["profile:gpt-6-astra"])
        decision = self.router.route(
            _work("work:legacy-display"), attempt_number=1, capability="research",
            policy_version_ref=rolled,
            credential_slot_refs=credential_slots_for(
                self.router, ["profile:gpt-6-astra"]),
            required_modalities=["text"], required_context_tokens=2_000,
            estimated_input_tokens=1_000, estimated_output_tokens=500,
            idempotency_key="legacy-display:1", purpose=BRAIN_PURPOSE,
        )["decision"]
        self.assertEqual(decision["outcome"], "selected", decision)
        self.assertEqual(decision["selected_endpoint"]["model"], "gpt-6-astra")

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
    unpriced = UNPRICED_PROFILE_ID

    def catalog_config(self) -> dict:
        # A model the gateway offers, publishes no price for, and Dalton has
        # never curated -- the only shape that is genuinely unpriced.
        return _with_unpriced_model(_allowing_config())

    def test_an_unpriced_model_registers_at_a_declared_ceiling_not_at_zero(
        self,
    ) -> None:
        from dalton_core.openclaw_catalog_reconcile import (
            UNPRICED_CEILING_INPUT_PER_MILLION_USD,
            UNPRICED_CEILING_OUTPUT_PER_MILLION_USD,
        )

        profile = next(
            item for item in self.router.latest_profiles()
            if item["id"] == self.unpriced
        )
        self.assertTrue(profile["unpriced"])
        # Zero would mean free: sorted first by a cost-ascending policy and
        # settled at nothing by the day ledger. The ceiling over-reserves,
        # which is the direction an unknown has to fail in.
        self.assertEqual(
            profile["cost"]["input_per_million_usd"],
            UNPRICED_CEILING_INPUT_PER_MILLION_USD,
        )
        self.assertEqual(
            profile["cost"]["output_per_million_usd"],
            UNPRICED_CEILING_OUTPUT_PER_MILLION_USD,
        )
        dearest = max(
            item["cost"]["input_per_million_usd"]
            for item in self.router.latest_profiles()
            if not item.get("unpriced")
        )
        self.assertGreater(profile["cost"]["input_per_million_usd"], dearest)

    def test_the_gateway_dropping_a_price_marks_even_a_curated_route_unpriced(
        self,
    ) -> None:
        # gemini-3.5-flash-lite is curated here and is the cheap chain's last
        # link. Runtime catalog facts supersede the static bootstrap card: an
        # absent price becomes a conservative ceiling and a last-link marker.
        before = next(
            item for item in self.router.latest_profiles()
            if item["id"] == "profile:gemini-3-5-flash-lite"
        )
        config = _unprice(self.catalog_config(), "google/gemini-3.5-flash-lite")
        sync_openclaw_model_catalog(self.router, config, checked_at=NOW)
        after = next(
            item for item in self.router.latest_profiles()
            if item["id"] == "profile:gemini-3-5-flash-lite"
        )
        self.assertNotEqual(after["cost"], before["cost"])
        self.assertEqual(after["cost"]["input_per_million_usd"], 25.0)
        self.assertTrue(after["unpriced"])

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
    def test_bootstrap_brain_defaults_do_not_block_a_verifier_selection(self) -> None:
        checked = validate_selection(
            self.router, purpose=VERIFY_PURPOSE, mode="explicit",
            chain=["profile:gemini-3-8-flash"],
        )
        self.assertEqual(checked["chain"][0], "profile:gemini-3-8-flash")

    def test_uncontrolled_profile_cannot_be_selected_for_verification(self) -> None:
        with self.assertRaisesRegex(FallbackChainError, "providerControls"):
            validate_selection(
                self.router, purpose=VERIFY_PURPOSE, mode="explicit",
                chain=["profile:claude-fable-5-1"],
            )

    def test_unknown_lineage_cannot_be_selected_for_verification(self) -> None:
        config = _config()
        broker = config["plugins"]["entries"]["dalton-openclaw-model-broker"]["config"]["profiles"][0]
        provider, model = broker["model"].split("/", 1)
        self.router.declare_profile_metadata(
            declaration_ref="metadata:unknown-fixture:1", profile_id=broker["id"],
            version=1, prior_declaration_ref=None, provider=provider, model=model,
            family="unclassified:fixture", capabilities=["research", "verify"],
            actor_ref=OWNER, created_at=NOW.isoformat(),
        )
        sync_openclaw_model_catalog(self.router, config, checked_at=NOW)
        with self.assertRaisesRegex(FallbackChainError, "no declared family"):
            validate_selection(self.router, purpose=VERIFY_PURPOSE, mode="explicit",
                               chain=[broker["id"]])

    def test_a_verifier_from_a_different_family_is_accepted(self) -> None:
        checked = validate_selection(
            self.router, purpose=VERIFY_PURPOSE, mode="explicit",
            chain=["profile:gemini-3-8-flash"],
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
    def test_cross_provider_selection_adds_slot_and_the_actual_route_selects_it(self) -> None:
        self.model_config["credential_slot_refs"] = ["credential-slot:openai:dalton"]
        self.config_path.write_text(json.dumps(self.model_config), encoding="utf-8")
        set_model_selection(
            self.root, purpose="plan", mode="explicit",
            chain=["profile:claude-fable-5-1"], now=NOW)
        config = self.stored()
        self.assertIn("credential-slot:openclaw:claude-cli-gateway",
                      config["credential_slot_refs"])
        decision = self.router.route(
            _work("work:cross-provider-selection"), attempt_number=1,
            capability="research", policy_version_ref=config["routing_policy_ref"],
            credential_slot_refs=config["credential_slot_refs"],
            required_modalities=["text"], required_context_tokens=2_000,
            estimated_input_tokens=1_000, estimated_output_tokens=500,
            idempotency_key="route:cross-provider-selection", purpose="plan",
        )["decision"]
        self.assertEqual(decision["selected_endpoint"]["provider"],
                         "claude-cli-gateway")

    def test_plan_selection_atomically_repoints_the_resident_service_pin(self) -> None:
        state = self.root / "state" / "dalton-core"
        state.mkdir(parents=True)
        moved_config = state / self.config_path.name
        self.config_path.replace(moved_config)
        service_path = self.root / "config" / "service.json"
        service_path.parent.mkdir()
        service = {
            "model_router_db": str(self.root / "model-router.sqlite"),
            "bounded_planner": {"config": {
                "planner_model_router_db": str(self.root / "model-router.sqlite"),
                "planner_routing_policy_ref": self.policies["brain"],
                "planner_credential_slot_refs": ["credential-slot:openai:dalton"],
            }},
        }
        service_path.write_text(json.dumps(service), encoding="utf-8")
        result = set_model_selection(
            state, purpose="plan", mode="explicit",
            chain=["profile:claude-fable-5-1"], now=NOW)
        updated = json.loads(service_path.read_text(encoding="utf-8"))
        self.assertNotEqual(
            updated["bounded_planner"]["config"]["planner_routing_policy_ref"],
            self.policies["brain"])
        self.assertIn(
            "credential-slot:openclaw:claude-cli-gateway",
            updated["bounded_planner"]["config"]["planner_credential_slot_refs"],
        )
        self.assertTrue(result["requires_restart"])
        self.assertIn("service.json#bounded_planner.planner_routing_policy_ref",
                      result["model_configs_repointed"])

    def test_registry_covers_every_installed_role_configuration(self) -> None:
        self.assertEqual(set(model_config_names()), {
            "document-extraction-model-config.json",
            "research-planner-model-config.json",
            "initial-screen-model-config.json",
            "claim-index-model-config.json",
            "dossier-model-config.json",
            "company-dossier-verifier-model-config.json",
            "dossier-verifier-model-config.json",
            "earnings-season-model-config.json",
            "earnings-season-verifier-model-config.json",
            "event-judgement-model-config.json",
            "event-verifier-model-config.json",
            "zero-base-review-model-config.json",
            "zero-base-review-verifier-model-config.json",
        })

    def test_a_selection_repoints_two_real_role_configs_together(self) -> None:
        verifier = self.root / "event-verifier-model-config.json"
        verifier.write_text(json.dumps(self.model_config), encoding="utf-8")
        result = set_model_selection(
            self.root, purpose="event_judgement_verifier", mode="tier", now=NOW)
        self.assertEqual(result["model_configs_repointed"], [
            "research-planner-model-config.json", "event-verifier-model-config.json"])
        first = self.stored()["routing_policy_ref"]
        second = json.loads(verifier.read_text("utf-8"))["routing_policy_ref"]
        self.assertEqual(first, second)
        policy = self.router.get_policy(first)
        self.assertEqual(
            policy["purpose_overrides"]["event_judgement_verifier"],
            {"mode": "tier"},
        )

    def test_a_config_replace_failure_restores_every_role_pin(self) -> None:
        verifier = self.root / "event-verifier-model-config.json"
        verifier.write_text(json.dumps(self.model_config), encoding="utf-8")
        before = {path: path.read_bytes() for path in (self.config_path, verifier)}
        real_replace = __import__("os").replace
        replacements = 0

        def fail_second(source, target):
            nonlocal replacements
            if str(source).endswith(".model-selection.tmp"):
                replacements += 1
                if replacements == 2:
                    raise OSError("disk refused the second replacement")
            return real_replace(source, target)

        with mock.patch("dalton_core.model_selection.os.replace", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "second replacement"):
                set_model_selection(
                    self.root, purpose="event_judgement_verifier",
                    mode="tier", now=NOW)
        self.assertEqual(
            {path: path.read_bytes() for path in (self.config_path, verifier)}, before)
        retried = set_model_selection(
            self.root, purpose="event_judgement_verifier", mode="tier", now=NOW)
        self.assertEqual(retried["status"], "published")
        self.assertEqual(
            json.loads(self.config_path.read_text("utf-8"))["routing_policy_ref"],
            json.loads(verifier.read_text("utf-8"))["routing_policy_ref"],
        )

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

    def test_a_verifier_selection_does_not_assume_the_bootstrap_producer(
        self,
    ) -> None:
        set_model_selection(
            self.root, purpose=VERIFY_PURPOSE, mode="explicit",
            chain=["profile:gemini-3-8-flash"], now=NOW,
        )
        current = self.router.get_policy(self.stored()["routing_policy_ref"])
        self.assertEqual(current["purpose_overrides"][VERIFY_PURPOSE]["chain"],
                         ["profile:gemini-3-8-flash"])

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

    def test_a_verification_failure_puts_the_file_back_byte_for_byte(self) -> None:
        # The read-back is the point of the whole sequence, so what it does
        # when it fails is the thing worth pinning: the host's file goes back
        # exactly as it was, and the backup stays -- a restore that deletes its
        # own evidence is not a restore.
        import dalton_core.openclaw_allow_patch as patch_module

        def refuse(path):
            raise patch_module.AllowPatchError("the written config did not read back")

        original = patch_module.load_openclaw_config
        patch_module.load_openclaw_config = lambda path: (
            original(path) if not getattr(refuse, "armed", False) else refuse(path)
        )
        try:
            refuse.armed = False

            def arm_after_first(path):
                value = original(path)
                if getattr(arm_after_first, "seen", 0):
                    raise patch_module.AllowPatchError(
                        "the written configuration did not read back the same"
                    )
                arm_after_first.seen = 1
                return value

            patch_module.load_openclaw_config = arm_after_first
            with self.assertRaises(AllowPatchError):
                apply_allow_patch(self.path, self.held_back, now=NOW)
        finally:
            patch_module.load_openclaw_config = original
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)
        backups = sorted(self.root.glob("openclaw.json.bak-dalton-allow-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), self.original)
        self.assertEqual(sorted(self.root.glob(".*.tmp")), [])

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
        self._drop("profile:gpt-6-astra")
        delivered: list[dict] = []
        first = record_retirement_notices(
            self.router, state_dir=self.root, delivery=delivered.append,
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
            self.router, state_dir=self.root, delivery=delivered.append,
        )
        self.assertEqual(again["notices_written"], [])
        self.assertEqual(len(delivered), len(first["notices_written"]))
        self.assertEqual(again["notices_repeated"], first["notices_written"])

    def test_a_stage_left_with_nothing_says_so_rather_than_naming_a_model(
        self,
    ) -> None:
        self._drop(*tier_chain("brain"))
        affected = retirement_fallbacks(self.router, policy=self.policy("brain"))
        row = next(item for item in affected if item["purpose"] == BRAIN_PURPOSE)
        self.assertIsNone(row["replacement_profile_id"])
        self.assertIn("没有可用的替代模型", row["message"])

    def test_acknowledging_closes_it_and_a_second_press_is_a_no_op(self) -> None:
        self._drop("profile:gpt-6-astra")
        record_retirement_notices(self.router, state_dir=self.root)
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
        self._drop("profile:gpt-6-astra")
        record_retirement_notices(self.router, state_dir=self.root)
        with self.assertRaises(Exception):
            self.router.connection.execute(
                "UPDATE model_fallback_notices SET purpose='ask'"
            )
        with self.assertRaises(Exception):
            self.router.connection.execute("DELETE FROM model_fallback_notices")


class ReviewFindingTests(StateDirectoryCase):
    """The four orderings and two invariants the first review found missing."""

    def _drop(self, *profile_ids: str) -> dict:
        config = _allowing_config()
        for profile_id in profile_ids:
            _drop_broker_profile(config, profile_id)
        return sync_openclaw_model_catalog(self.router, config, checked_at=NOW)

    def test_the_installers_sync_retires_and_the_lane_still_tells_the_owner(
        self,
    ) -> None:
        # The install order: install.sh runs the same catalog sync before this
        # lane has ever had an hour, so by the time the lane first runs there is
        # no delta left to read. Every stage pointed at the retired model still
        # has to be told.
        self._drop("profile:gpt-6-astra")
        self.assertEqual(self.router.fallback_notices(), [])
        result = record_retirement_notices(self.router, state_dir=self.root)
        self.assertTrue(result["notices_written"])
        self.assertIn(BRAIN_PURPOSE, result["affected_purposes"])
        self.assertIn(
            "profile:gpt-6-astra",
            [notice["profile_id"] for notice in self.router.fallback_notices()],
        )

    def test_an_error_between_the_sync_and_the_notice_loses_nothing(self) -> None:
        # The transient order: the sync commits, the notice write blows up, and
        # the next run has to heal it. It does, because the notice set is read
        # from what is retired now rather than from what this run retired.
        self._drop("profile:gpt-6-astra")

        def explode(notice):
            raise RuntimeError("the delivery seam threw")

        with self.assertRaises(RuntimeError):
            record_retirement_notices(
                self.router, state_dir=self.root, delivery=explode
            )
        healed = record_retirement_notices(self.router, state_dir=self.root)
        self.assertIn(
            BRAIN_PURPOSE,
            [notice["purpose"] for notice in self.router.fallback_notices()],
        )
        # Whatever the first attempt managed to write is not written twice.
        again = record_retirement_notices(self.router, state_dir=self.root)
        self.assertEqual(again["notices_written"], [])
        self.assertEqual(
            len(self.router.fallback_notices()),
            len(healed["notices_written"]) + len(healed["notices_repeated"]),
        )

    def test_a_selection_pointed_at_a_retired_model_is_told_about_too(self) -> None:
        published = publish_selection(
            self.router, policy_version_ref=self.policies["brain"],
            purpose=BRAIN_PURPOSE, mode="explicit",
            chain=["profile:gpt-6-astra"], now=NOW,
        )
        self.config_path.write_text(json.dumps({
            **self.model_config,
            "routing_policy_ref": published["policy_version_ref"],
        }), encoding="utf-8")
        self._drop("profile:gpt-6-astra")
        record_retirement_notices(self.router, state_dir=self.root)
        notice = next(
            item for item in self.router.fallback_notices()
            if item["purpose"] == BRAIN_PURPOSE
        )
        self.assertEqual(notice["profile_id"], "profile:gpt-6-astra")
        self.assertEqual(
            notice["detail"]["superseded_chain"], ["profile:gpt-6-astra"]
        )

    def test_replaying_a_work_order_from_before_selection_still_replays(
        self,
    ) -> None:
        # The purpose joins the request identity only when the pinned policy
        # carries an override for it. Otherwise a route request made before
        # selection existed would hash differently on replay, come back
        # ``conflict``, and stop a lane that had done nothing wrong.
        arguments = dict(
            attempt_number=1, capability="research",
            policy_version_ref=self.policies["brain"],
            credential_slot_refs=credential_slots_for(
                self.router, list(tier_chain("brain"))
            ),
            required_modalities=["text"], required_context_tokens=2_000,
            estimated_input_tokens=1_000, estimated_output_tokens=500,
            idempotency_key="p14m2-replay:1", tier="brain",
        )
        work = _work("work:p14m2-replay")
        first = self.router.route(work, **arguments)
        self.assertEqual(first["status"], "fresh")
        # The same request, now naming its purpose, against a policy with no
        # override for it: still the same request.
        replay = self.router.route(work, purpose=BRAIN_PURPOSE, **arguments)
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(replay["decision"]["id"], first["decision"]["id"])

    def test_a_selection_makes_the_same_work_a_different_request(self) -> None:
        published = publish_selection(
            self.router, policy_version_ref=self.policies["brain"],
            purpose=BRAIN_PURPOSE, mode="explicit",
            chain=["profile:claude-fable-5-1"], now=NOW,
        )
        arguments = dict(
            attempt_number=1, capability="research",
            policy_version_ref=published["policy_version_ref"],
            credential_slot_refs=credential_slots_for(
                self.router, list(tier_chain("brain"))
            ),
            required_modalities=["text"], required_context_tokens=2_000,
            estimated_input_tokens=1_000, estimated_output_tokens=500,
            idempotency_key="p14m2-selected:1", tier="brain",
        )
        work = _work("work:p14m2-selected")
        self.router.route(work, purpose=BRAIN_PURPOSE, **arguments)
        # Same key, same work, but no purpose supplied: the identity differs
        # because the policy has a selection for it, so this is a conflict
        # rather than a replay of somebody else's answer.
        without = self.router.route(work, **arguments)
        self.assertEqual(without["status"], "conflict")

    def test_re_running_the_installer_does_not_undo_a_selection(self) -> None:
        published = publish_selection(
            self.router, policy_version_ref=self.policies["brain"],
            purpose=BRAIN_PURPOSE, mode="explicit",
            chain=["profile:claude-fable-5-1"], actor_ref=OWNER, now=NOW,
        )
        chosen = self.router.get_policy(published["policy_version_ref"])
        self.assertEqual(
            chosen["purpose_overrides"][BRAIN_PURPOSE]["actor_ref"], OWNER
        )
        # The installer runs ensure_*_policy again with a different tier, which
        # is a real content change and appends a version. The owner's choice
        # has to ride forward on it.
        again = ensure_planner_policy(
            self.router, tier="cheap", now=NOW,
            policy_id="model-routing-policy:p14m2-brain",
        )
        self.assertEqual(again["status"], "fresh")
        carried = self.router.get_policy(again["policy_version_ref"])
        self.assertEqual(
            carried["purpose_overrides"][BRAIN_PURPOSE]["chain"],
            ["profile:claude-fable-5-1"],
        )
        self.assertEqual(
            carried["purpose_overrides"][BRAIN_PURPOSE]["actor_ref"], OWNER
        )

    def test_publishing_against_a_version_that_has_moved_on_is_refused(self) -> None:
        stale = self.policies["brain"]
        publish_selection(
            self.router, policy_version_ref=stale, purpose=BRAIN_PURPOSE,
            mode="explicit", chain=["profile:claude-fable-5-1"], now=NOW,
        )
        # Somebody read the old version and is now publishing against it. The
        # new version would be built from content that is no longer current,
        # so whatever changed in between would be discarded without a word.
        with self.assertRaisesRegex(ModelSelectionError, "has moved on"):
            publish_selection(
                self.router, policy_version_ref=stale, purpose=CHEAP_PURPOSE,
                mode="tier", now=NOW,
            )

    def test_an_unpriced_model_in_front_of_a_retired_one_is_still_reachable(
        self,
    ) -> None:
        # "Last" has to mean last of what can still be reached. Measured over
        # the declared chain, an unpriced link in front of a retired one would
        # be refused for not being last while being the only thing left.
        config = _with_unpriced_model(_allowing_config())
        sync_openclaw_model_catalog(self.router, config, checked_at=NOW)
        # A chain that was legal when it was written -- the unpriced link is
        # not last, so publishing it through the selection path is refused --
        # and then the link behind it goes away. Registered directly because
        # the rule under test is the router's, not the selection validator's.
        pinned = self.router.get_policy(self.policies["cheap"])
        wire = {
            key: value for key, value in pinned.items()
            if key not in {"content_hash", "policy_version_ref", "version",
                           "created_at", "prior_version_ref"}
        }
        chain = [UNPRICED_PROFILE_ID, "profile:gemini-3-5-flash-lite"]
        wire.update({
            "policy_version_ref": "model-routing-policy-version:p14m2-cheap:2",
            "version": 2,
            "prior_version_ref": pinned["policy_version_ref"],
            "created_at": NOW.isoformat(timespec="microseconds"),
            "filters": {**pinned["filters"], "allowed_profile_ids": chain},
            "purpose_overrides": {
                CHEAP_PURPOSE: {"mode": "explicit", "chain": chain}
            },
        })
        self.router.register_policy(wire)
        _drop_broker_profile(config, "profile:gemini-3-5-flash-lite")
        sync_openclaw_model_catalog(self.router, config, checked_at=NOW)
        decision = self.router.route(
            _work("work:p14m2-unpriced-live", capability="research"),
            attempt_number=1, capability="research",
            policy_version_ref=wire["policy_version_ref"],
            credential_slot_refs=credential_slots_for(
                self.router, [UNPRICED_PROFILE_ID]
            ),
            required_modalities=["text"], required_context_tokens=2_000,
            estimated_input_tokens=1_000, estimated_output_tokens=500,
            idempotency_key="p14m2-unpriced-live:1", tier="cheap",
            purpose=CHEAP_PURPOSE,
        )["decision"]
        self.assertEqual(decision["outcome"], "selected")
        profile = self.router.get_profile(decision["selected_profile_version_ref"])
        self.assertEqual(profile["id"], UNPRICED_PROFILE_ID)


class GovernanceOperationTests(unittest.TestCase):
    def test_the_model_operations_are_human_only_and_actor_bound(self) -> None:
        from dalton_core import writer_server

        for operation in ("set_model_selection", "allow_openclaw_model",
                          "declare_model_profile_metadata",
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
        self.assertEqual(
            writer_server.OPERATION_FIELDS["declare_model_profile_metadata"],
            frozenset({"profile_id", "profile_version_ref", "profile_hash",
                       "family", "capabilities", "actor_ref"}),
        )

    def test_writer_binds_metadata_to_the_current_profile_route_and_actor(self) -> None:
        from dalton_core import writer_server
        from dalton_core.writer_server import WriterServer
        from dalton_core.model_deployment import openclaw_broker_profiles

        with tempfile.TemporaryDirectory() as directory:
            router_path = Path(directory) / "router.sqlite"
            with ModelRouter(router_path) as router:
                profile = openclaw_broker_profiles(checked_at=NOW)[0]
                router.register_profile(profile)
                profile = next(item for item in router.latest_profiles()
                               if item["id"] == profile["id"])
            server = object.__new__(WriterServer)
            server._model_router_db = lambda: str(router_path)
            server.lane_launcher = lambda _name: None
            result = server._op_declare_model_profile_metadata({
                "profile_id": profile["id"], "family": "declared-family",
                "capabilities": ["research", "verify"], "actor_ref": OWNER,
                "profile_version_ref": profile["profile_version_ref"],
                "profile_hash": profile["content_hash"],
            })
            declaration = result["declaration"]
            self.assertEqual(
                (declaration["provider"], declaration["model"]),
                (profile["provider"], profile["model"]),
            )
            self.assertEqual(declaration["actor_ref"], OWNER)
            self.assertEqual(result["application_status"], "pending_catalog_sync")
            again = server._op_declare_model_profile_metadata({
                "profile_id": profile["id"], "family": "declared-family",
                "capabilities": ["research", "verify"], "actor_ref": OWNER,
                "profile_version_ref": profile["profile_version_ref"],
                "profile_hash": profile["content_hash"],
            })
            self.assertEqual(again["status"], "duplicate")
            with ModelRouter(router_path, read_only=True) as router:
                count = router.connection.execute(
                    "SELECT COUNT(*) FROM model_profile_metadata_declarations"
                ).fetchone()[0]
            self.assertEqual(count, 1)
            self.assertNotIn("provider", writer_server.OPERATION_FIELDS[
                "declare_model_profile_metadata"])

    def test_writer_refuses_a_stale_profile_version_without_writing(self) -> None:
        from dalton_core.writer_server import WriterServer, WriterServerError
        from dalton_core.model_deployment import openclaw_broker_profiles

        with tempfile.TemporaryDirectory() as directory:
            router_path = Path(directory) / "router.sqlite"
            with ModelRouter(router_path) as router:
                profile = openclaw_broker_profiles(checked_at=NOW)[0]
                router.register_profile(profile)
            server = object.__new__(WriterServer)
            server._model_router_db = lambda: str(router_path)
            server.lane_launcher = lambda _name: None
            with self.assertRaisesRegex(WriterServerError, "route changed"):
                server._op_declare_model_profile_metadata({
                    "profile_id": profile["id"], "family": "family",
                    "capabilities": ["research"], "actor_ref": OWNER,
                    "profile_version_ref": profile["profile_version_ref"],
                    "profile_hash": "0" * 64,
                })
            with ModelRouter(router_path, read_only=True) as router:
                self.assertIsNone(router.latest_profile_metadata(profile["id"]))

    def test_writer_applies_the_declaration_to_the_routable_profile_now(self) -> None:
        from types import SimpleNamespace
        from dalton_core.writer_server import WriterServer

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            router_path = root / "router.sqlite"
            openclaw_path = root / "openclaw.json"
            openclaw_path.write_text(json.dumps(_allowing_config()), encoding="utf-8")
            with ModelRouter(router_path) as router:
                sync_openclaw_model_catalog(
                    router, _allowing_config(), checked_at=NOW)
                profile = router.latest_profiles()[0]
            lane_config = root / "model-catalog-sync.json"
            lane_config.write_text(json.dumps({
                "openclaw_config_path": str(openclaw_path),
                "model_router_db": str(router_path),
            }), encoding="utf-8")
            server = object.__new__(WriterServer)
            server._model_router_db = lambda: str(router_path)
            server.lane_launcher = lambda _name: SimpleNamespace(
                config_path=lane_config)
            result = server._op_declare_model_profile_metadata({
                "profile_id": profile["id"], "family": "owner-declared-family",
                "capabilities": ["research", "verify"], "actor_ref": OWNER,
                "profile_version_ref": profile["profile_version_ref"],
                "profile_hash": profile["content_hash"],
            })
            self.assertEqual(result["application_status"], "applied")
            with ModelRouter(router_path, read_only=True) as router:
                current = next(
                    item for item in router.latest_profiles()
                    if item["id"] == profile["id"])
            self.assertEqual(current["family"], "owner-declared-family")
            self.assertEqual(current["capabilities"], ["research", "verify"])
            with ModelRouter(router_path) as router:
                policy = ensure_planner_policy(
                    router, profile_ids=[profile["id"]], now=NOW,
                    policy_id="model-routing-policy:metadata-ui-route")
                routed = router.route(
                    _work("work:metadata-ui-next-route"), attempt_number=1,
                    capability="research",
                    policy_version_ref=policy["policy_version_ref"],
                    credential_slot_refs=[current["credential_slot_ref"]],
                    required_modalities=["text"], required_context_tokens=2_000,
                    estimated_input_tokens=1_000, estimated_output_tokens=500,
                    idempotency_key="metadata-ui-next-route:1",
                    tier=None, purpose=None,
                )["decision"]
            self.assertEqual(routed["outcome"], "selected", routed)
            self.assertEqual(routed["selected_profile_version_ref"],
                             current["profile_version_ref"])

            # The host catalog can change after the page/profile was read but
            # before the saved declaration is applied. Never claim that the
            # old-route declaration took effect on the replacement model.
            config = _allowing_config()
            broker = next(item for item in config["plugins"]["entries"]
                          ["dalton-openclaw-model-broker"]["config"]["profiles"]
                          if item["id"] == current["id"])
            config["models"]["providers"][current["provider"]]["models"].append({
                "id": "changed-after-read", "contextWindow": 100_000,
                "maxTokens": 8_000, "cost": {"input": 1, "output": 2},
            })
            broker["model"] = f"{current['provider']}/changed-after-read"
            openclaw_path.write_text(json.dumps(config), encoding="utf-8")
            again = server._op_declare_model_profile_metadata({
                "profile_id": current["id"], "family": "owner-declared-family",
                "capabilities": ["research", "verify"], "actor_ref": OWNER,
                "profile_version_ref": current["profile_version_ref"],
                "profile_hash": current["content_hash"],
            })
            self.assertEqual(again["status"], "duplicate")
            self.assertEqual(again["application_status"], "route_changed")
            openclaw_path.write_text("{invalid", encoding="utf-8")
            self.assertEqual(server._sync_model_metadata_now(result["declaration"]),
                             "pending_catalog_sync")

    def test_writer_refuses_metadata_for_a_profile_that_is_not_live(self) -> None:
        from dalton_core.writer_server import WriterServer, WriterServerError

        with tempfile.TemporaryDirectory() as directory:
            router_path = Path(directory) / "router.sqlite"
            with ModelRouter(router_path):
                pass
            server = object.__new__(WriterServer)
            server._model_router_db = lambda: str(router_path)
            server.lane_launcher = lambda _name: None
            with self.assertRaisesRegex(WriterServerError, "not live"):
                server._op_declare_model_profile_metadata({
                    "profile_id": "profile:missing", "family": "family",
                    "capabilities": ["research"], "actor_ref": OWNER,
                    "profile_version_ref": "model-profile-version:missing:1",
                    "profile_hash": "0" * 64,
                })


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

    def test_page_uses_the_server_restart_status_after_selection(self) -> None:
        page = (Path(__file__).resolve().parents[1]
                / "src/dalton_core/cockpit_control.html").read_text("utf-8")
        self.assertIn("out.requires_restart", page)
        self.assertIn("out.reload_note", page)

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
        self.assertIn("family", view["choices"][0])
        self.assertIn("provider", view["choices"][0])
        self.assertIn("model_ref", view["choices"][0])
        self.assertIsInstance(view["choices"][0]["capabilities"], list)
        catalog = view["catalog"]
        self.assertTrue(catalog["available"])
        for key in ("in_openclaw_not_allowed", "allowed_not_in_dalton",
                    "dalton_not_in_openclaw"):
            self.assertIsInstance(catalog[key], list)
            self.assertTrue(catalog[f"{key}_note"])

    def test_page_reads_each_stage_from_its_actual_consumer_config(self) -> None:
        with ModelRouter(self.router_db) as router:
            sync_openclaw_model_catalog(router, _allowing_config(), checked_at=NOW)
            profile_ids = [item["id"] for item in router.latest_profiles()][:4]
            refs = []
            for number, profile_id in enumerate(profile_ids, 1):
                refs.append(ensure_planner_policy(
                    router, profile_ids=[profile_id], now=NOW,
                    policy_id=f"model-routing-policy:actual-binding-{number}",
                )["policy_version_ref"])
        base = {
            "model_router_db": str(self.router_db),
            "credential_slot_refs": [],
        }
        state = self.root / "install" / "state" / "dalton-core"
        state.mkdir(parents=True)
        paths = [
            self.root / "cockpit.json",
            state / "research-planner-model-config.json",
            state / "dossier-model-config.json",
            state / "company-dossier-verifier-model-config.json",
        ]
        for path, ref in zip(paths, refs, strict=True):
            path.write_text(json.dumps({**base, "routing_policy_ref": ref}),
                            encoding="utf-8")
        (state / "initial-screen-model-config.json").write_text(
            json.dumps({**base, "routing_policy_ref": refs[2]}), encoding="utf-8")
        service_path = self.root / "install" / "config" / "service.json"
        service_path.parent.mkdir()
        service_path.write_text(json.dumps({
            "bounded_planner": {"config": {
                "planner_model_router_db": str(self.router_db),
                "planner_routing_policy_ref": refs[1],
            }},
        }), encoding="utf-8")
        plane = self.plane(with_model_config=False)
        object.__setattr__(plane.config, "model_config_path", paths[0])
        object.__setattr__(plane.config, "state_dir", state)
        view = plane.models()
        rows = {row["purpose"]: row for row in view["purposes"]}
        self.assertEqual(rows["ask"]["policy_version_ref"], refs[0])
        self.assertFalse(rows["ask"]["editable"])
        self.assertEqual(rows["plan"]["policy_version_ref"], refs[1])
        self.assertEqual(rows["dossier"]["policy_version_ref"], refs[2])
        self.assertEqual(rows["dossier_verifier"]["policy_version_ref"], refs[3])
        self.assertEqual(
            [Path(rows[name]["configuration_source"]).name for name in
             ("ask", "plan", "dossier", "dossier_verifier")],
            ["cockpit.json", "service.json#bounded_planner.config.planner_routing_policy_ref",
             "dossier-model-config.json",
             "company-dossier-verifier-model-config.json"],
        )
        self.assertEqual(
            [rows[name]["chain"][0]["model"] for name in
             ("ask", "plan", "dossier", "dossier_verifier")],
            profile_ids,
        )
        for purpose in ("model_spec", "debate_map", "conviction_call"):
            self.assertEqual(rows[purpose]["policy_version_ref"], refs[2])
            self.assertEqual(Path(rows[purpose]["configuration_source"]).name,
                             "initial-screen-model-config.json")
        self.assertEqual(rows["quality_verifier"]["configuration_status"],
                         "unconfigured")
        self.assertEqual(rows["street_estimate"]["configuration_status"],
                         "deterministic")
        self.assertEqual(rows["street_estimate"]["mode"], "deterministic")
        self.assertFalse(rows["street_estimate"]["editable"])

    def test_page_reads_a_binding_from_its_own_router_even_when_refs_match(self) -> None:
        primary_profile = secondary_profile = None
        with ModelRouter(self.router_db) as router:
            sync_openclaw_model_catalog(router, _allowing_config(), checked_at=NOW)
            primary_profile = router.latest_profiles()[0]["id"]
            shared_ref = ensure_planner_policy(
                router, profile_ids=[primary_profile], now=NOW,
                policy_id="model-routing-policy:same-ref",
            )["policy_version_ref"]
        secondary_db = self.root / "secondary-router.sqlite"
        with ModelRouter(secondary_db) as router:
            sync_openclaw_model_catalog(router, _allowing_config(), checked_at=NOW)
            secondary_profile = router.latest_profiles()[1]["id"]
            self.assertEqual(ensure_planner_policy(
                router, profile_ids=[secondary_profile], now=NOW,
                policy_id="model-routing-policy:same-ref",
            )["policy_version_ref"], shared_ref)
        # The cockpit's own config selects the primary router; draft's actual
        # consumer deliberately selects another database with the same ref.
        cockpit = self.root / "research-planner-model-config.json"
        cockpit.write_text(json.dumps({
            "routing_policy_ref": shared_ref, "model_router_db": str(self.router_db),
        }), encoding="utf-8")
        (self.root / "initial-screen-model-config.json").write_text(json.dumps({
            "routing_policy_ref": shared_ref, "model_router_db": str(secondary_db),
        }), encoding="utf-8")
        view = self.plane(with_model_config=True).models()
        self.assertTrue(view["available"])
        draft = next(row for row in view["purposes"] if row["purpose"] == "draft")
        self.assertEqual([link["model"] for link in draft["chain"]],
                         [secondary_profile])
        self.assertNotEqual(secondary_profile, primary_profile)

    def test_multi_pin_legacy_policy_is_an_unordered_candidate_set(self) -> None:
        with ModelRouter(self.router_db) as router:
            sync_openclaw_model_catalog(router, _allowing_config(), checked_at=NOW)
            profiles = [item["id"] for item in router.latest_profiles()][:2]
            ref = ensure_planner_policy(
                router, profile_ids=profiles, now=NOW,
                policy_id="model-routing-policy:candidate-set",
            )["policy_version_ref"]
        (self.root / "research-planner-model-config.json").write_text(json.dumps({
            "routing_policy_ref": ref, "model_router_db": str(self.router_db),
        }), encoding="utf-8")
        row = next(item for item in self.plane(with_model_config=True).models()["purposes"]
                   if item["purpose"] == "ask")
        self.assertEqual(row["mode"], "candidate_set")
        self.assertEqual({link["model"] for link in row["chain"]}, set(profiles))
        page = (Path(__file__).resolve().parents[1]
                / "src/dalton_core/cockpit_control.html").read_text("utf-8")
        self.assertIn('pp.mode==="candidate_set"?"、":" → "', page)

    def test_one_unresolved_binding_does_not_hide_the_other_stages(self) -> None:
        self.install()
        (self.root / "initial-screen-model-config.json").write_text(json.dumps({
            "routing_policy_ref": "model-routing-policy-version:missing:9",
            "model_router_db": str(self.router_db),
        }), encoding="utf-8")
        view = self.plane(with_model_config=True).models()
        self.assertTrue(view["available"])
        rows = {row["purpose"]: row for row in view["purposes"]}
        self.assertEqual(rows["draft"]["configuration_status"], "error")
        self.assertIn("missing:9", rows["draft"]["configuration_error"])
        self.assertEqual(rows["draft"]["chain"], [])
        self.assertEqual(rows["ask"]["configuration_status"], "configured")

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
        plane.declare_model_metadata("owner@example.com", {
            "profile_id": "profile:gpt-6-astra", "family": "openai-gpt",
            "capabilities": ["research", "verify"],
            "profile_version_ref": "model-profile-version:gpt-6-astra:1",
            "profile_hash": "0" * 64,
        })
        plane.acknowledge_model_notice(
            "owner@example.com", {"ref": "model-fallback-notice:abc"}
        )
        self.assertEqual(
            [name for name, _ in self.calls],
            ["set_model_selection", "allow_openclaw_model",
             "declare_model_profile_metadata",
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

    def test_invalid_metadata_never_reaches_the_writer(self) -> None:
        from dalton_core.cockpit_plane import CockpitError

        plane = self.plane(with_model_config=True)
        with self.assertRaisesRegex(CockpitError, "能力"):
            plane.declare_model_metadata("owner@example.com", {
                "profile_id": "profile:deepseek-v4-flash",
                "family": "deepseek", "capabilities": [],
                "profile_version_ref": "model-profile-version:deepseek:1",
                "profile_hash": "0" * 64,
            })
        self.assertEqual(self.calls, [])

    def test_a_router_from_before_the_notice_tables_reads_as_no_notices(
        self,
    ) -> None:
        # A read-only open never runs the schema script, so a router written
        # before these tables existed does not have them. The owner's page must
        # not be the thing that discovers that.
        self.install()
        with ModelRouter(self.router_db) as router:
            router.connection.execute("PRAGMA writable_schema = ON")
            for table in ("model_fallback_notice_acks", "model_fallback_notices",
                          "model_openclaw_allow_decisions"):
                router.connection.execute(f"DROP TABLE IF EXISTS {table}")
        plane = self.plane(with_model_config=True)
        view = plane.models()
        self.assertTrue(view["available"])
        self.assertEqual(view["notices"], [])
        self.assertEqual(plane._model_fallback_items(), [])
        self.assertTrue(view["purposes"])

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
