from __future__ import annotations
import copy
import unittest
from unittest.mock import patch
from dalton_core.event_judgement import (
    MAX_REFS, EventJudgementValidationError, build_judge_prompt, validate_judge_output, judge)
from dalton_core.event_judgement_cli import event_model_contract_ref
from tests.test_event_judgement import JudgementHarness, decision, FakeModel
from tests.test_event_judgement_result import AcnHarness, acn_answer

class ReferenceContractTests(JudgementHarness):
    def bounded_context(self):
        context = self.context()
        context["event"] = {**context["event"], "source_refs": [
            f"document:synthetic-reference-{i}" for i in range(MAX_REFS + 1)]}
        return context

    def test_prompt_advertises_same_limit_as_local_parser(self):
        prompt = build_judge_prompt(self.bounded_context())
        self.assertIn(f"a JSON list of at most {MAX_REFS} references", prompt)
        self.assertIn("driver_refs, thesis_refs and citations", prompt)
        self.assertIn("before deduplication", prompt)
        self.assertIn("Do not invent a ref", prompt)

    def test_twelve_printed_refs_pass_thirteen_still_refuse_whole(self):
        context = self.bounded_context(); refs = context["event"]["source_refs"]
        result = validate_judge_output(decision(citations=refs[:MAX_REFS]), context)
        self.assertEqual(result["citations"], refs[:MAX_REFS])
        with self.assertRaisesRegex(EventJudgementValidationError, "at most 12"):
            validate_judge_output(decision(citations=refs), context)

    def test_duplicate_entries_do_not_evade_limit_and_foreign_refs_still_refuse(self):
        context = self.bounded_context(); ref = context["event"]["source_refs"][0]
        with self.assertRaisesRegex(EventJudgementValidationError, "at most 12"):
            validate_judge_output(decision(citations=[ref] * (MAX_REFS + 1)), context)
        with self.assertRaisesRegex(EventJudgementValidationError, "were not shown"):
            validate_judge_output(decision(citations=["document:never-shown"]), context)

    def test_over_limit_model_output_is_not_trimmed_or_automatically_retried(self):
        context = self.bounded_context(); model = FakeModel([
            decision(citations=context["event"]["source_refs"])])
        result = judge(context, model=model, mission=self.mission, request_id="synthetic-over-limit")
        self.assertEqual(result["status"], "refused")
        self.assertIn("at most 12", result["reason"])
        self.assertEqual(len(model.prompts), 1)
        self.assertEqual(result["model"]["cost_micros"], 20000)

    def test_new_prompt_contract_has_distinct_scheduler_namespace(self):
        new_ref = event_model_contract_ref()
        with patch("dalton_core.event_judgement_cli.EVENT_PROMPT_CONTRACT_VERSION", "event-output-constraints:0.7"):
            old_ref = event_model_contract_ref()
        self.assertNotEqual(new_ref, old_ref)

class ResultEvidenceReferenceTests(AcnHarness):
    def setUp(self):
        super().setUp(); self.index_all()
        event = self.record("release_event")
        self.ctx = self.context(event, self.material(event))

    def test_decisive_top_level_refs_do_not_remove_row_number_source_proof(self):
        prompt = build_judge_prompt(self.ctx)
        self.assertIn("These per-row source bindings", prompt)
        answer = acn_answer(prompt)
        all_sources = {tag for row in answer["actual_vs_expected"] for tag in row["sources"]}
        self.assertGreater(len(all_sources), len(answer["citations"]))
        self.assertLessEqual(len(answer["citations"]), MAX_REFS)
        validated = validate_judge_output(answer, self.ctx)
        self.assertEqual(len(validated["result"]["actual_vs_expected"]), 8)
        missing = copy.deepcopy(answer)
        missing["actual_vs_expected"][0]["actual"] = "999999 [N9999]"
        with self.assertRaises(EventJudgementValidationError):
            validate_judge_output(missing, self.ctx)
