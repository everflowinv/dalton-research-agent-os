import copy
import unittest
from dalton_core.cockpit_v2.charts_api import _model_from_record
from dalton_core.return_attribution import amount_scale
from dalton_core.company_scenario import metric_display_label
from tests.test_forecast_financial_execution import economic_model


class EconomicModelGridTests(unittest.TestCase):
    def test_exact_driver_binding_combines_history_and_forecast_with_own_units(self):
        model, _ = economic_model()
        model.update(id="forecast-model-version:test:1", version=1, created_at="2026-01-01T00:00:00Z")
        payload = _model_from_record({"notes": []}, model, amount_scale)
        for ref, unit in (("economic:volume", "个"), ("economic:price", "美元/个")):
            rows = [row for row in payload["rows"] if row["key"] in (ref, "result:" + ref)]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["unit_label"], unit)
            self.assertEqual(sum(c["status"] == "ok" for c in rows[0]["cells"]), 12)
            self.assertTrue(rows[0]["has_forecast"])
        self.assertNotIn("cost", [row["label"] for row in payload["rows"]])

    def test_different_binding_does_not_merge_just_because_label_or_values_match(self):
        model, _ = economic_model()
        model.update(id="forecast-model-version:test:1", version=1, created_at="2026-01-01T00:00:00Z")
        other = copy.deepcopy(next(r for r in model["results"] if r["ref"] == "result:economic:volume"))
        other.update(ref="result:another-volume", driver_ref="economic:another-volume")
        model["results"].append(other)
        payload = _model_from_record({"notes": []}, model, amount_scale)
        self.assertEqual(len([r for r in payload["rows"] if r["key"] in ("economic:volume", "result:another-volume")]), 2)

    def test_exact_registered_concept_is_chinese_and_unknown_is_an_explicit_gap(self):
        self.assertEqual(metric_display_label({"metric_ref": "concept:revenue", "observed_by": "sec_actual"}), "营业收入")
        self.assertNotIn("concept:", metric_display_label({"metric_ref": "concept:unknown-secret", "observed_by": "sec_actual"}))

if __name__ == "__main__":
    unittest.main()
