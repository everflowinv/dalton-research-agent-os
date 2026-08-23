"""Versioned industry driver packs and human-only initial thesis admission.

Industry semantics live in immutable driver packs.  The Core stores only the
generic pack, mandate and admission bindings.  An admitted thesis is written
as ThesisVersion v0.2 with ordinal confidence and a human-admission authority;
no model or automation principal can manufacture the deciding authority.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .contracts import ThesisVersion
from .store import DaltonStore, canonical_json, content_hash


SCHEMA_VERSION = "0.1"
THESIS_SCHEMA_VERSION = "0.2"
CONFIDENCE_LEVELS = frozenset({"low", "medium", "high"})
VERIFICATION_KINDS = frozenset({"numeric", "semantic", "numeric_and_semantic"})
THESIS_CONTENT_FIELDS = frozenset({
    "statement", "mechanism", "confidence", "implied_expectation",
    "claim_refs", "catalyst_refs", "falsifier_refs", "change_reason",
})


class CoverageAdmissionError(RuntimeError):
    """Base error for driver-pack and thesis-admission authority."""


class CoverageAdmissionValidationError(CoverageAdmissionError):
    """A request does not satisfy the closed contract."""


class CoverageAdmissionConflict(CoverageAdmissionError):
    """A request conflicts with immutable authority."""


class CoverageAdmissionNotFound(CoverageAdmissionError):
    """A bound mandate, pack, candidate, claim or decision is absent."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CoverageAdmissionValidationError(f"{name} must be non-empty text")
    return value.strip()


def _sha256(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise CoverageAdmissionValidationError(f"{name} must be a lowercase SHA-256")
    return value


def _human(value: Any, name: str) -> str:
    value = _text(value, name)
    if not value.startswith("human:") or len(value) == len("human:"):
        raise CoverageAdmissionValidationError(f"{name} must use the human: namespace")
    return value


def _refs(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise CoverageAdmissionValidationError(f"{name} must be an array")
    refs = [_text(item, f"{name}[]") for item in value]
    if len(refs) != len(set(refs)):
        raise CoverageAdmissionValidationError(f"{name} must be unique")
    return refs


def _objects(value: Any, name: str, *, nonempty: bool = False) -> list[dict[str, Any]]:
    if not isinstance(value, list) or (nonempty and not value):
        raise CoverageAdmissionValidationError(f"{name} must be an object array")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise CoverageAdmissionValidationError(f"{name}[{index}] must be an object")
        result.append(dict(item))
    return result


def _record(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(value)
    wire["content_hash"] = content_hash(wire)
    return wire


def _canonical_record(raw: Any, name: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise CoverageAdmissionConflict(f"{name} record is missing")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CoverageAdmissionConflict(f"{name} record is invalid") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise CoverageAdmissionConflict(f"{name} record is not canonical")
    return value


def validate_driver_pack_version(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "schema_version", "id", "created_at", "driver_pack_ref", "version",
        "prior_version_ref", "industry_ref", "title", "drivers", "metric_specs",
        "thesis_templates", "actor_ref", "content_hash",
    }
    wire = dict(value)
    if set(wire) != fields or wire.get("schema_version") != SCHEMA_VERSION:
        raise CoverageAdmissionValidationError("driver pack has an invalid closed shape")
    for field in ("id", "created_at", "driver_pack_ref", "industry_ref", "title"):
        wire[field] = _text(wire[field], field)
    wire["actor_ref"] = _human(wire["actor_ref"], "actor_ref")
    wire["content_hash"] = _sha256(wire["content_hash"], "content_hash")
    if type(wire["version"]) is not int or wire["version"] < 1:
        raise CoverageAdmissionValidationError("driver pack version must be positive")
    if wire["prior_version_ref"] is not None:
        wire["prior_version_ref"] = _text(wire["prior_version_ref"], "prior_version_ref")

    metrics = _objects(wire["metric_specs"], "metric_specs", nonempty=True)
    metric_refs: set[str] = set()
    for metric in metrics:
        expected = {
            "metric_ref", "label", "definition", "unit", "periodicity",
            "preferred_source_refs", "verification_kind", "caveats",
        }
        if set(metric) != expected:
            raise CoverageAdmissionValidationError("metric spec has an invalid closed shape")
        for field in ("metric_ref", "label", "definition", "unit", "periodicity"):
            metric[field] = _text(metric[field], f"metric_specs.{field}")
        if metric["metric_ref"] in metric_refs:
            raise CoverageAdmissionValidationError("metric_ref must be unique")
        metric_refs.add(metric["metric_ref"])
        metric["preferred_source_refs"] = _refs(
            metric["preferred_source_refs"], "preferred_source_refs", nonempty=True
        )
        if metric["verification_kind"] not in VERIFICATION_KINDS:
            raise CoverageAdmissionValidationError("verification_kind is invalid")
        metric["caveats"] = _refs(metric["caveats"], "caveats")

    drivers = _objects(wire["drivers"], "drivers", nonempty=True)
    driver_refs: set[str] = set()
    for driver in drivers:
        if set(driver) != {"driver_ref", "label", "mechanism", "metric_refs"}:
            raise CoverageAdmissionValidationError("driver has an invalid closed shape")
        for field in ("driver_ref", "label", "mechanism"):
            driver[field] = _text(driver[field], f"drivers.{field}")
        if driver["driver_ref"] in driver_refs:
            raise CoverageAdmissionValidationError("driver_ref must be unique")
        driver_refs.add(driver["driver_ref"])
        driver["metric_refs"] = _refs(driver["metric_refs"], "driver.metric_refs", nonempty=True)
        if not set(driver["metric_refs"]) <= metric_refs:
            raise CoverageAdmissionValidationError("driver references an unknown metric")

    templates = _objects(wire["thesis_templates"], "thesis_templates", nonempty=True)
    template_refs: set[str] = set()
    for template in templates:
        expected = {
            "template_ref", "statement", "mechanism", "driver_refs",
            "implied_expectation", "falsifier_refs",
        }
        if set(template) != expected:
            raise CoverageAdmissionValidationError("thesis template has an invalid closed shape")
        for field in ("template_ref", "statement", "mechanism", "implied_expectation"):
            template[field] = _text(template[field], f"thesis_templates.{field}")
        if template["template_ref"] in template_refs:
            raise CoverageAdmissionValidationError("template_ref must be unique")
        template_refs.add(template["template_ref"])
        template["driver_refs"] = _refs(template["driver_refs"], "template.driver_refs", nonempty=True)
        template["falsifier_refs"] = _refs(
            template["falsifier_refs"], "template.falsifier_refs", nonempty=True
        )
        if not set(template["driver_refs"]) <= driver_refs:
            raise CoverageAdmissionValidationError("template references an unknown driver")

    wire["metric_specs"] = metrics
    wire["drivers"] = drivers
    wire["thesis_templates"] = templates
    base = dict(wire)
    expected_hash = base.pop("content_hash")
    if content_hash(base) != expected_hash:
        raise CoverageAdmissionValidationError("driver pack content_hash is invalid")
    return wire


def validate_thesis_content(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(value)
    if set(wire) != THESIS_CONTENT_FIELDS:
        raise CoverageAdmissionValidationError("thesis content has an invalid closed shape")
    for field in ("statement", "mechanism", "implied_expectation", "change_reason"):
        wire[field] = _text(wire[field], field)
    if wire["confidence"] not in CONFIDENCE_LEVELS:
        raise CoverageAdmissionValidationError("confidence must be low, medium, or high")
    for field in ("claim_refs", "catalyst_refs", "falsifier_refs"):
        wire[field] = _refs(wire[field], field)
    return wire


class CoverageAdmissionAuthority:
    """Read and write generic coverage governance through one DaltonStore."""

    def __init__(self, store: DaltonStore):
        self.store = store
        self.connection = store.connection

    @staticmethod
    def _request_hash(operation: str, request: Mapping[str, Any]) -> str:
        return content_hash({"operation": operation, "request": dict(request)})

    def _idem(
        self, cur: Any, key: str, operation: str, request_hash: str
    ) -> dict[str, Any] | None:
        row = cur.execute(
            "SELECT * FROM coverage_governance_idempotency WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_hash"] != request_hash:
            raise CoverageAdmissionConflict("idempotency key conflicts with prior request")
        result = json.loads(row["result_json"])
        return {**result, "status": "duplicate"}

    @staticmethod
    def _save_idem(
        cur: Any, key: str, operation: str, request_hash: str,
        result: Mapping[str, Any], created_at: str,
    ) -> None:
        cur.execute(
            "INSERT INTO coverage_governance_idempotency"
            "(idempotency_key,operation,request_hash,result_json,created_at) VALUES(?,?,?,?,?)",
            (key, operation, request_hash, canonical_json(result), created_at),
        )

    def register_driver_pack(
        self,
        driver_pack_ref: str,
        *,
        industry_ref: str,
        title: str,
        drivers: list[Mapping[str, Any]],
        metric_specs: list[Mapping[str, Any]],
        thesis_templates: list[Mapping[str, Any]],
        actor_ref: str,
        version_id: str,
        prior_version_ref: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        driver_pack_ref = _text(driver_pack_ref, "driver_pack_ref")
        industry_ref = _text(industry_ref, "industry_ref")
        title = _text(title, "title")
        actor_ref = _human(actor_ref, "actor_ref")
        version_id = _text(version_id, "version_id")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        if prior_version_ref is not None:
            prior_version_ref = _text(prior_version_ref, "prior_version_ref")
        drivers_wire = _objects(drivers, "drivers", nonempty=True)
        metrics_wire = _objects(metric_specs, "metric_specs", nonempty=True)
        templates_wire = _objects(
            thesis_templates, "thesis_templates", nonempty=True
        )
        request = {
            "driver_pack_ref": driver_pack_ref,
            "industry_ref": industry_ref,
            "title": title,
            "drivers": drivers_wire,
            "metric_specs": metrics_wire,
            "thesis_templates": templates_wire,
            "actor_ref": actor_ref,
            "version_id": version_id,
            "prior_version_ref": prior_version_ref,
        }
        request_hash = self._request_hash("register_driver_pack", request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "register_driver_pack", request_hash)
            if duplicate is not None:
                return duplicate
            latest = cur.execute(
                "SELECT version_id,version_number FROM driver_pack_versions "
                "WHERE driver_pack_ref=? ORDER BY version_number DESC LIMIT 1",
                (driver_pack_ref,),
            ).fetchone()
            if latest is None:
                if prior_version_ref is not None:
                    raise CoverageAdmissionConflict("first driver pack cannot have a prior version")
                version = 1
            else:
                if prior_version_ref != latest["version_id"]:
                    raise CoverageAdmissionConflict("driver pack must continue the latest version")
                version = int(latest["version_number"]) + 1
            if cur.execute(
                "SELECT 1 FROM driver_pack_versions WHERE version_id=?", (version_id,)
            ).fetchone():
                raise CoverageAdmissionConflict("driver pack version id already exists")
            created_at = _now()
            wire = validate_driver_pack_version(_record({
                "schema_version": SCHEMA_VERSION,
                "id": version_id,
                "created_at": created_at,
                "driver_pack_ref": driver_pack_ref,
                "version": version,
                "prior_version_ref": prior_version_ref,
                "industry_ref": industry_ref,
                "title": title,
                "drivers": request["drivers"],
                "metric_specs": request["metric_specs"],
                "thesis_templates": request["thesis_templates"],
                "actor_ref": actor_ref,
            }))
            cur.execute(
                "INSERT INTO driver_pack_versions"
                "(version_id,driver_pack_ref,version_number,prior_version_id,industry_ref,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    version_id, driver_pack_ref, version, prior_version_ref, industry_ref,
                    canonical_json(wire), wire["content_hash"], actor_ref, created_at,
                ),
            )
            cur.execute(
                "INSERT INTO driver_pack_pointer(driver_pack_ref,version_id) VALUES(?,?) "
                "ON CONFLICT(driver_pack_ref) DO UPDATE SET version_id=excluded.version_id",
                (driver_pack_ref, version_id),
            )
            result = {"status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "register_driver_pack", request_hash, result, created_at)
            return result

    def driver_pack(self, version_id: str) -> dict[str, Any]:
        version_id = _text(version_id, "version_id")
        row = self.connection.execute(
            "SELECT * FROM driver_pack_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise CoverageAdmissionNotFound("driver pack version was not found")
        wire = validate_driver_pack_version(
            _canonical_record(row["record_json"], "driver pack")
        )
        if (
            wire["id"] != row["version_id"]
            or wire["driver_pack_ref"] != row["driver_pack_ref"]
            or wire["version"] != row["version_number"]
            or wire["prior_version_ref"] != row["prior_version_id"]
            or wire["industry_ref"] != row["industry_ref"]
            or wire["content_hash"] != row["content_hash"]
        ):
            raise CoverageAdmissionConflict("driver pack authority drifted")
        return wire

    def _mandate(self, cur: Any, version_id: str) -> dict[str, Any]:
        row = cur.execute(
            "SELECT * FROM mandate_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise CoverageAdmissionNotFound("mandate version was not found")
        wire = _canonical_record(row["record_json"], "mandate")
        base = dict(wire)
        asserted_hash = base.pop("content_hash", None)
        if (
            wire.get("id") != version_id
            or asserted_hash != row["content_hash"]
            or content_hash(base) != asserted_hash
        ):
            raise CoverageAdmissionConflict("mandate authority drifted")
        return wire

    def propose_thesis_admission(
        self,
        *,
        candidate_id: str,
        thesis_ref: str,
        company_ref: str,
        industry_ref: str,
        template_ref: str,
        driver_refs: list[str],
        mandate_version_ref: str,
        mandate_version_hash: str,
        driver_pack_version_ref: str,
        driver_pack_version_hash: str,
        content: Mapping[str, Any],
        actor_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        candidate_id = _text(candidate_id, "candidate_id")
        thesis_ref = _text(thesis_ref, "thesis_ref")
        company_ref = _text(company_ref, "company_ref")
        industry_ref = _text(industry_ref, "industry_ref")
        template_ref = _text(template_ref, "template_ref")
        driver_refs_wire = _refs(driver_refs, "driver_refs", nonempty=True)
        mandate_version_ref = _text(mandate_version_ref, "mandate_version_ref")
        mandate_version_hash = _sha256(mandate_version_hash, "mandate_version_hash")
        driver_pack_version_ref = _text(driver_pack_version_ref, "driver_pack_version_ref")
        driver_pack_version_hash = _sha256(driver_pack_version_hash, "driver_pack_version_hash")
        thesis_content = validate_thesis_content(content)
        actor_ref = _human(actor_ref, "actor_ref")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        request = {
            "candidate_id": candidate_id,
            "thesis_ref": thesis_ref,
            "company_ref": company_ref,
            "industry_ref": industry_ref,
            "template_ref": template_ref,
            "driver_refs": driver_refs_wire,
            "mandate_version_ref": mandate_version_ref,
            "mandate_version_hash": mandate_version_hash,
            "driver_pack_version_ref": driver_pack_version_ref,
            "driver_pack_version_hash": driver_pack_version_hash,
            "content": thesis_content,
            "actor_ref": actor_ref,
        }
        request_hash = self._request_hash("propose_thesis_admission", request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "propose_thesis_admission", request_hash)
            if duplicate is not None:
                return duplicate
            if cur.execute(
                "SELECT 1 FROM current_pointers WHERE thesis_id=?", (thesis_ref,)
            ).fetchone():
                raise CoverageAdmissionConflict("v0.1 admission cannot revise an existing thesis")
            mandate = self._mandate(cur, mandate_version_ref)
            if mandate["content_hash"] != mandate_version_hash:
                raise CoverageAdmissionConflict("mandate hash binding failed")
            scopes = set(mandate.get("scope_refs", []))
            if {company_ref, industry_ref} - scopes:
                raise CoverageAdmissionConflict("mandate does not authorize company and industry")
            current_time = _now()
            if (
                mandate.get("effective_from") > current_time
                or (
                    mandate.get("effective_until") is not None
                    and mandate["effective_until"] <= current_time
                )
            ):
                raise CoverageAdmissionConflict("mandate is outside its effective window")
            mandate_pointer = cur.execute(
                "SELECT version_id,active FROM mandate_pointer WHERE mandate_ref=?",
                (mandate["mandate_ref"],),
            ).fetchone()
            if (
                mandate_pointer is None
                or mandate_pointer["version_id"] != mandate_version_ref
                or int(mandate_pointer["active"]) != 1
            ):
                raise CoverageAdmissionConflict("mandate is not the active version")
            pack_row = cur.execute(
                "SELECT * FROM driver_pack_versions WHERE version_id=?",
                (driver_pack_version_ref,),
            ).fetchone()
            if pack_row is None:
                raise CoverageAdmissionNotFound("driver pack version was not found")
            if (
                pack_row["content_hash"] != driver_pack_version_hash
                or pack_row["industry_ref"] != industry_ref
            ):
                raise CoverageAdmissionConflict("driver pack binding failed")
            pack_pointer = cur.execute(
                "SELECT version_id FROM driver_pack_pointer WHERE driver_pack_ref=?",
                (pack_row["driver_pack_ref"],),
            ).fetchone()
            if pack_pointer is None or pack_pointer["version_id"] != driver_pack_version_ref:
                raise CoverageAdmissionConflict("driver pack is not the active version")
            pack_wire = validate_driver_pack_version(
                _canonical_record(pack_row["record_json"], "driver pack")
            )
            template = next(
                (
                    item for item in pack_wire["thesis_templates"]
                    if item["template_ref"] == template_ref
                ),
                None,
            )
            if template is None:
                raise CoverageAdmissionConflict("candidate references an unknown thesis template")
            if not set(driver_refs_wire) <= set(template["driver_refs"]):
                raise CoverageAdmissionConflict("candidate drivers are not authorized by its template")
            if (
                not thesis_content["falsifier_refs"]
                or not set(thesis_content["falsifier_refs"])
                <= set(template["falsifier_refs"])
            ):
                raise CoverageAdmissionConflict("candidate falsifiers are not authorized by its template")
            if cur.execute(
                "SELECT 1 FROM thesis_admission_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone():
                raise CoverageAdmissionConflict("candidate id already exists")
            created_at = _now()
            thesis_content_hash = content_hash(thesis_content)
            wire = _record({
                "schema_version": SCHEMA_VERSION,
                "id": candidate_id,
                "created_at": created_at,
                "thesis_ref": thesis_ref,
                "company_ref": company_ref,
                "industry_ref": industry_ref,
                "template_ref": template_ref,
                "driver_refs": driver_refs_wire,
                "mandate_version_ref": mandate_version_ref,
                "mandate_version_hash": mandate_version_hash,
                "driver_pack_version_ref": driver_pack_version_ref,
                "driver_pack_version_hash": driver_pack_version_hash,
                "content": thesis_content,
                "thesis_content_hash": thesis_content_hash,
                "proposed_by_ref": actor_ref,
            })
            cur.execute(
                "INSERT INTO thesis_admission_candidates"
                "(candidate_id,thesis_ref,company_ref,industry_ref,mandate_version_ref,mandate_version_hash,"
                "driver_pack_version_ref,driver_pack_version_hash,content_json,content_hash,record_json,record_hash,proposed_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    candidate_id, thesis_ref, company_ref, industry_ref,
                    mandate_version_ref, mandate_version_hash, driver_pack_version_ref,
                    driver_pack_version_hash, canonical_json(thesis_content), thesis_content_hash,
                    canonical_json(wire), wire["content_hash"], actor_ref, created_at,
                ),
            )
            result = {"status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "propose_thesis_admission", request_hash, result, created_at)
            return result

    def candidate(self, candidate_id: str) -> dict[str, Any]:
        candidate_id = _text(candidate_id, "candidate_id")
        row = self.connection.execute(
            "SELECT * FROM thesis_admission_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise CoverageAdmissionNotFound("thesis admission candidate was not found")
        wire = _canonical_record(row["record_json"], "thesis admission candidate")
        base = dict(wire)
        record_hash = base.pop("content_hash", None)
        if (
            wire.get("id") != row["candidate_id"]
            or wire.get("thesis_ref") != row["thesis_ref"]
            or wire.get("thesis_content_hash") != row["content_hash"]
            or record_hash != row["record_hash"]
            or content_hash(base) != record_hash
            or content_hash(wire.get("content")) != row["content_hash"]
        ):
            raise CoverageAdmissionConflict("thesis admission candidate authority drifted")
        validate_thesis_content(wire["content"])
        return wire

    def decide_thesis_admission(
        self,
        *,
        candidate_id: str,
        candidate_hash: str,
        verdict: str,
        rationale: str,
        decision_id: str,
        actor_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        candidate_id = _text(candidate_id, "candidate_id")
        candidate_hash = _sha256(candidate_hash, "candidate_hash")
        if verdict not in {"admit", "reject"}:
            raise CoverageAdmissionValidationError("verdict must be admit or reject")
        rationale = _text(rationale, "rationale")
        decision_id = _text(decision_id, "decision_id")
        actor_ref = _human(actor_ref, "actor_ref")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        request = {
            "candidate_id": candidate_id,
            "candidate_hash": candidate_hash,
            "verdict": verdict,
            "rationale": rationale,
            "decision_id": decision_id,
            "actor_ref": actor_ref,
        }
        request_hash = self._request_hash("decide_thesis_admission", request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "decide_thesis_admission", request_hash)
            if duplicate is not None:
                return duplicate
            candidate_row = cur.execute(
                "SELECT * FROM thesis_admission_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if candidate_row is None:
                raise CoverageAdmissionNotFound("thesis admission candidate was not found")
            if candidate_row["record_hash"] != candidate_hash:
                raise CoverageAdmissionConflict("candidate hash binding failed")
            if cur.execute(
                "SELECT 1 FROM thesis_admission_decisions WHERE candidate_id=? OR decision_id=?",
                (candidate_id, decision_id),
            ).fetchone():
                raise CoverageAdmissionConflict("candidate already has a decision")
            candidate = _canonical_record(candidate_row["record_json"], "candidate")
            candidate_base = dict(candidate)
            candidate_record_hash = candidate_base.pop("content_hash", None)
            if (
                candidate.get("id") != candidate_row["candidate_id"]
                or candidate.get("thesis_ref") != candidate_row["thesis_ref"]
                or candidate.get("thesis_content_hash") != candidate_row["content_hash"]
                or candidate_record_hash != candidate_row["record_hash"]
                or content_hash(candidate_base) != candidate_record_hash
                or content_hash(candidate.get("content")) != candidate_row["content_hash"]
            ):
                raise CoverageAdmissionConflict("candidate authority drifted")
            content = validate_thesis_content(candidate["content"])
            thesis_version_id: str | None = None
            decision_time = _now()
            if verdict == "admit":
                if cur.execute(
                    "SELECT 1 FROM current_pointers WHERE thesis_id=?",
                    (candidate_row["thesis_ref"],),
                ).fetchone():
                    raise CoverageAdmissionConflict("v0.1 admission cannot revise an existing thesis")
                mandate = self._mandate(cur, candidate_row["mandate_version_ref"])
                pointer = cur.execute(
                    "SELECT version_id,active FROM mandate_pointer WHERE mandate_ref=?",
                    (mandate["mandate_ref"],),
                ).fetchone()
                if (
                    mandate["content_hash"] != candidate_row["mandate_version_hash"]
                    or pointer is None
                    or pointer["version_id"] != mandate["id"]
                    or int(pointer["active"]) != 1
                    or mandate.get("effective_from") > decision_time
                    or (
                        mandate.get("effective_until") is not None
                        and mandate["effective_until"] <= decision_time
                    )
                ):
                    raise CoverageAdmissionConflict("candidate mandate is no longer active")
                pack = cur.execute(
                    "SELECT v.*,p.version_id AS active_version_id FROM driver_pack_versions v "
                    "JOIN driver_pack_pointer p ON p.driver_pack_ref=v.driver_pack_ref "
                    "WHERE v.version_id=?",
                    (candidate_row["driver_pack_version_ref"],),
                ).fetchone()
                if (
                    pack is None
                    or pack["content_hash"] != candidate_row["driver_pack_version_hash"]
                    or pack["active_version_id"] != pack["version_id"]
                ):
                    raise CoverageAdmissionConflict("candidate driver pack is no longer active")
                pack_wire = validate_driver_pack_version(
                    _canonical_record(pack["record_json"], "driver pack")
                )
                if (
                    pack_wire["id"] != pack["version_id"]
                    or pack_wire["content_hash"] != pack["content_hash"]
                ):
                    raise CoverageAdmissionConflict("candidate driver pack authority drifted")
                for claim_ref in content["claim_refs"]:
                    if not cur.execute(
                        "SELECT 1 FROM claim_versions WHERE claim_version_id=?", (claim_ref,)
                    ).fetchone():
                        raise CoverageAdmissionNotFound("thesis ClaimVersion was not found")
                    if not cur.execute(
                        "SELECT 1 FROM evidence_relations WHERE claim_version_id=? LIMIT 1",
                        (claim_ref,),
                    ).fetchone():
                        raise CoverageAdmissionConflict("thesis ClaimVersion has no EvidenceRelation")
                thesis_version_id = f"thesis-version:{uuid.uuid4().hex}"
            created_at = decision_time
            decision = _record({
                "schema_version": SCHEMA_VERSION,
                "id": decision_id,
                "created_at": created_at,
                "candidate_ref": candidate_id,
                "candidate_hash": candidate_hash,
                "verdict": verdict,
                "rationale": rationale,
                "reviewer_ref": actor_ref,
                "resulting_thesis_version_ref": thesis_version_id,
            })
            cur.execute(
                "INSERT INTO thesis_admission_decisions"
                "(decision_id,candidate_id,candidate_hash,verdict,rationale,reviewer_ref,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    decision_id, candidate_id, candidate_hash, verdict, rationale,
                    actor_ref, canonical_json(decision), decision["content_hash"], created_at,
                ),
            )
            result: dict[str, Any] = {"status": "fresh", "decision": decision}
            if verdict == "admit":
                assert thesis_version_id is not None
                thesis_wire = ThesisVersion.from_dict({
                    "schema_version": THESIS_SCHEMA_VERSION,
                    "id": thesis_version_id,
                    "created_at": created_at,
                    "thesis_ref": candidate_row["thesis_ref"],
                    "version": 1,
                    **content,
                    "prior_version_ref": None,
                    "authority_kind": "human_admission",
                    "authority_ref": decision_id,
                    "committed_by_ref": actor_ref,
                    "content_hash": candidate_row["content_hash"],
                }).to_dict()
                cur.execute(
                    "INSERT INTO thesis_versions"
                    "(version_id,thesis_id,version_number,content_json,content_hash,prior_version_id,change_id,verification_id,admission_decision_id,authority_kind,authority_ref,committed_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        thesis_version_id, candidate_row["thesis_ref"], 1,
                        canonical_json(thesis_wire), candidate_row["content_hash"], None,
                        None, None, decision_id, "human_admission", decision_id,
                        actor_ref, created_at,
                    ),
                )
                event_id = self.store._insert_event(
                    cur,
                    "admitted",
                    "thesis",
                    candidate_row["thesis_ref"],
                    {
                        "version_id": thesis_version_id,
                        "candidate_ref": candidate_id,
                        "candidate_hash": candidate_hash,
                        "decision_ref": decision_id,
                        "driver_pack_version_ref": candidate_row["driver_pack_version_ref"],
                        "template_ref": candidate["template_ref"],
                        "driver_refs": candidate["driver_refs"],
                        "mandate_version_ref": candidate_row["mandate_version_ref"],
                    },
                    version_id=thesis_version_id,
                    content_hash=candidate_row["content_hash"],
                    actor_id=actor_ref,
                    idempotency_key=idempotency_key,
                    correlation_id=candidate_id,
                )
                cur.execute(
                    "INSERT INTO current_pointers"
                    "(thesis_id,version_id,version_number,content_hash,updated_at) VALUES(?,?,?,?,?)",
                    (
                        candidate_row["thesis_ref"], thesis_version_id, 1,
                        candidate_row["content_hash"], created_at,
                    ),
                )
                result.update({
                    "thesis_version": thesis_wire,
                    "event_id": event_id,
                })
            self._save_idem(cur, idempotency_key, "decide_thesis_admission", request_hash, result, created_at)
            return result

    def decision(self, decision_id: str) -> dict[str, Any]:
        decision_id = _text(decision_id, "decision_id")
        row = self.connection.execute(
            "SELECT * FROM thesis_admission_decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if row is None:
            raise CoverageAdmissionNotFound("thesis admission decision was not found")
        wire = _canonical_record(row["record_json"], "thesis admission decision")
        base = dict(wire)
        saved_hash = base.pop("content_hash", None)
        if (
            wire.get("id") != row["decision_id"]
            or wire.get("candidate_ref") != row["candidate_id"]
            or wire.get("candidate_hash") != row["candidate_hash"]
            or saved_hash != row["content_hash"]
            or content_hash(base) != saved_hash
        ):
            raise CoverageAdmissionConflict("thesis admission decision authority drifted")
        return wire


__all__ = [
    "CONFIDENCE_LEVELS",
    "CoverageAdmissionAuthority",
    "CoverageAdmissionConflict",
    "CoverageAdmissionError",
    "CoverageAdmissionNotFound",
    "CoverageAdmissionValidationError",
    "validate_driver_pack_version",
    "validate_thesis_content",
]
