"""Fixture-only P2 coordinator with derived checkpoint/resume state.

The coordinator owns no Connector, Scheduler, Credential, or Research Ledger
database handle.  It receives one narrow execution port and persists only
rebuildable plan/context/index/run/checkpoint projections.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .connector_runner import validate_connector_runner_request
from .recorded_alphaengine_adapter import load_recorded_alphaengine_fixture
from .recorded_source_adapter import load_recorded_source_fixture
from .research_context import (
    ResearchContextConflict,
    ResearchContextError,
    validate_claim_index,
    validate_compiled_connector_plan,
    validate_compiled_connector_step,
    validate_context_pack,
    validate_runner_request_plan_binding,
)
from .store import canonical_json, content_hash


_SCHEMA_PATH = Path(__file__).with_name("research_coordinator_schema.sql")
_HASH_CHARS = frozenset("0123456789abcdef")


class ResearchCoordinatorError(ValueError):
    pass


class ResearchCoordinatorConflict(ResearchCoordinatorError):
    pass


class InjectedCoordinatorCrash(RuntimeError):
    pass


def _json(value: Any, name: str) -> Any:
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ResearchCoordinatorError(f"{name} must be finite JSON") from exc


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchCoordinatorError(f"{name} must be an object")
    wire = dict(value)
    missing = fields - set(wire)
    unknown = set(wire) - fields
    if missing or unknown:
        raise ResearchCoordinatorError(
            f"{name} closed shape mismatch: missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )
    return _json(wire, name)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResearchCoordinatorError(f"{name} must be a non-empty string")
    return value


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(char not in _HASH_CHARS for char in value):
        raise ResearchCoordinatorError(f"{name} must be lowercase SHA-256 hex")
    return value


def _integer(
    value: Any, name: str, *, minimum: int = 0, maximum: int | None = None
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ResearchCoordinatorError(f"{name} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ResearchCoordinatorError(f"{name} must be an integer <= {maximum}")
    return value


def _timestamp(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchCoordinatorError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ResearchCoordinatorError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _optional_text(value: Any, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _optional_hash(value: Any, name: str) -> str | None:
    return None if value is None else _hash(value, name)


def _paired(ref: Any, digest: Any, name: str) -> tuple[str | None, str | None]:
    ref_value = _optional_text(ref, f"{name}_ref")
    hash_value = _optional_hash(digest, f"{name}_hash")
    if (ref_value is None) != (hash_value is None):
        raise ResearchCoordinatorError(f"{name} ref/hash must be both null or both present")
    return ref_value, hash_value


def _refs(value: Any, name: str) -> list[str]:
    if not isinstance(value, list):
        raise ResearchCoordinatorError(f"{name} must be an array")
    result = [_text(item, f"{name}[]") for item in value]
    if len(result) != len(set(result)):
        raise ResearchCoordinatorError(f"{name} must contain unique values")
    return result


def _with_hash(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    wire = dict(value)
    declared = _hash(wire.pop("content_hash"), f"{name}.content_hash")
    if declared != content_hash(wire):
        raise ResearchCoordinatorConflict(f"{name} content_hash mismatch")
    wire["content_hash"] = declared
    return wire


def _ref_hash_items(value: Any, name: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ResearchCoordinatorError(f"{name} must be an array")
    result: list[dict[str, str]] = []
    for index, item in enumerate(value):
        wire = _closed(item, {"ref", "hash"}, f"{name}[{index}]")
        result.append(
            {
                "ref": _text(wire["ref"], f"{name}[{index}].ref"),
                "hash": _hash(wire["hash"], f"{name}[{index}].hash"),
            }
        )
    refs = [item["ref"] for item in result]
    if len(refs) != len(set(refs)):
        raise ResearchCoordinatorError(f"{name} refs must be unique")
    return sorted(result, key=lambda item: item["ref"])


_RECEIPT_FIELDS = {
    "schema_version", "id", "created_at", "runner_request_ref",
    "runner_request_hash", "status", "result_ref", "result_hash",
    "source_envelopes", "artifacts", "next_cursor", "error_code",
    "retry_after_ms", "content_hash",
}


def validate_connector_completion_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = _closed(value, _RECEIPT_FIELDS, "ConnectorCompletionReceipt")
    if wire["schema_version"] != "0.1":
        raise ResearchCoordinatorError("unsupported ConnectorCompletionReceipt schema_version")
    for field in ("id", "runner_request_ref", "result_ref"):
        wire[field] = _text(wire[field], field)
    for field in ("runner_request_hash", "result_hash"):
        wire[field] = _hash(wire[field], field)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    if wire["status"] not in {"succeeded", "retryable", "failed"}:
        raise ResearchCoordinatorError("ConnectorCompletionReceipt.status is invalid")
    wire["source_envelopes"] = _ref_hash_items(
        wire["source_envelopes"], "source_envelopes"
    )
    wire["artifacts"] = _ref_hash_items(wire["artifacts"], "artifacts")
    wire["next_cursor"] = _optional_text(wire["next_cursor"], "next_cursor")
    wire["error_code"] = _optional_text(wire["error_code"], "error_code")
    retry = wire["retry_after_ms"]
    wire["retry_after_ms"] = (
        None if retry is None else _integer(retry, "retry_after_ms", minimum=1)
    )
    if wire["status"] == "succeeded":
        if wire["error_code"] is not None or wire["retry_after_ms"] is not None:
            raise ResearchCoordinatorError("successful receipt cannot carry an error")
    else:
        if wire["error_code"] is None:
            raise ResearchCoordinatorError("non-success receipt requires error_code")
        if wire["source_envelopes"] or wire["artifacts"] or wire["next_cursor"] is not None:
            raise ResearchCoordinatorError("non-success receipt cannot fabricate source output")
        if (wire["status"] == "retryable") != (wire["retry_after_ms"] is not None):
            raise ResearchCoordinatorError("retryable receipt must bind retry_after_ms")
    return _with_hash(wire, "ConnectorCompletionReceipt")


_CHECKPOINT_FIELDS = {
    "schema_version", "id", "created_at", "run_ref", "attempt_ref",
    "attempt_hash", "sequence", "compiled_plan_ref", "compiled_plan_hash",
    "context_pack_ref", "context_pack_hash", "step_ref", "step_hash",
    "connector_attempt_number", "runner_request_ref", "runner_request_hash",
    "completion_receipt_ref", "completion_receipt_hash", "authority_bindings",
    "outcome", "source_envelopes", "artifacts", "next_cursor",
    "retry_after_ms", "idempotency_key", "prior_checkpoint_ref",
    "prior_checkpoint_hash", "content_hash",
}


def validate_research_checkpoint(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = _closed(value, _CHECKPOINT_FIELDS, "ResearchCheckpoint")
    if wire["schema_version"] != "0.1":
        raise ResearchCoordinatorError("unsupported ResearchCheckpoint schema_version")
    for field in (
        "id", "run_ref", "attempt_ref", "compiled_plan_ref", "context_pack_ref",
        "step_ref", "runner_request_ref", "completion_receipt_ref", "idempotency_key",
    ):
        wire[field] = _text(wire[field], field)
    for field in (
        "attempt_hash", "compiled_plan_hash", "context_pack_hash", "step_hash",
        "runner_request_hash", "completion_receipt_hash",
    ):
        wire[field] = _hash(wire[field], field)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    wire["sequence"] = _integer(wire["sequence"], "sequence", minimum=1)
    wire["connector_attempt_number"] = _integer(
        wire["connector_attempt_number"], "connector_attempt_number", minimum=1
    )
    authority = _closed(
        wire["authority_bindings"],
        {
            "connector_profile_ref", "connector_profile_hash",
            "capability_lease_ref", "capability_lease_hash", "source_ref",
            "source_hash",
        },
        "authority_bindings",
    )
    for field in ("connector_profile_ref", "capability_lease_ref", "source_ref"):
        authority[field] = _text(authority[field], f"authority_bindings.{field}")
    for field in ("connector_profile_hash", "capability_lease_hash", "source_hash"):
        authority[field] = _hash(authority[field], f"authority_bindings.{field}")
    wire["authority_bindings"] = authority
    if wire["outcome"] not in {"succeeded", "retryable", "failed"}:
        raise ResearchCoordinatorError("ResearchCheckpoint.outcome is invalid")
    wire["source_envelopes"] = _ref_hash_items(
        wire["source_envelopes"], "source_envelopes"
    )
    wire["artifacts"] = _ref_hash_items(wire["artifacts"], "artifacts")
    wire["next_cursor"] = _optional_text(wire["next_cursor"], "next_cursor")
    retry = wire["retry_after_ms"]
    wire["retry_after_ms"] = (
        None if retry is None else _integer(retry, "retry_after_ms", minimum=1)
    )
    if (wire["outcome"] == "retryable") != (wire["retry_after_ms"] is not None):
        raise ResearchCoordinatorError("checkpoint retry state is inconsistent")
    if wire["outcome"] != "succeeded" and (
        wire["source_envelopes"]
        or wire["artifacts"]
        or wire["next_cursor"] is not None
    ):
        raise ResearchCoordinatorError(
            "non-success checkpoint cannot carry source output"
        )
    prior = _paired(
        wire["prior_checkpoint_ref"], wire["prior_checkpoint_hash"], "prior_checkpoint"
    )
    wire["prior_checkpoint_ref"], wire["prior_checkpoint_hash"] = prior
    if wire["sequence"] == 1 and prior != (None, None):
        raise ResearchCoordinatorError("first checkpoint cannot claim a predecessor")
    if wire["sequence"] > 1 and prior == (None, None):
        raise ResearchCoordinatorError("continued checkpoint requires predecessor")
    return _with_hash(wire, "ResearchCheckpoint")


_RUN_STATE_FIELDS = {
    "schema_version", "id", "created_at", "run_ref", "version",
    "attempt_ref", "attempt_hash", "compiled_plan_ref", "compiled_plan_hash",
    "context_pack_ref", "context_pack_hash", "status", "completed_step_refs",
    "pending_step_refs", "blocked_step_refs", "last_checkpoint_ref",
    "last_checkpoint_hash", "prior_version_ref", "prior_version_hash",
    "content_hash",
}


def validate_research_run_state(
    value: Mapping[str, Any], *, plan: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    wire = _closed(value, _RUN_STATE_FIELDS, "ResearchRunState")
    if wire["schema_version"] != "0.1":
        raise ResearchCoordinatorError("unsupported ResearchRunState schema_version")
    for field in (
        "id", "run_ref", "attempt_ref", "compiled_plan_ref", "context_pack_ref",
    ):
        wire[field] = _text(wire[field], field)
    for field in (
        "attempt_hash", "compiled_plan_hash", "context_pack_hash",
    ):
        wire[field] = _hash(wire[field], field)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    wire["version"] = _integer(wire["version"], "version", minimum=1)
    if wire["status"] not in {"planned", "running", "retryable", "completed", "failed"}:
        raise ResearchCoordinatorError("ResearchRunState.status is invalid")
    for field in ("completed_step_refs", "pending_step_refs", "blocked_step_refs"):
        wire[field] = _refs(wire[field], field)
    completed = set(wire["completed_step_refs"])
    pending = set(wire["pending_step_refs"])
    blocked = set(wire["blocked_step_refs"])
    if completed & pending or completed & blocked or pending & blocked:
        raise ResearchCoordinatorError("run-state step partitions must be disjoint")
    last = _paired(
        wire["last_checkpoint_ref"], wire["last_checkpoint_hash"], "last_checkpoint"
    )
    prior = _paired(wire["prior_version_ref"], wire["prior_version_hash"], "prior_version")
    wire["last_checkpoint_ref"], wire["last_checkpoint_hash"] = last
    wire["prior_version_ref"], wire["prior_version_hash"] = prior
    if wire["version"] == 1 and prior != (None, None):
        raise ResearchCoordinatorError("first run state cannot claim a predecessor")
    if wire["version"] > 1 and prior == (None, None):
        raise ResearchCoordinatorError("continued run state requires predecessor")
    if wire["status"] == "completed" and (pending or blocked):
        raise ResearchCoordinatorError("completed run cannot have pending or blocked steps")
    if plan is not None:
        plan_wire = validate_compiled_connector_plan(plan)
        if (
            wire["compiled_plan_ref"] != plan_wire["id"]
            or wire["compiled_plan_hash"] != plan_wire["content_hash"]
        ):
            raise ResearchCoordinatorConflict("run state binds another compiled plan")
        expected = {item["id"] for item in plan_wire["steps"]}
        if completed | pending | blocked != expected:
            raise ResearchCoordinatorConflict("run-state partitions do not cover the plan")
    return _with_hash(wire, "ResearchRunState")


class ConnectorExecutionPort(Protocol):
    """The only connector-facing capability visible to the coordinator."""

    def execute(
        self,
        step: Mapping[str, Any],
        runner_request: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...


class RecordedShadowFixturePort:
    """Read only the three deterministic reference fixtures; never use network/auth."""

    def __init__(
        self,
        *,
        scenario_by_request: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._scenario_by_request = dict(scenario_by_request or {})
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._idempotency: dict[str, tuple[str, str, str, dict[str, Any]]] = {}
        self.execute_calls = 0
        self.transport_calls = 0

    def _now(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ResearchCoordinatorError("fixture port clock must be aware")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _scenario(step: Mapping[str, Any], scenario_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        source_ref = step["source_ref"]
        if source_ref == "source:cninfo":
            fixture = load_recorded_source_fixture("cninfo")
        elif source_ref == "source:sec-edgar":
            fixture = load_recorded_source_fixture("sec")
        elif source_ref == "source:alphaengine":
            fixture = load_recorded_alphaengine_fixture()
        else:
            raise ResearchCoordinatorError("fixture port received a non-reference source")
        matches = [item for item in fixture["scenarios"] if item["scenario"] == scenario_name]
        if len(matches) != 1:
            raise ResearchCoordinatorError("fixture scenario is not uniquely frozen")
        return fixture, matches[0]

    @staticmethod
    def _source_records(source_ref: str, scenario: Mapping[str, Any]) -> list[str]:
        if source_ref == "source:alphaengine":
            return list(scenario["source_record_refs"])
        refs: list[str] = []
        for page in scenario["pages"]:
            payload = page["raw_payload"]
            if not isinstance(payload, Mapping):
                continue
            if source_ref == "source:cninfo":
                refs.extend(
                    f"cninfo:announcement:{item['announcement_id']}"
                    for item in payload["announcements"]
                )
            else:
                refs.extend(
                    f"sec:filing:{item['accession']}" for item in payload["filings"]
                )
        return refs

    def execute(
        self,
        step: Mapping[str, Any],
        runner_request: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        self.execute_calls += 1
        step_wire = validate_compiled_connector_step(step)
        request = validate_connector_runner_request(runner_request)
        if (
            request.get("compiled_step_ref") != step_wire["id"]
            or request.get("compiled_step_hash") != step_wire["content_hash"]
            or request["connector_profile_ref"] != step_wire["connector_profile_ref"]
            or request["connector_profile_hash"] != step_wire["connector_profile_hash"]
        ):
            raise ResearchCoordinatorConflict(
                "fixture port request does not bind the supplied compiled step"
            )
        key = _text(idempotency_key, "idempotency_key")
        scenario_name = self._scenario_by_request.get(request["id"], "success")
        duplicate = self._idempotency.get(key)
        signature = (request["content_hash"], step_wire["content_hash"], scenario_name)
        if duplicate is not None:
            if duplicate[:3] != signature:
                raise ResearchCoordinatorConflict("fixture port idempotency conflict")
            return _json(duplicate[3], "duplicate receipt")
        self.transport_calls += 1
        fixture, scenario = self._scenario(step_wire, scenario_name)
        behavior = scenario["behavior"]
        if behavior == "return":
            status = "succeeded"
        elif behavior in {"rate_limited", "timeout"}:
            status = "retryable"
        else:
            status = "failed"
        source_envelopes: list[dict[str, str]] = []
        artifacts: list[dict[str, str]] = []
        next_cursor = None
        if status == "succeeded":
            source_records = self._source_records(step_wire["source_ref"], scenario)
            next_cursor = (
                scenario.get("next_cursor")
                if step_wire["source_ref"] == "source:alphaengine"
                else scenario["pages"][-1]["next_cursor"]
            )
            source_doc = {
                "fixture_ref": fixture["id"],
                "fixture_hash": fixture["content_hash"],
                "source_ref": step_wire["source_ref"],
                "operation": step_wire["operation"],
                "scenario": scenario_name,
                "source_record_refs": source_records,
                "next_cursor": next_cursor,
            }
            source_hash = content_hash(source_doc)
            source_envelopes = [
                {"ref": "source-envelope:fixture:" + source_hash, "hash": source_hash}
            ]
            raw_payload = (
                scenario.get("raw_payload")
                if step_wire["source_ref"] == "source:alphaengine"
                else [page["raw_payload"] for page in scenario["pages"]]
            )
            artifact_doc = {
                "fixture_ref": fixture["id"],
                "fixture_hash": fixture["content_hash"],
                "scenario": scenario_name,
                "raw_payload": raw_payload,
            }
            artifact_hash = content_hash(artifact_doc)
            artifacts = [{"ref": "artifact:fixture:" + artifact_hash, "hash": artifact_hash}]
        error_code = None if status == "succeeded" else scenario.get("error_code") or scenario_name
        retry_after_ms = None
        if status == "retryable":
            retry_after_ms = scenario.get("retry_after_ms") or 1
        result_doc = {
            "runner_request_ref": request["id"],
            "runner_request_hash": request["content_hash"],
            "status": status,
            "source_envelopes": source_envelopes,
            "artifacts": artifacts,
            "next_cursor": next_cursor,
            "error_code": error_code,
            "retry_after_ms": retry_after_ms,
        }
        result_hash = content_hash(result_doc)
        base = {
            "schema_version": "0.1",
            "id": "connector-completion-receipt:fixture:" + result_hash,
            "created_at": self._now(),
            "runner_request_ref": request["id"],
            "runner_request_hash": request["content_hash"],
            "status": status,
            "result_ref": "result-envelope:fixture:" + result_hash,
            "result_hash": result_hash,
            "source_envelopes": source_envelopes,
            "artifacts": artifacts,
            "next_cursor": next_cursor,
            "error_code": error_code,
            "retry_after_ms": retry_after_ms,
        }
        base["content_hash"] = content_hash(base)
        receipt = validate_connector_completion_receipt(base)
        self._idempotency[key] = (*signature, receipt)
        return _json(receipt, "receipt")


class ResearchCoordinatorStore:
    """Owner-local, rebuildable scratch store; not a Research Ledger authority."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            target = Path(self.path)
            target.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        if self.path != ":memory:":
            os.chmod(self.path, 0o600)

    def close(self) -> None:
        self.connection.close()

    def _insert(
        self,
        *,
        table: str,
        id_column: str,
        identifier: str,
        json_column: str,
        wire: Mapping[str, Any],
        extra_columns: Sequence[str],
        extra_values: Sequence[Any],
    ) -> dict[str, Any]:
        encoded = canonical_json(wire)
        existing = self.connection.execute(
            f"SELECT {json_column},content_hash FROM {table} WHERE {id_column}=?",
            (identifier,),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] != wire["content_hash"] or existing[json_column] != encoded:
                raise ResearchCoordinatorConflict(f"{table} identity conflict")
            return _json(wire, table)
        columns = [id_column, json_column, "content_hash", *extra_columns]
        placeholders = ",".join("?" for _ in columns)
        values = [identifier, encoded, wire["content_hash"], *extra_values]
        try:
            self.connection.execute(
                f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})",
                values,
            )
        except sqlite3.IntegrityError as exc:
            raise ResearchCoordinatorConflict(f"{table} append conflict") from exc
        return _json(wire, table)

    def store_plan(self, value: Mapping[str, Any]) -> dict[str, Any]:
        wire = validate_compiled_connector_plan(value)
        return self._insert(
            table="compiled_connector_plans", id_column="plan_id", identifier=wire["id"],
            json_column="plan_json", wire=wire, extra_columns=("created_at",),
            extra_values=(wire["created_at"],),
        )

    def store_context_pack(self, value: Mapping[str, Any]) -> dict[str, Any]:
        wire = validate_context_pack(value)
        return self._insert(
            table="context_packs", id_column="context_pack_id", identifier=wire["id"],
            json_column="context_pack_json", wire=wire, extra_columns=("created_at",),
            extra_values=(wire["created_at"],),
        )

    def store_claim_index(self, value: Mapping[str, Any]) -> dict[str, Any]:
        wire = validate_claim_index(value)
        return self._insert(
            table="claim_indexes", id_column="claim_index_id", identifier=wire["id"],
            json_column="claim_index_json", wire=wire, extra_columns=("created_at",),
            extra_values=(wire["created_at"],),
        )

    def append_checkpoint(self, value: Mapping[str, Any]) -> dict[str, Any]:
        wire = validate_research_checkpoint(value)
        encoded = canonical_json(wire)
        existing = self.connection.execute(
            "SELECT checkpoint_json,content_hash FROM research_checkpoints "
            "WHERE checkpoint_id=?",
            (wire["id"],),
        ).fetchone()
        if existing is not None:
            if (
                existing["content_hash"] != wire["content_hash"]
                or existing["checkpoint_json"] != encoded
            ):
                raise ResearchCoordinatorConflict(
                    "research_checkpoints identity conflict"
                )
            return _json(wire, "research_checkpoints")
        latest_row = self.connection.execute(
            "SELECT checkpoint_json FROM research_checkpoints "
            "WHERE run_ref=? ORDER BY sequence_number DESC LIMIT 1",
            (wire["run_ref"],),
        ).fetchone()
        latest = None if latest_row is None else json.loads(latest_row["checkpoint_json"])
        expected_sequence = 1 if latest is None else latest["sequence"] + 1
        expected_prior = (
            (None, None)
            if latest is None
            else (latest["id"], latest["content_hash"])
        )
        actual_prior = (
            wire["prior_checkpoint_ref"], wire["prior_checkpoint_hash"]
        )
        if wire["sequence"] != expected_sequence or actual_prior != expected_prior:
            raise ResearchCoordinatorConflict("checkpoint chain is not exact")
        if latest is not None and (
            wire["attempt_ref"] != latest["attempt_ref"]
            or wire["attempt_hash"] != latest["attempt_hash"]
            or wire["compiled_plan_ref"] != latest["compiled_plan_ref"]
            or wire["compiled_plan_hash"] != latest["compiled_plan_hash"]
            or wire["context_pack_ref"] != latest["context_pack_ref"]
            or wire["context_pack_hash"] != latest["context_pack_hash"]
        ):
            raise ResearchCoordinatorConflict(
                "checkpoint chain changed an authority binding"
            )
        return self._insert(
            table="research_checkpoints", id_column="checkpoint_id", identifier=wire["id"],
            json_column="checkpoint_json", wire=wire,
            extra_columns=(
                "run_ref", "sequence_number", "prior_checkpoint_id", "created_at"
            ),
            extra_values=(
                wire["run_ref"], wire["sequence"], wire["prior_checkpoint_ref"],
                wire["created_at"],
            ),
        )

    def append_run_state(
        self, value: Mapping[str, Any], *, plan: Mapping[str, Any]
    ) -> dict[str, Any]:
        wire = validate_research_run_state(value, plan=plan)
        latest = self.latest_run_state(wire["run_ref"])
        expected_version = 1 if latest is None else latest["version"] + 1
        expected_prior = (
            (None, None)
            if latest is None
            else (latest["id"], latest["content_hash"])
        )
        if wire["version"] != expected_version or (
            wire["prior_version_ref"], wire["prior_version_hash"]
        ) != expected_prior:
            raise ResearchCoordinatorConflict("run-state version chain is not exact")
        return self._insert(
            table="research_run_state_versions", id_column="run_state_id",
            identifier=wire["id"], json_column="state_json", wire=wire,
            extra_columns=("run_ref", "version_number", "prior_version_id", "created_at"),
            extra_values=(
                wire["run_ref"], wire["version"], wire["prior_version_ref"], wire["created_at"],
            ),
        )

    def latest_run_state(self, run_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT state_json FROM research_run_state_versions "
            "WHERE run_ref=? ORDER BY version_number DESC LIMIT 1",
            (_text(run_ref, "run_ref"),),
        ).fetchone()
        return None if row is None else json.loads(row["state_json"])

    def list_checkpoints(self, run_ref: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT checkpoint_json FROM research_checkpoints "
            "WHERE run_ref=? ORDER BY sequence_number",
            (_text(run_ref, "run_ref"),),
        ).fetchall()
        return [json.loads(row["checkpoint_json"]) for row in rows]


class FixtureResearchCoordinator:
    """Single-lane consumer of a once-compiled connector plan."""

    def __init__(
        self,
        *,
        store: ResearchCoordinatorStore,
        connector_port: ConnectorExecutionPort,
        clock: Callable[[], datetime] | None = None,
        fault_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> None:
        if not isinstance(store, ResearchCoordinatorStore):
            raise TypeError("FixtureResearchCoordinator requires ResearchCoordinatorStore")
        if not callable(getattr(connector_port, "execute", None)):
            raise TypeError("connector_port must expose only an execute capability")
        self.store = store
        self.connector_port = connector_port
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fault_hook = fault_hook

    def _now(self) -> str:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ResearchCoordinatorError("coordinator clock must be aware")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    def _fault(self, stage: str, payload: Mapping[str, Any]) -> None:
        if self.fault_hook is not None:
            self.fault_hook(stage, _json(payload, "fault payload"))

    @staticmethod
    def _validate_inputs(
        plan: Mapping[str, Any],
        context_pack: Mapping[str, Any],
        claim_index: Mapping[str, Any],
        runner_requests: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, list[dict[str, Any]]]]:
        plan_wire = validate_compiled_connector_plan(plan)
        if any(step["fallback_step_refs"] for step in plan_wire["steps"]):
            raise ResearchCoordinatorError(
                "fixture coordinator 0.1 does not execute fallback branches"
            )
        context = validate_context_pack(context_pack)
        index = validate_claim_index(claim_index)
        if (
            context["task_ref"] != plan_wire["task_ref"]
            or context["task_hash"] != plan_wire["task_hash"]
            or context["compiled_plan_ref"] != plan_wire["id"]
            or context["compiled_plan_hash"] != plan_wire["content_hash"]
            or context["claim_index_ref"] != index["id"]
            or context["claim_index_hash"] != index["content_hash"]
        ):
            raise ResearchCoordinatorConflict("ContextPack does not bind plan/ClaimIndex")
        if set(runner_requests) != {step["id"] for step in plan_wire["steps"]}:
            raise ResearchCoordinatorConflict("runner request map must cover every plan step")
        validated: dict[str, list[dict[str, Any]]] = {}
        for step in plan_wire["steps"]:
            requests = runner_requests[step["id"]]
            if not isinstance(requests, Sequence) or isinstance(requests, (str, bytes)):
                raise ResearchCoordinatorError("runner request attempts must be an array")
            if len(requests) != step["max_attempts"]:
                raise ResearchCoordinatorConflict("runner request attempts do not match max_attempts")
            wires = [
                validate_runner_request_plan_binding(item, plan_wire, step)
                for item in requests
            ]
            if [item["scheduler_attempt_number"] for item in wires] != list(
                range(1, step["max_attempts"] + 1)
            ):
                raise ResearchCoordinatorConflict("runner attempts must be contiguous")
            if len({item["id"] for item in wires}) != len(wires):
                raise ResearchCoordinatorConflict("runner attempts must have unique ids")
            validated[step["id"]] = wires
        return plan_wire, context, index, validated

    @staticmethod
    def _checkpoint_groups(
        checkpoints: Sequence[Mapping[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for checkpoint in checkpoints:
            wire = validate_research_checkpoint(checkpoint)
            result[wire["step_ref"]].append(wire)
        return result

    def _derive_state(
        self,
        *,
        plan: Mapping[str, Any],
        context_pack: Mapping[str, Any],
        run_ref: str,
        attempt_ref: str,
        attempt_hash: str,
        checkpoints: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        plan_wire = validate_compiled_connector_plan(plan)
        context = validate_context_pack(context_pack)
        groups = self._checkpoint_groups(checkpoints)
        step_by_ref = {step["id"]: step for step in plan_wire["steps"]}
        attempt_counts: dict[str, int] = defaultdict(int)
        for checkpoint in checkpoints:
            step = step_by_ref.get(checkpoint["step_ref"])
            if step is None:
                raise ResearchCoordinatorConflict(
                    "checkpoint names a step outside the compiled plan"
                )
            attempt_counts[step["id"]] += 1
            if (
                checkpoint["run_ref"] != run_ref
                or checkpoint["attempt_ref"] != attempt_ref
                or checkpoint["attempt_hash"] != attempt_hash
                or checkpoint["compiled_plan_ref"] != plan_wire["id"]
                or checkpoint["compiled_plan_hash"] != plan_wire["content_hash"]
                or checkpoint["context_pack_ref"] != context["id"]
                or checkpoint["context_pack_hash"] != context["content_hash"]
                or checkpoint["step_hash"] != step["content_hash"]
                or checkpoint["connector_attempt_number"]
                != attempt_counts[step["id"]]
                or checkpoint["connector_attempt_number"] > step["max_attempts"]
            ):
                raise ResearchCoordinatorConflict(
                    "checkpoint recovery authority does not match the run"
                )
        completed: set[str] = set()
        terminal_failed: set[str] = set()
        for step in plan_wire["steps"]:
            attempts = groups.get(step["id"], [])
            if attempts and attempts[-1]["outcome"] == "succeeded":
                completed.add(step["id"])
            elif attempts and (
                attempts[-1]["outcome"] == "failed"
                or len(attempts) >= step["max_attempts"]
            ):
                terminal_failed.add(step["id"])
        blocked: set[str] = set(terminal_failed)
        for step in plan_wire["steps"]:
            if any(dependency in terminal_failed or dependency in blocked for dependency in step["depends_on"]):
                blocked.add(step["id"])
        all_refs = {step["id"] for step in plan_wire["steps"]}
        pending = all_refs - completed - blocked
        if completed == all_refs:
            status = "completed"
        elif blocked:
            status = "failed"
        elif checkpoints and checkpoints[-1]["outcome"] == "retryable":
            status = "retryable"
        elif checkpoints:
            status = "running"
        else:
            status = "planned"
        latest = self.store.latest_run_state(run_ref)
        version = 1 if latest is None else latest["version"] + 1
        prior_ref = None if latest is None else latest["id"]
        prior_hash = None if latest is None else latest["content_hash"]
        last = checkpoints[-1] if checkpoints else None
        identity = {
            "run_ref": run_ref,
            "version": version,
            "plan_hash": plan_wire["content_hash"],
            "checkpoint_hash": None if last is None else last["content_hash"],
        }
        base = {
            "schema_version": "0.1",
            "id": "research-run-state:" + content_hash(identity),
            "created_at": self._now(),
            "run_ref": _text(run_ref, "run_ref"),
            "version": version,
            "attempt_ref": _text(attempt_ref, "attempt_ref"),
            "attempt_hash": _hash(attempt_hash, "attempt_hash"),
            "compiled_plan_ref": plan_wire["id"],
            "compiled_plan_hash": plan_wire["content_hash"],
            "context_pack_ref": context["id"],
            "context_pack_hash": context["content_hash"],
            "status": status,
            "completed_step_refs": sorted(completed),
            "pending_step_refs": sorted(pending),
            "blocked_step_refs": sorted(blocked),
            "last_checkpoint_ref": None if last is None else last["id"],
            "last_checkpoint_hash": None if last is None else last["content_hash"],
            "prior_version_ref": prior_ref,
            "prior_version_hash": prior_hash,
        }
        base["content_hash"] = content_hash(base)
        return validate_research_run_state(base, plan=plan_wire)

    def _reconcile_state(
        self,
        *,
        plan: Mapping[str, Any],
        context_pack: Mapping[str, Any],
        run_ref: str,
        attempt_ref: str,
        attempt_hash: str,
    ) -> dict[str, Any]:
        checkpoints = self.store.list_checkpoints(run_ref)
        derived = self._derive_state(
            plan=plan,
            context_pack=context_pack,
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            attempt_hash=attempt_hash,
            checkpoints=checkpoints,
        )
        latest = self.store.latest_run_state(run_ref)
        comparable = (
            "attempt_ref", "attempt_hash", "compiled_plan_ref", "compiled_plan_hash",
            "context_pack_ref", "context_pack_hash", "status",
            "completed_step_refs", "pending_step_refs", "blocked_step_refs",
            "last_checkpoint_ref", "last_checkpoint_hash",
        )
        if latest is not None and all(latest[field] == derived[field] for field in comparable):
            return validate_research_run_state(latest, plan=plan)
        return self.store.append_run_state(derived, plan=plan)

    def _checkpoint(
        self,
        *,
        plan: Mapping[str, Any],
        context_pack: Mapping[str, Any],
        run_ref: str,
        attempt_ref: str,
        attempt_hash: str,
        step: Mapping[str, Any],
        runner_request: Mapping[str, Any],
        receipt: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        plan_wire = validate_compiled_connector_plan(plan)
        context = validate_context_pack(context_pack)
        step_wire = validate_compiled_connector_step(step)
        request = validate_runner_request_plan_binding(runner_request, plan_wire, step_wire)
        receipt_wire = validate_connector_completion_receipt(receipt)
        if (
            receipt_wire["runner_request_ref"] != request["id"]
            or receipt_wire["runner_request_hash"] != request["content_hash"]
        ):
            raise ResearchCoordinatorConflict("completion receipt binds another runner request")
        prior_checkpoints = self.store.list_checkpoints(run_ref)
        prior = prior_checkpoints[-1] if prior_checkpoints else None
        sequence = len(prior_checkpoints) + 1
        identity = {
            "run_ref": run_ref,
            "sequence": sequence,
            "receipt_hash": receipt_wire["content_hash"],
        }
        base = {
            "schema_version": "0.1",
            "id": "research-checkpoint:" + content_hash(identity),
            "created_at": self._now(),
            "run_ref": _text(run_ref, "run_ref"),
            "attempt_ref": _text(attempt_ref, "attempt_ref"),
            "attempt_hash": _hash(attempt_hash, "attempt_hash"),
            "sequence": sequence,
            "compiled_plan_ref": plan_wire["id"],
            "compiled_plan_hash": plan_wire["content_hash"],
            "context_pack_ref": context["id"],
            "context_pack_hash": context["content_hash"],
            "step_ref": step_wire["id"],
            "step_hash": step_wire["content_hash"],
            "connector_attempt_number": request["scheduler_attempt_number"],
            "runner_request_ref": request["id"],
            "runner_request_hash": request["content_hash"],
            "completion_receipt_ref": receipt_wire["id"],
            "completion_receipt_hash": receipt_wire["content_hash"],
            "authority_bindings": {
                "connector_profile_ref": request["connector_profile_ref"],
                "connector_profile_hash": request["connector_profile_hash"],
                "capability_lease_ref": request["capability_lease_ref"],
                "capability_lease_hash": request["capability_lease_hash"],
                "source_ref": step_wire["source_ref"],
                "source_hash": step_wire["source_hash"],
            },
            "outcome": receipt_wire["status"],
            "source_envelopes": receipt_wire["source_envelopes"],
            "artifacts": receipt_wire["artifacts"],
            "next_cursor": receipt_wire["next_cursor"],
            "retry_after_ms": receipt_wire["retry_after_ms"],
            "idempotency_key": _text(idempotency_key, "idempotency_key"),
            "prior_checkpoint_ref": None if prior is None else prior["id"],
            "prior_checkpoint_hash": None if prior is None else prior["content_hash"],
        }
        base["content_hash"] = content_hash(base)
        return validate_research_checkpoint(base)

    def run(
        self,
        *,
        plan: Mapping[str, Any],
        context_pack: Mapping[str, Any],
        claim_index: Mapping[str, Any],
        runner_requests: Mapping[str, Sequence[Mapping[str, Any]]],
        run_ref: str,
        attempt_ref: str,
        attempt_hash: str,
    ) -> dict[str, Any]:
        plan_wire, context, index, requests = self._validate_inputs(
            plan, context_pack, claim_index, runner_requests
        )
        self.store.store_plan(plan_wire)
        self.store.store_claim_index(index)
        self.store.store_context_pack(context)
        state = self._reconcile_state(
            plan=plan_wire,
            context_pack=context,
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            attempt_hash=attempt_hash,
        )
        if state["status"] in {"completed", "failed"}:
            return {
                "status": state["status"],
                "run_state_ref": state["id"],
                "run_state_hash": state["content_hash"],
                "checkpoint_count": len(self.store.list_checkpoints(run_ref)),
            }
        step_by_ref = {step["id"]: step for step in plan_wire["steps"]}
        while True:
            checkpoints = self.store.list_checkpoints(run_ref)
            groups = self._checkpoint_groups(checkpoints)
            state = self._reconcile_state(
                plan=plan_wire,
                context_pack=context,
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                attempt_hash=attempt_hash,
            )
            if state["status"] in {"completed", "failed"}:
                return {
                    "status": state["status"],
                    "run_state_ref": state["id"],
                    "run_state_hash": state["content_hash"],
                    "checkpoint_count": len(checkpoints),
                }
            completed = set(state["completed_step_refs"])
            candidates = [
                step_by_ref[ref] for ref in state["pending_step_refs"]
                if set(step_by_ref[ref]["depends_on"]).issubset(completed)
            ]
            if not candidates:
                raise ResearchCoordinatorConflict("no runnable step in non-terminal state")
            step = min(candidates, key=lambda item: item["ordinal"])
            attempt_number = len(groups.get(step["id"], [])) + 1
            if attempt_number > step["max_attempts"]:
                raise ResearchCoordinatorConflict("retry bound exceeded before execution")
            request = requests[step["id"]][attempt_number - 1]
            idempotency_key = f"research-coordinator:{run_ref}:{step['id']}:{attempt_number}"
            receipt = validate_connector_completion_receipt(
                self.connector_port.execute(
                    step, request, idempotency_key=idempotency_key
                )
            )
            self._fault(
                "after_execute",
                {
                    "run_ref": run_ref,
                    "step_ref": step["id"],
                    "runner_request_ref": request["id"],
                    "receipt_ref": receipt["id"],
                },
            )
            checkpoint = self._checkpoint(
                plan=plan_wire,
                context_pack=context,
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                attempt_hash=attempt_hash,
                step=step,
                runner_request=request,
                receipt=receipt,
                idempotency_key=idempotency_key,
            )
            self.store.append_checkpoint(checkpoint)
            self._fault("after_checkpoint", checkpoint)
            state = self._reconcile_state(
                plan=plan_wire,
                context_pack=context,
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                attempt_hash=attempt_hash,
            )
            self._fault("after_state", state)
            if receipt["status"] == "retryable":
                if state["status"] == "failed":
                    return {
                        "status": "failed",
                        "run_state_ref": state["id"],
                        "run_state_hash": state["content_hash"],
                        "checkpoint_count": len(self.store.list_checkpoints(run_ref)),
                    }
                return {
                    "status": "retryable",
                    "run_state_ref": state["id"],
                    "run_state_hash": state["content_hash"],
                    "checkpoint_count": len(self.store.list_checkpoints(run_ref)),
                    "retry_after_ms": receipt["retry_after_ms"],
                }
            if receipt["status"] == "failed":
                return {
                    "status": "failed",
                    "run_state_ref": state["id"],
                    "run_state_hash": state["content_hash"],
                    "checkpoint_count": len(self.store.list_checkpoints(run_ref)),
                }


__all__ = [
    "ConnectorExecutionPort", "FixtureResearchCoordinator", "InjectedCoordinatorCrash",
    "RecordedShadowFixturePort", "ResearchCoordinatorConflict",
    "ResearchCoordinatorError", "ResearchCoordinatorStore",
    "validate_connector_completion_receipt", "validate_research_checkpoint",
    "validate_research_run_state",
]
