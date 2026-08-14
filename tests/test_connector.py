from __future__ import annotations

import sqlite3
import json
import re
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.connector import (
    ConnectorBlocked,
    ConnectorConflict,
    ConnectorQuotaExceeded,
    ConnectorStore,
    ConnectorValidationError,
    source_envelope_content_hash,
    validate_connector_proposal_manifest,
)
from dalton_core.contracts import ExecutionInvocation, ExecutionKind
from dalton_core.observability import ObservabilityStore
from dalton_core.store import DaltonStore, content_hash


WHEN = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def validate_json_schema(instance, schema: dict, root: dict, path: str = "$") -> None:
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        validate_json_schema(instance, target, root, path)
        return
    if "oneOf" in schema:
        matches = 0
        for choice in schema["oneOf"]:
            try:
                validate_json_schema(instance, choice, root, path)
            except AssertionError:
                continue
            matches += 1
        assert matches == 1, f"{path}: oneOf matched {matches} branches"
        return
    if "const" in schema:
        assert instance == schema["const"], f"{path}: const mismatch"
    if "enum" in schema:
        assert instance in schema["enum"], f"{path}: enum mismatch"
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        checks = {
            "null": lambda value: value is None,
            "object": lambda value: isinstance(value, dict),
            "array": lambda value: isinstance(value, list),
            "string": lambda value: isinstance(value, str),
            "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
            "boolean": lambda value: isinstance(value, bool),
        }
        assert any(checks[name](instance) for name in types), f"{path}: type mismatch"
    if isinstance(instance, dict):
        required = set(schema.get("required", ()))
        assert required <= set(instance), f"{path}: missing {sorted(required - set(instance))}"
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in properties:
                validate_json_schema(value, properties[key], root, f"{path}.{key}")
            elif additional is False:
                raise AssertionError(f"{path}: unexpected key {key}")
            elif isinstance(additional, dict):
                validate_json_schema(value, additional, root, f"{path}.{key}")
    if isinstance(instance, list):
        assert len(instance) >= schema.get("minItems", 0), f"{path}: too few items"
        if schema.get("uniqueItems"):
            encoded = [json.dumps(value, sort_keys=True) for value in instance]
            assert len(encoded) == len(set(encoded)), f"{path}: duplicate items"
        for index, value in enumerate(instance):
            validate_json_schema(value, schema.get("items", {}), root, f"{path}[{index}]")
    if isinstance(instance, str):
        assert len(instance) >= schema.get("minLength", 0), f"{path}: too short"
        if "pattern" in schema:
            assert re.search(schema["pattern"], instance), f"{path}: pattern mismatch"
    if isinstance(instance, int) and not isinstance(instance, bool) and "minimum" in schema:
        assert instance >= schema["minimum"], f"{path}: below minimum"


def assert_wire_schema(test: unittest.TestCase, filename: str, value: dict) -> None:
    schema = json.loads((CONTRACTS / filename).read_text(encoding="utf-8"))
    wire = dict(value)
    wire.pop("write_status", None)
    validate_json_schema(wire, schema, schema)
    test.assertEqual(set(wire), set(schema["required"]))


class MutableClock:
    def __init__(self) -> None:
        self.value = WHEN

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def profile_spec(*, version: int = 1, identifier: str = "connector-profile:a-share:v1") -> dict:
    wire = {
        "schema_version": "0.1",
        "id": identifier,
        "created_at": WHEN.isoformat(),
        "connector_ref": "connector:a-share-announcements",
        "version": version,
        "prior_version_ref": None if version == 1 else "connector-profile:a-share:v1",
        "capability_id": "capability:dalton:connector:a-share-announcements",
        "descriptor_revision_ref": "capability:dalton:connector:a-share-announcements@v1",
        "descriptor_hash": "a" * 64,
        "source_identity": {
            "source_ref": "cninfo", "source_type": "official_filing",
            "source_version": "2026-08-14",
        },
        "source_hash": "",
        "schema_hash": "",
        "catalog_epoch": 1,
        "adapter_ref": "adapter:cninfo:0.1",
        "adapter_hash": "d" * 64,
        "runner_runtime_ref": "runtime:connector-runner:0.1",
        "runner_actor_ref": "runner:connector",
        "runner_environment_hash": "7" * 64,
        "allowed_operations": ["list_announcements"],
        "allowed_hosts": ["www.cninfo.com.cn"],
        "auth_mode": "none",
        "credential_slot_refs": [],
        "input_schema_refs": {"list_announcements": "schema:cninfo:list-input:0.1"},
        "input_schema_hashes": {"list_announcements": "5" * 64},
        "output_schema_refs": {"list_announcements": "schema:cninfo:list-output:0.1"},
        "output_schema_hashes": {"list_announcements": "6" * 64},
        "pagination": {"mode": "page", "cursor_field": "page", "max_pages": 10},
        "completeness": {"list_announcements": "enumerated"},
        "max_response_bytes": 1_000_000,
        "max_records": 1000,
        "access_policy_ref": "policy:access:public",
        "retention_policy_ref": "policy:retention:filing",
        "terms_policy_ref": "policy:terms:cninfo",
        "network_policy": {
            "allowed_schemes": ["https"],
            "allow_redirects": False,
            "max_redirects": 0,
            "resolve_public_only": True,
        },
    }
    wire["source_hash"] = content_hash(wire["source_identity"])
    wire["schema_hash"] = content_hash(
        {
            "allowed_operations": wire["allowed_operations"],
            "input_schema_refs": wire["input_schema_refs"],
            "input_schema_hashes": wire["input_schema_hashes"],
            "output_schema_refs": wire["output_schema_refs"],
            "output_schema_hashes": wire["output_schema_hashes"],
        }
    )
    return wire


def rate_policy_spec(
    profile_ref: str,
    *,
    version: int = 1,
    identifier: str = "connector-rate-policy:a-share:v1",
    prior_version_ref: str | None = None,
    effective_from: datetime = WHEN,
    effective_until: datetime | None = None,
    price_rate_refs: tuple[str, ...] = (),
    required_price_meters: tuple[str, ...] = (),
    limits: dict | None = None,
) -> dict:
    price_book = {
        "price_rate_refs": sorted(price_rate_refs),
        "required_price_meters": sorted(required_price_meters),
    }
    return {
        "schema_version": "0.1", "id": identifier, "created_at": WHEN.isoformat(),
        "policy_ref": "connector-rate-policy:a-share",
        "quota_scope_ref": "connector-quota-scope:cninfo:public",
        "version": version, "prior_version_ref": prior_version_ref,
        "connector_profile_ref": profile_ref, "window_seconds": 60,
        "reset_timezone": "UTC", "max_concurrency": 1,
        "quota_currency": "USD",
        **price_book,
        "price_book_hash": content_hash(price_book),
        "limits": limits or {
            "calls": 2, "bytes": 10_000, "records": 100, "cost_micros": 1000
        },
        "effective_from": effective_from.isoformat(),
        "effective_until": None if effective_until is None else effective_until.isoformat(),
        "actor_ref": "human:governance",
    }


def price_rate_spec(
    profile_ref: str,
    *,
    identifier: str,
    rate_ref: str,
    meter: str,
    unit_quantity: int,
    unit_price_micros: int,
    currency: str = "USD",
    effective_from: datetime = WHEN - timedelta(days=1),
    effective_until: datetime | None = None,
) -> dict:
    return {
        "schema_version": "0.1",
        "id": identifier,
        "created_at": WHEN.isoformat(),
        "price_rate_ref": rate_ref,
        "version": 1,
        "prior_version_ref": None,
        "connector_profile_ref": profile_ref,
        "meter": meter,
        "unit_quantity": unit_quantity,
        "unit_price_micros": unit_price_micros,
        "rounding_mode": "ceiling",
        "currency": currency,
        "effective_from": effective_from.isoformat(),
        "effective_until": (
            None if effective_until is None else effective_until.isoformat()
        ),
        "source_ref": f"pricing:{rate_ref}",
        "actor_ref": "human:governance",
    }


def proposal_manifest_spec() -> dict:
    wire = {
        "schema_version": "0.1", "id": "connector-manifest:cninfo:v1",
        "created_at": WHEN.isoformat(timespec="microseconds"),
        "capability_proposal_ref": "proposal:cninfo:v1",
        "connector_ref": "connector:a-share-announcements",
        "source_identity": {
            "source": "cninfo", "adapter": "cninfo-http",
            "source_version": "2026-08-14", "adapter_version": "0.1",
        },
        "adapter_package_ref": "artifact:adapter:cninfo:v1", "adapter_source_hash": "a" * 64,
        "profile_template_ref": "artifact:profile:cninfo:v1", "profile_template_hash": "b" * 64,
        "operations": [
            {
                "operation": "list_announcements",
                "input_schema_ref": "schema:cninfo:input:v1", "input_schema_hash": "c" * 64,
                "output_schema_ref": "schema:cninfo:output:v1", "output_schema_hash": "d" * 64,
                "completeness": "enumerated", "pagination": "page",
                "side_effects": ["network:read"],
            }
        ],
        "fixture_manifest_ref": "artifact:fixtures:cninfo:v1", "fixture_manifest_hash": "e" * 64,
        "offline_attestation_policy_ref": "policy:offline-connector:v1",
        "requested_canary": {
            "allowed_hosts": ["www.cninfo.com.cn"], "credential_slot_refs": [],
            "max_calls": 10, "max_bytes": 1_000_000, "max_records": 1000,
            "max_cost_micros": 0,
            "expires_at": (WHEN + timedelta(hours=1)).isoformat(timespec="microseconds"),
        },
        "promotion_policy_ref": "policy:connector-promotion:v1", "builder_ref": "builder:dalton",
    }
    return {**wire, "content_hash": content_hash(wire)}


class ConnectorStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.core = DaltonStore(":memory:")
        self.connectors = ConnectorStore(self.core, clock=self.clock)
        self.obs = ObservabilityStore(self.core)
        self.addCleanup(self.core.close)
        self.profile = self.connectors.register_profile(
            profile_spec(), idempotency_key="profile:v1"
        )
        parameters = {"stock_code": "600309", "page": 1}
        self.call = self.connectors.register_call_spec(
            {
                "schema_version": "0.1",
                "id": "connector-call:a-share:1",
                "created_at": WHEN.isoformat(),
                "work_order_ref": "work:connector:1",
                "work_order_hash": "e" * 64,
                "connector_profile_ref": self.profile["id"],
                "operation": "list_announcements",
                "parameters": parameters,
                "query_hash": content_hash(
                    {"operation": "list_announcements", "parameters": parameters}
                ),
            },
            idempotency_key="call:1",
        )
        execution = ExecutionInvocation(
            schema_version="0.1",
            id="connector-invocation:a-share:1",
            created_at=WHEN.isoformat(),
            kind=ExecutionKind.CONNECTOR,
            work_order_ref="work:connector:1",
            profile_ref=self.profile["id"],
            capability=self.profile["capability_id"],
            input_refs=(self.call["id"],),
            output_refs=("artifact:cninfo:raw:1",),
            started_at=WHEN.isoformat(),
            completed_at=None,
            side_effects=(),
            runtime_ref="runtime:connector-runner:0.1",
            actor_ref="runner:connector",
            environment_hash=self.profile["runner_environment_hash"],
        )
        self.invocation = self.connectors.register_invocation(
            {
                "schema_version": "0.1",
                "id": execution.id,
                "created_at": WHEN.isoformat(),
                "work_order_ref": "work:connector:1",
                "work_order_hash": "e" * 64,
                "connector_profile_ref": self.profile["id"],
                "connector_profile_hash": self.profile["content_hash"],
                "call_spec_ref": self.call["id"],
                "call_spec_hash": self.call["content_hash"],
                "capability_lease_ref": "lease:connector:1",
                "capability_lease_hash": "f" * 64,
                "descriptor_revision_ref": self.profile["descriptor_revision_ref"],
                "catalog_epoch": 1,
                "logical_invocation_key": "connector-logical:" + content_hash(
                    {
                        "work_order_ref": "work:connector:1",
                        "work_order_hash": "e" * 64,
                        "connector_profile_hash": self.profile["content_hash"],
                        "call_spec_hash": self.call["content_hash"],
                    }
                ),
            },
            execution=execution,
            idempotency_key="invocation:1",
        )
        self.zero_rate = self.connectors.register_price_rate(
            price_rate_spec(
                self.profile["id"],
                identifier="connector-price:a-share:calls:zero:v1",
                rate_ref="connector-price:a-share:calls:zero",
                meter="calls",
                unit_quantity=1,
                unit_price_micros=0,
            ),
            idempotency_key="price:calls:zero:v1",
        )
        self.policy = self.connectors.register_rate_policy(
            rate_policy_spec(
                self.profile["id"],
                price_rate_refs=(self.zero_rate["id"],),
                required_price_meters=("calls",),
            ),
            idempotency_key="rate-policy:1",
        )
        assert_wire_schema(self, "connector-profile-version.schema.json", self.profile)
        assert_wire_schema(self, "connector-call-spec.schema.json", self.call)
        assert_wire_schema(self, "connector-invocation.schema.json", self.invocation)
        assert_wire_schema(self, "connector-rate-policy-version.schema.json", self.policy)
        activation = json.loads(
            self.connectors.conn.execute(
                "SELECT record_json FROM connector_rate_policy_activation_events "
                "WHERE policy_version_ref=?",
                (self.policy["id"],),
            ).fetchone()["record_json"]
        )
        assert_wire_schema(
            self, "connector-rate-policy-activation-event.schema.json", activation
        )

    def reserve(self, attempt: int, key: str) -> dict:
        return self.connectors.reserve_quota(
            self.invocation["id"], self.policy["id"], attempt,
            {"calls": 1, "bytes": 1000, "records": 10, "cost_micros": 100},
            ttl_seconds=30, idempotency_key=key,
        )

    def terminal_times(self, seconds: int = 1) -> tuple[str, str]:
        started_at = self.clock.value
        self.clock.advance(seconds)
        return started_at.isoformat(), self.clock.value.isoformat()

    def record_attempt_usage(
        self, reservation: dict, attempt_number: int, metrics: dict, prefix: str
    ) -> tuple[dict, dict, dict]:
        started_at, completed_at = self.terminal_times()
        attempt = self.connectors.record_physical_attempt(
            self.invocation["id"], reservation["id"], attempt_number, "succeeded",
            started_at=started_at, completed_at=completed_at,
            provider_request_id=f"provider:{prefix}", idempotency_key=f"attempt:{prefix}",
        )
        usage = self.connectors.record_usage(
            attempt["id"], metrics, measurement_status="final",
            metering_source="provider_reported", provider_usage_ref=f"usage:{prefix}",
            idempotency_key=f"usage:{prefix}",
        )
        cost = self.connectors.record_cost(
            usage["id"], price_rate_refs=[self.zero_rate["id"]], amount_micros=0,
            currency="USD", cost_status="actual",
            calculation_ref="calculator:connector:test", actor_ref="system:cost",
            idempotency_key=f"cost:{prefix}",
        )
        return attempt, usage, cost

    def test_profile_call_invocation_are_closed_versioned_and_idempotent(self) -> None:
        duplicate = self.connectors.register_profile(
            profile_spec(), idempotency_key="profile:v1"
        )
        self.assertEqual(duplicate["write_status"], "duplicate")
        with self.assertRaises(ConnectorConflict):
            self.connectors.register_profile(
                {**profile_spec(), "max_records": 999}, idempotency_key="profile:v1"
            )
        with self.assertRaises(ConnectorValidationError):
            self.connectors.register_profile(
                {**profile_spec(), "secret": "must-not-enter-authority"},
                idempotency_key="profile:bad",
            )
        with self.assertRaises(ConnectorValidationError):
            self.connectors.register_profile(
                {**profile_spec(), "source_hash": "0" * 64},
                idempotency_key="profile:source-hash-mismatch",
            )
        with self.assertRaises(ConnectorValidationError):
            self.connectors.register_profile(
                {**profile_spec(), "schema_hash": "0" * 64},
                idempotency_key="profile:schema-hash-mismatch",
            )
        credential_parameters = {"filters": {"api-key": "must-not-enter-call-spec"}}
        with self.assertRaises(ConnectorValidationError):
            self.connectors.register_call_spec(
                {
                    "schema_version": "0.1", "id": "connector-call:sensitive",
                    "created_at": WHEN.isoformat(), "work_order_ref": "work:connector:1",
                    "work_order_hash": "e" * 64, "connector_profile_ref": self.profile["id"],
                    "operation": "list_announcements", "parameters": credential_parameters,
                    "query_hash": content_hash(
                        {"operation": "list_announcements", "parameters": credential_parameters}
                    ),
                },
                idempotency_key="call:sensitive",
            )
        v2 = self.connectors.register_profile(
            profile_spec(version=2, identifier="connector-profile:a-share:v2"),
            idempotency_key="profile:v2",
        )
        self.assertEqual(v2["prior_version_ref"], self.profile["id"])
        execution = self.core.conn.execute(
            "SELECT kind FROM execution_invocations WHERE execution_id=?",
            (self.invocation["id"],),
        ).fetchone()
        self.assertEqual(execution["kind"], "connector")
        self.assertEqual(self.connectors.get_call_spec(self.call["id"])["query_hash"], self.call["query_hash"])
        invocation_spec = {
            key: value
            for key, value in self.invocation.items()
            if key not in {"write_status", "execution_ref", "execution_hash", "content_hash"}
        }
        stored_execution = ExecutionInvocation.from_dict(
            json.loads(
                self.core.conn.execute(
                    "SELECT execution_json FROM execution_invocations WHERE execution_id=?",
                    (self.invocation["id"],),
                ).fetchone()["execution_json"]
            )
        )
        with self.assertRaises(ConnectorConflict):
            self.connectors.register_invocation(
                invocation_spec,
                execution=replace(stored_execution, side_effects=("network:unfrozen",)),
                idempotency_key="invocation:unequal-execution",
            )
        for environment_hash, key in ((None, "missing"), ("0" * 64, "wrong")):
            with self.subTest(environment_hash=key), self.assertRaises(ConnectorConflict):
                self.connectors.register_invocation(
                    invocation_spec,
                    execution=replace(stored_execution, environment_hash=environment_hash),
                    idempotency_key=f"invocation:environment:{key}",
                )
        for table in (
            "connector_profile_versions", "connector_call_specs", "connector_invocations",
            "connector_rate_policy_versions",
        ):
            with self.subTest(table=table), self.assertRaises(sqlite3.DatabaseError):
                self.connectors.conn.execute(f"DELETE FROM {table}")

    def test_self_generated_manifest_is_closed_public_unique_and_unexpired(self) -> None:
        manifest = validate_connector_proposal_manifest(
            proposal_manifest_spec(), now=WHEN
        )
        assert_wire_schema(self, "connector-proposal-manifest.schema.json", manifest)
        duplicate_operation = proposal_manifest_spec()
        duplicate_operation["operations"] = duplicate_operation["operations"] * 2
        duplicate_operation["content_hash"] = content_hash(
            {key: value for key, value in duplicate_operation.items() if key != "content_hash"}
        )
        with self.assertRaises(ConnectorValidationError):
            validate_connector_proposal_manifest(duplicate_operation, now=WHEN)
        private_host = proposal_manifest_spec()
        private_host["requested_canary"]["allowed_hosts"] = ["127.0.0.1"]
        private_host["content_hash"] = content_hash(
            {key: value for key, value in private_host.items() if key != "content_hash"}
        )
        with self.assertRaises(ConnectorValidationError):
            validate_connector_proposal_manifest(private_host, now=WHEN)
        with self.assertRaises(ConnectorValidationError):
            validate_connector_proposal_manifest(
                proposal_manifest_spec(), now=WHEN + timedelta(hours=2)
            )

    def test_quota_reservation_is_serialized_bounded_and_releaseable(self) -> None:
        first = self.reserve(1, "reserve:1")
        assert_wire_schema(self, "connector-quota-reservation.schema.json", first)
        self.assertEqual(self.reserve(1, "reserve:1")["write_status"], "duplicate")
        with self.assertRaises(ConnectorQuotaExceeded):
            self.reserve(2, "reserve:2:blocked-concurrency")
        released = self.connectors.settle_quota(
            first["id"], "released",
            {"calls": 0, "bytes": 0, "records": 0, "cost_micros": 0},
            idempotency_key="settle:release:1",
        )
        self.assertEqual(released["state"], "released")
        second = self.reserve(2, "reserve:2")
        self.assertEqual(second["physical_attempt_number"], 2)

    def test_release_cannot_hide_or_precede_a_physical_attempt(self) -> None:
        first = self.reserve(1, "release-guard:reserve:1")
        _, usage1, cost1 = self.record_attempt_usage(
            first, 1, {"calls": 1, "bytes": 100, "records": 1, "cost_micros": 0},
            "release-guard:1",
        )
        with self.assertRaises(ConnectorConflict):
            self.connectors.settle_quota(
                first["id"], "released",
                {"calls": 0, "bytes": 0, "records": 0, "cost_micros": 0},
                idempotency_key="release-guard:settle:invalid",
            )
        self.connectors.settle_quota(
            first["id"], "consumed", usage1["metrics"], usage_entry_ref=usage1["id"],
            cost_entry_ref=cost1["id"],
            idempotency_key="release-guard:settle:1",
        )
        second = self.reserve(2, "release-guard:reserve:2")
        self.connectors.settle_quota(
            second["id"], "released",
            {"calls": 0, "bytes": 0, "records": 0, "cost_micros": 0},
            idempotency_key="release-guard:settle:2",
        )
        with self.assertRaises(ConnectorConflict):
            self.connectors.record_physical_attempt(
                self.invocation["id"], second["id"], 2, "succeeded",
                started_at=self.clock.value.isoformat(),
                completed_at=self.clock.value.isoformat(),
                idempotency_key="release-guard:attempt:2",
            )
        third = self.reserve(3, "release-guard:reserve:3")
        _, usage3, cost3 = self.record_attempt_usage(
            third, 3, {"calls": 1, "bytes": 100, "records": 1, "cost_micros": 0},
            "release-guard:3",
        )
        self.connectors.settle_quota(
            third["id"], "consumed", usage3["metrics"], usage_entry_ref=usage3["id"],
            cost_entry_ref=cost3["id"],
            idempotency_key="release-guard:settle:3",
        )
        with self.assertRaises(ConnectorQuotaExceeded):
            self.reserve(4, "release-guard:reserve:4")

    def test_physical_attempt_must_fit_reservation_time_boundary(self) -> None:
        reservation = self.reserve(1, "attempt-time:reserve")
        with self.assertRaises(ConnectorConflict):
            self.connectors.record_physical_attempt(
                self.invocation["id"], reservation["id"], 1, "failed",
                started_at=(WHEN - timedelta(seconds=1)).isoformat(),
                completed_at=WHEN.isoformat(), idempotency_key="attempt-time:before",
            )
        with self.assertRaises(ConnectorConflict):
            self.connectors.record_physical_attempt(
                self.invocation["id"], reservation["id"], 1, "failed",
                started_at=(WHEN + timedelta(seconds=31)).isoformat(),
                completed_at=(WHEN + timedelta(seconds=32)).isoformat(),
                idempotency_key="attempt-time:expired",
            )
        with self.assertRaises(ConnectorValidationError):
            self.connectors.record_physical_attempt(
                self.invocation["id"], reservation["id"], 1, "failed",
                started_at=WHEN.isoformat(), completed_at=None,
                idempotency_key="attempt-time:no-completion",
            )
        with self.assertRaises(ConnectorValidationError):
            self.connectors.record_physical_attempt(
                self.invocation["id"], reservation["id"], 1, "rate_limited",
                started_at=WHEN.isoformat(), completed_at=(WHEN + timedelta(seconds=1)).isoformat(),
                retry_at=(WHEN + timedelta(seconds=1)).isoformat(),
                idempotency_key="attempt-time:bad-retry",
            )
        with self.assertRaises(ConnectorConflict):
            self.connectors.record_physical_attempt(
                self.invocation["id"], reservation["id"], 1, "failed",
                started_at=(WHEN + timedelta(seconds=1)).isoformat(),
                completed_at=(WHEN + timedelta(seconds=1)).isoformat(),
                idempotency_key="attempt-time:future-start",
            )
        with self.assertRaises(ConnectorConflict):
            self.connectors.record_physical_attempt(
                self.invocation["id"], reservation["id"], 1, "failed",
                started_at=WHEN.isoformat(),
                completed_at=(WHEN + timedelta(seconds=1)).isoformat(),
                idempotency_key="attempt-time:future-completion",
            )
        self.clock.advance(1)
        saved = self.connectors.record_physical_attempt(
            self.invocation["id"], reservation["id"], 1, "failed",
            started_at=WHEN.isoformat(), completed_at=self.clock.value.isoformat(),
            idempotency_key="attempt-time:observed",
        )
        self.assertLessEqual(saved["completed_at"], saved["created_at"])

    def test_policy_versions_share_scope_and_only_exact_active_version_admits(self) -> None:
        first = self.reserve(1, "policy-scope:reserve:v1")
        policy_v2 = self.connectors.register_rate_policy(
            rate_policy_spec(
                self.profile["id"], version=2,
                identifier="connector-rate-policy:a-share:v2",
                prior_version_ref=self.policy["id"], effective_from=WHEN,
                price_rate_refs=(self.zero_rate["id"],),
                required_price_meters=("calls",),
            ),
            idempotency_key="policy-scope:v2",
        )
        with self.assertRaises(ConnectorQuotaExceeded):
            self.connectors.reserve_quota(
                self.invocation["id"], policy_v2["id"], 2,
                {"calls": 1, "bytes": 1000, "records": 10, "cost_micros": 100},
                ttl_seconds=30, idempotency_key="policy-scope:reserve:v2:blocked",
            )
        self.connectors.settle_quota(
            first["id"], "released",
            {"calls": 0, "bytes": 0, "records": 0, "cost_micros": 0},
            idempotency_key="policy-scope:release:v1",
        )
        self.assertEqual(
            self.connectors.reserve_quota(
                self.invocation["id"], policy_v2["id"], 2,
                {"calls": 1, "bytes": 1000, "records": 10, "cost_micros": 100},
                ttl_seconds=30, idempotency_key="policy-scope:reserve:v2",
            )["policy_version_ref"],
            policy_v2["id"],
        )
        with self.assertRaises(ConnectorBlocked):
            self.connectors.reserve_quota(
                self.invocation["id"], self.policy["id"], 3,
                {"calls": 1, "bytes": 100, "records": 1, "cost_micros": 1},
                ttl_seconds=30, idempotency_key="policy-scope:reserve:stale",
            )

    def test_future_and_expired_rate_policy_fail_closed(self) -> None:
        policy_v2 = self.connectors.register_rate_policy(
            rate_policy_spec(
                self.profile["id"], version=2,
                identifier="connector-rate-policy:a-share:future:v2",
                prior_version_ref=self.policy["id"],
                effective_from=WHEN + timedelta(seconds=30),
                effective_until=WHEN + timedelta(seconds=40),
                price_rate_refs=(self.zero_rate["id"],),
                required_price_meters=("calls",),
            ),
            idempotency_key="policy-time:v2",
        )
        with self.assertRaises(ConnectorBlocked):
            self.connectors.reserve_quota(
                self.invocation["id"], policy_v2["id"], 1,
                {"calls": 1, "bytes": 1, "records": 0, "cost_micros": 0},
                ttl_seconds=10, idempotency_key="policy-time:future",
            )
        current = self.reserve(1, "policy-time:v1")
        self.connectors.settle_quota(
            current["id"], "released",
            {"calls": 0, "bytes": 0, "records": 0, "cost_micros": 0},
            idempotency_key="policy-time:v1:release",
        )
        self.clock.advance(31)
        self.assertEqual(
            self.connectors.reserve_quota(
                self.invocation["id"], policy_v2["id"], 2,
                {"calls": 1, "bytes": 1, "records": 0, "cost_micros": 0},
                ttl_seconds=5, idempotency_key="policy-time:v2:active",
            )["policy_version_ref"],
            policy_v2["id"],
        )
        self.clock.advance(10)
        with self.assertRaises(ConnectorBlocked):
            self.connectors.reserve_quota(
                self.invocation["id"], policy_v2["id"], 3,
                {"calls": 1, "bytes": 1, "records": 0, "cost_micros": 0},
                ttl_seconds=5, idempotency_key="policy-time:v2:expired",
            )

    def test_two_sqlite_connections_serialize_quota_admission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "connector-core.sqlite"
            destination = sqlite3.connect(path)
            self.core.conn.backup(destination)
            destination.close()
            barrier = threading.Barrier(2)
            outcomes: list[str] = []
            outcome_lock = threading.Lock()

            def worker(attempt_number: int) -> None:
                core = DaltonStore(path)
                authority = ConnectorStore(core, clock=self.clock)
                try:
                    barrier.wait(timeout=5)
                    authority.reserve_quota(
                        self.invocation["id"], self.policy["id"], attempt_number,
                        {"calls": 1, "bytes": 1, "records": 0, "cost_micros": 0},
                        ttl_seconds=10,
                        idempotency_key=f"concurrent:reserve:{attempt_number}",
                    )
                except ConnectorQuotaExceeded:
                    result = "quota_exceeded"
                else:
                    result = "admitted"
                finally:
                    core.close()
                with outcome_lock:
                    outcomes.append(result)

            threads = [threading.Thread(target=worker, args=(number,)) for number in (1, 2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(sorted(outcomes), ["admitted", "quota_exceeded"])

    def test_settlement_is_exact_attempt_latest_usage_and_actual(self) -> None:
        first = self.reserve(1, "settlement-exact:reserve:1")
        _, usage1, cost1 = self.record_attempt_usage(
            first, 1, {"calls": 1, "bytes": 100, "records": 1, "cost_micros": 0},
            "settlement-exact:1",
        )
        self.connectors.settle_quota(
            first["id"], "consumed", usage1["metrics"], usage_entry_ref=usage1["id"],
            cost_entry_ref=cost1["id"],
            idempotency_key="settlement-exact:settle:1",
        )
        second = self.reserve(2, "settlement-exact:reserve:2")
        attempt2, usage2, cost2 = self.record_attempt_usage(
            second, 2, {"calls": 1, "bytes": 110, "records": 2, "cost_micros": 0},
            "settlement-exact:2",
        )
        with self.assertRaises(ConnectorConflict):
            self.connectors.settle_quota(
                second["id"], "consumed", usage1["metrics"], usage_entry_ref=usage1["id"],
                cost_entry_ref=cost1["id"],
                idempotency_key="settlement-exact:cross-attempt",
            )
        with self.assertRaises(ConnectorConflict):
            self.connectors.settle_quota(
                second["id"], "consumed",
                {"calls": 1, "bytes": 0, "records": 0, "cost_micros": 0},
                usage_entry_ref=usage2["id"], idempotency_key="settlement-exact:false-actual",
                cost_entry_ref=cost2["id"],
            )
        settlement2 = self.connectors.settle_quota(
            second["id"], "consumed", usage2["metrics"], usage_entry_ref=usage2["id"],
            cost_entry_ref=cost2["id"],
            idempotency_key="settlement-exact:settle:2",
        )
        usage2_v2 = self.connectors.record_usage(
            attempt2["id"], {"calls": 1, "bytes": 120, "records": 2, "cost_micros": 0},
            measurement_status="final", metering_source="provider_reported",
            provider_usage_ref="usage:settlement-exact:2:v2",
            correction_of_ref=usage2["id"], idempotency_key="usage:settlement-exact:2:v2",
        )
        with self.assertRaises(ConnectorConflict):
            self.connectors.settle_quota(
                second["id"], "consumed", usage2["metrics"], usage_entry_ref=usage2["id"],
                cost_entry_ref=cost2["id"],
                correction_of_ref=settlement2["id"],
                idempotency_key="settlement-exact:stale-usage",
            )
        cost2_v2 = self.connectors.record_cost(
            usage2_v2["id"], price_rate_refs=[self.zero_rate["id"]], amount_micros=0,
            currency="USD", cost_status="actual",
            calculation_ref="calculator:connector:test", actor_ref="system:cost",
            idempotency_key="cost:settlement-exact:2:v2",
        )
        corrected = self.connectors.settle_quota(
            second["id"], "consumed", usage2_v2["metrics"],
            usage_entry_ref=usage2_v2["id"], cost_entry_ref=cost2_v2["id"],
            correction_of_ref=settlement2["id"],
            idempotency_key="settlement-exact:corrected",
        )
        self.assertEqual(corrected["revision"], 2)

    def test_quota_overage_atomically_opens_blocking_incident(self) -> None:
        reservation = self.reserve(1, "overage:reserve")
        _, usage, cost = self.record_attempt_usage(
            reservation, 1,
            {"calls": 1, "bytes": 9500, "records": 1, "cost_micros": 0},
            "overage",
        )
        self.connectors.settle_quota(
            reservation["id"], "consumed", usage["metrics"], usage_entry_ref=usage["id"],
            cost_entry_ref=cost["id"],
            idempotency_key="overage:settle",
        )
        incidents = self.connectors.conn.execute(
            "SELECT count(*) AS n FROM connector_incidents WHERE incident_type='quota_drift' "
            "AND severity='blocking'"
        ).fetchone()
        self.assertEqual(incidents["n"], 1)
        with self.assertRaises(ConnectorBlocked):
            self.reserve(2, "overage:reserve:blocked")

    def test_nonfinal_usage_can_only_settle_indeterminate(self) -> None:
        reservation = self.reserve(1, "nonfinal:reserve")
        started_at, completed_at = self.terminal_times()
        attempt = self.connectors.record_physical_attempt(
            self.invocation["id"], reservation["id"], 1, "indeterminate",
            started_at=started_at, completed_at=completed_at,
            idempotency_key="nonfinal:attempt",
        )
        usage = self.connectors.record_usage(
            attempt["id"], {"calls": 1, "bytes": 0, "records": 0, "cost_micros": 0},
            measurement_status="unavailable", metering_source="runner_measured",
            idempotency_key="nonfinal:usage",
        )
        cost = self.connectors.record_cost(
            usage["id"], price_rate_refs=[self.zero_rate["id"]], amount_micros=0,
            currency="USD", cost_status="actual", calculation_ref="calculator:unavailable",
            actor_ref="system:cost", idempotency_key="nonfinal:cost",
        )
        with self.assertRaises(ConnectorConflict):
            self.connectors.settle_quota(
                reservation["id"], "consumed", usage["metrics"],
                usage_entry_ref=usage["id"], cost_entry_ref=cost["id"],
                idempotency_key="nonfinal:consumed",
            )
        self.connectors.settle_quota(
            reservation["id"], "indeterminate", usage["metrics"],
            usage_entry_ref=usage["id"], cost_entry_ref=cost["id"],
            idempotency_key="nonfinal:indeterminate",
        )
        with self.assertRaises(ConnectorQuotaExceeded):
            self.connectors.reserve_quota(
                self.invocation["id"], self.policy["id"], 2,
                {"calls": 1, "bytes": 9501, "records": 0, "cost_micros": 0},
                ttl_seconds=10, idempotency_key="nonfinal:conservative-projection",
            )

    def test_usage_correction_after_settlement_blocks_new_admission(self) -> None:
        reservation = self.reserve(1, "usage-drift:reserve")
        attempt, usage, cost = self.record_attempt_usage(
            reservation, 1,
            {"calls": 1, "bytes": 100, "records": 1, "cost_micros": 0},
            "usage-drift",
        )
        self.connectors.settle_quota(
            reservation["id"], "consumed", usage["metrics"],
            usage_entry_ref=usage["id"], cost_entry_ref=cost["id"],
            idempotency_key="usage-drift:settle",
        )
        self.connectors.record_usage(
            attempt["id"], {"calls": 1, "bytes": 9500, "records": 1, "cost_micros": 0},
            measurement_status="final", metering_source="provider_reported",
            correction_of_ref=usage["id"], idempotency_key="usage-drift:correction",
        )
        with self.assertRaises(ConnectorBlocked):
            self.reserve(2, "usage-drift:blocked")
        incident = self.connectors.conn.execute(
            "SELECT count(*) AS n FROM connector_incidents WHERE incident_type='quota_drift' "
            "AND severity='blocking'"
        ).fetchone()
        self.assertEqual(incident["n"], 1)

    def test_cross_window_usage_correction_opens_incident_at_write_time(self) -> None:
        reservation = self.reserve(1, "usage-cross-window:reserve")
        attempt, usage, cost = self.record_attempt_usage(
            reservation, 1,
            {"calls": 1, "bytes": 100, "records": 1, "cost_micros": 0},
            "usage-cross-window",
        )
        self.connectors.settle_quota(
            reservation["id"], "consumed", usage["metrics"],
            usage_entry_ref=usage["id"], cost_entry_ref=cost["id"],
            idempotency_key="usage-cross-window:settle",
        )
        self.clock.advance(61)
        self.connectors.record_usage(
            attempt["id"], {"calls": 1, "bytes": 9500, "records": 1, "cost_micros": 0},
            measurement_status="final", metering_source="provider_reported",
            correction_of_ref=usage["id"], idempotency_key="usage-cross-window:correction",
        )
        incident = self.connectors.conn.execute(
            "SELECT count(*) AS n FROM connector_incidents WHERE incident_type='quota_drift' "
            "AND severity='blocking'"
        ).fetchone()
        self.assertEqual(incident["n"], 1)
        with self.assertRaises(ConnectorBlocked):
            self.reserve(2, "usage-cross-window:blocked")

    def test_cross_window_cost_correction_opens_incident_at_write_time(self) -> None:
        reservation = self.reserve(1, "cost-cross-window:reserve")
        _, usage, cost = self.record_attempt_usage(
            reservation, 1,
            {"calls": 1, "bytes": 100, "records": 1, "cost_micros": 0},
            "cost-cross-window",
        )
        self.connectors.settle_quota(
            reservation["id"], "consumed", usage["metrics"],
            usage_entry_ref=usage["id"], cost_entry_ref=cost["id"],
            idempotency_key="cost-cross-window:settle",
        )
        self.clock.advance(61)
        self.connectors.record_cost(
            usage["id"], price_rate_refs=[self.zero_rate["id"]], amount_micros=0,
            currency="USD", cost_status="actual",
            calculation_ref="calculator:connector:correction",
            actor_ref="system:cost", correction_of_ref=cost["id"],
            idempotency_key="cost-cross-window:correction",
        )
        incident = self.connectors.conn.execute(
            "SELECT count(*) AS n FROM connector_incidents WHERE incident_type='quota_drift' "
            "AND severity='blocking'"
        ).fetchone()
        self.assertEqual(incident["n"], 1)
        with self.assertRaises(ConnectorBlocked):
            self.reserve(2, "cost-cross-window:blocked")

    def test_rate_policy_requires_complete_canonical_price_book(self) -> None:
        profile_v2 = self.connectors.register_profile(
            profile_spec(version=2, identifier="connector-profile:a-share:v2"),
            idempotency_key="price-book-complete:profile:v2",
        )
        zero_bytes = self.connectors.register_price_rate(
            price_rate_spec(
                profile_v2["id"], identifier="connector-price:v2:bytes:zero:v1",
                rate_ref="connector-price:v2:bytes:zero", meter="bytes",
                unit_quantity=1, unit_price_micros=0,
            ),
            idempotency_key="price-book-complete:bytes:zero",
        )
        paid_calls = self.connectors.register_price_rate(
            price_rate_spec(
                profile_v2["id"], identifier="connector-price:v2:calls:v1",
                rate_ref="connector-price:v2:calls", meter="calls",
                unit_quantity=1, unit_price_micros=5000,
            ),
            idempotency_key="price-book-complete:calls",
        )
        self.connectors.register_price_rate(
            price_rate_spec(
                profile_v2["id"], identifier="connector-price:v2:records:eur:v1",
                rate_ref="connector-price:v2:records:eur", meter="records",
                unit_quantity=1, unit_price_micros=10, currency="EUR",
            ),
            idempotency_key="price-book-complete:records:eur",
        )
        self.connectors.register_price_rate(
            price_rate_spec(
                profile_v2["id"], identifier="connector-price:v2:records:future:v1",
                rate_ref="connector-price:v2:records:future", meter="records",
                unit_quantity=1, unit_price_micros=10,
                effective_from=WHEN + timedelta(seconds=60),
            ),
            idempotency_key="price-book-complete:records:future",
        )
        omitted = rate_policy_spec(
            profile_v2["id"], identifier="connector-rate-policy:v2-source:v1",
            effective_until=WHEN + timedelta(seconds=30),
            price_rate_refs=(zero_bytes["id"],),
            required_price_meters=("bytes",),
        )
        omitted["policy_ref"] = "connector-rate-policy:v2-source"
        omitted["quota_scope_ref"] = "connector-quota-scope:v2-source"
        with self.assertRaises(ConnectorConflict):
            self.connectors.register_rate_policy(
                omitted, idempotency_key="price-book-complete:omitted"
            )
        exact = rate_policy_spec(
            profile_v2["id"], identifier="connector-rate-policy:v2-source:v1",
            effective_until=WHEN + timedelta(seconds=30),
            price_rate_refs=(zero_bytes["id"], paid_calls["id"]),
            required_price_meters=("bytes", "calls"),
            limits={
                "calls": 2, "bytes": 10_000, "records": 100,
                "cost_micros": 10_000,
            },
        )
        exact["policy_ref"] = "connector-rate-policy:v2-source"
        exact["quota_scope_ref"] = "connector-quota-scope:v2-source"
        saved = self.connectors.register_rate_policy(
            exact, idempotency_key="price-book-complete:exact"
        )
        self.assertEqual(
            saved["price_rate_refs"], sorted([zero_bytes["id"], paid_calls["id"]])
        )

    def test_new_current_price_rate_blocks_admission_and_opens_incident(self) -> None:
        self.connectors.register_price_rate(
            price_rate_spec(
                self.profile["id"], identifier="connector-price:drift:records:v1",
                rate_ref="connector-price:drift:records", meter="records",
                unit_quantity=1, unit_price_micros=1,
            ),
            idempotency_key="price-book-drift:records",
        )
        with self.assertRaises(ConnectorBlocked):
            self.reserve(1, "price-book-drift:reserve")
        incident = self.connectors.conn.execute(
            "SELECT record_json FROM connector_incidents "
            "WHERE incident_type='policy_violation' AND severity='blocking'"
        ).fetchone()
        self.assertIsNotNone(incident)
        self.assertEqual(
            json.loads(incident["record_json"])["details"]["drift_kind"],
            "price_book_drift",
        )
        self.assertEqual(
            self.connectors.conn.execute(
                "SELECT count(*) AS n FROM connector_quota_reservations"
            ).fetchone()["n"],
            0,
        )

    def test_ephemeral_attempt_time_price_drift_is_durable(self) -> None:
        reservation = self.reserve(1, "ephemeral-price-drift:reserve")
        ephemeral = self.connectors.register_price_rate(
            price_rate_spec(
                self.profile["id"], identifier="connector-price:ephemeral:records:v1",
                rate_ref="connector-price:ephemeral:records", meter="records",
                unit_quantity=1, unit_price_micros=1, effective_from=WHEN,
                effective_until=WHEN + timedelta(microseconds=500_000),
            ),
            idempotency_key="ephemeral-price-drift:rate",
        )
        started_at, completed_at = self.terminal_times()
        attempt = self.connectors.record_physical_attempt(
            self.invocation["id"], reservation["id"], 1, "succeeded",
            started_at=started_at, completed_at=completed_at,
            provider_request_id="provider:ephemeral-price-drift",
            idempotency_key="ephemeral-price-drift:attempt",
        )
        usage = self.connectors.record_usage(
            attempt["id"], {"calls": 1, "bytes": 100, "records": 1, "cost_micros": 0},
            measurement_status="final", metering_source="provider_reported",
            idempotency_key="ephemeral-price-drift:usage",
        )
        with self.assertRaises(ConnectorBlocked):
            self.connectors.record_cost(
                usage["id"], price_rate_refs=[self.zero_rate["id"]], amount_micros=0,
                currency="USD", cost_status="actual",
                calculation_ref="calculator:connector:test", actor_ref="system:cost",
                idempotency_key="ephemeral-price-drift:cost",
            )
        self.assertEqual(
            self.connectors.conn.execute(
                "SELECT count(*) AS n FROM connector_cost_entries"
            ).fetchone()["n"],
            0,
        )
        incident = self.connectors.conn.execute(
            "SELECT record_json FROM connector_incidents "
            "WHERE incident_type='policy_violation' AND severity='blocking'"
        ).fetchone()
        self.assertIsNotNone(incident)
        details = json.loads(incident["record_json"])["details"]
        self.assertEqual(details["physical_attempt_ref"], attempt["id"])
        self.assertEqual(details["policy_version_ref"], self.policy["id"])
        self.assertEqual(details["canonical_rate_refs"], sorted([
            self.zero_rate["id"], ephemeral["id"]
        ]))
        self.clock.advance(60)
        with self.assertRaises(ConnectorBlocked):
            self.reserve(2, "ephemeral-price-drift:later-admission")

    def test_hard_cost_quota_reserves_frozen_price_book_maximum(self) -> None:
        rate = self.connectors.register_price_rate(
            price_rate_spec(
                self.profile["id"], identifier="connector-price:cost-quota:v1",
                rate_ref="connector-price:cost-quota", meter="records",
                unit_quantity=1, unit_price_micros=5,
            ),
            idempotency_key="cost-quota:rate",
        )
        policy_v2 = self.connectors.register_rate_policy(
            rate_policy_spec(
                self.profile["id"], version=2,
                identifier="connector-rate-policy:a-share:priced:v2",
                prior_version_ref=self.policy["id"], effective_from=WHEN,
                price_rate_refs=(self.zero_rate["id"], rate["id"]),
                required_price_meters=("calls", "records"),
            ),
            idempotency_key="cost-quota:policy:v2",
        )
        self.policy = policy_v2
        with self.assertRaises(ConnectorQuotaExceeded):
            self.connectors.reserve_quota(
                self.invocation["id"], self.policy["id"], 1,
                {"calls": 1, "bytes": 1000, "records": 10, "cost_micros": 100},
                ttl_seconds=30, idempotency_key="cost-quota:under-reserved",
            )
        with self.assertRaises(ConnectorQuotaExceeded):
            self.connectors.reserve_quota(
                self.invocation["id"], self.policy["id"], 1,
                {"calls": 1, "bytes": 1000, "records": 10, "cost_micros": 5000},
                ttl_seconds=30, idempotency_key="cost-quota:over-window-limit",
            )

    def test_attempt_usage_settlement_and_retry_after_are_separate_facts(self) -> None:
        rate = self.connectors.register_price_rate(
            price_rate_spec(
                self.profile["id"], identifier="connector-price:a-share:bytes:v1",
                rate_ref="connector-price:a-share:bytes", meter="bytes",
                unit_quantity=1000, unit_price_micros=3,
            ),
            idempotency_key="price:bytes:v1",
        )
        self.policy = self.connectors.register_rate_policy(
            rate_policy_spec(
                self.profile["id"], version=2,
                identifier="connector-rate-policy:a-share:calls:v2",
                prior_version_ref=self.policy["id"], effective_from=WHEN,
                price_rate_refs=(self.zero_rate["id"], rate["id"]),
                required_price_meters=("calls", "bytes"),
                limits={
                    "calls": 2, "bytes": 10_000, "records": 100,
                    "cost_micros": 5000,
                },
            ),
            idempotency_key="price:calls:policy:v2",
        )
        reservation = self.connectors.reserve_quota(
            self.invocation["id"], self.policy["id"], 1,
            {"calls": 1, "bytes": 1000, "records": 10, "cost_micros": 3000},
            ttl_seconds=30, idempotency_key="reserve:retry",
        )
        started_at, completed_at = self.terminal_times()
        retry_at = (self.clock.value + timedelta(seconds=44)).isoformat()
        attempt = self.connectors.record_physical_attempt(
            self.invocation["id"], reservation["id"], 1, "rate_limited",
            started_at=started_at, completed_at=completed_at,
            provider_request_id="provider-request:1", retry_at=retry_at,
            idempotency_key="attempt:retry",
        )
        self.assertEqual(attempt["retry_at"], datetime.fromisoformat(retry_at).isoformat(timespec="microseconds"))
        assert_wire_schema(self, "connector-physical-attempt.schema.json", attempt)
        usage = self.connectors.record_usage(
            attempt["id"], {"calls": 1, "bytes": 120, "records": 0, "cost_micros": 5},
            measurement_status="final", metering_source="provider_reported",
            provider_usage_ref="provider-usage:1", idempotency_key="usage:retry",
        )
        assert_wire_schema(self, "connector-usage-entry.schema.json", usage)
        cost = self.connectors.record_cost(
            usage["id"], price_rate_refs=[self.zero_rate["id"], rate["id"]],
            amount_micros=1,
            currency="USD", cost_status="actual", calculation_ref="calculator:connector:0.1",
            actor_ref="system:cost", idempotency_key="cost:retry",
        )
        self.assertEqual(cost["amount_micros"], 1)
        settled_actual = {**usage["metrics"], "cost_micros": 1}
        settled = self.connectors.settle_quota(
            reservation["id"], "consumed", settled_actual,
            usage_entry_ref=usage["id"], cost_entry_ref=cost["id"],
            idempotency_key="settle:retry",
        )
        self.assertEqual(settled["actual"]["calls"], 1)
        assert_wire_schema(self, "connector-quota-settlement.schema.json", settled)
        assert_wire_schema(self, "connector-price-rate-version.schema.json", rate)
        assert_wire_schema(self, "connector-cost-entry.schema.json", cost)
        with self.assertRaises(ConnectorConflict):
            self.connectors.record_cost(
                usage["id"], price_rate_refs=[rate["id"]], amount_micros=24,
                currency="USD", cost_status="actual", calculation_ref="calculator:connector:0.1",
                actor_ref="system:cost", idempotency_key="cost:wrong",
            )
        future_rate = self.connectors.register_price_rate(
            {
                "schema_version": "0.1", "id": "connector-price:a-share:calls:future:v1",
                "created_at": WHEN.isoformat(),
                "price_rate_ref": "connector-price:a-share:calls:future", "version": 1,
                "prior_version_ref": None, "connector_profile_ref": self.profile["id"],
                "meter": "records", "unit_quantity": 1, "unit_price_micros": 25,
                "rounding_mode": "ceiling", "currency": "USD",
                "effective_from": (WHEN + timedelta(seconds=1)).isoformat(),
                "effective_until": None, "source_ref": "pricing:future",
                "actor_ref": "human:governance",
            },
            idempotency_key="price:calls:future:v1",
        )
        with self.assertRaises(ConnectorConflict):
            self.connectors.record_cost(
                usage["id"], price_rate_refs=[future_rate["id"]], amount_micros=25,
                currency="USD", cost_status="actual",
                calculation_ref="calculator:connector:0.1", actor_ref="system:cost",
                correction_of_ref=cost["id"], idempotency_key="cost:future-rate",
            )
        with self.assertRaises(ConnectorConflict):
            self.connectors.register_price_rate(
                {
                    "schema_version": "0.1", "id": "connector-price:bytes:second:v1",
                    "created_at": WHEN.isoformat(), "price_rate_ref": "connector-price:bytes:second",
                    "version": 1, "prior_version_ref": None,
                    "connector_profile_ref": self.profile["id"], "meter": "bytes",
                    "unit_quantity": 1000, "unit_price_micros": 4,
                    "rounding_mode": "ceiling", "currency": "USD",
                    "effective_from": "2026-01-01T00:00:00+00:00",
                    "effective_until": None, "source_ref": "pricing:bounded:v2",
                    "actor_ref": "human:governance",
                },
                idempotency_key="price:bytes:second:v1:overlap",
            )
        with self.assertRaises(ConnectorConflict):
            self.connectors.register_price_rate(
                {
                    "schema_version": "0.1", "id": "connector-price:a-share:calls:second:v1",
                    "created_at": WHEN.isoformat(),
                    "price_rate_ref": "connector-price:a-share:calls:second", "version": 1,
                    "prior_version_ref": None, "connector_profile_ref": self.profile["id"],
                    "meter": "calls", "unit_quantity": 1, "unit_price_micros": 25,
                    "rounding_mode": "ceiling", "currency": "USD",
                    "effective_from": "2026-01-01T00:00:00+00:00", "effective_until": None,
                    "source_ref": "pricing:second", "actor_ref": "human:governance",
                },
                idempotency_key="price:calls:second:v1",
            )
        self.assertEqual(
            self.connectors.record_usage(
                attempt["id"], usage["metrics"], measurement_status="final",
                metering_source="provider_reported", provider_usage_ref="provider-usage:1",
                idempotency_key="usage:retry",
            )["write_status"],
            "duplicate",
        )

    def test_blocking_incident_stops_admission_until_resolved(self) -> None:
        incident = self.connectors.open_incident(
            self.profile["id"], "quota_drift", "blocking", {"provider_overage": 1},
            actor_ref="system:quota", idempotency_key="incident:quota",
        )
        assert_wire_schema(self, "connector-incident.schema.json", {
            key: value for key, value in incident.items() if key != "event"
        })
        assert_wire_schema(self, "connector-incident-event.schema.json", incident["event"])
        with self.assertRaises(ConnectorBlocked):
            self.reserve(1, "reserve:blocked")
        self.connectors.resolve_incident(
            incident["id"], actor_ref="human:operator", idempotency_key="incident:resolve"
        )
        self.assertEqual(self.reserve(1, "reserve:after-resolve")["write_status"], "fresh")

    def test_source_envelope_requires_artifact_and_attempt_provenance(self) -> None:
        reservation = self.reserve(1, "reserve:source")
        started_at, completed_at = self.terminal_times()
        attempt = self.connectors.record_physical_attempt(
            self.invocation["id"], reservation["id"], 1, "succeeded",
            started_at=started_at, completed_at=completed_at,
            provider_request_id="cninfo:request:1", idempotency_key="attempt:source",
        )
        self.obs.register_artifact_version_v2(
            "artifact:cninfo:raw:1", version_id="artifact:cninfo:raw:1",
            title="CNINFO raw response", kind="raw_source", media_type="application/json",
            artifact_content_hash="1" * 64, size_bytes=120,
            storage_locator="artifact-store:cninfo/raw/1",
            producer_execution_ref=self.invocation["id"],
            result_envelope_ref="result:connector:1", result_envelope_hash="2" * 64,
            access_class="internal", preview_status="unavailable",
            actor_ref="system:artifact",
        )
        source = {
            "schema_version": "0.1", "id": "source-envelope:cninfo:1",
            "created_at": WHEN.isoformat(), "connector_invocation_ref": self.invocation["id"],
            "connector_profile_ref": self.profile["id"], "physical_attempt_refs": [attempt["id"]],
            "result_physical_attempt_ref": attempt["id"],
            "source": "cninfo", "operation": "list_announcements",
            "source_record_refs": ["cninfo:announcement:1"], "published_at": WHEN.isoformat(),
            "updated_at": None, "as_of": WHEN.isoformat(), "retrieved_at": WHEN.isoformat(),
            "cursor": "page:1", "provider_request_id": "cninfo:request:1",
            "raw_artifact_version_ref": "artifact:cninfo:raw:1", "raw_response_hash": "1" * 64,
            "source_schema_hash": "6" * 64, "source_content_hash": "4" * 64,
            "completeness": "enumerated", "status": "complete",
            "access_policy_ref": "policy:access:public",
            "retention_policy_ref": "policy:retention:filing",
            "terms_policy_ref": "policy:terms:cninfo", "error": None,
        }
        source["source_content_hash"] = source_envelope_content_hash(source)
        saved = self.connectors.record_source_envelope(source, idempotency_key="source:1")
        self.assertEqual(saved["raw_artifact_version_ref"], "artifact:cninfo:raw:1")
        assert_wire_schema(self, "source-envelope.schema.json", saved)
        mismatches = (
            ("raw-hash", {"raw_response_hash": "9" * 64}),
            ("source", {"source": "sec"}),
            ("operation", {"operation": "get_document"}),
            ("schema", {"source_schema_hash": "9" * 64}),
            ("policy", {"access_policy_ref": "policy:access:forged"}),
            ("provider", {"provider_request_id": "provider:forged"}),
        )
        for suffix, change in mismatches:
            with self.subTest(suffix=suffix), self.assertRaises(ConnectorConflict):
                self.connectors.record_source_envelope(
                    {**source, "id": f"source-envelope:{suffix}", **change},
                    idempotency_key=f"source:mismatch:{suffix}",
                )
        foreign_execution = replace(
            ExecutionInvocation.from_dict(
                json.loads(
                    self.core.conn.execute(
                        "SELECT execution_json FROM execution_invocations WHERE execution_id=?",
                        (self.invocation["id"],),
                    ).fetchone()["execution_json"]
                )
            ),
            id="connector-invocation:foreign",
            output_refs=("artifact:foreign:raw:1",),
        )
        with self.core._transaction() as cur:
            self.core._ensure_execution_invocation(cur, foreign_execution)
        self.obs.register_artifact_version_v2(
            "artifact:foreign:raw:1", version_id="artifact:foreign:raw:1",
            title="Foreign raw response", kind="raw_source", media_type="application/json",
            artifact_content_hash="7" * 64, size_bytes=120,
            storage_locator="artifact-store:foreign/raw/1",
            producer_execution_ref=foreign_execution.id,
            result_envelope_ref="result:foreign:1", result_envelope_hash="8" * 64,
            access_class="internal", preview_status="unavailable",
            actor_ref="system:artifact",
        )
        with self.assertRaises(ConnectorConflict):
            self.connectors.record_source_envelope(
                {
                    **source, "id": "source-envelope:foreign-artifact",
                    "raw_artifact_version_ref": "artifact:foreign:raw:1",
                    "raw_response_hash": "7" * 64,
                },
                idempotency_key="source:foreign-artifact",
            )
        with self.assertRaises(ConnectorConflict):
            self.connectors.record_source_envelope(
                {
                    **source, "id": "source-envelope:missing",
                    "raw_artifact_version_ref": "artifact:missing",
                },
                idempotency_key="source:missing",
            )

    def test_source_complete_or_empty_requires_a_succeeded_attempt(self) -> None:
        reservation = self.reserve(1, "source-status:reserve")
        started_at, completed_at = self.terminal_times()
        attempt = self.connectors.record_physical_attempt(
            self.invocation["id"], reservation["id"], 1, "rate_limited",
            started_at=started_at, completed_at=completed_at,
            provider_request_id="cninfo:rate-limited",
            retry_at=(self.clock.value + timedelta(seconds=29)).isoformat(),
            idempotency_key="source-status:attempt",
        )
        self.obs.register_artifact_version_v2(
            "artifact:cninfo:raw:1", version_id="artifact:cninfo:raw:1",
            title="CNINFO 429", kind="raw_source", media_type="application/json",
            artifact_content_hash="1" * 64, size_bytes=120,
            storage_locator="artifact-store:cninfo/raw/429",
            producer_execution_ref=self.invocation["id"],
            result_envelope_ref="result:connector:429", result_envelope_hash="2" * 64,
            access_class="internal", preview_status="unavailable", actor_ref="system:artifact",
        )
        base = {
            "schema_version": "0.1", "id": "source-envelope:invalid-complete",
            "created_at": WHEN.isoformat(), "connector_invocation_ref": self.invocation["id"],
            "connector_profile_ref": self.profile["id"], "physical_attempt_refs": [attempt["id"]],
            "result_physical_attempt_ref": attempt["id"],
            "source": "cninfo", "operation": "list_announcements",
            "source_record_refs": ["cninfo:announcement:1"], "published_at": WHEN.isoformat(),
            "updated_at": None, "as_of": WHEN.isoformat(), "retrieved_at": WHEN.isoformat(),
            "cursor": None, "provider_request_id": "cninfo:rate-limited",
            "raw_artifact_version_ref": "artifact:cninfo:raw:1", "raw_response_hash": "1" * 64,
            "source_schema_hash": "6" * 64, "source_content_hash": "4" * 64,
            "completeness": "enumerated", "status": "complete",
            "access_policy_ref": "policy:access:public",
            "retention_policy_ref": "policy:retention:filing",
            "terms_policy_ref": "policy:terms:cninfo", "error": None,
        }
        base["source_content_hash"] = source_envelope_content_hash(base)
        with self.assertRaises(ConnectorConflict):
            self.connectors.record_source_envelope(base, idempotency_key="source-status:complete")
        with self.assertRaises(ConnectorConflict):
            self.connectors.record_source_envelope(
                {
                    **base, "id": "source-envelope:invalid-empty", "status": "empty",
                    "source_record_refs": [],
                },
                idempotency_key="source-status:empty",
            )
        usage = self.connectors.record_usage(
            attempt["id"], {"calls": 1, "bytes": 120, "records": 0, "cost_micros": 0},
            measurement_status="final", metering_source="provider_reported",
            idempotency_key="source-status:usage:1",
        )
        cost = self.connectors.record_cost(
            usage["id"], price_rate_refs=[self.zero_rate["id"]], amount_micros=0,
            currency="USD", cost_status="actual", calculation_ref="calculator:connector:test",
            actor_ref="system:cost", idempotency_key="source-status:cost:1",
        )
        self.connectors.settle_quota(
            reservation["id"], "consumed", usage["metrics"],
            usage_entry_ref=usage["id"], cost_entry_ref=cost["id"],
            idempotency_key="source-status:settle:1",
        )
        second_reservation = self.reserve(2, "source-status:reserve:2")
        started_at, completed_at = self.terminal_times()
        succeeded = self.connectors.record_physical_attempt(
            self.invocation["id"], second_reservation["id"], 2, "succeeded",
            started_at=started_at, completed_at=completed_at,
            provider_request_id="cninfo:succeeded", idempotency_key="source-status:attempt:2",
        )
        failed_result = {
            **base,
            "id": "source-envelope:mixed-failed-result",
            "physical_attempt_refs": [attempt["id"], succeeded["id"]],
        }
        failed_result["source_content_hash"] = source_envelope_content_hash(failed_result)
        with self.assertRaises(ConnectorConflict):
            self.connectors.record_source_envelope(
                failed_result, idempotency_key="source-status:mixed-failed-result"
            )
        succeeded_result = {
            **failed_result,
            "id": "source-envelope:mixed-succeeded-result",
            "result_physical_attempt_ref": succeeded["id"],
            "provider_request_id": "cninfo:succeeded",
        }
        succeeded_result["source_content_hash"] = source_envelope_content_hash(succeeded_result)
        saved = self.connectors.record_source_envelope(
            succeeded_result, idempotency_key="source-status:mixed-succeeded-result"
        )
        self.assertEqual(saved["result_physical_attempt_ref"], succeeded["id"])

    def test_source_health_transition_is_append_only(self) -> None:
        healthy = self.connectors.record_source_health(
            self.profile["id"], "healthy", actor_ref="system:health",
            idempotency_key="health:healthy",
        )
        degraded = self.connectors.record_source_health(
            self.profile["id"], "degraded", actor_ref="system:health",
            idempotency_key="health:degraded",
        )
        self.assertEqual(degraded["prior_event_ref"], healthy["id"])
        assert_wire_schema(self, "connector-source-health-event.schema.json", degraded)
        self.connectors.record_source_health(
            self.profile["id"], "open_circuit", actor_ref="system:health",
            idempotency_key="health:open",
        )
        with self.assertRaises(ConnectorBlocked):
            self.reserve(1, "health:blocked-reservation")
        with self.assertRaises(ConnectorConflict):
            self.connectors.record_source_health(
                self.profile["id"], "healthy", actor_ref="system:health",
                idempotency_key="health:invalid",
            )


if __name__ == "__main__":
    unittest.main()
