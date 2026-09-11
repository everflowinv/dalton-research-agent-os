"""Core-hosted Gemini ``search_web`` (P9d-4a).

Web search is the second discovery source a CoverageMission may run after
AlphaEngine's ``search_library``.  This module is the ``search_web`` twin of
``alphaengine_core_search``: one governed capability, one connector profile /
price / rate policy, and a single-call executor that leaves the usual
Core-held connector authority behind it (``ConnectorInvocation``, physical
attempt, usage, cost, quota settlement, raw ``ArtifactVersion`` and a
``SourceEnvelope`` whose ``source_record_refs`` are opaque
``public-web-url:sha256:<hash>`` refs derived from Gemini's citations).

What a search leaves behind is discovery only.  Gemini's synthesized answer,
snippets and titles stay inside the raw artifact and never become page
content; the original bytes of a cited page only enter authority through the
separate credential-free public-web ``fetch_get`` lane (P9d-4b), which
rebuilds every URL authority from this exact raw artifact and envelope.

The host key never reaches Core.  The lane rides the same host-owned handle
protocol as AlphaEngine (``OpenClawToolHandle``): a trusted runtime hands the
child an opaque handle whose only tool is ``web_search``.  This slice ships
the rehearsal handle; the gateway-backed handle is not wired yet, and a
networked launch is refused before any process starts.
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
    RESOLVER_REF,
    RUNNER_ACTOR_REF,
    RUNNER_RUNTIME_REF,
    VISIBILITY_SCOPES,
)
from .alphaengine_core_search import register_chained_profile
from .capability_catalog import CapabilityCatalog, CapabilityNotFound
from .connector import ConnectorStore
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
    RunnerConflict,
    RunnerValidationError,
    StaticAdapterResolver,
    validate_runner_environment_manifest,
)
from .connector_transport_executor import ConnectorTransportExecutor
from .contracts import ExecutionInvocation, ExecutionKind, WorkOrder
from .credential_authority import CredentialAuthorityStore
from .live_mcp_connector import (
    GEMINI_WEB_SEARCH_CREDENTIAL_SLOT_REF,
    LiveMcpRunnerAdmissionGate,
    build_live_mcp_transport_plan,
    validate_live_mcp_adapter_request,
)
from .mcp_managed_runner import validate_mcp_managed_transport_observation
from .observability import ObservabilityStore
from .openclaw_connector_bridge import (
    BridgePermissionDenied,
    BridgeRateLimited,
    HostToolInvocationResult,
)
from .openclaw_web_search_broker_client import WebSearchProviderContractDrift
from .public_web_connector import (
    GEMINI_WEB_SEARCH_MAX_RECORDS,
    LEGACY_WEB_SEARCH_PROVIDER,
    OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_HASH,
    build_public_web_url_authorities,
    gemini_web_search_payload_from_result,
    normalize_web_search_payload,
    validate_gemini_search_parameters,
    validate_web_search_provider,
    web_search_provider_contract,
    web_search_provider_terms_ref,
)
from .raw_spool import RawSpool
from .research_context import build_compiled_connector_plan
from .runner_journal import RunnerJournal, RunnerJournalNotFound
from .scheduler import Scheduler
from .store import DaltonStore, canonical_json, content_hash


GOVERNANCE_SCHEMA_VERSION = "0.1"
SEARCH_KIND = "gemini-web-search"
TEMPLATE_KEY = "gemini-web-search"
SEARCH_CAPABILITY_ID = "capability:dalton:connector:gemini-web-search"
OPERATION = "search_web"
TOOL_NAME = "web_search"
SEARCH_PROFILE_REF = "connector-profile:gemini-web-search:v1"
SEARCH_RATE_POLICY_REF = "connector-rate-policy:gemini-web-search"
SEARCH_PRICE_RATE_REF = "connector-price-rate:gemini-web-search:calls"
SEARCH_PLANNER_REF = "planner:dalton-core-gemini-web-search:0.1"
SEARCH_ROUTING_POLICY_REF = "routing:dalton-core-gemini-web-search:0.1"
# The adapter target is the inventory template's host-tool transport target;
# the live gate requires the runtime binding to name exactly that target.
ADAPTER_REF = "host-tool:gemini-web-search"
ADAPTER_PACKAGE = "openclaw-gemini-web-search-live-adapter:0.1"
CREDENTIAL_SLOT_REF = GEMINI_WEB_SEARCH_CREDENTIAL_SLOT_REF
CREDENTIAL_AUTHORITY_REF = "credential-authority:host:gemini-web-search"
SIDE_EFFECT = "read:public-web-search"
# One call is one ranked page and there is no cursor.  The ceiling is the
# frozen inventory value shared with the URL-authority rebuild, so both admit
# the identical ranked slice.
SEARCH_MAX_RECORDS = GEMINI_WEB_SEARCH_MAX_RECORDS
SEARCH_MAX_RESPONSE_BYTES = 256_000
TRAILING_WINDOW = timedelta(hours=24)
_SPEC_FIELDS = frozenset({"query", "date_after", "date_before"})
_URL_REF_RE = re.compile(r"^public-web-url:sha256:[0-9a-f]{64}$")


def web_search_provider_contract_hash(expected_provider: str) -> str:
    return content_hash(web_search_provider_contract(expected_provider))


def _provider_suffix(expected_provider: str) -> str:
    provider = validate_web_search_provider(expected_provider)
    return "" if provider == LEGACY_WEB_SEARCH_PROVIDER else f":provider-{provider}"


class PublicWebCoreSearchError(RuntimeError):
    """A search request, governance record or authority binding is invalid."""


def _wire_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise PublicWebCoreSearchError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise PublicWebCoreSearchError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _with_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = json.loads(canonical_json(value))
    wire["content_hash"] = content_hash(wire)
    return wire


# ---------------------------------------------------------------------------
# frozen contract identity
# ---------------------------------------------------------------------------
def web_search_contract() -> tuple[dict[str, Any], dict[str, Any]]:
    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    matches = [item for item in template["operations"] if item["operation"] == OPERATION]
    if len(matches) != 1:
        raise PublicWebCoreSearchError("Gemini search_web is not frozen")
    if template["transport"]["target_ref"] != ADAPTER_REF:
        raise PublicWebCoreSearchError("Gemini host-tool transport target drifted")
    return template, matches[0]


def web_search_source_hash() -> str:
    template, _ = web_search_contract()
    return content_hash(template["source_identity"])


def web_search_schema_hash() -> str:
    _, contract = web_search_contract()
    return content_hash(
        {
            "allowed_operations": [OPERATION],
            "input_schema_refs": {OPERATION: contract["input_schema_ref"]},
            "input_schema_hashes": {OPERATION: contract["input_schema_hash"]},
            "output_schema_refs": {OPERATION: contract["output_schema_ref"]},
            "output_schema_hashes": {OPERATION: contract["output_schema_hash"]},
        }
    )


def web_search_adapter_hash(
    expected_provider: str = LEGACY_WEB_SEARCH_PROVIDER,
) -> str:
    legacy = {
        "target_ref": ADAPTER_REF,
        "package": ADAPTER_PACKAGE,
        "bridge_hash": OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_HASH,
        "operation": OPERATION,
    }
    provider = validate_web_search_provider(expected_provider)
    if provider == LEGACY_WEB_SEARCH_PROVIDER:
        return content_hash(legacy)
    return content_hash({
        "schema_version": "0.2",
        "legacy_bridge": legacy,
        "provider_contract": web_search_provider_contract(provider),
    })


def web_search_fixture_hash() -> str:
    template, _ = web_search_contract()
    return template["fixture_manifest_hash"]


def web_search_permissions() -> dict[str, Any]:
    """Permissions of the host-owned, read-only Gemini web_search capability."""

    return {
        "risk_class": "low",
        "network": False,
        "filesystem_read": [],
        "filesystem_write": ["runner:raw-sink"],
        "credential_slot_refs": [CREDENTIAL_SLOT_REF],
        "core_db": False,
        "side_effects": [SIDE_EFFECT],
    }


def build_web_search_governance_record(
    *,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-08-26T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Closed, hash-bound governance record for the web search capability."""

    if status not in {"proposed", "approved"}:
        raise PublicWebCoreSearchError("governance status must be proposed or approved")
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
        "allowed_permissions": web_search_permissions(),
        "expected_source_hash": web_search_source_hash(),
        "expected_schema_hash": web_search_schema_hash(),
    }
    return _with_hash(base)


class WebSearchConnectorGovernance(ConnectorGovernance):
    """Generic governance narrowed to the Gemini web search capability."""

    def __init__(self, value: Mapping[str, Any]) -> None:
        try:
            super().__init__(value)
        except ConnectorGovernanceError as exc:
            raise PublicWebCoreSearchError(str(exc)) from exc
        if self.capability_id != SEARCH_CAPABILITY_ID:
            raise PublicWebCoreSearchError("governance capability_id is not the web search connector")

    @classmethod
    def load(cls, path: str | Path) -> "WebSearchConnectorGovernance":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def _require_approved(self) -> None:
        try:
            super()._require_approved()
        except ConnectorGovernanceError as exc:
            raise PublicWebCoreSearchError(str(exc)) from exc


def write_web_search_governance_proposal(path: str | Path, *, approved_by: str) -> dict[str, Any]:
    record = build_web_search_governance_record(approved_by=approved_by, status="proposed")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(canonical_json(record) + "\n", encoding="utf-8")
    os.chmod(target, 0o600)
    return record


# ---------------------------------------------------------------------------
# search spec
# ---------------------------------------------------------------------------
def validate_web_search_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one ``search_web`` parameter object (closed shape).

    Mission discovery always freezes an explicit date window; OpenClaw's
    ``freshness`` shortcut is mutually exclusive with dates and is not part
    of a plan-driven search.
    """

    if not isinstance(value, Mapping) or set(value) != _SPEC_FIELDS:
        raise PublicWebCoreSearchError(
            "web search spec must have exactly query, date_after and date_before"
        )
    query = value["query"]
    if not isinstance(query, str) or not query.strip() or len(query) > 400:
        raise PublicWebCoreSearchError("web search query must be non-empty text (<=400 chars)")
    try:
        cleaned = validate_gemini_search_parameters({
            "query": query.strip(),
            "date_after": value["date_after"],
            "date_before": value["date_before"],
        })
    except (RunnerValidationError, ValueError) as exc:
        raise PublicWebCoreSearchError(f"web search spec is invalid: {exc}") from exc
    if set(cleaned) != _SPEC_FIELDS:
        raise PublicWebCoreSearchError("web search spec requires both date bounds")
    return {name: cleaned[name] for name in ("query", "date_after", "date_before")}


def web_search_spec_hash(spec: Mapping[str, Any]) -> str:
    return content_hash({"operation": OPERATION, "parameters": validate_web_search_spec(spec)})


def count_recent_web_search_calls(connection: Any, *, as_of: datetime | None = None) -> int:
    """Trailing-24h ``search_web`` invocations recorded against the Core profile."""

    now = as_of or datetime.now(timezone.utc)
    window_start = (now - TRAILING_WINDOW).isoformat(timespec="microseconds")
    row = connection.execute(
        "SELECT COUNT(*) FROM connector_invocations "
        "WHERE (connector_profile_ref=? OR connector_profile_ref LIKE ?) "
        "AND created_at >= ?",
        (SEARCH_PROFILE_REF, "connector-profile:gemini-web-search:provider-%", window_start),
    ).fetchone()
    return int(row[0])


def public_web_urls_in_authority(connection: Any, url_refs: list[str]) -> list[str]:
    """Subset of URL refs Core already holds fetched original bytes for.

    A discovery alone never counts: only a complete/partial ``SourceEnvelope``
    bound to a ``fetch_get`` invocation whose call spec names the exact
    ``url_ref`` proves the page is in authority.  Until the fetch lane lands
    this is empty, and the discovery ledger queues every ref as discovered.
    """

    present: list[str] = []
    for ref in url_refs:
        if _URL_REF_RE.fullmatch(ref) is None:
            raise PublicWebCoreSearchError("url ref is not a public-web-url:sha256 ref")
        row = connection.execute(
            "SELECT 1 FROM connector_source_envelopes e "
            "JOIN connector_invocations i ON i.connector_invocation_id=e.connector_invocation_ref "
            "JOIN connector_call_specs c ON c.call_spec_id=i.call_spec_ref "
            "WHERE c.operation='fetch_get' AND c.record_json LIKE ? "
            "AND e.status IN ('complete','partial') LIMIT 1",
            (f'%"{ref}"%',),
        ).fetchone()
        if row is not None:
            present.append(ref)
    return present


# ---------------------------------------------------------------------------
# live adapter + rehearsal handle
# ---------------------------------------------------------------------------
class GeminiWebSearchLiveAdapter:
    """Run ``search_web`` through the live lane's host-owned handle.

    The executor hands this adapter a ``LiveMcpAdapterRequest`` and the opaque
    handle the credential authority resolved for one use.  The Gemini payload
    is validated by the same normalizer the standalone discovery adapter
    uses; only the derived URL refs leave as structured output.
    """

    def __init__(self, *, expected_provider: str = LEGACY_WEB_SEARCH_PROVIDER) -> None:
        self.expected_provider = validate_web_search_provider(expected_provider)

    def __call__(
        self,
        request: Mapping[str, Any],
        raw_sink: Any,
        credential_handle: Any,
    ) -> dict[str, Any]:
        wire = validate_live_mcp_adapter_request(request)
        if wire["operation"] != OPERATION or wire["tool_name"] != TOOL_NAME:
            raise RunnerValidationError("web search adapter serves search_web only")
        invoke = getattr(credential_handle, "invoke", None)
        if not callable(invoke):
            raise RunnerValidationError("host-owned web search handle lacks invoke")
        parameters = wire["parameters"]
        arguments: dict[str, Any] = {"query": parameters["query"], "count": wire["max_records"]}
        for name in ("date_after", "date_before", "freshness"):
            if name in parameters:
                arguments[name] = parameters[name]
        try:
            invocation = invoke(
                TOOL_NAME,
                arguments,
                call_ref=wire["credential_use_ref"],
                deadline_at=wire["deadline_at"],
                max_response_bytes=wire["max_response_bytes"],
            )
        except BridgeRateLimited as exc:
            return self._failure(
                wire, outcome="rate_limited", code="rate_limited", message=str(exc),
                retryable=True, provider_status=429, retry_after_ms=exc.retry_after_ms,
            )
        except BridgePermissionDenied as exc:
            return self._failure(
                wire, outcome="failed", code="permission_denied", message=str(exc),
                retryable=False, provider_status=403, retry_after_ms=None,
            )
        except WebSearchProviderContractDrift as exc:
            return self._failure(
                wire, outcome="failed", code="provider_contract_drift",
                message=str(exc), retryable=False, provider_status=502,
                retry_after_ms=None,
            )
        if not isinstance(invocation, HostToolInvocationResult):
            raise RunnerValidationError("host-owned web search handle returned another type")
        if len(invocation.raw_response) > wire["max_response_bytes"]:
            raise RunnerValidationError("Gemini raw response exceeds byte limit")
        result = invocation.result
        if result.get("isError") is True:
            return self._failure(
                wire, outcome="failed", code="source_error",
                message="OpenClaw web_search returned an error", retryable=True,
                provider_status=502, retry_after_ms=None,
            )
        payload = gemini_web_search_payload_from_result(result)
        if "error" in payload:
            code = str(payload.get("error") or "source_error")
            permission = code in {"missing_gemini_api_key", "permission_denied", "revoked"}
            return self._failure(
                wire, outcome="failed",
                code="permission_denied" if permission else "source_error",
                message=str(payload.get("message") or code), retryable=not permission,
                provider_status=403 if permission else 502, retry_after_ms=None,
            )
        try:
            structured, _ = normalize_web_search_payload(
                payload, expected_query=parameters["query"],
                expected_provider=self.expected_provider,
                max_records=wire["max_records"],
            )
        except RunnerConflict as exc:
            return self._failure(
                wire, outcome="failed", code="provider_contract_drift",
                message=str(exc), retryable=False, provider_status=502,
                retry_after_ms=None,
            )
        raw_sink.write(invocation.raw_response)
        refs = structured["source_record_refs"]
        # S2: a ranked page that came back full is the top of a list whose
        # depth this lane cannot see. The live gate expects "partial" for a
        # saturated ranked page on every mcp_managed search, and it is the
        # honest word here too -- Gemini returning exactly ``count`` citations
        # is not Gemini saying the web holds exactly that many.
        saturated = bool(refs) and len(refs) >= wire["max_records"]
        base = {
            "protocol_version": "0.2",
            "request_hash": wire["content_hash"],
            "outcome": "succeeded",
            "provider_request_id": invocation.request_id,
            "provider_status_code": 200,
            "retry_after_ms": None,
            "structured_output": structured,
            "source_record_refs": refs,
            "cursor": None,
            "provider_usage": None,
            "source_status": (
                "empty" if not refs else "partial" if saturated else "complete"
            ),
            "completeness": "ranked",
            "error": None,
        }
        return validate_mcp_managed_transport_observation(
            {**base, "content_hash": content_hash(base)}
        )

    @staticmethod
    def _failure(
        wire: Mapping[str, Any],
        *,
        outcome: str,
        code: str,
        message: str,
        retryable: bool,
        provider_status: int,
        retry_after_ms: int | None,
    ) -> dict[str, Any]:
        base = {
            "protocol_version": "0.2",
            "request_hash": wire["content_hash"],
            "outcome": outcome,
            "provider_request_id": None,
            "provider_status_code": provider_status,
            "retry_after_ms": retry_after_ms,
            "structured_output": None,
            "source_record_refs": [],
            "cursor": None,
            "provider_usage": None,
            "source_status": None,
            "completeness": None,
            "error": {"code": code, "message": message[:1000], "retryable": retryable},
        }
        return validate_mcp_managed_transport_observation(
            {**base, "content_hash": content_hash(base)}
        )


class FakeWebSearchHandle:
    """Rehearsal stand-in returning canned citations in OpenClaw's exact shape."""

    def __init__(
        self, citations: list[Mapping[str, Any]], *,
        provider: str = LEGACY_WEB_SEARCH_PROVIDER,
    ) -> None:
        self.citations = [dict(item) for item in citations]
        self.provider = validate_web_search_provider(provider)
        self.calls: list[dict[str, Any]] = []

    def invoke(self, tool_name, arguments, *, call_ref, deadline_at, max_response_bytes):
        del deadline_at, max_response_bytes
        if tool_name != TOOL_NAME:
            raise RuntimeError("fake handle serves web_search only")
        self.calls.append({"arguments": dict(arguments), "call_ref": call_ref})
        count = int(arguments.get("count", SEARCH_MAX_RECORDS))
        payload = {
            # The exact inner shape the host runtime helper returns (the
            # broker unwraps {provider, result} before Dalton sees it).
            "query": arguments["query"],
            "provider": self.provider,
            "model": "gemini-2.5-flash",
            "tookMs": 25,
            "externalContent": {
                "untrusted": True, "source": "web_search",
                "provider": self.provider, "wrapped": True,
            },
            "content": "UNTRUSTED rehearsal synthesis; never promoted as page content",
            "citations": self.citations[:count],
        }
        result = {"content": [{"type": "text", "text": canonical_json(payload)}]}
        request_id = f"provider-request:fake-web-search:{len(self.calls)}"
        raw = canonical_json({"jsonrpc": "2.0", "id": request_id, "result": result}).encode("utf-8")
        return HostToolInvocationResult(request_id=request_id, raw_response=raw, result=result)


# ---------------------------------------------------------------------------
# governed search
# ---------------------------------------------------------------------------
class PublicWebCoreSearch:
    """Run one governed ``search_web`` call into Core connector authority."""

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
        governance: WebSearchConnectorGovernance,
        host_handle: Any,
        expected_provider: str = LEGACY_WEB_SEARCH_PROVIDER,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 60,
        grant_seconds: int = 900,
    ) -> None:
        if type(store) is not DaltonStore:
            raise TypeError("web search requires an exact DaltonStore")
        if type(connectors) is not ConnectorStore or connectors.connection is not store.connection:
            raise TypeError("web search requires the Core ConnectorStore")
        if type(observability) is not ObservabilityStore or observability.connection is not store.connection:
            raise TypeError("web search requires the Core ObservabilityStore")
        if type(journal) is not RunnerJournal:
            raise TypeError("web search requires the Core RunnerJournal")
        if type(spool) is not RawSpool:
            raise TypeError("web search requires an exact RawSpool")
        if not isinstance(governance, WebSearchConnectorGovernance):
            raise TypeError("web search requires WebSearchConnectorGovernance")
        if not callable(getattr(host_handle, "invoke", None)):
            raise TypeError("host_handle must expose invoke")
        for name, value in (("lease_seconds", lease_seconds), ("grant_seconds", grant_seconds)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise PublicWebCoreSearchError(f"{name} must be a positive integer")
        self.store = store
        self.connectors = connectors
        self.observability = observability
        self.journal = journal
        self.scheduler = scheduler
        self.catalog = catalog
        self.spool = spool
        self.governance = governance
        self.expected_provider = validate_web_search_provider(expected_provider)
        self.provider_contract = web_search_provider_contract(self.expected_provider)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_seconds = lease_seconds
        self.grant_seconds = grant_seconds
        self.credentials = CredentialAuthorityStore(
            store, handle_resolver=lambda _grant: host_handle, clock=self.clock
        )
        self.template, self.contract = web_search_contract()
        self.adapter = GeminiWebSearchLiveAdapter(
            expected_provider=self.expected_provider
        )
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
            "name": SEARCH_KIND,
            "label": "Core-hosted Gemini web search (discovery only)",
            "summary": (
                "Run one ranked public-web search through OpenClaw's host-owned "
                "Gemini web_search tool into Core connector authority; results are "
                "opaque URL refs, never page content"
            ),
            "aliases": ["web search", "discover public web sources"],
            "tags": ["connector", "public-web", "read-only", "mcp-managed", "search", "discovery"],
            "intent_examples": [
                "find recent public web coverage of a covered company",
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
            "permissions": web_search_permissions(),
            "eligibility": {
                "state": "ready",
                "visibility_scopes": list(VISIBILITY_SCOPES),
                "policy_ref": self.governance.policy_ref,
                "valid_until": None,
            },
            "source_hash": web_search_source_hash(),
            "schema_hash": web_search_schema_hash(),
        }

    def ensure_governed_authorities(self) -> dict[str, Any]:
        if self._authorities is not None:
            return self._authorities
        self.governance._require_approved()
        provider_suffix = _provider_suffix(self.expected_provider)
        profile_ref = (
            SEARCH_PROFILE_REF if not provider_suffix
            else f"connector-profile:gemini-web-search{provider_suffix}:v1"
        )
        binding_ref = f"runner-binding:gemini-web-search{provider_suffix}:0.1"
        adapter_hash = web_search_adapter_hash(self.expected_provider)
        package = (
            ADAPTER_PACKAGE if not provider_suffix
            else "openclaw-web-search-live-adapter:0.2"
        )
        rate_policy_ref = SEARCH_RATE_POLICY_REF + provider_suffix
        price_rate_ref = SEARCH_PRICE_RATE_REF + provider_suffix
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
            raise PublicWebCoreSearchError(
                "published web search capability differs from governed spec"
            )
        binding = {
            "binding_ref": binding_ref,
            "descriptor_revision_ref": descriptor.revision_ref,
            "descriptor_hash": descriptor.content_hash,
            "adapter_ref": ADAPTER_REF,
            "adapter_hash": adapter_hash,
            "source_ref": self.template["source_identity"]["source_ref"],
            "source_hash": web_search_source_hash(),
            "operation": OPERATION,
            "input_schema_ref": self.contract["input_schema_ref"],
            "input_schema_hash": self.contract["input_schema_hash"],
            "output_schema_ref": self.contract["output_schema_ref"],
            "output_schema_hash": self.contract["output_schema_hash"],
            "auth_mode": "mcp_managed",
            "credential_slot_refs": [CREDENTIAL_SLOT_REF],
            "required_permissions": web_search_permissions(),
            "side_effects": [SIDE_EFFECT],
            "rate_policy_ref": rate_policy_ref,
        }
        manifest_base = {
            "schema_version": "0.1",
            "id": f"runner-environment:dalton-core-gemini-web-search{provider_suffix}:0.1",
            "created_at": self.governance.effective_from,
            "runner_runtime_ref": RUNNER_RUNTIME_REF,
            "runner_actor_ref": RUNNER_ACTOR_REF,
            "resolver_ref": RESOLVER_REF,
            "resolver_version": "0.1",
            "package_manifest_ref": (
                f"artifact:runner-packages:gemini-web-search{provider_suffix}:0.1"
            ),
            "package_manifest_hash": content_hash(
                {
                    "package": package,
                    "bridge_hash": OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_HASH,
                    "adapter_hash": adapter_hash,
                    **({} if not provider_suffix else {
                        "provider_contract": self.provider_contract,
                    }),
                }
            ),
            "bindings": [binding],
        }
        manifest = validate_runner_environment_manifest(_with_hash(manifest_base))
        profile_wire = {
            "schema_version": "0.1",
            "id": profile_ref,
            "created_at": self.governance.effective_from,
            "connector_ref": self.template["connector_ref"],
            "version": None,
            "prior_version_ref": None,
            "capability_id": SEARCH_CAPABILITY_ID,
            "descriptor_revision_ref": descriptor.revision_ref,
            "descriptor_hash": descriptor.content_hash,
            "source_identity": dict(self.template["source_identity"]),
            "source_hash": web_search_source_hash(),
            "schema_hash": web_search_schema_hash(),
            "catalog_epoch": descriptor.catalog_epoch,
            "adapter_ref": ADAPTER_REF,
            "adapter_hash": adapter_hash,
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
            "access_policy_ref": "policy:access:public-web",
            "retention_policy_ref": "policy:retention:public-web-discovery",
            "terms_policy_ref": web_search_provider_terms_ref(
                self.expected_provider
            ),
            "network_policy": None,
        }
        profile = register_chained_profile(
            self.connectors, profile_wire,
            idempotency_key=f"gemini-web-search{provider_suffix}:profile:v1",
        )
        price = self.connectors.register_price_rate(
            {
                "schema_version": "0.1",
                "id": f"{price_rate_ref}:v1",
                "created_at": self.governance.effective_from,
                "price_rate_ref": price_rate_ref,
                "version": 1,
                "prior_version_ref": None,
                "connector_profile_ref": profile["id"],
                "meter": "calls",
                "unit_quantity": 1,
                # The host owns the Gemini key and its bill; Core meters calls
                # without asserting a unit price it cannot see.
                "unit_price_micros": 0,
                "rounding_mode": "ceiling",
                "currency": "USD",
                "effective_from": self.governance.effective_from,
                "effective_until": None,
                "source_ref": (
                    "pricing:openclaw-gemini-web-search:host-owned-unmetered"
                    if not provider_suffix else
                    f"pricing:openclaw-web-search:{self.expected_provider}:host-owned-unmetered"
                ),
                "actor_ref": self.governance.approved_by,
            },
            idempotency_key=f"gemini-web-search{provider_suffix}:price:v1",
        )
        quota = governed_daily_quota(TEMPLATE_KEY, OPERATION)
        price_book = {"price_rate_refs": [price["id"]], "required_price_meters": ["calls"]}
        rate_policy = self.connectors.register_rate_policy(
            {
                "schema_version": "0.1",
                "id": f"{rate_policy_ref}:v1",
                "created_at": self.governance.effective_from,
                "policy_ref": rate_policy_ref,
                "quota_scope_ref": "connector-quota-scope:gemini-web-search:search_web",
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
            idempotency_key=f"gemini-web-search{provider_suffix}:rate-policy:v1",
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

        parameters = validate_web_search_spec(spec)
        created = created_at or _wire_time(self.clock())
        _parse_time(created, "created_at")
        identity = {"operation": OPERATION, "parameters": parameters, "created_at": created}
        provider_binding = {}
        if self.expected_provider != LEGACY_WEB_SEARCH_PROVIDER:
            provider_binding = {
                "provider_contract": self.provider_contract,
                "provider_contract_hash": web_search_provider_contract_hash(
                    self.expected_provider
                ),
            }
            identity.update(provider_binding)
        return {
            "operation": OPERATION,
            "parameters": parameters,
            "created_at": created,
            "query_hash": web_search_spec_hash(parameters),
            "request_hash": content_hash(identity),
            **provider_binding,
        }

    def search(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Execute (or durably replay) one search; return the bound discovery receipt."""

        expected_fields = {
            "operation", "parameters", "created_at", "query_hash", "request_hash",
        }
        if self.expected_provider != LEGACY_WEB_SEARCH_PROVIDER:
            expected_fields |= {"provider_contract", "provider_contract_hash"}
        if not isinstance(request, Mapping) or set(request) != expected_fields:
            raise PublicWebCoreSearchError("search request has an invalid closed shape")
        rebuilt = self.build_request(request["parameters"], created_at=request["created_at"])
        if rebuilt != dict(request):
            raise PublicWebCoreSearchError("search request identity drifted")
        parameters = rebuilt["parameters"]
        created_at = rebuilt["created_at"]
        suffix = rebuilt["request_hash"][:20]
        authorities = self.ensure_governed_authorities()
        profile = authorities["profile"]
        descriptor = authorities["descriptor"]
        binding = authorities["binding"]
        manifest = authorities["manifest"]

        work_id = f"work:gemini-web-search:{suffix}"
        call_id = f"connector-call:gemini-web-search:{suffix}"
        invocation_id = f"connector-invocation:gemini-web-search:{suffix}"
        artifact_ref = f"artifact:gemini-web-search:{suffix}:raw"
        request_id = f"connector-runner-request:gemini-web-search:{suffix}"
        grant_id = f"credential-grant:gemini-web-search:{suffix}"

        work = WorkOrder(
            schema_version="0.1",
            id=work_id,
            created_at=created_at,
            updated_at=created_at,
            question=(
                "Search the public web through the host-owned "
                + ("Gemini web_search tool " if self.expected_provider == LEGACY_WEB_SEARCH_PROVIDER
                   else f"{self.expected_provider} web_search tool ")
                + "into Core connector authority (discovery only)"
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
                raise PublicWebCoreSearchError(
                    f"web search runner request {request_id} is incomplete at durable "
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
                raise PublicWebCoreSearchError(
                    f"scheduler already holds another WorkOrder for {work.id}"
                )
            claim = self.scheduler.claim(
                RUNNER_ACTOR_REF, work_order_id=work.id, lease_seconds=self.lease_seconds
            )
            if claim is None:
                raise PublicWebCoreSearchError(
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
                "adapter_hash": web_search_adapter_hash(self.expected_provider),
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
            raise PublicWebCoreSearchError("web search invocation authority drifted")
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
            raise PublicWebCoreSearchError("web search source envelope authority drifted")
        if (
            source["connector_invocation_ref"] != invocation["id"]
            or source["operation"] != OPERATION
            or source["source"] != profile["source_identity"]["source_ref"]
        ):
            raise PublicWebCoreSearchError("web search source envelope does not bind the call")
        document_refs: list[str] = []
        for ref in source["source_record_refs"]:
            if _URL_REF_RE.fullmatch(ref) is None:
                raise PublicWebCoreSearchError("web search source record is not a public-web URL ref")
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

    def url_authorities(self, source_envelope_ref: str) -> list[dict[str, Any]]:
        """Rebuild the fetchable URL authorities of one completed search.

        Nothing is stored here: the authorities are derived from the exact
        raw artifact bytes and the envelope, which is also how the fetch lane
        will resolve a ``url_ref`` before retrieving original bytes.
        """

        source = self.receipts.get_source_envelope(source_envelope_ref)
        if source is None:
            raise PublicWebCoreSearchError("web search source envelope was not found")
        raw = self.spool.read_object(source["raw_response_hash"])
        return build_public_web_url_authorities(raw, source)


__all__ = [
    "ADAPTER_REF",
    "CREDENTIAL_SLOT_REF",
    "FakeWebSearchHandle",
    "GeminiWebSearchLiveAdapter",
    "OPERATION",
    "PublicWebCoreSearch",
    "PublicWebCoreSearchError",
    "SEARCH_CAPABILITY_ID",
    "SEARCH_KIND",
    "SEARCH_MAX_RECORDS",
    "SEARCH_PROFILE_REF",
    "TOOL_NAME",
    "WebSearchConnectorGovernance",
    "build_web_search_governance_record",
    "count_recent_web_search_calls",
    "public_web_urls_in_authority",
    "validate_web_search_spec",
    "web_search_adapter_hash",
    "web_search_fixture_hash",
    "web_search_permissions",
    "web_search_provider_contract_hash",
    "web_search_schema_hash",
    "web_search_source_hash",
    "web_search_spec_hash",
    "write_web_search_governance_proposal",
]
