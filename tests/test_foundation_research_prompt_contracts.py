from __future__ import annotations
import unittest
from dalton_core import initial_screen as screen
from dalton_core import company_dossier_draft as dossier
from dalton_core import industry_framework_draft as framework
from dalton_core import deep_insight_gate_draft as gate
from dalton_core import company_model_spec as model_spec
from dalton_core import investment_memo_draft as memo

COMPANY={"ticker":"ACN","company_ref":"company:sec-cik:0001467373"}
RAW=[{"ref":"claim-version:x","kind":"claim","period":"FY2026Q1","importance":"high","text":"Revenue increased because demand improved."}]

class FoundationResearchPromptContractTests(unittest.TestCase):
    def test_initial_screen_is_decisive_but_not_a_trade_gate(self):
        prompt=screen.build_section_prompt(title="核心判断", guidance="判断驱动", company=COMPANY,
            mission={"title":"Coverage","objective":"Understand business","research_questions":[]},
            context={"numbers":[],"claims":[{"tag":"C1","period":"FY2026Q1","statement":"Demand improved."}]})
        self.assertIn("choose the evidence-weighted current case", prompt)
        self.assertIn("This is analysis, not a trade call", prompt)
        self.assertIn("判断/推断", prompt)
        self.assertNotIn("尚未接入", screen.VALUATION_GAP)

    def test_dossier_and_framework_support_qualified_inference_and_tracking(self):
        dp=dossier.build_unit_prompt(unit="demand_drivers", structure=[{"slot_id":"driver:x","prompt":"What drives demand?"}],
                                    material=dossier.material_rows(RAW), company=COMPANY)
        fp=framework.build_unit_prompt(unit="long_term_drivers", structure=[{"slot_id":"driver:x","prompt":"Demand"}],
                                      material=framework.material_rows(RAW), industry={"industry_ref":"industry:x","tickers":["ACN"]})
        for prompt in (dp,fp):
            self.assertIn("evidence-weighted", prompt)
            self.assertIn("falsifier", prompt)
            self.assertIn("observable", prompt)
            self.assertIn("inference", prompt)
        self.assertIn("moat", fp)

    def test_gate_prefers_a_case_without_inventing_a_recommendation(self):
        group=gate.GROUPS[0]
        questions={ref:"What is the current case?" for ref in gate.GROUP_QUESTIONS[group]}
        prompt=gate.build_group_prompt(group=group, questions=questions, material=gate.material_rows(RAW),
                                      company=COMPANY)
        self.assertIn("choose the evidence-weighted current case", prompt)
        self.assertIn("do not turn it into an investment recommendation", prompt)
        self.assertIn("existing source or research action", prompt)

    def test_model_spec_states_actual_context_boundary(self):
        prompt=model_spec.build_prompt({**COMPANY,"entity_name":"Accenture","cik":"1467373",
            "filings":[],"concepts":[],"market_proxies":[],"industry_classification":None})
        self.assertIn("cannot browse", prompt)
        self.assertIn("labelled market proxies", prompt)
        self.assertIn("current base case", prompt)
        self.assertIn("falsifier", prompt)

    def test_memo_distinguishes_market_evidence_house_view_and_arithmetic(self):
        prompt=memo.build_group_prompt(group="view", section_titles=["Variant view"],
            questions=[{"question_ref":"memo_q04","question":"What differs?"}],
            material=RAW, company=COMPANY)
        self.assertIn("Do not call vendor/sell-side consensus buy-side consensus", prompt)
        self.assertIn("return arithmetic", prompt)
        self.assertIn("current preferred case", prompt)
        vp=memo.build_verifier_prompt(sections=[],questions=[],material=[],material_hash="a"*64)
        self.assertIn("buy-side consensus", vp)
        self.assertIn("unranked alternatives", vp)

if __name__ == '__main__': unittest.main()
