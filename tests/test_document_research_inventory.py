from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from dalton_core.document_research_inventory import (
    document_inventory_signature, inventory_with_registry, load_document_inventory,
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

    def test_absent_config_does_not_create_sources_or_authority(self):
        before = list(self.state.iterdir())
        result = load_document_inventory(core=self.core, mission=self.mission, state_dir=self.state)
        self.assertEqual(result["status"], "unconfigured")
        self.assertEqual(list(self.state.iterdir()), before)

    def test_all_source_read_limits_are_explicit_config(self):
        value = {"schema_version": "document-research-config-0.1", "purpose": "directed_research",
                 "spool_dir": str(self.state), "enabled_sources": ["source:sales-notes"], "policy": policy(),
                 "source_reading_limits": {"alphaengine_max_document_chars": 10000000,
                     "public_web_max_source_chars": 9000000, "public_web_max_pdf_pages": 2000,
                     "public_web_max_decompressed_bytes": 80000000}}
        self.assertEqual(validate_inventory_config(value)["source_reading_limits"], value["source_reading_limits"])
        value["source_reading_limits"].pop("public_web_max_pdf_pages")
        with self.assertRaises(ValueError):
            validate_inventory_config(value)
