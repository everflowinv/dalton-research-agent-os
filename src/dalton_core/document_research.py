"""Read-only, exact-source research over already acquired documents.

This module does not discover, fetch, summarize, or promote anything.  It
turns one completed acquisition manifest into a versioned registration, then
re-verifies that acquisition before returning deterministic search excerpts
or an exact character range.  Claims and deliverables are deliberately not a
source adapter: a derived assertion cannot stand in for the document from
which it was derived.

Version 0.1 supports the two complete-text human feeds (sales notes and the
company wiki).  Their acquisition manifest binds the normalized UTF-8 object
that is read here.  It does not bind a separate copy of the original source
container, so ``raw_source`` says ``not_bound`` rather than pretending the
connector's JSON response is the original document.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .feed_acquisition import (
    COMPANY_WIKI_SOURCE_REF,
    SALES_NOTES_SOURCE_REF,
    verified_feed_source,
)
from .store import canonical_json, content_hash


REGISTRATION_SCHEMA_VERSION = "document-research-registration-0.1"
POLICY_SCHEMA_VERSION = "document-research-policy-0.1"
SEARCH_REQUEST_SCHEMA_VERSION = "document-research-search-request-0.1"
SEARCH_PROOF_SCHEMA_VERSION = "document-research-search-proof-0.1"
READ_REQUEST_SCHEMA_VERSION = "document-research-read-request-0.1"
READ_PROOF_SCHEMA_VERSION = "document-research-read-proof-0.1"
SEARCH_OPERATION = "search_registered_document"
READ_OPERATION = "read_registered_document"

SUPPORTED_FEED_SOURCE_REFS = frozenset({
    SALES_NOTES_SOURCE_REF,
    COMPANY_WIKI_SOURCE_REF,
})

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REGISTRATION_FIELDS = frozenset({
    "schema_version", "id", "authority_kind", "source_ref", "source_type",
    "source_version", "document_ref", "manifest_ref", "manifest_hash",
    "acquisition_ticket_ref", "doc_kind", "evidence_tier", "doc_date",
    "origin_ref", "subject_tickers", "raw_source", "normalized_text",
    "connector_invocation_ref", "connector_invocation_hash",
    "connector_profile_ref", "connector_profile_hash", "access_policy_ref",
    "retention_policy_ref", "terms_policy_ref", "content_hash",
})


class DocumentResearchError(RuntimeError):
    """A request or configured authority is invalid."""


class DocumentResearchConflict(DocumentResearchError):
    """Stored source authority drifted from its registered identity."""


class DocumentResearchAccessDenied(DocumentResearchError):
    """The configured research policy does not grant this purpose/source."""


def _record(body: Mapping[str, Any]) -> dict[str, Any]:
    return {**dict(body), "content_hash": content_hash(dict(body))}


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DocumentResearchError(f"{name} must be non-empty text")
    return value.strip()


def _sha256(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise DocumentResearchError(f"{name} must be lowercase SHA-256")
    return value


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DocumentResearchError(f"{name} must be a positive integer")
    return value


def _nonnegative_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DocumentResearchError(f"{name} must be a non-negative integer")
    return value


def build_document_research_policy(
    *,
    policy_ref: str,
    allowed_purposes: Sequence[str],
    allowed_access_policy_refs: Sequence[str],
    max_question_chars: int,
    max_query_terms: int,
    max_query_term_chars: int,
    max_results: int,
    max_context_before_chars: int,
    max_context_after_chars: int,
    max_read_chars: int,
) -> dict[str, Any]:
    """Build the explicit, owner-configurable bounds and access grant."""

    purposes = [_text(item, "allowed_purposes item") for item in allowed_purposes]
    access_refs = [
        _text(item, "allowed_access_policy_refs item")
        for item in allowed_access_policy_refs
    ]
    if not purposes or len(purposes) != len(set(purposes)):
        raise DocumentResearchError("allowed_purposes must be non-empty and unique")
    if not access_refs or len(access_refs) != len(set(access_refs)):
        raise DocumentResearchError(
            "allowed_access_policy_refs must be non-empty and unique"
        )
    body = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "policy_ref": _text(policy_ref, "policy_ref"),
        "allowed_purposes": purposes,
        "allowed_access_policy_refs": access_refs,
        "max_question_chars": _positive_integer(
            max_question_chars, "max_question_chars"
        ),
        "max_query_terms": _positive_integer(max_query_terms, "max_query_terms"),
        "max_query_term_chars": _positive_integer(
            max_query_term_chars, "max_query_term_chars"
        ),
        "max_results": _positive_integer(max_results, "max_results"),
        "max_context_before_chars": _positive_integer(
            max_context_before_chars, "max_context_before_chars"
        ),
        "max_context_after_chars": _positive_integer(
            max_context_after_chars, "max_context_after_chars"
        ),
        "max_read_chars": _positive_integer(max_read_chars, "max_read_chars"),
    }
    return _record(body)


def validate_document_research_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "schema_version", "policy_ref", "allowed_purposes",
        "allowed_access_policy_refs", "max_question_chars", "max_query_terms",
        "max_query_term_chars", "max_results", "max_context_before_chars",
        "max_context_after_chars", "max_read_chars", "content_hash",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise DocumentResearchError("document research policy has an invalid shape")
    wire = json.loads(canonical_json(value))
    rebuilt = build_document_research_policy(
        policy_ref=wire["policy_ref"],
        allowed_purposes=wire["allowed_purposes"],
        allowed_access_policy_refs=wire["allowed_access_policy_refs"],
        max_question_chars=wire["max_question_chars"],
        max_query_terms=wire["max_query_terms"],
        max_query_term_chars=wire["max_query_term_chars"],
        max_results=wire["max_results"],
        max_context_before_chars=wire["max_context_before_chars"],
        max_context_after_chars=wire["max_context_after_chars"],
        max_read_chars=wire["max_read_chars"],
    )
    if rebuilt != wire:
        raise DocumentResearchError("document research policy content hash drifted")
    return wire


class FeedDocumentSourceAdapter:
    """Resolve a feed document only through its completed acquisition ticket."""

    def __init__(
        self,
        *,
        source_ref: str,
        launcher: Any,
        core: Any,
        spool: Any,
        receipt_reader: Any,
    ) -> None:
        if source_ref not in SUPPORTED_FEED_SOURCE_REFS:
            raise DocumentResearchError(
                "feed document research supports sales notes and company wiki in v0.1"
            )
        for method in ("read_completed_manifest", "locate_completed_manifest"):
            if not callable(getattr(launcher, method, None)):
                raise TypeError(f"launcher must expose {method}")
        if not callable(getattr(spool, "read_object", None)):
            raise TypeError("spool must expose read_object")
        for method in ("get_invocation", "get_profile"):
            if not callable(getattr(receipt_reader, method, None)):
                raise TypeError(f"receipt_reader must expose {method}")
        self.source_ref = source_ref
        self.launcher = launcher
        self.core = core
        self.spool = spool
        self.receipt_reader = receipt_reader

    def materialize(
        self, *, document_ref: str, acquisition_ticket_ref: str | None
    ) -> tuple[dict[str, Any], str]:
        document_ref = _text(document_ref, "document_ref")
        if acquisition_ticket_ref is None:
            manifest = self.launcher.locate_completed_manifest(document_ref)
        else:
            manifest = self.launcher.read_completed_manifest(
                _text(acquisition_ticket_ref, "acquisition_ticket_ref"), document_ref
            )
        manifest, text = verified_feed_source(
            self.core, self.spool, manifest, self.receipt_reader
        )
        if manifest["source_ref"] != self.source_ref:
            raise DocumentResearchConflict(
                "completed acquisition belongs to a different source"
            )
        invocation_ref = manifest.get("connector_invocation_ref")
        invocation_hash = manifest.get("connector_invocation_hash")
        if invocation_ref is None or invocation_hash is None:
            raise DocumentResearchConflict(
                "document research requires the completed connector invocation authority"
            )
        invocation = self.receipt_reader.get_invocation(invocation_ref)
        if (
            not isinstance(invocation, Mapping)
            or invocation.get("id") != invocation_ref
            or invocation.get("content_hash") != invocation_hash
            or content_hash({
                key: item for key, item in invocation.items()
                if key != "content_hash"
            }) != invocation_hash
        ):
            raise DocumentResearchConflict("connector invocation authority drifted")
        profile_ref = invocation.get("connector_profile_ref")
        profile_hash = invocation.get("connector_profile_hash")
        profile = self.receipt_reader.get_profile(profile_ref)
        if (
            not isinstance(profile, Mapping)
            or profile.get("id") != profile_ref
            or profile.get("content_hash") != profile_hash
            or content_hash({
                key: item for key, item in profile.items()
                if key != "content_hash"
            }) != profile_hash
            or not isinstance(profile.get("source_identity"), Mapping)
            or profile["source_identity"].get("source_ref") != self.source_ref
        ):
            raise DocumentResearchConflict("connector profile source authority drifted")
        policies: dict[str, str] = {}
        for name in (
            "access_policy_ref", "retention_policy_ref", "terms_policy_ref"
        ):
            policies[name] = _text(profile.get(name), f"connector profile {name}")
        source_identity = profile["source_identity"]
        body = {
            "schema_version": REGISTRATION_SCHEMA_VERSION,
            "authority_kind": "feed-acquisition-manifest",
            "source_ref": self.source_ref,
            "source_type": _text(source_identity.get("source_type"), "source_type"),
            "source_version": _text(
                source_identity.get("source_version"), "source_version"
            ),
            "document_ref": manifest["document_ref"],
            "manifest_ref": manifest["id"],
            "manifest_hash": manifest["content_hash"],
            "acquisition_ticket_ref": acquisition_ticket_ref,
            "doc_kind": manifest["doc_kind"],
            "evidence_tier": manifest["evidence_tier"],
            "doc_date": manifest["doc_date"],
            "origin_ref": manifest["origin_ref"],
            "subject_tickers": list(manifest["subject_tickers"]),
            "raw_source": {
                "status": "not_bound",
                "reason": "feed manifest binds assembled UTF-8 text, not a separate original container",
                "artifact_ref": None,
                "content_hash": None,
                "size_bytes": None,
            },
            "normalized_text": {
                "status": "complete",
                "representation": "assembled-utf8",
                "renderer_ref": None,
                "object_hash": manifest["assembled_object"]["content_hash"],
                "size_bytes": manifest["assembled_object"]["size_bytes"],
                "text_sha256": manifest["declared_content_sha256"],
                "characters": manifest["content_chars"],
                "truncated": False,
            },
            "connector_invocation_ref": invocation_ref,
            "connector_invocation_hash": invocation_hash,
            "connector_profile_ref": profile_ref,
            "connector_profile_hash": profile_hash,
            **policies,
        }
        digest = content_hash(body)
        registration = _record({
            **body,
            "id": f"registered-document:sha256:{digest}",
        })
        return registration, text


def validate_registration(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _REGISTRATION_FIELDS:
        raise DocumentResearchError("document registration has an invalid shape")
    wire = json.loads(canonical_json(value))
    if wire["schema_version"] != REGISTRATION_SCHEMA_VERSION:
        raise DocumentResearchError("unsupported document registration version")
    asserted_hash = _sha256(wire["content_hash"], "registration content_hash")
    body = {key: item for key, item in wire.items() if key != "content_hash"}
    if content_hash(body) != asserted_hash:
        raise DocumentResearchError("document registration content hash drifted")
    identity_body = {key: item for key, item in body.items() if key != "id"}
    if wire["id"] != f"registered-document:sha256:{content_hash(identity_body)}":
        raise DocumentResearchError("document registration id drifted")
    for name in (
        "source_ref", "source_type", "source_version", "document_ref",
        "manifest_ref", "doc_kind", "evidence_tier", "doc_date", "origin_ref",
        "connector_invocation_ref", "connector_profile_ref", "access_policy_ref",
        "retention_policy_ref", "terms_policy_ref",
    ):
        _text(wire[name], f"registration {name}")
    for name in (
        "manifest_hash", "connector_invocation_hash", "connector_profile_hash"
    ):
        _sha256(wire[name], f"registration {name}")
    if wire["acquisition_ticket_ref"] is not None:
        _text(wire["acquisition_ticket_ref"], "registration acquisition_ticket_ref")
    if (
        not isinstance(wire["subject_tickers"], list)
        or any(not isinstance(item, str) or not item for item in wire["subject_tickers"])
        or len(wire["subject_tickers"]) != len(set(wire["subject_tickers"]))
    ):
        raise DocumentResearchError("registration subject_tickers are invalid")
    raw_source = wire["raw_source"]
    if (
        not isinstance(raw_source, Mapping)
        or set(raw_source) != {
            "status", "reason", "artifact_ref", "content_hash", "size_bytes"
        }
        or raw_source["status"] != "not_bound"
        or not isinstance(raw_source["reason"], str)
        or any(raw_source[name] is not None for name in (
            "artifact_ref", "content_hash", "size_bytes"
        ))
    ):
        raise DocumentResearchError("registration raw_source is invalid")
    normalized = wire["normalized_text"]
    if (
        not isinstance(normalized, Mapping)
        or set(normalized) != {
            "status", "representation", "renderer_ref", "object_hash",
            "size_bytes", "text_sha256", "characters", "truncated",
        }
        or normalized["status"] != "complete"
        or normalized["representation"] != "assembled-utf8"
        or normalized["renderer_ref"] is not None
        or normalized["truncated"] is not False
    ):
        raise DocumentResearchError("registration normalized_text is invalid")
    _sha256(normalized["object_hash"], "normalized_text.object_hash")
    _sha256(normalized["text_sha256"], "normalized_text.text_sha256")
    _positive_integer(normalized["size_bytes"], "normalized_text.size_bytes")
    _positive_integer(normalized["characters"], "normalized_text.characters")
    return wire


def _normalize_terms(
    raw: Any, *, maximum: int, maximum_characters: int
) -> list[str]:
    if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= maximum:
        raise DocumentResearchError("query_terms exceed the configured bound")
    terms: list[str] = []
    for item in raw:
        term = " ".join(_text(item, "query term").split())
        if len(term) > maximum_characters:
            raise DocumentResearchError("query term exceeds the configured bound")
        if term.casefold() in {existing.casefold() for existing in terms}:
            raise DocumentResearchError("query_terms must be unique")
        terms.append(term)
    return terms


class DocumentResearchRegistry:
    """A configured set of source adapters and one reusable access policy."""

    def __init__(
        self, *, adapters: Mapping[str, FeedDocumentSourceAdapter], policy: Mapping[str, Any]
    ) -> None:
        self.policy = validate_document_research_policy(policy)
        if not isinstance(adapters, Mapping) or not adapters:
            raise TypeError("adapters must be a non-empty mapping")
        self.adapters = dict(adapters)
        if any(key != adapter.source_ref for key, adapter in self.adapters.items()):
            raise TypeError("adapter map keys must equal adapter source_ref")

    def _authorize(self, purpose: Any, registration: Mapping[str, Any]) -> str:
        purpose = _text(purpose, "purpose")
        if purpose not in self.policy["allowed_purposes"]:
            raise DocumentResearchAccessDenied("research purpose is not authorized")
        if registration["access_policy_ref"] not in self.policy["allowed_access_policy_refs"]:
            raise DocumentResearchAccessDenied(
                "source access policy is not authorized for document research"
            )
        return purpose

    def register(
        self,
        *,
        source_ref: str,
        document_ref: str,
        purpose: str,
        acquisition_ticket_ref: str | None = None,
    ) -> dict[str, Any]:
        try:
            adapter = self.adapters[source_ref]
        except KeyError as exc:
            raise DocumentResearchError("source has no document research adapter") from exc
        registration, _ = adapter.materialize(
            document_ref=document_ref,
            acquisition_ticket_ref=acquisition_ticket_ref,
        )
        self._authorize(purpose, registration)
        return registration

    def _reresolve(
        self, registration: Mapping[str, Any], *, purpose: Any
    ) -> tuple[dict[str, Any], str]:
        expected = validate_registration(registration)
        try:
            adapter = self.adapters[expected["source_ref"]]
        except KeyError as exc:
            raise DocumentResearchError("registration source is not configured") from exc
        actual, text = adapter.materialize(
            document_ref=expected["document_ref"],
            acquisition_ticket_ref=expected["acquisition_ticket_ref"],
        )
        if actual != expected:
            raise DocumentResearchConflict("registered document authority changed")
        self._authorize(purpose, actual)
        return actual, text

    def search(self, request: Mapping[str, Any]) -> dict[str, Any]:
        fields = {
            "schema_version", "operation", "purpose", "research_question",
            "registration", "query_terms", "limits", "policy_ref", "policy_hash",
        }
        if not isinstance(request, Mapping) or set(request) != fields:
            raise DocumentResearchError("document search request has an invalid shape")
        wire = json.loads(canonical_json(request))
        if wire["schema_version"] != SEARCH_REQUEST_SCHEMA_VERSION \
                or wire["operation"] != SEARCH_OPERATION:
            raise DocumentResearchError("unsupported document search request")
        question = _text(wire["research_question"], "research_question")
        if len(question) > self.policy["max_question_chars"]:
            raise DocumentResearchError("research_question exceeds the configured bound")
        if wire["policy_ref"] != self.policy["policy_ref"] \
                or wire["policy_hash"] != self.policy["content_hash"]:
            raise DocumentResearchConflict("document research policy identity drifted")
        limits = wire["limits"]
        if not isinstance(limits, Mapping) or set(limits) != {
            "max_results", "context_before_chars", "context_after_chars"
        }:
            raise DocumentResearchError("document search limits have an invalid shape")
        for name, policy_name in (
            ("max_results", "max_results"),
            ("context_before_chars", "max_context_before_chars"),
            ("context_after_chars", "max_context_after_chars"),
        ):
            value = (
                _positive_integer(limits[name], f"limits.{name}")
                if name == "max_results"
                else _nonnegative_integer(limits[name], f"limits.{name}")
            )
            if value > self.policy[policy_name]:
                raise DocumentResearchError(f"limits.{name} exceeds the configured policy")
        terms = _normalize_terms(
            wire["query_terms"],
            maximum=self.policy["max_query_terms"],
            maximum_characters=self.policy["max_query_term_chars"],
        )
        registration, text = self._reresolve(
            wire["registration"], purpose=wire["purpose"]
        )
        candidates: list[tuple[int, int, int, str]] = []
        for term_order, term in enumerate(terms):
            pattern = re.compile(
                re.escape(term).replace(r"\ ", r"\s+"), re.IGNORECASE
            )
            for match in pattern.finditer(text):
                candidates.append((match.start(), term_order, match.end(), term))
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        matches: list[dict[str, Any]] = []
        seen_spans: set[tuple[int, int]] = set()
        for match_start, _order, match_end, term in candidates:
            if len(matches) >= limits["max_results"]:
                break
            span = (match_start, match_end)
            if span in seen_spans:
                continue
            seen_spans.add(span)
            start = max(0, match_start - limits["context_before_chars"])
            end = min(len(text), match_end + limits["context_after_chars"])
            excerpt = text[start:end]
            excerpt_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
            matches.append({
                "matched_term": term,
                "match_start": match_start,
                "match_end": match_end,
                "source_start": start,
                "source_end": end,
                "excerpt": excerpt,
                "excerpt_sha256": excerpt_hash,
                "source_location": (
                    f"document-context:{registration['id']}:{start}:{end}:{excerpt_hash}"
                ),
            })
        normalized_request = {
            **wire,
            "research_question": question,
            "query_terms": terms,
            "limits": dict(limits),
            "registration": registration,
        }
        proof_body = {
            "schema_version": SEARCH_PROOF_SCHEMA_VERSION,
            "operation": SEARCH_OPERATION,
            "request": normalized_request,
            "matches": matches,
        }
        proof_hash = content_hash(proof_body)
        return _record({
            **proof_body,
            "id": f"document-search-proof:sha256:{proof_hash}",
        })

    def read(self, request: Mapping[str, Any]) -> dict[str, Any]:
        fields = {
            "schema_version", "operation", "purpose", "research_question",
            "registration", "source_start", "source_end", "policy_ref", "policy_hash",
        }
        if not isinstance(request, Mapping) or set(request) != fields:
            raise DocumentResearchError("document read request has an invalid shape")
        wire = json.loads(canonical_json(request))
        if wire["schema_version"] != READ_REQUEST_SCHEMA_VERSION \
                or wire["operation"] != READ_OPERATION:
            raise DocumentResearchError("unsupported document read request")
        question = _text(wire["research_question"], "research_question")
        if len(question) > self.policy["max_question_chars"]:
            raise DocumentResearchError("research_question exceeds the configured bound")
        if wire["policy_ref"] != self.policy["policy_ref"] \
                or wire["policy_hash"] != self.policy["content_hash"]:
            raise DocumentResearchConflict("document research policy identity drifted")
        start, end = wire["source_start"], wire["source_end"]
        if (
            isinstance(start, bool) or not isinstance(start, int) or start < 0
            or isinstance(end, bool) or not isinstance(end, int) or end <= start
            or end - start > self.policy["max_read_chars"]
        ):
            raise DocumentResearchError("document read range is invalid or exceeds policy")
        registration, text = self._reresolve(
            wire["registration"], purpose=wire["purpose"]
        )
        if end > len(text):
            raise DocumentResearchError("document read range exceeds the registered text")
        excerpt = text[start:end]
        excerpt_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
        normalized_request = {
            **wire,
            "research_question": question,
            "registration": registration,
        }
        proof_body = {
            "schema_version": READ_PROOF_SCHEMA_VERSION,
            "operation": READ_OPERATION,
            "request": normalized_request,
            "source_start": start,
            "source_end": end,
            "text": excerpt,
            "text_sha256": excerpt_hash,
            "source_location": (
                f"document-context:{registration['id']}:{start}:{end}:{excerpt_hash}"
            ),
        }
        proof_hash = content_hash(proof_body)
        return _record({
            **proof_body,
            "id": f"document-read-proof:sha256:{proof_hash}",
        })

    def verify_search_proof(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """Re-run the exact registered search and require byte-identical proof."""

        if (
            not isinstance(value, Mapping)
            or value.get("schema_version") != SEARCH_PROOF_SCHEMA_VERSION
            or value.get("operation") != SEARCH_OPERATION
            or not isinstance(value.get("request"), Mapping)
        ):
            raise DocumentResearchError("document search proof has an invalid shape")
        expected = self.search(value["request"])
        if canonical_json(expected) != canonical_json(value):
            raise DocumentResearchConflict("document search proof does not replay")
        return expected

    def verify_read_proof(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """Re-read the exact range and require byte-identical proof."""

        if (
            not isinstance(value, Mapping)
            or value.get("schema_version") != READ_PROOF_SCHEMA_VERSION
            or value.get("operation") != READ_OPERATION
            or not isinstance(value.get("request"), Mapping)
        ):
            raise DocumentResearchError("document read proof has an invalid shape")
        expected = self.read(value["request"])
        if canonical_json(expected) != canonical_json(value):
            raise DocumentResearchConflict("document read proof does not replay")
        return expected


__all__ = [
    "DocumentResearchAccessDenied",
    "DocumentResearchConflict",
    "DocumentResearchError",
    "DocumentResearchRegistry",
    "FeedDocumentSourceAdapter",
    "READ_OPERATION",
    "READ_REQUEST_SCHEMA_VERSION",
    "SEARCH_OPERATION",
    "SEARCH_REQUEST_SCHEMA_VERSION",
    "build_document_research_policy",
    "validate_document_research_policy",
    "validate_registration",
]
