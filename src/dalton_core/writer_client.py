"""Client for Dalton Core's local writer service.

The client deliberately stores only a socket endpoint and a scoped token.  It
has no ``DaltonStore`` import, no SQLite connection, and no database path.
"""

from __future__ import annotations

import json
import socket
import uuid
from typing import Any, Mapping

from .writer_protocol import (
    MAX_FRAME_BYTES,
    ProtocolError,
    RemoteAuthorizationError,
    RemoteError,
    RemoteProtocolError,
    decode_frame,
    parse_response,
    request_frame,
)


class WriterClient:
    def __init__(self, socket_path: str, token: str, *, timeout: float = 10.0):
        if not isinstance(socket_path, str) or not socket_path:
            raise ValueError("socket_path must be a non-empty string")
        if not isinstance(token, str) or not token:
            raise ValueError("token must be a non-empty string")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.socket_path = socket_path
        self._token = token
        self.timeout = float(timeout)

    def call(self, operation: str, params: Mapping[str, Any] | None = None, *, request_id: str | None = None) -> Any:
        if not isinstance(operation, str) or not operation:
            raise ValueError("operation must be a non-empty string")
        request_id = request_id or uuid.uuid4().hex
        frame = request_frame(request_id, operation, params or {}, self._token)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(self.socket_path)
                sock.sendall(frame)
                reader = sock.makefile("rb")
                line = reader.readline(MAX_FRAME_BYTES + 1)
                reader.close()
        except OSError as exc:
            # Do not expose the endpoint, OS path, or nested traceback to the
            # runtime.  The caller gets a stable transport error only.
            raise RemoteError("transport_error", "writer service is unavailable", request_id=request_id) from None
        try:
            response = parse_response(decode_frame(line))
        except ProtocolError:
            raise RemoteProtocolError("protocol_error", "writer service returned a malformed response", request_id=request_id) from None
        if response.request_id != request_id:
            raise RemoteProtocolError("protocol_error", "writer service returned an unexpected request id", request_id=request_id)
        if not response.ok:
            error = response.error or {}
            code = str(error.get("code", "remote_error"))
            message = str(error.get("message", "writer service rejected the request"))
            error_type = RemoteAuthorizationError if code == "forbidden" else RemoteError
            raise error_type(code, message, request_id=request_id)
        return response.result

    def register_invocation(self, invocation: Mapping[str, Any]) -> Any:
        return self.call("register_invocation", {"invocation": dict(invocation)})

    def stage_change(self, **params: Any) -> Any:
        return self.call("stage_change", params)

    def verify_change(self, **params: Any) -> Any:
        return self.call("verify_change", params)

    def commit(self, **params: Any) -> Any:
        return self.call("commit", params)

    def create_policy(self, **params: Any) -> Any:
        return self.call("create_policy", params)

    def current_pointer(self, thesis_id: str) -> Any:
        return self.call("current_pointer", {"thesis_id": thesis_id})

    def get_version(self, version_id: str) -> Any:
        return self.call("get_version", {"version_id": version_id})

    def list_events(self, aggregate_id: str | None = None) -> Any:
        return self.call("list_events", {} if aggregate_id is None else {"aggregate_id": aggregate_id})

    def active_policy(self) -> Any:
        return self.call("active_policy", {})

    def register_evidence(self, **params: Any) -> Any:
        return self.call("register_evidence", params)

    def register_claim(self, **params: Any) -> Any:
        return self.call("register_claim", params)

    def relate_evidence(self, **params: Any) -> Any:
        return self.call("relate_evidence", params)

    def adjudicate_claim(self, **params: Any) -> Any:
        return self.call("adjudicate_claim", params)

    def submit_capability_proposal(self, **params: Any) -> Any:
        return self.call("submit_capability_proposal", params)

    def record_capability_evaluation(self, **params: Any) -> Any:
        return self.call("record_capability_evaluation", params)

    def decide_capability_promotion(self, **params: Any) -> Any:
        return self.call("decide_capability_promotion", params)

    def rollback_capability(self, **params: Any) -> Any:
        return self.call("rollback_capability", params)

    def active_capability(self, capability_ref: str) -> Any:
        return self.call("active_capability", {"capability_ref": capability_ref})

    def get_capability_version(self, revision_ref: str) -> Any:
        return self.call("get_capability_version", {"revision_ref": revision_ref})

    def get_capability_evaluation(self, evaluation_id: str) -> Any:
        return self.call("get_capability_evaluation", {"evaluation_id": evaluation_id})

    def get_capability_decision(self, decision_id: str) -> Any:
        return self.call("get_capability_decision", {"decision_id": decision_id})

    def capability_pointer_history(self, capability_ref: str) -> Any:
        return self.call("capability_pointer_history", {"capability_ref": capability_ref})

    def __repr__(self) -> str:
        return f"WriterClient(socket_path=<local>, timeout={self.timeout!r})"


Client = WriterClient
