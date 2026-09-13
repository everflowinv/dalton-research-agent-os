from __future__ import annotations

import inspect
import unittest

from dalton_core import (
    ask_answer, company_dossier_draft, conviction_call_draft, debate_map_draft,
    deep_insight_gate_draft, earnings_calibration, earnings_preview, event_judgement, industry_framework_draft,
    initial_screen, investment_memo_draft, research_planner, thesis_impact_control, zero_base_review,
)
from dalton_core.final_text_contract import final_text_instructions


class FinalTextContractTests(unittest.TestCase):
    def test_contract_requires_chinese_judgement_without_erasing_real_limits(self):
        text = " ".join(final_text_instructions())
        self.assertIn("Simplified Chinese", text)
        self.assertIn("clearest evidence-supported judgement", text)
        self.assertIn("Preserve the meaning of genuine gaps", text)
        self.assertIn("Never invent", text)
        self.assertIn("Claim/Claims→已核实结论", text)
        self.assertIn("ThesisRevisionCandidate→论点修订建议", text)
        self.assertIn("Internal system vocabulary is not a proper noun", text)
        self.assertIn("不改动任何权威", text)
        from dalton_core.research_language_review import build_brain_prompt, build_checker_prompt
        product = {"kind": "ui_text", "sections": [{"title": "t", "body": "b", "gaps": []}]}
        self.assertIn("Claim/Claims→已核实结论", build_checker_prompt(product))
        self.assertIn("ThesisRevisionCandidate→论点修订建议", build_brain_prompt(product, {"overall": "", "suggestions": []}))

    def test_every_human_facing_research_drafter_uses_the_shared_contract(self):
        producers = (
            initial_screen.build_section_prompt,
            company_dossier_draft.build_unit_prompt,
            debate_map_draft.build_prompt,
            industry_framework_draft.build_unit_prompt,
            investment_memo_draft.build_group_prompt,
            conviction_call_draft.build_prompt,
            deep_insight_gate_draft.build_group_prompt,
            earnings_calibration.build_calibration_prompt,
            earnings_preview.build_preview_prompt,
            event_judgement.build_judge_prompt,
            event_judgement.build_reflection_prompt,
            zero_base_review.build_review_prompt,
            ask_answer.build_prompt,
            research_planner.build_prompt,
            thesis_impact_control.ResearchPlanThesisImpactCoordinator._assessment_work_order,
        )
        missing = [item.__qualname__ for item in producers
                   if "final_text_instructions" not in inspect.getsource(item)]
        self.assertEqual(missing, [])
