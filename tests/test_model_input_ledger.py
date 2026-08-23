import copy
import sqlite3
import unittest

from dalton_core.model_input import (
    ModelInputConflict,
    ModelInputLedger,
    ModelInputNotFound,
    ModelInputValidationError,
    RECONCILIATION_CHECKS,
)
from dalton_core.store import DaltonStore, content_hash


NOW = "2026-08-23T12:00:00+00:00"
PERIOD = {
    "start": "2026-01-01",
    "end": "2026-03-31",
    "calendar": "company:fiscal",
    "kind": "quarter",
}
FORECAST_PERIOD = {
    "start": "2027-01-01",
    "end": "2027-03-31",
    "calendar": "company:fiscal",
    "kind": "forecast_period",
}


class ModelInputLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.ledger = ModelInputLedger(self.store)
        self.addCleanup(self.store.close)
        self.evidence = self.store.register_evidence({
            "evidence_ref": "evidence:acn:10q:revenue",
            "source_type": "filing",
            "source_ref": "sec:acn:10q:2026q1",
            "retrieved_at": NOW,
            "source_lineage": ["sec:acn:10q:2026q1"],
            "independence_group": "sec:acn:10q:2026q1",
            "actor_ref": "system:sec-adapter",
        })

    def source(self) -> list[dict]:
        return [{
            "authority_kind": "evidence_version",
            "version_ref": self.evidence["evidence_version_id"],
            "content_hash": self.evidence["content_hash"],
        }]

    def scenario_payload(self, ref: str = "scenario:base") -> dict:
        return {
            "schema_version": "0.1",
            "scenario_ref": ref,
            "label": "Base",
            "description": "Human-reviewed base case",
            "base_scenario_version_ref": None,
            "base_scenario_version_hash": None,
            "owner_ref": "human:pm",
        }

    def actual_payload(
        self, *, value: str = "100", period: dict | None = None,
        metric_ref: str = "metric:revenue",
    ) -> dict:
        return {
            "schema_version": "0.1",
            "metric_ref": metric_ref,
            "subject_ref": "company:acn",
            "business_line_ref": None,
            "period": copy.deepcopy(period or PERIOD),
            "unit": "million",
            "currency": "USD",
            "value": value,
            "source_authorities": self.source(),
        }

    def admit(
        self, kind: str, ref: str, payload: dict, *, suffix: str,
        prior: dict | None = None,
    ) -> dict:
        candidate = self.ledger.propose_input(
            candidate_id=f"candidate:{suffix}",
            input_kind=kind,
            model_input_ref=ref,
            prior_version_ref=None if prior is None else prior["id"],
            payload=payload,
            proposed_by="agent:researcher",
            idempotency_key=f"propose:{suffix}",
        )["candidate"]
        return self.ledger.decide_input(
            decision_id=f"decision:{suffix}",
            candidate_id=candidate["id"],
            candidate_hash=candidate["content_hash"],
            verdict="admit",
            rationale="Reviewed against source and modeling policy",
            findings=[],
            reviewer_ref="human:pm",
            version_id=f"input-version:{suffix}",
            idempotency_key=f"decide:{suffix}",
        )["version"]

    def base_inputs(self) -> tuple[dict, dict, dict, dict]:
        scenario = self.admit(
            "scenario", "scenario:base", self.scenario_payload(), suffix="scenario-base"
        )
        actual = self.admit(
            "actual", "input:acn:revenue:2026q1", self.actual_payload(), suffix="actual"
        )
        assumption = self.admit(
            "assumption", "input:acn:organic-growth:2027q1",
            {
                "schema_version": "0.1",
                "driver_ref": "driver:organic-growth",
                "subject_ref": "company:acn",
                "effective_period": copy.deepcopy(FORECAST_PERIOD),
                "unit": "percent",
                "currency": None,
                "value": "6.5",
                "formula": None,
                "scenario_version_ref": scenario["id"],
                "scenario_version_hash": scenario["content_hash"],
                "owner_ref": "human:analyst",
                "rationale": "Demand and booking conversion judgment",
                "provenance": "judgment",
                "source_authorities": [],
                "dependency_bindings": [],
            },
            suffix="assumption",
        )
        forecast = self.admit(
            "forecast_line", "input:acn:revenue:2027q1",
            {
                "schema_version": "0.1",
                "metric_ref": "metric:revenue",
                "subject_ref": "company:acn",
                "business_line_ref": None,
                "forecast_period": copy.deepcopy(FORECAST_PERIOD),
                "unit": "million",
                "currency": "USD",
                "value": None,
                "formula": "prior_revenue * (1 + growth)",
                "scenario_version_ref": scenario["id"],
                "scenario_version_hash": scenario["content_hash"],
                "historical_actual_bindings": [{
                    "binding_ref": "history:revenue",
                    "role": "historical_actual",
                    "version_ref": actual["id"],
                    "version_hash": actual["content_hash"],
                }],
                "dependency_bindings": [{
                    "binding_ref": "driver:growth",
                    "role": "growth_assumption",
                    "version_ref": assumption["id"],
                    "version_hash": assumption["content_hash"],
                }],
            },
            suffix="forecast",
        )
        return scenario, actual, assumption, forecast

    def test_human_gate_idempotency_and_immutable_history(self) -> None:
        proposed = self.ledger.propose_input(
            candidate_id="candidate:actual",
            input_kind="actual",
            model_input_ref="input:acn:revenue:2026q1",
            prior_version_ref=None,
            payload=self.actual_payload(),
            proposed_by="agent:researcher",
            idempotency_key="propose:actual",
        )
        replay = self.ledger.propose_input(
            candidate_id="candidate:actual",
            input_kind="actual",
            model_input_ref="input:acn:revenue:2026q1",
            prior_version_ref=None,
            payload=self.actual_payload(),
            proposed_by="agent:researcher",
            idempotency_key="propose:actual",
        )
        self.assertEqual("duplicate", replay["status"])
        with self.assertRaises(ModelInputValidationError):
            self.ledger.decide_input(
                decision_id="decision:auto", candidate_id="candidate:actual",
                candidate_hash=proposed["candidate"]["content_hash"], verdict="admit",
                rationale="automated", findings=[], reviewer_ref="agent:auto",
                version_id="input-version:auto", idempotency_key="decision:auto",
            )
        decided = self.ledger.decide_input(
            decision_id="decision:actual", candidate_id="candidate:actual",
            candidate_hash=proposed["candidate"]["content_hash"], verdict="admit",
            rationale="source checked", findings=[], reviewer_ref="human:pm",
            version_id="input-version:actual", idempotency_key="decision:actual",
        )
        self.assertEqual(1, decided["version"]["version"])
        self.assertEqual(decided["version"], self.ledger.current_input(
            "input:acn:revenue:2026q1"
        ))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE model_input_versions SET actor_ref='tampered'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "INSERT INTO model_input_candidates(candidate_id,input_kind,model_input_ref,"
                "payload_json,payload_hash,proposed_by,record_json,content_hash,created_at) "
                "VALUES('direct','actual','direct','{}','x','x','{}','x','x')"
            )
        self.assertTrue(self.ledger.integrity_report()["ok"])

    def test_closed_payload_and_exact_source_authority_fail_closed(self) -> None:
        no_source = self.actual_payload()
        no_source["source_authorities"] = []
        with self.assertRaises(ModelInputValidationError):
            self.ledger.propose_input(
                candidate_id="candidate:no-source", input_kind="actual",
                model_input_ref="input:no-source", prior_version_ref=None,
                payload=no_source, proposed_by="agent:x", idempotency_key="no-source",
            )
        wrong_hash = self.actual_payload()
        wrong_hash["source_authorities"][0]["content_hash"] = "0" * 64
        with self.assertRaises(ModelInputConflict):
            self.ledger.propose_input(
                candidate_id="candidate:wrong-hash", input_kind="actual",
                model_input_ref="input:wrong-hash", prior_version_ref=None,
                payload=wrong_hash, proposed_by="agent:x", idempotency_key="wrong-hash",
            )
        missing = self.actual_payload()
        missing["source_authorities"][0]["version_ref"] = "evidence-version:missing"
        with self.assertRaises(ModelInputNotFound):
            self.ledger.propose_input(
                candidate_id="candidate:missing", input_kind="actual",
                model_input_ref="input:missing", prior_version_ref=None,
                payload=missing, proposed_by="agent:x", idempotency_key="missing",
            )
        open_payload = self.actual_payload()
        open_payload["surprise"] = True
        with self.assertRaises(ModelInputValidationError):
            self.ledger.propose_input(
                candidate_id="candidate:open", input_kind="actual",
                model_input_ref="input:open", prior_version_ref=None,
                payload=open_payload, proposed_by="agent:x", idempotency_key="open",
            )

    def test_forecast_run_and_reconciliation_are_version_bound(self) -> None:
        scenario, actual, assumption, forecast = self.base_inputs()
        run = self.ledger.record_model_run(
            version_id="model-run-version:acn:base:1",
            model_run_ref="model-run:acn:base",
            prior_version_ref=None,
            scenario_version_ref=scenario["id"],
            scenario_version_hash=scenario["content_hash"],
            input_bindings=[
                {
                    "binding_ref": "actual:revenue", "role": "historical_actual",
                    "version_ref": actual["id"], "version_hash": actual["content_hash"],
                },
                {
                    "binding_ref": "assumption:growth", "role": "assumption",
                    "version_ref": assumption["id"],
                    "version_hash": assumption["content_hash"],
                },
                {
                    "binding_ref": "forecast:revenue", "role": "forecast_line",
                    "version_ref": forecast["id"], "version_hash": forecast["content_hash"],
                },
            ],
            formula_version_ref="formula-set:us-it-services:1",
            formula_version_hash=content_hash({"formula": "revenue"}),
            status="completed",
            outputs=[{
                "output_ref": "output:revenue:2027q1",
                "output_kind": "metric",
                "metric_ref": "metric:revenue",
                "period": copy.deepcopy(FORECAST_PERIOD),
                "unit": "million",
                "currency": "USD",
                "value": "106.5",
                "authority_bindings": [],
            }],
            errors=[], started_at=NOW, completed_at="2026-08-23T12:00:01+00:00",
            actor_ref="system:model-runner", idempotency_key="model-run:1",
        )["model_run"]
        replay = self.ledger.record_model_run(
            version_id="model-run-version:acn:base:1",
            model_run_ref="model-run:acn:base", prior_version_ref=None,
            scenario_version_ref=scenario["id"],
            scenario_version_hash=scenario["content_hash"],
            input_bindings=run["input_bindings"],
            formula_version_ref="formula-set:us-it-services:1",
            formula_version_hash=content_hash({"formula": "revenue"}),
            status="completed", outputs=run["outputs"], errors=[],
            started_at=NOW, completed_at="2026-08-23T12:00:01+00:00",
            actor_ref="system:model-runner", idempotency_key="model-run:1",
        )
        self.assertEqual("duplicate", replay["status"])
        checks = [{
            "check_kind": kind,
            "status": "pass",
            "details": f"{kind} checked",
            "authority_bindings": [{
                "authority_kind": "model_run_version",
                "version_ref": run["id"],
                "content_hash": run["content_hash"],
            }],
        } for kind in RECONCILIATION_CHECKS]
        reconciliation = self.ledger.record_reconciliation(
            reconciliation_id="reconciliation:acn:base:1",
            model_run_version_ref=run["id"],
            model_run_version_hash=run["content_hash"],
            checks=checks, actor_ref="human:analyst",
            idempotency_key="reconciliation:1",
        )["reconciliation"]
        self.assertEqual("pass", reconciliation["verdict"])
        self.assertEqual([reconciliation], self.ledger.reconciliations(run["id"]))
        report = self.ledger.integrity_report()
        self.assertTrue(report["ok"], report)
        self.assertEqual(4, report["counts"]["model_input_versions"])
        self.assertEqual(1, report["counts"]["model_run_versions"])

    def test_stale_candidate_and_superseded_run_reconciliation_fail_closed(self) -> None:
        initial = self.admit(
            "actual", "input:acn:revenue:2026q1", self.actual_payload(), suffix="actual-v1"
        )
        stale = self.ledger.propose_input(
            candidate_id="candidate:stale", input_kind="actual",
            model_input_ref="input:acn:revenue:2026q1",
            prior_version_ref=initial["id"], payload=self.actual_payload(value="101"),
            proposed_by="agent:a", idempotency_key="propose:stale",
        )["candidate"]
        current = self.admit(
            "actual", "input:acn:revenue:2026q1", self.actual_payload(value="102"),
            suffix="actual-v2", prior=initial,
        )
        self.assertEqual(2, current["version"])
        with self.assertRaises(ModelInputConflict):
            self.ledger.decide_input(
                decision_id="decision:stale", candidate_id=stale["id"],
                candidate_hash=stale["content_hash"], verdict="admit",
                rationale="late", findings=[], reviewer_ref="human:pm",
                version_id="input-version:stale", idempotency_key="decide:stale",
            )

    def test_valuation_output_requires_all_five_actual_authorities(self) -> None:
        scenario = self.admit(
            "scenario", "scenario:base", self.scenario_payload(), suffix="scenario"
        )
        price = self.admit(
            "actual", "input:acn:price", self.actual_payload(metric_ref="metric:price"),
            suffix="price",
        )
        binding = {
            "binding_ref": "market:price", "role": "price",
            "version_ref": price["id"], "version_hash": price["content_hash"],
        }
        with self.assertRaises(ModelInputConflict):
            self.ledger.record_model_run(
                version_id="run:valuation:1", model_run_ref="run:valuation",
                prior_version_ref=None, scenario_version_ref=scenario["id"],
                scenario_version_hash=scenario["content_hash"],
                input_bindings=[binding], formula_version_ref="formula:valuation:1",
                formula_version_hash=content_hash({"formula": "price"}),
                status="completed", outputs=[{
                    "output_ref": "valuation:acn", "output_kind": "valuation",
                    "metric_ref": "metric:equity-value", "period": copy.deepcopy(PERIOD),
                    "unit": "million", "currency": "USD", "value": "1000",
                    "authority_bindings": [{"role": "price", "binding_ref": "market:price"}],
                }], errors=[], started_at=NOW,
                completed_at="2026-08-23T12:00:01+00:00",
                actor_ref="system:model-runner", idempotency_key="run:valuation",
            )

    def test_model_run_requires_transitive_input_closure(self) -> None:
        scenario, actual, _assumption, forecast = self.base_inputs()
        with self.assertRaises(ModelInputConflict):
            self.ledger.record_model_run(
                version_id="run:open-inputs:1", model_run_ref="run:open-inputs",
                prior_version_ref=None, scenario_version_ref=scenario["id"],
                scenario_version_hash=scenario["content_hash"],
                input_bindings=[{
                    "binding_ref": "forecast", "role": "forecast_line",
                    "version_ref": forecast["id"],
                    "version_hash": forecast["content_hash"],
                }, {
                    "binding_ref": "actual", "role": "historical_actual",
                    "version_ref": actual["id"],
                    "version_hash": actual["content_hash"],
                }],
                formula_version_ref="formula:open:1",
                formula_version_hash=content_hash({"formula": "open"}),
                status="completed", outputs=[{
                    "output_ref": "output:open", "output_kind": "metric",
                    "metric_ref": "metric:revenue", "period": copy.deepcopy(FORECAST_PERIOD),
                    "unit": "million", "currency": "USD", "value": "106.5",
                    "authority_bindings": [],
                }], errors=[], started_at=NOW,
                completed_at="2026-08-23T12:00:01+00:00",
                actor_ref="system:model-runner", idempotency_key="run:open-inputs",
            )

    def test_source_revision_must_fail_reconciliation(self) -> None:
        scenario = self.admit(
            "scenario", "scenario:base", self.scenario_payload(), suffix="scenario-source"
        )
        actual = self.admit(
            "actual", "input:acn:revenue:2026q1", self.actual_payload(),
            suffix="actual-source",
        )
        run = self.ledger.record_model_run(
            version_id="run:source:1", model_run_ref="run:source", prior_version_ref=None,
            scenario_version_ref=scenario["id"],
            scenario_version_hash=scenario["content_hash"],
            input_bindings=[{
                "binding_ref": "actual", "role": "historical_actual",
                "version_ref": actual["id"], "version_hash": actual["content_hash"],
            }], formula_version_ref="formula:source:1",
            formula_version_hash=content_hash({"formula": "source"}),
            status="completed", outputs=[{
                "output_ref": "output:source", "output_kind": "metric",
                "metric_ref": "metric:revenue", "period": copy.deepcopy(PERIOD),
                "unit": "million", "currency": "USD", "value": "100",
                "authority_bindings": [],
            }], errors=[], started_at=NOW,
            completed_at="2026-08-23T12:00:01+00:00",
            actor_ref="system:model-runner", idempotency_key="run:source",
        )["model_run"]
        self.store.register_evidence({
            "evidence_ref": "evidence:acn:10q:revenue",
            "source_type": "filing",
            "source_ref": "sec:acn:10q:2026q1-amended",
            "retrieved_at": "2026-08-23T13:00:00+00:00",
            "source_lineage": ["sec:acn:10q:2026q1-amended"],
            "independence_group": "sec:acn:10q:2026q1",
            "actor_ref": "system:sec-adapter",
        })
        checks = [{
            "check_kind": kind, "status": "pass", "details": "checked",
            "authority_bindings": [{
                "authority_kind": "model_run_version", "version_ref": run["id"],
                "content_hash": run["content_hash"],
            }],
        } for kind in RECONCILIATION_CHECKS]
        with self.assertRaises(ModelInputConflict):
            self.ledger.record_reconciliation(
                reconciliation_id="reconciliation:source:bad",
                model_run_version_ref=run["id"],
                model_run_version_hash=run["content_hash"], checks=checks,
                actor_ref="system:reconciler", idempotency_key="reconciliation:source:bad",
            )
        next(item for item in checks if item["check_kind"] == "source_revision")[
            "status"
        ] = "fail"
        result = self.ledger.record_reconciliation(
            reconciliation_id="reconciliation:source:good",
            model_run_version_ref=run["id"],
            model_run_version_hash=run["content_hash"], checks=checks,
            actor_ref="system:reconciler", idempotency_key="reconciliation:source:good",
        )
        self.assertEqual("fail", result["reconciliation"]["verdict"])

    def test_failed_run_can_only_reconcile_fail(self) -> None:
        scenario = self.admit(
            "scenario", "scenario:base", self.scenario_payload(), suffix="scenario-failed"
        )
        run = self.ledger.record_model_run(
            version_id="run:failed:1", model_run_ref="run:failed", prior_version_ref=None,
            scenario_version_ref=scenario["id"],
            scenario_version_hash=scenario["content_hash"],
            input_bindings=[{
                "binding_ref": "scenario", "role": "scenario",
                "version_ref": scenario["id"], "version_hash": scenario["content_hash"],
            }],
            formula_version_ref="formula:failed:1",
            formula_version_hash=content_hash({"formula": "bad"}),
            status="failed", outputs=[], errors=["division by zero"],
            started_at=NOW, completed_at="2026-08-23T12:00:01+00:00",
            actor_ref="system:model-runner", idempotency_key="run:failed",
        )["model_run"]
        checks = [{
            "check_kind": kind,
            "status": "not_applicable",
            "details": "run failed before output",
            "authority_bindings": [{
                "authority_kind": "model_run_version",
                "version_ref": run["id"],
                "content_hash": run["content_hash"],
            }],
        } for kind in RECONCILIATION_CHECKS]
        result = self.ledger.record_reconciliation(
            reconciliation_id="reconciliation:failed:1",
            model_run_version_ref=run["id"],
            model_run_version_hash=run["content_hash"], checks=checks,
            actor_ref="system:reconciler", idempotency_key="reconciliation:failed",
        )
        self.assertEqual("fail", result["reconciliation"]["verdict"])


if __name__ == "__main__":
    unittest.main()
