"""S1: what a human / vendor feed leaves behind when it acquires one document.

The web and AlphaEngine lanes both end the same way: a child writes the raw
bytes into the content-addressed spool, writes a manifest naming those bytes,
and the extraction path re-reads the bytes and re-hashes them before a single
word is quoted. The manifest shapes differ because their provenance differs --
a fetched page binds a URL and a body hash, an acquired transcript binds a
declared document hash and its pages.

A local feed binds neither a URL nor a provider's declaration. What it can
bind, and what this manifest binds, is: which feed, which operation, which
document, which governance record authorised it, and the sha256 of the exact
text the spool holds. That is enough for the property the review path actually
depends on -- the words quoted are the bytes acquired -- and it does not
pretend to the byte-level source authority that a network transport earns.

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

SALES_NOTES_SOURCE_REF = "source:sales-notes"
COMPANY_WIKI_SOURCE_REF = "source:company-wiki"
# The two mission source-plan keys this manifest may describe. Extraction
# dispatches on exactly this set.
FEED_SOURCE_REFS = frozenset({SALES_NOTES_SOURCE_REF, COMPANY_WIKI_SOURCE_REF})

EVIDENCE_TIERS = frozenset({
    "management_statement", "expert", "sell_side", "internal", "unclassified",
})

_MANIFEST_FIELDS = frozenset({
    "schema_version", "id", "created_at", "source_ref", "operation",
    "document_ref", "target_ref", "governance_ref", "governance_hash",
    "status", "doc_kind", "evidence_tier", "doc_date", "origin_ref",
    "subject_tickers", "content_chars", "declared_content_sha256",
    "assembled_object", "connector_invocation_ref", "connector_invocation_hash",
    "content_hash",
})
_OBJECT_FIELDS = frozenset({"content_hash", "size_bytes", "storage_locator"})
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


def validate_feed_acquisition_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one closed feed acquisition manifest."""

    if not isinstance(value, Mapping) or set(value) != _MANIFEST_FIELDS:
        raise FeedManifestError("feed acquisition manifest has an invalid closed shape")
    wire = json.loads(canonical_json(value))
    if wire["schema_version"] != MANIFEST_SCHEMA_VERSION:
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
) -> dict[str, Any]:
    """One manifest for one acquired feed document.

    The id is derived from the document and the bytes, so re-acquiring an
    unchanged document produces the same manifest and re-acquiring a changed
    one cannot pass as the same acquisition.
    """

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    base = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
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
    base["content_hash"] = content_hash(base)
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
    return manifest, text


__all__ = [
    "COMPANY_WIKI_SOURCE_REF",
    "EVIDENCE_TIERS",
    "FEED_SOURCE_REFS",
    "MANIFEST_SCHEMA_VERSION",
    "MAX_DOCUMENT_BYTES",
    "SALES_NOTES_SOURCE_REF",
    "FeedManifestError",
    "FeedSourceConflict",
    "build_feed_acquisition_manifest",
    "validate_feed_acquisition_manifest",
    "verified_feed_source",
]
