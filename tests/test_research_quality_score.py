"""Q1: the two layers, and the record that keeps them apart.

The deterministic checks are asserted against the shapes the live documents
actually carry, not against invented ones.  The judge is asserted through a
fake model, because what is being tested is the verification -- what happens
when a model returns almost the right thing -- and a real call cannot be made
to return almost the right thing on purpose.
"""

from __future__ import annotations

import json
import unittest

from dalton_core.research_quality_rubrics import rubric
from dalton_core.research_quality_score import (
    JUDGE_MODEL_CONFIG_NAME,
    JUDGE_PURPOSE,
    QualityScoreAuthority,
    ResearchQualityConflict,
    ResearchQualityValidationError,
    SCORER_VERSION,
    artefact,
    artefact_from_ask_answer,
    artefact_from_deliverable,
    artefact_from_dossier,
    build_judge_prompt,
    judge,
    judge_fingerprint,
    residual_citation_artefacts,
    run_deterministic,
    score_artefact,
    summarise_scores,
    validate_judge_output,
    validate_verifier_output,
    verify,
)
from dalton_core.store import DaltonStore, content_hash

ACN = "company:sec-cik:0001467373"
SCREEN = rubric("initial_screen")
ASK = rubric("ask_answer")
DOSSIER = rubric("company_dossier")

REVENUE_TEXT = (
    "Accenture plc reported Revenues of USD 18742125000 for 2025-09-01..2025-11-30, "
    "up 5.95% year over year from USD 17689545000 in the comparable quarter."
)


def number(ref, *, text=REVENUE_TEXT, period="2025-09-01..2025-11-30"):
    return {"text": text, "claim_version_ref": ref, "period": period}


def screen(sections, *, ref="mission-deliverable-version:test", digest="a" * 64, expected=None):
    return artefact(
        artefact_kind="initial_screen", ref=ref, hash=digest, subject_ref=ACN,
        sections=sections,
        expected_sections=expected if expected is not None else [s["title"] for s in sections],
    )


class ResidualArtefactTests(unittest.TestCase):
    """Every pattern here was read out of a published, gate-passed screen."""

    def codes(self, text):
        return sorted({item["code"] for item in residual_citation_artefacts(text)})

    def test_the_accenture_fragment(self):
        # ；、、（同一季度数据重复）显示…  -- ACN Initial Screen v2, S7.
        fragment = "同比增5.59%；、、（同一季度数据重复）显示2025-09-01..季度收入为USD 18742125000"
        self.assertEqual(self.codes(fragment),
                         ["separator_after_sentence_end", "separator_run"])
        # The stripper takes the "、、" run out and what is left is a sentence
        # with no subject, which is the half no punctuation cleanup can fix and
        # the reason the drafting path now spends a corrective call on it.
        from dalton_core.initial_screen import strip_citation_tags

        self.assertEqual(self.codes(strip_citation_tags("：" + fragment)),
                         ["orphan_sentence_start_verb"])

    def test_the_epam_empty_source_list(self):
        # （数据来源：、、、） -- EPAM Initial Screen v1, S4.
        self.assertIn("separator_run",
                      self.codes("增速在4.53%至19.43%之间（数据来源：、、、），但无分解数据。"))

    def test_the_ibm_separator_against_a_bracket(self):
        # （管理层的信念，） / （…，、） -- IBM and EPAM, S4/S5.
        self.assertIn("separator_before_closing_bracket",
                      self.codes("其不可替代性（管理层的信念，），收入基础可能被侵蚀。"))

    def test_a_sentence_whose_subject_was_a_tag(self):
        self.assertIn("orphan_conjunction_verb",
                      self.codes("管理层表示支出持平，和强调discretionary环境未变。"))
        self.assertIn("orphan_sentence_start_verb",
                      self.codes("行业分化明显；显示2025年第三季度收入为USD 1300000000。"))

    def test_a_leftover_tag_is_itself_an_artefact(self):
        self.assertEqual(self.codes("C12显示收入上升。"), ["leftover_tag"])

    def test_an_empty_parenthetical_is_an_artefact_and_a_full_one_is_not(self):
        self.assertEqual(self.codes("收入增长（）是验证点。"), ["empty_parenthetical"])
        self.assertEqual(self.codes("收入增长（数据来源：）是验证点。"), ["empty_parenthetical"])
        self.assertEqual(self.codes("新签订单（new bookings）是领先指标。"), [])

    def test_ordinary_chinese_prose_is_not_flagged(self):
        # "，表示" is ordinary Chinese and must not be read as wreckage, or the
        # check would fire on every well-written paragraph.
        for text in ("公司发布公告，表示将维持全年指引。",
                     "管理层表示 discretionary 支出与去年持平，尚未进一步下探。",
                     "收入为 USD 18718144000，同比增 5.59%，环比回落。",
                     "新签订单（new bookings）与利用率（utilization）是两个领先指标。"):
            with self.subTest(text=text):
                self.assertEqual(residual_citation_artefacts(text), [])

    def test_the_finding_carries_the_excerpt_a_reader_needs(self):
        finding = residual_citation_artefacts("同比增5.59%；、、（重复）显示收入")[0]
        self.assertIn("；、、", finding["excerpt"])


class DeterministicCheckTests(unittest.TestCase):
    def test_a_figure_no_cited_claim_carries_is_reported_with_its_section(self):
        art = screen([{"title": "S3", "body": "新签订单同比增长 12.4%。", "claim_refs": [],
                       "numbers": [], "gaps": []}])
        result = run_deterministic(art, SCREEN)
        finding = next(item for item in result["checks"] if item["check"] == "numbers_without_refs")
        self.assertEqual(finding["status"], "fail")
        self.assertEqual(finding["findings"], [{"section": "S3", "figure": "12.4%"}])

    def test_three_claims_for_one_quarter_are_one_duplicate_finding(self):
        art = screen([{"title": "S7", "body": "季度收入为 USD 18742125000，同比增 5.95%。",
                       "claim_refs": [],
                       "numbers": [number("claim-version:ba4777"), number("claim-version:5848e0"),
                                   number("claim-version:ef266e")],
                       "gaps": []}])
        result = run_deterministic(art, SCREEN)
        check = next(item for item in result["checks"] if item["check"] == "duplicate_parallel_citations")
        self.assertEqual(check["status"], "fail")
        self.assertEqual(check["findings"][0]["code"], "duplicate_number_entries")
        self.assertEqual(len(check["findings"][0]["refs"]), 3)

    def test_a_restated_figure_is_not_a_duplicate(self):
        art = screen([{"title": "S7", "body": "收入为 USD 18742125000。", "claim_refs": [],
                       "numbers": [number("claim-version:a"),
                                   number("claim-version:b", period="2026-03-01..2026-05-31",
                                          text="Revenues of USD 18718144000 for 2026-03-01..2026-05-31.")],
                       "gaps": []}])
        result = run_deterministic(art, SCREEN)
        check = next(item for item in result["checks"] if item["check"] == "duplicate_parallel_citations")
        self.assertEqual(check["status"], "pass")

    def test_a_missing_section_and_an_empty_shell_are_both_findings(self):
        art = screen(
            [{"title": "S3", "body": "有内容。", "claim_refs": ["c"], "numbers": [], "gaps": []},
             {"title": "S4", "body": "", "claim_refs": [], "numbers": [], "gaps": []}],
            expected=["S3", "S4", "S7"])
        check = next(item for item in run_deterministic(art, SCREEN)["checks"]
                     if item["check"] == "required_sections_present")
        self.assertEqual(check["status"], "fail")
        self.assertEqual(sorted(finding["code"] for finding in check["findings"]),
                         ["empty_shell", "missing_section"])

    def test_a_section_that_says_why_it_is_blank_is_not_a_shell(self):
        # The live valuation section: no body, one gap explaining the discipline.
        art = screen([{"title": "S6", "body": "", "claim_refs": [], "numbers": [],
                       "gaps": ["估值一节按 Playbook 的数字纪律留空。"]}])
        check = next(item for item in run_deterministic(art, SCREEN)["checks"]
                     if item["check"] == "required_sections_present")
        self.assertEqual(check["status"], "pass")


class ClaimRefResolutionTests(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.live = self.write_claim("claim-version:" + "1" * 60)
        self.retired = self.write_claim("claim-version:" + "2" * 60, retired=True)

    def write_claim(self, ref, *, retired=False):
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO claim_versions(claim_version_id,claim_ref,version_number,claim_json,"
                "content_hash,created_at) VALUES(?,?,?,?,?,?)",
                (ref, ref.replace("-version", ""), 1, json.dumps({"id": ref}), "0" * 64,
                 "2026-09-01T00:00:00+00:00"),
            )
            if retired:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS claim_retirement_decisions "
                    "(claim_version_ref TEXT, decision TEXT)")
                cur.execute(
                    "INSERT INTO claim_retirement_decisions(claim_version_ref,decision) "
                    "VALUES(?, 'retired')", (ref,))
        return ref

    def check(self, refs):
        art = screen([{"title": "S3", "body": "文字。", "claim_refs": refs, "numbers": [], "gaps": []}])
        return next(item for item in run_deterministic(art, SCREEN, core=self.store.connection)["checks"]
                    if item["check"] == "claim_refs_resolve")

    def test_a_live_ref_resolves(self):
        self.assertEqual(self.check([self.live])["status"], "pass")

    def test_a_retired_ref_is_a_finding(self):
        result = self.check([self.live, self.retired])
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["findings"], [{"code": "retired_claim", "ref": self.retired}])

    def test_an_unknown_ref_is_a_finding(self):
        self.assertEqual(self.check(["claim-version:nope"])["findings"],
                         [{"code": "unknown_claim", "ref": "claim-version:nope"}])

    def test_a_document_that_cites_nothing_fails_rather_than_passes(self):
        self.assertEqual(self.check([])["findings"], [{"code": "no_citations"}])

    def test_without_a_core_the_check_is_skipped_and_says_so(self):
        art = screen([{"title": "S3", "body": "文字。", "claim_refs": [self.live],
                       "numbers": [], "gaps": []}])
        result = next(item for item in run_deterministic(art, SCREEN)["checks"]
                      if item["check"] == "claim_refs_resolve")
        self.assertEqual(result["status"], "skipped")


class AskAnswerCheckTests(unittest.TestCase):
    SHOWN = [
        {"tag": "C1", "ref": "claim-version:a", "period": "2026Q3",
         "statement": "Accenture plc reported Revenues of USD 18718144000, up 5.59%."},
        {"tag": "C2", "ref": "claim-version:b", "period": "2026Q2",
         "statement": "Accenture states demand may not be replaced by AI-enabled solutions."},
    ]

    def answer(self, text, *, citations, confidence="medium", gaps=()):
        return artefact_from_ask_answer(
            {"question": "ACN 收入增长多少？", "answer": text,
             "citations": [{"tag": tag} for tag in citations],
             "confidence": confidence, "gaps": list(gaps)},
            shown_claims=self.SHOWN, ref="cockpit-ask:test")

    def result(self, art, check):
        return next(item for item in run_deterministic(art, ASK)["checks"] if item["check"] == check)

    def test_an_answer_may_only_cite_what_it_was_shown(self):
        art = self.answer("收入增长 5.59%。", citations=["C1", "C9"])
        self.assertEqual(self.result(art, "cites_only_shown_claims")["findings"],
                         [{"code": "unshown_tag", "tag": "C9"}])

    def test_a_number_the_cited_claims_do_not_carry_is_invented(self):
        art = self.answer("收入增长 5.59%，经营利润率 15.4%。", citations=["C1"])
        self.assertEqual(self.result(art, "numbers_without_refs")["findings"],
                         [{"section": "答案", "figure": "15.4%"}])

    def test_a_correct_unit_conversion_is_still_an_invented_number(self):
        # 187.18 亿 is USD 18718144000. It is right and it is not the number in
        # the Ledger, and a reader cannot take it to the filing.
        art = self.answer("收入约 187.18 亿美元。", citations=["C1"])
        self.assertEqual(self.result(art, "numbers_without_refs")["status"], "fail")

    def test_an_honest_unknown_passes_every_deterministic_check(self):
        art = self.answer("我不知道；账本里没有 bookings 的记录。", citations=[],
                          confidence="low", gaps=["缺 bookings"])
        result = run_deterministic(art, ASK)
        self.assertTrue(result["passed"], result["failed_checks"])

    def test_a_missing_confidence_is_a_finding(self):
        art = self.answer("收入增长 5.59%。", citations=["C1"], confidence=None)
        self.assertEqual(self.result(art, "confidence_stated")["status"], "fail")


class DossierCheckTests(unittest.TestCase):
    def dossier(self, sections, *, prior=None, ref="company-dossier-version:test"):
        return artefact_from_dossier(
            {"id": ref, "content_hash": "a" * 64, "subject_ref": ACN, "sections": sections},
            prior=None if prior is None else {"sections": prior})

    def result(self, art, check):
        return next(item for item in run_deterministic(art, DOSSIER)["checks"] if item["check"] == check)

    def sections(self, body="第一版的文字，足够长以便相似度有意义地计算，讲的是 ACN 的业务结构。"):
        return [{"title": "业务与收入构成", "body": body, "claim_refs": ["claim-version:a"],
                 "numbers": [], "gaps": []}]

    def test_a_section_with_prose_and_no_citation_is_a_finding(self):
        art = self.dossier([{"title": "竞争位置与壁垒", "body": "壁垒稳固。", "claim_refs": [],
                             "numbers": [], "gaps": []}])
        self.assertEqual(self.result(art, "every_section_cites")["findings"],
                         [{"code": "section_without_citation", "title": "竞争位置与壁垒"}])

    def test_a_new_version_with_no_new_reference_is_a_finding(self):
        prior = self.sections()
        art = self.dossier(self.sections("完全不同的文字，但引用一条也没有变，说的还是同一件事。"),
                           prior=prior)
        self.assertEqual(self.result(art, "new_version_cites_new_refs")["status"], "fail")

    def test_a_first_version_has_nothing_to_compare_and_is_skipped(self):
        art = self.dossier(self.sections())
        self.assertEqual(self.result(art, "new_version_cites_new_refs")["status"], "skipped")
        self.assertEqual(self.result(art, "restatement_drift")["status"], "skipped")

    def test_a_version_that_only_rewords_every_section_is_drift(self):
        prior = self.sections()
        reworded = self.sections(
            "第一版的文字，足够长以便相似度有意义地计算，讲的是 ACN 的业务结构与构成。")
        art = self.dossier(reworded, prior=prior)
        self.assertEqual(self.result(art, "restatement_drift")["status"], "fail")

    def test_a_version_that_advances_one_section_is_not_drift(self):
        # Judging section by section would make honest, narrow revisions the
        # most-punished kind: a version that revisits one section and leaves
        # the rest alone is a normal version.
        prior = self.sections() + [
            {"title": "多空辩论焦点", "body": "上一版对多空辩论的描述，篇幅足够做相似度比较。",
             "claim_refs": ["claim-version:b"], "numbers": [], "gaps": []}]
        current = self.sections() + [
            {"title": "多空辩论焦点",
             "body": "这一版把辩论焦点收窄到替代速度与定价权两件事，并说明哪一条证据能定它。",
             "claim_refs": ["claim-version:b", "claim-version:c"], "numbers": [], "gaps": []}]
        art = self.dossier(current, prior=prior)
        result = self.result(art, "restatement_drift")
        self.assertEqual(result["status"], "pass")
        # And the unchanged section is still reported, because a reader should
        # be able to see how narrow the revision was.
        self.assertEqual([finding["title"] for finding in result["findings"]], ["业务与收入构成"])


class FakeModel:
    """Stands in for CockpitModel: returns whatever text it was handed."""

    def __init__(self, *replies, fail=None):
        self.replies = list(replies)
        self.fail = fail
        self.calls = []

    def call(self, *, purpose, request_id, prompt, mission):
        self.calls.append({"purpose": purpose, "request_id": request_id, "prompt": prompt})
        if self.fail is not None:
            from dalton_core.cockpit_model import CockpitModelError

            raise CockpitModelError(self.fail)
        return {"text": self.replies.pop(0), "replayed": False, "cost_micros": 1234,
                "work_order_ref": "work:cockpit-quality-abc", "invocation_ref": "invocation:abc",
                "result_envelope_ref": "result:abc", "route_decision_ref": "route:abc"}


def full_scores(rubric_obj, score=3, evidence="文档的这一部分支持这个分数"):
    return {"scores": [{"criterion_id": criterion_id, "score": score, "evidence": evidence}
                       for criterion_id in rubric_obj.criterion_ids]}


class JudgeValidationTests(unittest.TestCase):
    """What comes back is verified before it is believed."""

    def test_a_complete_reply_is_accepted_and_ordered_like_the_rubric(self):
        reply = full_scores(ASK)
        reply["scores"].reverse()
        validated = validate_judge_output(reply, ASK)
        self.assertEqual([row["criterion_id"] for row in validated["scores"]],
                         list(ASK.criterion_ids))

    def test_a_missing_criterion_is_refused(self):
        reply = full_scores(ASK)
        reply["scores"].pop()
        with self.assertRaises(ResearchQualityValidationError) as caught:
            validate_judge_output(reply, ASK)
        self.assertIn("did not score every criterion", str(caught.exception))

    def test_a_criterion_the_rubric_does_not_have_is_refused(self):
        reply = full_scores(ASK)
        reply["scores"][0]["criterion_id"] = "writing_style"
        with self.assertRaises(ResearchQualityValidationError):
            validate_judge_output(reply, ASK)

    def test_a_score_out_of_range_is_refused(self):
        reply = full_scores(ASK)
        reply["scores"][0]["score"] = 5
        with self.assertRaises(ResearchQualityValidationError):
            validate_judge_output(reply, ASK)

    def test_a_score_that_is_not_a_whole_number_is_refused(self):
        for value in (3.5, "3", True, None):
            reply = full_scores(ASK)
            reply["scores"][0]["score"] = value
            with self.subTest(value=value), self.assertRaises(ResearchQualityValidationError):
                validate_judge_output(reply, ASK)

    def test_an_extra_key_anywhere_is_refused(self):
        reply = full_scores(ASK)
        reply["overall"] = 3
        with self.assertRaises(ResearchQualityValidationError):
            validate_judge_output(reply, ASK)
        reply = full_scores(ASK)
        reply["scores"][0]["confidence"] = "high"
        with self.assertRaises(ResearchQualityValidationError):
            validate_judge_output(reply, ASK)

    def test_an_essay_where_a_sentence_belongs_is_refused(self):
        reply = full_scores(ASK, evidence="第一句。第二句。第三句。")
        with self.assertRaises(ResearchQualityValidationError):
            validate_judge_output(reply, ASK)

    def test_a_duplicate_criterion_is_refused(self):
        reply = full_scores(ASK)
        reply["scores"][1]["criterion_id"] = reply["scores"][0]["criterion_id"]
        with self.assertRaises(ResearchQualityValidationError) as caught:
            validate_judge_output(reply, ASK)
        self.assertIn("twice", str(caught.exception))


class JudgePurposeRegistrationTests(unittest.TestCase):
    """P14-0: the lane names its own purpose, from its own module.

    A cockpit call with an unregistered purpose is refused, and the purpose is
    what the WorkOrder is identified by and what the day ledger accounts
    against. Importing this module is what makes the judge reachable, so
    importing it is what registers.
    """

    def test_importing_the_scorer_registers_the_purpose_it_calls_with(self):
        from dalton_core.cockpit_model import purposes

        self.assertEqual(JUDGE_PURPOSE, "quality")
        self.assertIn(JUDGE_PURPOSE, purposes())
        self.assertIn("quality_verifier", purposes())

    def test_a_work_order_can_actually_be_built_for_it(self):
        from dalton_core.cockpit_model import build_work

        order = build_work(
            purpose=JUDGE_PURPOSE, request_id="r", prompt="grade this",
            mission_version_ref="coverage-mission-version:us-it-services:1",
            max_input_tokens=8_000, max_output_tokens=1_000, max_cost_usd=0.5,
            max_seconds=60, created_at="2026-09-09T00:00:00.000000+00:00",
        )
        self.assertIn("quality", order.id)
        self.assertEqual(order.metadata["purpose"], "quality")

    def test_the_judge_runs_on_an_already_installed_model_configuration(self):
        from dalton_core.model_configurations import model_config_names

        # Reusing the drafting configuration rather than installing another:
        # same route, same broker, same day ledger as the drafting it grades.
        self.assertIn(JUDGE_MODEL_CONFIG_NAME, model_config_names())


class JudgeCallTests(unittest.TestCase):
    def artefact(self):
        return screen([{"title": "答案", "body": "收入增长 5.95%。",
                        "claim_refs": ["claim-version:a"],
                        "numbers": [number("claim-version:a")], "gaps": []}])

    def test_the_prompt_carries_the_rubric_the_checks_and_the_cited_claims(self):
        art = self.artefact()
        prompt = build_judge_prompt(art, SCREEN, run_deterministic(art, SCREEN))
        self.assertIn("number_provenance", prompt)
        self.assertIn("0 = ", prompt)
        self.assertIn("Checks already run mechanically", prompt)
        self.assertIn("residual_citation_artefacts", prompt)
        self.assertIn("| ref | period | statement |", prompt)
        self.assertIn("18742125000", prompt)
        # And the notes that stop it marking the document down for obeying its
        # own contract.
        self.assertIn("估值一节", prompt)

    def test_a_good_reply_is_scored_with_its_provenance(self):
        art = self.artefact()
        model = FakeModel(json.dumps(full_scores(SCREEN)))
        result = judge(art, SCREEN, run_deterministic(art, SCREEN), model=model,
                       mission={"id": "m", "content_hash": "h"}, request_id="r")
        self.assertEqual(result["status"], "scored")
        self.assertEqual(result["summary"]["criteria"], len(SCREEN.criterion_ids))
        self.assertEqual(result["model"]["invocation_ref"], "invocation:abc")
        self.assertEqual(result["model"]["purpose"], "quality")
        self.assertEqual(model.calls[0]["purpose"], "quality")

    def test_a_reply_that_does_not_verify_is_refused_rather_than_repaired(self):
        art = self.artefact()
        model = FakeModel(json.dumps({"scores": [{"criterion_id": "number_provenance",
                                                  "score": 4, "evidence": "很好"}]}))
        result = judge(art, SCREEN, run_deterministic(art, SCREEN), model=model,
                       mission={"id": "m", "content_hash": "h"}, request_id="r")
        self.assertEqual(result["status"], "refused")
        self.assertIn("did not score every criterion", result["reason"])
        self.assertNotIn("scores", result)

    def test_prose_around_the_json_is_tolerated(self):
        art = self.artefact()
        model = FakeModel("```json\n" + json.dumps(full_scores(SCREEN)) + "\n```")
        result = judge(art, SCREEN, run_deterministic(art, SCREEN), model=model,
                       mission={"id": "m", "content_hash": "h"}, request_id="r")
        self.assertEqual(result["status"], "scored")

    def test_a_failed_call_is_a_refusal_with_the_reason(self):
        art = self.artefact()
        model = FakeModel(fail="today's research budget refused the call")
        result = judge(art, SCREEN, run_deterministic(art, SCREEN), model=model,
                       mission={"id": "m", "content_hash": "h"}, request_id="r")
        self.assertEqual(result["status"], "refused")
        self.assertIn("budget", result["reason"])

    def test_the_summary_names_the_criteria_below_passing(self):
        summary = summarise_scores([
            {"criterion_id": "a", "score": 4, "evidence": "x"},
            {"criterion_id": "b", "score": 1, "evidence": "y"},
            {"criterion_id": "c", "score": 0, "evidence": "z"},
        ])
        self.assertEqual(summary["below_passing"], ["b", "c"])
        self.assertEqual(summary["minimum"], 0)


class VerifierTests(unittest.TestCase):
    def artefact(self):
        return screen([{"title": "S3", "body": "文字。", "claim_refs": ["claim-version:a"],
                        "numbers": [], "gaps": []}])

    def judged(self):
        return {"status": "scored", "scores": full_scores(SCREEN)["scores"],
                "model": {"route_decision_ref": "route:producer"}}

    def test_a_pass_verdict_carries_no_findings(self):
        self.assertEqual(validate_verifier_output({"verdict": "pass", "findings": []}, SCREEN),
                         {"verdict": "pass", "findings": []})
        with self.assertRaises(ResearchQualityValidationError):
            validate_verifier_output(
                {"verdict": "pass", "findings": [
                    {"criterion_id": "gaps_honest", "code": "criterion_misread", "detail": "不对"}]},
                SCREEN)

    def test_a_reject_verdict_must_say_why(self):
        with self.assertRaises(ResearchQualityValidationError):
            validate_verifier_output({"verdict": "reject", "findings": []}, SCREEN)

    def test_an_unknown_verdict_or_code_is_refused(self):
        with self.assertRaises(ResearchQualityValidationError):
            validate_verifier_output({"verdict": "maybe", "findings": []}, SCREEN)
        with self.assertRaises(ResearchQualityValidationError):
            validate_verifier_output(
                {"verdict": "reject", "findings": [
                    {"criterion_id": "gaps_honest", "code": "bad_vibes", "detail": "不对"}]},
                SCREEN)

    def test_the_verdict_is_bound_to_the_scores_it_read(self):
        model = FakeModel(json.dumps({"verdict": "pass", "findings": []}))
        judged = self.judged()
        result = verify(self.artefact(), SCREEN, judged, model=model,
                        mission={"id": "m", "content_hash": "h"}, request_id="v")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["judged_scores_hash"], content_hash(judged["scores"]))
        self.assertEqual(model.calls[0]["purpose"], "quality_verifier")

    def test_an_unattributed_producer_costs_no_verifier_call(self):
        model = FakeModel(json.dumps({"verdict": "pass", "findings": []}))
        judged = {"status": "scored", "scores": full_scores(SCREEN)["scores"]}
        result = verify(self.artefact(), SCREEN, judged, model=model,
                        mission={"id": "m", "content_hash": "h"}, request_id="v")
        self.assertEqual(result["status"], "refused")
        self.assertEqual(model.calls, [])

    def test_there_is_nothing_to_verify_when_the_judge_refused(self):
        result = verify(self.artefact(), SCREEN, {"status": "refused", "reason": "x"},
                        model=FakeModel(), mission={"id": "m", "content_hash": "h"}, request_id="v")
        self.assertEqual(result["status"], "skipped")

    def test_the_verifier_only_returns_a_verdict(self):
        # It is given the artefact and the scores and asked one question; a
        # verifier that re-grades is a second judge, not a check on the first.
        model = FakeModel(json.dumps(full_scores(SCREEN)))
        result = verify(self.artefact(), SCREEN, self.judged(), model=model,
                        mission={"id": "m", "content_hash": "h"}, request_id="v")
        self.assertEqual(result["status"], "refused")


class QualityScoreAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.authority = QualityScoreAuthority(self.store)
        self.art = screen([{"title": "S3", "body": "文字。", "claim_refs": ["claim-version:a"],
                            "numbers": [], "gaps": []}])
        self.deterministic = run_deterministic(self.art, SCREEN)

    def record(self, **overrides):
        params = {
            "artefact_kind": "initial_screen", "target_ref": self.art["ref"],
            "target_hash": self.art["hash"], "rubric": SCREEN,
            "deterministic": self.deterministic, "actor_ref": "automation:coverage-mission",
            "subject_ref": ACN,
        }
        params.update(overrides)
        return self.authority.record(**params)

    @staticmethod
    def judged(*, status="scored", route="route:abc", scores=None):
        layer = {"status": status, "rubric_hash": SCREEN.content_hash,
                 "model": {"route_decision_ref": route, "purpose": "quality",
                           "invocation_ref": "invocation:x"}}
        if status == "scored":
            layer["scores"] = scores or full_scores(SCREEN)["scores"]
        else:
            layer["reason"] = "the judge did not score every criterion"
        return layer

    def test_a_score_reads_back_and_names_its_rubric_and_its_target(self):
        written = self.record()
        self.assertEqual(written["status"], "fresh")
        self.assertEqual(written["rubric_hash"], SCREEN.content_hash)
        self.assertEqual(written["target_hash"], self.art["hash"])
        self.assertEqual(written["scorer_version"], SCORER_VERSION)
        self.assertEqual(self.authority.latest(written["score_ref"])["id"], written["id"])

    def test_rescoring_the_same_document_the_same_way_is_a_duplicate(self):
        first = self.record()
        second = self.record()
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.authority.versions(first["score_ref"])), 1)

    def test_a_rewritten_document_is_a_new_version_of_the_same_chain(self):
        first = self.record()
        rewritten = screen([{"title": "S3", "body": "改写过的文字。", "claim_refs": ["claim-version:a"],
                             "numbers": [], "gaps": []}], digest="b" * 64)
        second = self.record(target_hash=rewritten["hash"],
                             deterministic=run_deterministic(rewritten, SCREEN))
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["prior_version_ref"], first["id"])

    def test_a_different_route_is_a_different_score(self):
        # The identity is derived from what actually answered, so a judgement
        # from another profile is another score rather than a duplicate.
        self.record(judge_layer=self.judged(route="route:one"))
        second = self.record(judge_layer=self.judged(route="route:two"))
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(second["version"], 2)

    def test_the_same_route_answering_twice_is_still_a_duplicate(self):
        first = self.record(judge_layer=self.judged())
        again = self.record(judge_layer=self.judged())
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], first["id"])

    def test_a_caller_cannot_declare_its_own_identity(self):
        # The whole duplicate rule rests on the caller not choosing the
        # fingerprint, so record() does not accept one.
        with self.assertRaises(TypeError):
            self.record(model_config_fingerprint="fingerprint:whatever")

    def test_a_scored_judgement_must_name_the_route_it_ran_under(self):
        layer = self.judged()
        layer["model"].pop("route_decision_ref")
        with self.assertRaises(ResearchQualityConflict) as caught:
            self.record(judge_layer=layer)
        self.assertIn("route decision", str(caught.exception))

    def test_a_refusal_does_not_permanently_consume_the_identity(self):
        # One malformed reply would otherwise block this document from ever
        # being judged under this rubric again.
        refused = self.record(judge_layer=self.judged(status="refused"))
        self.assertEqual(refused["status"], "fresh")
        scored = self.record(judge_layer=self.judged())
        self.assertEqual(scored["status"], "fresh")
        self.assertEqual(scored["version"], 2)
        self.assertEqual(scored["scoring_identity_hash"], refused["scoring_identity_hash"])
        # And the refusal is still in the chain: it happened.
        self.assertEqual([item["judge"]["status"]
                          for item in self.authority.versions(scored["score_ref"])],
                         ["refused", "scored"])

    def test_a_second_refusal_is_a_duplicate_so_a_retry_loop_cannot_fill_the_chain(self):
        first = self.record(judge_layer=self.judged(status="refused"))
        again = self.record(judge_layer=self.judged(status="refused"))
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], first["id"])

    def test_a_settled_score_is_not_superseded_by_another_judgement(self):
        self.record(judge_layer=self.judged())
        again = self.record(judge_layer=self.judged())
        self.assertEqual(again["status"], "duplicate")

    def test_a_verifier_verdict_must_be_bound_to_these_scores(self):
        judged = self.judged()
        good = {"status": "verified", "verdict": "pass", "findings": [],
                "judged_scores_hash": content_hash(judged["scores"])}
        self.assertEqual(self.record(judge_layer=judged, verifier_layer=good)["status"], "fresh")
        stray = {**good, "judged_scores_hash": "0" * 64}
        with self.assertRaises(ResearchQualityConflict) as caught:
            self.record(judge_layer=self.judged(route="route:two"), verifier_layer=stray)
        self.assertIn("bound to different scores", str(caught.exception))

    def test_a_verdict_without_a_judgement_is_refused(self):
        with self.assertRaises(ResearchQualityConflict):
            self.record(verifier_layer={"status": "verified", "verdict": "pass", "findings": [],
                                        "judged_scores_hash": "0" * 64})

    def test_two_targets_whose_refs_end_alike_do_not_share_a_chain(self):
        # Slicing the tail off a ref put two documents in one version chain.
        tail = "x" * 70
        first = self.record(target_ref=f"mission-deliverable-version:a{tail}")
        second = self.record(target_ref=f"mission-deliverable-version:b{tail}")
        self.assertNotEqual(first["score_ref"], second["score_ref"])
        self.assertEqual(second["version"], 1)

    def test_the_two_layers_are_stored_apart(self):
        written = self.record(judge_layer=self.judged())
        self.assertEqual(written["deterministic"]["scorer_version"], SCORER_VERSION)
        self.assertEqual(written["judge"]["model"]["invocation_ref"], "invocation:x")
        self.assertIsNone(written["verifier"])
        row = self.store.connection.execute(
            "SELECT judge_status FROM research_quality_score_versions WHERE version_id=?",
            (written["id"],)).fetchone()
        self.assertEqual(row["judge_status"], "scored")

    def test_a_deterministic_layer_from_another_rubric_is_refused(self):
        other = run_deterministic(self.art, ASK)
        with self.assertRaises(ResearchQualityConflict):
            self.record(deterministic=other)

    def test_a_deterministic_layer_from_another_document_is_refused(self):
        with self.assertRaises(ResearchQualityConflict):
            self.record(target_hash="c" * 64)

    def test_an_actor_that_is_neither_human_nor_automation_is_refused(self):
        with self.assertRaises(ResearchQualityValidationError):
            self.record(actor_ref="system:whatever")

    def test_scores_are_append_only_at_the_database(self):
        written = self.record()
        with self.assertRaises(Exception):
            self.store.connection.execute(
                "UPDATE research_quality_score_versions SET content_hash='x' WHERE version_id=?",
                (written["id"],))
        with self.assertRaises(Exception):
            self.store.connection.execute("DELETE FROM research_quality_score_versions")

    def test_a_write_outside_the_authority_is_refused(self):
        with self.assertRaises(Exception) as caught:
            self.store.connection.execute(
                "INSERT INTO research_quality_score_versions(version_id,score_ref,version_number,"
                "artefact_kind,target_ref,target_hash,rubric_ref,rubric_hash,scorer_version,"
                "model_config_fingerprint,scoring_identity_hash,record_json,content_hash,actor_ref,"
                "created_at) VALUES('v','s',1,'initial_screen','t','h','r','rh','0.1','none','i',"
                "'{}','c','human:x','2026-09-09')")
        self.assertIn("QualityScoreAuthority", str(caught.exception))

    def test_scores_for_a_target_come_back_in_order(self):
        self.record()
        self.record(judge_layer=self.judged())
        self.assertEqual([item["version"] for item in self.authority.scores_for(self.art["ref"])],
                         [1, 2])


class ScoreArtefactTests(unittest.TestCase):
    def test_without_a_model_only_the_deterministic_layer_runs(self):
        art = screen([{"title": "S3", "body": "文字。", "claim_refs": ["c"], "numbers": [], "gaps": []}])
        result = score_artefact(art, "initial_screen")
        self.assertIsNone(result["judge"])
        self.assertIsNone(result["verifier"])
        self.assertTrue(result["deterministic"]["checks"])

    def test_the_judge_layer_needs_the_mission_it_is_billed_to(self):
        art = screen([{"title": "S3", "body": "文字。", "claim_refs": ["c"], "numbers": [], "gaps": []}])
        with self.assertRaises(ResearchQualityValidationError):
            score_artefact(art, "initial_screen", model=FakeModel("{}"))

    def test_the_fingerprint_comes_from_what_answered_not_from_the_caller(self):
        scored = {"status": "scored", "model": {"route_decision_ref": "route:a", "purpose": "quality"}}
        same = {"status": "scored", "model": {"route_decision_ref": "route:a", "purpose": "quality",
                                              "invocation_ref": "invocation:different"}}
        other = {"status": "scored", "model": {"route_decision_ref": "route:b", "purpose": "quality"}}
        self.assertEqual(judge_fingerprint(scored), judge_fingerprint(same))
        self.assertNotEqual(judge_fingerprint(scored), judge_fingerprint(other))
        self.assertEqual(judge_fingerprint(None), "none")
        # The outcome is not part of it: a model that answered and a model that
        # answered badly are the same model, which is what lets a real
        # judgement supersede a refusal as a new version of the same score.
        self.assertEqual(judge_fingerprint(scored),
                         judge_fingerprint({**scored, "status": "refused"}))


class DeliverableAdapterTests(unittest.TestCase):
    def test_a_published_deliverable_becomes_a_scoreable_artefact(self):
        record = {"id": "mission-deliverable-version:x", "content_hash": "d" * 64,
                  "kind": "initial_screen", "subject_ref": ACN,
                  "deliverable_ref": "mission-deliverable:initial_screen:0001467373",
                  "sections": [{"title": "S3", "body": "文字。", "claim_refs": ["c"],
                                "numbers": [], "gaps": []}],
                  "gaps": ["缺 bookings"]}
        art = artefact_from_deliverable(record)
        self.assertEqual(art["ref"], record["id"])
        self.assertEqual(art["hash"], record["content_hash"])
        self.assertEqual(len(art["expected_sections"]), 8)

    def test_the_playbook_it_was_published_under_wins_over_the_frozen_copy(self):
        record = {"id": "mission-deliverable-version:x", "content_hash": "d" * 64,
                  "kind": "initial_screen", "subject_ref": ACN, "sections": [], "gaps": []}
        art = artefact_from_deliverable(
            record, playbook={"deliverable_templates": {"initial_screen": ["A", "B"]}})
        self.assertEqual(art["expected_sections"], ["A", "B"])

    def test_an_answer_gets_a_hash_of_what_it_said_and_what_it_cited(self):
        shown = [{"tag": "C1", "ref": "claim-version:a", "statement": "收入为 USD 100。",
                  "period": "2026Q1"}]
        first = artefact_from_ask_answer(
            {"question": "q", "answer": "a", "citations": [{"tag": "C1"}], "confidence": "low"},
            shown_claims=shown, ref="cockpit-ask:1")
        same = artefact_from_ask_answer(
            {"question": "q", "answer": "a", "citations": [{"tag": "C1"}], "confidence": "low"},
            shown_claims=shown, ref="cockpit-ask:2")
        different = artefact_from_ask_answer(
            {"question": "q", "answer": "a different answer", "citations": [{"tag": "C1"}],
             "confidence": "low"},
            shown_claims=shown, ref="cockpit-ask:3")
        self.assertEqual(first["hash"], same["hash"])
        self.assertNotEqual(first["hash"], different["hash"])


if __name__ == "__main__":
    unittest.main()
