from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.model_deployment import (
    ADAPTER_REF,
    ASSESSMENT_POLICY_REF,
    ASSESSMENT_PROFILE_ID,
    BROKER_POLICY_REF,
    VERIFIER_POLICY_REF,
    VERIFIER_PROFILE_ID,
    install_openclaw_catalog,
    openclaw_assessment_policy,
    openclaw_broker_profiles,
    openclaw_verifier_policy,
    upgrade_openclaw_broker_catalog,
    openclaw_policy,
    openclaw_profiles,
)
from dalton_core.model_router import ModelRouter
from tests.test_openclaw_catalog_reconcile import _config


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

    def test_verifier_policy_pins_exactly_one_broker_profile(self) -> None:
        policy = openclaw_verifier_policy(created_at=WHEN)
        self.assertEqual(policy["policy_version_ref"], VERIFIER_POLICY_REF)
        self.assertEqual(
            policy["filters"]["allowed_profile_ids"],
            [VERIFIER_PROFILE_ID],
        )
        self.assertEqual(
            policy["filters"]["family_independence_capabilities"],
            ["verify", "adjudicate"],
        )
        self.assertIsNone(policy["prior_version_ref"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model-router.sqlite"
            install_openclaw_catalog(path, checked_at=WHEN)
            upgraded = upgrade_openclaw_broker_catalog(path, checked_at=WHEN)
            self.assertEqual(
                upgraded["verifier_policy"]["policy"]["policy_version_ref"],
                VERIFIER_POLICY_REF,
            )
            with ModelRouter(path) as router:
                pinned = router.get_policy(VERIFIER_POLICY_REF)
                self.assertEqual(
                    pinned["filters"]["allowed_profile_ids"],
                    [VERIFIER_PROFILE_ID],
                )
                rerun = upgrade_openclaw_broker_catalog(path, checked_at=WHEN)
                self.assertEqual(rerun["verifier_policy"]["status"], "duplicate")

    def test_assessment_policy_pins_gpt_5_6_sol(self) -> None:
        policy = openclaw_assessment_policy(created_at=WHEN)
        self.assertEqual(policy["policy_version_ref"], ASSESSMENT_POLICY_REF)
        self.assertEqual(
            policy["filters"]["allowed_profile_ids"],
            [ASSESSMENT_PROFILE_ID],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model-router.sqlite"
            install_openclaw_catalog(path, checked_at=WHEN)
            upgraded = upgrade_openclaw_broker_catalog(path, checked_at=WHEN)
            self.assertEqual(
                upgraded["assessment_policy"]["policy"]["policy_version_ref"],
                ASSESSMENT_POLICY_REF,
            )
            with ModelRouter(path) as router:
                self.assertEqual(
                    router.get_policy(ASSESSMENT_POLICY_REF)["filters"][
                        "allowed_profile_ids"
                    ],
                    [ASSESSMENT_PROFILE_ID],
                )
            rerun = upgrade_openclaw_broker_catalog(path, checked_at=WHEN)
            self.assertEqual(rerun["assessment_policy"]["status"], "duplicate")

    def test_live_phase_prices_append_a_new_immutable_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            router_path = root / "model-router.sqlite"
            config_path = root / "openclaw.json"
            config = _config()
            sol = next(
                model
                for model in config["models"]["providers"]["openai"]["models"]
                if model["id"] == "gpt-5.6-sol"
            )
            sol["cost"] = {"input": 4, "output": 20}
            config_path.write_text(json.dumps(config), encoding="utf-8")
            install_openclaw_catalog(router_path, checked_at=WHEN)
            upgrade_openclaw_broker_catalog(router_path, checked_at=WHEN)
            upgrade_openclaw_broker_catalog(
                router_path,
                checked_at=WHEN,
                openclaw_config_path=config_path,
            )
            with ModelRouter(router_path) as router:
                rows = router.connection.execute(
                    "SELECT profile_json FROM model_endpoint_profile_versions "
                    "WHERE profile_id=? ORDER BY version",
                    (ASSESSMENT_PROFILE_ID,),
                ).fetchall()
            self.assertEqual(len(rows), 2)
            latest = json.loads(rows[-1]["profile_json"])
            self.assertEqual(latest["version"], 2)
            self.assertEqual(latest["cost"]["input_per_million_usd"], 4.0)
            self.assertEqual(latest["cost"]["output_per_million_usd"], 20.0)
            rerun = upgrade_openclaw_broker_catalog(
                router_path,
                checked_at=WHEN + timedelta(hours=1),
                openclaw_config_path=config_path,
            )
            self.assertTrue(
                all(row["status"] == "duplicate" for row in rerun["profiles"])
            )


if __name__ == "__main__":
    unittest.main()
