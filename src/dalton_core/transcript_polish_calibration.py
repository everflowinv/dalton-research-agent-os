"""Frozen corpus and deterministic scorer for routed transcript polishing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from importlib.resources import files
from typing import Any

from .store import canonical_json, content_hash
from .transcript_polish import (
    MAX_SOURCE_CHARS,
    TranscriptPolishError,
    parse_transcript_polish_candidate_text,
    verify_transcript_polish_candidate,
)
from .transcript_polish_model import build_transcript_polish_model_prompt


SCHEMA_VERSION = "0.1"
BASE_CORPUS_RESOURCE = "calibration_fixtures/transcript-polish-v0.1.json"
CORPUS_RESOURCE = "calibration_fixtures/transcript-polish-v0.2-overlay.json"
_CASE_FIELDS_V01 = {
    "id", "safety_critical", "language", "source_state", "source_parts",
    "additional_protected_terms", "unresolved_terms", "quality_rules",
}
_CASE_FIELDS_V02 = {
    "id", "safety_critical", "language", "source_state", "source_parts",
    "additional_protected_terms", "unresolved_corrections", "quality_rules",
}
_UNRESOLVED_CORRECTION_FIELDS = {"term", "correction_kind"}
_UNRESOLVED_CORRECTION_KINDS = {
    "proper_name", "terminology", "numeric", "negation", "semantic",
    "speaker_label",
}
_QUALITY_FIELDS = {
    "required_absent", "required_present", "forbidden_additions",
    "require_change",
}


class TranscriptPolishCalibrationError(RuntimeError):
    pass


def _texts(
    value: Any,
    name: str,
    *,
    nonempty: bool = False,
    unique: bool = True,
) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise TranscriptPolishCalibrationError(f"{name} must be an array")
    if any(not isinstance(item, str) or not item for item in value):
        raise TranscriptPolishCalibrationError(
            f"{name} must contain non-empty strings"
        )
    if unique and len(set(value)) != len(value):
        raise TranscriptPolishCalibrationError(f"{name} must be unique")
    return list(value)


def validate_transcript_polish_corpus(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "id", "cases", "content_hash",
    }:
        raise TranscriptPolishCalibrationError(
            "transcript polish corpus has invalid closed shape"
        )
    wire = dict(value)
    if (
        wire["schema_version"] != SCHEMA_VERSION
        or wire["id"] not in {
            "transcript-polish-calibration-corpus:0.1",
            "transcript-polish-calibration-corpus:0.2",
        }
        or not isinstance(wire["cases"], list)
        or not wire["cases"]
    ):
        raise TranscriptPolishCalibrationError(
            "transcript polish corpus identity is invalid"
        )
    cases: list[dict[str, Any]] = []
    ids: set[str] = set()
    corpus_version = wire["id"].rsplit(":", 1)[1]
    case_fields = (
        _CASE_FIELDS_V01 if corpus_version == "0.1" else _CASE_FIELDS_V02
    )
    for index, raw in enumerate(wire["cases"]):
        name = f"cases[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != case_fields:
            raise TranscriptPolishCalibrationError(
                f"{name} has invalid closed shape"
            )
        case = dict(raw)
        case_id = case["id"]
        if (
            not isinstance(case_id, str)
            or not case_id.startswith("transcript-polish:")
            or case_id in ids
        ):
            raise TranscriptPolishCalibrationError(
                f"{name}.id is invalid or duplicate"
            )
        ids.add(case_id)
        if not isinstance(case["safety_critical"], bool):
            raise TranscriptPolishCalibrationError(
                f"{name}.safety_critical must be boolean"
            )
        if case["language"] not in {"en", "zh"}:
            raise TranscriptPolishCalibrationError(
                f"{name}.language is unsupported"
            )
        if case["source_state"] not in {
            "raw", "admitted_correction", "unresolved_correction",
        }:
            raise TranscriptPolishCalibrationError(
                f"{name}.source_state is unsupported"
            )
        source_parts = _texts(
            case["source_parts"],
            f"{name}.source_parts",
            nonempty=True,
            unique=False,
        )
        source_text = "".join(source_parts)
        if not source_text or len(source_text) > MAX_SOURCE_CHARS:
            raise TranscriptPolishCalibrationError(
                f"{name} source size is outside worker bounds"
            )
        protected_terms = _texts(
            case["additional_protected_terms"],
            f"{name}.additional_protected_terms",
        )
        if any(term not in source_text for term in protected_terms):
            raise TranscriptPolishCalibrationError(
                f"{name} protected term is absent from source"
            )
        if corpus_version == "0.1":
            unresolved_corrections = [{
                "term": term,
                "correction_kind": "proper_name",
            } for term in _texts(
                case["unresolved_terms"], f"{name}.unresolved_terms"
            )]
        else:
            raw_corrections = case["unresolved_corrections"]
            if not isinstance(raw_corrections, list):
                raise TranscriptPolishCalibrationError(
                    f"{name}.unresolved_corrections must be an array"
                )
            unresolved_corrections = []
            for correction_index, raw_correction in enumerate(
                raw_corrections
            ):
                correction_name = (
                    f"{name}.unresolved_corrections[{correction_index}]"
                )
                if (
                    not isinstance(raw_correction, Mapping)
                    or set(raw_correction) != _UNRESOLVED_CORRECTION_FIELDS
                ):
                    raise TranscriptPolishCalibrationError(
                        f"{correction_name} has invalid closed shape"
                    )
                correction = dict(raw_correction)
                if (
                    not isinstance(correction["term"], str)
                    or not correction["term"]
                    or correction["correction_kind"]
                    not in _UNRESOLVED_CORRECTION_KINDS
                ):
                    raise TranscriptPolishCalibrationError(
                        f"{correction_name} is invalid"
                    )
                unresolved_corrections.append(correction)
            correction_terms = [
                item["term"] for item in unresolved_corrections
            ]
            if len(set(correction_terms)) != len(correction_terms):
                raise TranscriptPolishCalibrationError(
                    f"{name}.unresolved_corrections terms must be unique"
                )
        unresolved_terms = [
            item["term"] for item in unresolved_corrections
        ]
        if any(source_text.count(term) != 1 for term in unresolved_terms):
            raise TranscriptPolishCalibrationError(
                f"{name} unresolved terms must occur exactly once"
            )
        if bool(unresolved_terms) != (
            case["source_state"] == "unresolved_correction"
        ):
            raise TranscriptPolishCalibrationError(
                f"{name} unresolved state and terms disagree"
            )
        quality = case["quality_rules"]
        if not isinstance(quality, Mapping) or set(quality) != _QUALITY_FIELDS:
            raise TranscriptPolishCalibrationError(
                f"{name}.quality_rules has invalid closed shape"
            )
        quality = dict(quality)
        for field in (
            "required_absent", "required_present", "forbidden_additions",
        ):
            quality[field] = _texts(
                quality[field], f"{name}.quality_rules.{field}"
            )
        if not isinstance(quality["require_change"], bool):
            raise TranscriptPolishCalibrationError(
                f"{name}.quality_rules.require_change must be boolean"
            )
        if any(item not in source_text for item in quality["required_absent"]):
            raise TranscriptPolishCalibrationError(
                f"{name} required_absent text is absent from source"
            )
        if any(item not in source_text for item in quality["required_present"]):
            raise TranscriptPolishCalibrationError(
                f"{name} required_present text is absent from source"
            )
        if any(item in source_text for item in quality["forbidden_additions"]):
            raise TranscriptPolishCalibrationError(
                f"{name} forbidden addition already exists in source"
            )
        normalized = {
            **case,
            "source_parts": source_parts,
            "additional_protected_terms": protected_terms,
            "quality_rules": quality,
        }
        if corpus_version == "0.1":
            normalized["unresolved_terms"] = unresolved_terms
        else:
            normalized["unresolved_corrections"] = unresolved_corrections
        cases.append(normalized)
    body = {
        "schema_version": wire["schema_version"],
        "id": wire["id"],
        "cases": cases,
    }
    if wire["content_hash"] != content_hash(body):
        raise TranscriptPolishCalibrationError(
            "transcript polish corpus hash drifted"
        )
    return {**body, "content_hash": wire["content_hash"]}


def _resource_json(resource_name: str) -> Any:
    resource = files("dalton_core").joinpath(resource_name)
    return json.loads(resource.read_text(encoding="utf-8"))


def _load_v01_corpus() -> dict[str, Any]:
    return validate_transcript_polish_corpus(
        _resource_json(BASE_CORPUS_RESOURCE)
    )


def _load_v02_corpus() -> dict[str, Any]:
    base = _load_v01_corpus()
    overlay = _resource_json(CORPUS_RESOURCE)
    overlay_fields = {
        "schema_version", "id", "base_corpus_ref", "base_content_hash",
        "case_updates", "additional_cases", "resolved_content_hash",
        "content_hash",
    }
    if not isinstance(overlay, Mapping) or set(overlay) != overlay_fields:
        raise TranscriptPolishCalibrationError(
            "transcript polish v0.2 overlay has invalid closed shape"
        )
    overlay = dict(overlay)
    overlay_body = {
        key: overlay[key] for key in overlay_fields if key != "content_hash"
    }
    if (
        overlay["schema_version"] != SCHEMA_VERSION
        or overlay["id"] != "transcript-polish-calibration-overlay:0.2"
        or overlay["base_corpus_ref"] != base["id"]
        or overlay["base_content_hash"] != base["content_hash"]
        or overlay["content_hash"] != content_hash(overlay_body)
        or not isinstance(overlay["case_updates"], list)
        or not isinstance(overlay["additional_cases"], list)
    ):
        raise TranscriptPolishCalibrationError(
            "transcript polish v0.2 overlay identity is invalid"
        )
    cases: list[dict[str, Any]] = []
    updates: dict[str, dict[str, Any]] = {}
    update_fields = {
        "case_ref", "additional_protected_terms_append",
        "quality_required_present_append",
    }
    for index, raw_update in enumerate(overlay["case_updates"]):
        if (
            not isinstance(raw_update, Mapping)
            or set(raw_update) != update_fields
        ):
            raise TranscriptPolishCalibrationError(
                f"case_updates[{index}] has invalid closed shape"
            )
        update = dict(raw_update)
        case_ref = update["case_ref"]
        if not isinstance(case_ref, str) or case_ref in updates:
            raise TranscriptPolishCalibrationError(
                f"case_updates[{index}].case_ref is invalid or duplicate"
            )
        update["additional_protected_terms_append"] = _texts(
            update["additional_protected_terms_append"],
            f"case_updates[{index}].additional_protected_terms_append",
        )
        update["quality_required_present_append"] = _texts(
            update["quality_required_present_append"],
            f"case_updates[{index}].quality_required_present_append",
        )
        updates[case_ref] = update
    base_ids = {case["id"] for case in base["cases"]}
    if not set(updates).issubset(base_ids):
        raise TranscriptPolishCalibrationError(
            "v0.2 overlay updates an unknown base case"
        )
    for source in base["cases"]:
        case = json.loads(canonical_json(source))
        unresolved_terms = case.pop("unresolved_terms")
        case["unresolved_corrections"] = [{
            "term": term,
            "correction_kind": "proper_name",
        } for term in unresolved_terms]
        update = updates.get(case["id"])
        if update is not None:
            case["additional_protected_terms"].extend(
                update["additional_protected_terms_append"]
            )
            case["quality_rules"]["required_present"].extend(
                update["quality_required_present_append"]
            )
        cases.append(case)
    cases.extend(json.loads(canonical_json(overlay["additional_cases"])))
    body = {
        "schema_version": SCHEMA_VERSION,
        "id": "transcript-polish-calibration-corpus:0.2",
        "cases": cases,
    }
    if overlay["resolved_content_hash"] != content_hash(body):
        raise TranscriptPolishCalibrationError(
            "transcript polish v0.2 resolved content hash drifted"
        )
    return validate_transcript_polish_corpus({
        **body,
        "content_hash": overlay["resolved_content_hash"],
    })


def load_transcript_polish_corpus(*, version: str = "0.2") -> dict[str, Any]:
    if version == "0.1":
        return _load_v01_corpus()
    if version == "0.2":
        return _load_v02_corpus()
    raise TranscriptPolishCalibrationError(
        f"unsupported transcript polish corpus version: {version}"
    )


def calibration_source_text(case: Mapping[str, Any]) -> str:
    return "".join(case["source_parts"])


def build_transcript_polish_calibration_prompt(
    case: Mapping[str, Any],
) -> str:
    source_text = calibration_source_text(case)
    source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    identity = content_hash({"case_ref": case["id"], "source_hash": source_hash})
    corrected = case["source_state"] in {
        "admitted_correction", "unresolved_correction",
    }
    admitted = case["source_state"] == "admitted_correction"
    correction_ref = (
        "transcript-correction-set-version:" + identity[:32]
        if corrected else None
    )
    correction_hash = content_hash({
        "case_ref": case["id"], "state": case["source_state"]
    }) if corrected else None
    source_context = {
        "document_ref": "alphaengine-doc:calibration-" + identity[:24],
        "source_manifest_ref": (
            "alphaengine-document-acquisition-manifest:" + identity[:32]
        ),
        "source_manifest_hash": content_hash({
            "case_ref": case["id"], "kind": "manifest"
        }),
        "source_content_hash": source_hash,
        "correction_set_version_ref": correction_ref,
        "correction_set_version_hash": correction_hash,
        "resolved_source_text": source_text,
        "resolved_source_hash": source_hash,
        "correction_mappings": ([{"calibration": "admitted"}] if admitted else []),
        "unresolved_correction_spans": [{
            "source_start": source_text.index(term),
            "source_end": source_text.index(term) + len(term),
            "source_sha256": hashlib.sha256(term.encode("utf-8")).hexdigest(),
            "correction_kind": correction["correction_kind"],
        } for correction in case.get("unresolved_corrections", [{
            "term": term,
            "correction_kind": "proper_name",
        } for term in case.get("unresolved_terms", [])])
        for term in [correction["term"]]],
        "citation_mode": (
            "raw_span_plus_admitted_correction" if admitted else "raw_span"
        ),
    }
    return build_transcript_polish_model_prompt(
        source_context,
        additional_protected_terms=case["additional_protected_terms"],
    )


def score_transcript_polish_case(
    case: Mapping[str, Any], model_text: Any
) -> dict[str, Any]:
    reasons: list[str] = []
    candidate: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    try:
        candidate = parse_transcript_polish_candidate_text(model_text)
    except Exception as exc:
        reasons.append("contract:" + type(exc).__name__)
    contract_pass = candidate is not None
    if candidate is not None:
        try:
            verification = verify_transcript_polish_candidate(
                calibration_source_text(case),
                canonical_json(candidate),
                additional_protected_terms=case[
                    "additional_protected_terms"
                ],
            )
        except TranscriptPolishError as exc:
            reasons.append("conservation:" + type(exc).__name__)
    conservation_pass = verification is not None
    polished_text = "" if verification is None else verification["polished_text"]
    quality_pass = conservation_pass
    if quality_pass:
        rules = case["quality_rules"]
        missing = [
            item for item in rules["required_present"]
            if item not in polished_text
        ]
        retained = [
            item for item in rules["required_absent"]
            if item in polished_text
        ]
        added = [
            item for item in rules["forbidden_additions"]
            if item in polished_text
        ]
        unchanged = (
            rules["require_change"]
            and polished_text == calibration_source_text(case)
        )
        if missing:
            reasons.append("quality:missing_required")
        if retained:
            reasons.append("quality:retained_noise")
        if added:
            reasons.append("quality:forbidden_addition")
        if unchanged:
            reasons.append("quality:no_change")
        quality_pass = not (missing or retained or added or unchanged)
    return {
        "case_ref": case["id"],
        "safety_critical": case["safety_critical"],
        "contract_pass": contract_pass,
        "conservation_pass": conservation_pass,
        "quality_pass": quality_pass,
        "overall_pass": contract_pass and conservation_pass and quality_pass,
        "reasons": reasons,
        "candidate_hash": None if candidate is None else content_hash(candidate),
        "polished_content_hash": (
            None if verification is None
            else verification["polished_content_hash"]
        ),
    }


def score_transcript_polish_outputs(
    corpus: Mapping[str, Any],
    outputs: Mapping[str, Any],
    *,
    case_refs: Sequence[str] | None = None,
) -> dict[str, Any]:
    frozen = validate_transcript_polish_corpus(corpus)
    selected = (
        [case["id"] for case in frozen["cases"]]
        if case_refs is None else list(case_refs)
    )
    if (
        not selected
        or len(set(selected)) != len(selected)
        or not set(selected).issubset({case["id"] for case in frozen["cases"]})
    ):
        raise TranscriptPolishCalibrationError(
            "score case_refs are outside the frozen corpus"
        )
    results = [
        score_transcript_polish_case(case, outputs.get(case["id"]))
        for case in frozen["cases"] if case["id"] in selected
    ]
    safety = [item for item in results if item["safety_critical"]]
    return {
        "total_cases": len(results),
        "overall_passed": sum(item["overall_pass"] for item in results),
        "contract_passed": sum(item["contract_pass"] for item in results),
        "conservation_passed": sum(
            item["conservation_pass"] for item in results
        ),
        "quality_passed": sum(item["quality_pass"] for item in results),
        "safety_total": len(safety),
        "safety_passed": sum(item["overall_pass"] for item in safety),
        "eligible": bool(results) and all(
            item["overall_pass"] for item in results
        ),
        "case_results": results,
    }


__all__ = [
    "SCHEMA_VERSION", "BASE_CORPUS_RESOURCE", "CORPUS_RESOURCE",
    "TranscriptPolishCalibrationError",
    "validate_transcript_polish_corpus", "load_transcript_polish_corpus",
    "calibration_source_text", "build_transcript_polish_calibration_prompt",
    "score_transcript_polish_case", "score_transcript_polish_outputs",
]
