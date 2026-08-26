"""Explicit human review authority and lossless Ledger promotion contracts.

Candidate staging proves source and numeric bindings but deliberately leaves
claim semantics unverified.  This module owns the next boundary:

* an authenticated human records one terminal decision for an exact candidate
  version;
* accepted decisions create a durable, replayable commit intent;
* formal Evidence/Claim 0.2 records preserve candidate hashes, Decimal values,
  structured periods, currency/scale, and the human decision reference.

The authority never opens the Core database.  A separate scoped writer call
performs the formal atomic commit.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

from .research_verification import (
    validate_candidate_claim,
    validate_candidate_evidence,
    validate_numeric_verification_spec,
    validate_source_verification_material,
    validate_verification_bundle,
)
from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
LEDGER_SCHEMA_VERSION = "0.2"
_SCHEMA_PATH = Path(__file__).with_name("research_review_schema.sql")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_HUMAN_REVIEWER_RE = re.compile(r"^human:tailscale-[0-9a-f]{32}$")
_SEMANTIC_FIELDS = (
    "subject_ref", "metric_or_aspect", "period", "basis", "normalized_statement"
)
_CONSUMABLE_REVISION_FIELDS = ("normalized_statement",)
_REVISION_ACTOR_REF = "system:research-review-revision"


class ResearchReviewError(ValueError):
    """Malformed review authority input."""


class ResearchReviewConflict(ResearchReviewError):
    """An immutable review identity or idempotency key drifted."""


class ResearchReviewRejected(ResearchReviewError):
    """A candidate is not eligible for the requested review transition."""


def _serialized(method: Any) -> Any:
    """Serialize one shared SQLite connection across HTTP worker threads."""

    @wraps(method)
    def wrapped(self: "HumanReviewAuthority", *args: Any, **kwargs: Any) -> Any:
        with self._connection_lock:
            return method(self, *args, **kwargs)

    return wrapped


def _json(value: Any, name: str) -> Any:
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ResearchReviewError(f"{name} must be finite JSON") from exc


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchReviewError(f"{name} must be an object")
    wire = dict(value)
    missing = fields - set(wire)
    unknown = set(wire) - fields
    if missing or unknown:
        raise ResearchReviewError(
            f"{name} closed shape mismatch: missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    return _json(wire, name)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResearchReviewError(f"{name} must be a non-empty string")
    return value


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise ResearchReviewError(f"{name} must be lowercase SHA-256 hex")
    return value


def _timestamp(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchReviewError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ResearchReviewError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ResearchReviewError(f"{name} must be a positive integer")
    return value


def _strings(value: Any, name: str) -> list[str]:
    if not isinstance(value, list):
        raise ResearchReviewError(f"{name} must be an array")
    result = [_text(item, f"{name}[]") for item in value]
    if len(result) != len(set(result)):
        raise ResearchReviewError(f"{name} must contain unique strings")
    return result


def _ref_hash(value: Any, name: str) -> dict[str, str]:
    wire = _closed(value, {"ref", "hash"}, name)
    return {"ref": _text(wire["ref"], f"{name}.ref"), "hash": _hash(wire["hash"], f"{name}.hash")}


def _ref_hashes(value: Any, name: str, *, nonempty: bool = False) -> list[dict[str, str]]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ResearchReviewError(f"{name} must be {'non-empty ' if nonempty else ''}an array")
    result = [_ref_hash(item, f"{name}[{index}]") for index, item in enumerate(value)]
    if len(result) != len({item["ref"] for item in result}):
        raise ResearchReviewError(f"{name} refs must be unique")
    return result


def _with_hash(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    wire = dict(value)
    declared = _hash(wire.pop("content_hash"), f"{name}.content_hash")
    expected = content_hash(wire)
    if declared != expected:
        raise ResearchReviewConflict(f"{name} content_hash mismatch")
    wire["content_hash"] = declared
    return wire


_DECISION_FIELDS = {
    "schema_version", "id", "created_at", "candidate_claim_ref",
    "candidate_claim_hash", "candidate_evidence_ref", "candidate_evidence_hash",
    "verdict", "reviewed_semantics", "proposed_revisions", "relation",
    "rationale", "findings", "reviewer_ref", "authorization", "source",
    "source_event_ref", "content_hash",
}

_COMMIT_EVENT_FIELDS = {
    "schema_version", "id", "created_at", "decision_ref", "state",
    "prior_event_ref", "ledger_result", "error_code", "content_hash",
}


def _semantics(value: Any, name: str) -> dict[str, Any]:
    wire = _closed(value, set(_SEMANTIC_FIELDS), name)
    for field in ("subject_ref", "metric_or_aspect", "basis", "normalized_statement"):
        wire[field] = _text(wire[field], f"{name}.{field}")
    if not isinstance(wire["period"], (str, Mapping)):
        raise ResearchReviewError(f"{name}.period must be a string or object")
    wire["period"] = _json(wire["period"], f"{name}.period")
    return wire


def validate_human_review_decision(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = _closed(value, _DECISION_FIELDS, "HumanReviewDecision")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ResearchReviewError("unsupported HumanReviewDecision schema_version")
    for field in (
        "id", "candidate_claim_ref", "candidate_evidence_ref", "rationale",
        "reviewer_ref", "source_event_ref",
    ):
        wire[field] = _text(wire[field], field)
    for field in ("candidate_claim_hash", "candidate_evidence_hash"):
        wire[field] = _hash(wire[field], field)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    if wire["verdict"] not in {"accept", "revise", "reject"}:
        raise ResearchReviewError("HumanReviewDecision.verdict is invalid")
    wire["reviewed_semantics"] = _semantics(wire["reviewed_semantics"], "reviewed_semantics")
    if wire["relation"] != "supports":
        raise ResearchReviewError("candidate promotion currently requires a supports relation")
    if wire["authorization"] != "explicit_human_review":
        raise ResearchReviewRejected("automatic or implicit review cannot authorize promotion")
    if wire["source"] != "tailscale_review":
        raise ResearchReviewError("HumanReviewDecision.source is invalid")
    if _HUMAN_REVIEWER_RE.fullmatch(wire["reviewer_ref"]) is None:
        raise ResearchReviewRejected("reviewer_ref is not an authenticated Tailscale human")
    if not wire["source_event_ref"].startswith("research-review:"):
        raise ResearchReviewError("source_event_ref is outside the review control namespace")
    wire["findings"] = _strings(wire["findings"], "findings")
    revisions = wire["proposed_revisions"]
    if wire["verdict"] == "revise":
        if not isinstance(revisions, Mapping) or not revisions:
            raise ResearchReviewError("revise requires proposed_revisions")
        unknown = set(revisions) - set(_SEMANTIC_FIELDS)
        if unknown:
            raise ResearchReviewError("proposed_revisions contains unknown semantic fields")
        wire["proposed_revisions"] = _json(revisions, "proposed_revisions")
        revised_semantics = dict(wire["reviewed_semantics"])
        revised_semantics.update(wire["proposed_revisions"])
        _semantics(revised_semantics, "revised_semantics")
    elif revisions is not None:
        raise ResearchReviewError("accept/reject cannot carry proposed_revisions")
    return _with_hash(wire, "HumanReviewDecision")


def validate_human_review_commit_event(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one append-only review-to-Ledger delivery event."""

    wire = _closed(value, _COMMIT_EVENT_FIELDS, "HumanReviewCommitEvent")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ResearchReviewError(
            "unsupported HumanReviewCommitEvent schema_version"
        )
    for field in ("id", "decision_ref"):
        wire[field] = _text(wire[field], field)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    if wire["state"] not in {"queued", "committed", "failed"}:
        raise ResearchReviewError("HumanReviewCommitEvent.state is invalid")
    if wire["prior_event_ref"] is not None:
        wire["prior_event_ref"] = _text(
            wire["prior_event_ref"], "prior_event_ref"
        )
    if wire["ledger_result"] is not None:
        if not isinstance(wire["ledger_result"], Mapping):
            raise ResearchReviewError("ledger_result must be an object or null")
        wire["ledger_result"] = _json(wire["ledger_result"], "ledger_result")
    if wire["error_code"] is not None:
        wire["error_code"] = _text(wire["error_code"], "error_code")
    if wire["state"] == "queued" and (
        wire["prior_event_ref"] is not None
        or wire["ledger_result"] is not None
        or wire["error_code"] is not None
    ):
        raise ResearchReviewError("queued commit event has terminal fields")
    if wire["state"] == "committed" and (
        wire["prior_event_ref"] is None
        or wire["ledger_result"] is None
        or wire["error_code"] is not None
    ):
        raise ResearchReviewError("committed event has invalid result fields")
    if wire["state"] == "failed" and (
        wire["prior_event_ref"] is None
        or wire["ledger_result"] is not None
        or wire["error_code"] is None
    ):
        raise ResearchReviewError("failed event has invalid error fields")
    return _with_hash(wire, "HumanReviewCommitEvent")


_EVIDENCE_V2_FIELDS = {
    "schema_version", "id", "created_at", "evidence_ref", "version", "source_type",
    "source_ref", "source_envelope_ref", "source_envelope_hash", "retrieved_at",
    "valid_until", "artifact_refs", "source_lineage", "independence_group",
    "source_verification_ref", "source_verification_hash", "candidate_origin_ref",
    "candidate_origin_hash", "review_decision_ref", "review_decision_hash",
    "actor_ref", "prior_version_ref", "content_hash",
}


def validate_evidence_version_v0_2(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = _closed(value, _EVIDENCE_V2_FIELDS, "EvidenceVersionV0.2")
    if wire["schema_version"] != LEDGER_SCHEMA_VERSION:
        raise ResearchReviewError("unsupported EvidenceVersionV0.2 schema_version")
    for field in (
        "id", "evidence_ref", "source_type", "source_ref", "source_envelope_ref",
        "independence_group", "source_verification_ref", "candidate_origin_ref",
        "review_decision_ref", "actor_ref",
    ):
        wire[field] = _text(wire[field], field)
    wire["version"] = _positive_int(wire["version"], "version")
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    wire["retrieved_at"] = _timestamp(wire["retrieved_at"], "retrieved_at")
    wire["valid_until"] = _timestamp(wire["valid_until"], "valid_until", nullable=True)
    for field in (
        "source_envelope_hash", "source_verification_hash", "candidate_origin_hash",
        "review_decision_hash",
    ):
        wire[field] = _hash(wire[field], field)
    wire["artifact_refs"] = _ref_hashes(wire["artifact_refs"], "artifact_refs", nonempty=True)
    wire["source_lineage"] = _strings(wire["source_lineage"], "source_lineage")
    if not wire["source_lineage"]:
        raise ResearchReviewError("source_lineage must not be empty")
    wire["prior_version_ref"] = None if wire["prior_version_ref"] is None else _text(wire["prior_version_ref"], "prior_version_ref")
    return _with_hash(wire, "EvidenceVersionV0.2")


_CLAIM_V2_FIELDS = {
    "schema_version", "id", "created_at", "claim_ref", "version", "subject_ref",
    "metric_or_aspect", "period", "basis", "normalized_statement", "claim_kind",
    "value", "unit", "currency", "scale", "producer_execution_refs",
    "semantic_review_ref", "semantic_review_hash", "candidate_origin_ref",
    "candidate_origin_hash", "actor_ref", "prior_version_ref", "content_hash",
}


def validate_claim_version_v0_2(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = _closed(value, _CLAIM_V2_FIELDS, "ClaimVersionV0.2")
    if wire["schema_version"] != LEDGER_SCHEMA_VERSION:
        raise ResearchReviewError("unsupported ClaimVersionV0.2 schema_version")
    for field in (
        "id", "claim_ref", "subject_ref", "metric_or_aspect", "basis",
        "normalized_statement", "semantic_review_ref",
        "candidate_origin_ref", "actor_ref",
    ):
        wire[field] = _text(wire[field], field)
    wire["version"] = _positive_int(wire["version"], "version")
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    if wire["claim_kind"] == "quantitative":
        if not isinstance(wire["value"], str) or _DECIMAL_RE.fullmatch(wire["value"]) is None:
            raise ResearchReviewError("ClaimVersionV0.2.value must be canonical Decimal text")
        for field in ("unit", "scale"):
            wire[field] = _text(wire[field], field)
    elif wire["claim_kind"] == "qualitative":
        # ADR-0003 option B: a semantic claim carries no numeric authority.
        if any(wire[field] is not None for field in ("value", "unit", "currency", "scale")):
            raise ResearchReviewError(
                "qualitative ClaimVersionV0.2 must not carry value, unit, currency or scale"
            )
    else:
        raise ResearchReviewError("ClaimVersionV0.2.claim_kind must be quantitative or qualitative")
    if wire["currency"] is not None and (
        not isinstance(wire["currency"], str) or re.fullmatch(r"[A-Z]{3}", wire["currency"]) is None
    ):
        raise ResearchReviewError("currency must be ISO-4217 or null")
    if not isinstance(wire["period"], (str, Mapping)):
        raise ResearchReviewError("period must be a string or object")
    wire["period"] = _json(wire["period"], "period")
    refs = wire["producer_execution_refs"]
    if not isinstance(refs, list) or not refs:
        raise ResearchReviewError("producer_execution_refs must be a non-empty array")
    wire["producer_execution_refs"] = _strings(refs, "producer_execution_refs")
    for field in ("semantic_review_hash", "candidate_origin_hash"):
        wire[field] = _hash(wire[field], field)
    wire["prior_version_ref"] = None if wire["prior_version_ref"] is None else _text(wire["prior_version_ref"], "prior_version_ref")
    return _with_hash(wire, "ClaimVersionV0.2")


def _candidate_semantics(claim: Mapping[str, Any]) -> dict[str, Any]:
    return {field: _json(claim[field], field) for field in _SEMANTIC_FIELDS}


def _revised_candidate_claim(
    claim: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    """Deterministically apply one exact human revision to a candidate claim."""

    claim_wire = validate_candidate_claim(claim)
    decision_wire = validate_human_review_decision(decision)
    if decision_wire["verdict"] != "revise":
        raise ResearchReviewRejected("only a revise decision can produce a candidate revision")
    if set(decision_wire["proposed_revisions"]) - set(_CONSUMABLE_REVISION_FIELDS):
        raise ResearchReviewRejected(
            "in-place revision currently supports normalized_statement only; "
            "source, numeric or period changes require a new verified plan run"
        )
    if (
        decision_wire["candidate_claim_ref"] != claim_wire["id"]
        or decision_wire["candidate_claim_hash"] != claim_wire["content_hash"]
        or canonical_json(decision_wire["reviewed_semantics"])
        != canonical_json(_candidate_semantics(claim_wire))
    ):
        raise ResearchReviewConflict("revision decision drifted from its exact candidate")
    revised = dict(claim_wire)
    revised.pop("content_hash")
    revised.update(decision_wire["proposed_revisions"])
    revised["version"] = claim_wire["version"] + 1
    revised["id"] = "candidate-claim-version:" + content_hash({
        "candidate_claim_ref": claim_wire["candidate_claim_ref"],
        "version": revised["version"],
    })
    revised["created_at"] = decision_wire["created_at"]
    revised["actor_ref"] = _REVISION_ACTOR_REF
    revised["prior_version_ref"] = claim_wire["id"]
    revised["content_hash"] = content_hash(revised)
    return validate_candidate_claim(revised)


class HumanReviewAuthority:
    """Append-only human decisions over an owner-only candidate staging DB."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._connection_lock = threading.RLock()
        if self.path != ":memory:":
            target = Path(self.path)
            target.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        required = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='candidate_claim_versions'"
        ).fetchone()
        if required is None:
            raise ResearchReviewRejected("candidate staging schema is unavailable")
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        if self.path != ":memory:":
            os.chmod(self.path, 0o600)

    @_serialized
    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _load_record(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
        if row is None:
            raise ResearchReviewRejected(f"{name} is unavailable")
        try:
            return json.loads(row["record_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ResearchReviewConflict(f"{name} record is corrupt") from exc

    @_serialized
    def _candidate_pair(self, claim_version_ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
        claim_row = self.connection.execute(
            "SELECT * FROM candidate_claim_versions WHERE version_id=?", (claim_version_ref,)
        ).fetchone()
        claim = validate_candidate_claim(self._load_record(claim_row, "candidate claim"))
        if (
            claim_row is None
            or claim_row["record_json"] != canonical_json(claim)
            or claim_row["version_id"] != claim["id"]
            or claim_row["candidate_claim_ref"] != claim["candidate_claim_ref"]
            or claim_row["version_number"] != claim["version"]
            or claim_row["prior_version_id"] != claim["prior_version_ref"]
            or claim_row["content_hash"] != claim["content_hash"]
            or claim_row["created_at"] != claim["created_at"]
        ):
            raise ResearchReviewConflict("candidate claim columns drifted")
        evidence_ref = claim_row["evidence_version_id"] if claim_row is not None else None
        evidence_row = self.connection.execute(
            "SELECT * FROM candidate_evidence_versions WHERE version_id=?", (evidence_ref,)
        ).fetchone()
        evidence = validate_candidate_evidence(self._load_record(evidence_row, "candidate evidence"))
        if (
            evidence_row is None
            or evidence_row["record_json"] != canonical_json(evidence)
            or evidence_row["version_id"] != evidence["id"]
            or evidence_row["candidate_evidence_ref"] != evidence["candidate_evidence_ref"]
            or evidence_row["version_number"] != evidence["version"]
            or evidence_row["prior_version_id"] != evidence["prior_version_ref"]
            or evidence_row["content_hash"] != evidence["content_hash"]
            or evidence_row["created_at"] != evidence["created_at"]
        ):
            raise ResearchReviewConflict("candidate evidence columns drifted")
        expected = [{"ref": evidence["id"], "hash": evidence["content_hash"]}]
        if (
            claim_row["evidence_version_id"] != evidence["id"]
            or claim["candidate_evidence_refs"] != expected
        ):
            raise ResearchReviewConflict("candidate claim/evidence binding drifted")
        return claim, evidence

    @_serialized
    def list_candidates(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ResearchReviewError("limit must be 1..500")
        rows = self.connection.execute(
            "SELECT c.version_id,d.decision_json FROM candidate_claim_versions c "
            "LEFT JOIN human_review_decisions d ON d.candidate_claim_version_ref=c.version_id "
            "ORDER BY c.created_at,c.version_id LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for row in rows:
            claim, evidence = self._candidate_pair(row["version_id"])
            decision = None if row["decision_json"] is None else validate_human_review_decision(json.loads(row["decision_json"]))
            event = None if decision is None else self._current_commit_event(decision["id"])
            result.append({
                "claim": claim, "evidence": evidence, "decision": decision,
                "commit_state": None if event is None else event["state"],
            })
        return result

    @_serialized
    def candidate_bundle(self, candidate_claim_ref: str) -> dict[str, Any]:
        """Return one exact staged candidate pair without creating a review."""

        claim, evidence = self._candidate_pair(
            _text(candidate_claim_ref, "candidate_claim_ref")
        )
        return {"evidence": evidence, "claim": claim}

    @_serialized
    def candidate_status(self, candidate_claim_ref: str) -> dict[str, Any]:
        """Read one staged candidate back with its review decision and commit state.

        ``candidate_claim_ref`` may be the exact claim version id or the
        stable ``candidate_claim_ref``; the stable form resolves to the
        highest staged version.  Read-only; never creates a review.
        """

        ref = _text(candidate_claim_ref, "candidate_claim_ref")
        row = self.connection.execute(
            "SELECT version_id FROM candidate_claim_versions WHERE version_id=?", (ref,)
        ).fetchone()
        if row is None:
            row = self.connection.execute(
                "SELECT version_id FROM candidate_claim_versions WHERE candidate_claim_ref=? "
                "ORDER BY version_number DESC LIMIT 1", (ref,)
            ).fetchone()
        if row is None:
            raise ResearchReviewRejected("candidate claim is unavailable")
        claim, evidence = self._candidate_pair(row["version_id"])
        decision_row = self.connection.execute(
            "SELECT decision_json FROM human_review_decisions WHERE candidate_claim_version_ref=?",
            (claim["id"],),
        ).fetchone()
        decision = (
            None if decision_row is None
            else validate_human_review_decision(json.loads(decision_row["decision_json"]))
        )
        event = None if decision is None else self._current_commit_event(decision["id"])
        if decision is None:
            review_state = "staged"
        elif event is None:
            review_state = "decided"
        else:
            review_state = event["state"]
        return {
            "candidate_claim_ref": claim["id"],
            "candidate_claim_hash": claim["content_hash"],
            "candidate_evidence_ref": evidence["id"],
            "candidate_evidence_hash": evidence["content_hash"],
            "claim_kind": claim["claim_kind"],
            "review_state": review_state,
            "claim": claim,
            "evidence": evidence,
            "decision": decision,
            "commit_state": None if event is None else event["state"],
        }

    @_serialized
    def candidate_authority_bundle(
        self, candidate_claim_ref: str
    ) -> dict[str, Any]:
        """Return the exact staged source/numeric authorities for policy evaluation."""

        pair = self.candidate_bundle(candidate_claim_ref)
        claim = pair["claim"]

        def load(table: str, column: str, identifier: str, name: str) -> dict[str, Any]:
            row = self.connection.execute(
                f"SELECT record_json,content_hash FROM {table} WHERE {column}=?",
                (identifier,),
            ).fetchone()
            wire = self._load_record(row, name)
            if (
                row is None
                or row["record_json"] != canonical_json(wire)
                or row["content_hash"] != wire.get("content_hash")
                or wire.get("content_hash")
                != content_hash({
                    key: value for key, value in wire.items()
                    if key != "content_hash"
                })
            ):
                raise ResearchReviewConflict(f"{name} authority drifted")
            return wire

        source_verification = validate_verification_bundle(load(
            "candidate_verifications", "verification_id",
            claim["source_verification_ref"], "candidate source verification",
        ))
        if claim["claim_kind"] == "qualitative":
            # ADR-0003 option B: no numeric authority exists; the source
            # verification is the only staged authority and it binds the
            # material directly.
            numeric_spec = None
            numeric_verification = None
            material_ref = source_verification["subject_ref"]
            material_hash = source_verification["subject_hash"]
        else:
            numeric_spec = validate_numeric_verification_spec(load(
                "candidate_numeric_specs", "numeric_spec_id",
                claim["numeric_spec_ref"], "candidate numeric spec",
            ))
            numeric_verification = validate_verification_bundle(load(
                "candidate_verifications", "verification_id",
                claim["numeric_verification_ref"], "candidate numeric verification",
            ))
            material_refs = {
                (item["source_material_ref"], item["source_material_hash"])
                for item in numeric_spec["inputs"]
            }
            if len(material_refs) != 1:
                raise ResearchReviewConflict(
                    "candidate numeric spec does not bind one exact source material"
                )
            material_ref, material_hash = next(iter(material_refs))
        material = validate_source_verification_material(load(
            "candidate_source_materials", "material_id", material_ref,
            "candidate source material",
        ))
        if (
            material["content_hash"] != material_hash
            or claim["source_verification_hash"]
            != source_verification["content_hash"]
            or source_verification["subject_ref"] != material["id"]
            or source_verification["subject_hash"] != material["content_hash"]
            or (
                numeric_spec is not None
                and claim["numeric_spec_hash"] != numeric_spec["content_hash"]
            )
            or (
                numeric_verification is not None
                and claim["numeric_verification_hash"]
                != numeric_verification["content_hash"]
            )
        ):
            raise ResearchReviewConflict(
                "candidate authority bundle hash binding drifted"
            )
        return {
            **pair,
            "material": material,
            "numeric_spec": numeric_spec,
            "source_verification": source_verification,
            "numeric_verification": numeric_verification,
        }

    @_serialized
    def decide(
        self,
        *,
        candidate_claim_ref: str,
        candidate_claim_hash: str,
        verdict: str,
        reviewed_semantics: Mapping[str, Any],
        rationale: str,
        findings: Sequence[str],
        reviewer_ref: str,
        source_event_ref: str,
        idempotency_key: str,
        created_at: str,
        proposed_revisions: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        claim, evidence = self._candidate_pair(_text(candidate_claim_ref, "candidate_claim_ref"))
        if claim["content_hash"] != _hash(candidate_claim_hash, "candidate_claim_hash"):
            raise ResearchReviewConflict("candidate claim hash drifted")
        semantics = _semantics(reviewed_semantics, "reviewed_semantics")
        if canonical_json(semantics) != canonical_json(_candidate_semantics(claim)):
            raise ResearchReviewConflict("reviewed semantics do not bind the candidate exactly")
        if verdict == "revise":
            if not isinstance(proposed_revisions, Mapping) or not proposed_revisions:
                raise ResearchReviewError("revise requires proposed_revisions")
            if set(proposed_revisions) - set(_CONSUMABLE_REVISION_FIELDS):
                raise ResearchReviewRejected(
                    "in-place revision currently supports normalized_statement only; "
                    "source, numeric or period changes require a new verified plan run"
                )
            if all(
                field not in proposed_revisions
                or canonical_json(proposed_revisions[field]) == canonical_json(claim[field])
                for field in proposed_revisions
            ):
                raise ResearchReviewError("proposed_revisions must change candidate semantics")
        base = {
            "schema_version": SCHEMA_VERSION,
            "id": "human-review:" + content_hash({
                "candidate_claim_ref": claim["id"], "candidate_claim_hash": claim["content_hash"],
                "reviewer_ref": reviewer_ref, "source_event_ref": source_event_ref,
            }),
            "created_at": _timestamp(created_at, "created_at"),
            "candidate_claim_ref": claim["id"],
            "candidate_claim_hash": claim["content_hash"],
            "candidate_evidence_ref": evidence["id"],
            "candidate_evidence_hash": evidence["content_hash"],
            "verdict": verdict,
            "reviewed_semantics": semantics,
            "proposed_revisions": None if proposed_revisions is None else dict(proposed_revisions),
            "relation": "supports",
            "rationale": rationale,
            "findings": list(findings),
            "reviewer_ref": reviewer_ref,
            "authorization": "explicit_human_review",
            "source": "tailscale_review",
            "source_event_ref": source_event_ref,
        }
        base["content_hash"] = content_hash(base)
        decision = validate_human_review_decision(base)
        key = _text(idempotency_key, "idempotency_key")
        request_hash = content_hash({
            "decision": {
                field: value for field, value in decision.items()
                if field not in {"created_at", "content_hash"}
            },
            "idempotency_key": key,
        })
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            prior = self.connection.execute(
                "SELECT request_hash,result_json FROM human_review_requests WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != request_hash:
                    raise ResearchReviewConflict("human review idempotency conflict")
                result = json.loads(prior["result_json"])
                self.connection.execute("COMMIT")
                return {**result, "write_status": "duplicate"}
            existing = self.connection.execute(
                "SELECT decision_json FROM human_review_decisions WHERE candidate_claim_version_ref=?",
                (claim["id"],),
            ).fetchone()
            if existing is not None:
                raise ResearchReviewConflict("candidate version already has a terminal decision")
            self.connection.execute(
                "INSERT INTO human_review_decisions(decision_id,candidate_claim_version_ref,"
                "candidate_evidence_version_ref,verdict,reviewer_ref,decision_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    decision["id"], claim["id"], evidence["id"], decision["verdict"],
                    decision["reviewer_ref"], canonical_json(decision), decision["content_hash"],
                    decision["created_at"],
                ),
            )
            if decision["verdict"] == "accept":
                event = self._commit_event(
                    decision["id"], "queued", created_at=decision["created_at"]
                )
                commit_state = event["state"]
            else:
                commit_state = "not_applicable"
            result = {
                "write_status": "fresh", "decision_ref": decision["id"],
                "decision_hash": decision["content_hash"], "verdict": decision["verdict"],
                "commit_state": commit_state,
            }
            self.connection.execute(
                "INSERT INTO human_review_requests(idempotency_key,request_hash,result_json,created_at) "
                "VALUES(?,?,?,?)",
                (key, request_hash, canonical_json(result), decision["created_at"]),
            )
            self.connection.execute("COMMIT")
            return result
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    @_serialized
    def _current_commit_event(self, decision_ref: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT event_id,state FROM human_review_commit_events WHERE decision_ref=? "
            "AND event_id NOT IN (SELECT prior_event_ref FROM human_review_commit_events "
            "WHERE prior_event_ref IS NOT NULL) LIMIT 1", (decision_ref,)
        ).fetchone()

    @_serialized
    def _commit_event(
        self,
        decision_ref: str,
        state: str,
        *,
        created_at: str,
        ledger_result: Mapping[str, Any] | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        previous = self._current_commit_event(decision_ref)
        prior = None if previous is None else previous["event_id"]
        if state == "queued" and previous is not None:
            raise ResearchReviewConflict("commit intent is already queued")
        if state in {"committed", "failed"} and previous is None:
            raise ResearchReviewConflict("commit result has no queued intent")
        if previous is not None and previous["state"] == "committed":
            raise ResearchReviewConflict("committed review cannot transition again")
        if state == "committed" and ledger_result is None:
            raise ResearchReviewError("committed event requires ledger_result")
        if state == "failed" and (not isinstance(error_code, str) or not error_code):
            raise ResearchReviewError("failed event requires error_code")
        if state not in {"queued", "committed", "failed"}:
            raise ResearchReviewError("commit event state is invalid")
        base = {
            "schema_version": SCHEMA_VERSION,
            "id": "human-review-commit-event:" + content_hash({
                "decision_ref": decision_ref, "state": state, "prior": prior,
                "ledger_result": ledger_result, "error_code": error_code,
            }),
            "created_at": _timestamp(created_at, "created_at"),
            "decision_ref": decision_ref,
            "state": state,
            "prior_event_ref": prior,
            "ledger_result": None if ledger_result is None else _json(ledger_result, "ledger_result"),
            "error_code": error_code,
        }
        base["content_hash"] = content_hash(base)
        self.connection.execute(
            "INSERT INTO human_review_commit_events(event_id,decision_ref,state,prior_event_ref,"
            "ledger_result_json,error_code,event_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                base["id"], decision_ref, state, prior,
                None if ledger_result is None else canonical_json(ledger_result), error_code,
                canonical_json(base), base["content_hash"], base["created_at"],
            ),
        )
        return base

    @_serialized
    def decision_bundle(self, decision_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM human_review_decisions WHERE decision_id=?",
            (_text(decision_ref, "decision_ref"),),
        ).fetchone()
        if row is None:
            raise ResearchReviewRejected("review decision is unavailable")
        decision = validate_human_review_decision(json.loads(row["decision_json"]))
        if (
            row["decision_json"] != canonical_json(decision)
            or row["decision_id"] != decision["id"]
            or row["candidate_claim_version_ref"] != decision["candidate_claim_ref"]
            or row["candidate_evidence_version_ref"] != decision["candidate_evidence_ref"]
            or row["verdict"] != decision["verdict"]
            or row["reviewer_ref"] != decision["reviewer_ref"]
            or row["content_hash"] != decision["content_hash"]
            or row["created_at"] != decision["created_at"]
        ):
            raise ResearchReviewConflict("human review decision columns drifted")
        claim, evidence = self._candidate_pair(row["candidate_claim_version_ref"])
        return {"decision": decision, "evidence": evidence, "claim": claim}

    @_serialized
    def consume_revision(self, decision_ref: str) -> dict[str, Any]:
        """Consume one exact revise decision into the next candidate claim version.

        This is deliberately not a general replanner.  The current slice can
        rewrite only the human-reviewed statement while retaining the exact
        source, numeric result, period and evidence provenance of the plan's
        verified candidate.
        """

        bundle = self.decision_bundle(decision_ref)
        decision = bundle["decision"]
        prior_claim = bundle["claim"]
        evidence = bundle["evidence"]
        if decision["verdict"] != "revise":
            raise ResearchReviewRejected(
                "only a revise decision can be consumed as candidate rework"
            )
        if self.commit_event(decision["id"]) is not None:
            raise ResearchReviewConflict(
                "a revise decision cannot carry a Ledger commit event"
            )
        revised = _revised_candidate_claim(prior_claim, decision)

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            children = self.connection.execute(
                "SELECT version_id,evidence_version_id FROM candidate_claim_versions "
                "WHERE prior_version_id=? ORDER BY version_id",
                (prior_claim["id"],),
            ).fetchall()
            if children:
                if len(children) != 1:
                    raise ResearchReviewConflict("candidate revision chain forked")
                existing, existing_evidence = self._candidate_pair(
                    children[0]["version_id"]
                )
                if (
                    canonical_json(existing) != canonical_json(revised)
                    or children[0]["evidence_version_id"] != evidence["id"]
                    or canonical_json(existing_evidence) != canonical_json(evidence)
                ):
                    raise ResearchReviewConflict(
                        "existing candidate revision drifted from the human request"
                    )
                self.connection.execute("COMMIT")
                return {
                    "write_status": "duplicate",
                    "review_decision_ref": decision["id"],
                    "prior_candidate_claim_ref": prior_claim["id"],
                    "candidate_claim_ref": existing["id"],
                    "candidate_claim_hash": existing["content_hash"],
                    "candidate_evidence_ref": evidence["id"],
                    "candidate_evidence_hash": evidence["content_hash"],
                }
            latest = self.connection.execute(
                "SELECT version_id,version_number FROM candidate_claim_versions "
                "WHERE candidate_claim_ref=? ORDER BY version_number DESC LIMIT 1",
                (prior_claim["candidate_claim_ref"],),
            ).fetchone()
            if (
                latest is None
                or latest["version_id"] != prior_claim["id"]
                or latest["version_number"] != prior_claim["version"]
            ):
                raise ResearchReviewConflict(
                    "revision decision does not bind the current candidate head"
                )
            self.connection.execute(
                "INSERT INTO candidate_claim_versions("
                "version_id,candidate_claim_ref,version_number,prior_version_id,"
                "evidence_version_id,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    revised["id"], revised["candidate_claim_ref"],
                    revised["version"], revised["prior_version_ref"], evidence["id"],
                    canonical_json(revised), revised["content_hash"], revised["created_at"],
                ),
            )
            self.connection.execute("COMMIT")
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        return {
            "write_status": "fresh",
            "review_decision_ref": decision["id"],
            "prior_candidate_claim_ref": prior_claim["id"],
            "candidate_claim_ref": revised["id"],
            "candidate_claim_hash": revised["content_hash"],
            "candidate_evidence_ref": evidence["id"],
            "candidate_evidence_hash": evidence["content_hash"],
        }

    @_serialized
    def revision_lineage(self, candidate_claim_ref: str) -> dict[str, Any]:
        """Re-read and validate the complete lineage ending at one candidate head."""

        claim, evidence = self._candidate_pair(
            _text(candidate_claim_ref, "candidate_claim_ref")
        )
        if self.connection.execute(
            "SELECT 1 FROM candidate_claim_versions WHERE prior_version_id=? LIMIT 1",
            (claim["id"],),
        ).fetchone() is not None:
            raise ResearchReviewConflict("requested candidate is not the revision head")
        claims = [claim]
        evidences = [evidence]
        decisions: list[dict[str, Any]] = []
        visited = {claim["id"]}
        while claim["prior_version_ref"] is not None:
            parent, parent_evidence = self._candidate_pair(
                claim["prior_version_ref"]
            )
            if parent["id"] in visited:
                raise ResearchReviewConflict("candidate revision lineage contains a cycle")
            visited.add(parent["id"])
            row = self.connection.execute(
                "SELECT decision_id FROM human_review_decisions "
                "WHERE candidate_claim_version_ref=?",
                (parent["id"],),
            ).fetchone()
            if row is None:
                raise ResearchReviewConflict(
                    "candidate revision has no exact human revise decision"
                )
            decision = self.decision_bundle(row["decision_id"])["decision"]
            expected = _revised_candidate_claim(parent, decision)
            siblings = self.connection.execute(
                "SELECT version_id FROM candidate_claim_versions "
                "WHERE prior_version_id=? ORDER BY version_id",
                (parent["id"],),
            ).fetchall()
            if (
                len(siblings) != 1
                or siblings[0]["version_id"] != claim["id"]
                or canonical_json(expected) != canonical_json(claim)
                or canonical_json(parent_evidence) != canonical_json(evidence)
            ):
                raise ResearchReviewConflict(
                    "candidate revision lineage drifted or forked"
                )
            decisions.append(decision)
            claims.append(parent)
            evidences.append(parent_evidence)
            claim, evidence = parent, parent_evidence
        claims.reverse()
        evidences.reverse()
        decisions.reverse()
        return {
            "claims": claims,
            "evidences": evidences,
            "revision_decisions": decisions,
        }

    @_serialized
    def commit_event(self, decision_ref: str) -> dict[str, Any] | None:
        """Return the exact current commit event after validating its full chain."""

        decision_ref = _text(decision_ref, "decision_ref")
        decision_row = self.connection.execute(
            "SELECT decision_json FROM human_review_decisions WHERE decision_id=?",
            (decision_ref,),
        ).fetchone()
        if decision_row is None:
            raise ResearchReviewRejected("review decision is unavailable")
        decision = validate_human_review_decision(
            json.loads(decision_row["decision_json"])
        )
        rows = self.connection.execute(
            "SELECT * FROM human_review_commit_events WHERE decision_ref=? "
            "ORDER BY created_at,event_id",
            (decision_ref,),
        ).fetchall()
        if not rows:
            return None
        events: dict[str, dict[str, Any]] = {}
        children: dict[str, str] = {}
        for row in rows:
            try:
                event = validate_human_review_commit_event(
                    json.loads(row["event_json"])
                )
            except (TypeError, json.JSONDecodeError) as exc:
                raise ResearchReviewConflict(
                    "human review commit event record is corrupt"
                ) from exc
            expected_ledger = (
                None
                if event["ledger_result"] is None
                else canonical_json(event["ledger_result"])
            )
            if (
                event["id"] != row["event_id"]
                or event["decision_ref"] != row["decision_ref"]
                or event["state"] != row["state"]
                or event["prior_event_ref"] != row["prior_event_ref"]
                or expected_ledger != row["ledger_result_json"]
                or event["error_code"] != row["error_code"]
                or event["content_hash"] != row["content_hash"]
                or event["created_at"] != row["created_at"]
                or canonical_json(event) != row["event_json"]
            ):
                raise ResearchReviewConflict(
                    "human review commit event columns drifted"
                )
            if event["decision_ref"] != decision["id"]:
                raise ResearchReviewConflict(
                    "human review commit event binds another decision"
                )
            prior = event["prior_event_ref"]
            if prior is not None:
                if prior in children:
                    raise ResearchReviewConflict(
                        "human review commit event chain forked"
                    )
                children[prior] = event["id"]
            events[event["id"]] = event
        roots = [event for event in events.values() if event["prior_event_ref"] is None]
        heads = [event for event in events.values() if event["id"] not in children]
        if len(roots) != 1 or len(heads) != 1 or roots[0]["state"] != "queued":
            raise ResearchReviewConflict(
                "human review commit event chain is not linear"
            )
        current = roots[0]
        visited = {current["id"]}
        while current["id"] in children:
            current = events.get(children[current["id"]])
            if current is None or current["id"] in visited or current["state"] == "queued":
                raise ResearchReviewConflict(
                    "human review commit event chain is invalid"
                )
            visited.add(current["id"])
        if len(visited) != len(events) or current["id"] != heads[0]["id"]:
            raise ResearchReviewConflict(
                "human review commit event chain is disconnected"
            )
        return current

    @_serialized
    def pending_commits(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ResearchReviewError("limit must be 1..500")
        rows = self.connection.execute(
            "SELECT d.decision_id FROM human_review_decisions d WHERE d.verdict='accept' "
            "AND COALESCE((SELECT e.state FROM human_review_commit_events e "
            "WHERE e.decision_ref=d.decision_id AND e.event_id NOT IN "
            "(SELECT child.prior_event_ref FROM human_review_commit_events child "
            "WHERE child.prior_event_ref IS NOT NULL) LIMIT 1),'queued') "
            "IN ('queued','failed') ORDER BY d.created_at,d.decision_id LIMIT ?", (limit,)
        ).fetchall()
        return [self.decision_bundle(row["decision_id"]) for row in rows]

    @_serialized
    def record_commit_result(
        self,
        decision_ref: str,
        *,
        created_at: str,
        ledger_result: Mapping[str, Any] | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            state = "committed" if ledger_result is not None else "failed"
            event = self._commit_event(
                _text(decision_ref, "decision_ref"), state, created_at=created_at,
                ledger_result=ledger_result, error_code=error_code,
            )
            self.connection.execute("COMMIT")
            return event
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    @_serialized
    def counts(self) -> dict[str, int]:
        return {
            table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "human_review_decisions", "human_review_requests", "human_review_commit_events",
            )
        }


__all__ = [
    "HumanReviewAuthority", "ResearchReviewError", "ResearchReviewConflict",
    "ResearchReviewRejected", "validate_human_review_decision",
    "validate_human_review_commit_event",
    "validate_evidence_version_v0_2", "validate_claim_version_v0_2",
]
