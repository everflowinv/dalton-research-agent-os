"""Forecast line authority and deterministic growth extension tests."""

from __future__ import annotations

import unittest

from dalton_core.model_forecast import (
    AUTOMATION_ACTOR,
    FORMULA_HASH,
    FORMULA_REF,
    ModelForecastAuthority,
    ModelForecastConflict,
    ModelForecastValidationError,
    extend_growth,
)
from dalton_core.model_input import ModelInputLedger
from dalton_core.store import DaltonStore, content_hash


SCENARIO_REF = "input:acn:scenario:base"
ACTUAL_REF = "input:acn:revenue:q3fy26"
GROWTH_REF = "input:acn:revenue-growth:fy27"


class ModelForecastTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.ledger = ModelInputLedger(self.store)
        self.forecast = ModelForecastAuthority(self.store)
        self.scenario_version = self._admit(
            input_kind="scenario", model_input_ref=SCENARIO_REF,
            payload={
                "schema_version": "0.1", "scenario_ref": SCENARIO_REF,
                "label": "Base", "description": "Base scenario",
                "base_scenario_version_ref": None,
                "base_scenario_version_hash": None, "owner_ref": "human:owner",
            }, candidate_id="candidate:scenario:1",
        )
        evidence = self.store.register_evidence({
            "evidence_ref": "evidence:acn:q3", "source_type": "sec-filing",
            "source_ref": "sec:0001467373-26-000031", "retrieved_at": "2026-06-20",
            "source_lineage": ["sec:0001467373-26-000031"],
            "independence_group": "issuer:acn:q3fy26", "actor_ref": "system:sec",
        })
        self.actual_version = self._admit(
            input_kind="actual", model_input_ref=ACTUAL_REF,
            payload={
                "schema_version": "0.1", "metric_ref": "metric:revenue-usd",
                "subject_ref": "company:sec-cik:0001467373",
                "business_line_ref": None,
                "period": {
                    "start": "2026-03-01", "end": "2026-05-31",
                    "calendar": "company:fiscal", "kind": "quarter",
                },
                "unit": "million", "currency": "USD", "value": "17260",
                "source_authorities": [{
                    "authority_kind": "evidence_version",
                    "version_ref": evidence["evidence_version_id"],
                    "content_hash": evidence["content_hash"],
                }],
            }, candidate_id="candidate:actual:1",
        )
        self.growth_version = self._admit(
            input_kind="assumption", model_input_ref=GROWTH_REF,
            payload={
                "schema_version": "0.1",
                "driver_ref": "driver:bookings-mix-and-conversion",
                "subject_ref": "company:sec-cik:0001467373",
                "effective_period": {
                    "start": "2026-06-01", "end": "2027-05-31",
                    "calendar": "company:fiscal", "kind": "forecast_period",
                },
                "unit": "percent", "currency": "USD", "value": "1.15",
                "formula": None,
                "scenario_version_ref": self.scenario_version["id"],
                "scenario_version_hash": self.scenario_version["content_hash"],
                "owner_ref": "human:owner",
                "rationale": "Mid-single-digit annual growth compounds to ~1.15% per quarter.",
                "provenance": "judgment", "source_authorities": [],
                "dependency_bindings": [],
            }, candidate_id="candidate:growth:1",
        )

    def _admit(self, *, input_kind, model_input_ref, payload, candidate_id):
        candidate = self.ledger.propose_input(
            candidate_id=candidate_id, input_kind=input_kind,
            model_input_ref=model_input_ref, prior_version_ref=None,
            payload=payload, proposed_by="agent:researcher",
            idempotency_key=candidate_id,
        )["candidate"]
        decided = self.ledger.decide_input(
            decision_id=f"decision:{candidate_id}", candidate_id=candidate["id"],
            candidate_hash=candidate["content_hash"], verdict="admit",
            rationale="isolated test admission", findings=[],
            reviewer_ref="human:owner", version_id=f"input-version:{candidate_id}",
            idempotency_key=f"decision:{candidate_id}",
        )
        return decided["version"]

    def test_growth_extension_is_deterministic_and_replays(self) -> None:
        first = extend_growth(
            self.ledger, self.forecast,
            base_input_version_ref=self.actual_version["id"],
            growth_input_version_ref=self.growth_version["id"],
            periods=4, line_ref_prefix="forecast-line:acn:revenue",
            model_run_ref="model-run:acn-revenue-growth-extend",
            idempotency_key="extend:1",
        )
        self.assertEqual("fresh", first["status"])
        self.assertEqual(4, len(first["lines"]))
        from decimal import Decimal
        expected_q1 = (Decimal("17260") * Decimal("1.0115")).quantize(Decimal("0.00000001"))
        self.assertEqual(str(expected_q1), first["lines"][0]["value"])
        self.assertEqual(
            "derived_deterministic", first["lines"][0]["value_kind"]
        )
        self.assertEqual(
            "2026-06-01", first["lines"][0]["period"]["start"]
        )
        self.assertEqual(
            "2026-08-31", first["lines"][0]["period"]["end"]
        )
        self.assertEqual("2026-09-01", first["lines"][1]["period"]["start"])
        self.assertEqual("2027-05-31", first["lines"][3]["period"]["end"])
        for line in first["lines"]:
            self.assertEqual(FORMULA_REF, line["formula_ref"])
            self.assertEqual(FORMULA_HASH, line["formula_hash"])
            self.assertEqual(AUTOMATION_ACTOR, line["actor_ref"])
            reread = self.forecast.line(line["id"])
            self.assertEqual(line["content_hash"], reread["content_hash"])
        replay = extend_growth(
            self.ledger, self.forecast,
            base_input_version_ref=self.actual_version["id"],
            growth_input_version_ref=self.growth_version["id"],
            periods=4, line_ref_prefix="forecast-line:acn:revenue",
            model_run_ref="model-run:acn-revenue-growth-extend",
            idempotency_key="extend:1",
        )
        self.assertEqual(4, len(replay["lines"]))
        self.assertEqual(
            first["lines"][0]["content_hash"], replay["lines"][0]["content_hash"]
        )

    def test_human_lines_and_closed_shape_rules(self) -> None:
        line = self.forecast.publish_line(
            "forecast-line:acn:manual",
            subject_ref="company:sec-cik:0001467373",
            metric_or_aspect="metric:revenue-usd",
            period={
                "start": "2026-06-01", "end": "2026-08-31",
                "calendar": "company:fiscal", "kind": "quarter",
            },
            unit="million", currency="USD", value="17500",
            value_kind="estimate",
            scenario_version_ref=self.scenario_version["id"],
            scenario_version_hash=self.scenario_version["content_hash"],
            actor_ref="human:owner", rationale="manual estimate for review",
            version_id="forecast-line-version:acn-manual:1",
            prior_version_ref=None, idempotency_key="manual:1",
        )
        self.assertEqual("fresh", line["status"])
        with self.assertRaises(ModelForecastValidationError):
            self.forecast.publish_line(
                "forecast-line:acn:manual",
                subject_ref="company:sec-cik:0001467373",
                metric_or_aspect="metric:revenue-usd",
                period={
                    "start": "2026-06-01", "end": "2026-08-31",
                    "calendar": "company:fiscal", "kind": "quarter",
                },
                unit="million", currency="USD", value="17500",
                value_kind="estimate",
                scenario_version_ref=self.scenario_version["id"],
                scenario_version_hash=self.scenario_version["content_hash"],
                actor_ref="automation:rogue", rationale="automation guess",
                version_id="forecast-line-version:acn-manual:2",
                prior_version_ref=line["id"], idempotency_key="manual:2",
            )
        with self.assertRaises(ModelForecastValidationError):
            # derived line with a fabricated formula contract
            self.forecast.publish_line(
                "forecast-line:acn:fake",
                subject_ref="company:sec-cik:0001467373",
                metric_or_aspect="metric:revenue-usd",
                period={
                    "start": "2026-06-01", "end": "2026-08-31",
                    "calendar": "company:fiscal", "kind": "quarter",
                },
                unit="million", currency="USD", value="1",
                value_kind="derived_deterministic",
                scenario_version_ref=self.scenario_version["id"],
                scenario_version_hash=self.scenario_version["content_hash"],
                actor_ref="automation:forecast-extender",
                base_input_version_ref=self.actual_version["id"],
                base_input_version_hash=self.actual_version["content_hash"],
                growth_input_version_ref=self.growth_version["id"],
                growth_input_version_hash=self.growth_version["content_hash"],
                formula_ref="formula:wishful-thinking:1",
                formula_hash="a" * 64,
                model_run_version_ref="model-run-version:x:1",
                version_id="forecast-line-version:acn-fake:1",
                prior_version_ref=None, idempotency_key="fake:1",
            )
        with self.assertRaises(ModelForecastConflict):
            # version chain must continue the latest
            self.forecast.publish_line(
                "forecast-line:acn:manual",
                subject_ref="company:sec-cik:0001467373",
                metric_or_aspect="metric:revenue-usd",
                period={
                    "start": "2026-09-01", "end": "2026-11-30",
                    "calendar": "company:fiscal", "kind": "quarter",
                },
                unit="million", currency="USD", value="17600",
                value_kind="estimate",
                scenario_version_ref=self.scenario_version["id"],
                scenario_version_hash=self.scenario_version["content_hash"],
                actor_ref="human:owner", rationale="second estimate",
                version_id="forecast-line-version:acn-manual:2",
                prior_version_ref=None, idempotency_key="manual:3",
            )

    def test_extension_requires_matching_subjects_and_admitted_inputs(self) -> None:
        with self.assertRaises(ModelForecastValidationError):
            extend_growth(
                self.ledger, self.forecast,
                base_input_version_ref=self.growth_version["id"],
                growth_input_version_ref=self.growth_version["id"],
                periods=1, line_ref_prefix="forecast-line:x",
                model_run_ref="model-run:x", idempotency_key="x:1",
            )
        with self.assertRaises(ModelForecastValidationError):
            extend_growth(
                self.ledger, self.forecast,
                base_input_version_ref=self.actual_version["id"],
                growth_input_version_ref=self.actual_version["id"],
                periods=1, line_ref_prefix="forecast-line:x",
                model_run_ref="model-run:x", idempotency_key="x:2",
            )


if __name__ == "__main__":
    unittest.main()
