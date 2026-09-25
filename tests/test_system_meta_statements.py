"""2026-09-25b: a statement about the research system is not a finding.

ws-7d a2375cbf, filed under AMZN: "figures 抽取为零应解释为抽取尚未执行".  The
document qualitative rule refuses such a statement before the commit, and the
directed-document promotion refuses it before promotion.
"""

from __future__ import annotations

import unittest

from dalton_core.claim_admission_quality import statement_is_system_meta


class SystemMetaTests(unittest.TestCase):
    def test_statements_about_the_pipeline_are_meta(self) -> None:
        self.assertTrue(statement_is_system_meta(
            "AMZN 2025 财年 10-K 文档本身包含可提取的财务数字（如现金流量表、经营报表和自由现金流"
            "调节表中的数值），因此 figures 抽取结果为零应解释为抽取尚未执行或尚未产生结果，"
            "而非文档没有可提取数字。"))
        self.assertTrue(statement_is_system_meta(
            "DXC's net income fell sharply; the figures are captured in the separate numeric lane."))
        self.assertTrue(statement_is_system_meta(
            "The extraction result for this filing was empty because the table was not parsed."))

    def test_business_prose_is_not(self) -> None:
        for statement in (
            "Management said the deal pipeline is the strongest in years.",
            "The energy build-out is bottlenecked by turbines and midstream pipeline capex.",
            "Oil extraction costs rose in the Permian.",
            "Analyst Justin Post raised his AMZN capex estimates versus his prior figures.",
            "EPAM has qualified its entire delivery pipeline with agents.",
        ):
            self.assertFalse(statement_is_system_meta(statement), statement)

    def test_the_document_rule_refuses_it_before_the_commit(self) -> None:
        from dalton_core.research_auto_commit import document_qualitative_content_rejection

        wire = {"value": None, "unit": None, "scale": None, "currency": None,
                "normalized_statement": "The figures pass returned zero, so the extraction "
                                        "result should be read as not yet run."}
        self.assertIn("research system's own process",
                      document_qualitative_content_rejection(wire))
        wire["normalized_statement"] = "Amazon expects AWS capacity to double by 2027."
        self.assertIsNone(document_qualitative_content_rejection(wire))

if __name__ == "__main__":
    unittest.main()
