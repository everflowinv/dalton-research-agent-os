from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import timedelta

from dalton_core.capability_catalog import CapabilityCatalog
from dalton_core.connector import ConnectorStore, ConnectorValidationError
from dalton_core.connector_authority_port import ConnectorAuthorityPort
from dalton_core.connector_inventory import load_packaged_connector_inventory
from dalton_core.connector_runner import (
    RunnerConflict,
    StaticAdapterResolver,
    validate_runner_environment_manifest,
)
from dalton_core.connector_transport_executor import (
    ConnectorTransportExecutor,
    SimulatedRunnerCrash,
)
from dalton_core.contracts import ExecutionInvocation, ExecutionKind, WorkOrder
from dalton_core.credential_authority import (
    CredentialAuthorityStore,
    CredentialGrantRejected,
)
from dalton_core.mcp_managed_runner import (
    McpManagedRunnerAdmissionGate,
    build_recorded_mcp_shadow_plan,
    validate_mcp_managed_adapter_request,
    validate_mcp_managed_transport_observation,
    validate_recorded_mcp_shadow_plan,
)
from dalton_core.observability import ObservabilityStore
from dalton_core.raw_spool import RawSpool
from dalton_core.recorded_alphaengine_adapter import RecordedAlphaEngineAdapter
from dalton_core.runner_journal import RunnerJournal
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, content_hash
from tests.test_capability_catalog import FakeAuthorities, descriptor_spec
from tests.test_connector import (
    MutableClock,
    WHEN,
    assert_wire_schema,
    price_rate_spec,
    rate_policy_spec,
)


def with_hash(wire: dict) -> dict:
    return {**wire, "content_hash": content_hash(wire)}


def mcp_permissions() -> dict:
    return {
        "risk_class": "low",
        "network": False,
        "filesystem_read": ["runner:recorded-fixtures"],
        "filesystem_write": ["runner:raw-sink"],
        "credential_slot_refs": ["credential-slot:alphaengine"],
        "core_db": False,
        "side_effects": ["read:recorded-fixture"],
    }


class AlphaEngineHarness:
    def __init__(
        self,
        scenario: str,
        *,
        fault_at: str | None = None,
        revoke_after_authorize: bool = False,
        adapter_factory=None,
    ) -> None:
        self.clock = MutableClock()
        self.core = DaltonStore(":memory:")
        self.connectors = ConnectorStore(self.core, clock=self.clock)
        self.scheduler = Scheduler(
            ":memory:", clock=self.clock, default_lease_seconds=30,
            max_lease_seconds=60,
        )
        self.authorities = FakeAuthorities()
        self.authorities.policy_permissions = mcp_permissions()
        self.catalog = CapabilityCatalog(
            ":memory:", clock=self.clock,
            approval_resolver=self.authorities.approval,
            policy_resolver=self.authorities.policy,
        )
        self.temp = tempfile.TemporaryDirectory()
        self.inventory = load_packaged_connector_inventory()
        template = self.inventory["templates"]["alphaengine"]
        operation = next(
            item for item in template["operations"]
            if item["operation"] == "search_library"
        )
        self.plan = build_recorded_mcp_shadow_plan(scenario)
        self.handle = object()
        self.invocations: list[tuple[dict, object]] = []

        def probe(request, handle) -> None:
            self.invocations.append((dict(request), handle))

        adapter = (
            RecordedAlphaEngineAdapter(
                scenario,
                advance_clock=self.clock.advance,
                invocation_probe=probe,
            )
            if adapter_factory is None
            else adapter_factory(scenario, self.clock, probe)
        )
        capability_id = "capability:dalton:connector:alphaengine-search"
        adapter_hash = content_hash(
            {
                "target_ref": "mcp-target:alphaengine",
                "package": "recorded-alphaengine-adapter:0.2",
            }
        )
        descriptor_wire = descriptor_spec(
            capability_id,
            name="recorded-alphaengine-search",
            side_effects=["read:recorded-fixture"],
        )
        descriptor_wire.update(
            {
                "kind": "connector",
                "label": "Recorded AlphaEngine search adapter",
                "summary": "Replay one synthetic AlphaEngine MCP response offline",
                "contract": {
                    "mode": "typed_call",
                    "input_schema_ref": operation["input_schema_ref"],
                    "output_schema_ref": operation["output_schema_ref"],
                    "instruction_ref": None,
                    "adapter_ref": "mcp-target:alphaengine",
                },
                "permissions": mcp_permissions(),
                "source_hash": content_hash(template["source_identity"]),
                "schema_hash": content_hash(
                    {
                        "allowed_operations": ["search_library"],
                        "input_schema_refs": {
                            "search_library": operation["input_schema_ref"]
                        },
                        "input_schema_hashes": {
                            "search_library": operation["input_schema_hash"]
                        },
                        "output_schema_refs": {
                            "search_library": operation["output_schema_ref"]
                        },
                        "output_schema_hashes": {
                            "search_library": operation["output_schema_hash"]
                        },
                    }
                ),
            }
        )
        self.descriptor = self.catalog.publish(descriptor_wire)
        binding = {
            "binding_ref": "runner-binding:alphaengine-recorded:0.2",
            "descriptor_revision_ref": self.descriptor.revision_ref,
            "descriptor_hash": self.descriptor.content_hash,
            "adapter_ref": "mcp-target:alphaengine",
            "adapter_hash": adapter_hash,
            "source_ref": "source:alphaengine",
            "source_hash": content_hash(template["source_identity"]),
            "operation": "search_library",
            "input_schema_ref": operation["input_schema_ref"],
            "input_schema_hash": operation["input_schema_hash"],
            "output_schema_ref": operation["output_schema_ref"],
            "output_schema_hash": operation["output_schema_hash"],
            "auth_mode": "mcp_managed",
            "credential_slot_refs": ["credential-slot:alphaengine"],
            "required_permissions": mcp_permissions(),
            "side_effects": ["read:recorded-fixture"],
            "rate_policy_ref": "connector-rate-policy:alphaengine",
        }
        manifest_base = {
            "schema_version": "0.1",
            "id": "runner-environment:alphaengine-recorded:0.2",
            "created_at": WHEN.isoformat(timespec="microseconds"),
            "runner_runtime_ref": "runtime:mcp-managed-runner:0.2",
            "runner_actor_ref": "runner:mcp-managed",
            "resolver_ref": "resolver:mcp-static:0.2",
            "resolver_version": "0.2",
            "package_manifest_ref": "artifact:runner-packages:alphaengine:0.2",
            "package_manifest_hash": "9" * 64,
            "bindings": [binding],
        }
        self.manifest = validate_runner_environment_manifest(with_hash(manifest_base))
        self.resolver = StaticAdapterResolver(
            self.manifest,
            {binding["binding_ref"]: adapter},
            {binding["binding_ref"]: self._validate_input},
        )
        profile_wire = {
            "schema_version": "0.1",
            "id": "connector-profile:alphaengine:v1",
            "created_at": WHEN.isoformat(timespec="microseconds"),
            "connector_ref": template["connector_ref"],
            "version": 1,
            "prior_version_ref": None,
            "capability_id": capability_id,
            "descriptor_revision_ref": self.descriptor.revision_ref,
            "descriptor_hash": self.descriptor.content_hash,
            "source_identity": template["source_identity"],
            "source_hash": content_hash(template["source_identity"]),
            "schema_hash": descriptor_wire["schema_hash"],
            "catalog_epoch": self.descriptor.catalog_epoch,
            "adapter_ref": "mcp-target:alphaengine",
            "adapter_hash": adapter_hash,
            "runner_runtime_ref": manifest_base["runner_runtime_ref"],
            "runner_actor_ref": manifest_base["runner_actor_ref"],
            "runner_environment_hash": self.manifest["content_hash"],
            "allowed_operations": ["search_library"],
            "allowed_hosts": [],
            "auth_mode": "mcp_managed",
            "credential_slot_refs": ["credential-slot:alphaengine"],
            "input_schema_refs": {
                "search_library": operation["input_schema_ref"]
            },
            "input_schema_hashes": {
                "search_library": operation["input_schema_hash"]
            },
            "output_schema_refs": {
                "search_library": operation["output_schema_ref"]
            },
            "output_schema_hashes": {
                "search_library": operation["output_schema_hash"]
            },
            "pagination": {
                "mode": operation["pagination"]["mode"],
                "cursor_field": operation["pagination"]["cursor_field"],
                "max_pages": operation["pagination"]["max_pages"],
            },
            "completeness": {
                "search_library": operation["completeness_ceiling"]
            },
            "max_response_bytes": 1_000_000,
            "max_records": 100,
            "timeout_ms": 5_000,
            "access_policy_ref": "policy:access:alphaengine",
            "retention_policy_ref": "policy:retention:licensed-research",
            "terms_policy_ref": "policy:terms:alphaengine",
            "network_policy": None,
        }
        self.profile = self.connectors.register_profile(
            profile_wire, idempotency_key="alphaengine:profile"
        )
        assert_wire_schema(
            unittest.TestCase(), "connector-profile-version.schema.json", self.profile
        )

        self.call_id = "connector-call:alphaengine:search:1"
        self.work = WorkOrder(
            schema_version="0.1",
            id="work:alphaengine-recorded:1",
            created_at=WHEN.isoformat(),
            updated_at=WHEN.isoformat(),
            question="Replay the frozen AlphaEngine search fixture",
            requested_capabilities=(capability_id,),
            runtime_profile_ref=profile_wire["runner_runtime_ref"],
            budget={"max_seconds": 30},
            idempotency_key="work:alphaengine-recorded:1",
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
        self.call = self.connectors.register_call_spec(
            {
                "schema_version": "0.1",
                "id": self.call_id,
                "created_at": WHEN.isoformat(),
                "work_order_ref": self.work.id,
                "work_order_hash": self.work_hash,
                "connector_profile_ref": self.profile["id"],
                "operation": "search_library",
                "parameters": self.plan["parent_parameters"],
                "query_hash": self.plan["parent_query_hash"],
            },
            idempotency_key="alphaengine:call",
        )
        execution = ExecutionInvocation(
            schema_version="0.1",
            id="connector-invocation:alphaengine:1",
            created_at=WHEN.isoformat(),
            kind=ExecutionKind.CONNECTOR,
            work_order_ref=self.work.id,
            profile_ref=self.profile["id"],
            capability=capability_id,
            input_refs=(self.call["id"],),
            output_refs=("artifact:alphaengine:raw:1",),
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
            idempotency_key="alphaengine:invocation",
        )
        price = self.connectors.register_price_rate(
            price_rate_spec(
                self.profile["id"],
                identifier="connector-price:alphaengine:zero:v1",
                rate_ref="connector-price:alphaengine:zero",
                meter="calls",
                unit_quantity=1,
                unit_price_micros=0,
            ),
            idempotency_key="alphaengine:price",
        )
        rate_spec = rate_policy_spec(
            self.profile["id"],
            identifier="connector-rate-policy:alphaengine:v1",
            price_rate_refs=(price["id"],),
            required_price_meters=("calls",),
            limits={
                "calls": 2,
                "bytes": 2_000_000,
                "records": 200,
                "cost_micros": 1000,
            },
        )
        rate_spec["policy_ref"] = "connector-rate-policy:alphaengine"
        rate_spec["quota_scope_ref"] = "connector-quota-scope:alphaengine"
        self.rate_policy = self.connectors.register_rate_policy(
            rate_spec, idempotency_key="alphaengine:rate-policy"
        )
        self.scheduler.enqueue(self.work)
        self.claim = self.scheduler.claim(
            self.profile["runner_actor_ref"], work_order_id=self.work.id
        )
        assert self.claim is not None
        self.credentials = CredentialAuthorityStore(
            self.core, handle_resolver=lambda grant: self.handle, clock=self.clock
        )
        grant_base = {
            "schema_version": "0.1",
            "id": "credential-grant:alphaengine:work-1",
            "created_at": WHEN.isoformat(timespec="microseconds"),
            "expires_at": (WHEN + timedelta(minutes=5)).isoformat(
                timespec="microseconds"
            ),
            "authority_ref": "credential-authority:host:alphaengine",
            "grant_kind": "mcp_managed",
            "target_ref": "mcp-target:alphaengine",
            "connector_profile_ref": self.profile["id"],
            "connector_profile_hash": self.profile["content_hash"],
            "capability_lease_ref": self.capability_lease.id,
            "capability_lease_hash": self.capability_lease.content_hash,
            "adapter_ref": self.profile["adapter_ref"],
            "adapter_hash": self.profile["adapter_hash"],
            "principal_ref": "principal:worker-1",
            "credential_slot_refs": ["credential-slot:alphaengine"],
            "allowed_operations": ["search_library"],
            "max_calls": 1,
        }
        self.grant = self.credentials.register_grant(
            {**grant_base, "content_hash": content_hash(grant_base)},
            idempotency_key="alphaengine:grant",
        )
        self.observability = ObservabilityStore(self.core)
        self.journal = RunnerJournal(self.core, clock=self.clock)
        self.spool = RawSpool(self.temp.name, max_total_bytes=2_000_000)
        self.gate = McpManagedRunnerAdmissionGate(
            scheduler=self.scheduler,
            catalog=self.catalog,
            connectors=self.connectors,
            resolver=self.resolver,
            visibility_scopes=["research"],
            clock=self.clock,
            credential_authority=self.credentials,
            transport_plans=[self.plan],
        )
        self.authority = ConnectorAuthorityPort(
            connectors=self.connectors,
            observability=self.observability,
            scheduler=self.scheduler,
        )
        self.revoked_at_transport = False

        def fault_hook(barrier: str) -> None:
            if revoke_after_authorize and barrier == "after_transport_started":
                self.credentials.revoke(
                    self.grant["id"],
                    grant_hash=self.grant["content_hash"],
                    reason_code="manual",
                    actor_ref="human:governance",
                    idempotency_key="alphaengine:revoke:transport",
                )
                self.revoked_at_transport = True
            if barrier == fault_at:
                raise SimulatedRunnerCrash(barrier)

        self.executor = ConnectorTransportExecutor(
            gate=self.gate,
            journal=self.journal,
            spool=self.spool,
            authority=self.authority,
            connector_reader=self.connectors,
            clock=self.clock,
            fault_hook=(
                fault_hook
                if fault_at is not None or revoke_after_authorize
                else None
            ),
        )

    @staticmethod
    def _validate_input(parameters: dict) -> bool:
        return parameters == build_recorded_mcp_shadow_plan("success")[
            "parent_parameters"
        ]

    def request(self) -> dict:
        base = {
            "schema_version": "0.2",
            "id": "connector-runner-request:alphaengine:1",
            "created_at": WHEN.isoformat(timespec="microseconds"),
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
            "transport_plan_ref": self.plan["id"],
            "transport_plan_hash": self.plan["content_hash"],
            "idempotency_key": "runner-request:alphaengine:1",
        }
        return with_hash(base)

    def execute(self) -> dict:
        return self.executor.execute(
            self.request(), scheduler_lease_token=self.claim["lease_token"]
        )

    def revoke(self, key: str = "alphaengine:revoke") -> dict:
        return self.credentials.revoke(
            self.grant["id"],
            grant_hash=self.grant["content_hash"],
            reason_code="manual",
            actor_ref="human:governance",
            idempotency_key=key,
        )

    def count(self, table: str, *, scheduler: bool = False) -> int:
        connection = self.scheduler.connection if scheduler else self.core.connection
        return int(connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])

    def close(self) -> None:
        self.temp.cleanup()
        self.catalog.close()
        self.scheduler.close()
        self.core.close()


class McpManagedShadowTests(unittest.TestCase):
    def harness(self, scenario: str, **kwargs) -> AlphaEngineHarness:
        harness = AlphaEngineHarness(scenario, **kwargs)
        self.addCleanup(harness.close)
        return harness

    def test_success_runs_full_chain_with_out_of_band_handle(self) -> None:
        harness = self.harness("success")
        assert_wire_schema(
            self, "recorded-mcp-shadow-plan.schema.json", harness.plan
        )
        response = harness.execute()
        self.assertEqual(response["outcome"], "succeeded")
        self.assertEqual(len(harness.invocations), 1)
        request, handle = harness.invocations[0]
        self.assertIs(handle, harness.handle)
        assert_wire_schema(self, "mcp-managed-adapter-request.schema.json", request)
        validate_mcp_managed_adapter_request(request)
        serialized = json.dumps(request, sort_keys=True).lower()
        for forbidden in (
            "allowed_hosts", "network_policy", "token", "cookie",
            "password", "secret", "127.0.0.1",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(harness.count("credential_use_receipts"), 1)
        self.assertEqual(harness.count("connector_quota_reservations"), 1)
        self.assertEqual(harness.count("connector_physical_attempts"), 1)
        self.assertEqual(harness.count("connector_usage_entries"), 1)
        self.assertEqual(harness.count("connector_cost_entries"), 1)
        self.assertEqual(harness.count("connector_quota_settlements"), 1)
        self.assertEqual(harness.count("observability_artifact_versions_v2"), 1)
        self.assertEqual(harness.count("connector_source_envelopes"), 1)
        self.assertEqual(harness.count("scheduler_formal_results", scheduler=True), 1)
        duplicate = harness.execute()
        self.assertEqual(duplicate["idempotency_status"], "duplicate")
        self.assertEqual(len(harness.invocations), 1)
        self.assertEqual(harness.count("credential_use_receipts"), 1)

    def test_empty_partial_and_pagination_preserve_source_semantics(self) -> None:
        expected = {
            "empty": ("empty", "ranked", None, 0),
            "partial": ("partial", "partial", None, 1),
            "pagination": ("partial", "partial", "synthetic-next", 1),
        }
        for scenario, values in expected.items():
            with self.subTest(scenario=scenario):
                harness = AlphaEngineHarness(scenario)
                try:
                    response = harness.execute()
                    self.assertEqual(response["outcome"], "succeeded")
                    envelope = json.loads(
                        harness.core.connection.execute(
                            "SELECT record_json FROM connector_source_envelopes"
                        ).fetchone()["record_json"]
                    )
                    self.assertEqual(
                        (
                            envelope["status"], envelope["completeness"],
                            envelope["cursor"], len(envelope["source_record_refs"]),
                        ),
                        values,
                    )
                finally:
                    harness.close()

    def test_failure_matrix_never_fabricates_source_or_artifact(self) -> None:
        for scenario in (
            "schema_drift", "rate_limited", "malformed", "permission_denied"
        ):
            with self.subTest(scenario=scenario):
                harness = AlphaEngineHarness(scenario)
                try:
                    response = harness.execute()
                    self.assertEqual(response["outcome"], "retryable")
                    self.assertIsNone(response["source_envelope_ref"])
                    self.assertIsNone(response["raw_artifact_version_ref"])
                    self.assertEqual(harness.count("connector_source_envelopes"), 0)
                    self.assertEqual(
                        harness.count("observability_artifact_versions_v2"), 0
                    )
                finally:
                    harness.close()

    def test_timeout_is_runner_owned_and_aborts_raw(self) -> None:
        harness = self.harness("timeout")
        response = harness.execute()
        self.assertEqual(response["outcome"], "retryable")
        self.assertEqual(harness.spool.total_bytes(), 0)
        attempt = harness.core.connection.execute(
            "SELECT outcome FROM connector_physical_attempts"
        ).fetchone()
        self.assertEqual(attempt["outcome"], "timeout")

    def test_revoked_before_admission_stops_before_quota_and_adapter(self) -> None:
        harness = self.harness("revoked")
        harness.revoke()
        with self.assertRaises(CredentialGrantRejected):
            harness.execute()
        self.assertEqual(harness.invocations, [])
        self.assertEqual(harness.count("credential_use_receipts"), 0)
        self.assertEqual(harness.count("connector_quota_reservations"), 0)

    def test_revocation_after_authorization_is_rechecked_before_adapter(self) -> None:
        harness = self.harness("success", revoke_after_authorize=True)
        response = harness.execute()
        self.assertTrue(harness.revoked_at_transport)
        self.assertEqual(response["outcome"], "retryable")
        self.assertEqual(harness.invocations, [])
        self.assertEqual(harness.count("credential_use_receipts"), 1)
        self.assertEqual(harness.count("connector_source_envelopes"), 0)
        attempt = harness.core.connection.execute(
            "SELECT outcome FROM connector_physical_attempts"
        ).fetchone()
        self.assertEqual(attempt["outcome"], "failed")

    def test_plan_query_profile_and_scenario_tampering_fail_closed(self) -> None:
        harness = self.harness("success")
        request = harness.request()
        for field, value in (
            ("transport_plan_hash", "0" * 64),
            ("transport_plan_ref", "recorded-mcp-plan:alphaengine:other:0.2"),
        ):
            changed = {**request, field: value}
            changed["content_hash"] = content_hash(
                {key: item for key, item in changed.items() if key != "content_hash"}
            )
            with self.subTest(field=field), self.assertRaises(RunnerConflict):
                harness.gate.validate(
                    changed, scheduler_lease_token=harness.claim["lease_token"]
                )
        changed_plan = copy.deepcopy(harness.plan)
        changed_plan["parent_parameters"]["query"] = "another company"
        changed_plan["parent_query_hash"] = content_hash(
            {
                "operation": changed_plan["operation"],
                "parameters": changed_plan["parent_parameters"],
            }
        )
        changed_plan["content_hash"] = content_hash(
            {
                key: value for key, value in changed_plan.items()
                if key != "content_hash"
            }
        )
        with self.assertRaises(RunnerConflict):
            validate_recorded_mcp_shadow_plan(changed_plan)

    def test_mcp_profile_cannot_smuggle_public_host_authority(self) -> None:
        harness = self.harness("success")
        base = {
            key: value
            for key, value in harness.profile.items()
            if key not in {"write_status", "content_hash"}
        }
        base.update(
            {
                "id": "connector-profile:alphaengine:v2",
                "version": 2,
                "prior_version_ref": harness.profile["id"],
            }
        )
        for field, value in (
            ("allowed_hosts", ["127.0.0.1"]),
            (
                "network_policy",
                {
                    "allowed_schemes": ["https"],
                    "allow_redirects": False,
                    "max_redirects": 0,
                    "resolve_public_only": True,
                },
            ),
        ):
            changed = copy.deepcopy(base)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(
                ConnectorValidationError
            ):
                harness.connectors.register_profile(
                    changed, idempotency_key=f"alphaengine:profile:hostile:{field}"
                )

    def test_hostile_extra_output_is_rejected_before_source_commit(self) -> None:
        class HostileAdapter:
            def __init__(self, inner):
                self.inner = inner

            def __call__(self, request, sink, handle):
                observation = self.inner(request, sink, handle)
                observation["structured_output"]["system_prompt"] = "forged"
                observation["content_hash"] = content_hash(
                    {
                        key: value for key, value in observation.items()
                        if key != "content_hash"
                    }
                )
                return validate_mcp_managed_transport_observation(observation)

        def factory(scenario, clock, probe):
            return HostileAdapter(
                RecordedAlphaEngineAdapter(
                    scenario, advance_clock=clock.advance, invocation_probe=probe
                )
            )

        harness = self.harness("success", adapter_factory=factory)
        response = harness.execute()
        self.assertEqual(response["outcome"], "retryable")
        self.assertEqual(harness.count("connector_source_envelopes"), 0)
        self.assertEqual(harness.count("observability_artifact_versions_v2"), 0)

    def test_adapter_request_cannot_rebind_plan_before_handle_resolution(self) -> None:
        harness = self.harness("success")
        admission = harness.gate.validate(
            harness.request(), scheduler_lease_token=harness.claim["lease_token"]
        )
        harness.gate.reserve_for_admission(
            admission,
            scheduler_lease_token=harness.claim["lease_token"],
            idempotency_key="alphaengine:hostile:reservation",
        )
        request = harness.gate.build_adapter_request(
            admission, scheduler_lease_token=harness.claim["lease_token"]
        )
        for field, value in (
            ("recorded_scenario_ref", "recorded-mcp-scenario:alphaengine:search-library:empty:0.2"),
            ("transport_target_ref", "mcp-target:guidepoint"),
            ("output_schema_hash", "0" * 64),
        ):
            changed = {**request, field: value}
            changed["content_hash"] = content_hash(
                {key: item for key, item in changed.items() if key != "content_hash"}
            )
            with self.subTest(field=field), self.assertRaises(RunnerConflict):
                harness.gate.credential_handle_for_use(changed)
        self.assertEqual(harness.invocations, [])

    def test_mcp_gate_rejects_unverified_compiled_plan_binding(self) -> None:
        harness = self.harness("success")
        changed = {
            **{
                key: value
                for key, value in harness.request().items()
                if key != "content_hash"
            },
            "compiled_connector_plan_ref": "compiled-connector-plan:forged",
            "compiled_connector_plan_hash": "8" * 64,
            "compiled_step_ref": "compiled-connector-step:forged",
            "compiled_step_hash": "9" * 64,
        }
        changed["content_hash"] = content_hash(changed)
        with self.assertRaises(RunnerConflict):
            harness.gate.validate(
                changed, scheduler_lease_token=harness.claim["lease_token"]
            )
        self.assertEqual(harness.invocations, [])

    def test_crash_barriers_do_not_replay_provider_calls(self) -> None:
        reserved = self.harness("success", fault_at="after_reserved")
        with self.assertRaises(SimulatedRunnerCrash):
            reserved.execute()
        recovered = reserved.executor.recover(reserved.request()["id"])
        self.assertEqual(recovered["state"], "released_recovered")
        self.assertEqual(reserved.invocations, [])
        self.assertEqual(reserved.count("credential_use_receipts"), 0)

        started = AlphaEngineHarness("success", fault_at="after_transport_started")
        try:
            with self.assertRaises(SimulatedRunnerCrash):
                started.execute()
            recovered = started.executor.recover(started.request()["id"])
            self.assertEqual(recovered["state"], "indeterminate_recovered")
            self.assertEqual(started.invocations, [])
            self.assertEqual(started.count("credential_use_receipts"), 1)
        finally:
            started.close()

    def test_no_research_ledger_facts_are_written(self) -> None:
        harness = self.harness("success")
        before = {
            table: harness.count(table)
            for table in ("evidence_versions", "claim_versions", "thesis_versions")
        }
        harness.execute()
        after = {
            table: harness.count(table)
            for table in ("evidence_versions", "claim_versions", "thesis_versions")
        }
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
