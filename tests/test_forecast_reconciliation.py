"""P9c forecast reconciliation authority tests (no network, no model calls)."""

from __future__ import annotations

import sqlite3
import unittest
from decimal import Decimal, ROUND_HALF_UP

from dalton_core.forecast_reconciliation import (
    AUTOMATION_ACTOR,
    CONTRACT_HASH,
    CONTRACT_REF,
    ForecastReconciliationAuthority,
    ForecastReconciliationConflict,
    ForecastReconciliationValidationError,
    parse_company_facts_claim,
    render_company_facts_statement,
)
from dalton_core.model_forecast import ModelForecastAuthority
from dalton_core.model_input import ModelInputLedger
from dalton_core.store import DaltonStore


SUBJECT = "company:sec-cik:0001467373"
SCENARIO_REF = "input:acn:scenario:base"
OWNER = "human:owner"
CREATED_AT = "2026-08-20T20:00:00+00:00"
PERIOD = {
    "start": "2026-06-01", "end": "2026-08-31",
    "calendar": "company:fiscal", "kind": "quarter",
}
FORECAST = "18933.40265600"


def _invocation(identifier: str) -> dict:
    return {
        "schema_version": "0.1", "id": identifier, "created_at": CREATED_AT,
        "work_order_ref": "work:seed", "profile_ref": "profile:seed",
        "granularity": "task", "capability": "research", "provider": "provider-seed",
        "model": "model-seed", "model_family": "seed", "input_refs": [],
        "output_refs": [], "started_at": CREATED_AT,
        "completed_at": "2026-08-20T20:00:01+00:00", "usage": {"tokens": 1},
        "side_effects": [], "runtime_ref": "runtime:test", "actor_ref": "system:test",
        "parent_ref": None, "environment_hash": "a" * 64,
    }


def growth_percent(current: str, prior: str) -> str:
    value = (Decimal(current) - Decimal(prior)) / Decimal(prior) * Decimal(100)
    text = format(value.quantize(Decimal("0.01"), ROUND_HALF_UP), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def company_facts_statement(*, entity: str, current: str, prior: str, start: str, end: str) -> tuple[str, str]:
    growth = growth_percent(current, prior)
    direction = "down" if growth.startswith("-") else "up"
    statement = render_company_facts_statement(
        entity=entity, label="Revenues", unit="USD", current=current, start=start, end=end,
        direction=direction, growth=growth.lstrip("-"), prior=prior,
    )
    return statement, growth


class ForecastReconciliationFixture:
    """Shared builder: admitted scenario, human forecast lines, frozen SEC-shaped Claims."""

    def __init__(self, store: DaltonStore) -> None:
        self.store = store
        self.ledger = ModelInputLedger(store)
        self.forecast = ModelForecastAuthority(store)
        self.reconciler = ForecastReconciliationAuthority(store)
        self.store.register_invocation(_invocation("invocation:seed-producer"))
        candidate = self.ledger.propose_input(
            candidate_id="candidate:scenario:1", input_kind="scenario",
            model_input_ref=SCENARIO_REF, prior_version_ref=None,
            payload={
                "schema_version": "0.1", "scenario_ref": SCENARIO_REF,
                "label": "Base", "description": "Base scenario",
                "base_scenario_version_ref": None,
                "base_scenario_version_hash": None, "owner_ref": OWNER,
            }, proposed_by="agent:researcher", idempotency_key="candidate:scenario:1",
        )["candidate"]
        self.scenario = self.ledger.decide_input(
            decision_id="decision:scenario:1", candidate_id=candidate["id"],
            candidate_hash=candidate["content_hash"], verdict="admit",
            rationale="isolated test admission", findings=[], reviewer_ref=OWNER,
            version_id="input-version:scenario:1", idempotency_key="decision:scenario:1",
        )["version"]
        self._counter = 0

    def line(
        self, value: str, *, period: dict = PERIOD, line_ref: str = "forecast-line:acn:revenue:q1",
        currency: str = "USD", unit: str = "million", subject: str = SUBJECT,
    ) -> dict:
        prior = self.store.connection.execute(
            "SELECT version_id, version_number FROM model_forecast_line_versions "
            "WHERE line_ref=? ORDER BY version_number DESC LIMIT 1", (line_ref,),
        ).fetchone()
        version = 1 if prior is None else int(prior["version_number"]) + 1
        return self.forecast.publish_line(
            line_ref, subject_ref=subject, metric_or_aspect="metric:revenue-usd",
            period=period, unit=unit, currency=currency, value=value, value_kind="estimate",
            scenario_version_ref=self.scenario["id"],
            scenario_version_hash=self.scenario["content_hash"],
            actor_ref=OWNER, rationale="isolated test estimate",
            version_id=f"forecast-line-version:{line_ref.split('forecast-line:')[-1]}:{version}",
            prior_version_ref=None if prior is None else prior["version_id"],
            idempotency_key=f"publish:{line_ref}:{version}",
        )

    def claim(
        self, *, current: str, prior: str = "17727871000", period: dict = PERIOD,
        subject: str = SUBJECT, statement: str | None = None,
        metric: str = "quarterly_revenue_yoy_growth", supports: bool = True,
    ) -> dict:
        self._counter += 1
        rendered, growth = company_facts_statement(
            entity="Accenture plc", current=current, prior=prior,
            start=period["start"], end=period["end"],
        )
        evidence = self.store.register_evidence({
            "evidence_ref": f"evidence:test:{self._counter}", "source_type": "official_filing",
            "source_ref": "source:sec-edgar", "artifact_refs": ["artifact:test"],
            "source_lineage": ["source:sec-edgar"], "independence_group": "sec-public",
            "actor_ref": "system:test",
        })
        claim = self.store.register_claim({
            "claim_ref": f"claim:test:{self._counter}", "subject_ref": subject,
            "metric_or_aspect": metric, "period": f"{period['start']}..{period['end']}",
            "basis": "official-filing-xbrl",
            "normalized_statement": statement if statement is not None else rendered,
            "claim_kind": "quantitative", "value": float(growth), "unit": "percent",
            "producer_invocation_refs": ["invocation:seed-producer"],
            "actor_ref": "system:research-auto-commit",
        })
        if supports:
            self.store.relate_evidence({
                "id": f"relation:test:{self._counter}",
                "evidence_version_ref": evidence["evidence_version_id"],
                "claim_version_ref": claim["claim_version_id"], "relation": "supports",
            })
        return {**claim, "evidence": evidence}


class ForecastReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.f = ForecastReconciliationFixture(self.store)
        self.reconciler = self.f.reconciler

    def test_parse_round_trips_frozen_statement_only(self) -> None:
        statement, growth = company_facts_statement(
            entity="Accenture plc", current="18718144000", prior="17727871000",
            start="2026-03-01", end="2026-05-31",
        )
        parsed = parse_company_facts_claim({
            "normalized_statement": statement, "claim_kind": "quantitative",
            "unit": "percent", "scale": "one", "period": "2026-03-01..2026-05-31",
            "value": growth,
        })
        self.assertEqual(parsed["current"], "18718144000")
        self.assertEqual(parsed["growth_percent"], "5.59")
        with self.assertRaises(ForecastReconciliationValidationError):
            parse_company_facts_claim({
                "normalized_statement": statement, "claim_kind": "quantitative",
                "unit": "percent", "scale": "one", "period": "2026-03-01..2026-05-31",
                "value": "5.60",  # disagrees with its own statement
            })
        with self.assertRaises(ForecastReconciliationValidationError):
            parse_company_facts_claim({
                "normalized_statement": "Revenue grew nicely.", "claim_kind": "quantitative",
                "unit": "percent", "scale": "one", "period": "x", "value": "1",
            })

    def test_pending_pair_reconciles_deterministically_and_replays(self) -> None:
        line = self.f.line(FORECAST)
        claim = self.f.claim(current="18718144000")
        pairs = self.reconciler.pending_pairs()
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["forecast_line_version_ref"], line["id"])
        self.assertEqual(pairs[0]["claim_version_ref"], claim["claim_version_id"])

        result = self.reconciler.reconcile_pending(requested_by=OWNER, mission_resolver=None)
        self.assertEqual(result["status"], "reconciled", result)
        self.assertEqual(result["skipped"], [])
        record = result["created"][0]
        self.assertEqual(record["status"], "fresh")
        self.assertEqual(record["contract_ref"], CONTRACT_REF)
        self.assertEqual(record["contract_hash"], CONTRACT_HASH)
        self.assertEqual(record["actor_ref"], AUTOMATION_ACTOR)
        self.assertEqual(record["requested_by"], OWNER)
        self.assertIsNone(record["mission_binding"])
        self.assertEqual(record["forecast_value"], FORECAST)
        self.assertEqual(record["actual_value"], "18718.14400000")
        self.assertEqual(record["deviation_absolute"], "-215.25865600")
        self.assertEqual(record["deviation_percent"], "-1.1369")
        self.assertEqual(record["direction"], "below_forecast")
        self.assertEqual(record["band"], "notable")
        self.assertIsNone(record["human_checkpoint"])
        self.assertEqual(record["evidence_version_ref"], claim["evidence"]["evidence_version_id"])
        self.assertEqual(record["forecast_line_version_hash"], line["content_hash"])
        self.assertEqual(record["claim_version_hash"], claim["content_hash"])

        reread = self.reconciler.reconciliation(record["id"])
        self.assertEqual(reread["content_hash"], record["content_hash"])
        self.assertEqual(reread["checkpoint_status"], "not_required")
        self.assertEqual(self.reconciler.pending_pairs(), [])
        replay = self.reconciler.reconcile(
            forecast_line_version_ref=line["id"], claim_version_ref=claim["claim_version_id"],
            requested_by=OWNER, mission_binding=None,
        )
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(replay["content_hash"], record["content_hash"])
        self.assertEqual(self.reconciler.reconcile_pending(
            requested_by=OWNER, mission_resolver=None,
        )["status"], "idle")
        self.assertEqual(
            len(self.reconciler.reconciliations(company_ref=SUBJECT)), 1
        )
        self.assertEqual(self.reconciler.integrity_report()["status"], "ok")

    def test_overturn_candidate_raises_human_checkpoint_and_human_decides(self) -> None:
        self.f.line(FORECAST)
        self.f.claim(current="20000000000")
        record = self.reconciler.reconcile_pending(
            requested_by=OWNER, mission_resolver=None,
        )["created"][0]
        self.assertEqual(record["band"], "overturn_candidate")
        self.assertEqual(record["direction"], "above_forecast")
        self.assertEqual(record["human_checkpoint"], "forecast_overturn")
        self.assertEqual(self.reconciler.checkpoint_status(record["id"]), "pending_human")
        # The forecast line itself is untouched: no new version was published.
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM model_forecast_line_versions"
            ).fetchone()[0], 1,
        )
        with self.assertRaises(ForecastReconciliationConflict):
            self.reconciler.decide_overturn(
                reconciliation_ref=record["id"], reconciliation_hash="0" * 64,
                decision="keep_forecast", rationale="wrong hash", actor_ref=OWNER,
                idempotency_key="decide:bad",
            )
        with self.assertRaises(ForecastReconciliationValidationError):
            self.reconciler.decide_overturn(
                reconciliation_ref=record["id"], reconciliation_hash=record["content_hash"],
                decision="keep_forecast", rationale="automation may not decide",
                actor_ref="automation:coverage-mission", idempotency_key="decide:auto",
            )
        decision = self.reconciler.decide_overturn(
            reconciliation_ref=record["id"], reconciliation_hash=record["content_hash"],
            decision="keep_forecast", rationale="Q4 seasonality already in the assumption.",
            actor_ref=OWNER, idempotency_key="decide:1",
        )
        self.assertEqual(decision["status"], "fresh")
        replay = self.reconciler.decide_overturn(
            reconciliation_ref=record["id"], reconciliation_hash=record["content_hash"],
            decision="keep_forecast", rationale="Q4 seasonality already in the assumption.",
            actor_ref=OWNER, idempotency_key="decide:1",
        )
        self.assertEqual(replay["status"], "duplicate")
        with self.assertRaises(ForecastReconciliationConflict):
            self.reconciler.decide_overturn(
                reconciliation_ref=record["id"], reconciliation_hash=record["content_hash"],
                decision="revise_forecast", rationale="second opinion", actor_ref=OWNER,
                idempotency_key="decide:2",
            )
        self.assertEqual(
            self.reconciler.checkpoint_status(record["id"]), "decided:keep_forecast"
        )
        self.assertEqual(self.reconciler.integrity_report()["decision_count"], 1)

    def test_within_tolerance_band(self) -> None:
        self.f.line(FORECAST)
        self.f.claim(current="18933402656")
        record = self.reconciler.reconcile_pending(
            requested_by=OWNER, mission_resolver=None,
        )["created"][0]
        self.assertEqual(record["band"], "within_tolerance")
        self.assertEqual(record["deviation_percent"], "0.0000")
        self.assertEqual(record["direction"], "in_line")

    def test_automation_requires_mission_grant_and_reports_skips(self) -> None:
        line = self.f.line(FORECAST)
        claim = self.f.claim(current="18718144000")
        with self.assertRaises(ForecastReconciliationValidationError):
            self.reconciler.reconcile(
                forecast_line_version_ref=line["id"], claim_version_ref=claim["claim_version_id"],
                requested_by="automation:coverage-mission", mission_binding=None,
            )
        with self.assertRaises(ForecastReconciliationValidationError):
            self.reconciler.reconcile_pending(
                requested_by="automation:coverage-mission", mission_resolver=None,
            )

        def denied(company_ref: str) -> dict:
            raise RuntimeError(f"mission does not grant forecast_reconciliation for {company_ref}")

        result = self.reconciler.reconcile_pending(
            requested_by="automation:coverage-mission", mission_resolver=denied,
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["created"], [])
        self.assertIn("does not grant", result["skipped"][0]["reason"])
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM forecast_reconciliations").fetchone()[0],
            0,
        )

        def granted(company_ref: str) -> dict:
            return {
                "mission_version_ref": "coverage-mission-version:us-it-services:2",
                "mission_version_hash": "b" * 64,
                "mission_ref": "coverage-mission:us-it-services",
                "actor_ref": "automation:coverage-mission",
                "company_ref": company_ref,
                "scope": "forecast_reconciliation",
            }

        result = self.reconciler.reconcile_pending(
            requested_by="automation:coverage-mission", mission_resolver=granted,
        )
        self.assertEqual(result["status"], "reconciled")
        record = result["created"][0]
        self.assertEqual(record["requested_by"], "automation:coverage-mission")
        self.assertEqual(record["mission_binding"]["mission_version_hash"], "b" * 64)
        self.assertEqual(record["actor_ref"], AUTOMATION_ACTOR)

    def test_non_template_claim_is_skipped_not_guessed(self) -> None:
        self.f.line(FORECAST)
        self.f.claim(current="18718144000", statement="Accenture revenue was about USD 18.7bn.")
        result = self.reconciler.reconcile_pending(requested_by=OWNER, mission_resolver=None)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("frozen SEC company-facts template", result["skipped"][0]["reason"])

    def test_missing_supporting_evidence_fails_closed(self) -> None:
        self.f.line(FORECAST)
        self.f.claim(current="18718144000", supports=False)
        result = self.reconciler.reconcile_pending(requested_by=OWNER, mission_resolver=None)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("no supporting EvidenceVersion", result["skipped"][0]["reason"])

    def test_currency_mismatch_fails_closed(self) -> None:
        self.f.line(FORECAST, currency="EUR")
        self.f.claim(current="18718144000")
        result = self.reconciler.reconcile_pending(requested_by=OWNER, mission_resolver=None)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("currency", result["skipped"][0]["reason"])

    def test_only_latest_line_version_is_paired(self) -> None:
        first = self.f.line(FORECAST)
        second = self.f.line("19000")
        self.f.claim(current="18718144000")
        pairs = self.reconciler.pending_pairs()
        self.assertEqual([pair["forecast_line_version_ref"] for pair in pairs], [second["id"]])
        with self.assertRaises(ForecastReconciliationConflict):
            self.reconciler.reconcile(
                forecast_line_version_ref=first["id"],
                claim_version_ref=pairs[0]["claim_version_ref"],
                requested_by=OWNER, mission_binding=None,
            )

    def test_other_periods_and_subjects_do_not_pair(self) -> None:
        self.f.line(FORECAST)
        self.f.claim(current="18718144000", period={
            "start": "2026-03-01", "end": "2026-05-31",
            "calendar": "company:fiscal", "kind": "quarter",
        })
        self.f.claim(current="18718144000", subject="company:sec-cik:0001058290")
        self.assertEqual(self.reconciler.pending_pairs(), [])

    def test_sql_guards_block_direct_writes(self) -> None:
        self.f.line(FORECAST)
        self.f.claim(current="18718144000")
        record = self.reconciler.reconcile_pending(
            requested_by=OWNER, mission_resolver=None,
        )["created"][0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "INSERT INTO forecast_reconciliations(reconciliation_id,subject_ref,metric_ref,"
                "period_start,period_end,forecast_line_ref,forecast_line_version_ref,"
                "forecast_line_version_hash,claim_version_ref,claim_version_hash,band,"
                "human_checkpoint,mission_version_ref,requested_by,actor_ref,record_json,"
                "content_hash,created_at) VALUES('x','s','m','a','b','l','lv','h','c','ch',"
                "'notable',NULL,NULL,'human:x','automation:forecast-reconciler','{}','h','t')"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE forecast_reconciliations SET band='within_tolerance' WHERE reconciliation_id=?",
                (record["id"],),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM forecast_reconciliations WHERE reconciliation_id=?", (record["id"],)
            )


if __name__ == "__main__":
    unittest.main()
