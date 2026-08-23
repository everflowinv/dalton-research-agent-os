import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.model_deployment import openclaw_broker_profiles
from dalton_core.model_router import ModelRouter
from dalton_core.openclaw_catalog_reconcile import (
    OpenClawCatalogError,
    load_openclaw_config,
    openclaw_broker_profiles_from_config,
    reconcile_openclaw_model_catalog,
)


NOW = datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc)


def _config() -> dict:
    providers: dict[str, dict] = {}
    broker_profiles: list[dict] = []
    for profile in openclaw_broker_profiles(checked_at=NOW):
        provider = profile["provider"]
        providers.setdefault(provider, {"apiKey": "must-not-leak", "models": []})
        providers[provider]["models"].append({
            "id": profile["model"],
            "contextWindow": profile["context"]["max_context_tokens"],
            "maxTokens": profile["context"]["max_output_tokens"],
            "cost": {
                "input": profile["cost"]["input_per_million_usd"],
                "output": profile["cost"]["output_per_million_usd"],
            },
        })
        broker_profiles.append({
            "id": profile["id"],
            "model": f"{provider}/{profile['model']}",
            "maxTokens": profile["context"]["max_output_tokens"],
        })
    return {
        "models": {"providers": providers},
        "plugins": {
            "entries": {
                "dalton-openclaw-model-broker": {
                    "config": {"profiles": broker_profiles, "authKey": "also-secret"}
                }
            }
        },
    }


class OpenClawCatalogReconcileTests(unittest.TestCase):
    def test_static_catalog_is_in_sync_and_report_contains_no_secrets(self):
        report = reconcile_openclaw_model_catalog(_config(), checked_at=NOW)
        self.assertTrue(report["catalog_in_sync"])
        self.assertEqual(report["provider_model_count"], 23)
        self.assertEqual(report["broker_profile_count"], 23)
        serialized = json.dumps(report)
        self.assertNotIn("must-not-leak", serialized)
        self.assertNotIn("also-secret", serialized)

    def test_new_explicit_broker_profile_is_queued_and_runnable(self):
        config = _config()
        config["models"]["providers"]["google"]["models"].append({
            "id": "gemini-new-smoke",
            "contextWindow": 100_000,
            "maxTokens": 8_000,
            "cost": {"input": 0.4, "output": 1.2},
        })
        config["plugins"]["entries"]["dalton-openclaw-model-broker"]["config"]["profiles"].append({
            "id": "profile:gemini-new-smoke",
            "model": "google/gemini-new-smoke",
            "maxTokens": 4_000,
        })
        report = reconcile_openclaw_model_catalog(config, checked_at=NOW)
        self.assertEqual(report["new_broker_profile_ids"], ["profile:gemini-new-smoke"])
        self.assertEqual(report["smoke_required_profile_ids"], ["profile:gemini-new-smoke"])
        catalog = openclaw_broker_profiles_from_config(config, checked_at=NOW)
        dynamic = next(item for item in catalog if item["id"] == "profile:gemini-new-smoke")
        self.assertEqual(dynamic["capabilities"], ["verify"])
        self.assertEqual(dynamic["family"], "unclassified:google")
        self.assertEqual(dynamic["limits"]["max_output_tokens"], 4_000)
        with tempfile.TemporaryDirectory() as directory:
            with ModelRouter(Path(directory) / "router.sqlite") as router:
                self.assertEqual(router.register_profile(dynamic)["status"], "fresh")

    def test_changed_static_route_and_orphan_fail_closed(self):
        config = _config()
        config["models"]["providers"]["deepseek"]["models"].append({
            "id": "deepseek-v4-flash-alt",
            "contextWindow": 100_000,
            "maxTokens": 8_000,
            "cost": {"input": 0.2, "output": 0.6},
        })
        broker = config["plugins"]["entries"]["dalton-openclaw-model-broker"]["config"]["profiles"][0]
        broker["model"] = "deepseek/deepseek-v4-flash-alt"
        report = reconcile_openclaw_model_catalog(config, checked_at=NOW)
        self.assertEqual(report["changed_broker_profile_ids"], ["profile:deepseek-v4-flash"])
        with self.assertRaisesRegex(OpenClawCatalogError, "changed route"):
            openclaw_broker_profiles_from_config(config, checked_at=NOW)

        orphan = _config()
        del orphan["models"]["providers"]["deepseek"]["models"][0]
        with self.assertRaisesRegex(OpenClawCatalogError, "unknown model"):
            openclaw_broker_profiles_from_config(orphan, checked_at=NOW)

    def test_selected_static_profile_uses_current_public_price(self):
        config = _config()
        sol = next(
            model
            for model in config["models"]["providers"]["openai"]["models"]
            if model["id"] == "gpt-5.6-sol"
        )
        sol["cost"] = {"input": 4, "output": 20}
        catalog = openclaw_broker_profiles_from_config(
            config,
            checked_at=NOW,
            profile_ids=["profile:gpt-5-6-sol"],
        )
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]["cost"]["input_per_million_usd"], 4.0)
        self.assertEqual(catalog[0]["cost"]["output_per_million_usd"], 20.0)

    def test_loader_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "openclaw.json"
            path.write_text('{"models":{},"models":{}}', encoding="utf-8")
            with self.assertRaisesRegex(OpenClawCatalogError, "duplicate JSON key"):
                load_openclaw_config(path)


if __name__ == "__main__":
    unittest.main()
