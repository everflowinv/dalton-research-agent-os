import copy
import json
import unittest
from pathlib import Path

from dalton_core.thesis_impact import (
    VERIFIER_FINDING_SEVERITIES,
    ThesisImpactValidationError,
    validate_thesis_impact_verifier_output,
)
from dalton_core.thesis_impact_calibration import (
    ThesisImpactCalibrationError,
    build_calibration_prompt,
    load_frozen_calibration_corpus,
    model_visible_case,
    observed_output_map,
    score_verifier_outputs,
    validate_calibration_corpus,
)
from dalton_core.store import content_hash


ROOT = Path(__file__).parents[1]


class ThesisImpactVerifierContractV02Tests(unittest.TestCase):
    def output(self, **overrides):
        wire = {
            "schema_version": "0.2",
            "assessment_ref": "assessment:test",
            "assessment_hash": "a" * 64,
            "verdict": "pass",
            "findings": [],
        }
        wire.update(overrides)
        return wire

    @staticmethod
    def finding(code="unsupported_inference", **overrides):
        wire = {
            "code": code,
            "severity": VERIFIER_FINDING_SEVERITIES[code],
            "detail": "The conclusion materially exceeds the exact claim.",
            "expected_impact": None,
        }
        wire.update(overrides)
        return wire

    def test_strict_pass_and_reject_contracts(self):
        self.assertEqual(
            validate_thesis_impact_verifier_output(
                self.output(), required_schema_version="0.2"
            )["verdict"],
            "pass",
        )
        rejected = validate_thesis_impact_verifier_output(
            self.output(
                verdict="reject",
                findings=[self.finding()],
            ),
            required_schema_version="0.2",
        )
        self.assertEqual(rejected["findings"][0]["severity"], "high")

    def test_pass_with_findings_and_empty_reject_fail_closed(self):
        with self.assertRaisesRegex(ThesisImpactValidationError, "pass verdict"):
            validate_thesis_impact_verifier_output(
                self.output(findings=[self.finding()]),
                required_schema_version="0.2",
            )
        with self.assertRaisesRegex(ThesisImpactValidationError, "reject verdict"):
            validate_thesis_impact_verifier_output(
                self.output(verdict="reject"),
                required_schema_version="0.2",
            )

    def test_codes_severities_and_expected_impact_are_closed(self):
        with self.assertRaisesRegex(ThesisImpactValidationError, "finding code"):
            validate_thesis_impact_verifier_output(
                self.output(
                    verdict="reject",
                    findings=[{
                        "code": "hash_consistency",
                        "severity": "high",
                        "detail": "Invented code.",
                        "expected_impact": None,
                    }],
                ),
                required_schema_version="0.2",
            )
        with self.assertRaisesRegex(ThesisImpactValidationError, "frozen severity"):
            validate_thesis_impact_verifier_output(
                self.output(
                    verdict="reject",
                    findings=[self.finding(severity="low")],
                ),
                required_schema_version="0.2",
            )
        with self.assertRaisesRegex(ThesisImpactValidationError, "closed impact taxonomy"):
            validate_thesis_impact_verifier_output(
                self.output(
                    verdict="reject",
                    findings=[self.finding(
                        code="impact_mismatch",
                        expected_impact="contradictory",
                    )],
                ),
                required_schema_version="0.2",
            )

    def test_legacy_is_readable_but_cannot_satisfy_new_work_order(self):
        legacy = self.output(schema_version="0.1")
        self.assertEqual(
            validate_thesis_impact_verifier_output(legacy)["schema_version"],
            "0.1",
        )
        with self.assertRaisesRegex(ThesisImpactValidationError, "WorkOrder contract"):
            validate_thesis_impact_verifier_output(
                legacy, required_schema_version="0.2"
            )


class ThesisImpactCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.corpus = load_frozen_calibration_corpus()

    def test_frozen_corpus_has_twelve_cases_and_hidden_gold(self):
        self.assertEqual(len(self.corpus["cases"]), 12)
        self.assertEqual(
            content_hash(self.corpus),
            "c5f6928860f043fb4f3a01962dc68d7e53fd8c93b3291fafbc85e96aabb41797",
        )
        self.assertEqual(
            {item["severity"] for item in self.corpus["cases"]},
            {"none", "medium", "high"},
        )
        visible = model_visible_case(self.corpus["cases"][0])
        serialized = json.dumps(visible, sort_keys=True)
        self.assertNotIn("gold", serialized)
        self.assertNotIn("seeded_error", serialized)
        self.assertNotIn("expected_verdict", serialized)
        prompt = build_calibration_prompt(self.corpus["cases"][0])
        self.assertNotIn("seeded_error", prompt)
        self.assertNotIn("required_finding_codes", prompt)
        self.assertNotIn(self.corpus["cases"][0]["gold"]["rationale"], prompt)

    def test_json_contract_and_python_finding_codes_match(self):
        schema = json.loads((
            ROOT / "contracts" / "thesis-impact-verifier-output-v0.2.schema.json"
        ).read_text(encoding="utf-8"))
        packaged_schema = json.loads((
            ROOT / "src" / "dalton_core" / "thesis-impact-verifier-output-v0.2.schema.json"
        ).read_text(encoding="utf-8"))
        self.assertEqual(packaged_schema, schema)
        codes = schema["$defs"]["finding"]["properties"]["code"]["enum"]
        expected_impacts = schema["$defs"]["finding"]["allOf"][1]["then"][
            "properties"
        ]["expected_impact"]["enum"]
        self.assertEqual(set(codes), set(VERIFIER_FINDING_SEVERITIES))
        self.assertEqual(
            set(expected_impacts),
            {"supports", "weakens", "no_change", "insufficient"},
        )

    def test_historical_gate2_output_is_one_measured_false_positive(self):
        report = score_verifier_outputs(
            observed_output_map(self.corpus),
            corpus=self.corpus,
            required_schema_version=None,
        )
        self.assertEqual(report["evaluated_cases"], 1)
        self.assertEqual(report["expected_passes"], 1)
        self.assertEqual(report["false_positives"], 1)
        self.assertEqual(report["false_positive_rate"], 1.0)
        self.assertFalse(report["automation_eligible"])
        self.assertIn(
            "calibration coverage is incomplete",
            report["automation_ineligibility_reasons"],
        )

    def test_scorer_recognizes_gold_outputs_but_small_corpus_never_unlocks(self):
        outputs = {}
        for case in self.corpus["cases"]:
            gold = case["gold"]
            findings = []
            for code in gold["required_finding_codes"]:
                findings.append({
                    "code": code,
                    "severity": VERIFIER_FINDING_SEVERITIES[code],
                    "detail": "Seeded condition is present in the exact quoted input.",
                    "expected_impact": (
                        gold["expected_impact"] if code == "impact_mismatch" else None
                    ),
                })
            outputs[case["id"]] = {
                "schema_version": "0.2",
                "assessment_ref": case["input"]["assessment"]["id"],
                "assessment_hash": case["input"]["assessment"]["content_hash"],
                "verdict": gold["verdict"],
                "findings": findings,
            }
        report = score_verifier_outputs(outputs, corpus=self.corpus)
        self.assertEqual(report["evaluated_cases"], 12)
        self.assertEqual(report["accuracy"], 1.0)
        self.assertEqual(report["detection_rate"], 1.0)
        self.assertEqual(report["high_severity_misses"], 0)
        self.assertFalse(report["automation_eligible"])
        self.assertIn(
            "frozen corpus has fewer than 30 seeded cases",
            report["automation_ineligibility_reasons"],
        )

    def test_corpus_and_output_case_sets_are_closed(self):
        tampered = copy.deepcopy(self.corpus)
        tampered["rubric"]["release_thresholds"]["minimum_detection_rate"] = 0.5
        with self.assertRaisesRegex(ThesisImpactCalibrationError, "thresholds drifted"):
            validate_calibration_corpus(tampered)
        with self.assertRaisesRegex(ThesisImpactCalibrationError, "unknown cases"):
            score_verifier_outputs(
                {"calibration:unknown": {}}, corpus=self.corpus
            )


if __name__ == "__main__":
    unittest.main()
