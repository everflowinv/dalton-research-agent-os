"""Empty financial sections still have exact filing authority, never values."""
from __future__ import annotations

import unittest

from dalton_core.company_model_annual_projection import AnnualProjectionError, verify_statement_filing
from dalton_core.connector_governance import build_governance_record
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.model_cell_reconciliation import collect_actuals
from dalton_core.sec_financials_core import KIND
from dalton_core.store import DaltonStore, content_hash
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_ar_segment_bridge_identity import line

COMPANY = "company:sec-cik:0000999999"


class EmptyLineAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        methods = bootstrap_method_authorities(self.store)
        self.authority = CoverageMissionAuthority(self.store)
        params = mission_params(methods)
        params["universe"] = [{"company_ref": COMPANY, "ticker": "SYNTH",
                               "coverage_tier": "A", "bootstrap_priority": "P0"}]
        self.authority.create_mission(params.pop("mission_ref"), **params)
        self.grant = self.authority.sec_lane_authorization_for_company(COMPANY)
        self.governance = build_governance_record(
            KIND, approved_by="human:synthetic-owner", status="approved",
            effective_from="2026-01-01T00:00:00+00:00", version=5)

    def ingest(self, form="10-Q", lines=None, number=1):
        dispatch = self.authority.queue_statement_dispatch(
            authorization=self.grant, form=form.removesuffix("/A"),
            retry_salt=str(number))
        self.authority.mark_statement_dispatch_launched(
            dispatch["dispatch_id"], "sec-financials-run:" + f"{number:024x}")
        result = self.authority.record_statement_observation(
            dispatch_id=dispatch["dispatch_id"],
            observation={"schema_version": "0.1", "cik": "0000999999",
                "entity_name": "Synthetic Company", "filings": [{
                    "accession": f"0001234567-26-{number:06d}", "form": form,
                    "filed": "2026-08-01", "report_date": "2026-06-30",
                    "lines": [] if lines is None else lines}],
                "source_record_refs": ["raw-sink:" + "c" * 64],
                "next_cursor": None, "provider_status": 200},
            governance_ref=self.governance["id"],
            governance_hash=self.governance["content_hash"],
            governance_proof=self.governance)
        return dict(self.store.connection.execute(
            "SELECT * FROM coverage_mission_statement_filings WHERE ingest_id=?",
            (result["filings"][0]["ingest_id"],)).fetchone())

    def test_real_ingest_empty_and_populated_all_four_forms_replay_exactly(self):
        number = 0
        for form in ("10-Q", "10-K", "10-Q/A", "10-K/A"):
            for lines in ([], [line()]):
                number += 1
                with self.subTest(form=form, empty=not lines):
                    filing = self.ingest(form, lines, number)
                    before = self.store.connection.total_changes
                    self.assertEqual(verify_statement_filing(self.store.connection, filing), "line_bound")
                    self.assertEqual(self.store.connection.total_changes, before)
                    self.assertEqual(filing["line_count"], len(lines))

    def test_empty_reconciliation_has_publication_proof_but_no_numeric_actuals(self):
        filing = self.ingest("10-K/A")
        actuals, events = collect_actuals(self.store.connection, COMPANY)
        self.assertEqual(actuals, [])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source_ref"], filing["ingest_id"])
        self.assertEqual(events[0]["source_hash"], filing["content_hash"])

    def test_empty_filing_corrupt_hash_metadata_identity_or_line_count_still_refused(self):
        filing = self.ingest()
        for field, value in (("content_hash", "a" * 64), ("entity_name", "Foreign Company"),
                             ("source_record_refs_json", '["raw-sink:other"]'),
                             ("governance_hash", "d" * 64), ("filed", "1999-01-01"),
                             ("ingest_id", "statement-ingest:wrong"), ("line_count", 1)):
            with self.subTest(field=field), self.assertRaises(AnnualProjectionError):
                verify_statement_filing(self.store.connection, {**filing, field: value},
                                        allow_legacy_metadata=True)

    def test_legacy_header_only_empty_filing_still_needs_explicit_permission(self):
        filing = self.ingest()
        body = {k: filing[k] for k in ("company_ref", "cik", "accession", "form", "line_count",
            "entity_name", "filed", "report_date", "governance_ref", "governance_hash")}
        body["source_record_refs"] = ["raw-sink:" + "c" * 64]
        legacy = {**filing, "content_hash": content_hash(body)}
        with self.assertRaisesRegex(AnnualProjectionError, "legacy filing"):
            verify_statement_filing(self.store.connection, legacy)
        self.assertEqual(verify_statement_filing(self.store.connection, legacy,
                                               allow_legacy_metadata=True), "header_only")
