"""Isolated SEC authority demo harness.

This module is deliberately self-contained so the public canary script does
not import the test suite.  It creates all stores in memory except for a
temporary raw spool, injects no credentials, and is suitable for a local
read-only SEC demonstration.  It is not a production database bootstrap.
"""

from __future__ import annotations

import copy
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

from .capability_catalog import CapabilityCatalog
from .connector import ConnectorStore
from .connector_authority_port import ConnectorAuthorityPort
from .connector_inventory import load_packaged_connector_inventory
from .connector_runner import (
    ConnectorRunnerAdmissionGate,
    StaticAdapterResolver,
    validate_connector_runner_request,
    validate_runner_environment_manifest,
)
from .connector_transport_executor import ConnectorTransportExecutor
from .contracts import ExecutionInvocation, ExecutionKind, WorkOrder
from .observability import ObservabilityStore
from .public_http_transport import PublicHttpTransport
from .raw_spool import RawSpool
from .research_context import (
    build_claim_index,
    build_compiled_connector_plan,
    build_context_pack,
    build_fixture_runner_request,
)
from .research_coordinator import (
    FixtureResearchCoordinator,
    ResearchCoordinatorStore,
    validate_connector_completion_receipt,
)
from .runner_journal import RunnerJournal
from .scheduler import Scheduler
from .sec_public_adapter import SecPublicHttpAdapter
from .store import DaltonStore, canonical_json, content_hash


WHEN = datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc)
WIRE_WHEN = WHEN.isoformat(timespec="microseconds")

PUBLIC_PERMISSIONS = {
    "risk_class": "low",
    "network": True,
    "filesystem_read": [],
    "filesystem_write": ["runner:raw-sink"],
    "credential_slot_refs": [],
    "core_db": False,
    "side_effects": ["read:public-http"],
}


class MutableClock:
    """Frozen wall clock used to make the authority time chain deterministic."""

    def __init__(self, value: datetime = WHEN) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class _PublicAuthorities:
    """Small in-memory authority resolver for the isolated canary only."""

    def __init__(self) -> None:
        self.policy_permissions = copy.deepcopy(PUBLIC_PERMISSIONS)

    def approval(self, query: dict[str, Any]) -> dict[str, Any]:
        suffix = content_hash({
            "capability_id": query["capability_id"],
            "source_hash": query["source_hash"],
        })[:16]
        wire: dict[str, Any] = {
            "schema_version": "0.1",
            "approval_ref": f"approval:{suffix}",
            "capability_id": query["capability_id"],
            "registry_revision_ref": f"capability-version:{suffix}",
            "artifact_ref": query["source_ref"],
            "artifact_hash": query["source_hash"],
            "schema_hash": query["schema_hash"],
            "fixture_manifest_hash": "4" * 64,
            "attestation_ref": f"attestation:{suffix}",
            "attestation_hash": "5" * 64,
            "decision_ref": f"capability-decision:{suffix}",
            "decision": "approve",
            # This is a synthetic approval in an isolated canary authority,
            # not a statement that a named operator signed a live grant.
            "approved_by": "human:isolated-canary",
            "approved_permissions": copy.deepcopy(query["requested_permissions"]),
            "active": True,
            "effective_from": "2026-08-15T07:00:00+00:00",
            "effective_until": None,
        }
        wire["receipt_hash"] = content_hash(wire)
        return wire

    def policy(self, query: dict[str, Any]) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "schema_version": "0.1",
            "policy_ref": query["policy_ref"],
            "effective_from": "2026-08-15T07:00:00+00:00",
            "effective_until": None,
            "allowed_principal_refs": ["principal:worker-1"],
            "allowed_permissions": copy.deepcopy(self.policy_permissions),
            "max_lease_seconds": 120,
        }
        wire["content_hash"] = content_hash(wire)
        return wire


def _descriptor_spec(capability_id: str, *, name: str, side_effects: list[str]) -> dict[str, Any]:
    source_type, namespace = capability_id.split(":")[1:3]
    return {
        "schema_version": "0.1",
        "id": capability_id,
        "version": 1,
        "created_at": WIRE_WHEN,
        "kind": "transform",
        "name": name,
        "label": "SEC public filings adapter",
        "summary": "Read the public SEC submissions endpoint",
        "aliases": ["SEC filings"],
        "tags": ["connector", "public", "SEC"],
        "intent_examples": ["list public SEC filings"],
        "source": {
            "type": source_type,
            "namespace": namespace,
            "source_ref": "artifact:sec-public-source",
            "source_version": "1",
        },
        "contract": {
            "mode": "typed_call",
            "input_schema_ref": "schema:connector:sec:list-filings-input:0.1",
            "output_schema_ref": "schema:connector:sec:list-filings-output:0.1",
            "instruction_ref": None,
            "adapter_ref": "transport:public-http:0.1",
        },
        "permissions": {
            **copy.deepcopy(PUBLIC_PERMISSIONS),
            "side_effects": list(side_effects),
        },
        "eligibility": {
            "state": "ready",
            "visibility_scopes": ["research"],
            "policy_ref": "policy:capability-v1",
            "valid_until": None,
        },
        "source_hash": "",
        "schema_hash": "",
    }


def _price_rate_spec(profile_ref: str) -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "id": "connector-price:sec-public:zero:v1",
        "created_at": WIRE_WHEN,
        "price_rate_ref": "connector-price:sec-public:zero",
        "version": 1,
        "prior_version_ref": None,
        "connector_profile_ref": profile_ref,
        "meter": "calls",
        "unit_quantity": 1,
        "unit_price_micros": 0,
        "rounding_mode": "ceiling",
        "currency": "USD",
        "effective_from": "2026-08-14T08:00:00+00:00",
        "effective_until": None,
        "source_ref": "pricing:connector-price:sec-public:zero",
        "actor_ref": "human:governance",
    }


def _rate_policy_spec(profile_ref: str, price_rate_ref: str) -> dict[str, Any]:
    price_book = {
        "price_rate_refs": [price_rate_ref],
        "required_price_meters": ["calls"],
    }
    return {
        "schema_version": "0.1",
        "id": "connector-rate-policy:sec-public:v1",
        "created_at": WIRE_WHEN,
        "policy_ref": "connector-rate-policy:sec-public",
        "quota_scope_ref": "connector-quota-scope:sec-public",
        "version": 1,
        "prior_version_ref": None,
        "connector_profile_ref": profile_ref,
        "window_seconds": 60,
        "reset_timezone": "UTC",
        "max_concurrency": 2,
        "quota_currency": "USD",
        "price_rate_refs": price_book["price_rate_refs"],
        "required_price_meters": price_book["required_price_meters"],
        "price_book_hash": content_hash(price_book),
        "limits": {"calls": 2, "bytes": 2_000_000, "records": 100, "cost_micros": 1000},
        "effective_from": WIRE_WHEN,
        "effective_until": None,
        "actor_ref": "human:governance",
    }


class _Response:
    status = 200
    reason = "OK"

    def __init__(self, body: bytes):
        self._body = body

    def getheaders(self):
        return [("content-type", "application/json"), ("content-length", str(len(self._body)))]

    def read(self, _amount=None):
        body, self._body = self._body, b""
        return body

    def close(self):
        return None


def _sec_body() -> bytes:
    rows = [
        ("0000000001-25-000001", "8-K", "2025-01-01", "jan.htm", None),
        ("0000000002-25-000002", "10-Q", "2025-01-29", "q1.htm", None),
        ("0000000003-25-000003", "10-Q/A", "2025-04-30", "q2a.htm", "0000000002-25-000002"),
        ("0000000004-25-000004", "10-Q", "2025-10-29", "q3.htm", None),
        ("0000000005-25-000005", "8-K", "2025-12-31", "dec.htm", None),
    ]
    recent = {
        "accessionNumber": [row[0] for row in rows],
        "form": [row[1] for row in rows],
        "filingDate": [row[2] for row in rows],
        "primaryDocument": [row[3] for row in rows],
        "amendmentOf": [row[4] for row in rows],
    }
    return canonical_json({"cik": "0000789019", "filings": {"recent": recent}}).encode()


class _PublicBridge:
    def __init__(self, harness: "SecAuthorityHarness") -> None:
        self.harness = harness

    def execute(self, step, runner_request, *, idempotency_key):
        request = dict(runner_request)
        actual = {key: value for key, value in request.items() if not key.startswith("compiled_")}
        actual.update({
            "id": "connector-runner-request:sec-public:actual:1",
            "call_spec_hash": self.harness.call["content_hash"],
            "idempotency_key": "sec-public:actual:1",
        })
        actual.pop("content_hash", None)
        actual = validate_connector_runner_request({**actual, "content_hash": content_hash(actual)})
        self.harness.actual_request = actual
        response = self.harness.executor.execute(
            actual, scheduler_lease_token=self.harness.claim["lease_token"]
        )
        latest = self.harness.journal.latest(actual["id"])
        observed = latest["payload"].get("observation") or {}
        error = latest["payload"].get("error") or observed.get("error")
        outcome = response["outcome"]
        receipt: dict[str, Any] = {
            "schema_version": "0.2",
            "id": "connector-completion-receipt:sec-public:1",
            "created_at": response["created_at"],
            "runner_request_ref": request["id"],
            "runner_request_hash": request["content_hash"],
            "actual_runner_request_ref": actual["id"],
            "actual_runner_request_hash": actual["content_hash"],
            "status": outcome,
            "result_ref": response["result_envelope_ref"],
            "result_hash": response["result_envelope_hash"],
            "source_envelopes": [] if response["source_envelope_ref"] is None else [{
                "ref": response["source_envelope_ref"],
                "hash": response["source_envelope_hash"],
            }],
            "artifacts": [] if response["raw_artifact_version_ref"] is None else [{
                "ref": response["raw_artifact_version_ref"],
                "hash": response["raw_artifact_version_hash"],
            }],
            "next_cursor": None,
            "error_code": None if outcome == "succeeded" else (error or {}).get("code", "connector_failed"),
            "retry_after_ms": (
                observed.get("retry_after_ms") or 1 if outcome == "retryable" else None
            ),
        }
        receipt["content_hash"] = content_hash(receipt)
        return validate_connector_completion_receipt(receipt)


class SecAuthorityHarness:
    """Build one isolated SEC public WorkOrder and its persisted authority."""

    def __init__(self, *, live: bool = False, user_agent: str | None = None) -> None:
        self.clock = MutableClock()
        self.live = live
        self.user_agent = user_agent
        self.temp = tempfile.TemporaryDirectory(prefix="dalton-sec-authority-")
        self.inventory = load_packaged_connector_inventory()
        self.template = self.inventory["templates"]["sec"]
        self.core = DaltonStore(":memory:")
        self.connectors = ConnectorStore(self.core, clock=self.clock)
        self.scheduler = Scheduler(":memory:", clock=self.clock, default_lease_seconds=30, max_lease_seconds=60)
        self.authorities = _PublicAuthorities()
        self.catalog = CapabilityCatalog(
            ":memory:", clock=self.clock,
            approval_resolver=self.authorities.approval,
            policy_resolver=self.authorities.policy,
        )
        self.observability = ObservabilityStore(self.core)
        self.journal = RunnerJournal(self.core, clock=self.clock)
        self.spool = RawSpool(self.temp.name, max_total_bytes=2_000_000)
        self._build_control_plane()
        self._build_plan_and_request()

    def _build_control_plane(self) -> None:
        template = self.template
        op = next(item for item in template["operations"] if item["operation"] == "list_filings")
        self.capability_id = "capability:dalton:connector:sec-edgar"
        descriptor = _descriptor_spec(self.capability_id, name="sec-public-filings", side_effects=["read:public-http"])
        descriptor.update({
            "kind": "connector",
            "contract": {
                "mode": "typed_call",
                "input_schema_ref": op["input_schema_ref"],
                "output_schema_ref": op["output_schema_ref"],
                "instruction_ref": None,
                "adapter_ref": "transport:public-http:0.1",
            },
            "source_hash": content_hash(template["source_identity"]),
            "schema_hash": content_hash({
                "allowed_operations": ["list_filings"],
                "input_schema_refs": {"list_filings": op["input_schema_ref"]},
                "input_schema_hashes": {"list_filings": op["input_schema_hash"]},
                "output_schema_refs": {"list_filings": op["output_schema_ref"]},
                "output_schema_hashes": {"list_filings": op["output_schema_hash"]},
            }),
        })
        self.descriptor = self.catalog.publish(descriptor)
        self.authorities.policy_permissions = copy.deepcopy(PUBLIC_PERMISSIONS)
        adapter_hash = content_hash({"adapter_ref": "transport:public-http:0.1", "source": "sec-public"})
        binding = {
            "binding_ref": "runner-binding:sec-public:v1",
            "descriptor_revision_ref": self.descriptor.revision_ref,
            "descriptor_hash": self.descriptor.content_hash,
            "adapter_ref": "transport:public-http:0.1",
            "adapter_hash": adapter_hash,
            "source_ref": template["source_identity"]["source_ref"],
            "source_hash": content_hash(template["source_identity"]),
            "operation": "list_filings",
            "input_schema_ref": op["input_schema_ref"],
            "input_schema_hash": op["input_schema_hash"],
            "output_schema_ref": op["output_schema_ref"],
            "output_schema_hash": op["output_schema_hash"],
            "auth_mode": "none",
            "credential_slot_refs": [],
            "required_permissions": copy.deepcopy(PUBLIC_PERMISSIONS),
            "side_effects": ["read:public-http"],
            "rate_policy_ref": "connector-rate-policy:sec-public",
        }
        manifest = validate_runner_environment_manifest({
            "schema_version": "0.1",
            "id": "runner-environment:sec-public:v1",
            "created_at": WIRE_WHEN,
            "runner_runtime_ref": "runtime:connector-runner:0.1",
            "runner_actor_ref": "runner:connector",
            "resolver_ref": "resolver:connector-static:0.1",
            "resolver_version": "0.1",
            "package_manifest_ref": "artifact:runner-packages:sec-public:v1",
            "package_manifest_hash": "9" * 64,
            "bindings": [binding],
            "content_hash": content_hash({
                "schema_version": "0.1",
                "id": "runner-environment:sec-public:v1",
                "created_at": WIRE_WHEN,
                "runner_runtime_ref": "runtime:connector-runner:0.1",
                "runner_actor_ref": "runner:connector",
                "resolver_ref": "resolver:connector-static:0.1",
                "resolver_version": "0.1",
                "package_manifest_ref": "artifact:runner-packages:sec-public:v1",
                "package_manifest_hash": "9" * 64,
                "bindings": [binding],
            }),
        })
        # The manifest validator recomputes its own hash.  Build it through the
        # normal closed-shape helper so the value cannot drift from the payload.
        manifest_payload = {key: value for key, value in manifest.items() if key != "content_hash"}
        manifest = validate_runner_environment_manifest({**manifest_payload, "content_hash": content_hash(manifest_payload)})
        if self.live:
            adapter = SecPublicHttpAdapter(
                user_agent=self.user_agent or "Dalton Research Agent public-read-only canary",
                clock=self.clock,
            )
        else:
            adapter = SecPublicHttpAdapter(
                transport=PublicHttpTransport(
                    resolver=lambda _host, _port: ("93.184.216.34",),
                    exchange=lambda _target, _method, _headers, _body, _timeout: _Response(_sec_body()),
                ),
                clock=self.clock,
            )
        self.static_resolver = StaticAdapterResolver(
            manifest,
            {binding["binding_ref"]: adapter},
            {binding["binding_ref"]: lambda params: set(params) == {"issuer", "form", "date_from", "date_to", "limit"}},
        )
        profile = {
            "schema_version": "0.1",
            "id": "connector-profile:sec-public:v1",
            "created_at": WIRE_WHEN,
            "connector_ref": template["connector_ref"],
            "version": 1,
            "prior_version_ref": None,
            "capability_id": self.capability_id,
            "descriptor_revision_ref": self.descriptor.revision_ref,
            "descriptor_hash": self.descriptor.content_hash,
            "source_identity": dict(template["source_identity"]),
            "source_hash": content_hash(template["source_identity"]),
            "schema_hash": descriptor["schema_hash"],
            "catalog_epoch": self.descriptor.catalog_epoch,
            "adapter_ref": binding["adapter_ref"],
            "adapter_hash": adapter_hash,
            "runner_runtime_ref": manifest["runner_runtime_ref"],
            "runner_actor_ref": manifest["runner_actor_ref"],
            "runner_environment_hash": manifest["content_hash"],
            "allowed_operations": ["list_filings"],
            "allowed_hosts": ["data.sec.gov"],
            "auth_mode": "none",
            "credential_slot_refs": [],
            "input_schema_refs": {"list_filings": op["input_schema_ref"]},
            "input_schema_hashes": {"list_filings": op["input_schema_hash"]},
            "output_schema_refs": {"list_filings": op["output_schema_ref"]},
            "output_schema_hashes": {"list_filings": op["output_schema_hash"]},
            "pagination": {"mode": "cursor", "cursor_field": "cursor", "max_pages": 1},
            "completeness": {"list_filings": "enumerated"},
            "max_response_bytes": 2_000_000,
            "max_records": 100,
            "timeout_ms": 60_000,
            "access_policy_ref": "policy:access:public",
            "retention_policy_ref": "policy:retention:filing",
            "terms_policy_ref": "policy:terms:sec",
            "network_policy": {"allowed_schemes": ["https"], "allow_redirects": False, "max_redirects": 0, "resolve_public_only": True},
        }
        self.profile = self.connectors.register_profile(profile, idempotency_key="sec:profile")
        self.call_id = "connector-call:sec-public:1"
        parameters = {"issuer": "0000789019", "form": "10-Q", "date_from": "2025-01-01", "date_to": "2025-12-31", "limit": 10}
        work = WorkOrder(
            schema_version="0.1", id="work:sec-public:1", created_at=WIRE_WHEN, updated_at=WIRE_WHEN,
            question="List the bounded SEC 10-Q filings in 2025", requested_capabilities=(self.capability_id,),
            runtime_profile_ref=profile["runner_runtime_ref"], budget={"max_seconds": 60},
            idempotency_key="work:sec-public:1", declared_side_effects=("read:public-http",), status="ready", input_refs=(self.call_id,),
        )
        self.work = work
        self.work_hash = content_hash(work.to_dict())
        policy = self.authorities.policy({"policy_ref": "policy:capability-v1"})
        self.capability_lease = self.catalog.prepare(
            work, capability_id=self.descriptor.id, revision_ref=self.descriptor.revision_ref,
            catalog_epoch=self.descriptor.catalog_epoch, descriptor_hash=self.descriptor.content_hash,
            source_hash=self.descriptor.source_hash, schema_hash=self.descriptor.schema_hash,
            policy_ref="policy:capability-v1", policy_hash=policy["content_hash"],
            principal_ref="principal:worker-1", visibility_scopes=["research"], ttl_seconds=60,
        )
        self.call = self.connectors.register_call_spec({
            "schema_version": "0.1", "id": self.call_id, "created_at": WIRE_WHEN,
            "work_order_ref": work.id, "work_order_hash": self.work_hash,
            "connector_profile_ref": self.profile["id"], "operation": "list_filings",
            "parameters": parameters, "query_hash": content_hash({"operation": "list_filings", "parameters": parameters}),
        }, idempotency_key="sec:call")
        execution = ExecutionInvocation(
            schema_version="0.1", id="connector-invocation:sec-public:1", created_at=WIRE_WHEN,
            kind=ExecutionKind.CONNECTOR, work_order_ref=work.id, profile_ref=self.profile["id"],
            capability=self.capability_id, input_refs=(self.call["id"],), output_refs=("artifact:sec-public:raw:1",),
            started_at=WIRE_WHEN, completed_at=None, side_effects=(), runtime_ref=self.profile["runner_runtime_ref"],
            actor_ref=self.profile["runner_actor_ref"], environment_hash=self.profile["runner_environment_hash"],
        )
        self.invocation = self.connectors.register_invocation({
            "schema_version": "0.1", "id": execution.id, "created_at": WIRE_WHEN,
            "work_order_ref": work.id, "work_order_hash": self.work_hash,
            "connector_profile_ref": self.profile["id"], "connector_profile_hash": self.profile["content_hash"],
            "call_spec_ref": self.call["id"], "call_spec_hash": self.call["content_hash"],
            "capability_lease_ref": self.capability_lease.id, "capability_lease_hash": self.capability_lease.content_hash,
            "descriptor_revision_ref": self.descriptor.revision_ref, "catalog_epoch": self.descriptor.catalog_epoch,
            "logical_invocation_key": "connector-logical:" + content_hash({
                "work_order_ref": work.id, "work_order_hash": self.work_hash,
                "connector_profile_hash": self.profile["content_hash"], "call_spec_hash": self.call["content_hash"],
            }),
        }, execution=execution, idempotency_key="sec:invocation")
        rate = self.connectors.register_price_rate(_price_rate_spec(self.profile["id"]), idempotency_key="sec:price")
        self.policy = self.connectors.register_rate_policy(_rate_policy_spec(self.profile["id"], rate["id"]), idempotency_key="sec:policy")
        self.scheduler.enqueue(work)
        self.claim = self.scheduler.claim(self.profile["runner_actor_ref"], work_order_id=work.id)
        self.gate = ConnectorRunnerAdmissionGate(
            scheduler=self.scheduler, catalog=self.catalog, connectors=self.connectors,
            resolver=self.static_resolver, visibility_scopes=["research"], clock=self.clock,
        )
        self.authority = ConnectorAuthorityPort(connectors=self.connectors, observability=self.observability, scheduler=self.scheduler)
        self.executor = ConnectorTransportExecutor(
            gate=self.gate, journal=self.journal, spool=self.spool, authority=self.authority,
            connector_reader=self.connectors, clock=self.clock,
        )

    def _build_plan_and_request(self) -> None:
        op = next(item for item in self.template["operations"] if item["operation"] == "list_filings")
        step_spec = {
            "source_ref": self.profile["source_identity"]["source_ref"], "source_hash": self.profile["source_hash"],
            "connector_profile_ref": self.profile["id"], "connector_profile_hash": self.profile["content_hash"],
            "operation": "list_filings", "parameters": {"issuer": "0000789019", "form": "10-Q", "date_from": "2025-01-01", "date_to": "2025-12-31", "limit": 10},
            "input_schema_ref": op["input_schema_ref"], "input_schema_hash": op["input_schema_hash"],
            "output_schema_ref": op["output_schema_ref"], "output_schema_hash": op["output_schema_hash"],
            "completeness_required": "enumerated", "depends_on": [], "fallback_step_refs": [], "max_attempts": 1,
        }
        self.plan = build_compiled_connector_plan(
            task_ref=self.work.id, task_hash=self.work_hash, planner_ref="planner:sec-demo:1",
            planner_hash=content_hash({"planner": "sec-demo:1"}), routing_policy_ref="routing:public-only:1",
            routing_policy_hash=content_hash({"routing": "public-only:1"}), step_specs=[step_spec], created_at=WIRE_WHEN,
        )
        self.step = self.plan["steps"][0]
        self.claim_index = build_claim_index(
            ledger=self.core, created_at=WIRE_WHEN
        )
        self.context = build_context_pack([
            {"kind": "mandate", "ref": "mandate:sec-demo:1", "hash": content_hash({"mandate": "bounded SEC public read"}), "priority": 100, "content": "Count the bounded SEC filings."},
        ], task_ref=self.work.id, task_hash=self.work_hash, compiled_plan_ref=self.plan["id"], compiled_plan_hash=self.plan["content_hash"], claim_index_ref=self.claim_index["id"], claim_index_hash=self.claim_index["content_hash"], created_at=WIRE_WHEN, max_tokens=100, max_bytes=2000)
        request = build_fixture_runner_request(self.plan, self.step, attempt_number=1, created_at=WIRE_WHEN)
        request.update({
            "connector_invocation_ref": self.invocation["id"], "connector_invocation_hash": self.invocation["content_hash"],
            "execution_ref": self.invocation["execution_ref"], "execution_hash": self.invocation["execution_hash"],
            "scheduler_lease_revision_ref": self.claim["lease"]["id"], "scheduler_lease_hash": self.claim["lease"]["content_hash"],
            "call_spec_ref": self.call["id"], "capability_lease_ref": self.capability_lease.id,
            "capability_lease_hash": self.capability_lease.content_hash, "runner_runtime_ref": self.profile["runner_runtime_ref"],
            "runner_actor_ref": self.profile["runner_actor_ref"], "runner_environment_hash": self.profile["runner_environment_hash"],
            "principal_ref": "principal:worker-1", "idempotency_key": "sec-coordinator:1",
        })
        request["content_hash"] = content_hash({key: value for key, value in request.items() if key != "content_hash"})
        self.coordinator_request = validate_connector_runner_request(request)
        self.coordinator_store = ResearchCoordinatorStore(":memory:")
        self.bridge = _PublicBridge(self)
        self.coordinator = FixtureResearchCoordinator(store=self.coordinator_store, connector_port=self.bridge, clock=self.clock)
        result = self.coordinator.run(
            plan=self.plan, context_pack=self.context, claim_index=self.claim_index,
            runner_requests={self.step["id"]: [self.coordinator_request]}, run_ref="research-run:sec-public:1",
            attempt_ref="research-attempt:sec-public:1", attempt_hash=content_hash({"attempt": "sec-public:1"}),
        )
        if result["status"] != "completed":
            raise RuntimeError({"result": result, "journal": self.journal.history(self.actual_request["id"])})
        self.checkpoint = self.coordinator_store.list_checkpoints("research-run:sec-public:1")[0]

    def resolver(self):
        from .authority_resolver import ConnectorAuthorityResolver

        return ConnectorAuthorityResolver(
            core=self.core, connectors=self.connectors, observability=self.observability,
            scheduler=self.scheduler, coordinator=self.coordinator_store,
            artifact_reader=lambda artifact: self.spool.read_object(artifact["artifact_content_hash"]),
            runner_journal=self.journal,
        )

    def close(self) -> None:
        self.coordinator_store.close()
        self.catalog.close()
        self.scheduler.close()
        self.core.close()
        self.temp.cleanup()


__all__ = ["MutableClock", "PUBLIC_PERMISSIONS", "SecAuthorityHarness", "WHEN", "WIRE_WHEN"]
