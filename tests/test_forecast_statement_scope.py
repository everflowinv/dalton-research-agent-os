import copy
import unittest
from dalton_core.forecast_statement_scope import statement_scope
from dalton_core.model_forecast_driver import ForecastModelValidationError


class StatementScopeTests(unittest.TestCase):
    def fixture(self):
        ends = ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]
        axis = "us-gaap:StatementBusinessSegmentsAxis"
        declarations = [{"ref": member, "basis_concept": "revenue", "basis_identity": {
            "statement": "income", "dimensions": {axis: member}}} for member in ("new-east", "new-west")]
        record = {"execution_contract": {"specification": {"revenue_drivers": declarations,
            "driver_formulas": [{"output_ref": "revenue", "operator": "sum", "driver_refs": ["new-east", "new-west"]}]}},
            "drivers": [{"ref": "economic:" + d["ref"], "history": [
                {"period_end": end, "accessions": ["filing:1"]} for end in ends]} for d in declarations]}
        rows = []
        for end in ends:
            rows.append({"line_id": "total:" + end, "concept": "revenue", "statement": "income", "dimensions": {},
                         "dimension_axis": None, "period_end": end, "value": "100", "unit": "usd"})
            for member in ("new-east", "new-west"):
                rows.append({"line_id": member + end, "concept": "revenue", "statement": "income", "dimensions": {axis: member},
                    "dimension_axis": axis, "dimension_member": member, "period_start": "2025-01-01", "period_end": end,
                    "value": "50", "unit": "usd", "statement_context_ref": "context:complete:1"})
        # An older, different complete industry partition uses the same axis.
        for member in ("old-software", "old-services"):
            rows.append({"line_id": member, "concept": "revenue", "statement": "income", "dimensions": {axis: member},
                "dimension_axis": axis, "dimension_member": member, "period_start": "2025-01-01", "period_end": ends[0], "value": "50", "unit": "usd"})
        return record, rows

    def test_explicit_membership_keeps_old_parallel_partition_as_source_data(self):
        record, rows = self.fixture()
        scoped, manifest = statement_scope(record, rows)
        self.assertEqual(len(rows), 14)
        self.assertEqual(len(scoped), 12)
        self.assertEqual(manifest["partitions"][0]["members"], ["new-east", "new-west"])
        self.assertEqual(manifest["retained_source_rows"], 14)
        self.assertTrue(manifest["partitions"][0]["scope_ref"].startswith("filed-partition:"))

    def test_missing_member_in_consumed_period_fails_even_if_other_amount_closes(self):
        record, rows = self.fixture()
        rows = [r for r in rows if r["line_id"] != "new-east2025-06-30"]
        rows[0]["value"] = "50"
        with self.assertRaisesRegex(ForecastModelValidationError, "missing a declared member"):
            statement_scope(record, rows)

    def test_undeclared_current_member_cannot_be_hidden_by_a_passing_subset(self):
        record, rows = self.fixture()
        extra = copy.deepcopy(rows[-3])
        extra.update(line_id="hidden-current", dimensions={extra["dimension_axis"]: "new-central"}, dimension_member="new-central", value="0")
        rows.append(extra)
        with self.assertRaisesRegex(ForecastModelValidationError, "every current filed member"):
            statement_scope(record, rows)

if __name__ == "__main__":
    unittest.main()
