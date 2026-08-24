"""Fail-closed transcript polishing over exact AlphaEngine document authority.

The model is only a candidate producer. Core re-reads the complete acquisition
manifest and original UTF-8 bytes, verifies a contiguous source-span mapping,
and enforces numeric and protected-name conservation before writing a derived
polished artifact. Citations must follow the raw-source plus admitted-correction
lineage; the polished text is never citation authority.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .alphaengine_document_acquisition import (
    validate_alphaengine_document_acquisition_manifest,
)
from .contracts import WorkOrder
from .raw_spool import RawSpool
from .store import canonical_json, content_hash
from .transcript_correction import TranscriptCorrectionAuthority


CANDIDATE_SCHEMA_VERSION = "0.1"
ARTIFACT_SCHEMA_VERSION = "0.2"
SCHEMA_VERSION = CANDIDATE_SCHEMA_VERSION
TRANSCRIPT_POLISH_CAPABILITY = "capability:dalton:local:transcript-polish"
TRANSCRIPT_POLISH_OPERATION = "verify_and_materialize_transcript_polish"
TRANSCRIPT_POLISH_RUNTIME = "runtime-profile:dalton-core-transcript-polish:0.1"
TRANSCRIPT_POLISH_PERMISSION = "read_exact_alphaengine_document_artifact"
TRANSCRIPT_POLISH_OUTPUT_CONTRACT = "schema:transcript-polish-probe-output:0.2"
TRANSCRIPT_POLISH_VERIFIER = "verifier:transcript-polish-conservation:0.1"
TRANSCRIPT_POLISH_RULE_REF = "rules:transcript-polish-conservation:0.1"

MAX_SOURCE_CHARS = 200_000
MAX_SEGMENTS = 256
MAX_SOURCE_SPAN_CHARS = 2_000
RETENTION_FLOOR = Decimal("0.65")
EXPANSION_CEILING = Decimal("1.20")

_SCHEMA_PATH = Path(__file__).with_name("transcript_polish_schema.sql")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_NUMERIC_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:US\$|HK\$|RMB|CNY|USD|HKD|EUR|GBP|[$€£¥￥])?\s*"
    r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"(?:\s*(?:%|percent|percentage points?|basis points?|bps|"
    r"thousand|million|billion|trillion|mn|bn|k|m|b|t|"
    r"美元|港元|人民币(?:元)?|元|万|百万|亿|万亿))?",
    re.IGNORECASE,
)
_AUTO_TERM_RES = (
    re.compile(r"\b[A-Z]{2,}(?:-[A-Z0-9]+)*\b"),
    re.compile(r"\b[A-Z][A-Za-z]*[A-Z][A-Za-z0-9.-]*\b"),
    re.compile(r"\b[A-Za-z]+\d[A-Za-z0-9.-]*\b"),
    re.compile(r"\b(?:[A-Z][a-z]+[ -]){1,4}[A-Z][a-z]+\b"),
)


class TranscriptPolishError(RuntimeError):
    pass


class TranscriptPolishValidationError(TranscriptPolishError, ValueError):
    pass


class TranscriptPolishConflict(TranscriptPolishError):
    pass


class TranscriptPolishNotFound(TranscriptPolishError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TranscriptPolishValidationError(f"{name} must be non-empty text")
    return value.strip()


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise TranscriptPolishValidationError(f"{name} must be lowercase SHA-256")
    return value


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TranscriptPolishValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) != fields:
        raise TranscriptPolishValidationError(
            f"{name} has invalid closed shape; "
            f"missing={sorted(fields - set(wire))}, unknown={sorted(set(wire) - fields)}"
        )
    return wire


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TranscriptPolishValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _unique_terms(value: Any, name: str) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise TranscriptPolishValidationError(f"{name} must be an array")
    terms = [_text(item, f"{name}[]") for item in value]
    if len(terms) != len(set(terms)):
        raise TranscriptPolishValidationError(f"{name} must contain unique terms")
    if len(terms) > 100:
        raise TranscriptPolishValidationError(f"{name} exceeds 100 terms")
    return terms


def _record(base: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(base)
    wire["content_hash"] = content_hash(wire)
    return wire


def _numeric_expressions(text: str) -> list[str]:
    return [" ".join(match.group(0).split()).casefold() for match in _NUMERIC_RE.finditer(text)]


def _auto_terms(text: str) -> list[str]:
    first: dict[str, int] = {}
    for pattern in _AUTO_TERM_RES:
        for match in pattern.finditer(text):
            term = " ".join(match.group(0).split())
            first[term] = min(first.get(term, match.start()), match.start())
    return [term for term, _ in sorted(first.items(), key=lambda item: (item[1], item[0]))]


def _term_count(text: str, term: str) -> int:
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])")
    return len(pattern.findall(text))


def _term_sequence(text: str, terms: Sequence[str] | set[str]) -> list[str]:
    occurrences: list[tuple[int, int, str]] = []
    for term in terms:
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])"
        )
        occurrences.extend(
            (match.start(), match.end(), term) for match in pattern.finditer(text)
        )
    return [term for _, _, term in sorted(occurrences)]


def _protection_manifest(text: str, additional_terms: Sequence[str]) -> dict[str, Any]:
    terms = _auto_terms(text)
    for term in additional_terms:
        if _term_count(text, term) == 0:
            raise TranscriptPolishValidationError(
                f"additional protected term is absent from source: {term}"
            )
        if term not in terms:
            terms.append(term)
    base = {
        "rule_ref": TRANSCRIPT_POLISH_RULE_REF,
        "numeric_expressions": _numeric_expressions(text),
        "protected_terms": [
            {"term": term, "count": _term_count(text, term)} for term in terms
        ],
    }
    return {**base, "content_hash": content_hash(base)}


def parse_transcript_polish_candidate_text(value: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        raise TranscriptPolishValidationError("candidate text must be non-empty JSON")
    try:
        raw = json.loads(
            value,
            object_pairs_hook=_strict_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                TranscriptPolishValidationError(f"invalid JSON constant: {token}")
            ),
        )
    except (json.JSONDecodeError, TypeError) as exc:
        raise TranscriptPolishValidationError("candidate must be strict JSON") from exc
    wire = _closed(raw, {"schema_version", "segments"}, "TranscriptPolishCandidate")
    if wire["schema_version"] != CANDIDATE_SCHEMA_VERSION:
        raise TranscriptPolishValidationError("unsupported candidate schema_version")
    if not isinstance(wire["segments"], list) or not 1 <= len(wire["segments"]) <= MAX_SEGMENTS:
        raise TranscriptPolishValidationError("candidate segments cardinality is invalid")
    segments: list[dict[str, Any]] = []
    for index, value in enumerate(wire["segments"]):
        segment = _closed(
            value,
            {"source_start", "source_end", "source_sha256", "polished_text"},
            f"segments[{index}]",
        )
        for name in ("source_start", "source_end"):
            if isinstance(segment[name], bool) or not isinstance(segment[name], int):
                raise TranscriptPolishValidationError(f"segments[{index}].{name} must be integer")
        _hash(segment["source_sha256"], f"segments[{index}].source_sha256")
        if not isinstance(segment["polished_text"], str) or not segment["polished_text"]:
            raise TranscriptPolishValidationError(
                f"segments[{index}].polished_text must be non-empty"
            )
        segments.append(segment)
    return {"schema_version": CANDIDATE_SCHEMA_VERSION, "segments": segments}


class TranscriptPolishAuthority:
    """Verify and persist immutable derived polished transcript artifacts."""

    def __init__(
        self,
        store: Any,
        *,
        spool: RawSpool,
        manifest_resolver: Callable[[str], Mapping[str, Any]],
        correction_authority: TranscriptCorrectionAuthority | None = None,
    ) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("TranscriptPolishAuthority requires a DaltonStore")
        if not isinstance(spool, RawSpool):
            raise TypeError("TranscriptPolishAuthority requires a RawSpool")
        if not callable(manifest_resolver):
            raise TypeError("manifest_resolver must be callable")
        if correction_authority is not None and not isinstance(
            correction_authority, TranscriptCorrectionAuthority
        ):
            raise TypeError(
                "correction_authority must be TranscriptCorrectionAuthority or None"
            )
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.spool = spool
        self.manifest_resolver = manifest_resolver
        self.correction_authority = correction_authority
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def artifact(self, version_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT record_json,content_hash FROM transcript_polish_artifact_versions "
            "WHERE version_id=?",
            (_text(version_ref, "artifact_version_ref"),),
        ).fetchone()
        if row is None:
            raise TranscriptPolishNotFound(version_ref)
        try:
            wire = json.loads(row["record_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise TranscriptPolishConflict("polished artifact record is invalid") from exc
        if canonical_json(wire) != row["record_json"]:
            raise TranscriptPolishConflict("polished artifact record is not canonical")
        body = dict(wire)
        asserted = body.pop("content_hash", None)
        if asserted != content_hash(body) or asserted != row["content_hash"]:
            raise TranscriptPolishConflict("polished artifact record hash drifted")
        return wire

    def polished_text(self, version_ref: str) -> str:
        artifact = self.artifact(version_ref)
        raw = self.spool.read_object(artifact["polished_object"]["content_hash"])
        if (
            len(raw) != artifact["polished_object"]["size_bytes"]
            or hashlib.sha256(raw).hexdigest()
            != artifact["polished_object"]["content_hash"]
        ):
            raise TranscriptPolishConflict("polished object bytes drifted")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TranscriptPolishConflict("polished object is no longer UTF-8") from exc

    def materialize(
        self,
        *,
        source_manifest_ref: str,
        source_manifest_hash: str,
        source_content_hash: str,
        candidate_text: str,
        additional_protected_terms: Sequence[str],
        correction_set_version_ref: str | None = None,
        correction_set_version_hash: str | None = None,
        prior_version_ref: str | None = None,
    ) -> dict[str, Any]:
        source_manifest_ref = _text(source_manifest_ref, "source_manifest_ref")
        source_manifest_hash = _hash(source_manifest_hash, "source_manifest_hash")
        source_content_hash = _hash(source_content_hash, "source_content_hash")
        terms = _unique_terms(additional_protected_terms, "additional_protected_terms")
        try:
            manifest = validate_alphaengine_document_acquisition_manifest(
                self.manifest_resolver(source_manifest_ref)
            )
        except Exception as exc:
            raise TranscriptPolishNotFound(
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
            raise TranscriptPolishConflict("source manifest is not exact complete authority")
        original_bytes = self.spool.read_object(source_content_hash)
        if (
            len(original_bytes) != manifest["assembled_object"]["size_bytes"]
            or hashlib.sha256(original_bytes).hexdigest() != source_content_hash
        ):
            raise TranscriptPolishConflict("source transcript bytes drifted")
        try:
            original = original_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TranscriptPolishValidationError("source transcript must be UTF-8") from exc
        if not original or len(original) > MAX_SOURCE_CHARS:
            raise TranscriptPolishValidationError("source transcript size is outside v1 bounds")
        if manifest["content_chars"] != len(original):
            raise TranscriptPolishConflict("manifest source character count drifted")
        if (correction_set_version_ref is None) != (correction_set_version_hash is None):
            raise TranscriptPolishValidationError(
                "correction set ref and hash must be supplied together"
            )
        if correction_set_version_ref is None:
            resolved_source = original
            resolved_source_hash = source_content_hash
            correction_set_ref = None
            correction_set_hash = None
            correction_mappings: list[dict[str, Any]] = []
            unresolved_correction_spans: list[dict[str, Any]] = []
            citation_mode = "raw_span"
        else:
            if self.correction_authority is None:
                raise TranscriptPolishConflict(
                    "correction set supplied without correction authority"
                )
            correction_set_ref = _text(
                correction_set_version_ref, "correction_set_version_ref"
            )
            correction_set_hash = _hash(
                correction_set_version_hash, "correction_set_version_hash"
            )
            resolution = self.correction_authority.resolve(
                correction_set_ref, correction_set_hash
            )
            correction_set = resolution["correction_set"]
            if (
                correction_set["document_ref"] != manifest["document_ref"]
                or correction_set["source_manifest_ref"] != source_manifest_ref
                or correction_set["source_manifest_hash"] != source_manifest_hash
                or correction_set["source_content_hash"] != source_content_hash
                or resolution["original_text"] != original
            ):
                raise TranscriptPolishConflict(
                    "correction set does not bind the exact transcript source"
                )
            resolved_source = resolution["resolved_text"]
            resolved_source_hash = resolution["resolved_content_hash"]
            correction_mappings = resolution["correction_mappings"]
            unresolved_correction_spans = resolution[
                "unresolved_correction_spans"
            ]
            citation_mode = resolution["citation_mode"]
        candidate = parse_transcript_polish_candidate_text(candidate_text)
        protection = _protection_manifest(resolved_source, terms)
        mappings: list[dict[str, Any]] = []
        expected_start = 0
        polished_parts: list[str] = []
        polished_cursor = 0
        for index, segment in enumerate(candidate["segments"]):
            start = segment["source_start"]
            end = segment["source_end"]
            if start != expected_start or end <= start or end > len(resolved_source):
                raise TranscriptPolishConflict("candidate source spans are not a contiguous partition")
            if end - start > MAX_SOURCE_SPAN_CHARS:
                raise TranscriptPolishValidationError("candidate source span exceeds v1 bound")
            source_slice = resolved_source[start:end]
            if hashlib.sha256(source_slice.encode("utf-8")).hexdigest() != segment["source_sha256"]:
                raise TranscriptPolishConflict("candidate source span hash drifted")
            polished = segment["polished_text"]
            if _numeric_expressions(source_slice) != _numeric_expressions(polished):
                raise TranscriptPolishConflict(f"segment {index} numeric expressions drifted")
            terms_to_check = set(_auto_terms(source_slice)) | set(_auto_terms(polished))
            terms_to_check.update(
                term for term in terms
                if _term_count(source_slice, term) or _term_count(polished, term)
            )
            if _term_sequence(source_slice, terms_to_check) != _term_sequence(
                polished, terms_to_check
            ):
                raise TranscriptPolishConflict(f"segment {index} protected terms drifted")
            polished_start = polished_cursor
            polished_cursor += len(polished)
            mappings.append({
                "source_start": start,
                "source_end": end,
                "source_sha256": segment["source_sha256"],
                "polished_start": polished_start,
                "polished_end": polished_cursor,
                "polished_sha256": hashlib.sha256(polished.encode("utf-8")).hexdigest(),
            })
            polished_parts.append(polished)
            expected_start = end
        if expected_start != len(resolved_source):
            raise TranscriptPolishConflict(
                "candidate source spans do not cover the resolved source"
            )
        polished_text = "".join(polished_parts)
        ratio = Decimal(len(polished_text)) / Decimal(len(resolved_source))
        if ratio < RETENTION_FLOOR or ratio > EXPANSION_CEILING:
            raise TranscriptPolishConflict("candidate length ratio exceeds conservation bounds")
        if _numeric_expressions(resolved_source) != _numeric_expressions(polished_text):
            raise TranscriptPolishConflict("global numeric expressions drifted")
        for item in protection["protected_terms"]:
            if _term_count(polished_text, item["term"]) != item["count"]:
                raise TranscriptPolishConflict("global protected term counts drifted")
        global_terms = (
            set(_auto_terms(resolved_source))
            | set(_auto_terms(polished_text))
            | set(terms)
        )
        if _term_sequence(resolved_source, global_terms) != _term_sequence(
            polished_text, global_terms
        ):
            raise TranscriptPolishConflict("candidate introduced or removed protected-looking terms")

        polished_bytes = polished_text.encode("utf-8")
        polished_hash = hashlib.sha256(polished_bytes).hexdigest()
        candidate_hash = content_hash(candidate)
        artifact_ref = f"transcript-polish-artifact:{manifest['document_ref']}"
        latest = self.connection.execute(
            "SELECT record_json FROM transcript_polish_artifact_versions "
            "WHERE artifact_ref=? ORDER BY version_number DESC LIMIT 1",
            (artifact_ref,),
        ).fetchone()
        binding = {
            "source_manifest_ref": source_manifest_ref,
            "source_manifest_hash": source_manifest_hash,
            "source_content_hash": source_content_hash,
            "correction_set_version_ref": correction_set_ref,
            "correction_set_version_hash": correction_set_hash,
            "resolved_source_hash": resolved_source_hash,
            "candidate_hash": candidate_hash,
            "protection_manifest_hash": protection["content_hash"],
            "polished_content_hash": polished_hash,
        }
        if latest is None:
            if prior_version_ref is not None:
                raise TranscriptPolishConflict("first polished artifact cannot have a prior version")
            version = 1
        else:
            latest_wire = json.loads(latest["record_json"])
            if prior_version_ref is None and all(
                latest_wire.get(key) == value for key, value in binding.items()
            ):
                return {"status": "duplicate", **latest_wire}
            if prior_version_ref != latest_wire["id"]:
                raise TranscriptPolishConflict("polished artifact must continue the latest version")
            version = latest_wire["version"] + 1
        version_id = "transcript-polish-artifact-version:" + content_hash({
            "artifact_ref": artifact_ref,
            "version": version,
            "prior_version_ref": prior_version_ref,
            **binding,
        })[:32]
        sink = self.spool.open_sink(
            "raw-sink:" + content_hash({"version_id": version_id, "candidate_hash": candidate_hash}),
            max_response_bytes=max(1, len(polished_bytes)),
        )
        try:
            sink.write(polished_bytes)
            polished_object = sink.finalize().to_dict()
        except Exception:
            sink.abort()
            raise
        wire = _record({
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "id": version_id,
            "created_at": _now(),
            "artifact_ref": artifact_ref,
            "version": version,
            "prior_version_ref": prior_version_ref,
            "kind": "derived_polished_transcript",
            "media_type": "text/plain; charset=utf-8",
            "source_ref": "source:alphaengine",
            "document_ref": manifest["document_ref"],
            **binding,
            "correction_mappings": correction_mappings,
            "unresolved_correction_spans": unresolved_correction_spans,
            "polish_rule_ref": TRANSCRIPT_POLISH_RULE_REF,
            "protection_manifest": protection,
            "span_mappings": mappings,
            "polished_object": polished_object,
            "citation_authority": "source_lineage_only",
            "claim_citation_mode": citation_mode,
            "verification_status": "verified",
            "actor_ref": "core:transcript-polish-verifier",
        })
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO transcript_polish_artifact_versions "
                "(version_id,artifact_ref,version_number,prior_version_id,source_manifest_ref,"
                "source_manifest_hash,source_content_hash,polished_content_hash,record_json,"
                "content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id, artifact_ref, version, prior_version_ref,
                    source_manifest_ref, source_manifest_hash, source_content_hash,
                    polished_hash, canonical_json(wire), wire["content_hash"],
                    wire["actor_ref"], wire["created_at"],
                ),
            )
        return {"status": "fresh", **wire}


class TranscriptPolishWorker:
    """Admit one already-produced model candidate under an exact WorkOrder."""

    _PARAMETERS = {
        "source_ref", "locator", "query_terms", "source_manifest_ref",
        "source_manifest_hash", "source_content_hash", "additional_protected_terms",
        "correction_set_version_ref", "correction_set_version_hash",
        "prior_polished_artifact_version_ref",
    }

    def __init__(self, authority: TranscriptPolishAuthority) -> None:
        self.authority = authority

    def execute(self, work_order: Mapping[str, Any], candidate_text: str) -> dict[str, Any]:
        try:
            work = WorkOrder.from_dict(work_order).to_dict()
        except Exception as exc:
            raise TranscriptPolishConflict("WorkOrder is invalid") from exc
        if work["requested_capabilities"] != [TRANSCRIPT_POLISH_CAPABILITY]:
            raise TranscriptPolishConflict("WorkOrder capability drifted")
        if work["runtime_profile_ref"] != TRANSCRIPT_POLISH_RUNTIME:
            raise TranscriptPolishConflict("WorkOrder runtime profile drifted")
        if work["declared_side_effects"]:
            raise TranscriptPolishConflict("transcript polish must have no external side effects")
        metadata = work["metadata"]
        if (
            metadata.get("operation") != TRANSCRIPT_POLISH_OPERATION
            or metadata.get("permission_scope") != TRANSCRIPT_POLISH_PERMISSION
        ):
            raise TranscriptPolishConflict("WorkOrder operation or permission drifted")
        parameters = _closed(metadata.get("parameters"), self._PARAMETERS, "parameters")
        if parameters["source_ref"] != "source:alphaengine":
            raise TranscriptPolishConflict("WorkOrder source_ref drifted")
        locator = _text(parameters["locator"], "locator")
        if not locator.startswith("alphaengine-doc:"):
            raise TranscriptPolishConflict("WorkOrder locator is not an AlphaEngine document")
        if parameters["query_terms"] != ["transcript-polish"]:
            raise TranscriptPolishConflict("WorkOrder query_terms drifted")
        prior = parameters["prior_polished_artifact_version_ref"]
        if prior is not None:
            prior = _text(prior, "prior_polished_artifact_version_ref")
        artifact = self.authority.materialize(
            source_manifest_ref=parameters["source_manifest_ref"],
            source_manifest_hash=parameters["source_manifest_hash"],
            source_content_hash=parameters["source_content_hash"],
            candidate_text=candidate_text,
            additional_protected_terms=parameters["additional_protected_terms"],
            correction_set_version_ref=parameters["correction_set_version_ref"],
            correction_set_version_hash=parameters["correction_set_version_hash"],
            prior_version_ref=prior,
        )
        if artifact["document_ref"] != locator:
            raise TranscriptPolishConflict("WorkOrder locator and source manifest disagree")
        return {"matches": [{
            "source_location": f"{locator}#full-source-lineage",
            "polished_artifact_version_ref": artifact["id"],
            "polished_artifact_version_hash": artifact["content_hash"],
            "source_manifest_ref": artifact["source_manifest_ref"],
            "source_manifest_hash": artifact["source_manifest_hash"],
            "source_content_hash": artifact["source_content_hash"],
            "citation_authority": artifact["citation_authority"],
            "claim_citation_mode": artifact["claim_citation_mode"],
            "correction_set_version_ref": artifact["correction_set_version_ref"],
            "correction_set_version_hash": artifact["correction_set_version_hash"],
            "unresolved_correction_spans": artifact["unresolved_correction_spans"],
            "verification_status": artifact["verification_status"],
        }]}


__all__ = [
    "SCHEMA_VERSION", "CANDIDATE_SCHEMA_VERSION", "ARTIFACT_SCHEMA_VERSION",
    "TRANSCRIPT_POLISH_CAPABILITY", "TRANSCRIPT_POLISH_OPERATION",
    "TRANSCRIPT_POLISH_RUNTIME", "TRANSCRIPT_POLISH_PERMISSION",
    "TRANSCRIPT_POLISH_OUTPUT_CONTRACT", "TRANSCRIPT_POLISH_VERIFIER",
    "TRANSCRIPT_POLISH_RULE_REF", "TranscriptPolishAuthority",
    "TranscriptPolishWorker", "TranscriptPolishError", "TranscriptPolishConflict",
    "TranscriptPolishValidationError", "TranscriptPolishNotFound",
    "parse_transcript_polish_candidate_text",
]
