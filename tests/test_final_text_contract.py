from __future__ import annotations

import inspect
import unittest

from dalton_core import (
    ask_answer, company_dossier_draft, conviction_call_draft, debate_map_draft,
    deep_insight_gate_draft, event_judgement, industry_framework_draft,
    initial_screen, investment_memo_draft, thesis_impact_control, zero_base_review,
)
from dalton_core.final_text_contract import final_text_instructions


class FinalTextContractTests(unittest.TestCase):
    def test_contract_requires_chinese_judgement_without_erasing_real_limits(self):
        text = " ".join(final_text_instructions())
        self.assertIn("Simplified Chinese", text)
        self.assertIn("clearest evidence-supported judgement", text)
        self.assertIn("Preserve the meaning of genuine gaps", text)
        self.assertIn("Never invent", text)

    def test_every_human_facing_research_drafter_uses_the_shared_contract(self):
        producers = (
            initial_screen.build_section_prompt,
            company_dossier_draft.build_unit_prompt,
            debate_map_draft.build_prompt,
            industry_framework_draft.build_unit_prompt,
            investment_memo_draft.build_group_prompt,
            conviction_call_draft.build_prompt,
            deep_insight_gate_draft.build_group_prompt,
            event_judgement.build_judge_prompt,
            event_judgement.build_reflection_prompt,
            zero_base_review.build_review_prompt,
            ask_answer.build_prompt,
            thesis_impact_control.ResearchPlanThesisImpactCoordinator._assessment_work_order,
        )
        missing = [item.__qualname__ for item in producers
                   if "final_text_instructions" not in inspect.getsource(item)]
        self.assertEqual(missing, [])
