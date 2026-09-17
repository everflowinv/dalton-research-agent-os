"""D1/D2: nothing reaches a person that is not worth their time, and a return means something.

Two behaviours, and they are two halves of one loop.

**Before**: a draft that is well formed and well cited can still be worthless to
a reader -- three of twelve questions answered from twenty-six rows is a research
plan, not a finding -- so a deterministic standard weighs it, and a draft that
falls short is never published.  It leaves a note saying question by question
what is missing, and the lane waits for the evidence to move rather than paying
for four calls to reach the same refusal.

**After**: when a person does read one and returns it, their words are the
specification for the next version.  They reach the prompt verbatim, only the
questions they named are rewritten, the rest are carried forward unchanged, and
the new version says it exists because a reviewer asked for it.
"""

from __future__ import annotations

import json
import unittest

from dalton_core.deep_insight_gate import (
    REVIEWER_RETURNED,
    DeepInsightGateValidationError,
    evidence_scope,
)
from dalton_core.deep_insight_gate_quality import (
    DEFAULT_STANDARD,
    STANDARD_FILE_NAME,
    DeepInsightGateStandardError,
    assess,
    load_standard,
    normalise_standard,
    note_is_current,
    read_note,
    read_notes,
    suggested_return_reason,
)
from dalton_core.deep_insight_gate_review import (
    CARRIED_FORWARD_NOTE,
    REJECT_FOLLOW_UP,
    carried_forward_questions,
    change_note,
    changed_questions,
    groups_to_redraft,
    questions_to_redraft,
    review_note,
    review_of,
    review_prompt_lines,
    validate_question_notes,
)
from tests.test_deep_insight_gate import ACN, OWNER, answered, unknown
from tests.test_deep_insight_gate_lane import FakeModel, Harness, resolver


def draft(*, answered_refs, sources_each=1, classification="contract_compounder"):
    """A record-shaped draft with a chosen number of answers and refs."""

    from dalton_core.deep_insight_gate import QUESTION_REFS

    answers = []
    for index, ref in enumerate(QUESTION_REFS):
        if ref in answered_refs:
            row = answered(ref, f"claim-version:{ref}:0")
            extra = [{"kind": "claim", "ref": f"claim-version:{ref}:{n}",
                      "text": "合同期限为五年", "period": None}
                     for n in range(1, sources_each)]
            row["sources"] = row["sources"] + extra
            row["sentences"] = [{"text": "这一问的判断由所引材料支撑。",
                                 "refs": [row["sources"][0]["ref"], *[
                                     item["ref"] for item in extra]]}]
            answers.append(row)
        else:
            answers.append(unknown(ref))
    return {"classification": classification, "answers": answers,
            "company_ref": ACN}


def dossier(classification="contract_compounder"):
    return {"industry_classification": {"classification": classification}}


class StandardTests(unittest.TestCase):
    def test_the_shipped_numbers_are_the_ones_the_live_core_would_be_judged_by(self):
        self.assertEqual(DEFAULT_STANDARD["max_unknown"], 4)
        self.assertEqual(DEFAULT_STANDARD["min_evidence_refs"], 40)
        self.assertTrue(DEFAULT_STANDARD["require_question_one_classified"])
        self.assertTrue(DEFAULT_STANDARD["require_verifier_pass"])

    def test_a_file_overrides_one_number_and_keeps_the_rest(self):
        rules = normalise_standard({"max_unknown": 9})
        self.assertEqual(rules["max_unknown"], 9)
        self.assertEqual(rules["min_evidence_refs"],
                         DEFAULT_STANDARD["min_evidence_refs"])

    def test_a_misspelt_threshold_is_refused_rather_than_ignored(self):
        with self.assertRaises(DeepInsightGateStandardError) as caught:
            normalise_standard({"max_unknowns": 9})
        self.assertIn("max_unknowns", str(caught.exception))

    def test_a_threshold_outside_the_twelve_questions_is_refused(self):
        with self.assertRaises(DeepInsightGateStandardError):
            normalise_standard({"max_unknown": 13})

    def test_a_broken_file_stops_the_lane_rather_than_restoring_defaults(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as name:
            (Path(name) / STANDARD_FILE_NAME).write_text("{not json", encoding="utf-8")
            with self.assertRaises(DeepInsightGateStandardError):
                load_standard(Path(name))


class AssessmentTests(unittest.TestCase):
    def test_a_thin_draft_is_refused_and_says_what_is_missing(self):
        result = assess(draft(answered_refs={"q1", "q5"}), dossier=dossier(),
                        verifier_passed=True)
        self.assertFalse(result["submittable"])
        self.assertIn("unknown_within_cap", result["shortfalls"])
        self.assertIn("evidence_refs_sufficient", result["shortfalls"])
        self.assertTrue(result["summary"].startswith("系统判定草稿尚不足以提交"))
        # Every unanswered question is listed with what is missing and what
        # would settle it, in the draft's own words.
        gaps = {row["question_ref"] for row in result["question_gaps"]}
        self.assertEqual(len(gaps), 10)
        self.assertTrue(all(row["next_step"] for row in result["question_gaps"]))

    def test_a_full_draft_on_enough_evidence_is_submittable(self):
        from dalton_core.deep_insight_gate import QUESTION_REFS

        record = draft(answered_refs=set(QUESTION_REFS), sources_each=4)
        self.assertGreaterEqual(len(evidence_scope(record)), 40)
        result = assess(record, dossier=dossier(), verifier_passed=True)
        self.assertTrue(result["submittable"], result["shortfalls"])
        self.assertEqual(result["counts"]["unknown"], 0)

    def test_question_one_agreeing_with_a_file_that_knows_nothing_is_not_agreement(self):
        # The exact shape ACN and EPAM are in on the live Core: the gate and
        # the dossier both say "the evidence does not say", which
        # ``classification_agrees`` reads as agreement.
        from dalton_core.deep_insight_gate import QUESTION_REFS

        record = draft(answered_refs=set(QUESTION_REFS), sources_each=4,
                       classification="insufficient_evidence")
        result = assess(record, dossier=dossier("insufficient_evidence"),
                        verifier_passed=True)
        self.assertFalse(result["submittable"])
        self.assertIn("question_one_classified", result["shortfalls"])
        self.assertIn("classification_agrees", result["shortfalls"])

    def test_no_verdict_is_not_a_pass(self):
        from dalton_core.deep_insight_gate import QUESTION_REFS

        record = draft(answered_refs=set(QUESTION_REFS), sources_each=4)
        result = assess(record, dossier=dossier(), verifier_passed=None)
        self.assertFalse(result["submittable"])
        self.assertIn("verifier_passed", result["shortfalls"])

    def test_a_relaxed_standard_lets_the_same_draft_through(self):
        record = draft(answered_refs={"q1", "q5"})
        relaxed = {"max_unknown": 10, "min_evidence_refs": 1,
                   "require_classification_agrees": False}
        result = assess(record, dossier=dossier(), verifier_passed=True,
                        standard=relaxed)
        self.assertTrue(result["submittable"], result["shortfalls"])


class SuggestedReturnTests(unittest.TestCase):
    """H2: the return note the standard would have written, for a draft it
    never got to hold back."""

    def assessment(self):
        return assess(draft(answered_refs={"q1", "q5"}), dossier=dossier(),
                      verifier_passed=True)

    def test_it_names_the_shortfalls_and_then_the_questions(self):
        import re

        text = suggested_return_reason(self.assessment())
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("按提交标准复核"))
        self.assertIn("足够的证据引用", lines[0])
        questions = [line for line in lines if re.match(r"^q\d+：", line)]
        self.assertTrue(questions)
        self.assertIn("下一步取", questions[0])
        self.assertIn("自动填的", lines[-1])

    def test_every_question_line_is_one_the_redraft_prompt_can_read(self):
        # D2 parses a line beginning with a question number into that
        # question's own instruction; a suggestion the parser cannot read would
        # be a paragraph the next draft never hears.
        from dalton_core.cockpit_plane import _gate_question_notes
        from dalton_core.deep_insight_gate import QUESTION_REFS

        notes = _gate_question_notes(None, suggested_return_reason(self.assessment()))
        self.assertTrue(notes)
        self.assertTrue(set(notes) <= set(QUESTION_REFS))
        self.assertTrue(all(note.strip() for note in notes.values()))

    def test_it_stays_inside_the_length_the_writer_accepts(self):
        from dalton_core.deep_insight_gate_quality import MAX_RETURN_REASON_CHARS

        wordy = {
            "shortfall_labels": ["足够的证据引用"],
            "question_gaps": [
                {"question_ref": f"q{number}", "question_number": str(number),
                 "missing": "缺" * 400, "next_step": "取" * 400}
                for number in range(1, 13)],
        }
        text = suggested_return_reason(wordy)
        self.assertLessEqual(len(text), MAX_RETURN_REASON_CHARS)
        self.assertLess(len(text), 4000)
        self.assertIn("自动填的", text.splitlines()[-1])


class AutoReturnTests(unittest.TestCase):
    """The lane, end to end, with the shipped standard in force."""

    def setUp(self):
        # No standard file: the shipped numbers, which is what the live Core
        # would be judged by.
        self.harness = Harness(submission_standard=None)
        self.addCleanup(self.harness.close)

    def test_a_thin_draft_is_never_put_in_front_of_a_person(self):
        summary = self.harness.run()
        self.assertEqual(summary["gate_status"], "auto_returned")
        self.assertIsNone(summary["version_ref"])
        self.assertEqual(self.harness.gates().counts()["drafts"], 0)
        self.assertFalse(summary["quality"]["submittable"])
        self.assertTrue(summary["failure_reason"].startswith("系统判定草稿尚不足以提交"))

    def test_the_note_says_question_by_question_what_is_missing(self):
        self.harness.run()
        notes = read_notes(self.harness.state_dir)
        self.assertEqual(len(notes), 1)
        note = notes[0]
        self.assertEqual(note["company_ref"], ACN)
        gaps = note["assessment"]["question_gaps"]
        self.assertTrue(gaps)
        self.assertTrue(all(row["missing"] and row["next_step"] for row in gaps))

    def test_the_second_run_on_the_same_evidence_spends_nothing(self):
        first = self.harness.run()
        self.assertEqual(first["gate_status"], "auto_returned")
        calls = []

        def counted():
            model = FakeModel()
            calls.append(model)
            return model

        second = self.harness.run(model_factory=counted)
        self.assertEqual(second["gate_status"], "no_eligible_company")
        self.assertEqual(second["blocked"][ACN], "auto_returned_awaiting_evidence")
        self.assertEqual(calls, [])

    def test_the_note_is_current_only_against_the_fingerprint_it_was_written_for(self):
        self.harness.run()
        note = read_note(self.harness.state_dir, ACN)
        self.assertTrue(note_is_current(note, note["evidence_fingerprint"]))
        self.assertFalse(note_is_current(note, "something else"))

    def test_a_draft_that_clears_the_standard_is_published_and_clears_the_note(self):
        self.harness.run()
        self.assertIsNotNone(read_note(self.harness.state_dir, ACN))
        (self.harness.state_dir / STANDARD_FILE_NAME).write_text(json.dumps({
            "max_unknown": 12, "min_evidence_refs": 1,
            "require_question_one_classified": False,
            "require_classification_agrees": False,
        }), encoding="utf-8")
        summary = self.harness.run()
        self.assertEqual(summary["gate_status"], "submitted")
        self.assertIsNone(read_note(self.harness.state_dir, ACN))


class ReviewVocabularyTests(unittest.TestCase):
    def test_per_question_notes_are_closed_against_the_twelve(self):
        self.assertEqual(validate_question_notes({"q3": " 看错了 "}), {"q3": "看错了"})
        with self.assertRaises(DeepInsightGateValidationError):
            validate_question_notes({"q13": "不存在的题号"})
        with self.assertRaises(DeepInsightGateValidationError):
            validate_question_notes({"q3": "   "})

    def test_a_review_is_read_only_off_a_return(self):
        self.assertIsNone(review_of({"decision": "approve", "reason": "好"}))
        self.assertIsNone(review_of(None))
        review = review_of({"decision": "return_for_more_work", "reason": "太薄",
                            "question_notes": {"q3": "看错了"},
                            "created_at": "2026-09-16T00:00:00+00:00",
                            "actor_ref": OWNER, "gate_version_ref": "v1"})
        self.assertEqual(review["reason"], "太薄")
        self.assertEqual(review["question_notes"], {"q3": "看错了"})

    def test_the_reviewers_words_reach_the_prompt_verbatim(self):
        review = review_of({"decision": "return_for_more_work",
                            "reason": "把订单和收入搞混了",
                            "question_notes": {"q3": "供给那一段没写",
                                               "q7": "没有回答估值"}})
        lines = review_prompt_lines(review, group_questions=["q1", "q2", "q3", "q4"])
        text = "\n".join(lines)
        self.assertIn("把订单和收入搞混了", text)
        self.assertIn("供给那一段没写", text)
        # The market question's note is not shown to the industry call: a call
        # about industry structure should not spend its prompt on q7.
        self.assertNotIn("没有回答估值", text)

    def test_only_the_named_questions_and_their_groups_are_rewritten(self):
        review = review_of({"decision": "return_for_more_work", "reason": "看一下",
                            "question_notes": {"q3": "供给那一段没写"}})
        self.assertEqual(questions_to_redraft(review), ["q3"])
        self.assertEqual(groups_to_redraft(["q3"]), ["industry"])

    def test_a_return_that_names_nothing_reaches_for_the_unknowns(self):
        prior = draft(answered_refs={"q1", "q2", "q3", "q4", "q5", "q6"})
        review = review_of({"decision": "return_for_more_work", "reason": "整体太薄"})
        wanted = questions_to_redraft(review, prior=prior)
        self.assertEqual(wanted, ["q7", "q8", "q9", "q10", "q11", "q12"])

    def test_what_changed_is_read_off_the_two_versions(self):
        from dalton_core.deep_insight_gate import QUESTION_REFS

        before = draft(answered_refs=set(QUESTION_REFS))
        after = json.loads(json.dumps(before))
        after["answers"][2]["sentences"][0]["text"] = "换了一种说法。"
        self.assertEqual(changed_questions(after, before), ["q3"])
        self.assertEqual(len(carried_forward_questions(after, before)), 11)
        note = change_note(after, before)
        self.assertIn("q3", note)
        self.assertIn(CARRIED_FORWARD_NOTE, note)

    def test_a_rejection_says_what_would_bring_the_company_back(self):
        self.assertIn("重新评估", REJECT_FOLLOW_UP)
        self.assertIn("初步筛查", REJECT_FOLLOW_UP)

    def test_the_review_note_reads_as_one_block(self):
        review = review_of({"decision": "return_for_more_work", "reason": "太薄",
                            "question_notes": {"q3": "看错了"}})
        self.assertEqual(review_note(review), "太薄；q3：看错了")


class ReturnLoopTests(unittest.TestCase):
    """A person returns one draft, and the next version answers them."""

    def setUp(self):
        self.harness = Harness(submission_standard={
            "max_unknown": 12, "min_evidence_refs": 1,
            "require_question_one_classified": False,
            "require_classification_agrees": False,
        })
        self.addCleanup(self.harness.close)
        first = self.harness.run(
            model_factory=lambda: FakeModel(answer_all=True))
        self.assertEqual(first["gate_status"], "submitted")
        self.head = self.harness.gates().latest(ACN)

    def _return(self, **kwargs):
        return self.harness.gates().decide(
            gate_version_ref=self.head["id"],
            gate_version_hash=self.head["content_hash"],
            decision="return_for_more_work",
            reason="第三问把订单和收入搞混了，重写。", actor_ref=OWNER, **kwargs)

    def test_the_decision_carries_the_per_question_notes(self):
        decision = self._return(question_notes={"q3": "供给那一段没写"})
        stored = self.harness.gates().decision_for(self.head["id"])
        self.assertEqual(stored["question_notes"], {"q3": "供给那一段没写"})
        self.assertEqual(decision["status"], "fresh")

    def test_notes_are_refused_on_an_approval(self):
        with self.assertRaises(DeepInsightGateValidationError):
            self.harness.gates().decide(
                gate_version_ref=self.head["id"],
                gate_version_hash=self.head["content_hash"],
                decision="approve", reason="通过", actor_ref=OWNER,
                question_notes={"q3": "不该出现"})

    def test_the_next_version_rewrites_only_what_was_named(self):
        self._return(question_notes={"q3": "供给那一段没写"})
        model = FakeModel(answer_all=True, sentence="这一版按审阅意见重写了。")
        summary = self.harness.run(model_factory=lambda: model)
        self.assertEqual(summary["gate_status"], "submitted")
        # One call, not four: q3 lives in the industry group, and the other
        # three groups are carried forward untouched.
        self.assertEqual(len(model.prompts), 1)
        self.assertIn("Part: industry --", model.prompts[0])
        # The reviewer's own words are in it.
        self.assertIn("第三问把订单和收入搞混了", model.prompts[0])
        self.assertIn("供给那一段没写", model.prompts[0])

    def test_the_new_version_says_a_reviewer_asked_for_it(self):
        self._return(question_notes={"q3": "供给那一段没写"})
        self.harness.run(
            model_factory=lambda: FakeModel(answer_all=True,
                                            sentence="这一版按审阅意见重写了。"))
        head = self.harness.gates().latest(ACN)
        self.assertEqual(head["version"], 2)
        self.assertEqual(head["change_reason"], REVIEWER_RETURNED)

    def test_the_untouched_questions_are_carried_forward_unchanged(self):
        self._return(question_notes={"q3": "供给那一段没写"})
        summary = self.harness.run(
            model_factory=lambda: FakeModel(answer_all=True,
                                            sentence="这一版按审阅意见重写了。"))
        carried = summary["carried_forward_questions"]
        self.assertNotIn("q3", carried)
        for ref in ("q5", "q6", "q7", "q8", "q9", "q10", "q11", "q12"):
            self.assertIn(ref, carried)
        self.assertIn("沿用上一版", summary["change_note"])

    def test_a_returned_redraft_does_not_need_new_evidence_to_exist(self):
        # ADR-0008 refuses a version that cites nothing new, and that rule is
        # about automation re-asking on unchanged evidence.  A version a person
        # asked for is the opposite case: the new information is the review.
        self._return()
        summary = self.harness.run(
            model_factory=lambda: FakeModel(answer_all=True,
                                            sentence="这一版按审阅意见重写了。"))
        self.assertEqual(summary["gate_status"], "submitted")
        head = self.harness.gates().latest(ACN)
        self.assertEqual(sorted(evidence_scope(head)),
                         sorted(evidence_scope(self.head)))

    def test_a_redraft_that_changed_nothing_is_still_a_duplicate(self):
        self._return()
        summary = self.harness.run(
            model_factory=lambda: FakeModel(answer_all=True))
        self.assertEqual(summary["gate_status"], "duplicate")
        self.assertEqual(self.harness.gates().counts()["drafts"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
