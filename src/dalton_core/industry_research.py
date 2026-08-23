"""Versioned industry evidence packs and reusable company overlays.

The authority does not create Evidence, Claims, or model inputs.  It only
publishes immutable research views over exact formal Ledger versions.  A
company overlay therefore cannot smuggle an unreviewed fact into an industry
driver or create a thesis through a side channel.
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
COMPARABILITY_TIERS = frozenset({"core", "adjacent", "reference"})
DEBATE_STATUSES = frozenset({"open", "resolved"})
DRIVER_STANCES = frozenset({"positive", "neutral", "negative", "mixed", "unknown"})
METRIC_COVERAGE_STATUSES = frozenset({
    "observed", "not_found_in_reviewed_sources", "not_comparable", "not_applicable",
})
POSITION_STANCES = frozenset({"supports", "against", "qualifies"})
_SCHEMA_PATH = Path(__file__).with_name("industry_research_schema.sql")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class IndustryResearchError(RuntimeError):
    """Base error for industry research publication."""


class IndustryResearchValidationError(IndustryResearchError):
    """A request does not satisfy the closed v1 contract."""


class IndustryResearchConflict(IndustryResearchError):
    """A request conflicts with immutable authority."""


class IndustryResearchNotFound(IndustryResearchError):
    """A referenced authority is absent."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IndustryResearchValidationError(f"{name} must be non-empty text")
    return value.strip()


def _human(value: Any, name: str) -> str:
    value = _text(value, name)
    if not value.startswith("human:") or value == "human:":
        raise IndustryResearchValidationError(f"{name} must use the human: namespace")
    return value


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise IndustryResearchValidationError(f"{name} must be a lowercase SHA-256")
    return value


def _rfc3339(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IndustryResearchValidationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise IndustryResearchValidationError(f"{name} must include timezone")
    return value


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise IndustryResearchValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) != fields:
        raise IndustryResearchValidationError(f"{name} has an invalid closed shape")
    return wire


def _objects(value: Any, name: str, *, nonempty: bool = False) -> list[dict[str, Any]]:
    if not isinstance(value, list) or (nonempty and not value):
        raise IndustryResearchValidationError(f"{name} must be an object array")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise IndustryResearchValidationError(f"{name}[{index}] must be an object")
        result.append(dict(item))
    return result


def _strings(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise IndustryResearchValidationError(f"{name} must be an array")
    result = [_text(item, f"{name}[]") for item in value]
    if len(result) != len(set(result)):
        raise IndustryResearchValidationError(f"{name} must be unique")
    return result


def _ref_hash(value: Any, name: str) -> dict[str, str]:
    wire = _closed(value, {"ref", "hash"}, name)
    return {"ref": _text(wire["ref"], f"{name}.ref"), "hash": _hash(wire["hash"], f"{name}.hash")}


def _ref_hashes(value: Any, name: str, *, nonempty: bool = False) -> list[dict[str, str]]:
    if not isinstance(value, list) or (nonempty and not value):
        raise IndustryResearchValidationError(f"{name} must be an object array")
    result = [_ref_hash(item, f"{name}[{index}]") for index, item in enumerate(value)]
    if len(result) != len({item["ref"] for item in result}):
        raise IndustryResearchValidationError(f"{name} refs must be unique")
    return result


def _record(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(value)
    wire["content_hash"] = content_hash(wire)
    return wire


def _canonical_record(raw: Any, name: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise IndustryResearchConflict(f"{name} record is missing")
    try:
        wire = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IndustryResearchConflict(f"{name} record is invalid JSON") from exc
    if not isinstance(wire, dict) or canonical_json(wire) != raw:
        raise IndustryResearchConflict(f"{name} record is not canonical")
    base = dict(wire)
    declared = base.pop("content_hash", None)
    if declared is None or content_hash(base) != declared:
        raise IndustryResearchConflict(f"{name} record hash drifted")
    return wire


def _boundary(value: Any) -> dict[str, Any]:
    wire = _closed(value, {"definition", "inclusion_rules", "exclusion_rules"}, "boundary")
    wire["definition"] = _text(wire["definition"], "boundary.definition")
    wire["inclusion_rules"] = _strings(wire["inclusion_rules"], "boundary.inclusion_rules", nonempty=True)
    wire["exclusion_rules"] = _strings(wire["exclusion_rules"], "boundary.exclusion_rules", nonempty=True)
    return wire


def _coverage_universe(value: Any) -> list[dict[str, Any]]:
    rows = _objects(value, "coverage_universe", nonempty=True)
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        wire = _closed(row, {"company_ref", "ticker", "role", "comparability_tier"}, f"coverage_universe[{index}]")
        for field in ("company_ref", "ticker", "role"):
            wire[field] = _text(wire[field], f"coverage_universe[{index}].{field}")
        if wire["comparability_tier"] not in COMPARABILITY_TIERS:
            raise IndustryResearchValidationError("coverage universe comparability_tier is invalid")
        result.append(wire)
    if len(result) != len({row["company_ref"] for row in result}):
        raise IndustryResearchValidationError("coverage universe company_ref must be unique")
    if len(result) != len({row["ticker"] for row in result}):
        raise IndustryResearchValidationError("coverage universe ticker must be unique")
    if not any(row["comparability_tier"] == "core" for row in result):
        raise IndustryResearchValidationError("coverage universe requires a core company")
    return result


def _evidence_bindings(value: Any) -> list[dict[str, Any]]:
    rows = _objects(value, "evidence_bindings", nonempty=True)
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        wire = _closed(
            row,
            {"binding_ref", "driver_ref", "metric_ref", "claim_version_ref", "claim_version_hash", "relation_refs"},
            f"evidence_bindings[{index}]",
        )
        for field in ("binding_ref", "driver_ref", "metric_ref", "claim_version_ref"):
            wire[field] = _text(wire[field], f"evidence_bindings[{index}].{field}")
        wire["claim_version_hash"] = _hash(wire["claim_version_hash"], "claim_version_hash")
        wire["relation_refs"] = _ref_hashes(wire["relation_refs"], "relation_refs", nonempty=True)
        result.append(wire)
    if len(result) != len({row["binding_ref"] for row in result}):
        raise IndustryResearchValidationError("evidence binding_ref must be unique")
    if len(result) != len({row["claim_version_ref"] for row in result}):
        raise IndustryResearchValidationError("a claim version can only bind one industry metric")
    return result


def _debates(value: Any) -> list[dict[str, Any]]:
    rows = _objects(value, "debates", nonempty=True)
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        wire = _closed(row, {"debate_ref", "question", "status", "positions"}, f"debates[{index}]")
        wire["debate_ref"] = _text(wire["debate_ref"], "debate_ref")
        wire["question"] = _text(wire["question"], "debate.question")
        if wire["status"] not in DEBATE_STATUSES:
            raise IndustryResearchValidationError("debate status is invalid")
        positions = _objects(wire["positions"], "debate.positions", nonempty=True)
        if len(positions) < 2:
            raise IndustryResearchValidationError("debate requires at least two positions")
        normalized: list[dict[str, Any]] = []
        for p_index, position in enumerate(positions):
            item = _closed(position, {"label", "stance", "claim_version_refs"}, f"positions[{p_index}]")
            item["label"] = _text(item["label"], "position.label")
            if item["stance"] not in POSITION_STANCES:
                raise IndustryResearchValidationError("debate position stance is invalid")
            item["claim_version_refs"] = _strings(item["claim_version_refs"], "position.claim_version_refs", nonempty=True)
            normalized.append(item)
        if len(normalized) != len({item["label"] for item in normalized}):
            raise IndustryResearchValidationError("debate position labels must be unique")
        wire["positions"] = normalized
        result.append(wire)
    if len(result) != len({row["debate_ref"] for row in result}):
        raise IndustryResearchValidationError("debate_ref must be unique")
    return result


def _source_plan(value: Any) -> list[dict[str, Any]]:
    rows = _objects(value, "source_plan", nonempty=True)
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        wire = _closed(row, {"source_ref", "purpose", "priority", "required"}, f"source_plan[{index}]")
        wire["source_ref"] = _text(wire["source_ref"], "source_ref")
        wire["purpose"] = _text(wire["purpose"], "source purpose")
        if type(wire["priority"]) is not int or not 1 <= wire["priority"] <= 5:
            raise IndustryResearchValidationError("source priority must be 1..5")
        if type(wire["required"]) is not bool:
            raise IndustryResearchValidationError("source required must be boolean")
        result.append(wire)
    if len(result) != len({row["source_ref"] for row in result}):
        raise IndustryResearchValidationError("source_ref must be unique")
    return result


def _report_contract(value: Any) -> dict[str, Any]:
    wire = _closed(value, {"industry_brief_sections", "company_difference_fields"}, "report_contract")
    wire["industry_brief_sections"] = _strings(wire["industry_brief_sections"], "industry_brief_sections", nonempty=True)
    wire["company_difference_fields"] = _strings(wire["company_difference_fields"], "company_difference_fields", nonempty=True)
    return wire


def _driver_views(value: Any) -> list[dict[str, Any]]:
    rows = _objects(value, "driver_views", nonempty=True)
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        wire = _closed(
            row,
            {
                "driver_ref", "stance", "claim_version_refs", "model_input_version_refs",
                "metric_coverage", "differentiators", "watchpoints",
            },
            f"driver_views[{index}]",
        )
        wire["driver_ref"] = _text(wire["driver_ref"], "driver_ref")
        if wire["stance"] not in DRIVER_STANCES:
            raise IndustryResearchValidationError("driver stance is invalid")
        wire["claim_version_refs"] = _ref_hashes(wire["claim_version_refs"], "claim_version_refs", nonempty=True)
        wire["model_input_version_refs"] = _ref_hashes(wire["model_input_version_refs"], "model_input_version_refs")
        coverage = _objects(wire["metric_coverage"], "metric_coverage", nonempty=True)
        normalized_coverage: list[dict[str, Any]] = []
        for coverage_index, coverage_row in enumerate(coverage):
            item = _closed(
                coverage_row,
                {"metric_ref", "status", "claim_version_refs", "rationale"},
                f"metric_coverage[{coverage_index}]",
            )
            item["metric_ref"] = _text(item["metric_ref"], "metric_coverage.metric_ref")
            if item["status"] not in METRIC_COVERAGE_STATUSES:
                raise IndustryResearchValidationError("metric coverage status is invalid")
            item["claim_version_refs"] = _strings(
                item["claim_version_refs"], "metric_coverage.claim_version_refs",
                nonempty=item["status"] == "observed",
            )
            if item["status"] != "observed" and item["claim_version_refs"]:
                raise IndustryResearchValidationError(
                    "only observed metric coverage can reference claims"
                )
            item["rationale"] = _text(item["rationale"], "metric_coverage.rationale")
            normalized_coverage.append(item)
        if len(normalized_coverage) != len({item["metric_ref"] for item in normalized_coverage}):
            raise IndustryResearchValidationError("metric coverage must contain unique metrics")
        wire["metric_coverage"] = normalized_coverage
        wire["differentiators"] = _strings(wire["differentiators"], "differentiators")
        wire["watchpoints"] = _strings(wire["watchpoints"], "watchpoints", nonempty=True)
        result.append(wire)
    if len(result) != len({row["driver_ref"] for row in result}):
        raise IndustryResearchValidationError("driver views must be unique")
    return result


class IndustryResearchAuthority:
    """Publish and verify immutable industry evidence packs and overlays."""

    def __init__(self, store: DaltonStore):
        self.store = store
        self.connection = store.connection
        self._authorized = False
        self.connection.create_function(
            "dalton_industry_research_authorized", 0, lambda: int(self._authorized)
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("IndustryResearchAuthority operation cannot be nested")
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
            "SELECT * FROM industry_research_idempotency WHERE idempotency_key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_hash"] != request_hash:
            raise IndustryResearchConflict("idempotency key conflicts with prior request")
        return {**json.loads(row["result_json"]), "status": "duplicate"}

    @staticmethod
    def _save_idem(cur: sqlite3.Cursor, key: str, operation: str, request_hash: str, result: Mapping[str, Any], created_at: str) -> None:
        cur.execute(
            "INSERT INTO industry_research_idempotency"
            "(idempotency_key,operation,request_hash,result_json,created_at) VALUES(?,?,?,?,?)",
            (key, operation, request_hash, canonical_json(result), created_at),
        )

    @staticmethod
    def _driver_pack(cur: sqlite3.Cursor, version_ref: str, version_hash: str) -> dict[str, Any]:
        row = cur.execute(
            "SELECT * FROM driver_pack_versions WHERE version_id=?", (version_ref,)
        ).fetchone()
        if row is None:
            raise IndustryResearchNotFound("driver pack version was not found")
        wire = validate_driver_pack_version(_canonical_record(row["record_json"], "driver pack"))
        if wire["content_hash"] != version_hash or row["content_hash"] != version_hash:
            raise IndustryResearchConflict("driver pack hash binding failed")
        return wire

    @staticmethod
    def _claim(cur: sqlite3.Cursor, version_ref: str, version_hash: str) -> dict[str, Any]:
        row = cur.execute(
            "SELECT * FROM claim_versions WHERE claim_version_id=?", (version_ref,)
        ).fetchone()
        if row is None:
            raise IndustryResearchNotFound("claim version was not found")
        if row["content_hash"] != version_hash:
            raise IndustryResearchConflict("claim version hash binding failed")
        try:
            wire = json.loads(row["claim_json"])
        except json.JSONDecodeError as exc:
            raise IndustryResearchConflict("claim version is invalid JSON") from exc
        if not isinstance(wire, dict) or wire.get("content_hash") != version_hash:
            raise IndustryResearchConflict("claim version record drifted")
        return wire

    @staticmethod
    def _relation(cur: sqlite3.Cursor, relation_ref: str, relation_hash: str, claim_version_ref: str) -> dict[str, Any]:
        row = cur.execute(
            "SELECT * FROM evidence_relations WHERE relation_id=?", (relation_ref,)
        ).fetchone()
        if row is None:
            raise IndustryResearchNotFound("evidence relation was not found")
        if row["content_hash"] != relation_hash or row["claim_version_id"] != claim_version_ref:
            raise IndustryResearchConflict("evidence relation binding failed")
        try:
            wire = json.loads(row["relation_json"])
        except json.JSONDecodeError as exc:
            raise IndustryResearchConflict("evidence relation is invalid JSON") from exc
        if not isinstance(wire, dict) or wire.get("content_hash") != relation_hash:
            raise IndustryResearchConflict("evidence relation record drifted")
        return wire

    @staticmethod
    def _model_input(cur: sqlite3.Cursor, version_ref: str, version_hash: str) -> dict[str, Any]:
        row = cur.execute(
            "SELECT * FROM model_input_versions WHERE version_id=?", (version_ref,)
        ).fetchone()
        if row is None:
            raise IndustryResearchNotFound("model input version was not found")
        if row["content_hash"] != version_hash:
            raise IndustryResearchConflict("model input hash binding failed")
        return _canonical_record(row["record_json"], "model input")

    def register_evidence_pack(
        self,
        evidence_pack_ref: str,
        *,
        industry_ref: str,
        title: str,
        as_of: str,
        boundary: Mapping[str, Any],
        coverage_universe: list[Mapping[str, Any]],
        driver_pack_version_ref: str,
        driver_pack_version_hash: str,
        evidence_bindings: list[Mapping[str, Any]],
        debates: list[Mapping[str, Any]],
        source_plan: list[Mapping[str, Any]],
        report_contract: Mapping[str, Any],
        actor_ref: str,
        version_id: str,
        prior_version_ref: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        request = {
            "evidence_pack_ref": _text(evidence_pack_ref, "evidence_pack_ref"),
            "industry_ref": _text(industry_ref, "industry_ref"),
            "title": _text(title, "title"),
            "as_of": _rfc3339(as_of, "as_of"),
            "boundary": _boundary(boundary),
            "coverage_universe": _coverage_universe(coverage_universe),
            "driver_pack_version_ref": _text(driver_pack_version_ref, "driver_pack_version_ref"),
            "driver_pack_version_hash": _hash(driver_pack_version_hash, "driver_pack_version_hash"),
            "evidence_bindings": _evidence_bindings(evidence_bindings),
            "debates": _debates(debates),
            "source_plan": _source_plan(source_plan),
            "report_contract": _report_contract(report_contract),
            "actor_ref": _human(actor_ref, "actor_ref"),
            "version_id": _text(version_id, "version_id"),
            "prior_version_ref": None if prior_version_ref is None else _text(prior_version_ref, "prior_version_ref"),
        }
        idempotency_key = _text(idempotency_key, "idempotency_key")
        request_hash = self._request_hash("register_evidence_pack", request)
        with self._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "register_evidence_pack", request_hash)
            if duplicate is not None:
                return duplicate
            pack = self._driver_pack(cur, request["driver_pack_version_ref"], request["driver_pack_version_hash"])
            if pack["industry_ref"] != request["industry_ref"]:
                raise IndustryResearchConflict("driver pack industry binding failed")
            driver_metrics: dict[str, set[str]] = {}
            for driver in pack["drivers"]:
                driver_metrics[driver["driver_ref"]] = set(driver["metric_refs"])
            coverage_refs = {row["company_ref"] for row in request["coverage_universe"]}
            bound_claims: dict[str, dict[str, Any]] = {}
            bound_drivers: set[str] = set()
            for binding in request["evidence_bindings"]:
                driver_ref = binding["driver_ref"]
                metric_ref = binding["metric_ref"]
                if driver_ref not in driver_metrics or metric_ref not in driver_metrics[driver_ref]:
                    raise IndustryResearchConflict("evidence binding is outside the driver pack")
                claim = self._claim(cur, binding["claim_version_ref"], binding["claim_version_hash"])
                if claim.get("metric_or_aspect") != metric_ref:
                    raise IndustryResearchConflict("claim metric does not match evidence binding")
                if claim.get("subject_ref") not in coverage_refs | {request["industry_ref"]}:
                    raise IndustryResearchConflict("claim subject is outside the coverage universe")
                for relation in binding["relation_refs"]:
                    self._relation(cur, relation["ref"], relation["hash"], binding["claim_version_ref"])
                bound_claims[binding["claim_version_ref"]] = claim
                bound_drivers.add(driver_ref)
            if bound_drivers != set(driver_metrics):
                raise IndustryResearchConflict("evidence pack must cover every industry driver")
            for debate in request["debates"]:
                for position in debate["positions"]:
                    if not set(position["claim_version_refs"]) <= set(bound_claims):
                        raise IndustryResearchConflict("debate references a claim outside the evidence pack")
            latest = cur.execute(
                "SELECT version_id,version_number FROM industry_evidence_pack_versions "
                "WHERE evidence_pack_ref=? ORDER BY version_number DESC LIMIT 1",
                (request["evidence_pack_ref"],),
            ).fetchone()
            if latest is None:
                if request["prior_version_ref"] is not None:
                    raise IndustryResearchConflict("first evidence pack cannot have a prior version")
                version = 1
            else:
                if request["prior_version_ref"] != latest["version_id"]:
                    raise IndustryResearchConflict("evidence pack must continue the latest version")
                version = int(latest["version_number"]) + 1
            if cur.execute(
                "SELECT 1 FROM industry_evidence_pack_versions WHERE version_id=?", (request["version_id"],)
            ).fetchone():
                raise IndustryResearchConflict("evidence pack version id already exists")
            created_at = _now()
            wire = _record({
                "schema_version": SCHEMA_VERSION,
                "id": request["version_id"],
                "created_at": created_at,
                "evidence_pack_ref": request["evidence_pack_ref"],
                "version": version,
                "prior_version_ref": request["prior_version_ref"],
                "industry_ref": request["industry_ref"],
                "title": request["title"],
                "as_of": request["as_of"],
                "boundary": request["boundary"],
                "coverage_universe": request["coverage_universe"],
                "driver_pack_version_ref": request["driver_pack_version_ref"],
                "driver_pack_version_hash": request["driver_pack_version_hash"],
                "evidence_bindings": request["evidence_bindings"],
                "debates": request["debates"],
                "source_plan": request["source_plan"],
                "report_contract": request["report_contract"],
                "actor_ref": request["actor_ref"],
            })
            cur.execute(
                "INSERT INTO industry_evidence_pack_versions"
                "(version_id,evidence_pack_ref,version_number,prior_version_id,industry_ref,driver_pack_version_ref,driver_pack_version_hash,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    wire["id"], wire["evidence_pack_ref"], wire["version"], wire["prior_version_ref"],
                    wire["industry_ref"], wire["driver_pack_version_ref"], wire["driver_pack_version_hash"],
                    canonical_json(wire), wire["content_hash"], wire["actor_ref"], created_at,
                ),
            )
            cur.execute(
                "INSERT INTO industry_evidence_pack_pointer(evidence_pack_ref,version_id,version_number,content_hash,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(evidence_pack_ref) DO UPDATE SET "
                "version_id=excluded.version_id,version_number=excluded.version_number,content_hash=excluded.content_hash,updated_at=excluded.updated_at",
                (wire["evidence_pack_ref"], wire["id"], wire["version"], wire["content_hash"], created_at),
            )
            result = {"status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "register_evidence_pack", request_hash, result, created_at)
            return result

    def register_company_overlay(
        self,
        overlay_ref: str,
        *,
        company_ref: str,
        industry_ref: str,
        title: str,
        as_of: str,
        role: str,
        evidence_pack_version_ref: str,
        evidence_pack_version_hash: str,
        driver_views: list[Mapping[str, Any]],
        key_differences: list[str],
        open_questions: list[str],
        falsifier_refs: list[str],
        thesis_candidate_refs: list[str],
        actor_ref: str,
        version_id: str,
        prior_version_ref: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        request = {
            "overlay_ref": _text(overlay_ref, "overlay_ref"),
            "company_ref": _text(company_ref, "company_ref"),
            "industry_ref": _text(industry_ref, "industry_ref"),
            "title": _text(title, "title"),
            "as_of": _rfc3339(as_of, "as_of"),
            "role": _text(role, "role"),
            "evidence_pack_version_ref": _text(evidence_pack_version_ref, "evidence_pack_version_ref"),
            "evidence_pack_version_hash": _hash(evidence_pack_version_hash, "evidence_pack_version_hash"),
            "driver_views": _driver_views(driver_views),
            "key_differences": _strings(key_differences, "key_differences", nonempty=True),
            "open_questions": _strings(open_questions, "open_questions", nonempty=True),
            "falsifier_refs": _strings(falsifier_refs, "falsifier_refs", nonempty=True),
            "thesis_candidate_refs": _strings(thesis_candidate_refs, "thesis_candidate_refs"),
            "actor_ref": _human(actor_ref, "actor_ref"),
            "version_id": _text(version_id, "version_id"),
            "prior_version_ref": None if prior_version_ref is None else _text(prior_version_ref, "prior_version_ref"),
        }
        idempotency_key = _text(idempotency_key, "idempotency_key")
        request_hash = self._request_hash("register_company_overlay", request)
        with self._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "register_company_overlay", request_hash)
            if duplicate is not None:
                return duplicate
            row = cur.execute(
                "SELECT * FROM industry_evidence_pack_versions WHERE version_id=?",
                (request["evidence_pack_version_ref"],),
            ).fetchone()
            if row is None:
                raise IndustryResearchNotFound("industry evidence pack was not found")
            pack = _canonical_record(row["record_json"], "industry evidence pack")
            if row["content_hash"] != request["evidence_pack_version_hash"] or pack["content_hash"] != request["evidence_pack_version_hash"]:
                raise IndustryResearchConflict("industry evidence pack hash binding failed")
            if pack["industry_ref"] != request["industry_ref"]:
                raise IndustryResearchConflict("company overlay industry binding failed")
            company = next(
                (item for item in pack["coverage_universe"] if item["company_ref"] == request["company_ref"]), None
            )
            if company is None or company["role"] != request["role"]:
                raise IndustryResearchConflict("company overlay role is outside the coverage universe")
            driver_pack = self._driver_pack(cur, pack["driver_pack_version_ref"], pack["driver_pack_version_hash"])
            driver_metrics = {item["driver_ref"]: set(item["metric_refs"]) for item in driver_pack["drivers"]}
            if {item["driver_ref"] for item in request["driver_views"]} != set(driver_metrics):
                raise IndustryResearchConflict("company overlay must assess every industry driver")
            pack_claims = {
                item["claim_version_ref"]: item["claim_version_hash"] for item in pack["evidence_bindings"]
            }
            pack_claim_evidence: dict[str, set[str]] = {}
            for binding in pack["evidence_bindings"]:
                evidence_refs = pack_claim_evidence.setdefault(binding["claim_version_ref"], set())
                for relation_ref in binding["relation_refs"]:
                    relation_row = cur.execute(
                        "SELECT evidence_version_id FROM evidence_relations WHERE relation_id=?",
                        (relation_ref["ref"],),
                    ).fetchone()
                    if relation_row is None:
                        raise IndustryResearchNotFound("evidence relation was not found")
                    evidence_refs.add(relation_row["evidence_version_id"])
            for view in request["driver_views"]:
                coverage_by_metric = {
                    item["metric_ref"]: item for item in view["metric_coverage"]
                }
                if set(coverage_by_metric) != driver_metrics[view["driver_ref"]]:
                    raise IndustryResearchConflict(
                        "company overlay must state coverage for every driver metric"
                    )
                view_evidence_refs: set[str] = set()
                claim_metric_by_ref: dict[str, str] = {}
                for claim_ref in view["claim_version_refs"]:
                    if pack_claims.get(claim_ref["ref"]) != claim_ref["hash"]:
                        raise IndustryResearchConflict("overlay claim is outside the evidence pack")
                    claim = self._claim(cur, claim_ref["ref"], claim_ref["hash"])
                    if claim.get("subject_ref") != request["company_ref"]:
                        raise IndustryResearchConflict("overlay claim belongs to another subject")
                    if claim.get("metric_or_aspect") not in driver_metrics[view["driver_ref"]]:
                        raise IndustryResearchConflict("overlay claim metric belongs to another driver")
                    claim_metric_by_ref[claim_ref["ref"]] = claim["metric_or_aspect"]
                    view_evidence_refs.update(pack_claim_evidence[claim_ref["ref"]])
                covered_claim_refs: set[str] = set()
                for metric_ref, coverage in coverage_by_metric.items():
                    for claim_version_ref in coverage["claim_version_refs"]:
                        if claim_metric_by_ref.get(claim_version_ref) != metric_ref:
                            raise IndustryResearchConflict(
                                "metric coverage claim is missing or belongs to another metric"
                            )
                        covered_claim_refs.add(claim_version_ref)
                if covered_claim_refs != set(claim_metric_by_ref):
                    raise IndustryResearchConflict(
                        "every overlay claim must appear in observed metric coverage"
                    )
                for input_ref in view["model_input_version_refs"]:
                    model_input = self._model_input(cur, input_ref["ref"], input_ref["hash"])
                    payload = model_input.get("payload")
                    if model_input.get("input_kind") != "actual":
                        raise IndustryResearchConflict("company overlay only accepts actual model inputs")
                    if not isinstance(payload, dict) or payload.get("subject_ref") != request["company_ref"]:
                        raise IndustryResearchConflict("overlay model input belongs to another subject")
                    if payload.get("metric_ref") not in driver_metrics[view["driver_ref"]]:
                        raise IndustryResearchConflict("overlay model input belongs to another driver")
                    authorities = payload.get("source_authorities")
                    if not isinstance(authorities, list) or not any(
                        authority.get("authority_kind") == "evidence_version"
                        and authority.get("version_ref") in view_evidence_refs
                        for authority in authorities
                        if isinstance(authority, dict)
                    ):
                        raise IndustryResearchConflict(
                            "overlay model input is not sourced by its evidence-pack claims"
                        )
            latest = cur.execute(
                "SELECT version_id,version_number FROM company_overlay_versions "
                "WHERE overlay_ref=? ORDER BY version_number DESC LIMIT 1", (request["overlay_ref"],)
            ).fetchone()
            if latest is None:
                if request["prior_version_ref"] is not None:
                    raise IndustryResearchConflict("first company overlay cannot have a prior version")
                version = 1
            else:
                if request["prior_version_ref"] != latest["version_id"]:
                    raise IndustryResearchConflict("company overlay must continue the latest version")
                version = int(latest["version_number"]) + 1
            if cur.execute(
                "SELECT 1 FROM company_overlay_versions WHERE version_id=?", (request["version_id"],)
            ).fetchone():
                raise IndustryResearchConflict("company overlay version id already exists")
            created_at = _now()
            wire = _record({
                "schema_version": SCHEMA_VERSION,
                "id": request["version_id"],
                "created_at": created_at,
                "overlay_ref": request["overlay_ref"],
                "version": version,
                "prior_version_ref": request["prior_version_ref"],
                "company_ref": request["company_ref"],
                "industry_ref": request["industry_ref"],
                "title": request["title"],
                "as_of": request["as_of"],
                "role": request["role"],
                "evidence_pack_version_ref": request["evidence_pack_version_ref"],
                "evidence_pack_version_hash": request["evidence_pack_version_hash"],
                "driver_views": request["driver_views"],
                "key_differences": request["key_differences"],
                "open_questions": request["open_questions"],
                "falsifier_refs": request["falsifier_refs"],
                "thesis_candidate_refs": request["thesis_candidate_refs"],
                "actor_ref": request["actor_ref"],
            })
            cur.execute(
                "INSERT INTO company_overlay_versions"
                "(version_id,overlay_ref,version_number,prior_version_id,company_ref,industry_ref,evidence_pack_version_ref,evidence_pack_version_hash,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    wire["id"], wire["overlay_ref"], wire["version"], wire["prior_version_ref"],
                    wire["company_ref"], wire["industry_ref"], wire["evidence_pack_version_ref"],
                    wire["evidence_pack_version_hash"], canonical_json(wire), wire["content_hash"],
                    wire["actor_ref"], created_at,
                ),
            )
            cur.execute(
                "INSERT INTO company_overlay_pointer(overlay_ref,version_id,version_number,content_hash,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(overlay_ref) DO UPDATE SET "
                "version_id=excluded.version_id,version_number=excluded.version_number,content_hash=excluded.content_hash,updated_at=excluded.updated_at",
                (wire["overlay_ref"], wire["id"], wire["version"], wire["content_hash"], created_at),
            )
            result = {"status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "register_company_overlay", request_hash, result, created_at)
            return result

    def evidence_pack(self, version_id: str) -> dict[str, Any]:
        version_id = _text(version_id, "version_id")
        row = self.connection.execute(
            "SELECT * FROM industry_evidence_pack_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise IndustryResearchNotFound("industry evidence pack was not found")
        wire = _canonical_record(row["record_json"], "industry evidence pack")
        if wire["id"] != row["version_id"] or wire["content_hash"] != row["content_hash"]:
            raise IndustryResearchConflict("industry evidence pack row drifted")
        return wire

    def company_overlay(self, version_id: str) -> dict[str, Any]:
        version_id = _text(version_id, "version_id")
        row = self.connection.execute(
            "SELECT * FROM company_overlay_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise IndustryResearchNotFound("company overlay was not found")
        wire = _canonical_record(row["record_json"], "company overlay")
        if wire["id"] != row["version_id"] or wire["content_hash"] != row["content_hash"]:
            raise IndustryResearchConflict("company overlay row drifted")
        return wire

    def industry_brief_snapshot(
        self, evidence_pack_version_id: str, company_overlay_version_ids: list[str],
    ) -> dict[str, Any]:
        """Assemble one deterministic cross-company view from exact published versions."""

        pack = self.evidence_pack(evidence_pack_version_id)
        overlay_ids = _strings(
            company_overlay_version_ids, "company_overlay_version_ids", nonempty=True
        )
        overlays = [self.company_overlay(version_id) for version_id in overlay_ids]
        by_company: dict[str, dict[str, Any]] = {}
        for overlay in overlays:
            if (
                overlay["industry_ref"] != pack["industry_ref"]
                or overlay["evidence_pack_version_ref"] != pack["id"]
                or overlay["evidence_pack_version_hash"] != pack["content_hash"]
            ):
                raise IndustryResearchConflict(
                    "industry brief overlay is not bound to the evidence pack"
                )
            if overlay["company_ref"] in by_company:
                raise IndustryResearchConflict("industry brief contains duplicate companies")
            by_company[overlay["company_ref"]] = overlay
        coverage_refs = {item["company_ref"] for item in pack["coverage_universe"]}
        if set(by_company) != coverage_refs:
            raise IndustryResearchConflict(
                "industry brief requires one overlay for every coverage company"
            )

        driver_pack = self._driver_pack(
            self.connection, pack["driver_pack_version_ref"], pack["driver_pack_version_hash"]
        )
        views_by_company = {
            company_ref: {view["driver_ref"]: view for view in overlay["driver_views"]}
            for company_ref, overlay in by_company.items()
        }
        scoreboard = []
        metric_matrix = []
        for driver in driver_pack["drivers"]:
            driver_ref = driver["driver_ref"]
            companies = []
            for company in pack["coverage_universe"]:
                view = views_by_company[company["company_ref"]][driver_ref]
                observed = [
                    item["metric_ref"] for item in view["metric_coverage"]
                    if item["status"] == "observed"
                ]
                companies.append({
                    "company_ref": company["company_ref"], "ticker": company["ticker"],
                    "role": company["role"], "stance": view["stance"],
                    "observed_metric_refs": observed,
                    "unresolved_metric_refs": [
                        item["metric_ref"] for item in view["metric_coverage"]
                        if item["status"] != "observed"
                    ],
                })
            scoreboard.append({"driver_ref": driver_ref, "companies": companies})
            for metric_ref in driver["metric_refs"]:
                cells = []
                for company in pack["coverage_universe"]:
                    view = views_by_company[company["company_ref"]][driver_ref]
                    coverage = next(
                        item for item in view["metric_coverage"]
                        if item["metric_ref"] == metric_ref
                    )
                    cells.append({
                        "company_ref": company["company_ref"], "ticker": company["ticker"],
                        "status": coverage["status"],
                        "claim_version_refs": coverage["claim_version_refs"],
                        "rationale": coverage["rationale"],
                    })
                metric_matrix.append({
                    "driver_ref": driver_ref, "metric_ref": metric_ref, "companies": cells,
                })
        return _record({
            "schema_version": SCHEMA_VERSION, "projection_kind": "industry_brief_snapshot",
            "industry_ref": pack["industry_ref"], "as_of": pack["as_of"],
            "evidence_pack_version_ref": pack["id"],
            "evidence_pack_version_hash": pack["content_hash"],
            "driver_pack_version_ref": driver_pack["id"],
            "driver_pack_version_hash": driver_pack["content_hash"],
            "company_overlay_versions": [{
                "company_ref": company["company_ref"],
                "version_ref": by_company[company["company_ref"]]["id"],
                "content_hash": by_company[company["company_ref"]]["content_hash"],
            } for company in pack["coverage_universe"]],
            "driver_scoreboard": scoreboard, "metric_difference_matrix": metric_matrix,
            "debates": pack["debates"], "report_contract": pack["report_contract"],
        })

    def integrity_report(self) -> dict[str, Any]:
        issues: list[str] = []
        for table in ("industry_evidence_pack_versions", "company_overlay_versions"):
            for row in self.connection.execute(f"SELECT * FROM {table}").fetchall():
                try:
                    wire = _canonical_record(row["record_json"], table)
                    if wire["content_hash"] != row["content_hash"]:
                        issues.append(f"{table}: row hash mismatch")
                except IndustryResearchError as exc:
                    issues.append(f"{table}: {exc}")
        for pointer_table, version_table, ref_field in (
            ("industry_evidence_pack_pointer", "industry_evidence_pack_versions", "evidence_pack_ref"),
            ("company_overlay_pointer", "company_overlay_versions", "overlay_ref"),
        ):
            for pointer in self.connection.execute(f"SELECT * FROM {pointer_table}").fetchall():
                latest = self.connection.execute(
                    f"SELECT version_id,version_number,content_hash FROM {version_table} "
                    f"WHERE {ref_field}=? ORDER BY version_number DESC LIMIT 1", (pointer[ref_field],)
                ).fetchone()
                if latest is None or any(pointer[field] != latest[field] for field in ("version_id", "version_number", "content_hash")):
                    issues.append(f"{pointer_table}: pointer is not latest")
        if self.connection.execute("PRAGMA foreign_key_check").fetchall():
            issues.append("foreign_key_check failed")
        return {
            "ok": not issues,
            "issues": issues,
            "evidence_pack_versions": self.connection.execute("SELECT COUNT(*) FROM industry_evidence_pack_versions").fetchone()[0],
            "company_overlay_versions": self.connection.execute("SELECT COUNT(*) FROM company_overlay_versions").fetchone()[0],
        }
