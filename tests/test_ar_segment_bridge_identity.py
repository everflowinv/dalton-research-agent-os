"""Synthetic company: filed context identities survive normalization and storage."""
from __future__ import annotations

import json
import unittest

from dalton_core.company_model_annual_projection import AnnualProjectionError, verify_statement_filing
from dalton_core.coverage_mission import CoverageMissionAuthority, CoverageMissionValidationError
from dalton_core.sec_financials_normalise import normalise_statement
from dalton_core.store import DaltonStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

COMPANY = "company:sec-cik:0000999999"
SEGMENTS = "us-gaap:StatementBusinessSegmentsAxis"
CONSOLIDATION = "srt:ConsolidationItemsAxis"


def line(**overrides):
    out = {"statement": "income", "concept": "us-gaap:OperatingIncomeLoss", "label": "Operating income",
           "level": 1, "parent_concept": None, "is_breakdown": True,
           "dimension_axis": CONSOLIDATION, "dimension_member": "us-gaap:OperatingSegmentsMember",
           "dimension_count": 2, "dimensions": {SEGMENTS: "synthetic:AlphaMember",
                                                   CONSOLIDATION: "us-gaap:OperatingSegmentsMember"},
           "period_start": "2026-04-01", "period_end": "2026-06-30",
           "value": "200", "unit": "USD", "balance": "credit"}
    out.update(overrides)
    return out


class IdentityPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        state = bootstrap_method_authorities(self.store)
        self.authority = CoverageMissionAuthority(self.store)
        params = mission_params(state)
        params["universe"] = [{"company_ref": COMPANY, "ticker": "SYNTH", "coverage_tier": "A",
                                "bootstrap_priority": "P0"}]
        mission = self.authority.create_mission(params.pop("mission_ref"), **params)
        grant = self.authority.authorize_sec_lane(company_ref=COMPANY, ticker="SYNTH",
                                                  actor_ref="automation:coverage-mission",
                                                  mission_version_ref=mission["id"],
                                                  mission_version_hash=mission["content_hash"])
        dispatch = self.authority.queue_statement_dispatch(authorization=grant)
        self.authority.mark_statement_dispatch_launched(dispatch["dispatch_id"], "sec-financials-run:" + "1" * 24)
        self.dispatch = dispatch["dispatch_id"]

    def ingest(self, lines):
        self.authority.record_statement_observation(
            dispatch_id=self.dispatch,
            observation={"schema_version": "0.1", "cik": "0000999999", "entity_name": "Synthetic Company",
                         "filings": [{"accession": "0000999999-26-000001", "form": "10-Q", "filed": "2026-08-01",
                                      "report_date": "2026-06-30", "lines": lines}],
                         "source_record_refs": [getattr(self, "raw_ref", "raw-sink:" + "c" * 64)], "next_cursor": None, "provider_status": 200},
            governance_ref="connector-governance:sec-financial-statements:v4", governance_hash="b" * 64)
        filing = self.authority.statement_filings(COMPANY)[0]
        return filing, self.authority.statement_lines(filing["ingest_id"])

    def test_complete_identity_persists_and_is_hash_bound(self):
        filing, rows = self.ingest([line()])
        self.assertEqual(rows[0]["dimensions"], line()["dimensions"])
        verify_statement_filing(self.store.connection, filing)
        self.store.connection.execute("DROP TRIGGER coverage_mission_statement_lines_no_update")
        corrupted = {SEGMENTS: "synthetic:BetaMember", CONSOLIDATION: "us-gaap:OperatingSegmentsMember"}
        self.store.connection.execute("UPDATE coverage_mission_statement_lines SET dimensions_json=?", (json.dumps(corrupted),))
        with self.assertRaises(AnnualProjectionError):
            verify_statement_filing(self.store.connection, filing)

    def test_legacy_row_keeps_its_shape_and_filing_hash(self):
        legacy = line()
        legacy.pop("dimensions")
        filing, rows = self.ingest([legacy])
        self.assertNotIn("dimensions", rows[0])
        verify_statement_filing(self.store.connection, filing)

    def test_legacy_read_only_schema_and_migration_preserve_hash(self):
        legacy = line()
        legacy.pop("dimensions")
        filing, _ = self.ingest([legacy])
        self.store.connection.execute("ALTER TABLE coverage_mission_statement_lines DROP COLUMN dimensions_json")
        verify_statement_filing(self.store.connection, filing)
        CoverageMissionAuthority(self.store)
        verify_statement_filing(self.store.connection, filing)
        row = self.authority.statement_lines(filing["ingest_id"])[0]
        self.assertNotIn("dimensions", row)

    def test_unbound_identity_cannot_be_added_to_a_legacy_filing(self):
        legacy = line()
        legacy.pop("dimensions")
        filing, _ = self.ingest([legacy])
        self.store.connection.execute("DROP TRIGGER coverage_mission_statement_lines_no_update")
        self.store.connection.execute("UPDATE coverage_mission_statement_lines SET dimensions_json=?",
                                      (json.dumps(line()["dimensions"]),))
        with self.assertRaises(AnnualProjectionError):
            verify_statement_filing(self.store.connection, filing)

    def test_inconsistent_context_is_refused_before_storage(self):
        for invalid in (line(dimension_count=1), line(dimension_member="synthetic:WrongMember")):
            with self.subTest(invalid=invalid):
                with self.assertRaises(CoverageMissionValidationError):
                    self.ingest([invalid])
        self.assertEqual(self.authority.statement_filings(COMPANY), [])


class ContextBackfillTests(IdentityPersistenceTests):
    def raw_parse(self, raw=None):
        import tempfile
        from pathlib import Path
        from dalton_core.raw_spool import RawSpool
        from dalton_core.store import canonical_json
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        raw = raw or {"cik": "0000999999", "entity_name": "Synthetic Company", "filings": [{
            "accession": "0000999999-26-000001", "form": "10-Q", "filed": "2026-08-01", "report_date": "2026-06-30",
            "statements": {"income": {
                "structure": [{"concept": "us-gaap_OperatingIncomeLoss", "label": "Operating income", "level": 1,
                               "parent_concept": None, "abstract": False, "is_breakdown": True,
                               "dimension_axis": CONSOLIDATION, "dimension_member": "us-gaap_OperatingSegmentsMember",
                               "balance": "credit"}],
                "facts": [{"concept": "us-gaap:OperatingIncomeLoss", "dimension": CONSOLIDATION,
                           "member": "us-gaap:OperatingSegmentsMember", "period_start": "2026-04-01",
                           "period_end": "2026-06-30", "value": "200", "unit_ref": "USD", "balance": "credit",
                           "dim_srt_ConsolidationItemsAxis": "us-gaap:OperatingSegmentsMember",
                           "dim_us-gaap_StatementBusinessSegmentsAxis": "synthetic:AlphaMember"}]}}}]}
        spool = RawSpool(str(self.state_dir / "raw-spool"), max_total_bytes=1000000)
        import hashlib
        encoded = canonical_json(raw).encode()
        sink = spool.open_sink("raw-sink:" + hashlib.sha256(encoded).hexdigest(), max_response_bytes=100000)
        sink.write(encoded)
        artifact = sink.finalize()
        self.raw_ref = "raw-sink:" + artifact.content_hash

    def test_companyfacts_supplement_uses_independent_refs_and_same_series_proof_resolver(self):
        import hashlib
        from dalton_core.store import canonical_json
        from dalton_core.raw_spool import RawSpool
        from dalton_core.statement_context import StatementContextAuthority, resolve_line_refs, statement_rows
        self.raw_parse()
        legacy = line()
        legacy.pop("dimensions")
        filing, old = self.ingest([legacy])
        fact = {"start": "2026-04-01", "end": "2026-06-30", "val": 50, "accn": filing["accession"],
                "filed": filing["filed"], "form": filing["form"]}
        payload = {"cik": 999999, "facts": {"us-gaap": {
            "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [fact]}}}}}
        raw = canonical_json(payload).encode()
        spool = RawSpool(str(self.state_dir / "raw-spool"), max_total_bytes=1000000)
        sink = spool.open_sink("raw-sink:" + hashlib.sha256(raw).hexdigest(), max_response_bytes=100000)
        sink.write(raw)
        artifact = sink.finalize()
        source = {"raw_hash": artifact.content_hash, "facts": payload["facts"]}
        authority = StatementContextAuthority(self.store)
        from unittest.mock import patch
        from dalton_core.statement_context import refresh_company_contexts, context_record
        with patch("dalton_core.valuation_history.company_facts", return_value=source):
            refreshed = refresh_company_contexts(self.store, self.state_dir, COMPANY,
                                                ingest_ids=[filing["ingest_id"]])
            self.assertFalse(refreshed[0]["reused"])
            repeated = refresh_company_contexts(self.store, self.state_dir, COMPANY)
            self.assertTrue(repeated[0]["reused"])
            self.assertEqual(refreshed[0]["version_id"], repeated[0]["version_id"])
        record = context_record(self.store.connection, refreshed[0]["version_id"])
        self.assertEqual(record["kind"], "complete_snapshot")
        self.assertEqual(record["supplementary_companyfacts"]["raw_hash"], artifact.content_hash)
        current = statement_rows(self.store.connection, filing["ingest_id"])
        self.assertEqual(len(current), 2)
        self.assertNotEqual(current[0]["line_id"], old[0]["line_id"])
        shares = self.authority.statement_series_lines(COMPANY, "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding")
        self.assertEqual(shares[0]["value"], "50")
        self.assertEqual(shares[0]["statement_context_ref"], record["version_id"])
        self.assertEqual(resolve_line_refs(self.store.connection, [current[1]["line_id"]],
                         {filing["ingest_id"]: record["version_id"]}), [current[1]])
        self.assertEqual(resolve_line_refs(self.store.connection, [old[0]["line_id"]]), old)
        self.assertEqual(authority.backfill(self.state_dir, filing["ingest_id"], companyfacts_source=source)["version_id"], record["version_id"])
        bad = {"raw_hash": artifact.content_hash, "facts": {}}
        refused = authority.backfill(self.state_dir, filing["ingest_id"], companyfacts_source=bad)
        self.assertEqual(refused["status"], "refused")

    def test_historical_header_only_hash_requires_independent_source_snapshot(self):
        from dalton_core.store import content_hash
        from dalton_core.statement_context import StatementContextAuthority, statement_rows, resolve_line_refs
        self.raw_parse()
        legacy = line()
        legacy.pop("dimensions")
        filing, old = self.ingest([legacy])
        body = {key: filing[key] for key in ("company_ref", "cik", "accession", "form", "line_count",
                "entity_name", "filed", "report_date", "governance_ref", "governance_hash")}
        body["source_record_refs"] = filing["source_record_refs"]
        historical_hash = content_hash(body)
        self.store.connection.execute("DROP TRIGGER coverage_mission_statement_filings_no_update")
        self.store.connection.execute("UPDATE coverage_mission_statement_filings SET content_hash=?", (historical_hash,))
        filing["content_hash"] = historical_hash
        with self.assertRaisesRegex(AnnualProjectionError, "metadata only"):
            verify_statement_filing(self.store.connection, filing)
        self.assertEqual(verify_statement_filing(self.store.connection, filing, allow_legacy_metadata=True), "header_only")
        record = StatementContextAuthority(self.store).backfill(self.state_dir, filing["ingest_id"])
        self.assertEqual(record["kind"], "complete_snapshot")
        self.assertEqual(record["legacy_authority"], "header_only")
        current = statement_rows(self.store.connection, filing["ingest_id"])
        self.assertNotEqual(current[0]["line_id"], old[0]["line_id"])
        self.assertEqual(resolve_line_refs(self.store.connection, [old[0]["line_id"]]), old)
        with self.assertRaisesRegex(AnnualProjectionError, "metadata only"):
            verify_statement_filing(self.store.connection, filing)
        exact = resolve_line_refs(self.store.connection, [current[0]["line_id"]], {filing["ingest_id"]: record["version_id"]})
        self.assertEqual(exact, current)

    def test_context_overlay_is_exact_append_only_and_preserves_old_proof_resolution(self):
        from dalton_core.statement_context import StatementContextAuthority, resolve_line_refs, statement_rows
        self.raw_parse()
        legacy = line()
        legacy.pop("dimensions")
        filing, old = self.ingest([legacy])
        authority = StatementContextAuthority(self.store)
        context = authority.backfill(self.state_dir, filing["ingest_id"])
        self.assertEqual((context["status"], context["kind"]), ("available", "ordinal_overlay"))
        current = self.authority.statement_lines(filing["ingest_id"])
        self.assertEqual(current[0]["line_id"], old[0]["line_id"])
        self.assertEqual(current[0]["dimensions"], line()["dimensions"])
        self.assertEqual(current[0]["statement_context_ref"], context["version_id"])
        exact = resolve_line_refs(self.store.connection, [old[0]["line_id"]], {filing["ingest_id"]: context["version_id"]})
        self.assertEqual(exact, current)
        self.assertEqual(resolve_line_refs(self.store.connection, [old[0]["line_id"]]), old)
        self.assertEqual(authority.backfill(self.state_dir, filing["ingest_id"])["version_id"], context["version_id"])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM statement_context_versions").fetchone()[0], 1)
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute("UPDATE statement_context_versions SET status='refused'")

    def test_snapshot_uses_independent_refs_when_legacy_normalizer_shape_differs(self):
        from dalton_core.statement_context import StatementContextAuthority, resolve_line_refs
        self.raw_parse()
        legacy = line(label="Older presentation label")
        legacy.pop("dimensions")
        filing, old = self.ingest([legacy])
        context = StatementContextAuthority(self.store).backfill(self.state_dir, filing["ingest_id"])
        self.assertEqual((context["status"], context["kind"]), ("available", "complete_snapshot"))
        current = self.authority.statement_lines(filing["ingest_id"])
        self.assertNotEqual(current[0]["line_id"], old[0]["line_id"])
        self.assertEqual(current[0]["label"], "Operating income")
        self.assertEqual(resolve_line_refs(self.store.connection, [r["line_id"] for r in current],
                                          {filing["ingest_id"]: context["version_id"]}), current)
        self.assertEqual(self.authority.statement_lines(filing["ingest_id"], latest_context=False), old)
        self.assertEqual(self.authority.statement_series_lines(COMPANY, "us-gaap:OperatingIncomeLoss")[0]["line_id"], current[0]["line_id"])

    def test_missing_raw_is_an_explicit_refusal_and_never_enriches_old_rows(self):
        import tempfile
        from dalton_core.statement_context import StatementContextAuthority
        legacy = line()
        legacy.pop("dimensions")
        filing, old = self.ingest([legacy])
        with tempfile.TemporaryDirectory() as directory:
            record = StatementContextAuthority(self.store).backfill(directory, filing["ingest_id"])
        self.assertEqual(record["status"], "refused")
        self.assertTrue(record["reason"])
        self.assertEqual(self.authority.statement_lines(filing["ingest_id"]), old)

    def _check_published_model_proof(self, snapshot=False, supplementary=False):
        from dalton_core.statement_context import StatementContextAuthority
        from dalton_core.sec_financials_normalise import normalise_filing
        from dalton_core.company_model_inputs import build_model_inputs
        from dalton_core.model_forecast_driver import build_forecast_model, ForecastModelAuthority
        from tests.test_model_forecast_driver import spec
        structure = [{"concept": "us-gaap_Revenues", "label": "Revenue", "level": 1, "parent_concept": None,
                      "abstract": False, "is_breakdown": False, "dimension_axis": None, "dimension_member": None}]
        periods = [("2025-07-01", "2025-09-30"), ("2025-10-01", "2025-12-31"),
                   ("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-06-30")]
        facts = [{"concept": "us-gaap:Revenues", "dimension": None, "member": None, "period_start": start,
                  "period_end": end, "unit_ref": "usd", "value": str(100 + index * 5),
                  "dim_us-gaap_StatementBusinessSegmentsAxis": None} for index, (start, end) in enumerate(periods)]
        parsed = {"accession": "0000999999-26-000001", "form": "10-Q", "filed": "2026-08-01", "report_date": "2026-06-30",
                  "statements": {"income": {"structure": structure, "facts": facts}}}
        self.raw_parse({"cik": "0000999999", "entity_name": "Synthetic Company", "filings": [parsed]})
        normalized = normalise_filing(**parsed)["lines"]
        legacy = [{key: value for key, value in row.items() if key != "dimensions"} for row in normalized]
        if snapshot:
            legacy[0]["label"] = "Older normalized presentation"
        filing, _ = self.ingest(legacy)
        contexts = StatementContextAuthority(self.store)
        source = None
        if supplementary:
            import hashlib
            from dalton_core.store import canonical_json
            from dalton_core.raw_spool import RawSpool
            payload = {"cik": 999999, "facts": {"us-gaap": {
                "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [
                    {"start": start, "end": end, "val": 50, "accn": filing["accession"],
                     "filed": filing["filed"], "form": filing["form"]} for start, end in periods]}}}}}
            raw = canonical_json(payload).encode()
            spool = RawSpool(str(self.state_dir / "raw-spool"), max_total_bytes=1000000)
            sink = spool.open_sink("raw-sink:" + hashlib.sha256(raw).hexdigest(), max_response_bytes=100000)
            sink.write(raw)
            artifact = sink.finalize()
            source = {"raw_hash": artifact.content_hash, "facts": payload["facts"]}
        context = contexts.backfill(self.state_dir, filing["ingest_id"], companyfacts_source=source)
        self.assertEqual(context["kind"], "complete_snapshot" if snapshot or supplementary else "ordinal_overlay")
        specification = spec(expenses=[], quarters=8)
        specification["company_ref"] = COMPANY
        table = build_model_inputs(self.authority, specification)
        body = build_forecast_model(specification, table)
        models = ForecastModelAuthority(self.store)
        rows = self.authority.statement_lines(filing["ingest_id"])
        stored = models.publish(body, statement_rows=rows)
        proof = models.filing_proof(stored["id"])
        self.assertEqual(proof["statement_context_bindings"], {filing["ingest_id"]: context["version_id"]})
        from unittest.mock import patch
        with patch("dalton_core.statement_context.normalizer_hash", return_value="d" * 64):
            newer = contexts.backfill(self.state_dir, filing["ingest_id"], companyfacts_source=source)
        self.assertNotEqual(newer["version_id"], context["version_id"])
        self.assertEqual(self.authority.statement_lines(filing["ingest_id"])[0]["statement_context_ref"], newer["version_id"])
        self.assertEqual(models.filing_proof(stored["id"])["content_hash"], proof["content_hash"])

    def test_overlay_model_proof_keeps_exact_version_after_newer_context_exists(self):
        self._check_published_model_proof()

    def test_snapshot_model_proof_resolves_independent_line_refs(self):
        self._check_published_model_proof(snapshot=True)

    def test_supplementary_model_proof_binds_exact_context_and_retains_old_versions(self):
        self._check_published_model_proof(supplementary=True)

    def test_cli_defaults_to_read_only_dry_run(self):
        from pathlib import Path
        from dalton_core.statement_context_cli import main
        import sqlite3
        self.raw_parse()
        legacy = line()
        legacy.pop("dimensions")
        self.ingest([legacy])
        db = self.state_dir / "copy.sqlite"
        copy = sqlite3.connect(db)
        self.store.connection.backup(copy)
        copy.close()
        before = db.read_bytes()
        output = self.state_dir / "dry-run.json"
        main(["--db", str(db), "--state-dir", str(self.state_dir), "--output", str(output)])
        self.assertEqual(db.read_bytes(), before)
        self.assertEqual(json.loads(output.read_text())["available"], 1)
        self.assertTrue(json.loads(output.read_text())["dry_run"])


class NormalisationIdentityTests(unittest.TestCase):
    def test_projected_same_label_and_value_keep_distinct_context_members(self):
        structure = [{"concept": "us-gaap_OperatingIncomeLoss", "label": "Segment operating income",
                      "level": 1, "abstract": False, "parent_concept": None,
                      "dimension_axis": CONSOLIDATION, "dimension_member": "us-gaap_OperatingSegmentsMember"}]
        facts = [{"concept": "us-gaap:OperatingIncomeLoss", "dimension": CONSOLIDATION,
                  "member": "us-gaap:OperatingSegmentsMember", "period_start": "2026-04-01",
                  "period_end": "2026-06-30", "value": "100", "unit_ref": "USD",
                  "dim_us-gaap_StatementBusinessSegmentsAxis": member,
                  "dim_srt_ConsolidationItemsAxis": "us-gaap:OperatingSegmentsMember"}
                 for member in ("synthetic:AlphaMember", "synthetic:BetaMember")]
        rows = normalise_statement(statement="income", structure=structure, facts=facts)["lines"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["dimensions"][SEGMENTS] for row in rows},
                         {"synthetic:AlphaMember", "synthetic:BetaMember"})


if __name__ == "__main__":
    unittest.main()
