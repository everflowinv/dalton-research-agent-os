"""Executable driver identity choices and atomic source revenue projections."""
from __future__ import annotations

import copy
import unittest

from dalton_core.company_model_identity_catalog import (
    IDENTITY_CATALOG_REF, cell_identity_ref, resolve_identity,
)
from dalton_core.company_model_spec import (
    TASK_HASH, CompanyModelSpecError, assessment_compatibility_contract,
    build_prompt, spec_from_response,
)
from dalton_core.company_model_state import build_company_model_state


def cell(**overrides):
    result = {
        "concept": "us-gaap:Revenues", "statement": "income", "status": "filed",
        "dimensions": {"segment:Axis": "segment:Member", "scope:Axis": "scope:Operating"},
        "statement_context_ref": "statement-context:" + "a" * 32,
        "filing_content_hash": "b" * 64, "line_content_hash": "c" * 64,
        "accession": "0000000001-26-000001", "value": "10", "unit": "USD",
    }
    return {**result, **overrides}


def state(*cells):
    return {"numeric_context": {"identity_catalog_contract": IDENTITY_CATALOG_REF,
                                "cells": list(cells)}}


class DriverIdentityTests(unittest.TestCase):
    def test_exact_ref_binds_all_axes_in_actual_spec_parser(self):
        from tests import test_company_model_spec as fixtures
        source = cell()
        held = {**copy.deepcopy(fixtures.STATE), **state(source)}
        answer = fixtures._spec()
        answer["revenue_drivers"][1]["basis_identity"] = {
            "statement": "income", "dimensions": {"catalog_ref": cell_identity_ref(source)},
        }
        spec = spec_from_response(held, answer, decided_by=fixtures.DECIDED_BY)
        self.assertEqual(spec["revenue_drivers"][1]["basis_identity"],
                         {"statement": "income", "dimensions": source["dimensions"]})
        self.assertNotIn("catalog_ref", spec["revenue_drivers"][1]["basis_identity"]["dimensions"])

    def test_foreign_source_stale_hash_wrong_concept_and_statement_refused(self):
        source = cell()
        ref = cell_identity_ref(source)
        identity = {"statement": "income", "dimensions": {"catalog_ref": ref}}
        for changed in (
            cell(statement_context_ref="statement-context:" + "d" * 32),
            cell(line_content_hash="d" * 64), cell(accession="0000000001-26-000002"),
        ):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(ValueError, "catalog ref"):
                    resolve_identity(state(changed), source["concept"], identity)
        with self.assertRaises(ValueError):
            resolve_identity(state(source), "us-gaap:OtherRevenue", identity)
        with self.assertRaises(ValueError):
            resolve_identity(state(source), source["concept"], {**identity, "statement": "cash"})

    def test_legacy_direct_identity_and_assessment_signature_unchanged(self):
        source = cell()
        identity = {"statement": "income", "dimensions": source["dimensions"]}
        legacy = {"numeric_context": {"cells": [source]}}
        self.assertEqual(resolve_identity(legacy, source["concept"], identity), identity)
        partial = {"statement": "income", "dimensions": {"segment:Axis": "segment:Member"}}
        with self.assertRaisesRegex(ValueError, "admitted exact numeric context"):
            resolve_identity(state(source), source["concept"], partial)
        with self.assertRaisesRegex(ValueError, "catalog is not admitted"):
            resolve_identity(legacy, source["concept"], {
                "statement": "income", "dimensions": {"catalog_ref": cell_identity_ref(source)},
            })
        self.assertEqual(TASK_HASH, "608338bcd6cf70cbe6e012acd5f579b99d34f9d990d5bc846c4059a198eda9af")
        self.assertEqual(assessment_compatibility_contract()["current_task_hash"], TASK_HASH)
        from dalton_core.company_model_spec import _numeric_period_cell_line
        self.assertNotIn("catalog_ref", _numeric_period_cell_line(source))

    def test_ambiguous_ref_and_mixed_catalog_dimension_refused(self):
        source = cell(status="ambiguous")
        with self.assertRaises(ValueError):
            resolve_identity(state(source), source["concept"], {
                "statement": "income", "dimensions": {"catalog_ref": cell_identity_ref(source)},
            })
        with self.assertRaises(ValueError):
            resolve_identity(state(cell()), source["concept"], {
                "statement": "income", "dimensions": {
                    "catalog_ref": cell_identity_ref(cell()), "segment:Axis": "segment:Member",
                },
            })


class RevenueGroupTests(unittest.TestCase):
    def setUp(self):
        from tests import test_company_model_state as fixtures
        self.fixtures = fixtures
        self.fixture = fixtures.CompanyModelStateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def ingest_group(self):
        line = self.fixtures._line
        rows = [line("us-gaap:Revenues", value="100")]
        rows.extend(line(f"example:Incidental{n:03d}", statement="cash") for n in range(110))
        rows.extend(line("us-gaap:Revenues", value=str(n + 1), is_breakdown=True,
                         dimension_axis="segment:Axis", dimension_member=f"segment:Member{n}")
                    for n in range(4))
        self.fixture.ingest("0001467373-26-000031", rows)

    def test_revenue_members_survive_small_cell_cap_before_incidental_lines(self):
        self.ingest_group()
        held = build_company_model_state(self.fixture.missions, self.fixtures.ACN,
            numeric_context_policy={"max_periods_per_series": 1, "max_total_cells": 5})
        cells = held["numeric_context"]["cells"]
        self.assertEqual(len(cells), 5)
        self.assertEqual({c["concept"] for c in cells}, {"us-gaap:Revenues"})
        self.assertEqual(len({c["dimension_member"] for c in cells}), 5)

    def test_byte_cap_never_keeps_only_some_members(self):
        self.ingest_group()
        complete = build_company_model_state(self.fixture.missions, self.fixtures.ACN)
        bound = complete["numeric_context"]["base_prompt_bytes"] + 500
        held = build_company_model_state(self.fixture.missions, self.fixtures.ACN,
                                         prompt_byte_limit=bound)
        revenue = [c for c in held["numeric_context"]["cells"] if c["concept"] == "us-gaap:Revenues"]
        self.assertEqual(revenue, [])
        self.assertLessEqual(len(build_prompt(held).encode()), bound)
        self.assertEqual(held["numeric_context"]["prompt_bytes"], len(build_prompt(held).encode()))

    def test_income_chain_is_not_starved_by_all_historical_revenue(self):
        line = self.fixtures._line
        rows = []
        for start, end in (("2026-04-01", "2026-06-30"), ("2026-01-01", "2026-03-31")):
            rows.extend([
                line("us-gaap:Revenues", period_start=start, period_end=end, value="100"),
                line("us-gaap:Revenues", period_start=start, period_end=end,
                     is_breakdown=True, dimension_axis="segment:Axis", dimension_member="segment:Member", value="100"),
                line("example:FiledExpenseTotal", period_start=start, period_end=end, value="80"),
                line("us-gaap:OperatingIncomeLoss", period_start=start, period_end=end, value="20"),
            ])
        self.fixture.ingest("0001467373-26-000031", rows)
        held = build_company_model_state(self.fixture.missions, self.fixtures.ACN,
            numeric_context_policy={"max_periods_per_series": 2, "max_total_cells": 4})
        cells = held["numeric_context"]["cells"]
        self.assertEqual({c["period_end"] for c in cells}, {"2026-06-30"})
        self.assertEqual({c["concept"] for c in cells},
                         {"us-gaap:Revenues", "example:FiledExpenseTotal", "us-gaap:OperatingIncomeLoss"})

    def test_oversize_group_is_omitted_without_changing_values_or_cap(self):
        self.ingest_group()
        held = build_company_model_state(self.fixture.missions, self.fixtures.ACN,
            numeric_context_policy={"max_periods_per_series": 1, "max_total_cells": 4})
        self.assertFalse(any(c["concept"] == "us-gaap:Revenues" for c in held["numeric_context"]["cells"]))
        self.assertEqual(held["numeric_context"]["included_cells"], 4)
        self.assertTrue(held["numeric_context"]["truncated"])

    def test_different_sources_windows_and_units_do_not_form_one_group(self):
        from dalton_core.company_model_identity_catalog import coherent_revenue_groups
        source = cell(period_start="2026-04-01", period_end="2026-06-30")
        groups = [[source], [cell(**{**source, "accession": "other"})],
                  [cell(**{**source, "period_start": "2026-01-01"})],
                  [cell(**{**source, "unit": "EUR"})],
                  [cell(**{**source, "statement_context_ref": "other"})]]
        grouped = coherent_revenue_groups(groups)
        self.assertEqual(len(grouped), 5)
        self.assertTrue(all(len(g) == 1 for g in grouped))


if __name__ == "__main__":
    unittest.main()
