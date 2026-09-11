"""Exact Core-registered retrieval for acquired SEC annual-report text.

Issuer authority comes from statement-filing rows; source authority comes from
the completed fetch manifest, connector receipts, spool bytes, deterministic
full rendering and append-only document-read proof.  There is no path-based
registration surface.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from .store import canonical_json, content_hash
from .public_web_extraction_source import verified_public_web_source


OPERATION = "search_registered_annual_report"
PERMISSION_SCOPE = "registered_annual_report_read"
CAPABILITY = "capability:dalton:local:registered-annual-report"
RUNTIME_PROFILE_REF = "runtime:dalton-core:registered-annual-report:0.2"
OUTPUT_CONTRACT_REF = "schema:registered-annual-report-retrieval-proof:0.2"
SOURCE_REF = "source:sec-edgar"
DEFAULT_MAX_QUERY_TERMS = 12
DEFAULT_MAX_RESULTS = 20
DEFAULT_MAX_SOURCE_BYTES = 8 * 1024 * 1024
DEFAULT_CONTEXT_BEFORE_CHARS = 240
DEFAULT_CONTEXT_AFTER_CHARS = 480

_CIK_RE = re.compile(r"[0-9]{1,10}\Z")
_ACCESSION_RE = re.compile(r"([0-9]{10})-([0-9]{2})-([0-9]{6})\Z")
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


class RegisteredAnnualReportError(RuntimeError):
    pass


def source_bytes_hash(value: bytes) -> str:
    """Raw-byte SHA-256 used by filing acquisition/registration authority."""

    return hashlib.sha256(value).hexdigest()


def normalize_request(value: Any) -> dict[str, Any]:
    """Validate and canonicalize the closed qualitative request shape."""

    if not isinstance(value, Mapping):
        raise RegisteredAnnualReportError("annual report request must be an object")
    fields = {
        "mission_version_ref", "company_ref", "review_ref",
        "acquired_document_ref", "acquired_document_hash", "acquisition_ticket_ref",
        "document_read_proof_ref", "document_read_proof_hash",
        "issuer_cik", "accession", "source_manifest_ref", "source_manifest_hash",
        "source_raw_hash", "source_content_hash", "source_renderer",
        "query_terms", "limits", "model_execution",
    }
    if set(value) != fields:
        raise RegisteredAnnualReportError(
            "annual report request has an invalid closed shape"
        )
    cik_value = value.get("issuer_cik")
    if not isinstance(cik_value, str):
        raise RegisteredAnnualReportError("issuer_cik must be text")
    cik = cik_value.strip()
    if _CIK_RE.fullmatch(cik) is None:
        raise RegisteredAnnualReportError("issuer_cik must contain at most ten digits")
    cik = cik.zfill(10)
    accession_value = value.get("accession")
    if not isinstance(accession_value, str):
        raise RegisteredAnnualReportError("accession must be text")
    accession = accession_value.strip()
    if _ACCESSION_RE.fullmatch(accession) is None:
        raise RegisteredAnnualReportError("accession must use SEC accession format")
    source_hash_value = value.get("source_content_hash")
    if not isinstance(source_hash_value, str):
        raise RegisteredAnnualReportError("source_content_hash must be text")
    source_hash = source_hash_value.strip()
    if _HASH_RE.fullmatch(source_hash) is None:
        raise RegisteredAnnualReportError(
            "source_content_hash must be lowercase SHA-256"
        )
    raw_limits = value.get("limits")
    limit_fields = {
        "max_query_terms", "max_results", "max_source_bytes",
        "context_before_chars", "context_after_chars",
    }
    if not isinstance(raw_limits, Mapping) or set(raw_limits) != limit_fields:
        raise RegisteredAnnualReportError("annual report limits have an invalid closed shape")
    limits: dict[str, int] = {}
    for name in limit_fields:
        item = raw_limits.get(name)
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise RegisteredAnnualReportError(
                f"limits.{name} must be a positive integer"
            )
        limits[name] = item
    raw_terms = value.get("query_terms")
    if not isinstance(raw_terms, (list, tuple)) or not 1 <= len(raw_terms) <= limits["max_query_terms"]:
        raise RegisteredAnnualReportError("query_terms exceed the plan's max_query_terms")
    terms: list[str] = []
    for raw in raw_terms:
        if not isinstance(raw, str):
            raise RegisteredAnnualReportError("query_terms must contain text")
        term = " ".join(raw.casefold().split())
        if not 2 <= len(term) <= 160:
            raise RegisteredAnnualReportError(
                "each query term must contain 2..160 normalized characters"
            )
        if term in terms:
            raise RegisteredAnnualReportError("query_terms must be unique")
        terms.append(term)
    raw_model = value.get("model_execution")
    if not isinstance(raw_model, Mapping) or set(raw_model) != {"draft", "verifier"}:
        raise RegisteredAnnualReportError("model_execution must contain draft and verifier")
    model_execution: dict[str, dict[str, Any]] = {}
    model_fields = {
        "routing_policy_ref", "credential_slot_refs", "max_input_tokens",
        "max_output_tokens", "max_cost_usd", "max_seconds", "max_attempts",
        "max_elapsed_seconds", "provider_retry",
    }
    for stage in ("draft", "verifier"):
        config = raw_model.get(stage)
        if not isinstance(config, Mapping) or set(config) != model_fields:
            raise RegisteredAnnualReportError(f"model_execution.{stage} has an invalid closed shape")
        slots = config.get("credential_slot_refs")
        if (not isinstance(slots, (list, tuple)) or not slots
                or any(
                    not isinstance(item, str)
                    or re.fullmatch(r"credential-slot:[A-Za-z0-9][A-Za-z0-9._:/-]*", item) is None
                    for item in slots
                )
                or len(set(slots)) != len(slots)):
            raise RegisteredAnnualReportError(
                f"model_execution.{stage}.credential_slot_refs must be unique logical slots"
            )
        integer_budgets = (
            "max_input_tokens", "max_output_tokens", "max_seconds",
            "max_elapsed_seconds", "max_attempts",
        )
        parsed = {name: config.get(name) for name in integer_budgets}
        if any(isinstance(item, bool) or not isinstance(item, int) or item < 1
               for item in parsed.values()):
            raise RegisteredAnnualReportError(
                f"model_execution.{stage} integer budget is invalid"
            )
        cost = config.get("max_cost_usd")
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or not math.isfinite(float(cost))
            or not 0 < float(cost)
        ):
            raise RegisteredAnnualReportError(f"model_execution.{stage}.max_cost_usd is invalid")
        provider_retry = None
        if config.get("provider_retry") is not None:
            try:
                from .provider_retry import validate_provider_retry

                provider_retry = validate_provider_retry(config["provider_retry"])
            except Exception as exc:
                raise RegisteredAnnualReportError(
                    f"model_execution.{stage}.provider_retry is invalid"
                ) from exc
        model_execution[stage] = {
            "routing_policy_ref": _nonempty_text(
                config.get("routing_policy_ref"), f"model_execution.{stage}.routing_policy_ref"
            ),
            "credential_slot_refs": list(slots),
            **parsed,
            "max_cost_usd": float(cost),
            "provider_retry": provider_retry,
        }
    proof_ref = _optional_text(value.get("document_read_proof_ref"), "document_read_proof_ref")
    proof_hash = _optional_sha256(
        value.get("document_read_proof_hash"), "document_read_proof_hash"
    )
    if (proof_ref is None) != (proof_hash is None):
        raise RegisteredAnnualReportError(
            "document read proof ref and hash must both be present or null"
        )
    return {
        "mission_version_ref": _nonempty_text(value.get("mission_version_ref"), "mission_version_ref"),
        "company_ref": _nonempty_text(value.get("company_ref"), "company_ref"),
        "review_ref": _nonempty_text(value.get("review_ref"), "review_ref"),
        "acquired_document_ref": _nonempty_text(
            value.get("acquired_document_ref"), "acquired_document_ref"
        ),
        "acquired_document_hash": _sha256(
            value.get("acquired_document_hash"), "acquired_document_hash"
        ),
        "acquisition_ticket_ref": _nonempty_text(
            value.get("acquisition_ticket_ref"), "acquisition_ticket_ref"
        ),
        "document_read_proof_ref": proof_ref,
        "document_read_proof_hash": proof_hash,
        "issuer_cik": cik,
        "accession": accession,
        "source_manifest_ref": _nonempty_text(value.get("source_manifest_ref"), "source_manifest_ref"),
        "source_manifest_hash": _sha256(value.get("source_manifest_hash"), "source_manifest_hash"),
        "source_raw_hash": _sha256(value.get("source_raw_hash"), "source_raw_hash"),
        "source_content_hash": source_hash,
        "source_renderer": _nonempty_text(value.get("source_renderer"), "source_renderer"),
        "query_terms": terms,
        "limits": limits,
        "model_execution": model_execution,
    }


def _nonempty_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegisteredAnnualReportError(f"{name} must be non-empty text")
    return value.strip()


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _nonempty_text(value, name)


def _optional_sha256(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, name)


def _sha256(value: Any, name: str) -> str:
    value = _nonempty_text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise RegisteredAnnualReportError(f"{name} must be lowercase SHA-256")
    return value


def _acquisition_binding(row: sqlite3.Row) -> dict[str, Any]:
    """Stable acquired identity; review workflow state is intentionally absent."""

    return {
        "record_id": row["record_id"], "mission_version_ref": row["mission_version_ref"],
        "company_ref": row["company_ref"], "source_ref": row["source_ref"],
        "document_ref": row["document_ref"], "ticket_ref": row["ticket_ref"],
        "status": row["status"],
    }


def _read_completion_proof(
    connection: sqlite3.Connection, *, proof_ref: str, proof_hash: str
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM document_read_completion_proofs WHERE proof_id=?", (proof_ref,)
    ).fetchone()
    if row is None:
        raise RegisteredAnnualReportError("document read completion proof is unavailable")
    try:
        proof = json.loads(row["record_json"])
    except (TypeError, ValueError) as exc:
        raise RegisteredAnnualReportError(
            "document read completion proof is not canonical JSON"
        ) from exc
    if (
        proof.get("schema_version") != "0.1"
        or not isinstance(proof.get("source_review"), Mapping)
        or content_hash(proof["source_review"]) != proof.get("source_review_hash")
        or proof.get("proof_id") != "document-read-proof:" + content_hash({
            key: item for key, item in proof.items()
            if key not in {"proof_id", "content_hash"}
        })[:32]
        or row["content_hash"] != proof_hash
        or proof.get("content_hash") != proof_hash
        or content_hash({key: item for key, item in proof.items() if key != "content_hash"})
        != proof_hash
        or row["record_json"] != canonical_json(proof)
        or row["proof_id"] != proof.get("proof_id")
        or row["review_id"] != proof.get("review_id")
        or row["source_review_hash"] != proof.get("source_review_hash")
        or row["document_ref"] != proof.get("document_ref")
        or row["company_ref"] != proof.get("company_ref")
    ):
        raise RegisteredAnnualReportError("document read completion proof drifted")
    windows = proof.get("windows")
    if not isinstance(windows, list) or not windows:
        raise RegisteredAnnualReportError("document read completion proof has no windows")
    expected_offset = 0
    source_content_hash = None
    for window in windows:
        if (
            not isinstance(window, Mapping)
            or set(window) != {
                "offset", "context_ref", "context_hash", "next_offset",
                "source_content_hash", "work_order_ref", "result_envelope_ref",
                "result_envelope_hash", "status", "source_review_hash",
            }
            or window.get("status") != "succeeded"
            or window.get("source_review_hash") != proof["source_review_hash"]
            or window.get("offset") != expected_offset
            or _HASH_RE.fullmatch(str(window.get("source_content_hash", ""))) is None
        ):
            raise RegisteredAnnualReportError("document read completion window chain drifted")
        if source_content_hash is None:
            source_content_hash = window["source_content_hash"]
        elif source_content_hash != window["source_content_hash"]:
            raise RegisteredAnnualReportError("document read completion source changed")
        expected_offset = window.get("next_offset")
    if expected_offset is not None:
        raise RegisteredAnnualReportError("document read completion proof is incomplete")
    return {**proof, "verified_source_content_hash": source_content_hash}


def resolve_core_registration(
    connection: sqlite3.Connection, request: Mapping[str, Any]
) -> dict[str, Any]:
    """Resolve issuer proof from Core, never from accession string structure."""

    request = normalize_request(request)
    review = connection.execute(
        "SELECT * FROM coverage_mission_document_reviews WHERE review_id=?",
        (request["review_ref"],),
    ).fetchone()
    if review is None:
        raise RegisteredAnnualReportError("annual report review is not registered in Core")
    document_ref = f"sec:filing:{request['accession']}"
    if any((
        review["mission_version_ref"] != request["mission_version_ref"],
        review["company_ref"] != request["company_ref"],
        review["source_ref"] != SOURCE_REF,
        review["document_ref"] != document_ref,
    )):
        raise RegisteredAnnualReportError(
            "Core review does not bind the exact mission/company/SEC accession"
        )
    discovered = connection.execute(
        "SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?",
        (review["discovered_document_ref"],),
    ).fetchone()
    if discovered is None or discovered["status"] != "acquired" or any(
        discovered[key] != review[key]
        for key in ("mission_version_ref", "company_ref", "source_ref", "document_ref")
    ):
        raise RegisteredAnnualReportError(
            "Core review no longer binds an acquired SEC document"
        )
    acquisition_binding = _acquisition_binding(discovered)
    if (
        discovered["record_id"] != request["acquired_document_ref"]
        or discovered["ticket_ref"] != request["acquisition_ticket_ref"]
        or content_hash(acquisition_binding) != request["acquired_document_hash"]
    ):
        raise RegisteredAnnualReportError("stable acquired-document proof drifted")
    filing = connection.execute(
        "SELECT * FROM coverage_mission_statement_filings WHERE company_ref=? AND accession=?",
        (request["company_ref"], request["accession"]),
    ).fetchone()
    if filing is None or filing["form"] != "10-K":
        raise RegisteredAnnualReportError(
            "Core statement authority lacks the exact issuer 10-K"
        )
    if str(filing["cik"]).zfill(10) != request["issuer_cik"]:
        raise RegisteredAnnualReportError("Core statement issuer CIK differs from the plan")
    if request["document_read_proof_ref"] is not None:
        completion = _read_completion_proof(
            connection,
            proof_ref=request["document_read_proof_ref"],
            proof_hash=request["document_read_proof_hash"],
        )
        if (
            completion["review_id"] != request["review_ref"]
            or completion["company_ref"] != request["company_ref"]
            or completion["document_ref"] != document_ref
            or completion["mission_version_ref"] != request["mission_version_ref"]
            or completion["verified_source_content_hash"]
            != request["source_content_hash"]
        ):
            raise RegisteredAnnualReportError(
                "document read completion proof does not bind the exact annual report"
            )
    body = {
        "schema_version": "0.2", "source_ref": SOURCE_REF,
        "mission_version_ref": request["mission_version_ref"],
        "company_ref": request["company_ref"], "issuer_cik": request["issuer_cik"],
        "accession": request["accession"], "form": "10-K",
        "review_ref": request["review_ref"],
        "acquired_document_ref": request["acquired_document_ref"],
        "acquired_document_hash": request["acquired_document_hash"],
        "acquisition_ticket_ref": request["acquisition_ticket_ref"],
        "document_read_proof_ref": request["document_read_proof_ref"],
        "document_read_proof_hash": request["document_read_proof_hash"],
        "statement_ingest_ref": filing["ingest_id"],
        "statement_filing_hash": filing["content_hash"],
        "source_manifest_ref": request["source_manifest_ref"],
        "source_manifest_hash": request["source_manifest_hash"],
        "source_raw_hash": request["source_raw_hash"],
        "source_content_hash": request["source_content_hash"],
        "source_renderer": request["source_renderer"], "source_truncated": False,
    }
    return {"id": "registered-annual-report:" + content_hash(body)[:32], **body}


def validate_retrieval_proof(
    value: Any, *, expected_request: Mapping[str, Any]
) -> dict[str, Any]:
    """Rebuild the proof envelope's identity and exact filing binding."""

    if not isinstance(value, Mapping):
        raise RegisteredAnnualReportError("retrieval proof must be an object")
    fields = {
        "schema_version", "id", "operation", "permission_scope", "request",
        "registration", "matches", "content_hash",
    }
    if set(value) != fields or value.get("schema_version") != "0.2":
        raise RegisteredAnnualReportError("retrieval proof has an invalid closed shape")
    body = dict(value)
    asserted = body.pop("content_hash", None)
    if asserted != content_hash(body):
        raise RegisteredAnnualReportError("retrieval proof content_hash mismatch")
    request = normalize_request(value.get("request"))
    if canonical_json(request) != canonical_json(normalize_request(expected_request)):
        raise RegisteredAnnualReportError("retrieval proof request drifted from plan")
    registration = value.get("registration")
    registration_fields = {
        "id", "schema_version", "source_ref", "mission_version_ref",
        "company_ref", "issuer_cik", "accession", "form", "review_ref",
        "acquired_document_ref", "acquired_document_hash",
        "acquisition_ticket_ref", "document_read_proof_ref",
        "document_read_proof_hash", "statement_ingest_ref",
        "statement_filing_hash", "source_manifest_ref", "source_manifest_hash",
        "source_raw_hash", "source_content_hash", "source_renderer",
        "source_truncated",
    }
    if not isinstance(registration, Mapping) or set(registration) != registration_fields:
        raise RegisteredAnnualReportError("retrieval proof registration is invalid")
    if (
        registration.get("schema_version") != "0.2"
        or registration.get("source_ref") != SOURCE_REF
        or registration.get("form") != "10-K"
        or registration.get("source_truncated") is not False
    ):
        raise RegisteredAnnualReportError("retrieval proof registration contract drifted")
    registration_body = {key: item for key, item in registration.items() if key != "id"}
    if registration.get("id") != "registered-annual-report:" + content_hash(registration_body)[:32]:
        raise RegisteredAnnualReportError("retrieval proof registration identity drifted")
    for key in (
        "mission_version_ref", "company_ref", "issuer_cik", "accession",
        "review_ref", "acquired_document_ref", "acquired_document_hash",
        "acquisition_ticket_ref", "document_read_proof_ref", "document_read_proof_hash",
        "source_manifest_ref", "source_manifest_hash", "source_raw_hash",
        "source_content_hash", "source_renderer",
    ):
        if registration.get(key) != request[key]:
            raise RegisteredAnnualReportError(f"retrieval proof registration {key} drifted")
    if (
        value.get("operation") != OPERATION
        or value.get("permission_scope") != PERMISSION_SCOPE
    ):
        raise RegisteredAnnualReportError("retrieval proof operation drifted")
    matches = value.get("matches")
    if not isinstance(matches, list) or len(matches) > request["limits"]["max_results"]:
        raise RegisteredAnnualReportError("retrieval proof matches exceed request")
    match_fields = {
        "matched_term", "source_start", "source_end", "excerpt",
        "excerpt_sha256", "source_location",
    }
    for item in matches:
        if not isinstance(item, Mapping) or set(item) != match_fields:
            raise RegisteredAnnualReportError("retrieval match has an invalid shape")
        if item["matched_term"] not in request["query_terms"]:
            raise RegisteredAnnualReportError("retrieval match uses an unrequested term")
        start, end = item["source_start"], item["source_end"]
        if (
            isinstance(start, bool) or not isinstance(start, int) or start < 0
            or isinstance(end, bool) or not isinstance(end, int) or end <= start
        ):
            raise RegisteredAnnualReportError("retrieval match offsets are invalid")
        excerpt = item["excerpt"]
        if (
            not isinstance(excerpt, str)
            or source_bytes_hash(excerpt.encode("utf-8")) != item["excerpt_sha256"]
            or len(excerpt) != end - start
        ):
            raise RegisteredAnnualReportError("retrieval match excerpt hash mismatch")
        expected_location = (
            f"annual-report-context:sec:filing:{request['accession']}:"
            f"{start}:{end}:{item['excerpt_sha256']}"
        )
        if item["source_location"] != expected_location:
            raise RegisteredAnnualReportError("retrieval match location drifted")
    expected_id = "annual-report-retrieval-proof:" + content_hash({
        "request": request,
        "registration_ref": registration["id"],
        "matches": matches,
    })[:32]
    if value.get("id") != expected_id:
        raise RegisteredAnnualReportError("retrieval proof id drifted")
    return dict(value)


class RegisteredAnnualReportRegistry:
    """Read one annual report through Core's acquired-manifest authority.

    The constructor dependencies are the same authorities used by document
    extraction.  No path or caller-supplied source digest can be registered.
    ``bind_request`` discovers every source identity from the acquired Core
    row, append-only completion proof, completed fetch manifest and spool.
    ``search`` repeats that verification before exposing text.
    """

    def __init__(
        self, *, core: Any, spool: Any, manifest_reader: Any, receipt_reader: Any
    ) -> None:
        connection = getattr(core, "connection", None)
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("core must expose an exact sqlite3.Connection")
        if not callable(manifest_reader):
            raise TypeError("manifest_reader must be callable")
        if not callable(getattr(spool, "read_object", None)):
            raise TypeError("spool must expose read_object")
        self.core = core
        self.connection = connection
        self.spool = spool
        self.manifest_reader = manifest_reader
        self.receipt_reader = receipt_reader

    @classmethod
    def from_writer(cls, writer: Any) -> "RegisteredAnnualReportRegistry":
        """Bind the production registry to the existing Writer source lane."""

        from .connector_authority_port import ConnectorCompletionReceiptReader

        launcher = getattr(writer, "web_fetch_launcher", None)
        manifest_reader = getattr(launcher, "read_completed_manifest", None)
        if not callable(manifest_reader):
            raise TypeError("writer must expose the completed web-fetch manifest reader")
        return cls(
            core=writer.store,
            spool=writer._transcript_spool,
            manifest_reader=manifest_reader,
            receipt_reader=ConnectorCompletionReceiptReader(
                connectors=writer._connectors, observability=writer.observability
            ),
        )

    def _verified_text(
        self, request: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        normalized = normalize_request(request)
        registration = resolve_core_registration(self.connection, normalized)
        document_ref = f"sec:filing:{normalized['accession']}"
        try:
            manifest = self.manifest_reader(
                normalized["acquisition_ticket_ref"], document_ref
            )
            manifest, rendering = verified_public_web_source(
                self.core,
                self.spool,
                manifest,
                self.receipt_reader,
                max_source_chars=normalized["limits"]["max_source_bytes"],
            )
        except Exception as exc:  # acquisition authorities expose typed conflicts
            raise RegisteredAnnualReportError(
                f"acquired annual report source verification failed: {exc}"
            ) from exc
        host = urlsplit(manifest["canonical_url"]).hostname
        compact_accession = normalized["accession"].replace("-", "")
        if (
            host is None
            or not (host == "sec.gov" or host.endswith(".sec.gov"))
            or compact_accession not in manifest["canonical_url"]
        ):
            raise RegisteredAnnualReportError(
                "completed manifest is not the exact SEC accession source"
            )
        if manifest["body_bytes"] > normalized["limits"]["max_source_bytes"]:
            raise RegisteredAnnualReportError("annual report exceeds plan byte bound")
        text = rendering["text"]
        actual = {
            "source_manifest_ref": manifest["id"],
            "source_manifest_hash": manifest["content_hash"],
            "source_raw_hash": manifest["body_sha256"],
            "source_content_hash": source_bytes_hash(text.encode("utf-8")),
            "source_renderer": rendering["renderer"],
        }
        for key, value in actual.items():
            if normalized[key] != value:
                raise RegisteredAnnualReportError(f"annual report {key} drifted")
        if rendering["truncated"] is not False:
            raise RegisteredAnnualReportError(
                "annual report source must be rendered in full"
            )
        return registration, manifest, text

    def bind_request(
        self,
        *,
        mission_version_ref: str,
        company_ref: str,
        review_ref: str,
        issuer_cik: str,
        accession: str,
        query_terms: list[str],
        limits: Mapping[str, Any],
        model_execution: Mapping[str, Any],
        document_read_proof_ref: str | None = None,
    ) -> dict[str, Any]:
        """Derive an immutable plan request from already-acquired authority."""

        review = self.connection.execute(
            "SELECT * FROM coverage_mission_document_reviews WHERE review_id=?",
            (review_ref,),
        ).fetchone()
        if review is None:
            raise RegisteredAnnualReportError("annual report review is unavailable")
        discovered = self.connection.execute(
            "SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?",
            (review["discovered_document_ref"],),
        ).fetchone()
        if discovered is None:
            raise RegisteredAnnualReportError("acquired annual report is unavailable")
        completion = None
        if document_read_proof_ref is not None:
            completion_row = self.connection.execute(
                "SELECT content_hash FROM document_read_completion_proofs WHERE proof_id=?",
                (document_read_proof_ref,),
            ).fetchone()
            if completion_row is None:
                raise RegisteredAnnualReportError("document read completion proof is unavailable")
            completion = _read_completion_proof(
                self.connection,
                proof_ref=document_read_proof_ref,
                proof_hash=completion_row["content_hash"],
            )
        document_ref = f"sec:filing:{accession}"
        if (
            review["mission_version_ref"] != mission_version_ref
            or review["company_ref"] != company_ref
            or review["source_ref"] != SOURCE_REF
            or review["document_ref"] != document_ref
            or discovered["record_id"] != review["discovered_document_ref"]
            or discovered["status"] != "acquired"
            or discovered["ticket_ref"] is None
            or (completion is not None and (
                completion["review_id"] != review_ref
                or completion["document_ref"] != document_ref
                or completion["company_ref"] != company_ref
            ))
        ):
            raise RegisteredAnnualReportError(
                "annual report authorities do not bind the exact request"
            )
        limits_copy = dict(limits)
        # Read the authoritative manifest and bytes before an identity exists.
        try:
            manifest = self.manifest_reader(discovered["ticket_ref"], document_ref)
            manifest, rendering = verified_public_web_source(
                self.core,
                self.spool,
                manifest,
                self.receipt_reader,
                max_source_chars=limits_copy["max_source_bytes"],
            )
        except Exception as exc:  # acquisition authorities expose typed conflicts
            raise RegisteredAnnualReportError(
                f"acquired annual report source verification failed: {exc}"
            ) from exc
        host = urlsplit(manifest["canonical_url"]).hostname
        if (
            host is None
            or not (host == "sec.gov" or host.endswith(".sec.gov"))
            or accession.replace("-", "") not in manifest["canonical_url"]
            or manifest["body_bytes"] > limits_copy["max_source_bytes"]
            or rendering["truncated"] is not False
        ):
            raise RegisteredAnnualReportError(
                "completed source is not a full bounded SEC accession rendering"
            )
        source_content_hash = source_bytes_hash(rendering["text"].encode("utf-8"))
        if (
            completion is not None
            and completion["verified_source_content_hash"] != source_content_hash
        ):
            raise RegisteredAnnualReportError(
                "completion proof differs from the acquired annual report text"
            )
        request = normalize_request({
            "mission_version_ref": mission_version_ref,
            "company_ref": company_ref,
            "review_ref": review_ref,
            "acquired_document_ref": discovered["record_id"],
            "acquired_document_hash": content_hash(_acquisition_binding(discovered)),
            "acquisition_ticket_ref": discovered["ticket_ref"],
            "document_read_proof_ref": document_read_proof_ref,
            "document_read_proof_hash": (
                None if completion is None else completion["content_hash"]
            ),
            "issuer_cik": issuer_cik,
            "accession": accession,
            "source_manifest_ref": manifest["id"],
            "source_manifest_hash": manifest["content_hash"],
            "source_raw_hash": manifest["body_sha256"],
            "source_content_hash": source_content_hash,
            "source_renderer": rendering["renderer"],
            "query_terms": query_terms,
            "limits": limits_copy,
            "model_execution": dict(model_execution),
        })
        mission_row = self.connection.execute(
            "SELECT record_json,content_hash FROM coverage_mission_versions "
            "WHERE mission_version_id=?", (mission_version_ref,),
        ).fetchone()
        if mission_row is None:
            raise RegisteredAnnualReportError("coverage mission budget authority is unavailable")
        try:
            mission = json.loads(mission_row["record_json"])
        except (TypeError, ValueError) as exc:
            raise RegisteredAnnualReportError("coverage mission budget authority is invalid") from exc
        asserted = mission.get("content_hash") if isinstance(mission, Mapping) else None
        mission_body = dict(mission) if isinstance(mission, Mapping) else {}
        mission_body.pop("content_hash", None)
        if (
            not isinstance(mission, Mapping)
            or mission_row["content_hash"]
            != (asserted if asserted is not None else content_hash(mission_body))
            or (asserted is not None and asserted != content_hash(mission_body))
            or not isinstance(mission.get("budget"), Mapping)
        ):
            raise RegisteredAnnualReportError("coverage mission budget authority is invalid")
        mission_budget = mission["budget"]
        paid_calls = sum(
            request["model_execution"][stage]["max_attempts"]
            for stage in ("draft", "verifier")
        )
        paid_cost = sum(
            request["model_execution"][stage]["max_attempts"]
            * request["model_execution"][stage]["max_cost_usd"]
            for stage in ("draft", "verifier")
        )
        call_cap = mission_budget.get("max_daily_paid_calls")
        cost_cap = mission_budget.get("max_daily_cost_usd")
        if (
            isinstance(call_cap, bool) or not isinstance(call_cap, int)
            or isinstance(cost_cap, bool) or not isinstance(cost_cap, (int, float))
            or not math.isfinite(float(cost_cap))
            or call_cap < 0 or float(cost_cap) < 0
            or paid_calls > call_cap or paid_cost > float(cost_cap)
        ):
            raise RegisteredAnnualReportError(
                "annual-report model attempt/cost budget exceeds the exact coverage mission"
            )
        # Includes exact issuer statement authority and immutable proof checks.
        resolve_core_registration(self.connection, request)
        return request

    def verify_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        registration, _manifest, _text = self._verified_text(request)
        return registration

    def search(self, request: Mapping[str, Any]) -> dict[str, Any]:
        normalized = normalize_request(request)
        registration, _manifest, text = self._verified_text(normalized)
        matches: list[dict[str, Any]] = []
        for term in normalized["query_terms"]:
            pattern = re.compile(
                r"\s+".join(re.escape(part) for part in term.split()),
                re.IGNORECASE,
            )
            for found in pattern.finditer(text):
                start = max(0, found.start() - normalized["limits"]["context_before_chars"])
                end = min(len(text), found.end() + normalized["limits"]["context_after_chars"])
                excerpt = text[start:end]
                excerpt_hash = source_bytes_hash(excerpt.encode("utf-8"))
                matches.append({
                    "matched_term": term,
                    "source_start": start,
                    "source_end": end,
                    "excerpt": excerpt,
                    "excerpt_sha256": excerpt_hash,
                    "source_location": (
                        f"annual-report-context:sec:filing:{normalized['accession']}:"
                        f"{start}:{end}:{excerpt_hash}"
                    ),
                })
                if len(matches) == normalized["limits"]["max_results"]:
                    break
            if len(matches) == normalized["limits"]["max_results"]:
                break
        body = {
            "schema_version": "0.2",
            "id": "annual-report-retrieval-proof:" + content_hash({
                "request": normalized,
                "registration_ref": registration["id"],
                "matches": matches,
            })[:32],
            "operation": OPERATION,
            "permission_scope": PERMISSION_SCOPE,
            "request": normalized,
            "registration": registration,
            "matches": matches,
        }
        body["content_hash"] = content_hash(body)
        return validate_retrieval_proof(body, expected_request=normalized)


__all__ = [
    "CAPABILITY", "DEFAULT_CONTEXT_AFTER_CHARS", "DEFAULT_CONTEXT_BEFORE_CHARS",
    "DEFAULT_MAX_QUERY_TERMS", "DEFAULT_MAX_RESULTS", "DEFAULT_MAX_SOURCE_BYTES",
    "OPERATION", "OUTPUT_CONTRACT_REF",
    "PERMISSION_SCOPE", "RUNTIME_PROFILE_REF", "RegisteredAnnualReportError",
    "RegisteredAnnualReportRegistry", "normalize_request",
    "resolve_core_registration", "source_bytes_hash", "validate_retrieval_proof",
]
