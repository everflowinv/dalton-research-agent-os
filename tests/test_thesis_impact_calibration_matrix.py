import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from dalton_core.model_deployment import openclaw_broker_profiles
from dalton_core.openclaw_model_adapter import (
    PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC,
)
from dalton_core.thesis_impact_calibration import load_frozen_calibration_corpus
from dalton_core.thesis_impact_calibration_matrix import (
    build_calibration_matrix_manifest,
    load_calibration_matrix_records,
    validate_calibration_matrix_manifest,
    validate_calibration_matrix_record,
)
from dalton_core.thesis_impact_calibration_runner import (
    ThesisImpactCalibrationRunError,
)


NOW = datetime(2026, 8, 22, 6, 0, tzinfo=timezone.utc)


class ThesisImpactCalibrationMatrixTests(unittest.TestCase):
    def setUp(self):
        self.corpus = load_frozen_calibration_corpus()
        self.profiles = openclaw_broker_profiles(checked_at=NOW)
        self.case_ref = self.corpus["cases"][0]["id"]
        self.manifest = build_calibration_matrix_manifest(
            corpus=self.corpus,
            profiles=self.profiles,
            repo_commit="b" * 40,
            created_at=NOW,
            case_refs=[self.case_ref],
            total_cap_usd=Decimal("4.60"),
            per_case_cap_usd=Decimal("0.20"),
            max_input_tokens=3000,
            max_output_tokens=1000,
            timeout_seconds=120,
        )

    def test_manifest_freezes_all_profiles_and_posthoc_tier(self):
        parsed = validate_calibration_matrix_manifest(self.manifest)
        self.assertEqual(len(parsed["profile_ids"]), 23)
        self.assertEqual(parsed["case_refs"], [self.case_ref])
        self.assertEqual(
            parsed["execution_tier"],
            PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC,
        )
        with self.assertRaisesRegex(ThesisImpactCalibrationRunError, "reservations"):
            build_calibration_matrix_manifest(
                corpus=self.corpus,
                profiles=self.profiles,
                repo_commit="b" * 40,
                created_at=NOW,
                case_refs=[self.case_ref],
                total_cap_usd=Decimal("4.59"),
                per_case_cap_usd=Decimal("0.20"),
                max_input_tokens=3000,
                max_output_tokens=1000,
                timeout_seconds=120,
            )

    def test_record_log_is_closed_and_profile_unique(self):
        record = validate_calibration_matrix_record({
            "schema_version": "0.2",
            "profile_id": self.manifest["profile_ids"][0],
            "status": "failed",
            "started_at": NOW.isoformat(),
            "completed_at": NOW.isoformat(),
            "accounted_cost_usd": "0",
            "unpriced_reserve_usd": "0.20",
            "spent_or_reserved_usd": "0.20",
            "succeeded_calls": 0,
            "valid_outputs": 0,
            "run_summary": None,
            "error": "provider unavailable",
        })
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matrix-records.jsonl"
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            self.assertEqual(load_calibration_matrix_records(path), [record])
            path.write_text(
                json.dumps(record) + "\n" + json.dumps(record) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ThesisImpactCalibrationRunError, "duplicate"):
                load_calibration_matrix_records(path)

        drifted = copy.deepcopy(record)
        drifted["extra"] = True
        with self.assertRaisesRegex(ThesisImpactCalibrationRunError, "shape"):
            validate_calibration_matrix_record(drifted)

        unreconciled = copy.deepcopy(record)
        unreconciled["accounted_cost_usd"] = "0.01"
        with self.assertRaisesRegex(ThesisImpactCalibrationRunError, "reconcile"):
            validate_calibration_matrix_record(unreconciled)


if __name__ == "__main__":
    unittest.main()
