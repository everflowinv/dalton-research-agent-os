"""Versioned, deliberately small protocol for the Dalton writer service.

The protocol is JSON-lines over a local ``AF_UNIX`` stream.  It is an
interchange boundary, not an authentication system: the server additionally
checks a capability-scoped token and the socket's owner-only permissions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


PROTOCOL_VERSION = "0.1"
MAX_FRAME_BYTES = 1024 * 1024


class ProtocolError(ValueError):
    """A malformed, unsupported, or over-sized protocol frame."""


class RemoteError(RuntimeError):
    """A structured error returned by the writer service."""

    def __init__(self, code: str, message: str, *, request_id: str | None = None):
        self.code = code
        self.request_id = request_id
        super().__init__(message)


class RemoteAuthorizationError(RemoteError):
    pass


class RemoteProtocolError(RemoteError):
    pass


_REQUEST_FIELDS = frozenset({"protocol_version", "request_id", "operation", "params", "auth_token"})
_RESPONSE_FIELDS = frozenset({"protocol_version", "request_id", "ok", "result", "error"})


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{field} must be a non-empty string")
    return value


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{field} must be an object")
    return dict(value)


def _strict_fields(value: Mapping[str, Any], allowed: frozenset[str], name: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ProtocolError(f"{name} contains unknown field(s)")


@dataclass(frozen=True, slots=True)
class Request:
    protocol_version: str
    request_id: str
    operation: str
    params: dict[str, Any]
    auth_token: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "operation": self.operation,
            "params": dict(self.params),
            "auth_token": self.auth_token,
        }


@dataclass(frozen=True, slots=True)
class Response:
    protocol_version: str
    request_id: str
    ok: bool
    result: Any = None
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "ok": self.ok,
            "result": self.result,
            "error": self.error,
        }
        return value


def parse_request(value: Mapping[str, Any]) -> Request:
    obj = _object(value, "request")
    _strict_fields(obj, _REQUEST_FIELDS, "request")
    version = _string(obj.get("protocol_version"), "protocol_version")
    if version != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol_version")
    return Request(
        protocol_version=version,
        request_id=_string(obj.get("request_id"), "request_id"),
        operation=_string(obj.get("operation"), "operation"),
        params=_object(obj.get("params"), "params"),
        auth_token=_string(obj.get("auth_token"), "auth_token"),
    )


def parse_response(value: Mapping[str, Any]) -> Response:
    obj = _object(value, "response")
    _strict_fields(obj, _RESPONSE_FIELDS, "response")
    version = _string(obj.get("protocol_version"), "protocol_version")
    if version != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol_version")
    request_id = _string(obj.get("request_id"), "request_id")
    ok = obj.get("ok")
    if not isinstance(ok, bool):
        raise ProtocolError("ok must be boolean")
    if ok:
        if obj.get("error") is not None:
            raise ProtocolError("successful response cannot contain error")
    else:
        error = _object(obj.get("error"), "error")
        _strict_fields(error, frozenset({"code", "message"}), "error")
        _string(error.get("code"), "error.code")
        _string(error.get("message"), "error.message")
    return Response(version, request_id, ok, obj.get("result"), obj.get("error"))


def encode_frame(value: Mapping[str, Any]) -> bytes:
    """Encode one strict JSON frame and reject oversized output."""
    encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    if len(encoded) > MAX_FRAME_BYTES:
        raise ProtocolError("frame exceeds maximum size")
    return encoded


def decode_frame(line: bytes) -> dict[str, Any]:
    if not isinstance(line, bytes) or len(line) > MAX_FRAME_BYTES:
        raise ProtocolError("frame exceeds maximum size")
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("frame is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ProtocolError("frame must be a JSON object")
    return dict(value)


def request_frame(request_id: str, operation: str, params: Mapping[str, Any], auth_token: str) -> bytes:
    request = Request(PROTOCOL_VERSION, _string(request_id, "request_id"), _string(operation, "operation"), _object(params, "params"), _string(auth_token, "auth_token"))
    return encode_frame(request.to_dict())


def success_frame(request_id: str, result: Any) -> bytes:
    return encode_frame(Response(PROTOCOL_VERSION, request_id, True, result=result).to_dict())


def error_frame(request_id: str, code: str, message: str) -> bytes:
    # Error messages are supplied by the server's sanitizing layer.  Keep the
    # wire shape tiny and never serialize exception objects or tracebacks.
    return encode_frame(Response(PROTOCOL_VERSION, request_id, False, error={"code": _string(code, "error.code"), "message": _string(message, "error.message")}).to_dict())
