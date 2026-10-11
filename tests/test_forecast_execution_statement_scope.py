import copy
import unittest
from unittest.mock import patch

from dalton_core.forecast_consumed_source_groups import select_rows, evaluate
from dalton_core.forecast_statement_scope import statement_scope, RULE_REF as LEGACY_RULE
from dalton_core.forecast_execution_statement_scope import RULE_REF, evaluate_execution
from dalton_core.model_forecast_driver import ForecastModelValidationError
from dalton_core.store import content_hash


class ExecutionStatementScopeTests(unittest.TestCase):
    def fixture(self, company="company:other-issuer"):
        from tests.test_forecast_consumed_source_groups import ConsumedSourceGroupTests
        case = ConsumedSourceGroupTests("runTest")
        case.setUp()
        record, filings, sources = copy.deepcopy((case.record, case.filings, case.rows))
        record["company_ref"] = company
        for filing in filings:
            filing["company_ref"] = company
        record["financial_statement_structure"] = {"lines": [{"ref": "result:revenue",
            "kind": "filed", "concept": "revenue", "statement": "income"}], "formulas": []}
        record["drivers"].append({"ref": "total", "concept": "revenue", "statement": "income",
            "structure_line_ref": "result:revenue", "unit": "usd", "history": [
                {**c, "value": "100"} for c in record["drivers"][0]["history"]]})
        return record, filings, sources

    def scoped(self, record, filings, sources):
        rows, groups = select_rows(record, filings, sources)
        kept, scope = statement_scope(record, rows, source_groups=groups)
        return kept, scope, evaluate_execution(record, statement_rows=kept, scope=scope)

    def test_unselected_bad_disclosure_is_diagnostic_for_two_other_companies(self):
        for company in ("company:manufacturing", "company:consumer-services"):
            with self.subTest(company=company):
                record, filings, sources = self.fixture(company)
                record["drivers"] = [record["drivers"][-1]]
                record["execution_contract"]["specification"] = {"revenue_drivers": [], "driver_formulas": []}
                # The income base is consolidated; an unrelated current
                # disclosure has a genuine non-closing revenue partition.
                total = next(r for r in sources["ingest:old"] if not r["dimensions"])
                total["value"] = "101"
                rows, groups = select_rows(record, filings, sources)
                self.assertEqual(evaluate(record, statement_rows=rows).status, "unavailable")
                kept, scope = statement_scope(record, rows, source_groups=groups)
                self.assertEqual(kept, rows)  # No evidence is removed.
                self.assertEqual(scope["rule_ref"], RULE_REF)
                self.assertEqual(scope["consumed_families"], [])
                self.assertTrue(scope["supporting_partition_diagnostics"])
                self.assertEqual(evaluate_execution(record, statement_rows=kept, scope=scope).status, "available")

    def test_consumed_complete_partition_all_history_is_still_checked(self):
        record, filings, sources = self.fixture()
        rows, scope, report = self.scoped(record, filings, sources)
        self.assertEqual(report.status, "available", report.reasons)
        self.assertEqual(len({f["period_end"] for f in scope["consumed_families"]}), 4)
        self.assertEqual({m for f in scope["consumed_families"] for m in f["members"]}, {"a", "b"})
        # One historic total does not tie, although the newest quarter does.
        next(r for r in sources["ingest:old"] if not r["dimensions"])["value"] = "101"
        self.assertEqual(self.scoped(record, filings, sources)[2].status, "unavailable")

    def test_missing_consumed_member_cannot_borrow_a_restated_source(self):
        record, filings, sources = self.fixture()
        sources["ingest:old"] = [r for r in sources["ingest:old"] if not r["line_id"].endswith(":a")]
        with self.assertRaisesRegex(ForecastModelValidationError, "source is missing"):
            self.scoped(record, filings, sources)

    def test_family_counterpart_and_sibling_unit_cannot_be_hidden(self):
        record, filings, sources = self.fixture()
        # Even a one-member declared branch may not ignore its source peers.
        record["drivers"] = [record["drivers"][0]]
        record["drivers"][0]["structure_line_ref"] = "result:revenue"
        spec = record["execution_contract"]["specification"]
        spec["driver_formulas"] = []
        spec["revenue_drivers"] = spec["revenue_drivers"][:1]
        altered = copy.deepcopy(sources)
        next(r for r in altered["ingest:old"] if r["dimension_member"] == "b")["unit"] = "EUR"
        with self.assertRaisesRegex(ForecastModelValidationError, "mixes units"):
            self.scoped(record, filings, altered)
        altered = copy.deepcopy(sources)
        altered["ingest:old"] = [r for r in altered["ingest:old"] if r["dimensions"]]
        with self.assertRaisesRegex(ForecastModelValidationError, "own consolidated partition"):
            self.scoped(record, filings, altered)

    def test_current_zero_value_hidden_member_is_not_cut_to_declared_subset(self):
        record, filings, sources = self.fixture()
        row = copy.deepcopy(sources["ingest:old"][-1])
        axis = row["dimension_axis"]
        row.update(line_id="hidden-original-member", dimensions={axis: "third"}, dimension_member="third", value="0")
        sources["ingest:old"].append(row)
        with self.assertRaisesRegex(ForecastModelValidationError, "every current filed member"):
            self.scoped(record, filings, sources)

    def test_unavailable_branch_is_a_root_not_a_reason_to_omit_its_partition(self):
        record, filings, sources = self.fixture()
        record["results"] = [{"ref": "result:revenue", "status": "unavailable", "cells": []}]
        sources["ingest:old"][0]["value"] = "101"
        kept, scope, report = self.scoped(record, filings, sources)
        self.assertTrue(scope["consumed_families"])
        self.assertEqual(report.status, "unavailable")

    def test_unknown_formula_reference_or_missing_declared_branch_is_refused(self):
        for change in ("formula", "driver"):
            with self.subTest(change=change):
                record, filings, sources = self.fixture()
                if change == "formula":
                    record["financial_statement_structure"]["formulas"] = [
                        {"output_ref": "result:revenue", "terms": [{"line_ref": "unknown"}]}]
                else:
                    record["drivers"] = [d for d in record["drivers"] if d["ref"] != "economic:a"]
                with self.assertRaisesRegex(ForecastModelValidationError, "unknown structure dependency|lacks exact executed history|missing an executed identity driver"):
                    self.scoped(record, filings, sources)

    def test_changed_source_row_amount_unit_or_period_is_rejected_by_frozen_scope(self):
        record, filings, sources = self.fixture()
        rows, scope, report = self.scoped(record, filings, sources)
        for field, value in (("value", "999"), ("unit", "shares"), ("period_end", "2024-12-30"),
                             ("statement_context_ref", "context:forged")):
            with self.subTest(field=field):
                altered = copy.deepcopy(rows)
                altered[0][field] = value
                with self.assertRaisesRegex(ForecastModelValidationError, "full source rows"):
                    evaluate_execution(record, statement_rows=altered, scope=scope)

    def test_forged_axis_or_driver_scope_is_not_accepted(self):
        record, filings, sources = self.fixture()
        rows, groups = select_rows(record, filings, sources)
        for field, value in (("driver_ref", "unknown"), ("dimensions", {"unknown:Axis": "a"})):
            altered = copy.deepcopy(groups)
            item = next(g for g in altered["groups"] if g["consumed_by"])
            item["consumed_by"][0][field] = value
            altered["content_hash"] = content_hash({k: v for k, v in altered.items() if k != "content_hash"})
            with self.assertRaises(ForecastModelValidationError):
                statement_scope(record, rows, source_groups=altered)

    def test_all_non_partition_economic_checks_remain_active(self):
        record, filings, sources = self.fixture()
        record["drivers"][-1]["role"] = "income_tax_expense"
        record["assumptions"] = [{"ref": "tax", "driver_ref": "total", "measure": "share_of_line",
            "period": {"start": "2025-01-01", "end": "2025-03-31"}, "value": "2", "kind": "estimate"}]
        _, _, report = self.scoped(record, filings, sources)
        self.assertEqual(report.status, "unavailable")
        self.assertTrue(any("tax rate" in reason for reason in report.reasons))

    def test_old_v1_marker_replays_the_original_scope_algorithm(self):
        from tests.test_forecast_statement_scope import StatementScopeTests
        record, rows = StatementScopeTests("runTest").fixture()
        kept, old = statement_scope(record, rows)
        self.assertEqual(old["rule_ref"], LEGACY_RULE)
        with patch("dalton_core.forecast_execution_statement_scope.execution_scope", side_effect=AssertionError("new algorithm on old proof")):
            replay, manifest = statement_scope(record, rows, frozen_scope=old, source_groups={"forged": True})
        self.assertEqual(replay, kept)
        self.assertEqual(manifest, old)


class DurableExecutionScopeTests(unittest.TestCase):
    def fixture(self):
        from tests.test_company_model_forecast import LaneStateTests
        case = LaneStateTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_new_full_source_proof_durable_replay_and_same_model_no_republication(self):
        from dalton_core.model_forecast_driver import ForecastModelAuthority
        case = self.fixture()
        result = case.child()
        authority = ForecastModelAuthority(case.store)
        before = authority.filing_proof(result["model_version_ref"])
        self.assertEqual(before["model_statement_scope"]["rule_ref"], RULE_REF)
        self.assertEqual(authority._filing_proof(result["model_version_ref"], require_replay=True), before)
        self.assertEqual(case.child()["forecast_status"], "nothing_to_model")
        self.assertEqual(authority.filing_proof(result["model_version_ref"]), before)

    def test_fake_scope_or_original_row_is_rejected_on_durable_reader(self):
        from dalton_core.model_forecast_driver import ForecastModelAuthority, ForecastModelConflict
        import json
        case = self.fixture()
        result = case.child()
        authority = ForecastModelAuthority(case.store)
        proof = authority.filing_proof(result["model_version_ref"])
        case.store.connection.execute("DROP TRIGGER forecast_model_filing_proof_no_update")
        altered = copy.deepcopy(proof)
        altered["model_statement_scope"]["roots_hash"] = "0" * 64
        altered["model_statement_scope"]["content_hash"] = content_hash({k: v for k, v in altered["model_statement_scope"].items() if k != "content_hash"})
        altered["content_hash"] = content_hash({k: v for k, v in altered.items() if k != "content_hash"})
        case.store.connection.execute("UPDATE forecast_model_filing_proofs SET record_json=?,content_hash=? WHERE model_version_id=?",
            (json.dumps(altered), altered["content_hash"], result["model_version_ref"]))
        with self.assertRaises(ForecastModelConflict):
            authority.filing_proof(result["model_version_ref"])

    def test_real_publisher_still_refuses_wrong_value_period_unit_and_source(self):
        from dalton_core import company_model_forecast as forecast
        for field, value in (("value", "999999999999"), ("period_end", "2026-05-30"),
                             ("unit", "shares"), ("accessions", ["wrong-filing"])):
            with self.subTest(field=field):
                case = self.fixture()
                original = forecast.build_structured_forecast_model
                def altered(*args, **kwargs):
                    body = original(*args, **kwargs)
                    if field == "unit":
                        body["drivers"][0]["unit"] = value
                    else:
                        body["drivers"][0]["history"][0][field] = value
                    return body
                with patch("dalton_core.company_model_forecast.build_structured_forecast_model", side_effect=altered):
                    result = case.child()
                self.assertNotEqual(result.get("forecast_status"), "published")
                self.assertIsNone(case.store.connection.execute("SELECT version_id FROM forecast_model_versions LIMIT 1").fetchone())

    def test_rule_changes_only_free_forecast_identity_once_preserving_failed_ticket(self):
        from tests import test_mission_model_forecast_lane as lane_fixture
        from tests import test_company_financial_statement_structure as structure_fixture
        from tests import test_financial_structure_forecast_consumer as consumer_fixture
        from dalton_core.company_model_forecast import model_digest
        fixture = lane_fixture.LaneTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        table = consumer_fixture.financial_inputs()
        table["company_ref"] = lane_fixture.ACN
        spec = structure_fixture.company_spec()
        spec["company_ref"] = lane_fixture.ACN
        spec["decided_by"] = "automation:test"
        spec["financial_statement_structure"] = consumer_fixture.forecastable_proposal(table)
        before = copy.deepcopy(spec)
        def old_hash(body):
            if isinstance(body, dict) and "filed_execution_statement_scope_rule" in body:
                body = {k: v for k, v in body.items() if k != "filed_execution_statement_scope_rule"}
            return content_hash(body)
        with patch("dalton_core.mission_model_forecast_lane.pending_companies", return_value=[(lane_fixture.ACN, spec, table)]), \
             patch("dalton_core.mission_model_forecast_lane.forecast_reporting_boundary", return_value=None):
            with patch("dalton_core.company_model_forecast.content_hash", side_effect=old_hash):
                old_digest = model_digest(spec, table)
                first = fixture.lane.dispatch_once()
            fixture.launcher.finish(first["ticket_ref"], status="failed", summary={
                "forecast_status": "unavailable:economic_invariants", "failure_reason": "segment_sum: unconsumed disclosure does not close"})
            old_ticket = copy.deepcopy(fixture.launcher.tickets[first["ticket_ref"]])
            new = fixture.lane.dispatch_once()
            self.assertEqual(new["status"], "launched")
            self.assertNotEqual(new["model_digest"], old_digest)
            self.assertEqual(fixture.launcher.tickets[first["ticket_ref"]], old_ticket)
            fixture.launcher.finish(new["ticket_ref"], status="failed", summary={
                "forecast_status": "unavailable:economic_invariants", "failure_reason": "segment_sum: genuinely consumed group does not close"})
            self.assertEqual(fixture.lane.dispatch_once()["status"], "held")
            self.assertEqual(len(fixture.launcher.started), 2)
        self.assertEqual(spec, before)
