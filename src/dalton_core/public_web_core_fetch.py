"""Core-hosted public-web ``fetch_get`` (P9d-4b).

Web search (P9d-4a) leaves opaque ``public-web-url:sha256:<hash>`` refs
behind.  This module is the lane that turns one such ref into authority: it
rebuilds the URL authority from the exact raw search artifact and envelope,
resolves it through ``PublicWebFetchAdapter`` (credential-free public HTTPS
only) and leaves the usual Core-held connector authority behind
(``ConnectorInvocation``, physical attempt, usage, cost, quota settlement,
raw ``ArtifactVersion`` holding the original bytes, and a ``SourceEnvelope``
whose single ``source_record_ref`` names the final URL and body hash).

Two properties are frozen here:

* the runner hands the adapter the profile's static host list and the
  adapter demands it equal the URL authority's host exactly, so the lane
  publishes one operation-scoped connector profile *per host* (all on the
  ``connector:web-fetch`` chain) and never a wildcard profile;
* nothing here interprets the page.  Bytes enter authority; turning them
  into candidates is the human extraction path (ADR-0003 B).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .alphaengine_core_acquisition import RUNNER_ACTOR_REF, RUNNER_RUNTIME_REF, VISIBILITY_SCOPES
from .alphaengine_core_search import register_chained_profile
from .capability_catalog import CapabilityCatalog, CapabilityNotFound
from .connector import ConnectorStore
from .connector_authority_port import ConnectorAuthorityPort, ConnectorCompletionReceiptReader
from .connector_governance import ConnectorGovernance, ConnectorGovernanceError
from .connector_inventory import load_packaged_connector_inventory
from .connector_quota_policy import apply_governed_quota_to_limits, governed_daily_quota
from .connector_runner import (
    ConnectorRunnerAdmissionGate,
    StaticAdapterResolver,
    validate_runner_environment_manifest,
)
from .connector_transport_executor import ConnectorTransportExecutor
from .contracts import ExecutionInvocation, ExecutionKind, WorkOrder
from .observability import ObservabilityStore
from .public_http_transport import PublicHttpTransport
from .public_web_connector import (
    PublicWebFetchAdapter,
    PublicWebUrlAuthorityResolver,
    build_public_web_url_authorities,
    validate_public_web_url_authority,
)
from .raw_spool import RawSpool
from .runner_journal import RunnerJournal, RunnerJournalNotFound
from .scheduler import Scheduler
from .store import DaltonStore, canonical_json, content_hash


GOVERNANCE_SCHEMA_VERSION = "0.1"
FETCH_KIND = "web-fetch"
TEMPLATE_KEY = "web-fetch"
FETCH_CAPABILITY_ID = "capability:dalton:connector:web-fetch"
OPERATION = "fetch_get"
FETCH_PROFILE_PREFIX = "connector-profile:web-fetch"
FETCH_RATE_POLICY_PREFIX = "connector-rate-policy:web-fetch"
FETCH_PRICE_RATE_PREFIX = "connector-price-rate:web-fetch"
FETCH_QUOTA_SCOPE_REF = "connector-quota-scope:web-fetch:fetch_get"
# The adapter target is the inventory template's public transport target.
ADAPTER_REF = "transport:public-http:0.1"
ADAPTER_PACKAGE = "dalton-public-web-fetch-adapter:0.1"
SIDE_EFFECT = "read:public-http"
DEFAULT_USER_AGENT = "Dalton Research Agent OS public-web fetch lane (owner: lumos)"
# One page per call; a bigger page is a new profile version.
FETCH_MAX_RESPONSE_BYTES = 4_000_000
FETCH_MAX_RECORDS = 1
FETCH_TIMEOUT_MS = 60_000
MANIFEST_SCHEMA_VERSION = "0.1"
TRAILING_WINDOW = timedelta(hours=24)
_URL_REF_RE = re.compile(r"^public-web-url:sha256:[0-9a-f]{64}$")
_DOCUMENT_REF_RE = re.compile(
    r"^public-web-document:url-sha256:[0-9a-f]{64}:body-sha256:[0-9a-f]{64}$"
)
_MANIFEST_FIELDS = frozenset({
    "schema_version", "id", "created_at", "document_ref", "url_ref", "canonical_url", "host",
    "final_url", "source_record_ref", "body_sha256", "body_bytes", "raw_media_type",
    "url_authority_ref", "url_authority_hash", "discovery_source_envelope_ref",
    "discovery_source_envelope_hash", "connector_profile_ref", "connector_profile_hash",
    "connector_invocation_ref", "connector_invocation_hash", "source_envelope_ref",
    "source_envelope_hash", "raw_artifact_version_ref", "raw_response_hash", "content_hash",
})


class PublicWebCoreFetchError(RuntimeError):
    """A fetch request, governance record or authority binding is invalid."""


def _wire_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise PublicWebCoreFetchError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise PublicWebCoreFetchError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _with_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = json.loads(canonical_json(value))
    wire["content_hash"] = content_hash(wire)
    return wire


def host_slug(host: str) -> str:
    """Stable, ref-safe identity for one public host (lowercase, hashed)."""

    if not isinstance(host, str) or not host or host != host.lower() or "/" in host:
        raise PublicWebCoreFetchError("host must be a lowercase hostname")
    return hashlib.sha256(host.encode("utf-8")).hexdigest()[:20]


# ---------------------------------------------------------------------------
# frozen contract identity
# ---------------------------------------------------------------------------
def web_fetch_contract() -> tuple[dict[str, Any], dict[str, Any]]:
    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    matches = [item for item in template["operations"] if item["operation"] == OPERATION]
    if len(matches) != 1:
        raise PublicWebCoreFetchError("public-web fetch_get is not frozen")
    if template["transport"]["target_ref"] != ADAPTER_REF:
        raise PublicWebCoreFetchError("public-web transport target drifted")
    return template, matches[0]


def web_fetch_source_hash() -> str:
    template, _ = web_fetch_contract()
    return content_hash(template["source_identity"])


def web_fetch_schema_hash() -> str:
    _, contract = web_fetch_contract()
    return content_hash(
        {
            "allowed_operations": [OPERATION],
            "input_schema_refs": {OPERATION: contract["input_schema_ref"]},
            "input_schema_hashes": {OPERATION: contract["input_schema_hash"]},
            "output_schema_refs": {OPERATION: contract["output_schema_ref"]},
            "output_schema_hashes": {OPERATION: contract["output_schema_hash"]},
        }
    )


def web_fetch_adapter_hash() -> str:
    return content_hash(
        {"target_ref": ADAPTER_REF, "package": ADAPTER_PACKAGE, "operation": OPERATION}
    )


def web_fetch_fixture_hash() -> str:
    template, _ = web_fetch_contract()
    return template["fixture_manifest_hash"]


def web_fetch_permissions() -> dict[str, Any]:
    """Permissions of the credential-free, public-HTTPS-only fetch capability."""

    return {
        "risk_class": "low",
        "network": True,
        "filesystem_read": [],
        "filesystem_write": ["runner:raw-sink"],
        "credential_slot_refs": [],
        "core_db": False,
        "side_effects": [SIDE_EFFECT],
    }


def build_web_fetch_governance_record(
    *,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-08-26T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Closed, hash-bound governance record for the public-web fetch capability."""

    if status not in {"proposed", "approved"}:
        raise PublicWebCoreFetchError("governance status must be proposed or approved")
    base = {
        "schema_version": GOVERNANCE_SCHEMA_VERSION,
        "id": f"connector-governance:{FETCH_KIND}:v{version}",
        "status": status,
        "capability_id": FETCH_CAPABILITY_ID,
        "approved_by": approved_by,
        "principal_ref": "principal:dalton-core-trusted-runner",
        "policy_ref": f"policy:dalton:connector-governance:{FETCH_KIND}:v{version}",
        "approval_ref": f"approval:connector-governance:{FETCH_KIND}:v{version}",
        "decision_ref": f"capability-decision:connector-governance:{FETCH_KIND}:v{version}",
        "registry_revision_ref": f"{FETCH_CAPABILITY_ID}@v{version}",
        "attestation_ref": f"attestation:connector-governance:{FETCH_KIND}:v{version}",
        "effective_from": _wire_time(_parse_time(effective_from, "effective_from")),
        "effective_until": None,
        "max_lease_seconds": max_lease_seconds,
        "allowed_permissions": web_fetch_permissions(),
        "expected_source_hash": web_fetch_source_hash(),
        "expected_schema_hash": web_fetch_schema_hash(),
    }
    return _with_hash(base)


class WebFetchConnectorGovernance(ConnectorGovernance):
    """Generic governance narrowed to the public-web fetch capability."""

    def __init__(self, value: Mapping[str, Any]) -> None:
        try:
            super().__init__(value)
        except ConnectorGovernanceError as exc:
            raise PublicWebCoreFetchError(str(exc)) from exc
        if self.capability_id != FETCH_CAPABILITY_ID:
            raise PublicWebCoreFetchError("governance capability_id is not the public-web fetch connector")

    @classmethod
    def load(cls, path: str | Path) -> "WebFetchConnectorGovernance":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def _require_approved(self) -> None:
        try:
            super()._require_approved()
        except ConnectorGovernanceError as exc:
            raise PublicWebCoreFetchError(str(exc)) from exc


def write_web_fetch_governance_proposal(path: str | Path, *, approved_by: str) -> dict[str, Any]:
    record = build_web_fetch_governance_record(approved_by=approved_by, status="proposed")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(canonical_json(record) + "\n", encoding="utf-8")
    os.chmod(target, 0o600)
    return record


# ---------------------------------------------------------------------------
# helpers shared with the discovery coordinator
# ---------------------------------------------------------------------------
def count_recent_public_web_fetch_calls(connection: Any, *, as_of: datetime | None = None) -> int:
    """Trailing-24h ``fetch_get`` invocations across every per-host fetch profile."""

    now = as_of or datetime.now(timezone.utc)
    window_start = (now - TRAILING_WINDOW).isoformat(timespec="microseconds")
    row = connection.execute(
        "SELECT COUNT(*) FROM connector_invocations "
        "WHERE connector_profile_ref LIKE ? AND created_at >= ?",
        (f"{FETCH_PROFILE_PREFIX}:%", window_start),
    ).fetchone()
    return int(row[0])


def url_authority_from_discovery(
    connection: Any, spool: RawSpool, *, url_ref: str, source_envelope_ref: str
) -> dict[str, Any]:
    """Rebuild the exact URL authority a discovery envelope grants for ``url_ref``.

    The only route from a ref to a fetchable URL is the exact raw search bytes
    bound to that envelope; a ref the envelope never cited is refused.
    """

    if not isinstance(url_ref, str) or _URL_REF_RE.fullmatch(url_ref) is None:
        raise PublicWebCoreFetchError("url_ref must be a public-web-url:sha256 ref")
    row = connection.execute(
        "SELECT record_json,content_hash FROM connector_source_envelopes WHERE source_envelope_id=?",
        (source_envelope_ref,),
    ).fetchone()
    if row is None:
        raise PublicWebCoreFetchError("discovery source envelope was not found")
    envelope = json.loads(row["record_json"])
    if envelope.get("content_hash") != row["content_hash"]:
        raise PublicWebCoreFetchError("discovery source envelope hash drifted")
    raw = spool.read_object(envelope["raw_response_hash"])
    for authority in build_public_web_url_authorities(raw, envelope):
        if authority["url_ref"] == url_ref:
            return authority
    raise PublicWebCoreFetchError("url_ref is not cited by the discovery envelope")


def validate_public_web_fetch_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    """Closed, hash-bound record a fetch child leaves for the review plane."""

    if not isinstance(value, Mapping) or set(value) != _MANIFEST_FIELDS:
        raise PublicWebCoreFetchError("public-web fetch manifest has an invalid closed shape")
    wire = json.loads(canonical_json(value))
    if wire["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise PublicWebCoreFetchError("unsupported public-web fetch manifest schema_version")
    for name in (
        "id", "document_ref", "url_ref", "canonical_url", "host", "final_url", "source_record_ref",
        "raw_media_type", "url_authority_ref", "discovery_source_envelope_ref",
        "connector_profile_ref", "connector_invocation_ref", "source_envelope_ref",
        "raw_artifact_version_ref",
    ):
        if not isinstance(wire[name], str) or not wire[name]:
            raise PublicWebCoreFetchError(f"manifest {name} must be non-empty text")
    for name in (
        "body_sha256", "url_authority_hash", "discovery_source_envelope_hash",
        "connector_profile_hash", "connector_invocation_hash", "source_envelope_hash",
        "raw_response_hash", "content_hash",
    ):
        if not isinstance(wire[name], str) or re.fullmatch(r"[0-9a-f]{64}", wire[name]) is None:
            raise PublicWebCoreFetchError(f"manifest {name} must be SHA-256 hex")
    _parse_time(wire["created_at"], "created_at")
    if _URL_REF_RE.fullmatch(wire["url_ref"]) is None:
        raise PublicWebCoreFetchError("manifest url_ref is not a public-web-url ref")
    if _DOCUMENT_REF_RE.fullmatch(wire["document_ref"]) is None or wire["document_ref"] != wire["source_record_ref"]:
        raise PublicWebCoreFetchError("manifest document_ref must be the fetched source record ref")
    if not wire["document_ref"].endswith(f":body-sha256:{wire['body_sha256']}"):
        raise PublicWebCoreFetchError("manifest body hash differs from the source record ref")
    if wire["body_sha256"] != wire["raw_response_hash"]:
        raise PublicWebCoreFetchError("manifest raw response hash must equal the body hash")
    if isinstance(wire["body_bytes"], bool) or not isinstance(wire["body_bytes"], int) or wire["body_bytes"] < 1:
        raise PublicWebCoreFetchError("manifest body_bytes must be a positive integer")
    declared = wire.pop("content_hash")
    if content_hash(wire) != declared:
        raise PublicWebCoreFetchError("manifest content_hash mismatch")
    if wire["id"] != "public-web-fetch-manifest:" + content_hash(
        {"document_ref": wire["document_ref"], "source_envelope_hash": wire["source_envelope_hash"]}
    ):
        raise PublicWebCoreFetchError("manifest id is not deterministic")
    wire["content_hash"] = declared
    return wire


# ---------------------------------------------------------------------------
# governed fetch
# ---------------------------------------------------------------------------
class PublicWebCoreFetch:
    """Run one governed ``fetch_get`` of an authorized URL into Core authority."""

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
        governance: WebFetchConnectorGovernance,
        transport: Any | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 60,
        allow_redirects: bool = True,
        max_redirects: int = 3,
    ) -> None:
        if type(store) is not DaltonStore:
            raise TypeError("fetch requires an exact DaltonStore")
        if type(connectors) is not ConnectorStore or connectors.connection is not store.connection:
            raise TypeError("fetch requires the Core ConnectorStore")
        if type(observability) is not ObservabilityStore or observability.connection is not store.connection:
            raise TypeError("fetch requires the Core ObservabilityStore")
        if type(journal) is not RunnerJournal:
            raise TypeError("fetch requires the Core RunnerJournal")
        if type(spool) is not RawSpool:
            raise TypeError("fetch requires an exact RawSpool")
        if not isinstance(governance, WebFetchConnectorGovernance):
            raise TypeError("fetch requires WebFetchConnectorGovernance")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise PublicWebCoreFetchError("lease_seconds must be a positive integer")
        if type(allow_redirects) is not bool or isinstance(max_redirects, bool) or not isinstance(max_redirects, int) \
                or max_redirects < 0 or (not allow_redirects and max_redirects != 0):
            raise PublicWebCoreFetchError("redirect policy is invalid")
        self.store = store
        self.connectors = connectors
        self.observability = observability
        self.journal = journal
        self.scheduler = scheduler
        self.catalog = catalog
        self.spool = spool
        self.governance = governance
        self.transport = transport if transport is not None else PublicHttpTransport()
        self.user_agent = user_agent
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_seconds = lease_seconds
        self.network_policy = {
            "allowed_schemes": ["https"],
            "allow_redirects": allow_redirects,
            "max_redirects": max_redirects,
            "resolve_public_only": True,
        }
        self.template, self.contract = web_fetch_contract()
        self.receipts = ConnectorCompletionReceiptReader(connectors=connectors, observability=observability)
        self.authority_port = ConnectorAuthorityPort(
            connectors=connectors, observability=observability, scheduler=scheduler,
            receipt_reader=self.receipts,
        )
        self._descriptor: Any | None = None
        self._hosts: dict[str, dict[str, Any]] = {}

    # -- governed authorities ------------------------------------------------
    def _descriptor_spec(self) -> dict[str, Any]:
        return {
            "schema_version": "0.1",
            "id": FETCH_CAPABILITY_ID,
            "version": 1,
            "created_at": self.governance.effective_from,
            "kind": "connector",
            "name": FETCH_KIND,
            "label": "Core-hosted public-web fetch (original bytes only)",
            "summary": (
                "Fetch the original bytes of one URL a completed web search cited, "
                "through credential-free public HTTPS, into Core connector authority"
            ),
            "aliases": ["public web fetch", "fetch cited page"],
            "tags": ["connector", "public-web", "read-only", "public-https", "fetch"],
            "intent_examples": ["retrieve the page a web search cited for a covered company"],
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
            "permissions": web_fetch_permissions(),
            "eligibility": {
                "state": "ready",
                "visibility_scopes": list(VISIBILITY_SCOPES),
                "policy_ref": self.governance.policy_ref,
                "valid_until": None,
            },
            "source_hash": web_fetch_source_hash(),
            "schema_hash": web_fetch_schema_hash(),
        }

    def ensure_descriptor(self) -> Any:
        if self._descriptor is not None:
            return self._descriptor
        self.governance._require_approved()
        spec = self._descriptor_spec()
        try:
            descriptor = self.catalog.describe(FETCH_CAPABILITY_ID, visibility_scopes=list(VISIBILITY_SCOPES))
        except CapabilityNotFound:
            descriptor = self.catalog.publish(spec)
        if (
            descriptor.source_hash != spec["source_hash"]
            or descriptor.schema_hash != spec["schema_hash"]
            or descriptor.eligibility.policy_ref != self.governance.policy_ref
            or descriptor.permissions.to_dict() != spec["permissions"]
        ):
            raise PublicWebCoreFetchError("published fetch capability differs from governed spec")
        self._descriptor = descriptor
        return descriptor

    def ensure_host_authorities(self, host: str) -> dict[str, Any]:
        """Profile / price / rate policy / runner manifest for exactly one host."""

        cached = self._hosts.get(host)
        if cached is not None:
            return cached
        descriptor = self.ensure_descriptor()
        slug = host_slug(host)
        rate_policy_ref = f"{FETCH_RATE_POLICY_PREFIX}:{slug}"
        binding = {
            "binding_ref": f"runner-binding:web-fetch:{slug}:0.1",
            "descriptor_revision_ref": descriptor.revision_ref,
            "descriptor_hash": descriptor.content_hash,
            "adapter_ref": ADAPTER_REF,
            "adapter_hash": web_fetch_adapter_hash(),
            "source_ref": self.template["source_identity"]["source_ref"],
            "source_hash": web_fetch_source_hash(),
            "operation": OPERATION,
            "input_schema_ref": self.contract["input_schema_ref"],
            "input_schema_hash": self.contract["input_schema_hash"],
            "output_schema_ref": self.contract["output_schema_ref"],
            "output_schema_hash": self.contract["output_schema_hash"],
            "auth_mode": "none",
            "credential_slot_refs": [],
            "required_permissions": web_fetch_permissions(),
            "side_effects": [SIDE_EFFECT],
            "rate_policy_ref": rate_policy_ref,
        }
        manifest_base = {
            "schema_version": "0.1",
            "id": f"runner-environment:dalton-core-web-fetch:{slug}:0.1",
            "created_at": self.governance.effective_from,
            "runner_runtime_ref": RUNNER_RUNTIME_REF,
            "runner_actor_ref": RUNNER_ACTOR_REF,
            "resolver_ref": "resolver:dalton-core-static:0.1",
            "resolver_version": "0.1",
            "package_manifest_ref": "artifact:runner-packages:web-fetch:0.1",
            "package_manifest_hash": content_hash(
                {"package": ADAPTER_PACKAGE, "adapter_hash": web_fetch_adapter_hash(), "host": host}
            ),
            "bindings": [binding],
        }
        manifest = validate_runner_environment_manifest(_with_hash(manifest_base))
        profile_wire = {
            "schema_version": "0.1",
            "id": f"{FETCH_PROFILE_PREFIX}:{slug}:v1",
            "created_at": self.governance.effective_from,
            "connector_ref": self.template["connector_ref"],
            "version": None,
            "prior_version_ref": None,
            "capability_id": FETCH_CAPABILITY_ID,
            "descriptor_revision_ref": descriptor.revision_ref,
            "descriptor_hash": descriptor.content_hash,
            "source_identity": dict(self.template["source_identity"]),
            "source_hash": web_fetch_source_hash(),
            "schema_hash": web_fetch_schema_hash(),
            "catalog_epoch": descriptor.catalog_epoch,
            "adapter_ref": ADAPTER_REF,
            "adapter_hash": web_fetch_adapter_hash(),
            "runner_runtime_ref": RUNNER_RUNTIME_REF,
            "runner_actor_ref": RUNNER_ACTOR_REF,
            "runner_environment_hash": manifest["content_hash"],
            "allowed_operations": [OPERATION],
            "allowed_hosts": [host],
            "auth_mode": "none",
            "credential_slot_refs": [],
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
            "max_response_bytes": FETCH_MAX_RESPONSE_BYTES,
            "max_records": FETCH_MAX_RECORDS,
            "timeout_ms": FETCH_TIMEOUT_MS,
            "access_policy_ref": "policy:access:public-web",
            "retention_policy_ref": "policy:retention:public-web-page",
            "terms_policy_ref": "policy:terms:public-web-robots-respecting",
            "network_policy": dict(self.network_policy),
        }
        profile = register_chained_profile(
            self.connectors, profile_wire, idempotency_key=f"web-fetch:profile:{slug}:v1"
        )
        price_ref = f"{FETCH_PRICE_RATE_PREFIX}:{slug}:calls"
        price = self.connectors.register_price_rate(
            {
                "schema_version": "0.1",
                "id": f"{price_ref}:v1",
                "created_at": self.governance.effective_from,
                "price_rate_ref": price_ref,
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
                "source_ref": "pricing:public-web:free",
                "actor_ref": self.governance.approved_by,
            },
            idempotency_key=f"web-fetch:price:{slug}:v1",
        )
        quota = governed_daily_quota(TEMPLATE_KEY, OPERATION)
        price_book = {"price_rate_refs": [price["id"]], "required_price_meters": ["calls"]}
        rate_policy = self.connectors.register_rate_policy(
            {
                "schema_version": "0.1",
                "id": f"{rate_policy_ref}:v1",
                "created_at": self.governance.effective_from,
                "policy_ref": rate_policy_ref,
                "quota_scope_ref": FETCH_QUOTA_SCOPE_REF,
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
                    quota, max_response_bytes=profile["max_response_bytes"], max_records=profile["max_records"],
                ),
                "effective_from": self.governance.effective_from,
                "effective_until": None,
                "actor_ref": self.governance.approved_by,
            },
            idempotency_key=f"web-fetch:rate-policy:{slug}:v1",
        )
        authorities = {
            "descriptor": descriptor, "binding": binding, "manifest": manifest,
            "profile": profile, "price": price, "rate_policy": rate_policy,
        }
        self._hosts[host] = authorities
        return authorities

    # -- one fetch -----------------------------------------------------------
    def build_request(self, url_authority: Mapping[str, Any], *, created_at: str | None = None) -> dict[str, Any]:
        """Bind one exact URL authority to a creation time; the pair is the request identity."""

        try:
            authority = validate_public_web_url_authority(url_authority)
        except Exception as exc:  # RunnerValidationError / RunnerConflict from the authority validator
            raise PublicWebCoreFetchError(f"url authority is invalid: {exc}") from exc
        created = created_at or _wire_time(self.clock())
        _parse_time(created, "created_at")
        parameters = {"url_ref": authority["url_ref"]}
        identity = {
            "operation": OPERATION, "parameters": parameters, "created_at": created,
            "url_authority_hash": authority["content_hash"],
        }
        return {
            "operation": OPERATION,
            "parameters": parameters,
            "created_at": created,
            "url_authority": authority,
            "query_hash": content_hash({"operation": OPERATION, "parameters": parameters}),
            "request_hash": content_hash(identity),
        }

    def fetch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Execute (or durably replay) one fetch; return the bound receipt."""

        if not isinstance(request, Mapping) or set(request) != {
            "operation", "parameters", "created_at", "url_authority", "query_hash", "request_hash",
        }:
            raise PublicWebCoreFetchError("fetch request has an invalid closed shape")
        rebuilt = self.build_request(request["url_authority"], created_at=request["created_at"])
        if rebuilt != dict(request):
            raise PublicWebCoreFetchError("fetch request identity drifted")
        authority = rebuilt["url_authority"]
        parameters = rebuilt["parameters"]
        created_at = rebuilt["created_at"]
        suffix = rebuilt["request_hash"][:20]
        host = authority["host"]
        authorities = self.ensure_host_authorities(host)
        profile = authorities["profile"]
        descriptor = authorities["descriptor"]
        binding = authorities["binding"]
        manifest = authorities["manifest"]

        work_id = f"work:web-fetch:{suffix}"
        call_id = f"connector-call:web-fetch:{suffix}"
        invocation_id = f"connector-invocation:web-fetch:{suffix}"
        artifact_ref = f"artifact:web-fetch:{suffix}:raw"
        request_id = f"connector-runner-request:web-fetch:{suffix}"

        adapter = PublicWebFetchAdapter(
            url_authority_resolver=PublicWebUrlAuthorityResolver([authority]),
            transport=self.transport,
            user_agent=self.user_agent,
            clock=self.clock,
            source_identity=dict(self.template["source_identity"]),
            allowed_operations=(OPERATION,),
        )
        resolver = StaticAdapterResolver(
            manifest,
            {binding["binding_ref"]: adapter},
            {binding["binding_ref"]: lambda value, expected=parameters: value == expected},
        )
        gate = ConnectorRunnerAdmissionGate(
            scheduler=self.scheduler,
            catalog=self.catalog,
            connectors=self.connectors,
            resolver=resolver,
            visibility_scopes=list(VISIBILITY_SCOPES),
            clock=self.clock,
        )
        executor = ConnectorTransportExecutor(
            gate=gate,
            journal=self.journal,
            spool=self.spool,
            authority=self.authority_port,
            connector_reader=self.connectors,
            clock=self.clock,
        )
        work = WorkOrder(
            schema_version="0.1",
            id=work_id,
            created_at=created_at,
            updated_at=created_at,
            question="Fetch the original bytes of one cited public web page into Core connector authority",
            requested_capabilities=(FETCH_CAPABILITY_ID,),
            runtime_profile_ref=RUNNER_RUNTIME_REF,
            budget={"max_seconds": self.lease_seconds},
            idempotency_key=work_id,
            declared_side_effects=(SIDE_EFFECT,),
            status="ready",
            input_refs=(call_id,),
        )
        work_hash = content_hash(work.to_dict())

        try:
            stored = self.journal.request(request_id)
        except RunnerJournalNotFound:
            stored = None
        replayed = False
        if stored is not None:
            latest = self.journal.latest(request_id)
            if latest["state"] != "responded":
                raise PublicWebCoreFetchError(
                    f"fetch runner request {request_id} is incomplete at durable "
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
                    "query_hash": rebuilt["query_hash"],
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
                capability=FETCH_CAPABILITY_ID,
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
                capability_id=FETCH_CAPABILITY_ID,
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
                raise PublicWebCoreFetchError(f"scheduler already holds another WorkOrder for {work.id}")
            claim = self.scheduler.claim(RUNNER_ACTOR_REF, work_order_id=work.id, lease_seconds=self.lease_seconds)
            if claim is None:
                raise PublicWebCoreFetchError(
                    f"WorkOrder {work.id} is not claimable; it may already be complete "
                    "without a durable runner response"
                )
            runner_request = _with_hash(
                {
                    "schema_version": "0.1",
                    "id": request_id,
                    "created_at": created_at,
                    "connector_invocation_ref": invocation["id"],
                    "connector_invocation_hash": invocation["content_hash"],
                    "execution_ref": invocation["execution_ref"],
                    "execution_hash": invocation["execution_hash"],
                    "work_order_ref": work.id,
                    "work_order_hash": work_hash,
                    "scheduler_attempt_number": int(
                        claim["lease"].get("attempt_number") or claim["attempt"]["attempt_number"]
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
                    "idempotency_key": request_id,
                }
            )
            response = executor.execute(runner_request, scheduler_lease_token=claim["lease_token"])
        return self._receipt(rebuilt, response, profile, replayed=replayed)

    def _receipt(
        self, request: Mapping[str, Any], response: Mapping[str, Any], profile: Mapping[str, Any], *, replayed: bool,
    ) -> dict[str, Any]:
        invocation = self.receipts.get_invocation(response["connector_invocation_ref"])
        if invocation["content_hash"] != response["connector_invocation_hash"]:
            raise PublicWebCoreFetchError("fetch invocation authority drifted")
        authority = request["url_authority"]
        base: dict[str, Any] = {
            "operation": OPERATION,
            "parameters": dict(request["parameters"]),
            "created_at": request["created_at"],
            "query_hash": request["query_hash"],
            "request_hash": request["request_hash"],
            "url_ref": authority["url_ref"],
            "canonical_url": authority["canonical_url"],
            "host": authority["host"],
            "url_authority_ref": authority["id"],
            "url_authority_hash": authority["content_hash"],
            "discovery_source_envelope_ref": authority["discovery_source_envelope_ref"],
            "discovery_source_envelope_hash": authority["discovery_source_envelope_hash"],
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
            "raw_response_hash": None,
            "document_ref": None,
            "body_bytes": None,
            "raw_media_type": None,
            "source_status": None,
        }
        if response["outcome"] != "succeeded" or response["source_envelope_ref"] is None:
            return base
        source = self.receipts.get_source_envelope(response["source_envelope_ref"])
        if source is None or source["content_hash"] != response["source_envelope_hash"]:
            raise PublicWebCoreFetchError("fetch source envelope authority drifted")
        if (
            source["connector_invocation_ref"] != invocation["id"]
            or source["operation"] != OPERATION
            or source["source"] != profile["source_identity"]["source_ref"]
            or len(source["source_record_refs"]) != 1
        ):
            raise PublicWebCoreFetchError("fetch source envelope does not bind the call")
        document_ref = source["source_record_refs"][0]
        if _DOCUMENT_REF_RE.fullmatch(document_ref) is None:
            raise PublicWebCoreFetchError("fetch source record is not a public-web document ref")
        body_hash = document_ref.rsplit(":", 1)[1]
        if source["raw_response_hash"] != body_hash:
            raise PublicWebCoreFetchError("fetched raw bytes differ from the body hash the record names")
        raw = self.spool.read_object(source["raw_response_hash"])
        base.update({
            "source_envelope_ref": source["id"],
            "source_envelope_hash": source["content_hash"],
            "raw_artifact_version_ref": source["raw_artifact_version_ref"],
            "raw_response_hash": source["raw_response_hash"],
            "document_ref": document_ref,
            "body_bytes": len(raw),
            "raw_media_type": self._media_type(source["raw_artifact_version_ref"]),
            "source_status": source["status"],
        })
        return base

    def _media_type(self, artifact_version_ref: str) -> str:
        """Media type the raw ``ArtifactVersion`` recorded from the response headers."""

        artifact = self.receipts.get_artifact_version(artifact_version_ref)
        media = artifact.get("media_type") if isinstance(artifact, Mapping) else None
        return media if isinstance(media, str) and media else "application/octet-stream"

    def manifest(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        """Closed manifest a child leaves next to its ticket for the review plane."""

        if receipt.get("outcome") != "succeeded" or receipt.get("document_ref") is None:
            raise PublicWebCoreFetchError("only a succeeded fetch has a manifest")
        base = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "id": "public-web-fetch-manifest:" + content_hash(
                {"document_ref": receipt["document_ref"], "source_envelope_hash": receipt["source_envelope_hash"]}
            ),
            "created_at": receipt["created_at"],
            "document_ref": receipt["document_ref"],
            "url_ref": receipt["url_ref"],
            "canonical_url": receipt["canonical_url"],
            "host": receipt["host"],
            "final_url": receipt["canonical_url"],
            "source_record_ref": receipt["document_ref"],
            "body_sha256": receipt["raw_response_hash"],
            "body_bytes": receipt["body_bytes"],
            "raw_media_type": receipt["raw_media_type"],
            "url_authority_ref": receipt["url_authority_ref"],
            "url_authority_hash": receipt["url_authority_hash"],
            "discovery_source_envelope_ref": receipt["discovery_source_envelope_ref"],
            "discovery_source_envelope_hash": receipt["discovery_source_envelope_hash"],
            "connector_profile_ref": receipt["connector_profile_ref"],
            "connector_profile_hash": receipt["connector_profile_hash"],
            "connector_invocation_ref": receipt["connector_invocation_ref"],
            "connector_invocation_hash": receipt["connector_invocation_hash"],
            "source_envelope_ref": receipt["source_envelope_ref"],
            "source_envelope_hash": receipt["source_envelope_hash"],
            "raw_artifact_version_ref": receipt["raw_artifact_version_ref"],
            "raw_response_hash": receipt["raw_response_hash"],
        }
        return validate_public_web_fetch_manifest({**base, "content_hash": content_hash(base)})


__all__ = [
    "ADAPTER_REF",
    "DEFAULT_USER_AGENT",
    "FETCH_CAPABILITY_ID",
    "FETCH_KIND",
    "FETCH_PROFILE_PREFIX",
    "OPERATION",
    "PublicWebCoreFetch",
    "PublicWebCoreFetchError",
    "WebFetchConnectorGovernance",
    "build_web_fetch_governance_record",
    "count_recent_public_web_fetch_calls",
    "host_slug",
    "url_authority_from_discovery",
    "validate_public_web_fetch_manifest",
    "web_fetch_adapter_hash",
    "web_fetch_fixture_hash",
    "web_fetch_permissions",
    "web_fetch_schema_hash",
    "web_fetch_source_hash",
    "write_web_fetch_governance_proposal",
]
