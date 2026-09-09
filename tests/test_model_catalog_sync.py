"""P14-M: the catalog is made to agree by appending, never by deleting."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.contracts import WorkOrder
from dalton_core.model_deployment import openclaw_broker_profiles
from dalton_core.model_router import (
    RETIRED_REASON_NOT_IN_BROKER,
    ModelRouter,
    ModelRouterValidationError,
)
from dalton_core.openclaw_catalog_reconcile import (
    broker_catalog_hash,
    catalog_sync_status,
    sync_openclaw_model_catalog,
)
from tests.test_openclaw_catalog_reconcile import _config


NOW = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)


def _drop_broker_profile(config: dict, profile_id: str) -> dict:
    profiles = config["plugins"]["entries"]["dalton-openclaw-model-broker"]["config"][
        "profiles"
    ]
    config["plugins"]["entries"]["dalton-openclaw-model-broker"]["config"]["profiles"] = [
        profile for profile in profiles if profile["id"] != profile_id
    ]
    return config


class CatalogSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "router.sqlite"
        self.router = ModelRouter(self.path)
        self.addCleanup(self.router.close)

    def _install(self, config: dict) -> dict:
        return sync_openclaw_model_catalog(self.router, config, checked_at=NOW)

    def test_missing_profiles_are_added_and_a_second_run_is_a_no_op(self) -> None:
        config = _config()
        first = self._install(config)
        self.assertTrue(first["changed"])
        self.assertEqual(
            sorted(first["added_profile_ids"]),
            sorted(profile["id"] for profile in openclaw_broker_profiles(checked_at=NOW)),
        )
        self.assertTrue(first["catalog_in_sync"])

        second = sync_openclaw_model_catalog(self.router, config, checked_at=LATER)
        self.assertFalse(second["changed"])
        self.assertEqual(second["added_profile_ids"], [])
        self.assertEqual(second["retired_profile_ids_this_run"], [])
        self.assertEqual(second["revived_profile_ids"], [])
        self.assertTrue(second["catalog_in_sync"])

    def test_a_profile_the_broker_dropped_is_retired_not_deleted(self) -> None:
        config = _config()
        self._install(config)
        before = self.router.connection.execute(
            "SELECT COUNT(*) FROM model_endpoint_profile_versions "
            "WHERE profile_id='profile:glm-5-2'"
        ).fetchone()[0]
        original = self.router.latest_profiles()
        original_refs = {profile["profile_version_ref"] for profile in original}

        dropped = _drop_broker_profile(_config(), "profile:glm-5-2")
        report = sync_openclaw_model_catalog(self.router, dropped, checked_at=LATER)

        self.assertEqual(report["retired_profile_ids_this_run"], ["profile:glm-5-2"])
        self.assertTrue(report["catalog_in_sync"])
        after = self.router.connection.execute(
            "SELECT COUNT(*) FROM model_endpoint_profile_versions "
            "WHERE profile_id='profile:glm-5-2'"
        ).fetchone()[0]
        # History intact: a version was appended, none was removed or rewritten.
        self.assertEqual(after, before + 1)
        current = {
            profile["profile_version_ref"]
            for profile in self.router.latest_profiles()
        }
        self.assertTrue(original_refs.issubset(
            {
                row[0]
                for row in self.router.connection.execute(
                    "SELECT profile_version_ref FROM model_endpoint_profile_versions"
                )
            }
        ))
        self.assertNotIn("profile:glm-5-2", report["live_profile_ids"])
        self.assertIn("profile:glm-5-2", report["retired_profile_ids"])
        retired = next(
            profile
            for profile in self.router.latest_profiles()
            if profile["id"] == "profile:glm-5-2"
        )
        self.assertEqual(retired["status"], "retired")
        self.assertEqual(
            retired["retirement"]["reason"], RETIRED_REASON_NOT_IN_BROKER
        )
        self.assertEqual(
            retired["retirement"]["broker_catalog_hash"], broker_catalog_hash(dropped)
        )
        self.assertEqual(retired["retirement"]["retired_at"], retired["created_at"])
        self.assertIn(retired["profile_version_ref"], current)

    def test_old_route_decisions_still_resolve_after_a_retirement(self) -> None:
        config = _config()
        self._install(config)
        policy_ref = "model-routing-policy-version:catalog-sync-test:1"
        self.router.register_policy({
            "schema_version": "0.1",
            "policy_version_ref": policy_ref,
            "id": "model-routing-policy:catalog-sync-test",
            "version": 1,
            "created_at": NOW.isoformat(timespec="microseconds"),
            "prior_version_ref": None,
            "filters": {
                "allowed_profile_ids": ["profile:glm-5-2"],
                "allowed_providers": [],
                "allowed_families": [],
                "allowed_adapter_refs": ["adapter:openclaw-model-broker:0.1"],
                "required_modalities": ["text"],
                "family_independence_capabilities": [],
            },
            "ordered_preferences": [{"field": "estimated_cost_usd", "direction": "asc"}],
        })
        work = _work("work:catalog-sync-retire")
        decision = self.router.route(
            work,
            attempt_number=1,
            capability="research",
            policy_version_ref=policy_ref,
            credential_slot_refs=["credential-slot:openclaw:qwen"],
            required_modalities=["text"],
            required_context_tokens=2_000,
            estimated_input_tokens=1_000,
            estimated_output_tokens=500,
            idempotency_key="catalog-sync-retire:1",
        )["decision"]
        self.assertEqual(decision["outcome"], "selected")
        served_ref = decision["selected_profile_version_ref"]

        sync_openclaw_model_catalog(
            self.router, _drop_broker_profile(_config(), "profile:glm-5-2"),
            checked_at=LATER,
        )

        # The exact version the decision named is still readable, byte for byte.
        replayed = self.router.get_profile(served_ref)
        self.assertEqual(replayed["content_hash"], decision["selected_profile_hash"])
        self.assertNotIn("status", replayed)

        # And nothing routes to it any more.
        refused = self.router.route(
            work,
            attempt_number=2,
            capability="research",
            policy_version_ref=policy_ref,
            credential_slot_refs=["credential-slot:openclaw:qwen"],
            required_modalities=["text"],
            required_context_tokens=2_000,
            estimated_input_tokens=1_000,
            estimated_output_tokens=500,
            idempotency_key="catalog-sync-retire:2",
            decision_kind="retry",
            previous_decision_ref=decision["id"],
        )["decision"]
        self.assertEqual(refused["outcome"], "rejected")
        self.assertIn("profile_retired", refused["rejection_reasons"])

    def test_catalog_in_sync_transitions_both_ways(self) -> None:
        config = _config()
        self.assertFalse(
            catalog_sync_status(self.router, config, checked_at=NOW)["catalog_in_sync"]
        )
        self.assertTrue(self._install(config)["catalog_in_sync"])

        dropped = _drop_broker_profile(_config(), "profile:gpt-5-5")
        status = catalog_sync_status(self.router, dropped, checked_at=LATER)
        self.assertFalse(status["catalog_in_sync"])
        self.assertEqual(status["not_in_broker_profile_ids"], ["profile:gpt-5-5"])
        self.assertEqual(status["missing_static_profile_ids"], [])

        after = sync_openclaw_model_catalog(self.router, dropped, checked_at=LATER)
        self.assertTrue(after["catalog_in_sync"])

    def test_a_retired_profile_the_broker_offers_again_comes_back_live(self) -> None:
        config = _config()
        self._install(config)
        sync_openclaw_model_catalog(
            self.router, _drop_broker_profile(_config(), "profile:gpt-5-5"),
            checked_at=LATER,
        )
        back = sync_openclaw_model_catalog(self.router, _config(), checked_at=LATER)
        self.assertEqual(back["revived_profile_ids"], ["profile:gpt-5-5"])
        self.assertTrue(back["catalog_in_sync"])
        revived = next(
            profile
            for profile in self.router.latest_profiles()
            if profile["id"] == "profile:gpt-5-5"
        )
        self.assertNotIn("status", revived)
        self.assertEqual(
            len(self.router.connection.execute(
                "SELECT 1 FROM model_endpoint_profile_versions "
                "WHERE profile_id='profile:gpt-5-5'"
            ).fetchall()),
            3,
        )

    def test_the_pre_broker_profile_namespace_is_left_alone(self) -> None:
        legacy = dict(openclaw_broker_profiles(checked_at=NOW)[0])
        legacy["id"] = "model-profile:deepseek-v4-flash"
        legacy["profile_version_ref"] = "model-profile-version:legacy-deepseek:1"
        legacy.pop("content_hash", None)
        self.router.register_profile(legacy)
        report = self._install(_config())
        self.assertNotIn("model-profile:deepseek-v4-flash", report["retired_profile_ids"])
        self.assertTrue(report["catalog_in_sync"])

    def test_a_retirement_without_its_evidence_is_refused(self) -> None:
        profile = dict(openclaw_broker_profiles(checked_at=NOW)[0])
        profile.pop("content_hash", None)
        profile["status"] = "retired"
        with self.assertRaisesRegex(ModelRouterValidationError, "retired profile"):
            self.router.register_profile(profile)
        profile["retirement"] = {
            "reason": RETIRED_REASON_NOT_IN_BROKER,
            "retired_at": NOW.isoformat(timespec="microseconds"),
            "broker_catalog_hash": "not-a-digest",
        }
        with self.assertRaisesRegex(ModelRouterValidationError, "SHA-256"):
            self.router.register_profile(profile)

    def test_a_live_profile_may_not_carry_a_retirement_record(self) -> None:
        profile = dict(openclaw_broker_profiles(checked_at=NOW)[0])
        profile.pop("content_hash", None)
        profile["retirement"] = {
            "reason": RETIRED_REASON_NOT_IN_BROKER,
            "retired_at": NOW.isoformat(timespec="microseconds"),
            "broker_catalog_hash": "0" * 64,
        }
        with self.assertRaisesRegex(ModelRouterValidationError, "only a retired profile"):
            self.router.register_profile(profile)


def _work(work_id: str) -> WorkOrder:
    moment = NOW.isoformat(timespec="microseconds")
    return WorkOrder(
        schema_version="0.1",
        id=work_id,
        created_at=moment,
        updated_at=moment,
        question="which model is still offered?",
        requested_capabilities=("research",),
        runtime_profile_ref="runtime-profile:dalton-model-broker:0.1",
        budget={
            "max_input_tokens": 100_000,
            "max_output_tokens": 10_000,
            "max_total_tokens": 110_000,
            "max_cost_usd": 5.0,
            "max_seconds": 120,
        },
        idempotency_key=f"{work_id}:1",
        declared_side_effects=(),
        status="ready",
        input_refs=(),
        metadata={},
    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
