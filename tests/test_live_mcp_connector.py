from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dalton_core.capability_catalog import CapabilityCatalog
from dalton_core.connector import ConnectorStore
from dalton_core.connector_authority_port import ConnectorAuthorityPort
from dalton_core.connector_inventory import load_packaged_connector_inventory
from dalton_core.connector_quota_policy import (
    apply_governed_quota_to_limits,
    governed_daily_quota,
)
from dalton_core.connector_runner import (
    RunnerConflict,
    RunnerValidationError,
    StaticAdapterResolver,
    validate_runner_environment_manifest,
)
from dalton_core.connector_transport_executor import (
    ConnectorTransportExecutor,
    SimulatedRunnerCrash,
)
from dalton_core.contracts import ExecutionInvocation, ExecutionKind, WorkOrder
from dalton_core.credential_authority import CredentialAuthorityStore
from dalton_core.live_mcp_connector import (
    AlphaEngineLiveAdapter,
    LiveMcpRunnerAdmissionGate,
    OPENCLAW_ALPHAENGINE_BRIDGE_HASH,
    alphaengine_tool_arguments,
    build_live_mcp_transport_plan,
    validate_alphaengine_document_page,
    validate_live_mcp_adapter_request,
    validate_live_mcp_transport_plan,
)
from dalton_core.mcp_managed_runner import validate_mcp_schema_instance
from dalton_core.observability import ObservabilityStore
from dalton_core.openclaw_connector_bridge import (
    BridgeRequestRejected,
    HostToolInvocationResult,
    LoopbackStreamableHttpMcpHandle,
)
from dalton_core.research_context import build_compiled_connector_plan
from dalton_core.raw_spool import RawSpool
from dalton_core.runner_journal import RunnerJournal
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, canonical_json, content_hash
from tests.test_capability_catalog import FakeAuthorities, descriptor_spec
from tests.test_connector import (
    MutableClock,
    WHEN,
    assert_wire_schema,
    price_rate_spec,
    rate_policy_spec,
)


FUTURE = "2030-01-01T00:00:00.000000+00:00"


def with_hash(wire: dict) -> dict:
    return {**wire, "content_hash": content_hash(wire)}


def operation_spec(operation: str, parameters: dict) -> tuple[dict, dict]:
    template = load_packaged_connector_inventory()["templates"]["alphaengine"]
    contract = next(
        item for item in template["operations"] if item["operation"] == operation
    )
    profile_ref = f"connector-profile:alphaengine-live-{operation}:v1"
    profile_hash = content_hash({"profile_ref": profile_ref})
    compiled = build_compiled_connector_plan(
        task_ref=f"work:alphaengine-live-{operation}:1",
        task_hash=content_hash({"work": operation}),
        planner_ref="planner:alphaengine-live-canary:0.1",
        planner_hash=content_hash({"planner": "alphaengine-live-canary:0.1"}),
        routing_policy_ref="routing:alphaengine-live-canary:0.1",
        routing_policy_hash=content_hash({"routing": operation}),
        step_specs=[
            {
                "source_ref": template["source_identity"]["source_ref"],
                "source_hash": content_hash(template["source_identity"]),
                "connector_profile_ref": profile_ref,
                "connector_profile_hash": profile_hash,
                "operation": operation,
                "parameters": parameters,
                "input_schema_ref": contract["input_schema_ref"],
                "input_schema_hash": contract["input_schema_hash"],
                "output_schema_ref": contract["output_schema_ref"],
                "output_schema_hash": contract["output_schema_hash"],
                "completeness_required": contract["completeness_ceiling"],
                "depends_on": [],
                "fallback_step_refs": [],
                "max_attempts": 1,
            }
        ],
        created_at="2026-08-23T07:00:00.000000+00:00",
    )
    return compiled, compiled["steps"][0]


def search_parameters() -> dict:
    return {
        "query": "Accenture IT services AI demand and delivery margins",
        "filters": {
            "company": "Accenture",
            "date_from": "2026-01-01",
            "date_to": "2026-08-23",
            "document_type": "foreign_report",
            "geography": "US",
        },
    }


def document_parameters(cursor: str | None = None) -> dict:
    parameters = {"document_ref": "alphaengine-doc:320000610033807"}
    if cursor is not None:
        parameters["cursor"] = cursor
    return parameters


def adapter_request(operation: str, parameters: dict) -> tuple[dict, dict]:
    compiled, step = operation_spec(operation, parameters)
    plan = build_live_mcp_transport_plan(compiled, step)
    template = load_packaged_connector_inventory()["templates"]["alphaengine"]
    contract = next(
        item for item in template["operations"] if item["operation"] == operation
    )
    base = {
        "protocol_version": "0.3",
        "runner_request_ref": f"connector-runner-request:alphaengine-live-{operation}:1",
        "runner_request_hash": "1" * 64,
        "connector_invocation_ref": f"connector-invocation:alphaengine-live-{operation}:1",
        "connector_invocation_hash": "2" * 64,
        "profile_ref": step["connector_profile_ref"],
        "profile_hash": step["connector_profile_hash"],
        "call_spec_ref": f"connector-call:alphaengine-live-{operation}:1",
        "call_spec_hash": "3" * 64,
        "capability_lease_ref": f"capability-lease:alphaengine-live-{operation}:1",
        "capability_lease_hash": "4" * 64,
        "principal_ref": "principal:alphaengine-live-canary",
        "reservation_ref": f"connector-reservation:alphaengine-live-{operation}:1",
        "reservation_hash": "5" * 64,
        "physical_attempt_number": 1,
        "source_identity": template["source_identity"],
        "source_hash": content_hash(template["source_identity"]),
        "adapter_ref": "mcp-target:alphaengine",
        "adapter_hash": "6" * 64,
        "resolver_ref": "resolver:alphaengine-live:0.1",
        "resolver_manifest_hash": "7" * 64,
        "transport_target_ref": plan["transport_target_ref"],
        "transport_target_hash": plan["transport_target_hash"],
        "transport_plan_ref": plan["id"],
        "transport_plan_hash": plan["content_hash"],
        "bridge_ref": plan["bridge_ref"],
        "bridge_hash": plan["bridge_hash"],
        "compiled_connector_plan_ref": compiled["id"],
        "compiled_connector_plan_hash": compiled["content_hash"],
        "compiled_step_ref": step["id"],
        "compiled_step_hash": step["content_hash"],
        "credential_grant_ref": f"credential-grant:alphaengine-live-{operation}:1",
        "credential_grant_hash": "8" * 64,
        "credential_use_ref": f"credential-use:alphaengine-live-{operation}:1",
        "credential_use_hash": "9" * 64,
        "operation": operation,
        "tool_name": operation,
        "parameters": parameters,
        "query_hash": content_hash({"operation": operation, "parameters": parameters}),
        "input_schema_ref": contract["input_schema_ref"],
        "input_schema_hash": contract["input_schema_hash"],
        "output_schema_ref": contract["output_schema_ref"],
        "output_schema_hash": contract["output_schema_hash"],
        "deadline_at": FUTURE,
        "max_response_bytes": 1_000_000,
        "max_records": 10,
        "raw_sink_ref": "raw-sink:" + "a" * 64,
    }
    wire = validate_live_mcp_adapter_request(
        {**base, "content_hash": content_hash(base)}
    )
    return wire, plan


class MemorySink:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, value: bytes) -> None:
        self.data.extend(value)


class FakeHandle:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    def invoke(self, tool_name, arguments, **kwargs):
        del kwargs
        self.calls.append((tool_name, dict(arguments)))
        raw = canonical_json(
            {"jsonrpc": "2.0", "id": "provider-request:1", "result": self.result}
        ).encode("utf-8")
        return HostToolInvocationResult(
            request_id="provider-request:1", raw_response=raw, result=self.result
        )


def tool_result(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": canonical_json(payload)}]}


def live_permissions() -> dict:
    return {
        "risk_class": "low",
        "network": False,
        "filesystem_read": [],
        "filesystem_write": ["runner:raw-sink"],
        "credential_slot_refs": ["credential-slot:alphaengine"],
        "core_db": False,
        "side_effects": ["read:alphaengine-library"],
    }


class LiveGateHarness:
    def __init__(
        self,
        operation: str,
        parameters: dict,
        result: dict,
        *,
        fault_at: str | None = None,
    ) -> None:
        self.operation = operation
        self.parameters = parameters
        self.clock = MutableClock()
        self.core = DaltonStore(":memory:")
        self.connectors = ConnectorStore(self.core, clock=self.clock)
        self.scheduler = Scheduler(
            ":memory:", clock=self.clock, default_lease_seconds=30,
            max_lease_seconds=60,
        )
        self.authorities = FakeAuthorities()
        self.authorities.policy_permissions = live_permissions()
        self.catalog = CapabilityCatalog(
            ":memory:", clock=self.clock,
            approval_resolver=self.authorities.approval,
            policy_resolver=self.authorities.policy,
        )
        self.temp = tempfile.TemporaryDirectory()
        self.template = load_packaged_connector_inventory()["templates"]["alphaengine"]
        self.contract = next(
            item for item in self.template["operations"]
            if item["operation"] == operation
        )
        self.handle = FakeHandle(result)
        self.adapter = AlphaEngineLiveAdapter()
        self.capability_id = f"capability:dalton:connector:alphaengine-live-{operation}"
        self.adapter_hash = content_hash(
            {
                "target_ref": "mcp-target:alphaengine",
                "package": "openclaw-alphaengine-live-adapter:0.1",
                "bridge_hash": OPENCLAW_ALPHAENGINE_BRIDGE_HASH,
                "operation": operation,
            }
        )
        schema_hash = content_hash(
            {
                "allowed_operations": [operation],
                "input_schema_refs": {operation: self.contract["input_schema_ref"]},
                "input_schema_hashes": {operation: self.contract["input_schema_hash"]},
                "output_schema_refs": {operation: self.contract["output_schema_ref"]},
                "output_schema_hashes": {operation: self.contract["output_schema_hash"]},
            }
        )
        descriptor_wire = descriptor_spec(
            self.capability_id,
            name=f"alphaengine-live-{operation}",
            side_effects=["read:alphaengine-library"],
        )
        descriptor_wire.update(
            {
                "kind": "connector",
                "label": f"Live AlphaEngine {operation}",
                "summary": "Execute one compiled, read-only AlphaEngine operation",
                "contract": {
                    "mode": "typed_call",
                    "input_schema_ref": self.contract["input_schema_ref"],
                    "output_schema_ref": self.contract["output_schema_ref"],
                    "instruction_ref": None,
                    "adapter_ref": "mcp-target:alphaengine",
                },
                "permissions": live_permissions(),
                "source_hash": content_hash(self.template["source_identity"]),
                "schema_hash": schema_hash,
            }
        )
        self.descriptor = self.catalog.publish(descriptor_wire)
        self.binding = {
            "binding_ref": f"runner-binding:alphaengine-live-{operation}:0.1",
            "descriptor_revision_ref": self.descriptor.revision_ref,
            "descriptor_hash": self.descriptor.content_hash,
            "adapter_ref": "mcp-target:alphaengine",
            "adapter_hash": self.adapter_hash,
            "source_ref": "source:alphaengine",
            "source_hash": content_hash(self.template["source_identity"]),
            "operation": operation,
            "input_schema_ref": self.contract["input_schema_ref"],
            "input_schema_hash": self.contract["input_schema_hash"],
            "output_schema_ref": self.contract["output_schema_ref"],
            "output_schema_hash": self.contract["output_schema_hash"],
            "auth_mode": "mcp_managed",
            "credential_slot_refs": ["credential-slot:alphaengine"],
            "required_permissions": live_permissions(),
            "side_effects": ["read:alphaengine-library"],
            "rate_policy_ref": f"connector-rate-policy:alphaengine-live-{operation}",
        }
        manifest_base = {
            "schema_version": "0.1",
            "id": f"runner-environment:alphaengine-live-{operation}:0.1",
            "created_at": WHEN.isoformat(timespec="microseconds"),
            "runner_runtime_ref": "runtime:openclaw-mcp-bridge:0.1",
            "runner_actor_ref": "runner:openclaw-mcp-bridge",
            "resolver_ref": "resolver:openclaw-mcp-static:0.1",
            "resolver_version": "0.1",
            "package_manifest_ref": "artifact:runner-packages:alphaengine-live:0.1",
            "package_manifest_hash": "a" * 64,
            "bindings": [self.binding],
        }
        self.manifest = validate_runner_environment_manifest(with_hash(manifest_base))
        self.resolver = StaticAdapterResolver(
            self.manifest,
            {self.binding["binding_ref"]: self.adapter},
            {self.binding["binding_ref"]: lambda value: value == parameters},
        )
        profile_wire = {
            "schema_version": "0.1",
            "id": f"connector-profile:alphaengine-live-{operation}:v1",
            "created_at": WHEN.isoformat(timespec="microseconds"),
            "connector_ref": self.template["connector_ref"],
            "version": 1,
            "prior_version_ref": None,
            "capability_id": self.capability_id,
            "descriptor_revision_ref": self.descriptor.revision_ref,
            "descriptor_hash": self.descriptor.content_hash,
            "source_identity": self.template["source_identity"],
            "source_hash": content_hash(self.template["source_identity"]),
            "schema_hash": schema_hash,
            "catalog_epoch": self.descriptor.catalog_epoch,
            "adapter_ref": "mcp-target:alphaengine",
            "adapter_hash": self.adapter_hash,
            "runner_runtime_ref": manifest_base["runner_runtime_ref"],
            "runner_actor_ref": manifest_base["runner_actor_ref"],
            "runner_environment_hash": self.manifest["content_hash"],
            "allowed_operations": [operation],
            "allowed_hosts": [],
            "auth_mode": "mcp_managed",
            "credential_slot_refs": ["credential-slot:alphaengine"],
            "input_schema_refs": {operation: self.contract["input_schema_ref"]},
            "input_schema_hashes": {operation: self.contract["input_schema_hash"]},
            "output_schema_refs": {operation: self.contract["output_schema_ref"]},
            "output_schema_hashes": {operation: self.contract["output_schema_hash"]},
            "pagination": {
                "mode": self.contract["pagination"]["mode"],
                "cursor_field": self.contract["pagination"]["cursor_field"],
                "max_pages": self.contract["pagination"]["max_pages"],
            },
            "completeness": {operation: self.contract["completeness_ceiling"]},
            "max_response_bytes": 1_000_000,
            "max_records": 10,
            "timeout_ms": 5_000,
            "access_policy_ref": "policy:access:alphaengine",
            "retention_policy_ref": "policy:retention:licensed-research",
            "terms_policy_ref": "policy:terms:alphaengine",
            "network_policy": None,
        }
        self.profile = self.connectors.register_profile(
            profile_wire, idempotency_key=f"alphaengine-live:{operation}:profile"
        )
        self.call_id = f"connector-call:alphaengine-live-{operation}:1"
        self.work = WorkOrder(
            schema_version="0.1",
            id=f"work:alphaengine-live-{operation}:1",
            created_at=WHEN.isoformat(),
            updated_at=WHEN.isoformat(),
            question=f"Run one live AlphaEngine {operation} canary",
            requested_capabilities=(self.capability_id,),
            runtime_profile_ref=profile_wire["runner_runtime_ref"],
            budget={"max_seconds": 30},
            idempotency_key=f"work:alphaengine-live-{operation}:1",
            declared_side_effects=("read:alphaengine-library",),
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
                "operation": operation,
                "parameters": parameters,
                "query_hash": content_hash(
                    {"operation": operation, "parameters": parameters}
                ),
            },
            idempotency_key=f"alphaengine-live:{operation}:call",
        )
        execution = ExecutionInvocation(
            schema_version="0.1",
            id=f"connector-invocation:alphaengine-live-{operation}:1",
            created_at=WHEN.isoformat(),
            kind=ExecutionKind.CONNECTOR,
            work_order_ref=self.work.id,
            profile_ref=self.profile["id"],
            capability=self.capability_id,
            input_refs=(self.call["id"],),
            output_refs=(f"artifact:alphaengine-live-{operation}:raw:1",),
            started_at=WHEN.isoformat(),
            completed_at=None,
            side_effects=(),
            runtime_ref=self.profile["runner_runtime_ref"],
            actor_ref=self.profile["runner_actor_ref"],
            environment_hash=self.profile["runner_environment_hash"],
        )
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
            idempotency_key=f"alphaengine-live:{operation}:invocation",
        )
        self.compiled = build_compiled_connector_plan(
            task_ref=self.work.id,
            task_hash=self.work_hash,
            planner_ref="planner:alphaengine-live-canary:0.1",
            planner_hash=content_hash({"planner": "alphaengine-live-canary:0.1"}),
            routing_policy_ref="routing:alphaengine-live-canary:0.1",
            routing_policy_hash=content_hash({"routing": operation}),
            step_specs=[
                {
                    "source_ref": self.profile["source_identity"]["source_ref"],
                    "source_hash": self.profile["source_hash"],
                    "connector_profile_ref": self.profile["id"],
                    "connector_profile_hash": self.profile["content_hash"],
                    "operation": operation,
                    "parameters": parameters,
                    "input_schema_ref": self.contract["input_schema_ref"],
                    "input_schema_hash": self.contract["input_schema_hash"],
                    "output_schema_ref": self.contract["output_schema_ref"],
                    "output_schema_hash": self.contract["output_schema_hash"],
                    "completeness_required": self.contract["completeness_ceiling"],
                    "depends_on": [],
                    "fallback_step_refs": [],
                    "max_attempts": 1,
                }
            ],
            created_at=WHEN.isoformat(),
        )
        self.step = self.compiled["steps"][0]
        self.transport = build_live_mcp_transport_plan(self.compiled, self.step)
        price = self.connectors.register_price_rate(
            price_rate_spec(
                self.profile["id"], meter="calls", unit_price_micros=0,
                identifier=f"connector-price:alphaengine-live-{operation}:calls:v1",
                rate_ref=f"connector-price-rate:alphaengine-live-{operation}:calls",
                unit_quantity=1,
            ),
            idempotency_key=f"alphaengine-live:{operation}:price",
        )
        quota = governed_daily_quota("alphaengine", operation)
        rate = rate_policy_spec(
            self.profile["id"],
            identifier=f"connector-rate-policy:alphaengine-live-{operation}:v1",
            price_rate_refs=(price["id"],),
            required_price_meters=("calls",),
            limits=apply_governed_quota_to_limits(
                quota,
                max_response_bytes=self.profile["max_response_bytes"],
                max_records=self.profile["max_records"],
            ),
            window_seconds=quota["window_seconds"],
            reset_timezone=quota["reset_timezone"],
        )
        rate["policy_ref"] = self.binding["rate_policy_ref"]
        rate["quota_scope_ref"] = f"connector-quota-scope:alphaengine-live-{operation}"
        self.rate_policy = self.connectors.register_rate_policy(
            rate, idempotency_key=f"alphaengine-live:{operation}:rate"
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
            "id": f"credential-grant:alphaengine-live-{operation}:1",
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
            "allowed_operations": [operation],
            "max_calls": 1,
        }
        self.credentials.register_grant(
            with_hash(grant_base),
            idempotency_key=f"alphaengine-live:{operation}:grant",
        )
        self.observability = ObservabilityStore(self.core)
        self.journal = RunnerJournal(self.core, clock=self.clock)
        self.spool = RawSpool(self.temp.name, max_total_bytes=2_000_000)
        self.gate = LiveMcpRunnerAdmissionGate(
            scheduler=self.scheduler,
            catalog=self.catalog,
            connectors=self.connectors,
            resolver=self.resolver,
            visibility_scopes=["research"],
            clock=self.clock,
            credential_authority=self.credentials,
            transport_plans=[self.transport],
            compiled_plans=[self.compiled],
        )
        self.authority = ConnectorAuthorityPort(
            connectors=self.connectors,
            observability=self.observability,
            scheduler=self.scheduler,
        )
        self.executor = ConnectorTransportExecutor(
            gate=self.gate,
            journal=self.journal,
            spool=self.spool,
            authority=self.authority,
            connector_reader=self.connectors,
            clock=self.clock,
            fault_hook=(
                None if fault_at is None
                else lambda barrier: (
                    (_ for _ in ()).throw(SimulatedRunnerCrash(barrier))
                    if barrier == fault_at else None
                )
            ),
        )

    def request(self) -> dict:
        base = {
            "schema_version": "0.2",
            "id": f"connector-runner-request:alphaengine-live-{self.operation}:1",
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
            "transport_plan_ref": self.transport["id"],
            "transport_plan_hash": self.transport["content_hash"],
            "compiled_connector_plan_ref": self.compiled["id"],
            "compiled_connector_plan_hash": self.compiled["content_hash"],
            "compiled_step_ref": self.step["id"],
            "compiled_step_hash": self.step["content_hash"],
            "idempotency_key": f"runner-request:alphaengine-live-{self.operation}:1",
        }
        return with_hash(base)

    def execute(self) -> dict:
        return self.executor.execute(
            self.request(), scheduler_lease_token=self.claim["lease_token"]
        )

    def count(self, table: str, *, scheduler: bool = False) -> int:
        connection = self.scheduler.connection if scheduler else self.core.connection
        return int(connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])

    def close(self) -> None:
        self.temp.cleanup()
        self.catalog.close()
        self.scheduler.close()
        self.core.close()


class LiveMcpContractTests(unittest.TestCase):
    def test_transport_plan_binds_compiled_step_inventory_and_bridge(self) -> None:
        compiled, step = operation_spec("search_library", search_parameters())
        plan = build_live_mcp_transport_plan(compiled, step)
        assert_wire_schema(self, "live-mcp-transport-plan.schema.json", plan)
        self.assertEqual(plan["bridge_hash"], OPENCLAW_ALPHAENGINE_BRIDGE_HASH)
        self.assertEqual(plan["compiled_step_hash"], step["content_hash"])
        changed = copy.deepcopy(plan)
        changed["tool_name"] = "get_document"
        changed["content_hash"] = content_hash(
            {key: value for key, value in changed.items() if key != "content_hash"}
        )
        with self.assertRaises(RunnerConflict):
            validate_live_mcp_transport_plan(changed)

    def test_transport_plan_rejects_parameters_outside_inventory_schema(self) -> None:
        parameters = search_parameters()
        parameters["unexpected"] = True
        compiled, step = operation_spec("search_library", parameters)
        with self.assertRaises(RunnerValidationError):
            build_live_mcp_transport_plan(compiled, step)

    def test_live_adapter_request_schema_and_bridge_are_closed(self) -> None:
        request, _ = adapter_request("search_library", search_parameters())
        assert_wire_schema(self, "live-mcp-adapter-request.schema.json", request)
        serialized = canonical_json(request).lower()
        for forbidden in (
            "127.0.0.1", "localhost", "token", "cookie", "password",
            "oauth", "server_config", "allowed_hosts",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_live_schema_validator_enforces_live_debt_keywords(self) -> None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "score", "date"],
            "properties": {
                "status": {"type": "string", "enum": ["ok"], "maxLength": 2},
                "score": {"type": "integer", "maximum": 3},
                "date": {"type": "string", "format": "date"},
            },
        }
        validate_mcp_schema_instance(
            {"status": "ok", "score": 3, "date": "2026-08-23"}, schema
        )
        for hostile in (
            {"status": "no", "score": 3, "date": "2026-08-23"},
            {"status": "ok", "score": 4, "date": "2026-08-23"},
            {"status": "ok", "score": 3, "date": "2026-02-30"},
        ):
            with self.subTest(hostile=hostile), self.assertRaises(RunnerValidationError):
                validate_mcp_schema_instance(hostile, schema)


class AlphaEngineLiveAdapterTests(unittest.TestCase):
    def test_search_normalizes_doc_ids_and_preserves_exact_raw_rpc(self) -> None:
        request, _ = adapter_request("search_library", search_parameters())
        payload = {
            "query": "Accenture",
            "filters": {},
            "total_upstream": 2,
            "returned": 2,
            "results": [
                {"doc_id": "doc-1", "title": "One"},
                {"doc_id": "doc-2", "title": "Two"},
            ],
            "cursor": "ae1:next",
            "has_more": True,
        }
        handle = FakeHandle(tool_result(payload))
        sink = MemorySink()
        observation = AlphaEngineLiveAdapter()(request, sink, handle)
        self.assertEqual(observation["outcome"], "succeeded")
        self.assertEqual(
            observation["source_record_refs"],
            ["alphaengine-doc:doc-1", "alphaengine-doc:doc-2"],
        )
        self.assertEqual(observation["source_status"], "partial")
        self.assertEqual(observation["completeness"], "ranked")
        self.assertTrue(sink.data)
        tool_name, arguments = handle.calls[0]
        self.assertEqual(tool_name, "search_library")
        self.assertEqual(arguments["markets"], ["US"])
        self.assertEqual(arguments["document_categories"], ["foreign_report"])

    def test_get_document_binds_doc_hash_and_next_offset(self) -> None:
        request, _ = adapter_request("get_document", document_parameters())
        payload = {
            "metadata": {"doc_id": "320000610033807", "title": "ACN"},
            "content_chars": 100,
            "content_sha256": "b" * 64,
            "offset": 0,
            "returned_chars": len("evidence text"),
            "text": "evidence text",
            "next_offset": len("evidence text"),
            "complete": False,
        }
        handle = FakeHandle(tool_result(payload))
        observation = AlphaEngineLiveAdapter()(request, MemorySink(), handle)
        self.assertEqual(
            observation["source_record_refs"],
            ["alphaengine-doc:320000610033807:sha256:" + "b" * 64],
        )
        self.assertEqual(observation["cursor"], str(len("evidence text")))
        self.assertEqual(observation["completeness"], "partial")
        _, arguments = handle.calls[0]
        self.assertEqual(arguments["doc_id"], "320000610033807")
        self.assertEqual(arguments["offset"], 0)

    def test_document_page_rejects_non_contiguous_offsets_and_lengths(self) -> None:
        base = {
            "metadata": {"doc_id": "doc-1"},
            "content_chars": 10,
            "content_sha256": "a" * 64,
            "offset": 0,
            "returned_chars": 5,
            "text": "12345",
            "next_offset": 5,
            "complete": False,
        }
        page = validate_alphaengine_document_page(
            base, expected_doc_id="doc-1", expected_offset=0, max_chars=5
        )
        self.assertEqual(page["cursor"], "5")
        hostile = (
            {**base, "offset": 1},
            {**base, "returned_chars": 4},
            {**base, "next_offset": 6},
            {**base, "complete": True, "next_offset": None},
        )
        for payload in hostile:
            with self.subTest(payload=payload), self.assertRaises(
                (RunnerConflict, RunnerValidationError)
            ):
                validate_alphaengine_document_page(
                    payload,
                    expected_doc_id="doc-1",
                    expected_offset=0,
                    max_chars=5,
                )

    def test_document_page_normalizes_false_terminal_flag_at_exact_end(self) -> None:
        page = validate_alphaengine_document_page(
            {
                "metadata": {"doc_id": "doc-1"},
                "content_chars": 10,
                "content_sha256": "a" * 64,
                "offset": 5,
                "returned_chars": 5,
                "text": "67890",
                "next_offset": None,
                "complete": False,
            },
            expected_doc_id="doc-1",
            expected_offset=5,
            max_chars=5,
        )
        self.assertTrue(page["complete"])
        self.assertIsNone(page["cursor"])
        self.assertEqual(page["completeness"], "enumerated")

    def test_tool_error_does_not_write_raw_or_claim_success(self) -> None:
        request, _ = adapter_request("search_library", search_parameters())
        handle = FakeHandle(
            {"isError": True, "content": [{"type": "text", "text": "permission denied"}]}
        )
        sink = MemorySink()
        observation = AlphaEngineLiveAdapter()(request, sink, handle)
        self.assertEqual(observation["outcome"], "failed")
        self.assertEqual(observation["error"]["code"], "permission_denied")
        self.assertEqual(sink.data, b"")

    def test_hostile_cursor_and_document_identity_fail_closed(self) -> None:
        request, _ = adapter_request("search_library", search_parameters())
        handle = FakeHandle(
            tool_result({"results": [], "cursor": "next", "has_more": False})
        )
        with self.assertRaises(RunnerValidationError):
            AlphaEngineLiveAdapter()(request, MemorySink(), handle)

        request, _ = adapter_request("get_document", document_parameters())
        handle = FakeHandle(
            tool_result(
                {
                    "metadata": {"doc_id": "another-doc"},
                    "content_chars": 6,
                    "content_sha256": "c" * 64,
                    "offset": 0,
                    "returned_chars": 6,
                    "text": "forged",
                    "next_offset": None,
                    "complete": True,
                }
            )
        )
        with self.assertRaises(RunnerConflict):
            AlphaEngineLiveAdapter()(request, MemorySink(), handle)


class LiveMcpEndToEndTests(unittest.TestCase):
    def harness(self, operation: str, parameters: dict, payload: dict, **kwargs):
        harness = LiveGateHarness(
            operation, parameters, tool_result(payload), **kwargs
        )
        self.addCleanup(harness.close)
        return harness

    def test_search_runs_full_authority_chain_and_duplicate_is_free(self) -> None:
        harness = self.harness(
            "search_library",
            search_parameters(),
            {
                "results": [{"doc_id": "doc-1", "title": "ACN"}],
                "cursor": None,
                "has_more": False,
            },
        )
        response = harness.execute()
        self.assertEqual(response["outcome"], "succeeded")
        self.assertEqual(len(harness.handle.calls), 1)
        self.assertEqual(harness.count("credential_use_receipts"), 1)
        self.assertEqual(harness.count("connector_physical_attempts"), 1)
        self.assertEqual(harness.count("observability_artifact_versions_v2"), 1)
        self.assertEqual(harness.count("connector_source_envelopes"), 1)
        source = json.loads(
            harness.core.connection.execute(
                "SELECT record_json FROM connector_source_envelopes"
            ).fetchone()["record_json"]
        )
        self.assertEqual(source["source_record_refs"], ["alphaengine-doc:doc-1"])
        self.assertEqual(source["status"], "complete")
        self.assertEqual(source["completeness"], "ranked")
        duplicate = harness.execute()
        self.assertEqual(duplicate["idempotency_status"], "duplicate")
        self.assertEqual(len(harness.handle.calls), 1)

    def test_live_alphaengine_operations_use_beijing_daily_unit_ceilings(self) -> None:
        for operation, parameters, payload, expected_calls, expected_units in (
            (
                "search_library", search_parameters(),
                {"results": [], "cursor": None, "has_more": False}, 50, 500,
            ),
            (
                "get_document", document_parameters(),
                {
                    "metadata": {"doc_id": "320000610033807"},
                    "content_chars": 0,
                    "content_sha256": "e" * 64,
                    "offset": 0,
                    "returned_chars": 0,
                    "text": "",
                    "next_offset": None,
                    "complete": True,
                },
                1_600, 80,
            ),
        ):
            with self.subTest(operation=operation):
                harness = self.harness(operation, parameters, payload)
                self.assertEqual(harness.rate_policy["limits"]["calls"], expected_calls)
                self.assertEqual(
                    harness.rate_policy["limits"]["records"], expected_units
                )
                self.assertEqual(harness.rate_policy["window_seconds"], 86_400)
                self.assertEqual(
                    harness.rate_policy["reset_timezone"], "Asia/Shanghai"
                )

    def test_get_document_charges_one_document_unit_only_on_first_page(self) -> None:
        cases = (
            (document_parameters(), 0, 0, 1),
            (document_parameters("12"), 12, 12, 0),
        )
        for parameters, offset, content_chars, expected_document_units in cases:
            with self.subTest(cursor=parameters.get("cursor")):
                harness = self.harness(
                    "get_document",
                    parameters,
                    {
                        "metadata": {"doc_id": "320000610033807"},
                        "content_chars": content_chars,
                        "content_sha256": "e" * 64,
                        "offset": offset,
                        "returned_chars": 0,
                        "text": "",
                        "next_offset": None,
                        "complete": True,
                    },
                )
                response = harness.execute()
                self.assertEqual(response["outcome"], "succeeded")
                reservation = json.loads(
                    harness.core.connection.execute(
                        "SELECT record_json FROM connector_quota_reservations"
                    ).fetchone()["record_json"]
                )
                usage = json.loads(
                    harness.core.connection.execute(
                        "SELECT record_json FROM connector_usage_entries"
                    ).fetchone()["record_json"]
                )
                self.assertEqual(reservation["reserved"]["calls"], 1)
                self.assertEqual(usage["metrics"]["calls"], 1)
                self.assertEqual(
                    reservation["reserved"]["records"], expected_document_units
                )
                self.assertEqual(
                    usage["metrics"]["records"], expected_document_units
                )

    def test_get_document_commits_hash_bound_raw_artifact(self) -> None:
        digest = "d" * 64
        harness = self.harness(
            "get_document",
            document_parameters(),
            {
                "metadata": {"doc_id": "320000610033807", "title": "ACN"},
                "content_chars": len("source document body"),
                "content_sha256": digest,
                "offset": 0,
                "returned_chars": len("source document body"),
                "text": "source document body",
                "next_offset": None,
                "complete": True,
            },
        )
        response = harness.execute()
        self.assertEqual(response["outcome"], "succeeded")
        source = json.loads(
            harness.core.connection.execute(
                "SELECT record_json FROM connector_source_envelopes"
            ).fetchone()["record_json"]
        )
        self.assertEqual(
            source["source_record_refs"],
            ["alphaengine-doc:320000610033807:sha256:" + digest],
        )
        self.assertEqual(source["completeness"], "enumerated")
        self.assertEqual(harness.count("evidence_versions"), 0)
        self.assertEqual(harness.count("claim_versions"), 0)
        self.assertEqual(harness.count("thesis_versions"), 0)

    def test_compiled_plan_tampering_stops_before_quota_and_tool(self) -> None:
        harness = self.harness(
            "search_library",
            search_parameters(),
            {"results": [], "cursor": None, "has_more": False},
        )
        request = harness.request()
        request["compiled_step_hash"] = "0" * 64
        request["content_hash"] = content_hash(
            {key: value for key, value in request.items() if key != "content_hash"}
        )
        with self.assertRaises(RunnerConflict):
            harness.executor.execute(
                request, scheduler_lease_token=harness.claim["lease_token"]
            )
        self.assertEqual(harness.handle.calls, [])
        self.assertEqual(harness.count("connector_quota_reservations"), 0)

    def test_after_observed_crash_recovery_does_not_repeat_tool_call(self) -> None:
        harness = self.harness(
            "search_library",
            search_parameters(),
            {"results": [], "cursor": None, "has_more": False},
            fault_at="after_observed",
        )
        with self.assertRaises(SimulatedRunnerCrash):
            harness.execute()
        self.assertEqual(len(harness.handle.calls), 1)
        recovered = harness.executor.recover(
            harness.request()["id"],
            scheduler_lease_token=harness.claim["lease_token"],
        )
        self.assertEqual(recovered["state"], "responded")
        self.assertEqual(len(harness.handle.calls), 1)


class LoopbackBridgeTests(unittest.TestCase):
    def test_handle_rejects_remote_endpoint_and_unlisted_tool(self) -> None:
        with self.assertRaises(BridgeRequestRejected):
            LoopbackStreamableHttpMcpHandle(
                "https://example.com/mcp",
                allowed_tools={"search_library": "search_library"},
            )
        handle = LoopbackStreamableHttpMcpHandle(
            "http://127.0.0.1:1/mcp",
            allowed_tools={"search_library": "search_library"},
        )
        with self.assertRaises(BridgeRequestRejected):
            handle.invoke(
                "get_document", {}, call_ref="credential-use:test:1",
                deadline_at=FUTURE, max_response_bytes=1000
            )

    def test_handle_preserves_exact_json_rpc_body(self) -> None:
        response_result = tool_result({"results": [], "cursor": None, "has_more": False})

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers["Content-Length"])
                request = json.loads(self.rfile.read(size))
                raw = canonical_json(
                    {"jsonrpc": "2.0", "id": request["id"], "result": response_result}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format, *args):
                del format, args

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        handle = LoopbackStreamableHttpMcpHandle(
            f"http://127.0.0.1:{server.server_port}/mcp",
            allowed_tools={"search_library": "search_library"},
            timeout_seconds=2,
        )
        result = handle.invoke(
            "search_library",
            {"query": "ACN"},
            call_ref="credential-use:test:1",
            deadline_at=(datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(),
            max_response_bytes=10_000,
        )
        self.assertEqual(result.result, response_result)
        self.assertEqual(json.loads(result.raw_response)["id"], result.request_id)


if __name__ == "__main__":
    unittest.main()
