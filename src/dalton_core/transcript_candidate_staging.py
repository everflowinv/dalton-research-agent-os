"""Stage qualitative transcript candidates from Core-held AlphaEngine authority.

ADR-0003 option B.  A ``qualitative`` CandidateClaim carries no value, unit,
scale, currency or numeric verification.  Its only staged authority is a
source verification that re-derives, from one Core, the exact chain:

``TranscriptClaimCitationBinding (claim_eligible)``
  -> ``TranscriptCorrectionSetVersion`` (document_ref + whole-document digest)
  -> page-1 ``SourceEnvelope`` whose ``source_record_refs`` bind that digest
  -> ``ConnectorInvocation`` / producer execution
  -> raw ``ArtifactVersion`` v0.2 (``raw_response_hash`` == artifact bytes)

The Core-hosted AlphaEngine acquisition (S6b) records WorkOrders through the
CapabilityCatalog and has no ResearchCheckpoint, compiled connector plan or
coordinator receipt, so the existing ``connector_authority`` staging mode
(which replays a coordinator checkpoint and needs a source-specific numeric
normalizer) cannot verify these pages.  This module is the closed
``transcript_core_authority`` counterpart: it reads Core only, opens no
network path, and hands the result to the real ``CandidateStagingStore``.

``stage_transcript_qualitative_candidate`` is the entry point a writer
human-governance op (S7c) calls directly.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from .acquired_source_authority import (
    ACQUIRED_SOURCE_KINDS,
    acquisition_object_hash,
    verified_acquired_source,
)
from .research_verification import (
    ACQUIRED_SOURCE_AUTHORITY_MODE,
    ACQUIRED_SOURCE_VERIFIER_HASH,
    ACQUIRED_SOURCE_VERIFIER_REF,
    PUBLIC_WEB_CORE_AUTHORITY_MODE,
    PUBLIC_WEB_SOURCE_VERIFIER_HASH,
    PUBLIC_WEB_SOURCE_VERIFIER_REF,
    TRANSCRIPT_CORE_AUTHORITY_MODE,
    TRANSCRIPT_SOURCE_VERIFIER_HASH,
    TRANSCRIPT_SOURCE_VERIFIER_REF,
    ResearchVerificationError,
    VerificationRejected,
    _canonical_raw_bytes,
    _finding_wire,
    _sha256_bytes,
    build_candidate_evidence,
    validate_candidate_claim,
    validate_candidate_evidence,
    validate_source_verification_material,
    validate_verification_bundle,
)
from .store import canonical_json, content_hash
from .transcript_correction import (
    CITED_EVIDENCE_SOURCE_TYPES,
    PUBLIC_WEB_EVIDENCE_SOURCE_TYPE,
    TRANSCRIPT_EVIDENCE_SOURCE_TYPE,
    TranscriptCorrectionError,
    _persisted_citation_row,
    bind_candidate_evidence_to_transcript_citation,
    validate_persisted_transcript_claim_citation,
)

ALPHAENGINE_SOURCE_REF = "source:alphaengine"
ALPHAENGINE_DOCUMENT_OPERATION = "get_document"
_DOCUMENT_REF_PREFIX = "alphaengine-doc:"
# ADR-0005 / P9d-17c: the same resolver over a fetched public-web page.  The
# fetch SourceEnvelope names one record, the page itself; the cited original
# is its verified rendering, bound through the correction authority.
PUBLIC_WEB_SOURCE_REF = "source:public-web"
PUBLIC_WEB_FETCH_OPERATION = "fetch_get"
_WEB_DOCUMENT_REF_PREFIX = "public-web-document:"
SOURCE_KINDS: dict[str, dict[str, Any]] = {
    "alphaengine": {
        "document_prefix": _DOCUMENT_REF_PREFIX, "source_ref": ALPHAENGINE_SOURCE_REF,
        "operation": ALPHAENGINE_DOCUMENT_OPERATION, "material_prefix": "source-material:transcript-core:",
        "bundle_prefix": "verification-bundle:transcript-core-source:",
        "candidate_prefix": "transcript", "evidence_source_type": TRANSCRIPT_EVIDENCE_SOURCE_TYPE,
    },
    "public_web": {
        "document_prefix": _WEB_DOCUMENT_REF_PREFIX, "source_ref": PUBLIC_WEB_SOURCE_REF,
        "operation": PUBLIC_WEB_FETCH_OPERATION, "material_prefix": "source-material:public-web-core:",
        "bundle_prefix": "verification-bundle:public-web-core-source:",
        "candidate_prefix": "public-web", "evidence_source_type": PUBLIC_WEB_EVIDENCE_SOURCE_TYPE,
        "record_binding": "exact",
    },
    # P13aq: the four acquired sources.  Same Core chain -- envelope,
    # invocation, profile, raw artifact -- plus the acquisition manifest and
    # the content-addressed object it declares, which is what makes a local
    # corpus document citable at all.  Their table lives in
    # ``acquired_source_authority`` so the correction authority, the
    # auto-commit rule and the Ledger writer read the same one.
    **{
        kind: {
            "document_prefix": entry["document_prefix"],
            "source_ref": entry["source_ref"],
            "operation": entry["operation"],
            "material_prefix": entry["material_prefix"],
            "bundle_prefix": entry["bundle_prefix"],
            "candidate_prefix": entry["candidate_prefix"],
            "evidence_source_type": entry["evidence_source_type"],
            "record_binding": entry["record_binding"],
            "acquired": True,
        }
        for kind, entry in ACQUIRED_SOURCE_KINDS.items()
    },
}
#: Which closed verification mode and deterministic verifier each kind travels
#: under.  A table rather than a conditional so that adding a kind without
#: deciding this is a KeyError at construction, not a silent reuse of another
#: source's verifier.
SOURCE_KIND_MODES: dict[str, tuple[str, tuple[str, str]]] = {
    "alphaengine": (TRANSCRIPT_CORE_AUTHORITY_MODE,
                    (TRANSCRIPT_SOURCE_VERIFIER_REF, TRANSCRIPT_SOURCE_VERIFIER_HASH)),
    "public_web": (PUBLIC_WEB_CORE_AUTHORITY_MODE,
                   (PUBLIC_WEB_SOURCE_VERIFIER_REF, PUBLIC_WEB_SOURCE_VERIFIER_HASH)),
    **{
        kind: (ACQUIRED_SOURCE_AUTHORITY_MODE,
               (ACQUIRED_SOURCE_VERIFIER_REF, ACQUIRED_SOURCE_VERIFIER_HASH))
        for kind in ACQUIRED_SOURCE_KINDS
    },
}


class TranscriptCoreAuthorityError(ResearchVerificationError):
    """Core does not hold the exact transcript authority the candidate needs."""


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResearchVerificationError(f"{name} must be a non-empty string")
    return value


def _citation_projection(
    binding: Mapping[str, Any],
    correction_set: Mapping[str, Any],
    source: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """The structured observation a qualitative candidate is verified against.

    There is no numeric payload; the projection is the exact citation
    coordinate set so that ``normalized_payload_hash`` binds the cited span,
    the whole-document digest and the raw page artifact together.
    """

    return {
        "document_ref": correction_set["document_ref"],
        "source_content_hash": binding["source_content_hash"],
        "citation_binding_ref": binding["id"],
        "citation_binding_hash": binding["content_hash"],
        "correction_set_version_ref": binding["correction_set_version_ref"],
        "correction_set_version_hash": binding["correction_set_version_hash"],
        "source_start": binding["source_start"],
        "source_end": binding["source_end"],
        "accepted_correction_indexes": list(binding["accepted_correction_indexes"]),
        "unresolved_correction_indexes": list(binding["unresolved_correction_indexes"]),
        "raw_artifact_version_ref": artifact["id"],
        "raw_response_hash": source["raw_response_hash"],
        "claim_eligible": bool(binding["claim_eligible"]),
    }


class TranscriptCoreAuthorityResolver:
    """Read-only resolver over one Core for transcript-cited AlphaEngine pages.

    It never writes.  ``artifact_reader`` (optional) returns the raw bytes of
    an ArtifactVersion so the verifier can prove the spool object still
    matches ``artifact_content_hash``; without it that finding is skipped and
    the verifier relies on the Core row hashes only.
    """

    def __init__(
        self,
        core: Any,
        *,
        artifact_reader: Callable[[Mapping[str, Any]], bytes] | None = None,
        source_kind: str = "alphaengine",
        source_manifest: Mapping[str, Any] | None = None,
        spool: Any | None = None,
    ) -> None:
        connection = getattr(core, "connection", None)
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("TranscriptCoreAuthorityResolver requires a Core with a sqlite3 connection")
        if artifact_reader is not None and not callable(artifact_reader):
            raise TypeError("artifact_reader must be callable")
        if source_kind not in SOURCE_KINDS:
            raise TypeError("source_kind is not one of " + ", ".join(sorted(SOURCE_KINDS)))
        self.core = core
        self.connection = connection
        self.artifact_reader = artifact_reader
        self.source_kind = source_kind
        self.kind = SOURCE_KINDS[source_kind]
        self.provenance_mode, self.verifier = SOURCE_KIND_MODES[source_kind]
        # P13aq: an acquired original is cited against its own acquisition, so
        # the manifest and the spool that holds its bytes are not optional for
        # these kinds.  Refused at construction rather than producing a bundle
        # that verified everything except the thing being quoted.
        self.acquired = bool(self.kind.get("acquired"))
        if self.acquired and (source_manifest is None or spool is None):
            raise TranscriptCoreAuthorityError(
                f"{source_kind} candidates require their acquisition manifest and "
                "the spool its bytes were written to"
            )
        self.source_manifest = source_manifest
        self.spool = spool

    # -- Core reads -------------------------------------------------------

    def citation(self, citation_ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return the exact eligible citation and its correction set."""

        citation_ref = _text(citation_ref, "citation_ref")
        try:
            asserted = _persisted_citation_row(self.connection, citation_ref)
            binding = validate_persisted_transcript_claim_citation(
                self.connection, citation_ref, asserted["content_hash"]
            )
        except (TranscriptCorrectionError, sqlite3.Error) as exc:
            raise TranscriptCoreAuthorityError(
                f"transcript citation authority is unavailable or ineligible: {exc}"
            ) from exc
        row = self.connection.execute(
            "SELECT record_json FROM transcript_correction_set_versions WHERE version_id=?",
            (binding["correction_set_version_ref"],),
        ).fetchone()
        if row is None:
            raise TranscriptCoreAuthorityError("transcript correction set is unavailable")
        correction_set = json.loads(row["record_json"])
        document_ref = correction_set.get("document_ref")
        if not isinstance(document_ref, str) or not document_ref.startswith(self.kind["document_prefix"]):
            raise TranscriptCoreAuthorityError(
                f"correction set is not bound to a {self.source_kind} document"
            )
        return binding, correction_set

    def locate_source_envelope(self, document_ref: str, source_content_hash: str) -> str:
        """Find the page-1 SourceEnvelope that binds the whole-document digest."""

        expected = f"{_text(document_ref, 'document_ref')}:sha256:{_text(source_content_hash, 'source_content_hash')}"
        rows = self.connection.execute(
            "SELECT source_envelope_id FROM connector_source_envelopes "
            "WHERE json_extract(record_json,'$.source')=? "
            "AND json_extract(record_json,'$.operation')=? "
            "AND json_array_length(json_extract(record_json,'$.source_record_refs'))=1 "
            "AND json_extract(record_json,'$.source_record_refs[0]')=? "
            "ORDER BY created_at, source_envelope_id",
            (ALPHAENGINE_SOURCE_REF, ALPHAENGINE_DOCUMENT_OPERATION, expected),
        ).fetchall()
        if not rows:
            raise TranscriptCoreAuthorityError(
                "Core holds no AlphaEngine page-1 SourceEnvelope for the cited document digest"
            )
        return rows[0]["source_envelope_id"]

    def _authority(self, source_envelope_ref: str) -> dict[str, Any]:
        """Load the exact source/invocation/profile/artifact rows or fail closed."""

        source_row = self.connection.execute(
            "SELECT * FROM connector_source_envelopes WHERE source_envelope_id=?",
            (source_envelope_ref,),
        ).fetchone()
        if source_row is None:
            raise TranscriptCoreAuthorityError("SourceEnvelope is not Core authority")
        source = json.loads(source_row["record_json"])
        if (
            source.get("id") != source_envelope_ref
            or source.get("content_hash") != source_row["content_hash"]
            or content_hash({k: v for k, v in source.items() if k != "content_hash"})
            != source_row["content_hash"]
        ):
            raise TranscriptCoreAuthorityError("SourceEnvelope row hash is not canonical")
        invocation = self.connection.execute(
            "SELECT * FROM connector_invocations WHERE connector_invocation_id=?",
            (source_row["connector_invocation_ref"],),
        ).fetchone()
        if invocation is None:
            raise TranscriptCoreAuthorityError("ConnectorInvocation is not Core authority")
        profile_row = self.connection.execute(
            "SELECT record_json,content_hash FROM connector_profile_versions WHERE profile_version_id=?",
            (invocation["connector_profile_ref"],),
        ).fetchone()
        if profile_row is None or profile_row["content_hash"] != invocation["connector_profile_hash"]:
            raise TranscriptCoreAuthorityError("ConnectorProfile is not exact Core authority")
        profile = json.loads(profile_row["record_json"])
        artifact_row = self.connection.execute(
            "SELECT i.version_id,i.producer_execution_ref,i.schema_version,v.record_json,v.content_hash "
            "FROM observability_artifact_version_index i "
            "JOIN observability_artifact_versions_v2 v ON v.version_id=i.version_id "
            "WHERE i.version_id=? AND i.producer_execution_ref=?",
            (source_row["raw_artifact_version_ref"], invocation["execution_ref"]),
        ).fetchone()
        if artifact_row is None or artifact_row["schema_version"] != "0.2":
            raise TranscriptCoreAuthorityError(
                "raw ArtifactVersion is not bound to the producer execution in Core"
            )
        artifact = json.loads(artifact_row["record_json"])
        if (
            artifact.get("id") != artifact_row["version_id"]
            or artifact.get("content_hash") != artifact_row["content_hash"]
            or content_hash({k: v for k, v in artifact.items() if k != "content_hash"})
            != artifact_row["content_hash"]
        ):
            raise TranscriptCoreAuthorityError("ArtifactVersion row hash is not canonical")
        return {
            "source": source, "source_row": source_row, "invocation": invocation,
            "profile": profile, "artifact": artifact,
        }

    # -- material ---------------------------------------------------------

    def build_material(
        self,
        citation_ref: str,
        *,
        source_envelope_ref: str | None = None,
    ) -> dict[str, Any]:
        """Build 0.2 authority material for one eligible transcript citation."""

        binding, correction_set = self.citation(citation_ref)
        if source_envelope_ref is None:
            if self.acquired:
                # The acquisition itself names the envelope (Guidepoint) or the
                # invocation Core holds exactly one envelope for (the corpora).
                from .acquired_source_authority import acquisition_source_envelope_ref

                source_envelope_ref = acquisition_source_envelope_ref(
                    self.connection, self.source_manifest)
            elif self.source_kind != "alphaengine":
                raise TranscriptCoreAuthorityError("a fetched page's SourceEnvelope must be named explicitly")
            else:
                source_envelope_ref = self.locate_source_envelope(
                    correction_set["document_ref"], binding["source_content_hash"]
                )
        authority = self._authority(_text(source_envelope_ref, "source_envelope_ref"))
        source = authority["source"]
        artifact = authority["artifact"]
        profile = authority["profile"]
        payload = _citation_projection(binding, correction_set, source, artifact)
        base = {
            "schema_version": "0.2",
            "id": self.kind["material_prefix"] + content_hash({
                "source_envelope_hash": source["content_hash"],
                "citation_hash": binding["content_hash"],
            }),
            "created_at": source["retrieved_at"],
            "source_envelope_ref": source["id"],
            "source_envelope_hash": source["content_hash"],
            "artifact_ref": artifact["id"],
            "artifact_hash": artifact["content_hash"],
            "source_ref": source["source"],
            "source_type": profile["source_identity"]["source_type"],
            "operation": source["operation"],
            "provenance_mode": self.provenance_mode,
            "authority_resolution_ref": binding["id"],
            "authority_resolution_hash": binding["content_hash"],
            "source_record_refs": list(source["source_record_refs"]),
            "next_cursor": source.get("cursor"),
            "normalized_payload": payload,
            "normalized_payload_hash": _sha256_bytes(
                _canonical_raw_bytes(payload, "citation projection"), "citation projection"
            ),
            "source_schema_hash": source["source_schema_hash"],
            "source_content_hash": source["source_content_hash"],
            "source_lineage": [
                source["source"], source["id"], artifact["id"],
                binding["correction_set_version_ref"],
            ],
            "published_at": source.get("published_at"),
            "updated_at": source.get("updated_at"),
            "as_of": source.get("as_of"),
            "retrieved_at": source["retrieved_at"],
            "completeness": source["completeness"],
            "status": source["status"],
        }
        base["content_hash"] = content_hash(base)
        return validate_source_verification_material(base)

    # -- the acquisition an acquired original is cited against ------------

    def _check_acquisition(self, check: Callable[..., None], binding: Mapping[str, Any],
                           document_ref: str) -> None:
        """P13aq: the manifest is exact and its object still is the text.

        Three findings, in the order a reader would ask them: is this the
        acquisition the citation was taken against, is it an acquisition of
        *this* document, and do the bytes it declared still come back out of
        the spool and hash to the text the span was cut from.  The last one is
        the reason this mode exists: a local corpus document has no provider to
        re-ask, so the content-addressed object is the only original there is.
        """

        manifest = self.source_manifest
        check("acquisition_manifest_binding",
              (manifest.get("id"), manifest.get("content_hash")),
              (binding["source_manifest_ref"], binding["source_manifest_hash"]),
              "material.acquisition_manifest",
              "the citation binds this exact acquisition manifest")
        check("acquisition_document", manifest.get("document_ref"), document_ref,
              "material.acquisition_document",
              "the acquisition is an acquisition of the cited document")
        from .connector import ConnectorStore
        from .connector_authority_port import ConnectorCompletionReceiptReader
        from .observability import ObservabilityStore

        reader = ConnectorCompletionReceiptReader(
            connectors=ConnectorStore(self.core), observability=ObservabilityStore(self.core)
        )
        verified, text, _entry = verified_acquired_source(
            self.core, self.spool, manifest, reader)
        check("acquisition_object_rehash",
              (_sha256_bytes(text.encode("utf-8"), "acquired original"),
               acquisition_object_hash(verified)),
              (binding["source_content_hash"], binding["source_content_hash"]),
              "acquisition.object",
              "the acquired object still re-reads and re-hashes to the cited original")

    # -- deterministic verifier -------------------------------------------

    def verify_source_material(self, material: Mapping[str, Any]) -> dict[str, Any]:
        """Re-derive the whole authority chain from Core and emit a bundle.

        ``CandidateStagingStore.stage(verification_mode="transcript_core_authority")``
        calls this and requires the caller-supplied bundle to be byte-identical.
        """

        material_wire = validate_source_verification_material(material)
        if material_wire.get("provenance_mode") != self.provenance_mode:
            raise VerificationRejected(f"{self.source_kind} Core verifier requires {self.provenance_mode} material")
        findings: list[dict[str, Any]] = []

        def check(code: str, observed: Any, expected: Any, path: str, message: str) -> None:
            ok = observed == expected
            findings.append(_finding_wire(
                code, "info" if ok else "error", "pass" if ok else "fail", path,
                canonical_json(expected) if isinstance(expected, (dict, list)) else expected,
                canonical_json(observed) if isinstance(observed, (dict, list)) else observed,
                message if ok else message + " drifted",
            ))

        try:
            binding, correction_set = self.citation(material_wire["authority_resolution_ref"])
            findings.append(_finding_wire(
                "citation_eligible", "info", "pass", "citation.claim_eligible",
                True, binding["claim_eligible"],
                "persisted citation is claim eligible with exact correction lineage",
            ))
            check("citation_hash", material_wire["authority_resolution_hash"], binding["content_hash"],
                  "material.authority_resolution_hash", "material binds the exact citation")
            authority = self._authority(material_wire["source_envelope_ref"])
            source = authority["source"]
            artifact = authority["artifact"]
            profile = authority["profile"]
            document_ref = correction_set["document_ref"]
            check("source_envelope_hash", material_wire["source_envelope_hash"],
                  authority["source_row"]["content_hash"], "material.source_envelope_hash",
                  "SourceEnvelope hash is exact Core authority")
            check("source_is_cited_original", (source["source"], source["operation"]),
                  (self.kind["source_ref"], self.kind["operation"]), "source.operation",
                  f"SourceEnvelope is a {self.source_kind} original")
            if self.acquired:
                # P13aq.  A one-document read named exactly this document; a
                # Guidepoint search named the page of excerpts the acquisition
                # derived this one from, and the manifest's ordinal was already
                # checked against those same bytes by verified_guidepoint_source.
                refs = source["source_record_refs"]
                if self.kind["record_binding"] == "member":
                    check("source_record_binds_acquired_document",
                          document_ref in refs, True, "source.source_record_refs",
                          "the acquisition's SourceEnvelope returned the cited document")
                else:
                    check("source_record_binds_acquired_document", refs, [document_ref],
                          "source.source_record_refs",
                          "the acquisition's SourceEnvelope is exactly this document")
                self._check_acquisition(check, binding, document_ref)
            elif self.source_kind == "alphaengine":
                check("source_record_binds_document_digest", source["source_record_refs"],
                      [f"{document_ref}:sha256:{binding['source_content_hash']}"],
                      "source.source_record_refs",
                      "page-1 SourceEnvelope binds the cited whole-document digest")
            else:
                # A fetch names exactly one record: the page (url hash + body
                # hash).  The rendering hash the citation binds was verified
                # against those bytes by the correction authority.
                check("source_record_binds_fetched_page", source["source_record_refs"], [document_ref],
                      "source.source_record_refs", "fetch SourceEnvelope binds the cited page")
            check("artifact_ref", material_wire["artifact_ref"], artifact["id"],
                  "material.artifact_ref", "raw ArtifactVersion ref is exact")
            check("artifact_hash", material_wire["artifact_hash"], artifact["content_hash"],
                  "material.artifact_hash", "raw ArtifactVersion hash is exact")
            check("raw_response_hash_equals_artifact", source["raw_response_hash"],
                  artifact["artifact_content_hash"], "source.raw_response_hash",
                  "SourceEnvelope raw hash equals the ArtifactVersion bytes hash")
            check("artifact_work_order", artifact.get("work_order_ref"),
                  authority["invocation"]["work_order_ref"], "artifact.work_order_ref",
                  "ArtifactVersion binds the invocation WorkOrder")
            check("profile_source_identity",
                  (profile["source_identity"]["source_ref"], profile["source_identity"]["source_type"]),
                  (material_wire["source_ref"], material_wire["source_type"]),
                  "material.source_type", "material source ref/type come from the Core profile")
            check("source_schema_hash", material_wire["source_schema_hash"],
                  profile["output_schema_hashes"][source["operation"]],
                  "material.source_schema_hash", "material schema hash equals the profile output schema")
            check("source_schema_hash_envelope", source["source_schema_hash"],
                  material_wire["source_schema_hash"], "source.source_schema_hash",
                  "SourceEnvelope schema hash equals material")
            for field in (
                "source_ref", "operation", "source_record_refs", "source_content_hash",
                "retrieved_at", "completeness", "status", "published_at", "updated_at", "as_of",
            ):
                source_field = "source" if field == "source_ref" else field
                check(f"material_{field}", material_wire[field], source.get(source_field),
                      f"material.{field}", f"material {field} equals SourceEnvelope")
            check("material_next_cursor", material_wire["next_cursor"], source.get("cursor"),
                  "material.next_cursor", "material cursor equals SourceEnvelope")
            check("material_created_at", material_wire["created_at"], source["retrieved_at"],
                  "material.created_at", "material created_at equals retrieval time")
            check("source_lineage", material_wire["source_lineage"],
                  [source["source"], source["id"], artifact["id"], binding["correction_set_version_ref"]],
                  "material.source_lineage", "material lineage is source, envelope, artifact, correction set")
            check("citation_projection", material_wire["normalized_payload"],
                  _citation_projection(binding, correction_set, source, artifact),
                  "material.normalized_payload", "material projection equals the persisted citation")
            if self.artifact_reader is not None:
                raw = self.artifact_reader(artifact)
                check("raw_artifact_bytes",
                      (_sha256_bytes(raw, "raw artifact"), len(raw)),
                      (artifact["artifact_content_hash"], int(artifact["size_bytes"])),
                      "artifact.bytes", "raw artifact bytes match ArtifactVersion authority")
        except Exception as exc:  # fail closed as a finding, never as a crash
            findings.append(_finding_wire(
                "transcript_core_resolution", "error", "fail", "core_authority",
                f"exact passing {self.source_kind} authority", "unavailable", str(exc),
            ))
        for code, before, after in (
            ("published_before_retrieved", material_wire["published_at"], material_wire["retrieved_at"]),
            ("updated_before_retrieved", material_wire["updated_at"], material_wire["retrieved_at"]),
            ("as_of_before_retrieved", material_wire["as_of"], material_wire["retrieved_at"]),
        ):
            ok = before is None or before <= after
            findings.append(_finding_wire(
                code, "info" if ok else "error", "pass" if ok else "fail",
                f"material.{code}", after, before,
                "time ordering is valid" if ok else "source time is after retrieval",
            ))
        verdict = "pass" if not any(
            item["severity"] == "error" and item["status"] == "fail" for item in findings
        ) else "reject"
        base = {
            "schema_version": "0.1",
            "id": self.kind["bundle_prefix"] + content_hash({
                "subject": material_wire["id"],
                "citation": material_wire["authority_resolution_hash"],
                "findings": [item["content_hash"] for item in findings],
            }),
            "created_at": material_wire["retrieved_at"],
            "kind": "source",
            "subject_ref": material_wire["id"],
            "subject_hash": material_wire["content_hash"],
            "verdict": verdict,
            # The human-admitted citation is the checkpoint of this mode.
            "checkpoint_ref": material_wire["authority_resolution_ref"],
            "checkpoint_hash": material_wire["authority_resolution_hash"],
            "findings": findings,
            "verifier_ref": self.verifier[0],
            "verifier_hash": self.verifier[1],
        }
        base["content_hash"] = content_hash(base)
        return validate_verification_bundle(base)


def build_transcript_qualitative_candidate(
    evidence: Mapping[str, Any],
    source_verification: Mapping[str, Any],
    *,
    candidate_claim_ref: str,
    subject_ref: str,
    metric_or_aspect: str,
    period: Any,
    basis: str,
    normalized_statement: str,
    actor_ref: str,
    created_at: str,
) -> dict[str, Any]:
    """Build one qualitative CandidateClaim bound to transcript evidence.

    Pure: no I/O.  The claim carries no numeric fields; ``period`` is the
    caller's semantic period (string or closed object) because there is no
    numeric spec to derive it from.
    """

    evidence_wire = validate_candidate_evidence(evidence)
    source_wire = validate_verification_bundle(source_verification)
    if source_wire["verdict"] != "pass" or source_wire["kind"] != "source":
        raise VerificationRejected("qualitative candidate requires passing source verification")
    if (
        evidence_wire["source_verification_ref"] != source_wire["id"]
        or evidence_wire["source_verification_hash"] != source_wire["content_hash"]
    ):
        raise VerificationRejected("qualitative candidate evidence does not bind this source verification")
    if (
        evidence_wire["source_type"] not in CITED_EVIDENCE_SOURCE_TYPES
        or len(evidence_wire["artifact_refs"]) != 2
        or not evidence_wire["artifact_refs"][1]["ref"].startswith("transcript-claim-citation-binding:")
    ):
        raise VerificationRejected(
            "qualitative candidate requires cited original evidence with an exact citation binding"
        )
    base = {
        "schema_version": "0.1",
        "id": "candidate-claim-version:" + content_hash(
            {"candidate_claim_ref": _text(candidate_claim_ref, "candidate_claim_ref"), "version": 1}
        ),
        "created_at": created_at,
        "candidate_claim_ref": candidate_claim_ref,
        "version": 1,
        "subject_ref": subject_ref,
        "metric_or_aspect": metric_or_aspect,
        "period": period,
        "basis": basis,
        "normalized_statement": normalized_statement,
        "semantic_verification_status": "unverified",
        "claim_kind": "qualitative",
        "value": None,
        "unit": None,
        "currency": None,
        "scale": None,
        "candidate_evidence_refs": [
            {"ref": evidence_wire["id"], "hash": evidence_wire["content_hash"]}
        ],
        "source_verification_ref": source_wire["id"],
        "source_verification_hash": source_wire["content_hash"],
        "numeric_spec_ref": None,
        "numeric_spec_hash": None,
        "numeric_verification_ref": None,
        "numeric_verification_hash": None,
        "actor_ref": actor_ref,
        "prior_version_ref": None,
    }
    base["content_hash"] = content_hash(base)
    return validate_candidate_claim(base)


def stage_transcript_qualitative_candidate(
    core: Any,
    staging_store: Any,
    *,
    correction_set_ref: str,
    citation_ref: str,
    subject_ref: str,
    metric_or_aspect: str,
    period: Any,
    basis: str,
    normalized_statement: str,
    actor_ref: str,
    idempotency_key: str,
    created_at: str | None = None,
    candidate_evidence_ref: str | None = None,
    candidate_claim_ref: str | None = None,
    source_envelope_ref: str | None = None,
    artifact_reader: Callable[[Mapping[str, Any]], bytes] | None = None,
    source_kind: str = "alphaengine",
    source_manifest: Mapping[str, Any] | None = None,
    spool: Any | None = None,
) -> dict[str, Any]:
    """Read Core, build the qualitative candidate pair and stage it.

    Reads only: the persisted citation binding, its correction set, the
    page-1 SourceEnvelope, invocation, profile and raw ArtifactVersion.
    Writes only through ``staging_store.stage`` (CandidateStaging, not the
    Ledger).  The result includes every record so a writer op can return
    them to the Cockpit without re-reading.
    """

    resolver = TranscriptCoreAuthorityResolver(
        core, artifact_reader=artifact_reader, source_kind=source_kind,
        source_manifest=source_manifest, spool=spool)
    kind = SOURCE_KINDS[source_kind]
    binding, _correction_set = resolver.citation(citation_ref)
    if binding["correction_set_version_ref"] != _text(correction_set_ref, "correction_set_ref"):
        raise TranscriptCoreAuthorityError(
            "transcript citation does not belong to the requested correction set"
        )
    material = resolver.build_material(citation_ref, source_envelope_ref=source_envelope_ref)
    source_verification = resolver.verify_source_material(material)
    if source_verification["verdict"] != "pass":
        failed = [
            item["code"] for item in source_verification["findings"]
            if item["severity"] == "error" and item["status"] == "fail"
        ]
        raise VerificationRejected(
            "transcript Core authority verification rejected: " + ", ".join(failed)
        )
    when = material["retrieved_at"] if created_at is None else _text(created_at, "created_at")
    evidence_ref = (
        f"candidate-evidence:{kind['candidate_prefix']}:" + binding["content_hash"][:32]
        if candidate_evidence_ref is None else _text(candidate_evidence_ref, "candidate_evidence_ref")
    )
    claim_ref = (
        f"candidate-claim:{kind['candidate_prefix']}:" + content_hash({
            "citation": binding["id"], "subject_ref": subject_ref,
            "metric_or_aspect": metric_or_aspect, "period": period, "basis": basis,
        })[:32]
        if candidate_claim_ref is None else _text(candidate_claim_ref, "candidate_claim_ref")
    )
    evidence = build_candidate_evidence(
        material, source_verification,
        candidate_evidence_ref=evidence_ref, actor_ref=actor_ref, created_at=when,
        verification_mode=resolver.provenance_mode,
    )
    evidence = bind_candidate_evidence_to_transcript_citation(
        evidence, binding, source_type=kind["evidence_source_type"])
    claim = build_transcript_qualitative_candidate(
        evidence, source_verification,
        candidate_claim_ref=claim_ref, subject_ref=subject_ref,
        metric_or_aspect=metric_or_aspect, period=period, basis=basis,
        normalized_statement=normalized_statement, actor_ref=actor_ref, created_at=when,
    )
    staged = staging_store.stage(
        material=material,
        source_verification=source_verification,
        evidence=evidence,
        claim=claim,
        idempotency_key=idempotency_key,
        verification_mode=resolver.provenance_mode,
        authority_resolver=resolver,
    )
    return {
        "write_status": staged["write_status"],
        "staging": staged,
        "citation": binding,
        "material": material,
        "source_verification": source_verification,
        "evidence": evidence,
        "claim": claim,
    }


__all__ = [
    "ALPHAENGINE_DOCUMENT_OPERATION",
    "ALPHAENGINE_SOURCE_REF",
    "TranscriptCoreAuthorityError",
    "TranscriptCoreAuthorityResolver",
    "build_transcript_qualitative_candidate",
    "stage_transcript_qualitative_candidate",
]
