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
from datetime import date, datetime, timezone
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
from dalton_core.connector_inventory import load_packaged_connector_inventory
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
    attribute_notes,
    attribute_wiki_documents,
    feed_discovery_parameters,
    feed_query_hash,
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
        notes = enumerate_notes(FIXTURES, since="2026-09-01")
        ids = [note["note_id"] for note in notes]
        self.assertEqual(ids, [ACN_NOTE, MACRO_NOTE, EPAM_NOTE, CTSH_NOTE])
        # The morning note is republished by the evening run; it is one note.
        self.assertEqual(len(ids), len(set(ids)))
        first = notes[0]
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
        self.assertEqual(notes, enumerate_notes(FIXTURES, since="2026-09-01"))

    def test_window_and_sender_filters_are_applied_to_the_mail_not_the_run(self) -> None:
        self.assertNotIn(
            OLD_NOTE, [note["note_id"] for note in enumerate_notes(FIXTURES, since="2026-09-01")]
        )
        self.assertIn(
            OLD_NOTE, [note["note_id"] for note in enumerate_notes(FIXTURES, since="2026-07-01")]
        )
        filtered = enumerate_notes(
            FIXTURES, since="2026-09-01", sender_domain="example-broker.test"
        )
        self.assertEqual(
            [note["note_id"] for note in filtered], [MACRO_NOTE, CTSH_NOTE]
        )

    def test_bad_windows_and_unknown_ids_are_refused(self) -> None:
        with self.assertRaises(SalesNotesError):
            enumerate_notes(FIXTURES, since="September")
        with self.assertRaises(SalesNotesError):
            enumerate_notes(FIXTURES, since="2026-09-01", limit=0)
        with self.assertRaises(SalesNotesError):
            read_note(FIXTURES, "sales-note:ffffffffffffffff")


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
        documents = enumerate_documents(
            self.index, self.corpus, since="2026-07-01", company="ACN"
        )
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
        documents = enumerate_documents(
            self.index, self.corpus, since="2026-07-01", industry="us-it-services"
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
            enumerate_documents(self.index, self.corpus, since="2026-07-01", company="ACN")


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
                                 since="2026-09-01")
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
                                 since="2026-09-01")
        self.assertEqual(summary["status"], "succeeded", summary["failure_reason"])
        self.assertEqual(summary["note_count"], 4)
        self.assertEqual(summary["senders"],
                         {"example-bank.test": 2, "example-broker.test": 2})
        self.assertEqual(summary["observation"]["source_record_refs"],
                         [ACN_NOTE, MACRO_NOTE, EPAM_NOTE, CTSH_NOTE])
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
        documents = enumerate_documents(
            self.index, self.corpus, since="2026-07-01", company="ACN"
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
        notes = enumerate_notes(FIXTURES, since="2026-09-01")
        attribution = attribute_notes(notes, UNIVERSE)
        self.assertEqual(attribution["by_company"][ACN], [ACN_NOTE])
        self.assertEqual(attribution["by_company"][EPAM], [EPAM_NOTE])
        self.assertEqual(attribution["by_company"][CTSH], [CTSH_NOTE])
        # The rates note names nobody in its subject, so it is not forced onto
        # a company queue.
        self.assertEqual(attribution["unattributed"], [MACRO_NOTE])

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
            companies={ACN: "Accenture ACN"},
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
                source_ref="source:alphaengine", companies={},
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
            companies={ACN: "Accenture ACN"}, acquisitions_per_tick=2,
            acquisition_wait_seconds=90.0,
        )

    def test_enumeration_runs_through_the_governed_child(self) -> None:
        coordinator = self.coordinator(RecordingMissions([]))
        observation = coordinator.enumerate(since="2026-09-01")
        self.assertEqual(observation["note_count"], 4)
        self.assertEqual(
            coordinator.attribute(observation, UNIVERSE)["unattributed"], [MACRO_NOTE]
        )
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

    def test_recording_without_a_connector_receipt_refuses_with_a_reason(self) -> None:
        coordinator = self.coordinator(RecordingMissions([]))
        observation = coordinator.enumerate(since="2026-09-01")
        with self.assertRaises(FeedLaneError) as ctx:
            coordinator.record_discoveries(
                observation=observation, universe=UNIVERSE,
                parameters=feed_discovery_parameters(
                    terms="Accenture ACN", since="2026-09-01", as_of="2026-09-09"
                ),
                discovery_plan_ref="discovery-plan:x", discovery_plan_hash="0" * 64,
            )
        self.assertIn("host-tool runner", str(ctx.exception))


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
