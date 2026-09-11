from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from dalton_core.document_research import (
    DocumentResearchAccessDenied,
    DocumentResearchConflict,
    DocumentResearchError,
    DocumentResearchRegistry,
    FeedDocumentSourceAdapter,
    READ_OPERATION,
    READ_REQUEST_SCHEMA_VERSION,
    SEARCH_OPERATION,
    SEARCH_REQUEST_SCHEMA_VERSION,
    build_document_research_policy,
)
from dalton_core.feed_acquisition import (
    COMPANY_WIKI_SOURCE_REF,
    SALES_NOTES_SOURCE_REF,
    build_feed_acquisition_manifest,
)
from dalton_core.host_tool_runner import (
    ACCESS_POLICY_REF,
    RETENTION_POLICY_REF,
    TERMS_POLICY_REF,
)
from dalton_core.raw_spool import RawSpool
from dalton_core.store import content_hash


def record(body: dict) -> dict:
    return {**body, "content_hash": content_hash(body)}


class FakeReceiptReader:
    def __init__(self, *, source_ref: str, access_policy_ref: str = ACCESS_POLICY_REF):
        profile_body = {
            "id": f"connector-profile:test:{source_ref}",
            "source_identity": {
                "source_ref": source_ref,
                "source_type": "authenticated_library",
                "source_version": "fixture-1",
            },
            "access_policy_ref": access_policy_ref,
            "retention_policy_ref": RETENTION_POLICY_REF,
            "terms_policy_ref": TERMS_POLICY_REF,
        }
        self.profile = record(profile_body)
        invocation_body = {
            "id": f"connector-invocation:test:{source_ref}",
            "connector_profile_ref": self.profile["id"],
            "connector_profile_hash": self.profile["content_hash"],
        }
        self.invocation = record(invocation_body)

    def get_invocation(self, ref: str):
        return self.invocation if ref == self.invocation["id"] else None

    def get_profile(self, ref: str):
        return self.profile if ref == self.profile["id"] else None


class FakeLauncher:
    def __init__(self, manifest: dict, ticket_ref: str):
        self.manifest = manifest
        self.ticket_ref = ticket_ref

    def read_completed_manifest(self, ticket_ref: str, document_ref: str):
        if ticket_ref != self.ticket_ref or document_ref != self.manifest["document_ref"]:
            raise DocumentResearchConflict("ticket does not bind document")
        return self.manifest

    def locate_completed_manifest(self, document_ref: str):
        if document_ref != self.manifest["document_ref"]:
            raise DocumentResearchConflict("document has no complete acquisition")
        return self.manifest


class FakeSpool:
    def __init__(self, inner: RawSpool, replacement: bytes):
        self.inner = inner
        self.replacement = replacement

    def read_object(self, content_hash_value: str) -> bytes:
        self.inner.read_object(content_hash_value)
        return self.replacement


class DocumentResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.spool = RawSpool(str(Path(self.temp.name) / "spool"), max_total_bytes=5_000_000)
        self.policy = build_document_research_policy(
            policy_ref="policy:document-research:test:0.1",
            allowed_purposes=["qualitative_research"],
            allowed_access_policy_refs=[ACCESS_POLICY_REF],
            max_question_chars=2_000,
            max_query_terms=12,
            max_query_term_chars=200,
            max_results=10,
            max_context_before_chars=80,
            max_context_after_chars=120,
            max_read_chars=10_000,
        )

    def _source(
        self, *, source_ref: str, document_ref: str, text: str,
        ticket_ref: str, doc_kind: str,
        access_policy_ref: str = ACCESS_POLICY_REF,
    ):
        sink = self.spool.open_sink(
            "raw-sink:" + hashlib.sha256(
                f"{source_ref}:{document_ref}".encode("utf-8")
            ).hexdigest(),
            max_response_bytes=1_000_000,
        )
        sink.write(text.encode("utf-8"))
        assembled = sink.finalize().to_dict()
        receipts = FakeReceiptReader(
            source_ref=source_ref, access_policy_ref=access_policy_ref
        )
        manifest = build_feed_acquisition_manifest(
            created_at="2026-09-11T12:00:00.000000+00:00",
            source_ref=source_ref,
            operation=("get_note" if source_ref == SALES_NOTES_SOURCE_REF else "get_document"),
            document_ref=document_ref,
            target_ref=f"host-tool:{source_ref}",
            governance_ref=f"connector-governance:{source_ref}:get:approved",
            governance_hash="1" * 64,
            doc_kind=doc_kind,
            evidence_tier=("sell_side" if source_ref == SALES_NOTES_SOURCE_REF else "internal"),
            doc_date="2026-09-10",
            origin_ref=f"fixture:{source_ref}",
            subject_tickers=["ACN"],
            text=text,
            assembled_object=assembled,
            connector_invocation_ref=receipts.invocation["id"],
            connector_invocation_hash=receipts.invocation["content_hash"],
        )
        launcher = FakeLauncher(manifest, ticket_ref)
        adapter = FeedDocumentSourceAdapter(
            source_ref=source_ref,
            launcher=launcher,
            core=object(),
            spool=self.spool,
            receipt_reader=receipts,
        )
        return adapter, launcher, receipts

    def _registry(self, adapters):
        return DocumentResearchRegistry(adapters=adapters, policy=self.policy)

    def _registration(self, registry, source_ref, document_ref, ticket_ref):
        return registry.register(
            source_ref=source_ref,
            document_ref=document_ref,
            purpose="qualitative_research",
            acquisition_ticket_ref=ticket_ref,
        )

    def test_sales_note_search_binds_question_source_version_offsets_and_hash(self):
        text = (
            "September channel check. Cloud bookings accelerated in Europe. "
            "The analyst expects cloud backlog to convert over two quarters."
        )
        document_ref = "sales-note:fixture-1"
        ticket_ref = "feed-run:fixture-sales"
        adapter, _, _ = self._source(
            source_ref=SALES_NOTES_SOURCE_REF,
            document_ref=document_ref,
            text=text,
            ticket_ref=ticket_ref,
            doc_kind="broker_note",
        )
        registry = self._registry({SALES_NOTES_SOURCE_REF: adapter})
        registration = self._registration(
            registry, SALES_NOTES_SOURCE_REF, document_ref, ticket_ref
        )
        self.assertEqual(registration["source_version"], "fixture-1")
        self.assertEqual(registration["raw_source"]["status"], "not_bound")
        self.assertEqual(
            registration["normalized_text"]["text_sha256"],
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        proof = registry.search({
            "schema_version": SEARCH_REQUEST_SCHEMA_VERSION,
            "operation": SEARCH_OPERATION,
            "purpose": "qualitative_research",
            "research_question": "What is the timing of cloud backlog conversion?",
            "registration": registration,
            "query_terms": ["cloud backlog", "Europe"],
            "limits": {
                "max_results": 5,
                "context_before_chars": 20,
                "context_after_chars": 35,
            },
            "policy_ref": self.policy["policy_ref"],
            "policy_hash": self.policy["content_hash"],
        })
        self.assertEqual(len(proof["matches"]), 2)
        for match in proof["matches"]:
            self.assertEqual(
                match["excerpt"], text[match["source_start"]:match["source_end"]]
            )
            self.assertEqual(
                text[match["match_start"]:match["match_end"]].casefold(),
                match["matched_term"].casefold(),
            )
            self.assertIn(registration["id"], match["source_location"])
        self.assertEqual(
            proof["request"]["research_question"],
            "What is the timing of cloud backlog conversion?",
        )
        self.assertEqual(registry.verify_search_proof(proof), proof)
        altered = {**proof, "matches": [dict(proof["matches"][0])]}
        altered["matches"][0]["excerpt"] = "a derived Claim"
        with self.assertRaises(DocumentResearchConflict):
            registry.verify_search_proof(altered)

    def test_company_wiki_can_be_read_in_full_without_using_claims(self):
        text = "# Delivery model\n\nThe company moved two practices into one operating unit."
        document_ref = "wiki-doc:sha256:" + "2" * 64
        ticket_ref = "feed-run:fixture-wiki"
        adapter, _, _ = self._source(
            source_ref=COMPANY_WIKI_SOURCE_REF,
            document_ref=document_ref,
            text=text,
            ticket_ref=ticket_ref,
            doc_kind="company_note",
        )
        registry = self._registry({COMPANY_WIKI_SOURCE_REF: adapter})
        registration = self._registration(
            registry, COMPANY_WIKI_SOURCE_REF, document_ref, ticket_ref
        )
        proof = registry.read({
            "schema_version": READ_REQUEST_SCHEMA_VERSION,
            "operation": READ_OPERATION,
            "purpose": "qualitative_research",
            "research_question": "How did the delivery organization change?",
            "registration": registration,
            "source_start": 0,
            "source_end": len(text),
            "policy_ref": self.policy["policy_ref"],
            "policy_hash": self.policy["content_hash"],
        })
        self.assertEqual(proof["text"], text)
        self.assertEqual(proof["source_start"], 0)
        self.assertEqual(proof["source_end"], len(text))
        self.assertEqual(registry.verify_read_proof(proof), proof)
        with self.assertRaisesRegex(DocumentResearchError, "no document research adapter"):
            registry.register(
                source_ref="source:claims",
                document_ref="claim:anything",
                purpose="qualitative_research",
            )

    def test_source_bytes_are_reverified_for_every_search(self):
        document_ref = "sales-note:fixture-tamper"
        ticket_ref = "feed-run:fixture-tamper"
        adapter, launcher, receipts = self._source(
            source_ref=SALES_NOTES_SOURCE_REF,
            document_ref=document_ref,
            text="Original channel detail.",
            ticket_ref=ticket_ref,
            doc_kind="broker_note",
        )
        registry = self._registry({SALES_NOTES_SOURCE_REF: adapter})
        registration = self._registration(
            registry, SALES_NOTES_SOURCE_REF, document_ref, ticket_ref
        )
        tampered = FeedDocumentSourceAdapter(
            source_ref=SALES_NOTES_SOURCE_REF,
            launcher=launcher,
            core=object(),
            spool=FakeSpool(self.spool, b"Substituted claim text."),
            receipt_reader=receipts,
        )
        registry.adapters[SALES_NOTES_SOURCE_REF] = tampered
        with self.assertRaisesRegex(Exception, "drifted from its manifest"):
            registry.search({
                "schema_version": SEARCH_REQUEST_SCHEMA_VERSION,
                "operation": SEARCH_OPERATION,
                "purpose": "qualitative_research",
                "research_question": "What did the channel say?",
                "registration": registration,
                "query_terms": ["channel"],
                "limits": {
                    "max_results": 1,
                    "context_before_chars": 5,
                    "context_after_chars": 5,
                },
                "policy_ref": self.policy["policy_ref"],
                "policy_hash": self.policy["content_hash"],
            })

    def test_access_policy_is_checked_without_per_question_signature(self):
        adapter, _, _ = self._source(
            source_ref=COMPANY_WIKI_SOURCE_REF,
            document_ref="wiki-doc:sha256:" + "3" * 64,
            text="Private operating note.",
            ticket_ref="feed-run:private",
            doc_kind="company_note",
            access_policy_ref="policy:access:not-granted",
        )
        registry = self._registry({COMPANY_WIKI_SOURCE_REF: adapter})
        with self.assertRaises(DocumentResearchAccessDenied):
            registry.register(
                source_ref=COMPANY_WIKI_SOURCE_REF,
                document_ref="wiki-doc:sha256:" + "3" * 64,
                purpose="qualitative_research",
                acquisition_ticket_ref="feed-run:private",
            )

    def test_metadata_only_or_historical_unbound_manifest_is_not_text_authority(self):
        adapter, launcher, receipts = self._source(
            source_ref=SALES_NOTES_SOURCE_REF,
            document_ref="sales-note:fixture-metadata",
            text="Acquired note.",
            ticket_ref="feed-run:metadata",
            doc_kind="broker_note",
        )
        manifest = dict(launcher.manifest)
        body = {key: item for key, item in manifest.items() if key != "content_hash"}
        body["connector_invocation_ref"] = None
        body["connector_invocation_hash"] = None
        manifest = {**body, "content_hash": content_hash(body)}
        launcher.manifest = manifest
        registry = self._registry({SALES_NOTES_SOURCE_REF: adapter})
        with self.assertRaisesRegex(DocumentResearchConflict, "requires.*invocation"):
            registry.register(
                source_ref=SALES_NOTES_SOURCE_REF,
                document_ref=manifest["document_ref"],
                purpose="qualitative_research",
                acquisition_ticket_ref="feed-run:metadata",
            )

    def test_profile_source_and_policy_identity_cannot_be_relabelled(self):
        adapter, _, receipts = self._source(
            source_ref=SALES_NOTES_SOURCE_REF,
            document_ref="sales-note:fixture-profile",
            text="Bound report body.",
            ticket_ref="feed-run:profile",
            doc_kind="broker_note",
        )
        receipts.profile["source_identity"]["source_ref"] = COMPANY_WIKI_SOURCE_REF
        registry = self._registry({SALES_NOTES_SOURCE_REF: adapter})
        with self.assertRaisesRegex(DocumentResearchConflict, "profile source"):
            registry.register(
                source_ref=SALES_NOTES_SOURCE_REF,
                document_ref="sales-note:fixture-profile",
                purpose="qualitative_research",
                acquisition_ticket_ref="feed-run:profile",
            )


if __name__ == "__main__":
    unittest.main()
