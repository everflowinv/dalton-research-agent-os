"""P13aq: the table the whole acquired-source citation chain reads from.

Unit cases over ``acquired_source_authority``: the one place that says what a
Guidepoint excerpt, a sales note, a wiki page and this fund's prior research
are, so the correction authority, the candidate resolver, the auto-commit rule
and the Ledger writer cannot drift apart about it.  The end-to-end chain is in
``test_acquired_source_claims``; these are the closed-shape refusals underneath
it.
"""

from __future__ import annotations

import hashlib
import unittest

from dalton_core.acquired_source_authority import (
    ACQUIRED_EVIDENCE_SOURCE_TYPES,
    ACQUIRED_SOURCE_KIND_BY_EVIDENCE_TYPE,
    ACQUIRED_SOURCE_KIND_BY_SOURCE_REF,
    ACQUIRED_SOURCE_KINDS,
    AcquiredSourceAuthorityError,
    acquired_kind_for_manifest_ref,
    acquired_source_binding_is_exact,
    acquired_source_kind,
    acquisition_object_hash,
    is_acquired_source_manifest_ref,
    validate_acquired_source_manifest,
)
from dalton_core.feed_acquisition import build_feed_acquisition_manifest
from dalton_core.store import content_hash

TEXT = "Accenture bookings were described as steady by the desk.\n"


def feed_manifest(*, source_ref="source:sales-notes", operation="get_note",
                  document_ref="sales-note:aaaa000000000001", text=TEXT):
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return build_feed_acquisition_manifest(
        created_at="2026-09-18T00:00:00.000000+00:00", source_ref=source_ref,
        operation=operation, document_ref=document_ref,
        target_ref="host-tool:market-digest-output",
        governance_ref="connector-governance:sales-notes-get-note:v1",
        governance_hash="a" * 64, doc_kind="sell_side_note",
        evidence_tier="sell_side", doc_date="2026-09-01",
        origin_ref="market-digest:2026-09-01:PM", subject_tickers=["ACN"],
        text=text,
        assembled_object={"content_hash": digest, "size_bytes": len(text.encode("utf-8")),
                          "storage_locator": f"spool:objects/{digest[:2]}/{digest}"},
    )


def candidate_evidence_stub(*, source_ref, source_type="authenticated_library"):
    """One minimally valid CandidateEvidence, for the relabel refusals."""

    from dalton_core.research_verification import validate_candidate_evidence

    base = {
        "schema_version": "0.1",
        "id": "candidate-evidence-version:" + "0" * 64,
        "created_at": "2026-09-18T00:00:00.000000+00:00",
        "candidate_evidence_ref": "candidate-evidence:fixture:1",
        "version": 1, "source_type": source_type, "source_ref": source_ref,
        "source_envelope_ref": "source-envelope:fixture:1",
        "source_envelope_hash": "b" * 64,
        "artifact_refs": [{"ref": "artifact:fixture:1", "hash": "c" * 64}],
        "retrieved_at": "2026-09-18T00:00:00.000000+00:00", "valid_until": None,
        "source_lineage": [source_ref, "source-envelope:fixture:1"],
        "independence_group": "independence:" + source_ref,
        "source_verification_ref": "verification-bundle:fixture:1",
        "source_verification_hash": "d" * 64,
        "actor_ref": "automation:coverage-mission", "prior_version_ref": None,
    }
    return validate_candidate_evidence({**base, "content_hash": content_hash(base)})


def citation_binding_stub():
    """One valid, eligible citation binding, for the relabel refusals."""

    from dalton_core.transcript_correction import (
        validate_transcript_claim_citation_binding,
    )

    identity = {
        "correction_set_version_ref": "transcript-correction-set-version:" + "e" * 32,
        "correction_set_version_hash": "f" * 64,
        "source_start": 0, "source_end": 20,
    }
    base = {
        "schema_version": "0.1",
        "id": "transcript-claim-citation-binding:" + content_hash(identity)[:32],
        "created_at": "2026-09-18T00:00:00.000000+00:00",
        "source_manifest_ref": "feed-document-acquisition:" + "0" * 64,
        "source_manifest_hash": "1" * 64, "source_content_hash": "2" * 64,
        "source_start": 0, "source_end": 20, "source_sha256": "3" * 64,
        **identity,
        "accepted_correction_indexes": [], "unresolved_correction_indexes": [],
        "citation_mode": "raw_span", "claim_eligible": True,
        "blocking_reason": None,
        "actor_ref": "core:transcript-correction-citation-gate",
    }
    return validate_transcript_claim_citation_binding(
        {**base, "content_hash": content_hash(base)})


class TableTests(unittest.TestCase):
    """The vocabulary itself: one entry per source, no overlap, no gaps."""

    def test_every_source_has_exactly_one_kind_type_and_operation(self) -> None:
        self.assertEqual(sorted(ACQUIRED_SOURCE_KINDS), [
            "company_wiki", "guidepoint", "prior_research", "sales_notes"])
        self.assertEqual(len(ACQUIRED_SOURCE_KIND_BY_SOURCE_REF), 4)
        self.assertEqual(len(ACQUIRED_SOURCE_KIND_BY_EVIDENCE_TYPE), 4)
        self.assertEqual(len(ACQUIRED_EVIDENCE_SOURCE_TYPES), 4)
        for entry in ACQUIRED_SOURCE_KINDS.values():
            self.assertIn(entry["record_binding"], ("exact", "member"))
            self.assertTrue(entry["document_prefix"])
            self.assertTrue(entry["operation"])

    def test_every_kind_is_in_the_staging_and_citation_vocabularies(self) -> None:
        from dalton_core.document_extraction import STAGEABLE_SOURCE_REFS
        from dalton_core.transcript_candidate_staging import (
            SOURCE_KIND_MODES,
            SOURCE_KINDS,
        )
        from dalton_core.transcript_correction import CITED_EVIDENCE_SOURCE_TYPES

        for kind, entry in ACQUIRED_SOURCE_KINDS.items():
            self.assertIn(kind, SOURCE_KINDS)
            self.assertIn(kind, SOURCE_KIND_MODES)
            self.assertIn(entry["source_ref"], STAGEABLE_SOURCE_REFS)
            self.assertIn(entry["evidence_source_type"], CITED_EVIDENCE_SOURCE_TYPES)

    def test_every_kind_has_an_importance_the_claim_index_knows(self) -> None:
        from dalton_core.claim_index_authority import IMPORTANCE_TIERS
        from dalton_core.claim_index_tagging import SOURCE_TYPE_IMPORTANCE

        for entry in ACQUIRED_SOURCE_KINDS.values():
            importance = SOURCE_TYPE_IMPORTANCE[entry["evidence_source_type"]]
            self.assertIn(importance, IMPORTANCE_TIERS)
        self.assertEqual(SOURCE_TYPE_IMPORTANCE["sell_side_note"], "sell_side")
        self.assertEqual(SOURCE_TYPE_IMPORTANCE["internal_wiki"], "internal_prior")
        self.assertEqual(SOURCE_TYPE_IMPORTANCE["internal_prior_research"], "internal_prior")
        # No ``expert`` rung exists; ranking a paid expert call is an owner
        # decision, and ``other`` is the honest answer until it is made.
        self.assertNotIn("expert", IMPORTANCE_TIERS)
        self.assertEqual(SOURCE_TYPE_IMPORTANCE["expert_network"], "other")

    def test_the_two_pre_existing_sources_are_not_in_this_table(self) -> None:
        self.assertIsNone(acquired_source_kind("source:alphaengine"))
        self.assertIsNone(acquired_source_kind("source:web-search"))
        self.assertIsNone(acquired_source_kind(None))
        self.assertFalse(is_acquired_source_manifest_ref(
            "alphaengine-document-acquisition:abc"))
        self.assertFalse(is_acquired_source_manifest_ref("public-web-fetch-manifest:abc"))


class ManifestValidationTests(unittest.TestCase):
    """A manifest that does not say what it is cannot be read as anything."""

    def test_a_valid_sales_note_manifest_resolves_to_its_kind(self) -> None:
        wire, entry = validate_acquired_source_manifest(feed_manifest())
        self.assertEqual(entry["evidence_source_type"], "sell_side_note")
        self.assertEqual(acquisition_object_hash(wire),
                         hashlib.sha256(TEXT.encode("utf-8")).hexdigest())

    def test_a_manifest_family_and_source_that_disagree_are_refused(self) -> None:
        manifest = feed_manifest()
        with self.assertRaises(AcquiredSourceAuthorityError):
            validate_acquired_source_manifest({**manifest, "source_ref": "source:alphaengine"})
        self.assertIsNone(acquired_kind_for_manifest_ref(
            manifest["id"], source_ref="source:alphaengine"))

    def test_a_note_acquired_through_another_sources_operation_is_refused(self) -> None:
        # ``get_document`` is the wiki's and prior research's read; a sales
        # note read through it is a manifest describing a call nobody made.
        manifest = feed_manifest(operation="get_document")
        with self.assertRaises(AcquiredSourceAuthorityError) as caught:
            validate_acquired_source_manifest(manifest)
        self.assertIn("operation", str(caught.exception))

    def test_a_document_ref_from_another_source_is_refused(self) -> None:
        manifest = feed_manifest(
            source_ref="source:company-wiki", operation="get_document",
            document_ref="sales-note:aaaa000000000001")
        with self.assertRaises(AcquiredSourceAuthorityError) as caught:
            validate_acquired_source_manifest(manifest)
        self.assertIn("document_ref", str(caught.exception))

    def test_something_that_is_not_a_manifest_at_all_is_refused(self) -> None:
        for value in (None, {}, {"id": "feed-document-acquisition:x"}):
            with self.assertRaises(AcquiredSourceAuthorityError):
                validate_acquired_source_manifest(value)


class EnvelopeBindingTests(unittest.TestCase):
    """Which SourceEnvelope counts as the acquisition of which document."""

    def test_a_one_document_read_must_name_exactly_that_document(self) -> None:
        self.assertTrue(acquired_source_binding_is_exact(
            evidence_source_type="sell_side_note",
            source_record_refs=["sales-note:aaaa000000000001"],
            document_ref="sales-note:aaaa000000000001",
            source="source:sales-notes", operation="get_note"))
        # Another document in the same envelope is not this one.
        self.assertFalse(acquired_source_binding_is_exact(
            evidence_source_type="sell_side_note",
            source_record_refs=["sales-note:aaaa000000000001", "sales-note:b"],
            document_ref="sales-note:aaaa000000000001",
            source="source:sales-notes", operation="get_note"))

    def test_a_guidepoint_search_may_name_the_page_it_came_from(self) -> None:
        excerpt = "guidepoint-excerpt:sha256:" + "a" * 64
        self.assertTrue(acquired_source_binding_is_exact(
            evidence_source_type="expert_network",
            source_record_refs=["guidepoint-excerpt:sha256:" + "b" * 64, excerpt],
            document_ref=excerpt,
            source="source:guidepoint", operation="search_library"))
        self.assertFalse(acquired_source_binding_is_exact(
            evidence_source_type="expert_network",
            source_record_refs=["guidepoint-excerpt:sha256:" + "b" * 64],
            document_ref=excerpt,
            source="source:guidepoint", operation="search_library"))

    def test_the_wrong_source_operation_or_prefix_never_binds(self) -> None:
        note = "sales-note:aaaa000000000001"
        for kwargs in (
            {"source": "source:company-wiki"},
            {"operation": "get_document"},
            {"document_ref": "company-wiki-doc:sha256:" + "a" * 64},
            {"evidence_source_type": "public_web"},
            {"source_record_refs": "not-a-list"},
        ):
            base = {"evidence_source_type": "sell_side_note",
                    "source_record_refs": [note], "document_ref": note,
                    "source": "source:sales-notes", "operation": "get_note"}
            self.assertFalse(acquired_source_binding_is_exact(**{**base, **kwargs}), kwargs)


class EvidenceRelabelTests(unittest.TestCase):
    """The evidence type says what the original is; it may not be moved."""

    def test_a_wiki_page_cannot_be_labelled_an_expert_call(self) -> None:
        from dalton_core.transcript_correction import (
            TranscriptCorrectionValidationError,
            bind_candidate_evidence_to_transcript_citation,
        )

        evidence = candidate_evidence_stub(source_ref="source:company-wiki")
        with self.assertRaises(TranscriptCorrectionValidationError) as caught:
            bind_candidate_evidence_to_transcript_citation(
                evidence, {}, source_type="expert_network")
        self.assertIn("does not match the acquired source", str(caught.exception))

    def test_an_alphaengine_transcript_still_cannot_be_relabelled(self) -> None:
        from dalton_core.transcript_correction import (
            TranscriptCorrectionValidationError,
            bind_candidate_evidence_to_transcript_citation,
        )

        evidence = candidate_evidence_stub(
            source_ref="source:alphaengine", source_type="authenticated_transcript")
        with self.assertRaises(TranscriptCorrectionValidationError) as caught:
            bind_candidate_evidence_to_transcript_citation(
                evidence, citation_binding_stub(), source_type="public_web")
        self.assertIn("cannot be rebound as transcript", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
