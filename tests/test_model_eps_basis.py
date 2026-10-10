"""Synthetic filed EPS identities; no real company data or transport."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dalton_core.company_financial_statement_structure import (
    forecast_structure_binding, validate_financial_statement_structure,
)
from dalton_core.model_eps_basis import model_eps_basis, read_model_eps_basis
from dalton_core.model_forecast_driver import (
    ForecastModelAuthority, build_structured_forecast_model,
)
from dalton_core.store import DaltonStore, content_hash
from tests.test_financial_structure_forecast_consumer import (
    company_spec, financial_inputs, forecastable_proposal,
)


def statutory_body():
    inputs = financial_inputs()
    replacements = {"eps": "us-gaap:EarningsPerShareDiluted",
                    "shares": "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding"}
    for line in inputs["filed_lines"]:
        line["concept"] = replacements.get(line["concept"], line["concept"])
    candidate = forecastable_proposal(inputs)
    for line in candidate["lines"]:
        line["concept"] = replacements.get(line.get("concept"), line.get("concept"))
    for formula in candidate["formulas"]:
        formula["tie_out_concept"] = replacements.get(formula.get("tie_out_concept"), formula.get("tie_out_concept"))
    structure, replay = validate_financial_statement_structure(candidate, company_spec(), inputs)
    binding = forecast_structure_binding(structure, replay, inputs)
    return build_structured_forecast_model(company_spec(), inputs, structure=structure,
                                           replay=replay, binding=binding)


class ModelEpsBasisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DaltonStore(str(Path(self.temp.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.authority = ForecastModelAuthority(self.store)
        self.record = self.authority.publish(statutory_body())

    def test_complete_statutory_tie_proves_gaap_and_binds_exact_model(self):
        outcome = model_eps_basis(self.record)
        self.assertEqual(outcome["status"], "available", outcome)
        self.assertEqual(outcome["basis"], {"kind": "gaap"})
        proof = outcome["proof"]
        self.assertEqual(proof["model_version_ref"], self.record["id"])
        self.assertEqual(proof["model_content_hash"], self.record["content_hash"])
        self.assertEqual(proof["filed_eps_concept"], "us-gaap:EarningsPerShareDiluted")
        self.assertGreater(len(proof["tested_periods"]), 0)
        self.assertEqual(proof["content_hash"], content_hash({k: v for k, v in proof.items() if k != "content_hash"}))

    def test_strong_reader_has_no_authority_constructor_or_writes(self):
        before = self.store.connection.total_changes
        with patch.object(ForecastModelAuthority, "__init__", side_effect=AssertionError("writer constructed")):
            outcome = read_model_eps_basis(self.store.connection, self.record["id"],
                                            company_ref=self.record["company_ref"])
        self.assertEqual(outcome["basis"], {"kind": "gaap"}, outcome)
        self.assertEqual(self.store.connection.total_changes, before)

    def test_wrong_company_and_missing_version_are_unknown(self):
        self.assertIsNone(read_model_eps_basis(self.store.connection, self.record["id"], company_ref="company:peer")["basis"])
        self.assertIsNone(read_model_eps_basis(self.store.connection, "forecast-model-version:missing:1", company_ref=self.record["company_ref"])["basis"])
        self.assertIsNone(read_model_eps_basis(None, None, company_ref=self.record["company_ref"])["basis"])

    def test_changed_model_hash_or_formula_cannot_create_basis(self):
        record = copy.deepcopy(self.record)
        record["results"][-1]["formula"] = "{}"
        self.assertIsNone(model_eps_basis(record)["basis"])

    def _semantic_mutation(self, mutate):
        record = copy.deepcopy(self.record)
        mutate(record)
        # Audit the definition guard independently of the hash guard, so merely
        # copying a GAAP target name is not the positive test's mirror image.
        with patch("dalton_core.model_forecast_driver.validate_forecast_model", return_value=record):
            return model_eps_basis(record)

    def test_eps_label_or_income_over_shares_without_filed_tie_is_unknown(self):
        outcome = self._semantic_mutation(lambda r: next(f for f in r["financial_statement_structure"]["formulas"] if f["output_ref"] == "eps").update(tie_out_concept="company:adjusted-diluted-eps"))
        self.assertIsNone(outcome["basis"])

    def test_basic_shares_or_core_numerator_are_not_promoted_to_gaap(self):
        for ref, change in (("shares", {"concept": "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic"}),
                            ("eps-numerator", {"role": "core_net_income"})):
            with self.subTest(ref=ref):
                outcome = self._semantic_mutation(lambda r: next(l for l in r["financial_statement_structure"]["lines"] if l["ref"] == ref).update(change))
                self.assertIsNone(outcome["basis"])

    def test_missing_historical_proof_and_changed_projected_formula_are_unknown(self):
        for mutate in (
            lambda r: next(f for f in r["financial_statement_structure_replay"]["formulas"] if f["output_ref"] == "eps").update(tested_periods=[]),
            lambda r: next(f for f in r["financial_statement_structure_replay"]["formulas"] if f["output_ref"] == "eps").update(status="unavailable"),
            lambda r: next(f for f in r["results"] if f["role"] == "diluted_eps").update(formula=json.dumps({"operator": "divide", "numerator_ref": "core", "denominator_ref": "shares"})),
        ):
            with self.subTest(mutate=mutate):
                self.assertIsNone(self._semantic_mutation(mutate)["basis"])

    def test_legacy_untyped_model_stays_unknown(self):
        self.assertIsNone(model_eps_basis({"results": [{"role": "diluted_eps", "label": "GAAP EPS"}]})["basis"])

    def test_material_pack_emits_proof_only_for_strong_read_definition(self):
        from dalton_core.material_pack import our_model_years
        with patch("dalton_core.material_pack._model_record", return_value=(self.record, {
                "calendar_binding": {"fiscal_year_end_month": 12},
                "periods": [{"label": "FY2026E", "kind": "forecast", "forecast_eps": {"status": "computed", "value": "4.25"}}]})):
            rows = our_model_years(self.temp.name, self.record["company_ref"], {"consensus": {"periods": []}}, version_ref=self.record["id"])
        self.assertEqual(rows[0]["eps"]["eps_basis"], {"kind": "gaap"})
        self.assertEqual(rows[0]["eps"]["eps_basis_proof"]["model_version_ref"], self.record["id"])


if __name__ == "__main__":
    unittest.main()
