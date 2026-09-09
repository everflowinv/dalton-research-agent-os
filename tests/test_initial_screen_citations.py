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
    MAX_NUMBERS,
    build_claim_context,
    build_section_prompt,
    strip_citation_tags,
)
from dalton_core.initial_screen_cli import _correction_note, _dropped_section
from dalton_core.mission_deliverable import GAP_MARKER
from dalton_core.research_quality_score import residual_citation_artefacts


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
        # Q1 changed which copy survives. P13ap kept the earliest, for a stable
        # ref across redrafts; the preference is now filing-grade provenance
        # first and then the most recently recorded, because citing a weaker
        # source than the one available is the defect the Playbook's source
        # hierarchy exists to prevent, and none of these three is filing-grade.
        # See q1-research-quality-loop-v1.0-2026-09-09.md.
        self.assertEqual(context["numbers"][0]["ref"], "claim:3")
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


# The exact sentence a published, gate-passed Accenture Initial Screen carries.
# gate_passed is terminal, so this text can never be redrafted; it is here so
# that whatever else changes, the shape that produced it stays caught.
LIVE_ACN_S7_FRAGMENT = (
    "首先，收入增长轨迹是最直接的验证点：显示2026-03-01..2026-05-31季度收入为USD "
    "18718144000，同比增5.59%；、、（同一季度数据重复）显示2025-09-01..2025-11-30"
    "季度收入为USD 18742125000，同比增5.95%。"
)


class LiveResidualFragmentTests(unittest.TestCase):
    """P10c: the fragment that reached a reader, and the two things about it.

    One half is fixable by the stripper and is fixed: the "、、" run where three
    N tags used to be goes with them. The other half is not -- the sentence's
    subject was a tag, and no punctuation cleanup puts one back -- so the
    drafting path now detects it and spends one corrective call rather than
    publishing it. Both halves are asserted here against the live text.
    """

    def test_the_separator_run_the_tags_left_behind_is_removed(self):
        cleaned = strip_citation_tags(LIVE_ACN_S7_FRAGMENT)
        self.assertNotIn("、、", cleaned)
        self.assertNotIn("；、", cleaned)
        # And the figures it was reporting are untouched, because the whole
        # point of stripping the tags is that the numbers still verify.
        self.assertIn("USD 18718144000", cleaned)
        self.assertIn("USD 18742125000", cleaned)
        self.assertIn("同比增5.95%", cleaned)

    def test_what_the_stripper_cannot_fix_is_detected_rather_than_published(self):
        cleaned = strip_citation_tags(LIVE_ACN_S7_FRAGMENT)
        codes = {item["code"] for item in residual_citation_artefacts(LIVE_ACN_S7_FRAGMENT)}
        self.assertIn("separator_run", codes)
        # Still detected after stripping: the sentence beginning "显示2026-…"
        # lost its subject with the tag. The drafting path treats this exactly
        # as it treats an unsourced figure -- one corrective attempt, then the
        # section is dropped to a gap.
        remaining = {item["code"] for item in residual_citation_artefacts(cleaned)}
        self.assertEqual(remaining, {"orphan_sentence_start_verb"})

    def test_a_clean_rewrite_of_the_same_sentence_is_not_flagged(self):
        rewritten = (
            "首先，收入增长轨迹是最直接的验证点：该季报显示2026-03-01..2026-05-31季度收入为USD "
            "18718144000，同比增5.59%，上一财年同期为USD 18742125000。"
        )
        self.assertEqual(residual_citation_artefacts(rewritten), [])


class ThreeDuplicateRevenueClaimsTests(unittest.TestCase):
    """The live shape: three Claims, one figure, one citation.

    ACN's 2025-09-01..2025-11-30 revenue exists three times in the Ledger
    (claim-version:ba4777…, :5848e0…, :ef266e…), written by three separate
    research-plan executions from the same XBRL facts. The published screen's
    S7 lists all three refs for one number.
    """

    def revenue_claim(self, ref, *, created_at, basis="official-filing-xbrl", statement=None):
        return {
            "ref": ref,
            "statement": statement or (
                "Accenture plc reported Revenues of USD 18742125000 for "
                "2025-09-01..2025-11-30, up 5.95% year over year from USD "
                "17689545000 in the comparable quarter."
            ),
            "period": "2025-09-01..2025-11-30",
            "aspect": "quarterly_revenue_yoy_growth",
            "value": "5.95", "unit": "percent",
            "subject_ref": "company:sec-cik:0001467373",
            "basis": basis, "created_at": created_at,
        }

    def test_three_copies_of_one_quarter_become_one_citation(self):
        context = build_claim_context([
            self.revenue_claim("claim-version:ba4777", created_at="2026-09-07T19:36:01+00:00"),
            self.revenue_claim("claim-version:5848e0", created_at="2026-09-07T19:51:10+00:00"),
            self.revenue_claim("claim-version:ef266e", created_at="2026-09-09T08:36:51+00:00"),
        ])
        self.assertEqual(len(context["numbers"]), 1)
        self.assertEqual(context["duplicates_dropped"]["numbers"], 2)
        self.assertEqual(context["numbers"][0]["ref"], "claim-version:ef266e")

    def test_a_paraphrase_of_the_same_measurement_is_also_one_citation(self):
        # Word-for-word matching was not enough: a broker note restating the
        # filing's figure in its own words is the same measurement, and offering
        # both is the same defect wearing different clothes.
        context = build_claim_context([
            self.revenue_claim("claim-version:ba4777", created_at="2026-09-07T19:36:01+00:00"),
            self.revenue_claim(
                "claim-version:broker", created_at="2026-09-08T00:00:00+00:00",
                basis="Wells Fargo Securities note on ACN",
                statement="ACN 的 FQ1 收入同比增长 5.95%，符合我们的预期。",
            ),
        ])
        self.assertEqual(len(context["numbers"]), 1)
        # And the filing's copy is the one kept, however recent the note is.
        self.assertEqual(context["numbers"][0]["ref"], "claim-version:ba4777")

    def test_the_next_quarter_is_a_different_measurement(self):
        first = self.revenue_claim("claim-version:ba4777", created_at="2026-09-07T19:36:01+00:00")
        second = {**first, "ref": "claim-version:1c4f31",
                  "period": "2026-03-01..2026-05-31", "value": "5.59",
                  "statement": "Accenture plc reported Revenues of USD 18718144000 for "
                               "2026-03-01..2026-05-31, up 5.59% year over year."}
        context = build_claim_context([first, second])
        self.assertEqual(len(context["numbers"]), 2)


class NumericContextWidthTests(unittest.TestCase):
    """Only one numeric series reaches a screen; that is now visible and fair.

    Live, all 22 quantitative Claims in the Ledger are quarterly revenue growth
    and every published screen has exactly one numeric series. The context
    builder was not the cause -- it offers every quantitative Claim -- but it
    took the 40 most recently recorded, which is the wrong 40 as soon as a
    second series exists and is written less often.
    """

    def figure(self, aspect, period, statement, *, ref=None):
        return {"ref": ref or f"claim:{aspect}:{period}", "statement": statement,
                "period": period, "aspect": aspect, "value": "1",
                "unit": "usd", "basis": "official-filing-xbrl",
                "subject_ref": "company:sec-cik:0001467373",
                "created_at": f"2026-01-01T00:00:0{len(period) % 10}+00:00"}

    def test_a_thin_series_is_not_crowded_out_by_a_thick_one(self):
        claims = [
            self.figure("quarterly_revenue", f"2020-{month:02d}", f"revenue was USD {month}000000")
            for month in range(1, 13)
        ] * 4
        claims = [dict(claim, ref=f"{claim['ref']}:{index}") for index, claim in enumerate(claims)]
        claims.append(self.figure("operating_margin", "2026Q2", "operating margin was 15.7%"))
        context = build_claim_context(claims)
        self.assertIn("operating_margin", context["series"])
        self.assertIn("quarterly_revenue", context["series"])
        margins = [item for item in context["numbers"] if item["aspect"] == "operating_margin"]
        self.assertEqual(len(margins), 1)

    def test_a_single_series_still_fills_the_budget(self):
        claims = [
            self.figure("quarterly_revenue", f"20{year:02d}Q1", f"revenue was USD {year}000000")
            for year in range(10, 70)
        ]
        context = build_claim_context(claims)
        self.assertEqual(len(context["numbers"]), MAX_NUMBERS)

    def test_a_claim_whose_sentence_carries_a_figure_is_offered_as_a_figure(self):
        # Otherwise the number is unusable: the drafting contract lets a figure
        # into the body only through an N tag, so a utilisation rate stated in a
        # qualitative Claim can never be written down.
        context = build_claim_context([{
            "ref": "claim:utilisation", "statement": "管理层称本季利用率为 91.2%。",
            "period": "2026Q2", "aspect": "utilisation", "value": None,
            "unit": None, "basis": "Earnings call commentary",
            "subject_ref": "company:sec-cik:0001467373",
            "created_at": "2026-07-01T00:00:00+00:00",
        }])
        self.assertEqual(len(context["numbers"]), 1)
        self.assertEqual(context["numbers"][0]["figures"], ["91.2%"])
        self.assertEqual(context["claims"], [])

    def test_a_structured_period_does_not_crash_the_dedupe(self):
        # Live, one Accenture bookings Claim carries a period object rather
        # than a string: {"kind": "fiscal_quarter", "label": "FY2026Q3", ...}.
        # A dict cannot go in a set, and the drafter crashing on a Ledger row
        # is a worse failure than any duplicate it might have collapsed.
        period = {"kind": "fiscal_quarter", "label": "FY2026Q3",
                  "start": "2026-03-01T00:00:00+00:00", "end": "2026-05-31T23:59:59+00:00"}
        claim = {"ref": "claim:bookings", "statement": "Accenture 报告新签订单同比温和增长。",
                 "period": period, "aspect": "aspect:new-bookings-direction", "value": None,
                 "unit": None, "basis": "Earnings call commentary",
                 "subject_ref": "company:sec-cik:0001467373",
                 "created_at": "2026-07-01T00:00:00+00:00"}
        context = build_claim_context([claim, {**claim, "ref": "claim:bookings-copy"}])
        self.assertEqual(len(context["claims"]), 1)
        self.assertEqual(context["duplicates_dropped"]["claims"], 1)

    def test_a_claim_with_no_figure_is_still_a_statement(self):
        context = build_claim_context([{
            "ref": "claim:demand", "statement": "管理层称 discretionary 支出与去年持平。",
            "period": "2026Q2", "aspect": "demand", "value": None, "unit": None,
            "basis": "Earnings call commentary",
            "subject_ref": "company:sec-cik:0001467373",
            "created_at": "2026-07-01T00:00:00+00:00",
        }])
        self.assertEqual(context["numbers"], [])
        self.assertEqual(len(context["claims"]), 1)


class DraftingPathCorrectionTests(unittest.TestCase):
    """P10c: the wreckage now costs a corrective call instead of being published.

    The drafting path already spent one corrective attempt on a section that
    wrote an unsourced figure, and dropped the section to a gap if the attempt
    failed. Residual citation wreckage had no such loop -- the deliverable
    authority does not look for it -- so four of the five live screens carry
    some, permanently. It is now the same loop, told about both defects at
    once so it is still one call.
    """

    def test_the_correction_names_both_defects_and_says_what_to_do(self):
        note = _correction_note(
            ["12.4%"],
            [{"code": "separator_run", "excerpt": "；、、（同一季度数据重复）显示"}])
        self.assertIn("12.4%", note)
        self.assertIn(GAP_MARKER, note)
        self.assertIn("；、、（同一季度数据重复）显示", note)
        self.assertIn("no C or N tag anywhere in the body", note)
        # And what to write instead, because "do not do X" on its own leaves
        # the model to invent an attribution style.
        self.assertIn("管理层、该季报、卖方研报", note)

    def test_a_clean_section_is_told_nothing(self):
        self.assertEqual(_correction_note([], []).strip(), "")

    def test_a_section_that_could_not_be_fixed_becomes_a_gap_that_says_why(self):
        dropped = _dropped_section(
            "S7 数据跟踪", [],
            [{"code": "orphan_sentence_start_verb", "excerpt": "：显示2026-03-01.."}])
        self.assertEqual(dropped["body"], "")
        self.assertEqual(dropped["claim_refs"], [])
        self.assertIn("orphan_sentence_start_verb", dropped["gaps"][0])
        self.assertIn("：显示2026-03-01..", dropped["gaps"][0])

    def test_both_defects_are_reported_together(self):
        dropped = _dropped_section("S3", ["30"], [{"code": "leftover_tag", "excerpt": "C12显示"}])
        self.assertIn("没有来源的数字", dropped["gaps"][0])
        self.assertIn("残句", dropped["gaps"][0])


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
