import copy
import unittest
from unittest.mock import patch

from dalton_core.store import content_hash
from dalton_core.forecast_consumed_source_groups import select_rows
from dalton_core.forecast_statement_scope import statement_scope
from dalton_core.forecast_execution_statement_scope import (
    execution_scope, evaluate_execution, RULE_REF, LEGACY_RULE_REF)
from dalton_core import forecast_execution_statement_scope_legacy as legacy
from dalton_core.model_forecast_driver import ForecastModelValidationError


class SourceFamilyPartitionTests(unittest.TestCase):
    def fixture(self, company="company:industrial", fixed=False):
        from tests.test_forecast_execution_statement_scope import ExecutionStatementScopeTests
        record, filings, sources = ExecutionStatementScopeTests("runTest").fixture(company)
        if fixed:
            marker = {"srt:ConsolidationItemsAxis": "us-gaap:OperatingSegmentsMember"}
            for driver in record["drivers"]:
                if driver.get("statement_identity"):
                    driver["statement_identity"]["dimensions"].update(marker)
            for declaration in record["execution_contract"]["specification"]["revenue_drivers"]:
                declaration["basis_identity"]["dimensions"].update(marker)
            for rows in sources.values():
                for row in rows:
                    if row["dimensions"]:
                        row["dimensions"].update(marker)
                        row["dimension_count"] = 2
        return record, filings, sources

    def run_scope(self, record, filings, sources):
        rows, groups = select_rows(record, filings, sources)
        kept, scope = statement_scope(record, rows, source_groups=groups)
        return kept, scope, evaluate_execution(record, statement_rows=kept, scope=scope)

    def duplicates(self, sources):
        for rows in sources.values():
            for row in list(rows):
                copy_ = copy.deepcopy(row)
                copy_["line_id"] += ":second-original"
                copy_["ordinal"] += 100
                # Different scalar projections of identical full dimensions.
                if len(copy_["dimensions"]) == 2:
                    copy_["dimension_axis"] = "srt:ConsolidationItemsAxis"
                    copy_["dimension_member"] = "us-gaap:OperatingSegmentsMember"
                rows.append(copy_)

    def test_two_companies_equal_full_identity_dedup_retains_every_original(self):
        for company in ("company:industrial", "company:services"):
            for fixed in (False, True):
                with self.subTest(company=company, fixed=fixed):
                    record, filings, sources = self.fixture(company, fixed)
                    self.duplicates(sources)
                    before = copy.deepcopy(sources)
                    rows, scope, report = self.run_scope(record, filings, sources)
                    self.assertEqual(report.status, "available", report.reasons)
                    self.assertEqual(scope["rule_ref"], RULE_REF)
                    self.assertEqual(sources, before)
                    self.assertEqual(len(rows), 32)
                    self.assertEqual(len(scope["arithmetic_partitions"]), 4)
                    for entry in scope["arithmetic_partitions"]:
                        self.assertEqual(len(entry["group"]["parts"]), 2)
                        self.assertTrue(all(len(o["statement_line_refs"]) == 2 for o in entry["origins"]))

    def test_conflicting_duplicate_value_or_unit_refuses_without_selecting_a_source(self):
        for field, value in (("value", "41"), ("unit", "EUR")):
            record, filings, sources = self.fixture()
            row = copy.deepcopy(sources["ingest:old"][1])
            row.update(line_id="conflicting-original")
            row[field] = value
            sources["ingest:old"].append(row)
            rows, groups = select_rows(record, filings, sources)
            with self.assertRaisesRegex(ForecastModelValidationError, "conflicting|mixes units"):
                execution_scope(record, rows, groups)

    def test_equal_amount_different_full_dimensions_is_not_an_origin_alias(self):
        record, filings, sources = self.fixture(fixed=True)
        for rows in sources.values():
            for row in list(rows):
                if row["dimensions"]:
                    pure = copy.deepcopy(row)
                    pure["line_id"] += ":different-full-identity"
                    pure["dimensions"].pop("srt:ConsolidationItemsAxis")
                    pure["dimension_count"] = 1
                    pure["value"] = "999"  # A separate family is never summed here.
                    rows.append(pure)
        _, scope, report = self.run_scope(record, filings, sources)
        self.assertEqual(report.status, "available", report.reasons)
        self.assertTrue(all(len(o["statement_line_refs"]) == 1 for e in scope["arithmetic_partitions"] for o in e["origins"]))

    def test_other_source_statement_period_and_extra_dimensions_do_not_repair_missing_peer(self):
        record, filings, sources = self.fixture(fixed=True)
        rows, groups = select_rows(record, filings, sources)
        _, scope = execution_scope(record, rows, groups)
        old = sources["ingest:old"]
        missing = copy.deepcopy(old[2])
        old.remove(old[2])
        # Keep the requested member on another line, with no revenue peer.
        alternative = dict(missing, line_id="different-concept", concept="other")
        old.append(alternative)
        for name, updates in (("source", {"ingest_id": "ingest:elsewhere"}),
                              ("statement", {"statement": "balance"}),
                              ("period", {"period_start": "2023-01-01"}),
                              ("dimensions", {"dimensions": {**missing["dimensions"], "extra:Axis": "extra:Member"}, "dimension_count": 3})):
            row = dict(missing, **updates, line_id="foreign-" + name)
            old.append(row)
        # Driver's exact b source is missing: it cannot borrow alternatives.
        from dalton_core.forecast_source_family_partition import partitions
        with self.assertRaisesRegex(ForecastModelValidationError, "family members differ"):
            partitions(record, [*old, *sources["ingest:new"]], scope["consumed_families"])

    def test_missing_peer_and_nonclosing_historical_total_stay_unavailable(self):
        record, filings, sources = self.fixture()
        self.duplicates(sources)
        sources["ingest:old"][0]["value"] = "101"
        sources["ingest:old"][12]["value"] = "101"
        self.assertEqual(self.run_scope(record, filings, sources)[2].status, "unavailable")

    def test_rehashed_origin_or_group_tamper_is_rejected(self):
        record, filings, sources = self.fixture(fixed=True)
        self.duplicates(sources)
        rows, scope, _ = self.run_scope(record, filings, sources)
        for field in ("origins", "group", "fixed_dimensions"):
            altered = copy.deepcopy(scope)
            entry = altered["arithmetic_partitions"][0]
            if field == "origins":
                entry["origins"][0]["statement_line_refs"].pop()
            elif field == "group":
                entry["group"]["total"] = "999"
            else:
                entry["fixed_dimensions"] = {}
            entry["content_hash"] = content_hash({k: v for k, v in entry.items() if k != "content_hash"})
            altered["content_hash"] = content_hash({k: v for k, v in altered.items() if k != "content_hash"})
            with self.assertRaisesRegex(ForecastModelValidationError, "do not replay"):
                evaluate_execution(record, statement_rows=rows, scope=altered)

    def test_v2_frozen_scope_and_duplicate_refusal_are_unchanged(self):
        record, filings, sources = self.fixture()
        rows, groups = select_rows(record, filings, sources)
        def old_scope(*args, **kwargs):
            return execution_scope(*args, **kwargs, rule_ref=LEGACY_RULE_REF)
        with patch("dalton_core.forecast_execution_statement_scope.execution_scope", side_effect=old_scope):
            kept, old = statement_scope(record, rows, source_groups=groups)
        replay, scope = statement_scope(record, rows, source_groups=groups, frozen_scope=old)
        self.assertEqual((replay, scope), (kept, old))
        self.assertEqual(scope["rule_ref"], LEGACY_RULE_REF)
        self.assertEqual(evaluate_execution(record, statement_rows=rows, scope=old), legacy.evaluate_execution(record, statement_rows=rows, scope=old))
        self.duplicates(sources)
        rows, groups = select_rows(record, filings, sources)
        kept, old = legacy.execution_scope(record, rows, groups)
        with self.assertRaisesRegex(ForecastModelValidationError, "own consolidated partition"):
            evaluate_execution(record, statement_rows=kept, scope=old)

    def test_v2_published_model_keeps_its_durable_proof_and_hash(self):
        from tests.test_forecast_execution_statement_scope import DurableExecutionScopeTests
        from dalton_core.model_forecast_driver import ForecastModelAuthority
        case = DurableExecutionScopeTests("runTest").fixture()
        self.addCleanup(case.doCleanups)
        def old_scope(*args, **kwargs):
            return execution_scope(*args, **kwargs, rule_ref=LEGACY_RULE_REF)
        with patch("dalton_core.forecast_execution_statement_scope.execution_scope", side_effect=old_scope):
            result = case.child()
        authority = ForecastModelAuthority(case.store)
        proof = authority.filing_proof(result["model_version_ref"])
        self.assertEqual(proof["model_statement_scope"]["rule_ref"], LEGACY_RULE_REF)
        self.assertEqual(authority._filing_proof(result["model_version_ref"], require_replay=True), proof)
