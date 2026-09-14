from __future__ import annotations

import unittest

from dalton_core.research_localization import source_content_hash
from dalton_core.research_language_review import (
    build_brain_prompt, build_checker_prompt, publish_language_attachment, run_language_review,
    validate_checker_output,
)

IDENTITY = {"provider": "antigravity-cli-gateway",
            "model": "antigravity-cli-gateway/gemini-3.8-flash"}


def product():
    return {"kind": "memo", "version_ref": "memo-version:1", "sections": [
        {"title": "结论", "body": "收入为 15623445 USD，利润率为 5.24%。", "gaps": []}
    ]}


def review():
    return {"overall": "金额单位不便阅读。", "suggestions": [{
        "section_index": 0, "quote": "收入为 15623445 USD",
        "assessment": "大额数字不易扫读", "suggestion": "收入为 1562 万美元",
    }]}


class ResearchLanguageReviewTests(unittest.TestCase):
    def test_checker_treats_generated_statements_as_prose_not_quotes(self):
        prompt = build_checker_prompt(product())
        self.assertIn("normalized_statement", prompt)
        self.assertIn("not a verbatim source quotation", prompt)

    def test_original_numeric_source_allows_restoring_exact_precision(self):
        source = {"kind": "ui_text", "version_ref": "ui:1", "sections": [{
            "title": "收入", "body": "Revenue was 1535000000 USD.", "gaps": [],
        }]}
        draft = {**source, "sections": [{
            "index": 0, "title": "收入", "body": "收入为15.35亿美元。", "gaps": [],
        }]}
        checker = {"overall": "表达清楚。", "suggestions": []}
        brain = {"decisions": [], "sections": [{
            "index": 0, "title": "收入",
            "body": "收入为1535000000美元。", "gaps": [],
        }]}
        result = run_language_review(
            draft, checker=lambda _: checker, brain=lambda _: brain,
            checker_identity=IDENTITY, numeric_source_product=source,
        )
        self.assertEqual(result["status"], "ready_for_publication")
        self.assertEqual(result["numeric_source_hash"], source_content_hash(source))
        without_source = run_language_review(
            draft, checker=lambda _: checker, brain=lambda _: brain,
            checker_identity=IDENTITY,
        )
        self.assertEqual(without_source["status"], "pending_brain_revision")

    def test_numeric_source_must_match_identity_and_draft_numbers(self):
        source = {"kind": "ui_text", "version_ref": "ui:1", "sections": [{
            "title": "收入", "body": "Revenue was 1535000000 USD.", "gaps": [],
        }]}
        checker = {"overall": "表达清楚。", "suggestions": []}
        brain = {"decisions": [], "sections": [{
            "index": 0, "title": "收入", "body": "收入为15.35亿美元。", "gaps": [],
        }]}
        for draft in (
            {**source, "version_ref": "ui:2", "sections": brain["sections"]},
            {**source, "sections": [{"index": 0, "title": "收入", "body": "收入为15.36亿美元。", "gaps": []}]},
        ):
            with self.assertRaises(ValueError):
                run_language_review(
                    draft, checker=lambda _: checker, brain=lambda _: brain,
                    checker_identity=IDENTITY, numeric_source_product=source,
                )

    def test_checker_recovers_unique_quote_location_and_terminal_excerpt(self):
        sections = [{'title': '公司', 'body': '公司概况。', 'gaps': []},
                    {'title': '风险', 'body': 'AI 需求仍处于早期，收入转化需要时间。', 'gaps': []}]
        value = {'overall': '用词可以更自然。', 'suggestions': [{
            'section_index': 0, 'quote': 'AI 需求仍处于早期……',
            'assessment': '可更简洁。', 'suggestion': 'AI 需求刚刚起步。'}]}
        fixed = validate_checker_output(value, sections=sections)
        self.assertEqual(fixed['suggestions'][0]['section_index'], 1)
        self.assertEqual(fixed['suggestions'][0]['quote'], value['suggestions'][0]['quote'])
        self.assertEqual(value['suggestions'][0]['section_index'], 0)
        for quote in ['AI 需求……转化需要时间。', '……', 'AI 需求已经成熟……']:
            value['suggestions'][0]['quote'] = quote
            with self.assertRaises(ValueError):
                validate_checker_output(value, sections=sections)

    def test_checker_redundant_title_must_match_and_ambiguous_quote_is_rejected(self):
        sections = [{'title': '结论', 'body': '需求企稳。', 'gaps': []}]
        value = {'overall': '可简化。', 'suggestions': [{
            'section_index': 0, 'title': '结论', 'quote': '需求企稳。',
            'assessment': '可更直接。', 'suggestion': '需求趋稳。'}]}
        self.assertNotIn('title', validate_checker_output(value, sections=sections)['suggestions'][0])
        value['suggestions'][0]['title'] = '不存在的标题'
        with self.assertRaises(ValueError): validate_checker_output(value, sections=sections)
        del value['suggestions'][0]['title']
        with self.assertRaises(ValueError):
            validate_checker_output(value, sections=[{'title': '空', 'body': '', 'gaps': []}, *sections, *sections])

    def test_checker_accepts_exact_body_alias_for_assessment(self):
        sections = [{"title": "标题", "body": "原句。", "gaps": []}]
        value = {"overall": "可调整。", "suggestions": [{
            "section_index": 0, "quote": "原句。",
            "body": "句式略显生硬。", "suggestion": "建议句。",
        }]}
        checked = validate_checker_output(value, sections=sections)
        self.assertEqual(checked["suggestions"][0]["assessment"], "句式略显生硬。")
        self.assertNotIn("body", checked["suggestions"][0])

    def test_checker_accepts_only_matching_redundant_numeric_index(self):
        sections = [{"title": "标题", "body": "原句。", "gaps": []}]
        item = {"index": 0, "section_index": 0, "quote": "原句。",
                "assessment": "句式略显生硬。", "suggestion": "建议句。"}
        checked = validate_checker_output(
            {"overall": "可调整。", "suggestions": [item]}, sections=sections)
        self.assertNotIn("index", checked["suggestions"][0])
        for bad in (1, True, "0"):
            item["index"] = bad
            with self.assertRaisesRegex(ValueError, "redundant index"):
                validate_checker_output(
                    {"overall": "可调整。", "suggestions": [item]}, sections=sections)

    def test_checker_body_alias_remains_closed(self):
        sections = [{"title": "标题", "body": "原句。", "gaps": []}]
        for item in (
            {"section_index": 0, "quote": "原句。", "body": " ", "suggestion": "建议句。"},
            {"section_index": 0, "quote": "原句。", "body": "说明。", "assessment": "另一说明。", "suggestion": "建议句。"},
            {"section_index": 0, "quote": "原句。", "body": "说明。", "suggestion": "建议句。", "unexpected": "x"},
        ):
            value = {"overall": "可调整。", "suggestions": [item]}
            with self.assertRaisesRegex(ValueError, "invalid shape"):
                validate_checker_output(value, sections=sections)

    def test_quote_matches_escaped_zero_width_character_without_losing_visible_content(self):
        source=[{'title':'状态','body':r"m\u200banagement 分类无效。",'gaps':[]}]
        value={'overall':'分类名有不可见字符。','suggestions':[
            {'section_index':0,'quote':'m\u200banagement 分类无效。',
             'assessment':'分类名应放在技术详情。','suggestion':'该分类尚未启用。'}]}
        self.assertEqual(validate_checker_output(value,sections=source),value)
        for wrong in ['另一个分类无效。','\u200b']:
            value['suggestions'][0]['quote']=wrong
            with self.assertRaises(ValueError):validate_checker_output(value,sections=source)

    def test_publication_adapter_saves_review_before_publishing_attachment(self):
        order = []
        result = publish_language_attachment(
            product(), checker=lambda prompt: review(),
            brain=lambda prompt: {
                "decisions": [{"suggestion_index": 0, "decision": "reject",
                               "reason": "保留精确原值。"}],
                "sections": [{"index": 0, "title": "结论",
                              "body": "收入为 15623445 USD，利润率为 5.24%。", "gaps": []}],
            }, checker_identity=IDENTITY,
            save_review=lambda row: order.append(("save", row["status"])),
            publish_attachment=lambda source, row: (
                order.append(("publish", row["source_hash"])) or {"status": "published"}),
        )
        self.assertEqual([item[0] for item in order], ["save", "publish"])
        self.assertEqual(result["attachment"], {"status": "published"})

    def test_publication_adapter_saves_failure_and_never_publishes(self):
        order = []
        result = publish_language_attachment(
            product(), checker=lambda prompt: {"bad": True}, brain=lambda prompt: {},
            checker_identity=IDENTITY,
            save_review=lambda row: order.append(("save", row["status"])),
            publish_attachment=lambda source, row: order.append(("publish", "bad")),
        )
        self.assertEqual(order, [("save", "pending_language_review")])
        self.assertEqual(result["status"], "pending_language_review")

    def test_brain_prompt_omits_large_authority_and_source_payloads(self):
        source = product(); source["sources"] = [{"raw": "secret-large-authority"}]
        prompt = build_brain_prompt(source, review())
        self.assertNotIn("secret-large-authority", prompt)
        self.assertIn("source_hash", prompt)

    def test_checker_once_then_brain_once_and_preserves_advice_and_decision(self):
        calls = []
        def checker(prompt):
            calls.append(("checker", prompt)); return review()
        def brain(prompt):
            calls.append(("brain", prompt)); return {
                "decisions": [{"suggestion_index": 0, "decision": "adopt",
                               "reason": "单位换算更易读且原值可反算。"}],
                "sections": [{"index": 0, "title": "结论",
                              "body": "收入为 1562 万美元，利润率为 5.2%。", "gaps": []}],
            }
        result = run_language_review(
            product(), checker=checker, brain=brain,
            checker_identity=IDENTITY)
        self.assertEqual([name for name, _ in calls], ["checker", "brain"])
        self.assertEqual(result["status"], "ready_for_publication")
        self.assertIn("金额单位不便阅读", result["suggestions_markdown"])
        self.assertEqual(result["brain_revision"]["decisions"][0]["decision"], "adopt")
        self.assertEqual(result["language_scope"], "readability_only_not_fact_or_number_verification")

    def test_illegal_number_change_remains_pending_and_is_not_rechecked(self):
        calls = []
        def checker(prompt):
            calls.append("checker"); return review()
        def brain(prompt):
            calls.append("brain"); return {
                "decisions": [{"suggestion_index": 0, "decision": "adopt", "reason": "更简洁"}],
                "sections": [{"index": 0, "title": "结论",
                              "body": "收入为 1700 万美元，利润率为 5.2%。", "gaps": []}],
            }
        result = run_language_review(
            product(), checker=checker, brain=brain,
            checker_identity=IDENTITY)
        self.assertEqual(calls, ["checker", "brain"])
        self.assertEqual(result["status"], "pending_brain_revision")
        self.assertIn("number tokens", result["reason"])
        self.assertIn("suggestions_markdown", result)

    def test_checker_failure_does_not_call_brain_or_claim_success(self):
        calls = []
        def checker(prompt):
            calls.append("checker"); return {"bad": True}
        def brain(prompt):
            calls.append("brain"); return {}
        result = run_language_review(
            product(), checker=checker, brain=brain,
            checker_identity=IDENTITY)
        self.assertEqual(calls, ["checker"])
        self.assertEqual(result["status"], "pending_language_review")

    def test_checker_cannot_attach_advice_to_a_quote_outside_the_source(self):
        bad = review(); bad["suggestions"][0]["quote"] = "原文里不存在的事实"
        calls = []
        result = run_language_review(
            product(), checker=lambda prompt: (calls.append("checker") or bad),
            brain=lambda prompt: (calls.append("brain") or {}),
            checker_identity=IDENTITY)
        self.assertEqual(calls, ["checker"])
        self.assertEqual(result["status"], "pending_language_review")
        self.assertIn("quote", result["reason"])

class EofContainerClosureTests(unittest.TestCase):
    def test_only_missing_array_and_object_closers_are_restored(self):
        from dalton_core.research_language_review import parse_stage_output_with_proof
        raw = '{"decisions":[],"sections":[{"index":0,"title":"回答","body":"原值 10 美元。","gaps":[]}'
        value, proof = parse_stage_output_with_proof(raw, stage="brain")
        self.assertEqual(proof["mode"], "eof_container_closure")
        self.assertEqual(proof["suffix"], "]}")
        self.assertEqual(value["sections"][0]["body"], "原值 10 美元。")
        self.assertNotEqual(proof["raw_sha256"], proof["fixed_sha256"])

    def test_unclosed_scalar_and_mismatched_container_refuse(self):
        from dalton_core.research_language_review import parse_stage_output_with_proof
        values = (
            '{"decisions":[],"sections":[{"index":0,"title":"回答',
            '{"decisions":[],"sections":[}',
            '{"decisions":tru',
            '{"decisions":[],"sections":[',
            '{"decisions":[],"sections":[{"index":0',
            '{"decisions":[],"sections":[NaN',
        )
        for raw in values:
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "no unique complete"):
                parse_stage_output_with_proof(raw, stage="brain")

class RestartedStreamCompatibilityTests(unittest.TestCase):
    def test_unique_complete_object_after_incomplete_prefix_still_parses(self):
        from dalton_core.research_language_review import parse_stage_output_with_proof
        complete='{"decisions":[],"sections":[]}'
        value, proof=parse_stage_output_with_proof('{"decisions":['+complete,stage='brain')
        self.assertEqual(value,{"decisions":[],"sections":[]})
        self.assertEqual(proof['mode'],'exact')

class BrainOnlyEofRecoveryTests(unittest.TestCase):
    def test_checker_eof_container_truncation_is_not_repaired(self):
        from dalton_core.research_language_review import parse_stage_output_with_proof
        with self.assertRaisesRegex(ValueError, "no unique complete"):
            parse_stage_output_with_proof('{"overall":"好","suggestions":[]',stage='checker')
