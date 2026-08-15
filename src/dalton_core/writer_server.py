"""Owner-only local writer service for Dalton Core.

The service is intentionally boring: one process owns the ``DaltonStore``
connection and clients can only invoke an explicit operation allowlist over a
local Unix stream.  It is a file/authority boundary, not a hostile same-UID
sandbox.  A process which can read this process's token/config or open the DB
file can still defeat it; production deployment must use separate OS users or
a storage service with real identities for that threat model.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import hmac
import json
import os
import re
import signal
import socket
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .agenda import (
    AgendaConflict,
    AgendaError,
    AgendaNotFound,
    AgendaStore,
    AgendaValidationError,
)
from .agenda_context import build_agenda_context
from .perception import PerceptionError
from .context_materializer import (
    ContextMaterializerConflict,
    ContextMaterializerError,
    ContextMaterializerUnsupported,
)
from .capability_registry import (
    CapabilityConflict,
    CapabilityNotFound,
    CapabilityRegistry,
    CapabilityRegistryError,
    EvaluationRejected,
    PermissionEscalation,
    PromotionRejected,
)
from .errors import (
    BadVerdict,
    DaltonStoreError,
    GateRejected,
    IdempotencyConflict,
    IndependenceViolation,
    InvocationConflict,
    NotFound,
    ValidationError,
    VerificationRequired,
)
from .store import DaltonStore
from .observability import (
    ObservabilityConflict,
    ObservabilityError,
    ObservabilityNotFound,
    ObservabilityStore,
    ObservabilityValidationError,
)
from .writer_protocol import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    decode_frame,
    encode_frame,
    error_frame,
    parse_request,
    success_frame,
)


class WriterServerError(RuntimeError):
    pass


_HUMAN_ACTOR_RE = re.compile(r"human:[A-Za-z0-9._-]+\Z")


def _validate_actor_ref(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise WriterServerError("actor_ref is invalid")
    if value.startswith("human:") and _HUMAN_ACTOR_RE.fullmatch(value) is None:
        raise WriterServerError("actor_ref is invalid")
    return value


@dataclass(frozen=True, slots=True)
class Principal:
    principal_id: str
    token: str
    operations: frozenset[str]
    # These are subject constraints, not claims supplied by the runtime.  The
    # server resolves the referenced invocation from its own store before
    # allowing a worker/verifier operation.
    allowed_invocation_refs: frozenset[str] = field(default_factory=frozenset)
    work_order_refs: frozenset[str] = field(default_factory=frozenset)
    unrestricted: bool = False
    actor_ref: str | None = None

    def __post_init__(self) -> None:
        _validate_actor_ref(self.actor_ref or self.principal_id)

    @property
    def is_unrestricted(self) -> bool:
        # ``core`` is the canonical bootstrap principal.  The explicit flag
        # is persisted in config; the name fallback keeps the small Python
        # bootstrap API backwards compatible while config remains strict.
        return self.unrestricted or self.principal_id == "core"

    @property
    def resolved_actor_ref(self) -> str:
        return _validate_actor_ref(self.actor_ref or self.principal_id)


MAX_CONNECTIONS = 8
CONNECTION_IDLE_TIMEOUT = 1.0
STORE_REQUEST_TIMEOUT = 30.0


WORKER_OPERATIONS = frozenset({"stage_change"})
VERIFIER_OPERATIONS = frozenset({"verify_change"})
RESEARCHER_OPERATIONS = frozenset({"register_evidence", "register_claim", "relate_evidence"})
ADJUDICATOR_OPERATIONS = frozenset({"adjudicate_claim"})
CAPABILITY_BUILDER_OPERATIONS = frozenset({"submit_capability_proposal"})
CAPABILITY_EVALUATOR_OPERATIONS = frozenset({"record_capability_evaluation"})
HUMAN_GOVERNANCE_OPERATIONS = frozenset({
    "create_policy", "decide_capability_promotion", "rollback_capability",
    "create_agenda_policy", "create_mandate", "create_priority_override",
    "set_agenda_pause", "record_agenda_feedback",
})
FEEDBACK_BRIDGE_OPERATIONS = frozenset({
    "list_agenda_feedback_targets", "record_agenda_feedback",
})
RESEARCH_REVIEW_CONTROL_OPERATIONS = frozenset({"commit_reviewed_candidate"})
SCOPED_FEEDBACK_PRINCIPALS = {
    "feedback-bridge": ("bridge:openclaw-discord",),
    "dashboard-control": ("bridge:tailscale-dashboard",),
    "agenda-timeout": ("automation:agenda-timeout",),
}
SCOPED_REVIEW_PRINCIPALS = {
    "research-review-control": "bridge:tailscale-review",
}
CORE_OPERATIONS = frozenset({
    "register_invocation", "stage_change", "verify_change", "commit",
    "commit_reviewed_candidate",
    "current_pointer", "get_version", "list_events", "active_policy",
    "register_evidence", "register_claim", "relate_evidence", "adjudicate_claim",
    "submit_capability_proposal", "record_capability_evaluation",
    "active_capability", "get_capability_version", "get_capability_evaluation",
    "get_capability_decision", "capability_pointer_history",
    "agenda_control_state", "active_agenda_policy", "active_mandates",
    "agenda_budget_status", "register_perception_snapshot",
    "materialize_agenda_context", "get_agenda_mandate_version",
    "get_agenda_policy_version", "get_perception_snapshot",
    "active_priority_overrides", "start_agenda_cycle", "add_agenda_candidates",
    "decide_agenda_cycle", "fail_agenda_cycle", "agenda_cycle", "agenda_cycle_by_key",
    "pending_agenda_outbox", "claim_agenda_outbox", "record_agenda_delivery",
    "create_workflow_version", "link_work_order", "record_usage",
    "create_price_rate_version", "record_cost",
})


# Explicit operation parameter contracts.  The server must reject unknown
# fields before they reach a method accepting **kwargs.
OPERATION_FIELDS: dict[str, frozenset[str]] = {
    "register_invocation": frozenset({"invocation"}),
    "stage_change": frozenset({"change", "change_id", "thesis_id", "content", "payload", "producer_invocation", "producer_invocation_id", "actor_id"}),
    "verify_change": frozenset({"change_id", "verification", "verification_id", "verifier_invocation", "verifier_invocation_id", "verdict", "findings", "actor_id"}),
    "commit": frozenset({"change_id", "verification_id", "idempotency_key", "request", "actor_id", "request_hash"}),
    "commit_reviewed_candidate": frozenset({"decision", "evidence", "claim", "idempotency_key"}),
    "create_policy": frozenset({"policy", "policy_version_id", "version_number", "activate", "policy_ref", "effective_from", "effective_until", "actor_ref", "prior_version_ref", "change_reason", "content_hash_value"}),
    "current_pointer": frozenset({"thesis_id"}),
    "get_version": frozenset({"version_id"}),
    "list_events": frozenset({"aggregate_id"}),
    "active_policy": frozenset(),
    "register_evidence": frozenset({"evidence", "evidence_ref", "evidence_id", "evidence_version_id", "actor_ref"}),
    "register_claim": frozenset({"claim", "claim_ref", "claim_id", "claim_version_id", "producer_invocation_refs", "actor_ref"}),
    "relate_evidence": frozenset({"relation", "relation_id", "idempotency_key", "actor_ref"}),
    "adjudicate_claim": frozenset({"adjudication", "adjudication_version_id", "adjudicator_invocation_ref", "subject_invocation_refs", "actor_ref"}),
    "submit_capability_proposal": frozenset({"proposal", "capability_ref", "version_number", "artifact_hash", "builder_invocation_ref", "idempotency_key", "actor_ref"}),
    "record_capability_evaluation": frozenset({"proposal_ref", "evaluation_id", "fixtures", "baseline", "results", "environment_hash", "evaluator_invocation_ref", "proposal_hash", "idempotency_key", "actor_ref"}),
    "decide_capability_promotion": frozenset({"proposal_ref", "decision", "evaluation_id", "decision_id", "requested_permissions", "rationale", "rollback_to_revision_ref", "idempotency_key", "actor_ref"}),
    "rollback_capability": frozenset({"capability_ref", "target_revision_ref", "reason", "decision_id", "idempotency_key", "actor_ref"}),
    "active_capability": frozenset({"capability_ref"}),
    "get_capability_version": frozenset({"revision_ref"}),
    "get_capability_evaluation": frozenset({"evaluation_id"}),
    "get_capability_decision": frozenset({"decision_id"}),
    "capability_pointer_history": frozenset({"capability_ref"}),
    "create_agenda_policy": frozenset({"policy", "effective_from", "effective_until", "activate", "version_id", "idempotency_key", "actor_ref"}),
    "active_agenda_policy": frozenset({"at"}),
    "agenda_budget_status": frozenset({"daily_since", "monthly_since"}),
    "create_mandate": frozenset({"mandate_ref", "objective", "scope_refs", "constraints", "success_criteria", "effective_from", "effective_until", "activate", "version_id", "idempotency_key", "actor_ref"}),
    "active_mandates": frozenset({"at"}),
    "create_priority_override": frozenset({"override_ref", "scope_refs", "weight_deltas", "rationale", "effective_from", "effective_until", "revoked", "version_id", "idempotency_key", "actor_ref"}),
    "active_priority_overrides": frozenset({"scope_ref", "at"}),
    "set_agenda_pause": frozenset({"paused", "reason", "version_id", "idempotency_key", "actor_ref"}),
    "agenda_control_state": frozenset(),
    "register_perception_snapshot": frozenset({"snapshot", "idempotency_key", "actor_ref"}),
    "materialize_agenda_context": frozenset({"cycle_id", "max_tokens", "max_bytes"}),
    "get_agenda_mandate_version": frozenset({"version_id"}),
    "get_agenda_policy_version": frozenset({"version_id"}),
    "get_perception_snapshot": frozenset({"snapshot_id"}),
    "start_agenda_cycle": frozenset({"cycle_key", "perception_snapshot_ref", "perception_snapshot_hash", "mandate_version_ref", "policy_version_ref", "company_ref", "cycle_id", "idempotency_key", "actor_ref"}),
    "add_agenda_candidates": frozenset({"cycle_id", "candidates", "idempotency_key", "actor_ref"}),
    "decide_agenda_cycle": frozenset({"cycle_id", "decision_id", "idempotency_key", "actor_ref"}),
    "fail_agenda_cycle": frozenset({"cycle_id", "reason", "metadata", "actor_ref"}),
    "agenda_cycle": frozenset({"cycle_id"}),
    "agenda_cycle_by_key": frozenset({"cycle_key"}),
    "pending_agenda_outbox": frozenset({"limit"}),
    "claim_agenda_outbox": frozenset({"endpoint_ref", "now", "claim_ttl_seconds", "max_attempts", "limit", "idempotency_key", "actor_ref"}),
    "record_agenda_delivery": frozenset({"message_id", "state", "delivery_attempt_id", "delivery_receipt_id", "error_code", "retry_after", "idempotency_key", "actor_ref"}),
    "list_agenda_feedback_targets": frozenset({"endpoint_ref", "limit"}),
    "record_agenda_feedback": frozenset({"decision_id", "verdict", "notes", "feedback_id", "idempotency_key", "subject_ref", "prior_feedback_ref", "source", "source_event_ref", "actor_ref"}),
    "create_workflow_version": frozenset({"workflow_ref", "title", "objective", "scope_refs", "root_work_order_refs", "governance_policy_ref", "prior_version_ref", "version_id", "idempotency_key", "actor_ref"}),
    "link_work_order": frozenset({"workflow_ref", "parent_work_order_ref", "child_work_order_ref", "relation", "sequence", "actor_ref", "link_id", "idempotency_key"}),
    "record_usage": frozenset({"invocation_ref", "entry_id", "occurred_at", "metering_source", "measurement_status", "raw_usage", "workflow_ref", "provider_usage_ref", "correction_of_ref", "actor_ref", "idempotency_key", "input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens", "requests", "duration_ms", "input_bytes", "output_bytes"}),
    "create_price_rate_version": frozenset({"price_rate_ref", "provider", "model", "charge_type", "unit_quantity", "unit_price_micros", "currency", "effective_from", "effective_until", "source_ref", "prior_version_ref", "version_id", "idempotency_key", "actor_ref"}),
    "record_cost": frozenset({"usage_entry_ref", "price_rate_refs", "amount_micros", "currency", "cost_status", "calculation_ref", "correction_of_ref", "cost_entry_id", "idempotency_key", "actor_ref"}),
}


OPERATION_ACTOR_FIELDS: dict[str, str] = {
    "stage_change": "actor_id",
    "verify_change": "actor_id",
    "commit": "actor_id",
    "create_policy": "actor_ref",
    "register_evidence": "actor_ref",
    "register_claim": "actor_ref",
    "relate_evidence": "actor_ref",
    "adjudicate_claim": "actor_ref",
    "submit_capability_proposal": "actor_ref",
    "record_capability_evaluation": "actor_ref",
    "decide_capability_promotion": "actor_ref",
    "rollback_capability": "actor_ref",
    "create_agenda_policy": "actor_ref",
    "create_mandate": "actor_ref",
    "create_priority_override": "actor_ref",
    "set_agenda_pause": "actor_ref",
    "register_perception_snapshot": "actor_ref",
    "start_agenda_cycle": "actor_ref",
    "add_agenda_candidates": "actor_ref",
    "decide_agenda_cycle": "actor_ref",
    "fail_agenda_cycle": "actor_ref",
    "claim_agenda_outbox": "actor_ref",
    "record_agenda_delivery": "actor_ref",
    "record_agenda_feedback": "actor_ref",
    "create_workflow_version": "actor_ref",
    "link_work_order": "actor_ref",
    "record_usage": "actor_ref",
    "create_price_rate_version": "actor_ref",
    "record_cost": "actor_ref",
}


def _require_owner_only(path: Path, label: str) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise WriterServerError(f"{label} is unavailable") from exc
    if mode & 0o077:
        raise WriterServerError(f"{label} must be owner-only")


def load_principals(path: str | Path) -> dict[str, Principal]:
    """Load an owner-only token config without retaining unrelated fields."""
    config_path = Path(path)
    _require_owner_only(config_path, "token config")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WriterServerError("token config is invalid") from exc
    if not isinstance(raw, Mapping) or set(raw) != {"schema_version", "principals"} or raw.get("schema_version") != PROTOCOL_VERSION:
        raise WriterServerError("token config is invalid")
    entries = raw.get("principals")
    if not isinstance(entries, list) or not entries:
        raise WriterServerError("token config is invalid")
    result: dict[str, Principal] = {}
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {"principal_id", "token", "operations", "allowed_invocation_refs", "work_order_refs", "unrestricted", "actor_ref"}:
            raise WriterServerError("token config is invalid")
        principal_id = entry.get("principal_id")
        token = entry.get("token")
        operations = entry.get("operations")
        allowed_invocation_refs = entry.get("allowed_invocation_refs")
        work_order_refs = entry.get("work_order_refs")
        unrestricted = entry.get("unrestricted")
        actor_ref = entry.get("actor_ref")
        if not isinstance(principal_id, str) or not principal_id or not isinstance(token, str) or not token:
            raise WriterServerError("token config is invalid")
        if not isinstance(operations, list) or not operations or any(not isinstance(x, str) or x not in OPERATION_FIELDS for x in operations):
            raise WriterServerError("token config is invalid")
        if not isinstance(allowed_invocation_refs, list) or any(not isinstance(x, str) or not x for x in allowed_invocation_refs):
            raise WriterServerError("token config is invalid")
        if not isinstance(work_order_refs, list) or any(not isinstance(x, str) or not x for x in work_order_refs):
            raise WriterServerError("token config is invalid")
        if not isinstance(unrestricted, bool) or (unrestricted and principal_id != "core"):
            raise WriterServerError("token config is invalid")
        try:
            _validate_actor_ref(actor_ref or principal_id)
        except WriterServerError:
            raise WriterServerError("token config is invalid") from None
        if principal_id == "worker" and not set(operations) <= WORKER_OPERATIONS:
            raise WriterServerError("token config is invalid")
        if principal_id == "verifier" and not set(operations) <= VERIFIER_OPERATIONS:
            raise WriterServerError("token config is invalid")
        scoped_actor = SCOPED_FEEDBACK_PRINCIPALS.get(principal_id)
        if scoped_actor is not None and (
            set(operations) != FEEDBACK_BRIDGE_OPERATIONS or actor_ref not in scoped_actor
        ):
            raise WriterServerError("token config is invalid")
        review_actor = SCOPED_REVIEW_PRINCIPALS.get(principal_id)
        if review_actor is not None and (
            set(operations) != RESEARCH_REVIEW_CONTROL_OPERATIONS
            or actor_ref != review_actor
        ):
            raise WriterServerError("token config is invalid")
        if principal_id in result:
            raise WriterServerError("token config is invalid")
        result[principal_id] = Principal(
            principal_id, token, frozenset(operations), frozenset(allowed_invocation_refs),
            frozenset(work_order_refs), unrestricted, actor_ref,
        )
    return result


def replace_token_config(path: str | Path, principals: list[Principal], *, require_absent: bool = False) -> None:
    """Atomically replace an owner-only principal file without exposing tokens."""
    config_path = Path(path)
    if require_absent and config_path.exists():
        raise WriterServerError("token config already exists")
    for principal in principals:
        _validate_actor_ref(principal.resolved_actor_ref)
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    value = json.dumps({"schema_version": PROTOCOL_VERSION, "principals": [
        {
            "principal_id": p.principal_id,
            "token": p.token,
            "operations": sorted(p.operations),
            "allowed_invocation_refs": sorted(p.allowed_invocation_refs),
            "work_order_refs": sorted(p.work_order_refs),
            "unrestricted": p.is_unrestricted,
            "actor_ref": p.actor_ref,
        } for p in principals
    ]}, sort_keys=True, separators=(",", ":")) + "\n"
    temporary = config_path.with_name(f".{config_path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, config_path)
        os.chmod(config_path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def write_token_config(path: str | Path, principals: list[Principal]) -> None:
    """Create a token config with owner-only permissions (test/bootstrap use)."""
    replace_token_config(path, principals, require_absent=True)


class WriterServer:
    """Serve a capability-scoped, local-only Dalton write API."""

    def __init__(self, db_path: str | Path, socket_path: str | Path, principals: Mapping[str, Principal], *, token_config_path: str | Path | None = None):
        if not principals:
            raise WriterServerError("at least one principal is required")
        self.db_path = str(db_path)  # retained only by the owner process
        self.socket_path = str(socket_path)
        if len(os.fsencode(self.socket_path)) >= 104:
            raise WriterServerError("socket path is too long")
        self.principals = dict(principals)
        self._by_token = {p.token: p for p in self.principals.values()}
        if len(self._by_token) != len(self.principals):
            raise WriterServerError("principal tokens must be unique")
        self._store: DaltonStore | None = None
        self._registry: CapabilityRegistry | None = None
        self._agenda: AgendaStore | None = None
        self._observability: ObservabilityStore | None = None
        self._token_config_path = None if token_config_path is None else Path(token_config_path)
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._connection_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._connection_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._store_executor: concurrent.futures.ThreadPoolExecutor | None = None

    @property
    def store(self) -> DaltonStore:
        if self._store is None:
            raise WriterServerError("writer server is not started")
        return self._store

    @property
    def registry(self) -> CapabilityRegistry:
        if self._registry is None:
            raise WriterServerError("writer server is not started")
        return self._registry

    @property
    def agenda(self) -> AgendaStore:
        if self._agenda is None:
            self._agenda = AgendaStore(self.store)
        return self._agenda

    @property
    def observability(self) -> ObservabilityStore:
        if self._observability is None:
            self._observability = ObservabilityStore(self.store)
        return self._observability

    def start(self) -> None:
        if self._listener is not None:
            raise WriterServerError("writer server is already started")
        socket_path = Path(self.socket_path)
        socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            existing = socket_path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISSOCK(existing.st_mode):
                raise WriterServerError("socket path is not a socket")
            socket_path.unlink()
        self._store_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="dalton-store")
        self._connection_executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONNECTIONS, thread_name_prefix="dalton-rpc")
        try:
            self._store_executor.submit(self._open_store).result(timeout=STORE_REQUEST_TIMEOUT)
        except BaseException:
            self._connection_executor.shutdown(wait=True, cancel_futures=True)
            self._connection_executor = None
            self._store_executor.shutdown(wait=True, cancel_futures=True)
            self._store_executor = None
            raise
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(self.socket_path)
            os.chmod(self.socket_path, 0o600)
            listener.listen(MAX_CONNECTIONS)
            listener.settimeout(0.25)
        except BaseException:
            listener.close()
            self._close_executors()
            raise
        self._listener = listener

    def _open_store(self) -> None:
        self._store = DaltonStore(self.db_path)
        self._registry = CapabilityRegistry(self._store)
        self._observability = ObservabilityStore(self._store)
        self._agenda = AgendaStore(self._store)

    def serve_forever(self) -> None:
        if self._listener is None:
            self.start()
        assert self._listener is not None
        listener = self._listener
        while not self._stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                raise
            conn.settimeout(CONNECTION_IDLE_TIMEOUT)
            if not self._connection_slots.acquire(blocking=False):
                conn.close()
                continue
            executor = self._connection_executor
            if executor is None:
                conn.close()
                self._connection_slots.release()
                break
            try:
                executor.submit(self._serve_connection_guarded, conn)
            except RuntimeError:
                conn.close()
                self._connection_slots.release()
                if not self._stop.is_set():
                    raise

    def _serve_connection_guarded(self, conn: socket.socket) -> None:
        try:
            with conn:
                self._serve_connection(conn)
        finally:
            self._connection_slots.release()

    def stop(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        self._close_executors()
        try:
            path = Path(self.socket_path)
            if path.exists() and stat.S_ISSOCK(path.stat().st_mode):
                path.unlink()
        except OSError:
            pass

    def _close_executors(self) -> None:
        connection_executor, self._connection_executor = self._connection_executor, None
        if connection_executor is not None:
            connection_executor.shutdown(wait=True, cancel_futures=True)
        store_executor, self._store_executor = self._store_executor, None
        if store_executor is not None:
            try:
                store_executor.submit(self._close_store).result(timeout=STORE_REQUEST_TIMEOUT)
            except (RuntimeError, concurrent.futures.TimeoutError):
                pass
            store_executor.shutdown(wait=True, cancel_futures=True)

    def _close_store(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None
        self._registry = None
        self._agenda = None
        self._observability = None

    def _serve_connection(self, conn: socket.socket) -> None:
        reader = conn.makefile("rb")
        try:
            while not self._stop.is_set():
                line = reader.readline(MAX_FRAME_BYTES + 1)
                if not line:
                    break
                request_id = "unknown"
                try:
                    raw = decode_frame(line)
                    candidate = raw.get("request_id")
                    if isinstance(candidate, str) and candidate:
                        request_id = candidate
                    request = parse_request(raw)
                    executor = self._store_executor
                    if executor is None:
                        raise WriterServerError("writer server is stopping")
                    result = executor.submit(self._handle, request).result(timeout=STORE_REQUEST_TIMEOUT)
                    conn.sendall(success_frame(request.request_id, result))
                except ProtocolError:
                    conn.sendall(error_frame(request_id, "protocol_error", "malformed request"))
                except PermissionError:
                    conn.sendall(error_frame(request_id, "forbidden", "operation is not permitted"))
                except Exception as exc:  # all exceptions are intentionally sanitized
                    conn.sendall(error_frame(request_id, self._error_code(exc), self._error_message(exc)))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            reader.close()

    def _principal(self, token: str) -> Principal:
        if self._token_config_path is not None:
            refreshed = load_principals(self._token_config_path)
            self.principals = refreshed
            self._by_token = {principal.token: principal for principal in refreshed.values()}
        for known, principal in self._by_token.items():
            if hmac.compare_digest(known, token):
                return principal
        raise PermissionError("invalid token")

    def _handle(self, request: Any) -> Any:
        principal = self._principal(request.auth_token)
        operation = request.operation
        if operation not in OPERATION_FIELDS:
            raise PermissionError("unknown operation")
        if operation not in principal.operations:
            raise PermissionError("operation is not permitted")
        unknown = set(request.params) - OPERATION_FIELDS[operation]
        if unknown:
            raise ProtocolError("unknown operation parameter")
        params = self._authorized_params(principal, operation, request.params)
        method = getattr(self, f"_op_{operation}")
        return method(params)

    def _authorized_params(self, principal: Principal, operation: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Bind actor and invocation provenance to the authenticated principal."""
        result = dict(params)
        actor_field = OPERATION_ACTOR_FIELDS.get(operation)
        if actor_field is not None:
            actor = principal.resolved_actor_ref
            supplied_actor = result.get(actor_field)
            if supplied_actor is not None and supplied_actor != actor:
                raise PermissionError("request actor does not match principal")
            result[actor_field] = actor
        is_scoped_feedback = (
            principal.principal_id in SCOPED_FEEDBACK_PRINCIPALS
            and principal.operations == FEEDBACK_BRIDGE_OPERATIONS
        )
        if operation in HUMAN_GOVERNANCE_OPERATIONS and _HUMAN_ACTOR_RE.fullmatch(
            principal.resolved_actor_ref
        ) is None:
            if operation != "record_agenda_feedback" or not is_scoped_feedback:
                raise PermissionError("governance changes require an authenticated human principal")
        if operation == "record_agenda_feedback" and is_scoped_feedback:
            subject_ref = result.get("subject_ref")
            source_event_ref = result.get("source_event_ref")
            if principal.principal_id == "feedback-bridge":
                valid = (
                    isinstance(subject_ref, str) and subject_ref.startswith("human:discord-")
                    and result.get("source") == "openclaw_discord_reaction"
                    and isinstance(source_event_ref, str)
                    and source_event_ref.startswith("discord-reaction:")
                )
            elif principal.principal_id == "dashboard-control":
                valid = (
                    isinstance(subject_ref, str) and subject_ref.startswith("human:tailscale-")
                    and result.get("source") == "tailscale_dashboard"
                    and isinstance(source_event_ref, str)
                    and source_event_ref.startswith("dashboard-feedback:")
                )
            else:
                valid = (
                    subject_ref == "automation:timeout"
                    and result.get("source") == "auto_accept_timeout"
                    and result.get("verdict") == "agree"
                    and isinstance(source_event_ref, str)
                    and source_event_ref.startswith("agenda-timeout:")
                )
            if not valid:
                raise PermissionError("scoped feedback provenance is invalid")
        if operation == "commit_reviewed_candidate" and not principal.is_unrestricted:
            if (
                principal.principal_id not in SCOPED_REVIEW_PRINCIPALS
                or principal.operations != RESEARCH_REVIEW_CONTROL_OPERATIONS
            ):
                raise PermissionError("review promotion requires the scoped review control principal")
            decision = result.get("decision")
            if not isinstance(decision, Mapping):
                raise PermissionError("review decision is required")
            reviewer_ref = decision.get("reviewer_ref")
            source_event_ref = decision.get("source_event_ref")
            if (
                not isinstance(reviewer_ref, str)
                or _HUMAN_ACTOR_RE.fullmatch(reviewer_ref) is None
                or not reviewer_ref.startswith("human:tailscale-")
                or decision.get("authorization") != "explicit_human_review"
                or decision.get("source") != "tailscale_review"
                or not isinstance(source_event_ref, str)
                or not source_event_ref.startswith("research-review:")
                or decision.get("verdict") != "accept"
            ):
                raise PermissionError("review promotion provenance is invalid")
        if operation in {"stage_change", "verify_change"}:
            self._authorize_invocation_subject(principal, operation, result)
        elif operation == "register_claim":
            refs = result.pop("producer_invocation_refs", None)
            claim = dict(result.get("claim") or {})
            embedded = claim.get("producer_invocation_refs")
            if refs is None:
                refs = embedded
            elif embedded is not None and list(embedded) != list(refs):
                raise PermissionError("claim producer provenance is inconsistent")
            if not isinstance(refs, list) or not refs:
                raise PermissionError("claim producer invocation references are required")
            self._authorize_invocation_refs(principal, refs)
            claim["producer_invocation_refs"] = list(refs)
            if "actor_ref" in claim and claim["actor_ref"] != principal.resolved_actor_ref:
                raise PermissionError("claim actor does not match principal")
            claim["actor_ref"] = principal.resolved_actor_ref
            top_claim_ref = result.get("claim_ref") or result.get("claim_id")
            embedded_claim_ref = claim.get("claim_ref")
            if top_claim_ref is not None and embedded_claim_ref is not None and top_claim_ref != embedded_claim_ref:
                raise PermissionError("claim stable reference is inconsistent")
            self._authorize_version_owner(
                principal, "claim_versions", "claim_ref",
                top_claim_ref or embedded_claim_ref,
                "claim_json",
            )
            result["claim"] = claim
        elif operation == "adjudicate_claim":
            ref = result.pop("adjudicator_invocation_ref", None)
            adjudication = dict(result.get("adjudication") or {})
            embedded = adjudication.get("adjudicator_invocation_ref")
            if ref is None:
                ref = embedded
            elif embedded is not None and embedded != ref:
                raise PermissionError("adjudicator provenance is inconsistent")
            self._authorize_invocation_refs(principal, [ref])
            adjudication["adjudicator_invocation_ref"] = ref
            result["adjudication"] = adjudication
            result["actor_ref"] = principal.resolved_actor_ref
        elif operation == "submit_capability_proposal":
            ref = result.get("builder_invocation_ref")
            proposal = dict(result.get("proposal") or {})
            participants = dict(proposal.get("participants") or {})
            embedded = participants.get("builder_invocation_ref") or participants.get("builder_invocation_id") or participants.get("builder")
            if ref is None:
                ref = embedded
            elif embedded is not None and embedded != ref:
                raise PermissionError("builder provenance is inconsistent")
            self._authorize_invocation_refs(principal, [ref])
            if participants.get("actor_ref") not in {None, principal.resolved_actor_ref}:
                raise PermissionError("proposal actor does not match principal")
            participants["builder_invocation_ref"] = ref
            participants["actor_ref"] = principal.resolved_actor_ref
            proposal["participants"] = participants
            result["proposal"] = proposal
            result["builder_invocation_ref"] = ref
            result["actor_ref"] = principal.resolved_actor_ref
        elif operation == "record_capability_evaluation":
            ref = result.pop("evaluator_invocation_ref", None)
            self._authorize_invocation_refs(principal, [ref])
            result["evaluator_invocation"] = ref
            result["actor_ref"] = principal.resolved_actor_ref
        elif operation in {"decide_capability_promotion", "rollback_capability"}:
            if _HUMAN_ACTOR_RE.fullmatch(principal.resolved_actor_ref) is None:
                raise PermissionError("capability governance requires an authenticated human principal")
            result["actor_ref"] = principal.resolved_actor_ref
        elif operation in {"register_evidence", "relate_evidence"}:
            payload_name = "evidence" if operation == "register_evidence" else "relation"
            payload = dict(result.get(payload_name) or {})
            if payload.get("actor_ref") not in {None, principal.resolved_actor_ref}:
                raise PermissionError("record actor does not match principal")
            payload["actor_ref"] = principal.resolved_actor_ref
            result[payload_name] = payload
            result["actor_ref"] = principal.resolved_actor_ref
            if operation == "register_evidence":
                top_evidence_ref = result.get("evidence_ref") or result.get("evidence_id")
                embedded_evidence_ref = payload.get("evidence_ref")
                if top_evidence_ref is not None and embedded_evidence_ref is not None and top_evidence_ref != embedded_evidence_ref:
                    raise PermissionError("evidence stable reference is inconsistent")
                self._authorize_version_owner(
                    principal, "evidence_versions", "evidence_ref",
                    top_evidence_ref or embedded_evidence_ref,
                    "evidence_json",
                )
            else:
                if result.get("relation_id") is None and payload.get("id") is None:
                    identity = {
                        "evidence_version_ref": payload.get("evidence_version_ref"),
                        "claim_version_ref": payload.get("claim_version_ref"),
                        "relation": payload.get("relation"),
                    }
                    if any(not isinstance(value, str) or not value for value in identity.values()):
                        raise PermissionError("relation identity is incomplete")
                    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    result["relation_id"] = f"relation:{hashlib.sha256(encoded).hexdigest()}"
                if result.get("idempotency_key") is None:
                    relation_ref = result.get("relation_id") or payload.get("id")
                    result["idempotency_key"] = f"relation-write:{relation_ref}"
                self._authorize_relation_target(principal, payload)
        return result

    def _authorize_invocation_subject(self, principal: Principal, operation: str, params: Mapping[str, Any]) -> None:
        """Bind worker/verifier requests to core-created immutable subjects.

        ``DaltonStore`` deliberately accepts a complete invocation mapping for
        its trusted in-process API.  The RPC boundary must be stricter: a
        runtime cannot use that convenience to self-assert a model family or
        actor and then ask the commit gate to call it independent.
        """
        if principal.is_unrestricted:
            return
        if operation == "stage_change":
            ref_key = "producer_invocation_id"
            inline_key = "producer_invocation"
        else:
            ref_key = "verifier_invocation_id"
            inline_key = "verifier_invocation"
        ref = params.get(ref_key)
        if not isinstance(ref, str) or not ref:
            raise PermissionError("a core-registered invocation reference is required")
        if inline_key in params and params.get(inline_key) is not None:
            raise PermissionError("inline invocation is not permitted")
        # A nested change/verification mapping is another common way to hide
        # an inline self-asserted invocation.  Reject it explicitly.
        nested_key = "change" if operation == "stage_change" else "verification"
        nested = params.get(nested_key)
        if isinstance(nested, Mapping) and (inline_key in nested or "invocation" in nested):
            raise PermissionError("inline invocation is not permitted")
        self._authorize_invocation_refs(principal, [ref])

    def _authorize_invocation_refs(self, principal: Principal, refs: Any) -> None:
        if principal.is_unrestricted:
            return
        if not isinstance(refs, (list, tuple)) or not refs or any(not isinstance(ref, str) or not ref for ref in refs):
            raise PermissionError("core-registered invocation references are required")
        for ref in refs:
            if ref not in principal.allowed_invocation_refs:
                raise PermissionError("invocation reference is not assigned to this principal")
            row = self.store.conn.execute(
                "SELECT work_order_ref FROM model_invocations WHERE invocation_id=?", (ref,)
            ).fetchone()
            if row is None:
                raise PermissionError("invocation reference is not registered")
            if principal.work_order_refs and row[0] not in principal.work_order_refs:
                raise PermissionError("work order is not assigned to this principal")

    def _authorize_version_owner(
        self, principal: Principal, table: str, ref_column: str, stable_ref: Any, json_column: str,
    ) -> None:
        """Prevent a scoped runtime from appending to another actor's stable ID."""
        if principal.is_unrestricted:
            return
        if not isinstance(stable_ref, str) or not stable_ref:
            raise PermissionError("stable record reference is required")
        row = self.store.conn.execute(
            f"SELECT {json_column} FROM {table} WHERE {ref_column}=? ORDER BY version_number DESC LIMIT 1",
            (stable_ref,),
        ).fetchone()
        if row is None:
            return
        try:
            actor_ref = json.loads(row[0]).get("actor_ref")
        except (TypeError, json.JSONDecodeError):
            raise PermissionError("existing record provenance is invalid") from None
        if actor_ref != principal.resolved_actor_ref:
            raise PermissionError("stable record belongs to another actor")

    def _authorize_relation_target(self, principal: Principal, relation: Mapping[str, Any]) -> None:
        """A researcher may reuse shared evidence but may only mutate its assigned claim graph."""
        if principal.is_unrestricted:
            return
        claim_version_ref = relation.get("claim_version_ref") or relation.get("claim_version_id")
        if not isinstance(claim_version_ref, str) or not claim_version_ref:
            raise PermissionError("claim version reference is required")
        row = self.store.conn.execute(
            "SELECT claim_json FROM claim_versions WHERE claim_version_id=?", (claim_version_ref,)
        ).fetchone()
        if row is None:
            raise PermissionError("claim version is not registered")
        try:
            claim = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            raise PermissionError("claim provenance is invalid") from None
        if claim.get("actor_ref") != principal.resolved_actor_ref:
            raise PermissionError("claim belongs to another actor")
        producer_refs = claim.get("producer_invocation_refs")
        self._authorize_invocation_refs(principal, producer_refs)

    def _op_register_invocation(self, p: Mapping[str, Any]) -> Any:
        return self.store.register_invocation(p.get("invocation"))

    def _op_stage_change(self, p: Mapping[str, Any]) -> Any:
        return self.store.stage_change(**dict(p))

    def _op_verify_change(self, p: Mapping[str, Any]) -> Any:
        return self.store.verify_change(**dict(p))

    def _op_commit(self, p: Mapping[str, Any]) -> Any:
        return self.store.commit(**dict(p))

    def _op_commit_reviewed_candidate(self, p: Mapping[str, Any]) -> Any:
        return self.store.commit_reviewed_candidate(**dict(p))

    def _op_create_policy(self, p: Mapping[str, Any]) -> Any:
        return self.store.create_policy(**dict(p))

    def _op_current_pointer(self, p: Mapping[str, Any]) -> Any:
        return self.store.current_pointer(**dict(p))

    def _op_get_version(self, p: Mapping[str, Any]) -> Any:
        return self.store.get_version(**dict(p))

    def _op_list_events(self, p: Mapping[str, Any]) -> Any:
        return self.store.list_events(**dict(p))

    def _op_active_policy(self, p: Mapping[str, Any]) -> Any:
        if p:
            raise ProtocolError("active_policy takes no parameters")
        return self.store.active_policy()

    def _op_register_evidence(self, p: Mapping[str, Any]) -> Any:
        return self.store.register_evidence(**dict(p))

    def _op_register_claim(self, p: Mapping[str, Any]) -> Any:
        return self.store.register_claim(**dict(p))

    def _op_relate_evidence(self, p: Mapping[str, Any]) -> Any:
        return self.store.relate_evidence(**dict(p))

    def _op_adjudicate_claim(self, p: Mapping[str, Any]) -> Any:
        return self.store.adjudicate_claim(**dict(p))

    def _op_submit_capability_proposal(self, p: Mapping[str, Any]) -> Any:
        return self.registry.submit_proposal(**dict(p))

    def _op_record_capability_evaluation(self, p: Mapping[str, Any]) -> Any:
        return self.registry.record_evaluation(**dict(p))

    def _op_decide_capability_promotion(self, p: Mapping[str, Any]) -> Any:
        return self.registry.decide_promotion(**dict(p))

    def _op_rollback_capability(self, p: Mapping[str, Any]) -> Any:
        return self.registry.rollback(**dict(p))

    def _op_active_capability(self, p: Mapping[str, Any]) -> Any:
        return self.registry.active_pointer(**dict(p))

    def _op_get_capability_version(self, p: Mapping[str, Any]) -> Any:
        return self.registry.get_version(**dict(p))

    def _op_get_capability_evaluation(self, p: Mapping[str, Any]) -> Any:
        return self.registry.get_evaluation(**dict(p))

    def _op_get_capability_decision(self, p: Mapping[str, Any]) -> Any:
        return self.registry.get_decision(**dict(p))

    def _op_capability_pointer_history(self, p: Mapping[str, Any]) -> Any:
        return self.registry.pointer_history(**dict(p))

    def _op_create_agenda_policy(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.create_policy(**dict(p))

    def _op_active_agenda_policy(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.active_policy(**dict(p))

    def _op_agenda_budget_status(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.budget_status(**dict(p))

    def _op_create_mandate(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.create_mandate(**dict(p))

    def _op_active_mandates(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.active_mandates(**dict(p))

    def _op_create_priority_override(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.create_priority_override(**dict(p))

    def _op_active_priority_overrides(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.active_priority_overrides(**dict(p))

    def _op_set_agenda_pause(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.set_pause(**dict(p))

    def _op_agenda_control_state(self, p: Mapping[str, Any]) -> Any:
        if p:
            raise ProtocolError("agenda_control_state takes no parameters")
        return self.agenda.control_state()

    def _op_register_perception_snapshot(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        snapshot = values.pop("snapshot")
        return self.agenda.register_perception_snapshot(snapshot, **values)

    def _op_materialize_agenda_context(self, p: Mapping[str, Any]) -> Any:
        # Materialization runs inside the writer service on purpose: the
        # caller has no database path, no raw spool, and no way to substitute
        # a body for the mandate or the perception snapshot it names.
        return build_agenda_context(
            self.store, self.observability, **dict(p)
        )

    def _op_get_agenda_mandate_version(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.mandate_version(**dict(p))

    def _op_get_agenda_policy_version(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.policy_version(**dict(p))

    def _op_get_perception_snapshot(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.perception_snapshot(**dict(p))

    def _op_start_agenda_cycle(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.start_cycle(**dict(p))

    def _op_add_agenda_candidates(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.add_candidates(**dict(p))

    def _op_decide_agenda_cycle(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.decide_cycle(**dict(p))

    def _op_fail_agenda_cycle(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.fail_cycle(**dict(p))

    def _op_agenda_cycle(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.cycle(**dict(p))

    def _op_agenda_cycle_by_key(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.cycle_by_key(**dict(p))

    def _op_pending_agenda_outbox(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.pending_outbox(**dict(p))

    def _op_claim_agenda_outbox(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.claim_outbox(**dict(p))

    def _op_record_agenda_delivery(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.record_delivery(**dict(p))

    def _op_list_agenda_feedback_targets(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.feedback_targets(**dict(p))

    def _op_record_agenda_feedback(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.record_feedback(**dict(p))

    def _op_create_workflow_version(self, p: Mapping[str, Any]) -> Any:
        return self.observability.create_workflow_version(**dict(p))

    def _op_link_work_order(self, p: Mapping[str, Any]) -> Any:
        return self.observability.link_work_order(**dict(p))

    def _op_record_usage(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        invocation_ref = values.pop("invocation_ref")
        return self.observability.record_usage(invocation_ref, **values)

    def _op_create_price_rate_version(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        price_rate_ref = values.pop("price_rate_ref")
        return self.observability.create_price_rate_version(price_rate_ref, **values)

    def _op_record_cost(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        usage_entry_ref = values.pop("usage_entry_ref")
        return self.observability.record_cost(usage_entry_ref, **values)

    @staticmethod
    def _error_code(exc: Exception) -> str:
        if isinstance(exc, PermissionError):
            return "forbidden"
        if isinstance(exc, ProtocolError):
            return "protocol_error"
        if isinstance(exc, (ValidationError, BadVerdict, VerificationRequired, IndependenceViolation, GateRejected, AgendaValidationError, ObservabilityValidationError)):
            return "rejected"
        if isinstance(exc, (NotFound, AgendaNotFound, ObservabilityNotFound)):
            return "not_found"
        if isinstance(exc, (IdempotencyConflict, InvocationConflict, AgendaConflict, ObservabilityConflict, ContextMaterializerConflict)):
            return "conflict"
        if isinstance(exc, (ContextMaterializerUnsupported, ContextMaterializerError, PerceptionError)):
            return "rejected"
        if isinstance(exc, (CapabilityConflict,)):
            return "conflict"
        if isinstance(exc, (CapabilityNotFound,)):
            return "not_found"
        if isinstance(exc, (EvaluationRejected, PromotionRejected, PermissionEscalation)):
            return "rejected"
        if isinstance(exc, CapabilityRegistryError):
            return "store_error"
        if isinstance(exc, (DaltonStoreError, AgendaError, ObservabilityError)):
            return "store_error"
        return "internal_error"

    @staticmethod
    def _error_message(exc: Exception) -> str:
        if isinstance(exc, PermissionError):
            return "operation is not permitted"
        if isinstance(exc, ProtocolError):
            return "malformed request"
        if isinstance(exc, (ValidationError, BadVerdict, VerificationRequired, IndependenceViolation, GateRejected, AgendaValidationError, ObservabilityValidationError)):
            return "request rejected by contract or gate"
        if isinstance(exc, (NotFound, AgendaNotFound, ObservabilityNotFound)):
            return "requested object was not found"
        if isinstance(exc, (IdempotencyConflict, InvocationConflict, AgendaConflict, ObservabilityConflict, ContextMaterializerConflict)):
            return "request conflicts with existing immutable data"
        if isinstance(exc, (ContextMaterializerError, PerceptionError)):
            return "request rejected by contract or gate"
        if isinstance(exc, CapabilityConflict):
            return "request conflicts with existing immutable capability data"
        if isinstance(exc, CapabilityNotFound):
            return "requested capability object was not found"
        if isinstance(exc, (EvaluationRejected, PromotionRejected, PermissionEscalation)):
            return "request rejected by capability governance"
        return "writer service failed to complete the request"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dalton Core owner-only writer service")
    parser.add_argument("--db", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--token-config", required=True)
    args = parser.parse_args(argv)
    try:
        principals = load_principals(args.token_config)
        server = WriterServer(
            args.db, args.socket, principals, token_config_path=args.token_config
        )
        server.start()
        def stop(_signum: int, _frame: Any) -> None:
            server.stop()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        server.serve_forever()
        server.stop()
        return 0
    except WriterServerError:
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess tests
    raise SystemExit(main())
