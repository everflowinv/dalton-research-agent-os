"""P13aq: a citation authority for the four acquired sources that had none.

Reading a document and being able to publish a Claim from it are two different
permissions, and until now the second one existed for exactly two sources.  An
AlphaEngine transcript reaches a Claim because its acquisition manifest binds
nine Core receipts and the candidate chain re-derives every one of them; a
fetched public-web page reaches one because ADR-0005 / P9d-17c gave it the same
chain over a fetch manifest.  Guidepoint excerpts, sell-side sales notes,
company-wiki pages and this fund's own prior research were read, quoted,
drafted from -- and then dismissed with a reason, because nothing downstream
knew what a citation of one of them *was*.

Live on 2026-09-18 that was 148 documents in the Hyperscaler workspace and 388
in legacy: the majority of the research material in the building, read at cost
and contributing nothing.

This module is the one place that says what each of those four sources is, so
that the correction authority, the candidate resolver, the auto-commit rule and
the Ledger writer all read the same table rather than four copies of it:

``source_ref``
    the mission source-plan key, and the ``source`` of the Core SourceEnvelope
``operation``
    the connector operation that produced the bytes (``get_note``,
    ``get_document``, ``search_library``)
``document_prefix``
    what a document of this source is called, so a correction set bound to one
    source can never be read as another's
``manifest_prefix``
    which acquisition manifest family re-verifies it
``evidence_source_type``
    what the cited original *is*, in candidate Evidence terms; this is the
    value ``claim_index_tagging`` ranks the resulting Claim by
``record_binding``
    ``exact`` when the envelope named this one document (the three local
    corpora fetch one document per call) and ``member`` when it named a page of
    them (a Guidepoint search returns twenty excerpts and the acquisition
    derives one of them from those exact bytes)

Nothing here trusts a manifest.  ``verified_acquired_source`` hands straight
off to the source's own ``verified_*``, which re-reads the content-addressed
object out of the spool and re-hashes it; a missing object, a drifted hash or
a manifest that names another source is a refusal, never an approximation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from .feed_acquisition import (
    COMPANY_WIKI_SOURCE_REF,
    PRIOR_RESEARCH_SOURCE_REF,
    SALES_NOTES_SOURCE_REF,
)
from .guidepoint_acquisition import GUIDEPOINT_SOURCE_REF
from .research_verification import (
    ResearchVerificationConflict,
    ResearchVerificationError,
)

#: What a feed child's manifest id starts with.  One family for the three local
#: corpora, because they share one manifest contract (``feed_acquisition``).
FEED_MANIFEST_PREFIX = "feed-document-acquisition:"
#: And one for a Guidepoint excerpt derived from the search that returned it.
GUIDEPOINT_MANIFEST_PREFIX = "guidepoint-excerpt-acquisition:"

#: The evidence source types this module introduces.  Closed, named one per
#: source rather than lumped into one "other corpus" bucket, because the whole
#: point of the type is that a reader of one Claim is told what it is a claim
#: of -- an expert call, a broker's note, our own wiki, our own earlier work.
EXPERT_NETWORK_EVIDENCE_SOURCE_TYPE = "expert_network"
SELL_SIDE_NOTE_EVIDENCE_SOURCE_TYPE = "sell_side_note"
INTERNAL_WIKI_EVIDENCE_SOURCE_TYPE = "internal_wiki"
INTERNAL_PRIOR_RESEARCH_EVIDENCE_SOURCE_TYPE = "internal_prior_research"

ACQUIRED_SOURCE_KINDS: Mapping[str, Mapping[str, Any]] = {
    "guidepoint": {
        "source_ref": GUIDEPOINT_SOURCE_REF,
        "operation": "search_library",
        "document_prefix": "guidepoint-excerpt:sha256:",
        "manifest_prefix": GUIDEPOINT_MANIFEST_PREFIX,
        "evidence_source_type": EXPERT_NETWORK_EVIDENCE_SOURCE_TYPE,
        "record_binding": "member",
        "material_prefix": "source-material:guidepoint-core:",
        "bundle_prefix": "verification-bundle:guidepoint-core-source:",
        "candidate_prefix": "guidepoint",
    },
    "sales_notes": {
        "source_ref": SALES_NOTES_SOURCE_REF,
        "operation": "get_note",
        "document_prefix": "sales-note:",
        "manifest_prefix": FEED_MANIFEST_PREFIX,
        "evidence_source_type": SELL_SIDE_NOTE_EVIDENCE_SOURCE_TYPE,
        "record_binding": "exact",
        "material_prefix": "source-material:sales-notes-core:",
        "bundle_prefix": "verification-bundle:sales-notes-core-source:",
        "candidate_prefix": "sales-note",
    },
    "company_wiki": {
        "source_ref": COMPANY_WIKI_SOURCE_REF,
        "operation": "get_document",
        "document_prefix": "company-wiki-doc:sha256:",
        "manifest_prefix": FEED_MANIFEST_PREFIX,
        "evidence_source_type": INTERNAL_WIKI_EVIDENCE_SOURCE_TYPE,
        "record_binding": "exact",
        "material_prefix": "source-material:company-wiki-core:",
        "bundle_prefix": "verification-bundle:company-wiki-core-source:",
        "candidate_prefix": "company-wiki",
    },
    "prior_research": {
        "source_ref": PRIOR_RESEARCH_SOURCE_REF,
        "operation": "get_document",
        "document_prefix": "prior-research-doc:sha256:",
        "manifest_prefix": FEED_MANIFEST_PREFIX,
        "evidence_source_type": INTERNAL_PRIOR_RESEARCH_EVIDENCE_SOURCE_TYPE,
        "record_binding": "exact",
        "material_prefix": "source-material:prior-research-core:",
        "bundle_prefix": "verification-bundle:prior-research-core-source:",
        "candidate_prefix": "prior-research",
    },
}

#: ``source_ref`` -> staging source kind, for the caller that has a review row.
ACQUIRED_SOURCE_KIND_BY_SOURCE_REF: Mapping[str, str] = {
    entry["source_ref"]: kind for kind, entry in ACQUIRED_SOURCE_KINDS.items()
}
#: Evidence source type -> the same entry, for the Ledger writer and the
#: auto-commit rule, which start from the candidate rather than the review.
ACQUIRED_SOURCE_KIND_BY_EVIDENCE_TYPE: Mapping[str, Mapping[str, Any]] = {
    entry["evidence_source_type"]: entry for entry in ACQUIRED_SOURCE_KINDS.values()
}
ACQUIRED_EVIDENCE_SOURCE_TYPES = frozenset(ACQUIRED_SOURCE_KIND_BY_EVIDENCE_TYPE)
ACQUIRED_MANIFEST_PREFIXES = (FEED_MANIFEST_PREFIX, GUIDEPOINT_MANIFEST_PREFIX)


class AcquiredSourceAuthorityError(ResearchVerificationError):
    """The acquisition this citation rests on is not exact closed authority."""


def acquired_source_kind(source_ref: Any) -> str | None:
    """The staging source kind for a mission source-plan key, or ``None``."""

    if not isinstance(source_ref, str):
        return None
    return ACQUIRED_SOURCE_KIND_BY_SOURCE_REF.get(source_ref)


def is_acquired_source_manifest_ref(value: Any) -> bool:
    """Whether this manifest ref belongs to one of the two families here."""

    return isinstance(value, str) and value.startswith(ACQUIRED_MANIFEST_PREFIXES)


def acquired_kind_for_manifest_ref(value: Any, *, source_ref: Any = None) -> Mapping[str, Any] | None:
    """The kind entry a manifest ref belongs to, disambiguated by source.

    The three local corpora share one manifest family, so the ref alone does
    not say which of them wrote it; the manifest's own ``source_ref`` does, and
    is validated against this table before anything is read.
    """

    if not is_acquired_source_manifest_ref(value):
        return None
    kind = acquired_source_kind(source_ref)
    if kind is None:
        return None
    entry = ACQUIRED_SOURCE_KINDS[kind]
    return entry if value.startswith(entry["manifest_prefix"]) else None


def validate_acquired_source_manifest(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
    """Validate one acquisition manifest and return it with its kind entry.

    Refused rather than guessed at when the manifest family and the source it
    declares disagree: that pairing is the only thing that decides which
    verifier re-reads the bytes, and reading a wiki page through the Guidepoint
    verifier would be reading a document nobody acquired.
    """

    if not isinstance(manifest, Mapping):
        raise AcquiredSourceAuthorityError("acquisition manifest has an invalid closed shape")
    ref = manifest.get("id")
    source_ref = manifest.get("source_ref")
    entry = acquired_kind_for_manifest_ref(ref, source_ref=source_ref)
    if entry is None:
        raise AcquiredSourceAuthorityError(
            "acquisition manifest is not one of the acquired-source families "
            "this citation authority covers"
        )
    if entry["manifest_prefix"] == GUIDEPOINT_MANIFEST_PREFIX:
        from .guidepoint_acquisition import (
            validate_guidepoint_excerpt_acquisition_manifest,
        )

        wire = validate_guidepoint_excerpt_acquisition_manifest(manifest)
    else:
        from .feed_acquisition import validate_feed_acquisition_manifest

        wire = validate_feed_acquisition_manifest(manifest)
        if wire["source_ref"] != entry["source_ref"]:
            raise AcquiredSourceAuthorityError(
                "acquisition manifest belongs to a different feed source"
            )
        if wire["operation"] != entry["operation"]:
            raise AcquiredSourceAuthorityError(
                "acquisition manifest names an operation this source does not read with"
            )
    if not str(wire["document_ref"]).startswith(entry["document_prefix"]):
        raise AcquiredSourceAuthorityError(
            "acquisition manifest document_ref is not a document of this source"
        )
    return wire, entry


def verified_acquired_source(
    core: Any, spool: Any, manifest: Mapping[str, Any], receipt_reader: Any = None,
) -> tuple[dict[str, Any], str, Mapping[str, Any]]:
    """The exact text this acquisition recorded, re-read and re-hashed here.

    One dispatch over the two manifest families, ending in the source's own
    ``verified_*``.  Returns the validated manifest, its text and the kind
    entry, so a caller never has to decide which of those three it is holding.
    """

    wire, entry = validate_acquired_source_manifest(manifest)
    if entry["manifest_prefix"] == GUIDEPOINT_MANIFEST_PREFIX:
        from .guidepoint_acquisition import verified_guidepoint_source

        wire, text = verified_guidepoint_source(core, spool, wire, receipt_reader)
    else:
        from .feed_acquisition import verified_feed_source

        wire, text = verified_feed_source(core, spool, wire, receipt_reader)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest != wire["declared_content_sha256"]:
        # verified_* already checks this; repeated here because this function
        # is what every caller below trusts, and a verifier that stopped
        # checking would otherwise fail silently.
        raise ResearchVerificationConflict(
            "acquired original does not hash to the content its manifest declared"
        )
    return wire, text, entry


def acquisition_object_hash(manifest: Mapping[str, Any]) -> str:
    """The content-addressed object this acquisition put the text in."""

    obj = manifest.get("excerpt_object") or manifest.get("assembled_object")
    if not isinstance(obj, Mapping) or not isinstance(obj.get("content_hash"), str):
        raise AcquiredSourceAuthorityError("acquisition manifest names no spool object")
    return obj["content_hash"]


def acquisition_source_envelope_ref(connection: Any, manifest: Mapping[str, Any]) -> str:
    """The Core SourceEnvelope the acquisition that wrote these bytes recorded.

    A Guidepoint manifest names it outright.  A feed manifest names the
    invocation the host-tool runner registered around the child, and Core holds
    exactly one envelope per invocation; more than one, or none, is a refusal
    rather than a choice, because "which envelope" is the question the whole
    chain below is an answer to.
    """

    wire, entry = validate_acquired_source_manifest(manifest)
    named = wire.get("source_envelope_ref")
    if isinstance(named, str) and named:
        return named
    invocation = wire.get("connector_invocation_ref")
    if not isinstance(invocation, str) or not invocation:
        raise AcquiredSourceAuthorityError(
            f"the acquisition of {wire['document_ref']} recorded no connector "
            "invocation, so it has no Core source envelope a Claim could cite"
        )
    rows = connection.execute(
        "SELECT source_envelope_id FROM connector_source_envelopes "
        "WHERE connector_invocation_ref=? ORDER BY created_at, source_envelope_id",
        (invocation,),
    ).fetchall()
    if len(rows) != 1:
        raise AcquiredSourceAuthorityError(
            f"Core holds {len(rows)} source envelopes for the acquisition of "
            f"{wire['document_ref']}; exactly one is required"
        )
    return rows[0]["source_envelope_id"]


def acquired_source_correction_authority(
    store: Any, *, spool: Any, manifest: Mapping[str, Any], evidence_resolver: Any,
) -> tuple[Any, dict[str, Any]]:
    """The correction authority over one acquired original.

    The same ``TranscriptCorrectionAuthority`` the AlphaEngine and public-web
    paths use, resolving exactly one manifest -- the one the extraction pass
    located through this source's own ticket directory -- and reading the
    bytes out of the spool that source's child wrote them to.
    """

    from .transcript_correction import TranscriptCorrectionAuthority

    wire, _entry = validate_acquired_source_manifest(manifest)
    authority = TranscriptCorrectionAuthority(
        store, spool=spool,
        manifest_resolver=(lambda ref: wire if ref == wire["id"] else None),
        evidence_resolver=evidence_resolver,
    )
    return authority, wire


def acquired_source_binding_is_exact(
    *, evidence_source_type: str, source_record_refs: Any, document_ref: Any,
    source: Any, operation: Any,
) -> bool:
    """Whether a Core SourceEnvelope really is the acquisition of this document.

    Shared by the auto-commit rule and the Ledger writer so the two cannot
    drift: same source, same operation, and the envelope named the document --
    as its only record for a one-document read, or as one of the page of
    records a Guidepoint search returned.
    """

    entry = ACQUIRED_SOURCE_KIND_BY_EVIDENCE_TYPE.get(evidence_source_type)
    if entry is None or not isinstance(document_ref, str):
        return False
    if not document_ref.startswith(entry["document_prefix"]):
        return False
    if source != entry["source_ref"] or operation != entry["operation"]:
        return False
    if not isinstance(source_record_refs, list):
        return False
    if entry["record_binding"] == "member":
        return document_ref in source_record_refs
    return source_record_refs == [document_ref]


__all__ = [
    "ACQUIRED_EVIDENCE_SOURCE_TYPES",
    "ACQUIRED_MANIFEST_PREFIXES",
    "ACQUIRED_SOURCE_KINDS",
    "ACQUIRED_SOURCE_KIND_BY_EVIDENCE_TYPE",
    "ACQUIRED_SOURCE_KIND_BY_SOURCE_REF",
    "AcquiredSourceAuthorityError",
    "EXPERT_NETWORK_EVIDENCE_SOURCE_TYPE",
    "FEED_MANIFEST_PREFIX",
    "GUIDEPOINT_MANIFEST_PREFIX",
    "INTERNAL_PRIOR_RESEARCH_EVIDENCE_SOURCE_TYPE",
    "INTERNAL_WIKI_EVIDENCE_SOURCE_TYPE",
    "SELL_SIDE_NOTE_EVIDENCE_SOURCE_TYPE",
    "acquired_kind_for_manifest_ref",
    "acquired_source_binding_is_exact",
    "acquired_source_correction_authority",
    "acquired_source_kind",
    "acquisition_object_hash",
    "acquisition_source_envelope_ref",
    "is_acquired_source_manifest_ref",
    "validate_acquired_source_manifest",
    "verified_acquired_source",
]
