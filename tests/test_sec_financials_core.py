"""P13ag: SEC read as statements, alongside -- not instead of -- reading filings.

Dalton reads SEC company facts one XBRL concept per dispatch. Correct, and
slow: five successful runs in a day produced six figures. What it cannot
produce at all is the statement *structure* -- which concept is which line,
what it rolls into, the dimension axes a company reports against -- and a model
needs that before it can have line items.

The same SEC is the same source, so the source hash is shared with the filings
connector. The connector, the schema and the approval are its own, exactly as
P10e split list_filings from get_company_facts.

The owner accepted the trade this rests on: the parser does its own HTTP, so
these bytes are not verified by Dalton's transport the way company-facts bytes
are. Every line carries its accession, so any figure can be taken back to SEC.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from dalton_core.connector_governance import (
    ConnectorGovernance,
    build_governance_record,
    governance_kind_for_capability,
)
from dalton_core.connector_inventory import load_packaged_connector_inventory
from dalton_core.sec_financials_core import (
    CAPABILITY_ID,
    KIND,
    OPERATION,
    SecFinancialsError,
    build_sec_financials_governance_record,
    sec_financials_identity,
    sec_financials_permissions,
    sec_financials_schema_hash,
    sec_financials_source_hash,
)

REPO = Path(__file__).resolve().parents[1]
# The record the current contract is covered by. v1 predates period_start and
# is kept as history, exactly as the three sec-company-facts records are.
RECORD = REPO / "deploy" / "connector-governance" / "sec-financial-statements-v3.json"


class IdentityTests(unittest.TestCase):
    def test_the_source_is_the_same_sec(self):
        # Sharing the source hash is the point: one SEC, read two ways.
        from dalton_core.sec_filings_index import filings_index_source_hash

        self.assertEqual(sec_financials_source_hash(), filings_index_source_hash())

    def test_the_connector_is_not_the_filings_connector(self):
        templates = load_packaged_connector_inventory()["templates"]
        self.assertNotEqual(templates["sec-financials"]["connector_ref"],
                            templates["sec"]["connector_ref"])

    def test_the_schema_hash_binds_one_operation(self):
        identity = sec_financials_identity()
        self.assertEqual(identity["allowed_operations"], [OPERATION])
        self.assertEqual(list(identity["output_schema_refs"]), [OPERATION])

    def test_it_does_not_share_a_schema_hash_with_company_facts(self):
        # Sharing one would let either approval cover the other.
        from dalton_core.research_plan_executor import sec_connector_identity

        facts = sec_connector_identity(
            load_packaged_connector_inventory()["templates"]["sec"], "get_company_facts")
        self.assertNotEqual(sec_financials_schema_hash(), facts["schema_hash"])

    def test_the_adapter_hash_names_the_parser(self):
        # The parse is the part being trusted, so the identity says which
        # library produced it rather than leaving that implicit.
        from dalton_core.sec_financials_core import ADAPTER_LIBRARY, sec_financials_adapter_hash
        from dalton_core.store import content_hash

        template = load_packaged_connector_inventory()["templates"]["sec-financials"]
        self.assertEqual(sec_financials_adapter_hash(), content_hash({
            "target_ref": template["transport"]["target_ref"],
            "source": template["source_identity"]["source_ref"],
            "operation": OPERATION,
            "library": ADAPTER_LIBRARY,
        }))

    def test_the_capability_maps_back_to_its_kind(self):
        self.assertEqual(governance_kind_for_capability(CAPABILITY_ID), KIND)


class OutputContractTests(unittest.TestCase):
    """The wire is the normalised shape, not the parser's own."""

    def schema(self):
        template = load_packaged_connector_inventory()["templates"]["sec-financials"]
        ref = f"schema:connector-inventory:sec-financials:{OPERATION}:output:0.1"
        for document in template["schema_documents"]:
            if document["schema_ref"] == ref:
                return document["document"]
        raise AssertionError("packaged template has no statements output schema")

    def line(self, **overrides):
        base = {
            "statement": "income", "concept": "us-gaap_Revenues",
            "label": "Revenues", "level": 1, "parent_concept": None,
            "is_breakdown": False, "dimension_axis": None,
            "dimension_member": None, "period_start": "2026-04-01",
            "dimension_count": None,
            "period_end": "2026-06-30",
            "value": "2814828000", "unit": "USD", "balance": "credit",
        }
        base.update(overrides)
        return base

    def check(self, instance, pointer):
        from dalton_core.authority_resolver import _schema_matches

        _schema_matches(instance, pointer, "output")

    def test_a_disclosed_line_validates(self):
        properties = self.schema()["properties"]["filings"]["items"]["properties"]
        self.check(self.line(), properties["lines"]["items"])

    def test_a_figure_is_text_not_a_float(self):
        # Every figure in this system is text for the same reason: a float is
        # not what was filed.
        from dalton_core.authority_resolver import AuthorityResolutionConflict

        properties = self.schema()["properties"]["filings"]["items"]["properties"]
        with self.assertRaises(AuthorityResolutionConflict):
            self.check(self.line(value=2814828000.0), properties["lines"]["items"])

    def test_a_line_the_company_did_not_report_may_be_null(self):
        properties = self.schema()["properties"]["filings"]["items"]["properties"]
        self.check(self.line(value=None), properties["lines"]["items"])

    def test_the_statement_name_is_closed(self):
        from dalton_core.authority_resolver import AuthorityResolutionConflict

        properties = self.schema()["properties"]["filings"]["items"]["properties"]
        with self.assertRaises(AuthorityResolutionConflict):
            self.check(self.line(statement="equity"), properties["lines"]["items"])

    def test_the_structure_a_model_needs_is_required(self):
        # level / parent_concept / dimension_axis are the reason this connector
        # exists; a wire that could omit them would be company-facts again.
        required = set(
            self.schema()["properties"]["filings"]["items"]
            ["properties"]["lines"]["items"]["required"]
        )
        self.assertLessEqual(
            {"level", "parent_concept", "is_breakdown", "dimension_axis",
             "dimension_count", "period_start", "period_end"},
            required,
        )

    def test_a_quarter_and_a_year_to_date_are_distinguishable(self):
        # They share an end date; only the start tells them apart, and a
        # contract without it would let half-years be ingested as quarters.
        properties = self.schema()["properties"]["filings"]["items"]["properties"]
        quarter = self.line(period_start="2026-04-01", value="1414767000")
        year_to_date = self.line(period_start="2026-01-01", value="2814828000")
        self.check(quarter, properties["lines"]["items"])
        self.check(year_to_date, properties["lines"]["items"])
        self.assertNotEqual(quarter["period_start"], year_to_date["period_start"])
        self.assertEqual(quarter["period_end"], year_to_date["period_end"])

    def test_a_balance_sheet_line_has_no_start(self):
        # An instant is as-of, not over.
        properties = self.schema()["properties"]["filings"]["items"]["properties"]
        self.check(self.line(statement="balance", period_start=None),
                   properties["lines"]["items"])

    def test_every_filing_names_its_accession(self):
        filing = self.schema()["properties"]["filings"]["items"]
        self.assertIn("accession", filing["required"])
        self.assertEqual(filing["properties"]["accession"]["pattern"],
                         "^[0-9]{10}-[0-9]{2}-[0-9]{6}$")


class GovernanceTests(unittest.TestCase):
    def test_the_capability_is_credential_free_public_https(self):
        from dalton_core.sec_authority_harness import PUBLIC_PERMISSIONS

        permissions = sec_financials_permissions()
        self.assertEqual(permissions["credential_slot_refs"], [])
        self.assertIs(permissions["core_db"], False)
        # It does reach out, unlike the MCP connectors.
        self.assertIs(permissions["network"], True)
        # The same declaration the filings lane carries: one boundary, one copy.
        self.assertEqual(permissions, PUBLIC_PERMISSIONS)
        # Which hosts it may reach is the template's allowlist.
        template = load_packaged_connector_inventory()["templates"]["sec-financials"]
        self.assertEqual(sorted(template["transport"]["allowed_hosts"]),
                         ["data.sec.gov", "www.sec.gov"])

    def test_a_record_is_proposed_and_needs_a_human(self):
        self.assertEqual(
            build_sec_financials_governance_record(approved_by="human:lumos")["status"],
            "proposed")
        with self.assertRaises(SecFinancialsError):
            build_sec_financials_governance_record(approved_by="automation:dalton")

    def test_the_shared_builder_produces_the_same_record(self):
        direct = build_sec_financials_governance_record(
            approved_by="human:lumos", effective_from="2026-09-09T00:00:00+00:00", version=2)
        shared = build_governance_record(
            KIND, approved_by="human:lumos", status="proposed",
            effective_from="2026-09-09T00:00:00+00:00", version=2)
        self.assertEqual(direct, shared)

    def test_the_shipped_record_is_proposed_and_matches_the_template(self):
        self.assertTrue(RECORD.is_file())
        record = json.loads(RECORD.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "proposed")
        self.assertEqual(record["expected_schema_hash"], sec_financials_schema_hash())
        self.assertEqual(record["expected_source_hash"], sec_financials_source_hash())

    def test_v2_schema_identity_remains_byte_compatible(self):
        self.assertEqual(
            sec_financials_schema_hash(version=2),
            "086316a8feb82c20b8a8a54fc71fb055631edc31ada2ec44bffdd1cb90dc7330",
        )

    def test_the_shipped_record_loads_and_is_not_yet_usable(self):
        import tempfile

        record = json.loads(RECORD.read_text(encoding="utf-8"))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(record, handle)
            path = handle.name
        try:
            self.assertFalse(ConnectorGovernance.load(path).approved)
        finally:
            Path(path).unlink()

    def test_the_installer_seeds_it(self):
        install = (REPO / "deploy" / "macos" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("sec-financial-statements-${sec_financials_version}.json", install)


if __name__ == "__main__":
    unittest.main()
