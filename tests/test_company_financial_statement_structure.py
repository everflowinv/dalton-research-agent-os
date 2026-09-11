from __future__ import annotations

import copy
from decimal import Decimal
import unittest

from dalton_core.company_financial_statement_structure import (
    ANNUAL_SCHEMA_VERSION,
    LEGACY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    FinancialStatementStructureError,
    aggregate_fiscal_year,
    annual_diluted_eps,
    day_weighted_annual_shares,
    financial_input_authority,
    forecast_structure_binding,
    materialize_financial_statement_structure,
    validate_financial_statement_structure,
    validate_structure_proposal,
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


def presentation_state(inputs=None):
    inputs = financial_inputs() if inputs is None else inputs
    return {
        "company_ref": "company:test",
        "filings": [{"accession": ACCESSION, "form": "10-K",
                     "report_date": "2025-12-31",
                     "ingest_id": "statement-ingest:test",
                     "content_hash": "d" * 64}],
        "statements": {"income": [
            {"concept": line["concept"], "label": line["label"],
             "level": 0, "parent_concept": None, "is_breakdown": False,
             "dimension_axis": None, "unit": next(iter(line["cells"].values()))["unit"],
             "period_kind": line["period_basis"]}
            for line in inputs["filed_lines"]
        ]},
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


def typed_note(*, kind="annual", periods=None):
    return {
        "schema_version": "financial-note-evidence-binding-0.1",
        "ref": "financial-note-evidence:acn-eps",
        "content_hash": "c" * 64,
        "target_ref": "financial_note:diluted_eps_numerator:0.1",
        "company_ref": "company:test",
        "statement_ingest_ref": "statement-ingest:test",
        "statement_filing_hash": "d" * 64,
        "accession": ACCESSION,
        "form": "10-K",
        "applicability_kind": kind,
        "periods": periods or [{
            "period_start": "2024-01-01", "period_end": "2024-12-31",
        }],
    }


def note_backed_eps_inputs_and_proposal():
    inputs = financial_inputs()
    annual = ("2024-01-01", "2024-12-31")
    values = {
        "parent": ("7678433000", "usd"),
        "canada-nci": ("7240000", "usd"),
        "other-nci": ("146727000", "usd"),
        "shares": ("632435108", "shares"),
        "eps": ("12.15", "usd_per_share"),
    }
    inputs["filed_lines"].extend([
        input_line("canada-nci", (), unit="usd"),
        input_line("other-nci", (), unit="usd"),
    ])
    for concept, (value, unit) in values.items():
        line = next(item for item in inputs["filed_lines"] if item["concept"] == concept)
        annual_fact = {
            "period_start": annual[0], "period_end": annual[1],
            "period_kind": "cumulative", "value": value, "unit": unit,
            "source_accessions": [ACCESSION], "source_forms": ["10-K"],
        }
        line["duration_facts"] = [annual_fact]
        line["cells"][annual[1]] = {
            key: item for key, item in annual_fact.items()
            if key not in {"period_end", "period_kind", "source_forms"}
        }
    candidate = proposal(inputs)
    candidate["schema_version"] = SCHEMA_VERSION
    for line in candidate["lines"]:
        line["annual_forecast_method"] = (
            "unavailable" if line["role"] == "diluted_weighted_average_shares" else None
        )
    parent = next(line for line in candidate["lines"] if line["ref"] == "parent")
    parent.update({
        "kind": "filed", "concept": "parent", "forecast_method": "unavailable",
    })
    candidate["formulas"] = [
        formula for formula in candidate["formulas"]
        if formula["output_ref"] != "parent"
    ]
    numerator = next(line for line in candidate["lines"] if line["ref"] == "eps-numerator")
    numerator.update({"kind": "derived", "concept": None, "forecast_method": "formula"})
    candidate["lines"].extend([
        {**filed("canada-nci", "dilutive_securities_adjustment", "canada-nci"),
         "annual_forecast_method": None},
        {**filed("other-nci", "dilutive_securities_adjustment", "other-nci"),
         "annual_forecast_method": None},
    ])
    candidate["formulas"].insert(-1, {
        "output_ref": "eps-numerator", "operator": "sum",
        "terms": [
            {"line_ref": "parent", "coefficient": "1"},
            {"line_ref": "canada-nci", "coefficient": "1"},
        ],
        "tie_out_concept": None,
        "evidence_refs": [ACCESSION, "financial-note-evidence:acn-eps"],
    })
    return inputs, candidate


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
    def test_note_backed_annual_eps_numerator_ties_indirectly_without_quarter_readiness(self):
        inputs, candidate = note_backed_eps_inputs_and_proposal()
        note = typed_note()
        structure, replay = validate_financial_statement_structure(
            candidate, company_spec(), inputs, note_evidence=[note],
            note_evidence_resolver=lambda ref: note if ref == note["ref"] else None,
        )
        self.assertEqual(structure["schema_version"], SCHEMA_VERSION)
        self.assertEqual(structure["authority_ref"],
                         "company-financial-statement-structure:0.3")
        annual = replay["note_formula_periods"][0]
        self.assertEqual(annual["status"], "validated")
        self.assertEqual(annual["periods"][0]["value"], "7685673000")
        self.assertEqual(annual["periods"][0]["filed_diluted_eps"], "12.15")
        self.assertFalse(replay["ready_for_forecast"])
        numerator = next(item for item in replay["formulas"]
                         if item["output_ref"] == "eps-numerator")
        self.assertEqual(numerator["status"], "unavailable")

        definition = validate_structure_proposal(
            {key: candidate[key] for key in ("schema_version", "lines", "formulas")},
            presentation_state(inputs),
            revenue_anchor_concept="revenue",
            expense_lines=company_spec()["expense_lines"],
            note_evidence=[note],
            note_evidence_resolver=lambda _ref: note,
        )
        self.assertEqual(definition["note_evidence"], [note])
        spec = {
            **company_spec(), "financial_statement_structure": definition,
            "decided_by": "automation:test",
        }
        rematerialized, replayed = materialize_financial_statement_structure(
            spec, inputs, note_evidence_resolver=lambda _ref: note,
        )
        self.assertEqual(rematerialized["note_evidence"], [note])
        self.assertEqual(replayed["note_formula_periods"], replay["note_formula_periods"])

    def test_note_backed_numerator_refuses_other_nci_sign_and_missing_period(self):
        inputs, candidate = note_backed_eps_inputs_and_proposal()
        note = typed_note()
        numerator = next(item for item in candidate["formulas"]
                         if item["output_ref"] == "eps-numerator")
        numerator["terms"][1]["line_ref"] = "other-nci"
        with self.assertRaisesRegex(FinancialStatementStructureError,
                                    "does not tie through filed EPS"):
            validate_financial_statement_structure(
                candidate, company_spec(), inputs, note_evidence=[note],
                note_evidence_resolver=lambda _ref: note,
            )
        numerator["terms"][1].update({"line_ref": "canada-nci", "coefficient": "-1"})
        with self.assertRaisesRegex(FinancialStatementStructureError,
                                    "does not tie through filed EPS"):
            validate_financial_statement_structure(
                candidate, company_spec(), inputs, note_evidence=[note],
                note_evidence_resolver=lambda _ref: note,
            )

        inputs, candidate = note_backed_eps_inputs_and_proposal()
        adjustment = next(item for item in inputs["filed_lines"]
                          if item["concept"] == "canada-nci")
        prior = {
            "period_start": "2023-01-01", "period_end": "2023-12-31",
            "period_kind": "cumulative", "value": "7000000", "unit": "usd",
            "source_accessions": [ACCESSION], "source_forms": ["10-K"],
        }
        adjustment["duration_facts"] = [prior]
        adjustment["cells"] = {"2023-12-31": {
            key: value for key, value in prior.items()
            if key not in {"period_end", "period_kind", "source_forms"}
        }}
        candidate["financial_input_hash"] = financial_input_authority(inputs)["content_hash"]
        # The missing amount is honest unavailability, never an implicit zero.
        _structure, replay = validate_financial_statement_structure(
            candidate, company_spec(), inputs, note_evidence=[note],
            note_evidence_resolver=lambda _ref: note,
        )
        self.assertEqual(replay["note_formula_periods"][0]["status"], "unavailable")

    def test_typed_note_binding_refuses_foreign_company_period_and_resolver_drift(self):
        inputs, candidate = note_backed_eps_inputs_and_proposal()
        note = typed_note()
        for changed, error in (
            ({**note, "company_ref": "company:foreign"}, "company differs"),
            ({**note, "applicability_kind": "quarter"}, "applicability kind"),
        ):
            with self.assertRaisesRegex(FinancialStatementStructureError, error):
                validate_financial_statement_structure(
                    candidate, company_spec(), inputs, note_evidence=[changed],
                    note_evidence_resolver=lambda _ref, item=changed: item,
                )
        with self.assertRaisesRegex(FinancialStatementStructureError,
                                    "authority differs"):
            validate_financial_statement_structure(
                candidate, company_spec(), inputs, note_evidence=[note],
                note_evidence_resolver=lambda _ref: {**note, "content_hash": "e" * 64},
            )
        state = presentation_state(inputs)
        state["filings"][0]["content_hash"] = "e" * 64
        with self.assertRaisesRegex(FinancialStatementStructureError,
                                    "statement filing differs"):
            validate_structure_proposal(
                {key: candidate[key] for key in ("schema_version", "lines", "formulas")},
                state, revenue_anchor_concept="revenue",
                expense_lines=company_spec()["expense_lines"],
                note_evidence=[note], note_evidence_resolver=lambda _ref: note,
            )

    def test_structure_0_2_replays_with_its_exact_prior_shape(self):
        inputs = financial_inputs()
        candidate = proposal(inputs)
        candidate["schema_version"] = ANNUAL_SCHEMA_VERSION
        for line in candidate["lines"]:
            line["annual_forecast_method"] = (
                "unavailable" if line["role"] == "diluted_weighted_average_shares" else None
            )
        structure, replay = validate_financial_statement_structure(
            candidate, company_spec(), inputs,
        )
        self.assertEqual(structure["authority_ref"],
                         "company-financial-statement-structure:0.2")
        self.assertEqual(replay["schema_version"],
                         "financial-statement-structure-replay-0.2")
        self.assertNotIn("note_formula_periods", replay)
        self.assertEqual(structure["content_hash"],
                         "a4c044f8da824a0eb4439fc1bd64c14870e8cb45d254a344e3fcd027df83aa03")
        from dalton_core.store import content_hash
        self.assertEqual(content_hash(replay),
                         "29f9fa2947b59f00f4c597557292da3bddc009f57f4d3c65c83bb7ce094a00b2")

    def test_versioned_annual_share_method_needs_a_historical_direct_tie(self):
        inputs = financial_inputs()
        inputs["schema_version"] = "0.3"
        shares = next(line for line in inputs["filed_lines"]
                      if line["concept"] == "shares")
        shares["duration_facts"] = [
            {"period_start": cell["period_start"], "period_end": end,
             "period_kind": "quarter", "value": cell["value"],
             "unit": "shares", "source_accessions": [ACCESSION],
             "source_forms": ["10-Q"]}
            for end, cell in shares["cells"].items()
        ] + [{
            "period_start": "2025-01-01", "period_end": "2025-12-31",
            "period_kind": "cumulative", "value": "100", "unit": "shares",
            "source_accessions": [ACCESSION], "source_forms": ["10-K"],
        }]
        for line in inputs["filed_lines"]:
            line.setdefault("duration_facts", [])
            line.setdefault("ambiguous_periods", [])
        candidate = proposal(inputs)
        candidate["schema_version"] = SCHEMA_VERSION
        for line in candidate["lines"]:
            line["annual_forecast_method"] = None
            if line["role"] == "diluted_weighted_average_shares":
                line["forecast_method"] = "quarterly_growth"
                line["annual_forecast_method"] = "day_weighted_quarters"
        structure, replay = validate_financial_statement_structure(
            candidate, company_spec(), inputs)
        self.assertEqual(structure["schema_version"], SCHEMA_VERSION)
        share_replay = next(item for item in replay["forecast_methods"]
                            if item["line_ref"] == "shares")
        self.assertEqual((share_replay["annual_method"], share_replay["annual_status"]),
                         ("day_weighted_quarters", "validated"))
        self.assertTrue(replay["ready_for_forecast"])

        drifted = copy.deepcopy(inputs)
        drifted_shares = next(line for line in drifted["filed_lines"]
                              if line["concept"] == "shares")
        drifted_shares["duration_facts"][-1]["value"] = "99"
        candidate = proposal(drifted)
        candidate["schema_version"] = SCHEMA_VERSION
        for line in candidate["lines"]:
            line["annual_forecast_method"] = None
            if line["role"] == "diluted_weighted_average_shares":
                line["forecast_method"] = "quarterly_growth"
                line["annual_forecast_method"] = "day_weighted_quarters"
        _structure, replay = validate_financial_statement_structure(
            candidate, company_spec(), drifted)
        self.assertFalse(replay["ready_for_forecast"])

    def test_legacy_structure_bytes_do_not_gain_annual_method(self):
        structure, _replay = validate_financial_statement_structure(
            proposal(), company_spec(), financial_inputs())
        self.assertEqual(structure["schema_version"], LEGACY_SCHEMA_VERSION)
        self.assertEqual(
            structure["authority_ref"],
            "company-financial-statement-structure:0.1",
        )
        self.assertTrue(all("annual_forecast_method" not in line
                            for line in structure["lines"]))

    def test_day_weighted_shares_use_exact_quarter_days(self):
        quarters = [
            {"period_start": start, "period_end": end, "value": str(value),
             "unit": "shares"}
            for (start, end), value in zip(QUARTERS, (100, 100, 200, 200))
        ]
        result = day_weighted_annual_shares(quarters)
        expected = (
            Decimal(100 * 90 + 100 * 91 + 200 * 92 + 200 * 92)
            / Decimal(365)
        )
        self.assertEqual(Decimal(result["value"]), expected)

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
        methods = {row["line_ref"]: row for row in replay["forecast_methods"]}
        self.assertEqual((methods["revenue"]["status"],
                          len(methods["revenue"]["observations"])),
                         ("validated", 3))
        self.assertEqual((methods["cost"]["base_ref"],
                          len(methods["cost"]["observations"])),
                         ("revenue", 4))
        self.assertEqual(methods["interest-income"]["status"], "unavailable")

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

    def test_company_presented_after_tax_equity_method_enters_net_income_bridge(self):
        inputs = financial_inputs()
        inputs["filed_lines"].append(input_line("equity_method_after_tax", (5, 5, 5, 5)))
        for concept in ("net", "parent"):
            line = next(item for item in inputs["filed_lines"]
                        if item["concept"] == concept)
            for cell in line["cells"].values():
                cell["value"] = str(Decimal(cell["value"]) + Decimal(5))
        candidate = proposal(inputs)
        candidate["lines"].append(filed(
            "equity-method", "company_presented_component",
            "equity_method_after_tax", forecast="unavailable",
        ))
        net = next(formula for formula in candidate["formulas"]
                   if formula["output_ref"] == "net")
        net["terms"].append({"line_ref": "equity-method", "coefficient": "1"})
        structure, replay = validate_financial_statement_structure(
            candidate, company_spec(), inputs)
        self.assertTrue(replay["ready_for_forecast"])
        self.assertEqual(
            next(line for line in structure["lines"]
                 if line["ref"] == "equity-method")["role"],
            "company_presented_component",
        )

    def test_company_component_formula_places_it_before_or_after_operating_income(self):
        for output_ref, adjusted in (
            ("operating", ("operating", "pretax", "net", "parent")),
            ("pretax", ("pretax", "net", "parent")),
        ):
            with self.subTest(output_ref=output_ref):
                inputs = financial_inputs()
                inputs["filed_lines"].append(input_line(
                    "company_other_income_expense", (5, 5, 5, 5)))
                for concept in adjusted:
                    line = next(item for item in inputs["filed_lines"]
                                if item["concept"] == concept)
                    for cell in line["cells"].values():
                        cell["value"] = str(Decimal(cell["value"]) + Decimal(5))
                candidate = proposal(inputs)
                candidate["lines"].append(filed(
                    "company-other", "company_presented_component",
                    "company_other_income_expense", forecast="unavailable",
                ))
                formula = next(item for item in candidate["formulas"]
                               if item["output_ref"] == output_ref)
                formula["terms"].append(
                    {"line_ref": "company-other", "coefficient": "1"})
                structure, replay = validate_financial_statement_structure(
                    candidate, company_spec(), inputs)
                self.assertTrue(replay["ready_for_forecast"])
                held = next(item for item in structure["formulas"]
                            if item["output_ref"] == output_ref)
                self.assertIn("company-other",
                              {term["line_ref"] for term in held["terms"]})

    def test_company_presented_roles_cannot_bypass_filed_or_tied_subtotal_authority(self):
        inputs = financial_inputs()
        candidate = proposal(inputs)
        operating = next(line for line in candidate["lines"]
                         if line["ref"] == "operating")
        operating["role"] = "company_presented_component"
        with self.assertRaisesRegex(FinancialStatementStructureError,
                                    "components must be filed"):
            validate_financial_statement_structure(candidate, company_spec(), inputs)

        candidate = proposal(inputs)
        operating = next(line for line in candidate["lines"]
                         if line["ref"] == "operating")
        operating["role"] = "company_presented_subtotal"
        formula = next(item for item in candidate["formulas"]
                       if item["output_ref"] == "operating")
        formula["tie_out_concept"] = None
        with self.assertRaisesRegex(FinancialStatementStructureError,
                                    "must tie to an exact filed concept"):
            validate_financial_statement_structure(candidate, company_spec(), inputs)

    def test_structure_cannot_claim_readiness_without_a_tied_final_earnings_bridge(self):
        inputs = financial_inputs()
        candidate = proposal(inputs)
        candidate["lines"] = [
            line for line in candidate["lines"] if line["kind"] == "filed"
        ]
        candidate["formulas"] = []
        with self.assertRaisesRegex(FinancialStatementStructureError,
                                    "at least one formula"):
            validate_financial_statement_structure(candidate, company_spec(), inputs)

        candidate = proposal(inputs)
        net = next(line for line in candidate["lines"] if line["ref"] == "net")
        net.update({
            "kind": "filed", "concept": "net",
            "forecast_method": "quarterly_growth",
        })
        candidate["formulas"] = [
            formula for formula in candidate["formulas"]
            if formula["output_ref"] != "net"
        ]
        with self.assertRaisesRegex(FinancialStatementStructureError,
                                    "filed subtotal.*unavailable"):
            validate_financial_statement_structure(candidate, company_spec(), inputs)

        candidate = proposal(inputs)
        next(formula for formula in candidate["formulas"]
             if formula["output_ref"] == "net")["tie_out_concept"] = None
        with self.assertRaisesRegex(FinancialStatementStructureError,
                                    "every derived formula must tie"):
            validate_financial_statement_structure(candidate, company_spec(), inputs)

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

    def test_spec_proposal_materializes_against_each_current_financial_input(self):
        inputs = financial_inputs()
        candidate = proposal(inputs)
        definition = validate_structure_proposal(
            {key: candidate[key] for key in ("schema_version", "lines", "formulas")},
            presentation_state(inputs),
            revenue_anchor_concept="revenue",
            expense_lines=company_spec()["expense_lines"],
        )
        spec = {**company_spec(), "financial_statement_structure": definition,
                "decided_by": "automation:test"}
        structure, replay = materialize_financial_statement_structure(spec, inputs)
        self.assertTrue(replay["ready_for_forecast"])
        self.assertEqual(structure["financial_input_hash"],
                         financial_input_authority(inputs)["content_hash"])
        first_binding = forecast_structure_binding(structure, replay, inputs)

        moved = copy.deepcopy(inputs)
        next(line for line in moved["filed_lines"]
             if line["concept"] == "revenue")["cells"]["2025-12-31"]["value"] = "1400"
        for concept, value in {
            "operating": "360", "pretax": "350", "net": "295", "parent": "290",
        }.items():
            next(line for line in moved["filed_lines"]
                 if line["concept"] == concept)["cells"]["2025-12-31"]["value"] = value
        current, current_replay = materialize_financial_statement_structure(spec, moved)
        current_binding = forecast_structure_binding(current, current_replay, moved)
        self.assertNotEqual(first_binding["financial_input_hash"],
                            current_binding["financial_input_hash"])
        self.assertNotEqual(first_binding["structure_hash"],
                            current_binding["structure_hash"])
        with self.assertRaisesRegex(FinancialStatementStructureError,
                                    "authority is invalid"):
            forecast_structure_binding(structure, replay, moved)

        untied = copy.deepcopy(moved)
        next(line for line in untied["filed_lines"]
             if line["concept"] == "operating")["cells"]["2025-12-31"]["value"] = "361"
        with self.assertRaisesRegex(FinancialStatementStructureError, "does not tie"):
            materialize_financial_statement_structure(spec, untied)

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

    def test_annual_eps_accepts_a_tied_direct_numerator_and_refuses_drift(self):
        common = {
            "fiscal_year": "FY2025", "period_kind": "annual",
            "period_start": "2025-01-01", "period_end": "2025-12-31",
            "calendar": "company:fiscal",
        }
        numerator = [{**common, "value": "460", "unit": "hkd",
                      "definition_ref": "diluted-eps-numerator"}]
        shares = [{**common, "value": "92", "unit": "shares",
                   "definition_ref": "diluted-weighted-average-shares"}]
        eps = [{**common, "value": "5.00", "unit": "hkd_per_share",
                "definition_ref": "diluted-eps"}]
        result = annual_diluted_eps(
            diluted_eps_numerator_cells=numerator,
            diluted_weighted_share_cells=shares,
            diluted_eps_cells=eps,
            fiscal_year="FY2025",
        )
        self.assertEqual((result["status"], result["value"]), ("computed", "5"))
        eps[0]["value"] = "4.99"
        self.assertEqual(annual_diluted_eps(
            diluted_eps_numerator_cells=numerator,
            diluted_weighted_share_cells=shares,
            diluted_eps_cells=eps,
            fiscal_year="FY2025",
        )["status"], "unavailable")

        quarters = [
            {**common, "period_kind": "quarter", "period_start": start,
             "period_end": end, "value": value, "unit": "hkd",
             "definition_ref": "diluted-eps-numerator"}
            for (start, end), value in zip(QUARTERS, (100, 110, 120, 131))
        ]
        self.assertIn("disagrees", annual_diluted_eps(
            diluted_eps_numerator_cells=[*quarters, *numerator],
            diluted_weighted_share_cells=shares,
            fiscal_year="FY2025",
        )["reason"])
        ambiguous = [*numerator, {**numerator[0], "period_start": "2025-01-02"}]
        self.assertIn("ambiguous", annual_diluted_eps(
            diluted_eps_numerator_cells=ambiguous,
            diluted_weighted_share_cells=shares,
            fiscal_year="FY2025",
        )["reason"])


if __name__ == "__main__":
    unittest.main()
