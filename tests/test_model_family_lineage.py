"""Model family derivation: keep lineage on a same-generation upgrade, never guess.

Live 2026-09-23: the broker moved profile:claude-opus-5 to claude-opus-5-5 and
the catalog projection reset its family to ``unclassified:claude-cli-gateway``
(likewise gpt-5-6-sol/luna -> gpt-6-*).  Unclassified is never independent, so
the mission document verifier preflight and the publication worker failed with
"verifier cannot remain independent of every producer family".
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dalton_core import openclaw_catalog_reconcile as reconcile
from dalton_core.model_deployment import _ENDPOINTS, openclaw_broker_profiles
from dalton_core.model_family_lineage import (
    LINEAGE_RULES, derive_model_family, family_for_route,
)
from dalton_core.model_router import ModelRouter, independent_families

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _config() -> dict:
    providers: dict[str, dict] = {}
    broker_profiles: list[dict] = []
    for profile in openclaw_broker_profiles(checked_at=NOW):
        provider = profile["provider"]
        providers.setdefault(provider, {"apiKey": "x", "models": []})
        if not any(m["id"] == profile["model"] for m in providers[provider]["models"]):
            providers[provider]["models"].append({
                "id": profile["model"],
                "contextWindow": profile["context"]["max_context_tokens"],
                "maxTokens": profile["context"]["max_output_tokens"],
                "cost": {"input": profile["cost"]["input_per_million_usd"],
                         "output": profile["cost"]["output_per_million_usd"]},
            })
        broker_profiles.append({
            "id": profile["id"], "model": f"{provider}/{profile['model']}",
            "maxTokens": profile["context"]["max_output_tokens"],
        })
    return {"models": {"providers": providers},
            "plugins": {"entries": {"dalton-openclaw-model-broker": {
                "config": {"profiles": broker_profiles, "authKey": "k"}}}}}


def _move(config: dict, profile_id: str, provider: str, model: str) -> None:
    """The live drift: the broker re-points a curated profile id."""

    models = config["models"]["providers"].setdefault(
        provider, {"apiKey": "x", "models": []})["models"]
    if not any(item["id"] == model for item in models):
        models.append({"id": model, "contextWindow": 200_000, "maxTokens": 32_000,
                       "cost": {"input": 1.0, "output": 5.0}})
    for entry in config["plugins"]["entries"]["dalton-openclaw-model-broker"]["config"]["profiles"]:
        if entry["id"] == profile_id:
            entry["model"] = f"{provider}/{model}"
            return
    config["plugins"]["entries"]["dalton-openclaw-model-broker"]["config"]["profiles"].append(
        {"id": profile_id, "model": f"{provider}/{model}", "maxTokens": 32_000})


def _live_drift() -> dict:
    config = _config()
    _move(config, "profile:claude-opus-5", "claude-cli-gateway", "claude-opus-5-5")
    _move(config, "profile:gpt-5-6-sol", "openai", "gpt-6-sol")
    _move(config, "profile:gpt-5-6-luna", "openai", "gpt-6-luna")
    _move(config, "profile:grok-4-7", "xai", "grok-4.7")
    _move(config, "profile:gemini-3-8-flash-antigravity-high",
          "antigravity-cli-gateway", "gemini-3.8-flash")
    _move(config, "profile:auto-antigravity-cli-gateway-gemini-3-1-pro-e5bd948a788a",
          "antigravity-cli-gateway", "gemini-3.1-pro")
    _move(config, "profile:auto-muse-cli-gateway-muse-spark-1-3-27804db256e4",
          "muse-cli-gateway", "muse-spark-1.3")
    return config


def _families(config: dict) -> dict[str, str]:
    return {row["id"]: row["family"] for row in
            reconcile.openclaw_broker_profiles_from_config(config, checked_at=NOW)}


class DerivationRuleTests(unittest.TestCase):
    def test_every_curated_endpoint_derives_its_own_family(self):
        for endpoint in _ENDPOINTS:
            with self.subTest(endpoint=endpoint["name"]):
                self.assertEqual(
                    derive_model_family(endpoint["provider"], endpoint["model"]),
                    endpoint["family"])
                # The pattern table alone must never contradict the curation.
                by_rule = {family for family, providers, pattern in LINEAGE_RULES
                           if endpoint["provider"] in providers
                           and pattern.fullmatch(endpoint["model"])}
                self.assertLessEqual(by_rule, {endpoint["family"]})

    def test_rules_only_name_curated_families(self):
        curated = {endpoint["family"] for endpoint in _ENDPOINTS}
        for family, _providers, _pattern in LINEAGE_RULES:
            self.assertIn(family, curated)

    def test_same_generation_upgrades_keep_or_find_their_family(self):
        cases = {
            ("claude-cli-gateway", "claude-opus-5-5"): "anthropic-claude-5",
            ("claude-cli-gateway", "claude-sonnet-5-2"): "anthropic-claude-5",
            ("openai", "gpt-6-sol"): "openai-gpt-6",
            ("openai", "gpt-6-luna"): "openai-gpt-6",
            ("xai", "grok-4.7"): "xai-grok-4",
            ("antigravity-cli-gateway", "gemini-3.1-pro"): "google-gemini-3",
            ("antigravity-cli-gateway", "gemini-3.8-flash"): "google-gemini-3",
            ("zai", "glm-5.3-flash"): "zhipu-glm-5.3",
            ("qwen", "deepseek-v4-pro-0901"): "deepseek-v4",
        }
        for (provider, model), family in cases.items():
            with self.subTest(model=model):
                self.assertEqual(derive_model_family(provider, model), family)

    def test_uncertain_lineage_stays_unclassified(self):
        for provider, model in (
            ("muse-cli-gateway", "muse-spark-1.3"),          # no curated lineage
            ("claude-cli-gateway", "claude-opus-6"),        # next generation
            ("openai", "gpt-6.1-sol"),                      # OpenAI minor = new family
            ("openai", "gpt-7"),
            ("openrouter", "claude-opus-5-5"),              # provider never curated for it
            ("claude-cli-gateway", "claude-opus-5-5-20260901"),  # unknown suffix
            ("claude-cli-gateway", "Claude-Opus-5-5"),      # case is not normalised
            ("claude-cli-gateway", "claude-opus-50"),
            ("google", "gemini-flash-latest-2"),            # moving alias, exact only
            ("google", "gemini-4-pro"),
            ("xai", "grok-5"),
            ("deepseek", "deepseek-v4-flash-alt"),
            ("zai", "glm-5.4"),
        ):
            with self.subTest(model=model):
                self.assertIsNone(derive_model_family(provider, model))
                self.assertEqual(family_for_route(provider, model),
                                 f"unclassified:{provider}")

    def test_a_derived_family_never_creates_false_independence(self):
        # gpt-6-sol is the gpt-6 generation: it must NOT keep its predecessor's
        # openai-gpt-5.6, which would make it "independent" of gpt-6-astra.
        sol = derive_model_family("openai", "gpt-6-sol")
        astra = derive_model_family("openai", "gpt-6-astra")
        self.assertFalse(independent_families(sol, astra))
        opus = derive_model_family("claude-cli-gateway", "claude-opus-5-5")
        fable = derive_model_family("claude-cli-gateway", "claude-fable-5-1")
        self.assertFalse(independent_families(opus, fable))
        self.assertTrue(independent_families(
            derive_model_family("google", "gemini-3.7-flash"), opus))


class CatalogProjectionTests(unittest.TestCase):
    def test_live_drift_projects_certain_families_only(self):
        families = _families(_live_drift())
        self.assertEqual(families["profile:claude-opus-5"], "anthropic-claude-5")
        self.assertEqual(families["profile:gpt-5-6-sol"], "openai-gpt-6")
        self.assertEqual(families["profile:gpt-5-6-luna"], "openai-gpt-6")
        self.assertEqual(families["profile:grok-4-7"], "xai-grok-4")
        self.assertEqual(families["profile:gemini-3-8-flash-antigravity-high"],
                         "google-gemini-3")
        self.assertEqual(
            families["profile:auto-antigravity-cli-gateway-gemini-3-1-pro-e5bd948a788a"],
            "google-gemini-3")
        self.assertEqual(
            families["profile:auto-muse-cli-gateway-muse-spark-1-3-27804db256e4"],
            "unclassified:muse-cli-gateway")

    def test_owner_declaration_still_wins(self):
        config = _live_drift()
        declarations = {"profile:claude-opus-5": {
            "provider": "claude-cli-gateway", "model": "claude-opus-5-5",
            "family": "anthropic-claude-5.5", "capabilities": ["verify"]}}
        row = next(item for item in reconcile.openclaw_broker_profiles_from_config(
            config, checked_at=NOW, metadata_declarations=declarations)
            if item["id"] == "profile:claude-opus-5")
        self.assertEqual(row["family"], "anthropic-claude-5.5")

    def test_profiles_already_reset_recover_on_the_next_sync(self):
        """The deploy must repair the live rows, not only future ones."""

        config = _live_drift()
        with tempfile.TemporaryDirectory() as directory:
            with ModelRouter(Path(directory) / "router.sqlite") as router:
                reconcile.sync_openclaw_model_catalog(
                    router, _config(), checked_at=NOW - timedelta(days=2))
                # Reproduce what the old code wrote live on 2026-09-23.
                with patch.object(reconcile, "family_for_route",
                                  lambda provider, model: f"unclassified:{provider}"):
                    reconcile.sync_openclaw_model_catalog(
                        router, config, checked_at=NOW - timedelta(days=1))
                reset = {p["id"]: p for p in router.latest_profiles()}
                self.assertEqual(reset["profile:claude-opus-5"]["family"],
                                 "unclassified:claude-cli-gateway")
                reset_ref = reset["profile:claude-opus-5"]["profile_version_ref"]
                frozen = copy.deepcopy(router.get_profile(reset_ref))

                status = reconcile.catalog_sync_status(router, config, checked_at=NOW)
                self.assertIn("profile:claude-opus-5", status["drifted_profile_ids"])
                result = reconcile.sync_openclaw_model_catalog(router, config, checked_at=NOW)
                self.assertIn("profile:claude-opus-5", result["updated_profile_ids"])
                self.assertIn("profile:gpt-5-6-sol", result["updated_profile_ids"])
                after = {p["id"]: p for p in router.latest_profiles()}
                self.assertEqual(after["profile:claude-opus-5"]["family"], "anthropic-claude-5")
                self.assertEqual(after["profile:claude-opus-5"]["prior_version_ref"], reset_ref)
                self.assertEqual(after["profile:gpt-5-6-sol"]["family"], "openai-gpt-6")
                self.assertEqual(after["profile:grok-4-7"]["family"], "xai-grok-4")
                self.assertEqual(
                    after["profile:auto-muse-cli-gateway-muse-spark-1-3-27804db256e4"]["family"],
                    "unclassified:muse-cli-gateway")
                # History is append-only: the reset version is still readable.
                self.assertEqual(router.get_profile(reset_ref), frozen)
                again = reconcile.sync_openclaw_model_catalog(router, config, checked_at=NOW)
                self.assertEqual(again["updated_profile_ids"], [])
                self.assertTrue(again["catalog_in_sync"])



class ReadOnlyCheckScriptTests(unittest.TestCase):
    """scripts/check_model_family_independence.py backs the runbook check."""

    def test_it_reports_unclassified_and_drift_without_writing(self):
        import hashlib
        import json
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import check_model_family_independence as check

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory).resolve()
            openclaw = state / "openclaw.json"
            openclaw.write_text(json.dumps(_live_drift()), encoding="utf-8")
            (state / "model-catalog-sync.json").write_text(json.dumps({
                "model_router_db": str(state / "model-router.sqlite"),
                "openclaw_config_path": str(openclaw),
                "follow_provider_catalog": True}), encoding="utf-8")
            with ModelRouter(state / "model-router.sqlite") as router:
                reconcile.sync_openclaw_model_catalog(
                    router, _config(), checked_at=NOW - timedelta(days=2))
                with patch.object(reconcile, "family_for_route",
                                  lambda provider, model: f"unclassified:{provider}"):
                    reconcile.sync_openclaw_model_catalog(
                        router, _live_drift(), checked_at=NOW - timedelta(days=1))
                digest = hashlib.sha256(
                    (state / "model-router.sqlite").read_bytes()).hexdigest()
                report = check.check_state_dir(state, now=NOW)
                self.assertEqual(digest, hashlib.sha256(
                    (state / "model-router.sqlite").read_bytes()).hexdigest())
        self.assertIn("profile:claude-opus-5", report["unclassified_profile_ids"])
        self.assertEqual(report["family_drift"]["profile:claude-opus-5"],
                         {"stored": "unclassified:claude-cli-gateway",
                          "projected": "anthropic-claude-5"})
        self.assertNotIn("profile:auto-muse-cli-gateway-muse-spark-1-3-27804db256e4",
                         report["family_drift"])
        # No document model configs in this fixture: reported, never raised.
        self.assertEqual(report["document_verifier_preflight"]["status"], "failed")

if __name__ == "__main__":
    unittest.main()
