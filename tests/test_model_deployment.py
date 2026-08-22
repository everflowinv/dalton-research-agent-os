from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.model_deployment import (
    ADAPTER_REF,
    BROKER_POLICY_REF,
    install_openclaw_catalog,
    openclaw_broker_profiles,
    upgrade_openclaw_broker_catalog,
    openclaw_policy,
    openclaw_profiles,
)
from dalton_core.model_router import ModelRouter


WHEN = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)


class ModelDeploymentTests(unittest.TestCase):
    def test_all_configured_routes_and_independent_verification_policy(self) -> None:
        profiles = openclaw_profiles(checked_at=WHEN)
        self.assertEqual(len(profiles), 23)
        self.assertEqual(
            {(item["provider"], item["model"]) for item in profiles},
            {
                ("deepseek", "deepseek-v4-flash"),
                ("openai", "gpt-5.6-sol"),
                ("openai", "gpt-5.6-terra"),
                ("openai", "gpt-5.6-luna"),
                ("claude-cli-gateway", "claude-fable-5"),
                ("claude-cli-gateway", "claude-opus-5"),
                ("claude-cli-gateway", "claude-sonnet-5"),
                ("google", "gemini-3.7-flash"),
                ("google", "gemini-flash-latest"),
                ("google", "gemini-3.1-pro-preview"),
                ("google", "gemini-3.5-flash-lite"),
                ("qwen", "qwen3.8-max"),
                ("qwen", "deepseek-v4-flash-0731"),
                ("qwen", "deepseek-v4-pro"),
                ("qwen", "glm-5.2"),
                ("openai", "gpt-5.5"),
                ("deepseek", "deepseek-v4-pro"),
                ("xai", "grok-4.6"),
                ("xai", "grok-build-0.1"),
                ("xai", "grok-4.3"),
                ("xai", "grok-4.20-beta-latest-reasoning"),
                ("xai", "grok-4.20-beta-latest-non-reasoning"),
                ("openrouter", "stealth/ox-alpha"),
            },
        )
        self.assertTrue(all(item["adapter_ref"] == ADAPTER_REF for item in profiles))
        self.assertTrue(
            all(item["credential_slot_ref"].startswith("credential-slot:") for item in profiles)
        )
        policy = openclaw_policy(created_at=WHEN)
        self.assertEqual(
            policy["filters"]["family_independence_capabilities"],
            ["verify", "adjudicate"],
        )

    def test_catalog_persists_all_profiles_and_one_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model-router.sqlite"
            installed = install_openclaw_catalog(path, checked_at=WHEN)
            self.assertEqual(installed["policy"]["status"], "fresh")
            self.assertTrue(all(row["status"] == "fresh" for row in installed["profiles"]))
            with ModelRouter(path) as router:
                profile_count = router.connection.execute(
                    "SELECT COUNT(*) FROM model_endpoint_profile_versions"
                ).fetchone()[0]
                policy_count = router.connection.execute(
                    "SELECT COUNT(*) FROM model_routing_policy_versions"
                ).fetchone()[0]
                self.assertEqual(profile_count, 23)
                self.assertEqual(policy_count, 1)

    def test_v2_profiles_use_ids_accepted_by_broker_protocol(self) -> None:
        profiles = openclaw_broker_profiles(checked_at=WHEN)
        self.assertTrue(all(item["id"].startswith("profile:") for item in profiles))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model-router.sqlite"
            install_openclaw_catalog(path, checked_at=WHEN)
            upgraded = upgrade_openclaw_broker_catalog(path, checked_at=WHEN)
            self.assertEqual(upgraded["policy"]["policy"]["policy_version_ref"], BROKER_POLICY_REF)
            with ModelRouter(path) as router:
                self.assertEqual(len(router.get_policy(BROKER_POLICY_REF)["filters"]["allowed_profile_ids"]), 23)

    def test_v2_profile_version_can_advance_without_changing_entity_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model-router.sqlite"
            install_openclaw_catalog(path, checked_at=WHEN)
            upgrade_openclaw_broker_catalog(path, checked_at=WHEN)
            with ModelRouter(path) as router:
                first = openclaw_broker_profiles(checked_at=WHEN)[0]
                second = dict(first)
                second["version"] = 2
                second["prior_version_ref"] = first["profile_version_ref"]
                second["profile_version_ref"] = first["profile_version_ref"].rsplit(":", 1)[0] + ":2"
                second["created_at"] = (WHEN + timedelta(minutes=1)).isoformat()
                second.pop("content_hash", None)
                self.assertEqual(router.register_profile(second)["status"], "fresh")


if __name__ == "__main__":
    unittest.main()
