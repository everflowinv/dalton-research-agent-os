from __future__ import annotations

import unittest
from unittest.mock import patch

from dalton_core.forecast_sensitivity import (
    BAND_RULE_REF,
    SELECTION_RULE_HASH,
    SELECTION_RULE_REF,
)
from dalton_core.mission_model_stage_lane import advance_once
from dalton_core.model_stage_readiness import (
    company_model_readiness,
    industry_model_readiness,
)


class FakeMissions:
    def __init__(self, state):
        self.state = state
        self.writes = []

    def current_stage_state(self, mission_ref, company_ref):
        return dict(self.state[company_ref])

    def record_stage(self, **values):
        self.writes.append(values)
        if values["status"] == "entered":
            self.state[values["company_ref"]].update(
                current_stage=values["stage_ref"], next_stage=values["stage_ref"])
        elif values["status"] == "gate_passed":
            nxt = {"industry_model": "company_model",
                   "company_model": "investment_memo"}[values["stage_ref"]]
            self.state[values["company_ref"]].update(
                current_stage=values["stage_ref"], current_status="gate_passed",
                next_stage=nxt)
        return {"id": f"stage:{len(self.writes)}", **values}


PRICE_REF = "market-price-series-version:a"
VALUATION = {"id": "valuation-snapshot-version:a", "company_ref": "company:a",
             "price": {"version_ref": PRICE_REF},
             "metrics": [{"metric": "trailing_pe", "status": "available"}]}


def mission(version="mission:v14"):
    return {
        "id": version, "mission_ref": "mission:coverage", "content_hash": "a" * 64,
        "industry_ref": "industry:it", "universe": [{"company_ref": "company:a"}],
        "autonomy": {"automation_principal": "automation:mission",
                     "may_write": ["stage_record"]},
    }


class ModelStageReadinessTests(unittest.TestCase):
    def test_industry_requires_each_explicit_exit_input(self):
        source = {"ref": "claim:1"}
        framework = {
            "id": "framework:v1", "evidence_refs": ["claim:1"],
            "sections": [{"status": "drafted", "sources": [source]}],
            "industry_characteristics": {"status": "drafted", "sources": [source]},
            "long_term_drivers": {"status": "drafted", "sources": [source]},
            "short_term_drivers": {"status": "drafted", "sources": [source]},
            "cross_company_comparison": {"status": "computed",
                "companies": [{"company_ref": "company:a"}], "cells": [{"value": "1"}]},
            "gaps": [{"gap_ref": "gap:high-frequency-demand", "status": "open",
                      "candidate_sources": [{"source_ref": "source:web"}]}],
        }
        result = industry_model_readiness(framework, mission=mission())
        self.assertFalse(result["passed"])
        self.assertIn("five_company_revenue_growth_margin_comparison", result["reasons"])
        self.assertIn("comparison_limits_explained", result["reasons"])
        framework["cross_company_comparison"]["cells"] = []
        result = industry_model_readiness(framework)
        self.assertFalse(result["passed"])
        self.assertIn("five_company_revenue_growth_margin_comparison", result["reasons"])

    def test_company_requires_history_assumption_refs_and_quantified_bridge(self):
        model = {
            "id": "model:v1", "history_periods": [{}], "forecast_periods": [{}],
            "drivers": [{"ref": "driver:revenue", "history": [{}], "role": "revenue",
                         "status": "forecastable"}],
            "assumptions": [{"driver_ref": "driver:revenue", "kind": "estimate",
                             "because": "filing and guidance", "refs": [{"ref": "claim:1"}]}],
            "results": [{"ref": "result:revenue", "status": "computed", "cells": []}],
        }
        sensitivity = {
            "id": "sensitivity:v1", "selection": {"status": "selected"},
            "impact_metric": {"result_ref": "result:revenue"}, "horizon": [{}],
            "history_window": {"quarters": 1},
            "drivers": [{"driver_ref": "driver:revenue", "band": {"status": "available"},
                         "what_if": [{"lines": [{"cells": [{"status": "computed"}]}]}]}],
            "consensus_bridge": {"status": "available", "metrics": [{}]},
        }
        result = company_model_readiness(
            model, sensitivity, mission=mission(), company_ref="company:a")
        self.assertFalse(result["passed"])
        self.assertIn("three_to_five_key_drivers", result["reasons"])
        self.assertIn("two_year_filings_reconciled_zero_error", result["reasons"])
        self.assertIn("two_year_filings_reconciled_zero_error", result["reasons"])
        model["assumptions"][0]["refs"] = []
        self.assertIn("forecast_assumptions_explicit",
                      company_model_readiness(model, sensitivity)["reasons"])

    def test_stale_sensitivity_cannot_pass_a_newer_model(self):
        model = {"id": "forecast-model-version:company-a:2", "content_hash": "b" * 64,
                 "company_ref": "company:a", "mission_version_ref": "mission:v14",
                 "history_periods": [{}] * 8, "forecast_periods": [{}],
                 "drivers": [{"ref": f"driver:{n}", "history": [{}], "role": "revenue",
                              "status": "forecastable"} for n in range(3)],
                 "assumptions": [{"driver_ref": f"driver:{n}", "because": "filed",
                                  "refs": [{"ref": "claim:1"}]} for n in range(3)],
                 "results": [{"ref": "result:revenue", "status": "computed"}]}
        sensitivity = {"id": "sensitivity:v1", "company_ref": "company:a",
                       "mission_version_ref": "mission:v14",
                       "model_version_ref": "forecast-model-version:company-a:1",
                       "model_version_hash": "a" * 64,
                       "selection": {"status": "selected"}, "drivers": [],
                       "consensus_bridge": {"status": "available", "metrics": [{}]}}
        result = company_model_readiness(
            model, sensitivity, mission=mission(), company_ref="company:a")
        self.assertFalse(result["passed"])
        self.assertIn("active_mission_model_sensitivity_binding", result["reasons"])

    def test_unselected_estimated_driver_does_not_fail_filed_history_check(self):
        model = {
            "id": "forecast-model-version:company-a:1", "content_hash": "b" * 64,
            "company_ref": "company:a", "mission_version_ref": "mission:v14",
            "history_periods": [{}] * 8, "forecast_periods": [{}],
            "drivers": [
                {"ref": f"driver:{n}", "history": [{}] * 8, "role": "revenue",
                 "status": "filed"} for n in range(3)
            ] + [{"ref": "driver:qualitative-volume", "history": [], "role": None,
                  "status": "estimated"}],
            "assumptions": [
                {"driver_ref": f"driver:{n}", "because": "filed history",
                 "refs": [{"ref": f"filing:{n}"}]} for n in range(3)
            ],
            "results": [{"ref": "result:revenue", "status": "computed"}],
        }
        sensitivity = {
            "id": "sensitivity:v1", "company_ref": "company:a",
            "mission_version_ref": "mission:v14",
            "model_version_ref": model["id"], "model_version_hash": model["content_hash"],
            "selection_rule_ref": SELECTION_RULE_REF,
            "selection_rule_hash": SELECTION_RULE_HASH,
            "band_rule_ref": BAND_RULE_REF,
            "selection": {"status": "available"}, "impact_metric": {
                "result_ref": "result:revenue"}, "horizon": [{}],
            "history_window": {"quarters": 8},
            "drivers": [
                {"driver_ref": f"driver:{n}", "band": {"status": "available"},
                 "what_if": [{"lines": [{"cells": [{"status": "computed"}]}]}]}
                for n in range(3)
            ],
            "consensus_bridge": {"status": "available", "metrics": [{}]},
        }
        filing_proof = {
            "model_version_ref": model["id"],
            "model_content_hash": model["content_hash"],
            "invariant_report": {"status": "available"},
        }

        current = {"valuation": VALUATION, "current_price_ref": PRICE_REF,
                   "latest_filed_period": "2026-06-30"}
        model["history_periods"] = [f"2024-{n:02d}-30" for n in range(3, 10)] + ["2026-06-30"]
        result = company_model_readiness(
            model, sensitivity, mission=mission(), company_ref="company:a",
            filing_proof=filing_proof, **current)

        self.assertTrue(result["passed"], result)

        for changed in (
            {"band_rule_ref": "rule:historical-band:1"},
            {"selection_rule_ref": "rule:swing-rank:0"},
            {"selection_rule_hash": "a" * 64},
        ):
            legacy = {**sensitivity, **changed}
            waiting = company_model_readiness(
                model, legacy, mission=mission(), company_ref="company:a",
                filing_proof=filing_proof, **current)
            self.assertFalse(waiting["passed"], changed)
            self.assertIn("current_sensitivity_rule_binding", waiting["reasons"])

    def test_baseline_cadence_is_not_completed_calendar_proof(self):
        source = {"ref": "claim:1", "period": "2026-09-10"}
        block = {"status": "drafted", "sources": [source]}
        framework = {
            "id": "framework:v1", "industry_ref": "industry:it",
            "evidence_refs": ["claim:1"], "sections": [block],
            "industry_characteristics": block, "long_term_drivers": block,
            "short_term_drivers": block,
            "bindings": {"mission_version_ref": "mission:v14"},
            "cross_company_comparison": {
                "status": "computed", "comparability_notes": ["gap basis is filed"],
                "companies": [{"company_ref": "company:a"},
                              {"company_ref": "company:b"}],
                "cells": [{"company_ref": "company:a", "status": "computed"}]},
            "gaps": [{"gap_ref": "gap:high-frequency-demand", "status": "covered",
                      "candidate_sources": [{"slug": "market_price",
                                             "connection_status": "connected"}]}],
        }
        result = industry_model_readiness(
            framework, mission=mission(), cadence_source_keys=frozenset({"market_price"}))
        self.assertFalse(result["passed"])
        self.assertIn("five_company_revenue_growth_margin_comparison", result["reasons"])

    def test_waiting_company_does_not_starve_later_ready_company(self):
        stages = {
            "company:a": {"current_stage": "industry_model",
                          "current_status": "entered", "next_stage": "industry_model"},
            "company:b": {"current_stage": "industry_model",
                          "current_status": "entered", "next_stage": "industry_model"},
        }
        authority = FakeMissions(stages)
        value = mission()
        value["universe"].append({"company_ref": "company:b"})
        held = {"passed": False, "checks": [], "reasons": ["missing"],
                "evidence_refs": []}
        passed = {"passed": True, "checks": [], "reasons": [],
                  "evidence_refs": ["framework:v1"]}
        with patch("dalton_core.mission_model_stage_lane._evaluate",
                   side_effect=[held, passed, held]):
            result = advance_once(authority, object(), value)
        self.assertEqual(result["status"], "advanced")
        self.assertEqual(result["company_ref"], "company:b")

    def test_accessions_do_not_prove_wrong_history_reconciles(self):
        model = {"id": "model:v1", "content_hash": "b" * 64,
                 "company_ref": "company:a", "mission_version_ref": "mission:v14",
                 "history_periods": [{}] * 8, "forecast_periods": [{}],
                 "drivers": [{"ref": f"driver:{n}", "history": [
                     {"value": "999999", "accessions": ["0001"]}],
                     "role": "revenue", "status": "forecastable"} for n in range(3)],
                 "assumptions": [{"driver_ref": "driver:0", "because": "filed",
                                  "refs": [{"ref": "claim:1"}]}],
                 "results": [{"ref": "result:revenue", "status": "computed"}]}
        sensitivity = {"id": "s:v1", "company_ref": "company:a",
                       "mission_version_ref": "mission:v14", "model_version_ref": "model:v1",
                       "model_version_hash": "b" * 64, "selection": {"status": "selected"},
                       "drivers": [], "consensus_bridge": {"status": "available", "metrics": [{}]}}
        result = company_model_readiness(
            model, sensitivity, mission=mission(), company_ref="company:a")
        self.assertIn("two_year_filings_reconciled_zero_error", result["reasons"])

    def test_unrelated_peer_table_is_not_peer_sensitivity(self):
        model = {"id": "model:v1", "content_hash": "b" * 64,
                 "company_ref": "company:a", "mission_version_ref": "mission:v14",
                 "history_periods": [{}] * 8, "forecast_periods": [{}],
                 "drivers": [{"ref": f"driver:{n}", "history": [{}], "role": "revenue",
                              "status": "forecastable"} for n in range(3)],
                 "assumptions": [{"driver_ref": "driver:0", "because": "filed",
                                  "refs": [{"ref": "claim:1"}]}],
                 "results": [{"ref": "result:revenue", "status": "computed"}]}
        sensitivity = {"id": "s:v1", "company_ref": "company:a",
                       "mission_version_ref": "mission:v14", "model_version_ref": "model:v1",
                       "model_version_hash": "b" * 64, "selection": {"status": "selected"},
                       "drivers": [], "consensus_bridge": {"status": "available", "metrics": [{}]}}
        peers = {"status": "computed", "companies": [{"company_ref": "company:b"},
                                                       {"company_ref": "company:c"}],
                 "cells": [{"company_ref": "company:b", "status": "computed"}]}
        result = company_model_readiness(
            model, sensitivity, mission=mission(), company_ref="company:a",
            peer_comparison=peers)
        self.assertNotIn("peer_relative_sensitivity_quantified", result["reasons"])

    def test_signed_execution_scope_allows_explicit_unconnected_industry_gaps(self):
        source = {"ref": "claim:1", "period": "2026-Q2"}
        block = {"status": "drafted", "sources": [source]}
        framework = {
            "id": "framework:v1", "industry_ref": "industry:it",
            "evidence_refs": ["claim:1"], "sections": [block],
            "industry_characteristics": block, "long_term_drivers": block,
            "short_term_drivers": block,
            "bindings": {"mission_version_ref": "mission:v14"},
            "cross_company_comparison": {
                "status": "computed", "comparability_notes": ["filed basis"],
                "companies": [{"company_ref": "company:a"}],
                "cells": [{"company_ref": "company:a", "metric": metric,
                           "status": "computed"} for metric in
                          ("revenue", "revenue_yoy_growth", "gross_margin")]},
            "gaps": [{"gap_ref": "gap:high-frequency-demand", "status": "open",
                      "candidate_sources": [{"source_ref": "source:not-connected"}]},
                     {"gap_ref": "gap:demand-tam", "status": "open",
                      "candidate_sources": [{"source_ref": "source:not-connected"}]}],
        }
        self.assertTrue(industry_model_readiness(
            framework, mission=mission())["passed"])
        framework["cross_company_comparison"]["cells"] = [
            row for row in framework["cross_company_comparison"]["cells"]
            if row["metric"] != "gross_margin"]
        refused = industry_model_readiness(framework, mission=mission())
        self.assertFalse(refused["passed"])
        self.assertIn("five_company_revenue_growth_margin_comparison",
                      refused["reasons"])

    def test_waiting_readiness_recovers_without_terminal_gate_failure(self):
        stages = {"company:a": {"current_stage": "deep_insight_gate",
                                "current_status": "gate_passed",
                                "next_stage": "industry_model"}}
        authority = FakeMissions(stages)
        held = {"passed": False, "checks": [], "reasons": ["missing"],
                "evidence_refs": []}
        passed = {"passed": True, "checks": [], "reasons": [],
                  "evidence_refs": ["framework:v1"]}
        with patch("dalton_core.mission_model_stage_lane._evaluate", return_value=held):
            first = advance_once(authority, object(), mission())
        self.assertEqual(first["status"], "waiting")
        self.assertEqual([x["status"] for x in authority.writes], ["entered"])
        with patch("dalton_core.mission_model_stage_lane._evaluate",
                   side_effect=[passed, held]):
            second = advance_once(authority, object(), mission("mission:v15"))
        self.assertEqual(second["status"], "advanced")
        # ADR-0011: a company that passes one gate is evaluated against the
        # next in the same tick, so it enters company_model at once.
        self.assertEqual([(x["stage_ref"], x["status"]) for x in authority.writes],
                         [("industry_model", "entered"), ("industry_model", "gate_passed"),
                          ("company_model", "entered")])
        self.assertEqual(authority.writes[1]["mission_version_ref"], "mission:v15")

    def test_never_skips_deep_insight_human_checkpoint(self):
        authority = FakeMissions({"company:a": {"current_stage": "initial_screen",
            "current_status": "gate_passed", "next_stage": "deep_insight_gate"}})
        result = advance_once(authority, object(), mission())
        self.assertEqual(result["status"], "idle")
        self.assertEqual(authority.writes, [])


def framework_for(companies, version="mission:v14"):
    source = {"ref": "claim:1", "period": "2026-Q2"}
    block = {"status": "drafted", "sources": [source]}
    cells = [{"company_ref": ref, "metric": metric, "quarter": f"2026Q{q}",
              "status": "computed"}
             for ref in companies for q in range(1, 5)
             for metric in ("revenue", "revenue_yoy_growth", "gross_margin")]
    return {
        "id": "framework:v1", "industry_ref": "industry:it",
        "evidence_refs": ["claim:1"], "sections": [block],
        "industry_characteristics": block, "long_term_drivers": block,
        "short_term_drivers": block, "bindings": {"mission_version_ref": version},
        "cross_company_comparison": {
            "status": "computed", "comparability_notes": ["filed basis"],
            "companies": [{"company_ref": ref} for ref in companies], "cells": cells},
        "gaps": [{"gap_ref": "gap:high-frequency-demand", "status": "open",
                  "candidate_sources": [{"source_ref": "source:web"}]}],
    }


def passing_company_inputs():
    model = {"id": "forecast-model-version:a:1", "content_hash": "b" * 64,
             "company_ref": "company:a", "mission_version_ref": "mission:v12",
             "inputs_hash": "c" * 64,
             "history_periods": [f"2024-{m:02d}-30" for m in range(3, 10)] + ["2026-06-30"],
             "forecast_periods": [{}],
             "drivers": [{"ref": f"driver:{n}", "history": [{}] * 8, "role": "revenue",
                          "status": "filed"} for n in range(3)],
             "assumptions": [{"driver_ref": f"driver:{n}", "because": "filed",
                              "refs": [{"ref": f"filing:{n}"}]} for n in range(3)],
             "results": [{"ref": "result:revenue", "role": "revenue", "status": "computed"}]}
    sensitivity = {
        "id": "sensitivity:v1", "company_ref": "company:a",
        "mission_version_ref": "mission:v14", "inputs_hash": "c" * 64,
        "model_version_ref": model["id"], "model_version_hash": model["content_hash"],
        "selection_rule_ref": SELECTION_RULE_REF, "selection_rule_hash": SELECTION_RULE_HASH,
        "band_rule_ref": BAND_RULE_REF, "selection": {"status": "available"},
        "impact_metric": {"result_ref": "result:revenue"}, "horizon": [{}],
        "history_window": {"quarters": 8},
        "drivers": [{"driver_ref": f"driver:{n}", "band": {"status": "available"},
                     "what_if": [{"lines": [{"cells": [{"status": "computed"}]}]}]}
                    for n in range(3)],
        "consensus_bridge": {"status": "available", "metrics": [{}]},
    }
    proof = {"model_version_ref": model["id"], "model_content_hash": model["content_hash"],
             "invariant_report": {"status": "available"}, "replays": False,
             "current_invariant_report": {"status": "available"}}
    return model, sensitivity, proof


class AchievableModelGateTests(unittest.TestCase):
    """ADR-0011: the gates pass on what the lanes produce, and say why not."""

    def test_industry_gate_belongs_to_the_mission_not_one_version(self):
        framework = framework_for(["company:a"], version="mission:v12")
        calendar = {"id": "catalyst-calendar-version:a", "company_ref": "company:a"}
        refused = industry_model_readiness(
            framework, mission=mission(), company_ref="company:a", update_calendar=calendar)
        self.assertIn("active_mission_binding", refused["reasons"])
        passed = industry_model_readiness(
            framework, mission=mission(), company_ref="company:a", update_calendar=calendar,
            mission_version_refs={"mission:v12", "mission:v14"})
        self.assertTrue(passed["passed"], passed)
        self.assertEqual(passed["evidence_refs"], ["framework:v1", calendar["id"]])

    def test_industry_gate_counts_its_metric_base_and_needs_an_update_calendar(self):
        framework = framework_for(["company:a"])
        framework["cross_company_comparison"]["cells"] = [
            cell for cell in framework["cross_company_comparison"]["cells"]
            if not (cell["metric"] == "gross_margin" and cell["quarter"] == "2026Q1")]
        result = industry_model_readiness(
            framework, mission=mission(), company_ref="company:a", update_calendar=None)
        self.assertEqual(set(result["reasons"]),
                         {"company_update_calendar_bound", "stage_metric_base_held"})
        base = next(row for row in result["checks"]
                    if row["criterion"] == "stage_metric_base_held")
        self.assertEqual(base["missing"], [{"metric_ref": "metric:gross-margin",
                                            "have": 3, "periods": 4}])
        self.assertIn("这家公司还没有更新日历（催化剂日历）", result["reason_labels"])

    def test_company_gate_passes_on_a_rechecked_proof_and_a_current_valuation(self):
        model, sensitivity, proof = passing_company_inputs()
        current = {"valuation": VALUATION, "current_price_ref": PRICE_REF,
                   "latest_filed_period": "2026-06-30",
                   "mission_version_refs": {"mission:v12", "mission:v14"}}
        result = company_model_readiness(model, sensitivity, mission=mission(),
                                         company_ref="company:a", filing_proof=proof,
                                         **current)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["evidence_refs"],
                         [model["id"], sensitivity["id"], VALUATION["id"]])

    def test_company_gate_names_each_missing_criterion(self):
        model, sensitivity, proof = passing_company_inputs()
        cases = (
            ({"current_price_ref": "market-price-series-version:newer"},
             "valuation_snapshot_current", "估值快照没有按最新股价重算"),
            ({"latest_filed_period": "2026-09-30"},
             "model_covers_latest_filing", "模型没有包含最新一期申报"),
            ({"proof": {**proof, "current_invariant_report": {"status": "unavailable"}}},
             "two_year_filings_reconciled_zero_error", "历史数与申报文件对不上"),
            ({"sensitivity": {**sensitivity, "inputs_hash": "d" * 64}},
             "active_mission_model_sensitivity_binding", "敏感性表与当前模型不对应"),
        )
        for change, criterion, label in cases:
            values = {"valuation": VALUATION, "current_price_ref": PRICE_REF,
                      "latest_filed_period": "2026-06-30",
                      "mission_version_refs": {"mission:v12", "mission:v14"}}
            values.update({key: value for key, value in change.items()
                           if key not in {"proof", "sensitivity"}})
            result = company_model_readiness(
                model, change.get("sensitivity", sensitivity), mission=mission(),
                company_ref="company:a", filing_proof=change.get("proof", proof), **values)
            self.assertEqual(result["reasons"], [criterion], change)
            self.assertEqual(result["reason_labels"], [label])

    def test_industry_gate_no_longer_reads_the_repository_tracking_policy(self):
        # The live lane waited on every company from 09-30 because the packaged
        # module looked for deploy/phase9/p14a-tracking-policy-v2.json beside
        # site-packages, raised, and reported "waiting" with no reason.
        from dalton_core import mission_model_stage_lane as lane

        framework = framework_for(["company:a"])
        calendar = {"id": "catalyst-calendar-version:a", "company_ref": "company:a"}
        with patch.object(lane, "_latest", return_value=framework), \
             patch.object(lane, "_latest_version", return_value=calendar), \
             patch.object(lane, "mission_version_refs", return_value=frozenset({"mission:v14"})), \
             patch("dalton_core.tracking_cadence.load_policy",
                   side_effect=AssertionError("policy must not be read")):
            verdict = lane._evaluate(object(), mission(), "company:a", "industry_model")
        self.assertTrue(verdict["passed"], verdict)


class StageLedgerTickTests(unittest.TestCase):
    def gate_core(self, bound="mission:v14"):
        import json
        import sqlite3

        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE deep_insight_gate_versions(version_id TEXT, "
                           "company_ref TEXT, version_number INTEGER, record_json TEXT)")
        connection.execute("INSERT INTO deep_insight_gate_versions VALUES(?,?,?,?)", (
            "deep-insight-gate-version:a:2", "company:a", 2,
            json.dumps({"bindings": {"mission_version_ref": bound}})))
        connection.execute("CREATE TABLE coverage_mission_versions(mission_version_id TEXT, "
                           "mission_ref TEXT)")
        for version in ("mission:v12", "mission:v14"):
            connection.execute("INSERT INTO coverage_mission_versions VALUES(?,?)",
                               (version, "mission:coverage"))
        return connection

    def test_a_gate_draft_enters_the_deep_insight_stage_without_deciding_it(self):
        authority = FakeMissions({"company:a": {"current_stage": "initial_screen",
            "current_status": "gate_passed", "next_stage": "deep_insight_gate"}})
        result = advance_once(authority, self.gate_core(bound="mission:v12"), mission())
        self.assertEqual(result["status"], "entered")
        self.assertEqual([(w["stage_ref"], w["status"], w["evidence_refs"])
                          for w in authority.writes],
                         [("deep_insight_gate", "entered",
                           ["deep-insight-gate-version:a:2"])])
        self.assertEqual(authority.writes[0]["actor_ref"], "automation:mission")
        # Entered once; the next tick does nothing more -- passing is human.
        again = advance_once(authority, self.gate_core(), mission())
        self.assertEqual(again["status"], "idle")
        self.assertEqual(len(authority.writes), 1)

    def test_no_draft_no_entry(self):
        import sqlite3

        authority = FakeMissions({"company:a": {"current_stage": "initial_screen",
            "current_status": "gate_passed", "next_stage": "deep_insight_gate"}})
        empty = sqlite3.connect(":memory:")
        self.assertEqual(advance_once(authority, empty, mission())["status"], "idle")
        foreign = self.gate_core(bound="mission:other")
        self.assertEqual(advance_once(authority, foreign, mission())["status"], "idle")
        self.assertEqual(authority.writes, [])

    def test_every_qualifying_company_advances_in_one_tick_and_waits_say_why(self):
        stages = {ref: {"current_stage": "industry_model", "current_status": "entered",
                        "next_stage": "industry_model"}
                  for ref in ("company:a", "company:b")}
        authority = FakeMissions(stages)
        value = mission()
        value["universe"] = [{"company_ref": "company:a", "ticker": "AAA"},
                             {"company_ref": "company:b", "ticker": "BBB"}]
        passed = {"passed": True, "checks": [], "reasons": [], "evidence_refs": ["f:1"]}
        stale = {"passed": False, "checks": [], "reasons": ["valuation_snapshot_current"],
                 "reason_labels": ["估值快照没有按最新股价重算"], "evidence_refs": ["m:1"]}
        with patch("dalton_core.mission_model_stage_lane._evaluate",
                   side_effect=[passed, passed, passed, stale]):
            result = advance_once(authority, object(), value)
        self.assertEqual([(row["company_ref"], row["stage_ref"]) for row in result["advanced"]],
                         [("company:a", "industry_model"), ("company:a", "company_model"),
                          ("company:b", "industry_model")])
        self.assertEqual(result["waiting:BBB"], "公司模型缺：估值快照没有按最新股价重算")
        self.assertIn("BBB 公司模型缺：估值快照没有按最新股价重算", result["reason"])
        self.assertLessEqual(len(result["reason"]), 200)
        from dalton_core.tick_ledger import bounded_counts
        counts = bounded_counts(result)
        self.assertEqual(counts["waiting:BBB"], result["waiting:BBB"])


if __name__ == "__main__":
    unittest.main()
