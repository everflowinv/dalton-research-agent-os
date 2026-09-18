"""WP-C1-4: the wiring rules a specification may be asked to reconsider once.

Live refusals, from
``~/Library/Application Support/Dalton/state/dalton-core/company-model-spec-runs/``:

* ``c0716945d8dc26ae15099567`` (2026-09-16T09:33Z) --
  ``financial_statement_structure is invalid: diluted weighted-average shares
  require duration direct_annual shares``
* ``9cd8b287b605b1a0ba15cabc`` (2026-09-16T10:03Z) --
  ``... a filed subtotal may only be actual/tie authority or unavailable;
  forecasted subtotals require an explicit formula``
* ``4121bf4d1fb203c841e04976`` (2026-09-12T03:13Z) --
  ``... a derived line cannot claim a filed concept``
* ``91542e3c26db00b37aa084bb`` (2026-09-13T14:00Z) --
  ``... sum formula roles do not match its company statement output``
* ``cb9bc070f41608b4314963af`` (2026-09-11T07:06Z) --
  ``assessment is longer than 1200 characters``

All of them ``status: "succeeded"``, ``spec_status: "refused"``: a whole
specification thrown away over which authority a line carries.
"""

from __future__ import annotations

import unittest

from dalton_core.company_model_cli import (
    DEFAULT_REPAIR_ATTEMPTS,
    REPAIR_CONTRACT,
    _repair_prompt,
    structured_output_repair_config,
)
from dalton_core.company_model_spec import (
    REPAIRABLE_STRUCTURE_RULES,
    REPAIRABLE_STRUCTURE_RULES_REF,
    CompanyModelSpecError,
    financial_line_gaps,
    structure_error_code,
)


class DefaultAttemptTests(unittest.TestCase):
    """Why the mechanism above never ran.

    Live 2026-09-18: the model-specification lane held durably with
    ``CompanyModelSpecError: financial_statement_structure is invalid: sum
    formula roles do not match its company statement output`` -- a rule that
    has been on the repairable allow-list since 0.1.  The repair is gated on
    ``structured_output_repair.max_attempts``, that key is in no deploy
    artefact in this repository, and its default was zero.  A capability that
    is off in every installation is not a capability.
    """

    def test_a_configuration_that_says_nothing_gets_one_repair(self):
        self.assertEqual(DEFAULT_REPAIR_ATTEMPTS, 1)
        for config in ({}, None, {"model_router_db": "x"}):
            self.assertEqual(structured_output_repair_config(config),
                             {"max_attempts": 1}, config)

    def test_zero_is_still_how_an_operator_turns_it_off(self):
        self.assertEqual(
            structured_output_repair_config(
                {"structured_output_repair": {"max_attempts": 0}}),
            {"max_attempts": 0})


class StructureCodeTests(unittest.TestCase):
    def test_the_eps_numerator_half_of_the_divide_rule_is_eligible(self):
        # 2026-09-18: the denominator half was repairable and the numerator
        # half was a whole refusal, which is the same choice among the same
        # roles.  Both are wiring; neither is arithmetic about the filings.
        for message in (
            "EPS numerator must use the company-specific diluted EPS numerator role",
            "EPS denominator must be diluted weighted-average shares",
        ):
            self.assertEqual(
                structure_error_code(
                    f"financial_statement_structure is invalid: {message}"),
                "structure", message)

    def test_the_repair_prompt_lists_the_rule_the_way_every_lane_does(self):
        # One renderer for all three lanes: the dossier's verifier findings,
        # the gate's output-rubric findings and this validation error are put
        # in front of a model under the same label, in the same numbered form.
        from dalton_core.draft_contract_repair import FINDINGS_LIST_LABEL

        prompt = _repair_prompt("{}", CompanyModelSpecError(
            "financial_statement_structure is invalid: sum formula roles do "
            "not match its company statement output", code="structure"))
        self.assertIn(FINDINGS_LIST_LABEL, prompt)
        self.assertIn("1. financial_statement_structure:", prompt)
        self.assertIn("sum formula roles do not match", prompt)
        # The machine-readable object an auditor replays is still there.
        self.assertIn("VALIDATION_ERROR:", prompt)


    def test_every_live_wiring_refusal_is_eligible(self):
        for message in (
            "diluted weighted-average shares require duration direct_annual shares",
            "a filed subtotal may only be actual/tie authority or unavailable; "
            "forecasted subtotals require an explicit formula",
            "a derived line cannot claim a filed concept",
            "sum formula roles do not match its company statement output",
            "only diluted weighted-average shares may carry annual_forecast_method",
            "EPS denominator must be diluted weighted-average shares",
        ):
            self.assertEqual(structure_error_code(message), "structure", message)

    def test_arithmetic_about_the_filings_is_never_eligible(self):
        # A repair here would be an invitation to make the numbers agree.
        for message in (
            "formula gross-profit does not tie to filed history",
            "structure omits expense concepts selected by the company spec: "
            "ibm:IntellectualPropertyAndCustomDevelopmentIncome",
            "diluted weighted-average shares must be positive",
            "something nobody has written yet",
        ):
            self.assertEqual(structure_error_code(message), "semantic", message)

    def test_the_wrapped_structure_refusal_carries_the_code(self):
        error = CompanyModelSpecError(
            "financial_statement_structure is invalid: a derived line cannot "
            "claim a filed concept",
            code=structure_error_code("a derived line cannot claim a filed concept"))
        self.assertEqual(error.code, "structure")

    def test_the_contract_names_the_codes_and_the_rule_list(self):
        self.assertEqual(REPAIR_CONTRACT["eligible_error_codes"],
                         ["format", "structure", "text_length"])
        self.assertEqual(REPAIR_CONTRACT["eligible_structure_rules_ref"],
                         REPAIRABLE_STRUCTURE_RULES_REF)
        self.assertEqual(REPAIR_CONTRACT["eligible_structure_rules"],
                         list(REPAIRABLE_STRUCTURE_RULES))

    def test_the_structure_repair_prompt_forbids_inventing_a_line(self):
        prompt = _repair_prompt(
            "{}", CompanyModelSpecError(
                "financial_statement_structure is invalid: a derived line "
                "cannot claim a filed concept", code="structure"))
        self.assertIn("one deterministic wiring rule", prompt)
        self.assertIn("rather than inventing a line", prompt)
        self.assertIn("do not change any figure", prompt)
        self.assertIn("a derived line cannot claim a filed concept", prompt)

    def test_the_length_repair_prompt_is_unchanged(self):
        prompt = _repair_prompt(
            "{}", CompanyModelSpecError("assessment is longer than 1200 characters",
                                        code="text_length"))
        self.assertIn("Fix only the reported JSON format", prompt)
        self.assertNotIn("wiring rule", prompt)


class FinancialLineGapTests(unittest.TestCase):
    def test_a_statement_nobody_has_read_is_named_as_a_whole(self):
        gaps = financial_line_gaps({"company_ref": "company:sec-cik:0001467373",
                                    "statements": {"income": [], "balance": [],
                                                   "cash": []}})
        self.assertEqual({item["statement"] for item in gaps},
                         {"income", "balance", "cash"})
        self.assertTrue(all(item["line"] == "(the whole statement)" for item in gaps))
        self.assertIn("company:sec-cik:0001467373", gaps[0]["detail"])
        self.assertIn("一行也没有", gaps[0]["detail"])

    def test_the_missing_share_line_behind_the_live_refusal_is_named(self):
        gaps = financial_line_gaps({
            "company_ref": "company:acn",
            "statements": {
                "income": [{"label": "Revenues", "concept": "us-gaap:Revenues"},
                           {"label": "Operating income", "concept": "x"},
                           {"label": "Net income", "concept": "y"}],
                "balance": [{"label": "Total assets"}, {"label": "Total liabilities"},
                            {"label": "Total equity"}],
                "cash": [{"label": "Net cash from operating activities"},
                         {"label": "Capital expenditure"}],
            }})
        self.assertEqual([item["line"] for item in gaps], ["weighted average"])
        self.assertIn("稀释加权平均股数", gaps[0]["detail"])
        self.assertEqual(gaps[0]["company_ref"], "company:acn")
        self.assertEqual(gaps[0]["statement"], "income")

    def test_a_complete_state_reports_nothing(self):
        gaps = financial_line_gaps({
            "company_ref": "company:acn",
            "statements": {
                "income": [{"label": "Revenues"}, {"label": "Operating income"},
                           {"label": "Net income"},
                           {"label": "Weighted average shares, diluted"}],
                "balance": [{"label": "Total assets"}, {"label": "Total liabilities"},
                            {"label": "Total equity"}],
                "cash": [{"label": "Net cash from operating activities"},
                         {"label": "Capital expenditure"}],
            }})
        self.assertEqual(gaps, [])

    def test_a_concept_name_without_the_label_still_counts(self):
        gaps = financial_line_gaps({
            "company_ref": "company:acn",
            "statements": {
                "income": [
                    {"label": "净收入", "concept": "us-gaap:Revenues"},
                    {"label": "经营利润", "concept": "us-gaap:OperatingIncomeLoss"},
                    {"label": "本期净利", "concept": "us-gaap:NetIncomeLoss"},
                    {"label": "摊薄股数",
                     "concept": "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding"},
                ],
                "balance": [{"concept": "us-gaap:TotalAssets"},
                            {"concept": "us-gaap:TotalLiabilities"},
                            {"concept": "us-gaap:StockholdersEquity"}],
                "cash": [{"concept": "us-gaap:NetCashProvidedByOperatingActivities"},
                         {"concept": "us-gaap:CapitalExpenditure"}],
            }})
        self.assertEqual(gaps, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
