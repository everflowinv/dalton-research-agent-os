"""Read-only, exact-source research over already acquired documents.

This module does not discover, fetch, summarize, or promote anything.  It
turns one completed acquisition manifest into a versioned registration, then
re-verifies that acquisition before returning deterministic search excerpts
or an exact character range.  Claims and deliverables are deliberately not a
source adapter: a derived assertion cannot stand in for the document from
which it was derived.

The registration contract's current version supports complete-text human
feeds, complete AlphaEngine acquisitions, and complete deterministic
public-web renderings. Sales-note and wiki feed manifests bind normalized
UTF-8 but no separate original source container, so those registrations say
``raw_source`` is ``not_bound``. Prior-research manifest 0.2 instead binds and
re-renders the exact original container; historical normalized-only manifests
cannot register as complete original documents.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .feed_acquisition import (
    COMPANY_WIKI_SOURCE_REF,
    PRIOR_MANIFEST_SCHEMA_VERSION,
    PRIOR_RESEARCH_SOURCE_REF,
    SALES_NOTES_SOURCE_REF,
    verified_feed_source,
)
from .store import canonical_json, content_hash


REGISTRATION_SCHEMA_VERSION = "document-research-registration-0.2"
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
    PRIOR_RESEARCH_SOURCE_REF,
})
SUPPORTED_FETCH_DISCOVERY_SOURCE_REFS = frozenset({
    "source:public-web", "source:web-search", "source:sec-edgar",
})

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REGISTRATION_FIELDS = frozenset({
    "schema_version", "id", "authority_kind", "source_ref", "source_type",
    "source_version", "document_ref", "content_document_ref", "source_authority", "manifest_ref",
    "manifest_hash", "acquisition_ticket_ref", "doc_kind", "evidence_tier", "doc_date",
    "origin_ref", "subject_tickers", "raw_source", "normalized_text",
    "reader",
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
                "feed document research source is unsupported"
            )
        for method in (
            "read_completed_manifest", "locate_completed_manifest",
            "locate_completed_manifest_binding",
        ):
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
            binding = self.launcher.locate_completed_manifest_binding(document_ref)
            if (
                not isinstance(binding, Mapping)
                or set(binding) != {"ticket_ref", "manifest"}
            ):
                raise DocumentResearchConflict(
                    "completed acquisition locator did not return an exact ticket binding"
                )
            acquisition_ticket_ref = _text(
                binding["ticket_ref"], "located acquisition ticket ref"
            )
            manifest = binding["manifest"]
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
        if self.source_ref == PRIOR_RESEARCH_SOURCE_REF:
            if manifest["schema_version"] != PRIOR_MANIFEST_SCHEMA_VERSION:
                raise DocumentResearchConflict(
                    "legacy prior-research acquisition does not bind its original source"
                )
            bundle = manifest["original_source_bundle"]
            projection = bundle["normalized_projection"]
            if projection["complete"] is not True:
                raise DocumentResearchConflict(
                    "prior-research normalized projection is incomplete"
                )
            original = bundle["source_object"]
            raw_source = {
                "status": "bound",
                "representation": "original-source-container",
                "reason": None,
                "artifact_refs": [original["storage_locator"]],
                "content_hashes": [original["content_hash"]],
                "total_size_bytes": original["size_bytes"],
            }
            normalized_text = {
                "status": "complete",
                "representation": "rendered-original-source",
                "renderer_ref": projection["renderer"],
                "object_hash": manifest["assembled_object"]["content_hash"],
                "size_bytes": manifest["assembled_object"]["size_bytes"],
                "text_sha256": manifest["declared_content_sha256"],
                "characters": manifest["content_chars"],
                "truncated": False,
            }
            reader = _record({
                "ref": "document-reader:prior-original-render:0.1",
                "config": {
                    "format": projection["format"],
                    "renderer": projection["renderer"],
                    "preserves": list(projection["preserves"]),
                    "omits": list(projection["omits"]),
                    "original_source_bundle_hash": bundle["content_hash"],
                },
            })
        else:
            raw_source = {
                "status": "not_bound",
                "representation": None,
                "reason": (
                    "feed manifest binds assembled UTF-8 text, not a separate "
                    "original container"
                ),
                "artifact_refs": [],
                "content_hashes": [],
                "total_size_bytes": None,
            }
            normalized_text = {
                "status": "complete",
                "representation": "assembled-utf8",
                "renderer_ref": None,
                "object_hash": manifest["assembled_object"]["content_hash"],
                "size_bytes": manifest["assembled_object"]["size_bytes"],
                "text_sha256": manifest["declared_content_sha256"],
                "characters": manifest["content_chars"],
                "truncated": False,
            }
            reader = _record({
                "ref": "document-reader:feed-acquisition:0.1",
                "config": {},
            })
        body = {
            "schema_version": REGISTRATION_SCHEMA_VERSION,
            "authority_kind": "feed-acquisition-manifest",
            "source_ref": self.source_ref,
            "source_type": _text(source_identity.get("source_type"), "source_type"),
            "source_version": _text(
                source_identity.get("source_version"), "source_version"
            ),
            "document_ref": document_ref,
            "content_document_ref": manifest["document_ref"],
            "source_authority": {
                "kind": "feed-acquisition-manifest",
                "ref": manifest["id"],
                "hash": manifest["content_hash"],
                "mission_version_ref": None,
                "company_ref": None,
            },
            "manifest_ref": manifest["id"],
            "manifest_hash": manifest["content_hash"],
            "acquisition_ticket_ref": acquisition_ticket_ref,
            "doc_kind": manifest["doc_kind"],
            "evidence_tier": manifest["evidence_tier"],
            "doc_date": manifest["doc_date"],
            "origin_ref": manifest["origin_ref"],
            "subject_tickers": list(manifest["subject_tickers"]),
            "raw_source": raw_source,
            "normalized_text": normalized_text,
            "reader": reader,
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


class AlphaEngineDocumentSourceAdapter:
    """Materialize one complete AlphaEngine document from every bound page."""

    source_ref = "source:alphaengine"

    def __init__(
        self, *, launcher: Any, core: Any, spool: Any, receipt_reader: Any,
        max_document_chars: int,
    ) -> None:
        for method in (
            "read_completed_manifest", "locate_completed_manifest_binding",
        ):
            if not callable(getattr(launcher, method, None)):
                raise TypeError(f"launcher must expose {method}")
        self.launcher = launcher
        self.core = core
        self.spool = spool
        self.receipt_reader = receipt_reader
        self.max_document_chars = _positive_integer(
            max_document_chars, "max_document_chars"
        )

    def materialize(
        self, *, document_ref: str, acquisition_ticket_ref: str | None
    ) -> tuple[dict[str, Any], str]:
        lookup_ref = _text(document_ref, "document_ref")
        if acquisition_ticket_ref is None:
            binding = self.launcher.locate_completed_manifest_binding(lookup_ref)
            if not isinstance(binding, Mapping) or set(binding) != {"ticket_ref", "manifest"}:
                raise DocumentResearchConflict(
                    "AlphaEngine locator did not return an exact ticket binding"
                )
            acquisition_ticket_ref = _text(
                binding["ticket_ref"], "located acquisition ticket ref"
            )
            manifest = binding["manifest"]
        else:
            manifest = self.launcher.read_completed_manifest(
                _text(acquisition_ticket_ref, "acquisition_ticket_ref"), lookup_ref
            )
        # Imported lazily so the read contract does not create extraction or
        # model workers at module import time.
        from .document_extraction import verified_source

        manifest, text = verified_source(
            self.core, self.spool, manifest, self.receipt_reader,
            max_document_chars=self.max_document_chars,
        )
        if manifest["source_ref"] != self.source_ref:
            raise DocumentResearchConflict("AlphaEngine manifest source drifted")
        profiles: list[Mapping[str, Any]] = []
        for page in manifest["pages"]:
            profile = self.receipt_reader.get_profile(page["connector_profile_ref"])
            if (
                not isinstance(profile, Mapping)
                or profile.get("id") != page["connector_profile_ref"]
                or profile.get("content_hash") != page["connector_profile_hash"]
                or content_hash({key: item for key, item in profile.items()
                                 if key != "content_hash"}) != profile["content_hash"]
            ):
                raise DocumentResearchConflict("AlphaEngine profile authority drifted")
            profiles.append(profile)
        first = profiles[0]
        policy_names = (
            "access_policy_ref", "retention_policy_ref", "terms_policy_ref"
        )
        stable_profile = {
            "source_identity": first.get("source_identity"),
            **{name: first.get(name) for name in policy_names},
        }
        if any({
            "source_identity": item.get("source_identity"),
            **{name: item.get(name) for name in policy_names},
        } != stable_profile for item in profiles):
            raise DocumentResearchConflict(
                "AlphaEngine page source policies changed within one document"
            )
        source_identity = first.get("source_identity")
        if not isinstance(source_identity, Mapping) \
                or source_identity.get("source_ref") != self.source_ref:
            raise DocumentResearchConflict("AlphaEngine source identity drifted")
        for name in policy_names:
            _text(first.get(name), f"AlphaEngine profile {name}")
        assembled = manifest["assembled_object"]
        reader = _record({
            "ref": "document-reader:alphaengine-complete:0.1",
            "config": {"max_document_chars": self.max_document_chars},
        })
        body = {
            "schema_version": REGISTRATION_SCHEMA_VERSION,
            "authority_kind": "alphaengine-document-acquisition-manifest",
            "source_ref": self.source_ref,
            "source_type": _text(source_identity.get("source_type"), "source_type"),
            "source_version": _text(source_identity.get("source_version"), "source_version"),
            "document_ref": lookup_ref,
            "content_document_ref": manifest["document_ref"],
            "source_authority": {
                "kind": "alphaengine-document-acquisition-manifest",
                "ref": manifest["id"],
                "hash": manifest["content_hash"],
                "mission_version_ref": None,
                "company_ref": None,
            },
            "manifest_ref": manifest["id"],
            "manifest_hash": manifest["content_hash"],
            "acquisition_ticket_ref": acquisition_ticket_ref,
            "doc_kind": None,
            "evidence_tier": None,
            "doc_date": None,
            "origin_ref": None,
            "subject_tickers": [],
            "raw_source": {
                "status": "bound",
                "representation": "paged-connector-responses",
                "reason": None,
                "artifact_refs": [page["raw_artifact_version_ref"] for page in manifest["pages"]],
                "content_hashes": [page["raw_response_hash"] for page in manifest["pages"]],
                "total_size_bytes": manifest["total_raw_response_bytes"],
            },
            "normalized_text": {
                "status": "complete",
                "representation": "assembled-utf8",
                "renderer_ref": "alphaengine-paged-text:0.1",
                "object_hash": assembled["content_hash"],
                "size_bytes": assembled["size_bytes"],
                "text_sha256": manifest["declared_content_sha256"],
                "characters": manifest["content_chars"],
                "truncated": False,
            },
            "reader": reader,
            "connector_invocation_ref": manifest["pages"][0]["connector_invocation_ref"],
            "connector_invocation_hash": manifest["pages"][0]["connector_invocation_hash"],
            "connector_profile_ref": first["id"],
            "connector_profile_hash": first["content_hash"],
            **{name: first[name] for name in policy_names},
        }
        digest = content_hash(body)
        return _record({
            **body, "id": f"registered-document:sha256:{digest}"
        }), text


class PublicWebDocumentSourceAdapter:
    """Materialize one fetched body and derive its discovery-source identity."""

    def __init__(
        self, *, source_ref: str | None, launcher: Any, core: Any, spool: Any,
        receipt_reader: Any, max_source_chars: int, max_pdf_pages: int,
        max_decompressed_bytes: int,
    ) -> None:
        if source_ref is not None and source_ref not in SUPPORTED_FETCH_DISCOVERY_SOURCE_REFS:
            raise DocumentResearchError("unsupported fetched-document discovery source")
        for method in (
            "read_completed_manifest", "locate_completed_manifest_binding",
        ):
            if not callable(getattr(launcher, method, None)):
                raise TypeError(f"launcher must expose {method}")
        self.source_ref = source_ref
        self.launcher = launcher
        self.core = core
        self.spool = spool
        self.receipt_reader = receipt_reader
        self.max_source_chars = _positive_integer(max_source_chars, "max_source_chars")
        self.max_pdf_pages = _positive_integer(max_pdf_pages, "max_pdf_pages")
        self.max_decompressed_bytes = _positive_integer(
            max_decompressed_bytes, "max_decompressed_bytes"
        )

    def materialize(
        self, *, document_ref: str, acquisition_ticket_ref: str | None
    ) -> tuple[dict[str, Any], str]:
        lookup_ref = _text(document_ref, "document_ref")
        if acquisition_ticket_ref is None:
            binding = self.launcher.locate_completed_manifest_binding(lookup_ref)
            if not isinstance(binding, Mapping) or set(binding) != {"ticket_ref", "manifest"}:
                raise DocumentResearchConflict(
                    "public-web locator did not return an exact ticket binding"
                )
            acquisition_ticket_ref = _text(
                binding["ticket_ref"], "located acquisition ticket ref"
            )
            manifest = binding["manifest"]
        else:
            manifest = self.launcher.read_completed_manifest(
                _text(acquisition_ticket_ref, "acquisition_ticket_ref"), lookup_ref
            )
        from .public_web_extraction_source import verified_public_web_source

        manifest, rendering = verified_public_web_source(
            self.core, self.spool, manifest, self.receipt_reader,
            max_source_chars=self.max_source_chars,
            max_pdf_pages=self.max_pdf_pages,
            max_decompressed_bytes=self.max_decompressed_bytes,
        )
        if rendering["truncated"] is not False:
            raise DocumentResearchConflict(
                "document research requires a complete non-truncated rendering"
            )
        discovery = self.receipt_reader.get_source_envelope(
            manifest["discovery_source_envelope_ref"]
        )
        if (
            not isinstance(discovery, Mapping)
            or discovery.get("id") != manifest["discovery_source_envelope_ref"]
            or discovery.get("content_hash") != manifest["discovery_source_envelope_hash"]
            or content_hash({key: item for key, item in discovery.items()
                             if key != "content_hash"}) != discovery["content_hash"]
            or (self.source_ref is not None and discovery.get("source") != self.source_ref)
        ):
            raise DocumentResearchConflict(
                "fetched document discovery-source authority drifted"
            )
        from .public_web_core_fetch import url_authority_from_discovery

        authority = url_authority_from_discovery(
            self.core.connection,
            self.spool,
            url_ref=lookup_ref,
            source_envelope_ref=manifest["discovery_source_envelope_ref"],
        )
        if (
            authority.get("id") != manifest["url_authority_ref"]
            or authority.get("content_hash") != manifest["url_authority_hash"]
            or authority.get("url_ref") != manifest["url_ref"]
            or authority.get("canonical_url") != manifest["canonical_url"]
        ):
            raise DocumentResearchConflict(
                "fetched document URL authority drifted from discovery bytes"
            )
        profile = self.receipt_reader.get_profile(manifest["connector_profile_ref"])
        if (
            not isinstance(profile, Mapping)
            or profile.get("id") != manifest["connector_profile_ref"]
            or profile.get("content_hash") != manifest["connector_profile_hash"]
        ):
            raise DocumentResearchConflict("public-web fetch profile authority drifted")
        policies = {}
        for name in (
            "access_policy_ref", "retention_policy_ref", "terms_policy_ref"
        ):
            policies[name] = _text(profile.get(name), f"public-web profile {name}")
        source_identity = profile["source_identity"]
        text = rendering["text"]
        reader = _record({
            "ref": "document-reader:public-web-render:0.1",
            "config": {
                "max_source_chars": self.max_source_chars,
                "max_pdf_pages": self.max_pdf_pages,
                "max_decompressed_bytes": self.max_decompressed_bytes,
            },
        })
        body = {
            "schema_version": REGISTRATION_SCHEMA_VERSION,
            "authority_kind": "public-web-fetch-manifest",
            "source_ref": discovery["source"],
            "source_type": _text(source_identity.get("source_type"), "source_type"),
            "source_version": _text(source_identity.get("source_version"), "source_version"),
            "document_ref": lookup_ref,
            "content_document_ref": manifest["document_ref"],
            "source_authority": {
                "kind": "discovery-source-envelope",
                "ref": discovery["id"],
                "hash": discovery["content_hash"],
                "mission_version_ref": None,
                "company_ref": None,
            },
            "manifest_ref": manifest["id"],
            "manifest_hash": manifest["content_hash"],
            "acquisition_ticket_ref": acquisition_ticket_ref,
            "doc_kind": None,
            "evidence_tier": None,
            "doc_date": None,
            "origin_ref": manifest["final_url"],
            "subject_tickers": [],
            "raw_source": {
                "status": "bound",
                "representation": "connector-response-body",
                "reason": None,
                "artifact_refs": [manifest["raw_artifact_version_ref"]],
                "content_hashes": [manifest["body_sha256"]],
                "total_size_bytes": manifest["body_bytes"],
            },
            "normalized_text": {
                "status": "complete",
                "representation": "deterministic-rendering",
                "renderer_ref": rendering["renderer"],
                "object_hash": None,
                "size_bytes": len(text.encode("utf-8")),
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "characters": len(text),
                "truncated": False,
            },
            "reader": reader,
            "connector_invocation_ref": manifest["connector_invocation_ref"],
            "connector_invocation_hash": manifest["connector_invocation_hash"],
            "connector_profile_ref": profile["id"],
            "connector_profile_hash": profile["content_hash"],
            **policies,
        }
        digest = content_hash(body)
        return _record({
            **body, "id": f"registered-document:sha256:{digest}"
        }), text


class CoreAcquiredDocumentSourceAdapter:
    """Bind one mission/company row to a source adapter's exact acquisition."""

    def __init__(
        self, *, core: Any, adapters: Mapping[str, Any],
        source_aliases: Mapping[str, str] | None = None,
    ) -> None:
        connection = getattr(core, "connection", None)
        if connection is None or not callable(getattr(connection, "execute", None)):
            raise TypeError("core must expose a database connection")
        if not isinstance(adapters, Mapping) or not adapters:
            raise TypeError("Core acquired documents require source adapters")
        configured = dict(adapters)
        if any(key != adapter.source_ref for key, adapter in configured.items()):
            raise TypeError("Core adapter map keys must equal adapter source_ref")
        aliases = {} if source_aliases is None else dict(source_aliases)
        if any(
            not isinstance(key, str) or not key
            or not isinstance(item, str) or item not in configured
            for key, item in aliases.items()
        ):
            raise TypeError("Core source aliases must name configured source adapters")
        self.core = core
        self.adapters = configured
        self.source_aliases = aliases

    @staticmethod
    def _authority(
        row: Mapping[str, Any], *, resolved_source_ref: str | None = None,
        discovery: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = {
            "schema_version": "coverage-mission-acquired-document-binding-0.1",
            "record_id": row["record_id"],
            "mission_version_ref": row["mission_version_ref"],
            "company_ref": row["company_ref"],
            "source_ref": row["source_ref"],
            "document_ref": row["document_ref"],
            "ticket_ref": row["ticket_ref"],
            "status": row["status"],
        }
        # Keep the existing 0.1 identity byte-for-byte for sources whose Core
        # and acquisition authorities use the same source_ref.  An alias is a
        # new authority shape: it must also freeze the exact discovery row and
        # the physical SourceEnvelope which justified that translation.
        if resolved_source_ref is not None:
            if discovery is None:
                raise TypeError("aliased Core authority requires discovery")
            body.update({
                "schema_version": (
                    "coverage-mission-acquired-document-binding-0.2"
                ),
                "resolved_source_ref": resolved_source_ref,
                "discovery_ref": row.get("discovery_ref"),
                "discovery_mission_version_ref": discovery.get(
                    "mission_version_ref"
                ),
                "discovery_source_envelope_ref": discovery.get(
                    "source_envelope_ref"
                ),
                "discovery_source_envelope_hash": discovery.get(
                    "source_envelope_hash"
                ),
            })
        return _record(body)

    def _mission_lineage_contains(
        self, *, current_ref: str, ancestor_ref: str,
    ) -> bool:
        """Prove a carried row still descends from its discovery mission."""

        cursor: str | None = current_ref
        mission_ref: str | None = None
        seen: set[str] = set()
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            rows = self.core.connection.execute(
                "SELECT mission_ref,prior_version_id FROM "
                "coverage_mission_versions WHERE mission_version_id=?",
                (cursor,),
            ).fetchall()
            if len(rows) != 1:
                return False
            version = dict(rows[0])
            if mission_ref is None:
                mission_ref = version.get("mission_ref")
            elif version.get("mission_ref") != mission_ref:
                return False
            if cursor == ancestor_ref:
                return True
            cursor = version.get("prior_version_id")
        return False

    def materialize_record(
        self, *, record_id: str,
        acquisition_ticket_ref: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        record_id = _text(record_id, "record_id")
        rows = self.core.connection.execute(
            "SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?",
            (record_id,),
        ).fetchall()
        if len(rows) != 1:
            raise DocumentResearchConflict(
                "Core acquired-document authority is not exact"
            )
        row = dict(rows[0])
        if (
            row.get("status") != "acquired"
            or row.get("ticket_ref") is not None
            and (
                not isinstance(row["ticket_ref"], str)
                or not row["ticket_ref"]
            )
        ):
            raise DocumentResearchConflict(
                "Core row is not a readable acquired document"
            )
        adapter_source_ref = self.source_aliases.get(
            row["source_ref"], row["source_ref"]
        )
        try:
            adapter = self.adapters[adapter_source_ref]
        except KeyError as exc:
            raise DocumentResearchConflict(
                "Core acquired-document source has no configured adapter"
            ) from exc
        row_ticket = row["ticket_ref"]
        if (
            row_ticket is not None
            and acquisition_ticket_ref is not None
            and acquisition_ticket_ref != row_ticket
        ):
            raise DocumentResearchConflict(
                "registered ticket differs from the acquired Core row"
            )
        resolved_ticket = row_ticket or acquisition_ticket_ref
        registration, text = adapter.materialize(
            document_ref=row["document_ref"],
            acquisition_ticket_ref=resolved_ticket,
        )
        if (
            registration.get("source_ref") != adapter_source_ref
            or registration.get("document_ref") != row["document_ref"]
            or not isinstance(registration.get("acquisition_ticket_ref"), str)
            or not registration["acquisition_ticket_ref"]
            or resolved_ticket is not None
            and registration["acquisition_ticket_ref"] != resolved_ticket
        ):
            raise DocumentResearchConflict(
                "Core acquired row and source acquisition authority disagree"
            )
        discovery = None
        if adapter_source_ref != row["source_ref"]:
            discovery_rows = self.core.connection.execute(
                "SELECT * FROM coverage_mission_source_discoveries WHERE record_id=?",
                (row.get("discovery_ref"),),
            ).fetchall()
            if len(discovery_rows) != 1:
                raise DocumentResearchConflict(
                    "Core source alias has no exact discovery authority"
                )
            discovery = dict(discovery_rows[0])
            source_authority = registration.get("source_authority", {})
            if (
                not self._mission_lineage_contains(
                    current_ref=row["mission_version_ref"],
                    ancestor_ref=discovery.get("mission_version_ref"),
                )
                or discovery.get("company_ref") != row["company_ref"]
                or discovery.get("source_ref") != row["source_ref"]
                or discovery.get("source_envelope_ref") != source_authority.get("ref")
                or discovery.get("source_envelope_hash") != source_authority.get("hash")
            ):
                raise DocumentResearchConflict(
                    "Core source alias and discovery envelope authority disagree"
                )
        authority = self._authority(
            row,
            resolved_source_ref=(
                adapter_source_ref
                if adapter_source_ref != row["source_ref"] else None
            ),
            discovery=discovery,
        )
        body = {
            key: item for key, item in registration.items()
            if key not in {"id", "content_hash"}
        }
        body.update({
            "source_ref": row["source_ref"],
            "document_ref": row["document_ref"],
            "source_authority": {
                "kind": "coverage-mission-acquired-document",
                "ref": authority["record_id"],
                "hash": authority["content_hash"],
                "mission_version_ref": authority["mission_version_ref"],
                "company_ref": authority["company_ref"],
            },
        })
        digest = content_hash(body)
        return _record({
            **body, "id": f"registered-document:sha256:{digest}"
        }), text


class CoreAcquiredFetchedDocumentSourceAdapter(
    CoreAcquiredDocumentSourceAdapter
):
    """Compatibility wrapper for the former fetched-only adapter."""

    def __init__(
        self, *, core: Any, launcher: Any, spool: Any, receipt_reader: Any,
        max_source_chars: int, max_pdf_pages: int, max_decompressed_bytes: int,
    ) -> None:
        adapters = {
            source_ref: PublicWebDocumentSourceAdapter(
                source_ref=source_ref, launcher=launcher, core=core, spool=spool,
                receipt_reader=receipt_reader, max_source_chars=max_source_chars,
                max_pdf_pages=max_pdf_pages,
                max_decompressed_bytes=max_decompressed_bytes,
            )
            for source_ref in SUPPORTED_FETCH_DISCOVERY_SOURCE_REFS
        }
        super().__init__(
            core=core, adapters=adapters,
            source_aliases={"source:web-search": "source:public-web"},
        )


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
        "content_document_ref",
        "manifest_ref",
        "connector_invocation_ref", "connector_profile_ref", "access_policy_ref",
        "retention_policy_ref", "terms_policy_ref",
    ):
        _text(wire[name], f"registration {name}")
    for name in ("doc_kind", "evidence_tier", "doc_date", "origin_ref"):
        if wire[name] is not None:
            _text(wire[name], f"registration {name}")
    source_authority = wire["source_authority"]
    if (
        not isinstance(source_authority, Mapping)
        or set(source_authority) != {
            "kind", "ref", "hash", "mission_version_ref", "company_ref"
        }
        or source_authority["kind"] not in {
            "feed-acquisition-manifest",
            "alphaengine-document-acquisition-manifest",
            "discovery-source-envelope",
            "coverage-mission-acquired-document",
        }
    ):
        raise DocumentResearchError("registration source_authority is invalid")
    _text(source_authority["ref"], "source_authority.ref")
    _sha256(source_authority["hash"], "source_authority.hash")
    for name in ("mission_version_ref", "company_ref"):
        if source_authority[name] is not None:
            _text(source_authority[name], f"source_authority.{name}")
    if source_authority["kind"] == "coverage-mission-acquired-document":
        if source_authority["mission_version_ref"] is None \
                or source_authority["company_ref"] is None:
            raise DocumentResearchError(
                "Core acquired-document authority must bind mission and company"
            )
    authority_kind = wire["authority_kind"]
    allowed_source_authorities = {
        "feed-acquisition-manifest": {
            "feed-acquisition-manifest", "coverage-mission-acquired-document"
        },
        "alphaengine-document-acquisition-manifest": {
            "alphaengine-document-acquisition-manifest",
            "coverage-mission-acquired-document",
        },
        "public-web-fetch-manifest": {
            "discovery-source-envelope", "coverage-mission-acquired-document"
        },
    }
    if source_authority["kind"] not in allowed_source_authorities.get(
        authority_kind, set()
    ):
        raise DocumentResearchError(
            "registration source authority does not match its manifest authority"
        )
    if source_authority["kind"] != "coverage-mission-acquired-document" \
            and authority_kind in {
        "feed-acquisition-manifest", "alphaengine-document-acquisition-manifest"
    } and (
        source_authority["ref"] != wire["manifest_ref"]
        or source_authority["hash"] != wire["manifest_hash"]
    ):
        raise DocumentResearchError(
            "registration source authority does not bind its manifest"
        )
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
            "status", "representation", "reason", "artifact_refs",
            "content_hashes", "total_size_bytes"
        }
    ):
        raise DocumentResearchError("registration raw_source is invalid")
    raw_contracts = {
        "feed-acquisition-manifest": (
            ("bound", "original-source-container")
            if wire["source_ref"] == PRIOR_RESEARCH_SOURCE_REF
            else ("not_bound", None)
        ),
        "alphaengine-document-acquisition-manifest": (
            "bound", "paged-connector-responses"
        ),
        "public-web-fetch-manifest": ("bound", "connector-response-body"),
    }
    if authority_kind not in raw_contracts \
            or (raw_source["status"], raw_source["representation"]) != raw_contracts[authority_kind]:
        raise DocumentResearchError("registration raw source representation is unsupported")
    if raw_source["status"] == "not_bound":
        if (
            not isinstance(raw_source["reason"], str) or not raw_source["reason"]
            or raw_source["artifact_refs"] != []
            or raw_source["content_hashes"] != []
            or raw_source["total_size_bytes"] is not None
        ):
            raise DocumentResearchError("registration unbound raw source is invalid")
    else:
        refs, hashes = raw_source["artifact_refs"], raw_source["content_hashes"]
        if (
            raw_source["reason"] is not None
            or not isinstance(refs, list) or not refs
            or any(not isinstance(item, str) or not item for item in refs)
            or not isinstance(hashes, list) or len(hashes) != len(refs)
        ):
            raise DocumentResearchError("registration bound raw source is invalid")
        for item in hashes:
            _sha256(item, "raw_source content hash")
        _positive_integer(raw_source["total_size_bytes"], "raw_source.total_size_bytes")
    normalized = wire["normalized_text"]
    if (
        not isinstance(normalized, Mapping)
        or set(normalized) != {
            "status", "representation", "renderer_ref", "object_hash",
            "size_bytes", "text_sha256", "characters", "truncated",
        }
        or normalized["status"] != "complete"
        or normalized["truncated"] is not False
    ):
        raise DocumentResearchError("registration normalized_text is invalid")
    normalized_contracts = {
        "feed-acquisition-manifest": (
            ("rendered-original-source", True, False)
            if wire["source_ref"] == PRIOR_RESEARCH_SOURCE_REF
            else ("assembled-utf8", False, False)
        ),
        "alphaengine-document-acquisition-manifest": (
            "assembled-utf8", True, False
        ),
        "public-web-fetch-manifest": (
            "deterministic-rendering", True, True
        ),
    }
    representation, needs_renderer, permits_null_object = normalized_contracts[authority_kind]
    if normalized["representation"] != representation:
        raise DocumentResearchError("registration normalized representation is unsupported")
    if needs_renderer != isinstance(normalized["renderer_ref"], str):
        raise DocumentResearchError("registration normalized renderer binding is invalid")
    if normalized["renderer_ref"] is not None:
        _text(normalized["renderer_ref"], "normalized_text.renderer_ref")
    if normalized["object_hash"] is None:
        if not permits_null_object:
            raise DocumentResearchError("registration normalized object hash is missing")
    else:
        _sha256(normalized["object_hash"], "normalized_text.object_hash")
    _sha256(normalized["text_sha256"], "normalized_text.text_sha256")
    _positive_integer(normalized["size_bytes"], "normalized_text.size_bytes")
    _positive_integer(normalized["characters"], "normalized_text.characters")
    reader = wire["reader"]
    if (
        not isinstance(reader, Mapping)
        or set(reader) != {"ref", "config", "content_hash"}
        or not isinstance(reader["config"], Mapping)
        or content_hash({"ref": reader["ref"], "config": reader["config"]})
        != reader["content_hash"]
    ):
        raise DocumentResearchError("registration reader identity is invalid")
    _text(reader["ref"], "reader.ref")
    if wire["source_ref"] == PRIOR_RESEARCH_SOURCE_REF:
        config = reader["config"]
        if (
            reader["ref"] != "document-reader:prior-original-render:0.1"
            or set(config) != {
                "format", "renderer", "preserves", "omits",
                "original_source_bundle_hash",
            }
            or config["renderer"] != normalized["renderer_ref"]
        ):
            raise DocumentResearchError(
                "prior-research reader identity is invalid"
            )
        _text(config["format"], "reader.config.format")
        _text(config["renderer"], "reader.config.renderer")
        _sha256(
            config["original_source_bundle_hash"],
            "reader.config.original_source_bundle_hash",
        )
        for name in ("preserves", "omits"):
            if (
                not isinstance(config[name], list)
                or len(config[name]) != len(set(config[name]))
                or any(not isinstance(item, str) or not item for item in config[name])
            ):
                raise DocumentResearchError(
                    f"prior-research reader {name} are invalid"
                )
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
        self, *, adapters: Mapping[str, Any], policy: Mapping[str, Any],
        acquired_document_adapter: CoreAcquiredDocumentSourceAdapter | None = None,
        acquired_fetched_adapter: CoreAcquiredFetchedDocumentSourceAdapter | None = None,
    ) -> None:
        self.policy = validate_document_research_policy(policy)
        if (
            acquired_document_adapter is not None
            and acquired_fetched_adapter is not None
            and acquired_document_adapter is not acquired_fetched_adapter
        ):
            raise TypeError("only one Core acquired-document adapter may be configured")
        core_adapter = acquired_document_adapter or acquired_fetched_adapter
        if not isinstance(adapters, Mapping) or (
            not adapters and core_adapter is None
        ):
            raise TypeError("at least one document research adapter is required")
        self.adapters = dict(adapters)
        if any(key != adapter.source_ref for key, adapter in self.adapters.items()):
            raise TypeError("adapter map keys must equal adapter source_ref")
        self.acquired_document_adapter = core_adapter
        # Read-only compatibility for callers that inspected the old member.
        self.acquired_fetched_adapter = core_adapter

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

    def inspect(
        self, *, source_ref: str, document_ref: str, purpose: str,
        acquisition_ticket_ref: str | None = None,
    ) -> dict[str, Any]:
        """Describe read availability without manufacturing partial authority."""

        base = {
            "schema_version": "document-research-availability-0.1",
            "source_ref": source_ref,
            "document_ref": document_ref,
            "purpose": purpose,
            "acquisition_ticket_ref": acquisition_ticket_ref,
            "policy_ref": self.policy["policy_ref"],
            "policy_hash": self.policy["content_hash"],
        }
        try:
            registration = self.register(
                source_ref=source_ref,
                document_ref=document_ref,
                purpose=purpose,
                acquisition_ticket_ref=acquisition_ticket_ref,
            )
        except DocumentResearchAccessDenied as exc:
            return _record({
                **base, "available": False, "reason": "access_policy_denied",
                "detail": str(exc), "registration": None,
            })
        except Exception as exc:  # source adapters retain their typed details
            return _record({
                **base, "available": False, "reason": "source_not_readable",
                "detail": f"{type(exc).__name__}: {exc}", "registration": None,
            })
        return _record({
            **base, "available": True, "reason": "complete_verified_text",
            "detail": None, "registration": registration,
        })

    def register_acquired_document(
        self, *, record_id: str, purpose: str
    ) -> dict[str, Any]:
        """Register one exact acquired Core row; source labels come from Core."""

        if self.acquired_document_adapter is None:
            raise DocumentResearchError(
                "Core acquired-document adapter is not configured"
            )
        registration, _ = self.acquired_document_adapter.materialize_record(
            record_id=record_id
        )
        self._authorize(purpose, registration)
        return registration

    def inspect_acquired_document(
        self, *, record_id: str, purpose: str
    ) -> dict[str, Any]:
        """Report whether an exact Core acquisition is complete and readable."""

        base = {
            "schema_version": "document-research-acquired-availability-0.1",
            "record_id": record_id,
            "purpose": purpose,
            "policy_ref": self.policy["policy_ref"],
            "policy_hash": self.policy["content_hash"],
        }
        try:
            registration = self.register_acquired_document(
                record_id=record_id, purpose=purpose
            )
        except DocumentResearchAccessDenied as exc:
            return _record({
                **base, "available": False, "reason": "access_policy_denied",
                "detail": str(exc), "registration": None,
            })
        except Exception as exc:
            return _record({
                **base, "available": False, "reason": "source_not_readable",
                "detail": f"{type(exc).__name__}: {exc}", "registration": None,
            })
        return _record({
            **base, "available": True, "reason": "complete_verified_text",
            "detail": None, "registration": registration,
        })

    def _reresolve(
        self, registration: Mapping[str, Any], *, purpose: Any
    ) -> tuple[dict[str, Any], str]:
        expected = validate_registration(registration)
        if expected["source_authority"]["kind"] == "coverage-mission-acquired-document":
            if self.acquired_document_adapter is None:
                raise DocumentResearchError(
                    "Core acquired-document adapter is not configured"
                )
            actual, text = self.acquired_document_adapter.materialize_record(
                record_id=expected["source_authority"]["ref"],
                acquisition_ticket_ref=expected["acquisition_ticket_ref"],
            )
            if actual != expected:
                raise DocumentResearchConflict("registered document authority changed")
            self._authorize(purpose, actual)
            return actual, text
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
        def term_matches(term_order: int, term: str):
            pattern = re.compile(
                re.escape(term).replace(r"\ ", r"\s+"), re.IGNORECASE
            )
            for match in pattern.finditer(text):
                yield match.start(), term_order, match.end(), term

        candidates = heapq.merge(*(
            term_matches(term_order, term)
            for term_order, term in enumerate(terms)
        ))
        matches: list[dict[str, Any]] = []
        seen_spans: set[tuple[int, int]] = set()
        while len(matches) < limits["max_results"]:
            try:
                match_start, _order, match_end, term = next(candidates)
            except StopIteration:
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


def build_document_research_registry(
    *,
    core: Any,
    state_dir: str | Path,
    spool: Any,
    receipt_reader: Any,
    policy: Mapping[str, Any],
    feed_launchers: Mapping[str, Any],
    alphaengine_launcher: Any | None,
    public_web_launcher: Any | None,
    public_web_source_refs: Sequence[str],
    source_reading_limits: Mapping[str, Any],
) -> DocumentResearchRegistry:
    """Compose production adapters without opening or mutating authority.

    Launchers, the existing spool, and the existing receipt reader are
    injected because their constructors can create directories or schema.
    This factory verifies that every launcher belongs to ``state_dir`` and
    otherwise only builds read-only adapter objects.
    """

    state = Path(state_dir).expanduser().resolve()
    if not isinstance(feed_launchers, Mapping):
        raise TypeError("feed_launchers must be a mapping")
    if not isinstance(source_reading_limits, Mapping) or set(source_reading_limits) != {
        "alphaengine_max_document_chars", "public_web_max_source_chars",
        "public_web_max_pdf_pages", "public_web_max_decompressed_bytes",
    }:
        raise DocumentResearchError("source_reading_limits have an invalid shape")

    def same_state(launcher: Any) -> None:
        launcher_state = getattr(launcher, "state_dir", None)
        if launcher_state is None or Path(launcher_state).expanduser().resolve() != state:
            raise DocumentResearchConflict(
                "document research launcher belongs to a different state directory"
            )

    adapters: dict[str, Any] = {}
    for source_ref, launcher in feed_launchers.items():
        same_state(launcher)
        adapters[source_ref] = FeedDocumentSourceAdapter(
            source_ref=source_ref, launcher=launcher, core=core, spool=spool,
            receipt_reader=receipt_reader,
        )
    if alphaengine_launcher is not None:
        same_state(alphaengine_launcher)
        adapters["source:alphaengine"] = AlphaEngineDocumentSourceAdapter(
            launcher=alphaengine_launcher, core=core, spool=spool,
            receipt_reader=receipt_reader,
            max_document_chars=source_reading_limits[
                "alphaengine_max_document_chars"
            ],
        )
    source_refs = list(public_web_source_refs)
    if len(source_refs) != len(set(source_refs)):
        raise DocumentResearchError("public_web_source_refs must be unique")
    if public_web_launcher is None and source_refs:
        raise DocumentResearchError(
            "public-web source refs require the public-web launcher"
        )
    if public_web_launcher is not None:
        same_state(public_web_launcher)
        for source_ref in source_refs:
            adapters[source_ref] = PublicWebDocumentSourceAdapter(
                source_ref=source_ref, launcher=public_web_launcher, core=core,
                spool=spool, receipt_reader=receipt_reader,
                max_source_chars=source_reading_limits["public_web_max_source_chars"],
                max_pdf_pages=source_reading_limits["public_web_max_pdf_pages"],
                max_decompressed_bytes=source_reading_limits[
                    "public_web_max_decompressed_bytes"
                ],
            )
    core_adapters = dict(adapters)
    if public_web_launcher is not None:
        for source_ref in {"source:public-web", "source:sec-edgar"}:
            if source_ref in core_adapters:
                continue
            core_adapters[source_ref] = PublicWebDocumentSourceAdapter(
                source_ref=source_ref, launcher=public_web_launcher, core=core,
                spool=spool, receipt_reader=receipt_reader,
                max_source_chars=source_reading_limits["public_web_max_source_chars"],
                max_pdf_pages=source_reading_limits["public_web_max_pdf_pages"],
                max_decompressed_bytes=source_reading_limits[
                    "public_web_max_decompressed_bytes"
                ],
            )
    if not core_adapters:
        raise DocumentResearchError("no document research source is configured")
    acquired_document_adapter = None
    connection = getattr(core, "connection", None)
    if connection is not None and callable(getattr(connection, "execute", None)):
        from .source_capability_map import SOURCE_PLAN_ALIASES

        acquired_document_adapter = CoreAcquiredDocumentSourceAdapter(
            core=core, adapters=core_adapters,
            source_aliases={
                key: item for key, item in SOURCE_PLAN_ALIASES.items()
                if item in core_adapters
            },
        )
    return DocumentResearchRegistry(
        adapters=adapters, policy=policy,
        acquired_document_adapter=acquired_document_adapter,
    )


def build_document_research_registry_from_writer(
    writer: Any, *, state_dir: str | Path, policy: Mapping[str, Any],
    public_web_source_refs: Sequence[str], source_reading_limits: Mapping[str, Any],
) -> DocumentResearchRegistry:
    """Bind the factory to the already-open Writer authorities."""

    from .connector_authority_port import ConnectorCompletionReceiptReader

    spool = getattr(writer, "_transcript_spool", None)
    if spool is None:
        raise DocumentResearchError("writer has no configured document spool")
    receipt_reader = ConnectorCompletionReceiptReader(
        connectors=writer._connectors, observability=writer.observability
    )
    feed_launchers = {}
    for source_ref, key in (
        (SALES_NOTES_SOURCE_REF, "sales_notes_feed_launcher"),
        (COMPANY_WIKI_SOURCE_REF, "company_wiki_feed_launcher"),
        (PRIOR_RESEARCH_SOURCE_REF, "prior_research_feed_launcher"),
    ):
        launcher = writer.lane_launcher(key)
        if launcher is not None:
            feed_launchers[source_ref] = launcher
    return build_document_research_registry(
        core=writer.store,
        state_dir=state_dir,
        spool=spool,
        receipt_reader=receipt_reader,
        policy=policy,
        feed_launchers=feed_launchers,
        alphaengine_launcher=getattr(writer, "_acquisition_launcher", None),
        public_web_launcher=getattr(writer, "_web_fetch_launcher", None),
        public_web_source_refs=public_web_source_refs,
        source_reading_limits=source_reading_limits,
    )


__all__ = [
    "DocumentResearchAccessDenied",
    "DocumentResearchConflict",
    "DocumentResearchError",
    "DocumentResearchRegistry",
    "AlphaEngineDocumentSourceAdapter",
    "CoreAcquiredDocumentSourceAdapter",
    "CoreAcquiredFetchedDocumentSourceAdapter",
    "FeedDocumentSourceAdapter",
    "PublicWebDocumentSourceAdapter",
    "READ_OPERATION",
    "READ_REQUEST_SCHEMA_VERSION",
    "SEARCH_OPERATION",
    "SEARCH_REQUEST_SCHEMA_VERSION",
    "build_document_research_policy",
    "build_document_research_registry",
    "build_document_research_registry_from_writer",
    "validate_document_research_policy",
    "validate_registration",
]
