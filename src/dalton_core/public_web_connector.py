"""Gemini discovery and independent public-web fetch adapters.

Gemini's host-owned ``web_search`` tool is a ranked discovery source.  Its
synthesized answer and snippets are deliberately kept inside the raw search
artifact and are never promoted as fetched page content.  A separate,
credential-free ``PublicWebFetchAdapter`` resolves one opaque URL authority
and retrieves the original bytes through ``PublicHttpTransport``.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from .connector_inventory import load_packaged_connector_inventory
from .connector_runner import (
    RunnerConflict,
    RunnerValidationError,
    validate_adapter_transport_observation,
    validate_connector_adapter_request,
)
from .mcp_managed_runner import validate_mcp_schema_instance
from .openclaw_connector_bridge import (
    BridgePermissionDenied,
    BridgeRateLimited,
    HostToolInvocationResult,
)
from .public_http_transport import PublicHttpRequest, PublicHttpTransport
from .store import canonical_json, content_hash


GEMINI_WEB_SEARCH_ADAPTER_PROTOCOL_VERSION = "0.1"
OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_REF = (
    "openclaw-bridge:gemini-web-search:0.1"
)
OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_HASH = content_hash(
    {
        "bridge_ref": OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_REF,
        "transport_kind": "host_tool",
        "source_ref": "source:public-web",
        "operation_tools": {"search_web": "web_search"},
        "provider": "gemini",
        "arbitrary_tool_execution": False,
        "credential_material_serialized": False,
        "search_content_is_discovery_only": True,
    }
)

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_RAW_SINK_RE = re.compile(r"^raw-sink:[0-9a-f]{64}$")
_MEDIA_TYPE_RE = re.compile(
    r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$"
)
_FRESHNESS = frozenset({"day", "week", "month", "year"})
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "key",
        "password",
        "refresh_token",
        "secret",
        "sig",
        "signature",
        "token",
    }
)


class PublicWebConnectorError(ValueError):
    """The public-web connector contract is malformed."""


class PublicWebAuthorityConflict(PublicWebConnectorError):
    """Discovery, URL, fetch, or source authority drifted."""


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RunnerValidationError(f"{name} must be an object")
    missing = fields - set(value)
    unknown = set(value) - fields
    if missing or unknown:
        raise RunnerValidationError(
            f"{name} closed shape mismatch: missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise RunnerValidationError(f"{name} must be finite JSON") from exc


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RunnerValidationError(f"{name} must be a non-empty string")
    return value


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise RunnerValidationError(f"{name} must be lowercase SHA-256")
    return value


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RunnerValidationError(f"{name} must be an integer >= {minimum}")
    return value


def _timestamp(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RunnerValidationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise RunnerValidationError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _validate_content_hash(wire: Mapping[str, Any], name: str) -> None:
    declared = _hash(wire["content_hash"], f"{name}.content_hash")
    expected = content_hash(
        {key: value for key, value in wire.items() if key != "content_hash"}
    )
    if declared != expected:
        raise RunnerConflict(f"{name} content_hash mismatch")


def _inventory_operation(slug: str, operation_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    template = load_packaged_connector_inventory()["templates"][slug]
    matches = [
        item
        for item in template["operations"]
        if item["operation"] == operation_name
    ]
    if len(matches) != 1:
        raise RunnerConflict(f"{slug} operation is not frozen")
    return template, matches[0]


def validate_gemini_search_parameters(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate OpenClaw/Gemini's mutually exclusive time-filter contract."""

    if not isinstance(value, Mapping):
        raise RunnerValidationError("Gemini search parameters must be an object")
    allowed = {"query", "date_after", "date_before", "freshness"}
    if set(value) - allowed or "query" not in value:
        raise RunnerValidationError("Gemini search parameters have an invalid closed shape")
    wire = json.loads(canonical_json(value))
    wire["query"] = _text(wire["query"], "query").strip()
    if not wire["query"] or len(wire["query"]) > 8_000:
        raise RunnerValidationError("Gemini search query length is invalid")
    freshness = wire.get("freshness")
    if freshness is not None:
        freshness = _text(freshness, "freshness")
        if freshness not in _FRESHNESS:
            raise RunnerValidationError("Gemini freshness is not supported")
        wire["freshness"] = freshness
    parsed_dates: dict[str, date] = {}
    for name in ("date_after", "date_before"):
        raw = wire.get(name)
        if raw is None:
            continue
        raw = _text(raw, name)
        if _DATE_RE.fullmatch(raw) is None:
            raise RunnerValidationError(f"{name} must be YYYY-MM-DD")
        try:
            parsed_dates[name] = date.fromisoformat(raw)
        except ValueError as exc:
            raise RunnerValidationError(f"{name} is not a calendar date") from exc
    if freshness is not None and parsed_dates:
        raise RunnerValidationError(
            "Gemini freshness and explicit date filters are mutually exclusive"
        )
    if (
        "date_after" in parsed_dates
        and "date_before" in parsed_dates
        and parsed_dates["date_after"] >= parsed_dates["date_before"]
    ):
        raise RunnerValidationError("date_after must be before date_before")
    return wire


_SEARCH_REQUEST_FIELDS = {
    "protocol_version",
    "connector_invocation_ref",
    "reservation_ref",
    "physical_attempt_number",
    "source_identity",
    "source_hash",
    "operation",
    "tool_name",
    "parameters",
    "query_hash",
    "input_schema_ref",
    "input_schema_hash",
    "output_schema_ref",
    "output_schema_hash",
    "bridge_ref",
    "bridge_hash",
    "credential_use_ref",
    "deadline_at",
    "max_response_bytes",
    "max_records",
    "raw_sink_ref",
    "content_hash",
}


def validate_gemini_web_search_adapter_request(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the bridge-facing, credential-free serialized search request."""

    wire = _closed(value, _SEARCH_REQUEST_FIELDS, "GeminiWebSearchAdapterRequest")
    if wire["protocol_version"] != GEMINI_WEB_SEARCH_ADAPTER_PROTOCOL_VERSION:
        raise RunnerValidationError("unsupported GeminiWebSearchAdapterRequest version")
    for name in (
        "connector_invocation_ref",
        "reservation_ref",
        "operation",
        "tool_name",
        "input_schema_ref",
        "output_schema_ref",
        "bridge_ref",
        "credential_use_ref",
    ):
        wire[name] = _text(wire[name], name)
    for name in (
        "source_hash",
        "query_hash",
        "input_schema_hash",
        "output_schema_hash",
        "bridge_hash",
    ):
        wire[name] = _hash(wire[name], name)
    wire["physical_attempt_number"] = _integer(
        wire["physical_attempt_number"], "physical_attempt_number", minimum=1
    )
    wire["max_response_bytes"] = _integer(
        wire["max_response_bytes"], "max_response_bytes", minimum=1
    )
    wire["max_records"] = _integer(
        wire["max_records"], "max_records", minimum=1
    )
    if wire["max_records"] > 10:
        raise RunnerValidationError("OpenClaw Gemini search supports at most 10 results")
    wire["deadline_at"] = _timestamp(wire["deadline_at"], "deadline_at")
    wire["raw_sink_ref"] = _text(wire["raw_sink_ref"], "raw_sink_ref")
    if _RAW_SINK_RE.fullmatch(wire["raw_sink_ref"]) is None:
        raise RunnerValidationError("raw_sink_ref must be Runner-derived")
    identity = _closed(
        wire["source_identity"],
        {"source_ref", "source_type", "source_version"},
        "source_identity",
    )
    template, operation = _inventory_operation(
        "gemini-web-search", "search_web"
    )
    if identity != template["source_identity"]:
        raise RunnerConflict("Gemini search source identity drifted")
    wire["source_identity"] = identity
    if wire["source_hash"] != content_hash(identity):
        raise RunnerConflict("Gemini search source hash drifted")
    parameters = validate_gemini_search_parameters(wire["parameters"])
    schema_documents = {
        item["schema_ref"]: item for item in template["schema_documents"]
    }
    input_document = schema_documents.get(wire["input_schema_ref"])
    expected = (
        operation["input_schema_ref"],
        operation["input_schema_hash"],
        operation["output_schema_ref"],
        operation["output_schema_hash"],
    )
    actual = (
        wire["input_schema_ref"],
        wire["input_schema_hash"],
        wire["output_schema_ref"],
        wire["output_schema_hash"],
    )
    if expected != actual or input_document is None:
        raise RunnerConflict("Gemini search schema authority drifted")
    validate_mcp_schema_instance(parameters, input_document["document"])
    wire["parameters"] = parameters
    if wire["query_hash"] != content_hash(
        {"operation": "search_web", "parameters": parameters}
    ):
        raise RunnerConflict("Gemini search query hash drifted")
    if (
        wire["operation"] != "search_web"
        or wire["tool_name"] != "web_search"
        or wire["bridge_ref"] != OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_REF
        or wire["bridge_hash"] != OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_HASH
    ):
        raise RunnerConflict("Gemini search bridge authority drifted")
    _validate_content_hash(wire, "GeminiWebSearchAdapterRequest")
    return wire


def gemini_web_search_tool_arguments(request: Mapping[str, Any]) -> dict[str, Any]:
    wire = validate_gemini_web_search_adapter_request(request)
    parameters = wire["parameters"]
    arguments = {"query": parameters["query"], "count": wire["max_records"]}
    for name in ("date_after", "date_before", "freshness"):
        if name in parameters:
            arguments[name] = parameters[name]
    return arguments


def _tool_text_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    content = result.get("content")
    if not isinstance(content, list):
        raise RunnerValidationError("OpenClaw web_search result lacks content blocks")
    text_blocks = [
        item.get("text")
        for item in content
        if isinstance(item, Mapping) and item.get("type") == "text"
    ]
    if len(text_blocks) != 1 or not isinstance(text_blocks[0], str):
        raise RunnerValidationError("OpenClaw web_search requires one JSON text block")
    try:
        payload = json.loads(text_blocks[0])
    except json.JSONDecodeError as exc:
        raise RunnerValidationError("OpenClaw web_search text is not JSON") from exc
    if not isinstance(payload, Mapping):
        raise RunnerValidationError("OpenClaw web_search payload must be an object")
    return dict(payload)


def _credential_shaped_query_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return normalized in _SENSITIVE_QUERY_KEYS or any(
        part in _SENSITIVE_QUERY_KEYS for part in normalized.split("_") if part
    )


def canonical_public_web_url(value: str) -> str:
    """Canonicalize a discoverable URL into the stricter fetchable subset."""

    value = _text(value, "citation.url")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise PublicWebConnectorError("public web URL contains control characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise PublicWebConnectorError("public web URL is malformed") from exc
    if parsed.scheme.lower() != "https":
        raise PublicWebConnectorError("public web URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise PublicWebConnectorError("public web URL userinfo is forbidden")
    if port not in (None, 443):
        raise PublicWebConnectorError("public web URL only permits port 443")
    hostname = parsed.hostname or ""
    try:
        hostname = hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise PublicWebConnectorError("public web URL hostname is invalid") from exc
    if not hostname or hostname == "localhost" or hostname.endswith(".local"):
        raise PublicWebConnectorError("public web URL hostname is not public")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise PublicWebConnectorError("public web URL literal IP is not public")
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if _credential_shaped_query_key(key):
            raise PublicWebConnectorError(
                "credential-shaped public web URL query is forbidden"
            )
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    path = parsed.path or "/"
    return urlunsplit(("https", netloc, path, parsed.query, ""))


def public_web_url_ref(url: str) -> str:
    canonical = canonical_public_web_url(url)
    return "public-web-url:sha256:" + hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()


def normalize_gemini_web_search_payload(
    payload: Mapping[str, Any],
    *,
    expected_query: str,
    max_records: int,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Validate the current OpenClaw Gemini provider result and derive URL refs."""

    if not isinstance(payload, Mapping):
        raise RunnerValidationError("Gemini web_search payload must be an object")
    if "error" in payload:
        raise RunnerValidationError(
            "Gemini web_search provider returned an error payload"
        )
    required = {
        "query",
        "provider",
        "model",
        "tookMs",
        "externalContent",
        "content",
        "citations",
    }
    if set(payload) != required:
        raise RunnerValidationError("Gemini web_search payload shape drifted")
    if payload["query"] != expected_query or payload["provider"] != "gemini":
        raise RunnerConflict("Gemini web_search query/provider drifted")
    _text(payload["model"], "Gemini model")
    _integer(payload["tookMs"], "Gemini tookMs")
    _text(payload["content"], "Gemini synthesized content")
    external = payload["externalContent"]
    if external != {
        "untrusted": True,
        "source": "web_search",
        "provider": "gemini",
        "wrapped": True,
    }:
        raise RunnerConflict("Gemini external-content trust label drifted")
    citations = payload["citations"]
    if not isinstance(citations, list):
        raise RunnerValidationError("Gemini citations must be an array")
    discoveries: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(citations):
        if not isinstance(item, Mapping) or not set(item).issubset({"url", "title"}):
            raise RunnerValidationError(
                f"Gemini citation[{index}] has an invalid shape"
            )
        canonical = canonical_public_web_url(item.get("url"))
        title = item.get("title")
        if title is not None:
            _text(title, f"Gemini citation[{index}].title")
        ref = public_web_url_ref(canonical)
        if ref in seen:
            continue
        seen.add(ref)
        discovery = {"url_ref": ref, "canonical_url": canonical}
        if title is not None:
            discovery["title"] = title
        discoveries.append(discovery)
    if len(discoveries) > max_records:
        raise RunnerValidationError("Gemini citations exceeded max_records")
    structured = {
        "source_record_refs": [item["url_ref"] for item in discoveries],
        "next_cursor": None,
        "provider_status": 200,
    }
    return structured, discoveries


def _observation(
    request_hash: str,
    *,
    outcome: str,
    provider_request_id: str | None,
    provider_status: int | None,
    structured_output: Mapping[str, Any] | None,
    source_record_refs: Sequence[str],
    retry_after_ms: int | None = None,
    provider_usage: Mapping[str, Any] | None = None,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    base = {
        "protocol_version": "0.1",
        "request_hash": request_hash,
        "outcome": outcome,
        "provider_request_id": provider_request_id,
        "provider_status_code": provider_status,
        "retry_after_ms": retry_after_ms,
        "structured_output": structured_output,
        "source_record_refs": list(source_record_refs),
        "cursor": None,
        "provider_usage": provider_usage,
        "error": error,
    }
    return validate_adapter_transport_observation(
        {**base, "content_hash": content_hash(base)}
    )


class GeminiWebSearchAdapter:
    """Invoke only OpenClaw's host-owned Gemini ``web_search`` tool."""

    def __call__(
        self,
        request: Mapping[str, Any],
        raw_sink: Any,
        credential_handle: Any,
    ) -> dict[str, Any]:
        wire = validate_gemini_web_search_adapter_request(request)
        invoke = getattr(credential_handle, "invoke", None)
        if not callable(invoke):
            raise RunnerValidationError("host-owned Gemini handle lacks invoke")
        try:
            invocation = invoke(
                "web_search",
                gemini_web_search_tool_arguments(wire),
                call_ref=wire["credential_use_ref"],
                deadline_at=wire["deadline_at"],
                max_response_bytes=wire["max_response_bytes"],
            )
        except BridgeRateLimited as exc:
            return _observation(
                wire["content_hash"],
                outcome="rate_limited",
                provider_request_id=None,
                provider_status=429,
                structured_output=None,
                source_record_refs=[],
                retry_after_ms=exc.retry_after_ms,
                error={
                    "code": "rate_limited",
                    "message": str(exc)[:1000],
                    "retryable": True,
                },
            )
        except BridgePermissionDenied as exc:
            return _observation(
                wire["content_hash"],
                outcome="failed",
                provider_request_id=None,
                provider_status=403,
                structured_output=None,
                source_record_refs=[],
                error={
                    "code": "permission_denied",
                    "message": str(exc)[:1000],
                    "retryable": False,
                },
            )
        if not isinstance(invocation, HostToolInvocationResult):
            raise RunnerValidationError("Gemini handle returned another result type")
        if len(invocation.raw_response) > wire["max_response_bytes"]:
            raise RunnerValidationError("Gemini raw response exceeds byte limit")
        if invocation.result.get("isError") is True:
            return _observation(
                wire["content_hash"],
                outcome="failed",
                provider_request_id=invocation.request_id,
                provider_status=502,
                structured_output=None,
                source_record_refs=[],
                error={
                    "code": "source_error",
                    "message": "OpenClaw web_search returned an error",
                    "retryable": True,
                },
            )
        payload = _tool_text_payload(invocation.result)
        if "error" in payload:
            code = str(payload.get("error") or "source_error")
            permission = code in {
                "missing_gemini_api_key",
                "permission_denied",
                "revoked",
            }
            return _observation(
                wire["content_hash"],
                outcome="failed",
                provider_request_id=invocation.request_id,
                provider_status=403 if permission else 502,
                structured_output=None,
                source_record_refs=[],
                error={
                    "code": "permission_denied" if permission else "source_error",
                    "message": str(payload.get("message") or code)[:1000],
                    "retryable": not permission,
                },
            )
        structured, _ = normalize_gemini_web_search_payload(
            payload,
            expected_query=wire["parameters"]["query"],
            max_records=wire["max_records"],
        )
        raw_sink.write(invocation.raw_response)
        return _observation(
            wire["content_hash"],
            outcome="succeeded",
            provider_request_id=invocation.request_id,
            provider_status=200,
            structured_output=structured,
            source_record_refs=structured["source_record_refs"],
            provider_usage={"raw_media_type": "application/json"},
        )


_URL_AUTHORITY_FIELDS = {
    "schema_version",
    "id",
    "created_at",
    "url_ref",
    "canonical_url",
    "url_hash",
    "host",
    "discovery_source_envelope_ref",
    "discovery_source_envelope_hash",
    "discovery_raw_artifact_version_ref",
    "search_record_ref",
    "content_hash",
}


def validate_public_web_url_authority(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    wire = _closed(value, _URL_AUTHORITY_FIELDS, "PublicWebUrlAuthority")
    if wire["schema_version"] != "0.1":
        raise RunnerValidationError("unsupported PublicWebUrlAuthority version")
    for name in (
        "id",
        "url_ref",
        "discovery_source_envelope_ref",
        "discovery_raw_artifact_version_ref",
        "search_record_ref",
    ):
        wire[name] = _text(wire[name], name)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    wire["discovery_source_envelope_hash"] = _hash(
        wire["discovery_source_envelope_hash"],
        "discovery_source_envelope_hash",
    )
    canonical = canonical_public_web_url(wire["canonical_url"])
    if canonical != wire["canonical_url"]:
        raise RunnerConflict("PublicWebUrlAuthority URL is not canonical")
    expected_url_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if wire["url_hash"] != expected_url_hash:
        raise RunnerConflict("PublicWebUrlAuthority url_hash drifted")
    expected_ref = "public-web-url:sha256:" + expected_url_hash
    if wire["url_ref"] != expected_ref or wire["search_record_ref"] != expected_ref:
        raise RunnerConflict("PublicWebUrlAuthority URL ref drifted")
    if wire["host"] != urlsplit(canonical).hostname:
        raise RunnerConflict("PublicWebUrlAuthority host drifted")
    expected_id = (
        "public-web-url-authority:"
        + content_hash(
            {
                "url_ref": wire["url_ref"],
                "source_envelope_hash": wire["discovery_source_envelope_hash"],
            }
        )
    )
    if wire["id"] != expected_id:
        raise RunnerConflict("PublicWebUrlAuthority id is not deterministic")
    _validate_content_hash(wire, "PublicWebUrlAuthority")
    return wire


def _payload_from_raw_host_response(raw_response: bytes) -> dict[str, Any]:
    if not isinstance(raw_response, bytes):
        raise RunnerValidationError("Gemini raw response must be bytes")
    try:
        envelope = json.loads(raw_response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerValidationError("Gemini raw response is not strict JSON") from exc
    if not isinstance(envelope, Mapping):
        raise RunnerValidationError("Gemini raw response must be an object")
    result = envelope.get("result", envelope)
    if not isinstance(result, Mapping):
        raise RunnerValidationError("Gemini raw response lacks a tool result")
    return _tool_text_payload(result)


def build_public_web_url_authorities(
    raw_response: bytes,
    source_envelope: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Rebuild fetchable URL authorities from exact ranked-search bytes."""

    if not isinstance(source_envelope, Mapping):
        raise RunnerValidationError("search SourceEnvelope must be an object")
    source = json.loads(canonical_json(source_envelope))
    if (
        source.get("source") != "source:public-web"
        or source.get("operation") != "search_web"
        or source.get("completeness") != "ranked"
        or source.get("status") not in {"complete", "empty"}
        or source.get("cursor") is not None
    ):
        raise PublicWebAuthorityConflict(
            "URL authority requires a completed ranked Gemini discovery source"
        )
    source_hash = source.get("content_hash")
    if not isinstance(source_hash, str) or _HASH_RE.fullmatch(source_hash) is None:
        raise RunnerValidationError("search SourceEnvelope hash is invalid")
    expected_source_hash = content_hash(
        {key: value for key, value in source.items() if key != "content_hash"}
    )
    if source_hash != expected_source_hash:
        raise PublicWebAuthorityConflict("search SourceEnvelope hash drifted")
    raw_hash = hashlib.sha256(raw_response).hexdigest()
    if source.get("raw_response_hash") != raw_hash:
        raise PublicWebAuthorityConflict("search raw response hash drifted")
    payload = _payload_from_raw_host_response(raw_response)
    structured, discoveries = normalize_gemini_web_search_payload(
        payload,
        expected_query=payload.get("query"),
        max_records=10,
    )
    if structured["source_record_refs"] != source.get("source_record_refs"):
        raise PublicWebAuthorityConflict(
            "search SourceEnvelope refs differ from exact Gemini citations"
        )
    created_at = source.get("retrieved_at")
    artifact_ref = source.get("raw_artifact_version_ref")
    if not isinstance(created_at, str) or not isinstance(artifact_ref, str):
        raise RunnerValidationError("search source lacks retrieval/artifact authority")
    authorities: list[dict[str, Any]] = []
    for discovery in discoveries:
        url_hash = discovery["url_ref"].removeprefix(
            "public-web-url:sha256:"
        )
        identity = {
            "url_ref": discovery["url_ref"],
            "source_envelope_hash": source_hash,
        }
        base = {
            "schema_version": "0.1",
            "id": "public-web-url-authority:" + content_hash(identity),
            "created_at": created_at,
            "url_ref": discovery["url_ref"],
            "canonical_url": discovery["canonical_url"],
            "url_hash": url_hash,
            "host": urlsplit(discovery["canonical_url"]).hostname,
            "discovery_source_envelope_ref": source["id"],
            "discovery_source_envelope_hash": source_hash,
            "discovery_raw_artifact_version_ref": artifact_ref,
            "search_record_ref": discovery["url_ref"],
        }
        authorities.append(
            validate_public_web_url_authority(
                {**base, "content_hash": content_hash(base)}
            )
        )
    return authorities


class PublicWebUrlAuthorityResolver:
    """Immutable, reconstructable URL-ref resolver for one runner epoch."""

    def __init__(self, authorities: Sequence[Mapping[str, Any]]) -> None:
        validated = [
            validate_public_web_url_authority(item) for item in authorities
        ]
        by_ref = {item["url_ref"]: item for item in validated}
        if not validated or len(by_ref) != len(validated):
            raise RunnerValidationError(
                "public web URL authorities must be non-empty and unique"
            )
        self._by_ref = MappingProxyType(by_ref)
        self.content_hash = content_hash(
            {
                "authorities": [
                    {"ref": item["url_ref"], "hash": item["content_hash"]}
                    for item in sorted(validated, key=lambda item: item["url_ref"])
                ]
            }
        )

    def resolve(self, url_ref: str) -> Mapping[str, Any]:
        authority = self._by_ref.get(url_ref)
        if authority is None:
            raise PublicWebAuthorityConflict("public web URL ref is not authorized")
        return MappingProxyType(authority)


def _retry_after_ms(headers: Mapping[str, str]) -> int | None:
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds <= 0 or seconds > 86_400:
        return None
    return max(1, int(seconds * 1000))


def _raw_media_type(headers: Mapping[str, str]) -> str:
    value = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if not value or _MEDIA_TYPE_RE.fullmatch(value) is None:
        return "application/octet-stream"
    return value


class PublicWebFetchAdapter:
    """Fetch one URL authority through the existing SSRF-safe HTTPS lane."""

    def __init__(
        self,
        *,
        url_authority_resolver: PublicWebUrlAuthorityResolver,
        transport: PublicHttpTransport | None = None,
        user_agent: str = "DaltonResearchConnector/0.1 operator@example.invalid",
        clock: Any | None = None,
        source_identity: Mapping[str, Any] | None = None,
        allowed_operations: Sequence[str] | None = None,
    ) -> None:
        if not isinstance(url_authority_resolver, PublicWebUrlAuthorityResolver):
            raise TypeError("url_authority_resolver must be exact")
        self.url_authority_resolver = url_authority_resolver
        self.transport = transport or PublicHttpTransport()
        self.user_agent = _text(user_agent, "user_agent")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.source_identity = json.loads(canonical_json(
            source_identity if source_identity is not None else {
                "source_ref": "source:public-web",
                "source_type": "public_web",
                "source_version": "inventory-2026-08-14",
            }
        ))
        if set(self.source_identity) != {
            "source_ref", "source_type", "source_version",
        }:
            raise RunnerValidationError("public web fetch source identity is invalid")
        for name in ("source_ref", "source_type", "source_version"):
            self.source_identity[name] = _text(self.source_identity[name], name)
        if self.source_identity["source_type"] != "public_web":
            raise RunnerValidationError(
                "public web fetch cannot relabel bytes as a stronger source type"
            )
        operations = tuple(
            ("fetch_get", "fetch_head")
            if allowed_operations is None else allowed_operations
        )
        if not operations or len(set(operations)) != len(operations) or any(
            operation not in {"fetch_get", "fetch_head"}
            for operation in operations
        ):
            raise RunnerValidationError("public web fetch operations are invalid")
        self.allowed_operations = frozenset(operations)

    def _url_ref(self, wire: Mapping[str, Any]) -> str:
        if set(wire["parameters"]) != {"url_ref"}:
            raise RunnerValidationError(
                "public web fetch parameters must contain url_ref"
            )
        return _text(wire["parameters"]["url_ref"], "url_ref")

    def _successful_source_record_ref(
        self,
        wire: Mapping[str, Any],
        *,
        canonical_final_url: str,
        response_body: bytes,
        method: str,
    ) -> str:
        final_url_hash = hashlib.sha256(
            canonical_final_url.encode("utf-8")
        ).hexdigest()
        body_hash = hashlib.sha256(response_body).hexdigest()
        prefix = "public-web-document" if method == "GET" else "public-web-head"
        return f"{prefix}:url-sha256:{final_url_hash}:body-sha256:{body_hash}"

    def __call__(
        self,
        request: Mapping[str, Any],
        raw_sink: Any,
        credential_handle: Any | None = None,
    ) -> dict[str, Any]:
        if credential_handle is not None:
            raise RunnerValidationError(
                "public web fetch does not accept credential handles"
            )
        wire = validate_connector_adapter_request(request)
        if wire["source_identity"] != self.source_identity:
            raise RunnerConflict("public web fetch source identity drifted")
        if wire["operation"] not in self.allowed_operations:
            raise RunnerValidationError("public web fetch operation is not approved")
        url_ref = self._url_ref(wire)
        authority = self.url_authority_resolver.resolve(url_ref)
        if wire["allowed_hosts"] != [authority["host"]]:
            raise RunnerConflict(
                "public web fetch host allowlist differs from URL authority"
            )
        try:
            deadline = datetime.fromisoformat(
                wire["deadline_at"].replace("Z", "+00:00")
            )
            now = self.clock()
            if deadline.tzinfo is None or not isinstance(now, datetime) or now.tzinfo is None:
                raise ValueError
            remaining = (
                deadline.astimezone(timezone.utc)
                - now.astimezone(timezone.utc)
            ).total_seconds()
        except (TypeError, ValueError) as exc:
            raise RunnerValidationError("public web fetch deadline is invalid") from exc
        if remaining <= 0:
            raise RunnerValidationError("public web fetch deadline has expired")
        method = "GET" if wire["operation"] == "fetch_get" else "HEAD"
        response = self.transport.request(
            PublicHttpRequest(
                method,
                authority["canonical_url"],
                {
                    "User-Agent": self.user_agent,
                    "Accept": "*/*",
                },
            ),
            raw_sink,
            allowed_hosts=wire["allowed_hosts"],
            allow_redirects=wire["network_policy"]["allow_redirects"],
            max_redirects=wire["network_policy"]["max_redirects"],
            max_response_bytes=wire["max_response_bytes"],
            timeout_seconds=max(0.001, remaining),
        )
        provider_request_id = "public-web-http:" + content_hash(
            {
                "method": method,
                "url_ref": url_ref,
                "final_url": response.final_url,
                "status": response.status,
                "body_hash": hashlib.sha256(response.body).hexdigest(),
            }
        )
        media_type = _raw_media_type(response.headers)
        if response.status == 429:
            retry_after_ms = _retry_after_ms(response.headers)
            if retry_after_ms is not None:
                return _observation(
                    wire["content_hash"],
                    outcome="rate_limited",
                    provider_request_id=provider_request_id,
                    provider_status=429,
                    structured_output=None,
                    source_record_refs=[],
                    retry_after_ms=retry_after_ms,
                    provider_usage={"raw_media_type": media_type},
                    error={
                        "code": "rate_limited",
                        "message": "public web fetch was rate limited",
                        "retryable": True,
                    },
                )
        if not 200 <= response.status < 300:
            return _observation(
                wire["content_hash"],
                outcome="failed",
                provider_request_id=provider_request_id,
                provider_status=response.status,
                structured_output=None,
                source_record_refs=[],
                provider_usage={"raw_media_type": media_type},
                error={
                    "code": "http_status",
                    "message": f"public web fetch returned HTTP {response.status}",
                    "retryable": response.status in {408, 429, 500, 502, 503, 504},
                },
            )
        final_url = canonical_public_web_url(response.final_url)
        record_ref = self._successful_source_record_ref(
            wire,
            canonical_final_url=final_url,
            response_body=response.body,
            method=method,
        )
        structured = {
            "source_record_refs": [record_ref],
            "next_cursor": None,
            "provider_status": response.status,
        }
        return _observation(
            wire["content_hash"],
            outcome="succeeded",
            provider_request_id=provider_request_id,
            provider_status=response.status,
            structured_output=structured,
            source_record_refs=[record_ref],
            provider_usage={"raw_media_type": media_type},
        )


__all__ = [
    "GEMINI_WEB_SEARCH_ADAPTER_PROTOCOL_VERSION",
    "GeminiWebSearchAdapter",
    "OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_HASH",
    "OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_REF",
    "PublicWebAuthorityConflict",
    "PublicWebConnectorError",
    "PublicWebFetchAdapter",
    "PublicWebUrlAuthorityResolver",
    "build_public_web_url_authorities",
    "canonical_public_web_url",
    "gemini_web_search_tool_arguments",
    "normalize_gemini_web_search_payload",
    "public_web_url_ref",
    "validate_gemini_search_parameters",
    "validate_gemini_web_search_adapter_request",
    "validate_public_web_url_authority",
]
