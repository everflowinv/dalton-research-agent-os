"""S1: the two human / vendor feeds -- identity, children, attribution, lane.

Everything here runs offline against synthetic fixtures. No real mail and no
real wiki document is committed to this repository; the digest files under
``tests/fixtures/s1_feeds`` are four invented notes from two invented banks,
and the wiki corpus is built into a temp directory from a small JSON
description at test time.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest import mock

from dalton_core import coverage_mission as coverage_mission_module
from dalton_core.company_wiki_core import (
    CompanyWikiError,
    classify_doc_type,
    company_wiki_identity,
    company_wiki_schema_hash,
    company_wiki_source_hash,
    document_ref as wiki_document_ref,
    enumerate_documents,
    read_document,
)
from dalton_core.connector_governance import (
    COMPANY_WIKI_GET_KIND,
    COMPANY_WIKI_LIST_KIND,
    SALES_NOTES_GET_KIND,
    SALES_NOTES_LIST_KIND,
    ConnectorGovernance,
    build_governance_record,
)
from dalton_core.connector import ConnectorStore
from dalton_core.connector_authority_port import ConnectorCompletionReceiptReader
from dalton_core.connector_inventory import load_packaged_connector_inventory
from dalton_core.observability import ObservabilityStore
from dalton_core.coverage_mission import (
    CoverageMissionAuthority,
    CoverageMissionConflict,
    validate_mission_source_discovery,
)
from dalton_core.feed_acquisition import (
    FeedManifestError,
    FeedSourceConflict,
    validate_feed_acquisition_manifest,
    verified_feed_source,
)
from dalton_core.feed_launcher import (
    CompanyWikiFeedLauncher,
    FeedLaunchRejected,
    SalesNotesFeedLauncher,
)
from dalton_core.mission_feed_lane import (
    FEED_DISCOVERY_SOURCES,
    FeedDiscoveryCoordinator,
    FeedLaneError,
    FeedLaneRejected,
    attribute_body,
    attribute_notes,
    attribute_wiki_documents,
    build_feed_runner,
    feed_discovery_parameters,
    feed_query_hash,
    load_feed_discovery_plan,
    plan_terms,
    triage_notes,
    validate_feed_discovery_plan,
    wiki_spec_ref,
)
from dalton_core.raw_spool import RawSpool
from dalton_core.sales_notes_core import (
    SalesNotesError,
    enumerate_notes,
    read_note,
    sales_notes_identity,
    sales_notes_schema_hash,
    sales_notes_source_hash,
)
from dalton_core.store import DaltonStore, canonical_json, content_hash
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

import dalton_core.company_wiki_cli as wiki_cli
import dalton_core.sales_notes_cli as notes_cli

REPO = Path(__file__).resolve().parents[1]
PLAN_PATH = REPO / "deploy" / "phase9" / "p9-us-it-services-feeds-v1.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "s1_feeds"
ACN = "company:sec-cik:0001467373"
CTSH = "company:sec-cik:0001058290"
EPAM = "company:sec-cik:0001352010"
AUTOMATION = "automation:coverage-mission"
OWNER = "human:coverage-owner"
SALES_NOTES = "source:sales-notes"
COMPANY_WIKI = "source:company-wiki"

UNIVERSE = [
    {"company_ref": ACN, "ticker": "ACN"},
    {"company_ref": CTSH, "ticker": "CTSH"},
    {"company_ref": EPAM, "ticker": "EPAM"},
]

ACN_NOTE = "sales-note:aaaa000000000001"
MACRO_NOTE = "sales-note:aaaa000000000002"
EPAM_NOTE = "sales-note:aaaa000000000003"
CTSH_NOTE = "sales-note:aaaa000000000004"
OLD_NOTE = "sales-note:aaaa000000000005"
OPEN_NOTE = "sales-note:aaaa000000000006"


def write_governance(directory: Path, kind: str, *, status: str = "approved") -> Path:
    record = build_governance_record(kind, approved_by=OWNER, status=status)
    path = directory / f"{kind}-{status}.json"
    path.write_text(canonical_json(record) + "\n", encoding="utf-8")
    return path


def build_wiki(root: Path) -> tuple[Path, Path]:
    """Materialise the synthetic corpus and its index under ``root``."""

    spec = json.loads((FIXTURES / "wiki_corpus.json").read_text(encoding="utf-8"))
    root.mkdir(parents=True, exist_ok=True)
    index = root / "wiki-index.sqlite"
    connection = sqlite3.connect(index)
    connection.execute(
        "CREATE TABLE documents(id INTEGER PRIMARY KEY, category_type TEXT, "
        "category_name TEXT, content_type TEXT, date TEXT, filename TEXT, "
        "filepath TEXT UNIQUE, created_at TEXT, tags TEXT, related TEXT)"
    )
    for number, row in enumerate(spec["documents"], start=1):
        target = root / row["filepath"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(row["text"], encoding="utf-8")
        connection.execute(
            "INSERT INTO documents(id,category_type,category_name,content_type,date,"
            "filename,filepath,created_at,tags,related) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                number, row["category_type"], row["category_name"], row["content_type"],
                row["date"], row["filename"], row["filepath"],
                "2026-09-01T00:00:00+00:00",
                json.dumps(row["tags"], ensure_ascii=False),
                json.dumps(row["related"], ensure_ascii=False),
            ),
        )
    connection.commit()
    connection.close()
    return index, root


def spawn_env(case: unittest.TestCase) -> None:
    """Let a spawned child import this worktree, not whatever is installed.

    The launcher runs the child with the state directory as its cwd, so a
    relative ``PYTHONPATH`` would resolve somewhere else entirely -- and an
    editable install can point at a different checkout.
    """

    patch = mock.patch.dict("os.environ", {"PYTHONPATH": str(REPO / "src")})
    patch.start()
    case.addCleanup(patch.stop)


class FeedIdentityTests(unittest.TestCase):
    def test_each_operation_binds_its_own_schema_and_shares_one_source(self) -> None:
        # A schema hash binds one operation, so approving the index is not
        # approving the document -- the split every library here already has.
        self.assertNotEqual(
            sales_notes_schema_hash("list_notes"), sales_notes_schema_hash("get_note")
        )
        self.assertNotEqual(
            company_wiki_schema_hash("list_documents"),
            company_wiki_schema_hash("get_document"),
        )
        self.assertEqual(
            sales_notes_identity("list_notes")["source_hash"], sales_notes_source_hash()
        )
        self.assertEqual(
            company_wiki_identity("get_document")["source_hash"], company_wiki_source_hash()
        )
        self.assertNotEqual(sales_notes_source_hash(), company_wiki_source_hash())

    def test_profiles_are_keyless_local_host_tools_with_forbidden_upstreams(self) -> None:
        templates = load_packaged_connector_inventory()["templates"]
        for slug, forbidden in (
            ("sales-notes", "route:gmail-api"),
            ("company-wiki", "route:wiki-embedding-search"),
        ):
            with self.subTest(slug=slug):
                profile = templates[slug]
                self.assertEqual(profile["transport"]["kind"], "host_tool")
                self.assertEqual(profile["auth_boundary"]["mode"], "none")
                self.assertEqual(profile["transport"]["allowed_hosts"], [])
                self.assertEqual(profile["readiness"]["required_gate"], "host_tool_runner_v0.2")
                self.assertIn(forbidden, profile["route_restrictions"]["forbidden_target_refs"])
                for operation in profile["operations"]:
                    self.assertEqual(operation["completeness_ceiling"], "enumerated")

    def test_governance_records_round_trip_and_declare_no_credential(self) -> None:
        for kind in (SALES_NOTES_LIST_KIND, SALES_NOTES_GET_KIND,
                     COMPANY_WIKI_LIST_KIND, COMPANY_WIKI_GET_KIND):
            with self.subTest(kind=kind):
                record = build_governance_record(kind, approved_by=OWNER, status="approved")
                governance = ConnectorGovernance(record)
                self.assertTrue(governance.approved)
                permissions = governance.allowed_permissions
                self.assertFalse(permissions["network"])
                self.assertFalse(permissions["core_db"])
                self.assertEqual(permissions["credential_slot_refs"], [])
                self.assertEqual(permissions["filesystem_write"], ["runner:raw-sink"])

    def test_committed_proposals_are_proposed_and_match_the_builder(self) -> None:
        root = Path(__file__).resolve().parents[1] / "deploy" / "connector-governance"
        for kind in (SALES_NOTES_LIST_KIND, SALES_NOTES_GET_KIND,
                     COMPANY_WIKI_LIST_KIND, COMPANY_WIKI_GET_KIND):
            with self.subTest(kind=kind):
                record = json.loads((root / f"{kind}-v1.json").read_text(encoding="utf-8"))
                self.assertEqual(record["status"], "proposed")
                self.assertFalse(ConnectorGovernance(record).approved)
                self.assertEqual(record, build_governance_record(
                    kind, approved_by=OWNER, status="proposed",
                    effective_from="2026-09-09T00:00:00+00:00",
                ))


class SalesNotesFeedTests(unittest.TestCase):
    def test_enumeration_hashes_bodies_and_suppresses_the_carried_forward_note(self) -> None:
        notes, truncated = enumerate_notes(FIXTURES, since="2026-09-01", until="2026-09-30")
        self.assertFalse(truncated)
        ids = [note["note_id"] for note in notes]
        # Newest first: the evening note leads and the morning ones follow.
        self.assertEqual(ids, [CTSH_NOTE, ACN_NOTE, OPEN_NOTE, EPAM_NOTE, MACRO_NOTE])
        # The morning note is republished by the evening run; it is one note.
        self.assertEqual(len(ids), len(set(ids)))
        first = next(note for note in notes if note["note_id"] == ACN_NOTE)
        self.assertEqual(first["digest_ref"], "market-digest:2026-09-08:AM")
        self.assertEqual(first["evidence_tier"], "sell_side")
        self.assertTrue(first["analyst_named"])
        self.assertEqual(first["sender_domain"], "example-bank.test")
        self.assertEqual(first["sent_at"], "2026-09-08T12:01:02.000000+00:00")
        _, body = read_note(FIXTURES, ACN_NOTE)
        self.assertEqual(
            first["body_sha256"], hashlib.sha256(body.encode("utf-8")).hexdigest()
        )
        self.assertEqual(first["body_chars"], len(body))
        # A second run over the same directory is the same enumeration: the
        # feed grows at the end, so re-reading it is not re-discovering it.
        self.assertEqual(
            (notes, truncated),
            enumerate_notes(FIXTURES, since="2026-09-01", until="2026-09-30"),
        )

    def test_window_and_sender_filters_are_applied_to_the_mail_not_the_run(self) -> None:
        recent, _ = enumerate_notes(FIXTURES, since="2026-09-01", until="2026-09-30")
        self.assertNotIn(OLD_NOTE, [note["note_id"] for note in recent])
        wider, _ = enumerate_notes(FIXTURES, since="2026-07-01", until="2026-09-30")
        self.assertIn(OLD_NOTE, [note["note_id"] for note in wider])
        # `until` bounds the other end, so an older window excludes the newer
        # notes rather than returning everything since a date.
        older, _ = enumerate_notes(FIXTURES, since="2026-07-01", until="2026-08-31")
        self.assertEqual([note["note_id"] for note in older], [OLD_NOTE])
        filtered, _ = enumerate_notes(
            FIXTURES, since="2026-09-01", until="2026-09-30",
            sender_domain="example-broker.test",
        )
        self.assertEqual(
            [note["note_id"] for note in filtered], [CTSH_NOTE, OPEN_NOTE, MACRO_NOTE]
        )

    def test_bad_windows_and_unknown_ids_are_refused(self) -> None:
        with self.assertRaises(SalesNotesError):
            enumerate_notes(FIXTURES, since="September", until="2026-09-30")
        with self.assertRaises(SalesNotesError):
            enumerate_notes(FIXTURES, since="2026-09-30", until="2026-09-01")
        with self.assertRaises(SalesNotesError):
            enumerate_notes(FIXTURES, since="2026-09-01", until="2026-09-30", limit=0)
        with self.assertRaises(SalesNotesError):
            read_note(FIXTURES, "sales-note:ffffffffffffffff")

    def test_a_window_that_does_not_fit_says_so_and_keeps_the_newest(self) -> None:
        notes, truncated = enumerate_notes(
            FIXTURES, since="2026-07-01", until="2026-09-30", limit=2
        )
        self.assertTrue(truncated)
        # The two kept are the newest two, not the two the scan happened to
        # reach first. A prefix of the oldest is the failure this prevents.
        self.assertEqual([note["note_id"] for note in notes], [CTSH_NOTE, ACN_NOTE])


class CompanyWikiFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.index, self.corpus = build_wiki(Path(self.temp.name))

    def test_document_kinds_map_to_the_tier_the_kind_implies(self) -> None:
        self.assertEqual(classify_doc_type("管理层会议纪要"),
                         ("management_meeting_minutes", "management_statement"))
        self.assertEqual(classify_doc_type("专家访谈"), ("expert_interview", "expert"))
        self.assertEqual(classify_doc_type("券商研报"), ("broker_report", "sell_side"))
        self.assertEqual(classify_doc_type("Earnings Update"), ("broker_report", "sell_side"))
        self.assertEqual(classify_doc_type("季度研究笔记"), ("quarterly_note", "internal"))
        self.assertEqual(classify_doc_type("买方交流"), ("buy_side_note", "internal"))
        self.assertEqual(classify_doc_type("NDR纪要"), ("ndr", "management_statement"))
        # Anything the rules do not recognise stays unclassified rather than
        # being rounded up to the nearest plausible tier.
        self.assertEqual(classify_doc_type("微信文章"), ("other", "unclassified"))
        self.assertEqual(classify_doc_type(""), ("other", "unclassified"))

    def test_enumeration_reads_tags_from_the_corpus_and_hashes_the_file(self) -> None:
        documents, truncated = enumerate_documents(
            self.index, self.corpus, since="2026-07-01", until="2026-12-31", company="ACN"
        )
        self.assertFalse(truncated)
        self.assertEqual(
            [item["doc_type_key"] for item in documents],
            ["expert_interview", "management_meeting_minutes"],
        )
        self.assertEqual([item["evidence_tier"] for item in documents],
                         ["expert", "management_statement"])
        expert = documents[0]
        self.assertEqual(expert["company_tags"], ["ACN", "CRM"])
        self.assertEqual(expert["sector_tags"], ["us-software"])
        _, text = read_document(self.index, self.corpus, expert["document_id"])
        self.assertEqual(expert["text_sha256"],
                         hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertEqual(
            expert["document_id"],
            wiki_document_ref("wiki/companies/ACN/2026-08-21-expert.md"),
        )
        # The quarterly note is older than the window and is not enumerated.
        self.assertNotIn("quarterly_note", [item["doc_type_key"] for item in documents])

    def test_an_industry_document_carries_no_company_tag(self) -> None:
        documents, _ = enumerate_documents(
            self.index, self.corpus, since="2026-07-01", until="2026-12-31",
            industry="us-it-services",
        )
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["category_type"], "sector")
        self.assertEqual(documents[0]["company_tags"], [])
        self.assertEqual(documents[0]["evidence_tier"], "unclassified")

    def test_a_filepath_that_leaves_the_corpus_is_refused(self) -> None:
        connection = sqlite3.connect(self.index)
        connection.execute(
            "INSERT INTO documents(id,category_type,category_name,content_type,date,"
            "filename,filepath,created_at,tags,related) VALUES"
            "(99,'company','ACN','专家访谈','2026-09-01','x.md','wiki/../../etc/hosts',"
            "'2026-09-01T00:00:00+00:00','[]','[\"ACN\"]')"
        )
        connection.commit()
        connection.close()
        with self.assertRaises(CompanyWikiError):
            enumerate_documents(self.index, self.corpus, since="2026-07-01",
                                until="2026-12-31", company="ACN")


class FeedChildTests(unittest.TestCase):
    """The child CLIs: approval first, artifact always, contract last."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        spawn_env(self)
        self.spool_dir = self.root / "spool"
        self.index, self.corpus = build_wiki(self.root / "wiki-fixture")

    def run_notes(self, **overrides: Any) -> dict[str, Any]:
        argv = [
            "--state-dir", str(self.state),
            "--governance", str(overrides.pop("governance")),
            "--digest-dir", str(FIXTURES),
            "--spool-dir", str(self.spool_dir),
            "--summary-dir", str(overrides.pop("summary_dir", self.state)),
            "--quiet",
        ]
        for key, value in overrides.items():
            argv += ["--" + key.replace("_", "-"), str(value)]
        return notes_cli.run(notes_cli.build_parser().parse_args(argv))

    def test_an_unapproved_or_wrong_capability_record_stops_the_run(self) -> None:
        proposed = write_governance(self.root, SALES_NOTES_LIST_KIND, status="proposed")
        summary = self.run_notes(governance=proposed, operation="list_notes",
                                 since="2026-09-01", until="2026-09-30")
        self.assertEqual(summary["status"], "failed")
        self.assertIn("not approved", summary["failure_reason"])
        self.assertIsNone(summary["artifact"])
        # The list approval cannot be spent on the document operation: a
        # schema hash binds one operation, which is what makes this checkable.
        approved_list = write_governance(self.root, SALES_NOTES_LIST_KIND)
        summary = self.run_notes(governance=approved_list, operation="get_note",
                                 note_id=ACN_NOTE)
        self.assertEqual(summary["status"], "failed")
        self.assertIn("different capability", summary["failure_reason"])
        # summary.json is written on every path, including both refusals.
        self.assertTrue((self.state / "summary.json").is_file())

    def test_list_notes_validates_against_the_frozen_contract_and_spools_the_read(self) -> None:
        governance = write_governance(self.root, SALES_NOTES_LIST_KIND)
        summary = self.run_notes(governance=governance, operation="list_notes",
                                 since="2026-09-01", until="2026-09-30")
        self.assertEqual(summary["status"], "succeeded", summary["failure_reason"])
        self.assertEqual(summary["note_count"], 5)
        self.assertFalse(summary["observation"]["truncated"])
        self.assertIsNone(summary["observation"]["next_cursor"])
        self.assertEqual(summary["senders"],
                         {"example-bank.test": 2, "example-broker.test": 3})
        self.assertEqual(summary["observation"]["source_record_refs"],
                         [CTSH_NOTE, ACN_NOTE, OPEN_NOTE, EPAM_NOTE, MACRO_NOTE])

    def test_a_truncated_listing_carries_a_cursor_and_never_claims_completeness(self) -> None:
        governance = write_governance(self.root, SALES_NOTES_LIST_KIND)
        summary = self.run_notes(governance=governance, operation="list_notes",
                                 since="2026-07-01", until="2026-09-30", limit=2)
        self.assertEqual(summary["status"], "succeeded", summary["failure_reason"])
        self.assertTrue(summary["observation"]["truncated"])
        # The cursor is the day of the oldest row returned: everything before
        # it is still unread, and the caller narrows the window.
        self.assertEqual(summary["observation"]["next_cursor"], "2026-09-08")
        spool = RawSpool(str(self.spool_dir), max_total_bytes=1_000_000_000)
        self.assertTrue(spool.object_exists(summary["artifact"]["content_hash"]))

    def test_get_note_writes_a_manifest_whose_bytes_verify(self) -> None:
        governance = write_governance(self.root, SALES_NOTES_GET_KIND)
        ticket_dir = self.root / "ticket"
        ticket_dir.mkdir()
        summary = self.run_notes(governance=governance, operation="get_note",
                                 note_id=ACN_NOTE, summary_dir=ticket_dir)
        self.assertEqual(summary["status"], "succeeded", summary["failure_reason"])
        manifest = validate_feed_acquisition_manifest(
            json.loads((ticket_dir / "manifest.json").read_text(encoding="utf-8"))
        )
        self.assertEqual(manifest["document_ref"], ACN_NOTE)
        self.assertEqual(manifest["evidence_tier"], "sell_side")
        self.assertEqual(manifest["origin_ref"], "market-digest:2026-09-08:AM")
        self.assertEqual(manifest["subject_tickers"], [])
        spool = RawSpool(str(self.spool_dir), max_total_bytes=1_000_000_000)
        _, text = verified_feed_source(None, spool, manifest)
        _, body = read_note(FIXTURES, ACN_NOTE)
        self.assertEqual(text, body)

    def test_a_manifest_pointing_at_other_bytes_is_refused(self) -> None:
        governance = write_governance(self.root, SALES_NOTES_GET_KIND)
        ticket_dir = self.root / "ticket"
        ticket_dir.mkdir()
        self.run_notes(governance=governance, operation="get_note",
                       note_id=ACN_NOTE, summary_dir=ticket_dir)
        manifest = json.loads((ticket_dir / "manifest.json").read_text(encoding="utf-8"))
        spool = RawSpool(str(self.spool_dir), max_total_bytes=1_000_000_000)
        other = hashlib.sha256(b"different bytes").hexdigest()
        sink = spool.open_sink(f"raw-sink:{other}", max_response_bytes=1024)
        sink.write(b"different bytes")
        swapped = sink.finalize().to_dict()
        # Repoint the object and re-derive the manifest hash, so this is a
        # coherent manifest that simply names the wrong bytes.
        base = {key: value for key, value in manifest.items() if key != "content_hash"}
        base["assembled_object"] = swapped
        base["content_hash"] = content_hash(base)
        with self.assertRaises(FeedSourceConflict):
            verified_feed_source(None, spool, base)

    def test_wiki_child_reads_one_document_and_keeps_the_corpus_tags(self) -> None:
        governance = write_governance(self.root, COMPANY_WIKI_GET_KIND)
        documents, _ = enumerate_documents(
            self.index, self.corpus, since="2026-07-01", until="2026-12-31", company="ACN"
        )
        ticket_dir = self.root / "wiki-ticket"
        ticket_dir.mkdir()
        args = wiki_cli.build_parser().parse_args([
            "--state-dir", str(self.state), "--governance", str(governance),
            "--index-db", str(self.index), "--corpus-root", str(self.corpus),
            "--operation", "get_document", "--document-id", documents[0]["document_id"],
            "--spool-dir", str(self.spool_dir), "--summary-dir", str(ticket_dir), "--quiet",
        ])
        summary = wiki_cli.run(args)
        self.assertEqual(summary["status"], "succeeded", summary["failure_reason"])
        manifest = validate_feed_acquisition_manifest(
            json.loads((ticket_dir / "manifest.json").read_text(encoding="utf-8"))
        )
        self.assertEqual(manifest["evidence_tier"], "expert")
        self.assertEqual(manifest["doc_kind"], "expert_interview")
        self.assertEqual(manifest["subject_tickers"], ["ACN", "CRM"])
        spool = RawSpool(str(self.spool_dir), max_total_bytes=1_000_000_000)
        _, text = verified_feed_source(None, spool, manifest)
        _, expected = read_document(self.index, self.corpus, manifest["document_ref"])
        self.assertEqual(text, expected)

    def test_a_manifest_outside_the_closed_shape_is_refused(self) -> None:
        with self.assertRaises(FeedManifestError):
            validate_feed_acquisition_manifest({"schema_version": "0.1"})


class FeedAttributionTests(unittest.TestCase):
    def test_a_note_naming_a_covered_company_is_queued_and_a_macro_note_is_not(self) -> None:
        notes, _ = enumerate_notes(FIXTURES, since="2026-09-01", until="2026-09-30")
        attribution = attribute_notes(notes, UNIVERSE)
        self.assertEqual(attribution["by_company"][ACN], [ACN_NOTE])
        self.assertEqual(attribution["by_company"][EPAM], [EPAM_NOTE])
        self.assertEqual(attribution["by_company"][CTSH], [CTSH_NOTE])
        # The rates note names nobody in its subject, so it is not forced onto
        # a company queue.
        self.assertEqual(attribution["unattributed"], [MACRO_NOTE, OPEN_NOTE])

    def test_wiki_attribution_uses_the_corpus_tags_and_leaves_industry_alone(self) -> None:
        documents = [
            {"document_id": "company-wiki-doc:sha256:" + "a" * 64,
             "company_tags": ["ACN", "CRM"]},
            {"document_id": "company-wiki-doc:sha256:" + "b" * 64, "company_tags": []},
            {"document_id": "company-wiki-doc:sha256:" + "c" * 64,
             "company_tags": ["NVDA"]},
        ]
        attribution = attribute_wiki_documents(documents, UNIVERSE)
        self.assertEqual(attribution["by_company"],
                         {ACN: ["company-wiki-doc:sha256:" + "a" * 64]})
        # A sector note and a document about a company outside the universe
        # are both simply not queued.
        self.assertEqual(attribution["unattributed"],
                         ["company-wiki-doc:sha256:" + "b" * 64,
                          "company-wiki-doc:sha256:" + "c" * 64])

    def test_a_company_the_name_tables_do_not_know_fails_loudly(self) -> None:
        with self.assertRaises(FeedLaneRejected):
            attribute_notes([], [{"company_ref": "company:x", "ticker": "ZZZZ"}])

    def test_spec_refs_are_per_document_kind_and_carry_no_figure_grade(self) -> None:
        from dalton_core.document_figure_grade import grade_for

        self.assertEqual(wiki_spec_ref("expert_interview"), "wiki-expert-interview")
        for spec in ("sales-note", "wiki-expert-interview", "wiki-broker-report"):
            # None of these is a filed statement or a transcript, so no figure
            # is ever read out of one.
            self.assertIsNone(grade_for(spec))


class FeedDiscoveryRecordTests(unittest.TestCase):
    """The discovery wire, checked against the authority's own validator."""

    def setUp(self) -> None:
        self.patch = mock.patch.object(
            coverage_mission_module, "DISCOVERY_SOURCES",
            MappingProxyType({
                **dict(coverage_mission_module.DISCOVERY_SOURCES),
                **dict(FEED_DISCOVERY_SOURCES),
            }),
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_feed_parameters_are_the_windowed_shape_the_authority_already_takes(self) -> None:
        parameters = feed_discovery_parameters(
            terms="Accenture ACN", since="2026-03-01", as_of=date(2026, 9, 9)
        )
        self.assertEqual(set(parameters), {"query", "date_after", "date_before"})
        with self.assertRaises(FeedLaneRejected):
            feed_discovery_parameters(terms="", since="2026-03-01", as_of="2026-09-09")
        with self.assertRaises(FeedLaneRejected):
            feed_discovery_parameters(
                terms="Accenture", since="2026-09-09", as_of="2026-03-01"
            )

    def test_a_feed_discovery_record_validates_against_the_mission_contract(self) -> None:
        parameters = feed_discovery_parameters(
            terms="Accenture ACN", since="2026-09-01", as_of="2026-09-09"
        )
        base = {
            "schema_version": "0.1",
            "id": "mission-source-discovery:" + "0" * 32,
            "created_at": "2026-09-09T00:00:00.000000+00:00",
            "mission_version_ref": "coverage-mission-version:us-it-services:2",
            "mission_version_hash": "1" * 64,
            "company_ref": ACN,
            "source_ref": SALES_NOTES,
            "discovery_plan_ref": "discovery-plan:us-it-services:sales-notes:1",
            "discovery_plan_hash": "2" * 64,
            "spec_ref": "sales-note",
            "query_hash": feed_query_hash(SALES_NOTES, parameters),
            "parameters": parameters,
            "connector_invocation_ref": "connector-invocation:sales-notes:1",
            "connector_invocation_hash": "3" * 64,
            "source_envelope_ref": "source-envelope:sales-notes:1",
            "source_envelope_hash": "4" * 64,
            "document_refs": [ACN_NOTE],
            "new_document_refs": [ACN_NOTE],
            "in_authority_document_refs": [],
            "actor_ref": AUTOMATION,
            "requested_by": AUTOMATION,
        }
        base["content_hash"] = content_hash(base)
        record = validate_mission_source_discovery(base)
        self.assertEqual(record["document_refs"], [ACN_NOTE])
        # A ref that does not carry the feed's prefix cannot be queued as one
        # of its documents.
        wrong = dict(base)
        wrong["document_refs"] = ["alphaengine-doc:1"]
        wrong["new_document_refs"] = ["alphaengine-doc:1"]
        wrong.pop("content_hash")
        wrong["content_hash"] = content_hash(wrong)
        with self.assertRaises(Exception):
            validate_mission_source_discovery(wrong)


class FeedGrantTests(unittest.TestCase):
    """The mission's own grant check, called rather than re-implemented."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core = DaltonStore(str(Path(self.temp.name) / "core.sqlite"))
        self.addCleanup(self.core.close)
        self.state = bootstrap_method_authorities(self.core)
        self.missions = CoverageMissionAuthority(self.core)
        self.patch = mock.patch.object(
            coverage_mission_module, "DISCOVERY_SOURCES",
            MappingProxyType({
                **dict(coverage_mission_module.DISCOVERY_SOURCES),
                **dict(FEED_DISCOVERY_SOURCES),
            }),
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def create(self, *, connected: bool, scopes: bool, version: int = 1, prior=None):
        params = mission_params(self.state)
        params["source_plan"] = list(params["source_plan"]) + [{
            "source_ref": SALES_NOTES,
            "role": "named sell-side notes already on this machine",
            "status": "connected" if connected else "not_connected",
        }]
        if scopes:
            params["autonomy"]["may_write"] = list(
                params["autonomy"]["may_write"]
            ) + ["source_discovery"]
        params.update({
            "version_id": f"coverage-mission-version:us-it-services:{version}",
            "prior_version_ref": prior,
            "idempotency_key": f"coverage-mission:us-it-services:{version}",
        })
        ref = params.pop("mission_ref")
        return self.missions.create_mission(ref, **params)

    def test_a_feed_the_mission_has_not_connected_is_refused_for_automation(self) -> None:
        v1 = self.create(connected=False, scopes=True)
        with self.assertRaises(CoverageMissionConflict):
            self.missions.authorize_source_discovery(
                company_ref=ACN, source_ref=SALES_NOTES, requested_by=AUTOMATION
            )
        v2 = self.create(connected=True, scopes=False, version=2, prior=v1["id"])
        # Connected but without the scope: the mission grants observation and
        # not source_discovery, and both are required.
        with self.assertRaises(CoverageMissionConflict) as ctx:
            self.missions.authorize_source_discovery(
                company_ref=ACN, source_ref=SALES_NOTES, requested_by=AUTOMATION
            )
        self.assertIn("source_discovery", str(ctx.exception))
        v3 = self.create(connected=True, scopes=True, version=3, prior=v2["id"])
        authorization = self.missions.authorize_source_discovery(
            company_ref=ACN, source_ref=SALES_NOTES, requested_by=AUTOMATION
        )
        self.assertEqual(authorization["mission_version_ref"], v3["id"])
        self.assertEqual(authorization["scope"], "source_discovery")
        self.assertEqual(authorization["ticker"], "ACN")
        self.assertIn("observation", v3["autonomy"]["may_write"])

    def test_the_coordinator_calls_the_authority_rather_than_deciding_itself(self) -> None:
        v1 = self.create(connected=False, scopes=True)
        self.create(connected=True, scopes=True, version=2, prior=v1["id"])
        coordinator = FeedDiscoveryCoordinator(
            missions=self.missions, launcher=None, source_ref=SALES_NOTES,
            plan=load_feed_discovery_plan(PLAN_PATH),
        )
        self.assertEqual(
            coordinator.authorize(company_ref=ACN)["source_ref"], SALES_NOTES
        )
        with self.assertRaises(CoverageMissionConflict):
            coordinator.authorize(company_ref="company:not-covered")

    def test_an_unknown_feed_source_cannot_have_a_coordinator(self) -> None:
        with self.assertRaises(FeedLaneRejected):
            FeedDiscoveryCoordinator(
                missions=self.missions, launcher=None,
                source_ref="source:alphaengine",
                plan=load_feed_discovery_plan(PLAN_PATH),
            )


class RecordingMissions:
    """The five queue methods the tick uses, with the authority's semantics.

    A double rather than the real authority because a discovered-document row
    can only be created by ``record_source_discovery``, which binds a real
    connector invocation and source envelope -- and the host-tool runner that
    would produce them is not built. ``AuthoritySeamTests`` below checks that
    every call this double accepts is a call the real authority would accept,
    so the seam cannot drift silently.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = {row["record_id"]: dict(row) for row in rows}
        self.reviews: list[str] = []

    def launched_discovered_documents(self, *, limit: int = 20, source_ref=None):
        return [dict(row) for row in self.rows.values()
                if row["status"] == "acquisition_launched"
                and (source_ref is None or row["source_ref"] == source_ref)][:limit]

    def next_discovered_document(self, *, source_ref=None, preferred_hosts=(),
                                 skip_hosts=(), preferred_needs=()):
        for row in self.rows.values():
            if row["status"] == "discovered" and (
                source_ref is None or row["source_ref"] == source_ref
            ):
                return dict(row)
        return None

    def mark_discovered_document_launched(self, record_id, ticket_ref):
        self.rows[record_id].update(status="acquisition_launched", ticket_ref=ticket_ref)
        return dict(self.rows[record_id])

    def settle_discovered_document(self, record_id, *, status, reason=None):
        self.rows[record_id].update(status=status, failure_reason=reason)
        return dict(self.rows[record_id])

    def register_document_review(self, record_id, *, requested_by):
        self.reviews.append(record_id)
        return {"review_id": f"review:{record_id}", "registered_by": requested_by}


class FeedLaneTickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        spawn_env(self)
        self.spool_dir = self.root / "spool"
        self.launcher = SalesNotesFeedLauncher(
            digest_dir=FIXTURES,
            state_dir=self.state,
            governance_paths={
                "list_notes": write_governance(self.root, SALES_NOTES_LIST_KIND),
                "get_note": write_governance(self.root, SALES_NOTES_GET_KIND),
            },
            spool_dir=self.spool_dir,
        )
        self.addCleanup(self.launcher.close)

    def coordinator(self, missions) -> FeedDiscoveryCoordinator:
        return FeedDiscoveryCoordinator(
            missions=missions, launcher=self.launcher, source_ref=SALES_NOTES,
            plan=load_feed_discovery_plan(PLAN_PATH), acquisitions_per_tick=2,
            acquisition_wait_seconds=90.0,
        )

    def test_enumeration_runs_through_the_governed_child(self) -> None:
        coordinator = self.coordinator(RecordingMissions([]))
        observation = coordinator.enumerate(since="2026-09-01", until="2026-09-30")
        self.assertEqual(observation["note_count"], 5)
        self.assertEqual(coordinator.spec_refs(observation)[ACN_NOTE], "sales-note")

    def test_a_tick_acquires_a_bounded_batch_settles_it_and_opens_reviews(self) -> None:
        missions = RecordingMissions([
            {"record_id": "mission-discovered-document:1", "document_ref": ACN_NOTE,
             "source_ref": SALES_NOTES, "company_ref": ACN, "status": "discovered",
             "ticket_ref": None},
            {"record_id": "mission-discovered-document:2", "document_ref": EPAM_NOTE,
             "source_ref": SALES_NOTES, "company_ref": EPAM, "status": "discovered",
             "ticket_ref": None},
            {"record_id": "mission-discovered-document:3", "document_ref": CTSH_NOTE,
             "source_ref": SALES_NOTES, "company_ref": CTSH, "status": "discovered",
             "ticket_ref": None},
        ])
        coordinator = self.coordinator(missions)
        result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "dispatched")
        # Two per tick, and the third is still waiting.
        self.assertEqual(len(result["launched"]), 2)
        acquired = [row for row in missions.rows.values() if row["status"] == "acquired"]
        self.assertEqual(len(acquired), 2)
        self.assertEqual(len(missions.reviews), 2)
        self.assertEqual(
            missions.rows["mission-discovered-document:3"]["status"], "discovered"
        )
        # The manifests are readable back through the ticket the queue named.
        for row in acquired:
            manifest = self.launcher.read_completed_manifest(
                row["ticket_ref"], row["document_ref"]
            )
            self.assertEqual(manifest["document_ref"], row["document_ref"])
            self.assertEqual(manifest["evidence_tier"], "sell_side")
        # A second tick finishes the queue; a third has nothing to do.
        self.assertEqual(coordinator.dispatch_once()["status"], "dispatched")
        self.assertEqual(coordinator.dispatch_once()["status"], "idle")
        self.assertEqual(len(missions.reviews), 3)
        self.assertEqual(len(set(missions.reviews)), 3)

    def test_a_child_that_cannot_find_the_document_settles_as_failed(self) -> None:
        missions = RecordingMissions([
            {"record_id": "mission-discovered-document:9",
             "document_ref": "sales-note:ffffffffffffffff",
             "source_ref": SALES_NOTES, "company_ref": ACN, "status": "discovered",
             "ticket_ref": None},
        ])
        coordinator = self.coordinator(missions)
        coordinator.dispatch_once()
        row = missions.rows["mission-discovered-document:9"]
        self.assertEqual(row["status"], "acquisition_failed")
        self.assertEqual(missions.reviews, [])

    def test_a_coordinator_without_a_runner_refuses_to_read_and_says_why(self) -> None:
        coordinator = self.coordinator(RecordingMissions([]))
        with self.assertRaises(FeedLaneError) as ctx:
            coordinator.resolve_documents(
                queue=[ACN_NOTE], universe=UNIVERSE, headers={}, header_company={},
                since="2026-08-01",
            )
        self.assertIn("host-tool runner", str(ctx.exception))
        with self.assertRaises(FeedLaneError):
            coordinator.enumerate_via_runner(since="2026-09-01", until="2026-09-30")


class FeedLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        spawn_env(self)

    def test_a_launcher_needs_a_record_per_operation_and_an_approved_one(self) -> None:
        with self.assertRaises(FeedLaunchRejected):
            SalesNotesFeedLauncher(
                digest_dir=FIXTURES, state_dir=self.state,
                governance_paths={"list_notes": self.root / "missing.json"},
            )
        launcher = SalesNotesFeedLauncher(
            digest_dir=FIXTURES, state_dir=self.state,
            governance_paths={
                "list_notes": write_governance(
                    self.root, SALES_NOTES_LIST_KIND, status="proposed"
                ),
                "get_note": self.root / "absent.json",
            },
        )
        self.addCleanup(launcher.close)
        with self.assertRaises(FeedLaunchRejected) as ctx:
            launcher.start_enumeration(caller_ref=AUTOMATION, since="2026-09-01")
        self.assertIn("not approved", str(ctx.exception))
        with self.assertRaises(FeedLaunchRejected):
            launcher.start_bounded_probe(document_ref=ACN_NOTE, caller_ref=AUTOMATION)

    def test_a_run_must_name_a_principal_and_a_document(self) -> None:
        launcher = SalesNotesFeedLauncher(
            digest_dir=FIXTURES, state_dir=self.state,
            governance_paths={
                "list_notes": write_governance(self.root, SALES_NOTES_LIST_KIND),
                "get_note": write_governance(self.root, SALES_NOTES_GET_KIND),
            },
        )
        self.addCleanup(launcher.close)
        with self.assertRaises(FeedLaunchRejected):
            launcher.start_bounded_probe(document_ref=ACN_NOTE, caller_ref="nobody")
        with self.assertRaises(FeedLaunchRejected):
            launcher.start_bounded_probe(document_ref="", caller_ref=AUTOMATION)
        with self.assertRaises(FeedLaunchRejected):
            launcher.read_completed_manifest("not-a-ticket", ACN_NOTE)
        with self.assertRaises(FeedLaunchRejected):
            launcher.locate_completed_manifest(ACN_NOTE)

    def test_a_ticket_that_disagrees_with_its_manifest_is_refused(self) -> None:
        launcher = SalesNotesFeedLauncher(
            digest_dir=FIXTURES, state_dir=self.state,
            governance_paths={
                "list_notes": write_governance(self.root, SALES_NOTES_LIST_KIND),
                "get_note": write_governance(self.root, SALES_NOTES_GET_KIND),
            },
            spool_dir=self.root / "spool",
        )
        self.addCleanup(launcher.close)
        ticket = launcher.start_bounded_probe(document_ref=ACN_NOTE, caller_ref=AUTOMATION)
        launcher.wait(timeout=90.0)
        self.assertEqual(launcher.status(ticket["id"])["status"], "succeeded")
        launcher.read_completed_manifest(ticket["id"], ACN_NOTE)
        # Asking for a different document than the ticket acquired is a
        # refusal, not a manifest for the wrong note.
        with self.assertRaises(FeedLaunchRejected):
            launcher.read_completed_manifest(ticket["id"], EPAM_NOTE)
        self.assertEqual(
            launcher.locate_completed_manifest(ACN_NOTE)["document_ref"], ACN_NOTE
        )

    def test_the_wiki_launcher_builds_its_own_child_command(self) -> None:
        index, corpus = build_wiki(self.root / "wiki")
        launcher = CompanyWikiFeedLauncher(
            index_db=index, corpus_root=corpus, state_dir=self.state,
            governance_paths={
                "list_documents": write_governance(self.root, COMPANY_WIKI_LIST_KIND),
                "get_document": write_governance(self.root, COMPANY_WIKI_GET_KIND),
            },
        )
        self.addCleanup(launcher.close)
        command = launcher._command(
            ticket_dir=self.state, operation="list_documents", since="2026-07-01",
            company="ACN",
        )
        self.assertIn("dalton_core.company_wiki_cli", command)
        self.assertIn("--corpus-root", command)
        self.assertIn("--company", command)
        self.assertNotIn("--document-id", command)


class FeedPlanTests(unittest.TestCase):
    def test_the_committed_plan_loads_and_binds_its_own_hash(self) -> None:
        plan = load_feed_discovery_plan(PLAN_PATH)
        self.assertEqual(plan["mission_ref"], "coverage-mission:us-it-services")
        self.assertEqual(len(plan["companies"]), 5)
        self.assertEqual(set(plan["source_refs"]), set(FEED_DISCOVERY_SOURCES))
        # The coverage companies are matched by the ledger's own name table,
        # so the plan's terms are the industry and the peers and nothing else.
        self.assertIn("IT services", plan_terms(plan))
        self.assertIn("Infosys", plan_terms(plan))
        self.assertNotIn("Accenture", plan_terms(plan))

    def test_a_tampered_or_unsorted_plan_is_refused(self) -> None:
        plan = load_feed_discovery_plan(PLAN_PATH)
        tampered = dict(plan)
        tampered["lookback_days"] = 10
        with self.assertRaises(FeedLaneRejected):
            validate_feed_discovery_plan(tampered)
        unsorted_terms = dict(plan)
        unsorted_terms["peer_names"] = list(reversed(plan["peer_names"]))
        unsorted_terms.pop("content_hash")
        unsorted_terms["content_hash"] = content_hash(unsorted_terms)
        with self.assertRaises(FeedLaneRejected):
            validate_feed_discovery_plan(unsorted_terms)
        for bad in ({}, {**plan, "extra": 1}):
            with self.assertRaises(FeedLaneRejected):
                validate_feed_discovery_plan(bad)


class FeedTriageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = load_feed_discovery_plan(PLAN_PATH)

    def test_a_header_hit_attributes_and_a_header_miss_only_defers(self) -> None:
        notes, _ = enumerate_notes(FIXTURES, since="2026-09-01", until="2026-09-30")
        triage = triage_notes(notes, UNIVERSE, self.plan)
        self.assertEqual(triage["header_company"][ACN], [ACN_NOTE])
        self.assertEqual(triage["header_company"][EPAM], [EPAM_NOTE])
        # Nothing is dropped at the header. The queue holds every note,
        # header hits first, because the body is a local file and the header
        # saying nothing is not evidence that the body says nothing.
        self.assertEqual(sorted(triage["read_queue"]),
                         sorted(note["note_id"] for note in notes))
        self.assertEqual(set(triage["read_queue"][:3]),
                         {ACN_NOTE, EPAM_NOTE, CTSH_NOTE})

    def test_the_body_decides_company_industry_or_dropped(self) -> None:
        _, acn_body = read_note(FIXTURES, ACN_NOTE)
        self.assertEqual(
            attribute_body(acn_body, UNIVERSE, self.plan)["outcome"], "company"
        )
        _, macro_body = read_note(FIXTURES, MACRO_NOTE)
        macro = attribute_body(macro_body, UNIVERSE, self.plan)
        self.assertEqual(macro["outcome"], "industry")
        self.assertEqual(macro["industry_terms"], ["IT services"])
        _, open_body = read_note(FIXTURES, OPEN_NOTE)
        dropped = attribute_body(open_body, UNIVERSE, self.plan)
        self.assertEqual(dropped["outcome"], "dropped")
        self.assertTrue(dropped["reason"])
        # A header hit stands even when the body never repeats the name.
        self.assertEqual(
            attribute_body(open_body, UNIVERSE, self.plan,
                           header_companies=[ACN])["company_refs"],
            [ACN],
        )


class FeedEndToEndTests(unittest.TestCase):
    """The whole lane against the real mission authority and a real runner."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        spawn_env(self)
        self.core = DaltonStore(str(self.root / "core.sqlite"))
        self.addCleanup(self.core.close)
        self.connectors = ConnectorStore(self.core)
        self.observability = ObservabilityStore(self.core)
        self.spool = RawSpool(str(self.root / "spool"), max_total_bytes=1_000_000_000)
        self.bootstrap = bootstrap_method_authorities(self.core)
        self.missions = CoverageMissionAuthority(self.core)
        patch = mock.patch.object(
            coverage_mission_module, "DISCOVERY_SOURCES",
            MappingProxyType({
                **dict(coverage_mission_module.DISCOVERY_SOURCES),
                **dict(FEED_DISCOVERY_SOURCES),
            }),
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.mission = self.publish_mission()
        self.governance = {
            "list_notes": write_governance(self.root, SALES_NOTES_LIST_KIND),
            "get_note": write_governance(self.root, SALES_NOTES_GET_KIND),
        }
        self.launcher = SalesNotesFeedLauncher(
            digest_dir=FIXTURES, state_dir=self.state,
            governance_paths=self.governance, spool_dir=self.root / "spool",
        )
        self.addCleanup(self.launcher.close)

    def publish_mission(self):
        params = mission_params(self.bootstrap)
        params["source_plan"] = list(params["source_plan"]) + [{
            "source_ref": SALES_NOTES, "role": "named sell-side notes on this machine",
            "status": "connected",
        }]
        params["autonomy"]["may_write"] = list(
            params["autonomy"]["may_write"]
        ) + ["source_discovery"]
        ref = params.pop("mission_ref")
        return self.missions.create_mission(ref, **params)

    def coordinator(self, **overrides):
        runners = {
            name: build_feed_runner(
                launcher=self.launcher, operation=operation,
                governance=ConnectorGovernance.load(self.governance[operation]),
                store=self.core, connectors=self.connectors,
                observability=self.observability, spool=self.spool,
                source_ref=SALES_NOTES,
            )
            for name, operation in (("enumerator", "list_notes"), ("runner", "get_note"))
        }
        return FeedDiscoveryCoordinator(
            missions=self.missions, launcher=self.launcher, source_ref=SALES_NOTES,
            plan=load_feed_discovery_plan(PLAN_PATH), **runners, **overrides,
        )

    def test_a_tick_reads_records_and_opens_reviews_end_to_end(self) -> None:
        coordinator = self.coordinator()
        result = coordinator.dispatch_once(universe=UNIVERSE, since="2026-08-01")
        self.assertEqual(result["status"], "dispatched")
        read = result["read"]
        self.assertEqual(read["read"], 6)
        # Four notes name a covered company, one names only the industry, one
        # names neither. Only the four enter the mission's queue.
        self.assertEqual(read["company"], 4)
        self.assertEqual(read["industry"], 1)
        self.assertEqual(read["dropped"], 1)
        outcomes = {item["document_ref"]: item for item in read["outcomes"]}
        self.assertEqual(outcomes[MACRO_NOTE]["outcome"], "industry")
        self.assertEqual(outcomes[MACRO_NOTE]["records"], [])
        self.assertEqual(outcomes[OPEN_NOTE]["outcome"], "dropped")

        version_ref = self.mission["id"]
        queued = self.missions.discovered_documents(version_ref, limit=100)
        self.assertEqual({row["document_ref"] for row in queued},
                         {ACN_NOTE, EPAM_NOTE, CTSH_NOTE, OLD_NOTE})
        self.assertEqual({row["status"] for row in queued}, {"acquired"})
        reviews = {row["document_ref"] for row in
                   self.missions.document_reviews(version_ref, limit=100)}
        self.assertEqual(reviews, {ACN_NOTE, EPAM_NOTE, CTSH_NOTE, OLD_NOTE})

        # Every queued document's discovery binds a source envelope that is
        # really in Core and really names that one document.
        reader = ConnectorCompletionReceiptReader(
            connectors=self.connectors, observability=self.observability
        )
        for discovery in self.missions.source_discoveries(version_ref, limit=100):
            envelope = reader.get_source_envelope(discovery["source_envelope_ref"])
            self.assertIsNotNone(envelope)
            self.assertEqual(envelope["content_hash"], discovery["source_envelope_hash"])
            self.assertEqual(envelope["operation"], "get_note")
            self.assertEqual(envelope["source"], SALES_NOTES)
            self.assertEqual(envelope["source_record_refs"], discovery["document_refs"])
            invocation = reader.get_invocation(discovery["connector_invocation_ref"])
            self.assertEqual(invocation["content_hash"],
                             discovery["connector_invocation_hash"])

        # And the bytes the review path will read verify against the manifest
        # the same run wrote.
        for row in queued:
            manifest = self.launcher.read_completed_manifest(
                row["ticket_ref"], row["document_ref"]
            )
            # The manifest names the invocation that read the bytes, so the
            # review path re-reads that receipt from Core as well as the bytes.
            self.assertEqual(
                manifest["connector_invocation_hash"],
                reader.get_invocation(manifest["connector_invocation_ref"])["content_hash"],
            )
            _, text = verified_feed_source(self.core, self.spool, manifest, reader)
            self.assertEqual(manifest["evidence_tier"], "sell_side")
            self.assertTrue(text)
        # Without the reader the manifest cannot be honoured: it names an
        # authority nobody offered to check.
        with self.assertRaises(FeedSourceConflict):
            verified_feed_source(self.core, self.spool, manifest)

    def test_a_second_tick_discovers_nothing_new(self) -> None:
        coordinator = self.coordinator()
        coordinator.dispatch_once(universe=UNIVERSE, since="2026-08-01")
        before = len(self.missions.source_discoveries(self.mission["id"], limit=200))
        again = coordinator.dispatch_once(universe=UNIVERSE, since="2026-08-01")
        after = self.missions.source_discoveries(self.mission["id"], limit=200)
        # Nothing new is discovered and nothing is queued twice: the four
        # documents the mission already holds are not read again.
        self.assertEqual(len(after), before)
        self.assertEqual(again["read"]["already_held"], 4)
        self.assertEqual(again["read"]["company"], 0)
        # The two it kept nothing from are re-read, because the ledger has no
        # row for "looked, kept nothing"; the per-tick bound is the guard.
        self.assertEqual(again["read"]["read"], 2)
        self.assertEqual(
            {row["status"] for row in
             self.missions.discovered_documents(self.mission["id"], limit=100)},
            {"acquired"},
        )

    def test_the_batch_is_bounded_by_the_plan(self) -> None:
        coordinator = self.coordinator(body_reads_per_tick=2)
        result = coordinator.dispatch_once(universe=UNIVERSE, since="2026-08-01")
        self.assertEqual(result["read"]["read"], 2)
        self.assertEqual(
            len(self.missions.discovered_documents(self.mission["id"], limit=100)), 2
        )

    def test_a_document_the_child_cannot_read_settles_as_failed_not_queued(self) -> None:
        coordinator = self.coordinator()
        outcome = coordinator._resolve_one(
            document_ref="sales-note:ffffffffffffffff", universe=UNIVERSE,
            header={}, header_companies=(), since="2026-08-01", requested_by=None,
        )
        self.assertEqual(outcome["outcome"], "failed")
        self.assertIn("get_note", outcome["reason"])
        self.assertEqual(
            self.missions.discovered_documents(self.mission["id"], limit=100), []
        )


def synthetic_digests(root: Path, *, days: int, per_day: int) -> Path:
    """A feed big enough that one bounded response cannot hold a window."""

    root.mkdir(parents=True, exist_ok=True)
    start = date(2026, 5, 1)
    serial = 0
    for offset in range(days):
        day = start + timedelta(days=offset)
        emails = []
        for slot in range(per_day):
            serial += 1
            body = f"SYNTHETIC FIXTURE BODY -- note {serial}.\r\n"
            emails.append({
                "id": f"bbbb{serial:012d}",
                "from": '"Desk" <desk@example-broker.test>',
                "subject": f"Synthetic note {serial}",
                "date": format_datetime(
                    datetime(day.year, day.month, day.day, 6 + slot,
                             tzinfo=timezone.utc)
                ),
                "is_priority": False,
                "body_length": len(body),
                "body": body,
            })
        (root / f"digest_{day.isoformat()}_AM.json").write_text(
            json.dumps({
                "ok": True, "status": "emails_fetched", "date": day.isoformat(),
                "period": "AM", "count": len(emails), "priority_count": 0,
                "raw_chunks_indexed": 0, "emails": emails,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
    return root


class TruncationTests(unittest.TestCase):
    """A window that does not fit says so, and the walk still covers it."""

    NOTES = 1_200

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.digests = synthetic_digests(self.root / "digests", days=120, per_day=10)
        self.plan = load_feed_discovery_plan(PLAN_PATH)

    def test_a_window_returns_the_newest_and_admits_what_it_left(self) -> None:
        notes, truncated = enumerate_notes(
            self.digests, since="2026-05-01", until="2026-08-28", limit=100
        )
        self.assertTrue(truncated)
        self.assertEqual(len(notes), 100)
        # Newest first, so the hundred kept are the hundred that matter.
        self.assertEqual(notes[0]["sent_at"][:10], "2026-08-28")
        self.assertGreater(notes[0]["sent_at"], notes[-1]["sent_at"])

    def test_the_walk_covers_every_note_newest_first_over_successive_ticks(self) -> None:
        # A fake enumerator: the real core read, wrapped in the wire shape the
        # runner would return. The subject under test is the window walk, and
        # twelve hundred child processes would test the operating system.
        limit = 100
        calls: list[tuple[str, str]] = []

        class Enumerator:
            def run(self, *, parameters, work_ref, output_dir=None):
                calls.append((parameters["since"], parameters["until"]))
                rows, truncated = enumerate_notes(
                    self_digests, since=parameters["since"],
                    until=parameters["until"], limit=limit,
                )
                observation = {
                    "schema_version": "0.1", "since": parameters["since"],
                    "until": parameters["until"], "sender_domain": None,
                    "notes": rows, "note_count": len(rows), "truncated": truncated,
                    "source_record_refs": [row["note_id"] for row in rows],
                    "next_cursor": rows[-1]["sent_at"][:10] if truncated and rows else None,
                    "provider_status": 200,
                }
                return mock.Mock(observation=observation)

        self_digests = self.digests
        clock = lambda: datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
        coordinator = FeedDiscoveryCoordinator(
            missions=RecordingMissions([]), launcher=None, source_ref=SALES_NOTES,
            plan=self.plan, enumerator=Enumerator(), clock=clock,
        )
        seen: list[str] = []
        for window_since, window_until in coordinator.windows(since="2026-05-01"):
            for observation in coordinator.enumerate_window(
                since=window_since, until=window_until
            ):
                # Every observation is honest about itself: a cursor exactly
                # when it left rows behind.
                self.assertEqual(
                    observation["next_cursor"] is not None, observation["truncated"]
                )
                seen.extend(observation["source_record_refs"])
        every, _ = enumerate_notes(
            self.digests, since="2026-05-01", until="2026-08-28", limit=2_000
        )
        self.assertEqual(len(every), self.NOTES)
        # Every note is enumerated exactly once across the walk, and the walk
        # went newest first: the first window asked for the most recent days.
        self.assertEqual(sorted(seen), sorted(note["note_id"] for note in every))
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(calls[0][1], "2026-08-28")
        self.assertGreater(calls[0][0], calls[-1][0])
        # A fortnight of this feed is a hundred and forty notes, so the first
        # ask truncated and was split rather than accepted.
        self.assertGreater(len(calls), len(coordinator.windows(since="2026-05-01")))

    def test_a_truncated_listing_binds_partial_not_enumerated(self) -> None:
        spawn_env(self)
        core = DaltonStore(str(self.root / "core.sqlite"))
        self.addCleanup(core.close)
        connectors = ConnectorStore(core)
        observability = ObservabilityStore(core)
        spool = RawSpool(str(self.root / "spool"), max_total_bytes=1_000_000_000)
        governance = {
            "list_notes": write_governance(self.root, SALES_NOTES_LIST_KIND),
            "get_note": write_governance(self.root, SALES_NOTES_GET_KIND),
        }
        launcher = SalesNotesFeedLauncher(
            digest_dir=FIXTURES, state_dir=self.root, governance_paths=governance,
            spool_dir=self.root / "spool",
        )
        self.addCleanup(launcher.close)
        runner = build_feed_runner(
            launcher=launcher, operation="list_notes",
            governance=ConnectorGovernance.load(governance["list_notes"]),
            store=core, connectors=connectors, observability=observability,
            spool=spool, source_ref=SALES_NOTES,
        )
        receipt = runner.run(
            parameters={"since": "2026-07-01", "until": "2026-09-30", "limit": 2},
            work_ref="work:truncation:1",
        )
        reader = ConnectorCompletionReceiptReader(
            connectors=connectors, observability=observability
        )
        envelope = reader.get_source_envelope(receipt.source_envelope_ref)
        # The operation's ceiling is `enumerated`; this listing did not earn it.
        self.assertEqual(envelope["completeness"], "partial")
        self.assertEqual(envelope["status"], "partial")
        self.assertEqual(len(envelope["source_record_refs"]), 2)
        full = runner.run(
            parameters={"since": "2026-09-01", "until": "2026-09-30"},
            work_ref="work:truncation:2",
        )
        complete = reader.get_source_envelope(full.source_envelope_ref)
        self.assertEqual(complete["completeness"], "enumerated")
        self.assertEqual(complete["status"], "complete")


class RunnerBoundaryTests(unittest.TestCase):
    def test_the_runner_refuses_a_template_that_is_not_a_host_tool(self) -> None:
        from dalton_core.host_tool_runner import HostToolRunError, HostToolRunner
        from dalton_core.sec_financials_core import sec_financials_identity

        # Pointed at a public_https template it would publish a profile
        # claiming no host allowlist and no network policy for a connector
        # whose template names two SEC hosts.
        with self.assertRaises(HostToolRunError) as ctx:
            HostToolRunner(
                store=None, connectors=None, observability=None, spool=None,
                template_key="sec-financials",
                identity=sec_financials_identity(),
                governance=None, command=lambda parameters, output_dir: [],
            )
        self.assertIn("public_https", str(ctx.exception))

    def test_the_lane_refuses_to_spend_quota_before_the_authority_knows_the_feed(self) -> None:
        from dalton_core import mission_feed_lane

        class Server:
            def lane_launcher(self, kwarg):
                return object()

        # DISCOVERY_SOURCES is not patched here: this is the writer as it
        # stands today, and the honest answer is "not configured" rather than
        # fifty child processes whose results the authority will refuse.
        result = mission_feed_lane._dispatch(
            Server(), SALES_NOTES, mission_feed_lane.SALES_NOTES_LAUNCHER_KWARG
        )
        self.assertEqual(result["status"], "unconfigured")
        self.assertIn("DISCOVERY_SOURCES", result["reason"])


class PagedMissions(RecordingMissions):
    """A queue reader with the authority's own page cap and no cursor."""

    PAGE_CAP = 1_000

    def discovered_documents(self, mission_version_ref, *, company_ref=None,
                             status=None, limit=100):
        if not 1 <= limit <= self.PAGE_CAP:
            raise ValueError("discovered document limit must be 1..1000")
        rows = [
            row for row in self.rows.values()
            if (company_ref is None or row["company_ref"] == company_ref)
            and (status is None or row["status"] == status)
        ]
        return [dict(row) for row in rows[:limit]]


class LargeQueueTests(unittest.TestCase):
    """The queue outgrows one page long before the mission is finished."""

    def setUp(self) -> None:
        rows = []
        for index in range(600):
            for company in (ACN, EPAM):
                rows.append({
                    "record_id": f"mission-discovered-document:{company}:{index}",
                    "document_ref": f"sales-note:aaaa{index:012d}{'a' if company == ACN else 'b'}",
                    "source_ref": SALES_NOTES, "company_ref": company,
                    "status": "acquired", "ticket_ref": "sales-notes-run:" + "0" * 24,
                })
        self.missions = PagedMissions(rows)
        self.coordinator = FeedDiscoveryCoordinator(
            missions=self.missions, launcher=None, source_ref=SALES_NOTES,
            plan=load_feed_discovery_plan(PLAN_PATH),
        )

    def test_a_row_past_the_first_page_is_still_found(self) -> None:
        # 1,200 rows: an unfiltered read caps at a thousand and the last two
        # hundred vanish. They are exactly the rows a long-running mission has
        # most of, and the failure lands *after* record_source_discovery has
        # committed -- the row stuck at `discovered`, the tick failing on
        # every pass. Narrowing by company and status is what makes each
        # query small enough to be an answer.
        last = self.missions.rows[f"mission-discovered-document:{EPAM}:599"]
        found = self.coordinator._queued_row(
            "coverage-mission-version:us-it-services:1",
            last["document_ref"], EPAM,
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["record_id"], last["record_id"])

    def test_every_held_document_is_reported_not_the_first_page_of_them(self) -> None:
        held = set()
        for company_ref in (ACN, EPAM):
            held |= self.coordinator._documents_for(
                "coverage-mission-version:us-it-services:1", company_ref=company_ref
            )
        self.assertEqual(len(held), 1_200)

    def test_a_bucket_that_fills_a_page_is_reported_rather_than_truncated(self) -> None:
        # Under-reporting held documents means re-reading them for ever, so
        # the lane refuses to guess when a bucket reaches the cap.
        rows = {
            f"mission-discovered-document:{index}": {
                "record_id": f"mission-discovered-document:{index}",
                "document_ref": f"sales-note:cccc{index:012d}",
                "source_ref": SALES_NOTES, "company_ref": ACN,
                "status": "acquired", "ticket_ref": None,
            }
            for index in range(PagedMissions.PAGE_CAP)
        }
        coordinator = FeedDiscoveryCoordinator(
            missions=PagedMissions(list(rows.values())), launcher=None,
            source_ref=SALES_NOTES, plan=load_feed_discovery_plan(PLAN_PATH),
        )
        with self.assertRaises(FeedLaneError):
            coordinator._documents_for("coverage-mission-version:us-it-services:1")

    def test_the_page_limit_is_the_authority_own_cap(self) -> None:
        from dalton_core.mission_feed_lane import DOCUMENT_PAGE_LIMIT

        signature = inspect.signature(CoverageMissionAuthority.discovered_documents)
        self.assertIn("limit", signature.parameters)
        self.assertEqual(DOCUMENT_PAGE_LIMIT, PagedMissions.PAGE_CAP)


class AuthoritySeamTests(unittest.TestCase):
    """Every call the tick makes must be one the real authority accepts."""

    def test_the_queue_calls_bind_against_the_real_signatures(self) -> None:
        calls = {
            "launched_discovered_documents": ((), {"source_ref": SALES_NOTES, "limit": 2}),
            "next_discovered_document": ((), {"source_ref": SALES_NOTES}),
            "mark_discovered_document_launched": (("record", "ticket"), {}),
            "settle_discovered_document": (("record",), {"status": "acquired", "reason": None}),
            "register_document_review": (("record",), {"requested_by": AUTOMATION}),
            "discovered_documents": (("mission-version",),
                                     {"company_ref": ACN, "limit": 500}),
            "authorize_source_discovery": ((), {"company_ref": ACN,
                                                "source_ref": SALES_NOTES,
                                                "requested_by": AUTOMATION}),
        }
        for name, (args, kwargs) in calls.items():
            with self.subTest(method=name):
                signature = inspect.signature(getattr(CoverageMissionAuthority, name))
                signature.bind(object(), *args, **kwargs)
        # The double stands in for exactly the queue half of that surface.
        for name in ("launched_discovered_documents", "next_discovered_document",
                     "mark_discovered_document_launched", "settle_discovered_document",
                     "register_document_review"):
            self.assertTrue(hasattr(RecordingMissions, name))

    def test_the_feed_sources_are_shaped_like_the_ones_already_registered(self) -> None:
        existing = next(iter(coverage_mission_module.DISCOVERY_SOURCES.values()))
        for source_ref, entry in FEED_DISCOVERY_SOURCES.items():
            with self.subTest(source_ref=source_ref):
                self.assertEqual(set(entry), set(existing))
                self.assertEqual(entry["connector_source_ref"], source_ref)
                self.assertNotIn(source_ref, coverage_mission_module.DISCOVERY_SOURCES)


if __name__ == "__main__":
    unittest.main()
