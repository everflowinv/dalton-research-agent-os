"""DXC supply_and_cost, refused nine runs running over one figure.

Live evidence (read-only), legacy state ``lane-failure-ledger.sqlite``,
lane ``company_dossier``, item ``company:sec-cik:001688568``:

* 2026-09-26T13:22Z .. 2026-09-27T23:49Z, seven runs:
  ``hard checks failed: numbers_without_refs; first target: {"figure": "970",
  "section": "supply_and_cost"}``.  The drafts (scheduler result envelopes
  ``result:d82530c0...``, ``result:e7d10418...``, ``result:0dd3f55d...``) wrote
  "调整后EBIT为970 million美元", citing C1, whose text is "Adjusted EBIT was
  $970 million, down 4.8% year-over-year ...".  Same digits, same scale word;
  the currency symbol had moved into 美元, which the drafting prompt itself
  asks for.  A matching defect.
* 2026-09-28T09:45Z and 11:23Z: ``"figure": "970000000"``.  The draft
  (``result:a53ae7f8...``, 08:45Z) wrote "970000000美元" from the same C1: a
  rescaling the rule refuses by design.  The 11:18Z run redrafted nothing for
  that unit -- its prompt was unchanged, so the content-addressed request
  replayed the refused reply for free -- and paid for the other units of a run
  that could not publish.

Two fixes are tested: the currency symbol no longer separates a figure from
its row, and a figure the gate still refuses is handed back to the model that
wrote it once, inside the existing bounded findings-repair round.
"""

from __future__ import annotations

import json
import re
import unittest

from dalton_core.company_dossier import REPAIR_FINDING_KINDS
from dalton_core.company_dossier_cli import (
    NUMBERS_REPAIR,
    findings_repair_targets,
    numbers_repair_findings,
)
from dalton_core.mission_deliverable import (
    NUMBER_SOURCE_CONTRACT_VERSION,
    unsourced_numbers,
)
from tests import test_dossier_lane as _lane

FINDINGS_HEAD = "An independent check read your previous reply"
DXC_C1 = ("Adjusted EBIT was $970 million, down 4.8% year-over-year with a "
          "corresponding margin of for fiscal 2026 was 7.7 (percent), as "
          "published by the company in this document.")


class CurrencySymbolTests(unittest.TestCase):
    def test_the_dxc_sentence_that_was_refused_seven_times_is_sourced(self):
        body = ("fiscal 2026 调整后 EBIT 为 970 million 美元，同比下降 4.8%，"
                "利润率为 7.7%")
        self.assertEqual(unsourced_numbers(body, [{"text": DXC_C1}]), [])

    def test_a_rescaled_figure_is_still_refused(self):
        body = "2026财年调整后EBIT为970000000美元，同比下降4.8%，利润率7.7%"
        self.assertEqual(unsourced_numbers(body, [{"text": DXC_C1}]),
                         ["970000000"])
        self.assertEqual(
            unsourced_numbers("调整后EBIT约 9.7 亿美元", [{"text": DXC_C1}]),
            ["9.7"])

    def test_only_the_symbol_is_dropped_never_the_digits(self):
        rows = [{"text": "revenue was €12.5 billion and £1,204 million"}]
        self.assertEqual(unsourced_numbers("收入 12.5 billion 欧元", rows), [])
        self.assertEqual(unsourced_numbers("1204 million 英镑", rows), [])
        self.assertEqual(unsourced_numbers("收入 12.6 billion 欧元", rows), ["12.6"])
        # A bare cited figure still does not source a currency-prefixed one.
        self.assertEqual(unsourced_numbers("$4200", [{"text": "4200 staff"}]),
                         ["$4200"])

    def test_the_contract_moved_so_terminal_holds_release(self):
        self.assertEqual(NUMBER_SOURCE_CONTRACT_VERSION, "number-source-contract:0.7")


class NumbersRepairFindingsTests(unittest.TestCase):
    TARGETS = [
        {"check": "numbers_without_refs", "figure": "970000000",
         "section": "supply_and_cost"},
        {"check": "new_version_cites_new_refs", "section": "x"},
    ]

    def test_only_figure_findings_become_repairs_and_they_are_deterministic(self):
        first = numbers_repair_findings(self.TARGETS)
        self.assertEqual(first, numbers_repair_findings(self.TARGETS))
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["figure"], "970000000")
        self.assertEqual(first[0]["section"], "supply_and_cost")
        self.assertIn("970000000", first[0]["detail"])
        self.assertLessEqual(len(first[0]["detail"]), 500)

    def test_a_carried_forward_section_has_nothing_to_repair(self):
        targets = findings_repair_targets(
            numbers_repair_findings(self.TARGETS), {"demand_drivers": {}},
            key="section")
        self.assertEqual(targets, {})

    def test_the_formal_record_accepts_the_kind(self):
        self.assertIn(NUMBERS_REPAIR, REPAIR_FINDING_KINDS)


class UnsourcedOnceModel(_lane.FakeModel):
    """Drafts one figure no row carries; removes it when told which one."""

    def __init__(self, *, repairable=True, **kwargs):
        super().__init__(extra_number="987654321", **kwargs)
        self.repairable = repairable
        self.repair_prompts: list[str] = []
        self.clean: dict[str, str] = {}

    def call(self, *, purpose, request_id, prompt, mission):
        if prompt.startswith(FINDINGS_HEAD):
            self.prompts.append(prompt)
            self.repair_prompts.append(prompt)
            # One unit per run in these tests: the repair names no part.
            if not self.repairable:
                return self._envelope(next(iter(self.dirty.values())))
            return self._envelope(next(iter(self.clean.values())))
        result = super().call(purpose=purpose, request_id=request_id,
                              prompt=prompt, mission=mission)
        found = re.search(r"^Part: (\S+)", prompt, flags=re.MULTILINE)
        if found is not None and not prompt.startswith("You are an independent verifier"):
            self.dirty = getattr(self, "dirty", {})
            self.dirty[found.group(1)] = result["text"]
            self.clean[found.group(1)] = result["text"].replace("规模约为 987654321。", "")
        return result


class NumbersRepairRunTests(unittest.TestCase):
    def setUp(self):
        self.harness = _lane.Harness()
        self.addCleanup(self.harness.close)

    def test_a_refused_figure_is_handed_back_once_and_the_file_publishes(self):
        model = UnsourcedOnceModel()
        summary = self.harness.run(
            model_factory=lambda: model,
            verifier_model_factory=lambda: _lane.FakeModel(route="route:verify"),
            max_units=1)
        self.assertIn(summary["dossier_status"], {"published", "partial_published"})
        self.assertEqual(summary["findings_repair_rounds"], 1)
        row = summary["findings_repair"][0]
        self.assertEqual(row["kind"], NUMBERS_REPAIR)
        self.assertEqual(row["status"], "repaired")
        self.assertEqual(row["findings"][0]["figure"], "987654321")
        self.assertEqual(len(model.repair_prompts), 1)
        self.assertIn("987654321", model.repair_prompts[0])
        self.assertEqual(summary["repair_targets"], [])

    def test_a_repair_that_keeps_the_figure_refuses_once_and_stops(self):
        model = UnsourcedOnceModel(repairable=False)
        summary = self.harness.run(
            model_factory=lambda: model,
            verifier_model_factory=lambda: _lane.FakeModel(route="route:verify"),
            max_units=1)
        self.assertEqual(summary["dossier_status"], "rubric_refused")
        self.assertEqual(summary["findings_repair_rounds"], 1)
        self.assertEqual(len(model.repair_prompts), 1)
        self.assertIn("after one repair call", summary["failure_reason"])
        self.assertIn("first target", summary["failure_reason"])
        self.assertEqual(summary["repair_targets"][0]["figure"], "987654321")

    def test_an_unrepairable_model_is_refused_as_before(self):
        summary = self.harness.run(
            model_factory=lambda: _lane.FakeModel(extra_number="45.6 亿美元"),
            max_units=1)
        self.assertEqual(summary["dossier_status"], "rubric_refused")
        self.assertEqual(summary["repair_targets"][0]["check"],
                         "numbers_without_refs")
        self.assertEqual(summary["findings_repair_rounds"], 0)


if __name__ == "__main__":
    unittest.main()
