"""Human-admitted correction overlays for immutable transcript captures.

The captured transcript is never edited in place.  A correction set binds
exact raw spans to exact evidence authorities and records either an accepted
replacement or an unresolved flag.  Numeric, negation, speaker, and semantic
changes require utterance-level evidence; a filing that merely looks
consistent cannot be used to rewrite what a speaker said.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .alphaengine_document_acquisition import (
    validate_alphaengine_document_acquisition_manifest,
)
from .raw_spool import RawSpool
from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("transcript_correction_schema.sql")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_HUMAN_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._/@:-]*$")
_KINDS = frozenset({
    "proper_name", "terminology", "numeric", "negation", "semantic",
    "speaker_label",
})
_DISPOSITIONS = frozenset({"accepted", "unresolved"})
_EVIDENCE_KINDS = frozenset({
    "audio_span", "official_transcript_span", "primary_reference",
})
_UTTERANCE_EVIDENCE_KINDS = frozenset({
    "audio_span", "official_transcript_span",
})
_UTTERANCE_LEVEL_CORRECTIONS = frozenset({
    "numeric", "negation", "semantic", "speaker_label",
})
_REVIEW_SCOPES = frozenset({"targeted_flags", "full_document"})


class TranscriptCorrectionError(RuntimeError):
    pass


class TranscriptCorrectionValidationError(TranscriptCorrectionError, ValueError):
    pass


class TranscriptCorrectionConflict(TranscriptCorrectionError):
    pass


class TranscriptCorrectionNotFound(TranscriptCorrectionError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TranscriptCorrectionValidationError(f"{name} must be non-empty text")
    return value.strip()


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise TranscriptCorrectionValidationError(f"{name} must be lowercase SHA-256")
    return value


def _human(value: Any) -> str:
    value = _text(value, "actor_ref")
    if _HUMAN_RE.fullmatch(value) is None:
        raise TranscriptCorrectionValidationError(
            "correction admission requires a human: actor"
        )
    return value


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TranscriptCorrectionValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) != fields:
        raise TranscriptCorrectionValidationError(
            f"{name} has invalid closed shape; "
            f"missing={sorted(fields - set(wire))}, "
            f"unknown={sorted(set(wire) - fields)}"
        )
    return wire


def _record(base: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(base)
    wire["content_hash"] = content_hash(wire)
    return wire


class TranscriptCorrectionAuthority:
    """Publish and resolve immutable, evidence-bound correction sets."""

    def __init__(
        self,
        store: Any,
        *,
        spool: RawSpool,
        manifest_resolver: Callable[[str], Mapping[str, Any]],
        evidence_resolver: Callable[[str], Mapping[str, Any]],
    ) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("TranscriptCorrectionAuthority requires a DaltonStore")
        if not isinstance(spool, RawSpool):
            raise TypeError("TranscriptCorrectionAuthority requires a RawSpool")
        if not callable(manifest_resolver) or not callable(evidence_resolver):
            raise TypeError("correction resolvers must be callable")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.spool = spool
        self.manifest_resolver = manifest_resolver
        self.evidence_resolver = evidence_resolver
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def correction_set(self, version_ref: str) -> dict[str, Any]:
        version_ref = _text(version_ref, "correction_set_version_ref")
        row = self.connection.execute(
            "SELECT * FROM transcript_correction_set_versions WHERE version_id=?",
            (version_ref,),
        ).fetchone()
        if row is None:
            raise TranscriptCorrectionNotFound(version_ref)
        try:
            wire = json.loads(row["record_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise TranscriptCorrectionConflict("correction set JSON is invalid") from exc
        if not isinstance(wire, dict) or canonical_json(wire) != row["record_json"]:
            raise TranscriptCorrectionConflict("correction set JSON is not canonical")
        body = dict(wire)
        asserted = body.pop("content_hash", None)
        if asserted != content_hash(body) or asserted != row["content_hash"]:
            raise TranscriptCorrectionConflict("correction set content hash drifted")
        checks = {
            "version_id": wire.get("id"),
            "correction_set_ref": wire.get("correction_set_ref"),
            "version_number": wire.get("version"),
            "prior_version_id": wire.get("prior_version_ref"),
            "source_manifest_ref": wire.get("source_manifest_ref"),
            "source_content_hash": wire.get("source_content_hash"),
            "actor_ref": wire.get("actor_ref"),
            "created_at": wire.get("created_at"),
        }
        if any(row[column] != expected for column, expected in checks.items()):
            raise TranscriptCorrectionConflict("correction set SQL projection drifted")
        return wire

    def _source(
        self,
        source_manifest_ref: str,
        source_manifest_hash: str,
        source_content_hash: str,
    ) -> tuple[dict[str, Any], str]:
        try:
            manifest = validate_alphaengine_document_acquisition_manifest(
                self.manifest_resolver(source_manifest_ref)
            )
        except Exception as exc:
            raise TranscriptCorrectionNotFound(
                f"source manifest {source_manifest_ref} is unavailable or invalid"
            ) from exc
        if (
            manifest["id"] != source_manifest_ref
            or manifest["content_hash"] != source_manifest_hash
            or manifest["status"] != "complete"
            or manifest["termination_reason"] != "terminal"
            or manifest["assembled_object"] is None
            or manifest["assembled_object"]["content_hash"] != source_content_hash
            or manifest["assembled_prefix_sha256"] != source_content_hash
            or manifest["declared_content_sha256"] != source_content_hash
        ):
            raise TranscriptCorrectionConflict(
                "correction source is not exact complete authority"
            )
        raw = self.spool.read_object(source_content_hash)
        if (
            len(raw) != manifest["assembled_object"]["size_bytes"]
            or hashlib.sha256(raw).hexdigest() != source_content_hash
        ):
            raise TranscriptCorrectionConflict("correction source bytes drifted")
        try:
            original = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TranscriptCorrectionValidationError(
                "correction source must be UTF-8"
            ) from exc
        if not original or manifest["content_chars"] != len(original):
            raise TranscriptCorrectionConflict("correction source character count drifted")
        return manifest, original

    def _evidence_binding(self, value: Any, name: str) -> dict[str, str]:
        item = _closed(
            value,
            {"authority_ref", "authority_hash", "evidence_kind", "location"},
            name,
        )
        authority_ref = _text(item["authority_ref"], f"{name}.authority_ref")
        authority_hash = _hash(item["authority_hash"], f"{name}.authority_hash")
        evidence_kind = item["evidence_kind"]
        if evidence_kind not in _EVIDENCE_KINDS:
            raise TranscriptCorrectionValidationError(
                f"{name}.evidence_kind is unsupported"
            )
        location = _text(item["location"], f"{name}.location")
        try:
            authority = self.evidence_resolver(authority_ref)
        except Exception as exc:
            raise TranscriptCorrectionNotFound(
                f"correction evidence {authority_ref} is unavailable"
            ) from exc
        if (
            not isinstance(authority, Mapping)
            or authority.get("id") != authority_ref
            or authority.get("content_hash") != authority_hash
        ):
            raise TranscriptCorrectionConflict(
                "correction evidence does not bind exact authority"
            )
        return {
            "authority_ref": authority_ref,
            "authority_hash": authority_hash,
            "evidence_kind": evidence_kind,
            "location": location,
        }

    def _corrections(
        self, corrections: Sequence[Mapping[str, Any]], original: str
    ) -> list[dict[str, Any]]:
        if not isinstance(corrections, (list, tuple)) or not 1 <= len(corrections) <= 256:
            raise TranscriptCorrectionValidationError(
                "corrections must contain between 1 and 256 entries"
            )
        normalized: list[dict[str, Any]] = []
        previous_end = 0
        for index, raw in enumerate(corrections):
            name = f"corrections[{index}]"
            item = _closed(
                raw,
                {
                    "source_start", "source_end", "source_sha256",
                    "correction_kind", "disposition", "replacement_text",
                    "rationale", "evidence_bindings",
                },
                name,
            )
            start = item["source_start"]
            end = item["source_end"]
            if (
                isinstance(start, bool) or not isinstance(start, int)
                or isinstance(end, bool) or not isinstance(end, int)
                or start < previous_end or end <= start or end > len(original)
            ):
                raise TranscriptCorrectionValidationError(
                    "correction spans must be ordered, non-overlapping, and in bounds"
                )
            source_slice = original[start:end]
            source_sha256 = _hash(item["source_sha256"], f"{name}.source_sha256")
            if hashlib.sha256(source_slice.encode("utf-8")).hexdigest() != source_sha256:
                raise TranscriptCorrectionConflict("correction source span hash drifted")
            kind = item["correction_kind"]
            disposition = item["disposition"]
            if kind not in _KINDS or disposition not in _DISPOSITIONS:
                raise TranscriptCorrectionValidationError(
                    "correction kind or disposition is unsupported"
                )
            replacement = item["replacement_text"]
            if disposition == "accepted":
                replacement = _text(replacement, f"{name}.replacement_text")
                if replacement == source_slice:
                    raise TranscriptCorrectionValidationError(
                        "accepted correction must change the source text"
                    )
            elif replacement is not None:
                replacement = _text(replacement, f"{name}.replacement_text")
            evidence_raw = item["evidence_bindings"]
            if not isinstance(evidence_raw, (list, tuple)):
                raise TranscriptCorrectionValidationError(
                    f"{name}.evidence_bindings must be an array"
                )
            evidence = [
                self._evidence_binding(binding, f"{name}.evidence_bindings[{offset}]")
                for offset, binding in enumerate(evidence_raw)
            ]
            refs = [binding["authority_ref"] for binding in evidence]
            if len(refs) != len(set(refs)):
                raise TranscriptCorrectionValidationError(
                    "correction evidence authorities must be unique per entry"
                )
            if disposition == "accepted":
                evidence_kinds = {binding["evidence_kind"] for binding in evidence}
                if not evidence_kinds:
                    raise TranscriptCorrectionValidationError(
                        "accepted correction requires exact evidence authority"
                    )
                if (
                    kind in _UTTERANCE_LEVEL_CORRECTIONS
                    and not evidence_kinds.intersection(_UTTERANCE_EVIDENCE_KINDS)
                ):
                    raise TranscriptCorrectionValidationError(
                        "utterance-level correction requires audio or official transcript evidence"
                    )
            normalized.append({
                "source_start": start,
                "source_end": end,
                "source_sha256": source_sha256,
                "source_text": source_slice,
                "correction_kind": kind,
                "disposition": disposition,
                "replacement_text": replacement,
                "rationale": _text(item["rationale"], f"{name}.rationale"),
                "evidence_bindings": evidence,
            })
            previous_end = end
        return normalized

    def publish(
        self,
        correction_set_ref: str,
        *,
        source_manifest_ref: str,
        source_manifest_hash: str,
        source_content_hash: str,
        review_scope: str,
        corrections: Sequence[Mapping[str, Any]],
        actor_ref: str,
        prior_version_ref: str | None = None,
    ) -> dict[str, Any]:
        correction_set_ref = _text(correction_set_ref, "correction_set_ref")
        source_manifest_ref = _text(source_manifest_ref, "source_manifest_ref")
        source_manifest_hash = _hash(source_manifest_hash, "source_manifest_hash")
        source_content_hash = _hash(source_content_hash, "source_content_hash")
        actor_ref = _human(actor_ref)
        if review_scope not in _REVIEW_SCOPES:
            raise TranscriptCorrectionValidationError("review_scope is unsupported")
        manifest, original = self._source(
            source_manifest_ref, source_manifest_hash, source_content_hash
        )
        normalized = self._corrections(corrections, original)
        latest = self.connection.execute(
            "SELECT * FROM transcript_correction_set_versions "
            "WHERE correction_set_ref=? ORDER BY version_number DESC LIMIT 1",
            (correction_set_ref,),
        ).fetchone()
        stable = {
            "document_ref": manifest["document_ref"],
            "source_manifest_ref": source_manifest_ref,
            "source_manifest_hash": source_manifest_hash,
            "source_content_hash": source_content_hash,
            "review_scope": review_scope,
            "corrections": normalized,
        }
        if latest is None:
            if prior_version_ref is not None:
                raise TranscriptCorrectionConflict(
                    "first correction set cannot have a prior version"
                )
            version = 1
        else:
            latest_wire = self.correction_set(latest["version_id"])
            if prior_version_ref is None and all(
                latest_wire[key] == value for key, value in stable.items()
            ):
                return {"status": "duplicate", **latest_wire}
            if prior_version_ref != latest_wire["id"]:
                raise TranscriptCorrectionConflict(
                    "correction set must continue the latest version"
                )
            if (
                latest_wire["source_manifest_ref"] != source_manifest_ref
                or latest_wire["source_content_hash"] != source_content_hash
            ):
                raise TranscriptCorrectionConflict(
                    "correction set versions cannot switch source authority"
                )
            version = latest_wire["version"] + 1
        identity = {
            "correction_set_ref": correction_set_ref,
            "version": version,
            "prior_version_ref": prior_version_ref,
            **stable,
        }
        version_id = (
            "transcript-correction-set-version:" + content_hash(identity)[:32]
        )
        wire = _record({
            "schema_version": SCHEMA_VERSION,
            "id": version_id,
            "created_at": _now(),
            **identity,
            "accepted_count": sum(
                item["disposition"] == "accepted" for item in normalized
            ),
            "unresolved_count": sum(
                item["disposition"] == "unresolved" for item in normalized
            ),
            "actor_ref": actor_ref,
        })
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO transcript_correction_set_versions "
                "(version_id,correction_set_ref,version_number,prior_version_id,"
                "source_manifest_ref,source_manifest_hash,source_content_hash,"
                "record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id, correction_set_ref, version, prior_version_ref,
                    source_manifest_ref, source_manifest_hash, source_content_hash,
                    canonical_json(wire), wire["content_hash"], actor_ref,
                    wire["created_at"],
                ),
            )
        return {"status": "fresh", **wire}

    def resolve(
        self,
        version_ref: str,
        version_hash: str,
    ) -> dict[str, Any]:
        version_hash = _hash(version_hash, "correction_set_version_hash")
        correction_set = self.correction_set(version_ref)
        if correction_set["content_hash"] != version_hash:
            raise TranscriptCorrectionConflict("correction set hash drifted")
        _, original = self._source(
            correction_set["source_manifest_ref"],
            correction_set["source_manifest_hash"],
            correction_set["source_content_hash"],
        )
        parts: list[str] = []
        cursor = 0
        resolved_cursor = 0
        mappings: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        for item in correction_set["corrections"]:
            start = item["source_start"]
            end = item["source_end"]
            prefix = original[cursor:start]
            parts.append(prefix)
            resolved_cursor += len(prefix)
            if item["disposition"] == "accepted":
                replacement = item["replacement_text"]
                resolved_start = resolved_cursor
                parts.append(replacement)
                resolved_cursor += len(replacement)
                mappings.append({
                    "source_start": start,
                    "source_end": end,
                    "source_sha256": item["source_sha256"],
                    "resolved_start": resolved_start,
                    "resolved_end": resolved_cursor,
                    "resolved_sha256": hashlib.sha256(
                        replacement.encode("utf-8")
                    ).hexdigest(),
                    "correction_kind": item["correction_kind"],
                })
            else:
                source_slice = original[start:end]
                parts.append(source_slice)
                unresolved.append({
                    "source_start": start,
                    "source_end": end,
                    "source_sha256": item["source_sha256"],
                    "correction_kind": item["correction_kind"],
                })
                resolved_cursor += len(source_slice)
            cursor = end
        parts.append(original[cursor:])
        resolved = "".join(parts)
        return {
            "correction_set": correction_set,
            "original_text": original,
            "resolved_text": resolved,
            "resolved_content_hash": hashlib.sha256(
                resolved.encode("utf-8")
            ).hexdigest(),
            "correction_mappings": mappings,
            "unresolved_correction_spans": unresolved,
            "citation_mode": (
                "raw_span_plus_admitted_correction"
                if mappings else "raw_span"
            ),
        }

    def bind_claim_citation(
        self,
        version_ref: str,
        version_hash: str,
        *,
        source_start: int,
        source_end: int,
    ) -> dict[str, Any]:
        """Bind one Claim citation to raw capture plus correction lineage.

        The citation is in raw-source coordinates.  Accepted corrections that
        intersect the span are part of its authority chain.  Any intersecting
        unresolved correction makes the citation ineligible for a formal
        Claim; callers may still display it as a review candidate.
        """

        resolved = self.resolve(version_ref, version_hash)
        original = resolved["original_text"]
        if (
            isinstance(source_start, bool)
            or not isinstance(source_start, int)
            or isinstance(source_end, bool)
            or not isinstance(source_end, int)
            or source_start < 0
            or source_end <= source_start
            or source_end > len(original)
        ):
            raise TranscriptCorrectionValidationError(
                "claim citation source span is invalid"
            )
        correction_set = resolved["correction_set"]
        accepted_indexes: list[int] = []
        unresolved_indexes: list[int] = []
        for index, item in enumerate(correction_set["corrections"]):
            overlaps = (
                source_start < item["source_end"]
                and source_end > item["source_start"]
            )
            if not overlaps:
                continue
            if item["disposition"] == "accepted":
                accepted_indexes.append(index)
            else:
                unresolved_indexes.append(index)
        source_slice = original[source_start:source_end]
        identity = {
            "correction_set_version_ref": correction_set["id"],
            "correction_set_version_hash": correction_set["content_hash"],
            "source_start": source_start,
            "source_end": source_end,
        }
        base = {
            "schema_version": SCHEMA_VERSION,
            "id": "transcript-claim-citation-binding:" + content_hash(identity)[:32],
            "created_at": correction_set["created_at"],
            "source_manifest_ref": correction_set["source_manifest_ref"],
            "source_manifest_hash": correction_set["source_manifest_hash"],
            "source_content_hash": correction_set["source_content_hash"],
            "source_start": source_start,
            "source_end": source_end,
            "source_sha256": hashlib.sha256(
                source_slice.encode("utf-8")
            ).hexdigest(),
            "correction_set_version_ref": correction_set["id"],
            "correction_set_version_hash": correction_set["content_hash"],
            "accepted_correction_indexes": accepted_indexes,
            "unresolved_correction_indexes": unresolved_indexes,
            "citation_mode": (
                "raw_span_plus_admitted_correction"
                if accepted_indexes else "raw_span"
            ),
            "claim_eligible": not unresolved_indexes,
            "blocking_reason": (
                "unresolved_correction_overlap" if unresolved_indexes else None
            ),
            "actor_ref": "core:transcript-correction-citation-gate",
        }
        return {**base, "content_hash": content_hash(base)}


__all__ = [
    "SCHEMA_VERSION",
    "TranscriptCorrectionAuthority",
    "TranscriptCorrectionConflict",
    "TranscriptCorrectionError",
    "TranscriptCorrectionNotFound",
    "TranscriptCorrectionValidationError",
]
