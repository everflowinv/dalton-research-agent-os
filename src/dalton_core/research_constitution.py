"""Versioned research constitution manifest authority.

A constitution is human-authored research method policy, never Evidence or a
Claim.  It binds the exact immutable versions of the authorities that already
govern research execution (mandate, driver pack, governance policy, optional
doctrine pack, optional weekly brief schedule plan) and only adds the method
those objects cannot express: which questions are worth researching, the
causal chain under study, source standards, falsification duties, materiality
mapping, lifecycle rules and an output rubric.  Publishing is human-only;
nothing in this module lets a model or automation principal propose, publish
or activate a constitution.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .coverage_admission import validate_driver_pack_version
from .store import DaltonStore, canonical_json, content_hash


SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("research_constitution_schema.sql")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HUMAN_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._/@:-]*$")

_BINDING_FIELDS = frozenset({
    "mandate_version", "driver_pack_version", "governance_policy_version",
    "doctrine_pack_version", "weekly_brief_plan",
})


class ResearchConstitutionError(RuntimeError):
    """Base error for the research constitution authority."""


class ResearchConstitutionValidationError(ResearchConstitutionError):
    """A request does not satisfy the closed contract."""


class ResearchConstitutionConflict(ResearchConstitutionError):
    """A request conflicts with immutable authority."""


class ResearchConstitutionNotFound(ResearchConstitutionError):
    """A bound mandate, pack, policy or constitution version is absent."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchConstitutionValidationError(f"{name} must be non-empty text")
    return value.strip()


def _human(value: Any, name: str = "actor_ref") -> str:
    value = _text(value, name)
    if _HUMAN_RE.fullmatch(value) is None:
        raise ResearchConstitutionValidationError(f"{name} must use the human: namespace")
    return value


def _sha256(value: Any, name: str) -> str:
    value = _text(value, name)
    if _SHA256_RE.fullmatch(value) is None:
        raise ResearchConstitutionValidationError(f"{name} must be a lowercase SHA-256")
    return value


def _texts(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ResearchConstitutionValidationError(f"{name} must be an array")
    result = [_text(item, f"{name}[]") for item in value]
    if nonempty and not result:
        raise ResearchConstitutionValidationError(f"{name} must not be empty")
    if len(set(result)) != len(result):
        raise ResearchConstitutionValidationError(f"{name} must contain unique values")
    return result


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ResearchConstitutionValidationError(f"{name} must be a positive integer")
    return value


def _binding(value: Any, name: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"ref", "hash"}:
        raise ResearchConstitutionValidationError(f"{name} must be an exact ref+hash binding")
    return {"ref": _text(value["ref"], f"{name}.ref"), "hash": _sha256(value["hash"], f"{name}.hash")}


def validate_constitution_method(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchConstitutionValidationError("method must be an object")
    method = dict(value)
    expected = {
        "question_admission", "causal_chain", "source_standards",
        "falsification", "materiality", "lifecycle", "output_rubric",
    }
    if set(method) != expected:
        raise ResearchConstitutionValidationError("method has an invalid closed shape")
    method["question_admission"] = _texts(method["question_admission"], "method.question_admission", nonempty=True)
    method["causal_chain"] = _texts(method["causal_chain"], "method.causal_chain", nonempty=True)

    standards = dict(method["source_standards"]) if isinstance(method["source_standards"], Mapping) else None
    if standards is None or set(standards) != {"hierarchy", "conflict_adjudication", "minimum_independent_sources"}:
        raise ResearchConstitutionValidationError("method.source_standards has an invalid closed shape")
    standards["hierarchy"] = _texts(standards["hierarchy"], "source_standards.hierarchy", nonempty=True)
    standards["conflict_adjudication"] = _texts(
        standards["conflict_adjudication"], "source_standards.conflict_adjudication", nonempty=True
    )
    standards["minimum_independent_sources"] = _positive_int(
        standards["minimum_independent_sources"], "source_standards.minimum_independent_sources"
    )
    method["source_standards"] = standards

    falsification = dict(method["falsification"]) if isinstance(method["falsification"], Mapping) else None
    if falsification is None or set(falsification) != {"required_falsifier_searches", "alternative_explanations"}:
        raise ResearchConstitutionValidationError("method.falsification has an invalid closed shape")
    falsification["required_falsifier_searches"] = _texts(
        falsification["required_falsifier_searches"], "falsification.required_falsifier_searches", nonempty=True
    )
    falsification["alternative_explanations"] = _texts(
        falsification["alternative_explanations"], "falsification.alternative_explanations", nonempty=True
    )
    method["falsification"] = falsification

    method["materiality"] = _texts(method["materiality"], "method.materiality", nonempty=True)

    lifecycle = dict(method["lifecycle"]) if isinstance(method["lifecycle"], Mapping) else None
    if lifecycle is None or set(lifecycle) != {"continue_when", "refresh_when", "stop_when", "escalate_when"}:
        raise ResearchConstitutionValidationError("method.lifecycle has an invalid closed shape")
    for field in ("continue_when", "refresh_when", "stop_when", "escalate_when"):
        lifecycle[field] = _texts(lifecycle[field], f"lifecycle.{field}", nonempty=True)
    method["lifecycle"] = lifecycle

    rubric = dict(method["output_rubric"]) if isinstance(method["output_rubric"], Mapping) else None
    if rubric is None or set(rubric) != {"criteria", "good_samples", "bad_samples"}:
        raise ResearchConstitutionValidationError("method.output_rubric has an invalid closed shape")
    rubric["criteria"] = _texts(rubric["criteria"], "output_rubric.criteria", nonempty=True)
    rubric["good_samples"] = _texts(rubric["good_samples"], "output_rubric.good_samples")
    rubric["bad_samples"] = _texts(rubric["bad_samples"], "output_rubric.bad_samples")
    method["output_rubric"] = rubric
    return method


def validate_research_constitution_version(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "schema_version", "id", "created_at", "constitution_ref", "version",
        "prior_version_ref", "industry_ref", "title", "bindings", "method",
        "actor_ref", "content_hash",
    }
    wire = dict(value)
    if set(wire) != fields or wire.get("schema_version") != SCHEMA_VERSION:
        raise ResearchConstitutionValidationError("research constitution has an invalid closed shape")
    for field in ("id", "created_at", "constitution_ref", "industry_ref", "title"):
        wire[field] = _text(wire[field], field)
    wire["actor_ref"] = _human(wire["actor_ref"])
    wire["content_hash"] = _sha256(wire["content_hash"], "content_hash")
    if type(wire["version"]) is not int or wire["version"] < 1:
        raise ResearchConstitutionValidationError("research constitution version must be positive")
    if wire["prior_version_ref"] is not None:
        wire["prior_version_ref"] = _text(wire["prior_version_ref"], "prior_version_ref")

    bindings = dict(wire["bindings"]) if isinstance(wire["bindings"], Mapping) else None
    if bindings is None or set(bindings) != _BINDING_FIELDS:
        raise ResearchConstitutionValidationError("research constitution bindings have an invalid closed shape")
    bindings["mandate_version"] = _binding(bindings["mandate_version"], "bindings.mandate_version")
    bindings["driver_pack_version"] = _binding(bindings["driver_pack_version"], "bindings.driver_pack_version")
    bindings["governance_policy_version"] = _binding(
        bindings["governance_policy_version"], "bindings.governance_policy_version"
    )
    for optional in ("doctrine_pack_version", "weekly_brief_plan"):
        if bindings[optional] is not None:
            bindings[optional] = _binding(bindings[optional], f"bindings.{optional}")
    wire["bindings"] = bindings

    wire["method"] = validate_constitution_method(wire["method"])
    base = dict(wire)
    expected_hash = base.pop("content_hash")
    if content_hash(base) != expected_hash:
        raise ResearchConstitutionValidationError("research constitution content_hash is invalid")
    return wire


def _canonical_record(raw: Any, name: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ResearchConstitutionConflict(f"{name} record is missing")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ResearchConstitutionConflict(f"{name} record is invalid") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise ResearchConstitutionConflict(f"{name} record is not canonical")
    return value


class ResearchConstitutionAuthority:
    """Publish and read immutable, human-only research constitution versions."""

    def __init__(self, store: DaltonStore):
        self.store = store
        self.connection = store.connection
        self._authorized = False
        self.connection.create_function(
            "dalton_research_constitution_authorized", 0, lambda: int(self._authorized)
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("ResearchConstitutionAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    @staticmethod
    def _request_hash(operation: str, request: Mapping[str, Any]) -> str:
        return content_hash({"operation": operation, "request": dict(request)})

    def _idem(self, cur: sqlite3.Cursor, key: str, operation: str, request_hash: str) -> dict[str, Any] | None:
        row = cur.execute(
            "SELECT * FROM research_constitution_idempotency WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_hash"] != request_hash:
            raise ResearchConstitutionConflict("idempotency key conflicts with prior request")
        result = json.loads(row["result_json"])
        return {**result, "status": "duplicate"}

    def _save_idem(
        self, cur: sqlite3.Cursor, key: str, operation: str, request_hash: str,
        result: Mapping[str, Any], created_at: str,
    ) -> None:
        cur.execute(
            "INSERT INTO research_constitution_idempotency"
            "(idempotency_key,operation,request_hash,result_json,created_at) VALUES(?,?,?,?,?)",
            (key, operation, request_hash, canonical_json(result), created_at),
        )

    def _validate_mandate_binding(self, cur: sqlite3.Cursor, binding: Mapping[str, str], industry_ref: str) -> None:
        row = cur.execute(
            "SELECT * FROM mandate_versions WHERE version_id=?", (binding["ref"],)
        ).fetchone()
        if row is None:
            raise ResearchConstitutionNotFound("mandate version was not found")
        wire = _canonical_record(row["record_json"], "mandate")
        base = dict(wire)
        asserted_hash = base.pop("content_hash", None)
        if (
            wire.get("id") != binding["ref"]
            or asserted_hash != row["content_hash"]
            or asserted_hash != binding["hash"]
            or content_hash(base) != asserted_hash
        ):
            raise ResearchConstitutionConflict("mandate binding failed")
        if industry_ref not in set(wire.get("scope_refs", [])):
            raise ResearchConstitutionConflict("mandate does not cover the constitution industry")
        now = _now()
        if (
            wire.get("effective_from") > now
            or (wire.get("effective_until") is not None and wire["effective_until"] <= now)
        ):
            raise ResearchConstitutionConflict("mandate is outside its effective window")
        pointer = cur.execute(
            "SELECT version_id,active FROM mandate_pointer WHERE mandate_ref=?",
            (wire["mandate_ref"],),
        ).fetchone()
        if pointer is None or pointer["version_id"] != binding["ref"] or int(pointer["active"]) != 1:
            raise ResearchConstitutionConflict("mandate is not the active version")

    def _validate_driver_pack_binding(self, cur: sqlite3.Cursor, binding: Mapping[str, str], industry_ref: str) -> None:
        row = cur.execute(
            "SELECT * FROM driver_pack_versions WHERE version_id=?", (binding["ref"],)
        ).fetchone()
        if row is None:
            raise ResearchConstitutionNotFound("driver pack version was not found")
        if row["content_hash"] != binding["hash"] or row["industry_ref"] != industry_ref:
            raise ResearchConstitutionConflict("driver pack binding failed")
        pointer = cur.execute(
            "SELECT version_id FROM driver_pack_pointer WHERE driver_pack_ref=?",
            (row["driver_pack_ref"],),
        ).fetchone()
        if pointer is None or pointer["version_id"] != binding["ref"]:
            raise ResearchConstitutionConflict("driver pack is not the active version")
        validate_driver_pack_version(_canonical_record(row["record_json"], "driver pack"))

    def _validate_policy_binding(self, cur: sqlite3.Cursor, binding: Mapping[str, str]) -> None:
        row = cur.execute(
            "SELECT policy_version_id,content_hash FROM governance_policy_versions "
            "WHERE policy_version_id=?",
            (binding["ref"],),
        ).fetchone()
        if row is None:
            raise ResearchConstitutionNotFound("governance policy version was not found")
        if row["content_hash"] != binding["hash"]:
            raise ResearchConstitutionConflict("governance policy binding failed")
        pointer = cur.execute(
            "SELECT policy_version_id FROM governance_policy_pointer WHERE pointer_id=1"
        ).fetchone()
        if pointer is None or pointer["policy_version_id"] != binding["ref"]:
            raise ResearchConstitutionConflict("governance policy is not the active version")

    def _validate_doctrine_binding(self, cur: sqlite3.Cursor, binding: Mapping[str, str]) -> None:
        table = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='doctrine_pack_versions'"
        ).fetchone()
        if table is None:
            raise ResearchConstitutionNotFound("doctrine authority is not open on this Core")
        row = cur.execute(
            "SELECT version_id,content_hash FROM doctrine_pack_versions WHERE version_id=?",
            (binding["ref"],),
        ).fetchone()
        if row is None:
            raise ResearchConstitutionNotFound("doctrine pack version was not found")
        if row["content_hash"] != binding["hash"]:
            raise ResearchConstitutionConflict("doctrine pack binding failed")
        latest = cur.execute(
            "SELECT version_id FROM doctrine_pack_versions "
            "WHERE doctrine_pack_ref=(SELECT doctrine_pack_ref FROM doctrine_pack_versions WHERE version_id=?) "
            "ORDER BY version_number DESC LIMIT 1",
            (binding["ref"],),
        ).fetchone()
        if latest is None or latest["version_id"] != binding["ref"]:
            raise ResearchConstitutionConflict("doctrine pack is not the latest version")

    def publish_constitution(
        self,
        constitution_ref: str,
        *,
        industry_ref: str,
        title: str,
        bindings: Mapping[str, Any],
        method: Mapping[str, Any],
        actor_ref: str,
        version_id: str,
        prior_version_ref: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        constitution_ref = _text(constitution_ref, "constitution_ref")
        industry_ref = _text(industry_ref, "industry_ref")
        title = _text(title, "title")
        actor_ref = _human(actor_ref)
        version_id = _text(version_id, "version_id")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        if prior_version_ref is not None:
            prior_version_ref = _text(prior_version_ref, "prior_version_ref")
        if not isinstance(bindings, Mapping) or set(bindings) != _BINDING_FIELDS:
            raise ResearchConstitutionValidationError("bindings have an invalid closed shape")
        bindings_wire = {
            "mandate_version": _binding(bindings["mandate_version"], "bindings.mandate_version"),
            "driver_pack_version": _binding(bindings["driver_pack_version"], "bindings.driver_pack_version"),
            "governance_policy_version": _binding(
                bindings["governance_policy_version"], "bindings.governance_policy_version"
            ),
            "doctrine_pack_version": (
                None if bindings["doctrine_pack_version"] is None
                else _binding(bindings["doctrine_pack_version"], "bindings.doctrine_pack_version")
            ),
            "weekly_brief_plan": (
                None if bindings["weekly_brief_plan"] is None
                else _binding(bindings["weekly_brief_plan"], "bindings.weekly_brief_plan")
            ),
        }
        method_wire = validate_constitution_method(method)
        request = {
            "constitution_ref": constitution_ref,
            "industry_ref": industry_ref,
            "title": title,
            "bindings": bindings_wire,
            "method": method_wire,
            "actor_ref": actor_ref,
            "version_id": version_id,
            "prior_version_ref": prior_version_ref,
        }
        request_hash = self._request_hash("publish_constitution", request)
        with self._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "publish_constitution", request_hash)
            if duplicate is not None:
                return duplicate
            latest = cur.execute(
                "SELECT constitution_version_id,version_number FROM research_constitution_versions "
                "WHERE constitution_ref=? ORDER BY version_number DESC LIMIT 1",
                (constitution_ref,),
            ).fetchone()
            if latest is None:
                if prior_version_ref is not None:
                    raise ResearchConstitutionConflict("first constitution cannot have a prior version")
                version = 1
            else:
                if prior_version_ref != latest["constitution_version_id"]:
                    raise ResearchConstitutionConflict("constitution must continue the latest version")
                version = int(latest["version_number"]) + 1
            if cur.execute(
                "SELECT 1 FROM research_constitution_versions WHERE constitution_version_id=?",
                (version_id,),
            ).fetchone():
                raise ResearchConstitutionConflict("constitution version id already exists")
            self._validate_mandate_binding(cur, bindings_wire["mandate_version"], industry_ref)
            self._validate_driver_pack_binding(cur, bindings_wire["driver_pack_version"], industry_ref)
            self._validate_policy_binding(cur, bindings_wire["governance_policy_version"])
            if bindings_wire["doctrine_pack_version"] is not None:
                self._validate_doctrine_binding(cur, bindings_wire["doctrine_pack_version"])
            created_at = _now()
            record = {
                "schema_version": SCHEMA_VERSION,
                "id": version_id,
                "created_at": created_at,
                "constitution_ref": constitution_ref,
                "version": version,
                "prior_version_ref": prior_version_ref,
                "industry_ref": industry_ref,
                "title": title,
                "bindings": bindings_wire,
                "method": method_wire,
                "actor_ref": actor_ref,
            }
            wire = dict(record)
            wire["content_hash"] = content_hash(record)
            validate_research_constitution_version(wire)
            cur.execute(
                "INSERT INTO research_constitution_versions"
                "(constitution_version_id,constitution_ref,version_number,prior_version_id,industry_ref,"
                "record_json,content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    version_id, constitution_ref, version, prior_version_ref, industry_ref,
                    canonical_json(wire), wire["content_hash"], actor_ref, created_at,
                ),
            )
            cur.execute(
                "INSERT INTO research_constitution_pointer"
                "(constitution_ref,constitution_version_id,version_number,content_hash,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(constitution_ref) DO UPDATE SET "
                "constitution_version_id=excluded.constitution_version_id,"
                "version_number=excluded.version_number,"
                "content_hash=excluded.content_hash,updated_at=excluded.updated_at",
                (constitution_ref, version_id, version, wire["content_hash"], created_at),
            )
            result = {"status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "publish_constitution", request_hash, result, created_at)
            return result

    def constitution(self, version_id: str) -> dict[str, Any]:
        version_id = _text(version_id, "version_id")
        row = self.connection.execute(
            "SELECT * FROM research_constitution_versions WHERE constitution_version_id=?",
            (version_id,),
        ).fetchone()
        if row is None:
            raise ResearchConstitutionNotFound("research constitution version was not found")
        wire = validate_research_constitution_version(
            _canonical_record(row["record_json"], "research constitution")
        )
        if (
            wire["id"] != row["constitution_version_id"]
            or wire["constitution_ref"] != row["constitution_ref"]
            or wire["version"] != row["version_number"]
            or wire["prior_version_ref"] != row["prior_version_id"]
            or wire["industry_ref"] != row["industry_ref"]
            or wire["content_hash"] != row["content_hash"]
        ):
            raise ResearchConstitutionConflict("research constitution authority drifted")
        return wire

    def active_constitution(self, constitution_ref: str) -> dict[str, Any]:
        constitution_ref = _text(constitution_ref, "constitution_ref")
        pointer = self.connection.execute(
            "SELECT * FROM research_constitution_pointer WHERE constitution_ref=?",
            (constitution_ref,),
        ).fetchone()
        if pointer is None:
            raise ResearchConstitutionNotFound("research constitution pointer was not found")
        wire = self.constitution(pointer["constitution_version_id"])
        if (
            wire["version"] != pointer["version_number"]
            or wire["content_hash"] != pointer["content_hash"]
        ):
            raise ResearchConstitutionConflict("research constitution pointer drifted")
        return wire

    def constitution_report(self) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT p.constitution_ref, v.constitution_version_id, v.version_number, "
            "v.content_hash, v.actor_ref, v.created_at "
            "FROM research_constitution_pointer p "
            "JOIN research_constitution_versions v "
            "ON v.constitution_version_id=p.constitution_version_id "
            "ORDER BY p.constitution_ref"
        ).fetchall()
        constitutions = []
        for row in rows:
            wire = self.active_constitution(row["constitution_ref"])
            constitutions.append({
                "constitution_ref": row["constitution_ref"],
                "version_id": wire["id"],
                "version_number": wire["version"],
                "content_hash": wire["content_hash"],
                "industry_ref": wire["industry_ref"],
                "actor_ref": wire["actor_ref"],
                "created_at": wire["created_at"],
            })
        version_count = self.connection.execute(
            "SELECT COUNT(*) FROM research_constitution_versions"
        ).fetchone()[0]
        return {
            "projection_kind": "research_constitution_report",
            "constitution_count": len(constitutions),
            "version_count": version_count,
            "constitutions": constitutions,
        }


__all__ = [
    "ResearchConstitutionAuthority",
    "ResearchConstitutionConflict",
    "ResearchConstitutionError",
    "ResearchConstitutionNotFound",
    "ResearchConstitutionValidationError",
    "validate_constitution_method",
    "validate_research_constitution_version",
]
