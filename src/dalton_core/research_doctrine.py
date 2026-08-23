"""Versioned research doctrine and exact Planner context materialization.

Doctrine is human-authored attention policy, never Evidence or Claim.  The
materializer quotes exact authority data for one bounded planning round; Core
continues to own scope, permissions, parameters, budgets and terminal gates.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import ThesisVersion
from .coverage_admission import validate_driver_pack_version
from .research_question_backlog import read_exact_backlog_question_version
from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
PLANNER_CONTEXT_BUILDER_REF = "builder:planner-context-pack:0.1"
PLANNER_CONTEXT_BUILDER_HASH = content_hash({
    "builder_ref": PLANNER_CONTEXT_BUILDER_REF,
    "selection": "exact_authorities_plus_active_exact_loop_override",
    "prompt_semantics": "quoted_authority_data_only",
})
DOCTRINE_AWARE_PLANNER_REF = "planner:doctrine-aware-checklist:0.1"
DOCTRINE_AWARE_PLANNER_HASH = content_hash({
    "planner_ref": DOCTRINE_AWARE_PLANNER_REF,
    "algorithm": "directive_then_lens_priority_then_first_uncovered_else_closed_terminal",
    "proposal_contract": "planner-proposal-version:0.2",
})

_SCHEMA_PATH = Path(__file__).with_name("research_doctrine_schema.sql")
_HUMAN_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._/@:-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NEGATIVE_CLAIM_RULE = "candidate_only_until_separate_claim_admission"


class ResearchDoctrineError(Exception):
    pass


class ResearchDoctrineValidationError(ResearchDoctrineError, ValueError):
    pass


class ResearchDoctrineConflict(ResearchDoctrineError):
    pass


class ResearchDoctrineNotFound(ResearchDoctrineError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchDoctrineValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _human(value: Any, name: str = "actor_ref") -> str:
    value = _text(value, name)
    if _HUMAN_RE.fullmatch(value) is None:
        raise ResearchDoctrineValidationError(f"{name} must use the human: namespace")
    return value


def _sha256(value: Any, name: str) -> str:
    value = _text(value, name)
    if _SHA256_RE.fullmatch(value) is None:
        raise ResearchDoctrineValidationError(f"{name} must be lowercase SHA-256")
    return value


def _timestamp(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchDoctrineValidationError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ResearchDoctrineValidationError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchDoctrineValidationError(f"{name} must be an object")
    result = dict(value)
    if set(result) != fields:
        raise ResearchDoctrineValidationError(
            f"{name} has invalid closed shape; missing={sorted(fields - set(result))}, "
            f"unknown={sorted(set(result) - fields)}"
        )
    return result


def _texts(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ResearchDoctrineValidationError(f"{name} must be an array")
    result = [_text(item, f"{name}[]") for item in value]
    if nonempty and not result:
        raise ResearchDoctrineValidationError(f"{name} must not be empty")
    if len(set(result)) != len(result):
        raise ResearchDoctrineValidationError(f"{name} must contain unique values")
    return result


def _positive(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ResearchDoctrineValidationError(f"{name} must be a positive integer")
    return value


def _record(base: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(base)
    wire["content_hash"] = content_hash(wire)
    return wire


def _ref(prefix: str, identity: Mapping[str, Any]) -> str:
    return f"{prefix}:{content_hash(identity)[:32]}"


def _decode(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
    if row is None:
        raise ResearchDoctrineNotFound(name)
    try:
        wire = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ResearchDoctrineConflict(f"{name} record_json is invalid") from exc
    if type(wire) is not dict or canonical_json(wire) != row["record_json"]:
        raise ResearchDoctrineConflict(f"{name} record_json is not canonical")
    body = dict(wire)
    asserted = body.pop("content_hash", None)
    if asserted != content_hash(body) or asserted != row["content_hash"]:
        raise ResearchDoctrineConflict(f"{name} content hash drifted")
    if wire.get("id") not in {
        row[key] for key in row.keys() if key.endswith("_id") and row[key] is not None
    }:
        raise ResearchDoctrineConflict(f"{name} identity column drifted")
    bindings = {
        "doctrine_pack_ref": "doctrine_pack_ref",
        "loop_version_ref": "loop_version_ref",
        "effective_from": "effective_from",
        "effective_until": "effective_until",
        "selected_lens_ref": "selected_lens_ref",
        "as_of": "as_of",
    }
    for column, field in bindings.items():
        if column in row.keys() and field in wire and row[column] != wire[field]:
            raise ResearchDoctrineConflict(f"{name} SQL column for {field} drifted")
    if "version_number" in row.keys() and row["version_number"] != wire.get("version"):
        raise ResearchDoctrineConflict(f"{name} version column drifted")
    if "prior_version_id" in row.keys() and row["prior_version_id"] != wire.get("prior_version_ref"):
        raise ResearchDoctrineConflict(f"{name} prior version column drifted")
    if "round_ordinal" in row.keys() and row["round_ordinal"] != wire.get("round_ordinal"):
        raise ResearchDoctrineConflict(f"{name} round column drifted")
    return wire


def _validate_lenses(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ResearchDoctrineValidationError("lenses must be a non-empty array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        lens = _closed(
            raw,
            {"lens_ref", "label", "objective", "priority_topics", "evidence_standard"},
            "lens",
        )
        lens_ref = _text(lens["lens_ref"], "lens.lens_ref")
        if lens_ref in seen:
            raise ResearchDoctrineValidationError("lens_ref must be unique")
        seen.add(lens_ref)
        standard = _closed(
            lens["evidence_standard"],
            {"preferred_source_classes", "minimum_independent_sources", "negative_claim_rule"},
            "lens.evidence_standard",
        )
        if standard["negative_claim_rule"] != _NEGATIVE_CLAIM_RULE:
            raise ResearchDoctrineValidationError(
                "doctrine cannot weaken the separate negative-Claim admission gate"
            )
        result.append({
            "lens_ref": lens_ref,
            "label": _text(lens["label"], "lens.label"),
            "objective": _text(lens["objective"], "lens.objective"),
            "priority_topics": _texts(lens["priority_topics"], "lens.priority_topics", nonempty=True),
            "evidence_standard": {
                "preferred_source_classes": _texts(
                    standard["preferred_source_classes"],
                    "lens.evidence_standard.preferred_source_classes",
                    nonempty=True,
                ),
                "minimum_independent_sources": _positive(
                    standard["minimum_independent_sources"],
                    "lens.evidence_standard.minimum_independent_sources",
                ),
                "negative_claim_rule": _NEGATIVE_CLAIM_RULE,
            },
        })
    return result


def read_exact_planner_context_pack(
    connection: sqlite3.Connection, context_pack_ref: str
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM planner_context_pack_versions WHERE context_pack_id=?",
        (_text(context_pack_ref, "planner_context_pack_ref"),),
    ).fetchone()
    return _decode(row, "PlannerContextPackVersion")


def revalidate_planner_context_pack(
    bounded_authority: Any, context_pack_ref: str, *, expected_hash: str | None = None
) -> dict[str, Any]:
    """Re-read a context pack and prove that its round state is still current."""

    context = read_exact_planner_context_pack(
        bounded_authority.connection, context_pack_ref
    )
    if expected_hash is not None and context["content_hash"] != _sha256(
        expected_hash, "planner_context_pack_hash"
    ):
        raise ResearchDoctrineConflict("planner context pack hash binding failed")
    loop = bounded_authority.loop(context["loop_version_ref"])
    if context["loop_version_hash"] != loop["content_hash"]:
        raise ResearchDoctrineConflict("planner context loop binding drifted")
    doctrine = ResearchDoctrineAuthority(bounded_authority.store)
    pack = doctrine.pack(context["doctrine_input"]["ref"])
    if context["doctrine_input"] != {
        "ref": pack["id"], "hash": pack["content_hash"], "quoted_data": pack,
    }:
        raise ResearchDoctrineConflict("planner context doctrine binding drifted")
    as_of = _timestamp(context["as_of"], "planner context as_of")
    active_override = doctrine._active_override(loop["id"], pack, as_of)
    expected_override = (
        {
            "ref": active_override["id"],
            "hash": active_override["content_hash"],
            "quoted_data": active_override,
        }
        if active_override else None
    )
    if context["override_input"] != expected_override:
        raise ResearchDoctrineConflict("planner context doctrine override is stale")
    selected_ref = (
        active_override["lens_ref"] if active_override else pack["default_lens_ref"]
    )
    selected = next(
        (item for item in pack["lenses"] if item["lens_ref"] == selected_ref), None
    )
    if (
        selected is None
        or context["selected_lens_ref"] != selected_ref
        or context["selected_lens"] != selected
    ):
        raise ResearchDoctrineConflict("planner context selected lens drifted")
    question = read_exact_backlog_question_version(
        bounded_authority.connection.cursor(), loop["question_version_ref"]
    )
    if (
        question["content_hash"] != loop["question_version_hash"]
        or context["question_input"] != {
            "ref": question["id"],
            "hash": question["content_hash"],
            "quoted_data": question,
        }
    ):
        raise ResearchDoctrineConflict("planner context question binding drifted")
    expected_catalog = []
    for binding in loop["template_bindings"]:
        template = bounded_authority.probe_template(binding["template_version_ref"])
        if template["content_hash"] != binding["template_version_hash"]:
            raise ResearchDoctrineConflict("planner context template authority drifted")
        expected_catalog.append({
            "coverage_item_ref": binding["coverage_item_ref"],
            "template_version_ref": template["id"],
            "template_version_hash": template["content_hash"],
            "parameters": binding["parameters"],
            "quoted_data": template,
        })
    if context["catalog_inputs"] != expected_catalog:
        raise ResearchDoctrineConflict("planner context catalog binding drifted")
    driver = context["driver_pack_input"]
    expected_driver = doctrine._driver_input(
        {"ref": driver["ref"], "hash": driver["hash"]} if driver else None
    )
    if driver != expected_driver:
        raise ResearchDoctrineConflict("planner context driver pack binding drifted")
    expected_theses = doctrine._thesis_inputs([
        {"ref": item["ref"], "hash": item["hash"]}
        for item in context["thesis_inputs"]
    ])
    if context["thesis_inputs"] != expected_theses:
        raise ResearchDoctrineConflict("planner context thesis binding drifted")
    if (
        context["builder_ref"] != PLANNER_CONTEXT_BUILDER_REF
        or context["builder_hash"] != PLANNER_CONTEXT_BUILDER_HASH
        or context["actor_ref"] != "core:planner-context-materializer"
    ):
        raise ResearchDoctrineConflict("planner context builder binding drifted")
    rounds = bounded_authority.rounds(loop["id"])
    if context["round_ordinal"] != len(rounds) + 1:
        raise ResearchDoctrineConflict("planner context is stale for the current round")
    if rounds and bounded_authority.outcome_for_round(rounds[-1]["id"]) is None:
        raise ResearchDoctrineConflict("planner context cannot bypass a pending round")
    expected_outcomes = [
        {"ref": item["id"], "hash": item["content_hash"], "quoted_data": item}
        for item in bounded_authority.outcomes(loop["id"])
    ]
    if context["outcome_inputs"] != expected_outcomes:
        raise ResearchDoctrineConflict("planner context outcome history is stale")
    if context["remaining_budget"] != bounded_authority._budget_snapshot(loop):
        raise ResearchDoctrineConflict("planner context budget snapshot is stale")
    expected_directives = []
    for row in bounded_authority.connection.execute(
        "SELECT * FROM bounded_research_directive_versions "
        "WHERE loop_version_ref=? AND effective_round<=? ORDER BY created_at,version_id",
        (loop["id"], context["round_ordinal"]),
    ).fetchall():
        from .bounded_planner_loop import _decode_record

        item = _decode_record(row, "ResearchDirectiveVersion")
        expected_directives.append({
            "ref": item["id"], "hash": item["content_hash"], "quoted_data": item,
        })
    if context["directive_inputs"] != expected_directives:
        raise ResearchDoctrineConflict("planner context directive history is stale")
    return context


class ResearchDoctrineAuthority:
    """Append-only human doctrine and machine-derived Planner context authority."""

    def __init__(self, store: Any):
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("ResearchDoctrineAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def publish_pack(
        self,
        doctrine_pack_ref: str,
        *,
        title: str,
        default_lens_ref: str,
        lenses: Sequence[Mapping[str, Any]],
        actor_ref: str,
        prior_version_ref: str | None = None,
    ) -> dict[str, Any]:
        doctrine_pack_ref = _text(doctrine_pack_ref, "doctrine_pack_ref")
        title = _text(title, "title")
        actor_ref = _human(actor_ref)
        lens_wire = _validate_lenses(lenses)
        default_lens_ref = _text(default_lens_ref, "default_lens_ref")
        if default_lens_ref not in {item["lens_ref"] for item in lens_wire}:
            raise ResearchDoctrineValidationError("default_lens_ref is not present in lenses")
        latest = self.connection.execute(
            "SELECT * FROM doctrine_pack_versions "
            "WHERE doctrine_pack_ref=? ORDER BY version_number DESC LIMIT 1",
            (doctrine_pack_ref,),
        ).fetchone()
        if latest is not None:
            latest_wire = _decode(latest, "DoctrinePackVersion")
            if (
                latest_wire["prior_version_ref"] == prior_version_ref
                and latest_wire["title"] == title
                and latest_wire["default_lens_ref"] == default_lens_ref
                and latest_wire["lenses"] == lens_wire
                and latest_wire["actor_ref"] == actor_ref
            ):
                return {"status": "duplicate", **latest_wire}
        if latest is None:
            if prior_version_ref is not None:
                raise ResearchDoctrineConflict("first doctrine pack cannot have a prior version")
            version = 1
        else:
            if prior_version_ref != latest["version_id"]:
                raise ResearchDoctrineConflict("doctrine pack must continue the latest version")
            version = int(latest["version_number"]) + 1
        identity = {
            "doctrine_pack_ref": doctrine_pack_ref,
            "version": version,
            "prior_version_ref": prior_version_ref,
            "title": title,
            "default_lens_ref": default_lens_ref,
            "lenses": lens_wire,
        }
        version_id = _ref("doctrine-pack-version", identity)
        existing = self.connection.execute(
            "SELECT * FROM doctrine_pack_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if existing is not None:
            return {"status": "duplicate", **_decode(existing, "DoctrinePackVersion")}
        wire = _record({
            "schema_version": SCHEMA_VERSION,
            "id": version_id,
            "created_at": _now(),
            **identity,
            "actor_ref": actor_ref,
        })
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO doctrine_pack_versions "
                "(version_id,doctrine_pack_ref,version_number,prior_version_id,record_json,"
                "content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (version_id, doctrine_pack_ref, version, prior_version_ref,
                 canonical_json(wire), wire["content_hash"], actor_ref, wire["created_at"]),
            )
        return {"status": "fresh", **wire}

    def pack(self, version_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM doctrine_pack_versions WHERE version_id=?",
            (_text(version_ref, "doctrine_pack_version_ref"),),
        ).fetchone()
        return _decode(row, "DoctrinePackVersion")

    def publish_override(
        self,
        override_ref: str,
        *,
        doctrine_pack_version_ref: str,
        doctrine_pack_version_hash: str,
        loop_version_ref: str,
        lens_ref: str,
        rationale: str,
        effective_from: str,
        effective_until: str,
        revoked: bool,
        actor_ref: str,
        prior_version_ref: str | None = None,
    ) -> dict[str, Any]:
        override_ref = _text(override_ref, "override_ref")
        actor_ref = _human(actor_ref)
        if type(revoked) is not bool:
            raise ResearchDoctrineValidationError("revoked must be boolean")
        pack = self.pack(doctrine_pack_version_ref)
        if pack["content_hash"] != _sha256(
            doctrine_pack_version_hash, "doctrine_pack_version_hash"
        ):
            raise ResearchDoctrineConflict("doctrine pack hash binding failed")
        lens_ref = _text(lens_ref, "lens_ref")
        if lens_ref not in {item["lens_ref"] for item in pack["lenses"]}:
            raise ResearchDoctrineValidationError("override lens_ref is not in the exact doctrine pack")
        from .bounded_planner_loop import BoundedPlannerAuthority

        loop = BoundedPlannerAuthority(self.store).loop(loop_version_ref)
        effective_from = _timestamp(effective_from, "effective_from")
        effective_until = _timestamp(effective_until, "effective_until")
        if effective_until <= effective_from:
            raise ResearchDoctrineValidationError("effective_until must be after effective_from")
        latest = self.connection.execute(
            "SELECT * FROM doctrine_override_versions "
            "WHERE override_ref=? ORDER BY version_number DESC LIMIT 1",
            (override_ref,),
        ).fetchone()
        if latest is not None:
            latest_wire = _decode(latest, "DoctrineOverrideVersion")
            retry_fields = {
                "prior_version_ref": prior_version_ref,
                "doctrine_pack_version_ref": pack["id"],
                "doctrine_pack_version_hash": pack["content_hash"],
                "loop_version_ref": loop["id"],
                "loop_version_hash": loop["content_hash"],
                "lens_ref": lens_ref,
                "rationale": _text(rationale, "rationale"),
                "effective_from": effective_from,
                "effective_until": effective_until,
                "revoked": revoked,
                "actor_ref": actor_ref,
            }
            if all(latest_wire[key] == value for key, value in retry_fields.items()):
                return {"status": "duplicate", **latest_wire}
        if latest is None:
            if prior_version_ref is not None:
                raise ResearchDoctrineConflict("first doctrine override cannot have a prior version")
            version = 1
        else:
            if prior_version_ref != latest["version_id"]:
                raise ResearchDoctrineConflict("doctrine override must continue the latest version")
            version = int(latest["version_number"]) + 1
        identity = {
            "override_ref": override_ref,
            "version": version,
            "prior_version_ref": prior_version_ref,
            "doctrine_pack_version_ref": pack["id"],
            "doctrine_pack_version_hash": pack["content_hash"],
            "loop_version_ref": loop["id"],
            "loop_version_hash": loop["content_hash"],
            "lens_ref": lens_ref,
            "rationale": _text(rationale, "rationale"),
            "effective_from": effective_from,
            "effective_until": effective_until,
            "revoked": revoked,
        }
        version_id = _ref("doctrine-override-version", identity)
        existing = self.connection.execute(
            "SELECT * FROM doctrine_override_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if existing is not None:
            return {"status": "duplicate", **_decode(existing, "DoctrineOverrideVersion")}
        wire = _record({
            "schema_version": SCHEMA_VERSION,
            "id": version_id,
            "created_at": _now(),
            **identity,
            "actor_ref": actor_ref,
        })
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO doctrine_override_versions "
                "(version_id,override_ref,version_number,prior_version_id,loop_version_ref,"
                "effective_from,effective_until,revoked,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (version_id, override_ref, version, prior_version_ref, loop["id"],
                 effective_from, effective_until, int(revoked), canonical_json(wire),
                 wire["content_hash"], actor_ref, wire["created_at"]),
            )
        return {"status": "fresh", **wire}

    def _active_override(
        self, loop_version_ref: str, pack: Mapping[str, Any], as_of: str
    ) -> dict[str, Any] | None:
        latest_by_ref: dict[str, dict[str, Any]] = {}
        for row in self.connection.execute(
            "SELECT * FROM doctrine_override_versions WHERE loop_version_ref=? "
            "ORDER BY override_ref,version_number",
            (loop_version_ref,),
        ).fetchall():
            item = _decode(row, "DoctrineOverrideVersion")
            latest_by_ref[item["override_ref"]] = item
        active = [
            item for item in latest_by_ref.values()
            if not item["revoked"]
            and item["doctrine_pack_version_ref"] == pack["id"]
            and item["doctrine_pack_version_hash"] == pack["content_hash"]
            and item["effective_from"] <= as_of < item["effective_until"]
        ]
        if len(active) > 1:
            raise ResearchDoctrineConflict("multiple doctrine overrides are active for one loop")
        return active[0] if active else None

    def _driver_input(self, binding: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if binding is None:
            return None
        obj = _closed(binding, {"ref", "hash"}, "driver_pack_binding")
        row = self.connection.execute(
            "SELECT * FROM driver_pack_versions WHERE version_id=?",
            (_text(obj["ref"], "driver_pack_binding.ref"),),
        ).fetchone()
        if row is None:
            raise ResearchDoctrineNotFound("IndustryDriverPackVersion")
        try:
            wire = validate_driver_pack_version(json.loads(row["record_json"]))
        except Exception as exc:
            raise ResearchDoctrineConflict("driver pack authority is invalid") from exc
        if canonical_json(wire) != row["record_json"] or wire["content_hash"] != row["content_hash"]:
            raise ResearchDoctrineConflict("driver pack authority drifted")
        if wire["content_hash"] != _sha256(obj["hash"], "driver_pack_binding.hash"):
            raise ResearchDoctrineConflict("driver pack hash binding failed")
        return {"ref": wire["id"], "hash": wire["content_hash"], "quoted_data": wire}

    def _thesis_inputs(self, bindings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(bindings, (list, tuple)):
            raise ResearchDoctrineValidationError("thesis_bindings must be an array")
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in bindings:
            obj = _closed(raw, {"ref", "hash"}, "thesis_binding")
            ref = _text(obj["ref"], "thesis_binding.ref")
            if ref in seen:
                raise ResearchDoctrineValidationError("thesis bindings must be unique")
            seen.add(ref)
            row = self.connection.execute(
                "SELECT * FROM thesis_versions WHERE version_id=?", (ref,)
            ).fetchone()
            if row is None:
                raise ResearchDoctrineNotFound("ThesisVersion")
            try:
                quoted = ThesisVersion.from_dict(json.loads(row["content_json"])).to_dict()
            except Exception as exc:
                raise ResearchDoctrineConflict("thesis content_json is invalid") from exc
            if (
                canonical_json(quoted) != row["content_json"]
                or quoted["id"] != row["version_id"]
                or quoted["thesis_ref"] != row["thesis_id"]
                or quoted["version"] != row["version_number"]
                or quoted["prior_version_ref"] != row["prior_version_id"]
                or quoted["authority_kind"] != row["authority_kind"]
                or quoted["authority_ref"] != row["authority_ref"]
                or quoted["committed_by_ref"] != row["committed_by"]
                or quoted["created_at"] != row["created_at"]
                or quoted["content_hash"] != row["content_hash"]
            ):
                raise ResearchDoctrineConflict("thesis authority drifted")
            if row["content_hash"] != _sha256(obj["hash"], "thesis_binding.hash"):
                raise ResearchDoctrineConflict("thesis hash binding failed")
            result.append({
                "ref": ref,
                "hash": row["content_hash"],
                "thesis_ref": row["thesis_id"],
                "version": row["version_number"],
                "authority_kind": row["authority_kind"],
                "authority_ref": row["authority_ref"],
                "quoted_data": quoted,
            })
        return result

    def materialize_planner_context(
        self,
        bounded_authority: Any,
        loop_version_ref: str,
        *,
        doctrine_pack_version_ref: str,
        doctrine_pack_version_hash: str,
        as_of: str,
        driver_pack_binding: Mapping[str, Any] | None = None,
        thesis_bindings: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        if bounded_authority.connection is not self.connection:
            raise TypeError("doctrine and bounded authorities must share one Core connection")
        loop = bounded_authority.loop(loop_version_ref)
        pack = self.pack(doctrine_pack_version_ref)
        if pack["content_hash"] != _sha256(
            doctrine_pack_version_hash, "doctrine_pack_version_hash"
        ):
            raise ResearchDoctrineConflict("doctrine pack hash binding failed")
        as_of = _timestamp(as_of, "as_of")
        override = self._active_override(loop["id"], pack, as_of)
        selected_ref = override["lens_ref"] if override else pack["default_lens_ref"]
        selected = next(item for item in pack["lenses"] if item["lens_ref"] == selected_ref)
        question = read_exact_backlog_question_version(
            self.connection.cursor(), loop["question_version_ref"]
        )
        if question["content_hash"] != loop["question_version_hash"]:
            raise ResearchDoctrineConflict("loop question binding drifted")
        rounds = bounded_authority.rounds(loop["id"])
        if rounds and bounded_authority.outcome_for_round(rounds[-1]["id"]) is None:
            raise ResearchDoctrineConflict("cannot materialize context while a round is pending")
        ordinal = len(rounds) + 1
        outcomes = [
            {"ref": item["id"], "hash": item["content_hash"], "quoted_data": item}
            for item in bounded_authority.outcomes(loop["id"])
        ]
        directives = []
        from .bounded_planner_loop import _decode_record

        for row in self.connection.execute(
            "SELECT * FROM bounded_research_directive_versions "
            "WHERE loop_version_ref=? AND effective_round<=? ORDER BY created_at,version_id",
            (loop["id"], ordinal),
        ).fetchall():
            item = _decode_record(row, "ResearchDirectiveVersion")
            directives.append({
                "ref": item["id"], "hash": item["content_hash"], "quoted_data": item,
            })
        catalog_inputs = []
        for binding in loop["template_bindings"]:
            template = bounded_authority.probe_template(binding["template_version_ref"])
            if template["content_hash"] != binding["template_version_hash"]:
                raise ResearchDoctrineConflict("loop template binding drifted")
            catalog_inputs.append({
                "coverage_item_ref": binding["coverage_item_ref"],
                "template_version_ref": template["id"],
                "template_version_hash": template["content_hash"],
                "parameters": binding["parameters"],
                "quoted_data": template,
            })
        identity = {
            "loop_version_ref": loop["id"],
            "loop_version_hash": loop["content_hash"],
            "round_ordinal": ordinal,
            "as_of": as_of,
            "question_input": {"ref": question["id"], "hash": question["content_hash"], "quoted_data": question},
            "doctrine_input": {"ref": pack["id"], "hash": pack["content_hash"], "quoted_data": pack},
            "selected_lens_ref": selected_ref,
            "selected_lens": selected,
            "override_input": (
                {"ref": override["id"], "hash": override["content_hash"], "quoted_data": override}
                if override else None
            ),
            "driver_pack_input": self._driver_input(driver_pack_binding),
            "thesis_inputs": self._thesis_inputs(thesis_bindings),
            "outcome_inputs": outcomes,
            "directive_inputs": directives,
            "remaining_budget": bounded_authority._budget_snapshot(loop),
            "catalog_inputs": catalog_inputs,
            "builder_ref": PLANNER_CONTEXT_BUILDER_REF,
            "builder_hash": PLANNER_CONTEXT_BUILDER_HASH,
        }
        context_id = _ref("planner-context-pack-version", identity)
        existing = self.connection.execute(
            "SELECT * FROM planner_context_pack_versions WHERE context_pack_id=?",
            (context_id,),
        ).fetchone()
        if existing is not None:
            return {"status": "duplicate", **_decode(existing, "PlannerContextPackVersion")}
        wire = _record({
            "schema_version": SCHEMA_VERSION,
            "id": context_id,
            "created_at": _now(),
            **identity,
            "actor_ref": "core:planner-context-materializer",
        })
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO planner_context_pack_versions "
                "(context_pack_id,loop_version_ref,round_ordinal,doctrine_pack_version_ref,"
                "selected_lens_ref,override_version_ref,as_of,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (context_id, loop["id"], ordinal, pack["id"], selected_ref,
                 override["id"] if override else None, as_of, canonical_json(wire),
                 wire["content_hash"], wire["created_at"]),
            )
        return {"status": "fresh", **wire}

    def context(self, context_pack_ref: str) -> dict[str, Any]:
        return read_exact_planner_context_pack(self.connection, context_pack_ref)


__all__ = [
    "DOCTRINE_AWARE_PLANNER_HASH",
    "DOCTRINE_AWARE_PLANNER_REF",
    "PLANNER_CONTEXT_BUILDER_HASH",
    "PLANNER_CONTEXT_BUILDER_REF",
    "ResearchDoctrineAuthority",
    "ResearchDoctrineConflict",
    "ResearchDoctrineError",
    "ResearchDoctrineNotFound",
    "ResearchDoctrineValidationError",
    "read_exact_planner_context_pack",
    "revalidate_planner_context_pack",
]
