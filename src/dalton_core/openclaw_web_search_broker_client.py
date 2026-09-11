"""Client for the host-owned OpenClaw web search broker (P9d-4d).

P9d-4a froze the Gemini ``search_web`` bridge but had no way to reach it: the
gateway exposes web search to OpenClaw agents, not over MCP, so a networked
search child had nothing to call and refused before spawning.  This module
closes that gap the way the model broker already does for completions: a
Dalton-owned OpenClaw plugin owns the provider and its credential and serves
one bounded search over an owner-only Unix socket; Core sends a query and
never a key, provider, model, endpoint or header.

``WebSearchBrokerHandle`` implements the same ``OpenClawToolHandle`` protocol
the loopback MCP handle implements, so the frozen adapter, transport plan and
authority chain above it are unchanged.  The broker's reply frame is stored
verbatim as the raw artifact: it already carries the provider payload inside
a tool-result envelope, which is exactly what the public-web URL authority
rebuild parses.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import socket
import stat
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from .openclaw_connector_bridge import (
    BridgePermissionDenied,
    BridgeRateLimited,
    BridgeRequestRejected,
    BridgeResponseTooLarge,
    HostToolInvocationResult,
)
from .store import canonical_json


PROTOCOL_VERSION = "0.1"
TOOL_NAME = "web_search"
DEFAULT_PROFILE_ID = "profile:web-search"
DEFAULT_CLIENT_ID = "client:dalton-core"
# The host gives no retry hint, so a rate limit is reported with one
# conservative fixed delay rather than an invented provider value.
DEFAULT_RETRY_AFTER_MS = 60_000
# The host's own web search timeout ceiling; a longer lease is clamped rather
# than sent, because the broker refuses a timeout above its configured max.
DEFAULT_MAX_TIMEOUT_MS = 240_000
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_CLIENT_RE = re.compile(r"^client:[A-Za-z0-9._-]+$")
_PROFILE_RE = re.compile(r"^profile:[A-Za-z0-9._-]+$")
_CALL_REF_RE = re.compile(r"^credential-use:[A-Za-z0-9._:-]+$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_PERMISSION_CODES = frozenset({"PROVIDER_PERMISSION_DENIED"})
_RATE_LIMIT_CODES = frozenset({"PROVIDER_RATE_LIMITED"})
_PROVIDER_CONTRACT_CODES = frozenset({"PROVIDER_CONTRACT_DRIFT"})


class WebSearchBrokerError(RuntimeError):
    """The broker socket, key or reply is unusable."""


class WebSearchProviderContractDrift(BridgeRequestRejected):
    """The broker proved its configured provider did not match the payload."""


def _require_owner_only(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise WebSearchBrokerError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise WebSearchBrokerError(f"{label} must be a regular file")
    if info.st_mode & 0o077:
        raise WebSearchBrokerError(f"{label} must be owner-only")
    if info.st_uid != os.getuid():
        raise WebSearchBrokerError(f"{label} must be owned by this user")


def load_broker_key(path: str | Path) -> str:
    """Read the broker's owner-only shared key without logging it."""

    target = Path(path).expanduser()
    _require_owner_only(target, "web search broker auth key")
    secret = target.read_text(encoding="utf-8").strip()
    if _KEY_RE.fullmatch(secret) is None:
        raise WebSearchBrokerError("web search broker auth key is invalid")
    return secret


def sign_request(core: Mapping[str, Any], *, secret: str, client_id: str, timestamp_ms: int, nonce: str) -> dict[str, Any]:
    """Attach the broker's HMAC envelope; the MAC covers the unsigned auth."""

    unsigned = {
        "scheme": "hmac-sha256-v1",
        "clientId": client_id,
        "timestampMs": timestamp_ms,
        "nonce": nonce,
    }
    payload = canonical_json({**dict(core), "auth": unsigned})
    mac = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), sha256).hexdigest()
    return {**dict(core), "auth": {**unsigned, "mac": mac}}


class WebSearchBrokerHandle:
    """Host-owned ``web_search`` handle backed by the OpenClaw search broker."""

    def __init__(
        self,
        *,
        socket_path: str | Path,
        auth_key_path: str | Path,
        client_id: str = DEFAULT_CLIENT_ID,
        profile_id: str = DEFAULT_PROFILE_ID,
        retry_after_ms: int = DEFAULT_RETRY_AFTER_MS,
        max_timeout_ms: int = DEFAULT_MAX_TIMEOUT_MS,
        connect_timeout_seconds: float = 10.0,
    ) -> None:
        if _CLIENT_RE.fullmatch(client_id) is None:
            raise WebSearchBrokerError("web search broker client_id is invalid")
        if _PROFILE_RE.fullmatch(profile_id) is None:
            raise WebSearchBrokerError("web search broker profile_id is invalid")
        if isinstance(retry_after_ms, bool) or not isinstance(retry_after_ms, int) or retry_after_ms < 1:
            raise WebSearchBrokerError("retry_after_ms must be a positive integer")
        if isinstance(max_timeout_ms, bool) or not isinstance(max_timeout_ms, int) or max_timeout_ms < 1:
            raise WebSearchBrokerError("max_timeout_ms must be a positive integer")
        self.socket_path = str(Path(socket_path).expanduser())
        self.auth_key_path = Path(auth_key_path).expanduser()
        self.client_id = client_id
        self.profile_id = profile_id
        self.retry_after_ms = int(retry_after_ms)
        self.max_timeout_ms = int(max_timeout_ms)
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        # Fail closed at construction rather than after a lease is spent.
        self._secret = load_broker_key(self.auth_key_path)

    # -- request -------------------------------------------------------------
    def _core_request(self, arguments: Mapping[str, Any], *, call_ref: str, timeout_ms: int) -> dict[str, Any]:
        if _CALL_REF_RE.fullmatch(call_ref) is None:
            raise BridgeRequestRejected("web search broker call_ref must be a credential-use ref")
        if not isinstance(arguments, Mapping) or not set(arguments) <= {"query", "count", "date_after", "date_before", "freshness"}:
            raise BridgeRequestRejected("web search arguments have an unexpected shape")
        query = arguments.get("query")
        count = arguments.get("count")
        if not isinstance(query, str) or not query.strip():
            raise BridgeRequestRejected("web search query is required")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise BridgeRequestRejected("web search count must be a positive integer")
        if "freshness" in arguments:
            # Mission discovery always freezes an explicit window; the broker
            # protocol has no freshness field to forward it to.
            raise BridgeRequestRejected("web search broker does not accept a freshness shortcut")
        request: dict[str, Any] = {
            "schemaVersion": PROTOCOL_VERSION,
            "callRef": call_ref,
            "profileId": self.profile_id,
            "query": query,
            "count": count,
            "timeoutMs": timeout_ms,
        }
        after, before = arguments.get("date_after"), arguments.get("date_before")
        if (after is None) != (before is None):
            raise BridgeRequestRejected("web search date bounds must be supplied together")
        if after is not None:
            if _DATE_RE.fullmatch(str(after)) is None or _DATE_RE.fullmatch(str(before)) is None:
                raise BridgeRequestRejected("web search date bounds must be YYYY-MM-DD")
            request["dateAfter"] = after
            request["dateBefore"] = before
        return request

    def _timeout_ms(self, deadline_at: str) -> int:
        try:
            deadline = datetime.fromisoformat(str(deadline_at).replace("Z", "+00:00"))
        except ValueError as exc:
            raise BridgeRequestRejected("web search deadline is not RFC3339") from exc
        if deadline.tzinfo is None:
            raise BridgeRequestRejected("web search deadline must include a timezone")
        remaining_ms = int((deadline - datetime.now(timezone.utc)).total_seconds() * 1000)
        if remaining_ms <= 0:
            raise BridgeRequestRejected("web search deadline has already passed")
        return min(remaining_ms, self.max_timeout_ms)

    def _exchange(self, frame: bytes, *, timeout_seconds: float, max_response_bytes: int) -> bytes:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(max(0.001, timeout_seconds))
        try:
            try:
                connection.connect(self.socket_path)
            except OSError as exc:
                raise WebSearchBrokerError("web search broker socket is unavailable") from exc
            # Write the frame and keep this side open: the broker replies by
            # ending its side, and a half-close here would race that reply.
            connection.sendall(frame)
            chunks: list[bytes] = []
            total = 0
            while True:
                try:
                    chunk = connection.recv(65536)
                except socket.timeout as exc:
                    raise WebSearchBrokerError("web search broker did not reply before the deadline") from exc
                if not chunk:
                    break
                total += len(chunk)
                if total > max_response_bytes:
                    raise BridgeResponseTooLarge("web search broker reply exceeds the byte limit")
                chunks.append(chunk)
                if chunks[-1].endswith(b"\n"):
                    break
            return b"".join(chunks)
        finally:
            connection.close()

    # -- OpenClawToolHandle --------------------------------------------------
    def invoke(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        call_ref: str,
        deadline_at: str,
        max_response_bytes: int,
    ) -> HostToolInvocationResult:
        if tool_name != TOOL_NAME:
            raise BridgeRequestRejected("web search broker serves web_search only")
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or max_response_bytes < 1:
            raise BridgeRequestRejected("max_response_bytes must be a positive integer")
        timeout_ms = self._timeout_ms(deadline_at)
        core = self._core_request(arguments, call_ref=call_ref, timeout_ms=timeout_ms)
        signed = sign_request(
            core,
            secret=self._secret,
            client_id=self.client_id,
            timestamp_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            nonce=os.urandom(16).hex(),
        )
        frame = (canonical_json(signed) + "\n").encode("utf-8")
        raw = self._exchange(
            frame,
            timeout_seconds=min(timeout_ms / 1000.0, self.connect_timeout_seconds + timeout_ms / 1000.0),
            max_response_bytes=max_response_bytes,
        )
        body = raw.rstrip(b"\n")
        if not body:
            raise WebSearchBrokerError("web search broker returned an empty reply")
        try:
            response = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebSearchBrokerError("web search broker reply is not JSON") from exc
        if not isinstance(response, Mapping):
            raise WebSearchBrokerError("web search broker reply is not an object")
        if response.get("schemaVersion") != PROTOCOL_VERSION:
            raise WebSearchBrokerError("web search broker reply uses an unsupported protocol")
        if response.get("ok") is not True:
            self._raise_failure(response)
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise WebSearchBrokerError("web search broker reply carries no tool result")
        request_id = response.get("providerRequestId")
        if not isinstance(request_id, str) or not request_id:
            raise WebSearchBrokerError("web search broker reply carries no provider request id")
        # The stored raw artifact is the exact reply frame: it holds the
        # provider payload plus the broker's own provenance fields.
        return HostToolInvocationResult(request_id=request_id, raw_response=body, result=dict(result))

    def _raise_failure(self, response: Mapping[str, Any]) -> None:
        error = response.get("error")
        code = error.get("code") if isinstance(error, Mapping) else None
        message = error.get("message") if isinstance(error, Mapping) else None
        text = f"{code}: {message}" if code else "web search broker refused the request"
        if code in _RATE_LIMIT_CODES:
            raise BridgeRateLimited(text, retry_after_ms=self.retry_after_ms)
        if code in _PERMISSION_CODES:
            raise BridgePermissionDenied(text)
        if code in _PROVIDER_CONTRACT_CODES:
            raise WebSearchProviderContractDrift(text)
        raise BridgeRequestRejected(text)


__all__ = [
    "DEFAULT_CLIENT_ID",
    "DEFAULT_PROFILE_ID",
    "DEFAULT_RETRY_AFTER_MS",
    "PROTOCOL_VERSION",
    "TOOL_NAME",
    "WebSearchBrokerError",
    "WebSearchBrokerHandle",
    "WebSearchProviderContractDrift",
    "load_broker_key",
    "sign_request",
]
