"""Core-hosted AlphaEngine ``search_library`` (P9d-1).

The M1/AE probe reads *known* AlphaEngine documents by exact ref.  Search-driven
discovery needs the other frozen operation of the same connector template:
``search_library``.  This module is the ``search_library`` twin of
``alphaengine_core_acquisition``: one governed capability, one connector
profile / price / rate policy, and a single-call executor that leaves the
usual Core-held connector authority behind it (``ConnectorInvocation``,
physical attempt, usage, cost, quota settlement, raw ``ArtifactVersion`` and a
``SourceEnvelope`` whose ``source_record_refs`` are the discovered
``alphaengine-doc:<id>`` refs).

It adds no contract.  The capability is published only against an
operator-reviewed governance record whose ``status`` is ``approved`` and whose
``expected_schema_hash`` binds exactly the ``search_library`` operation of the
packaged inventory; ``get_document`` keeps its own record and capability.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .alphaengine_core_acquisition import (
    ADAPTER_PACKAGE,
    ADAPTER_REF,
    CREDENTIAL_AUTHORITY_REF,
    CREDENTIAL_SLOT_REF,
    RESOLVER_REF,
    RUNNER_ACTOR_REF,
    RUNNER_RUNTIME_REF,
    SIDE_EFFECT,
    VISIBILITY_SCOPES,
    alphaengine_source_hash,
    live_alphaengine_permissions,
)
from .capability_catalog import CapabilityCatalog, CapabilityNotFound
from .connector import ConnectorConflict, ConnectorNotFound, ConnectorStore
from .connector_authority_port import (
    ConnectorAuthorityPort,
    ConnectorCompletionReceiptReader,
)
from .connector_governance import (
    ConnectorGovernance,
    ConnectorGovernanceError,
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
from .openclaw_connector_bridge import HostToolInvocationResult
from .raw_spool import RawSpool
from .research_context import build_compiled_connector_plan
from .runner_journal import RunnerJournal, RunnerJournalNotFound
from .scheduler import Scheduler
from .store import DaltonStore, canonical_json, content_hash


GOVERNANCE_SCHEMA_VERSION = "0.1"
SEARCH_KIND = "alphaengine-search-library"
SEARCH_CAPABILITY_ID = "capability:dalton:connector:alphaengine-search-library"
OPERATION = "search_library"
SEARCH_PROFILE_REF = "connector-profile:alphaengine-search-library:v1"
SEARCH_RATE_POLICY_REF = "connector-rate-policy:alphaengine-search-library"
SEARCH_PRICE_RATE_REF = "connector-price-rate:alphaengine-search-library:calls"
SEARCH_PLANNER_REF = "planner:dalton-core-alphaengine-search:0.1"
SEARCH_ROUTING_POLICY_REF = "routing:dalton-core-alphaengine-search:0.1"
# One ranked page of at most 20 hits with snippets; well under the adapter's
# 100-hit ceiling.  A bigger window is a new profile version.
SEARCH_MAX_RECORDS = 20
SEARCH_MAX_RESPONSE_BYTES = 512_000
# Document types the frozen live adapter can map to AlphaEngine categories.
SEARCH_DOCUMENT_TYPES: tuple[str, ...] = (
    "research_report", "foreign_report", "domestic_report", "sell_side_report",
    "sell_side_comment", "meeting_minutes", "announcement", "news",
)
_GEOGRAPHIES = frozenset({"US", "HK", "A"})
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_DOC_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SPEC_FIELDS = frozenset({"query", "filters", "cursor"})
_FILTER_FIELDS = frozenset({
    "company", "date_from", "date_to", "document_type", "geography", "industry",
})


class AlphaEngineCoreSearchError(RuntimeError):
    """A search request, governance record or authority binding is invalid."""


def _wire_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AlphaEngineCoreSearchError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise AlphaEngineCoreSearchError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _with_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = json.loads(canonical_json(value))
    wire["content_hash"] = content_hash(wire)
    return wire


# ---------------------------------------------------------------------------
# frozen contract identity
# ---------------------------------------------------------------------------
def alphaengine_search_library_contract() -> tuple[dict[str, Any], dict[str, Any]]:
    template = load_packaged_connector_inventory()["templates"]["alphaengine"]
    matches = [item for item in template["operations"] if item["operation"] == OPERATION]
    if len(matches) != 1:
        raise AlphaEngineCoreSearchError("AlphaEngine search_library is not frozen")
    return template, matches[0]


def alphaengine_search_schema_hash() -> str:
    _, contract = alphaengine_search_library_contract()
    return content_hash(
        {
            "allowed_operations": [OPERATION],
            "input_schema_refs": {OPERATION: contract["input_schema_ref"]},
            "input_schema_hashes": {OPERATION: contract["input_schema_hash"]},
            "output_schema_refs": {OPERATION: contract["output_schema_ref"]},
            "output_schema_hashes": {OPERATION: contract["output_schema_hash"]},
        }
    )


def alphaengine_search_adapter_hash() -> str:
    return content_hash(
        {
            "target_ref": ADAPTER_REF,
            "package": ADAPTER_PACKAGE,
            "bridge_hash": OPENCLAW_ALPHAENGINE_BRIDGE_HASH,
            "operation": OPERATION,
        }
    )


def build_search_governance_record(
    *,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-08-26T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Closed, hash-bound governance record for the search capability.

    The default ``effective_from`` matches the generic
    ``connector_governance.build_governance_record`` default so the two
    builders produce byte-identical records for the same inputs.
    """

    if status not in {"proposed", "approved"}:
        raise AlphaEngineCoreSearchError("governance status must be proposed or approved")
    base = {
        "schema_version": GOVERNANCE_SCHEMA_VERSION,
        "id": f"connector-governance:{SEARCH_KIND}:v{version}",
        "status": status,
        "capability_id": SEARCH_CAPABILITY_ID,
        "approved_by": approved_by,
        "principal_ref": "principal:dalton-core-trusted-runner",
        "policy_ref": f"policy:dalton:connector-governance:{SEARCH_KIND}:v{version}",
        "approval_ref": f"approval:connector-governance:{SEARCH_KIND}:v{version}",
        "decision_ref": f"capability-decision:connector-governance:{SEARCH_KIND}:v{version}",
        "registry_revision_ref": f"{SEARCH_CAPABILITY_ID}@v{version}",
        "attestation_ref": f"attestation:connector-governance:{SEARCH_KIND}:v{version}",
        "effective_from": _wire_time(_parse_time(effective_from, "effective_from")),
        "effective_until": None,
        "max_lease_seconds": max_lease_seconds,
        "allowed_permissions": live_alphaengine_permissions(),
        "expected_source_hash": alphaengine_source_hash(),
        "expected_schema_hash": alphaengine_search_schema_hash(),
    }
    return _with_hash(base)


class SearchConnectorGovernance(ConnectorGovernance):
    """Generic governance narrowed to the search capability."""

    def __init__(self, value: Mapping[str, Any]) -> None:
        try:
            super().__init__(value)
        except ConnectorGovernanceError as exc:
            raise AlphaEngineCoreSearchError(str(exc)) from exc
        if self.capability_id != SEARCH_CAPABILITY_ID:
            raise AlphaEngineCoreSearchError("governance capability_id is not the search connector")

    @classmethod
    def load(cls, path: str | Path) -> "SearchConnectorGovernance":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def _require_approved(self) -> None:
        try:
            super()._require_approved()
        except ConnectorGovernanceError as exc:
            raise AlphaEngineCoreSearchError(str(exc)) from exc


def write_search_governance_proposal(path: str | Path, *, approved_by: str) -> dict[str, Any]:
    record = build_search_governance_record(approved_by=approved_by, status="proposed")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(canonical_json(record) + "\n", encoding="utf-8")
    os.chmod(target, 0o600)
    return record


# ---------------------------------------------------------------------------
# search spec
# ---------------------------------------------------------------------------
def validate_search_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one ``search_library`` parameter object (closed shape)."""

    if not isinstance(value, Mapping) or set(value) != _SPEC_FIELDS:
        raise AlphaEngineCoreSearchError(
            "search spec must have exactly query, filters and cursor"
        )
    query = value["query"]
    if not isinstance(query, str) or not query.strip() or len(query) > 400:
        raise AlphaEngineCoreSearchError("search query must be non-empty text (<=400 chars)")
    filters = value["filters"]
    if not isinstance(filters, Mapping) or not set(filters) <= _FILTER_FIELDS:
        raise AlphaEngineCoreSearchError("search filters have an unknown field")
    cleaned: dict[str, Any] = {}
    for name in sorted(filters):
        item = filters[name]
        if item is None:
            continue
        if not isinstance(item, str) or not item:
            raise AlphaEngineCoreSearchError(f"search filter {name} must be non-empty text")
        cleaned[name] = item
    if cleaned.get("document_type") not in SEARCH_DOCUMENT_TYPES:
        raise AlphaEngineCoreSearchError("search document_type is not a mapped AlphaEngine type")
    if ("date_from" in cleaned) != ("date_to" in cleaned):
        raise AlphaEngineCoreSearchError("search requires both date bounds or neither")
    if "date_from" in cleaned:
        for name in ("date_from", "date_to"):
            if _DATE_RE.fullmatch(cleaned[name]) is None:
                raise AlphaEngineCoreSearchError(f"search {name} must be YYYY-MM-DD")
        if cleaned["date_from"] > cleaned["date_to"]:
            raise AlphaEngineCoreSearchError("search date window is reversed")
    if "geography" in cleaned and cleaned["geography"] not in _GEOGRAPHIES:
        raise AlphaEngineCoreSearchError("search geography is not mapped")
    cursor = value["cursor"]
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise AlphaEngineCoreSearchError("search cursor must be null or non-empty text")
    return {"query": query.strip(), "filters": cleaned, "cursor": cursor}


def search_spec_hash(spec: Mapping[str, Any]) -> str:
    return content_hash({"operation": OPERATION, "parameters": validate_search_spec(spec)})


def register_chained_profile(
    connectors: ConnectorStore, profile_wire: Mapping[str, Any], *, idempotency_key: str
) -> dict[str, Any]:
    """Register a connector profile at the next version of its connector chain.

    ``ConnectorStore`` keeps one contiguous version chain per ``connector_ref``
    and the live MCP gate requires the profile's ``connector_ref`` to equal the
    template's, so the ``get_document`` and ``search_library`` profiles of the
    AlphaEngine connector share a chain.  Whichever registers first is v1; the
    other becomes v2 with ``prior_version_ref`` bound to it.  A profile whose
    id already exists is returned as-is (its chain position is already fixed),
    so replays never recompute a different version.
    """

    try:
        existing = connectors.get_profile(profile_wire["id"])
    except ConnectorNotFound:
        existing = None
    if existing is not None:
        for field in ("capability_id", "source_hash", "schema_hash", "adapter_hash",
                      "allowed_operations", "connector_ref"):
            if existing.get(field) != profile_wire.get(field):
                raise ConnectorConflict(
                    f"existing connector profile {profile_wire['id']} differs in {field}"
                )
        return existing
    latest = connectors.connection.execute(
        "SELECT profile_version_id,version_number FROM connector_profile_versions "
        "WHERE connector_ref=? ORDER BY version_number DESC LIMIT 1",
        (profile_wire["connector_ref"],),
    ).fetchone()
    wire = dict(profile_wire)
    wire["version"] = 1 if latest is None else int(latest["version_number"]) + 1
    wire["prior_version_ref"] = None if latest is None else latest["profile_version_id"]
    return connectors.register_profile(wire, idempotency_key=idempotency_key)


def alphaengine_documents_in_authority(connection: Any, document_refs: list[str]) -> list[str]:
    """Return the subset of ``alphaengine-doc:`` refs Core holds a successful page for."""

    from .bounded_alphaengine_probe import document_in_authority

    return [ref for ref in document_refs if document_in_authority(connection, ref)]


# ---------------------------------------------------------------------------
# rehearsal handle
# ---------------------------------------------------------------------------
class FakeSearchHandle:
    """Host-owned loopback stand-in returning one canned ranked page."""

    def __init__(self, results: list[Mapping[str, Any]]) -> None:
        self.results = [dict(item) for item in results]
        self.calls: list[dict[str, Any]] = []

    def invoke(self, tool_name, arguments, *, call_ref, deadline_at, max_response_bytes):
        del deadline_at, max_response_bytes
        if tool_name != OPERATION:
            raise RuntimeError("fake handle serves search_library only")
        self.calls.append({"arguments": dict(arguments), "call_ref": call_ref})
        limit = int(arguments.get("limit", SEARCH_MAX_RECORDS))
        payload = {
            "results": self.results[:limit],
            "cursor": None,
            "has_more": False,
            "total": len(self.results[:limit]),
        }
        result = {"content": [{"type": "text", "text": canonical_json(payload)}]}
        request_id = f"provider-request:fake-search:{len(self.calls)}"
        raw = canonical_json({"jsonrpc": "2.0", "id": request_id, "result": result}).encode("utf-8")
        return HostToolInvocationResult(request_id=request_id, raw_response=raw, result=result)


# ---------------------------------------------------------------------------
# governed search
# ---------------------------------------------------------------------------
class AlphaEngineCoreSearch:
    """Run one governed ``search_library`` call into Core connector authority."""

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
        governance: SearchConnectorGovernance,
        mcp_handle: Any,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 60,
        grant_seconds: int = 900,
    ) -> None:
        if type(store) is not DaltonStore:
            raise TypeError("search requires an exact DaltonStore")
        if type(connectors) is not ConnectorStore or connectors.connection is not store.connection:
            raise TypeError("search requires the Core ConnectorStore")
        if type(observability) is not ObservabilityStore or observability.connection is not store.connection:
            raise TypeError("search requires the Core ObservabilityStore")
        if type(journal) is not RunnerJournal:
            raise TypeError("search requires the Core RunnerJournal")
        if type(spool) is not RawSpool:
            raise TypeError("search requires an exact RawSpool")
        if not isinstance(governance, SearchConnectorGovernance):
            raise TypeError("search requires SearchConnectorGovernance")
        if not callable(getattr(mcp_handle, "invoke", None)):
            raise TypeError("mcp_handle must expose invoke")
        for name, value in (("lease_seconds", lease_seconds), ("grant_seconds", grant_seconds)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise AlphaEngineCoreSearchError(f"{name} must be a positive integer")
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
        self.template, self.contract = alphaengine_search_library_contract()
        self.adapter = AlphaEngineLiveAdapter()
        self.receipts = ConnectorCompletionReceiptReader(
            connectors=connectors, observability=observability
        )
        self.authority_port = ConnectorAuthorityPort(
            connectors=connectors, observability=observability, scheduler=scheduler,
            receipt_reader=self.receipts,
        )
        self._authorities: dict[str, Any] | None = None

    # -- governed authorities ------------------------------------------------
    def _descriptor_spec(self) -> dict[str, Any]:
        return {
            "schema_version": "0.1",
            "id": SEARCH_CAPABILITY_ID,
            "version": 1,
            "created_at": self.governance.effective_from,
            "kind": "connector",
            "name": "alphaengine-search-library",
            "label": "Core-hosted AlphaEngine search_library",
            "summary": (
                "Run one ranked AlphaEngine library search through the host-owned "
                "loopback MCP bridge into Core connector authority"
            ),
            "aliases": ["alphaengine search", "discover alphaengine documents"],
            "tags": ["connector", "alphaengine", "read-only", "mcp-managed", "search"],
            "intent_examples": [
                "find recent AlphaEngine transcripts for a covered company",
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
            "schema_hash": alphaengine_search_schema_hash(),
        }

    def ensure_governed_authorities(self) -> dict[str, Any]:
        if self._authorities is not None:
            return self._authorities
        self.governance._require_approved()
        spec = self._descriptor_spec()
        try:
            descriptor = self.catalog.describe(
                SEARCH_CAPABILITY_ID, visibility_scopes=list(VISIBILITY_SCOPES)
            )
        except CapabilityNotFound:
            descriptor = self.catalog.publish(spec)
        if (
            descriptor.source_hash != spec["source_hash"]
            or descriptor.schema_hash != spec["schema_hash"]
            or descriptor.eligibility.policy_ref != self.governance.policy_ref
            or descriptor.permissions.to_dict() != spec["permissions"]
        ):
            raise AlphaEngineCoreSearchError(
                "published search capability differs from governed spec"
            )
        binding = {
            "binding_ref": "runner-binding:alphaengine-search-library:0.1",
            "descriptor_revision_ref": descriptor.revision_ref,
            "descriptor_hash": descriptor.content_hash,
            "adapter_ref": ADAPTER_REF,
            "adapter_hash": alphaengine_search_adapter_hash(),
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
            "rate_policy_ref": SEARCH_RATE_POLICY_REF,
        }
        manifest_base = {
            "schema_version": "0.1",
            "id": "runner-environment:dalton-core-alphaengine-search-library:0.1",
            "created_at": self.governance.effective_from,
            "runner_runtime_ref": RUNNER_RUNTIME_REF,
            "runner_actor_ref": RUNNER_ACTOR_REF,
            "resolver_ref": RESOLVER_REF,
            "resolver_version": "0.1",
            "package_manifest_ref": "artifact:runner-packages:alphaengine-search-library:0.1",
            "package_manifest_hash": content_hash(
                {
                    "package": ADAPTER_PACKAGE,
                    "bridge_hash": OPENCLAW_ALPHAENGINE_BRIDGE_HASH,
                    "adapter_hash": alphaengine_search_adapter_hash(),
                }
            ),
            "bindings": [binding],
        }
        manifest = validate_runner_environment_manifest(_with_hash(manifest_base))
        profile_wire = {
            "schema_version": "0.1",
            "id": SEARCH_PROFILE_REF,
            "created_at": self.governance.effective_from,
            "connector_ref": self.template["connector_ref"],
            # version / prior_version_ref are assigned by register_chained_profile
            "version": None,
            "prior_version_ref": None,
            "capability_id": SEARCH_CAPABILITY_ID,
            "descriptor_revision_ref": descriptor.revision_ref,
            "descriptor_hash": descriptor.content_hash,
            "source_identity": dict(self.template["source_identity"]),
            "source_hash": alphaengine_source_hash(),
            "schema_hash": alphaengine_search_schema_hash(),
            "catalog_epoch": descriptor.catalog_epoch,
            "adapter_ref": ADAPTER_REF,
            "adapter_hash": alphaengine_search_adapter_hash(),
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
            "max_response_bytes": SEARCH_MAX_RESPONSE_BYTES,
            "max_records": SEARCH_MAX_RECORDS,
            "timeout_ms": 120_000,
            "access_policy_ref": "policy:access:alphaengine",
            "retention_policy_ref": "policy:retention:licensed-research",
            "terms_policy_ref": "policy:terms:alphaengine",
            "network_policy": None,
        }
        profile = register_chained_profile(
            self.connectors, profile_wire, idempotency_key="alphaengine-search-library:profile:v1"
        )
        price = self.connectors.register_price_rate(
            {
                "schema_version": "0.1",
                "id": f"{SEARCH_PRICE_RATE_REF}:v1",
                "created_at": self.governance.effective_from,
                "price_rate_ref": SEARCH_PRICE_RATE_REF,
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
            idempotency_key="alphaengine-search-library:price:v1",
        )
        quota = governed_daily_quota("alphaengine", OPERATION)
        price_book = {"price_rate_refs": [price["id"]], "required_price_meters": ["calls"]}
        rate_policy = self.connectors.register_rate_policy(
            {
                "schema_version": "0.1",
                "id": f"{SEARCH_RATE_POLICY_REF}:v1",
                "created_at": self.governance.effective_from,
                "policy_ref": SEARCH_RATE_POLICY_REF,
                "quota_scope_ref": "connector-quota-scope:alphaengine:search_library",
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
            idempotency_key="alphaengine-search-library:rate-policy:v1",
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

    # -- one search ------------------------------------------------------------
    def build_request(self, spec: Mapping[str, Any], *, created_at: str | None = None) -> dict[str, Any]:
        """Bind one validated spec to a creation time; the pair is the request identity."""

        parameters = validate_search_spec(spec)
        created = created_at or _wire_time(self.clock())
        _parse_time(created, "created_at")
        identity = {"operation": OPERATION, "parameters": parameters, "created_at": created}
        return {
            "operation": OPERATION,
            "parameters": parameters,
            "created_at": created,
            "query_hash": search_spec_hash(parameters),
            "request_hash": content_hash(identity),
        }

    def search(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Execute (or durably replay) one search; return the bound discovery receipt."""

        if not isinstance(request, Mapping) or set(request) != {
            "operation", "parameters", "created_at", "query_hash", "request_hash",
        }:
            raise AlphaEngineCoreSearchError("search request has an invalid closed shape")
        rebuilt = self.build_request(request["parameters"], created_at=request["created_at"])
        if rebuilt != dict(request):
            raise AlphaEngineCoreSearchError("search request identity drifted")
        parameters = rebuilt["parameters"]
        created_at = rebuilt["created_at"]
        suffix = rebuilt["request_hash"][:20]
        authorities = self.ensure_governed_authorities()
        profile = authorities["profile"]
        descriptor = authorities["descriptor"]
        binding = authorities["binding"]
        manifest = authorities["manifest"]

        work_id = f"work:alphaengine-search:{suffix}"
        call_id = f"connector-call:alphaengine-search:{suffix}"
        invocation_id = f"connector-invocation:alphaengine-search:{suffix}"
        artifact_ref = f"artifact:alphaengine-search:{suffix}:raw"
        request_id = f"connector-runner-request:alphaengine-search:{suffix}"
        grant_id = f"credential-grant:alphaengine-search:{suffix}"

        work = WorkOrder(
            schema_version="0.1",
            id=work_id,
            created_at=created_at,
            updated_at=created_at,
            question=(
                f"Search the AlphaEngine library for {parameters['filters']['document_type']} "
                "documents into Core connector authority"
            ),
            requested_capabilities=(SEARCH_CAPABILITY_ID,),
            runtime_profile_ref=RUNNER_RUNTIME_REF,
            budget={"max_seconds": self.lease_seconds},
            idempotency_key=work_id,
            declared_side_effects=(SIDE_EFFECT,),
            status="ready",
            input_refs=(call_id,),
        )
        work_hash = content_hash(work.to_dict())
        compiled = build_compiled_connector_plan(
            task_ref=work.id,
            task_hash=work_hash,
            planner_ref=SEARCH_PLANNER_REF,
            planner_hash=content_hash({"planner": SEARCH_PLANNER_REF}),
            routing_policy_ref=SEARCH_ROUTING_POLICY_REF,
            routing_policy_hash=content_hash({"routing": OPERATION}),
            step_specs=[
                {
                    "source_ref": profile["source_identity"]["source_ref"],
                    "source_hash": profile["source_hash"],
                    "connector_profile_ref": profile["id"],
                    "connector_profile_hash": profile["content_hash"],
                    "operation": OPERATION,
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
            created_at=created_at,
        )
        step = compiled["steps"][0]
        transport = build_live_mcp_transport_plan(compiled, step)
        resolver = StaticAdapterResolver(
            manifest,
            {binding["binding_ref"]: self.adapter},
            {binding["binding_ref"]: lambda value, expected=parameters: value == expected},
        )
        gate = LiveMcpRunnerAdmissionGate(
            scheduler=self.scheduler,
            catalog=self.catalog,
            connectors=self.connectors,
            resolver=resolver,
            visibility_scopes=list(VISIBILITY_SCOPES),
            clock=self.clock,
            credential_authority=self.credentials,
            transport_plans=[transport],
            compiled_plans=[compiled],
        )
        executor = ConnectorTransportExecutor(
            gate=gate,
            journal=self.journal,
            spool=self.spool,
            authority=self.authority_port,
            connector_reader=self.connectors,
            clock=self.clock,
        )

        try:
            stored = self.journal.request(request_id)
        except RunnerJournalNotFound:
            stored = None
        replayed = False
        if stored is not None:
            latest = self.journal.latest(request_id)
            if latest["state"] != "responded":
                raise AlphaEngineCoreSearchError(
                    f"search runner request {request_id} is incomplete at durable "
                    f"state {latest['state']}; run transport recovery first"
                )
            replayed = True
            response = executor.execute(stored, scheduler_lease_token="replay")
        else:
            call = self.connectors.register_call_spec(
                {
                    "schema_version": "0.1",
                    "id": call_id,
                    "created_at": created_at,
                    "work_order_ref": work.id,
                    "work_order_hash": work_hash,
                    "connector_profile_ref": profile["id"],
                    "operation": OPERATION,
                    "parameters": parameters,
                    "query_hash": content_hash({"operation": OPERATION, "parameters": parameters}),
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
                capability=SEARCH_CAPABILITY_ID,
                input_refs=(call["id"],),
                output_refs=(artifact_ref,),
                started_at=created_at,
                completed_at=None,
                side_effects=(),
                runtime_ref=profile["runner_runtime_ref"],
                actor_ref=profile["runner_actor_ref"],
                environment_hash=profile["runner_environment_hash"],
            )
            lease = self.catalog.prepare(
                work,
                capability_id=SEARCH_CAPABILITY_ID,
                revision_ref=descriptor.revision_ref,
                catalog_epoch=descriptor.catalog_epoch,
                descriptor_hash=descriptor.content_hash,
                source_hash=descriptor.source_hash,
                schema_hash=descriptor.schema_hash,
                policy_ref=self.governance.policy_ref,
                policy_hash=self.governance.policy_hash(),
                principal_ref=self.governance.principal_ref,
                visibility_scopes=list(VISIBILITY_SCOPES),
                ttl_seconds=self.lease_seconds,
            )
            invocation = self.connectors.register_invocation(
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
            enqueued = self.scheduler.enqueue(work)
            if enqueued.get("status") == "conflict":
                raise AlphaEngineCoreSearchError(
                    f"scheduler already holds another WorkOrder for {work.id}"
                )
            claim = self.scheduler.claim(
                RUNNER_ACTOR_REF, work_order_id=work.id, lease_seconds=self.lease_seconds
            )
            if claim is None:
                raise AlphaEngineCoreSearchError(
                    f"WorkOrder {work.id} is not claimable; it may already be complete "
                    "without a durable runner response"
                )
            now = self.clock()
            grant_base = {
                "schema_version": "0.1",
                "id": grant_id,
                "created_at": _wire_time(now),
                "expires_at": _wire_time(now + timedelta(seconds=self.grant_seconds)),
                "authority_ref": CREDENTIAL_AUTHORITY_REF,
                "grant_kind": "mcp_managed",
                "target_ref": ADAPTER_REF,
                "connector_profile_ref": profile["id"],
                "connector_profile_hash": profile["content_hash"],
                "capability_lease_ref": lease.id,
                "capability_lease_hash": lease.content_hash,
                "adapter_ref": ADAPTER_REF,
                "adapter_hash": alphaengine_search_adapter_hash(),
                "principal_ref": self.governance.principal_ref,
                "credential_slot_refs": [CREDENTIAL_SLOT_REF],
                "allowed_operations": [OPERATION],
                "max_calls": 1,
            }
            self.credentials.register_grant(
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
                    "principal_ref": self.governance.principal_ref,
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
            response = executor.execute(runner_request, scheduler_lease_token=claim["lease_token"])
        return self._receipt(rebuilt, response, profile, replayed=replayed)

    def _receipt(
        self,
        request: Mapping[str, Any],
        response: Mapping[str, Any],
        profile: Mapping[str, Any],
        *,
        replayed: bool,
    ) -> dict[str, Any]:
        invocation = self.receipts.get_invocation(response["connector_invocation_ref"])
        if invocation["content_hash"] != response["connector_invocation_hash"]:
            raise AlphaEngineCoreSearchError("search invocation authority drifted")
        base: dict[str, Any] = {
            "operation": OPERATION,
            "parameters": dict(request["parameters"]),
            "created_at": request["created_at"],
            "query_hash": request["query_hash"],
            "request_hash": request["request_hash"],
            "connector_profile_ref": profile["id"],
            "connector_profile_hash": profile["content_hash"],
            "connector_invocation_ref": invocation["id"],
            "connector_invocation_hash": invocation["content_hash"],
            "runner_response_ref": response["id"],
            "outcome": response["outcome"],
            "replayed": replayed,
            "provider_calls": 0 if replayed else 1,
            "source_envelope_ref": None,
            "source_envelope_hash": None,
            "raw_artifact_version_ref": None,
            "document_refs": [],
            "next_cursor": None,
            "source_status": None,
        }
        if response["outcome"] != "succeeded" or response["source_envelope_ref"] is None:
            return base
        source = self.receipts.get_source_envelope(response["source_envelope_ref"])
        if source is None or source["content_hash"] != response["source_envelope_hash"]:
            raise AlphaEngineCoreSearchError("search source envelope authority drifted")
        if (
            source["connector_invocation_ref"] != invocation["id"]
            or source["operation"] != OPERATION
            or source["source"] != profile["source_identity"]["source_ref"]
        ):
            raise AlphaEngineCoreSearchError("search source envelope does not bind the call")
        document_refs: list[str] = []
        for ref in source["source_record_refs"]:
            doc_id = ref.removeprefix("alphaengine-doc:")
            if ref == doc_id or _DOC_ID_RE.fullmatch(doc_id) is None:
                raise AlphaEngineCoreSearchError("search source record is not an AlphaEngine doc ref")
            document_refs.append(ref)
        base.update({
            "source_envelope_ref": source["id"],
            "source_envelope_hash": source["content_hash"],
            "raw_artifact_version_ref": source["raw_artifact_version_ref"],
            "document_refs": document_refs,
            "next_cursor": source["cursor"],
            "source_status": source["status"],
        })
        return base


__all__ = [
    "AlphaEngineCoreSearch",
    "AlphaEngineCoreSearchError",
    "FakeSearchHandle",
    "OPERATION",
    "SEARCH_CAPABILITY_ID",
    "SEARCH_DOCUMENT_TYPES",
    "SEARCH_KIND",
    "SEARCH_MAX_RECORDS",
    "SEARCH_PROFILE_REF",
    "SearchConnectorGovernance",
    "alphaengine_documents_in_authority",
    "alphaengine_search_schema_hash",
    "build_search_governance_record",
    "register_chained_profile",
    "search_spec_hash",
    "validate_search_spec",
    "write_search_governance_proposal",
]
