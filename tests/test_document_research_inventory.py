from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from dalton_core.document_research_inventory import (
    document_inventory_signature, financial_note_targets_for_registration,
    inventory_with_registry, load_document_inventory,
    validate_inventory_config,
)
from dalton_core.document_research import build_document_research_policy
from dalton_core.store import content_hash


def policy():
    return build_document_research_policy(
        policy_ref="document-research-policy:fixture", allowed_purposes=["directed_research"],
        allowed_access_policy_refs=["access-policy:fixture"], max_question_chars=4000,
        max_query_terms=12, max_query_term_chars=200, max_results=20,
        max_context_before_chars=2000, max_context_after_chars=2000, max_read_chars=20000)


class DocumentInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE coverage_mission_discovered_documents (record_id TEXT, "
                        "mission_version_ref TEXT, company_ref TEXT, source_ref TEXT, "
                        "document_ref TEXT, status TEXT, ticket_ref TEXT)")
        self.db.execute("CREATE TABLE coverage_mission_pointer (mission_version_id TEXT)")
        self.db.execute("INSERT INTO coverage_mission_pointer VALUES ('mission:1')")
        self.core = SimpleNamespace(connection=self.db)
        self.mission = {"id": "mission:1", "universe": [{"company_ref": "company:A"}]}

    def insert(self, ref="record:1", mission="mission:1", company="company:A", ticket="ticket:1"):
        self.db.execute("INSERT INTO coverage_mission_discovered_documents VALUES (?,?,?,?,?,?,?)",
                        (ref, mission, company, "source:sales-notes", "sales-note:1", "acquired", ticket))

    def registry(self):
        registration = {"id": "registered-document:fixture", "document_ref": "sales-note:1",
                        "source_ref": "source:sales-notes", "acquisition_ticket_ref": "ticket:1",
                        "doc_kind": "sales_note", "doc_date": "2026-09-11",
                        "source_authority": {"kind": "coverage-mission-acquired-document",
                            "ref": "record:1", "company_ref": "company:A", "mission_version_ref": "mission:1"},
                        "normalized_text": {"status": "complete", "truncated": False, "text_sha256": "a" * 64}}
        registration["content_hash"] = content_hash(registration)
        registry = SimpleNamespace(policy=policy(), inspect_acquired_document=Mock(return_value={
            "available": True, "registration": registration}))
        return registry, registration

    def test_only_exact_mission_and_company_rows_enter_planner_inventory(self):
        self.insert()
        self.insert("foreign:mission", mission="mission:old")
        self.insert("foreign:company", company="company:B")
        registry, registration = self.registry()
        result = inventory_with_registry(core=self.core, mission=self.mission, registry=registry,
                                         purpose="directed_research")
        registry.inspect_acquired_document.assert_called_once_with(record_id="record:1", purpose="directed_research")
        self.assertEqual(len(result["readable_documents_by_company"]["company:A"]), 1)
        self.assertEqual(result["registration_by_hash"][registration["content_hash"]], registration)
        self.assertNotIn("registration", result["readable_documents_by_company"]["company:A"][0])

    def test_self_hashed_foreign_ownership_is_not_a_company_read_capability(self):
        self.insert()
        registry, registration = self.registry()
        registration["source_authority"]["company_ref"] = "company:B"
        registration["content_hash"] = content_hash({k: v for k, v in registration.items() if k != "content_hash"})
        with self.assertRaisesRegex(ValueError, "Core ownership"):
            inventory_with_registry(core=self.core, mission=self.mission, registry=registry, purpose="directed_research")

    def test_legacy_null_ticket_uses_the_registrys_verified_pinned_ticket(self):
        self.insert(ticket=None)
        registry, registration = self.registry()
        result = inventory_with_registry(core=self.core, mission=self.mission, registry=registry,
                                         purpose="directed_research")
        self.assertEqual(result["registration_by_hash"][registration["content_hash"]]["acquisition_ticket_ref"], "ticket:1")

    def test_missing_or_denied_original_is_explicit_and_cannot_be_selected(self):
        self.insert()
        registry, _ = self.registry()
        registry.inspect_acquired_document.return_value = {
            "available": False, "registration": None, "reason": "access_policy_denied"}
        result = inventory_with_registry(core=self.core, mission=self.mission, registry=registry, purpose="directed_research")
        self.assertEqual(result["readable_documents_by_company"]["company:A"], [])
        self.assertEqual(result["registration_by_hash"], {})
        self.assertEqual(result["unavailable_documents_by_company"]["company:A"][0]["reason"], "access_policy_denied")

    def test_exact_ticket_and_config_wake_planner_without_a_count_change(self):
        self.insert()
        before = document_inventory_signature(self.core, self.state)
        self.db.execute("UPDATE coverage_mission_discovered_documents SET ticket_ref='ticket:2'")
        after = document_inventory_signature(self.core, self.state)
        self.assertNotEqual(before, after)
        (self.state / "document-research-config.json").write_text("{}")
        self.assertNotEqual(after, document_inventory_signature(self.core, self.state))

    def test_new_statement_filing_wakes_target_planning(self):
        before = document_inventory_signature(self.core, self.state)
        self.db.execute("CREATE TABLE coverage_mission_statement_dispatches ("
                        "dispatch_id TEXT,mission_version_ref TEXT,mission_version_hash TEXT,"
                        "company_ref TEXT,form TEXT,status TEXT)")
        self.db.execute("CREATE TABLE coverage_mission_statement_filings ("
                        "ingest_id TEXT,dispatch_id TEXT,company_ref TEXT,accession TEXT,"
                        "form TEXT,report_date TEXT,content_hash TEXT)")
        self.db.execute("INSERT INTO coverage_mission_statement_dispatches VALUES (?,?,?,?,?,?)",
                        ("dispatch:1", "mission:1", "a" * 64, "company:A", "10-K", "succeeded"))
        self.db.execute("INSERT INTO coverage_mission_statement_filings VALUES (?,?,?,?,?,?,?)",
                        ("statement-ingest:" + "b" * 32, "dispatch:1", "company:A",
                         "0000000001-25-000001", "10-K", "2024-12-31", "c" * 64))
        self.assertNotEqual(before, document_inventory_signature(self.core, self.state))

    def test_absent_config_does_not_create_sources_or_authority(self):
        before = list(self.state.iterdir())
        result = load_document_inventory(core=self.core, mission=self.mission, state_dir=self.state)
        self.assertEqual(result["status"], "unconfigured")
        self.assertEqual(list(self.state.iterdir()), before)

    def test_all_source_read_limits_are_explicit_config(self):
        value = {"schema_version": "document-research-config-0.1", "purpose": "directed_research",
                 "inventory_preview_chars": 600,
                 "spool_dir": str(self.state), "enabled_sources": ["source:sales-notes", "source:prior-research"], "policy": policy(),
                 "source_reading_limits": {"alphaengine_max_document_chars": 10000000,
                     "public_web_max_source_chars": 9000000, "public_web_max_pdf_pages": 2000,
                     "public_web_max_decompressed_bytes": 80000000}}
        self.assertEqual(validate_inventory_config(value)["source_reading_limits"], value["source_reading_limits"])
        value["source_reading_limits"].pop("public_web_max_pdf_pages")
        with self.assertRaises(ValueError):
            validate_inventory_config(value)

    def test_sec_10k_target_requires_exact_annual_diluted_eps_and_share_lines(self):
        mission_hash = "9" * 64
        self.mission["content_hash"] = mission_hash
        self.db.executescript("""
            CREATE TABLE coverage_mission_statement_dispatches (
                dispatch_id TEXT, mission_version_ref TEXT, mission_version_hash TEXT,
                company_ref TEXT, form TEXT, status TEXT);
            CREATE TABLE coverage_mission_statement_filings (
                ingest_id TEXT, dispatch_id TEXT, company_ref TEXT, cik TEXT,
                entity_name TEXT, accession TEXT, form TEXT, filed TEXT,
                report_date TEXT, line_count INTEGER, source_record_refs_json TEXT,
                governance_ref TEXT, governance_hash TEXT, recorded_at TEXT,
                content_hash TEXT);
            CREATE TABLE coverage_mission_statement_lines (
                line_id TEXT, ingest_id TEXT, statement TEXT, ordinal INTEGER,
                concept TEXT, label TEXT, level INTEGER, parent_concept TEXT,
                is_breakdown INTEGER, dimension_axis TEXT, dimension_member TEXT,
                dimension_count INTEGER, period_start TEXT, period_end TEXT,
                value TEXT, unit TEXT, balance TEXT);
        """)
        accession = "0000000001-25-000001"
        identity = {"company_ref": "company:A", "cik": "0000000001",
                    "accession": accession, "form": "10-K", "line_count": 2}
        ingest = "statement-ingest:" + content_hash(identity)[:32]
        lines = [
            {"statement": "income", "concept": "us-gaap:EarningsPerShareDiluted",
             "label": "Diluted earnings per share", "level": 0,
             "parent_concept": None, "is_breakdown": False,
             "dimension_axis": None, "dimension_member": None, "dimension_count": 0,
             "period_start": "2024-01-01", "period_end": "2024-12-31",
             "value": "4.25", "unit": "usdPerShare", "balance": None},
            {"statement": "income",
             "concept": "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
             "label": "Diluted weighted average shares", "level": 0,
             "parent_concept": None, "is_breakdown": False,
             "dimension_axis": None, "dimension_member": None, "dimension_count": 0,
             "period_start": "2024-01-01", "period_end": "2024-12-31",
             "value": "100", "unit": "shares", "balance": None},
        ]
        source_refs = ["source-record:fixture"]
        body = {**identity, "entity_name": "Fixture", "filed": "2025-02-01",
                "report_date": "2024-12-31", "source_record_refs": source_refs,
                "governance_ref": "governance:fixture", "governance_hash": "8" * 64,
                "statement_lines_hash": content_hash(lines)}
        filing_hash = content_hash(body)
        self.db.execute("INSERT INTO coverage_mission_statement_dispatches VALUES (?,?,?,?,?,?)",
                        ("dispatch:1", "mission:1", mission_hash, "company:A", "10-K", "succeeded"))
        self.db.execute("INSERT INTO coverage_mission_statement_filings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ingest, "dispatch:1", "company:A", "0000000001", "Fixture", accession,
                         "10-K", "2025-02-01", "2024-12-31", 2,
                         '[\"source-record:fixture\"]', "governance:fixture", "8" * 64,
                         "2025-02-01T00:00:00+00:00", filing_hash))
        for ordinal, line in enumerate(lines):
            self.db.execute(
                "INSERT INTO coverage_mission_statement_lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{ingest}#{ordinal}", ingest, line["statement"], ordinal,
                 line["concept"], line["label"], line["level"], line["parent_concept"],
                 int(line["is_breakdown"]), line["dimension_axis"], line["dimension_member"],
                 line["dimension_count"], line["period_start"], line["period_end"],
                 line["value"], line["unit"], line["balance"]),
            )
        registration = {
            "id": "registered-document:fixture", "document_ref": f"sec:filing:{accession}",
            "source_ref": "source:sec-edgar", "acquisition_ticket_ref": "ticket:1",
            "doc_kind": "sec_filing", "doc_date": "2025-02-01",
            "source_authority": {"kind": "coverage-mission-acquired-document",
                "ref": "record:1", "company_ref": "company:A", "mission_version_ref": "mission:1"},
            "normalized_text": {"status": "complete", "truncated": False,
                                "text_sha256": "a" * 64},
        }
        registration["content_hash"] = content_hash(registration)
        targets = financial_note_targets_for_registration(
            connection=self.db, mission=self.mission, company_ref="company:A",
            registration=registration,
        )
        self.assertEqual(targets, [{
            "schema_version": "financial-note-target-0.1",
            "target_ref": "financial_note:diluted_eps_numerator:0.1",
            "kind": "diluted_eps_numerator", "statement_ingest_ref": ingest,
            "statement_filing_hash": filing_hash, "accession": accession,
            "form": "10-K", "applicability_kind": "annual",
            "periods": [{"period_start": "2024-01-01", "period_end": "2024-12-31"}],
        }])
        self.db.execute(
            "INSERT INTO coverage_mission_discovered_documents VALUES (?,?,?,?,?,?,?)",
            ("record:1", "mission:1", "company:A", "source:sec-edgar",
             f"sec:filing:{accession}", "acquired", "ticket:1"),
        )
        registry = SimpleNamespace(
            policy=policy(), inspect_acquired_document=Mock(return_value={
                "available": True, "registration": registration,
            }),
        )
        inventory = inventory_with_registry(
            core=self.core, mission=self.mission, registry=registry,
            purpose="directed_research",
        )
        self.assertEqual(
            inventory["readable_documents_by_company"]["company:A"][0]["evidence_targets"],
            targets,
        )
        # A later attempt to remove the filed share authority is statement
        # tampering, not a reason to keep projecting the old target.
        self.db.execute(
            "UPDATE coverage_mission_statement_lines SET concept='us-gaap:CommonStockSharesOutstanding' "
            "WHERE ordinal=1")
        self.assertEqual(financial_note_targets_for_registration(
            connection=self.db, mission=self.mission, company_ref="company:A",
            registration=registration,
        ), [])
        inventory = inventory_with_registry(
            core=self.core, mission=self.mission, registry=registry,
            purpose="directed_research",
        )
        document = inventory["readable_documents_by_company"]["company:A"][0]
        self.assertNotIn("evidence_targets", document)
        self.assertEqual(document["unavailable_evidence_targets"], [{
            "target_ref": "financial_note:diluted_eps_numerator:0.1",
            "status": "unavailable",
            "reason": "exact_standard_diluted_eps_and_weighted_shares_authority_unavailable",
            "statement_ingest_ref": ingest,
            "statement_filing_hash": filing_hash,
        }])
