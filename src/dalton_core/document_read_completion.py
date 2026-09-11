"""Append-only proof that every window of one document was read successfully."""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import authorization_flag, canonical_json, content_hash

_SCHEMA = Path(__file__).with_name("document_read_completion_schema.sql")
_SHA = re.compile(r"[0-9a-f]{64}")
_WINDOW_FIELDS = frozenset({
    "offset", "context_ref", "context_hash", "next_offset", "source_content_hash",
    "work_order_ref", "result_envelope_ref", "result_envelope_hash", "status",
    "source_review_hash",
})

class DocumentReadCompletionError(ValueError): pass


def review_wire(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in (
        "review_id", "mission_version_ref", "company_ref", "source_ref", "document_ref",
        "discovered_document_ref", "state", "candidate_claim_version_ref", "rationale",
        "registered_by", "created_at", "updated_at")}


def _window(value: Mapping[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _WINDOW_FIELDS:
        raise DocumentReadCompletionError(f"windows[{index}] has an invalid closed shape")
    out = dict(value)
    if type(out["offset"]) is not int or out["offset"] < 0:
        raise DocumentReadCompletionError(f"windows[{index}].offset is invalid")
    if out["next_offset"] is not None and (type(out["next_offset"]) is not int or out["next_offset"] <= out["offset"]):
        raise DocumentReadCompletionError(f"windows[{index}].next_offset is invalid")
    for key in ("context_hash", "source_content_hash", "result_envelope_hash",
                "source_review_hash"):
        if not isinstance(out[key], str) or _SHA.fullmatch(out[key]) is None:
            raise DocumentReadCompletionError(f"windows[{index}].{key} is invalid")
    for key in ("context_ref", "work_order_ref", "result_envelope_ref"):
        if not isinstance(out[key], str) or not out[key]:
            raise DocumentReadCompletionError(f"windows[{index}].{key} is invalid")
    if out["status"] != "succeeded":
        raise DocumentReadCompletionError(f"windows[{index}] did not succeed")
    return out


class DocumentReadCompletionAuthority:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self._authorization = authorization_flag(
            connection, "dalton_document_read_completion_authorized")
        self.connection.executescript(_SCHEMA.read_text())

    def record(self, *, review_id: str, source_review_hash: str,
               actor_ref: str,
               windows: Sequence[Mapping[str, Any]],
               receipt_reader: Any,
               created_at: str | None = None) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM coverage_mission_document_reviews WHERE review_id=?", (review_id,)
        ).fetchone()
        if row is None:
            raise DocumentReadCompletionError("review does not exist")
        source_review = review_wire(row)
        existing = self.connection.execute(
            "SELECT record_json FROM document_read_completion_proofs WHERE review_id=?", (review_id,)
        ).fetchone()
        if existing is None and source_review["state"] != "awaiting_human_extraction":
            raise DocumentReadCompletionError("completion proof must precede review resolution")
        if existing is None and content_hash(source_review) != source_review_hash:
            raise DocumentReadCompletionError("source review hash drifted")
        mission_row = self.connection.execute(
            "SELECT record_json FROM coverage_mission_versions WHERE mission_version_id=?",
            (source_review["mission_version_ref"],),
        ).fetchone()
        if mission_row is None:
            raise DocumentReadCompletionError("mission version does not exist")
        mission = json.loads(mission_row["record_json"])
        if actor_ref != (mission.get("autonomy") or {}).get("automation_principal"):
            raise DocumentReadCompletionError("completion requires the mission automation principal")
        if not isinstance(source_review_hash, str) or _SHA.fullmatch(source_review_hash) is None:
            raise DocumentReadCompletionError("source review hash is invalid")
        if not windows:
            raise DocumentReadCompletionError("completion requires at least one window")
        verified = []
        expected_offset = 0
        source_hash = None
        for index, claimed in enumerate(windows):
            normalized = _window(claimed, index)
            reader = getattr(receipt_reader, "read_completion_receipt", None)
            if not callable(reader):
                raise DocumentReadCompletionError("an authoritative completion receipt reader is required")
            authoritative = _window(reader(
                review_id=review_id, source_review_hash=source_review_hash,
                offset=normalized["offset"], actor_ref=actor_ref), index)
            if authoritative != normalized:
                raise DocumentReadCompletionError(f"windows[{index}] authority binding drifted")
            if normalized["source_review_hash"] != source_review_hash:
                raise DocumentReadCompletionError("window is bound to a different source review")
            if normalized["offset"] != expected_offset:
                raise DocumentReadCompletionError("window chain is not contiguous")
            if source_hash is None:
                source_hash = normalized["source_content_hash"]
            elif source_hash != normalized["source_content_hash"]:
                raise DocumentReadCompletionError("source bytes changed between windows")
            expected_offset = normalized["next_offset"]
            verified.append(normalized)
        if expected_offset is not None:
            raise DocumentReadCompletionError("window chain is incomplete")
        if existing is not None:
            saved = json.loads(existing["record_json"])
            if (saved.get("source_review_hash") != source_review_hash
                    or saved.get("actor_ref") != actor_ref
                    or saved.get("windows") != verified):
                raise DocumentReadCompletionError("review already has a different completion proof")
            return {"status": "duplicate", **saved}
        at = created_at or datetime.now(timezone.utc).isoformat(timespec="microseconds")
        body = {"schema_version": "0.1", "review_id": review_id,
                "source_review_hash": source_review_hash,
                "source_review": source_review if existing is None else json.loads(existing["record_json"])["source_review"],
                "mission_version_ref": source_review["mission_version_ref"],
                "company_ref": source_review["company_ref"], "document_ref": source_review["document_ref"],
                "actor_ref": actor_ref, "windows": verified, "created_at": at}
        wire = {**body, "proof_id": "document-read-proof:" + content_hash(body)[:32]}
        wire["content_hash"] = content_hash(wire)
        self._authorization.authorized = True
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO document_read_completion_proofs(proof_id,review_id,source_review_hash,document_ref,company_ref,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (wire["proof_id"], review_id, source_review_hash, source_review["document_ref"],
                     source_review["company_ref"], canonical_json(wire), wire["content_hash"], at))
        finally:
            self._authorization.authorized = False
        return {"status": "fresh", **wire}
