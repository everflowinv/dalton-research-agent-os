from __future__ import annotations

import copy
import inspect
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from dalton_core.capability_catalog import CapabilityCatalog, canonical_hash
from dalton_core.connector import ConnectorBlocked, ConnectorStore
from dalton_core.connector_runner import (
    AdapterNotResolved,
    ConnectorRunnerAdmissionGate,
    RunnerConflict,
    RunnerValidationError,
    StaticAdapterResolver,
    validate_adapter_transport_observation,
    validate_connector_adapter_request,
    validate_connector_runner_request,
    validate_connector_runner_response,
    validate_runner_environment_manifest,
)
from dalton_core.contracts import ExecutionInvocation, ExecutionKind, WorkOrder
from dalton_core.scheduler import LeaseRejected, Scheduler
from dalton_core.store import DaltonStore, content_hash
from tests.test_capability_catalog import FakeAuthorities, descriptor_spec
from tests.test_connector import (
    MutableClock,
    assert_wire_schema,
    price_rate_spec,
    profile_spec,
    rate_policy_spec,
)


WHEN = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
WIRE_WHEN = WHEN.isoformat(timespec="microseconds")


def with_hash(wire: dict) -> dict:
    return {**wire, "content_hash": content_hash(wire)}


def permissions() -> dict:
    return {
        "risk_class": "low",
        "network": False,
        "filesystem_read": ["runner:recorded-fixtures"],
        "filesystem_write": ["runner:raw-sink"],
        "credential_slot_refs": [],
        "core_db": False,
        "side_effects": ["read:recorded-fixture"],
    }


class RecordedAdapter:
    def __call__(self, request: dict, raw_sink) -> dict:
        raise AssertionError("P0-2a control-plane tests must not execute adapter")


def validate_recorded_input(parameters: dict) -> bool:
    return (
        set(parameters) == {"stock_code", "page"}
        and isinstance(parameters["stock_code"], str)
        and isinstance(parameters["page"], int)
        and not isinstance(parameters["page"], bool)
        and parameters["page"] >= 1
    )


class ConnectorRunnerControlPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.core = DaltonStore(":memory:")
        self.connectors = ConnectorStore(self.core, clock=self.clock)
        self.scheduler = Scheduler(
            ":memory:", clock=self.clock, default_lease_seconds=30,
            max_lease_seconds=60,
        )
        self.authorities = FakeAuthorities()
        self.authorities.policy_permissions = permissions()
        self.catalog = CapabilityCatalog(
            ":memory:", clock=self.clock,
            approval_resolver=self.authorities.approval,
            policy_resolver=self.authorities.policy,
        )
        self.addCleanup(self.catalog.close)
        self.addCleanup(self.scheduler.close)
        self.addCleanup(self.core.close)

        template = profile_spec()
        capability_id = template["capability_id"]
        descriptor_wire = descriptor_spec(
            capability_id, name="recorded-a-share-announcements",
            side_effects=["read:recorded-fixture"],
        )
        descriptor_wire.update(
            {
                "kind": "connector",
                "label": "Recorded A-share announcement adapter",
                "summary": "Replay one recorded announcement response without network",
                "contract": {
                    "mode": "typed_call",
                    "input_schema_ref": template["input_schema_refs"]["list_announcements"],
                    "output_schema_ref": template["output_schema_refs"]["list_announcements"],
                    "instruction_ref": None,
                    "adapter_ref": template["adapter_ref"],
                },
                "permissions": permissions(),
                "source_hash": template["source_hash"],
                "schema_hash": template["schema_hash"],
            }
        )
        self.descriptor = self.catalog.publish(descriptor_wire)
        binding = {
            "binding_ref": "runner-binding:cninfo-recorded:v1",
            "descriptor_revision_ref": self.descriptor.revision_ref,
            "descriptor_hash": self.descriptor.content_hash,
            "adapter_ref": template["adapter_ref"],
            "adapter_hash": template["adapter_hash"],
            "source_ref": template["source_identity"]["source_ref"],
            "source_hash": template["source_hash"],
            "operation": "list_announcements",
            "input_schema_ref": template["input_schema_refs"]["list_announcements"],
            "input_schema_hash": template["input_schema_hashes"]["list_announcements"],
            "output_schema_ref": template["output_schema_refs"]["list_announcements"],
            "output_schema_hash": template["output_schema_hashes"]["list_announcements"],
            "auth_mode": "none",
            "credential_slot_refs": [],
            "required_permissions": permissions(),
            "side_effects": ["read:recorded-fixture"],
            "rate_policy_ref": "connector-rate-policy:a-share",
        }
        manifest_base = {
            "schema_version": "0.1",
            "id": "runner-environment:recorded:v1",
            "created_at": WIRE_WHEN,
            "runner_runtime_ref": template["runner_runtime_ref"],
            "runner_actor_ref": template["runner_actor_ref"],
            "resolver_ref": "resolver:connector-static:0.1",
            "resolver_version": "0.1",
            "package_manifest_ref": "artifact:runner-packages:recorded:v1",
            "package_manifest_hash": "9" * 64,
            "bindings": [binding],
        }
        self.manifest = validate_runner_environment_manifest(with_hash(manifest_base))
        self.resolver = StaticAdapterResolver(
            self.manifest,
            {binding["binding_ref"]: RecordedAdapter()},
            {binding["binding_ref"]: validate_recorded_input},
        )

        profile_wire = profile_spec()
        profile_wire.update(
            {
                "descriptor_revision_ref": self.descriptor.revision_ref,
                "descriptor_hash": self.descriptor.content_hash,
                "catalog_epoch": self.descriptor.catalog_epoch,
                "runner_environment_hash": self.manifest["content_hash"],
            }
        )
        self.profile = self.connectors.register_profile(
            profile_wire, idempotency_key="runner:profile"
        )

        self.call_id = "connector-call:a-share:runner"
        self.work = WorkOrder(
            schema_version="0.1",
            id="work:connector-runner:1",
            created_at=WHEN.isoformat(),
            updated_at=WHEN.isoformat(),
            question="Replay the bounded A-share announcement fixture",
            requested_capabilities=(capability_id,),
            runtime_profile_ref=template["runner_runtime_ref"],
            budget={"max_seconds": 60},
            idempotency_key="work:connector-runner:1",
            declared_side_effects=("read:recorded-fixture",),
            status="ready",
            input_refs=(self.call_id,),
        )
        self.work_hash = content_hash(self.work.to_dict())
        policy = self.authorities.policy({"policy_ref": "policy:capability-v1"})
        self.capability_lease = self.catalog.prepare(
            self.work,
            capability_id=self.descriptor.id,
            revision_ref=self.descriptor.revision_ref,
            catalog_epoch=self.descriptor.catalog_epoch,
            descriptor_hash=self.descriptor.content_hash,
            source_hash=self.descriptor.source_hash,
            schema_hash=self.descriptor.schema_hash,
            policy_ref="policy:capability-v1",
            policy_hash=policy["content_hash"],
            principal_ref="principal:worker-1",
            visibility_scopes=["research"],
            ttl_seconds=60,
        )
        parameters = {"stock_code": "600309", "page": 1}
        self.call = self.connectors.register_call_spec(
            {
                "schema_version": "0.1",
                "id": self.call_id,
                "created_at": WHEN.isoformat(),
                "work_order_ref": self.work.id,
                "work_order_hash": self.work_hash,
                "connector_profile_ref": self.profile["id"],
                "operation": "list_announcements",
                "parameters": parameters,
                "query_hash": content_hash(
                    {"operation": "list_announcements", "parameters": parameters}
                ),
            },
            idempotency_key="runner:call",
        )
        execution = ExecutionInvocation(
            schema_version="0.1",
            id="connector-invocation:runner:1",
            created_at=WHEN.isoformat(),
            kind=ExecutionKind.CONNECTOR,
            work_order_ref=self.work.id,
            profile_ref=self.profile["id"],
            capability=capability_id,
            input_refs=(self.call["id"],),
            output_refs=("artifact:runner:raw:1",),
            started_at=WHEN.isoformat(),
            completed_at=None,
            side_effects=(),
            runtime_ref=self.profile["runner_runtime_ref"],
            actor_ref=self.profile["runner_actor_ref"],
            environment_hash=self.profile["runner_environment_hash"],
        )
        self.execution = execution
        self.invocation = self.connectors.register_invocation(
            {
                "schema_version": "0.1",
                "id": execution.id,
                "created_at": WHEN.isoformat(),
                "work_order_ref": self.work.id,
                "work_order_hash": self.work_hash,
                "connector_profile_ref": self.profile["id"],
                "connector_profile_hash": self.profile["content_hash"],
                "call_spec_ref": self.call["id"],
                "call_spec_hash": self.call["content_hash"],
                "capability_lease_ref": self.capability_lease.id,
                "capability_lease_hash": self.capability_lease.content_hash,
                "descriptor_revision_ref": self.descriptor.revision_ref,
                "catalog_epoch": self.descriptor.catalog_epoch,
                "logical_invocation_key": "connector-logical:" + content_hash(
                    {
                        "work_order_ref": self.work.id,
                        "work_order_hash": self.work_hash,
                        "connector_profile_hash": self.profile["content_hash"],
                        "call_spec_hash": self.call["content_hash"],
                    }
                ),
            },
            execution=execution,
            idempotency_key="runner:invocation",
        )
        self.zero_rate = self.connectors.register_price_rate(
            price_rate_spec(
                self.profile["id"], identifier="connector-price:runner:zero:v1",
                rate_ref="connector-price:runner:zero", meter="calls",
                unit_quantity=1, unit_price_micros=0,
            ),
            idempotency_key="runner:price:zero",
        )
        self.policy = self.connectors.register_rate_policy(
            rate_policy_spec(
                self.profile["id"], price_rate_refs=(self.zero_rate["id"],),
                required_price_meters=("calls",),
                max_concurrency=2,
                limits={
                    "calls": 2,
                    "bytes": 2_000_000,
                    "records": 2000,
                    "cost_micros": 1000,
                },
            ),
            idempotency_key="runner:rate-policy",
        )
        self.scheduler.enqueue(self.work)
        self.claim = self.scheduler.claim(
            self.profile["runner_actor_ref"], work_order_id=self.work.id
        )
        assert self.claim is not None
        self.gate = ConnectorRunnerAdmissionGate(
            scheduler=self.scheduler,
            catalog=self.catalog,
            connectors=self.connectors,
            resolver=self.resolver,
            visibility_scopes=["research"],
            clock=self.clock,
        )

    def runner_request(self) -> dict:
        base = {
            "schema_version": "0.1",
            "id": "connector-runner-request:1",
            "created_at": WIRE_WHEN,
            "connector_invocation_ref": self.invocation["id"],
            "connector_invocation_hash": self.invocation["content_hash"],
            "execution_ref": self.invocation["execution_ref"],
            "execution_hash": self.invocation["execution_hash"],
            "work_order_ref": self.work.id,
            "work_order_hash": self.work_hash,
            "scheduler_attempt_number": 1,
            "scheduler_lease_revision_ref": self.claim["lease"]["id"],
            "scheduler_lease_hash": self.claim["lease"]["content_hash"],
            "connector_profile_ref": self.profile["id"],
            "connector_profile_hash": self.profile["content_hash"],
            "call_spec_ref": self.call["id"],
            "call_spec_hash": self.call["content_hash"],
            "capability_lease_ref": self.capability_lease.id,
            "capability_lease_hash": self.capability_lease.content_hash,
            "principal_ref": "principal:worker-1",
            "runner_runtime_ref": self.profile["runner_runtime_ref"],
            "runner_actor_ref": self.profile["runner_actor_ref"],
            "runner_environment_hash": self.profile["runner_environment_hash"],
            "idempotency_key": "runner-request:1",
        }
        return with_hash(base)

    def test_closed_contracts_and_static_resolver(self) -> None:
        assert_wire_schema(
            self, "runner-environment-manifest.schema.json", self.manifest
        )
        request = validate_connector_runner_request(self.runner_request())
        assert_wire_schema(self, "connector-runner-request.schema.json", request)
        self.assertEqual(request["connector_invocation_ref"], self.invocation["id"])
        with self.assertRaises(RunnerValidationError):
            validate_connector_runner_request({**request, "token": "must-not-enter"})
        with self.assertRaises(RunnerConflict):
            validate_connector_runner_request({**request, "content_hash": "0" * 64})
        source = inspect.getsource(StaticAdapterResolver)
        for forbidden in ("importlib", "exec(", "eval("):
            self.assertNotIn(forbidden, source)
        with self.assertRaises(RunnerValidationError):
            StaticAdapterResolver(self.manifest, {}, {})
        bad_manifest = copy.deepcopy(self.manifest)
        bad_manifest["bindings"][0]["required_permissions"]["unexpected"] = True
        bad_manifest["content_hash"] = content_hash(
            {key: value for key, value in bad_manifest.items() if key != "content_hash"}
        )
        with self.assertRaises(AssertionError):
            assert_wire_schema(
                self, "runner-environment-manifest.schema.json", bad_manifest
            )
        with self.assertRaises(RunnerValidationError):
            validate_runner_environment_manifest(bad_manifest)
        bad_credentials = copy.deepcopy(self.manifest)
        bad_credentials["bindings"][0]["credential_slot_refs"] = [
            "credential-slot:unexpected"
        ]
        bad_credentials["content_hash"] = content_hash(
            {
                key: value
                for key, value in bad_credentials.items()
                if key != "content_hash"
            }
        )
        with self.assertRaises(AssertionError):
            assert_wire_schema(
                self, "runner-environment-manifest.schema.json", bad_credentials
            )
        with self.assertRaises(RunnerValidationError):
            validate_runner_environment_manifest(bad_credentials)

    def test_use_time_gates_and_adapter_request_are_authority_derived(self) -> None:
        admission = self.gate.validate(
            self.runner_request(), scheduler_lease_token=self.claim["lease_token"]
        )
        self.assertIsInstance(admission.adapter, RecordedAdapter)
        self.assertEqual(admission.binding["rate_policy_ref"], self.policy["policy_ref"])
        reservation = self.gate.reserve_for_admission(
            admission, scheduler_lease_token=self.claim["lease_token"],
            idempotency_key="runner:reservation",
        )
        duplicate = self.gate.reserve_for_admission(
            admission, scheduler_lease_token=self.claim["lease_token"],
            idempotency_key="runner:reservation",
        )
        self.assertEqual(duplicate["write_status"], "duplicate")
        self.assertEqual(duplicate["id"], reservation["id"])
        with self.assertRaises(ConnectorBlocked):
            self.gate.reserve_for_admission(
                admission, scheduler_lease_token=self.claim["lease_token"],
                idempotency_key="runner:reservation:second-pending",
            )
        adapter_request = self.gate.build_adapter_request(
            admission, scheduler_lease_token=self.claim["lease_token"]
        )
        assert_wire_schema(
            self, "connector-adapter-request.schema.json", adapter_request
        )
        self.assertEqual(adapter_request["parameters"], self.call["parameters"])
        self.assertEqual(adapter_request["reservation_ref"], reservation["id"])
        self.assertTrue(adapter_request["raw_sink_ref"].startswith("raw-sink:"))
        self.assertGreater(
            datetime.fromisoformat(adapter_request["deadline_at"]), self.clock()
        )
        self.assertNotIn("lease_token", json.dumps(adapter_request))
        self.assertNotIn("storage_locator", adapter_request)
        parameters = inspect.signature(self.gate.build_adapter_request).parameters
        self.assertNotIn("raw_sink_ref", parameters)
        self.assertNotIn("credential_grant_ref", parameters)
        forged_profile = {
            **dict(admission.profile),
            "allowed_hosts": ["evil.example"],
            "source_identity": {"source_ref": "source:evil"},
            "max_response_bytes": 999_999_999,
            "max_records": 999_999_999,
            "auth_mode": "credential_slot",
        }
        forged_call = {
            **dict(admission.call_spec),
            "parameters": {"token": "must-not-enter"},
        }
        forged = replace(admission, profile=forged_profile, call_spec=forged_call)
        rebuilt = self.gate.build_adapter_request(
            forged, scheduler_lease_token=self.claim["lease_token"]
        )
        self.assertEqual(rebuilt, adapter_request)
        bad_adapter_request = copy.deepcopy(adapter_request)
        bad_adapter_request["source_identity"]["unexpected"] = True
        bad_adapter_request["content_hash"] = content_hash(
            {
                key: value
                for key, value in bad_adapter_request.items()
                if key != "content_hash"
            }
        )
        with self.assertRaises(AssertionError):
            assert_wire_schema(
                self, "connector-adapter-request.schema.json", bad_adapter_request
            )
        with self.assertRaises(RunnerValidationError):
            validate_connector_adapter_request(bad_adapter_request)
        for field, value in (
            ("raw_sink_ref", "raw-sink:%2Ftmp%2Fevil"),
            ("credential_grant_ref", "credential-grant:untrusted"),
        ):
            changed = copy.deepcopy(adapter_request)
            changed[field] = value
            changed["content_hash"] = content_hash(
                {key: item for key, item in changed.items() if key != "content_hash"}
            )
            with self.subTest(field=field), self.assertRaises(AssertionError):
                assert_wire_schema(
                    self, "connector-adapter-request.schema.json", changed
                )
            with self.subTest(field=field), self.assertRaises(RunnerValidationError):
                validate_connector_adapter_request(changed)
        for network_policy in (
            {**adapter_request["network_policy"], "resolve_public_only": False},
            {
                **adapter_request["network_policy"],
                "allow_redirects": False,
                "max_redirects": 5,
            },
        ):
            changed = copy.deepcopy(adapter_request)
            changed["network_policy"] = network_policy
            changed["content_hash"] = content_hash(
                {key: item for key, item in changed.items() if key != "content_hash"}
            )
            with self.subTest(network_policy=network_policy), self.assertRaises(
                AssertionError
            ):
                assert_wire_schema(
                    self, "connector-adapter-request.schema.json", changed
                )
            with self.subTest(network_policy=network_policy), self.assertRaises(
                RunnerValidationError
            ):
                validate_connector_adapter_request(changed)

    def test_underreservation_and_noncontiguous_attempt_fail_closed(self) -> None:
        self.connectors.reserve_quota(
            self.invocation["id"], self.policy["id"], 1,
            {"calls": 1, "bytes": 1, "records": 1, "cost_micros": 0},
            ttl_seconds=30, idempotency_key="runner:reservation:underreported",
        )
        admission = self.gate.validate(
            self.runner_request(), scheduler_lease_token=self.claim["lease_token"]
        )
        with self.assertRaises(ConnectorBlocked):
            self.gate.build_adapter_request(
                admission, scheduler_lease_token=self.claim["lease_token"]
            )

    def test_noncontiguous_attempt_fails_closed(self) -> None:
        self.connectors.reserve_quota(
            self.invocation["id"], self.policy["id"], 999,
            {"calls": 1, "bytes": 1_000_000, "records": 1000, "cost_micros": 0},
            ttl_seconds=30, idempotency_key="runner:reservation:attempt-999",
        )
        admission = self.gate.validate(
            self.runner_request(), scheduler_lease_token=self.claim["lease_token"]
        )
        with self.assertRaises(ConnectorBlocked):
            self.gate.build_adapter_request(
                admission, scheduler_lease_token=self.claim["lease_token"]
            )

    def test_final_gate_catches_renewal_during_reservation_validation(self) -> None:
        admission = self.gate.validate(
            self.runner_request(), scheduler_lease_token=self.claim["lease_token"]
        )
        self.gate.reserve_for_admission(
            admission, scheduler_lease_token=self.claim["lease_token"],
            idempotency_key="runner:reservation:toctou",
        )
        original = self.connectors.validate_reservation_for_use

        def renew_during_validation(*args, **kwargs):
            result = original(*args, **kwargs)
            self.scheduler.renew(
                self.work.id, 1, self.profile["runner_actor_ref"],
                self.claim["lease_token"], extend_seconds=5,
            )
            return result

        with patch.object(
            self.connectors,
            "validate_reservation_for_use",
            side_effect=renew_during_validation,
        ), self.assertRaises(LeaseRejected):
            self.gate.build_adapter_request(
                admission, scheduler_lease_token=self.claim["lease_token"]
            )

    def test_input_schema_validator_is_mandatory_and_fail_closed(self) -> None:
        binding = self.manifest["bindings"][0]
        with self.assertRaises(RunnerValidationError):
            self.resolver.validate_input(binding, {"token": "must-not-enter"})

    def test_scheduler_capability_profile_and_environment_drift_fail_closed(self) -> None:
        request = self.runner_request()
        admission = self.gate.validate(
            request, scheduler_lease_token=self.claim["lease_token"]
        )
        reservation = self.gate.reserve_for_admission(
            admission, scheduler_lease_token=self.claim["lease_token"],
            idempotency_key="runner:reservation:second-gate",
        )
        with self.assertRaises(RunnerConflict):
            self.gate.validate(
                with_hash({**{k: v for k, v in request.items() if k != "content_hash"},
                           "connector_profile_hash": "0" * 64}),
                scheduler_lease_token=self.claim["lease_token"],
            )
        bad_descriptor = replace(
            self.descriptor,
            contract=replace(
                self.descriptor.contract, adapter_ref="adapter:unexpected"
            ),
        )
        with patch.object(
            self.catalog, "describe", return_value=bad_descriptor
        ), self.assertRaises(RunnerConflict):
            self.gate.validate(
                request, scheduler_lease_token=self.claim["lease_token"]
            )
        for bad_contract in (
            replace(self.descriptor.contract, mode="process"),
            replace(
                self.descriptor.contract,
                instruction_ref="instruction:connector:unexpected",
            ),
        ):
            bad_descriptor = replace(self.descriptor, contract=bad_contract)
            with self.subTest(contract=bad_contract), patch.object(
                self.catalog, "describe", return_value=bad_descriptor
            ), self.assertRaises(RunnerConflict):
                self.gate.validate(
                    request, scheduler_lease_token=self.claim["lease_token"]
                )
        changed_manifest = dict(self.manifest)
        changed_manifest["package_manifest_hash"] = "8" * 64
        changed_manifest["content_hash"] = content_hash(
            {k: v for k, v in changed_manifest.items() if k != "content_hash"}
        )
        other_resolver = StaticAdapterResolver(
            changed_manifest,
            {self.manifest["bindings"][0]["binding_ref"]: RecordedAdapter()},
            {self.manifest["bindings"][0]["binding_ref"]: validate_recorded_input},
        )
        gate = ConnectorRunnerAdmissionGate(
            scheduler=self.scheduler, catalog=self.catalog, connectors=self.connectors,
            resolver=other_resolver, visibility_scopes=["research"], clock=self.clock,
        )
        with self.assertRaises(AdapterNotResolved):
            gate.validate(request, scheduler_lease_token=self.claim["lease_token"])
        renewed = self.scheduler.renew(
            self.work.id, 1, self.profile["runner_actor_ref"],
            self.claim["lease_token"], extend_seconds=5,
        )
        with self.assertRaises(LeaseRejected):
            self.gate.validate(request, scheduler_lease_token=self.claim["lease_token"])
        with self.assertRaises(LeaseRejected):
            self.gate.build_adapter_request(
                admission, scheduler_lease_token=self.claim["lease_token"],
            )
        self.assertNotEqual(renewed["id"], request["scheduler_lease_revision_ref"])

    def test_expired_reservation_is_not_transport_authority(self) -> None:
        reservation = self.connectors.reserve_quota(
            self.invocation["id"], self.policy["id"], 1,
            {"calls": 1, "bytes": 1_000_000, "records": 1000, "cost_micros": 0},
            ttl_seconds=2, idempotency_key="runner:reservation:expires",
        )
        self.clock.advance(3)
        with self.assertRaises(ConnectorBlocked):
            self.connectors.validate_reservation_for_use(
                reservation["id"], reservation_hash=reservation["content_hash"],
                connector_invocation_ref=self.invocation["id"],
                physical_attempt_number=1,
            )

    def test_observation_and_response_reject_authority_or_retry_ambiguity(self) -> None:
        observation = with_hash(
            {
                "protocol_version": "0.1", "request_hash": "1" * 64,
                "outcome": "succeeded", "provider_request_id": "fixture:1",
                "provider_status_code": 200, "retry_after_ms": None,
                "structured_output": {"records": []}, "source_record_refs": [],
                "cursor": None, "provider_usage": None, "error": None,
            }
        )
        validated_observation = validate_adapter_transport_observation(observation)
        assert_wire_schema(
            self, "adapter-transport-observation.schema.json", validated_observation
        )
        self.assertEqual(validated_observation["outcome"], "succeeded")
        with self.assertRaises(RunnerValidationError):
            validate_adapter_transport_observation(
                {**observation, "started_at": WHEN.isoformat()}
            )
        bad_observation = dict(observation)
        bad_observation["retry_after_ms"] = 1000
        bad_observation["content_hash"] = content_hash(
            {key: value for key, value in bad_observation.items() if key != "content_hash"}
        )
        with self.assertRaises(AssertionError):
            assert_wire_schema(
                self, "adapter-transport-observation.schema.json", bad_observation
            )
        with self.assertRaises(RunnerValidationError):
            validate_adapter_transport_observation(bad_observation)
        rate_limited = dict(observation)
        rate_limited.update(
            {"outcome": "rate_limited", "provider_status_code": 429, "retry_after_ms": 1000}
        )
        rate_limited["content_hash"] = content_hash(
            {k: v for k, v in rate_limited.items() if k != "content_hash"}
        )
        self.assertEqual(
            validate_adapter_transport_observation(rate_limited)["retry_after_ms"], 1000
        )
        response_base = {
            "schema_version": "0.1", "id": "connector-runner-response:1",
            "created_at": WIRE_WHEN, "runner_request_ref": "runner-request:1",
            "runner_request_hash": "1" * 64, "idempotency_status": "fresh",
            "connector_invocation_ref": "connector-invocation:1",
            "connector_invocation_hash": "2" * 64,
            "physical_attempt_ref": "connector-attempt:1", "physical_attempt_hash": "3" * 64,
            "usage_entry_ref": "connector-usage:1", "usage_entry_hash": "4" * 64,
            "cost_entry_ref": "connector-cost:1", "cost_entry_hash": "5" * 64,
            "quota_settlement_ref": "connector-settlement:1", "quota_settlement_hash": "6" * 64,
            "raw_artifact_version_ref": "artifact-version:1", "raw_artifact_version_hash": "7" * 64,
            "source_envelope_ref": "source-envelope:1", "source_envelope_hash": "8" * 64,
            "result_envelope_ref": "result-envelope:1", "result_envelope_hash": "9" * 64,
            "outcome": "succeeded", "retry_at": None,
        }
        response = with_hash(response_base)
        validated_response = validate_connector_runner_response(response)
        assert_wire_schema(
            self, "connector-runner-response.schema.json", validated_response
        )
        self.assertEqual(validated_response["outcome"], "succeeded")
        retryable = dict(response_base)
        retryable.update({"outcome": "retryable", "retry_at": None})
        invalid_retryable = with_hash(retryable)
        with self.assertRaises(AssertionError):
            assert_wire_schema(
                self, "connector-runner-response.schema.json", invalid_retryable
            )
        with self.assertRaises(RunnerValidationError):
            validate_connector_runner_response(invalid_retryable)


if __name__ == "__main__":
    unittest.main()
