"""WP-C2 on the Deep Insight Gate: the output rubric reaches the model once.

Live evidence: the return path for DXC (``company:sec-cik:001688568``) held 23
times with ``constitution_refused``, and the owner's note read "按审阅意见重写的
内容不满足宪法的产出标准。… 涉及：q10".  A reviewer had already spent their
attention on that return; the criterion that refused the redraft never reached
the model that wrote the answer, and nothing the lane could do next would have
changed that.

Same bargain as the dossier lane and through the same shared helper: the
finder's own words, the group's own reply, one call, then the same refusal.
"""

from __future__ import annotations

import json
import re
import unittest

from dalton_core.deep_insight_gate_cli import (
    MAX_FINDINGS_REPAIR_ROUNDS,
    rubric_repair_targets,
)
from dalton_core.deep_insight_gate import GROUP_OF, GROUP_QUESTIONS
from tests import test_deep_insight_gate_lane as _gate

FINDINGS_HEAD = "An independent check read your previous reply"
CALL = "我们给出买入评级。"


class ConclusionModel(_gate.FakeModel):
    """Writes an investment conclusion, and removes it when it is named."""

    def __init__(self, *, repairable=True, **kwargs):
        super().__init__(**kwargs)
        self.repairable = repairable
        self.repair_prompts: list[str] = []
        self._drafting: dict[str, str] = {}

    def call(self, *, purpose, request_id, prompt, mission,
             producer_route_decision_refs=()):
        if prompt.startswith(FINDINGS_HEAD):
            self.repair_prompts.append(prompt)
            self.prompts.append(prompt)
            # Which group this is, read out of the reply it is being shown.
            ref = re.findall(r'"question_ref": "(q\d+)"', prompt)[0]
            original = self._drafting[GROUP_OF[ref]]
            clean, self.sentence = self.sentence, (
                self.sentence if not self.repairable
                else self.sentence.replace(CALL, ""))
            try:
                return super().call(purpose=purpose, request_id=request_id,
                                    prompt=original, mission=mission)
            finally:
                self.sentence = clean
        group = next(iter(re.findall(r"^Part: (\w+) --", prompt, flags=re.MULTILINE)),
                     None)
        if group is not None:
            self._drafting[group] = prompt
        return super().call(
            purpose=purpose, request_id=request_id, prompt=prompt, mission=mission,
            producer_route_decision_refs=producer_route_decision_refs)


class TargetTests(unittest.TestCase):
    def test_a_finding_names_a_question_and_a_call_answers_a_group(self):
        answers = {ref: {"status": "answered"} for ref in GROUP_QUESTIONS["industry"]}
        targets = rubric_repair_targets(
            [{"code": "investment_conclusion", "criterion_index": 0,
              "section": "q2", "phrase": "买入"}], answers)
        self.assertEqual(list(targets), ["industry"])

    def test_a_question_this_run_did_not_draft_is_not_repairable(self):
        self.assertEqual(
            rubric_repair_targets(
                [{"code": "investment_conclusion", "section": "q10",
                  "phrase": "买入"}], {}),
            {})

    def test_a_finding_about_the_whole_document_names_no_group(self):
        self.assertEqual(
            rubric_repair_targets([{"code": "no_new_evidence",
                                    "criterion_index": 0}], {}), {})

    def test_one_round_is_one_round(self):
        self.assertEqual(MAX_FINDINGS_REPAIR_ROUNDS, 1)


class GateRubricRepairTests(unittest.TestCase):
    def setUp(self):
        self.harness = _gate.Harness()
        self.addCleanup(self.harness.close)
        policy = json.loads(self.harness.policy_path.read_text(encoding="utf-8"))
        policy["output_rubric_bindings"][0]["check"] = "no_investment_conclusion"
        self.harness.policy_path.write_text(json.dumps(policy), encoding="utf-8")

    def run_with(self, model):
        return self.harness.run(
            model_factory=lambda: model,
            verifier_model_factory=lambda: _gate.FakeModel(route="route:verify"))

    def test_the_conclusion_is_removed_once_and_the_draft_goes_through(self):
        model = ConclusionModel(
            sentence="这一问的判断由所引材料支撑。" + CALL, answer_all=True)
        summary = self.run_with(model)
        if summary["gate_status"] in {"nothing_drafted", "classification_conflict"}:
            self.skipTest(f"fixture stopped earlier: {summary['gate_status']}")
        self.assertEqual(summary["findings_repair_rounds"],
                         MAX_FINDINGS_REPAIR_ROUNDS)
        self.assertTrue(summary["findings_repair"])
        row = summary["findings_repair"][0]
        self.assertEqual(row["kind"], "output_rubric")
        self.assertEqual(row["status"], "repaired")
        self.assertEqual(row["repair_attempts"], 1)
        self.assertEqual(row["findings"][0]["code"], "investment_conclusion")
        self.assertEqual(summary["output_rubric_findings"], [])
        self.assertNotEqual(summary["gate_status"], "constitution_refused")
        self.assertIn("买入", model.repair_prompts[0])

    def test_a_repair_that_keeps_the_call_refuses_once_and_stops(self):
        model = ConclusionModel(
            sentence="这一问的判断由所引材料支撑。" + CALL, answer_all=True,
            repairable=False)
        summary = self.run_with(model)
        if summary["gate_status"] in {"nothing_drafted", "classification_conflict"}:
            self.skipTest(f"fixture stopped earlier: {summary['gate_status']}")
        self.assertEqual(summary["gate_status"], "constitution_refused")
        self.assertEqual(summary["findings_repair_rounds"], 1)
        self.assertIn("after one repair call", summary["failure_reason"])
        self.assertTrue(summary["output_rubric_findings"])
        # One, not a loop.
        self.assertEqual(
            len([row for row in summary["findings_repair"]
                 if row["repair_attempts"]]), len(model.repair_prompts))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
