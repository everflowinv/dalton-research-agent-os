"""S1: what a human / vendor feed leaves behind when it acquires one document.

The web and AlphaEngine lanes both end the same way: a child writes the raw
bytes into the content-addressed spool, writes a manifest naming those bytes,
and the extraction path re-reads the bytes and re-hashes them before a single
word is quoted. The manifest shapes differ because their provenance differs --
a fetched page binds a URL and a body hash, an acquired transcript binds a
declared document hash and its pages.

A local feed binds neither a URL nor a provider's declaration. Sales notes and
wiki bind which feed, operation, document and governance record authorised the
read plus the exact normalized text in the spool. Prior-research manifest 0.2
also binds the original file container, its structural/loss description and a
deterministic normalized projection, because a PDF or workbook projection is
not the source file itself.

The connector-authority fields are present and null. When the host-tool runner
lands, a feed acquisition will carry a real invocation and profile the way a
fetch does, and ``verified_feed_source`` will re-read them; until then they are
explicitly absent rather than quietly missing.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from .research_verification import ResearchVerificationConflict, ResearchVerificationError
from .store import canonical_json, content_hash

MANIFEST_SCHEMA_VERSION = "0.1"
PRIOR_MANIFEST_SCHEMA_VERSION = "0.2"

SALES_NOTES_SOURCE_REF = "source:sales-notes"
COMPANY_WIKI_SOURCE_REF = "source:company-wiki"
# W3: the fund's own earlier work on a company, acquired the same way.
PRIOR_RESEARCH_SOURCE_REF = "source:prior-research"
# The mission source-plan keys this manifest may describe. Extraction
# dispatches on exactly this set.
FEED_SOURCE_REFS = frozenset({
    SALES_NOTES_SOURCE_REF, COMPANY_WIKI_SOURCE_REF, PRIOR_RESEARCH_SOURCE_REF,
})

EVIDENCE_TIERS = frozenset({
    "management_statement", "expert", "sell_side", "internal", "unclassified",
    # W3: work this fund did itself, earlier. Distinct from ``internal``,
    # which is the wiki's word for a note somebody took: this one carries an
    # ``as_of`` the owner declared and is subject to a staleness downgrade.
    "internal_prior",
})

_MANIFEST_FIELDS = frozenset({
    "schema_version", "id", "created_at", "source_ref", "operation",
    "document_ref", "target_ref", "governance_ref", "governance_hash",
    "status", "doc_kind", "evidence_tier", "doc_date", "origin_ref",
    "subject_tickers", "content_chars", "declared_content_sha256",
    "assembled_object", "connector_invocation_ref", "connector_invocation_hash",
    "content_hash",
})
_PRIOR_MANIFEST_FIELDS = _MANIFEST_FIELDS | {"original_source_bundle"}
_OBJECT_FIELDS = frozenset({"content_hash", "size_bytes", "storage_locator"})
_ORIGINAL_BUNDLE_FIELDS = frozenset({
    "schema_version", "document_ref", "source_file_sha256", "source_file_bytes",
    "source_object", "structure", "normalized_projection", "content_hash",
})
_PROJECTION_FIELDS = frozenset({
    "format", "renderer", "text_sha256", "text_chars", "complete",
    "preserves", "omits",
})
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9-]*$")
# 64 MB of markdown is not a research document; it is a mistake with a
# manifest attached.
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024


class FeedManifestError(ResearchVerificationError):
    """The feed acquisition manifest is malformed."""


class FeedSourceConflict(ResearchVerificationConflict):
    """The spooled bytes disagree with the manifest that names them."""


def _require(condition: Any, message: str, *, conflict: bool = True) -> None:
    if not condition:
        raise (FeedSourceConflict if conflict else FeedManifestError)(message)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FeedManifestError(f"feed manifest {name} must be non-empty text")
    return value


def _hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise FeedManifestError(f"feed manifest {name} must be SHA-256 hex")
    return value


def _string_list(value: Any, name: str) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) != len(set(value))
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise FeedManifestError(f"feed manifest {name} must be unique text")
    return list(value)


def _validate_original_source_bundle(
    value: Any, *, document_ref: str, declared_text_hash: str,
    declared_text_chars: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _ORIGINAL_BUNDLE_FIELDS:
        raise FeedManifestError("prior-research original source bundle is malformed")
    bundle = json.loads(canonical_json(value))
    if bundle["schema_version"] != "prior-import-artifact-bundle-0.2":
        raise FeedManifestError("unsupported prior-research original source bundle")
    if bundle["document_ref"] != document_ref:
        raise FeedManifestError("prior-research original bundle names another document")
    source_hash = _hash(bundle["source_file_sha256"], "source_file_sha256")
    size = bundle["source_file_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_DOCUMENT_BYTES:
        raise FeedManifestError("prior-research original source size is out of range")
    source_object = bundle["source_object"]
    if not isinstance(source_object, Mapping) or set(source_object) != _OBJECT_FIELDS:
        raise FeedManifestError("prior-research source object is malformed")
    source_object = dict(source_object)
    if _hash(source_object["content_hash"], "source_object.content_hash") != source_hash:
        raise FeedManifestError("prior-research source object hash differs from the file")
    if source_object["size_bytes"] != size:
        raise FeedManifestError("prior-research source object size differs from the file")
    _text(source_object["storage_locator"], "source_object.storage_locator")
    bundle["source_object"] = source_object
    structure = bundle["structure"]
    if not isinstance(structure, Mapping) or not isinstance(
        structure.get("text_projection"), Mapping
    ):
        raise FeedManifestError("prior-research structure has no text projection")
    if structure.get("file_sha256") != source_hash:
        raise FeedManifestError(
            "prior-research structure differs from the original source"
        )
    projection = bundle["normalized_projection"]
    if not isinstance(projection, Mapping) or set(projection) != _PROJECTION_FIELDS:
        raise FeedManifestError("prior-research normalized projection is malformed")
    projection = dict(projection)
    for name in ("format", "renderer"):
        projection[name] = _text(projection[name], f"normalized_projection.{name}")
    if projection["format"] not in {"markdown", "text", "pdf", "docx", "xlsx"}:
        raise FeedManifestError("prior-research normalized format is unsupported")
    _hash(projection["text_sha256"], "normalized_projection.text_sha256")
    if (
        isinstance(projection["text_chars"], bool)
        or not isinstance(projection["text_chars"], int)
        or projection["text_chars"] < 0
    ):
        raise FeedManifestError(
            "prior-research normalized projection text_chars must be a count"
        )
    if projection["text_sha256"] != declared_text_hash \
            or projection["text_chars"] != declared_text_chars:
        raise FeedManifestError(
            "prior-research normalized projection differs from assembled text"
        )
    if type(projection["complete"]) is not bool:
        raise FeedManifestError("prior-research projection completeness must be boolean")
    projection["preserves"] = _string_list(
        projection["preserves"], "normalized_projection.preserves"
    )
    projection["omits"] = _string_list(
        projection["omits"], "normalized_projection.omits"
    )
    structure_projection = structure["text_projection"]
    for name in ("complete", "preserves", "omits"):
        if structure_projection.get(name) != projection[name]:
            raise FeedManifestError(
                "prior-research structure and normalized projection disagree"
            )
    if structure.get("format") != projection["format"]:
        raise FeedManifestError(
            "prior-research structure and projection formats disagree"
        )
    bundle["normalized_projection"] = projection
    asserted_hash = _hash(bundle.pop("content_hash"), "original_source_bundle.content_hash")
    if content_hash(bundle) != asserted_hash:
        raise FeedManifestError("prior-research original source bundle hash is invalid")
    bundle["content_hash"] = asserted_hash
    return bundle


def validate_feed_acquisition_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one closed feed acquisition manifest."""

    if not isinstance(value, Mapping):
        raise FeedManifestError("feed acquisition manifest has an invalid closed shape")
    schema_version = value.get("schema_version")
    fields = (
        _PRIOR_MANIFEST_FIELDS
        if schema_version == PRIOR_MANIFEST_SCHEMA_VERSION
        else _MANIFEST_FIELDS
    )
    if set(value) != fields:
        raise FeedManifestError("feed acquisition manifest has an invalid closed shape")
    wire = json.loads(canonical_json(value))
    if wire["schema_version"] not in {
        MANIFEST_SCHEMA_VERSION, PRIOR_MANIFEST_SCHEMA_VERSION,
    }:
        raise FeedManifestError("unsupported feed acquisition manifest schema_version")
    for name in ("id", "created_at", "operation", "document_ref", "target_ref",
                 "governance_ref", "doc_kind", "origin_ref"):
        wire[name] = _text(wire[name], name)
    if wire["source_ref"] not in FEED_SOURCE_REFS:
        raise FeedManifestError("feed acquisition manifest names an unknown feed source")
    if wire["status"] != "complete":
        # A partial local read is a bug, not a state: the file is either there
        # and readable or the run failed and wrote no manifest at all.
        raise FeedManifestError("feed acquisition manifest status must be complete")
    if wire["evidence_tier"] not in EVIDENCE_TIERS:
        raise FeedManifestError("feed acquisition manifest evidence_tier is not in the vocabulary")
    if _DATE_RE.fullmatch(str(wire["doc_date"])) is None:
        raise FeedManifestError("feed acquisition manifest doc_date must be YYYY-MM-DD")
    tickers = wire["subject_tickers"]
    if not isinstance(tickers, list) or len(set(tickers)) != len(tickers) or any(
        not isinstance(item, str) or _TICKER_RE.fullmatch(item) is None for item in tickers
    ):
        raise FeedManifestError("feed acquisition manifest subject_tickers must be unique tickers")
    wire["governance_hash"] = _hash(wire["governance_hash"], "governance_hash")
    wire["declared_content_sha256"] = _hash(
        wire["declared_content_sha256"], "declared_content_sha256"
    )
    chars = wire["content_chars"]
    if isinstance(chars, bool) or not isinstance(chars, int) or chars < 0:
        raise FeedManifestError("feed acquisition manifest content_chars must be a count")
    obj = wire["assembled_object"]
    if not isinstance(obj, Mapping) or set(obj) != _OBJECT_FIELDS:
        raise FeedManifestError("feed acquisition manifest assembled_object is malformed")
    obj = dict(obj)
    obj["content_hash"] = _hash(obj["content_hash"], "assembled_object.content_hash")
    obj["storage_locator"] = _text(obj["storage_locator"], "assembled_object.storage_locator")
    size = obj["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_DOCUMENT_BYTES:
        raise FeedManifestError("feed acquisition manifest assembled_object size is out of range")
    wire["assembled_object"] = obj
    if wire["schema_version"] == PRIOR_MANIFEST_SCHEMA_VERSION:
        if wire["source_ref"] != PRIOR_RESEARCH_SOURCE_REF:
            raise FeedManifestError(
                "only prior research may carry an original source bundle"
            )
        wire["original_source_bundle"] = _validate_original_source_bundle(
            wire["original_source_bundle"],
            document_ref=wire["document_ref"],
            declared_text_hash=wire["declared_content_sha256"],
            declared_text_chars=wire["content_chars"],
        )
    paired = (wire["connector_invocation_ref"] is None) == (
        wire["connector_invocation_hash"] is None
    )
    if not paired:
        raise FeedManifestError(
            "feed acquisition manifest connector invocation ref and hash are paired"
        )
    if wire["connector_invocation_ref"] is not None:
        wire["connector_invocation_ref"] = _text(
            wire["connector_invocation_ref"], "connector_invocation_ref"
        )
        wire["connector_invocation_hash"] = _hash(
            wire["connector_invocation_hash"], "connector_invocation_hash"
        )
    declared = wire.pop("content_hash")
    if content_hash(wire) != declared:
        raise FeedManifestError("feed acquisition manifest content_hash is invalid")
    wire["content_hash"] = declared
    return wire


def build_feed_acquisition_manifest(
    *,
    created_at: str,
    source_ref: str,
    operation: str,
    document_ref: str,
    target_ref: str,
    governance_ref: str,
    governance_hash: str,
    doc_kind: str,
    evidence_tier: str,
    doc_date: str,
    origin_ref: str,
    subject_tickers: list[str],
    text: str,
    assembled_object: Mapping[str, Any],
    connector_invocation_ref: str | None = None,
    connector_invocation_hash: str | None = None,
    original_source_bundle: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One manifest for one acquired feed document.

    The id is derived from the document and the bytes, so re-acquiring an
    unchanged document produces the same manifest and re-acquiring a changed
    one cannot pass as the same acquisition.
    """

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if original_source_bundle is not None \
            and source_ref != PRIOR_RESEARCH_SOURCE_REF:
        raise FeedManifestError(
            "only prior research may carry an original source bundle"
        )
    base = {
        "schema_version": (
            PRIOR_MANIFEST_SCHEMA_VERSION
            if original_source_bundle is not None else MANIFEST_SCHEMA_VERSION
        ),
        "id": "feed-document-acquisition:" + content_hash(
            {"document_ref": document_ref, "content_sha256": digest}
        ),
        "created_at": created_at,
        "source_ref": source_ref,
        "operation": operation,
        "document_ref": document_ref,
        "target_ref": target_ref,
        "governance_ref": governance_ref,
        "governance_hash": governance_hash,
        "status": "complete",
        "doc_kind": doc_kind,
        "evidence_tier": evidence_tier,
        "doc_date": doc_date,
        "origin_ref": origin_ref,
        "subject_tickers": sorted(dict.fromkeys(subject_tickers)),
        "content_chars": len(text),
        "declared_content_sha256": digest,
        "assembled_object": dict(assembled_object),
        "connector_invocation_ref": connector_invocation_ref,
        "connector_invocation_hash": connector_invocation_hash,
    }
    if original_source_bundle is not None:
        base["original_source_bundle"] = dict(original_source_bundle)
    base["content_hash"] = content_hash(base)
    return validate_feed_acquisition_manifest(base)


def bind_manifest_to_invocation(
    manifest: Mapping[str, Any], *,
    connector_invocation_ref: str, connector_invocation_hash: str,
) -> dict[str, Any]:
    """Bind a child-written manifest to the invocation that ran the child.

    The child writes the manifest because the child is what read the bytes;
    it cannot know the invocation, because the runner registers that around
    it. So the runner's caller closes the loop afterwards, and the manifest
    that lands on disk carries both halves. The content hash is recomputed,
    which is the point: a manifest that named an invocation without rehashing
    would be a manifest whose own hash no longer described it.
    """

    wire = validate_feed_acquisition_manifest(manifest)
    base = {key: value for key, value in wire.items() if key != "content_hash"}
    base["connector_invocation_ref"] = _text(
        connector_invocation_ref, "connector_invocation_ref"
    )
    base["connector_invocation_hash"] = _hash(
        connector_invocation_hash, "connector_invocation_hash"
    )
    base["content_hash"] = content_hash(
        {key: value for key, value in base.items() if key != "content_hash"}
    )
    return validate_feed_acquisition_manifest(base)


def verified_feed_source(
    core: Any, spool: Any, manifest: Mapping[str, Any], receipt_reader: Any = None
) -> tuple[dict[str, Any], str]:
    """Re-read one acquired feed document from the spool and prove it is the same.

    ``core`` is accepted and unused until these acquisitions carry connector
    receipts; the signature matches ``verified_source`` and
    ``verified_public_web_source`` so the extraction dispatch is one branch and
    not a special case with a different shape.
    """

    manifest = validate_feed_acquisition_manifest(manifest)
    declared = manifest["assembled_object"]
    raw = spool.read_object(declared["content_hash"])
    _require(raw is not None, "acquired feed document is not in the spool")
    _require(
        len(raw) == declared["size_bytes"],
        "acquired feed document byte count drifted from its manifest",
    )
    _require(
        hashlib.sha256(raw).hexdigest() == declared["content_hash"],
        "acquired feed document bytes do not hash to the spool object they claim",
    )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FeedSourceConflict("acquired feed document is not UTF-8") from exc
    _require(
        hashlib.sha256(text.encode("utf-8")).hexdigest() == manifest["declared_content_sha256"],
        "acquired feed document text does not match the declared content hash",
    )
    _require(
        len(text) == manifest["content_chars"],
        "acquired feed document length drifted from its manifest",
    )
    if manifest["connector_invocation_ref"] is not None:
        _require(receipt_reader is not None,
                 "feed manifest names a connector invocation but no receipt reader was given")
        invocation = receipt_reader.get_invocation(manifest["connector_invocation_ref"])
        _require(
            invocation is not None
            and invocation["content_hash"] == manifest["connector_invocation_hash"],
            "feed acquisition invocation authority drifted",
        )
    if manifest["schema_version"] == PRIOR_MANIFEST_SCHEMA_VERSION:
        bundle = manifest["original_source_bundle"]
        source = bundle["source_object"]
        original = spool.read_object(source["content_hash"])
        _require(original is not None, "prior-research original source is not in the spool")
        _require(
            len(original) == source["size_bytes"],
            "prior-research original source byte count drifted",
        )
        _require(
            hashlib.sha256(original).hexdigest() == source["content_hash"],
            "prior-research original source hash drifted",
        )
        from .prior_research_core import (
            describe_archived_text_projection,
            render_document_archive,
        )

        rendered, renderer = render_document_archive(
            original, bundle["normalized_projection"]["format"]
        )
        projection = bundle["normalized_projection"]
        _require(
            renderer == projection["renderer"],
            "prior-research normalized renderer drifted",
        )
        _require(
            rendered == text,
            "prior-research original source no longer renders to assembled text",
        )
        expected_semantics = describe_archived_text_projection(
            original, projection["format"], rendered
        )
        _require(
            projection["complete"] is expected_semantics["complete"]
            and projection["preserves"] == expected_semantics["preserves"]
            and projection["omits"] == expected_semantics["omits"],
            "prior-research normalized projection semantics drifted",
        )
    return manifest, text


__all__ = [
    "COMPANY_WIKI_SOURCE_REF",
    "EVIDENCE_TIERS",
    "FEED_SOURCE_REFS",
    "MANIFEST_SCHEMA_VERSION",
    "PRIOR_MANIFEST_SCHEMA_VERSION",
    "MAX_DOCUMENT_BYTES",
    "PRIOR_RESEARCH_SOURCE_REF",
    "SALES_NOTES_SOURCE_REF",
    "FeedManifestError",
    "FeedSourceConflict",
    "bind_manifest_to_invocation",
    "build_feed_acquisition_manifest",
    "validate_feed_acquisition_manifest",
    "verified_feed_source",
]
