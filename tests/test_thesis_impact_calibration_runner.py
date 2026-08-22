import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from dalton_core.contracts import InvocationGranularity, ModelInvocation, ResultEnvelope
from dalton_core.model_deployment import openclaw_broker_profiles
from dalton_core.openclaw_model_adapter import (
    PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC,
    PROVIDER_CONTROL_MODE_REQUIRED,
)
from dalton_core.thesis_impact_calibration import load_frozen_calibration_corpus
from dalton_core.thesis_impact_calibration_runner import (
    ThesisImpactCalibrationRunError,
    _record_cost,
    _strict_json_output,
    build_calibration_run_manifest,
    build_calibration_work_order,
    calibration_output_map,
    load_calibration_records,
    validate_calibration_record,
    validate_calibration_run_manifest,
)


NOW = datetime(2026, 8, 21, 20, 0, tzinfo=timezone.utc)


class ThesisImpactCalibrationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.corpus = load_frozen_calibration_corpus()
        self.profile = next(
            item for item in openclaw_broker_profiles(checked_at=NOW)
            if item["id"] == "profile:deepseek-v4-flash"
        )
        self.manifest = build_calibration_run_manifest(
            corpus=self.corpus,
            profile=self.profile,
            repo_commit="a" * 40,
            created_at=NOW,
            run_cap_usd=Decimal("0.30"),
            per_case_cap_usd=Decimal("0.01"),
            max_input_tokens=3000,
            max_output_tokens=1000,
            timeout_seconds=120,
        )

    def test_manifest_freezes_profile_corpus_code_and_spend(self):
        parsed = validate_calibration_run_manifest(self.manifest)
        self.assertEqual(parsed["profile_id"], "profile:deepseek-v4-flash")
        self.assertEqual(parsed["case_refs"], [
            case["id"] for case in self.corpus["cases"]
        ])
        self.assertEqual(parsed["per_case_cap_usd"], "0.01")
        self.assertEqual(parsed["execution_tier"], PROVIDER_CONTROL_MODE_REQUIRED)
        with self.assertRaisesRegex(ThesisImpactCalibrationRunError, "exceeds"):
            build_calibration_run_manifest(
                corpus=self.corpus,
                profile=self.profile,
                repo_commit="a" * 40,
                created_at=NOW,
                run_cap_usd=Decimal("0.11"),
                per_case_cap_usd=Decimal("0.01"),
                max_input_tokens=3000,
                max_output_tokens=1000,
                timeout_seconds=120,
            )

        smoke = build_calibration_run_manifest(
            corpus=self.corpus,
            profile=self.profile,
            repo_commit="a" * 40,
            created_at=NOW,
            run_cap_usd=Decimal("0.20"),
            per_case_cap_usd=Decimal("0.20"),
            max_input_tokens=3000,
            max_output_tokens=1000,
            timeout_seconds=120,
            execution_tier=PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC,
            case_refs=[self.corpus["cases"][0]["id"]],
        )
        self.assertEqual(smoke["case_refs"], [self.corpus["cases"][0]["id"]])
        self.assertEqual(
            smoke["execution_tier"],
            PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC,
        )
        changed_budget = build_calibration_run_manifest(
            corpus=self.corpus,
            profile=self.profile,
            repo_commit="a" * 40,
            created_at=NOW,
            run_cap_usd=Decimal("0.30"),
            per_case_cap_usd=Decimal("0.01"),
            max_input_tokens=3001,
            max_output_tokens=1000,
            timeout_seconds=120,
        )
        self.assertNotEqual(changed_budget["id"], self.manifest["id"])
        later_instance = build_calibration_run_manifest(
            corpus=self.corpus,
            profile=self.profile,
            repo_commit="a" * 40,
            created_at=NOW + timedelta(microseconds=1),
            run_cap_usd=Decimal("0.30"),
            per_case_cap_usd=Decimal("0.01"),
            max_input_tokens=3000,
            max_output_tokens=1000,
            timeout_seconds=120,
        )
        self.assertNotEqual(later_instance["id"], self.manifest["id"])

    def test_thinking_level_is_frozen_and_closed(self):
        self.assertIsNone(self.manifest["thinking_level"])
        thinking = build_calibration_run_manifest(
            corpus=self.corpus,
            profile=self.profile,
            repo_commit="a" * 40,
            created_at=NOW,
            run_cap_usd=Decimal("0.30"),
            per_case_cap_usd=Decimal("0.01"),
            max_input_tokens=3000,
            max_output_tokens=1000,
            timeout_seconds=120,
            thinking_level="low",
        )
        self.assertEqual(thinking["thinking_level"], "low")
        self.assertNotEqual(thinking["id"], self.manifest["id"])
        parsed = validate_calibration_run_manifest(thinking)
        self.assertEqual(parsed["thinking_level"], "low")
        case = self.corpus["cases"][0]
        frozen = build_calibration_work_order(case, thinking).metadata
        self.assertEqual(frozen["verifier_thinking_level"], "low")
        self.assertNotIn(
            "verifier_thinking_level",
            build_calibration_work_order(case, self.manifest).metadata,
        )
        with self.assertRaisesRegex(
            ThesisImpactCalibrationRunError, "thinking_level"
        ):
            build_calibration_run_manifest(
                corpus=self.corpus,
                profile=self.profile,
                repo_commit="a" * 40,
                created_at=NOW,
                run_cap_usd=Decimal("0.30"),
                per_case_cap_usd=Decimal("0.01"),
                max_input_tokens=3000,
                max_output_tokens=1000,
                timeout_seconds=120,
                thinking_level="high",
            )
        broken = dict(thinking)
        broken["thinking_level"] = "high"
        with self.assertRaisesRegex(
            ThesisImpactCalibrationRunError, "thinking_level"
        ):
            validate_calibration_run_manifest(broken)

    def test_work_order_is_deterministic_and_contains_no_gold(self):
        case = self.corpus["cases"][0]
        first = build_calibration_work_order(case, self.manifest).to_dict()
        second = build_calibration_work_order(case, self.manifest).to_dict()
        self.assertEqual(first, second)
        serialized = json.dumps(first, sort_keys=True)
        self.assertNotIn("seeded_error", serialized)
        self.assertNotIn("required_finding_codes", serialized)
        self.assertNotIn(case["gold"]["rationale"], serialized)
        self.assertEqual(first["budget"]["max_cost_usd"], 0.01)
        self.assertEqual(first["metadata"]["verifier_output_schema_version"], "0.2")
        self.assertEqual(first["metadata"]["verifier_decision_schema_version"], "0.1")
        self.assertEqual(first["metadata"]["verifier_binding_mode"], "wrapper-owned-v1")
        self.assertEqual(
            first["metadata"]["execution_tier"],
            PROVIDER_CONTROL_MODE_REQUIRED,
        )

    def _record(self):
        case = self.corpus["cases"][0]
        work = build_calibration_work_order(case, self.manifest)
        decision = {
            "schema_version": "0.1",
            "verdict": "pass",
            "findings": [],
        }
        output = {
            "schema_version": "0.2",
            "assessment_ref": case["input"]["assessment"]["id"],
            "assessment_hash": case["input"]["assessment"]["content_hash"],
            "verdict": "pass",
            "findings": [],
        }
        invocation = ModelInvocation(
            schema_version="0.1",
            id="invocation:test-calibration",
            created_at=self.manifest["created_at"],
            work_order_ref=work.id,
            profile_ref=self.profile["profile_version_ref"],
            granularity=InvocationGranularity.VERIFICATION,
            capability="verify",
            provider="deepseek",
            model="deepseek-v4-flash",
            model_family="deepseek-v4",
            input_refs=work.input_refs,
            output_refs=(),
            started_at=self.manifest["created_at"],
            completed_at=self.manifest["created_at"],
            usage={"raw_provider_telemetry": {"cost": {"available": True, "usd": 0.001}}},
            side_effects=(),
            runtime_ref="adapter:openclaw-model-broker:0.1",
            actor_ref="runtime:openclaw-model-broker",
        )
        result = ResultEnvelope(
            schema_version="0.1",
            id="result:test-calibration",
            created_at=self.manifest["created_at"],
            work_order_ref=work.id,
            invocation_ref=invocation.id,
            status="succeeded",
            outputs={"text": json.dumps(decision), "content_hash": "a" * 64},
            actual_side_effects=(),
            usage_refs=("usage:test-calibration",),
            artifact_refs=(),
        )
        return {
            "schema_version": "0.1",
            "case_ref": case["id"],
            "work_order": work.to_dict(),
            "route_decision_ref": "route-decision:test-calibration",
            "recovery_mode": "fresh_execute",
            "invocation": invocation.to_dict(),
            "result": result.to_dict(),
            "parsed_output": output,
            "parse_error": None,
            "accounted_cost_usd": "0.001",
            "cost_reserve_usd": "0",
        }

    def test_record_log_is_closed_bound_and_resume_safe(self):
        record = validate_calibration_record(self._record())
        self.assertEqual(
            calibration_output_map([record], self.manifest)[record["case_ref"]]["verdict"],
            "pass",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "responses.jsonl"
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            self.assertEqual(load_calibration_records(path), [record])
            path.write_text(json.dumps(record) + "\n" + json.dumps(record) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ThesisImpactCalibrationRunError, "duplicate"):
                load_calibration_records(path)

    def test_record_binding_and_manifest_shape_fail_closed(self):
        record = self._record()
        record["result"]["work_order_ref"] = "work:other"
        with self.assertRaisesRegex(ThesisImpactCalibrationRunError, "bindings"):
            validate_calibration_record(record)
        manifest = copy.deepcopy(self.manifest)
        manifest["extra"] = True
        with self.assertRaisesRegex(ThesisImpactCalibrationRunError, "shape"):
            validate_calibration_run_manifest(manifest)

    def test_model_output_duplicate_keys_are_scored_invalid(self):
        record = self._record()
        text = '{"schema_version":"0.2","schema_version":"0.1"}'
        result = ResultEnvelope.from_dict(record["result"])
        result = ResultEnvelope(
            schema_version=result.schema_version,
            id=result.id,
            created_at=result.created_at,
            work_order_ref=result.work_order_ref,
            invocation_ref=result.invocation_ref,
            status=result.status,
            outputs={
                "text": text,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            },
            actual_side_effects=result.actual_side_effects,
            usage_refs=result.usage_refs,
            artifact_refs=result.artifact_refs,
            error=result.error,
            metadata=result.metadata,
        )
        parsed, error = _strict_json_output(result)
        self.assertEqual(parsed, {})
        self.assertIn("duplicate JSON key", error)

    def test_provider_float_cost_is_normalized_without_fake_precision(self):
        record = self._record()
        invocation = record["invocation"]
        invocation["usage"]["raw_provider_telemetry"]["cost"]["usd"] = (
            0.00020064000000000003
        )
        accounted, reserve = _record_cost(
            ModelInvocation.from_dict(invocation), Decimal("0.01")
        )
        self.assertEqual(accounted, "0.00020064")
        self.assertEqual(reserve, "0")

    def test_failed_provider_record_is_durable_but_not_scored(self):
        record = self._record()
        record["result"]["status"] = "failed"
        record["result"]["outputs"] = {}
        record["result"]["error"] = {
            "code": "PROVIDER_BUDGET_EXCEEDED",
            "message": "provider output exceeded the WorkOrder budget",
            "source": "openclaw-model-adapter",
        }
        record["parsed_output"] = {}
        record["parse_error"] = "broker result failed"
        parsed = validate_calibration_record(record)
        self.assertEqual(calibration_output_map([parsed], self.manifest), {})
        invocation = ModelInvocation.from_dict(record["invocation"])
        invocation.usage["raw_provider_telemetry"]["cost"]["usd"] = 0.11585
        with self.assertRaisesRegex(ThesisImpactCalibrationRunError, "exceeds"):
            _record_cost(invocation, Decimal("0.04"))
        self.assertEqual(
            _record_cost(
                invocation, Decimal("0.04"), allow_over_cap=True
            ),
            ("0.11585", "0"),
        )


if __name__ == "__main__":
    unittest.main()
