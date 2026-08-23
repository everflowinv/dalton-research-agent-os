"""Human-gated model inputs, frozen model runs, and reconciliation authority.

The ledger deliberately stops before spreadsheet execution.  It owns exact,
immutable inputs and run manifests; callers may calculate elsewhere, but they
cannot replace an admitted assumption, silently revise an actual, or publish a
valuation without version-bound market-data authorities.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

from .store import DaltonStore, canonical_json, content_hash


SCHEMA_VERSION = "0.1"
INPUT_KINDS = frozenset({"actual", "assumption", "forecast_line", "scenario"})
PERIOD_KINDS = frozenset({
    "instant", "quarter", "fiscal_year", "calendar_year",
    "trailing_twelve_months", "forecast_period",
})
RUN_STATUSES = frozenset({"completed", "failed"})
RECONCILIATION_CHECKS = (
    "financial_statement", "unit_currency", "period_calendar", "share_count",
    "actual_override", "source_revision",
)
VALUATION_AUTHORITY_ROLES = frozenset({
    "price", "shares", "fx", "rates", "consensus",
})
_SCHEMA_PATH = Path(__file__).with_name("model_input_schema.sql")
_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class ModelInputLedgerError(RuntimeError):
    """Base error for model-input authority."""


class ModelInputValidationError(ModelInputLedgerError):
    """A request does not satisfy the closed v1 contract."""


class ModelInputConflict(ModelInputLedgerError):
    """A request conflicts with immutable or current authority."""


class ModelInputNotFound(ModelInputLedgerError):
    """A referenced authority is absent."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelInputValidationError(f"{name} must be non-empty text")
    return value.strip()


def _optional_text(value: Any, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise ModelInputValidationError(f"{name} must be a lowercase SHA-256")
    return value


def _human(value: Any, name: str) -> str:
    value = _text(value, name)
    if not value.startswith("human:") or value == "human:":
        raise ModelInputValidationError(f"{name} must use the human: namespace")
    return value


def _decimal(value: Any, name: str) -> str:
    if not isinstance(value, str) or _DECIMAL_RE.fullmatch(value) is None:
        raise ModelInputValidationError(f"{name} must be a canonical decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ModelInputValidationError(f"{name} must be decimal") from exc
    if not parsed.is_finite():
        raise ModelInputValidationError(f"{name} must be finite")
    return value


def _currency(value: Any, name: str) -> str | None:
    if value is None:
        return None
    value = _text(value, name)
    if len(value) != 3 or not value.isascii() or not value.isalpha() or value != value.upper():
        raise ModelInputValidationError(f"{name} must be an uppercase ISO-style code")
    return value


def _rfc3339(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelInputValidationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ModelInputValidationError(f"{name} must include timezone")
    return value


def _objects(value: Any, name: str, *, nonempty: bool = False) -> list[dict[str, Any]]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ModelInputValidationError(f"{name} must be an object array")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ModelInputValidationError(f"{name}[{index}] must be an object")
        result.append(dict(item))
    return result


def _strings(value: Any, name: str) -> list[str]:
    if not isinstance(value, list):
        raise ModelInputValidationError(f"{name} must be an array")
    result = [_text(item, f"{name}[]") for item in value]
    if len(result) != len(set(result)):
        raise ModelInputValidationError(f"{name} must be unique")
    return result


def _closed(wire: Mapping[str, Any], fields: set[str], name: str) -> dict[str, Any]:
    result = wire if isinstance(wire, dict) else dict(wire)
    if set(result) != fields:
        missing = sorted(fields - set(result))
        extra = sorted(set(result) - fields)
        raise ModelInputValidationError(
            f"{name} has an invalid closed shape (missing={missing}, extra={extra})"
        )
    return result


def _period(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelInputValidationError(f"{name} must be an object")
    wire = _closed(dict(value), {"start", "end", "calendar", "kind"}, name)
    for field in ("start", "end"):
        wire[field] = _text(wire[field], f"{name}.{field}")
        try:
            date.fromisoformat(wire[field])
        except ValueError as exc:
            raise ModelInputValidationError(f"{name}.{field} must be YYYY-MM-DD") from exc
    if wire["start"] > wire["end"]:
        raise ModelInputValidationError(f"{name} start cannot follow end")
    wire["calendar"] = _text(wire["calendar"], f"{name}.calendar")
    if wire["kind"] not in PERIOD_KINDS:
        raise ModelInputValidationError(f"{name}.kind is invalid")
    if wire["kind"] == "instant" and wire["start"] != wire["end"]:
        raise ModelInputValidationError(f"{name} instant must use one date")
    return wire


def _source_bindings(value: Any, name: str) -> list[dict[str, Any]]:
    rows = _objects(value, name)
    seen: set[tuple[str, str]] = set()
    for row in rows:
        row = _closed(row, {"authority_kind", "version_ref", "content_hash"}, f"{name}[]")
        if row["authority_kind"] not in {"evidence_version", "claim_version"}:
            raise ModelInputValidationError(f"{name}.authority_kind is invalid")
        row["version_ref"] = _text(row["version_ref"], f"{name}.version_ref")
        row["content_hash"] = _hash(row["content_hash"], f"{name}.content_hash")
        identity = (row["authority_kind"], row["version_ref"])
        if identity in seen:
            raise ModelInputValidationError(f"{name} contains a duplicate authority")
        seen.add(identity)
    return rows


def _input_bindings(value: Any, name: str, *, nonempty: bool = False) -> list[dict[str, Any]]:
    rows = _objects(value, name, nonempty=nonempty)
    seen: set[str] = set()
    seen_versions: set[str] = set()
    for index, raw in enumerate(rows):
        row = _closed(
            raw, {"binding_ref", "role", "version_ref", "version_hash"}, f"{name}[{index}]"
        )
        for field in ("binding_ref", "role", "version_ref"):
            row[field] = _text(row[field], f"{name}.{field}")
        row["version_hash"] = _hash(row["version_hash"], f"{name}.version_hash")
        if row["binding_ref"] in seen:
            raise ModelInputValidationError(f"{name}.binding_ref must be unique")
        if row["version_ref"] in seen_versions:
            raise ModelInputValidationError(f"{name}.version_ref must be unique")
        seen.add(row["binding_ref"])
        seen_versions.add(row["version_ref"])
    return rows


def _record(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(value)
    wire["content_hash"] = content_hash(wire)
    return wire


def _canonical_record(raw: Any, name: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ModelInputConflict(f"{name} record is missing")
    try:
        wire = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModelInputConflict(f"{name} record is invalid JSON") from exc
    if not isinstance(wire, dict) or canonical_json(wire) != raw:
        raise ModelInputConflict(f"{name} record is not canonical")
    asserted = wire.get("content_hash")
    base = dict(wire)
    base.pop("content_hash", None)
    if asserted is None or content_hash(base) != asserted:
        raise ModelInputConflict(f"{name} record hash drifted")
    return wire


class ModelInputLedger:
    """Append-only model authority embedded in the Dalton Core database."""

    def __init__(self, store: DaltonStore):
        self.store = store
        self.connection = store.connection
        self._authorized = False
        self.connection.create_function(
            "dalton_model_authorized", 0, lambda: int(self._authorized)
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self.connection.in_transaction:
            raise RuntimeError("ModelInputLedger operation cannot be nested")
        self.connection.execute("BEGIN IMMEDIATE")
        self._authorized = True
        try:
            yield self.connection.cursor()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            self._authorized = False

    @staticmethod
    def _request_hash(operation: str, request: Mapping[str, Any]) -> str:
        return content_hash({"operation": operation, "request": dict(request)})

    def _idem(
        self, cur: sqlite3.Cursor, key: str, operation: str, request_hash: str
    ) -> dict[str, Any] | None:
        row = cur.execute(
            "SELECT operation,request_hash,result_json FROM model_input_idempotency "
            "WHERE idempotency_key=?", (key,),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_hash"] != request_hash:
            raise ModelInputConflict("idempotency key conflicts with prior request")
        result = json.loads(row["result_json"])
        return {**result, "status": "duplicate"}

    @staticmethod
    def _save_idem(
        cur: sqlite3.Cursor, key: str, operation: str, request_hash: str,
        result: Mapping[str, Any], created_at: str,
    ) -> None:
        cur.execute(
            "INSERT INTO model_input_idempotency"
            "(idempotency_key,operation,request_hash,result_json,created_at) VALUES(?,?,?,?,?)",
            (key, operation, request_hash, canonical_json(result), created_at),
        )

    @staticmethod
    def _event(
        cur: sqlite3.Cursor, *, event_type: str, aggregate_ref: str,
        aggregate_version_ref: str, aggregate_hash: str, actor_ref: str,
        idempotency_key: str, created_at: str,
    ) -> dict[str, Any]:
        event_id = "model-event:" + content_hash({
            "event_type": event_type,
            "aggregate_version_ref": aggregate_version_ref,
            "idempotency_key": idempotency_key,
        })
        wire = _record({
            "schema_version": SCHEMA_VERSION,
            "id": event_id,
            "created_at": created_at,
            "event_type": event_type,
            "aggregate_ref": aggregate_ref,
            "aggregate_version_ref": aggregate_version_ref,
            "aggregate_hash": aggregate_hash,
            "actor_ref": actor_ref,
            "idempotency_key": idempotency_key,
        })
        cur.execute(
            "INSERT INTO model_input_events"
            "(event_id,event_type,aggregate_ref,aggregate_version_ref,aggregate_hash,"
            "actor_ref,idempotency_key,record_json,content_hash,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                event_id, event_type, aggregate_ref, aggregate_version_ref,
                aggregate_hash, actor_ref, idempotency_key, canonical_json(wire),
                wire["content_hash"], created_at,
            ),
        )
        return wire

    @staticmethod
    def _source_row(cur: sqlite3.Cursor, binding: Mapping[str, Any]) -> sqlite3.Row:
        if binding["authority_kind"] == "evidence_version":
            row = cur.execute(
                "SELECT evidence_version_id AS version_ref,content_hash "
                "FROM evidence_versions WHERE evidence_version_id=?",
                (binding["version_ref"],),
            ).fetchone()
        else:
            row = cur.execute(
                "SELECT claim_version_id AS version_ref,content_hash "
                "FROM claim_versions WHERE claim_version_id=?",
                (binding["version_ref"],),
            ).fetchone()
        if row is None:
            raise ModelInputNotFound("source authority was not found")
        if row["content_hash"] != binding["content_hash"]:
            raise ModelInputConflict("source authority hash binding failed")
        return row

    @staticmethod
    def _version_row(
        cur: sqlite3.Cursor, version_ref: str, version_hash: str,
        *, expected_kind: str | None = None,
    ) -> sqlite3.Row:
        row = cur.execute(
            "SELECT * FROM model_input_versions WHERE version_id=?", (version_ref,)
        ).fetchone()
        if row is None:
            raise ModelInputNotFound("model input version was not found")
        if row["content_hash"] != version_hash:
            raise ModelInputConflict("model input version hash binding failed")
        if expected_kind is not None and row["input_kind"] != expected_kind:
            raise ModelInputConflict(f"model input version must be {expected_kind}")
        return row

    def _validate_payload(
        self, cur: sqlite3.Cursor, input_kind: str, model_input_ref: str,
        value: Mapping[str, Any],
    ) -> dict[str, Any]:
        if input_kind not in INPUT_KINDS:
            raise ModelInputValidationError("input_kind is invalid")
        if not isinstance(value, Mapping):
            raise ModelInputValidationError("payload must be an object")
        wire = dict(value)
        if wire.get("schema_version") != SCHEMA_VERSION:
            raise ModelInputValidationError("payload schema_version is invalid")

        if input_kind == "actual":
            wire = _closed(wire, {
                "schema_version", "metric_ref", "subject_ref", "business_line_ref",
                "period", "unit", "currency", "value", "source_authorities",
            }, "actual payload")
            for field in ("metric_ref", "subject_ref", "unit"):
                wire[field] = _text(wire[field], field)
            wire["business_line_ref"] = _optional_text(
                wire["business_line_ref"], "business_line_ref"
            )
            wire["period"] = _period(wire["period"], "period")
            wire["currency"] = _currency(wire["currency"], "currency")
            wire["value"] = _decimal(wire["value"], "value")
            wire["source_authorities"] = _source_bindings(
                wire["source_authorities"], "source_authorities"
            )
            if not wire["source_authorities"]:
                raise ModelInputValidationError("actual requires source authority")

        elif input_kind == "scenario":
            wire = _closed(wire, {
                "schema_version", "scenario_ref", "label", "description",
                "base_scenario_version_ref", "base_scenario_version_hash", "owner_ref",
            }, "scenario payload")
            for field in ("scenario_ref", "label", "description"):
                wire[field] = _text(wire[field], field)
            if wire["scenario_ref"] != model_input_ref:
                raise ModelInputValidationError("scenario_ref must equal model_input_ref")
            wire["owner_ref"] = _human(wire["owner_ref"], "owner_ref")
            base_ref = _optional_text(
                wire["base_scenario_version_ref"], "base_scenario_version_ref"
            )
            base_hash = wire["base_scenario_version_hash"]
            if (base_ref is None) != (base_hash is None):
                raise ModelInputValidationError("base scenario ref and hash must be paired")
            if base_ref is not None:
                base_hash = _hash(base_hash, "base_scenario_version_hash")
                self._version_row(cur, base_ref, base_hash, expected_kind="scenario")
            wire["base_scenario_version_ref"] = base_ref
            wire["base_scenario_version_hash"] = base_hash

        elif input_kind == "assumption":
            wire = _closed(wire, {
                "schema_version", "driver_ref", "subject_ref", "effective_period",
                "unit", "currency", "value", "formula", "scenario_version_ref",
                "scenario_version_hash", "owner_ref", "rationale", "provenance",
                "source_authorities", "dependency_bindings",
            }, "assumption payload")
            for field in ("driver_ref", "subject_ref", "unit", "rationale"):
                wire[field] = _text(wire[field], field)
            wire["effective_period"] = _period(wire["effective_period"], "effective_period")
            wire["currency"] = _currency(wire["currency"], "currency")
            wire["owner_ref"] = _human(wire["owner_ref"], "owner_ref")
            if (wire["value"] is None) == (wire["formula"] is None):
                raise ModelInputValidationError("assumption requires exactly one value or formula")
            wire["value"] = None if wire["value"] is None else _decimal(wire["value"], "value")
            wire["formula"] = _optional_text(wire["formula"], "formula")
            wire["scenario_version_ref"] = _text(
                wire["scenario_version_ref"], "scenario_version_ref"
            )
            wire["scenario_version_hash"] = _hash(
                wire["scenario_version_hash"], "scenario_version_hash"
            )
            self._version_row(
                cur, wire["scenario_version_ref"], wire["scenario_version_hash"],
                expected_kind="scenario",
            )
            if wire["provenance"] not in {"source", "judgment"}:
                raise ModelInputValidationError("assumption provenance is invalid")
            wire["source_authorities"] = _source_bindings(
                wire["source_authorities"], "source_authorities"
            )
            if wire["provenance"] == "source" and not wire["source_authorities"]:
                raise ModelInputValidationError("source assumption requires source authority")
            if wire["provenance"] == "judgment" and wire["source_authorities"]:
                raise ModelInputValidationError("judgment assumption cannot imply source authority")
            wire["dependency_bindings"] = _input_bindings(
                wire["dependency_bindings"], "dependency_bindings"
            )
            if wire["formula"] is not None and not wire["dependency_bindings"]:
                raise ModelInputValidationError("formula assumption requires dependencies")

        else:
            wire = _closed(wire, {
                "schema_version", "metric_ref", "subject_ref", "business_line_ref",
                "forecast_period", "unit", "currency", "value", "formula",
                "scenario_version_ref", "scenario_version_hash",
                "historical_actual_bindings", "dependency_bindings",
            }, "forecast line payload")
            for field in ("metric_ref", "subject_ref", "unit"):
                wire[field] = _text(wire[field], field)
            wire["business_line_ref"] = _optional_text(
                wire["business_line_ref"], "business_line_ref"
            )
            wire["forecast_period"] = _period(wire["forecast_period"], "forecast_period")
            if wire["forecast_period"]["kind"] != "forecast_period":
                raise ModelInputValidationError("forecast line requires forecast_period kind")
            wire["currency"] = _currency(wire["currency"], "currency")
            if (wire["value"] is None) == (wire["formula"] is None):
                raise ModelInputValidationError("forecast line requires exactly one value or formula")
            wire["value"] = None if wire["value"] is None else _decimal(wire["value"], "value")
            wire["formula"] = _optional_text(wire["formula"], "formula")
            wire["scenario_version_ref"] = _text(
                wire["scenario_version_ref"], "scenario_version_ref"
            )
            wire["scenario_version_hash"] = _hash(
                wire["scenario_version_hash"], "scenario_version_hash"
            )
            self._version_row(
                cur, wire["scenario_version_ref"], wire["scenario_version_hash"],
                expected_kind="scenario",
            )
            wire["historical_actual_bindings"] = _input_bindings(
                wire["historical_actual_bindings"], "historical_actual_bindings",
                nonempty=True,
            )
            for binding in wire["historical_actual_bindings"]:
                self._version_row(
                    cur, binding["version_ref"], binding["version_hash"],
                    expected_kind="actual",
                )
            wire["dependency_bindings"] = _input_bindings(
                wire["dependency_bindings"], "dependency_bindings"
            )
            if wire["formula"] is not None and not wire["dependency_bindings"]:
                raise ModelInputValidationError("formula forecast requires dependencies")

        for binding in wire.get("source_authorities", []):
            self._source_row(cur, binding)
        for binding in wire.get("dependency_bindings", []):
            self._version_row(cur, binding["version_ref"], binding["version_hash"])
        return wire

    def propose_input(
        self, *, candidate_id: str, input_kind: str, model_input_ref: str,
        prior_version_ref: str | None, payload: Mapping[str, Any], proposed_by: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        candidate_id = _text(candidate_id, "candidate_id")
        input_kind = _text(input_kind, "input_kind")
        model_input_ref = _text(model_input_ref, "model_input_ref")
        prior_version_ref = _optional_text(prior_version_ref, "prior_version_ref")
        proposed_by = _text(proposed_by, "proposed_by")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        request = {
            "candidate_id": candidate_id, "input_kind": input_kind,
            "model_input_ref": model_input_ref, "prior_version_ref": prior_version_ref,
            "payload": dict(payload), "proposed_by": proposed_by,
        }
        request_hash = self._request_hash("propose_input", request)
        with self._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "propose_input", request_hash)
            if duplicate is not None:
                return duplicate
            if cur.execute(
                "SELECT 1 FROM model_input_candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone():
                raise ModelInputConflict("candidate id already exists")
            pointer = cur.execute(
                "SELECT version_id FROM model_input_pointer WHERE model_input_ref=?",
                (model_input_ref,),
            ).fetchone()
            current_ref = None if pointer is None else pointer["version_id"]
            if current_ref != prior_version_ref:
                raise ModelInputConflict("candidate must continue the current input version")
            payload_wire = self._validate_payload(cur, input_kind, model_input_ref, payload)
            created_at = _now()
            wire = _record({
                "schema_version": SCHEMA_VERSION,
                "id": candidate_id,
                "created_at": created_at,
                "input_kind": input_kind,
                "model_input_ref": model_input_ref,
                "prior_version_ref": prior_version_ref,
                "payload": payload_wire,
                "proposed_by": proposed_by,
            })
            cur.execute(
                "INSERT INTO model_input_candidates"
                "(candidate_id,input_kind,model_input_ref,prior_version_id,payload_json,"
                "payload_hash,proposed_by,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    candidate_id, input_kind, model_input_ref, prior_version_ref,
                    canonical_json(payload_wire), content_hash(payload_wire), proposed_by,
                    canonical_json(wire), wire["content_hash"], created_at,
                ),
            )
            event = self._event(
                cur, event_type="candidate_staged", aggregate_ref=model_input_ref,
                aggregate_version_ref=candidate_id, aggregate_hash=wire["content_hash"],
                actor_ref=proposed_by, idempotency_key=idempotency_key, created_at=created_at,
            )
            result = {"status": "fresh", "candidate": wire, "event": event}
            self._save_idem(
                cur, idempotency_key, "propose_input", request_hash, result, created_at
            )
            return result

    def decide_input(
        self, *, decision_id: str, candidate_id: str, candidate_hash: str,
        verdict: str, rationale: str, findings: list[str], reviewer_ref: str,
        version_id: str | None, idempotency_key: str,
    ) -> dict[str, Any]:
        decision_id = _text(decision_id, "decision_id")
        candidate_id = _text(candidate_id, "candidate_id")
        candidate_hash = _hash(candidate_hash, "candidate_hash")
        if verdict not in {"admit", "reject"}:
            raise ModelInputValidationError("verdict must be admit or reject")
        rationale = _text(rationale, "rationale")
        findings_wire = _strings(findings, "findings")
        reviewer_ref = _human(reviewer_ref, "reviewer_ref")
        version_id = _optional_text(version_id, "version_id")
        if (verdict == "admit") != (version_id is not None):
            raise ModelInputValidationError("admit requires version_id and reject forbids it")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        request = {
            "decision_id": decision_id, "candidate_id": candidate_id,
            "candidate_hash": candidate_hash, "verdict": verdict,
            "rationale": rationale, "findings": findings_wire,
            "reviewer_ref": reviewer_ref, "version_id": version_id,
        }
        request_hash = self._request_hash("decide_input", request)
        with self._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "decide_input", request_hash)
            if duplicate is not None:
                return duplicate
            row = cur.execute(
                "SELECT * FROM model_input_candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise ModelInputNotFound("candidate was not found")
            candidate = _canonical_record(row["record_json"], "candidate")
            if row["content_hash"] != candidate_hash or candidate["content_hash"] != candidate_hash:
                raise ModelInputConflict("candidate hash binding failed")
            if cur.execute(
                "SELECT 1 FROM model_input_decisions WHERE candidate_id=?", (candidate_id,)
            ).fetchone():
                raise ModelInputConflict("candidate already has a decision")
            if cur.execute(
                "SELECT 1 FROM model_input_decisions WHERE decision_id=?", (decision_id,)
            ).fetchone():
                raise ModelInputConflict("decision id already exists")
            pointer = cur.execute(
                "SELECT version_id FROM model_input_pointer WHERE model_input_ref=?",
                (candidate["model_input_ref"],),
            ).fetchone()
            current_ref = None if pointer is None else pointer["version_id"]
            if current_ref != candidate["prior_version_ref"]:
                raise ModelInputConflict("candidate became stale before decision")
            self._validate_payload(
                cur, candidate["input_kind"], candidate["model_input_ref"],
                candidate["payload"],
            )
            created_at = _now()
            decision = _record({
                "schema_version": SCHEMA_VERSION,
                "id": decision_id,
                "created_at": created_at,
                "candidate_ref": candidate_id,
                "candidate_hash": candidate_hash,
                "verdict": verdict,
                "rationale": rationale,
                "findings": findings_wire,
                "reviewer_ref": reviewer_ref,
            })
            cur.execute(
                "INSERT INTO model_input_decisions"
                "(decision_id,candidate_id,candidate_hash,verdict,rationale,findings_json,"
                "reviewer_ref,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id, candidate_id, candidate_hash, verdict, rationale,
                    canonical_json(findings_wire), reviewer_ref, canonical_json(decision),
                    decision["content_hash"], created_at,
                ),
            )
            decision_event = self._event(
                cur, event_type="candidate_decided",
                aggregate_ref=candidate["model_input_ref"],
                aggregate_version_ref=decision_id, aggregate_hash=decision["content_hash"],
                actor_ref=reviewer_ref, idempotency_key=idempotency_key,
                created_at=created_at,
            )
            version = None
            commit_event = None
            if verdict == "admit":
                assert version_id is not None
                if cur.execute(
                    "SELECT 1 FROM model_input_versions WHERE version_id=?", (version_id,)
                ).fetchone():
                    raise ModelInputConflict("model input version id already exists")
                number = 1 if current_ref is None else int(cur.execute(
                    "SELECT version_number FROM model_input_versions WHERE version_id=?",
                    (current_ref,),
                ).fetchone()["version_number"]) + 1
                version = _record({
                    "schema_version": SCHEMA_VERSION,
                    "id": version_id,
                    "created_at": created_at,
                    "model_input_ref": candidate["model_input_ref"],
                    "version": number,
                    "prior_version_ref": current_ref,
                    "input_kind": candidate["input_kind"],
                    "payload": candidate["payload"],
                    "admission_decision_ref": decision_id,
                    "actor_ref": reviewer_ref,
                })
                cur.execute(
                    "INSERT INTO model_input_versions"
                    "(version_id,model_input_ref,version_number,prior_version_id,input_kind,"
                    "payload_json,payload_hash,admission_decision_id,record_json,content_hash,"
                    "actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        version_id, candidate["model_input_ref"], number, current_ref,
                        candidate["input_kind"], canonical_json(candidate["payload"]),
                        content_hash(candidate["payload"]), decision_id,
                        canonical_json(version), version["content_hash"], reviewer_ref, created_at,
                    ),
                )
                cur.execute(
                    "INSERT INTO model_input_pointer"
                    "(model_input_ref,version_id,version_number,content_hash,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(model_input_ref) DO UPDATE SET "
                    "version_id=excluded.version_id,version_number=excluded.version_number,"
                    "content_hash=excluded.content_hash,updated_at=excluded.updated_at",
                    (
                        candidate["model_input_ref"], version_id, number,
                        version["content_hash"], created_at,
                    ),
                )
                commit_event = self._event(
                    cur, event_type="input_committed",
                    aggregate_ref=candidate["model_input_ref"],
                    aggregate_version_ref=version_id, aggregate_hash=version["content_hash"],
                    actor_ref=reviewer_ref, idempotency_key=idempotency_key,
                    created_at=created_at,
                )
            result = {
                "status": "fresh", "decision": decision, "version": version,
                "events": [decision_event] + ([] if commit_event is None else [commit_event]),
            }
            self._save_idem(
                cur, idempotency_key, "decide_input", request_hash, result, created_at
            )
            return result

    def _validate_run_bindings(
        self, cur: sqlite3.Cursor, bindings: list[dict[str, Any]]
    ) -> dict[str, sqlite3.Row]:
        rows: dict[str, sqlite3.Row] = {}
        for binding in bindings:
            rows[binding["binding_ref"]] = self._version_row(
                cur, binding["version_ref"], binding["version_hash"]
            )
        return rows

    def _validate_run_input_closure(
        self, cur: sqlite3.Cursor, rows: Mapping[str, sqlite3.Row],
        bindings: list[dict[str, Any]], scenario_version_ref: str,
        scenario_version_hash: str,
    ) -> None:
        frozen = {
            (binding["version_ref"], binding["version_hash"])
            for binding in bindings
        }
        for row in rows.values():
            payload = json.loads(row["payload_json"])
            if row["input_kind"] in {"assumption", "forecast_line"} and (
                payload["scenario_version_ref"] != scenario_version_ref
                or payload["scenario_version_hash"] != scenario_version_hash
            ):
                raise ModelInputConflict(
                    "run input scenario does not match the frozen run scenario"
                )
            nested = list(payload.get("dependency_bindings", []))
            nested.extend(payload.get("historical_actual_bindings", []))
            for dependency in nested:
                exact = (dependency["version_ref"], dependency["version_hash"])
                if exact not in frozen:
                    raise ModelInputConflict(
                        "model run omits a version-bound input dependency"
                    )
                self._version_row(cur, *exact)

    def _outputs(
        self, cur: sqlite3.Cursor, value: Any,
        input_rows: Mapping[str, sqlite3.Row], input_bindings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        outputs = _objects(value, "outputs")
        binding_by_ref = {row["binding_ref"]: row for row in input_bindings}
        seen: set[str] = set()
        for index, raw in enumerate(outputs):
            row = _closed(raw, {
                "output_ref", "output_kind", "metric_ref", "period", "unit",
                "currency", "value", "authority_bindings",
            }, f"outputs[{index}]")
            for field in ("output_ref", "metric_ref", "unit"):
                row[field] = _text(row[field], f"outputs.{field}")
            if row["output_ref"] in seen:
                raise ModelInputValidationError("output_ref must be unique")
            seen.add(row["output_ref"])
            if row["output_kind"] not in {"metric", "valuation"}:
                raise ModelInputValidationError("output_kind is invalid")
            row["period"] = _period(row["period"], "outputs.period")
            row["currency"] = _currency(row["currency"], "outputs.currency")
            row["value"] = _decimal(row["value"], "outputs.value")
            authorities = _objects(row["authority_bindings"], "authority_bindings")
            authority_roles: set[str] = set()
            for authority in authorities:
                authority = _closed(
                    authority, {"role", "binding_ref"}, "authority_bindings[]"
                )
                role = _text(authority["role"], "authority role")
                binding_ref = _text(authority["binding_ref"], "authority binding_ref")
                if role in authority_roles:
                    raise ModelInputValidationError("valuation authority role must be unique")
                authority_roles.add(role)
                binding = binding_by_ref.get(binding_ref)
                if binding is None:
                    raise ModelInputConflict("valuation authority is not a frozen run input")
                if binding["role"] != role:
                    raise ModelInputConflict(
                        "valuation authority role does not match its frozen input role"
                    )
                if input_rows[binding_ref]["input_kind"] != "actual":
                    raise ModelInputConflict("valuation authority must bind an actual input")
            if row["output_kind"] == "metric" and authorities:
                raise ModelInputValidationError("metric output cannot claim valuation authority")
            if row["output_kind"] == "valuation" and authority_roles != VALUATION_AUTHORITY_ROLES:
                raise ModelInputConflict("valuation output lacks price/shares/fx/rates/consensus authority")
            row["authority_bindings"] = authorities
        return outputs

    def record_model_run(
        self, *, version_id: str, model_run_ref: str,
        prior_version_ref: str | None, scenario_version_ref: str,
        scenario_version_hash: str, input_bindings: list[Mapping[str, Any]],
        formula_version_ref: str, formula_version_hash: str,
        status: str, outputs: list[Mapping[str, Any]], errors: list[str],
        started_at: str, completed_at: str, actor_ref: str, idempotency_key: str,
    ) -> dict[str, Any]:
        version_id = _text(version_id, "version_id")
        model_run_ref = _text(model_run_ref, "model_run_ref")
        prior_version_ref = _optional_text(prior_version_ref, "prior_version_ref")
        scenario_version_ref = _text(scenario_version_ref, "scenario_version_ref")
        scenario_version_hash = _hash(scenario_version_hash, "scenario_version_hash")
        bindings = _input_bindings(input_bindings, "input_bindings", nonempty=True)
        formula_version_ref = _text(formula_version_ref, "formula_version_ref")
        formula_version_hash = _hash(formula_version_hash, "formula_version_hash")
        if status not in RUN_STATUSES:
            raise ModelInputValidationError("model run status is invalid")
        errors_wire = _strings(errors, "errors")
        started_at = _rfc3339(started_at, "started_at")
        completed_at = _rfc3339(completed_at, "completed_at")
        if datetime.fromisoformat(started_at.replace("Z", "+00:00")) > datetime.fromisoformat(
            completed_at.replace("Z", "+00:00")
        ):
            raise ModelInputValidationError("model run completed before it started")
        actor_ref = _text(actor_ref, "actor_ref")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        request = {
            "version_id": version_id, "model_run_ref": model_run_ref,
            "prior_version_ref": prior_version_ref,
            "scenario_version_ref": scenario_version_ref,
            "scenario_version_hash": scenario_version_hash,
            "input_bindings": bindings, "formula_version_ref": formula_version_ref,
            "formula_version_hash": formula_version_hash, "status": status,
            "outputs": [dict(item) for item in outputs], "errors": errors_wire,
            "started_at": started_at, "completed_at": completed_at, "actor_ref": actor_ref,
        }
        request_hash = self._request_hash("record_model_run", request)
        with self._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "record_model_run", request_hash)
            if duplicate is not None:
                return duplicate
            if cur.execute(
                "SELECT 1 FROM model_run_versions WHERE version_id=?", (version_id,)
            ).fetchone():
                raise ModelInputConflict("model run version id already exists")
            pointer = cur.execute(
                "SELECT version_id,version_number FROM model_run_pointer WHERE model_run_ref=?",
                (model_run_ref,),
            ).fetchone()
            current_ref = None if pointer is None else pointer["version_id"]
            if current_ref != prior_version_ref:
                raise ModelInputConflict("model run must continue the current version")
            self._version_row(
                cur, scenario_version_ref, scenario_version_hash, expected_kind="scenario"
            )
            input_rows = self._validate_run_bindings(cur, bindings)
            self._validate_run_input_closure(
                cur, input_rows, bindings, scenario_version_ref, scenario_version_hash
            )
            output_wire = self._outputs(cur, outputs, input_rows, bindings)
            if status == "completed" and (not output_wire or errors_wire):
                raise ModelInputValidationError("completed run requires outputs and no errors")
            if status == "failed" and (output_wire or not errors_wire):
                raise ModelInputValidationError("failed run requires errors and no outputs")
            created_at = _now()
            number = 1 if pointer is None else int(pointer["version_number"]) + 1
            wire = _record({
                "schema_version": SCHEMA_VERSION,
                "id": version_id,
                "created_at": created_at,
                "model_run_ref": model_run_ref,
                "version": number,
                "prior_version_ref": prior_version_ref,
                "scenario_version_ref": scenario_version_ref,
                "scenario_version_hash": scenario_version_hash,
                "input_bindings": bindings,
                "formula_version_ref": formula_version_ref,
                "formula_version_hash": formula_version_hash,
                "status": status,
                "outputs": output_wire,
                "errors": errors_wire,
                "started_at": started_at,
                "completed_at": completed_at,
                "actor_ref": actor_ref,
            })
            cur.execute(
                "INSERT INTO model_run_versions"
                "(version_id,model_run_ref,version_number,prior_version_id,scenario_version_ref,"
                "scenario_version_hash,status,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id, model_run_ref, number, prior_version_ref,
                    scenario_version_ref, scenario_version_hash, status,
                    canonical_json(wire), wire["content_hash"], actor_ref, created_at,
                ),
            )
            cur.execute(
                "INSERT INTO model_run_pointer"
                "(model_run_ref,version_id,version_number,content_hash,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(model_run_ref) DO UPDATE SET version_id=excluded.version_id,"
                "version_number=excluded.version_number,content_hash=excluded.content_hash,"
                "updated_at=excluded.updated_at",
                (model_run_ref, version_id, number, wire["content_hash"], created_at),
            )
            event = self._event(
                cur, event_type="model_run_committed", aggregate_ref=model_run_ref,
                aggregate_version_ref=version_id, aggregate_hash=wire["content_hash"],
                actor_ref=actor_ref, idempotency_key=idempotency_key, created_at=created_at,
            )
            result = {"status": "fresh", "model_run": wire, "event": event}
            self._save_idem(
                cur, idempotency_key, "record_model_run", request_hash, result, created_at
            )
            return result

    @staticmethod
    def _reconciliation_checks(value: Any) -> list[dict[str, Any]]:
        checks = _objects(value, "checks", nonempty=True)
        by_kind: dict[str, dict[str, Any]] = {}
        for raw in checks:
            row = _closed(raw, {
                "check_kind", "status", "details", "authority_bindings",
            }, "checks[]")
            if row["check_kind"] not in RECONCILIATION_CHECKS:
                raise ModelInputValidationError("reconciliation check kind is invalid")
            if row["check_kind"] in by_kind:
                raise ModelInputValidationError("reconciliation check kind must be unique")
            if row["status"] not in {"pass", "fail", "not_applicable"}:
                raise ModelInputValidationError("reconciliation check status is invalid")
            row["details"] = _text(row["details"], "check details")
            authorities = _objects(row["authority_bindings"], "authority_bindings")
            identities: set[tuple[str, str]] = set()
            for authority in authorities:
                authority = _closed(
                    authority, {"authority_kind", "version_ref", "content_hash"},
                    "authority_bindings[]",
                )
                if authority["authority_kind"] not in {
                    "model_input_version", "model_run_version",
                    "evidence_version", "claim_version",
                }:
                    raise ModelInputValidationError(
                        "reconciliation authority_kind is invalid"
                    )
                authority["version_ref"] = _text(
                    authority["version_ref"], "authority version_ref"
                )
                authority["content_hash"] = _hash(
                    authority["content_hash"], "authority content_hash"
                )
                identity = (authority["authority_kind"], authority["version_ref"])
                if identity in identities:
                    raise ModelInputValidationError(
                        "reconciliation authority must be unique"
                    )
                identities.add(identity)
            if row["status"] != "not_applicable" and not authorities:
                raise ModelInputValidationError(
                    "applicable reconciliation check requires authority"
                )
            row["authority_bindings"] = authorities
            by_kind[row["check_kind"]] = row
        if set(by_kind) != set(RECONCILIATION_CHECKS):
            raise ModelInputValidationError("reconciliation must cover every v1 check")
        return [by_kind[kind] for kind in RECONCILIATION_CHECKS]

    def record_reconciliation(
        self, *, reconciliation_id: str, model_run_version_ref: str,
        model_run_version_hash: str, checks: list[Mapping[str, Any]],
        actor_ref: str, idempotency_key: str,
    ) -> dict[str, Any]:
        reconciliation_id = _text(reconciliation_id, "reconciliation_id")
        model_run_version_ref = _text(model_run_version_ref, "model_run_version_ref")
        model_run_version_hash = _hash(model_run_version_hash, "model_run_version_hash")
        checks_wire = self._reconciliation_checks(checks)
        actor_ref = _text(actor_ref, "actor_ref")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        request = {
            "reconciliation_id": reconciliation_id,
            "model_run_version_ref": model_run_version_ref,
            "model_run_version_hash": model_run_version_hash,
            "checks": checks_wire, "actor_ref": actor_ref,
        }
        request_hash = self._request_hash("record_reconciliation", request)
        with self._transaction() as cur:
            duplicate = self._idem(
                cur, idempotency_key, "record_reconciliation", request_hash
            )
            if duplicate is not None:
                return duplicate
            if cur.execute(
                "SELECT 1 FROM model_reconciliations WHERE reconciliation_id=?",
                (reconciliation_id,),
            ).fetchone():
                raise ModelInputConflict("reconciliation id already exists")
            row = cur.execute(
                "SELECT * FROM model_run_versions WHERE version_id=?",
                (model_run_version_ref,),
            ).fetchone()
            if row is None:
                raise ModelInputNotFound("model run version was not found")
            if row["content_hash"] != model_run_version_hash:
                raise ModelInputConflict("model run hash binding failed")
            run = _canonical_record(row["record_json"], "model run")
            by_kind = {item["check_kind"]: item for item in checks_wire}
            for check in checks_wire:
                for authority in check["authority_bindings"]:
                    if authority["authority_kind"] in {
                        "evidence_version", "claim_version",
                    }:
                        self._source_row(cur, authority)
                    elif authority["authority_kind"] == "model_input_version":
                        self._version_row(
                            cur, authority["version_ref"], authority["content_hash"]
                        )
                    else:
                        authority_row = cur.execute(
                            "SELECT content_hash FROM model_run_versions WHERE version_id=?",
                            (authority["version_ref"],),
                        ).fetchone()
                        if authority_row is None:
                            raise ModelInputNotFound(
                                "reconciliation model run authority was not found"
                            )
                        if authority_row["content_hash"] != authority["content_hash"]:
                            raise ModelInputConflict(
                                "reconciliation model run authority hash failed"
                            )
            stale = False
            for binding in run["input_bindings"]:
                pointer = cur.execute(
                    "SELECT version_id FROM model_input_pointer WHERE model_input_ref=("
                    "SELECT model_input_ref FROM model_input_versions WHERE version_id=?)",
                    (binding["version_ref"],),
                ).fetchone()
                if pointer is None or pointer["version_id"] != binding["version_ref"]:
                    stale = True
                    break
                input_row = cur.execute(
                    "SELECT payload_json FROM model_input_versions WHERE version_id=?",
                    (binding["version_ref"],),
                ).fetchone()
                payload = json.loads(input_row["payload_json"])
                for source in payload.get("source_authorities", []):
                    if source["authority_kind"] == "evidence_version":
                        latest = cur.execute(
                            "SELECT evidence_version_id AS version_ref FROM evidence_versions "
                            "WHERE evidence_ref=(SELECT evidence_ref FROM evidence_versions "
                            "WHERE evidence_version_id=?) ORDER BY version_number DESC LIMIT 1",
                            (source["version_ref"],),
                        ).fetchone()
                    else:
                        latest = cur.execute(
                            "SELECT claim_version_id AS version_ref FROM claim_versions "
                            "WHERE claim_ref=(SELECT claim_ref FROM claim_versions "
                            "WHERE claim_version_id=?) ORDER BY version_number DESC LIMIT 1",
                            (source["version_ref"],),
                        ).fetchone()
                    if latest is None or latest["version_ref"] != source["version_ref"]:
                        stale = True
                        break
                if stale:
                    break
            if stale and by_kind["source_revision"]["status"] != "fail":
                raise ModelInputConflict("source_revision must fail for superseded inputs")
            actual_override = False
            for binding in run["input_bindings"]:
                version = self._version_row(
                    cur, binding["version_ref"], binding["version_hash"]
                )
                if version["input_kind"] != "forecast_line":
                    continue
                forecast = json.loads(version["payload_json"])
                candidates = cur.execute(
                    "SELECT payload_json FROM model_input_versions v JOIN model_input_pointer p "
                    "ON p.version_id=v.version_id WHERE v.input_kind='actual'"
                ).fetchall()
                for actual_row in candidates:
                    actual = json.loads(actual_row["payload_json"])
                    if (
                        actual["metric_ref"] == forecast["metric_ref"]
                        and actual["subject_ref"] == forecast["subject_ref"]
                        and actual["business_line_ref"] == forecast["business_line_ref"]
                        and {
                            key: actual["period"][key]
                            for key in ("start", "end", "calendar")
                        } == {
                            key: forecast["forecast_period"][key]
                            for key in ("start", "end", "calendar")
                        }
                    ):
                        actual_override = True
                        break
            if actual_override and by_kind["actual_override"]["status"] != "fail":
                raise ModelInputConflict("actual_override must fail when an actual supersedes forecast")
            has_valuation = any(
                item["output_kind"] == "valuation" for item in run["outputs"]
            )
            if has_valuation:
                for kind in ("unit_currency", "share_count", "source_revision"):
                    if by_kind[kind]["status"] != "pass":
                        raise ModelInputConflict(f"valuation reconciliation requires {kind}=pass")
            verdict = "pass"
            if row["status"] != "completed" or any(
                item["status"] == "fail" for item in checks_wire
            ):
                verdict = "fail"
            created_at = _now()
            wire = _record({
                "schema_version": SCHEMA_VERSION,
                "id": reconciliation_id,
                "created_at": created_at,
                "model_run_version_ref": model_run_version_ref,
                "model_run_version_hash": model_run_version_hash,
                "verdict": verdict,
                "checks": checks_wire,
                "actor_ref": actor_ref,
            })
            cur.execute(
                "INSERT INTO model_reconciliations"
                "(reconciliation_id,model_run_version_ref,model_run_version_hash,verdict,"
                "record_json,content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    reconciliation_id, model_run_version_ref, model_run_version_hash,
                    verdict, canonical_json(wire), wire["content_hash"], actor_ref, created_at,
                ),
            )
            event = self._event(
                cur, event_type="reconciliation_committed",
                aggregate_ref=run["model_run_ref"],
                aggregate_version_ref=reconciliation_id,
                aggregate_hash=wire["content_hash"], actor_ref=actor_ref,
                idempotency_key=idempotency_key, created_at=created_at,
            )
            result = {"status": "fresh", "reconciliation": wire, "event": event}
            self._save_idem(
                cur, idempotency_key, "record_reconciliation", request_hash,
                result, created_at,
            )
            return result

    @staticmethod
    def _read_record(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
        if row is None:
            raise ModelInputNotFound(f"{name} was not found")
        wire = _canonical_record(row["record_json"], name)
        if wire["content_hash"] != row["content_hash"]:
            raise ModelInputConflict(f"{name} row hash drifted")
        return wire

    def candidate(self, candidate_id: str) -> dict[str, Any]:
        candidate_id = _text(candidate_id, "candidate_id")
        return self._read_record(self.connection.execute(
            "SELECT * FROM model_input_candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone(), "candidate")

    def decision(self, decision_id: str) -> dict[str, Any]:
        decision_id = _text(decision_id, "decision_id")
        return self._read_record(self.connection.execute(
            "SELECT * FROM model_input_decisions WHERE decision_id=?", (decision_id,)
        ).fetchone(), "decision")

    def input_version(self, version_id: str) -> dict[str, Any]:
        version_id = _text(version_id, "version_id")
        return self._read_record(self.connection.execute(
            "SELECT * FROM model_input_versions WHERE version_id=?", (version_id,)
        ).fetchone(), "model input version")

    def current_input(self, model_input_ref: str) -> dict[str, Any]:
        model_input_ref = _text(model_input_ref, "model_input_ref")
        return self._read_record(self.connection.execute(
            "SELECT v.* FROM model_input_versions v JOIN model_input_pointer p "
            "ON p.version_id=v.version_id WHERE p.model_input_ref=?", (model_input_ref,)
        ).fetchone(), "current model input")

    def model_run(self, version_id: str) -> dict[str, Any]:
        version_id = _text(version_id, "version_id")
        return self._read_record(self.connection.execute(
            "SELECT * FROM model_run_versions WHERE version_id=?", (version_id,)
        ).fetchone(), "model run")

    def reconciliations(self, model_run_version_ref: str) -> list[dict[str, Any]]:
        model_run_version_ref = _text(model_run_version_ref, "model_run_version_ref")
        rows = self.connection.execute(
            "SELECT * FROM model_reconciliations WHERE model_run_version_ref=? "
            "ORDER BY created_at,reconciliation_id", (model_run_version_ref,),
        ).fetchall()
        return [self._read_record(row, "reconciliation") for row in rows]

    def integrity_report(self) -> dict[str, Any]:
        issues: list[str] = []
        tables = (
            "model_input_candidates", "model_input_decisions", "model_input_versions",
            "model_run_versions", "model_reconciliations", "model_input_events",
        )
        for table in tables:
            for row in self.connection.execute(f"SELECT * FROM {table}").fetchall():
                try:
                    wire = _canonical_record(row["record_json"], table)
                    if wire["content_hash"] != row["content_hash"]:
                        issues.append(f"{table}: row hash mismatch")
                except ModelInputLedgerError as exc:
                    issues.append(f"{table}: {exc}")
        for pointer_table, version_table, ref_field in (
            ("model_input_pointer", "model_input_versions", "model_input_ref"),
            ("model_run_pointer", "model_run_versions", "model_run_ref"),
        ):
            for pointer in self.connection.execute(f"SELECT * FROM {pointer_table}").fetchall():
                latest = self.connection.execute(
                    f"SELECT version_id,version_number,content_hash FROM {version_table} "
                    f"WHERE {ref_field}=? ORDER BY version_number DESC LIMIT 1",
                    (pointer[ref_field],),
                ).fetchone()
                if latest is None or any(
                    pointer[field] != latest[field]
                    for field in ("version_id", "version_number", "content_hash")
                ):
                    issues.append(f"{pointer_table}: pointer is not latest")
        foreign_keys = [tuple(row) for row in self.connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()]
        if foreign_keys:
            issues.append("foreign_key_check failed")
        return {
            "ok": not issues,
            "issues": issues,
            "foreign_key_violations": foreign_keys,
            "counts": {
                table: self.connection.execute(
                    f"SELECT COUNT(*) AS n FROM {table}"
                ).fetchone()["n"]
                for table in tables
            },
        }
