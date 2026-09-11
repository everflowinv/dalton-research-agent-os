from __future__ import annotations

import copy
import unittest

from dalton_core.company_financial_statement_structure import (
    FinancialStatementStructureError,
    aggregate_fiscal_year,
    annual_diluted_eps,
    financial_input_authority,
    forecast_structure_binding,
    validate_financial_statement_structure,
)


ACCESSION = "0000000001-26-000001"
QUARTERS = (
    ("2025-01-01", "2025-03-31"),
    ("2025-04-01", "2025-06-30"),
    ("2025-07-01", "2025-09-30"),
    ("2025-10-01", "2025-12-31"),
)


def input_line(concept, values, *, unit="usd", statement="income"):
    return {
        "concept": concept, "statement": statement, "label": concept,
        "status": "filed", "drawn_on_by": [], "is_split": False,
        "period_basis": "duration", "gaps": [], "derived_count": 0,
        "cells": {
            end: {"period_start": start, "value": str(value), "unit": unit,
                  "basis": "reported", "source_accessions": [ACCESSION]}
            for (start, end), value in zip(QUARTERS, values)
        },
    }


def financial_inputs():
    values = {
        "revenue": (1000, 1100, 1200, 1300),
        "cost": (600, 660, 720, 780),
        "opex": (200, 220, 240, 260),
        "operating": (200, 220, 240, 260),
        "interest_income": (10, 10, 10, 10),
        "interest_expense": (20, 20, 20, 20),
        "pretax": (190, 210, 230, 250),
        "tax": (40, 45, 50, 55),
        "net": (150, 165, 180, 195),
        "nci": (5, 5, 5, 5),
        "parent": (145, 160, 175, 190),
        "eps_numerator": (145, 160, 175, 190),
        "shares": (100, 100, 100, 100),
        "eps": ("1.45", "1.6", "1.75", "1.9"),
    }
    lines = [
        input_line(name, series, unit=("shares" if name == "shares" else
                                      "usd_per_share" if name == "eps" else "usd"))
        for name, series in values.items()
    ]
    return {
        "schema_version": "0.2", "company_ref": "company:test",
        # This deliberately does not enter financial_input_authority.
        "state_hash": "a" * 64, "spec_ref": "company-model-spec:test",
        "periods": [end for _start, end in QUARTERS],
        "filed_lines": lines, "cash_flow_inputs": [], "rows": [],
    }


def company_spec():
    return {
        "spec_id": "company-model-spec:test", "company_ref": "company:test",
        "content_hash": "b" * 64,
        "revenue_anchor_concept": "revenue",
        "expense_lines": [
            {"basis_concept": "cost"}, {"basis_concept": "opex"},
        ],
    }


def filed(ref, role, concept, *, unit="usd", annual="sum_quarters",
          forecast="unavailable", base=None):
    return {
        "ref": ref, "role": role, "label": ref, "kind": "filed",
        "concept": concept, "statement": "income", "unit": unit,
        "period_kind": "duration", "annual_semantics": annual,
        "forecast_method": forecast, "forecast_base_ref": base,
    }


def derived(ref, role, *, unit="usd", annual="sum_quarters"):
    return {
        "ref": ref, "role": role, "label": ref, "kind": "derived",
        "concept": None, "statement": "income", "unit": unit,
        "period_kind": "duration", "annual_semantics": annual,
        "forecast_method": "formula", "forecast_base_ref": None,
    }


def sum_formula(output, terms, tie):
    return {
        "output_ref": output, "operator": "sum",
        "terms": [{"line_ref": ref, "coefficient": str(coefficient)}
                  for ref, coefficient in terms],
        "tie_out_concept": tie, "evidence_refs": [ACCESSION],
    }


def proposal(inputs=None):
    inputs = financial_inputs() if inputs is None else inputs
    lines = [
        filed("revenue", "revenue", "revenue", forecast="quarterly_growth"),
        filed("cost", "cost_of_revenue", "cost", forecast="share_of_line",
              base="revenue"),
        filed("opex", "operating_expense", "opex", forecast="share_of_line",
              base="revenue"),
        derived("operating", "operating_income"),
        filed("interest-income", "interest_income", "interest_income"),
        filed("interest-expense", "interest_expense", "interest_expense"),
        derived("pretax", "pretax_income"),
        filed("tax", "income_tax_expense", "tax"),
        derived("net", "net_income"),
        filed("nci", "noncontrolling_interest", "nci"),
        derived("parent", "parent_net_income"),
        filed("eps-numerator", "diluted_eps_numerator", "eps_numerator"),
        filed("shares", "diluted_weighted_average_shares", "shares",
              unit="shares", annual="direct_annual"),
        derived("eps", "diluted_eps", unit="usd_per_share", annual="annual_ratio"),
    ]
    formulas = [
        sum_formula("operating", (("revenue", 1), ("cost", -1), ("opex", -1)),
                    "operating"),
        sum_formula("pretax", (("operating", 1), ("interest-income", 1),
                               ("interest-expense", -1)), "pretax"),
        sum_formula("net", (("pretax", 1), ("tax", -1)), "net"),
        sum_formula("parent", (("net", 1), ("nci", -1)), "parent"),
        {"output_ref": "eps", "operator": "divide", "numerator_ref": "eps-numerator",
         "denominator_ref": "shares", "tie_out_concept": "eps",
         "evidence_refs": [ACCESSION]},
    ]
    return {
        "schema_version": "0.1", "structure_ref": "statement-structure:test:1",
        "company_ref": "company:test",
        "spec_ref": "company-model-spec:test", "spec_hash": "b" * 64,
        "financial_input_hash": financial_input_authority(inputs)["content_hash"],
        "lines": lines, "formulas": formulas, "created_by": "automation:test",
    }


class FinancialStatementStructureTests(unittest.TestCase):
    def test_company_specific_nonoperating_tax_attribution_and_eps_bridge_ties(self):
        inputs = financial_inputs()
        structure, replay = validate_financial_statement_structure(
            proposal(inputs), company_spec(), inputs)
        self.assertEqual(structure["company_ref"], "company:test")
        self.assertTrue(replay["ready_for_forecast"])
        self.assertEqual(
            {row["output_ref"] for row in replay["formulas"]},
            {"operating", "pretax", "net", "parent", "eps"},
        )
        self.assertTrue(all(len(row["tested_periods"]) == 4
                            for row in replay["formulas"]))

    def test_unrelated_state_hash_does_not_change_financial_authority(self):
        first = financial_inputs()
        second = copy.deepcopy(first)
        second["state_hash"] = "b" * 64
        self.assertEqual(financial_input_authority(first), financial_input_authority(second))

    def test_missing_nonoperating_period_is_not_assumed_zero(self):
        inputs = financial_inputs()
        interest = next(line for line in inputs["filed_lines"]
                        if line["concept"] == "interest_income")
        interest["cells"].pop("2025-12-31")
        candidate = proposal(inputs)
        _structure, replay = validate_financial_statement_structure(
            candidate, company_spec(), inputs)
        pretax = next(row for row in replay["formulas"] if row["output_ref"] == "pretax")
        self.assertEqual(len(pretax["tested_periods"]), 3)

    def test_historical_mismatch_refuses_the_structure(self):
        inputs = financial_inputs()
        target = next(line for line in inputs["filed_lines"] if line["concept"] == "pretax")
        target["cells"]["2025-12-31"]["value"] = "251"
        with self.assertRaisesRegex(FinancialStatementStructureError, "does not tie"):
            validate_financial_statement_structure(proposal(inputs), company_spec(), inputs)

    def test_eps_tie_uses_each_filed_periods_disclosed_precision(self):
        inputs = financial_inputs()
        shares = next(line for line in inputs["filed_lines"]
                      if line["concept"] == "shares")
        eps = next(line for line in inputs["filed_lines"] if line["concept"] == "eps")
        shares["cells"]["2025-03-31"]["value"] = "99"
        eps["cells"]["2025-03-31"]["value"] = "1.46"
        validate_financial_statement_structure(proposal(inputs), company_spec(), inputs)
        eps["cells"]["2025-03-31"]["value"] = "1.45"
        with self.assertRaisesRegex(FinancialStatementStructureError, "does not tie"):
            validate_financial_statement_structure(proposal(inputs), company_spec(), inputs)

    def test_eps_refuses_parent_income_shortcut_wrong_units_and_nonpositive_shares(self):
        inputs = financial_inputs()
        candidate = proposal(inputs)
        eps = next(formula for formula in candidate["formulas"]
                   if formula["output_ref"] == "eps")
        eps["numerator_ref"] = "parent"
        with self.assertRaisesRegex(FinancialStatementStructureError,
                                    "company-specific diluted EPS numerator"):
            validate_financial_statement_structure(candidate, company_spec(), inputs)

        candidate = proposal(inputs)
        line = next(item for item in candidate["lines"] if item["ref"] == "eps")
        line["unit"] = "ratio"
        with self.assertRaisesRegex(FinancialStatementStructureError,
                                    "currency-per-share"):
            validate_financial_statement_structure(candidate, company_spec(), inputs)

        shares = next(line for line in inputs["filed_lines"]
                      if line["concept"] == "shares")
        shares["cells"]["2025-03-31"]["value"] = "-1"
        with self.assertRaisesRegex(FinancialStatementStructureError, "must be positive"):
            validate_financial_statement_structure(proposal(inputs), company_spec(), inputs)

    def test_foreign_concept_and_unheld_note_evidence_are_refused(self):
        inputs = financial_inputs()
        candidate = proposal(inputs)
        candidate["lines"][0]["concept"] = "us-gaap:ImaginedRevenue"
        with self.assertRaisesRegex(FinancialStatementStructureError, "not a filed line"):
            validate_financial_statement_structure(candidate, company_spec(), inputs)
        candidate = proposal(inputs)
        candidate["formulas"][0]["evidence_refs"] = ["note:unheld"]
        with self.assertRaisesRegex(FinancialStatementStructureError, "held statement or note"):
            validate_financial_statement_structure(candidate, company_spec(), inputs)

    def test_formula_cycle_is_refused(self):
        inputs = financial_inputs()
        candidate = proposal(inputs)
        candidate["lines"].extend([
            derived("nonop-a", "nonoperating_income_expense"),
            derived("nonop-b", "nonoperating_income_expense"),
        ])
        candidate["formulas"].extend([
            sum_formula("nonop-a", (("nonop-b", 1),), "interest_income"),
            sum_formula("nonop-b", (("nonop-a", 1),), "interest_income"),
        ])
        with self.assertRaisesRegex(FinancialStatementStructureError, "cycle"):
            validate_financial_statement_structure(candidate, company_spec(), inputs)

    def test_structure_extends_exact_company_spec_and_handoff_binds_replay(self):
        inputs = financial_inputs()
        structure, replay = validate_financial_statement_structure(
            proposal(inputs), company_spec(), inputs)
        binding = forecast_structure_binding(structure, replay, inputs)
        self.assertEqual(binding["spec_ref"], "company-model-spec:test")
        drifted = company_spec()
        drifted["content_hash"] = "c" * 64
        with self.assertRaisesRegex(FinancialStatementStructureError, "spec authority"):
            validate_financial_statement_structure(proposal(inputs), drifted, inputs)
        wrong_anchor = company_spec()
        wrong_anchor["revenue_anchor_concept"] = "interest_income"
        with self.assertRaisesRegex(FinancialStatementStructureError, "exact filed anchor"):
            validate_financial_statement_structure(proposal(inputs), wrong_anchor, inputs)

    def test_note_evidence_must_replay_from_authority(self):
        inputs = financial_inputs()
        candidate = proposal(inputs)
        note = {"ref": "document-version:note", "content_hash": "c" * 64,
                "source_content_hash": "d" * 64}
        candidate["formulas"][0]["evidence_refs"].append(note["ref"])
        with self.assertRaisesRegex(FinancialStatementStructureError, "resolver"):
            validate_financial_statement_structure(
                candidate, company_spec(), inputs, note_evidence=[note])
        validate_financial_statement_structure(
            candidate, company_spec(), inputs, note_evidence=[note],
            note_evidence_resolver=lambda ref: note if ref == note["ref"] else None,
        )

    def test_fiscal_year_amount_needs_four_consecutive_quarters(self):
        cells = [
            {"fiscal_year": "FY2025", "period_kind": "quarter",
             "period_start": start, "period_end": end, "value": str(value), "unit": "USD"}
            for (start, end), value in zip(QUARTERS, (10, 20, 30, 40))
        ]
        for cell in cells:
            cell.update({"calendar": "company:fiscal", "definition_ref": "revenue"})
        total = aggregate_fiscal_year(cells, semantic="sum_quarters", fiscal_year="FY2025")
        self.assertEqual((total["status"], total["value"], total["unit"]),
                         ("computed", "100", "usd"))
        self.assertEqual(
            aggregate_fiscal_year(cells[:-1], semantic="sum_quarters",
                                  fiscal_year="FY2025")["status"],
            "unavailable",
        )

    def test_annual_eps_uses_disclosed_numerator_and_direct_annual_weighted_shares(self):
        income = [
            {"fiscal_year": "FY2025", "period_kind": "quarter",
             "period_start": start, "period_end": end, "value": value, "unit": "hkd",
             "calendar": "company:fiscal", "definition_ref": "diluted-eps-numerator"}
            for (start, end), value in zip(QUARTERS, (100, 110, 120, 130))
        ]
        quarterly_shares = [
            {"fiscal_year": "FY2025", "period_kind": "quarter",
             "period_end": end, "value": "100", "unit": "shares"}
            for _start, end in QUARTERS
        ]
        self.assertEqual(annual_diluted_eps(
            diluted_eps_numerator_cells=income,
            diluted_weighted_share_cells=quarterly_shares,
            fiscal_year="FY2025",
        )["status"], "unavailable")
        annual_shares = [{"fiscal_year": "FY2025", "period_kind": "annual",
                          "period_start": "2025-01-01", "period_end": "2025-12-31",
                          "value": "92",
                          "unit": "shares", "calendar": "company:fiscal",
                          "definition_ref": "diluted-weighted-average-shares"}]
        result = annual_diluted_eps(
            diluted_eps_numerator_cells=income,
            diluted_weighted_share_cells=annual_shares,
            fiscal_year="FY2025",
        )
        self.assertEqual((result["status"], result["value"], result["unit"]),
                         ("computed", "5", "hkd_per_share"))
        annual_shares[0]["period_start"] = "2024-12-01"
        self.assertEqual(annual_diluted_eps(
            diluted_eps_numerator_cells=income,
            diluted_weighted_share_cells=annual_shares,
            fiscal_year="FY2025",
        )["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
