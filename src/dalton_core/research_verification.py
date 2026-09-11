"""Offline source/numeric verification and candidate-only research contracts.

This module is deliberately downstream of the P2 fixture coordinator and
upstream of the Research Ledger.  It consumes immutable P2 refs and packaged
recorded fixtures; it never opens a connector, network, credential authority,
DaltonStore, or Ledger writer.  The two verifiers return a closed
``VerificationBundle``.  A separate candidate staging store is the only
consumer allowed to persist a candidate bundle.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_UP
from pathlib import Path
from typing import Any, Callable

from .connector_inventory import load_packaged_connector_inventory
from .recorded_alphaengine_adapter import load_recorded_alphaengine_fixture
from .recorded_source_adapter import load_recorded_source_fixture
from .research_context import (
    validate_compiled_connector_plan,
    validate_compiled_connector_step,
    validate_context_pack,
    validate_runner_request_plan_binding,
)
from .research_coordinator import (
    validate_connector_completion_receipt,
    validate_research_checkpoint,
)
from .store import canonical_json, content_hash
from .transcript_correction import TRANSCRIPT_EVIDENCE_SOURCE_TYPE


SCHEMA_VERSION = "0.1"
_STAGING_SCHEMA_PATH = Path(__file__).with_name("candidate_staging_schema.sql")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_ROUNDING = {
    "half_up": ROUND_HALF_UP,
    "half_even": ROUND_HALF_EVEN,
    "half_down": ROUND_HALF_DOWN,
    "up": ROUND_UP,
    "down": ROUND_DOWN,
    "ceiling": ROUND_CEILING,
    "floor": ROUND_FLOOR,
}
_OPERATORS = {"identity", "sum", "difference", "ratio", "growth_percentage"}
_SOURCE_VERIFIER_REF = "verifier:offline-source:0.1"
_SOURCE_VERIFIER_HASH = content_hash({
    "ref": _SOURCE_VERIFIER_REF,
    "rules": ["p2-bindings", "packaged-fixture-replay", "raw-hash", "source-hash", "completeness", "time-order"],
})
_AUTHORITY_SOURCE_VERIFIER_REF = "verifier:connector-authority-source:0.2"
_AUTHORITY_SOURCE_VERIFIER_HASH = content_hash({
    "ref": _AUTHORITY_SOURCE_VERIFIER_REF,
    "rules": [
        "authority-resolution", "raw-provider-replay", "structured-observation",
        "source-hash", "schema", "completeness", "time-order",
    ],
})
# ADR-0003 option B: transcript candidates whose raw ArtifactVersion is
# Core-held AlphaEngine authority have no ResearchCheckpoint and no numeric
# authority.  Their source verification is a separate closed verifier.
TRANSCRIPT_CORE_AUTHORITY_MODE = "transcript_core_authority"
# ADR-0005 / P9d-17c: the same chain for a fetched public-web page, whose
# citable original is the verified rendering of its exact bytes.
PUBLIC_WEB_CORE_AUTHORITY_MODE = "public_web_core_authority"
REGISTERED_ANNUAL_REPORT_AUTHORITY_MODE = "registered_annual_report_authority"
CITED_CORE_AUTHORITY_MODES = frozenset({
    TRANSCRIPT_CORE_AUTHORITY_MODE, PUBLIC_WEB_CORE_AUTHORITY_MODE,
    REGISTERED_ANNUAL_REPORT_AUTHORITY_MODE,
})
# ADR-0007: whether a cited original may carry a number into the Ledger.
#
# ``reject_cited_quantitative`` is the rule as it has stood since ADR-0003:
# a cited original is not a numeric authority, full stop, because the only way
# to get a number out of a document's text was to build a text extractor and
# that was the option the ADR refused.
#
# ``verified_figure`` is the rule ADR-0007 accepted, and it does not build a
# text extractor either.  It admits a number whose authority is a
# *already-verified figure row* -- one whose digits and as-reported label were
# found in the exact quote it cites, checked before the row was written, and
# re-checkable from the stored quote and hashes by anyone.  Admission re-runs
# that check against the row read back out of Core; the candidate's own copy of
# the figure is compared, never trusted.
#
# The default stays the old rule.  Changing what the Ledger will accept as a
# number is a governance decision, and this flag exists so the integrator can
# flip it deliberately -- with the policy re-signed -- rather than have it
# arrive with a merge.
FIGURE_ADMISSION_REJECT = "reject_cited_quantitative"
FIGURE_ADMISSION_VERIFIED_FIGURE = "verified_figure"
FIGURE_ADMISSION_POLICIES = frozenset({
    FIGURE_ADMISSION_REJECT, FIGURE_ADMISSION_VERIFIED_FIGURE,
})
FIGURE_ADMISSION_DEFAULT = FIGURE_ADMISSION_REJECT

# ADR-0007 §Decision: the refusal that ADR-0003 B installed keeps applying to
# ``transcript_core_authority`` and to ``public_web_core_authority``, word for
# word.  What ADR-0007 adds is a *different* mode whose material is not a cited
# original at all: it is a row of ``coverage_mission_document_figures``, whose
# digits and as-reported label were checked against the exact quote before the
# row was written and can be checked again from the stored quote and hashes.
#
# The rule is therefore about the authority the number comes from, not about
# the fact that the original was cited -- which is what ADR-0003 meant and what
# it could not yet say.
MISSION_FIGURE_AUTHORITY_MODE = "mission_figure_authority"
# The auto-commit rule ref the owner has to publish and sign before any of this
# is legal (ADR-0007, last paragraph of the Decision).  Named here so the
# integrator can grep for it and so a policy listing it means exactly one thing.
MISSION_VERIFIED_FIGURE_RULE_REF = "research-auto-commit:mission-verified-figure:v1"
MISSION_FIGURE_SOURCE_VERIFIER_REF = "verifier:mission-figure-authority-source:0.1"
MISSION_FIGURE_SOURCE_VERIFIER_HASH = content_hash({
    "ref": MISSION_FIGURE_SOURCE_VERIFIER_REF,
    "rules": [
        "figure-row-hash", "figure-not-retracted", "citation-digits-and-label",
        "review-binds-document", "discovered-document-binds-discovery",
        "discovery-envelope-is-core-authority", "envelope-names-the-document",
        "artifact-is-core-authority", "quote-names-a-span",
    ],
})
# ADR-0007 §Decision 4: a company-filed document is the company publishing the
# number; an earnings-call transcript is a record that someone *said* it, and
# ADR-0003's finding about transcripts was right about transcripts.  A spoken
# figure stays qualitative.
FIGURE_ADMISSIBLE_GRADES = frozenset({"company-filed-document"})

# The two numeric verifiers ADR-0007 adds, named here beside the others because
# the VerificationBundle contract keeps a closed list of who is allowed to have
# produced one.  A verifier that is not on this list cannot sign a bundle, and
# that is the guard that stops "we re-checked it" from being an assertion
# anyone can make.
FIGURE_NUMERIC_VERIFIER_REF = "verifier:document-figure-row-recheck:0.1"
FIGURE_NUMERIC_VERIFIER_HASH = content_hash({
    "verifier": FIGURE_NUMERIC_VERIFIER_REF,
    "checks": [
        "figure row content hash recomputes from its own columns",
        "figure is not retracted",
        "digits and as-reported label are in the stored citation",
        "caller's copy is byte-identical to the Core row",
    ],
})
STATEMENT_LINE_VERIFIER_REF = "verifier:statement-line-row-recheck:0.1"
STATEMENT_LINE_VERIFIER_HASH = content_hash({
    "verifier": STATEMENT_LINE_VERIFIER_REF,
    "checks": [
        "line belongs to an ingested filing with an accession",
        "filing row content hash recomputes from its own columns",
        "caller's copy is byte-identical to the Core row",
    ],
})


def figure_candidate_numerics(figure: Mapping[str, Any]) -> dict[str, Any]:
    """The numeric fields a CandidateClaim takes from a verified figure row.

    One mapping, in one place, because two of the four fields need one: a
    figure with no scale word is scale ``one`` (the CandidateClaim contract has
    no null scale for a number), and the value is canonicalised the way every
    other decimal in this module is.  Everything else is copied.
    """

    scale = figure.get("scale")
    return {
        "value": _decimal(_canonical_decimal_text(figure["value"]), "figure.value"),
        "unit": figure["unit"],
        "currency": figure.get("currency"),
        "scale": scale if scale else "one",
        "period": figure["period"],
    }


def _canonical_decimal_text(value: Any) -> str:
    """``"5.60"`` and ``"5.6"`` are the same number; the contract wants one."""

    if not isinstance(value, str):
        raise ResearchVerificationError("figure value must be text")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ResearchVerificationError("figure value is not a decimal") from exc
    formatted = format(parsed, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return "0" if formatted in {"", "-0"} else formatted
_AUTHORITY_PROVENANCE_MODES = frozenset({
    "connector_authority", TRANSCRIPT_CORE_AUTHORITY_MODE,
    PUBLIC_WEB_CORE_AUTHORITY_MODE, REGISTERED_ANNUAL_REPORT_AUTHORITY_MODE,
    "mission_figure_authority",
})
PUBLIC_WEB_SOURCE_VERIFIER_REF = "verifier:public-web-core-authority-source:0.1"
REGISTERED_ANNUAL_REPORT_SOURCE_VERIFIER_REF = (
    "verifier:registered-annual-report-core-authority-source:0.1"
)
REGISTERED_ANNUAL_REPORT_SOURCE_VERIFIER_HASH = content_hash({
    "ref": REGISTERED_ANNUAL_REPORT_SOURCE_VERIFIER_REF,
    "rules": [
        "core-statement-filing-issuer", "core-acquired-sec-review",
        "source-manifest-binding", "source-content-hash", "retrieval-proof",
        "independent-model-verification",
    ],
})
TRANSCRIPT_SOURCE_VERIFIER_REF = "verifier:transcript-core-authority-source:0.1"
TRANSCRIPT_SOURCE_VERIFIER_HASH = content_hash({
    "ref": TRANSCRIPT_SOURCE_VERIFIER_REF,
    "rules": [
        "persisted-citation-eligibility", "correction-set-lineage",
        "core-source-envelope", "core-invocation-execution", "core-raw-artifact",
        "alphaengine-document-digest-binding", "profile-source-type", "schema",
        "citation-projection", "time-order",
    ],
})
PUBLIC_WEB_SOURCE_VERIFIER_HASH = content_hash({
    "ref": PUBLIC_WEB_SOURCE_VERIFIER_REF,
    "rules": [
        "persisted-citation-eligibility", "correction-set-lineage",
        "core-source-envelope", "core-invocation-execution", "core-raw-artifact",
        "alphaengine-document-digest-binding", "profile-source-type", "schema",
        "citation-projection", "time-order",
    ],
})
_NUMERIC_VERIFIER_REF = "verifier:offline-numeric:0.1"
_NUMERIC_VERIFIER_HASH = content_hash({
    "ref": _NUMERIC_VERIFIER_REF,
    "operators": sorted(_OPERATORS),
    "numeric": "canonical-decimal",
    "input_binding": "source-material-json-pointer",
})


class ResearchVerificationError(ValueError):
    """Malformed or unverifiable offline material."""


class ResearchVerificationConflict(ResearchVerificationError):
    """A stable reference was presented with a different payload/binding."""


class VerificationRejected(ResearchVerificationError):
    """A candidate cannot cross the verification/staging boundary."""


def _json(value: Any, name: str) -> Any:
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ResearchVerificationError(f"{name} must be finite JSON") from exc


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchVerificationError(f"{name} must be an object")
    wire = dict(value)
    missing = fields - set(wire)
    unknown = set(wire) - fields
    if missing or unknown:
        raise ResearchVerificationError(
            f"{name} closed shape mismatch: missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    return _json(wire, name)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResearchVerificationError(f"{name} must be a non-empty string")
    return value


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise ResearchVerificationError(f"{name} must be lowercase SHA-256 hex")
    return value


def _integer(value: Any, name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ResearchVerificationError(f"{name} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ResearchVerificationError(f"{name} must be an integer <= {maximum}")
    return value


def _timestamp(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchVerificationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ResearchVerificationError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _with_hash(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    wire = dict(value)
    declared = _hash(wire.pop("content_hash"), f"{name}.content_hash")
    expected = content_hash(wire)
    if declared != expected:
        raise ResearchVerificationConflict(f"{name} content_hash mismatch")
    wire["content_hash"] = declared
    return wire


def _ref_hash(value: Any, name: str) -> dict[str, str]:
    wire = _closed(value, {"ref", "hash"}, name)
    return {"ref": _text(wire["ref"], f"{name}.ref"), "hash": _hash(wire["hash"], f"{name}.hash")}


def _ref_hashes(value: Any, name: str, *, nonempty: bool = False) -> list[dict[str, str]]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ResearchVerificationError(f"{name} must be {'non-empty ' if nonempty else ''}an array")
    result = [_ref_hash(item, f"{name}[{i}]") for i, item in enumerate(value)]
    if len({item["ref"] for item in result}) != len(result):
        raise ResearchVerificationError(f"{name} refs must be unique")
    return result


def _strings(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ResearchVerificationError(f"{name} must be {'non-empty ' if nonempty else ''}an array")
    result = [_text(item, f"{name}[]") for item in value]
    if len(set(result)) != len(result):
        raise ResearchVerificationError(f"{name} must contain unique strings")
    return result


def _sha256_bytes(value: bytes, name: str) -> str:
    if not isinstance(value, bytes):
        raise ResearchVerificationError(f"{name} must be bytes")
    return hashlib.sha256(value).hexdigest()


def _canonical_raw_bytes(value: Any, name: str) -> bytes:
    if isinstance(value, bytes):
        return value
    return canonical_json(value).encode("utf-8")


_MATERIAL_FIELDS = {
    "schema_version", "id", "created_at", "source_envelope_ref", "source_envelope_hash",
    "artifact_ref", "artifact_hash", "source_ref", "operation", "scenario", "fixture_ref",
    "fixture_hash", "source_record_refs", "next_cursor", "raw_payload", "raw_payload_hash",
    "source_schema_hash", "source_lineage", "published_at", "updated_at", "as_of", "retrieved_at",
    "completeness", "status", "content_hash",
}

_AUTHORITY_MATERIAL_FIELDS = {
    "schema_version", "id", "created_at", "source_envelope_ref", "source_envelope_hash",
    "artifact_ref", "artifact_hash", "source_ref", "source_type", "operation",
    "provenance_mode", "authority_resolution_ref", "authority_resolution_hash",
    "source_record_refs", "next_cursor", "normalized_payload", "normalized_payload_hash",
    "source_schema_hash", "source_content_hash", "source_lineage", "published_at",
    "updated_at", "as_of", "retrieved_at", "completeness", "status", "content_hash",
}


def validate_source_verification_material(value: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Mapping) and value.get("schema_version") == "0.2":
        return _validate_authority_source_verification_material(value)
    wire = _closed(value, _MATERIAL_FIELDS, "SourceVerificationMaterial")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ResearchVerificationError("unsupported SourceVerificationMaterial schema_version")
    for name in ("id", "source_envelope_ref", "artifact_ref", "source_ref", "operation", "scenario", "fixture_ref"):
        wire[name] = _text(wire[name], name)
    for name in ("source_envelope_hash", "artifact_hash", "fixture_hash", "source_schema_hash", "raw_payload_hash"):
        wire[name] = _hash(wire[name], name)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    wire["source_record_refs"] = _strings(wire["source_record_refs"], "source_record_refs")
    wire["source_lineage"] = _strings(wire["source_lineage"], "source_lineage", nonempty=True)
    wire["next_cursor"] = None if wire["next_cursor"] is None else _text(wire["next_cursor"], "next_cursor")
    for name in ("published_at", "updated_at", "as_of"):
        wire[name] = _timestamp(wire[name], name, nullable=True)
    wire["retrieved_at"] = _timestamp(wire["retrieved_at"], "retrieved_at")
    if wire["completeness"] not in {"enumerated", "ranked", "partial", "unknown"}:
        raise ResearchVerificationError("SourceVerificationMaterial.completeness is invalid")
    if wire["status"] not in {"complete", "partial", "empty", "error"}:
        raise ResearchVerificationError("SourceVerificationMaterial.status is invalid")
    if wire["status"] == "complete" and wire["completeness"] == "unknown":
        raise ResearchVerificationError("complete material cannot have unknown completeness")
    if wire["status"] == "empty" and wire["source_record_refs"]:
        raise ResearchVerificationError("empty material cannot contain source records")
    if wire["status"] == "complete" and not wire["source_record_refs"]:
        raise ResearchVerificationError("complete material must contain source records")
    if wire["status"] == "partial" and wire["completeness"] != "partial":
        raise ResearchVerificationError("partial material requires partial completeness")
    if wire["status"] == "error" and wire["completeness"] not in {"partial", "unknown"}:
        raise ResearchVerificationError("error material must be partial or unknown")
    wire["raw_payload"] = _json(wire["raw_payload"], "raw_payload")
    if wire["raw_payload_hash"] != _sha256_bytes(_canonical_raw_bytes(wire["raw_payload"], "raw_payload"), "raw_payload"):
        raise ResearchVerificationConflict("raw_payload_hash does not bind canonical raw bytes")
    return _with_hash(wire, "SourceVerificationMaterial")


def _validate_authority_source_verification_material(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the separate authority provenance contract.

    Authority material intentionally has no fixture/scenario fields.  This
    prevents a live provider body from being relabeled as a recorded fixture
    and makes the authority-resolution hash an explicit provenance edge.
    """
    wire = _closed(value, _AUTHORITY_MATERIAL_FIELDS, "AuthoritySourceVerificationMaterial")
    if wire["schema_version"] != "0.2":
        raise ResearchVerificationError("unsupported AuthoritySourceVerificationMaterial schema_version")
    for name in (
        "id", "source_envelope_ref", "artifact_ref", "source_ref", "source_type",
        "operation", "provenance_mode", "authority_resolution_ref",
    ):
        wire[name] = _text(wire[name], name)
    if wire["provenance_mode"] not in _AUTHORITY_PROVENANCE_MODES:
        raise ResearchVerificationError("authority material provenance_mode is closed")
    if wire["source_type"] not in {
        "official_filing", "authenticated_library", "social_enumeration",
        "social_search", "public_web", "market_data",
    }:
        raise ResearchVerificationError("authority material source_type is invalid")
    for name in (
        "source_envelope_hash", "artifact_hash", "authority_resolution_hash",
        "source_schema_hash", "source_content_hash", "normalized_payload_hash",
    ):
        wire[name] = _hash(wire[name], name)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    wire["source_record_refs"] = _strings(wire["source_record_refs"], "source_record_refs")
    wire["source_lineage"] = _strings(wire["source_lineage"], "source_lineage", nonempty=True)
    wire["next_cursor"] = None if wire["next_cursor"] is None else _text(wire["next_cursor"], "next_cursor")
    for name in ("published_at", "updated_at", "as_of"):
        wire[name] = _timestamp(wire[name], name, nullable=True)
    wire["retrieved_at"] = _timestamp(wire["retrieved_at"], "retrieved_at")
    if wire["completeness"] not in {"enumerated", "ranked", "partial", "unknown"}:
        raise ResearchVerificationError("authority material completeness is invalid")
    if wire["status"] not in {"complete", "partial", "empty", "error"}:
        raise ResearchVerificationError("authority material status is invalid")
    if wire["status"] == "complete" and wire["completeness"] == "unknown":
        raise ResearchVerificationError("complete authority material cannot have unknown completeness")
    if wire["status"] == "complete" and not wire["source_record_refs"]:
        raise ResearchVerificationError("complete authority material must contain source records")
    if wire["status"] == "empty" and wire["source_record_refs"]:
        raise ResearchVerificationError("empty authority material cannot contain source records")
    wire["normalized_payload"] = _json(
        wire["normalized_payload"], "authority normalized_payload"
    )
    if wire["normalized_payload_hash"] != _sha256_bytes(
        _canonical_raw_bytes(
            wire["normalized_payload"], "authority normalized_payload"
        ),
        "authority normalized_payload",
    ):
        raise ResearchVerificationConflict(
            "authority normalized_payload_hash does not bind canonical structured output"
        )
    return _with_hash(wire, "AuthoritySourceVerificationMaterial")


_NUMERIC_INPUT_FIELDS = {
    "name", "value", "unit", "currency", "scale", "period",
    "source_material_ref", "source_material_hash", "json_pointer", "extractor",
}
_PERIOD_FIELDS = {"kind", "label", "start", "end"}
_ROUNDING_FIELDS = {"mode", "digits"}


def _period(value: Any, name: str) -> Any:
    # Period labels are preserved for compatibility with the formal Claim
    # contract.  Structured periods additionally get deterministic ordering.
    if isinstance(value, str) and value:
        return value
    wire = _closed(value, _PERIOD_FIELDS, name)
    wire["kind"] = _text(wire["kind"], f"{name}.kind")
    wire["label"] = _text(wire["label"], f"{name}.label")
    wire["start"] = _timestamp(wire["start"], f"{name}.start")
    wire["end"] = _timestamp(wire["end"], f"{name}.end")
    if wire["start"] > wire["end"]:
        raise ResearchVerificationError(f"{name}.start follows end")
    return wire


def _decimal(value: Any, name: str) -> str:
    if not isinstance(value, str) or _DECIMAL_RE.fullmatch(value) is None:
        raise ResearchVerificationError(f"{name} must be a canonical decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ResearchVerificationError(f"{name} is not a decimal") from exc
    if not parsed.is_finite():
        raise ResearchVerificationError(f"{name} must be finite")
    formatted = format(parsed, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    if formatted in {"", "-0"}:
        formatted = "0"
    if value != formatted:
        raise ResearchVerificationError(f"{name} is not canonical; expected {formatted!r}")
    return value


def _currency(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"[A-Z]{3}", value) is None:
        raise ResearchVerificationError(f"{name} must be an ISO-4217 uppercase code or null")
    return value


def _scale(value: Any, name: str) -> str:
    value = _text(value, name)
    aliases = {"one", "thousand", "million", "billion"}
    if value in aliases:
        return value
    if Decimal(_decimal(value, name)) <= 0:
        raise ResearchVerificationError(f"{name} must be positive")
    return value


def _rounding(value: Any, name: str) -> dict[str, Any]:
    wire = _closed(value, _ROUNDING_FIELDS, name)
    if wire["mode"] not in _ROUNDING:
        raise ResearchVerificationError(f"{name}.mode is unsupported")
    wire["digits"] = _integer(wire["digits"], f"{name}.digits", maximum=18)
    return wire


def _validate_numeric_input(value: Any, name: str) -> dict[str, Any]:
    wire = _closed(value, _NUMERIC_INPUT_FIELDS, name)
    wire["name"] = _text(wire["name"], f"{name}.name")
    wire["value"] = _decimal(wire["value"], f"{name}.value")
    wire["unit"] = _text(wire["unit"], f"{name}.unit")
    wire["currency"] = _currency(wire["currency"], f"{name}.currency")
    wire["scale"] = _scale(wire["scale"], f"{name}.scale")
    wire["period"] = _period(wire["period"], f"{name}.period")
    wire["source_material_ref"] = _text(
        wire["source_material_ref"], f"{name}.source_material_ref"
    )
    wire["source_material_hash"] = _hash(
        wire["source_material_hash"], f"{name}.source_material_hash"
    )
    wire["json_pointer"] = _text(wire["json_pointer"], f"{name}.json_pointer")
    if not wire["json_pointer"].startswith("/"):
        raise ResearchVerificationError(f"{name}.json_pointer must be absolute")
    if wire["extractor"] not in {"number", "count"}:
        raise ResearchVerificationError(f"{name}.extractor is unsupported")
    return wire


_NUMERIC_SPEC_FIELDS = {
    "schema_version", "id", "created_at", "operator", "inputs", "output_value", "output_unit",
    "output_currency", "output_scale", "output_period", "rounding", "content_hash",
}


def validate_numeric_verification_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = _closed(value, _NUMERIC_SPEC_FIELDS, "NumericVerificationSpec")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ResearchVerificationError("unsupported NumericVerificationSpec schema_version")
    wire["id"] = _text(wire["id"], "id")
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    if wire["operator"] not in _OPERATORS:
        raise ResearchVerificationError("NumericVerificationSpec.operator is unsupported")
    if not isinstance(wire["inputs"], list) or not wire["inputs"]:
        raise ResearchVerificationError("NumericVerificationSpec.inputs must be non-empty")
    wire["inputs"] = [_validate_numeric_input(item, f"inputs[{i}]") for i, item in enumerate(wire["inputs"])]
    if len({item["name"] for item in wire["inputs"]}) != len(wire["inputs"]):
        raise ResearchVerificationError("NumericVerificationSpec input names must be unique")
    wire["output_value"] = _decimal(wire["output_value"], "output_value")
    wire["output_unit"] = _text(wire["output_unit"], "output_unit")
    wire["output_currency"] = _currency(wire["output_currency"], "output_currency")
    wire["output_scale"] = _scale(wire["output_scale"], "output_scale")
    wire["output_period"] = _period(wire["output_period"], "output_period")
    wire["rounding"] = _rounding(wire["rounding"], "rounding")
    arity = len(wire["inputs"])
    if wire["operator"] == "identity" and arity != 1:
        raise ResearchVerificationError("identity requires exactly one input")
    if wire["operator"] in {"difference", "ratio", "growth_percentage"} and arity != 2:
        raise ResearchVerificationError(
            f"{wire['operator']} requires exactly two inputs"
        )
    if wire["operator"] == "sum" and arity < 2:
        raise ResearchVerificationError("sum requires at least two inputs")
    return _with_hash(wire, "NumericVerificationSpec")


_BUNDLE_FINDING_FIELDS = {"code", "severity", "status", "path", "expected", "observed", "message", "content_hash"}


def _finding(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    wire = _closed(value, _BUNDLE_FINDING_FIELDS, name)
    wire["code"] = _text(wire["code"], f"{name}.code")
    if wire["severity"] not in {"info", "warning", "error"}:
        raise ResearchVerificationError(f"{name}.severity is invalid")
    if wire["status"] not in {"pass", "fail"}:
        raise ResearchVerificationError(f"{name}.status is invalid")
    wire["path"] = _text(wire["path"], f"{name}.path")
    wire["expected"] = None if wire["expected"] is None else _text(wire["expected"], f"{name}.expected")
    wire["observed"] = None if wire["observed"] is None else _text(wire["observed"], f"{name}.observed")
    wire["message"] = _text(wire["message"], f"{name}.message")
    if wire["status"] == "fail" and wire["severity"] == "info":
        raise ResearchVerificationError(f"{name} failed finding cannot be informational")
    return _with_hash(wire, name)


_BUNDLE_FIELDS = {
    "schema_version", "id", "created_at", "kind", "subject_ref", "subject_hash", "verdict",
    "checkpoint_ref", "checkpoint_hash", "verifier_ref", "verifier_hash",
    "findings", "content_hash",
}


def validate_verification_bundle(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = _closed(value, _BUNDLE_FIELDS, "VerificationBundle")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ResearchVerificationError("unsupported VerificationBundle schema_version")
    for name in ("id", "subject_ref", "checkpoint_ref", "verifier_ref"):
        wire[name] = _text(wire[name], name)
    for name in ("subject_hash", "checkpoint_hash", "verifier_hash"):
        wire[name] = _hash(wire[name], name)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    if wire["kind"] not in {"source", "numeric"}:
        raise ResearchVerificationError("VerificationBundle.kind is invalid")
    expected_verifiers = (
        {
            (_SOURCE_VERIFIER_REF, _SOURCE_VERIFIER_HASH),
            (_AUTHORITY_SOURCE_VERIFIER_REF, _AUTHORITY_SOURCE_VERIFIER_HASH),
            (TRANSCRIPT_SOURCE_VERIFIER_REF, TRANSCRIPT_SOURCE_VERIFIER_HASH),
            (PUBLIC_WEB_SOURCE_VERIFIER_REF, PUBLIC_WEB_SOURCE_VERIFIER_HASH),
            (REGISTERED_ANNUAL_REPORT_SOURCE_VERIFIER_REF,
             REGISTERED_ANNUAL_REPORT_SOURCE_VERIFIER_HASH),
            (MISSION_FIGURE_SOURCE_VERIFIER_REF, MISSION_FIGURE_SOURCE_VERIFIER_HASH),
        }
        if wire["kind"] == "source"
        else {
            (_NUMERIC_VERIFIER_REF, _NUMERIC_VERIFIER_HASH),
            (FIGURE_NUMERIC_VERIFIER_REF, FIGURE_NUMERIC_VERIFIER_HASH),
            (STATEMENT_LINE_VERIFIER_REF, STATEMENT_LINE_VERIFIER_HASH),
        }
    )
    if (wire["verifier_ref"], wire["verifier_hash"]) not in expected_verifiers:
        raise ResearchVerificationConflict("VerificationBundle verifier version drifted")
    if wire["verdict"] not in {"pass", "reject"}:
        raise ResearchVerificationError("VerificationBundle.verdict is invalid")
    if not isinstance(wire["findings"], list):
        raise ResearchVerificationError("VerificationBundle.findings must be an array")
    wire["findings"] = [_finding(item, f"findings[{i}]") for i, item in enumerate(wire["findings"])]
    expected_reject = any(item["severity"] == "error" and item["status"] == "fail" for item in wire["findings"])
    if (wire["verdict"] == "reject") != expected_reject:
        raise ResearchVerificationConflict("VerificationBundle verdict does not match error findings")
    return _with_hash(wire, "VerificationBundle")


_CANDIDATE_EVIDENCE_FIELDS = {
    "schema_version", "id", "created_at", "candidate_evidence_ref", "version", "source_type", "source_ref",
    "source_envelope_ref", "source_envelope_hash", "artifact_refs", "retrieved_at", "valid_until",
    "source_lineage", "independence_group", "source_verification_ref", "source_verification_hash",
    "actor_ref", "prior_version_ref", "content_hash",
}


def validate_candidate_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = _closed(value, _CANDIDATE_EVIDENCE_FIELDS, "CandidateEvidence")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ResearchVerificationError("unsupported CandidateEvidence schema_version")
    for name in ("id", "candidate_evidence_ref", "source_type", "source_ref", "source_envelope_ref", "independence_group", "source_verification_ref", "actor_ref"):
        wire[name] = _text(wire[name], name)
    if not wire["candidate_evidence_ref"].startswith("candidate-evidence:"):
        raise ResearchVerificationError("CandidateEvidence must use a candidate-only identity")
    wire["version"] = _integer(wire["version"], "version", minimum=1)
    for name in ("source_envelope_hash", "source_verification_hash"):
        wire[name] = _hash(wire[name], name)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    wire["retrieved_at"] = _timestamp(wire["retrieved_at"], "retrieved_at")
    wire["valid_until"] = _timestamp(wire["valid_until"], "valid_until", nullable=True)
    wire["artifact_refs"] = _ref_hashes(wire["artifact_refs"], "artifact_refs", nonempty=True)
    wire["source_lineage"] = _strings(wire["source_lineage"], "source_lineage", nonempty=True)
    wire["prior_version_ref"] = None if wire["prior_version_ref"] is None else _text(wire["prior_version_ref"], "prior_version_ref")
    return _with_hash(wire, "CandidateEvidence")


_CANDIDATE_CLAIM_FIELDS = {
    "schema_version", "id", "created_at", "candidate_claim_ref", "version", "subject_ref", "metric_or_aspect",
    "period", "basis", "normalized_statement", "semantic_verification_status", "claim_kind", "value", "unit", "currency", "scale",
    "candidate_evidence_refs", "source_verification_ref", "source_verification_hash", "numeric_spec_ref",
    "numeric_spec_hash", "numeric_verification_ref", "numeric_verification_hash", "actor_ref", "prior_version_ref",
    "content_hash",
}


def validate_candidate_claim(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = _closed(value, _CANDIDATE_CLAIM_FIELDS, "CandidateClaim")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ResearchVerificationError("unsupported CandidateClaim schema_version")
    for name in ("id", "candidate_claim_ref", "subject_ref", "metric_or_aspect", "basis", "normalized_statement", "source_verification_ref", "actor_ref"):
        wire[name] = _text(wire[name], name)
    if not wire["candidate_claim_ref"].startswith("candidate-claim:"):
        raise ResearchVerificationError("CandidateClaim must use a candidate-only identity")
    wire["version"] = _integer(wire["version"], "version", minimum=1)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    wire["period"] = _period(wire["period"], "period")
    if wire["semantic_verification_status"] != "unverified":
        raise ResearchVerificationError(
            "candidate claim semantics remain unverified until human review"
        )
    if wire["claim_kind"] == "quantitative":
        for name in ("numeric_spec_ref", "numeric_verification_ref"):
            wire[name] = _text(wire[name], name)
        wire["value"] = _decimal(wire["value"], "value")
        wire["unit"] = _text(wire["unit"], "unit")
        wire["scale"] = _scale(wire["scale"], "scale")
        wire["currency"] = _currency(wire["currency"], "currency")
        for name in ("numeric_spec_hash", "numeric_verification_hash"):
            wire[name] = _hash(wire[name], name)
    elif wire["claim_kind"] == "qualitative":
        # ADR-0003 option B: a semantic candidate carries no numeric authority.
        # Any value or numeric reference is an attempt to smuggle a number
        # past the deterministic numeric verifier and is rejected outright.
        offending = [
            name for name in (
                "value", "unit", "currency", "scale", "numeric_spec_ref",
                "numeric_spec_hash", "numeric_verification_ref",
                "numeric_verification_hash",
            )
            if wire[name] is not None
        ]
        if offending:
            raise ResearchVerificationError(
                "qualitative candidate claim must not carry numeric fields: "
                + ", ".join(offending)
            )
    else:
        raise ResearchVerificationError(
            "CandidateClaim.claim_kind must be quantitative or qualitative"
        )
    wire["candidate_evidence_refs"] = _ref_hashes(wire["candidate_evidence_refs"], "candidate_evidence_refs", nonempty=True)
    wire["source_verification_hash"] = _hash(wire["source_verification_hash"], "source_verification_hash")
    wire["prior_version_ref"] = None if wire["prior_version_ref"] is None else _text(wire["prior_version_ref"], "prior_version_ref")
    return _with_hash(wire, "CandidateClaim")


def _fixture_for_source(source_ref: str) -> dict[str, Any]:
    if source_ref == "source:cninfo":
        return load_recorded_source_fixture("cninfo")
    if source_ref == "source:sec-edgar":
        return load_recorded_source_fixture("sec")
    if source_ref == "source:alphaengine":
        return load_recorded_alphaengine_fixture()
    raise ResearchVerificationError("source is outside the packaged offline fixture set")


def _fixture_material_payload(fixture: Mapping[str, Any], scenario_name: str) -> tuple[list[str], Any, Any, str | None, str]:
    scenarios = [item for item in fixture["scenarios"] if item["scenario"] == scenario_name]
    if len(scenarios) != 1:
        raise ResearchVerificationError("fixture scenario is not uniquely frozen")
    scenario = scenarios[0]
    source_ref = fixture["source_ref"]
    if source_ref == "source:alphaengine":
        records = list(scenario["source_record_refs"])
        raw_payload = scenario["raw_payload"]
        next_cursor = scenario["next_cursor"]
    else:
        records = []
        for page in scenario["pages"]:
            payload = page["raw_payload"]
            if not isinstance(payload, Mapping):
                continue
            collection = "announcements" if source_ref == "source:cninfo" else "filings"
            key = "announcement_id" if source_ref == "source:cninfo" else "accession"
            prefix = "cninfo:announcement:" if source_ref == "source:cninfo" else "sec:filing:"
            records.extend(prefix + str(item[key]) for item in payload[collection])
        raw_payload = [page["raw_payload"] for page in scenario["pages"]]
        next_cursor = scenario["pages"][-1]["next_cursor"] if scenario["pages"] else None
    source_doc = {
        "fixture_ref": fixture["id"], "fixture_hash": fixture["content_hash"],
        "source_ref": source_ref, "operation": fixture["operation"], "scenario": scenario_name,
        "source_record_refs": records, "next_cursor": next_cursor,
    }
    artifact_doc = {
        "fixture_ref": fixture["id"], "fixture_hash": fixture["content_hash"],
        "scenario": scenario_name, "raw_payload": raw_payload,
    }
    return records, raw_payload, source_doc, next_cursor, content_hash(artifact_doc)


def build_source_verification_material(
    *, source_ref: str, scenario: str, source_envelope_ref: str, source_envelope_hash: str,
    artifact_ref: str, created_at: str, retrieved_at: str,
    completeness: str, status: str,
) -> dict[str, Any]:
    """Build material from a packaged reference fixture, never from network data."""
    fixture = _fixture_for_source(source_ref)
    records, raw_payload, source_doc, next_cursor, artifact_hash = _fixture_material_payload(fixture, scenario)
    inventory = load_packaged_connector_inventory()
    slug = {"source:cninfo": "cninfo", "source:sec-edgar": "sec", "source:alphaengine": "alphaengine"}[source_ref]
    template = inventory["templates"][slug]
    operation = next(item for item in template["operations"] if item["operation"] == fixture["operation"])
    base = {
        "schema_version": SCHEMA_VERSION, "id": "source-material:fixture:" + content_hash({"source": source_ref, "scenario": scenario}),
        "created_at": created_at, "source_envelope_ref": source_envelope_ref, "source_envelope_hash": source_envelope_hash,
        "artifact_ref": artifact_ref, "artifact_hash": artifact_hash, "source_ref": source_ref,
        "operation": fixture["operation"], "scenario": scenario, "fixture_ref": fixture["id"],
        "fixture_hash": fixture["content_hash"], "source_record_refs": records, "next_cursor": next_cursor,
        "raw_payload": raw_payload, "raw_payload_hash": _sha256_bytes(_canonical_raw_bytes(raw_payload, "raw_payload"), "raw_payload"),
        "source_schema_hash": operation["output_schema_hash"],
        "source_lineage": [source_ref, fixture["id"]],
        "published_at": None, "updated_at": None, "as_of": None, "retrieved_at": retrieved_at,
        "completeness": completeness, "status": status,
    }
    base["content_hash"] = content_hash(base)
    return validate_source_verification_material(base)


def _finding_wire(code: str, severity: str, status: str, path: str, expected: Any, observed: Any, message: str) -> dict[str, Any]:
    base = {
        "code": code, "severity": severity, "status": status, "path": path,
        "expected": None if expected is None else str(expected),
        "observed": None if observed is None else str(observed), "message": message,
    }
    base["content_hash"] = content_hash(base)
    return _finding(base, "VerificationFinding")


def _binding_findings(
    *, checkpoint: Mapping[str, Any], plan: Mapping[str, Any], context_pack: Mapping[str, Any], step: Mapping[str, Any],
    runner_request: Mapping[str, Any], receipt: Mapping[str, Any], material: Mapping[str, Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    checks = [
        ("checkpoint_outcome", checkpoint["outcome"], "succeeded", "checkpoint must be completed successfully"),
        ("receipt_status", receipt["status"], "succeeded", "receipt must be successful"),
        ("receipt_ref", checkpoint["completion_receipt_ref"], receipt["id"], "checkpoint receipt ref must match"),
        ("receipt_hash", checkpoint["completion_receipt_hash"], receipt["content_hash"], "checkpoint receipt hash must match"),
        ("receipt_request_ref", receipt["runner_request_ref"], runner_request["id"], "receipt request ref must match"),
        ("receipt_request_hash", receipt["runner_request_hash"], runner_request["content_hash"], "receipt request hash must match"),
        ("request_ref", checkpoint["runner_request_ref"], runner_request["id"], "checkpoint request ref must match"),
        ("request_hash", checkpoint["runner_request_hash"], runner_request["content_hash"], "checkpoint request hash must match"),
        ("idempotency_key", checkpoint["idempotency_key"], runner_request["idempotency_key"], "checkpoint idempotency key must match"),
        ("connector_attempt_number", checkpoint["connector_attempt_number"], runner_request["scheduler_attempt_number"], "connector attempt must match"),
        ("plan_ref", checkpoint["compiled_plan_ref"], plan["id"], "checkpoint plan ref must match"),
        ("plan_hash", checkpoint["compiled_plan_hash"], plan["content_hash"], "checkpoint plan hash must match"),
        ("context_ref", checkpoint["context_pack_ref"], context_pack["id"], "checkpoint context ref must match"),
        ("context_hash", checkpoint["context_pack_hash"], context_pack["content_hash"], "checkpoint context hash must match"),
        ("step_ref", checkpoint["step_ref"], step["id"], "checkpoint step ref must match"),
        ("step_hash", checkpoint["step_hash"], step["content_hash"], "checkpoint step hash must match"),
        ("receipt_source_refs", receipt["source_envelopes"], checkpoint["source_envelopes"], "receipt/checkpoint source refs must match"),
        ("receipt_artifact_refs", receipt["artifacts"], checkpoint["artifacts"], "receipt/checkpoint artifact refs must match"),
        ("material_source_ref", material["source_envelope_ref"], (receipt["source_envelopes"] or [{}])[0].get("ref"), "material source ref must match receipt"),
        ("material_source_hash", material["source_envelope_hash"], (receipt["source_envelopes"] or [{}])[0].get("hash"), "material source hash must match receipt"),
        ("material_artifact_ref", material["artifact_ref"], (receipt["artifacts"] or [{}])[0].get("ref"), "material artifact ref must match receipt"),
        ("material_artifact_hash", material["artifact_hash"], None, "material artifact hash is recomputed below"),
    ]
    for code, observed, expected, message in checks:
        if code == "material_artifact_hash":
            continue
        ok = observed == expected
        findings.append(_finding_wire(code, "info" if ok else "error", "pass" if ok else "fail", code, expected, observed, message))
    try:
        validate_runner_request_plan_binding(runner_request, plan, step)
        findings.append(_finding_wire("runner_plan_binding", "info", "pass", "runner_request", "exact", "exact", "RunnerRequest binds plan and step"))
    except Exception as exc:
        findings.append(_finding_wire("runner_plan_binding", "error", "fail", "runner_request", "exact", "mismatch", str(exc)))
    if context_pack["task_ref"] != plan["task_ref"] or context_pack["task_hash"] != plan["task_hash"]:
        findings.append(_finding_wire("context_task_binding", "error", "fail", "context_pack.task", plan["task_ref"], context_pack["task_ref"], "ContextPack task binding drifted"))
    expected_authority = {
        "connector_profile_ref": runner_request["connector_profile_ref"],
        "connector_profile_hash": runner_request["connector_profile_hash"],
        "capability_lease_ref": runner_request["capability_lease_ref"],
        "capability_lease_hash": runner_request["capability_lease_hash"],
        "source_ref": step["source_ref"],
        "source_hash": step["source_hash"],
    }
    authority_ok = checkpoint["authority_bindings"] == expected_authority
    findings.append(_finding_wire(
        "checkpoint_authority", "info" if authority_ok else "error",
        "pass" if authority_ok else "fail", "checkpoint.authority_bindings",
        canonical_json(expected_authority), canonical_json(checkpoint["authority_bindings"]),
        "checkpoint authority is exact" if authority_ok else "checkpoint authority drifted",
    ))
    cardinality_ok = len(receipt["source_envelopes"]) == 1 and len(receipt["artifacts"]) == 1
    findings.append(_finding_wire(
        "source_artifact_cardinality", "info" if cardinality_ok else "error",
        "pass" if cardinality_ok else "fail", "receipt.outputs", "one source and one artifact",
        f"{len(receipt['source_envelopes'])}/{len(receipt['artifacts'])}",
        "fixture completion has one source and one raw artifact" if cardinality_ok
        else "fixture completion cardinality is invalid",
    ))
    return findings


def verify_source_material(
    material: Mapping[str, Any], *, checkpoint: Mapping[str, Any], plan: Mapping[str, Any], context_pack: Mapping[str, Any],
    step: Mapping[str, Any], runner_request: Mapping[str, Any], receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify one completed fixture step and return a deterministic bundle."""
    material_wire = validate_source_verification_material(material)
    checkpoint_wire = validate_research_checkpoint(checkpoint)
    plan_wire = validate_compiled_connector_plan(plan)
    context_wire = validate_context_pack(context_pack)
    step_wire = validate_compiled_connector_step(step)
    receipt_wire = validate_connector_completion_receipt(receipt)
    request_wire = validate_runner_request_plan_binding(runner_request, plan_wire, step_wire)
    findings = _binding_findings(
        checkpoint=checkpoint_wire, plan=plan_wire, context_pack=context_wire, step=step_wire,
        runner_request=request_wire, receipt=receipt_wire, material=material_wire,
    )
    fixture = _fixture_for_source(material_wire["source_ref"])
    try:
        records, raw_payload, source_doc, next_cursor, expected_artifact_hash = _fixture_material_payload(fixture, material_wire["scenario"])
        checks = [
            ("fixture_ref", material_wire["fixture_ref"], fixture["id"]),
            ("fixture_hash", material_wire["fixture_hash"], fixture["content_hash"]),
            ("source_lineage", material_wire["source_lineage"], [material_wire["source_ref"], fixture["id"]]),
            ("source_ref", material_wire["source_ref"], step_wire["source_ref"]),
            ("operation", material_wire["operation"], step_wire["operation"]),
            ("records", material_wire["source_record_refs"], records),
            ("next_cursor", material_wire["next_cursor"], next_cursor),
            ("raw_payload", material_wire["raw_payload"], raw_payload),
            (
                "source_envelope_hash_recomputed",
                material_wire["source_envelope_hash"],
                content_hash(source_doc),
            ),
            ("artifact_hash_recomputed", material_wire["artifact_hash"], expected_artifact_hash),
            ("raw_payload_hash_recomputed", material_wire["raw_payload_hash"], _sha256_bytes(_canonical_raw_bytes(raw_payload, "fixture raw payload"), "fixture raw payload")),
            ("retrieved_at", material_wire["retrieved_at"], receipt_wire["created_at"]),
        ]
        inventory = load_packaged_connector_inventory()
        slug = {"source:cninfo": "cninfo", "source:sec-edgar": "sec", "source:alphaengine": "alphaengine"}[material_wire["source_ref"]]
        template = inventory["templates"][slug]
        operation = next(item for item in template["operations"] if item["operation"] == material_wire["operation"])
        checks.append(("source_schema_hash", material_wire["source_schema_hash"], operation["output_schema_hash"]))
    except Exception as exc:
        findings.append(_finding_wire("fixture_replay", "error", "fail", "material", "packaged fixture", "unavailable", str(exc)))
        checks = []
    for code, observed, expected in checks:
        ok = observed == expected
        findings.append(_finding_wire(code, "info" if ok else "error", "pass" if ok else "fail", f"material.{code}", expected, observed, f"{code} matches packaged fixture" if ok else f"{code} drifted from packaged fixture"))
    for code, before, after in (("published_before_retrieved", material_wire["published_at"], material_wire["retrieved_at"]), ("updated_before_retrieved", material_wire["updated_at"], material_wire["retrieved_at"]), ("as_of_before_retrieved", material_wire["as_of"], material_wire["retrieved_at"])):
        ok = before is None or before <= after
        findings.append(_finding_wire(code, "info" if ok else "error", "pass" if ok else "fail", f"material.{code}", after, before, "time ordering is valid" if ok else "source time is after retrieval"))
    required = step_wire["completeness_required"]
    completeness_ok = required == "partial_allowed" or material_wire["completeness"] == required
    findings.append(_finding_wire("completeness", "info" if completeness_ok else "error", "pass" if completeness_ok else "fail", "material.completeness", required, material_wire["completeness"], "completeness satisfies plan" if completeness_ok else "completeness is weaker than plan"))
    verdict = "pass" if not any(item["severity"] == "error" and item["status"] == "fail" for item in findings) else "reject"
    base = {
        "schema_version": SCHEMA_VERSION,
        "id": "verification-bundle:source:" + content_hash({"subject": material_wire["id"], "checkpoint": checkpoint_wire["content_hash"], "findings": [item["content_hash"] for item in findings]}),
        "created_at": material_wire["retrieved_at"], "kind": "source", "subject_ref": material_wire["id"],
        "subject_hash": material_wire["content_hash"], "verdict": verdict,
        "checkpoint_ref": checkpoint_wire["id"], "checkpoint_hash": checkpoint_wire["content_hash"], "findings": findings,
        "verifier_ref": _SOURCE_VERIFIER_REF, "verifier_hash": _SOURCE_VERIFIER_HASH,
    }
    base["content_hash"] = content_hash(base)
    return validate_verification_bundle(base)


def build_authority_source_material(resolved: Any) -> dict[str, Any]:
    """Build verifier material from a resolved authority join.

    ``normalized_payload`` is the adapter's structured observation used by
    numeric verification.  The provider bytes remain separately bound by the
    ArtifactVersion/raw hash and are replayed by the authority resolver.
    """
    summary = resolved.summary
    observation = resolved.records.get("observation")
    if not isinstance(observation, Mapping) or not isinstance(observation.get("structured_output"), Mapping):
        raise ResearchVerificationError("authority resolution lacks structured adapter observation")
    normalized_payload = _json(
        observation["structured_output"], "authority structured output"
    )
    profile = resolved.records.get("profile")
    if not isinstance(profile, Mapping):
        raise ResearchVerificationError("authority resolution lacks trusted profile")
    source_identity = profile.get("source_identity")
    if not isinstance(source_identity, Mapping):
        raise ResearchVerificationError("authority profile lacks source identity")
    source_type = source_identity.get("source_type")
    if source_type not in {
        "official_filing", "authenticated_library", "social_enumeration",
        "social_search", "public_web", "market_data",
    }:
        raise ResearchVerificationError("authority profile source type is not closed")
    if source_type == "public_web" and summary.get("operation") != "fetch_get":
        raise ResearchVerificationError(
            "public web discovery/search/HEAD authority cannot form evidence material; "
            "fetch_get the original source first"
        )
    base = {
        "schema_version": "0.2",
        "id": "source-material:authority:" + summary["source_envelope_hash"],
        "created_at": summary["created_at"],
        "source_envelope_ref": summary["source_envelope_ref"],
        "source_envelope_hash": summary["source_envelope_hash"],
        "artifact_ref": summary["artifact_ref"],
        "artifact_hash": summary["artifact_hash"],
        "source_ref": summary["source_ref"],
        "source_type": source_type,
        "operation": summary["operation"],
        "provenance_mode": "connector_authority",
        "authority_resolution_ref": summary["id"],
        "authority_resolution_hash": summary["content_hash"],
        "source_record_refs": list(summary["source_record_refs"]),
        "next_cursor": observation.get("cursor"),
        "normalized_payload": normalized_payload,
        "normalized_payload_hash": _sha256_bytes(
            _canonical_raw_bytes(normalized_payload, "authority structured output"),
            "authority structured output",
        ),
        "source_schema_hash": summary["source_schema_hash"],
        "source_content_hash": summary["source_content_hash"],
        "source_lineage": [summary["source_ref"], summary["source_envelope_ref"], summary["artifact_ref"], summary["id"]],
        "published_at": summary["published_at"],
        "updated_at": summary["updated_at"],
        "as_of": summary["as_of"],
        "retrieved_at": summary["retrieved_at"],
        "completeness": summary["completeness"],
        "status": summary["status"],
    }
    base["content_hash"] = content_hash(base)
    return validate_source_verification_material(base)


def verify_authority_source_material(
    material: Mapping[str, Any], *, resolver: Any, checkpoint: Mapping[str, Any],
    plan: Mapping[str, Any], context_pack: Mapping[str, Any], step: Mapping[str, Any],
    runner_request: Mapping[str, Any], receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Re-resolve exact connector authority and verify one source material."""
    material_wire = validate_source_verification_material(material)
    checkpoint_wire = validate_research_checkpoint(checkpoint)
    plan_wire = validate_compiled_connector_plan(plan)
    context_wire = validate_context_pack(context_pack)
    step_wire = validate_compiled_connector_step(step)
    receipt_wire = validate_connector_completion_receipt(receipt)
    request_wire = validate_runner_request_plan_binding(runner_request, plan_wire, step_wire)
    findings = _binding_findings(
        checkpoint=checkpoint_wire, plan=plan_wire, context_pack=context_wire,
        step=step_wire, runner_request=request_wire, receipt=receipt_wire,
        material=material_wire,
    )
    try:
        resolved = resolver.resolve(
            material_wire["source_envelope_ref"], checkpoint_ref=checkpoint_wire["id"]
        )
        summary = resolved.summary
        observation = resolved.records["observation"]
        checks = [
            ("authority_resolution_ref", material_wire["authority_resolution_ref"], summary["id"]),
            ("authority_resolution_hash", material_wire["authority_resolution_hash"], summary["content_hash"]),
            ("authority_source_ref", material_wire["source_envelope_ref"], summary["source_envelope_ref"]),
            ("authority_source_hash", material_wire["source_envelope_hash"], summary["source_envelope_hash"]),
            ("authority_artifact_ref", material_wire["artifact_ref"], summary["artifact_ref"]),
            ("authority_artifact_hash", material_wire["artifact_hash"], summary["artifact_hash"]),
            ("authority_source_records", material_wire["source_record_refs"], summary["source_record_refs"]),
            ("authority_schema_hash", material_wire["source_schema_hash"], summary["source_schema_hash"]),
            ("authority_source_content_hash", material_wire["source_content_hash"], summary["source_content_hash"]),
            ("authority_source_type", material_wire["source_type"], resolved.records["profile"]["source_identity"]["source_type"]),
            (
                "authority_normalized_payload",
                material_wire["normalized_payload"],
                observation["structured_output"],
            ),
            ("authority_retrieved_at", material_wire["retrieved_at"], summary["retrieved_at"]),
            ("authority_completeness", material_wire["completeness"], summary["completeness"]),
            ("authority_status", material_wire["status"], summary["status"]),
        ]
        for code, observed, expected in checks:
            ok = observed == expected
            findings.append(_finding_wire(
                code, "info" if ok else "error", "pass" if ok else "fail",
                "authority." + code, expected, observed,
                "authority resolver replay matches" if ok else "authority resolver replay drifted",
            ))
    except Exception as exc:
        findings.append(_finding_wire(
            "authority_resolution", "error", "fail", "authority_resolution",
            "exact passing authority", "unavailable", str(exc),
        ))
    verdict = "pass" if not any(item["severity"] == "error" and item["status"] == "fail" for item in findings) else "reject"
    base = {
        "schema_version": SCHEMA_VERSION,
        "id": "verification-bundle:authority-source:" + content_hash({
            "subject": material_wire["id"], "checkpoint": checkpoint_wire["content_hash"],
            "findings": [item["content_hash"] for item in findings],
        }),
        "created_at": material_wire["retrieved_at"], "kind": "source",
        "subject_ref": material_wire["id"], "subject_hash": material_wire["content_hash"],
        "verdict": verdict, "checkpoint_ref": checkpoint_wire["id"],
        "checkpoint_hash": checkpoint_wire["content_hash"], "findings": findings,
        "verifier_ref": _AUTHORITY_SOURCE_VERIFIER_REF,
        "verifier_hash": _AUTHORITY_SOURCE_VERIFIER_HASH,
    }
    base["content_hash"] = content_hash(base)
    return validate_verification_bundle(base)


def _scale_factor(value: str) -> Decimal:
    return {"one": Decimal("1"), "thousand": Decimal("1000"), "million": Decimal("1000000"), "billion": Decimal("1000000000")}.get(value, Decimal(value))


def _same_metadata(inputs: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> bool:
    return all(len({canonical_json(item[field]) for item in inputs}) == 1 for field in fields)


def _json_pointer(document: Any, pointer: str) -> Any:
    current = document
    for raw_token in pointer.split("/")[1:]:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise ResearchVerificationError(f"json pointer key is absent: {token}")
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise ResearchVerificationError("json pointer array index is invalid")
            index = int(token)
            if index >= len(current):
                raise ResearchVerificationError("json pointer array index is out of range")
            current = current[index]
        else:
            raise ResearchVerificationError("json pointer traversed a scalar")
    return current


def _extract_decimal(material: Mapping[str, Any], item: Mapping[str, Any]) -> str:
    payload_field = (
        "normalized_payload" if material.get("schema_version") == "0.2" else "raw_payload"
    )
    selected = _json_pointer(material[payload_field], item["json_pointer"])
    if item["extractor"] == "count":
        if not isinstance(selected, (list, Mapping)):
            raise ResearchVerificationError("count extractor requires an array or object")
        return str(len(selected))
    if isinstance(selected, bool) or not isinstance(selected, (int, str)):
        raise ResearchVerificationError(
            "number extractor accepts only an integer or canonical decimal string"
        )
    return _decimal(str(selected), "extracted number")


def verify_numeric_spec(
    spec: Mapping[str, Any], *, checkpoint_ref: str, checkpoint_hash: str,
    source_material: Mapping[str, Any], source_bundle: Mapping[str, Any],
) -> dict[str, Any]:
    spec_wire = validate_numeric_verification_spec(spec)
    checkpoint_ref = _text(checkpoint_ref, "checkpoint_ref")
    checkpoint_hash = _hash(checkpoint_hash, "checkpoint_hash")
    material_wire = validate_source_verification_material(source_material)
    source_wire = validate_verification_bundle(source_bundle)
    findings: list[dict[str, Any]] = []
    source_ok = (
        source_wire["verdict"] == "pass"
        and source_wire["kind"] == "source"
        and source_wire["subject_ref"] == material_wire["id"]
        and source_wire["subject_hash"] == material_wire["content_hash"]
        and source_wire["checkpoint_ref"] == checkpoint_ref
        and source_wire["checkpoint_hash"] == checkpoint_hash
        and not any(item["status"] == "fail" for item in source_wire["findings"])
    )
    findings.append(_finding_wire(
        "source_bundle_binding", "info" if source_ok else "error",
        "pass" if source_ok else "fail", "source_bundle",
        "exact passing source material and checkpoint", source_wire["verdict"],
        "source verification is exact" if source_ok else "source verification binding drifted",
    ))
    inputs = spec_wire["inputs"]
    operator = spec_wire["operator"]
    try:
        for item in inputs:
            binding_ok = (
                item["source_material_ref"] == material_wire["id"]
                and item["source_material_hash"] == material_wire["content_hash"]
            )
            extracted = _extract_decimal(material_wire, item) if binding_ok else None
            expected_input_metadata = (
                {"unit": "records", "currency": None, "scale": "one"}
                if item["extractor"] == "count"
                else {"unit": "number", "currency": None, "scale": "one"}
            )
            observed_input_metadata = {
                "unit": item["unit"], "currency": item["currency"],
                "scale": item["scale"],
            }
            value_ok = (
                binding_ok and extracted == item["value"]
                and observed_input_metadata == expected_input_metadata
            )
            findings.append(_finding_wire(
                "input_source_value:" + item["name"],
                "info" if value_ok else "error", "pass" if value_ok else "fail",
                "inputs." + item["name"], extracted, item["value"],
                "numeric input and metadata match verified raw material" if value_ok
                else "numeric input or metadata is not bound to verified raw material",
            ))
        if operator == "identity":
            if len(inputs) != 1:
                raise ResearchVerificationError("identity requires exactly one input")
            raw = Decimal(inputs[0]["value"])
        elif operator == "sum":
            if len(inputs) < 2 or not _same_metadata(inputs, ("unit", "currency", "scale", "period")):
                raise ResearchVerificationError("sum requires two or more inputs with equal unit/currency/scale/period")
            factor = _scale_factor(inputs[0]["scale"])
            raw = sum((Decimal(item["value"]) * _scale_factor(item["scale"]) / factor for item in inputs), Decimal("0"))
        elif operator == "difference":
            if len(inputs) != 2 or not _same_metadata(inputs, ("unit", "currency", "scale", "period")):
                raise ResearchVerificationError("difference requires two inputs with equal unit/currency/scale/period")
            raw = Decimal(inputs[0]["value"]) - Decimal(inputs[1]["value"])
        elif operator == "ratio":
            if len(inputs) != 2 or not _same_metadata(inputs, ("unit", "currency", "scale", "period")):
                raise ResearchVerificationError("ratio requires two inputs with equal unit/currency/scale/period")
            denominator = Decimal(inputs[1]["value"])
            if denominator == 0:
                raise ResearchVerificationError("ratio denominator cannot be zero")
            raw = Decimal(inputs[0]["value"]) / denominator
        elif operator == "growth_percentage":
            if len(inputs) != 2 or not _same_metadata(
                inputs, ("unit", "currency", "scale")
            ):
                raise ResearchVerificationError(
                    "growth_percentage requires two inputs with equal unit/currency/scale"
                )
            denominator = Decimal(inputs[1]["value"])
            if denominator <= 0:
                raise ResearchVerificationError(
                    "growth_percentage denominator must be positive"
                )
            raw = (
                Decimal(inputs[0]["value"]) / denominator - Decimal("1")
            ) * Decimal("100")
        else:  # validator makes this unreachable; retain explicit fail-closed branch.
            raise ResearchVerificationError("unsupported computation operator")
        quantum = Decimal(1).scaleb(-spec_wire["rounding"]["digits"])
        expected_value = format(raw.quantize(quantum, rounding=_ROUNDING[spec_wire["rounding"]["mode"]]), "f")
        if "." in expected_value:
            expected_value = expected_value.rstrip("0").rstrip(".")
        if expected_value in {"", "-0"}:
            expected_value = "0"
        ok = expected_value == spec_wire["output_value"]
        findings.append(_finding_wire("computed_value", "info" if ok else "error", "pass" if ok else "fail", "output_value", expected_value, spec_wire["output_value"], "deterministic Decimal result matches" if ok else "deterministic Decimal result differs"))
        if operator == "ratio":
            expected_metadata = {
                "unit": "ratio",
                "currency": None,
                "scale": "one",
                "period": inputs[0]["period"],
            }
        elif operator == "growth_percentage":
            expected_metadata = {
                "unit": "percent",
                "currency": None,
                "scale": "one",
                "period": inputs[0]["period"],
            }
        else:
            expected_metadata = {
                "unit": inputs[0]["unit"],
                "currency": inputs[0]["currency"],
                "scale": inputs[0]["scale"],
                "period": inputs[0]["period"],
            }
        observed_metadata = {
            "unit": spec_wire["output_unit"],
            "currency": spec_wire["output_currency"],
            "scale": spec_wire["output_scale"],
            "period": spec_wire["output_period"],
        }
        metadata_ok = observed_metadata == expected_metadata
        findings.append(
            _finding_wire(
                "output_metadata",
                "info" if metadata_ok else "error",
                "pass" if metadata_ok else "fail",
                "output_metadata",
                canonical_json(expected_metadata),
                canonical_json(observed_metadata),
                "output metadata matches the deterministic rule"
                if metadata_ok
                else "output unit/currency/scale/period drifted",
            )
        )
    except (ResearchVerificationError, InvalidOperation, ZeroDivisionError) as exc:
        findings.append(_finding_wire("numeric_rule", "error", "fail", "operator", operator, None, str(exc)))
    verdict = "pass" if not any(item["severity"] == "error" and item["status"] == "fail" for item in findings) else "reject"
    base = {
        "schema_version": SCHEMA_VERSION,
        "id": "verification-bundle:numeric:" + content_hash({"subject": spec_wire["id"], "checkpoint": checkpoint_hash, "findings": [item["content_hash"] for item in findings]}),
        "created_at": spec_wire["created_at"], "kind": "numeric", "subject_ref": spec_wire["id"], "subject_hash": spec_wire["content_hash"],
        "verdict": verdict, "checkpoint_ref": checkpoint_ref, "checkpoint_hash": checkpoint_hash, "findings": findings,
        "verifier_ref": _NUMERIC_VERIFIER_REF, "verifier_hash": _NUMERIC_VERIFIER_HASH,
    }
    base["content_hash"] = content_hash(base)
    return validate_verification_bundle(base)


def build_candidate_evidence(
    material: Mapping[str, Any],
    source_verification: Mapping[str, Any],
    *,
    candidate_evidence_ref: str,
    actor_ref: str,
    created_at: str,
    verification_mode: str = "recorded_fixture",
) -> dict[str, Any]:
    """Build a candidate-only evidence record; never a Ledger EvidenceVersion."""
    material_wire = validate_source_verification_material(material)
    verification = validate_verification_bundle(source_verification)
    if (
        verification["kind"] != "source"
        or verification["verdict"] != "pass"
        or verification["subject_ref"] != material_wire["id"]
        or verification["subject_hash"] != material_wire["content_hash"]
    ):
        raise VerificationRejected("source material has no exact passing verification")
    verification_mode = _text(verification_mode, "verification_mode")
    if verification_mode == "recorded_fixture":
        expected_source_type = "recorded_fixture"
        if material_wire["schema_version"] != "0.1":
            raise VerificationRejected("recorded_fixture evidence requires fixture material")
    elif verification_mode == "connector_authority":
        if material_wire["schema_version"] != "0.2" or material_wire.get("provenance_mode") != "connector_authority":
            raise VerificationRejected("connector_authority evidence requires authority material")
        if (
            material_wire.get("source_type") == "public_web"
            and material_wire.get("operation") != "fetch_get"
        ):
            raise VerificationRejected(
                "public web discovery/search/HEAD material cannot become evidence; "
                "fetch_get the original source first"
            )
        # This value came from the validated connector profile when the
        # material was built; it is not accepted as a caller label.
        expected_source_type = material_wire["source_type"]
    elif verification_mode == MISSION_FIGURE_AUTHORITY_MODE:
        if (
            material_wire["schema_version"] != "0.2"
            or material_wire.get("provenance_mode") != MISSION_FIGURE_AUTHORITY_MODE
        ):
            raise VerificationRejected(
                f"{MISSION_FIGURE_AUTHORITY_MODE} evidence requires mission figure material"
            )
        if (verification["verifier_ref"], verification["verifier_hash"]) != (
            MISSION_FIGURE_SOURCE_VERIFIER_REF, MISSION_FIGURE_SOURCE_VERIFIER_HASH
        ):
            raise VerificationRejected(
                f"{MISSION_FIGURE_AUTHORITY_MODE} evidence requires its own Core source verifier"
            )
        # Read from the Core chain by the resolver, never a caller label: a
        # figure taken from a filed document is official_filing evidence.
        expected_source_type = material_wire["source_type"]
    elif verification_mode in CITED_CORE_AUTHORITY_MODES:
        expected_verifier = {
            TRANSCRIPT_CORE_AUTHORITY_MODE:
                (TRANSCRIPT_SOURCE_VERIFIER_REF, TRANSCRIPT_SOURCE_VERIFIER_HASH),
            PUBLIC_WEB_CORE_AUTHORITY_MODE:
                (PUBLIC_WEB_SOURCE_VERIFIER_REF, PUBLIC_WEB_SOURCE_VERIFIER_HASH),
            REGISTERED_ANNUAL_REPORT_AUTHORITY_MODE:
                (REGISTERED_ANNUAL_REPORT_SOURCE_VERIFIER_REF,
                 REGISTERED_ANNUAL_REPORT_SOURCE_VERIFIER_HASH),
        }[verification_mode]
        if (
            material_wire["schema_version"] != "0.2"
            or material_wire.get("provenance_mode") != verification_mode
        ):
            raise VerificationRejected(
                f"{verification_mode} evidence requires matching Core authority material"
            )
        if (verification["verifier_ref"], verification["verifier_hash"]) != expected_verifier:
            raise VerificationRejected(
                f"{verification_mode} evidence requires its own Core source verifier"
            )
        # The profile source type is read from Core by the resolver; the
        # citation binder later relabels the evidence (authenticated
        # transcript, or public web page).
        expected_source_type = material_wire["source_type"]
    else:
        raise VerificationRejected("verification_mode is not a closed value")
    base = {
        "schema_version": SCHEMA_VERSION,
        "id": "candidate-evidence-version:" + content_hash(
            {"candidate_evidence_ref": candidate_evidence_ref, "version": 1}
        ),
        "created_at": created_at,
        "candidate_evidence_ref": candidate_evidence_ref,
        "version": 1,
        "source_type": expected_source_type,
        "source_ref": material_wire["source_ref"],
        "source_envelope_ref": material_wire["source_envelope_ref"],
        "source_envelope_hash": material_wire["source_envelope_hash"],
        "artifact_refs": [
            {"ref": material_wire["artifact_ref"], "hash": material_wire["artifact_hash"]}
        ],
        "retrieved_at": material_wire["retrieved_at"],
        "valid_until": None,
        "source_lineage": list(material_wire["source_lineage"]),
        "independence_group": "independence:" + material_wire["source_ref"],
        "source_verification_ref": verification["id"],
        "source_verification_hash": verification["content_hash"],
        "actor_ref": actor_ref,
        "prior_version_ref": None,
    }
    base["content_hash"] = content_hash(base)
    return validate_candidate_evidence(base)


def build_candidate_claim(
    evidence: Mapping[str, Any],
    source_verification: Mapping[str, Any],
    numeric_spec: Mapping[str, Any],
    numeric_verification: Mapping[str, Any],
    *,
    candidate_claim_ref: str,
    subject_ref: str,
    metric_or_aspect: str,
    basis: str,
    normalized_statement: str,
    actor_ref: str,
    created_at: str,
) -> dict[str, Any]:
    """Build a quantitatively verified candidate, not a formal ClaimVersion."""
    evidence_wire = validate_candidate_evidence(evidence)
    source_wire = validate_verification_bundle(source_verification)
    spec_wire = validate_numeric_verification_spec(numeric_spec)
    numeric_wire = validate_verification_bundle(numeric_verification)
    if source_wire["verdict"] != "pass" or source_wire["kind"] != "source":
        raise VerificationRejected("candidate claim requires passing source verification")
    if (
        numeric_wire["verdict"] != "pass"
        or numeric_wire["kind"] != "numeric"
        or numeric_wire["subject_ref"] != spec_wire["id"]
        or numeric_wire["subject_hash"] != spec_wire["content_hash"]
    ):
        raise VerificationRejected("candidate claim requires exact passing numeric verification")
    base = {
        "schema_version": SCHEMA_VERSION,
        "id": "candidate-claim-version:" + content_hash(
            {"candidate_claim_ref": candidate_claim_ref, "version": 1}
        ),
        "created_at": created_at,
        "candidate_claim_ref": candidate_claim_ref,
        "version": 1,
        "subject_ref": subject_ref,
        "metric_or_aspect": metric_or_aspect,
        "period": spec_wire["output_period"],
        "basis": basis,
        "normalized_statement": normalized_statement,
        "semantic_verification_status": "unverified",
        "claim_kind": "quantitative",
        "value": spec_wire["output_value"],
        "unit": spec_wire["output_unit"],
        "currency": spec_wire["output_currency"],
        "scale": spec_wire["output_scale"],
        "candidate_evidence_refs": [
            {"ref": evidence_wire["id"], "hash": evidence_wire["content_hash"]}
        ],
        "source_verification_ref": source_wire["id"],
        "source_verification_hash": source_wire["content_hash"],
        "numeric_spec_ref": spec_wire["id"],
        "numeric_spec_hash": spec_wire["content_hash"],
        "numeric_verification_ref": numeric_wire["id"],
        "numeric_verification_hash": numeric_wire["content_hash"],
        "actor_ref": actor_ref,
        "prior_version_ref": None,
    }
    base["content_hash"] = content_hash(base)
    return validate_candidate_claim(base)


class InjectedStagingCrash(RuntimeError):
    pass


class CandidateStagingStore:
    """Owner-only candidate scratch authority with no Research Ledger handle."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        fault_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            target = Path(self.path)
            target.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(_STAGING_SCHEMA_PATH.read_text(encoding="utf-8"))
        if self.path != ":memory:":
            os.chmod(self.path, 0o600)
        self._fault_hook = fault_hook

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _require_clean_pass(bundle: Mapping[str, Any], kind: str) -> dict[str, Any]:
        wire = validate_verification_bundle(bundle)
        if wire["kind"] != kind or wire["verdict"] != "pass":
            raise VerificationRejected(f"{kind} verification did not pass")
        if any(item["status"] == "fail" for item in wire["findings"]):
            raise VerificationRejected(f"{kind} verification still contains a failed finding")
        return wire

    @staticmethod
    def _insert_immutable(
        cur: sqlite3.Cursor,
        table: str,
        id_column: str,
        identifier: str,
        wire: Mapping[str, Any],
    ) -> None:
        encoded = canonical_json(wire)
        existing = cur.execute(
            f"SELECT record_json,content_hash FROM {table} WHERE {id_column}=?",
            (identifier,),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] != wire["content_hash"] or existing["record_json"] != encoded:
                raise ResearchVerificationConflict(f"{table} identity conflict")
            return
        cur.execute(
            f"INSERT INTO {table}({id_column},record_json,content_hash,created_at) VALUES(?,?,?,?)",
            (identifier, encoded, wire["content_hash"], wire["created_at"]),
        )

    @staticmethod
    def _check_candidate_chain(
        cur: sqlite3.Cursor,
        *,
        table: str,
        stable_column: str,
        stable_ref: str,
        version: int,
        prior_ref: str | None,
    ) -> None:
        previous = cur.execute(
            f"SELECT version_id,version_number FROM {table} WHERE {stable_column}=? "
            "ORDER BY version_number DESC LIMIT 1",
            (stable_ref,),
        ).fetchone()
        expected_version = 1 if previous is None else int(previous["version_number"]) + 1
        expected_prior = None if previous is None else previous["version_id"]
        if version != expected_version or prior_ref != expected_prior:
            raise ResearchVerificationConflict("candidate version chain mismatch")

    def stage(
        self,
        *,
        material: Mapping[str, Any],
        source_verification: Mapping[str, Any],
        evidence: Mapping[str, Any],
        claim: Mapping[str, Any],
        idempotency_key: str,
        checkpoint: Mapping[str, Any] | None = None,
        plan: Mapping[str, Any] | None = None,
        context_pack: Mapping[str, Any] | None = None,
        step: Mapping[str, Any] | None = None,
        runner_request: Mapping[str, Any] | None = None,
        receipt: Mapping[str, Any] | None = None,
        numeric_spec: Mapping[str, Any] | None = None,
        numeric_verification: Mapping[str, Any] | None = None,
        verification_mode: str = "recorded_fixture",
        authority_resolver: Any | None = None,
        figure_admission_policy: str = FIGURE_ADMISSION_DEFAULT,
        verified_figure: Mapping[str, Any] | None = None,
        figure_resolver: Any | None = None,
    ) -> dict[str, Any]:
        """Stage one verified candidate pair.

        ``quantitative`` candidates keep the 0.1 contract: numeric spec and
        numeric verification are mandatory and are recomputed here.
        ``qualitative`` candidates (ADR-0003 option B) carry no numeric
        authority at all; they are admitted only as ``authenticated_transcript``
        evidence with an exact citation binding, and never through a policy
        path.  ``transcript_core_authority`` is the closed verification mode
        for transcript candidates whose raw ArtifactVersion is Core-held
        AlphaEngine authority without a ResearchCheckpoint.
        """
        # Checked before anything is validated or read: which rule this Core
        # runs is not a property of the candidate, and a caller asking for a
        # rule that does not exist should be told so rather than told its
        # material is malformed.
        if figure_admission_policy not in FIGURE_ADMISSION_POLICIES:
            raise VerificationRejected("figure_admission_policy is not a closed value")
        if verified_figure is not None and figure_admission_policy != FIGURE_ADMISSION_VERIFIED_FIGURE:
            raise VerificationRejected(
                "a verified figure was supplied but this Core still runs the "
                f"{FIGURE_ADMISSION_REJECT} rule; ADR-0007 has to be enabled "
                "deliberately"
            )
        if verified_figure is not None and verification_mode != MISSION_FIGURE_AUTHORITY_MODE:
            # ADR-0007 leaves the cited-original refusal exactly where ADR-0003
            # put it. A figure travels its own mode or it does not travel.
            raise VerificationRejected(
                "a verified figure is admitted only through "
                f"{MISSION_FIGURE_AUTHORITY_MODE}; the cited-original modes "
                "stay qualitative"
            )
        material_wire = validate_source_verification_material(material)
        source_wire = self._require_clean_pass(source_verification, "source")
        evidence_wire = validate_candidate_evidence(evidence)
        claim_wire = validate_candidate_claim(claim)
        key = _text(idempotency_key, "idempotency_key")
        verification_mode = _text(verification_mode, "verification_mode")
        qualitative = claim_wire["claim_kind"] == "qualitative"
        # ADR-0005 / P9d-17c: a fetched public-web page cited through the
        # same correction authority is cited evidence too.
        transcript_evidence = evidence_wire["source_type"] in (TRANSCRIPT_EVIDENCE_SOURCE_TYPE, "public_web")
        annual_report_evidence = (
            verification_mode == REGISTERED_ANNUAL_REPORT_AUTHORITY_MODE
            and evidence_wire["source_type"] == "official_filing"
        )

        spec_wire: dict[str, Any] | None
        numeric_wire: dict[str, Any] | None
        figure_wire: dict[str, Any] | None = None
        if qualitative:
            if numeric_spec is not None or numeric_verification is not None:
                raise VerificationRejected(
                    "qualitative candidate cannot carry numeric spec or numeric verification"
                )
            if not transcript_evidence and not annual_report_evidence:
                raise VerificationRejected(
                    "qualitative candidate requires authenticated transcript evidence "
                    "or a fetched public-web/registered annual-report original"
                )
            spec_wire = None
            numeric_wire = None
        elif verified_figure is not None:
            # ADR-0007.  The figure row is the numeric authority, so there is
            # no JSON-pointer spec to recompute and asking for one would be
            # asking for the text extractor ADR-0003 refused to build.
            if numeric_spec is not None or numeric_verification is not None:
                raise VerificationRejected(
                    "a figure-backed quantitative candidate carries no numeric "
                    "spec; the figure row is its numeric authority"
                )
            if figure_resolver is None or not callable(
                getattr(figure_resolver, "verify_figure", None)
            ):
                raise VerificationRejected(
                    "figure admission requires a Core figure resolver"
                )
            spec_wire = None
            numeric_wire = None
        else:
            if numeric_spec is None or numeric_verification is None:
                raise VerificationRejected(
                    "quantitative candidate requires numeric spec and numeric verification"
                )
            spec_wire = validate_numeric_verification_spec(numeric_spec)
            numeric_wire = self._require_clean_pass(numeric_verification, "numeric")

        if verification_mode == MISSION_FIGURE_AUTHORITY_MODE:
            # ADR-0007: the material *is* the figure row, so there is no
            # checkpoint, no plan and no runner request; the resolver re-derives
            # the whole chain from Core and this store demands byte equality
            # with what the caller brought, exactly as the cited modes do.
            if qualitative:
                raise VerificationRejected(
                    f"{MISSION_FIGURE_AUTHORITY_MODE} staging is for a verified "
                    "number; a semantic candidate has no figure to rest on"
                )
            if verified_figure is None:
                raise VerificationRejected(
                    f"{MISSION_FIGURE_AUTHORITY_MODE} staging requires the figure row"
                )
            if figure_resolver is None or not callable(
                getattr(figure_resolver, "verify_source_material", None)
            ):
                raise VerificationRejected(
                    f"{MISSION_FIGURE_AUTHORITY_MODE} staging requires a Core figure resolver"
                )
            if material_wire.get("provenance_mode") != MISSION_FIGURE_AUTHORITY_MODE:
                raise VerificationRejected(
                    f"{MISSION_FIGURE_AUTHORITY_MODE} staging requires matching "
                    "mission figure material"
                )
            recomputed_source = figure_resolver.verify_source_material(material_wire)
        elif verification_mode in CITED_CORE_AUTHORITY_MODES:
            if not qualitative:
                raise VerificationRejected(
                    f"{verification_mode} staging admits qualitative candidates only; "
                    "a cited original is not a numeric authority"
                )
            if authority_resolver is None or not callable(
                getattr(authority_resolver, "verify_source_material", None)
            ):
                raise VerificationRejected(
                    f"{verification_mode} staging requires a Core authority resolver"
                )
            if material_wire.get("provenance_mode") != verification_mode:
                raise VerificationRejected(
                    f"{verification_mode} staging requires matching Core authority material"
                )
            recomputed_source = authority_resolver.verify_source_material(material_wire)
        else:
            bindings = (checkpoint, plan, context_pack, step, runner_request, receipt)
            if any(item is None for item in bindings):
                raise VerificationRejected(
                    f"{verification_mode} staging requires checkpoint, plan, context pack, "
                    "step, runner request and receipt"
                )
            if verification_mode == "connector_authority":
                if authority_resolver is None:
                    raise VerificationRejected("connector_authority staging requires an authority resolver")
                if material_wire.get("provenance_mode") != "connector_authority":
                    raise VerificationRejected(
                        "connector_authority staging requires connector authority material"
                    )
                recomputed_source = verify_authority_source_material(
                    material_wire, resolver=authority_resolver, checkpoint=checkpoint,
                    plan=plan, context_pack=context_pack, step=step,
                    runner_request=runner_request, receipt=receipt,
                )
            elif verification_mode == "recorded_fixture":
                if material_wire["schema_version"] != "0.1":
                    raise VerificationRejected("recorded_fixture staging requires fixture material")
                recomputed_source = verify_source_material(
                    material_wire, checkpoint=checkpoint, plan=plan,
                    context_pack=context_pack, step=step,
                    runner_request=runner_request, receipt=receipt,
                )
            else:
                raise VerificationRejected("verification_mode is not a closed value")
        if canonical_json(recomputed_source) != canonical_json(source_wire):
            raise ResearchVerificationConflict(
                "source verification was not produced by the deterministic verifier"
            )
        if verified_figure is not None:
            # Re-verified here rather than trusted: the resolver reads the row
            # back out of Core by id, recomputes its content hash from its own
            # columns, refuses a retracted one, and re-runs the digits-and-label
            # check against the quote the row stores.  Only then is the caller's
            # copy compared, byte for byte.
            figure_wire, figure_bundle = figure_resolver.verify_figure(verified_figure)
            if canonical_json(figure_wire) != canonical_json(dict(verified_figure)):
                raise ResearchVerificationConflict(
                    "the supplied figure is not the figure Core holds"
                )
            if figure_wire.get("source_grade") not in FIGURE_ADMISSIBLE_GRADES:
                # ADR-0007 §Decision 4. Enforced here rather than in the
                # promoter, so that no caller can route a spoken figure in.
                raise VerificationRejected(
                    "only a company-filed figure may become a quantitative "
                    "Claim; an earnings-call figure stays qualitative"
                )
            numeric_wire = self._require_clean_pass(figure_bundle, "numeric")
            if (
                numeric_wire["subject_ref"] != figure_wire["figure_id"]
                or numeric_wire["subject_hash"] != figure_wire["content_hash"]
            ):
                raise ResearchVerificationConflict(
                    "figure numeric verification binds another figure"
                )
        if spec_wire is not None and numeric_wire is not None:
            recomputed_numeric = verify_numeric_spec(
                spec_wire, checkpoint_ref=recomputed_source["checkpoint_ref"],
                checkpoint_hash=recomputed_source["checkpoint_hash"],
                source_material=material_wire, source_bundle=recomputed_source,
            )
            if canonical_json(recomputed_numeric) != canonical_json(numeric_wire):
                raise ResearchVerificationConflict(
                    "numeric verification was not produced by the deterministic verifier"
                )

        if (source_wire["subject_ref"], source_wire["subject_hash"]) != (
            material_wire["id"], material_wire["content_hash"]
        ):
            raise ResearchVerificationConflict("source verification binds another material")
        if spec_wire is not None and numeric_wire is not None:
            if (numeric_wire["subject_ref"], numeric_wire["subject_hash"]) != (
                spec_wire["id"], spec_wire["content_hash"]
            ):
                raise ResearchVerificationConflict("numeric verification binds another spec")
            if (
                source_wire["checkpoint_ref"] != numeric_wire["checkpoint_ref"]
                or source_wire["checkpoint_hash"] != numeric_wire["checkpoint_hash"]
            ):
                raise ResearchVerificationConflict("source and numeric verification bind different checkpoints")
        expected_artifacts = [{
            "ref": material_wire["artifact_ref"],
            "hash": material_wire["artifact_hash"],
        }]
        if transcript_evidence:
            transcript_shape = (
                verification_mode in {"connector_authority", TRANSCRIPT_CORE_AUTHORITY_MODE, PUBLIC_WEB_CORE_AUTHORITY_MODE}
                and material_wire["source_type"] != "recorded_fixture"
                and len(evidence_wire["artifact_refs"]) == 2
                and evidence_wire["artifact_refs"][:1] == expected_artifacts
                and evidence_wire["artifact_refs"][1]["ref"].startswith(
                    "transcript-claim-citation-binding:"
                )
                and evidence_wire["source_lineage"]
                == [
                    *material_wire["source_lineage"],
                    evidence_wire["artifact_refs"][1]["ref"],
                ]
            )
        else:
            transcript_shape = (
                evidence_wire["source_type"] == (
                    "recorded_fixture"
                    if verification_mode == "recorded_fixture"
                    else material_wire["source_type"]
                )
                and evidence_wire["artifact_refs"] == expected_artifacts
                and evidence_wire["source_lineage"]
                == material_wire["source_lineage"]
            )
        evidence_checks = (
            transcript_shape,
            evidence_wire["source_ref"] == material_wire["source_ref"],
            evidence_wire["source_envelope_ref"] == material_wire["source_envelope_ref"],
            evidence_wire["source_envelope_hash"] == material_wire["source_envelope_hash"],
            evidence_wire["retrieved_at"] == material_wire["retrieved_at"],
            evidence_wire["valid_until"] is None,
            evidence_wire["independence_group"]
            == "independence:" + material_wire["source_ref"],
            evidence_wire["source_verification_ref"] == source_wire["id"],
            evidence_wire["source_verification_hash"] == source_wire["content_hash"],
        )
        if not all(evidence_checks):
            raise ResearchVerificationConflict("candidate evidence drifted from verified source material")
        expected_evidence = [{"ref": evidence_wire["id"], "hash": evidence_wire["content_hash"]}]
        claim_checks = [
            claim_wire["candidate_evidence_refs"] == expected_evidence,
            claim_wire["source_verification_ref"] == source_wire["id"],
            claim_wire["source_verification_hash"] == source_wire["content_hash"],
        ]
        if figure_wire is not None and numeric_wire is not None:
            # ADR-0007: the figure row stands where the NumericVerificationSpec
            # stands for a connector candidate, so the claim's numeric refs
            # point at it and every numeric field has to equal the row's.  The
            # value is compared as a number rather than as text: "5.60" and
            # "5.6" are the same figure and only one of them is canonical.
            expected_numerics = figure_candidate_numerics(figure_wire)
            claim_checks.extend((
                claim_wire["numeric_spec_ref"] == figure_wire["figure_id"],
                claim_wire["numeric_spec_hash"] == figure_wire["content_hash"],
                claim_wire["numeric_verification_ref"] == numeric_wire["id"],
                claim_wire["numeric_verification_hash"] == numeric_wire["content_hash"],
                Decimal(claim_wire["value"]) == Decimal(expected_numerics["value"]),
                claim_wire["unit"] == expected_numerics["unit"],
                claim_wire["currency"] == expected_numerics["currency"],
                claim_wire["scale"] == expected_numerics["scale"],
                canonical_json(claim_wire["period"])
                == canonical_json(expected_numerics["period"]),
                claim_wire["subject_ref"] == figure_wire["company_ref"],
            ))
        elif spec_wire is not None and numeric_wire is not None:
            claim_checks.extend((
                claim_wire["numeric_spec_ref"] == spec_wire["id"],
                claim_wire["numeric_spec_hash"] == spec_wire["content_hash"],
                claim_wire["numeric_verification_ref"] == numeric_wire["id"],
                claim_wire["numeric_verification_hash"] == numeric_wire["content_hash"],
                claim_wire["value"] == spec_wire["output_value"],
                claim_wire["unit"] == spec_wire["output_unit"],
                claim_wire["currency"] == spec_wire["output_currency"],
                claim_wire["scale"] == spec_wire["output_scale"],
                canonical_json(claim_wire["period"]) == canonical_json(spec_wire["output_period"]),
            ))
        else:
            claim_checks.extend(
                claim_wire[field] is None
                for field in (
                    "value", "unit", "currency", "scale", "numeric_spec_ref",
                    "numeric_spec_hash", "numeric_verification_ref",
                    "numeric_verification_hash",
                )
            )
        if not all(claim_checks):
            raise ResearchVerificationConflict("candidate claim drifted from verified inputs")

        request_identity = {
            "material_hash": material_wire["content_hash"],
            "numeric_spec_hash": None if spec_wire is None else spec_wire["content_hash"],
            "source_verification_hash": source_wire["content_hash"],
            "numeric_verification_hash": None if numeric_wire is None else numeric_wire["content_hash"],
            "evidence_hash": evidence_wire["content_hash"],
            "claim_hash": claim_wire["content_hash"],
        }
        if figure_wire is not None:
            # Added only on the figure path.  Every idempotency key already
            # written was written under the six-key definition, and a request
            # hash that changed shape for existing rows would turn every one of
            # them into a permanent conflict.
            request_identity["figure_hash"] = figure_wire["content_hash"]
        request_hash = content_hash(request_identity)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            prior = self.connection.execute(
                "SELECT request_hash,result_json FROM candidate_stage_requests WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != request_hash:
                    raise ResearchVerificationConflict("candidate staging idempotency conflict")
                result = json.loads(prior["result_json"])
                self.connection.execute("COMMIT")
                return {**result, "write_status": "duplicate"}
            self._check_candidate_chain(
                self.connection, table="candidate_evidence_versions",
                stable_column="candidate_evidence_ref",
                stable_ref=evidence_wire["candidate_evidence_ref"],
                version=evidence_wire["version"], prior_ref=evidence_wire["prior_version_ref"],
            )
            self._check_candidate_chain(
                self.connection, table="candidate_claim_versions",
                stable_column="candidate_claim_ref",
                stable_ref=claim_wire["candidate_claim_ref"],
                version=claim_wire["version"], prior_ref=claim_wire["prior_version_ref"],
            )
            self._insert_immutable(self.connection, "candidate_source_materials", "material_id", material_wire["id"], material_wire)
            if spec_wire is not None:
                self._insert_immutable(self.connection, "candidate_numeric_specs", "numeric_spec_id", spec_wire["id"], spec_wire)
            if figure_wire is not None:
                self._insert_immutable(
                    self.connection, "candidate_figures", "figure_id",
                    figure_wire["figure_id"], figure_wire,
                )
            self._insert_immutable(self.connection, "candidate_verifications", "verification_id", source_wire["id"], source_wire)
            if numeric_wire is not None:
                self._insert_immutable(self.connection, "candidate_verifications", "verification_id", numeric_wire["id"], numeric_wire)
            self.connection.execute(
                "INSERT INTO candidate_evidence_versions(version_id,candidate_evidence_ref,version_number,prior_version_id,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                (evidence_wire["id"], evidence_wire["candidate_evidence_ref"], evidence_wire["version"], evidence_wire["prior_version_ref"], canonical_json(evidence_wire), evidence_wire["content_hash"], evidence_wire["created_at"]),
            )
            self.connection.execute(
                "INSERT INTO candidate_claim_versions(version_id,candidate_claim_ref,version_number,prior_version_id,evidence_version_id,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (claim_wire["id"], claim_wire["candidate_claim_ref"], claim_wire["version"], claim_wire["prior_version_ref"], evidence_wire["id"], canonical_json(claim_wire), claim_wire["content_hash"], claim_wire["created_at"]),
            )
            result = {
                "write_status": "fresh",
                "candidate_evidence_ref": evidence_wire["id"],
                "candidate_evidence_hash": evidence_wire["content_hash"],
                "candidate_claim_ref": claim_wire["id"],
                "candidate_claim_hash": claim_wire["content_hash"],
                "source_verification_ref": source_wire["id"],
                "numeric_verification_ref": None if numeric_wire is None else numeric_wire["id"],
            }
            self.connection.execute(
                "INSERT INTO candidate_stage_requests(idempotency_key,request_hash,result_json,created_at) VALUES(?,?,?,?)",
                (key, request_hash, canonical_json(result), claim_wire["created_at"]),
            )
            if self._fault_hook is not None:
                self._fault_hook("before_commit", result)
            self.connection.execute("COMMIT")
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        if self._fault_hook is not None:
            self._fault_hook("after_commit", result)
        return result


    def counts(self) -> dict[str, int]:
        return {
            table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "candidate_source_materials", "candidate_numeric_specs",
                "candidate_figures", "candidate_verifications",
                "candidate_evidence_versions", "candidate_claim_versions",
                "candidate_stage_requests",
            )
        }


__all__ = [
    "ResearchVerificationError", "ResearchVerificationConflict", "VerificationRejected",
    "InjectedStagingCrash", "CandidateStagingStore",
    "FIGURE_ADMISSION_DEFAULT", "FIGURE_ADMISSION_POLICIES",
    "FIGURE_ADMISSION_REJECT", "FIGURE_ADMISSION_VERIFIED_FIGURE",
    "FIGURE_ADMISSIBLE_GRADES",
    "FIGURE_NUMERIC_VERIFIER_HASH", "FIGURE_NUMERIC_VERIFIER_REF",
    "MISSION_FIGURE_AUTHORITY_MODE", "MISSION_FIGURE_SOURCE_VERIFIER_HASH",
    "MISSION_FIGURE_SOURCE_VERIFIER_REF", "MISSION_VERIFIED_FIGURE_RULE_REF",
    "STATEMENT_LINE_VERIFIER_HASH", "STATEMENT_LINE_VERIFIER_REF",
    "figure_candidate_numerics",
    "TRANSCRIPT_CORE_AUTHORITY_MODE", "TRANSCRIPT_SOURCE_VERIFIER_REF",
    "TRANSCRIPT_SOURCE_VERIFIER_HASH",
    "REGISTERED_ANNUAL_REPORT_AUTHORITY_MODE",
    "REGISTERED_ANNUAL_REPORT_SOURCE_VERIFIER_REF",
    "REGISTERED_ANNUAL_REPORT_SOURCE_VERIFIER_HASH",
    "build_source_verification_material", "build_authority_source_material",
    "validate_source_verification_material",
    "validate_numeric_verification_spec", "validate_verification_bundle",
    "validate_candidate_evidence", "validate_candidate_claim", "verify_source_material",
    "verify_authority_source_material", "verify_numeric_spec",
    "build_candidate_evidence", "build_candidate_claim",
]
