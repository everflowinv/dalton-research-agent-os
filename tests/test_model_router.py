from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.model_router import (
    ModelRouter,
    ModelRouterConflict,
    ModelRouterValidationError,
    RouteTransitionError,
    canonical_hash,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


def profile(
    name: str,
    *,
    version: int = 1,
    prior: str | None = None,
    provider: str = "openai",
    model: str | None = None,
    family: str = "family-alpha",
    cost: float = 1.0,
    slot: str | None = None,
    capabilities: list[str] | None = None,
    state: str = "available",
    valid_until: str = "2026-08-15T12:00:00+00:00",
    max_context: int = 100_000,
) -> dict:
    return {
        "schema_version": "0.1",
        "profile_version_ref": f"model-profile-version:{name}:{version}",
        "id": f"model-profile:{name}",
        "version": version,
        "created_at": "2026-08-14T11:00:00+00:00",
        "prior_version_ref": prior,
        "provider": provider,
        "model": model or f"model-{name}",
        "family": family,
        "adapter_ref": "adapter:openclaw-simple-completion:0.1",
        "credential_slot_ref": slot or f"credential-slot:{provider}:dalton",
        "capabilities": capabilities or ["research", "verify"],
        "modalities": ["text"],
        "context": {
            "max_context_tokens": max_context,
            "max_output_tokens": 8_000,
        },
        "availability": {
            "state": state,
            "checked_at": "2026-08-14T11:00:00+00:00",
            "valid_until": valid_until,
        },
        "cost": {
            "currency": "USD",
            "input_per_million_usd": cost,
            "output_per_million_usd": cost * 2,
        },
        "limits": {
            "max_input_tokens": min(max_context, 80_000),
            "max_output_tokens": 8_000,
            "max_total_tokens": min(max_context + 8_000, 88_000),
            "max_cost_usd": 20.0,
        },
    }


def policy(
    *,
    independence: list[str] | None = None,
    allowed_providers: list[str] | None = None,
) -> dict:
    return {
        "schema_version": "0.1",
        "policy_version_ref": "model-routing-policy-version:default:1",
        "id": "model-routing-policy:default",
        "version": 1,
        "created_at": "2026-08-14T11:00:00+00:00",
        "prior_version_ref": None,
        "filters": {
            "allowed_profile_ids": [],
            "allowed_providers": allowed_providers or [],
            "allowed_families": [],
            "allowed_adapter_refs": ["adapter:openclaw-simple-completion:0.1"],
            "required_modalities": ["text"],
            "family_independence_capabilities": independence or [],
        },
        "ordered_preferences": [
            {"field": "estimated_cost_usd", "direction": "asc"},
            {"field": "profile_version_ref", "direction": "asc"},
        ],
    }


def work_order(
    *,
    work_id: str = "work:model-route-1",
    capabilities: list[str] | None = None,
    budget: dict | None = None,
) -> dict:
    return {
        "schema_version": "0.1",
        "id": work_id,
        "created_at": "2026-08-14T11:30:00+00:00",
        "updated_at": "2026-08-14T11:30:00+00:00",
        "question": "Route this bounded research task",
        "requested_capabilities": capabilities or ["research", "verify"],
        "runtime_profile_ref": "runtime-profile:dalton-native:0.1",
        "budget": budget
        or {
            "max_input_tokens": 2_000,
            "max_output_tokens": 1_000,
            "max_total_tokens": 3_000,
            "max_cost_usd": 1.0,
        },
        "idempotency_key": f"work-key:{work_id}",
        "declared_side_effects": [],
        "status": "ready",
        "input_refs": [],
        "metadata": {},
    }


def route_args(**overrides):
    result = {
        "attempt_number": 1,
        "capability": "research",
        "policy_version_ref": "model-routing-policy-version:default:1",
        "credential_slot_refs": [
            "credential-slot:openai:dalton",
            "credential-slot:deepseek:dalton",
        ],
        "required_modalities": ["text"],
        "required_context_tokens": 1_500,
        "estimated_input_tokens": 1_000,
        "estimated_output_tokens": 500,
        "idempotency_key": "route-key:1",
    }
    result.update(overrides)
    return result


class ModelRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "router.sqlite"
        self.clock = MutableClock()
        self.router = ModelRouter(self.path, clock=self.clock)

    def tearDown(self) -> None:
        self.router.close()
        self.temp.cleanup()

    def seed(self) -> None:
        self.assertEqual(self.router.register_policy(policy())["status"], "fresh")
        self.assertEqual(
            self.router.register_profile(profile("cheap", cost=0.5))["status"],
            "fresh",
        )
        self.assertEqual(
            self.router.register_profile(
                profile(
                    "independent",
                    provider="deepseek",
                    family="family-beta",
                    cost=2.0,
                )
            )["status"],
            "fresh",
        )

    def test_versioned_catalog_and_deterministic_cost_selection(self) -> None:
        self.seed()
        result = self.router.route(work_order(), **route_args())
        self.assertEqual(result["status"], "fresh")
        decision = result["decision"]
        self.assertEqual(decision["outcome"], "selected")
        self.assertEqual(
            decision["selected_profile_version_ref"], "model-profile-version:cheap:1"
        )
        self.assertEqual(
            decision["selected_endpoint"],
            {
                "provider": "openai",
                "model": "model-cheap",
                "family": "family-alpha",
                "adapter_ref": "adapter:openclaw-simple-completion:0.1",
                "credential_slot_ref": "credential-slot:openai:dalton",
            },
        )
        self.assertEqual(
            decision["candidate_snapshot_hash"],
            canonical_hash(decision["candidate_snapshot"]),
        )
        self.assertEqual(len(self.router.list_decisions()), 1)

    def test_profile_and_policy_versions_are_append_only_chains(self) -> None:
        first = profile("cheap")
        self.assertEqual(self.router.register_profile(first)["status"], "fresh")
        self.assertEqual(self.router.register_profile(first)["status"], "duplicate")
        changed_same_ref = profile("cheap", model="different")
        self.assertEqual(
            self.router.register_profile(changed_same_ref)["status"], "conflict"
        )
        second = profile(
            "cheap",
            version=2,
            prior="model-profile-version:cheap:1",
            model="model-cheap-v2",
        )
        self.assertEqual(self.router.register_profile(second)["status"], "fresh")
        bad_third = profile(
            "cheap",
            version=3,
            prior="model-profile-version:cheap:1",
            model="model-cheap-v3",
        )
        with self.assertRaises(ModelRouterConflict):
            self.router.register_profile(bad_third)
        stored = self.router.get_profile("model-profile-version:cheap:2")
        self.assertEqual(stored["prior_version_ref"], "model-profile-version:cheap:1")
        self.assertRegex(stored["content_hash"], r"^[0-9a-f]{64}$")
        first_policy = policy()
        self.assertEqual(self.router.register_policy(first_policy)["status"], "fresh")
        self.assertEqual(self.router.register_policy(first_policy)["status"], "duplicate")
        second_policy = policy()
        second_policy.update(
            {
                "policy_version_ref": "model-routing-policy-version:default:2",
                "version": 2,
                "prior_version_ref": "model-routing-policy-version:default:1",
            }
        )
        second_policy["ordered_preferences"] = [
            {"field": "context.max_context_tokens", "direction": "desc"}
        ]
        self.assertEqual(self.router.register_policy(second_policy)["status"], "fresh")
        self.assertEqual(
            self.router.get_policy("model-routing-policy-version:default:2")[
                "prior_version_ref"
            ],
            "model-routing-policy-version:default:1",
        )

    def test_policy_filter_and_snapshot_use_only_latest_profile_version(self) -> None:
        self.router.register_policy(policy(allowed_providers=["openai"]))
        first = profile("cheap", cost=3.0)
        self.router.register_profile(first)
        self.router.register_profile(
            profile(
                "cheap",
                version=2,
                prior="model-profile-version:cheap:1",
                model="model-cheap-v2",
                cost=0.25,
            )
        )
        self.router.register_profile(
            profile("other", provider="deepseek", family="family-beta", cost=0.01)
        )
        decision = self.router.route(work_order(), **route_args())["decision"]
        refs = [item["profile_version_ref"] for item in decision["candidate_snapshot"]]
        self.assertNotIn("model-profile-version:cheap:1", refs)
        self.assertIn("model-profile-version:cheap:2", refs)
        self.assertEqual(
            decision["selected_profile_version_ref"], "model-profile-version:cheap:2"
        )
        other = next(
            item
            for item in decision["candidate_snapshot"]
            if item["profile_version_ref"] == "model-profile-version:other:1"
        )
        self.assertIn("provider_not_allowed", other["rejection_reasons"])

    def test_verifier_family_independence_is_fail_closed(self) -> None:
        self.router.register_policy(policy(independence=["verify"]))
        self.router.register_profile(profile("same", family="family-alpha", cost=0.1))
        self.router.register_profile(
            profile(
                "independent",
                provider="deepseek",
                family="family-beta",
                cost=1.0,
            )
        )
        missing = self.router.route(
            work_order(),
            **route_args(capability="verify", idempotency_key="route-key:missing"),
        )["decision"]
        self.assertEqual(missing["outcome"], "rejected")
        self.assertIn("producer_family_required", missing["rejection_reasons"])
        # A new WorkOrder is used because the rejected initial decision is still
        # authoritative and may only be followed by explicit switch/retry.
        selected = self.router.route(
            work_order(work_id="work:model-route-verify"),
            **route_args(
                capability="verify",
                producer_family="family-alpha",
                idempotency_key="route-key:verify",
            ),
        )["decision"]
        self.assertEqual(selected["selected_endpoint"]["family"], "family-beta")
        same = next(
            item
            for item in selected["candidate_snapshot"]
            if item["profile_version_ref"] == "model-profile-version:same:1"
        )
        self.assertIn("model_family_not_independent", same["rejection_reasons"])

    def test_budget_context_auth_and_availability_fail_closed(self) -> None:
        self.router.register_policy(policy())
        self.router.register_profile(
            profile(
                "expired",
                valid_until="2026-08-14T11:30:00+00:00",
                max_context=1_200,
            )
        )
        decision = self.router.route(
            work_order(
                budget={
                    "max_input_tokens": 900,
                    "max_output_tokens": 300,
                    "max_total_tokens": 1_200,
                    "max_cost_usd": 0.000001,
                }
            ),
            **route_args(credential_slot_refs=[]),
        )["decision"]
        self.assertEqual(decision["outcome"], "rejected")
        reasons = set(decision["candidate_snapshot"][0]["rejection_reasons"])
        self.assertTrue(
            {
                "availability_expired",
                "credential_slot_unavailable",
                "context_window_insufficient",
                "work_order_budget_input_exceeded",
                "work_order_budget_output_exceeded",
                "work_order_budget_total_exceeded",
                "work_order_cost_budget_exceeded",
            }.issubset(reasons)
        )
        missing_budget = self.router.route(
            work_order(
                work_id="work:model-route-missing-budget",
                budget={"max_input_tokens": 2_000},
            ),
            **route_args(idempotency_key="route-key:missing-budget"),
        )["decision"]
        self.assertEqual(missing_budget["outcome"], "rejected")
        self.assertIn(
            "work_order_budget_missing:max_cost_usd",
            missing_budget["rejection_reasons"],
        )

    def test_switch_and_retry_always_append_new_explicit_decisions(self) -> None:
        self.seed()
        initial = self.router.route(work_order(), **route_args())["decision"]
        with self.assertRaises(RouteTransitionError):
            self.router.route(
                work_order(), **route_args(idempotency_key="route-key:second-initial")
            )
        switched = self.router.switch(
            work_order(),
            **route_args(
                previous_decision_ref=initial["id"],
                idempotency_key="route-key:switch",
            ),
        )["decision"]
        self.assertNotEqual(switched["id"], initial["id"])
        self.assertEqual(switched["decision_kind"], "switch")
        self.assertEqual(
            switched["selected_profile_version_ref"],
            "model-profile-version:independent:1",
        )
        excluded = next(
            item
            for item in switched["candidate_snapshot"]
            if item["profile_version_ref"] == initial["selected_profile_version_ref"]
        )
        self.assertIn("already_tried_in_switch_chain", excluded["rejection_reasons"])
        retried = self.router.retry(
            work_order(),
            **route_args(
                attempt_number=2,
                previous_decision_ref=switched["id"],
                idempotency_key="route-key:retry",
            ),
        )["decision"]
        self.assertEqual(retried["decision_kind"], "retry")
        self.assertEqual(retried["attempt_number"], 2)
        self.assertEqual(len(self.router.list_decisions()), 3)
        with self.assertRaises(RouteTransitionError):
            self.router.switch(
                work_order(),
                **route_args(
                    previous_decision_ref=initial["id"],
                    idempotency_key="route-key:stale",
                ),
            )

    def test_route_idempotency_is_fresh_duplicate_conflict(self) -> None:
        self.seed()
        args = route_args()
        fresh = self.router.route(work_order(), **args)
        duplicate = self.router.route(work_order(), **args)
        self.assertEqual(fresh["status"], "fresh")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(
            duplicate["decision"]["id"], fresh["decision"]["id"]
        )
        conflict = self.router.route(
            work_order(), **route_args(required_context_tokens=1_600)
        )
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(len(self.router.list_decisions()), 1)

    def test_closed_profiles_cannot_store_secret_material(self) -> None:
        unsafe = profile("unsafe")
        unsafe["api_key"] = "must-not-be-stored"
        with self.assertRaises(ModelRouterValidationError):
            self.router.register_profile(unsafe)
        unsafe = profile("unsafe")
        unsafe["credential_slot_ref"] = "sk-not-a-slot"
        with self.assertRaises(ModelRouterValidationError):
            self.router.register_profile(unsafe)

    def test_authoritative_rows_reject_bare_insert_update_and_delete(self) -> None:
        self.seed()
        self.router.route(work_order(), **route_args())
        with self.assertRaises(sqlite3.DatabaseError):
            self.router.conn.execute(
                "UPDATE model_endpoint_profile_versions SET model='tampered' "
                "WHERE profile_version_ref='model-profile-version:cheap:1'"
            )
        with self.assertRaises(sqlite3.DatabaseError):
            self.router.conn.execute(
                "DELETE FROM model_route_decisions WHERE work_order_id='work:model-route-1'"
            )
        with self.assertRaises(sqlite3.DatabaseError):
            self.router.conn.execute(
                "INSERT INTO model_routing_policy_versions "
                "(policy_version_ref, policy_id, version, prior_version_ref, policy_hash, "
                "policy_json, created_at) VALUES (?, ?, ?, NULL, ?, '{}', ?)",
                (
                    "model-routing-policy-version:rogue:1",
                    "model-routing-policy:rogue",
                    1,
                    "0" * 64,
                    "2026-08-14T12:00:00+00:00",
                ),
            )

    def test_contract_schema_files_are_closed_and_loadable(self) -> None:
        contract_root = Path(__file__).parents[1] / "contracts"
        for name in (
            "model-endpoint-profile-version.schema.json",
            "model-routing-policy-version.schema.json",
            "model-route-decision.schema.json",
        ):
            schema = json.loads((contract_root / name).read_text(encoding="utf-8"))
            self.assertEqual(schema["additionalProperties"], False)
            self.assertEqual(schema["properties"]["schema_version"]["const"], "0.1")


if __name__ == "__main__":
    unittest.main()
