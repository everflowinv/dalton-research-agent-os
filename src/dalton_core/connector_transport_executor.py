"""Recorded Connector transport execution and crash recovery thin slice."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .connector import source_envelope_content_hash
from .connector_runner import (
    ConnectorRunnerAdmissionGate,
    RunnerConflict,
    ValidatedRunnerAdmission,
    validate_connector_runner_request,
    validate_connector_runner_response,
)
from .contracts import ResultEnvelope
from .contracts import ExecutionInvocation, WorkOrder
from .raw_spool import (
    RawObject,
    RawSpool,
    RawSpoolCapacityError,
    RawSpoolLimitExceeded,
)
from .runner_journal import RunnerJournal, RunnerJournalConflict, RunnerJournalNotFound
from .store import content_hash


class ConnectorTransportError(Exception):
    pass


class RunnerRecoveryRequired(ConnectorTransportError):
    pass


class SimulatedRunnerCrash(BaseException):
    """Fault-injection sentinel that bypasses normal adapter error handling."""


class _AdapterDeadlineExceeded(Exception):
    pass


AdapterDeadlineExceeded = _AdapterDeadlineExceeded


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _wire_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ConnectorTransportError("runner clock must return aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ConnectorTransportError("stored runner time lacks timezone")
    return parsed.astimezone(timezone.utc)


def _derived_id(prefix: str, idempotency_key: str) -> str:
    return f"{prefix}:{content_hash({'kind': prefix, 'idempotency_key': idempotency_key})}"


def invoke_adapter_with_deadline(
    adapter: Callable[..., Any],
    request: Mapping[str, Any],
    raw_sink: Any,
    *,
    clock: Callable[[], datetime],
) -> Any:
    """Invoke an offline adapter inside the runner-owned wall-clock boundary."""

    remaining = (
        _parse_time(request["deadline_at"])
        - _parse_time(_wire_time(clock()))
    ).total_seconds()
    if remaining <= 0:
        raise _AdapterDeadlineExceeded
    if threading.current_thread() is not threading.main_thread():
        raise ConnectorTransportError(
            "recorded transport watchdog requires the runner main thread"
        )
    prior_handler = signal.getsignal(signal.SIGALRM)
    prior_timer = signal.getitimer(signal.ITIMER_REAL)

    def deadline_handler(signum: int, frame: Any) -> None:
        del signum, frame
        raise _AdapterDeadlineExceeded

    signal.signal(signal.SIGALRM, deadline_handler)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        return adapter(request, raw_sink)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prior_handler)
        if prior_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *prior_timer)


@dataclass(frozen=True, slots=True)
class _CommitContext:
    request: Mapping[str, Any]
    work_order: WorkOrder
    profile: Mapping[str, Any]
    call_spec: Mapping[str, Any]
    invocation: Mapping[str, Any]
    execution: ExecutionInvocation
    binding_side_effects: tuple[str, ...]


class ConnectorTransportExecutor:
    """Execute operator-injected recorded adapters behind durable barriers."""

    def __init__(
        self,
        *,
        gate: ConnectorRunnerAdmissionGate,
        journal: RunnerJournal,
        spool: RawSpool,
        authority: Any,
        connector_reader: Any,
        clock: Callable[[], datetime] | None = None,
        retry_backoff_seconds: float = 1.0,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        if retry_backoff_seconds <= 0:
            raise ConnectorTransportError("retry_backoff_seconds must be positive")
        self._gate = gate
        self._journal = journal
        self._spool = spool
        self._authority = authority
        self._connector_reader = connector_reader
        self._clock = clock or _utc_now
        self._retry_backoff_seconds = float(retry_backoff_seconds)
        self._fault_hook = fault_hook

    def _barrier(self, name: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(name)

    def execute(
        self,
        request: Mapping[str, Any],
        *,
        scheduler_lease_token: str,
    ) -> dict[str, Any]:
        request_wire = validate_connector_runner_request(request)
        try:
            stored = self._journal.request(request_wire["id"])
            if stored["content_hash"] != request_wire["content_hash"]:
                raise RunnerJournalConflict(
                    "runner request id already identifies another payload"
                )
            existing = self._journal.latest(request_wire["id"])
            if existing["state"] == "responded":
                return self._duplicate_response(existing["payload"]["response"])
            if existing["state"] != "admitted":
                raise RunnerRecoveryRequired(
                    f"runner request is incomplete at durable state {existing['state']}"
                )
        except RunnerJournalNotFound:
            pass
        admission = self._gate.validate(
            request_wire, scheduler_lease_token=scheduler_lease_token
        )
        begun = self._journal.begin_request(admission.request)
        if begun["state"] != "admitted":
            raise RunnerRecoveryRequired(
                f"runner request is incomplete at durable state {begun['state']}"
            )
        self._barrier("after_admitted")

        reservation = self._gate.reserve_for_admission(
            admission,
            scheduler_lease_token=scheduler_lease_token,
            idempotency_key=self._key(admission, "reserve"),
        )
        self._barrier("after_quota_reserved")
        self._journal.append(
            admission.request["id"],
            "reserved",
            {
                "reservation_ref": reservation["id"],
                "reservation_hash": reservation["content_hash"],
                "physical_attempt_number": reservation["physical_attempt_number"],
            },
        )
        self._barrier("after_reserved")

        adapter_request = self._gate.build_adapter_request(
            admission, scheduler_lease_token=scheduler_lease_token
        )
        try:
            raw_sink = self._spool.open_sink(
                adapter_request["raw_sink_ref"],
                max_response_bytes=adapter_request["max_response_bytes"],
            )
        except RawSpoolCapacityError:
            settlement = self._authority.settle_quota(
                reservation["id"],
                "released",
                usage_entry_ref=None,
                cost_entry_ref=None,
                idempotency_key=self._recovery_key(
                    reservation["id"], "settle-released"
                ),
            )
            self._barrier("after_released_settlement")
            self._journal.append(
                admission.request["id"],
                "released_recovered",
                {
                    "reservation_ref": reservation["id"],
                    "quota_settlement_ref": settlement["id"],
                    "reason": "spool_capacity",
                },
            )
            raise
        self._barrier("after_sink_opened")

        started_at = _wire_time(self._clock())
        self._journal.append(
            admission.request["id"],
            "transport_started",
            {
                "reservation_ref": reservation["id"],
                "reservation_hash": reservation["content_hash"],
                "physical_attempt_number": reservation["physical_attempt_number"],
                "adapter_request": adapter_request,
                "adapter_request_hash": adapter_request["content_hash"],
                "started_at": started_at,
                "raw_sink_ref": adapter_request["raw_sink_ref"],
            },
            event_at=started_at,
        )
        self._barrier("after_transport_started")

        observation: dict[str, Any] | None = None
        raw_object: RawObject | None = None
        runner_error: dict[str, Any] | None = None
        try:
            credential_handle = self._gate.credential_handle_for_use(adapter_request)
            returned = self._invoke_adapter(
                admission.adapter,
                adapter_request,
                raw_sink,
                credential_handle=credential_handle,
            )
            completed_at = _wire_time(self._clock())
            if _parse_time(completed_at) >= _parse_time(adapter_request["deadline_at"]):
                raw_sink.abort()
                attempt_outcome = "timeout"
                runner_error = {
                    "code": "deadline_exceeded",
                    "message": "recorded transport completed after the authority deadline",
                    "retryable": True,
                }
            else:
                observation = self._gate.validate_observation(
                    returned, adapter_request
                )
                attempt_outcome = observation["outcome"]
                if attempt_outcome == "succeeded":
                    raw_object = raw_sink.finalize()
                else:
                    raw_sink.abort()
                if attempt_outcome == "failed":
                    runner_error = dict(observation["error"])
        except _AdapterDeadlineExceeded:
            completed_at = _wire_time(self._clock())
            raw_sink.abort()
            attempt_outcome = "timeout"
            runner_error = {
                "code": "deadline_exceeded",
                "message": "recorded transport exceeded the authority deadline",
                "retryable": True,
            }
        except RawSpoolLimitExceeded as exc:
            completed_at = _wire_time(self._clock())
            raw_sink.abort()
            attempt_outcome = "failed"
            runner_error = {
                "code": "response_too_large",
                "message": str(exc),
                "retryable": True,
            }
        except Exception as exc:
            completed_at = _wire_time(self._clock())
            raw_sink.abort()
            attempt_outcome = "failed"
            runner_error = {
                "code": "adapter_failure",
                "message": f"{type(exc).__name__}: {exc}",
                "retryable": True,
            }

        retry_at = None
        if attempt_outcome == "rate_limited":
            assert observation is not None
            retry_at = _wire_time(
                _parse_time(completed_at)
                + timedelta(milliseconds=int(observation["retry_after_ms"]))
            )
        observed_payload = {
            "reservation_ref": reservation["id"],
            "reservation_hash": reservation["content_hash"],
            "physical_attempt_number": reservation["physical_attempt_number"],
            "adapter_request_hash": adapter_request["content_hash"],
            "started_at": started_at,
            "completed_at": completed_at,
            "attempt_outcome": attempt_outcome,
            "retry_at": retry_at,
            "observation": observation,
            "raw_object": None if raw_object is None else raw_object.to_dict(),
            "error": runner_error,
            "commit_context": self._commit_context_wire(admission),
        }
        self._journal.append(
            admission.request["id"],
            "observed",
            observed_payload,
            event_at=completed_at,
        )
        self._barrier("after_observed")
        response = self._commit_observed(
            self._commit_context(admission),
            observed_payload,
            scheduler_lease_token=scheduler_lease_token,
            idempotency_status="fresh",
        )
        self._journal.append(
            admission.request["id"],
            "responded",
            {"reservation_ref": reservation["id"], "response": response},
        )
        self._barrier("after_responded")
        return response

    def recover(
        self,
        runner_request_ref: str,
        *,
        scheduler_lease_token: str | None = None,
    ) -> dict[str, Any]:
        latest = self._journal.latest(runner_request_ref)
        if latest["state"] in {
            "responded", "released_recovered", "indeterminate_recovered"
        }:
            self._spool.gc_orphans()
            return {"status": "no-op", "state": latest["state"]}
        if latest["state"] == "admitted":
            return {"status": "no-op", "state": "admitted"}
        if latest["state"] == "reserved":
            reservation_ref = latest["payload"]["reservation_ref"]
            settlement = self._authority.settle_quota(
                reservation_ref,
                "released",
                usage_entry_ref=None,
                cost_entry_ref=None,
                idempotency_key=self._recovery_key(reservation_ref, "settle-released"),
            )
            self._journal.append(
                runner_request_ref,
                "released_recovered",
                {
                    "reservation_ref": reservation_ref,
                    "quota_settlement_ref": settlement["id"],
                    "quota_settlement_hash": settlement["content_hash"],
                },
            )
            self._spool.gc_orphans()
            return {"status": "recovered", "state": "released_recovered"}
        if latest["state"] == "transport_started":
            result = self._recover_transport_started(runner_request_ref, latest)
            self._spool.gc_orphans()
            return result
        if latest["state"] == "observed":
            if scheduler_lease_token is None:
                raise RunnerRecoveryRequired(
                    "observed recovery requires the live scheduler lease token"
                )
            response = self._commit_observed(
                self._commit_context_from_wire(latest["payload"]["commit_context"]),
                latest["payload"],
                scheduler_lease_token=scheduler_lease_token,
                idempotency_status="duplicate",
            )
            self._journal.append(
                runner_request_ref,
                "responded",
                {
                    "reservation_ref": latest["payload"]["reservation_ref"],
                    "response": response,
                },
            )
            return {"status": "recovered", "state": "responded", "response": response}
        raise RunnerRecoveryRequired(f"unsupported recovery state {latest['state']}")

    def recover_orphaned_reservations(self) -> list[dict[str, Any]]:
        """Conservatively settle reservations absent from the entire journal."""

        covered = self._journal.reservation_refs()
        results = []
        for reservation in self._connector_reader.unsettled_reservations():
            if reservation["id"] in covered:
                continue
            results.append(
                self._record_indeterminate(
                    connector_invocation_ref=reservation["connector_invocation_ref"],
                    reservation=reservation,
                    started_at=reservation["created_at"],
                    completed_at=_wire_time(self._clock()),
                    key_prefix=f"runner:orphan:{reservation['id']}",
                )
            )
        self._spool.gc_orphans()
        return results

    def _recover_transport_started(
        self, runner_request_ref: str, latest: Mapping[str, Any]
    ) -> dict[str, Any]:
        payload = latest["payload"]
        reservation = self._connector_reader.get_reservation(payload["reservation_ref"])
        result = self._record_indeterminate(
            connector_invocation_ref=self._journal.request(runner_request_ref)[
                "connector_invocation_ref"
            ],
            reservation=reservation,
            started_at=payload["started_at"],
            completed_at=_wire_time(self._clock()),
            key_prefix=self._recovery_key(reservation["id"], "indeterminate"),
        )
        self._journal.append(
            runner_request_ref,
            "indeterminate_recovered",
            {"reservation_ref": reservation["id"], **result},
        )
        return {"status": "recovered", "state": "indeterminate_recovered", **result}

    def _record_indeterminate(
        self,
        *,
        connector_invocation_ref: str,
        reservation: Mapping[str, Any],
        started_at: str,
        completed_at: str,
        key_prefix: str,
    ) -> dict[str, Any]:
        attempt = self._authority.record_physical_attempt(
            connector_invocation_ref,
            reservation["id"],
            reservation["physical_attempt_number"],
            "indeterminate",
            started_at=started_at,
            completed_at=completed_at,
            idempotency_key=f"{key_prefix}:attempt",
        )
        usage = self._authority.record_usage(
            attempt["id"],
            {"calls": 1, "bytes": 0, "records": 0, "cost_micros": 0},
            measurement_status="unavailable",
            metering_source="estimated",
            idempotency_key=f"{key_prefix}:usage",
        )
        cost = self._authority.record_cost(
            usage["id"],
            cost_status="estimated",
            calculation_ref="calculator:connector-runner:0.2",
            actor_ref="system:connector-runner",
            idempotency_key=f"{key_prefix}:cost",
        )
        settlement = self._authority.settle_quota(
            reservation["id"],
            "indeterminate",
            usage_entry_ref=usage["id"],
            cost_entry_ref=cost["id"],
            idempotency_key=f"{key_prefix}:settle",
        )
        return {
            "physical_attempt_ref": attempt["id"],
            "physical_attempt_hash": attempt["content_hash"],
            "usage_entry_ref": usage["id"],
            "usage_entry_hash": usage["content_hash"],
            "cost_entry_ref": cost["id"],
            "cost_entry_hash": cost["content_hash"],
            "quota_settlement_ref": settlement["id"],
            "quota_settlement_hash": settlement["content_hash"],
        }

    def _commit_observed(
        self,
        admission: _CommitContext,
        observed: Mapping[str, Any],
        *,
        scheduler_lease_token: str,
        idempotency_status: str,
    ) -> dict[str, Any]:
        number = int(observed["physical_attempt_number"])
        key_prefix = f"runner:{admission.invocation['id']}:{number}"
        observation = observed["observation"]
        provider_request_id = (
            None if observation is None else observation["provider_request_id"]
        )
        attempt = self._authority.record_physical_attempt(
            admission.invocation["id"],
            observed["reservation_ref"],
            number,
            observed["attempt_outcome"],
            started_at=observed["started_at"],
            completed_at=observed["completed_at"],
            provider_request_id=provider_request_id,
            retry_at=observed["retry_at"],
            idempotency_key=f"{key_prefix}:attempt",
        )
        self._barrier("after_attempt_recorded")
        successful = observed["attempt_outcome"] == "succeeded"
        final_usage = observed["attempt_outcome"] in {"succeeded", "rate_limited"}
        raw = observed["raw_object"]
        records = [] if observation is None else observation["source_record_refs"]
        usage = self._authority.record_usage(
            attempt["id"],
            {
                "calls": 1,
                "bytes": int(raw["size_bytes"]) if successful else 0,
                "records": len(records) if successful else 0,
                "cost_micros": 0,
            },
            measurement_status="final" if final_usage else "unavailable",
            metering_source="runner_measured" if final_usage else "estimated",
            provider_usage_ref=(
                None
                if observation is None or observation["provider_usage"] is None
                else "provider-usage:" + content_hash(observation["provider_usage"])
            ),
            idempotency_key=f"{key_prefix}:usage",
        )
        self._barrier("after_usage_recorded")
        cost = self._authority.record_cost(
            usage["id"],
            cost_status="actual" if final_usage else "estimated",
            calculation_ref="calculator:connector-runner:0.2",
            actor_ref="system:connector-runner",
            idempotency_key=f"{key_prefix}:cost",
        )
        self._barrier("after_cost_recorded")
        settlement = self._authority.settle_quota(
            observed["reservation_ref"],
            "consumed" if final_usage else "indeterminate",
            usage_entry_ref=usage["id"],
            cost_entry_ref=cost["id"],
            idempotency_key=f"{key_prefix}:settle",
        )
        self._barrier("after_settlement_recorded")

        artifact = None
        source = None
        artifact_ref = None
        artifact_version_id = None
        source_spec = None
        if successful:
            if raw is None or not self._spool.object_exists(raw["content_hash"]):
                raise ConnectorTransportError(
                    "successful observation has no finalized raw object"
                )
            if len(admission.execution.output_refs) != 1:
                raise ConnectorTransportError(
                    "recorded connector execution requires one raw artifact output"
                )
            artifact_ref = admission.execution.output_refs[0]
            artifact_version_id = _derived_id(
                "artifact-version", f"{key_prefix}:artifact"
            )
            source_id = _derived_id("source-envelope", f"{key_prefix}:source")
            status = (
                observation.get("source_status")
                if observation is not None
                else None
            ) or ("complete" if records else "empty")
            completeness = (
                observation.get("completeness")
                if observation is not None
                else None
            ) or admission.profile["completeness"][
                admission.call_spec["operation"]
            ]
            source_spec = {
                "schema_version": "0.1",
                "id": source_id,
                "created_at": observed["completed_at"],
                "connector_invocation_ref": admission.invocation["id"],
                "connector_profile_ref": admission.profile["id"],
                "physical_attempt_refs": [attempt["id"]],
                "result_physical_attempt_ref": attempt["id"],
                "source": admission.profile["source_identity"]["source_ref"],
                "operation": admission.call_spec["operation"],
                "source_record_refs": records,
                "published_at": None,
                "updated_at": None,
                "as_of": None,
                "retrieved_at": observed["completed_at"],
                "cursor": observation["cursor"],
                "provider_request_id": provider_request_id,
                "raw_artifact_version_ref": artifact_version_id,
                "raw_response_hash": raw["content_hash"],
                "source_schema_hash": admission.profile["output_schema_hashes"][
                    admission.call_spec["operation"]
                ],
                "source_content_hash": "",
                "completeness": completeness,
                "status": status,
                "access_policy_ref": admission.profile["access_policy_ref"],
                "retention_policy_ref": admission.profile["retention_policy_ref"],
                "terms_policy_ref": admission.profile["terms_policy_ref"],
                "error": None,
            }
            source_spec["source_content_hash"] = source_envelope_content_hash(source_spec)
            source_hash = content_hash(source_spec)
        else:
            source_id = None
            source_hash = None

        scheduler_outcome = "succeeded" if successful else "retryable"
        scheduler_retry_at = None
        if scheduler_outcome == "retryable":
            scheduler_retry_at = observed["retry_at"] or _wire_time(
                _parse_time(observed["completed_at"])
                + timedelta(seconds=self._retry_backoff_seconds)
            )
        result_id = _derived_id("result-envelope", f"{key_prefix}:result")
        result = ResultEnvelope(
            schema_version="0.1",
            id=result_id,
            created_at=observed["completed_at"],
            work_order_ref=admission.work_order.id,
            invocation_ref=admission.execution.id,
            status=scheduler_outcome,
            outputs={
                "connector_invocation_ref": admission.invocation["id"],
                "physical_attempt_ref": attempt["id"],
                "quota_settlement_ref": settlement["id"],
                "source_envelope_ref": source_id,
            },
            # Admission has revalidated the immutable binding at use time;
            # report the adapter's authorized effects, not the WorkOrder's
            # potentially broader declaration.
            actual_side_effects=admission.binding_side_effects,
            usage_refs=(usage["id"],),
            artifact_refs=() if artifact_ref is None else (artifact_ref,),
            error=None if successful else dict(observed["error"] or {
                "code": observed["attempt_outcome"],
                "message": "connector physical attempt did not succeed",
                "retryable": True,
            }),
            metadata={
                "runner_request_ref": admission.request["id"],
                "physical_attempt_outcome": observed["attempt_outcome"],
            },
        )
        result_wire = result.to_dict()
        result_hash = content_hash(result_wire)

        if successful:
            assert artifact_ref is not None and artifact_version_id is not None
            assert source_spec is not None and raw is not None
            artifact = self._authority.register_artifact_version_v2(
                artifact_ref,
                title="Recorded connector raw response",
                kind="connector_raw_response",
                media_type="application/json",
                artifact_content_hash=raw["content_hash"],
                size_bytes=int(raw["size_bytes"]),
                storage_locator=raw["storage_locator"],
                producer_execution_ref=admission.execution.id,
                result_envelope_ref=result.id,
                result_envelope_hash=result_hash,
                access_class="internal",
                preview_status="unavailable",
                actor_ref="system:connector-runner",
                version_id=artifact_version_id,
                idempotency_key=f"{key_prefix}:artifact",
            )
            self._barrier("after_artifact_recorded")
            source = self._authority.record_source_envelope(
                source_spec, idempotency_key=f"{key_prefix}:source"
            )
            self._barrier("after_source_recorded")
            if source["content_hash"] != source_hash:
                raise ConnectorTransportError("SourceEnvelope precomputed hash drifted")

        self._authority.complete(
            admission.work_order.id,
            admission.request["scheduler_attempt_number"],
            admission.request["runner_actor_ref"],
            scheduler_lease_token,
            result,
            idempotency_key=f"{key_prefix}:scheduler-complete",
            result_envelope_hash=result_hash,
            retry_at=scheduler_retry_at,
        )
        self._barrier("after_scheduler_completed")
        response_base = {
            "schema_version": "0.2",
            "id": _derived_id("connector-runner-response", f"{key_prefix}:response"),
            "created_at": observed["completed_at"],
            "runner_request_ref": admission.request["id"],
            "runner_request_hash": admission.request["content_hash"],
            "idempotency_status": idempotency_status,
            "connector_invocation_ref": admission.invocation["id"],
            "connector_invocation_hash": admission.invocation["content_hash"],
            "physical_attempt_ref": attempt["id"],
            "physical_attempt_hash": attempt["content_hash"],
            "usage_entry_ref": usage["id"],
            "usage_entry_hash": usage["content_hash"],
            "cost_entry_ref": cost["id"],
            "cost_entry_hash": cost["content_hash"],
            "quota_settlement_ref": settlement["id"],
            "quota_settlement_hash": settlement["content_hash"],
            "raw_artifact_version_ref": None if artifact is None else artifact["id"],
            "raw_artifact_version_hash": (
                None if artifact is None else artifact["content_hash"]
            ),
            "source_envelope_ref": None if source is None else source["id"],
            "source_envelope_hash": None if source is None else source["content_hash"],
            "result_envelope_ref": result.id,
            "result_envelope_hash": result_hash,
            "outcome": scheduler_outcome,
            "retry_at": scheduler_retry_at,
        }
        response_base["content_hash"] = content_hash(response_base)
        return validate_connector_runner_response(response_base)

    def _invoke_adapter(
        self,
        adapter: Callable[..., Any],
        request: Mapping[str, Any],
        raw_sink: Any,
        *,
        credential_handle: Any | None = None,
    ) -> Any:
        """Bound synchronous recorded adapters with a runner-owned watchdog."""
        if credential_handle is not None:
            return invoke_adapter_with_deadline(
                lambda request_wire, sink: adapter(
                    request_wire, sink, credential_handle
                ),
                request,
                raw_sink,
                clock=self._clock,
            )
        return invoke_adapter_with_deadline(
            adapter, request, raw_sink, clock=self._clock
        )

    @staticmethod
    def _duplicate_response(response: Mapping[str, Any]) -> dict[str, Any]:
        duplicate = dict(response)
        duplicate["idempotency_status"] = "duplicate"
        duplicate.pop("content_hash", None)
        duplicate["content_hash"] = content_hash(duplicate)
        return validate_connector_runner_response(duplicate)

    @staticmethod
    def _key(admission: ValidatedRunnerAdmission, step: str) -> str:
        return f"runner:{admission.invocation['id']}:{admission.request['scheduler_attempt_number']}:{step}"

    @staticmethod
    def _recovery_key(reservation_ref: str, step: str) -> str:
        return f"runner:recovery:{reservation_ref}:{step}"

    @staticmethod
    def _commit_context(admission: ValidatedRunnerAdmission) -> _CommitContext:
        return _CommitContext(
            request=dict(admission.request),
            work_order=admission.work_order,
            profile=dict(admission.profile),
            call_spec=dict(admission.call_spec),
            invocation=dict(admission.invocation),
            execution=admission.execution,
            binding_side_effects=tuple(admission.binding["side_effects"]),
        )

    @staticmethod
    def _commit_context_wire(admission: ValidatedRunnerAdmission) -> dict[str, Any]:
        return {
            "request": dict(admission.request),
            "work_order": admission.work_order.to_dict(),
            "profile": dict(admission.profile),
            "call_spec": dict(admission.call_spec),
            "invocation": dict(admission.invocation),
            "execution": admission.execution.to_dict(),
            "binding_side_effects": list(admission.binding["side_effects"]),
        }

    @staticmethod
    def _commit_context_from_wire(wire: Mapping[str, Any]) -> _CommitContext:
        expected = {
            "request", "work_order", "profile", "call_spec", "invocation",
            "execution", "binding_side_effects",
        }
        if not isinstance(wire, Mapping) or set(wire) != expected:
            raise ConnectorTransportError("journal commit context is not closed")
        return _CommitContext(
            request=dict(wire["request"]),
            work_order=WorkOrder.from_dict(wire["work_order"]),
            profile=dict(wire["profile"]),
            call_spec=dict(wire["call_spec"]),
            invocation=dict(wire["invocation"]),
            execution=ExecutionInvocation.from_dict(wire["execution"]),
            binding_side_effects=tuple(wire["binding_side_effects"]),
        )


__all__ = [
    "AdapterDeadlineExceeded", "ConnectorTransportError",
    "ConnectorTransportExecutor", "RunnerRecoveryRequired",
    "SimulatedRunnerCrash", "invoke_adapter_with_deadline",
]
