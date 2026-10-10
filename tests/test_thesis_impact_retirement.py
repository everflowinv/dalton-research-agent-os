"""ADR-0010: the thesis-impact lane is retired, its data and budget anchor are not."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.model_selection import (
    PURPOSE_LABELS,
    RETIRED_PURPOSES,
    purpose_policy_bindings,
)

ROOT = Path(__file__).resolve().parents[1]


class ThesisImpactRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "config").mkdir()
        self.state = root / "state" / "dalton-core"
        self.state.mkdir(parents=True)
        (root / "config" / "service.json").write_text(json.dumps({
            "schema_version": "0.1",
            "thesis_impact": {"enabled": False, "interval_seconds": 300, "config": {
                "assessment_routing_policy_ref": "model-routing-policy-version:a:9",
                "verifier_routing_policy_ref": "model-routing-policy-version:v:4",
                "budget_db": "/nowhere/budget.sqlite",
                "budget_policy_version_id": "configured:x"}},
        }), encoding="utf-8")

    def test_the_two_stages_are_marked_retired_and_still_read(self) -> None:
        bindings = purpose_policy_bindings(self.state)
        for purpose in ("thesis_impact_assessment", "thesis_impact_verifier"):
            self.assertEqual(bindings[purpose]["retired_by"], "ADR-0010")
            # Still read: old records name these policies and the block is the
            # budget anchor, so the pin is shown, not hidden.
            self.assertEqual(bindings[purpose]["status"], "configured")
            self.assertIn("已退役", PURPOSE_LABELS[purpose])

    def test_only_the_declared_historical_stages_are_retired(self) -> None:
        self.assertEqual(set(RETIRED_PURPOSES),
                         {"thesis_impact_assessment", "thesis_impact_verifier",
                          "agenda_planning"})
        self.assertEqual(RETIRED_PURPOSES["agenda_planning"], "ADR-0009")
        bindings = purpose_policy_bindings(self.state)
        self.assertEqual(bindings["agenda_planning"]["retired_by"], "ADR-0009")
        self.assertNotIn("retired_by", bindings["plan"])

    def test_the_reopen_design_is_gone(self) -> None:
        self.assertIsNone(importlib.util.find_spec("dalton_core.thesis_impact_reopen"))

    def test_the_shared_budget_ledger_is_not_retired(self) -> None:
        # Same name, different job: the day budget for every paid lane.
        self.assertIsNotNone(importlib.util.find_spec("dalton_core.thesis_impact_budget"))

    def test_the_adr_exists_and_names_what_it_keeps(self) -> None:
        text = (ROOT / "docs" / "adr" / "0010-retire-thesis-impact-lane.md").read_text(
            encoding="utf-8")
        self.assertIn("thesis-impact-budget.sqlite", text)
        self.assertIn("thesis_impact_assessments", text)


if __name__ == "__main__":
    unittest.main()
