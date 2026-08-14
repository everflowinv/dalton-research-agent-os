"""Bounded multi-page recorded reference shadows for CNINFO and SEC."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .connector import source_envelope_content_hash
from .connector_inventory import load_packaged_connector_inventory
from .connector_runner import (
    ConnectorRunnerAdmissionGate,
    ValidatedRunnerAdmission,
    validate_adapter_transport_observation,
    validate_connector_adapter_request,
    validate_connector_runner_request,
    validate_connector_runner_response,
)
from .connector_transport_executor import (
    AdapterDeadlineExceeded,
    ConnectorTransportError,
    invoke_adapter_with_deadline,
)
from .contracts import ResultEnvelope
from .raw_spool import RawObject, RawSpool, RawSpoolCapacityError, RawSpoolLimitExceeded
from .runner_journal import RunnerJournal, RunnerJournalNotFound
from .recorded_source_adapter import (
    RecordedSourceFixtureAdapter,
    validate_recorded_source_fixture,
)
from .store import canonical_json, content_hash


class RecordedReferenceShadowError(ConnectorTransportError):
    pass


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RecordedReferenceShadowError(f"{name} must be an object")
    unknown = set(value) - fields
    missing = fields - set(value)
    if unknown or missing:
        raise RecordedReferenceShadowError(
            f"{name} has invalid closed shape; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise RecordedReferenceShadowError(f"{name} must be finite JSON") from exc


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecordedReferenceShadowError(f"{name} must be a non-empty string")
    return value.strip()


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise RecordedReferenceShadowError("timestamp must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise RecordedReferenceShadowError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _wire_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RecordedReferenceShadowError("runner clock must return aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _derived_id(prefix: str, key: str) -> str:
    return f"{prefix}:{content_hash({'kind': prefix, 'idempotency_key': key})}"


def validate_recorded_reference_shadow_plan(spec: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "schema_version", "id", "created_at", "connector_profile_ref",
        "connector_profile_hash", "inventory_template_ref",
        "inventory_template_hash", "recorded_fixture_ref",
        "recorded_fixture_hash", "source_ref", "operation",
        "pagination_mode", "cursor_field", "max_pages",
        "bounded_window_fields", "completeness_target", "content_hash",
    }
    wire = _closed(spec, fields, "RecordedReferenceShadowPlan")
    if wire["schema_version"] != "0.1":
        raise RecordedReferenceShadowError("unsupported reference shadow plan version")
    for name in (
        "id", "created_at", "connector_profile_ref", "inventory_template_ref",
        "recorded_fixture_ref", "source_ref", "operation", "pagination_mode",
        "cursor_field", "completeness_target",
    ):
        wire[name] = _text(wire[name], name)
    for name in (
        "connector_profile_hash", "inventory_template_hash", "recorded_fixture_hash"
    ):
        if len(wire[name]) != 64 or any(
            char not in "0123456789abcdef" for char in wire[name]
        ):
            raise RecordedReferenceShadowError(f"{name} must be SHA-256")
    expected = {
        "source:cninfo": (
            "cninfo", "list_announcements", "page", "page"
        ),
        "source:sec-edgar": (
            "sec", "list_filings", "cursor", "cursor"
        ),
    }.get(wire["source_ref"])
    if expected is None:
        raise RecordedReferenceShadowError("reference source plan identity is invalid")
    slug, operation, pagination_mode, cursor_field = expected
    if (
        wire["inventory_template_ref"]
        != f"connector-profile-template:{slug}:0.1"
        or wire["recorded_fixture_ref"]
        != f"recorded-source-fixture:{slug}:0.1"
        or (wire["operation"], wire["pagination_mode"], wire["cursor_field"])
        != (operation, pagination_mode, cursor_field)
    ):
        raise RecordedReferenceShadowError("reference source plan graph is invalid")
    if wire["completeness_target"] != "enumerated":
        raise RecordedReferenceShadowError("reference shadow target must be enumerated")
    if isinstance(wire["max_pages"], bool) or not isinstance(wire["max_pages"], int) or not 1 <= wire["max_pages"] <= 20:
        raise RecordedReferenceShadowError("max_pages must be between 1 and 20")
    if wire["bounded_window_fields"] != ["date_from", "date_to"]:
        raise RecordedReferenceShadowError(
            "reference shadow requires the exact bounded filing window"
        )
    declared = wire.pop("content_hash")
    if declared != content_hash(wire):
        raise RecordedReferenceShadowError("reference shadow plan content_hash mismatch")
    wire["content_hash"] = declared
    return wire


class RecordedReferenceShadowCoordinator:
    """Record every simulated page as an independent physical attempt."""

    def __init__(
        self,
        *,
        plan: Mapping[str, Any],
        gate: ConnectorRunnerAdmissionGate,
        journal: RunnerJournal,
        spool: RawSpool,
        authority: Any,
        connector_reader: Any,
        clock: Callable[[], datetime] | None = None,
        retry_backoff_seconds: float = 1.0,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.plan = validate_recorded_reference_shadow_plan(plan)
        self.gate = gate
        self.journal = journal
        self.spool = spool
        self.authority = authority
        self.connector_reader = connector_reader
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fault_hook = fault_hook
        if retry_backoff_seconds <= 0:
            raise RecordedReferenceShadowError("retry backoff must be positive")
        self.retry_backoff_seconds = float(retry_backoff_seconds)

    def execute(
        self,
        request: Mapping[str, Any],
        *,
        scheduler_lease_token: str,
    ) -> dict[str, Any]:
        parent_request = validate_connector_runner_request(request)
        try:
            latest = self.journal.latest(parent_request["id"])
        except RunnerJournalNotFound:
            latest = None
        if latest is not None and latest["state"] == "responded":
            response = latest["payload"].get("response")
            result_wire = latest["payload"].get("result_envelope")
            result_hash = latest["payload"].get("result_envelope_hash")
            retry_at = latest["payload"].get("retry_at")
            if (
                not isinstance(response, Mapping)
                or not isinstance(result_wire, Mapping)
                or not isinstance(result_hash, str)
            ):
                raise RecordedReferenceShadowError(
                    "reference shadow parent journal lacks completion authority"
                )
            result = ResultEnvelope.from_dict(result_wire)
            if (
                content_hash(result.to_dict()) != result_hash
                or response.get("runner_request_ref") != parent_request["id"]
                or response.get("runner_request_hash") != parent_request["content_hash"]
                or response.get("result_envelope_ref") != result.id
                or response.get("result_envelope_hash") != result_hash
            ):
                raise RecordedReferenceShadowError(
                    "journaled result/response completion binding is invalid"
                )
            self._ensure_scheduler_completion(
                parent_request,
                result,
                result_hash,
                retry_at=retry_at,
                scheduler_lease_token=scheduler_lease_token,
            )
            return self._duplicate_response(response)

        parent_admission = self.gate.validate(
            parent_request, scheduler_lease_token=scheduler_lease_token
        )
        self._validate_plan_binding(parent_admission)
        receipts: list[dict[str, Any]] = []
        prior_cursor: str | None = None
        terminal = False
        all_fresh = True
        for ordinal in range(1, int(self.plan["max_pages"]) + 1):
            page_request = self._page_request(parent_request, ordinal)
            receipt = self._execute_page(
                page_request,
                ordinal=ordinal,
                prior_cursor=prior_cursor,
                prior_receipt=None if not receipts else receipts[-1],
                scheduler_lease_token=scheduler_lease_token,
                is_parent=ordinal == 1,
            )
            all_fresh = all_fresh and receipt.pop("page_write_status") == "fresh"
            receipts.append(receipt)
            if receipt["attempt_outcome"] != "succeeded":
                return self._finish_failure(
                    parent_admission,
                    receipts,
                    scheduler_lease_token=scheduler_lease_token,
                    idempotency_status="fresh" if all_fresh else "duplicate",
                )
            observation = receipt["observation"]
            assert observation is not None
            prior_cursor = observation["cursor"]
            if prior_cursor is None:
                terminal = True
                break
        self._validate_normalized_pages(receipts)
        return self._finish_success(
            parent_admission,
            receipts,
            terminal=terminal,
            scheduler_lease_token=scheduler_lease_token,
            idempotency_status="fresh" if all_fresh else "duplicate",
        )

    def _validate_plan_binding(self, admission: ValidatedRunnerAdmission) -> None:
        profile = admission.profile
        call = admission.call_spec
        slug = "cninfo" if self.plan["source_ref"] == "source:cninfo" else "sec"
        inventory = load_packaged_connector_inventory()
        template = inventory["templates"][slug]
        if not isinstance(admission.adapter, RecordedSourceFixtureAdapter):
            raise RecordedReferenceShadowError(
                "reference shadow requires the frozen recorded fixture adapter"
            )
        fixture = validate_recorded_source_fixture(
            getattr(admission.adapter, "fixture", {})
        )
        operation = next(
            (
                item for item in template["operations"]
                if item["operation"] == self.plan["operation"]
            ),
            None,
        )
        if (
            self.plan["inventory_template_ref"] != template["id"]
            or self.plan["inventory_template_hash"] != template["content_hash"]
            or self.plan["recorded_fixture_ref"] != fixture["id"]
            or self.plan["recorded_fixture_hash"] != fixture["content_hash"]
            or fixture["source_ref"] != self.plan["source_ref"]
            or fixture["operation"] != self.plan["operation"]
            or operation is None
            or self.plan["connector_profile_ref"] != profile["id"]
            or self.plan["connector_profile_hash"] != profile["content_hash"]
            or self.plan["source_ref"] != profile["source_identity"]["source_ref"]
            or template["source_identity"] != profile["source_identity"]
            or template["connector_ref"] != profile["connector_ref"]
            or self.plan["operation"] != call["operation"]
            or self.plan["operation"] not in profile["allowed_operations"]
            or profile["completeness"][call["operation"]] != "enumerated"
            or profile["pagination"]["mode"] != self.plan["pagination_mode"]
            or profile["pagination"]["cursor_field"] != self.plan["cursor_field"]
            or int(self.plan["max_pages"]) > int(profile["pagination"]["max_pages"])
            or profile["input_schema_refs"][call["operation"]]
            != operation["input_schema_ref"]
            or profile["input_schema_hashes"][call["operation"]]
            != operation["input_schema_hash"]
            or profile["output_schema_refs"][call["operation"]]
            != operation["output_schema_ref"]
            or profile["output_schema_hashes"][call["operation"]]
            != operation["output_schema_hash"]
            or profile["auth_mode"] != "none"
        ):
            raise RecordedReferenceShadowError(
                "reference shadow plan does not exactly bind Runner authority"
            )
        for field in self.plan["bounded_window_fields"]:
            value = call["parameters"].get(field)
            if not isinstance(value, str) or not value:
                raise RecordedReferenceShadowError(
                    "enumerated reference shadow requires a bounded filing window"
                )
        try:
            date_from = date.fromisoformat(call["parameters"]["date_from"])
            date_to = date.fromisoformat(call["parameters"]["date_to"])
        except ValueError as exc:
            raise RecordedReferenceShadowError(
                "filing window dates must use YYYY-MM-DD"
            ) from exc
        if (
            date_from.isoformat() != call["parameters"]["date_from"]
            or date_to.isoformat() != call["parameters"]["date_to"]
        ):
            raise RecordedReferenceShadowError(
                "filing window dates must be canonical"
            )
        if date_from > date_to:
            raise RecordedReferenceShadowError("filing window is reversed")

    @staticmethod
    def _page_request(parent: Mapping[str, Any], ordinal: int) -> dict[str, Any]:
        if ordinal == 1:
            return dict(parent)
        wire = {key: value for key, value in parent.items() if key != "content_hash"}
        wire["id"] = _derived_id("connector-runner-request", f"{parent['id']}:page:{ordinal}")
        wire["idempotency_key"] = f"{parent['idempotency_key']}:page:{ordinal}"
        wire["content_hash"] = content_hash(wire)
        return validate_connector_runner_request(wire)

    def _execute_page(
        self,
        request: Mapping[str, Any],
        *,
        ordinal: int,
        prior_cursor: str | None,
        prior_receipt: Mapping[str, Any] | None,
        scheduler_lease_token: str,
        is_parent: bool,
    ) -> dict[str, Any]:
        admission = self.gate.validate(
            request, scheduler_lease_token=scheduler_lease_token
        )
        try:
            latest = self.journal.latest(request["id"])
        except RunnerJournalNotFound:
            latest = self.journal.begin_request(admission.request)
        if latest["state"] == "responded":
            receipt = latest["payload"].get("page_receipt")
            if not isinstance(receipt, Mapping):
                raise RecordedReferenceShadowError("page journal lacks its receipt")
            return {**dict(receipt), "page_write_status": "duplicate"}
        if latest["state"] == "observed":
            receipt = self._commit_page(
                admission, latest["payload"], idempotency_status="duplicate"
            )
            if not is_parent:
                self.journal.append(
                    request["id"], "responded",
                    {"reservation_ref": receipt["reservation_ref"], "page_receipt": receipt},
                )
            self._validate_page_cursor(receipt, ordinal, prior_cursor)
            return {**receipt, "page_write_status": "duplicate"}
        if latest["state"] != "admitted":
            raise RecordedReferenceShadowError(
                f"reference page requires recovery from {latest['state']}"
            )

        reservation = self.gate.reserve_for_admission(
            admission,
            scheduler_lease_token=scheduler_lease_token,
            idempotency_key=f"runner-shadow:{admission.invocation['id']}:{ordinal}:reserve",
        )
        self.journal.append(
            request["id"], "reserved",
            {
                "reservation_ref": reservation["id"],
                "reservation_hash": reservation["content_hash"],
                "physical_attempt_number": reservation["physical_attempt_number"],
            },
        )
        adapter_request = self.gate.build_adapter_request(
            admission, scheduler_lease_token=scheduler_lease_token
        )
        if int(adapter_request["physical_attempt_number"]) != ordinal:
            raise RecordedReferenceShadowError(
                "recorded page ordinal does not match physical attempt number"
            )
        adapter_request = self._paginated_adapter_request(
            adapter_request,
            admission,
            ordinal=ordinal,
            prior_receipt=prior_receipt,
        )
        try:
            raw_sink = self.spool.open_sink(
                adapter_request["raw_sink_ref"],
                max_response_bytes=adapter_request["max_response_bytes"],
            )
        except RawSpoolCapacityError:
            settlement = self.authority.settle_quota(
                reservation["id"], "released", usage_entry_ref=None,
                cost_entry_ref=None,
                idempotency_key=f"runner-shadow:{admission.invocation['id']}:{ordinal}:release",
            )
            self.journal.append(
                request["id"], "released_recovered",
                {"reservation_ref": reservation["id"], "quota_settlement_ref": settlement["id"]},
            )
            raise
        started_at = _wire_time(self.clock())
        self.journal.append(
            request["id"], "transport_started",
            {
                "reservation_ref": reservation["id"],
                "reservation_hash": reservation["content_hash"],
                "physical_attempt_number": ordinal,
                "adapter_request_hash": adapter_request["content_hash"],
                "started_at": started_at,
                "raw_sink_ref": adapter_request["raw_sink_ref"],
                "prior_cursor": prior_cursor,
            },
            event_at=started_at,
        )
        observation: dict[str, Any] | None = None
        raw_object: RawObject | None = None
        error: dict[str, Any] | None = None
        try:
            returned = invoke_adapter_with_deadline(
                admission.adapter, adapter_request, raw_sink, clock=self.clock
            )
            completed_at = _wire_time(self.clock())
            if _parse_time(completed_at) >= _parse_time(adapter_request["deadline_at"]):
                raw_sink.abort()
                outcome = "timeout"
                error = {
                    "code": "deadline_exceeded",
                    "message": "recorded source page exceeded its authority deadline",
                    "retryable": True,
                }
            else:
                observation = validate_adapter_transport_observation(returned)
                if observation["request_hash"] != adapter_request["content_hash"]:
                    raise RecordedReferenceShadowError(
                        "recorded page observation does not bind AdapterRequest"
                    )
                outcome = observation["outcome"]
                if outcome == "succeeded":
                    raw_object = raw_sink.finalize()
                else:
                    raw_sink.abort()
        except AdapterDeadlineExceeded:
            completed_at = _wire_time(self.clock())
            raw_sink.abort()
            outcome = "timeout"
            error = {
                "code": "deadline_exceeded", "message": "recorded source page timed out",
                "retryable": True,
            }
        except RawSpoolLimitExceeded as exc:
            completed_at = _wire_time(self.clock())
            raw_sink.abort()
            outcome = "failed"
            error = {"code": "response_too_large", "message": str(exc), "retryable": True}
        except Exception as exc:
            completed_at = _wire_time(self.clock())
            raw_sink.abort()
            outcome = "failed"
            error = {
                "code": "adapter_failure", "message": f"{type(exc).__name__}: {exc}",
                "retryable": True,
            }
        retry_at = None
        if outcome == "rate_limited":
            assert observation is not None
            retry_at = _wire_time(
                _parse_time(completed_at)
                + timedelta(milliseconds=int(observation["retry_after_ms"]))
            )
        payload = {
            "reservation_ref": reservation["id"],
            "reservation_hash": reservation["content_hash"],
            "physical_attempt_number": ordinal,
            "adapter_request_hash": adapter_request["content_hash"],
            "started_at": started_at,
            "completed_at": completed_at,
            "attempt_outcome": outcome,
            "retry_at": retry_at,
            "observation": observation,
            "raw_object": None if raw_object is None else raw_object.to_dict(),
            "error": error,
        }
        self.journal.append(request["id"], "observed", payload, event_at=completed_at)
        receipt = self._commit_page(admission, payload, idempotency_status="fresh")
        self._validate_page_cursor(receipt, ordinal, prior_cursor)
        if not is_parent:
            self.journal.append(
                request["id"], "responded",
                {"reservation_ref": reservation["id"], "page_receipt": receipt},
            )
        return {**receipt, "page_write_status": "fresh"}

    def _paginated_adapter_request(
        self,
        base_request: Mapping[str, Any],
        admission: ValidatedRunnerAdmission,
        *,
        ordinal: int,
        prior_receipt: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Derive a page-specific 0.2 request from immutable prior authority."""

        parameters = dict(admission.call_spec["parameters"])
        cursor_field = self.plan["cursor_field"]
        request_cursor: str | None = None
        prior_adapter_request_hash = None
        prior_observation_hash = None
        prior_attempt_ref = None
        prior_attempt_hash = None
        if ordinal == 1:
            if prior_receipt is not None:
                raise RecordedReferenceShadowError("first page cannot have a prior receipt")
            if cursor_field == "page" and parameters.get(cursor_field) != 1:
                raise RecordedReferenceShadowError("page pagination must start at page one")
            if cursor_field == "cursor" and parameters.get(cursor_field) not in {None, ""}:
                raise RecordedReferenceShadowError("cursor pagination must start without a cursor")
        else:
            if prior_receipt is None:
                raise RecordedReferenceShadowError("continued page lacks prior authority")
            observation = validate_adapter_transport_observation(
                prior_receipt.get("observation")
            )
            request_cursor = observation["cursor"]
            if request_cursor is None:
                raise RecordedReferenceShadowError(
                    "terminal observation cannot authorize another page"
                )
            prior_adapter_request_hash = _text(
                prior_receipt.get("adapter_request_hash"), "adapter_request_hash"
            )
            if observation["request_hash"] != prior_adapter_request_hash:
                raise RecordedReferenceShadowError(
                    "prior observation does not bind its adapter request"
                )
            prior_observation_hash = observation["content_hash"]
            prior_attempt_ref = _text(
                prior_receipt.get("physical_attempt_ref"), "physical_attempt_ref"
            )
            prior_attempt_hash = _text(
                prior_receipt.get("physical_attempt_hash"), "physical_attempt_hash"
            )
            row = self.connector_reader.connection.execute(
                "SELECT record_json,content_hash FROM connector_physical_attempts "
                "WHERE physical_attempt_id=?",
                (prior_attempt_ref,),
            ).fetchone()
            if row is None:
                raise RecordedReferenceShadowError("prior physical attempt is missing")
            attempt = json.loads(row["record_json"])
            if (
                row["content_hash"] != prior_attempt_hash
                or attempt["connector_invocation_ref"] != admission.invocation["id"]
                or int(attempt["physical_attempt_number"]) != ordinal - 1
                or attempt["outcome"] != "succeeded"
            ):
                raise RecordedReferenceShadowError(
                    "prior physical attempt cannot authorize pagination"
                )
            if cursor_field == "page":
                try:
                    page = int(request_cursor)
                except ValueError as exc:
                    raise RecordedReferenceShadowError(
                        "recorded page cursor is not an integer"
                    ) from exc
                if str(page) != request_cursor or page != ordinal:
                    raise RecordedReferenceShadowError(
                        "recorded page cursor is not contiguous"
                    )
                parameters[cursor_field] = page
            else:
                parameters[cursor_field] = request_cursor
        self.gate.resolver.validate_input(admission.binding, parameters)
        wire = dict(base_request)
        wire.pop("content_hash", None)
        wire.update(
            {
                "protocol_version": "0.2",
                "parameters": parameters,
                "query_hash": content_hash(
                    {"operation": admission.call_spec["operation"], "parameters": parameters}
                ),
                "pagination_authority": {
                    "mode": self.plan["pagination_mode"],
                    "cursor_field": cursor_field,
                    "page_ordinal": ordinal,
                    "request_cursor": request_cursor,
                    "parent_query_hash": admission.call_spec["query_hash"],
                    "prior_adapter_request_hash": prior_adapter_request_hash,
                    "prior_observation_hash": prior_observation_hash,
                    "prior_physical_attempt_ref": prior_attempt_ref,
                    "prior_physical_attempt_hash": prior_attempt_hash,
                },
            }
        )
        wire["content_hash"] = content_hash(wire)
        return validate_connector_adapter_request(wire)

    @staticmethod
    def _validate_page_cursor(
        receipt: Mapping[str, Any], ordinal: int, prior_cursor: str | None
    ) -> None:
        observation = receipt["observation"]
        if observation is None:
            return
        structured = observation["structured_output"]
        if (
            structured.get("page_ordinal") != ordinal
            or structured.get("request_cursor") != prior_cursor
        ):
            raise RecordedReferenceShadowError(
                "recorded page cursor/ordinal chain is not exact"
            )

    def _commit_page(
        self,
        admission: ValidatedRunnerAdmission,
        observed: Mapping[str, Any],
        *,
        idempotency_status: str,
    ) -> dict[str, Any]:
        ordinal = int(observed["physical_attempt_number"])
        key = f"runner-shadow:{admission.invocation['id']}:{ordinal}"
        observation = observed["observation"]
        provider_request_id = None if observation is None else observation["provider_request_id"]
        attempt = self.authority.record_physical_attempt(
            admission.invocation["id"], observed["reservation_ref"], ordinal,
            observed["attempt_outcome"], started_at=observed["started_at"],
            completed_at=observed["completed_at"], provider_request_id=provider_request_id,
            retry_at=observed["retry_at"], idempotency_key=f"{key}:attempt",
        )
        successful = observed["attempt_outcome"] == "succeeded"
        final_usage = observed["attempt_outcome"] in {"succeeded", "rate_limited"}
        raw = observed["raw_object"]
        records = [] if observation is None else observation["source_record_refs"]
        usage = self.authority.record_usage(
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
                None if observation is None or observation["provider_usage"] is None
                else "provider-usage:" + content_hash(observation["provider_usage"])
            ),
            idempotency_key=f"{key}:usage",
        )
        cost = self.authority.record_cost(
            usage["id"], cost_status="actual" if final_usage else "estimated",
            calculation_ref="calculator:recorded-reference-shadow:0.1",
            actor_ref="system:connector-runner", idempotency_key=f"{key}:cost",
        )
        settlement = self.authority.settle_quota(
            observed["reservation_ref"],
            "consumed" if final_usage else "indeterminate",
            usage_entry_ref=usage["id"], cost_entry_ref=cost["id"],
            idempotency_key=f"{key}:settle",
        )
        return {
            "idempotency_status": idempotency_status,
            "runner_request_ref": admission.request["id"],
            "runner_request_hash": admission.request["content_hash"],
            "reservation_ref": observed["reservation_ref"],
            "physical_attempt_ref": attempt["id"],
            "physical_attempt_hash": attempt["content_hash"],
            "adapter_request_hash": observed["adapter_request_hash"],
            "usage_entry_ref": usage["id"], "usage_entry_hash": usage["content_hash"],
            "cost_entry_ref": cost["id"], "cost_entry_hash": cost["content_hash"],
            "quota_settlement_ref": settlement["id"],
            "quota_settlement_hash": settlement["content_hash"],
            "attempt_outcome": observed["attempt_outcome"],
            "retry_at": observed["retry_at"], "observation": observation,
            "raw_object": raw, "error": observed["error"],
        }

    @staticmethod
    def _validate_normalized_pages(receipts: list[Mapping[str, Any]]) -> None:
        seen: set[str] = set()
        for receipt in receipts:
            observation = receipt["observation"]
            if observation is None:
                continue
            records = observation["structured_output"]["records"]
            if not isinstance(records, list):
                raise RecordedReferenceShadowError("normalized page records must be an array")
            if [record.get("record_ref") for record in records] != observation[
                "source_record_refs"
            ]:
                raise RecordedReferenceShadowError(
                    "normalized records do not bind source_record_refs"
                )
            for record in records:
                if not isinstance(record, Mapping) or set(record) != {
                    "record_ref", "revision_of_ref", "record_hash"
                }:
                    raise RecordedReferenceShadowError("normalized record shape is open")
                record_ref = _text(record["record_ref"], "record_ref")
                if record_ref in seen:
                    raise RecordedReferenceShadowError("recorded pages contain duplicate records")
                prior = record["revision_of_ref"]
                if prior is not None and prior not in seen:
                    raise RecordedReferenceShadowError(
                        "recorded revision chain points outside earlier pages"
                    )
                seen.add(record_ref)

    def _finish_success(
        self,
        admission: ValidatedRunnerAdmission,
        receipts: list[Mapping[str, Any]],
        *,
        terminal: bool,
        scheduler_lease_token: str,
        idempotency_status: str,
    ) -> dict[str, Any]:
        last = receipts[-1]
        all_records = [
            ref
            for receipt in receipts
            for ref in receipt["observation"]["source_record_refs"]
        ]
        completeness = "enumerated" if terminal else "partial"
        status = "empty" if terminal and not all_records else (
            "complete" if terminal else "partial"
        )
        key = f"runner-shadow:{admission.invocation['id']}:aggregate"
        source_id = _derived_id("source-envelope", f"{key}:source")
        result_id = _derived_id("result-envelope", f"{key}:result")
        artifact_ref = admission.execution.output_refs[0]
        result = ResultEnvelope(
            schema_version="0.1", id=result_id, created_at=last["observation"]["structured_output"].get("retrieved_at", _wire_time(self.clock())),
            work_order_ref=admission.work_order.id, invocation_ref=admission.execution.id,
            status="succeeded",
            outputs={
                "connector_invocation_ref": admission.invocation["id"],
                "physical_attempt_refs": [item["physical_attempt_ref"] for item in receipts],
                "result_physical_attempt_ref": last["physical_attempt_ref"],
                "quota_settlement_refs": [item["quota_settlement_ref"] for item in receipts],
                "source_envelope_ref": source_id,
            },
            actual_side_effects=("read:recorded-fixture",),
            usage_refs=tuple(item["usage_entry_ref"] for item in receipts),
            artifact_refs=(artifact_ref,), error=None,
            metadata={
                "runner_request_ref": admission.request["id"],
                "physical_attempt_count": len(receipts),
                "terminal_cursor_observed": terminal,
                "completeness": completeness,
            },
        )
        result_wire = result.to_dict()
        result_hash = content_hash(result_wire)
        prior_artifact_version_ref = None
        artifacts: list[dict[str, Any]] = []
        for ordinal, receipt in enumerate(receipts, start=1):
            raw = receipt["raw_object"]
            if raw is None or not self.spool.object_exists(raw["content_hash"]):
                raise RecordedReferenceShadowError("successful page lacks finalized raw artifact")
            version_id = _derived_id("artifact-version", f"{key}:artifact:{ordinal}")
            artifact = self.authority.register_artifact_version_v2(
                artifact_ref,
                title=f"Recorded {self.plan['source_ref']} response page {ordinal}",
                kind="connector_raw_response", media_type="application/json",
                artifact_content_hash=raw["content_hash"], size_bytes=int(raw["size_bytes"]),
                storage_locator=raw["storage_locator"],
                producer_execution_ref=admission.execution.id,
                result_envelope_ref=result.id, result_envelope_hash=result_hash,
                access_class="internal", preview_status="unavailable",
                actor_ref="system:connector-runner",
                prior_version_ref=prior_artifact_version_ref, version_id=version_id,
                idempotency_key=f"{key}:artifact:{ordinal}",
            )
            artifacts.append(artifact)
            prior_artifact_version_ref = artifact["id"]
        final_raw = last["raw_object"]
        final_artifact = artifacts[-1]
        retrieved_at = _wire_time(self.clock())
        source_spec = {
            "schema_version": "0.1", "id": source_id, "created_at": retrieved_at,
            "connector_invocation_ref": admission.invocation["id"],
            "connector_profile_ref": admission.profile["id"],
            "physical_attempt_refs": [item["physical_attempt_ref"] for item in receipts],
            "result_physical_attempt_ref": last["physical_attempt_ref"],
            "source": admission.profile["source_identity"]["source_ref"],
            "operation": admission.call_spec["operation"],
            "source_record_refs": all_records,
            "published_at": None, "updated_at": None, "as_of": None,
            "retrieved_at": retrieved_at,
            "cursor": last["observation"]["cursor"],
            "provider_request_id": last["observation"]["provider_request_id"],
            "raw_artifact_version_ref": final_artifact["id"],
            "raw_response_hash": final_raw["content_hash"],
            "source_schema_hash": admission.profile["output_schema_hashes"][admission.call_spec["operation"]],
            "source_content_hash": "", "completeness": completeness, "status": status,
            "access_policy_ref": admission.profile["access_policy_ref"],
            "retention_policy_ref": admission.profile["retention_policy_ref"],
            "terms_policy_ref": admission.profile["terms_policy_ref"], "error": None,
        }
        source_spec["source_content_hash"] = source_envelope_content_hash(source_spec)
        source = self.authority.record_source_envelope(
            source_spec, idempotency_key=f"{key}:source"
        )
        response = self._response(
            admission, last, result, result_hash,
            artifact=final_artifact, source=source, outcome="succeeded",
            retry_at=None, idempotency_status=idempotency_status,
        )
        self.journal.append(
            admission.request["id"], "responded",
            {
                "reservation_ref": receipts[0]["reservation_ref"],
                "response": response, "result_envelope": result_wire,
                "result_envelope_hash": result_hash, "retry_at": None,
            },
        )
        self._fault("after_response_journaled")
        self._ensure_scheduler_completion(
            admission.request, result, result_hash, retry_at=None,
            scheduler_lease_token=scheduler_lease_token,
        )
        self._fault("after_scheduler_completed")
        return response

    def _finish_failure(
        self,
        admission: ValidatedRunnerAdmission,
        receipts: list[Mapping[str, Any]],
        *,
        scheduler_lease_token: str,
        idempotency_status: str,
    ) -> dict[str, Any]:
        last = receipts[-1]
        key = f"runner-shadow:{admission.invocation['id']}:aggregate"
        retry_at = last["retry_at"] or _wire_time(
            self.clock() + timedelta(seconds=self.retry_backoff_seconds)
        )
        result = ResultEnvelope(
            schema_version="0.1", id=_derived_id("result-envelope", f"{key}:result"),
            created_at=_wire_time(self.clock()), work_order_ref=admission.work_order.id,
            invocation_ref=admission.execution.id, status="retryable",
            outputs={
                "connector_invocation_ref": admission.invocation["id"],
                "physical_attempt_refs": [item["physical_attempt_ref"] for item in receipts],
                "result_physical_attempt_ref": last["physical_attempt_ref"],
                "quota_settlement_refs": [item["quota_settlement_ref"] for item in receipts],
                "source_envelope_ref": None,
            },
            actual_side_effects=("read:recorded-fixture",),
            usage_refs=tuple(item["usage_entry_ref"] for item in receipts),
            artifact_refs=(), error=last["error"] or {
                "code": last["attempt_outcome"], "message": "recorded source page failed",
                "retryable": True,
            },
            metadata={
                "runner_request_ref": admission.request["id"],
                "physical_attempt_count": len(receipts), "completeness": "unknown",
            },
        )
        result_hash = content_hash(result.to_dict())
        response = self._response(
            admission, last, result, result_hash, artifact=None, source=None,
            outcome="retryable", retry_at=retry_at,
            idempotency_status=idempotency_status,
        )
        self.journal.append(
            admission.request["id"], "responded",
            {
                "reservation_ref": receipts[0]["reservation_ref"],
                "response": response, "result_envelope": result.to_dict(),
                "result_envelope_hash": result_hash, "retry_at": retry_at,
            },
        )
        self._fault("after_response_journaled")
        self._ensure_scheduler_completion(
            admission.request, result, result_hash, retry_at=retry_at,
            scheduler_lease_token=scheduler_lease_token,
        )
        self._fault("after_scheduler_completed")
        return response

    def _ensure_scheduler_completion(
        self,
        request: Mapping[str, Any],
        result: ResultEnvelope,
        result_hash: str,
        *,
        retry_at: str | None,
        scheduler_lease_token: str,
    ) -> None:
        row = self.gate.scheduler.connection.execute(
            "SELECT work_order_id,attempt_number,result_envelope_hash,outcome "
            "FROM scheduler_result_envelopes WHERE result_envelope_id=?",
            (result.id,),
        ).fetchone()
        expected = (
            result.work_order_ref,
            int(request["scheduler_attempt_number"]),
            result_hash,
            result.status,
        )
        if row is not None:
            observed = (
                row["work_order_id"], int(row["attempt_number"]),
                row["result_envelope_hash"], row["outcome"],
            )
            if observed != expected:
                raise RecordedReferenceShadowError(
                    "scheduler completion conflicts with journaled result"
                )
            return
        completion = self.authority.complete(
            result.work_order_ref,
            int(request["scheduler_attempt_number"]),
            request["runner_actor_ref"],
            scheduler_lease_token,
            result,
            idempotency_key=(
                f"runner-shadow:{request['connector_invocation_ref']}:aggregate:"
                "scheduler-complete"
            ),
            result_envelope_hash=result_hash,
            retry_at=retry_at,
        )
        if completion.get("status") not in {"fresh", "duplicate"}:
            raise RecordedReferenceShadowError("scheduler completion did not converge")
        stored = self.gate.scheduler.connection.execute(
            "SELECT work_order_id,attempt_number,result_envelope_hash,outcome "
            "FROM scheduler_result_envelopes WHERE result_envelope_id=?",
            (result.id,),
        ).fetchone()
        if stored is None or (
            stored["work_order_id"], int(stored["attempt_number"]),
            stored["result_envelope_hash"], stored["outcome"],
        ) != expected:
            raise RecordedReferenceShadowError("scheduler completion receipt is missing")

    def _fault(self, barrier: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(barrier)

    @staticmethod
    def _response(
        admission: ValidatedRunnerAdmission,
        last: Mapping[str, Any],
        result: ResultEnvelope,
        result_hash: str,
        *,
        artifact: Mapping[str, Any] | None,
        source: Mapping[str, Any] | None,
        outcome: str,
        retry_at: str | None,
        idempotency_status: str,
    ) -> dict[str, Any]:
        base = {
            "schema_version": "0.2",
            "id": _derived_id(
                "connector-runner-response",
                f"runner-shadow:{admission.invocation['id']}:response",
            ),
            "created_at": result.created_at,
            "runner_request_ref": admission.request["id"],
            "runner_request_hash": admission.request["content_hash"],
            "idempotency_status": idempotency_status,
            "connector_invocation_ref": admission.invocation["id"],
            "connector_invocation_hash": admission.invocation["content_hash"],
            "physical_attempt_ref": last["physical_attempt_ref"],
            "physical_attempt_hash": last["physical_attempt_hash"],
            "usage_entry_ref": last["usage_entry_ref"],
            "usage_entry_hash": last["usage_entry_hash"],
            "cost_entry_ref": last["cost_entry_ref"],
            "cost_entry_hash": last["cost_entry_hash"],
            "quota_settlement_ref": last["quota_settlement_ref"],
            "quota_settlement_hash": last["quota_settlement_hash"],
            "raw_artifact_version_ref": None if artifact is None else artifact["id"],
            "raw_artifact_version_hash": None if artifact is None else artifact["content_hash"],
            "source_envelope_ref": None if source is None else source["id"],
            "source_envelope_hash": None if source is None else source["content_hash"],
            "result_envelope_ref": result.id, "result_envelope_hash": result_hash,
            "outcome": outcome, "retry_at": retry_at,
        }
        base["content_hash"] = content_hash(base)
        return validate_connector_runner_response(base)

    @staticmethod
    def _duplicate_response(response: Mapping[str, Any]) -> dict[str, Any]:
        duplicate = dict(response)
        duplicate["idempotency_status"] = "duplicate"
        duplicate.pop("content_hash", None)
        duplicate["content_hash"] = content_hash(duplicate)
        return validate_connector_runner_response(duplicate)


__all__ = [
    "RecordedReferenceShadowCoordinator", "RecordedReferenceShadowError",
    "validate_recorded_reference_shadow_plan",
]
