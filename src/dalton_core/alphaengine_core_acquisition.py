"""Core-hosted AlphaEngine document acquisition.

This module composes the existing trusted-runner authorities so that one
AlphaEngine ``get_document`` acquisition leaves *Core-held* connector
authority behind it: ``ConnectorInvocation``, physical attempt, usage, cost,
quota settlement, raw ``ArtifactVersion`` v0.2 and ``SourceEnvelope`` rows in
the same ``DaltonStore`` that later has to promote a transcript candidate.

It does not add a contract.  Every record is written by the existing
``ConnectorStore`` / ``ObservabilityStore`` / ``Scheduler`` /
``CapabilityCatalog`` / ``CredentialAuthorityStore`` validators through the
existing ``LiveMcpRunnerAdmissionGate`` and ``ConnectorTransportExecutor``.
The document is assembled by the existing
``AlphaEngineDocumentAcquisitionCoordinator`` reading immutable receipts back
from Core.

Governance is explicit and hash-bound: the connector capability is only
published against an operator-reviewed ``StaticConnectorGovernance`` record
whose ``status`` must be ``approved``.  A ``proposed`` record fails closed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .alphaengine_document_acquisition import (
    AlphaEngineDocumentAcquisitionCoordinator,
    build_alphaengine_document_acquisition_plan,
    validate_alphaengine_document_acquisition_plan,
    validate_alphaengine_document_page_request,
)
from .capability_catalog import (
    CapabilityCatalog,
    CapabilityNotFound,
    CapabilityPermissions,
    canonical_hash,
)
from .connector import ConnectorStore
from .connector_governance import (
    ConnectorGovernance,
    ConnectorGovernanceError,
    GOVERNANCE_FIELDS,
)
from .connector_authority_port import (
    ConnectorAuthorityPort,
    ConnectorCompletionReceiptReader,
)
from .connector_inventory import load_packaged_connector_inventory
from .connector_quota_policy import (
    apply_governed_quota_to_limits,
    governed_daily_quota,
)
from .connector_runner import (
    StaticAdapterResolver,
    validate_runner_environment_manifest,
)
from .connector_transport_executor import ConnectorTransportExecutor
from .contracts import ExecutionInvocation, ExecutionKind, WorkOrder
from .credential_authority import CredentialAuthorityStore
from .live_mcp_connector import (
    AlphaEngineLiveAdapter,
    LiveMcpRunnerAdmissionGate,
    OPENCLAW_ALPHAENGINE_BRIDGE_HASH,
    build_live_mcp_transport_plan,
)
from .observability import ObservabilityStore
from .raw_spool import RawSpool
from .research_context import build_compiled_connector_plan
from .runner_journal import RunnerJournal, RunnerJournalNotFound
from .scheduler import Scheduler
from .store import DaltonStore, canonical_json, content_hash


GOVERNANCE_SCHEMA_VERSION = "0.1"
CAPABILITY_ID = "capability:dalton:connector:alphaengine-get-document"
OPERATION = "get_document"
# 180,000 bytes => 30,000 chars per page, the same page shape used by the
# 2026-08-24 isolated ACN acquisition.
PAGE_MAX_RESPONSE_BYTES = 180_000
DEFAULT_MAX_PAGES = 20
DEFAULT_MAX_DOCUMENT_CHARS = 600_000
RUNNER_RUNTIME_REF = "runtime:dalton-core-trusted-runner:0.1"
RUNNER_ACTOR_REF = "runner:dalton-core-trusted-runner"
RESOLVER_REF = "resolver:dalton-core-static:0.1"
ADAPTER_REF = "mcp-target:alphaengine"
ADAPTER_PACKAGE = "openclaw-alphaengine-live-adapter:0.1"
CREDENTIAL_SLOT_REF = "credential-slot:alphaengine"
CREDENTIAL_AUTHORITY_REF = "credential-authority:host:alphaengine"
SIDE_EFFECT = "read:alphaengine-library"
VISIBILITY_SCOPES = ("research",)
PROFILE_REF = "connector-profile:alphaengine-get-document:v1"
RATE_POLICY_REF = "connector-rate-policy:alphaengine-get-document"
PRICE_RATE_REF = "connector-price-rate:alphaengine-get-document:calls"
PLANNER_REF = "planner:dalton-core-alphaengine-acquisition:0.1"
ROUTING_POLICY_REF = "routing:dalton-core-alphaengine-acquisition:0.1"

_GOVERNANCE_FIELDS = set(GOVERNANCE_FIELDS)
_GOVERNANCE_STATUSES = {"proposed", "approved"}


class AlphaEngineCoreAcquisitionError(RuntimeError):
    pass


def _wire_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AlphaEngineCoreAcquisitionError("clock must return an aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AlphaEngineCoreAcquisitionError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise AlphaEngineCoreAcquisitionError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _with_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(value)
    wire["content_hash"] = content_hash(wire)
    return wire


def live_alphaengine_permissions() -> dict[str, Any]:
    """Permissions of the host-owned, read-only AlphaEngine MCP capability."""

    return {
        "risk_class": "low",
        "network": False,
        "filesystem_read": [],
        "filesystem_write": ["runner:raw-sink"],
        "credential_slot_refs": [CREDENTIAL_SLOT_REF],
        "core_db": False,
        "side_effects": [SIDE_EFFECT],
    }


def alphaengine_get_document_contract() -> tuple[dict[str, Any], dict[str, Any]]:
    template = load_packaged_connector_inventory()["templates"]["alphaengine"]
    matches = [
        item for item in template["operations"]
        if item["operation"] == OPERATION
    ]
    if len(matches) != 1:
        raise AlphaEngineCoreAcquisitionError("AlphaEngine get_document is not frozen")
    return template, matches[0]


def alphaengine_get_document_schema_hash() -> str:
    _, contract = alphaengine_get_document_contract()
    return content_hash(
        {
            "allowed_operations": [OPERATION],
            "input_schema_refs": {OPERATION: contract["input_schema_ref"]},
            "input_schema_hashes": {OPERATION: contract["input_schema_hash"]},
            "output_schema_refs": {OPERATION: contract["output_schema_ref"]},
            "output_schema_hashes": {OPERATION: contract["output_schema_hash"]},
        }
    )


def alphaengine_source_hash() -> str:
    template, _ = alphaengine_get_document_contract()
    return content_hash(template["source_identity"])


def alphaengine_adapter_hash() -> str:
    return content_hash(
        {
            "target_ref": ADAPTER_REF,
            "package": ADAPTER_PACKAGE,
            "bridge_hash": OPENCLAW_ALPHAENGINE_BRIDGE_HASH,
            "operation": OPERATION,
        }
    )


def build_governance_record(
    *,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-08-26T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Build the closed, hash-bound governance record for this connector.

    ``expected_source_hash`` / ``expected_schema_hash`` are derived from the
    frozen packaged inventory so an owner reviews exactly the schema that the
    catalog will later be asked to approve.
    """

    base = {
        "schema_version": GOVERNANCE_SCHEMA_VERSION,
        "id": f"connector-governance:alphaengine-get-document:v{version}",
        "status": status,
        "capability_id": CAPABILITY_ID,
        "approved_by": approved_by,
        "principal_ref": "principal:dalton-core-trusted-runner",
        "policy_ref": (
            f"policy:dalton:connector-governance:alphaengine-get-document:v{version}"
        ),
        "approval_ref": f"approval:connector-governance:alphaengine-get-document:v{version}",
        "decision_ref": (
            f"capability-decision:connector-governance:alphaengine-get-document:v{version}"
        ),
        "registry_revision_ref": f"{CAPABILITY_ID}@v{version}",
        "attestation_ref": (
            f"attestation:connector-governance:alphaengine-get-document:v{version}"
        ),
        "effective_from": _wire_time(_parse_time(effective_from, "effective_from")),
        "effective_until": None,
        "max_lease_seconds": max_lease_seconds,
        "allowed_permissions": live_alphaengine_permissions(),
        "expected_source_hash": alphaengine_source_hash(),
        "expected_schema_hash": alphaengine_get_document_schema_hash(),
    }
    return _with_hash(base)


class StaticConnectorGovernance(ConnectorGovernance):
    """Backward-compatible AlphaEngine-only view of generic governance."""

    def __init__(self, value: Mapping[str, Any]) -> None:
        try:
            super().__init__(value)
        except ConnectorGovernanceError as exc:
            # Existing AlphaEngine callers use this module's exception type.
            raise AlphaEngineCoreAcquisitionError(str(exc)) from exc
        if self.capability_id != CAPABILITY_ID:
            raise AlphaEngineCoreAcquisitionError(
                "governance capability_id is not this connector"
            )

    @classmethod
    def load(cls, path: str | Path) -> "StaticConnectorGovernance":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def _require_approved(self) -> None:
        try:
            super()._require_approved()
        except ConnectorGovernanceError as exc:
            raise AlphaEngineCoreAcquisitionError(str(exc)) from exc


class AlphaEngineCoreAcquisition:
    """Acquire one AlphaEngine document into Core-held connector authority."""

    def __init__(
        self,
        *,
        store: DaltonStore,
        connectors: ConnectorStore,
        observability: ObservabilityStore,
        journal: RunnerJournal,
        scheduler: Scheduler,
        catalog: CapabilityCatalog,
        spool: RawSpool,
        governance: StaticConnectorGovernance,
        mcp_handle: Any,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 60,
        grant_seconds: int = 900,
    ) -> None:
        if type(store) is not DaltonStore:
            raise TypeError("acquisition requires an exact DaltonStore")
        if type(connectors) is not ConnectorStore or connectors.connection is not store.connection:
            raise TypeError("acquisition requires the Core ConnectorStore")
        if type(observability) is not ObservabilityStore or observability.connection is not store.connection:
            raise TypeError("acquisition requires the Core ObservabilityStore")
        if type(journal) is not RunnerJournal:
            raise TypeError("acquisition requires the Core RunnerJournal")
        if type(spool) is not RawSpool:
            raise TypeError("acquisition requires an exact RawSpool")
        if not isinstance(governance, StaticConnectorGovernance):
            raise TypeError("acquisition requires StaticConnectorGovernance")
        if not callable(getattr(mcp_handle, "invoke", None)):
            raise TypeError("mcp_handle must expose invoke")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise AlphaEngineCoreAcquisitionError("lease_seconds must be a positive integer")
        if isinstance(grant_seconds, bool) or not isinstance(grant_seconds, int) or grant_seconds < 1:
            raise AlphaEngineCoreAcquisitionError("grant_seconds must be a positive integer")
        self.store = store
        self.connectors = connectors
        self.observability = observability
        self.journal = journal
        self.scheduler = scheduler
        self.catalog = catalog
        self.spool = spool
        self.governance = governance
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_seconds = lease_seconds
        self.grant_seconds = grant_seconds
        self.credentials = CredentialAuthorityStore(
            store, handle_resolver=lambda _grant: mcp_handle, clock=self.clock
        )
        self.template, self.contract = alphaengine_get_document_contract()
        self.adapter = AlphaEngineLiveAdapter()
        self.receipts = ConnectorCompletionReceiptReader(
            connectors=connectors, observability=observability
        )
        self.authority_port = ConnectorAuthorityPort(
            connectors=connectors, observability=observability, scheduler=scheduler,
            receipt_reader=self.receipts,
        )
        self._authorities: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # governed, idempotent authorities
    # ------------------------------------------------------------------
    def _descriptor_spec(self) -> dict[str, Any]:
        return {
            "schema_version": "0.1",
            "id": CAPABILITY_ID,
            "version": 1,
            "created_at": self.governance.effective_from,
            "kind": "connector",
            "name": "alphaengine-get-document",
            "label": "Core-hosted AlphaEngine get_document",
            "summary": (
                "Read one exact AlphaEngine library document through the "
                "host-owned loopback MCP bridge into Core connector authority"
            ),
            "aliases": ["alphaengine document", "read alphaengine transcript"],
            "tags": ["connector", "alphaengine", "read-only", "mcp-managed"],
            "intent_examples": [
                "acquire the exact AlphaEngine document for a transcript review",
            ],
            "source": {
                "type": "dalton",
                "namespace": "connector",
                "source_ref": self.template["id"],
                "source_version": "0.1",
            },
            "contract": {
                "mode": "typed_call",
                "input_schema_ref": self.contract["input_schema_ref"],
                "output_schema_ref": self.contract["output_schema_ref"],
                "instruction_ref": None,
                "adapter_ref": ADAPTER_REF,
            },
            "permissions": live_alphaengine_permissions(),
            "eligibility": {
                "state": "ready",
                "visibility_scopes": list(VISIBILITY_SCOPES),
                "policy_ref": self.governance.policy_ref,
                "valid_until": None,
            },
            "source_hash": alphaengine_source_hash(),
            "schema_hash": alphaengine_get_document_schema_hash(),
        }

    def ensure_governed_authorities(self) -> dict[str, Any]:
        """Publish or re-read the exact descriptor, profile, price and policy."""

        if self._authorities is not None:
            return self._authorities
        self.governance._require_approved()
        spec = self._descriptor_spec()
        try:
            descriptor = self.catalog.describe(
                CAPABILITY_ID, visibility_scopes=list(VISIBILITY_SCOPES)
            )
        except CapabilityNotFound:
            descriptor = self.catalog.publish(spec)
        if (
            descriptor.source_hash != spec["source_hash"]
            or descriptor.schema_hash != spec["schema_hash"]
            or descriptor.eligibility.policy_ref != self.governance.policy_ref
            or descriptor.permissions.to_dict() != spec["permissions"]
        ):
            raise AlphaEngineCoreAcquisitionError(
                "published connector capability differs from governed spec"
            )
        binding = {
            "binding_ref": "runner-binding:alphaengine-get-document:0.1",
            "descriptor_revision_ref": descriptor.revision_ref,
            "descriptor_hash": descriptor.content_hash,
            "adapter_ref": ADAPTER_REF,
            "adapter_hash": alphaengine_adapter_hash(),
            "source_ref": self.template["source_identity"]["source_ref"],
            "source_hash": alphaengine_source_hash(),
            "operation": OPERATION,
            "input_schema_ref": self.contract["input_schema_ref"],
            "input_schema_hash": self.contract["input_schema_hash"],
            "output_schema_ref": self.contract["output_schema_ref"],
            "output_schema_hash": self.contract["output_schema_hash"],
            "auth_mode": "mcp_managed",
            "credential_slot_refs": [CREDENTIAL_SLOT_REF],
            "required_permissions": live_alphaengine_permissions(),
            "side_effects": [SIDE_EFFECT],
            "rate_policy_ref": RATE_POLICY_REF,
        }
        manifest_base = {
            "schema_version": "0.1",
            "id": "runner-environment:dalton-core-alphaengine-get-document:0.1",
            "created_at": self.governance.effective_from,
            "runner_runtime_ref": RUNNER_RUNTIME_REF,
            "runner_actor_ref": RUNNER_ACTOR_REF,
            "resolver_ref": RESOLVER_REF,
            "resolver_version": "0.1",
            "package_manifest_ref": "artifact:runner-packages:alphaengine-get-document:0.1",
            "package_manifest_hash": content_hash(
                {
                    "package": ADAPTER_PACKAGE,
                    "bridge_hash": OPENCLAW_ALPHAENGINE_BRIDGE_HASH,
                    "adapter_hash": alphaengine_adapter_hash(),
                }
            ),
            "bindings": [binding],
        }
        manifest = validate_runner_environment_manifest(_with_hash(manifest_base))
        profile_wire = {
            "schema_version": "0.1",
            "id": PROFILE_REF,
            "created_at": self.governance.effective_from,
            "connector_ref": self.template["connector_ref"],
            "version": 1,
            "prior_version_ref": None,
            "capability_id": CAPABILITY_ID,
            "descriptor_revision_ref": descriptor.revision_ref,
            "descriptor_hash": descriptor.content_hash,
            "source_identity": dict(self.template["source_identity"]),
            "source_hash": alphaengine_source_hash(),
            "schema_hash": alphaengine_get_document_schema_hash(),
            "catalog_epoch": descriptor.catalog_epoch,
            "adapter_ref": ADAPTER_REF,
            "adapter_hash": alphaengine_adapter_hash(),
            "runner_runtime_ref": RUNNER_RUNTIME_REF,
            "runner_actor_ref": RUNNER_ACTOR_REF,
            "runner_environment_hash": manifest["content_hash"],
            "allowed_operations": [OPERATION],
            "allowed_hosts": [],
            "auth_mode": "mcp_managed",
            "credential_slot_refs": [CREDENTIAL_SLOT_REF],
            "input_schema_refs": {OPERATION: self.contract["input_schema_ref"]},
            "input_schema_hashes": {OPERATION: self.contract["input_schema_hash"]},
            "output_schema_refs": {OPERATION: self.contract["output_schema_ref"]},
            "output_schema_hashes": {OPERATION: self.contract["output_schema_hash"]},
            "pagination": {
                "mode": self.contract["pagination"]["mode"],
                "cursor_field": self.contract["pagination"]["cursor_field"],
                "max_pages": self.contract["pagination"]["max_pages"],
            },
            "completeness": {OPERATION: self.contract["completeness_ceiling"]},
            "max_response_bytes": PAGE_MAX_RESPONSE_BYTES,
            "max_records": 10,
            "timeout_ms": 120_000,
            "access_policy_ref": "policy:access:alphaengine",
            "retention_policy_ref": "policy:retention:licensed-research",
            "terms_policy_ref": "policy:terms:alphaengine",
            "network_policy": None,
        }
        profile = self.connectors.register_profile(
            profile_wire, idempotency_key="alphaengine-get-document:profile:v1"
        )
        price = self.connectors.register_price_rate(
            {
                "schema_version": "0.1",
                "id": f"{PRICE_RATE_REF}:v1",
                "created_at": self.governance.effective_from,
                "price_rate_ref": PRICE_RATE_REF,
                "version": 1,
                "prior_version_ref": None,
                "connector_profile_ref": profile["id"],
                "meter": "calls",
                "unit_quantity": 1,
                "unit_price_micros": 0,
                "rounding_mode": "ceiling",
                "currency": "USD",
                "effective_from": self.governance.effective_from,
                "effective_until": None,
                "source_ref": "pricing:alphaengine:subscription-included",
                "actor_ref": self.governance.approved_by,
            },
            idempotency_key="alphaengine-get-document:price:v1",
        )
        quota = governed_daily_quota("alphaengine", OPERATION)
        price_book = {
            "price_rate_refs": [price["id"]],
            "required_price_meters": ["calls"],
        }
        rate_policy = self.connectors.register_rate_policy(
            {
                "schema_version": "0.1",
                "id": f"{RATE_POLICY_REF}:v1",
                "created_at": self.governance.effective_from,
                "policy_ref": RATE_POLICY_REF,
                "quota_scope_ref": "connector-quota-scope:alphaengine:get_document",
                "version": 1,
                "prior_version_ref": None,
                "connector_profile_ref": profile["id"],
                "window_seconds": quota["window_seconds"],
                "reset_timezone": quota["reset_timezone"],
                "max_concurrency": 1,
                "quota_currency": "USD",
                **price_book,
                "price_book_hash": content_hash(price_book),
                "limits": apply_governed_quota_to_limits(
                    quota,
                    max_response_bytes=profile["max_response_bytes"],
                    max_records=profile["max_records"],
                ),
                "effective_from": self.governance.effective_from,
                "effective_until": None,
                "actor_ref": self.governance.approved_by,
            },
            idempotency_key="alphaengine-get-document:rate-policy:v1",
        )
        self._authorities = {
            "descriptor": descriptor,
            "binding": binding,
            "manifest": manifest,
            "profile": profile,
            "price": price,
            "rate_policy": rate_policy,
        }
        return self._authorities

    # ------------------------------------------------------------------
    # acquisition
    # ------------------------------------------------------------------
    def build_plan(
        self,
        document_ref: str,
        *,
        created_at: str | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_document_chars: int = DEFAULT_MAX_DOCUMENT_CHARS,
    ) -> dict[str, Any]:
        return build_alphaengine_document_acquisition_plan(
            document_ref=document_ref,
            created_at=created_at or _wire_time(self.clock()),
            max_pages=max_pages,
            page_max_response_bytes=PAGE_MAX_RESPONSE_BYTES,
            max_total_response_bytes=max_pages * PAGE_MAX_RESPONSE_BYTES,
            max_document_chars=max_document_chars,
        )

    def acquire(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        """Run the coordinator with a Core-backed page port; return the manifest."""

        plan_wire = validate_alphaengine_document_acquisition_plan(plan)
        authorities = self.ensure_governed_authorities()
        port = _CorePagePort(self, plan_wire, authorities)
        coordinator = AlphaEngineDocumentAcquisitionCoordinator(
            plan=plan_wire,
            page_port=port,
            authority_reader=self.receipts,
            spool=self.spool,
        )
        manifest = coordinator.execute()
        return {
            "plan": plan_wire,
            "manifest": manifest,
            "provider_calls": port.provider_calls,
            "replayed_pages": port.replayed_pages,
            "descriptor_revision_ref": authorities["descriptor"].revision_ref,
            "connector_profile_ref": authorities["profile"]["id"],
            "connector_profile_hash": authorities["profile"]["content_hash"],
        }


class _CorePagePort:
    """Execute one exact page through the trusted runner into Core."""

    def __init__(
        self,
        owner: AlphaEngineCoreAcquisition,
        plan: Mapping[str, Any],
        authorities: Mapping[str, Any],
    ) -> None:
        self.owner = owner
        self.plan = plan
        self.authorities = authorities
        self.provider_calls = 0
        self.replayed_pages = 0

    def _suffix(self, request: Mapping[str, Any]) -> str:
        return content_hash(
            {
                "plan_hash": self.plan["content_hash"],
                "page_request_hash": request["content_hash"],
            }
        )[:20]

    def execute_page(self, request: Mapping[str, Any]) -> dict[str, Any]:
        owner = self.owner
        page_request = validate_alphaengine_document_page_request(request)
        suffix = self._suffix(page_request)
        created_at = self.plan["created_at"]
        profile = self.authorities["profile"]
        descriptor = self.authorities["descriptor"]
        binding = self.authorities["binding"]
        manifest = self.authorities["manifest"]
        parameters: dict[str, Any] = {"document_ref": self.plan["document_ref"]}
        if page_request["request_cursor"] is not None:
            parameters["cursor"] = page_request["request_cursor"]
        work_id = f"work:alphaengine-doc:{suffix}"
        call_id = f"connector-call:alphaengine-doc:{suffix}"
        invocation_id = f"connector-invocation:alphaengine-doc:{suffix}"
        artifact_ref = f"artifact:alphaengine-doc:{suffix}:raw"
        request_id = f"connector-runner-request:alphaengine-doc:{suffix}"
        grant_id = f"credential-grant:alphaengine-doc:{suffix}"

        work = WorkOrder(
            schema_version="0.1",
            id=work_id,
            created_at=created_at,
            updated_at=created_at,
            question=(
                f"Acquire AlphaEngine {self.plan['document_ref']} page "
                f"{page_request['page_ordinal']} into Core connector authority"
            ),
            requested_capabilities=(CAPABILITY_ID,),
            runtime_profile_ref=RUNNER_RUNTIME_REF,
            budget={"max_seconds": owner.lease_seconds},
            idempotency_key=work_id,
            declared_side_effects=(SIDE_EFFECT,),
            status="ready",
            input_refs=(call_id,),
        )
        work_hash = content_hash(work.to_dict())
        compiled = build_compiled_connector_plan(
            task_ref=work.id,
            task_hash=work_hash,
            planner_ref=PLANNER_REF,
            planner_hash=content_hash({"planner": PLANNER_REF}),
            routing_policy_ref=ROUTING_POLICY_REF,
            routing_policy_hash=content_hash({"routing": OPERATION}),
            step_specs=[
                {
                    "source_ref": profile["source_identity"]["source_ref"],
                    "source_hash": profile["source_hash"],
                    "connector_profile_ref": profile["id"],
                    "connector_profile_hash": profile["content_hash"],
                    "operation": OPERATION,
                    "parameters": parameters,
                    "input_schema_ref": owner.contract["input_schema_ref"],
                    "input_schema_hash": owner.contract["input_schema_hash"],
                    "output_schema_ref": owner.contract["output_schema_ref"],
                    "output_schema_hash": owner.contract["output_schema_hash"],
                    "completeness_required": owner.contract["completeness_ceiling"],
                    "depends_on": [],
                    "fallback_step_refs": [],
                    "max_attempts": 1,
                }
            ],
            created_at=created_at,
        )
        step = compiled["steps"][0]
        transport = build_live_mcp_transport_plan(compiled, step)
        resolver = StaticAdapterResolver(
            manifest,
            {binding["binding_ref"]: owner.adapter},
            {binding["binding_ref"]: lambda value, expected=parameters: value == expected},
        )
        gate = LiveMcpRunnerAdmissionGate(
            scheduler=owner.scheduler,
            catalog=owner.catalog,
            connectors=owner.connectors,
            resolver=resolver,
            visibility_scopes=list(VISIBILITY_SCOPES),
            clock=owner.clock,
            credential_authority=owner.credentials,
            transport_plans=[transport],
            compiled_plans=[compiled],
        )
        executor = ConnectorTransportExecutor(
            gate=gate,
            journal=owner.journal,
            spool=owner.spool,
            authority=owner.authority_port,
            connector_reader=owner.connectors,
            clock=owner.clock,
        )

        # Durable replay: a page that already responded is returned from the
        # journal without any new admission, lease, grant, or provider call.
        try:
            stored = owner.journal.request(request_id)
        except RunnerJournalNotFound:
            stored = None
        if stored is not None:
            latest = owner.journal.latest(request_id)
            if latest["state"] != "responded":
                raise AlphaEngineCoreAcquisitionError(
                    f"page runner request {request_id} is incomplete at durable "
                    f"state {latest['state']}; run transport recovery first"
                )
            self.replayed_pages += 1
            return executor.execute(stored, scheduler_lease_token="replay")

        call = owner.connectors.register_call_spec(
            {
                "schema_version": "0.1",
                "id": call_id,
                "created_at": created_at,
                "work_order_ref": work.id,
                "work_order_hash": work_hash,
                "connector_profile_ref": profile["id"],
                "operation": OPERATION,
                "parameters": parameters,
                "query_hash": content_hash(
                    {"operation": OPERATION, "parameters": parameters}
                ),
            },
            idempotency_key=f"{call_id}:register",
        )
        execution = ExecutionInvocation(
            schema_version="0.1",
            id=invocation_id,
            created_at=created_at,
            kind=ExecutionKind.CONNECTOR,
            work_order_ref=work.id,
            profile_ref=profile["id"],
            capability=CAPABILITY_ID,
            input_refs=(call["id"],),
            output_refs=(artifact_ref,),
            started_at=created_at,
            completed_at=None,
            side_effects=(),
            runtime_ref=profile["runner_runtime_ref"],
            actor_ref=profile["runner_actor_ref"],
            environment_hash=profile["runner_environment_hash"],
        )
        lease = self._prepare_lease(work, descriptor)
        invocation = owner.connectors.register_invocation(
            {
                "schema_version": "0.1",
                "id": invocation_id,
                "created_at": created_at,
                "work_order_ref": work.id,
                "work_order_hash": work_hash,
                "connector_profile_ref": profile["id"],
                "connector_profile_hash": profile["content_hash"],
                "call_spec_ref": call["id"],
                "call_spec_hash": call["content_hash"],
                "capability_lease_ref": lease.id,
                "capability_lease_hash": lease.content_hash,
                "descriptor_revision_ref": descriptor.revision_ref,
                "catalog_epoch": descriptor.catalog_epoch,
                "logical_invocation_key": "connector-logical:" + content_hash(
                    {
                        "work_order_ref": work.id,
                        "work_order_hash": work_hash,
                        "connector_profile_hash": profile["content_hash"],
                        "call_spec_hash": call["content_hash"],
                    }
                ),
            },
            execution=execution,
            idempotency_key=f"{invocation_id}:register",
        )
        enqueued = owner.scheduler.enqueue(work)
        if enqueued.get("status") == "conflict":
            raise AlphaEngineCoreAcquisitionError(
                f"scheduler already holds another WorkOrder for {work.id}"
            )
        claim = owner.scheduler.claim(
            RUNNER_ACTOR_REF, work_order_id=work.id, lease_seconds=owner.lease_seconds
        )
        if claim is None:
            raise AlphaEngineCoreAcquisitionError(
                f"WorkOrder {work.id} is not claimable; it may already be complete "
                "without a durable runner response"
            )
        now = owner.clock()
        grant_base = {
            "schema_version": "0.1",
            "id": grant_id,
            "created_at": _wire_time(now),
            "expires_at": _wire_time(now + timedelta(seconds=owner.grant_seconds)),
            "authority_ref": CREDENTIAL_AUTHORITY_REF,
            "grant_kind": "mcp_managed",
            "target_ref": ADAPTER_REF,
            "connector_profile_ref": profile["id"],
            "connector_profile_hash": profile["content_hash"],
            "capability_lease_ref": lease.id,
            "capability_lease_hash": lease.content_hash,
            "adapter_ref": ADAPTER_REF,
            "adapter_hash": alphaengine_adapter_hash(),
            "principal_ref": owner.governance.principal_ref,
            "credential_slot_refs": [CREDENTIAL_SLOT_REF],
            "allowed_operations": [OPERATION],
            "max_calls": 1,
        }
        owner.credentials.register_grant(
            _with_hash(grant_base), idempotency_key=f"{grant_id}:register"
        )
        runner_request = _with_hash(
            {
                "schema_version": "0.2",
                "id": request_id,
                "created_at": created_at,
                "connector_invocation_ref": invocation["id"],
                "connector_invocation_hash": invocation["content_hash"],
                "execution_ref": invocation["execution_ref"],
                "execution_hash": invocation["execution_hash"],
                "work_order_ref": work.id,
                "work_order_hash": work_hash,
                "scheduler_attempt_number": int(
                    claim["lease"].get("attempt_number")
                    or claim["attempt"]["attempt_number"]
                ),
                "scheduler_lease_revision_ref": claim["lease"]["id"],
                "scheduler_lease_hash": claim["lease"]["content_hash"],
                "connector_profile_ref": profile["id"],
                "connector_profile_hash": profile["content_hash"],
                "call_spec_ref": call["id"],
                "call_spec_hash": call["content_hash"],
                "capability_lease_ref": lease.id,
                "capability_lease_hash": lease.content_hash,
                "principal_ref": owner.governance.principal_ref,
                "runner_runtime_ref": profile["runner_runtime_ref"],
                "runner_actor_ref": profile["runner_actor_ref"],
                "runner_environment_hash": profile["runner_environment_hash"],
                "transport_plan_ref": transport["id"],
                "transport_plan_hash": transport["content_hash"],
                "compiled_connector_plan_ref": compiled["id"],
                "compiled_connector_plan_hash": compiled["content_hash"],
                "compiled_step_ref": step["id"],
                "compiled_step_hash": step["content_hash"],
                "idempotency_key": request_id,
            }
        )
        self.provider_calls += 1
        return executor.execute(
            runner_request, scheduler_lease_token=claim["lease_token"]
        )

    def _prepare_lease(self, work: WorkOrder, descriptor: Any) -> Any:
        """Prepare the capability lease before the invocation is registered."""

        owner = self.owner
        return owner.catalog.prepare(
            work,
            capability_id=CAPABILITY_ID,
            revision_ref=descriptor.revision_ref,
            catalog_epoch=descriptor.catalog_epoch,
            descriptor_hash=descriptor.content_hash,
            source_hash=descriptor.source_hash,
            schema_hash=descriptor.schema_hash,
            policy_ref=owner.governance.policy_ref,
            policy_hash=owner.governance.policy_hash(),
            principal_ref=owner.governance.principal_ref,
            visibility_scopes=list(VISIBILITY_SCOPES),
            ttl_seconds=owner.lease_seconds,
        )


def core_transcript_authority_probe(
    store: DaltonStore, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Report whether Core holds the exact authority the transcript gate needs.

    This is a read-only projection of the same checks that
    ``DaltonStore._commit_authorized_candidate`` applies to an
    ``authenticated_transcript`` candidate whose first artifact is the raw
    page-1 AlphaEngine response.
    """

    pages = manifest.get("pages") or []
    if not pages:
        return {"ok": False, "reason": "manifest has no pages"}
    first = pages[0]
    source_row = store.connection.execute(
        "SELECT connector_invocation_ref,record_json,content_hash FROM "
        "connector_source_envelopes WHERE source_envelope_id=?",
        (first["source_envelope_ref"],),
    ).fetchone()
    if source_row is None:
        return {"ok": False, "reason": "page-1 SourceEnvelope is not Core authority"}
    source = json.loads(source_row["record_json"])
    invocation = store.connection.execute(
        "SELECT execution_ref FROM connector_invocations WHERE connector_invocation_id=?",
        (source_row["connector_invocation_ref"],),
    ).fetchone()
    artifact = store.connection.execute(
        "SELECT v.content_hash,v.artifact_content_hash FROM "
        "observability_artifact_version_index i JOIN observability_artifact_versions_v2 v "
        "ON v.version_id=i.version_id WHERE i.version_id=? AND i.producer_execution_ref=?",
        (
            source["raw_artifact_version_ref"],
            None if invocation is None else invocation["execution_ref"],
        ),
    ).fetchone()
    digest = manifest["assembled_object"]["content_hash"]
    expected_record = f"{manifest['document_ref']}:sha256:{digest}"
    checks = {
        "source_envelope_hash_matches_manifest": (
            source_row["content_hash"] == first["source_envelope_hash"]
        ),
        "invocation_execution_present": invocation is not None,
        "artifact_bound_to_producer_execution": artifact is not None,
        "source_is_alphaengine_get_document": (
            source.get("source") == "source:alphaengine"
            and source.get("operation") == OPERATION
        ),
        "source_record_binds_assembled_digest": (
            source.get("source_record_refs") == [expected_record]
        ),
        "raw_response_hash_equals_artifact": (
            artifact is not None
            and source.get("raw_response_hash") == artifact["artifact_content_hash"]
        ),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "source_envelope_ref": first["source_envelope_ref"],
        "raw_artifact_version_ref": source.get("raw_artifact_version_ref"),
        "assembled_content_hash": digest,
    }


def write_governance_proposal(path: str | Path, *, approved_by: str) -> dict[str, Any]:
    """Write a ``proposed`` governance record for owner review (mode 0600)."""

    record = build_governance_record(approved_by=approved_by, status="proposed")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(canonical_json(record) + "\n", encoding="utf-8")
    os.chmod(target, 0o600)
    return record


__all__ = [
    "AlphaEngineCoreAcquisition",
    "AlphaEngineCoreAcquisitionError",
    "CAPABILITY_ID",
    "PAGE_MAX_RESPONSE_BYTES",
    "StaticConnectorGovernance",
    "build_governance_record",
    "core_transcript_authority_probe",
    "live_alphaengine_permissions",
    "write_governance_proposal",
]
