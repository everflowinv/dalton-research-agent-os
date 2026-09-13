from __future__ import annotations

import unittest

from dalton_core.research_language_review import (
    build_brain_prompt, publish_language_attachment, run_language_review,
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
