"""Acquiring one Guidepoint excerpt (S2).

Every other source in this codebase acquires a discovered document by calling
the provider a second time: AlphaEngine pages a transcript through
``get_document``, the public-web lane fetches the URL a search cited.  Guidepoint
cannot, and this module is the honest consequence rather than a workaround.

The upstream has no operation that reads a document.  What ``search_library``
returns *is* the document: a Q&A excerpt, whole, with its citation.  So
acquisition here is a **derivation**, not a second call -- it re-reads the
immutable raw bytes of the search that discovered the excerpt, recovers that
one excerpt from them, and writes it into the spool as an object with a
declared hash.  ``provider_calls`` on the manifest is zero, and that is a
statement, not an omission: no Guidepoint quota is spent acquiring what a
search already paid for.

Everything else about the standard path is unchanged, because everything else
about it is what makes a document citable.  The manifest binds the same nine
Core receipts an AlphaEngine page binds (invocation, profile, call spec,
physical attempt, usage, cost, quota settlement, source envelope, raw
artifact).  ``verified_guidepoint_source`` re-reads every one of them by the
ref *and* the hash the manifest names, re-derives the excerpt from the raw
bytes, and refuses if the text it recovers is not the text the manifest
declared.  A prior step's transcription is never trusted; the bytes are.

``document_extraction._document_text`` dispatches on ``review["source_ref"]``
and needs one branch to reach this:

    if review["source_ref"] == GUIDEPOINT_SOURCE_REF:
        _, text = verified_guidepoint_source(
            self.writer.store, self.writer._transcript_spool, manifest, reader)
        return text

``verified_source`` itself is not reusable: it validates an AlphaEngine paged
manifest, and a Guidepoint excerpt has no pages, no offsets and no assembled
document to compare a prefix hash against.  Forcing this shape through that
validator would mean inventing a one-page fiction with a fabricated
``content_chars``, which is the kind of lie a hash cannot catch.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .guidepoint_search import (
    EXCERPT_REF_PREFIX,
    OPERATION,
    QUOTE_POLICY,
    SOURCE_REF,
    guidepoint_excerpts_from_raw_response,
)
from .live_mcp_connector import OPENCLAW_GUIDEPOINT_BRIDGE_REF
from .raw_spool import RawObject
from .store import canonical_json, content_hash


MANIFEST_SCHEMA_VERSION = "0.1"
GUIDEPOINT_SOURCE_REF = SOURCE_REF
MAX_EXCERPT_CHARS = 20_000

_MANIFEST_FIELDS = frozenset({
    "schema_version", "id", "created_at", "document_ref", "source_ref",
    "bridge_ref", "operation", "status", "provider_calls",
    "connector_invocation_ref", "connector_invocation_hash",
    "connector_profile_ref", "connector_profile_hash",
    "call_spec_ref", "call_spec_hash",
    "physical_attempt_ref", "physical_attempt_hash",
    "usage_entry_ref", "usage_entry_hash",
    "cost_entry_ref", "cost_entry_hash",
    "quota_settlement_ref", "quota_settlement_hash",
    "source_envelope_ref", "source_envelope_hash",
    "raw_artifact_version_ref", "raw_response_hash", "raw_response_bytes",
    "excerpt_ordinal", "excerpt_object", "content_chars",
    "declared_content_sha256", "transcript_name", "excerpt_date",
    "respondent", "reference_url", "source_attribution_markdown",
    "quote_policy", "content_hash",
})
_OBJECT_FIELDS = frozenset({"content_hash", "size_bytes", "storage_locator"})
_HASH_LENGTH = 64


class GuidepointAcquisitionError(RuntimeError):
    """The acquisition plan, manifest or its Core bindings are invalid."""


class GuidepointAcquisitionConflict(GuidepointAcquisitionError):
    """Core no longer holds the authority this manifest names."""


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise GuidepointAcquisitionConflict(message)


def _hash_field(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != _HASH_LENGTH:
        raise GuidepointAcquisitionError(f"{name} must be SHA-256 hex")
    return value


def _one_row(connection: Any, query: str, params: tuple[Any, ...], label: str) -> Any:
    rows = connection.execute(query, params).fetchall()
    _require(len(rows) == 1, f"Guidepoint excerpt {label} authority is not exact")
    return rows[0]


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------
def validate_guidepoint_excerpt_acquisition_manifest(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Closed shape check for one acquired Guidepoint excerpt."""

    if not isinstance(value, Mapping) or set(value) != _MANIFEST_FIELDS:
        raise GuidepointAcquisitionError(
            "Guidepoint excerpt acquisition manifest has an invalid closed shape"
        )
    wire = json.loads(canonical_json(value))
    if wire["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise GuidepointAcquisitionError("unsupported manifest schema_version")
    if wire["source_ref"] != GUIDEPOINT_SOURCE_REF or wire["operation"] != OPERATION:
        raise GuidepointAcquisitionError("manifest is not a Guidepoint search_library excerpt")
    if wire["bridge_ref"] != OPENCLAW_GUIDEPOINT_BRIDGE_REF:
        raise GuidepointAcquisitionError("manifest bridge is not the frozen Guidepoint bridge")
    if wire["status"] != "complete":
        raise GuidepointAcquisitionError("a Guidepoint excerpt is acquired whole or not at all")
    if wire["provider_calls"] != 0:
        # Not cosmetic: a non-zero count here would claim a Guidepoint call
        # nobody made, and the quota ledger would be reconciling against a
        # number this lane invented.
        raise GuidepointAcquisitionError(
            "acquiring a Guidepoint excerpt spends no provider call"
        )
    if not isinstance(wire["document_ref"], str) or not wire["document_ref"].startswith(
        EXCERPT_REF_PREFIX
    ):
        raise GuidepointAcquisitionError("manifest document_ref is not a guidepoint-excerpt ref")
    for name in (
        "connector_invocation_hash", "connector_profile_hash", "call_spec_hash",
        "physical_attempt_hash", "usage_entry_hash", "cost_entry_hash",
        "quota_settlement_hash", "source_envelope_hash", "raw_response_hash",
        "declared_content_sha256", "content_hash",
    ):
        _hash_field(wire[name], name)
    for name in (
        "connector_invocation_ref", "connector_profile_ref", "call_spec_ref",
        "physical_attempt_ref", "usage_entry_ref", "cost_entry_ref",
        "quota_settlement_ref", "source_envelope_ref", "raw_artifact_version_ref",
        "id", "created_at", "transcript_name", "excerpt_date", "respondent",
    ):
        if not isinstance(wire[name], str) or not wire[name]:
            raise GuidepointAcquisitionError(f"manifest {name} must be non-empty text")
    for name in ("raw_response_bytes", "content_chars", "excerpt_ordinal"):
        item = wire[name]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise GuidepointAcquisitionError(f"manifest {name} must be a non-negative integer")
    if wire["content_chars"] > MAX_EXCERPT_CHARS or wire["content_chars"] < 1:
        raise GuidepointAcquisitionError("manifest content_chars is outside the bounded window")
    obj = wire["excerpt_object"]
    if not isinstance(obj, Mapping) or set(obj) != _OBJECT_FIELDS:
        raise GuidepointAcquisitionError("manifest excerpt_object has an invalid closed shape")
    _hash_field(obj["content_hash"], "excerpt_object.content_hash")
    if obj["content_hash"] != wire["declared_content_sha256"]:
        raise GuidepointAcquisitionError(
            "manifest excerpt object does not carry the declared content hash"
        )
    if not isinstance(obj["size_bytes"], int) or obj["size_bytes"] < 1:
        raise GuidepointAcquisitionError("manifest excerpt_object size_bytes must be positive")
    for name in ("reference_url", "source_attribution_markdown"):
        item = wire[name]
        if item is not None and (not isinstance(item, str) or not item):
            raise GuidepointAcquisitionError(f"manifest {name} must be null or non-empty text")
    policy = wire["quote_policy"]
    if not isinstance(policy, Mapping) or set(policy) != {"max_verbatim_words"}:
        raise GuidepointAcquisitionError("manifest quote_policy has an invalid closed shape")
    ceiling = policy["max_verbatim_words"]
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 1:
        raise GuidepointAcquisitionError("manifest quote_policy ceiling must be a positive integer")
    if ceiling > int(QUOTE_POLICY["max_verbatim_words"]):
        # The manifest may be stricter than the licence; it may never be looser.
        raise GuidepointAcquisitionError(
            "manifest quote_policy is looser than the Guidepoint verbatim licence"
        )
    declared = wire.pop("content_hash")
    if content_hash(wire) != declared:
        raise GuidepointAcquisitionError("manifest content_hash does not bind its content")
    wire["content_hash"] = declared
    return wire


# ---------------------------------------------------------------------------
# acquisition
# ---------------------------------------------------------------------------
def _spool_object(spool: Any, data: bytes, *, sink_key: Mapping[str, Any]) -> RawObject:
    digest = hashlib.sha256(data).hexdigest()
    if spool.object_exists(digest):
        existing = spool.read_object(digest)
        if existing != data:
            raise GuidepointAcquisitionError("Guidepoint excerpt object hash collision")
        return RawObject(
            content_hash=digest,
            size_bytes=len(data),
            storage_locator=f"spool:objects/{digest[:2]}/{digest}",
        )
    sink_ref = "raw-sink:" + content_hash(dict(sink_key))
    sink = spool.open_sink(sink_ref, max_response_bytes=max(1, len(data)))
    try:
        sink.write(data)
        return sink.finalize()
    except Exception:
        sink.abort()
        raise


def acquire_guidepoint_excerpt(
    *,
    core: Any,
    spool: Any,
    receipt_reader: Any,
    source_envelope_ref: str,
    document_ref: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Acquire one discovered excerpt from the search that discovered it.

    The only inputs are a source envelope Core already holds and the excerpt
    ref the discovery ledger queued.  Everything else -- the receipts, the
    bytes, the text -- is read back out of Core, so an acquisition cannot
    assert a document the search did not return.
    """

    if not isinstance(document_ref, str) or not document_ref.startswith(EXCERPT_REF_PREFIX):
        raise GuidepointAcquisitionError("document_ref is not a guidepoint-excerpt ref")
    connection = core.connection

    envelope = receipt_reader.get_source_envelope(source_envelope_ref)
    _require(envelope is not None, "Guidepoint source envelope was not found")
    _require(
        envelope["source"] == GUIDEPOINT_SOURCE_REF
        and envelope["operation"] == OPERATION
        and envelope["status"] in {"complete", "partial"},
        "source envelope is not a completed Guidepoint search",
    )
    refs = list(envelope["source_record_refs"])
    if document_ref not in refs:
        raise GuidepointAcquisitionError(
            "the source envelope did not return this excerpt"
        )
    ordinal = refs.index(document_ref) + 1

    invocation = receipt_reader.get_invocation(envelope["connector_invocation_ref"])
    _require(invocation is not None, "Guidepoint invocation authority was not found")
    profile = receipt_reader.get_profile(invocation["connector_profile_ref"])
    _require(
        profile is not None
        and profile["content_hash"] == invocation["connector_profile_hash"]
        and profile["source_identity"]["source_ref"] == GUIDEPOINT_SOURCE_REF
        and profile["allowed_operations"] == [OPERATION],
        "Guidepoint profile is not the search_library route",
    )
    call = receipt_reader.get_call_spec(invocation["call_spec_ref"])
    _require(
        call is not None
        and call["content_hash"] == invocation["call_spec_hash"]
        and call["operation"] == OPERATION,
        "Guidepoint call spec authority drifted",
    )
    attempt_row = _one_row(
        connection,
        "SELECT physical_attempt_id,reservation_ref,outcome,content_hash FROM "
        "connector_physical_attempts WHERE connector_invocation_ref=? AND outcome='succeeded'",
        (invocation["id"],),
        "physical attempt",
    )
    attempt = receipt_reader.get_physical_attempt(attempt_row["physical_attempt_id"])
    _require(
        attempt is not None and attempt["content_hash"] == attempt_row["content_hash"],
        "Guidepoint physical attempt authority drifted",
    )
    usage_row = _one_row(
        connection,
        "SELECT usage_entry_id,content_hash FROM connector_usage_entries "
        "WHERE physical_attempt_ref=? AND NOT EXISTS (SELECT 1 FROM connector_usage_entries n "
        "WHERE n.correction_of_ref=connector_usage_entries.usage_entry_id)",
        (attempt_row["physical_attempt_id"],),
        "usage entry",
    )
    usage = receipt_reader.get_usage_entry(usage_row["usage_entry_id"])
    _require(
        usage is not None and usage["content_hash"] == usage_row["content_hash"],
        "Guidepoint usage authority drifted",
    )
    cost_row = _one_row(
        connection,
        "SELECT cost_entry_id,content_hash FROM connector_cost_entries "
        "WHERE usage_entry_ref=? AND NOT EXISTS (SELECT 1 FROM connector_cost_entries n "
        "WHERE n.correction_of_ref=connector_cost_entries.cost_entry_id)",
        (usage_row["usage_entry_id"],),
        "cost entry",
    )
    cost = receipt_reader.get_cost_entry(cost_row["cost_entry_id"])
    _require(
        cost is not None and cost["content_hash"] == cost_row["content_hash"],
        "Guidepoint cost authority drifted",
    )
    settlement_row = _one_row(
        connection,
        "SELECT settlement_id,state,content_hash FROM connector_quota_settlements "
        "WHERE reservation_ref=? AND NOT EXISTS (SELECT 1 FROM connector_quota_settlements n "
        "WHERE n.correction_of_ref=connector_quota_settlements.settlement_id)",
        (attempt_row["reservation_ref"],),
        "quota settlement",
    )
    settlement = receipt_reader.get_quota_settlement(settlement_row["settlement_id"])
    _require(
        settlement is not None
        and settlement["content_hash"] == settlement_row["content_hash"]
        and settlement["state"] == "consumed",
        "Guidepoint quota settlement is not a consumed reservation",
    )
    artifact = receipt_reader.get_artifact_version(envelope["raw_artifact_version_ref"])
    _require(
        artifact is not None
        and artifact["artifact_content_hash"] == envelope["raw_response_hash"],
        "Guidepoint raw artifact authority drifted",
    )

    raw = spool.read_object(envelope["raw_response_hash"])
    _require(
        hashlib.sha256(raw).hexdigest() == envelope["raw_response_hash"]
        and len(raw) == artifact["size_bytes"],
        "Guidepoint raw bytes differ from the recorded artifact",
    )
    excerpts = guidepoint_excerpts_from_raw_response(raw)
    _require(
        [item["excerpt_ref"] for item in excerpts] == refs,
        "Guidepoint raw artifact does not reproduce the envelope's excerpt refs",
    )
    excerpt = excerpts[ordinal - 1]
    text = excerpt["excerpt_text"]
    data = text.encode("utf-8")
    obj = _spool_object(
        spool, data,
        sink_key={"excerpt_ref": document_ref, "raw_response_hash": envelope["raw_response_hash"]},
    )

    base = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "id": "guidepoint-excerpt-acquisition:" + content_hash(
            {
                "document_ref": document_ref,
                "source_envelope_ref": envelope["id"],
                "source_envelope_hash": envelope["content_hash"],
            }
        ),
        # The moment the search happened, not the moment somebody derived
        # from it. A derivation has no independent time, and taking one from
        # the wall clock would make the same excerpt hash differently on every
        # replay -- which is exactly the property a content hash is for.
        "created_at": created_at or invocation["created_at"],
        "document_ref": document_ref,
        "source_ref": GUIDEPOINT_SOURCE_REF,
        "bridge_ref": OPENCLAW_GUIDEPOINT_BRIDGE_REF,
        "operation": OPERATION,
        "status": "complete",
        # Zero, and the validator enforces it: the search already paid.
        "provider_calls": 0,
        "connector_invocation_ref": invocation["id"],
        "connector_invocation_hash": invocation["content_hash"],
        "connector_profile_ref": profile["id"],
        "connector_profile_hash": profile["content_hash"],
        "call_spec_ref": call["id"],
        "call_spec_hash": call["content_hash"],
        "physical_attempt_ref": attempt["id"],
        "physical_attempt_hash": attempt["content_hash"],
        "usage_entry_ref": usage["id"],
        "usage_entry_hash": usage["content_hash"],
        "cost_entry_ref": cost["id"],
        "cost_entry_hash": cost["content_hash"],
        "quota_settlement_ref": settlement["id"],
        "quota_settlement_hash": settlement["content_hash"],
        "source_envelope_ref": envelope["id"],
        "source_envelope_hash": envelope["content_hash"],
        "raw_artifact_version_ref": artifact["id"],
        "raw_response_hash": envelope["raw_response_hash"],
        "raw_response_bytes": len(raw),
        "excerpt_ordinal": ordinal,
        "excerpt_object": obj.to_dict(),
        "content_chars": len(text),
        "declared_content_sha256": excerpt["excerpt_sha256"],
        "transcript_name": excerpt["transcript_name"],
        "excerpt_date": excerpt["date"],
        "respondent": excerpt["respondent"],
        "reference_url": excerpt["reference_url"],
        "source_attribution_markdown": excerpt["source_attribution_markdown"],
        "quote_policy": dict(QUOTE_POLICY),
    }
    return validate_guidepoint_excerpt_acquisition_manifest(
        {**base, "content_hash": content_hash(base)}
    )


# ---------------------------------------------------------------------------
# extraction's entry point
# ---------------------------------------------------------------------------
def verified_guidepoint_source(
    core: Any, spool: Any, manifest: Mapping[str, Any], receipt_reader: Any
) -> tuple[dict[str, Any], str]:
    """Re-read every receipt and the raw bytes, and return the exact excerpt.

    The twin of ``document_extraction.verified_source`` and
    ``public_web_extraction_source.verified_public_web_source``, and the same
    discipline: nothing the manifest asserts is believed, everything is read
    back by ref and hash, and the text is re-derived from the raw JSON-RPC
    bytes rather than lifted from the object the acquisition wrote.  The
    stored object is then compared against that derivation, so a tampered
    spool object fails here rather than becoming a quotation.
    """

    manifest = validate_guidepoint_excerpt_acquisition_manifest(manifest)
    connection = core.connection

    invocation = receipt_reader.get_invocation(manifest["connector_invocation_ref"])
    _require(
        invocation is not None
        and invocation["content_hash"] == manifest["connector_invocation_hash"],
        "Guidepoint excerpt invocation authority drifted",
    )
    _require(
        invocation["connector_profile_ref"] == manifest["connector_profile_ref"]
        and invocation["connector_profile_hash"] == manifest["connector_profile_hash"]
        and invocation["call_spec_ref"] == manifest["call_spec_ref"]
        and invocation["call_spec_hash"] == manifest["call_spec_hash"],
        "Guidepoint excerpt invocation does not bind the manifest profile and call",
    )
    profile = receipt_reader.get_profile(manifest["connector_profile_ref"])
    _require(
        profile is not None
        and profile["content_hash"] == manifest["connector_profile_hash"]
        and profile["source_identity"]["source_ref"] == GUIDEPOINT_SOURCE_REF
        and profile["auth_mode"] == "mcp_managed"
        and profile["allowed_operations"] == [OPERATION],
        "Guidepoint excerpt profile is not the governed search route",
    )
    call = receipt_reader.get_call_spec(manifest["call_spec_ref"])
    _require(
        call is not None
        and call["content_hash"] == manifest["call_spec_hash"]
        and call["operation"] == OPERATION,
        "Guidepoint excerpt call spec authority drifted",
    )
    for label, method, ref_key, hash_key in (
        ("physical attempt", "get_physical_attempt",
         "physical_attempt_ref", "physical_attempt_hash"),
        ("usage", "get_usage_entry", "usage_entry_ref", "usage_entry_hash"),
        ("cost", "get_cost_entry", "cost_entry_ref", "cost_entry_hash"),
        ("quota settlement", "get_quota_settlement",
         "quota_settlement_ref", "quota_settlement_hash"),
    ):
        record = getattr(receipt_reader, method)(manifest[ref_key])
        _require(
            record is not None and record["content_hash"] == manifest[hash_key],
            f"Guidepoint excerpt {label} authority drifted",
        )
        if label == "physical attempt":
            _require(
                record["outcome"] == "succeeded"
                and record["connector_invocation_ref"] == invocation["id"],
                "Guidepoint excerpt attempt does not bind the manifest invocation",
            )
        if label == "quota settlement":
            _require(record["state"] == "consumed",
                     "Guidepoint excerpt quota settlement is not consumed")

    envelope = receipt_reader.get_source_envelope(manifest["source_envelope_ref"])
    _require(
        envelope is not None
        and envelope["content_hash"] == manifest["source_envelope_hash"],
        "Guidepoint excerpt source envelope authority drifted",
    )
    _require(
        envelope["connector_invocation_ref"] == invocation["id"]
        and envelope["source"] == GUIDEPOINT_SOURCE_REF
        and envelope["operation"] == OPERATION
        and envelope["status"] in {"complete", "partial"}
        and envelope["raw_response_hash"] == manifest["raw_response_hash"]
        and envelope["raw_artifact_version_ref"] == manifest["raw_artifact_version_ref"],
        "Guidepoint excerpt envelope does not bind the manifest record",
    )
    refs = list(envelope["source_record_refs"])
    _require(
        1 <= manifest["excerpt_ordinal"] <= len(refs)
        and refs[manifest["excerpt_ordinal"] - 1] == manifest["document_ref"],
        "Guidepoint excerpt ordinal does not name the manifest document",
    )
    artifact = receipt_reader.get_artifact_version(manifest["raw_artifact_version_ref"])
    _require(
        artifact is not None
        and artifact["artifact_content_hash"] == manifest["raw_response_hash"]
        and artifact["size_bytes"] == manifest["raw_response_bytes"],
        "Guidepoint excerpt raw artifact authority drifted",
    )
    # A sanity check that the attempt is the one Core recorded for this
    # invocation, read from the table rather than from the manifest.
    _one_row(
        connection,
        "SELECT physical_attempt_id FROM connector_physical_attempts "
        "WHERE connector_invocation_ref=? AND outcome='succeeded'",
        (invocation["id"],),
        "physical attempt",
    )

    raw = spool.read_object(manifest["raw_response_hash"])
    _require(
        hashlib.sha256(raw).hexdigest() == manifest["raw_response_hash"]
        and len(raw) == manifest["raw_response_bytes"],
        "Guidepoint excerpt raw bytes differ from the recorded artifact",
    )
    excerpts = guidepoint_excerpts_from_raw_response(raw)
    _require(
        [item["excerpt_ref"] for item in excerpts] == refs,
        "Guidepoint raw artifact does not reproduce the envelope's excerpt refs",
    )
    excerpt = excerpts[manifest["excerpt_ordinal"] - 1]
    text = excerpt["excerpt_text"]
    _require(
        excerpt["excerpt_ref"] == manifest["document_ref"]
        and excerpt["excerpt_sha256"] == manifest["declared_content_sha256"]
        and len(text) == manifest["content_chars"]
        and excerpt["transcript_name"] == manifest["transcript_name"]
        and excerpt["date"] == manifest["excerpt_date"]
        and excerpt["respondent"] == manifest["respondent"],
        "Guidepoint excerpt differs from the raw response it claims to come from",
    )
    stored = spool.read_object(manifest["excerpt_object"]["content_hash"])
    _require(
        stored == text.encode("utf-8")
        and len(stored) == manifest["excerpt_object"]["size_bytes"],
        "stored Guidepoint excerpt differs from the raw response",
    )
    return manifest, text


__all__ = [
    "GUIDEPOINT_SOURCE_REF",
    "GuidepointAcquisitionConflict",
    "GuidepointAcquisitionError",
    "MANIFEST_SCHEMA_VERSION",
    "acquire_guidepoint_excerpt",
    "validate_guidepoint_excerpt_acquisition_manifest",
    "verified_guidepoint_source",
]
