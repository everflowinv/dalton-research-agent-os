from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dalton_core.document_research import (
    DocumentResearchAccessDenied,
    DocumentResearchConflict,
    DocumentResearchError,
    DocumentResearchRegistry,
    AlphaEngineDocumentSourceAdapter,
    CoreAcquiredDocumentSourceAdapter,
    FeedDocumentSourceAdapter,
    PublicWebDocumentSourceAdapter,
    READ_OPERATION,
    READ_REQUEST_SCHEMA_VERSION,
    SEARCH_OPERATION,
    SEARCH_REQUEST_SCHEMA_VERSION,
    build_document_research_policy,
    build_document_research_registry,
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
from tests.test_document_extraction import (
    ExtractionHarness, NEW_DOC, ORIGINAL, TICKET as ALPHA_TICKET,
)
from dalton_core.public_web_fetch_launcher import PublicWebFetchLauncher
from dalton_core.public_web_fetch_launcher import ReadOnlyPublicWebFetchManifestReader
from dalton_core.alphaengine_acquisition_launcher import ReadOnlyAlphaEngineManifestReader
from dalton_core.feed_launcher import FeedLaunchRejected, ReadOnlyFeedManifestReader
from dalton_core.capability_catalog import CapabilityCatalog
from dalton_core.connector_authority_port import ConnectorCompletionReceiptReader
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.public_http_transport import PublicHttpTransport
from dalton_core.public_web_core_fetch import (
    PublicWebCoreFetch,
    WebFetchConnectorGovernance,
    build_web_fetch_governance_record,
    url_authority_from_discovery,
)
from tests.test_research_plan_annual_report import _Response
from tests.test_sec_filings_index_core import Harness as SecIndexHarness, SPEC as SEC_SPEC
from tests.test_public_web_extraction_source import PAGE
from tests.test_public_web_fetch_lane import FetchHarness, URL_A, transport_for


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
    def __init__(
        self, manifest: dict, ticket_ref: str, *, lookup_ref: str | None = None,
        state_dir: str | Path | None = None,
    ):
        self.ticket_ref = ticket_ref
        self.manifests = {ticket_ref: manifest}
        self.lookup_refs = {ticket_ref: lookup_ref or manifest["document_ref"]}
        self.state_dir = Path(state_dir or "/fixture-state").resolve()

    @property
    def manifest(self):
        return self.manifests[self.ticket_ref]

    @manifest.setter
    def manifest(self, value):
        self.manifests[self.ticket_ref] = value

    def add_version(self, ticket_ref: str, manifest: dict, *, lookup_ref: str | None = None):
        self.ticket_ref = ticket_ref
        self.manifests[ticket_ref] = manifest
        self.lookup_refs[ticket_ref] = lookup_ref or manifest["document_ref"]

    def read_completed_manifest(self, ticket_ref: str, document_ref: str):
        manifest = self.manifests.get(ticket_ref)
        if manifest is None or document_ref != self.lookup_refs[ticket_ref]:
            raise DocumentResearchConflict("ticket does not bind document")
        return manifest

    def locate_completed_manifest(self, document_ref: str):
        if document_ref != self.lookup_refs[self.ticket_ref]:
            raise DocumentResearchConflict("document has no complete acquisition")
        return self.manifest

    def locate_completed_manifest_binding(self, document_ref: str):
        return {
            "ticket_ref": self.ticket_ref,
            "manifest": self.locate_completed_manifest(document_ref),
        }


class FakeCoreRows:
    def __init__(self, *rows: dict):
        self.rows = {row["record_id"]: dict(row) for row in rows}
        self.connection = self

    def execute(self, query: str, params: tuple):
        if "coverage_mission_discovered_documents" not in query or len(params) != 1:
            raise AssertionError("unexpected Core acquired-document query")
        row = self.rows.get(params[0])

        class Result:
            def fetchall(self):
                return [] if row is None else [dict(row)]

        return Result()


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
        unavailable = registry.inspect(
            source_ref="source:claims",
            document_ref="claim:anything",
            purpose="qualitative_research",
        )
        self.assertFalse(unavailable["available"])
        self.assertEqual(unavailable["reason"], "source_not_readable")

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

    def test_registration_pins_located_ticket_when_a_newer_acquisition_arrives(self):
        document_ref = "sales-note:fixture-versioned"
        adapter, launcher, receipts = self._source(
            source_ref=SALES_NOTES_SOURCE_REF,
            document_ref=document_ref,
            text="Old version says backlog improved.",
            ticket_ref="feed-run:old",
            doc_kind="broker_note",
        )
        registry = self._registry({SALES_NOTES_SOURCE_REF: adapter})
        registration = registry.register(
            source_ref=SALES_NOTES_SOURCE_REF,
            document_ref=document_ref,
            purpose="qualitative_research",
        )
        self.assertEqual(registration["acquisition_ticket_ref"], "feed-run:old")

        sink = self.spool.open_sink("raw-sink:" + "7" * 64, max_response_bytes=1000)
        sink.write(b"New version says backlog declined.")
        assembled = sink.finalize().to_dict()
        newer = build_feed_acquisition_manifest(
            created_at="2026-09-11T13:00:00.000000+00:00",
            source_ref=SALES_NOTES_SOURCE_REF,
            operation="get_note",
            document_ref=document_ref,
            target_ref="host-tool:sales-notes",
            governance_ref="connector-governance:sales-notes:get:approved",
            governance_hash="1" * 64,
            doc_kind="broker_note",
            evidence_tier="sell_side",
            doc_date="2026-09-10",
            origin_ref="fixture:sales-notes",
            subject_tickers=["ACN"],
            text="New version says backlog declined.",
            assembled_object=assembled,
            connector_invocation_ref=receipts.invocation["id"],
            connector_invocation_hash=receipts.invocation["content_hash"],
        )
        launcher.add_version("feed-run:new", newer)
        proof = registry.read({
            "schema_version": READ_REQUEST_SCHEMA_VERSION,
            "operation": READ_OPERATION,
            "purpose": "qualitative_research",
            "research_question": "What did the earlier note say?",
            "registration": registration,
            "source_start": 0,
            "source_end": registration["normalized_text"]["characters"],
            "policy_ref": self.policy["policy_ref"],
            "policy_hash": self.policy["content_hash"],
        })
        self.assertEqual(proof["text"], "Old version says backlog improved.")

    def test_core_acquired_feed_registration_binds_mission_company_and_ticket(self):
        document_ref = "sales-note:fixture-core-row"
        ticket_ref = "feed-run:core-row"
        adapter, _, _ = self._source(
            source_ref=SALES_NOTES_SOURCE_REF,
            document_ref=document_ref,
            text="The channel expects a two-quarter conversion window.",
            ticket_ref=ticket_ref,
            doc_kind="broker_note",
        )
        record_id = "mission-discovered-document:sales-note-fixture"
        core = FakeCoreRows({
            "record_id": record_id,
            "mission_version_ref": "coverage-mission-version:fixture",
            "company_ref": "company:fixture",
            "source_ref": SALES_NOTES_SOURCE_REF,
            "document_ref": document_ref,
            "ticket_ref": ticket_ref,
            "status": "acquired",
        })
        registry = DocumentResearchRegistry(
            adapters={SALES_NOTES_SOURCE_REF: adapter},
            policy=self.policy,
            acquired_document_adapter=CoreAcquiredDocumentSourceAdapter(
                core=core, adapters={SALES_NOTES_SOURCE_REF: adapter}
            ),
        )
        registration = registry.register_acquired_document(
            record_id=record_id, purpose="qualitative_research"
        )
        authority = registration["source_authority"]
        self.assertEqual(authority["kind"], "coverage-mission-acquired-document")
        self.assertEqual(authority["ref"], record_id)
        self.assertRegex(authority["hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            authority["mission_version_ref"], "coverage-mission-version:fixture"
        )
        self.assertEqual(authority["company_ref"], "company:fixture")
        self.assertEqual(registration["acquisition_ticket_ref"], ticket_ref)
        self.assertTrue(registry.inspect_acquired_document(
            record_id=record_id, purpose="qualitative_research"
        )["available"])
        core.rows[record_id]["source_ref"] = COMPANY_WIKI_SOURCE_REF
        unavailable = registry.inspect_acquired_document(
            record_id=record_id, purpose="qualitative_research"
        )
        self.assertFalse(unavailable["available"])
        self.assertEqual(unavailable["reason"], "source_not_readable")

    def test_search_stops_consuming_matches_at_the_result_bound(self):
        document_ref = "sales-note:fixture-many-matches"
        adapter, _, _ = self._source(
            source_ref=SALES_NOTES_SOURCE_REF,
            document_ref=document_ref,
            text="term " * 10_000,
            ticket_ref="feed-run:many-matches",
            doc_kind="broker_note",
        )
        registry = self._registry({SALES_NOTES_SOURCE_REF: adapter})
        registration = self._registration(
            registry, SALES_NOTES_SOURCE_REF, document_ref, "feed-run:many-matches"
        )

        class Match:
            def __init__(self, start):
                self._start = start

            def start(self):
                return self._start

            def end(self):
                return self._start + 4

        class BoundedPattern:
            def finditer(self, _text):
                yield Match(0)
                raise AssertionError("search consumed a match after max_results")

        with mock.patch("dalton_core.document_research.re.compile", return_value=BoundedPattern()):
            proof = registry.search({
                "schema_version": SEARCH_REQUEST_SCHEMA_VERSION,
                "operation": SEARCH_OPERATION,
                "purpose": "qualitative_research",
                "research_question": "Where is the first term?",
                "registration": registration,
                "query_terms": ["term"],
                "limits": {
                    "max_results": 1,
                    "context_before_chars": 0,
                    "context_after_chars": 0,
                },
                "policy_ref": self.policy["policy_ref"],
                "policy_hash": self.policy["content_hash"],
            })
        self.assertEqual(len(proof["matches"]), 1)

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

    def test_factory_binds_launchers_to_one_state_without_opening_new_authority(self):
        document_ref = "sales-note:fixture-factory"
        adapter, launcher, receipts = self._source(
            source_ref=SALES_NOTES_SOURCE_REF,
            document_ref=document_ref,
            text="Factory source text.",
            ticket_ref="feed-run:factory",
            doc_kind="broker_note",
        )
        del adapter
        launcher.state_dir = Path(self.temp.name).resolve()
        registry = build_document_research_registry(
            core=object(),
            state_dir=self.temp.name,
            spool=self.spool,
            receipt_reader=receipts,
            policy=self.policy,
            feed_launchers={SALES_NOTES_SOURCE_REF: launcher},
            alphaengine_launcher=None,
            public_web_launcher=None,
            public_web_source_refs=[],
            source_reading_limits={
                "alphaengine_max_document_chars": 100_000,
                "public_web_max_source_chars": 100_000,
                "public_web_max_pdf_pages": 20,
                "public_web_max_decompressed_bytes": 1_000_000,
            },
        )
        self.assertTrue(registry.inspect(
            source_ref=SALES_NOTES_SOURCE_REF,
            document_ref=document_ref,
            purpose="qualitative_research",
            acquisition_ticket_ref="feed-run:factory",
        )["available"])
        launcher.state_dir = Path(self.temp.name, "other")
        with self.assertRaisesRegex(DocumentResearchConflict, "different state"):
            build_document_research_registry(
                core=object(), state_dir=self.temp.name, spool=self.spool,
                receipt_reader=receipts, policy=self.policy,
                feed_launchers={SALES_NOTES_SOURCE_REF: launcher},
                alphaengine_launcher=None, public_web_launcher=None,
                public_web_source_refs=[],
                source_reading_limits={
                    "alphaengine_max_document_chars": 100_000,
                    "public_web_max_source_chars": 100_000,
                    "public_web_max_pdf_pages": 20,
                    "public_web_max_decompressed_bytes": 1_000_000,
                },
            )

    def test_read_only_feed_manifest_reader_reopens_without_state_mutation(self):
        document_ref = "sales-note:fixture-read-only"
        adapter, launcher, _ = self._source(
            source_ref=SALES_NOTES_SOURCE_REF,
            document_ref=document_ref,
            text="Read-only source text.",
            ticket_ref="feed-run:source",
            doc_kind="broker_note",
        )
        del adapter
        ticket_ref = "sales-notes-run:" + "6" * 24
        tickets_dir = Path(self.temp.name) / "feed-acquisitions-sales-notes"
        directory = tickets_dir / ("6" * 24)
        directory.mkdir(parents=True, mode=0o700)
        for name, value in {
            "ticket.json": {
                "id": ticket_ref, "status": "succeeded",
                "document_ref": document_ref,
                "source_ref": SALES_NOTES_SOURCE_REF,
                "started_at": "2026-09-11T12:00:00.000000+00:00",
            },
            "summary.json": {
                "status": "succeeded", "document_ref": document_ref,
                "source_ref": SALES_NOTES_SOURCE_REF,
                "manifest_ref": launcher.manifest["id"],
                "manifest_hash": launcher.manifest["content_hash"],
            },
            "manifest.json": launcher.manifest,
        }.items():
            path = directory / name
            path.write_text(__import__("json").dumps(value), encoding="utf-8")
            path.chmod(0o600)
        before = {
            path: (
                path.stat().st_mode,
                path.stat().st_mtime_ns,
                path.stat().st_ctime_ns,
            )
            for path in (Path(self.temp.name), tickets_dir, directory)
        }
        reader = ReadOnlyFeedManifestReader(
            state_dir=self.temp.name, source_ref=SALES_NOTES_SOURCE_REF
        )
        binding = reader.locate_completed_manifest_binding(document_ref)
        self.assertEqual(binding["ticket_ref"], ticket_ref)
        self.assertEqual(binding["manifest"], launcher.manifest)
        after = {
            path: (
                path.stat().st_mode,
                path.stat().st_mtime_ns,
                path.stat().st_ctime_ns,
            )
            for path in before
        }
        self.assertEqual(after, before)
        state_link = Path(self.temp.name).with_name(
            Path(self.temp.name).name + "-state-link"
        )
        state_link.symlink_to(self.temp.name, target_is_directory=True)
        self.addCleanup(state_link.unlink)
        with self.assertRaisesRegex(FeedLaunchRejected, "symlink"):
            ReadOnlyFeedManifestReader(
                state_dir=state_link, source_ref=SALES_NOTES_SOURCE_REF
            )


class NetworkAcquisitionDocumentResearchTests(unittest.TestCase):
    @staticmethod
    def policy(access_policy_ref: str, *, max_read_chars: int = 100_000):
        return build_document_research_policy(
            policy_ref="policy:document-research:network-fixture:0.1",
            allowed_purposes=["qualitative_research"],
            allowed_access_policy_refs=[access_policy_ref],
            max_question_chars=2_000,
            max_query_terms=12,
            max_query_term_chars=200,
            max_results=10,
            max_context_before_chars=100,
            max_context_after_chars=200,
            max_read_chars=max_read_chars,
        )

    def test_public_web_adapter_replays_raw_body_renderer_and_discovery_source(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        harness = FetchHarness(Path(temp.name), transport=transport_for(PAGE))
        self.addCleanup(harness.close)
        discovery_receipt = harness.discover()
        fetched = harness.fetch.fetch(
            harness.fetch.build_request(harness.authority(discovery_receipt, URL_A))
        )
        manifest = harness.fetch.manifest(fetched)
        discovery = harness.fetch.receipts.get_source_envelope(
            manifest["discovery_source_envelope_ref"]
        )
        profile = harness.fetch.receipts.get_profile(manifest["connector_profile_ref"])
        ticket = "public-web-fetch:" + "8" * 24
        launcher = PublicWebFetchLauncher(
            state_dir=Path(temp.name), governance_path=Path(temp.name) / "unused.json"
        )
        self.addCleanup(launcher.close)
        directory = Path(temp.name) / "fetches" / ("8" * 24)
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        files = {
            "ticket.json": {"id": ticket, "status": "succeeded", "document_ref": URL_A,
                            "started_at": "2026-09-11T12:00:00.000000+00:00"},
            "summary.json": {"url_ref": URL_A, "canonical_url": manifest["canonical_url"],
                             "manifest_ref": manifest["id"],
                             "manifest_hash": manifest["content_hash"],
                             "status": "succeeded"},
            "manifest.json": manifest,
        }
        for name, value in files.items():
            path = directory / name
            path.write_text(__import__("json").dumps(value), encoding="utf-8")
            path.chmod(0o600)
        self.assertEqual(
            launcher.locate_completed_manifest_binding(URL_A)["ticket_ref"], ticket
        )
        directory_before = (
            launcher.tickets_dir.stat().st_mode,
            launcher.tickets_dir.stat().st_mtime_ns,
            launcher.tickets_dir.stat().st_ctime_ns,
        )
        launcher.close()
        launcher = ReadOnlyPublicWebFetchManifestReader(state_dir=temp.name)
        self.assertEqual(
            launcher.locate_completed_manifest_binding(URL_A)["ticket_ref"], ticket
        )
        self.assertEqual(
            (launcher.tickets_dir.stat().st_mode,
             launcher.tickets_dir.stat().st_mtime_ns,
             launcher.tickets_dir.stat().st_ctime_ns),
            directory_before,
        )
        adapter = PublicWebDocumentSourceAdapter(
            source_ref=discovery["source"],
            launcher=launcher,
            core=harness.core,
            spool=harness.spool,
            receipt_reader=harness.fetch.receipts,
            max_source_chars=20_000,
            max_pdf_pages=20,
            max_decompressed_bytes=100_000,
        )
        policy = self.policy(profile["access_policy_ref"])
        registry = DocumentResearchRegistry(
            adapters={discovery["source"]: adapter}, policy=policy
        )
        registration = registry.register(
            source_ref=discovery["source"],
            document_ref=URL_A,
            purpose="qualitative_research",
            acquisition_ticket_ref=ticket,
        )
        self.assertEqual(registration["document_ref"], URL_A)
        self.assertEqual(registration["content_document_ref"], manifest["document_ref"])
        self.assertEqual(
            registration["raw_source"]["content_hashes"], [manifest["body_sha256"]]
        )
        self.assertEqual(
            registration["normalized_text"]["renderer_ref"],
            "html-visible-blocks:0.1",
        )
        proof = registry.search({
            "schema_version": SEARCH_REQUEST_SCHEMA_VERSION,
            "operation": SEARCH_OPERATION,
            "purpose": "qualitative_research",
            "research_question": "When is the leadership change effective?",
            "registration": registration,
            "query_terms": ["effective immediately"],
            "limits": {
                "max_results": 1,
                "context_before_chars": 20,
                "context_after_chars": 20,
            },
            "policy_ref": policy["policy_ref"],
            "policy_hash": policy["content_hash"],
        })
        self.assertEqual(len(proof["matches"]), 1)
        self.assertIn("effective immediately", proof["matches"][0]["excerpt"])

    def test_public_web_adapter_rejects_a_truncated_rendering(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        body = b"<p>" + b"long source " * 200 + b"</p>"
        harness = FetchHarness(Path(temp.name), transport=transport_for(body))
        self.addCleanup(harness.close)
        receipt = harness.discover()
        fetched = harness.fetch.fetch(
            harness.fetch.build_request(harness.authority(receipt, URL_A))
        )
        manifest = harness.fetch.manifest(fetched)
        discovery = harness.fetch.receipts.get_source_envelope(
            manifest["discovery_source_envelope_ref"]
        )
        adapter = PublicWebDocumentSourceAdapter(
            source_ref=discovery["source"],
            launcher=FakeLauncher(
                manifest, "public-web-fetch:truncated", lookup_ref=URL_A
            ),
            core=harness.core,
            spool=harness.spool,
            receipt_reader=harness.fetch.receipts,
            max_source_chars=100,
            max_pdf_pages=20,
            max_decompressed_bytes=100_000,
        )
        profile = harness.fetch.receipts.get_profile(manifest["connector_profile_ref"])
        registry = DocumentResearchRegistry(
            adapters={discovery["source"]: adapter},
            policy=self.policy(profile["access_policy_ref"]),
        )
        with self.assertRaisesRegex(DocumentResearchConflict, "non-truncated"):
            registry.register(
                source_ref=discovery["source"],
                document_ref=URL_A,
                purpose="qualitative_research",
                acquisition_ticket_ref="public-web-fetch:truncated",
            )

    def test_alphaengine_adapter_replays_all_pages_and_assembled_text(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        harness = ExtractionHarness(Path(temp.name))
        self.addCleanup(harness.close)
        launcher = harness.writer.acquisition_launcher
        self.assertEqual(
            launcher.locate_completed_manifest_binding(NEW_DOC)["ticket_ref"],
            ALPHA_TICKET,
        )
        directory_before = (
            launcher.tickets_dir.stat().st_mode,
            launcher.tickets_dir.stat().st_mtime_ns,
            launcher.tickets_dir.stat().st_ctime_ns,
        )
        read_only_launcher = ReadOnlyAlphaEngineManifestReader(state_dir=temp.name)
        self.assertEqual(
            read_only_launcher.locate_completed_manifest_binding(NEW_DOC)["ticket_ref"],
            ALPHA_TICKET,
        )
        self.assertEqual(
            (read_only_launcher.tickets_dir.stat().st_mode,
             read_only_launcher.tickets_dir.stat().st_mtime_ns,
             read_only_launcher.tickets_dir.stat().st_ctime_ns),
            directory_before,
        )
        launcher = read_only_launcher
        first_page = harness.manifest["pages"][0]
        profile = harness.h.acquisition.receipts.get_profile(
            first_page["connector_profile_ref"]
        )
        adapter = AlphaEngineDocumentSourceAdapter(
            launcher=launcher,
            core=harness.h.core,
            spool=harness.h.spool,
            receipt_reader=harness.h.acquisition.receipts,
            max_document_chars=100_000,
        )
        policy = self.policy(profile["access_policy_ref"], max_read_chars=100_000)
        registry = DocumentResearchRegistry(
            adapters={"source:alphaengine": adapter}, policy=policy
        )
        registration = registry.register(
            source_ref="source:alphaengine",
            document_ref=NEW_DOC,
            purpose="qualitative_research",
            acquisition_ticket_ref=ALPHA_TICKET,
        )
        self.assertGreater(len(registration["raw_source"]["artifact_refs"]), 1)
        self.assertEqual(
            registration["normalized_text"]["text_sha256"],
            hashlib.sha256(ORIGINAL.encode("utf-8")).hexdigest(),
        )
        acquired_ref = "mission-discovered-document:alphaengine-fixture"
        acquired_core = FakeCoreRows({
            "record_id": acquired_ref,
            "mission_version_ref": "coverage-mission-version:alpha-fixture",
            "company_ref": "company:alpha-fixture",
            "source_ref": "source:alphaengine",
            "document_ref": NEW_DOC,
            "ticket_ref": ALPHA_TICKET,
            "status": "acquired",
        })
        acquired_registry = DocumentResearchRegistry(
            adapters={"source:alphaengine": adapter},
            policy=policy,
            acquired_document_adapter=CoreAcquiredDocumentSourceAdapter(
                core=acquired_core, adapters={"source:alphaengine": adapter}
            ),
        )
        acquired_registration = acquired_registry.register_acquired_document(
            record_id=acquired_ref, purpose="qualitative_research"
        )
        self.assertEqual(
            acquired_registration["source_authority"]["company_ref"],
            "company:alpha-fixture",
        )
        self.assertEqual(
            acquired_registration["acquisition_ticket_ref"], ALPHA_TICKET
        )
        proof = acquired_registry.search({
            "schema_version": SEARCH_REQUEST_SCHEMA_VERSION,
            "operation": SEARCH_OPERATION,
            "purpose": "qualitative_research",
            "research_question": "What did management say about client decisions?",
            "registration": acquired_registration,
            "query_terms": ["client decisions remain cautious"],
            "limits": {
                "max_results": 2,
                "context_before_chars": 25,
                "context_after_chars": 25,
            },
            "policy_ref": policy["policy_ref"],
            "policy_hash": policy["content_hash"],
        })
        self.assertEqual(len(proof["matches"]), 2)
        self.assertEqual(acquired_registry.verify_search_proof(proof), proof)

    def test_core_acquired_nonannual_sec_filing_keeps_core_source_and_body_version(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        harness = SecIndexHarness(root)
        self.addCleanup(harness.close)
        spec = {**SEC_SPEC, "form": "8-K"}
        index_receipt = harness.index.list_filings(harness.index.build_request(spec))
        filing_ref = "sec:filing:0001467373-25-000100"
        self.assertIn(filing_ref, index_receipt["source_record_refs"])
        authority = url_authority_from_discovery(
            harness.core.connection,
            harness.spool,
            url_ref=filing_ref,
            source_envelope_ref=index_receipt["source_envelope_ref"],
        )
        governance = WebFetchConnectorGovernance(
            build_web_fetch_governance_record(
                approved_by="human:test-owner", status="approved"
            )
        )
        fetch_catalog = CapabilityCatalog(
            root / "fetch-catalog.sqlite",
            approval_resolver=governance.approval,
            policy_resolver=governance.policy,
            clock=harness.clock,
        )
        self.addCleanup(fetch_catalog.close)
        body = (
            b"<html><body><h1>Current report</h1><p>The board appointed a new "
            b"chief operating officer effective immediately.</p></body></html>"
        )
        fetch = PublicWebCoreFetch(
            store=harness.core,
            connectors=harness.connectors,
            observability=harness.observability,
            journal=harness.journal,
            scheduler=harness.scheduler,
            catalog=fetch_catalog,
            spool=harness.spool,
            governance=governance,
            transport=PublicHttpTransport(
                resolver=lambda _host, _port: ("23.33.29.153",),
                exchange=lambda *_args: _Response(body),
            ),
            clock=harness.clock,
        )
        manifest = fetch.manifest(fetch.fetch(fetch.build_request(authority)))
        ticket = "public-web-fetch:" + "9" * 24
        launcher = PublicWebFetchLauncher(
            state_dir=root, governance_path=root / "unused.json"
        )
        self.addCleanup(launcher.close)
        directory = root / "fetches" / ("9" * 24)
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        for name, value in {
            "ticket.json": {
                "id": ticket, "status": "succeeded", "document_ref": filing_ref,
                "started_at": "2026-09-11T12:00:00.000000+00:00",
            },
            "summary.json": {
                "url_ref": filing_ref, "canonical_url": manifest["canonical_url"],
                "manifest_ref": manifest["id"], "manifest_hash": manifest["content_hash"],
                "status": "succeeded",
            },
            "manifest.json": manifest,
        }.items():
            path = directory / name
            path.write_text(__import__("json").dumps(value), encoding="utf-8")
            path.chmod(0o600)
        connection = harness.core.connection
        coverage_authority = CoverageMissionAuthority(harness.core)
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        acquired_ref = "mission-discovered-document:sec-8k-fixture"
        with coverage_authority._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_discovered_documents"
                "(record_id,mission_version_ref,company_ref,source_ref,document_ref,"
                "discovery_ref,status,ticket_ref,failure_reason,failure_retryable,"
                "created_at,updated_at,host) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    acquired_ref, "coverage-mission-version:fixture",
                    "company:sec-cik:0001467373", "source:sec-edgar", filing_ref,
                    "mission-source-discovery:fixture", "acquired", ticket, None, None,
                    "2026-09-11T12:00:00.000000+00:00",
                    "2026-09-11T12:00:00.000000+00:00", "www.sec.gov",
                ),
            )
        reader = ConnectorCompletionReceiptReader(
            connectors=harness.connectors, observability=harness.observability
        )
        profile = reader.get_profile(manifest["connector_profile_ref"])
        policy = self.policy(profile["access_policy_ref"])
        registry = build_document_research_registry(
            core=harness.core, state_dir=root, spool=harness.spool,
            receipt_reader=reader, policy=policy, feed_launchers={},
            alphaengine_launcher=None, public_web_launcher=launcher,
            public_web_source_refs=[],
            source_reading_limits={
                "alphaengine_max_document_chars": 20_000,
                "public_web_max_source_chars": 20_000,
                "public_web_max_pdf_pages": 20,
                "public_web_max_decompressed_bytes": 100_000,
            },
        )
        unavailable = registry.inspect_acquired_document(
            record_id="mission-discovered-document:missing",
            purpose="qualitative_research",
        )
        self.assertFalse(unavailable["available"])
        self.assertEqual(unavailable["reason"], "source_not_readable")
        registration = registry.register_acquired_document(
            record_id=acquired_ref, purpose="qualitative_research"
        )
        self.assertEqual(registration["source_ref"], "source:sec-edgar")
        self.assertEqual(registration["document_ref"], filing_ref)
        self.assertEqual(registration["content_document_ref"], manifest["document_ref"])
        self.assertEqual(
            registration["source_authority"]["company_ref"],
            "company:sec-cik:0001467373",
        )
        available = registry.inspect_acquired_document(
            record_id=acquired_ref, purpose="qualitative_research"
        )
        self.assertTrue(available["available"])
        self.assertEqual(available["registration"], registration)
        with coverage_authority._transaction() as cur:
            cur.execute(
                "UPDATE coverage_mission_discovered_documents SET updated_at=? "
                "WHERE record_id=?",
                ("2026-09-11T13:00:00.000000+00:00", acquired_ref),
            )
        proof = registry.search({
            "schema_version": SEARCH_REQUEST_SCHEMA_VERSION,
            "operation": SEARCH_OPERATION,
            "purpose": "qualitative_research",
            "research_question": "Which executive did the board appoint?",
            "registration": registration,
            "query_terms": ["chief operating officer"],
            "limits": {
                "max_results": 1, "context_before_chars": 20,
                "context_after_chars": 30,
            },
            "policy_ref": policy["policy_ref"],
            "policy_hash": policy["content_hash"],
        })
        self.assertEqual(len(proof["matches"]), 1)
        self.assertEqual(registry.verify_search_proof(proof), proof)


if __name__ == "__main__":
    unittest.main()
