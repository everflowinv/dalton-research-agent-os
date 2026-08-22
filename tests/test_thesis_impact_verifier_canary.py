"""Closed tests for the 3x30 provider-controlled verifier canary campaign."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from dalton_core.contracts import InvocationGranularity, ModelInvocation, ResultEnvelope
from dalton_core.thesis_impact_calibration import (
    load_frozen_calibration_corpus,
    score_verifier_outputs,
)
from dalton_core.thesis_impact_calibration_runner import (
    ThesisImpactCalibrationRunError,
    build_calibration_run_manifest,
    build_calibration_work_order,
)
from dalton_core.thesis_impact_verifier_canary import (
    PRODUCTION_MINIMUM_ROUNDS,
    build_verifier_canary_manifest,
    evaluate_campaign_gate,
    evaluate_round_records,
    run_verifier_canary,
    validate_verifier_canary_manifest,
)


NOW = datetime(2026, 8, 22, 20, 0, tzinfo=timezone.utc)
COMMIT = "b" * 40


def _record(
    case: dict[str, Any],
    manifest: dict[str, Any],
    profile: dict[str, Any],
    *,
    controlled: bool = True,
    succeeded: bool = True,
) -> dict[str, Any]:
    work = build_calibration_work_order(case, manifest)
    invocation = ModelInvocation(
        schema_version="0.1",
        id=f"invocation:canary-{case['id']}",
        created_at=manifest["created_at"],
        work_order_ref=work.id,
        profile_ref=profile["profile_version_ref"],
        granularity=InvocationGranularity.VERIFICATION,
        capability="verify",
        provider=profile["provider"],
        model=profile["model"],
        model_family=profile["family"],
        input_refs=work.input_refs,
        output_refs=(),
        started_at=manifest["created_at"],
        completed_at=manifest["created_at"],
        usage={"raw_provider_telemetry": {"cost": {"available": True, "usd": 0.001}}},
        side_effects=(),
        runtime_ref="adapter:openclaw-model-broker:0.1",
        actor_ref="runtime:openclaw-model-broker",
    )
    outputs = (
        {
            "text": json.dumps({"schema_version": "0.1", "verdict": "pass", "findings": []}),
            "content_hash": "a" * 64,
        }
        if succeeded
        else {}
    )
    result = ResultEnvelope(
        schema_version="0.1",
        id=f"result:canary-{case['id']}",
        created_at=manifest["created_at"],
        work_order_ref=work.id,
        invocation_ref=invocation.id,
        status="succeeded" if succeeded else "failed",
        outputs=outputs,
        actual_side_effects=(),
        usage_refs=(f"usage:canary-{case['id']}",),
        artifact_refs=(),
        error=None if succeeded else {"code": "REQUIRED_CONTROLS_UNAVAILABLE"},
        metadata={
            "required_provider_controls": controlled,
            "provider_control_schema_hash": "b" * 64 if controlled else None,
            "provider_control_mode": "provider-controlled-v1",
            "broker_request_mode": "execute",
            "broker_idempotency_status": "fresh",
            "profile_version_ref": profile["profile_version_ref"],
            "route_decision_ref": f"route-decision:canary-{case['id']}",
        },
    )
    parsed = (
        {
            "schema_version": "0.2",
            "assessment_ref": case["input"]["assessment"]["id"],
            "assessment_hash": case["input"]["assessment"]["content_hash"],
            "verdict": "pass",
            "findings": [],
        }
        if succeeded
        else {}
    )
    return {
        "schema_version": "0.1",
        "case_ref": case["id"],
        "work_order": work.to_dict(),
        "route_decision_ref": f"route-decision:canary-{case['id']}",
        "recovery_mode": "fresh_execute",
        "invocation": invocation.to_dict(),
        "result": result.to_dict(),
        "parsed_output": parsed,
        "parse_error": None if succeeded else "broker result failed",
        "accounted_cost_usd": "0.001" if succeeded else None,
        "cost_reserve_usd": "0" if succeeded else "0.01",
    }


def _score(coverage: int, *, false_positives: int = 0, high_misses: int = 0) -> dict[str, Any]:
    return {
        "coverage": {"numerator": coverage, "denominator": coverage},
        "false_positives": false_positives,
        "high_severity_misses": high_misses,
        "accuracy": 1.0 if not false_positives and not high_misses else 0.5,
    }


class VerifierCanaryManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.corpus = load_frozen_calibration_corpus()

    def test_manifest_freezes_campaign_contract_and_reservations(self) -> None:
        manifest = build_verifier_canary_manifest(
            corpus=self.corpus,
            profile_id="profile:gemini-3-7-flash",
            repo_commit=COMMIT,
            created_at=NOW,
            thinking_level="low",
            rounds=3,
            per_case_cap_usd=Decimal("0.05"),
            per_round_cap_usd=Decimal("1.60"),
            campaign_cap_usd=Decimal("5.00"),
        )
        self.assertEqual(manifest["case_count"], len(self.corpus["cases"]))
        self.assertEqual(manifest["execution_tier"], "provider-controlled-v1")
        self.assertEqual(manifest["thinking_level"], "low")
        self.assertEqual(manifest["rounds"], 3)
        parsed = validate_verifier_canary_manifest(manifest)
        self.assertEqual(parsed["id"], manifest["id"])
        later = build_verifier_canary_manifest(
            corpus=self.corpus,
            profile_id="profile:gemini-3-7-flash",
            repo_commit=COMMIT,
            created_at=NOW + timedelta(microseconds=1),
            thinking_level="low",
            rounds=3,
            per_case_cap_usd=Decimal("0.05"),
            per_round_cap_usd=Decimal("1.60"),
            campaign_cap_usd=Decimal("5.00"),
        )
        self.assertNotEqual(later["id"], manifest["id"])
        smoke = build_verifier_canary_manifest(
            corpus=self.corpus,
            profile_id="profile:gemini-3-7-flash",
            repo_commit=COMMIT,
            created_at=NOW,
            thinking_level="low",
            rounds=1,
            case_refs=[self.corpus["cases"][0]["id"]],
            per_case_cap_usd=Decimal("0.05"),
            per_round_cap_usd=Decimal("0.05"),
            campaign_cap_usd=Decimal("0.05"),
        )
        self.assertEqual(smoke["case_count"], 1)
        self.assertNotEqual(smoke["id"], manifest["id"])
        with self.assertRaisesRegex(ThesisImpactCalibrationRunError, "round cap"):
            build_verifier_canary_manifest(
                corpus=self.corpus,
                profile_id="profile:gemini-3-7-flash",
                repo_commit=COMMIT,
                created_at=NOW,
                thinking_level="low",
                rounds=3,
                per_case_cap_usd=Decimal("0.10"),
                per_round_cap_usd=Decimal("1.60"),
                campaign_cap_usd=Decimal("5.00"),
            )
        with self.assertRaisesRegex(ThesisImpactCalibrationRunError, "campaign cap"):
            build_verifier_canary_manifest(
                corpus=self.corpus,
                profile_id="profile:gemini-3-7-flash",
                repo_commit=COMMIT,
                created_at=NOW,
                thinking_level="low",
                rounds=4,
                per_case_cap_usd=Decimal("0.05"),
                per_round_cap_usd=Decimal("1.60"),
                campaign_cap_usd=Decimal("5.00"),
            )
        with self.assertRaisesRegex(ThesisImpactCalibrationRunError, "thinking"):
            build_verifier_canary_manifest(
                corpus=self.corpus,
                profile_id="profile:gemini-3-7-flash",
                repo_commit=COMMIT,
                created_at=NOW,
                thinking_level="high",
            )
        with self.assertRaisesRegex(ThesisImpactCalibrationRunError, "rounds"):
            build_verifier_canary_manifest(
                corpus=self.corpus,
                profile_id="profile:gemini-3-7-flash",
                repo_commit=COMMIT,
                created_at=NOW,
                thinking_level="low",
                rounds=0,
            )

    def test_manifest_validation_fails_closed(self) -> None:
        manifest = build_verifier_canary_manifest(
            corpus=self.corpus,
            profile_id="profile:gemini-3-7-flash",
            repo_commit=COMMIT,
            created_at=NOW,
            thinking_level="low",
        )
        for mutation, pattern in (
            ({"execution_tier": "calibration-posthoc-v1"}, "provider-controlled"),
            ({"thinking_level": "high"}, "thinking"),
            ({"case_count": manifest["case_count"] + 1}, "case_count"),
            ({"rounds": 11}, "rounds"),
            ({"campaign_cap_usd": "1.00"}, "campaign cap"),
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(
                    ThesisImpactCalibrationRunError, pattern
                ):
                    validate_verifier_canary_manifest({**manifest, **mutation})
        del manifest["thinking_level"]
        with self.assertRaisesRegex(ThesisImpactCalibrationRunError, "shape"):
            validate_verifier_canary_manifest(manifest)


class VerifierCanaryEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.corpus = load_frozen_calibration_corpus()

    def _controlled_record(self, *, thinking: str | None = "low") -> dict[str, Any]:
        work_metadata: dict[str, Any] = {
            "phase": "verification-calibration",
            "verifier_thinking_level": thinking,
        }
        return {
            "recovery_mode": "fresh_execute",
            "result": {
                "status": "succeeded",
                "metadata": {
                    "required_provider_controls": True,
                    "provider_control_schema_hash": "b" * 64,
                    "provider_control_mode": "provider-controlled-v1",
                    "broker_request_mode": "execute",
                    "broker_idempotency_status": "fresh",
                },
            },
            "work_order": {"metadata": work_metadata},
            "parse_error": None,
        }

    def test_clean_round_is_accepted(self) -> None:
        records = [self._controlled_record() for _ in range(30)]
        evaluation = evaluate_round_records(
            records, _score(30), thinking_level="low", case_count=30
        )
        self.assertTrue(evaluation["accepted"])
        self.assertEqual(evaluation["rejection_reasons"], [])
        self.assertTrue(evaluation["complete"])

    def test_quality_control_and_binding_failures_are_counted(self) -> None:
        records = [self._controlled_record() for _ in range(30)]
        records[3] = {
            **records[3],
            "result": {
                "status": "failed",
                "metadata": {
                    "required_provider_controls": False,
                    "provider_control_schema_hash": None,
                    "provider_control_mode": "provider-controlled-v1",
                    "broker_request_mode": "execute",
                    "broker_idempotency_status": "fresh",
                },
            },
            "parse_error": "broker result failed",
        }
        records[7]["work_order"]["metadata"]["verifier_thinking_level"] = None
        evaluation = evaluate_round_records(
            records, _score(30, false_positives=2, high_misses=1),
            thinking_level="low", case_count=30,
        )
        self.assertFalse(evaluation["accepted"])
        self.assertEqual(evaluation["failed_calls"], 1)
        self.assertEqual(evaluation["parse_failures"], 1)
        self.assertEqual(evaluation["control_failures"], 1)
        self.assertEqual(evaluation["thinking_binding_failures"], 1)
        self.assertEqual(evaluation["false_positives"], 2)
        self.assertEqual(evaluation["high_severity_misses"], 1)
        reasons = " ".join(evaluation["rejection_reasons"])
        for fragment in (
            "did not succeed", "closed schema", "provider-control contract",
            "thinking level", "false positives", "high-severity misses",
        ):
            self.assertIn(fragment, reasons)
        incomplete = evaluate_round_records(
            records[:25], _score(25), thinking_level="low", case_count=30
        )
        self.assertFalse(incomplete["accepted"])
        self.assertIn("round coverage is incomplete", incomplete["rejection_reasons"])
        self.assertIn("round did not record every case", incomplete["rejection_reasons"])

    def test_campaign_gate_requires_three_accepted_rounds_within_cap(self) -> None:
        campaign = build_verifier_canary_manifest(
            corpus=self.corpus,
            profile_id="profile:gemini-3-7-flash",
            repo_commit=COMMIT,
            created_at=NOW,
            thinking_level="low",
        )
        accepted = [
            {
                "accepted": True,
                "rejection_reasons": [],
                "run_ref": f"run:{index}",
                "profile_version_ref": "profile-version:gemini:1",
            }
            for index in range(1, 4)
        ]
        rejected = {
            "accepted": False,
            "rejection_reasons": ["1 false positives"],
            "run_ref": "run:3",
            "profile_version_ref": "profile-version:gemini:1",
        }
        two_rounds = evaluate_campaign_gate(
            accepted[:2],
            campaign={**campaign, "rounds": 2},
            spent_or_reserved=Decimal("0.20"),
        )
        self.assertFalse(two_rounds["eligible"])
        self.assertIn(
            f"at least {PRODUCTION_MINIMUM_ROUNDS} complete rounds",
            " ".join(two_rounds["reasons"]),
        )
        failed_round = evaluate_campaign_gate(
            [accepted[0], accepted[1], rejected],
            campaign=campaign,
            spent_or_reserved=Decimal("0.20"),
        )
        self.assertFalse(failed_round["eligible"])
        self.assertIn("round 3 was not accepted", " ".join(failed_round["reasons"]))
        over_cap = evaluate_campaign_gate(
            accepted,
            campaign=campaign,
            spent_or_reserved=Decimal("6.00"),
        )
        self.assertFalse(over_cap["eligible"])
        self.assertIn("exceeded the campaign cap", " ".join(over_cap["reasons"]))
        passed = evaluate_campaign_gate(
            accepted,
            campaign=campaign,
            spent_or_reserved=Decimal("0.20"),
        )
        self.assertTrue(passed["eligible"])
        self.assertEqual(passed["reasons"], [])
        self.assertEqual(passed["rounds_accepted"], 3)
        duplicate_runs = [
            accepted[0],
            {**accepted[1], "run_ref": "run:1"},
            accepted[2],
        ]
        duplicate_gate = evaluate_campaign_gate(
            duplicate_runs,
            campaign=campaign,
            spent_or_reserved=Decimal("0.20"),
        )
        self.assertFalse(duplicate_gate["eligible"])
        self.assertIn("not unique", " ".join(duplicate_gate["reasons"]))


class VerifierCanaryRoundDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.corpus = load_frozen_calibration_corpus()
        self.case = self.corpus["cases"][0]
        from dalton_core.model_deployment import openclaw_broker_profiles

        self.profile = next(
            item
            for item in openclaw_broker_profiles(checked_at=NOW)
            if item["id"] == "profile:gemini-3-7-flash"
        )

    def _write_round(self, round_dir: Path, *, thinking: str | None) -> None:
        manifest = build_calibration_run_manifest(
            corpus=self.corpus,
            profile=self.profile,
            repo_commit=COMMIT,
            created_at=NOW,
            run_cap_usd=Decimal("0.05"),
            per_case_cap_usd=Decimal("0.05"),
            max_input_tokens=30_000,
            max_output_tokens=4_000,
            timeout_seconds=180,
            thinking_level=thinking,
            case_refs=[self.case["id"]],
        )
        round_dir.mkdir(parents=True, mode=0o700)
        (round_dir / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        record = _record(self.case, manifest, self.profile)
        (round_dir / "responses.jsonl").write_text(
            json.dumps(record) + "\n", encoding="utf-8"
        )
        (round_dir / "score.json").write_text(
            json.dumps(
                score_verifier_outputs(
                    {record["case_ref"]: record["parsed_output"]},
                    corpus=self.corpus,
                )
            ),
            encoding="utf-8",
        )

    def test_round_dir_evaluation_binds_the_campaign_contract(self) -> None:
        from dalton_core.thesis_impact_verifier_canary import (
            _evaluate_round_dir,
        )

        campaign = build_verifier_canary_manifest(
            corpus=self.corpus,
            profile_id="profile:gemini-3-7-flash",
            repo_commit=COMMIT,
            created_at=NOW,
            thinking_level="low",
            rounds=3,
            per_case_cap_usd=Decimal("0.05"),
            per_round_cap_usd=Decimal("1.60"),
            campaign_cap_usd=Decimal("5.00"),
        )
        campaign = {
            **campaign,
            "case_refs": [self.case["id"]],
            "case_count": 1,
        }
        base = Path(self.temp.name)
        matching = base / "round-matching"
        self._write_round(matching, thinking="low")
        evaluation = _evaluate_round_dir(matching, campaign=campaign)
        self.assertTrue(evaluation["accepted"])
        self.assertEqual(evaluation["status"], "complete")
        self.assertEqual(evaluation["accounted_cost_usd"], "0.001")
        (matching / "score.json").write_text(
            json.dumps(_score(1, false_positives=1)), encoding="utf-8"
        )
        tampered = _evaluate_round_dir(matching, campaign=campaign)
        self.assertFalse(tampered["accepted"])
        self.assertIn(
            "differs from records recomputation",
            " ".join(tampered["rejection_reasons"]),
        )

        legacy = base / "round-legacy"
        self._write_round(legacy, thinking=None)
        legacy_evaluation = _evaluate_round_dir(legacy, campaign=campaign)
        self.assertFalse(legacy_evaluation["accepted"])
        self.assertIn(
            "does not bind the exact campaign contract",
            " ".join(legacy_evaluation["rejection_reasons"]),
        )
        missing = _evaluate_round_dir(base / "round-absent", campaign=campaign)
        self.assertEqual(missing["status"], "missing")
        self.assertFalse(missing["accepted"])

    def test_run_refuses_existing_output_dir_without_resume(self) -> None:
        output_dir = Path(self.temp.name) / "campaign"
        output_dir.mkdir()
        with self.assertRaisesRegex(
            ThesisImpactCalibrationRunError, "output directory already exists"
        ):
            run_verifier_canary(
                repo_root=Path(__file__).resolve().parent.parent,
                output_dir=output_dir,
                socket_path=Path("/nonexistent/broker.sock"),
                auth_key_path=Path("/nonexistent/broker.key"),
                allow_dirty=True,
            )


if __name__ == "__main__":
    unittest.main()
