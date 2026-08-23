"""Host-owned bridge primitives for OpenClaw-managed connector tools.

Dalton Core never receives an MCP endpoint, token, cookie, server config, or
arbitrary tool name.  A trusted runtime creates an opaque handle with an exact
allowlist and gives that object to ``CredentialAuthorityStore``.  The connector
adapter can invoke only the operation frozen by its plan.

The first implementation is a bounded streamable-HTTP MCP handle for the local
AlphaEngine service.  The same interface can later be implemented by the
OpenClaw gateway for gateway-visible host tools such as Gemini web search.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from .store import canonical_json, content_hash


_REF_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9+._-]*:[A-Za-z0-9][A-Za-z0-9:._-]*$"
)


class OpenClawConnectorBridgeError(Exception):
    """Base error for the host-owned connector bridge."""


class BridgeRequestRejected(OpenClawConnectorBridgeError, ValueError):
    pass


class BridgeRateLimited(OpenClawConnectorBridgeError):
    def __init__(self, message: str, *, retry_after_ms: int) -> None:
        super().__init__(message)
        if isinstance(retry_after_ms, bool) or retry_after_ms < 1:
            raise ValueError("retry_after_ms must be a positive integer")
        self.retry_after_ms = int(retry_after_ms)


class BridgePermissionDenied(OpenClawConnectorBridgeError):
    pass


class BridgeResponseTooLarge(OpenClawConnectorBridgeError):
    pass


@dataclass(frozen=True, slots=True)
class HostToolInvocationResult:
    """Exact raw RPC body plus the parsed MCP CallToolResult."""

    request_id: str
    raw_response: bytes
    result: Mapping[str, Any]


class OpenClawToolHandle(Protocol):
    """Opaque host-owned handle accepted by live connector adapters."""

    def invoke(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        call_ref: str,
        deadline_at: str,
        max_response_bytes: int,
    ) -> HostToolInvocationResult: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _parse_deadline(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise BridgeRequestRejected("deadline_at must be a non-empty RFC3339 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BridgeRequestRejected("deadline_at must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise BridgeRequestRejected("deadline_at must include timezone")
    return parsed.astimezone(timezone.utc)


def _parse_mcp_body(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8", errors="strict").strip()
    except UnicodeError as exc:
        raise OpenClawConnectorBridgeError("MCP response is not strict UTF-8") from exc
    if not text:
        raise OpenClawConnectorBridgeError("MCP response is empty")
    try:
        if text.startswith("{"):
            payload = json.loads(text)
        else:
            data_lines = [
                line[5:].strip() for line in text.splitlines()
                if line.startswith("data:")
            ]
            if not data_lines:
                raise ValueError("response is neither JSON nor SSE data")
            payload = json.loads(data_lines[-1])
    except (ValueError, json.JSONDecodeError) as exc:
        raise OpenClawConnectorBridgeError("MCP response is malformed") from exc
    if not isinstance(payload, Mapping):
        raise OpenClawConnectorBridgeError("MCP response must be an object")
    return dict(payload)


class LoopbackStreamableHttpMcpHandle:
    """Bounded client for an operator-installed loopback MCP endpoint.

    The endpoint and allowlist live only in this opaque object.  They are never
    serialized into a ConnectorProfile, RunnerRequest, AdapterRequest, Ledger,
    journal, or ArtifactVersion.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        allowed_tools: Mapping[str, str],
        timeout_seconds: float = 120.0,
    ) -> None:
        parsed = urllib.parse.urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path != "/mcp"
        ):
            raise BridgeRequestRejected(
                "MCP endpoint must be an uncredentialed loopback HTTP URL"
            )
        try:
            port = parsed.port
        except ValueError as exc:
            raise BridgeRequestRejected("MCP endpoint port is invalid") from exc
        if port is None or not 1 <= port <= 65535:
            raise BridgeRequestRejected("MCP endpoint requires an explicit port")
        if (
            not isinstance(allowed_tools, Mapping)
            or not allowed_tools
            or any(
                not isinstance(operation, str)
                or not operation
                or not isinstance(tool, str)
                or not tool
                for operation, tool in allowed_tools.items()
            )
        ):
            raise BridgeRequestRejected("allowed_tools must be a non-empty string map")
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise BridgeRequestRejected("timeout_seconds must be positive")
        self._endpoint = endpoint
        self._allowed_tools = dict(allowed_tools)
        self._timeout_seconds = float(timeout_seconds)
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def invoke(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        call_ref: str,
        deadline_at: str,
        max_response_bytes: int,
    ) -> HostToolInvocationResult:
        if tool_name not in self._allowed_tools.values():
            raise BridgeRequestRejected("tool is not in the host-owned allowlist")
        if not isinstance(arguments, Mapping):
            raise BridgeRequestRejected("tool arguments must be an object")
        if not isinstance(call_ref, str) or _REF_RE.fullmatch(call_ref) is None:
            raise BridgeRequestRejected("call_ref must be an opaque namespaced ref")
        if isinstance(max_response_bytes, bool) or max_response_bytes < 1:
            raise BridgeRequestRejected("max_response_bytes must be positive")
        try:
            args = json.loads(canonical_json(arguments))
        except (TypeError, ValueError) as exc:
            raise BridgeRequestRejected("tool arguments must be finite JSON") from exc
        remaining = (
            _parse_deadline(deadline_at) - datetime.now(timezone.utc)
        ).total_seconds()
        if remaining <= 0:
            raise BridgeRequestRejected("tool deadline has expired")
        timeout = min(remaining, self._timeout_seconds)
        request_id = "dalton-" + content_hash(
            {"call_ref": call_ref, "tool_name": tool_name, "arguments": args}
        )[:24]
        body = canonical_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": args},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(max_response_bytes + 1)
                if len(raw) > max_response_bytes:
                    raise BridgeResponseTooLarge(
                        "MCP response exceeds the connector byte limit"
                    )
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry = exc.headers.get("Retry-After")
                retry_after_ms = 1000
                if retry is not None:
                    try:
                        retry_after_ms = max(1, int(float(retry) * 1000))
                    except ValueError:
                        retry_after_ms = 1000
                raise BridgeRateLimited(
                    "MCP endpoint rate limited the request",
                    retry_after_ms=retry_after_ms,
                ) from exc
            if exc.code in {401, 403}:
                raise BridgePermissionDenied(
                    "MCP endpoint rejected host authorization"
                ) from exc
            raise OpenClawConnectorBridgeError(
                f"MCP endpoint returned HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise OpenClawConnectorBridgeError(
                "MCP endpoint is unavailable"
            ) from exc
        payload = _parse_mcp_body(raw)
        if payload.get("id") != request_id:
            raise OpenClawConnectorBridgeError("MCP response id does not bind request")
        if payload.get("jsonrpc") != "2.0":
            raise OpenClawConnectorBridgeError("MCP response protocol is invalid")
        if payload.get("error") is not None:
            error = payload["error"]
            message = (
                str(error.get("message", "MCP tool failed"))
                if isinstance(error, Mapping)
                else "MCP tool failed"
            )
            raise OpenClawConnectorBridgeError(message[:1000])
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise OpenClawConnectorBridgeError("MCP result must be an object")
        return HostToolInvocationResult(
            request_id=request_id,
            raw_response=raw,
            result=dict(result),
        )


__all__ = [
    "BridgePermissionDenied",
    "BridgeRateLimited",
    "BridgeRequestRejected",
    "BridgeResponseTooLarge",
    "HostToolInvocationResult",
    "LoopbackStreamableHttpMcpHandle",
    "OpenClawConnectorBridgeError",
    "OpenClawToolHandle",
]
