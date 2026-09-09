"""P13ap: the citation scaffolding has to come out without taking the prose.

C/N tags are scaffolding the prompt introduced. The section's claim list is
what carries provenance to a reader, so the tags are stripped before anything
is published -- and stripping them used to wreck the sentences they were part
of. Live, in screens that passed the gate and can never be redrafted:

    ；、、（同一季度数据重复）显示2025-09-01..季度收入为USD 18742125000
    目前C1至C4显示…（数据来源：、、、），但无分解数据

The real fix is the prompt: tags belong in the JSON arrays and never in the
body. This is the second line of defence, and its job is to delete what the
tags left behind -- not to pretend it can put a subject back into a sentence
that lost one.
"""

from __future__ import annotations

import unittest

from dalton_core.initial_screen import (
    build_claim_context,
    build_section_prompt,
    strip_citation_tags,
)


class StripCitationTagsTests(unittest.TestCase):
    def test_a_list_of_tags_goes_with_its_separators(self):
        self.assertEqual(
            strip_citation_tags("C5、C6显示本季收入上升。"),
            "显示本季收入上升。")
        self.assertEqual(
            strip_citation_tags("目前C1至C4显示增速在 4.5% 至 19.4% 之间。"),
            "目前显示增速在 4.5% 至 19.4% 之间。")
        self.assertEqual(
            strip_citation_tags("C7和C8指出联邦支出削减。"),
            "指出联邦支出削减。")

    def test_a_source_list_that_lost_its_sources_is_removed_whole(self):
        # "（数据来源：）" reads as a citation that lost its citations: it tells
        # the reader something was there and does not say what.
        self.assertEqual(
            strip_citation_tags("收入同比增 5.59%（数据来源：N1、N2、N3、N4），但无分解。"),
            "收入同比增 5.59%，但无分解。")
        self.assertEqual(strip_citation_tags("收入增长（N1）是验证点。"),
                         "收入增长是验证点。")

    def test_a_parenthetical_that_still_says_something_is_kept(self):
        self.assertEqual(
            strip_citation_tags("新签订单（new bookings）是领先指标。"),
            "新签订单（new bookings）是领先指标。")
        self.assertEqual(
            strip_citation_tags("利润率（调整后，N3）承压。"),
            "利润率（调整后）承压。")

    def test_figures_are_never_touched(self):
        # The tag pattern is bounded by non-alphanumerics on both sides, so the
        # digits inside a figure are safe -- which is the whole point, since a
        # figure must survive verbatim to be checked against its N tag.
        for text in ("收入为 USD 18718144000，同比增 5.59%。",
                     "Revenue reached USD 18,718,144,000 in the quarter.",
                     "毛利率 31.4%，环比提升 0.3 个百分点。"):
            with self.subTest(text=text):
                self.assertEqual(strip_citation_tags(text), text)

    def test_words_that_merely_look_like_tags_survive(self):
        for text in ("ABC3 是产品代号。", "型号 N1000X 已停产。", "C的评级维持。"):
            with self.subTest(text=text):
                self.assertEqual(strip_citation_tags(text), text)

    def test_a_separator_left_at_a_sentence_boundary_is_removed(self):
        self.assertEqual(
            strip_citation_tags("收入上升；C1、C2显示订单回暖。"),
            "收入上升；显示订单回暖。")
        self.assertEqual(strip_citation_tags("C1、C2显示订单回暖。"),
                         "显示订单回暖。")

    def test_an_untagged_body_passes_through_unchanged(self):
        text = "管理层表示 discretionary 支出与去年持平，尚未进一步下探。"
        self.assertEqual(strip_citation_tags(text), text)

    def test_empty_input_is_empty_output(self):
        self.assertEqual(strip_citation_tags(""), "")
        self.assertEqual(strip_citation_tags(None), "")

    def test_it_does_not_pretend_to_repair_a_lost_subject(self):
        # "C12显示收入为…" loses its subject when the tag goes, and no amount of
        # punctuation cleanup puts one back. The prompt is what prevents this;
        # this test records that the stripper does not claim to.
        self.assertEqual(strip_citation_tags("C12显示收入为 USD 100。"),
                         "显示收入为 USD 100。")


class ClaimContextDedupeTests(unittest.TestCase):
    """The other half of the same defect: the same figure offered three times.

    Live, three Claims asserted Accenture's 2025Q1 revenue in exactly the same
    words. The drafter tagged them N1, N2 and N3, and the model -- correctly,
    given what it was shown -- cited all three and said so in the published
    text. The model was not wrong; the context was.
    """

    def figure(self, ref, *, value="5.95", period="2025-09-01..2025-11-30",
               statement="Accenture plc reported Revenues of USD 18742125000."):
        return {"ref": ref, "statement": statement, "period": period,
                "aspect": "quarterly_revenue_yoy_growth", "value": value,
                "created_at": f"2026-09-0{ref[-1]}T00:00:00+00:00"}

    def test_the_same_figure_asserted_three_times_is_offered_once(self):
        context = build_claim_context([
            self.figure("claim:1"), self.figure("claim:2"), self.figure("claim:3"),
        ])
        self.assertEqual(len(context["numbers"]), 1)
        self.assertEqual(context["duplicates_dropped"]["numbers"], 2)
        # Earliest kept, so a redraft cites the same Claim rather than whichever
        # copy happened to be recorded last.
        self.assertEqual(context["numbers"][0]["ref"], "claim:1")
        self.assertEqual(context["numbers"][0]["tag"], "N1")

    def test_a_restated_figure_is_not_a_duplicate(self):
        context = build_claim_context([
            self.figure("claim:1", value="5.95"),
            self.figure("claim:2", value="6.10",
                        statement="Accenture plc reported Revenues of USD 18800000000."),
        ])
        self.assertEqual(len(context["numbers"]), 2)
        self.assertEqual(context["duplicates_dropped"]["numbers"], 0)

    def test_the_same_figure_in_two_periods_is_two_figures(self):
        context = build_claim_context([
            self.figure("claim:1", period="2025-09-01..2025-11-30"),
            self.figure("claim:2", period="2026-03-01..2026-05-31"),
        ])
        self.assertEqual(len(context["numbers"]), 2)

    def test_word_for_word_repeated_statements_are_offered_once(self):
        claim = {"ref": "claim:1", "statement": "管理层称需求企稳。",
                 "period": "2026Q3", "aspect": None, "value": None,
                 "created_at": "2026-09-01T00:00:00+00:00"}
        context = build_claim_context([
            claim, {**claim, "ref": "claim:2"}, {**claim, "ref": "claim:3"},
        ])
        self.assertEqual(len(context["claims"]), 1)
        self.assertEqual(context["duplicates_dropped"]["claims"], 2)

    def test_deduplication_happens_before_the_budget_is_spent(self):
        # Otherwise the cap is filled with copies and distinct material is cut.
        claims = []
        for index in range(10):
            claims.append({"ref": f"claim:dup{index}", "statement": "同一句话。",
                           "period": "2026Q3", "aspect": None, "value": None,
                           "created_at": "2026-09-01T00:00:00+00:00"})
        claims.append({"ref": "claim:distinct", "statement": "另一件事。",
                       "period": "2026Q3", "aspect": None, "value": None,
                       "created_at": "2026-09-02T00:00:00+00:00"})
        context = build_claim_context(claims, max_claims=2)
        statements = [item["statement"] for item in context["claims"]]
        self.assertEqual(statements, ["同一句话。", "另一件事。"])

    def test_nothing_to_drop_is_reported_as_nothing(self):
        context = build_claim_context([self.figure("claim:1")])
        self.assertEqual(context["duplicates_dropped"],
                         {"claims": 0, "numbers": 0})


class SectionPromptTests(unittest.TestCase):
    def prompt(self):
        return build_section_prompt(
            title="核心 thesis", guidance="识别关键 driver。",
            company={"ticker": "ACN", "company_ref": "company:sec-cik:0001467373"},
            mission={"title": "US IT services", "objective": "coverage",
                     "research_questions": ["需求见底了吗"]},
            context={"numbers": [{"tag": "N1", "period": "2026Q3",
                                  "statement": "收入为 USD 18718144000"}],
                     "claims": [{"tag": "C1", "period": "2026Q3",
                                 "statement": "管理层称需求企稳"}]},
        )

    def test_the_prompt_forbids_tags_in_the_body(self):
        prompt = self.prompt()
        self.assertIn("NEVER write a C or N tag inside the body text", prompt)
        self.assertIn("JSON arrays", prompt)
        # And says what to do instead, because "do not do X" alone leaves the
        # model to guess how to attribute anything.
        self.assertIn("name the source in words", prompt)

    def test_the_prompt_still_carries_the_material_and_the_figure_rule(self):
        prompt = self.prompt()
        self.assertIn("N1", prompt)
        self.assertIn("18718144000", prompt)
        self.assertIn("VERBATIM", prompt)


if __name__ == "__main__":
    unittest.main()
