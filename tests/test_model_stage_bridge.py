from __future__ import annotations

import unittest
from unittest.mock import patch

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
        self.assertTrue(industry_model_readiness(framework)["passed"])
        framework["cross_company_comparison"]["cells"] = []
        result = industry_model_readiness(framework)
        self.assertFalse(result["passed"])
        self.assertIn("cross_company_comparison_computed", result["reasons"])

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
        self.assertTrue(company_model_readiness(model, sensitivity)["passed"])
        model["assumptions"][0]["refs"] = []
        self.assertIn("forecast_assumptions_explicit",
                      company_model_readiness(model, sensitivity)["reasons"])

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
        with patch("dalton_core.mission_model_stage_lane._evaluate", return_value=passed):
            second = advance_once(authority, object(), mission("mission:v15"))
        self.assertEqual(second["status"], "advanced")
        self.assertEqual([x["status"] for x in authority.writes], ["entered", "gate_passed"])
        self.assertEqual(authority.writes[-1]["mission_version_ref"], "mission:v15")

    def test_never_skips_deep_insight_human_checkpoint(self):
        authority = FakeMissions({"company:a": {"current_stage": "initial_screen",
            "current_status": "gate_passed", "next_stage": "deep_insight_gate"}})
        result = advance_once(authority, object(), mission())
        self.assertEqual(result["status"], "idle")
        self.assertEqual(authority.writes, [])


if __name__ == "__main__":
    unittest.main()
