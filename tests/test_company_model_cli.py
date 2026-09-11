"""P13am: decide, store, go quiet.

This file exists for one bug. The lane picked a company using a projection
built without its ticker, while the run stored its specification against a
projection built *with* it. The ticker is part of the state and therefore part
of its hash, so the selector could never see the answer it had just produced.

It cost nothing -- the child found the stored specification and replayed it for
free -- but it cost progress: the lane relaunched one company every tick while
the other four waited behind it forever. It reached production and was caught
by reading a heartbeat, not by a test, because every test had one side of the
projection or the other and never both.

So the invariant here is end to end and deliberately blunt: after a
specification is stored, there is nothing left to decide.
"""

from __future__ import annotations

import json
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.company_model_cli import (
    _validated_spec_with_repair, choose_company, filed_classifications,
    model_spec_request_id, run_model_spec, structured_output_repair_config,
)
from dalton_core.company_model_forecast_cli import run_model_forecast
from dalton_core.company_model_spec import TASK_HASH, spec_from_response
from dalton_core.company_dossier import CompanyDossierAuthority
from dalton_core.company_model_state import build_company_model_state
from dalton_core.coverage_mission import CoverageMissionAuthority, CoverageMissionConflict
from dalton_core.store import DaltonStore
from dalton_core.store import canonical_json, content_hash
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_company_dossier import body as dossier_body, classification

ACN = "company:sec-cik:0001467373"


def _spec_body():
    return {
        "schema_version": "0.4",
        "revenue_anchor_concept": "us-gaap:Revenues",
        "assessment": "A people business: billable heads times realised rate.",
        "revenue_drivers": [{
            "ref": "heads", "label": "Billable headcount", "kind": "volume",
            "basis_concept": None, "unit": "headcount",
            "because": "Capacity is the binding constraint on delivery revenue.",
        }],
        "expense_lines": [{
            "ref": "delivery", "label": "Cost of services",
            "basis_concept": None,
            "behaviour": "variable_with_headcount", "driver_ref": "heads",
            "because": "Delivery payroll follows the billable base.",
        }],
        "forecast_statements": [
            {"statement": "income", "importance": "required",
             "because": "Revenue and margin are the question."},
            {"statement": "balance", "importance": "supporting",
             "because": "Capital light."},
            {"statement": "cash", "importance": "required",
             "because": "Free cash flow funds the buyback."},
        ],
        "operating_metrics": [],
        "horizon": {"historical_quarters": 12, "forecast_quarters": 8,
                    "because": "Three years spans the cycle."},
        "financial_statement_structure": {
            "schema_version": "0.1",
            "lines": [{
                "ref": "revenue", "role": "revenue", "label": "Revenue",
                "kind": "filed", "concept": "us-gaap:Revenues",
                "statement": "income", "unit": "usd",
                "period_kind": "duration", "annual_semantics": "sum_quarters",
                "forecast_method": "quarterly_growth", "forecast_base_ref": None,
            }, {
                "ref": "pretax", "role": "pretax_income", "label": "Pretax",
                "kind": "filed", "concept": "us-gaap:IncomeBeforeTax",
                "statement": "income", "unit": "usd", "period_kind": "duration",
                "annual_semantics": "sum_quarters", "forecast_method": "unavailable",
                "forecast_base_ref": None,
            }, {
                "ref": "tax", "role": "income_tax_expense", "label": "Tax",
                "kind": "filed", "concept": "us-gaap:IncomeTaxExpenseBenefit",
                "statement": "income", "unit": "usd", "period_kind": "duration",
                "annual_semantics": "sum_quarters", "forecast_method": "unavailable",
                "forecast_base_ref": None,
            }, {
                "ref": "net", "role": "net_income", "label": "Net income",
                "kind": "derived", "concept": None, "statement": "income",
                "unit": "usd", "period_kind": "duration",
                "annual_semantics": "sum_quarters", "forecast_method": "formula",
                "forecast_base_ref": None,
            }],
            "formulas": [{
                "output_ref": "net", "operator": "sum",
                "terms": [
                    {"line_ref": "pretax", "coefficient": "1"},
                    {"line_ref": "tax", "coefficient": "-1"},
                ],
                "tie_out_concept": "us-gaap:NetIncomeLoss",
                "evidence_refs": ["0001467373-26-000031"],
            }],
        },
        "cash_flow_companion": {
            "schema_version": "0.1",
            "lines": [
                {"role": "operating_cash_flow", "concept": None,
                 "forecast_method": "unavailable", "forecast_base_ref": None,
                 "because": "No exact cash source is present in this fixture."},
                {"role": "capital_expenditure", "concept": None,
                 "forecast_method": "unavailable", "forecast_base_ref": None,
                 "because": "No exact cash source is present in this fixture."},
            ],
            "formula": {"output_ref": "free_cash_flow", "operator": "sum",
                        "terms": [
                            {"role": "operating_cash_flow", "coefficient": "1"},
                            {"role": "capital_expenditure", "coefficient": "-1"},
                        ]},
        },
    }


class ChooseCompanyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state_dir = Path(self._dir.name)
        self.store = DaltonStore(str(self.state_dir / "core.sqlite"))
        self.addCleanup(self.store.close)
        state = bootstrap_method_authorities(self.store)
        self.missions = CoverageMissionAuthority(self.store)
        params = mission_params(state)
        self.mission = self.missions.create_mission(params.pop("mission_ref"), **params)
        authorization = self.missions.authorize_sec_lane(
            company_ref=ACN, ticker="ACN", actor_ref="automation:coverage-mission",
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"])
        dispatch = self.missions.queue_statement_dispatch(authorization=authorization)
        self.missions.mark_statement_dispatch_launched(
            dispatch["dispatch_id"], "sec-financials-run:" + "1" * 24)
        self.missions.record_statement_observation(
            dispatch_id=dispatch["dispatch_id"],
            observation={
                "schema_version": "0.1", "cik": "0001467373",
                "entity_name": "Accenture plc",
                "filings": [{
                    "accession": "0001467373-26-000031", "form": "10-Q",
                    "filed": "2026-06-25", "report_date": "2026-06-30",
                    "lines": [{
                        "statement": "income", "concept": "us-gaap:Revenues",
                        "label": "Revenues", "level": 0, "parent_concept": None,
                        "is_breakdown": False, "dimension_axis": None,
                        "dimension_member": None, "period_start": "2026-04-01",
                        "period_end": "2026-06-30", "value": "17700000000",
                        "unit": "USD", "balance": "credit",
                    }, {
                        "statement": "income", "concept": "us-gaap:IncomeBeforeTax",
                        "label": "Income before tax", "level": 0,
                        "parent_concept": "us-gaap:NetIncomeLoss",
                        "is_breakdown": False, "dimension_axis": None,
                        "dimension_member": None, "period_start": "2026-04-01",
                        "period_end": "2026-06-30", "value": "100",
                        "unit": "USD", "balance": "credit",
                    }, {
                        "statement": "income", "concept": "us-gaap:IncomeTaxExpenseBenefit",
                        "label": "Income tax", "level": 1,
                        "parent_concept": "us-gaap:NetIncomeLoss",
                        "is_breakdown": False, "dimension_axis": None,
                        "dimension_member": None, "period_start": "2026-04-01",
                        "period_end": "2026-06-30", "value": "20",
                        "unit": "USD", "balance": "debit",
                    }, {
                        "statement": "income", "concept": "us-gaap:NetIncomeLoss",
                        "label": "Net income", "level": 0, "parent_concept": None,
                        "is_breakdown": False, "dimension_axis": None,
                        "dimension_member": None, "period_start": "2026-04-01",
                        "period_end": "2026-06-30", "value": "80",
                        "unit": "USD", "balance": "credit",
                    }],
                }],
                "source_record_refs": ["raw-sink:" + "c" * 64],
                "next_cursor": None, "provider_status": 200,
            },
            governance_ref="g", governance_hash="b" * 64)
        self.missions.settle_statement_dispatch(
            dispatch["dispatch_id"], outcome="succeeded")

    def test_the_chosen_state_is_the_one_a_specification_is_stored_against(self):
        company_ref, state = choose_company(self.missions, self.mission)
        self.assertEqual(company_ref, ACN)
        # The projection carries the ticker, so its hash is the ticker's hash.
        self.assertEqual(state["ticker"], "ACN")
        self.assertEqual(
            state["state_hash"],
            build_company_model_state(self.missions, ACN, ticker="ACN")["state_hash"])

    def test_once_a_specification_is_stored_there_is_nothing_left_to_decide(self):
        _, state = choose_company(self.missions, self.mission)
        self.missions.record_company_model_spec(
            spec_from_response(state, _spec_body(), decided_by="automation:x"),
            mission_version_ref=self.mission["id"])
        self.assertEqual(choose_company(self.missions, self.mission), (None, None))

    def test_a_new_spec_contract_reopens_the_same_filed_state(self):
        _, state = choose_company(self.missions, self.mission)
        old = spec_from_response(state, _spec_body(), decided_by="automation:x")
        old = {**old, "task_hash": "d" * 64}
        old.pop("content_hash")
        old["content_hash"] = content_hash(old)
        self.missions.record_company_model_spec(
            old, mission_version_ref=self.mission["id"])
        company_ref, reopened = choose_company(self.missions, self.mission)
        self.assertEqual(company_ref, ACN)
        self.assertEqual(reopened["state_hash"], state["state_hash"])

    def test_a_legacy_spec_remains_byte_readable_when_the_new_contract_reopens(self):
        _, state = choose_company(self.missions, self.mission)
        current = spec_from_response(state, _spec_body(), decided_by="automation:x")
        legacy = {
            key: value for key, value in current.items()
            if key not in {"financial_statement_structure", "cash_flow_companion",
                           "content_hash"}
        }
        legacy.update({"schema_version": "0.2", "task_hash": "d" * 64})
        legacy["content_hash"] = content_hash(legacy)
        self.missions.record_company_model_spec(
            legacy, mission_version_ref=self.mission["id"])
        before = canonical_json(
            self.missions.company_model_spec_for_state(
                ACN, state["state_hash"], task_hash=legacy["task_hash"],
            )
        )

        company_ref, reopened = choose_company(self.missions, self.mission)

        self.assertEqual(company_ref, ACN)
        self.assertEqual(reopened["state_hash"], state["state_hash"])
        self.assertEqual(
            canonical_json(self.missions.company_model_specs(ACN)[0]), before,
        )

    def test_validated_persistence_refuses_a_different_candidate_at_the_same_id(self):
        _, state = choose_company(self.missions, self.mission)
        first = spec_from_response(state, _spec_body(), decided_by="automation:x")
        original = self.missions.record_company_model_spec(
            first, mission_version_ref=self.mission["id"])
        changed_body = copy.deepcopy(_spec_body())
        changed_body["assessment"] = "A different but schema-valid assessment."
        changed = spec_from_response(
            state, changed_body, decided_by="automation:x",
        )

        with self.assertRaisesRegex(
            CoverageMissionConflict, "differs from stored authority",
        ):
            self.missions.record_validated_company_model_spec(
                changed, mission_version_ref=self.mission["id"],
                work_order_ref="work:different-candidate",
            )

        replayed = self.missions.company_model_specs(ACN)
        self.assertEqual(len(replayed), 1)
        self.assertEqual(replayed[0]["content_hash"], original["content_hash"])

    def test_scheduler_identity_changes_with_the_spec_contract(self):
        state_hash = "a" * 64
        self.assertEqual(model_spec_request_id(state_hash, "b" * 64),
                         model_spec_request_id(state_hash, "b" * 64))
        self.assertNotEqual(model_spec_request_id(state_hash, "b" * 64),
                            model_spec_request_id(state_hash, "c" * 64))
        disabled = {"max_attempts": 0}
        enabled = {"max_attempts": 1}
        self.assertNotEqual(
            model_spec_request_id(state_hash, "b" * 64, repair_config=disabled),
            model_spec_request_id(state_hash, "b" * 64, repair_config=enabled),
        )

    def test_a_named_company_is_used_as_given(self):
        company_ref, state = choose_company(
            self.missions, self.mission, company_ref=ACN)
        self.assertEqual(company_ref, ACN)
        self.assertEqual(state["ticker"], "ACN")

    def test_a_company_with_no_statements_is_skipped_not_chosen(self):
        stranger = "company:sec-cik:0009999999"
        mission = {**self.mission, "universe": [
            {"company_ref": stranger, "ticker": "ZZZ"}]}
        self.assertEqual(choose_company(self.missions, mission), (None, None))

    def test_a_dry_run_reports_the_same_state_the_lane_would_launch_for(self):
        _, state = choose_company(self.missions, self.mission)
        summary = run_model_spec(
            state_dir=self.state_dir, model_config_path=None,
            summary_dir=self.state_dir / "summary", scheduler_db=None,
            dry_run=True)
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["spec_status"], "gated")
        self.assertEqual(summary["state_hash"], state["state_hash"])
        self.assertEqual(summary["company_ref"], ACN)
        self.assertEqual(summary["formal_authority_writes"], 0)
        written = json.loads(
            (self.state_dir / "summary" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(written["state_hash"], state["state_hash"])

    def test_ticket_input_drift_is_typed_and_stops_before_a_model_call(self):
        _, state = choose_company(self.missions, self.mission)
        config = self.state_dir / "model.json"
        config.write_text("{}", encoding="utf-8")
        summary = run_model_spec(
            state_dir=self.state_dir, model_config_path=config,
            summary_dir=self.state_dir / "stale-summary", scheduler_db=None,
            company_ref=ACN, expected_state_hash="f" * 64,
            expected_task_hash="e" * 64,
        )
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["spec_status"], "stale_input")
        self.assertEqual(summary["state_hash"], state["state_hash"])
        self.assertEqual(summary["cost_micros"], 0)
        self.assertEqual(summary["formal_authority_writes"], 0)

    def test_exact_ticket_input_reaches_the_normal_dry_run_path(self):
        _, state = choose_company(self.missions, self.mission)
        from dalton_core.company_model_spec import TASK_HASH
        summary = run_model_spec(
            state_dir=self.state_dir, model_config_path=None,
            summary_dir=self.state_dir / "exact-summary", scheduler_db=None,
            company_ref=ACN, expected_state_hash=state["state_hash"],
            expected_task_hash=TASK_HASH, dry_run=True,
        )
        self.assertEqual(summary["spec_status"], "gated")

    def test_an_overbudget_fixed_prompt_is_reported_without_being_skipped(self):
        config = self.state_dir / "too-small-model.json"
        config.write_text(json.dumps({
            "purpose_call_budgets": {
                "model_spec": {"max_input_tokens": 1_000},
            },
        }), encoding="utf-8")
        summary = run_model_spec(
            state_dir=self.state_dir, model_config_path=config,
            summary_dir=self.state_dir / "too-small-summary",
            scheduler_db=None, dry_run=True,
        )
        self.assertEqual(summary["spec_status"], "refused")
        self.assertEqual(summary["prompt_budget_report"]["prompt_byte_limit"], 1_000)
        self.assertGreater(
            summary["prompt_budget_report"]["base_prompt_bytes"], 1_000,
        )
        self.assertEqual(self.missions.company_model_specs(ACN), [])

    def test_dry_run_rebuilds_the_state_with_the_configured_numeric_bounds(self):
        policy = {"max_periods_per_series": 1, "max_total_cells": 2}
        prompt_byte_limit = 90_000
        config = self.state_dir / "numeric-context-model.json"
        config.write_text(json.dumps({
            "model_spec_numeric_context": policy,
            "purpose_call_budgets": {
                "model_spec": {"max_input_tokens": prompt_byte_limit},
            },
        }), encoding="utf-8")
        expected = build_company_model_state(
            self.missions, ACN, ticker="ACN", numeric_context_policy=policy,
            prompt_byte_limit=prompt_byte_limit,
        )

        summary = run_model_spec(
            state_dir=self.state_dir, model_config_path=config,
            summary_dir=self.state_dir / "numeric-context-summary",
            scheduler_db=None, company_ref=ACN,
            expected_state_hash=expected["state_hash"],
            expected_task_hash=TASK_HASH, dry_run=True,
        )

        self.assertEqual(summary["spec_status"], "gated")
        self.assertEqual(summary["state_hash"], expected["state_hash"])
        self.assertEqual(summary["numeric_context_policy"], policy)
        self.assertEqual(summary["prompt_byte_limit"], prompt_byte_limit)
        self.assertLessEqual(summary["prompt_bytes"], prompt_byte_limit)
        self.assertEqual(
            summary["numeric_context_hash"],
            expected["numeric_context"]["content_hash"],
        )

    def test_filed_classification_selects_the_same_state_the_child_rebuilds(self):
        dossiers = CompanyDossierAuthority(self.store)
        dossiers.publish(dossier_body(
            drafted_sections={},
            classification_block=classification(
                "claim-version:classification",
                word="contract_compounder"),
            evidence=[{
                "kind": "claim", "ref": "claim-version:classification",
                "text": "合同期限为五年", "period": "2026Q2",
            }],
        ))
        classifications = filed_classifications(self.store)
        company_ref, selected = choose_company(
            self.missions, self.mission, classifications=classifications)
        self.assertEqual(company_ref, ACN)
        self.assertEqual(selected["industry_classification"],
                         "contract_compounder")
        summary = run_model_spec(
            state_dir=self.state_dir, model_config_path=None,
            summary_dir=self.state_dir / "classified-summary", scheduler_db=None,
            company_ref=ACN, expected_state_hash=selected["state_hash"],
            expected_task_hash=TASK_HASH,
            dry_run=True,
        )
        self.assertEqual(summary["spec_status"], "gated")
        self.assertEqual(summary["state_hash"], selected["state_hash"])

    def test_a_run_with_nothing_to_decide_says_so_rather_than_paying(self):
        _, state = choose_company(self.missions, self.mission)
        self.missions.record_company_model_spec(
            spec_from_response(state, _spec_body(), decided_by="automation:x"),
            mission_version_ref=self.mission["id"])
        summary = run_model_spec(
            state_dir=self.state_dir, model_config_path=Path("/nonexistent.json"),
            summary_dir=self.state_dir / "summary", scheduler_db=None)
        self.assertEqual(summary["status"], "idle")
        self.assertEqual(summary["spec_status"], "nothing_to_decide")
        self.assertEqual(summary["cost_micros"], 0)

    def test_full_run_repairs_then_stores_the_accepted_work_and_total_cost(self):
        _, state = choose_company(self.missions, self.mission)
        original = _spec_body()
        original["assessment"] = "x" * 1201
        repaired = copy.deepcopy(original)
        repaired["assessment"] = "x" * 1199
        calls = [
            {
                "text": json.dumps(original), "work_order_ref": "work:original",
                "work_order_hash": "1" * 64,
                "result_envelope_ref": "result:original",
                "result_envelope_hash": "2" * 64,
                "invocation_ref": "invocation:original",
                "route_decision_ref": "route:original", "cost_micros": 11,
                "replayed": False,
            },
            {
                "text": json.dumps(repaired), "work_order_ref": "work:repair",
                "work_order_hash": "3" * 64,
                "result_envelope_ref": "result:repair",
                "result_envelope_hash": "4" * 64,
                "invocation_ref": "invocation:repair",
                "route_decision_ref": "route:repair", "cost_micros": 13,
                "replayed": False,
            },
        ]

        class Model:
            def __init__(inner, config, **kwargs):
                inner.config = config
                inner.requests = []

            def call(inner, **kwargs):
                inner.requests.append(kwargs)
                return calls.pop(0)

        config = self.state_dir / "model.json"
        config.write_text(json.dumps({
            "structured_output_repair": {"max_attempts": 1}
        }), encoding="utf-8")
        with patch("dalton_core.company_model_cli.CockpitModel", Model):
            summary = run_model_spec(
                state_dir=self.state_dir, model_config_path=config,
                summary_dir=self.state_dir / "repair-summary",
                scheduler_db=self.state_dir / "scheduler.sqlite",
                company_ref=ACN, expected_state_hash=state["state_hash"],
                expected_task_hash=TASK_HASH,
                expected_repair_policy_hash=content_hash({"max_attempts": 1}),
            )
        self.assertEqual(summary["spec_status"], "fresh")
        self.assertEqual(summary["pre_persistence_validation"], "ready")
        self.assertEqual(len(summary["financial_input_hash"]), 64)
        self.assertTrue(summary["financial_structure_ref"].startswith(
            "financial-statement-structure:"))
        self.assertEqual(len(summary["financial_structure_hash"]), 64)
        self.assertEqual(summary["cost_micros"], 24)
        self.assertEqual(summary["repair_attempts"][0]["work_order_ref"],
                         "work:repair")
        stored = self.missions.company_model_specs(ACN)[-1]
        self.assertEqual(stored["work_order_ref"], "work:repair")
        forecast = run_model_forecast(
            state_dir=self.state_dir,
            summary_dir=self.state_dir / "forecast-summary",
        )
        self.assertEqual(forecast["status"], "succeeded")
        self.assertEqual(forecast["forecast_status"], "published")
        self.assertEqual(forecast["spec_ref"], stored["spec_id"])

    def test_schema_valid_false_historical_tie_is_refused_before_persistence(self):
        _, state = choose_company(self.missions, self.mission)
        body = copy.deepcopy(_spec_body())
        # Structurally legal and accepted by the presentation-only validator,
        # but 100 + 20 cannot tie to the filed net-income value of 80.
        body["financial_statement_structure"]["formulas"][0]["terms"][1][
            "coefficient"
        ] = "1"
        call = {
            "text": json.dumps(body), "work_order_ref": "work:false-tie",
            "work_order_hash": "1" * 64,
            "result_envelope_ref": "result:false-tie",
            "result_envelope_hash": "2" * 64,
            "invocation_ref": "invocation:false-tie",
            "route_decision_ref": "route:false-tie", "cost_micros": 11,
            "replayed": False,
        }
        model_calls = []

        class Model:
            def __init__(inner, config, **kwargs):
                inner.config = config

            def call(inner, **kwargs):
                model_calls.append(kwargs)
                return call

        config = self.state_dir / "false-tie-model.json"
        config.write_text(json.dumps({
            "structured_output_repair": {"max_attempts": 1}
        }), encoding="utf-8")
        with patch("dalton_core.company_model_cli.CockpitModel", Model):
            summary = run_model_spec(
                state_dir=self.state_dir, model_config_path=config,
                summary_dir=self.state_dir / "false-tie-summary",
                scheduler_db=self.state_dir / "scheduler.sqlite",
                company_ref=ACN, expected_state_hash=state["state_hash"],
                expected_task_hash=TASK_HASH,
                expected_repair_policy_hash=content_hash({"max_attempts": 1}),
            )
        self.assertEqual(summary["spec_status"], "refused")
        self.assertEqual(summary["pre_persistence_validation"], "refused")
        self.assertIn("does not tie to filed history", summary["failure_reason"])
        self.assertEqual(len(model_calls), 1)
        self.assertEqual(self.missions.company_model_specs(ACN), [])
        self.assertEqual(choose_company(self.missions, self.mission)[0], ACN)


class StructuredOutputRepairTests(unittest.TestCase):
    def call(self, text, suffix="0", cost=0):
        return {
            "text": text,
            "work_order_ref": "work:" + suffix,
            "work_order_hash": ("a" if suffix == "0" else "b") * 64,
            "result_envelope_ref": "result:" + suffix,
            "result_envelope_hash": ("c" if suffix == "0" else "d") * 64,
            "invocation_ref": "invocation:" + suffix,
            "route_decision_ref": "route:" + suffix,
            "cost_micros": cost,
            "replayed": False,
        }

    def state(self):
        return {
            "company_ref": ACN,
            "state_hash": "a" * 64,
            "filings": [{"accession": "0001467373-26-000031"}],
            "concepts": ["us-gaap:Revenues", "us-gaap:IncomeBeforeTax",
                         "us-gaap:IncomeTaxExpenseBenefit", "us-gaap:NetIncomeLoss"],
            "statements": {"income": [{
                "concept": "us-gaap:Revenues", "level": 0,
                "parent_concept": None, "is_breakdown": False,
                "dimension_axis": None, "unit": "USD", "period_kind": "duration",
            }, {
                "concept": "us-gaap:IncomeBeforeTax", "level": 0,
                "parent_concept": "us-gaap:NetIncomeLoss", "is_breakdown": False,
                "dimension_axis": None, "unit": "USD", "period_kind": "duration",
            }, {
                "concept": "us-gaap:IncomeTaxExpenseBenefit", "level": 1,
                "parent_concept": "us-gaap:NetIncomeLoss", "is_breakdown": False,
                "dimension_axis": None, "unit": "USD", "period_kind": "duration",
            }, {
                "concept": "us-gaap:NetIncomeLoss", "level": 0,
                "parent_concept": None, "is_breakdown": False,
                "dimension_axis": None, "unit": "USD", "period_kind": "duration",
            }]},
        }

    def mission(self):
        return {"autonomy": {"automation_principal": "automation:test"}}

    def test_text_length_repair_preserves_every_other_semantic_node(self):
        original = _spec_body()
        original["assessment"] = "x" * 1201
        repaired = copy.deepcopy(original)
        repaired["assessment"] = "x" * 1199

        class Model:
            def __init__(self, answer):
                self.answer = answer
                self.calls = []

            def call(inner, **kwargs):
                inner.calls.append(kwargs)
                return self.call(json.dumps(inner.answer), "1", 71)

        model = Model(repaired)
        spec, attempts, accepted = _validated_spec_with_repair(
            model=model, state=self.state(), mission=self.mission(),
            original_call=self.call(json.dumps(original)),
            repair_config={"max_attempts": 1}, decided_by="automation:test",
        )
        self.assertEqual(spec["assessment"], repaired["assessment"])
        self.assertEqual(accepted["work_order_ref"], "work:1")
        self.assertEqual(attempts[0]["cost_micros"], 71)
        binding = model.calls[0]["_structured_output_repair"]
        self.assertEqual(binding["root_original"]["work_order_ref"], "work:0")
        self.assertEqual(binding["repair_parent"]["result_envelope_ref"], "result:0")
        self.assertEqual(binding["repair_config"], {"max_attempts": 1})
        self.assertEqual(binding["state_hash"], "a" * 64)
        self.assertEqual(binding["repair_number"], 1)

    def test_semantic_drift_in_a_length_repair_refuses_whole_and_keeps_cost(self):
        original = _spec_body()
        original["assessment"] = "x" * 1201
        hostile = copy.deepcopy(original)
        hostile["assessment"] = "short"
        hostile["horizon"]["forecast_quarters"] = 9
        attempts = []

        class Model:
            def call(inner, **kwargs):
                return self.call(json.dumps(hostile), "1", 91)

        with self.assertRaisesRegex(Exception, "already valid"):
            _validated_spec_with_repair(
                model=Model(), state=self.state(), mission=self.mission(),
                original_call=self.call(json.dumps(original)),
                repair_config={"max_attempts": 1}, decided_by="automation:test",
                repair_attempts=attempts,
            )
        self.assertEqual(attempts[0]["cost_micros"], 91)

    def test_text_length_repair_cannot_exchange_equal_python_scalar_types(self):
        original = _spec_body()
        original["assessment"] = "x" * 1201
        hostile = copy.deepcopy(original)
        hostile["assessment"] = "short"
        hostile["horizon"]["forecast_quarters"] = 8.0

        class Model:
            def call(inner, **kwargs):
                return self.call(json.dumps(hostile), "1", 1)

        with self.assertRaisesRegex(Exception, "already valid"):
            _validated_spec_with_repair(
                model=Model(), state=self.state(), mission=self.mission(),
                original_call=self.call(json.dumps(original)),
                repair_config={"max_attempts": 1}, decided_by="automation:test",
            )

    def test_semantic_failure_never_calls_repair(self):
        semantic = _spec_body()
        semantic["revenue_anchor_concept"] = "us-gaap:NotFiled"

        class Model:
            calls = 0
            def call(inner, **kwargs):
                inner.calls += 1
                raise AssertionError("semantic failures must not call the model")

        model = Model()
        with self.assertRaisesRegex(Exception, "not a concept"):
            _validated_spec_with_repair(
                model=model, state=self.state(), mission=self.mission(),
                original_call=self.call(json.dumps(semantic)),
                repair_config={"max_attempts": 1000}, decided_by="automation:test",
            )
        self.assertEqual(model.calls, 0)

    def test_zero_and_one_attempt_limits_are_exact(self):
        original = _spec_body()
        original["assessment"] = "x" * 1201

        class Model:
            def __init__(inner):
                inner.calls = 0

            def call(inner, **kwargs):
                inner.calls += 1
                return self.call(json.dumps(original), str(inner.calls), 7)

        disabled = Model()
        with self.assertRaisesRegex(Exception, "longer than 1200"):
            _validated_spec_with_repair(
                model=disabled, state=self.state(), mission=self.mission(),
                original_call=self.call(json.dumps(original)),
                repair_config={"max_attempts": 0}, decided_by="automation:test",
            )
        self.assertEqual(disabled.calls, 0)

        one = Model()
        attempts = []
        with self.assertRaisesRegex(Exception, "did not replace"):
            _validated_spec_with_repair(
                model=one, state=self.state(), mission=self.mission(),
                original_call=self.call(json.dumps(original)),
                repair_config={"max_attempts": 1}, decided_by="automation:test",
                repair_attempts=attempts,
            )
        self.assertEqual(one.calls, 1)
        self.assertEqual(len(attempts), 1)

    def test_absent_and_zero_disable_while_large_nonnegative_limits_are_retained(self):
        self.assertEqual(structured_output_repair_config({}), {"max_attempts": 0})
        self.assertEqual(
            structured_output_repair_config({
                "structured_output_repair": {"max_attempts": 1000}
            }),
            {"max_attempts": 1000},
        )


if __name__ == "__main__":
    unittest.main()
