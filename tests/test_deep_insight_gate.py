"""P12d: twelve answers are a chain, an unknown is an answer, and only a person decides.

Four things carry this authority and everything else follows from them.

The questions are the bound Playbook's, and a draft binds their hash. A gate
answered under one methodology must never be readable as though it had been
answered under another, so a record whose answers do not carry the twelve
questions it binds is refused before it is stored.

An answer is sentences with refs *or* it is ``unknown`` and says what evidence
would settle it. Nothing in between: an answered question with no confidence, an
unknown that carries prose, a sentence citing nothing, are all shapes the
contract has no room for. Most of these questions are expected to be unknown on
a thin file, and the structural standard must not punish saying so -- which is
why an unknown's missing evidence is written where every other gap is written.

A draft that cites nothing the current one does not is a ``duplicate``
(ADR-0008). That is the whole guard against a lane that re-asks the owner the
same question every tick.

And the decision is a person's. It refuses a non-``human:`` actor, it binds the
exact draft's content hash, and there is exactly one of them per draft: a change
of mind is a new draft, not a second verdict on the same one.
"""

from __future__ import annotations

import unittest

from dalton_core.company_dossier import causal_chain_hash, policy_hash, validate_policy
from dalton_core.deep_insight_gate import (
    DEEP_INSIGHT_GATE_RUBRIC,
    GROUP_OF,
    QUESTION_COUNT,
    QUESTION_REFS,
    QUESTION_SOURCE_MAP_HASH,
    QUESTION_SOURCE_MAP_REF,
    DeepInsightGateAuthority,
    DeepInsightGateConflict,
    DeepInsightGateValidationError,
    answer_body,
    answered_count,
    classification_agrees,
    evidence_fingerprint,
    fingerprint_of,
    gate_artefact,
    gate_questions,
    new_refs,
    output_rubric_findings,
    questions_hash,
    validate_answer,
    validate_gate_version,
)
from dalton_core.research_quality_score import run_deterministic
from dalton_core.store import content_hash
from tests.test_claim_index_entries import ACN, LedgerFixture

ACTOR = "automation:coverage-mission"
OWNER = "human:coverage-owner"
CHAIN = ["Bookings lead revenue by two to four quarters."]
CRITERIA = ["State what changed and its impact."]
QUESTIONS = [f"第 {index} 问：这家公司在这一点上是什么情况？" for index in range(1, 13)]


def playbook(questions=QUESTIONS):
    return {
        "id": "playbook-version:1", "content_hash": "d" * 64,
        "stages": [
            {"stage_ref": "initial_screen", "exit_gate": {"questions": ["a"],
                                                          "pass_rule": "r"}},
            {"stage_ref": "deep_insight_gate",
             "exit_gate": {"questions": list(questions), "pass_rule": "由人裁决"}},
        ],
    }


def constitution(criteria=CRITERIA):
    return {
        "id": "constitution-version:us-it-services:1",
        "constitution_ref": "constitution:us-it-services",
        "content_hash": "c" * 64,
        "method": {
            "causal_chain": list(CHAIN),
            "output_rubric": {"criteria": list(criteria), "good_samples": [],
                              "bad_samples": []},
        },
    }


def policy(criteria=CRITERIA, check="not_a_restatement"):
    return validate_policy({
        "schema_version": "0.1",
        "policy_ref": "dossier-policy:test:v1",
        "causal_chain_maps": [{
            "constitution_ref": "constitution:us-it-services",
            "causal_chain_hash": causal_chain_hash(CHAIN),
            "sections": ["demand_drivers"], "note": "test",
        }],
        "output_rubric_bindings": [
            {"criterion_hash": content_hash(criteria[0]), "check": check, "reason": ""},
        ],
    })


def source(ref, text="合同期限为五年", kind="claim", period="2026-05-31"):
    return {"kind": kind, "ref": ref, "text": text, "period": period}


def answered(question_ref, ref, *, text="这一问的判断由所引材料支撑。",
             confidence="medium", source_text="合同期限为五年", kind="claim"):
    return {
        "question_ref": question_ref,
        "question": QUESTIONS[QUESTION_REFS.index(question_ref)],
        "group": GROUP_OF[question_ref], "status": "answered",
        "confidence": confidence, "unknown": None,
        "sentences": [{"text": text, "refs": [ref]}],
        "sources": [source(ref, source_text, kind=kind)], "gaps": [],
    }


def unknown(question_ref, *, reason="no_material_shown"):
    return {
        "question_ref": question_ref,
        "question": QUESTIONS[QUESTION_REFS.index(question_ref)],
        "group": GROUP_OF[question_ref], "status": "unknown", "confidence": None,
        "unknown": {"reason": reason, "missing": "还不知道这一点",
                    "evidence_that_would_answer": "下一份 10-K 的分部披露就能定"},
        "sentences": [], "sources": [], "gaps": [],
    }


def bindings(dossier_ref="company-dossier-version:acn:1", map_ref=None,
             questions=QUESTIONS):
    return {
        "constitution_version": {"ref": "constitution-version:us-it-services:1",
                                 "hash": "c" * 64},
        "playbook_version": {"ref": "playbook-version:1", "hash": "d" * 64},
        "mission_version_ref": "mission-version:1",
        "dossier_version_ref": dossier_ref,
        "dossier_version_hash": "e" * 64,
        "debate_map_version_ref": map_ref,
        "policy_ref": "dossier-policy:test:v1",
        "policy_hash": policy_hash(policy()),
        "questions_hash": questions_hash(questions),
        "source_map_ref": QUESTION_SOURCE_MAP_REF,
        "source_map_hash": QUESTION_SOURCE_MAP_HASH,
        "rubric_ref": DEEP_INSIGHT_GATE_RUBRIC.rubric_ref,
        "rubric_hash": DEEP_INSIGHT_GATE_RUBRIC.content_hash,
    }


def body(*, answers=None, classification="contract_compounder", evidence=None,
         company_ref=ACN, prior_ref=None, change_reason="evidence_thicker",
         dossier_ref="company-dossier-version:acn:1", questions=QUESTIONS):
    supplied = dict(answers or {})
    rows = [supplied.get(ref) or unknown(ref) for ref in QUESTION_REFS]
    refs = evidence if evidence is not None else (
        [row for answer in rows for row in answer["sources"]][:1]
        or [source("fallback-ref")])
    record = {
        "company_ref": company_ref,
        "classification": classification,
        "answers": rows,
        "bindings": bindings(dossier_ref=dossier_ref, questions=questions),
        "actor_ref": ACTOR,
        "change_reason": change_reason,
        "evidence_refs": refs,
        "drafted_at": {},
    }
    if prior_ref is not None:
        record["computed_from_version_ref"] = prior_ref
    return record


class QuestionSourceTests(unittest.TestCase):
    def test_the_questions_come_from_the_bound_playbook(self):
        self.assertEqual(gate_questions(playbook()), QUESTIONS)

    def test_a_playbook_that_asks_a_different_number_stops_the_lane(self):
        with self.assertRaises(DeepInsightGateValidationError):
            gate_questions(playbook(QUESTIONS[:11]))

    def test_a_playbook_with_no_gate_stage_is_refused(self):
        with self.assertRaises(DeepInsightGateValidationError):
            gate_questions({"stages": [{"stage_ref": "initial_screen",
                                        "exit_gate": {"questions": []}}]})

    def test_the_twelve_questions_are_grouped_into_four_calls(self):
        self.assertEqual(len(QUESTION_REFS), QUESTION_COUNT)
        self.assertEqual(sorted(GROUP_OF), sorted(QUESTION_REFS))
        self.assertEqual(len({GROUP_OF[ref] for ref in QUESTION_REFS}), 4)


class AnswerShapeTests(unittest.TestCase):
    def test_an_answered_question_needs_a_confidence(self):
        row = answered("q1", "claim-1")
        row["confidence"] = None
        with self.assertRaises(DeepInsightGateValidationError):
            validate_answer(row, "answers[0]", question_ref="q1")

    def test_an_unknown_question_may_not_carry_a_confidence(self):
        row = unknown("q1")
        row["confidence"] = "low"
        with self.assertRaises(DeepInsightGateValidationError):
            validate_answer(row, "answers[0]", question_ref="q1")

    def test_an_unknown_must_say_what_evidence_would_answer_it(self):
        row = unknown("q1")
        row["unknown"] = {"reason": "no_material_shown", "missing": "不知道"}
        with self.assertRaises(DeepInsightGateValidationError):
            validate_answer(row, "answers[0]", question_ref="q1")

    def test_a_sentence_may_not_cite_a_ref_that_was_not_shown(self):
        row = answered("q1", "claim-1")
        row["sentences"][0]["refs"] = ["claim-2"]
        with self.assertRaises(DeepInsightGateValidationError):
            validate_answer(row, "answers[0]", question_ref="q1")

    def test_a_citation_tag_written_into_the_prose_is_refused(self):
        row = answered("q1", "claim-1", text="按 C3 所述，公司靠合同赚钱。")
        with self.assertRaises(DeepInsightGateValidationError):
            validate_answer(row, "answers[0]", question_ref="q1")

    def test_a_source_no_sentence_cites_is_refused(self):
        row = answered("q1", "claim-1")
        row["sources"].append(source("claim-2"))
        with self.assertRaises(DeepInsightGateValidationError):
            validate_answer(row, "answers[0]", question_ref="q1")

    def test_the_answer_body_is_assembled_here_and_carries_no_tags(self):
        row = validate_answer(answered("q1", "claim-1"), "a", question_ref="q1")
        self.assertEqual(answer_body(row), "这一问的判断由所引材料支撑。")


class RecordTests(unittest.TestCase):
    def wire(self, **kwargs):
        record = body(**kwargs)
        record.pop("computed_from_version_ref", None)
        record.update({
            "schema_version": "0.1", "id": "deep-insight-gate-version:acn:1",
            "created_at": "2026-09-09T00:00:00+00:00",
            "gate_ref": "deep-insight-gate:company-sec-cik-0001467373",
            "version": 1, "prior_version_ref": None,
            "generator_ref": "generator:deep-insight-gate:0.1",
        })
        from dalton_core.deep_insight_gate import body_hash

        record["body_hash"] = body_hash(record)
        record["content_hash"] = content_hash(record)
        return record

    def test_a_record_answers_exactly_twelve_questions(self):
        record = self.wire()
        self.assertEqual(len(validate_gate_version(record)["answers"]), QUESTION_COUNT)

    def test_the_answers_must_carry_the_questions_the_draft_binds(self):
        record = self.wire()
        record["answers"][3]["question"] = "另一个问题"
        from dalton_core.deep_insight_gate import body_hash

        record["body_hash"] = body_hash(record)
        record["content_hash"] = content_hash(
            {k: v for k, v in record.items() if k != "content_hash"})
        with self.assertRaises(DeepInsightGateConflict):
            validate_gate_version(record)

    def test_a_classification_outside_the_closed_list_is_refused(self):
        record = self.wire(classification="a_good_business")
        with self.assertRaises(DeepInsightGateValidationError):
            validate_gate_version(record)

    def test_question_one_must_agree_with_the_dossier(self):
        dossier = {"industry_classification": {"classification": "structural_growth"}}
        agrees, why = classification_agrees(
            {"classification": "contract_compounder"}, dossier)
        self.assertFalse(agrees)
        self.assertIn("structural_growth", why)
        agrees, _ = classification_agrees(
            {"classification": "structural_growth"}, dossier)
        self.assertTrue(agrees)


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.fixture = LedgerFixture()
        self.addCleanup(self.fixture.close)
        self.authority = DeepInsightGateAuthority(self.fixture.store)

    def test_a_first_draft_is_a_version_and_reads_back(self):
        published = self.authority.publish(
            body(answers={"q1": answered("q1", "claim-1")}))
        self.assertEqual(published["status"], "fresh")
        stored = self.authority.gate(published["id"])
        self.assertEqual(stored["version"], 1)
        self.assertEqual(answered_count(stored), 1)
        self.assertEqual(self.authority.latest(ACN)["id"], published["id"])

    def test_an_identical_body_is_a_duplicate(self):
        first = self.authority.publish(body(answers={"q1": answered("q1", "claim-1")}))
        again = self.authority.publish(body(answers={"q1": answered("q1", "claim-1")}))
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["duplicate_reason"], "identical_body")
        self.assertEqual(again["id"], first["id"])

    def test_a_rewrite_with_no_new_evidence_is_a_duplicate(self):
        self.authority.publish(body(answers={"q1": answered("q1", "claim-1")}))
        again = self.authority.publish(body(answers={
            "q1": answered("q1", "claim-1", text="换一种说法再写一遍。")}))
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["duplicate_reason"], "no_new_evidence")

    def test_a_draft_citing_something_new_is_a_second_version(self):
        first = self.authority.publish(body(answers={"q1": answered("q1", "claim-1")}))
        second = self.authority.publish(body(
            answers={"q1": answered("q1", "claim-1"),
                     "q5": answered("q5", "claim-2")},
            evidence=[source("claim-2")], prior_ref=first["id"]))
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(second["version"], 2)
        self.assertEqual(new_refs(second, first), ["claim-2"])

    def test_a_body_computed_from_a_stale_head_is_refused(self):
        self.authority.publish(body(answers={"q1": answered("q1", "claim-1")}))
        stale = body(answers={"q5": answered("q5", "claim-2")},
                     evidence=[source("claim-2")])
        # Computed from "no version", which is where the chain was before the
        # first draft landed. A writer that read the chain, drafted for four
        # minutes and published without looking again would overwrite exactly
        # this way.
        stale["computed_from_version_ref"] = None
        with self.assertRaises(DeepInsightGateConflict):
            self.authority.publish(stale)

    def test_a_version_must_name_the_evidence_that_occasioned_it(self):
        with self.assertRaises(DeepInsightGateValidationError):
            self.authority.publish(body(
                answers={"q1": answered("q1", "claim-1")}, evidence=[]))

    def test_one_question_replays_across_the_chain(self):
        first = self.authority.publish(body(answers={"q1": answered("q1", "claim-1")}))
        self.authority.publish(body(
            answers={"q1": answered("q1", "claim-2", text="新证据把这一问定了。")},
            evidence=[source("claim-2")], prior_ref=first["id"]))
        history = self.authority.replay_question(ACN, "q1")
        self.assertEqual([row["version"] for row in history], [1, 2])
        self.assertEqual(history[1]["refs"], ["claim-2"])

    def test_the_fingerprint_is_the_two_files_the_draft_was_made_from(self):
        published = self.authority.publish(
            body(answers={"q1": answered("q1", "claim-1")}))
        dossier = {"id": "company-dossier-version:acn:1", "content_hash": "e" * 64}
        self.assertEqual(fingerprint_of(published),
                         evidence_fingerprint(dossier, None))
        self.assertNotEqual(fingerprint_of(published),
                            evidence_fingerprint(dossier, {"id": "debate-map:1"}))


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = LedgerFixture()
        self.addCleanup(self.fixture.close)
        self.authority = DeepInsightGateAuthority(self.fixture.store)
        self.draft = self.authority.publish(
            body(answers={"q1": answered("q1", "claim-1")}))

    def decide(self, **kwargs):
        values = {
            "gate_version_ref": self.draft["id"],
            "gate_version_hash": self.draft["content_hash"],
            "decision": "approve", "reason": "十二问够看了", "actor_ref": OWNER,
        }
        values.update(kwargs)
        return self.authority.decide(**values)

    def test_a_published_draft_is_undecided_until_a_person_decides(self):
        self.assertEqual([row["id"] for row in self.authority.undecided()],
                         [self.draft["id"]])
        self.assertFalse(self.authority.status_for(ACN)["decided"])
        self.decide()
        self.assertEqual(self.authority.undecided(), [])
        self.assertEqual(self.authority.status_for(ACN)["decision"], "approve")

    def test_automation_can_never_decide_the_gate(self):
        with self.assertRaises(DeepInsightGateValidationError):
            self.decide(actor_ref=ACTOR)
        self.assertEqual(len(self.authority.undecided()), 1)

    def test_a_decision_binds_the_exact_draft_it_read(self):
        with self.assertRaises(DeepInsightGateConflict):
            self.decide(gate_version_hash="f" * 64)

    def test_a_decision_needs_a_reason(self):
        with self.assertRaises(DeepInsightGateValidationError):
            self.decide(reason="   ")

    def test_a_decision_outside_the_three_words_is_refused(self):
        with self.assertRaises(DeepInsightGateValidationError):
            self.decide(decision="maybe")

    def test_the_same_decision_twice_is_one_decision(self):
        first = self.decide()
        again = self.decide()
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self.authority.counts()["decisions"], 1)

    def test_a_change_of_mind_is_a_new_draft_not_a_second_verdict(self):
        self.decide()
        with self.assertRaises(DeepInsightGateConflict):
            self.decide(decision="reject", reason="想想还是不行")

    def test_a_returned_draft_lets_the_chain_move_again(self):
        self.decide(decision="return_for_more_work", reason="第七问没有价格材料")
        second = self.authority.publish(body(
            answers={"q1": answered("q1", "claim-1"),
                     "q7": answered("q7", "claim-2")},
            evidence=[source("claim-2")], prior_ref=self.draft["id"]))
        self.assertEqual(second["version"], 2)
        self.assertEqual([row["id"] for row in self.authority.undecided()],
                         [second["id"]])

    def test_the_decision_may_name_the_stage_record_it_produced(self):
        decided = self.decide(stage_record_ref="mission-stage-record:abc")
        self.assertEqual(
            self.authority.decision_for(self.draft["id"])["stage_record_ref"],
            "mission-stage-record:abc")
        self.assertEqual(decided["company_ref"], ACN)


class StructuralStandardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = LedgerFixture()
        self.addCleanup(self.fixture.close)
        self.claim = self.fixture.add_claim(
            "gate-1", statement="合同期限为五年", kind="qualitative", value=None,
            unit=None)
        self.ref = self.claim["claim_version_id"]

    def record(self, **kwargs):
        from dalton_core.deep_insight_gate import body_hash

        record = body(**kwargs)
        record.pop("computed_from_version_ref", None)
        record.update({
            "schema_version": "0.1", "id": "deep-insight-gate-draft:x",
            "created_at": "2026-09-09T00:00:00+00:00",
            "gate_ref": "deep-insight-gate:acn", "version": 1,
            "prior_version_ref": None,
            "generator_ref": "generator:deep-insight-gate:0.1",
        })
        record["body_hash"] = body_hash(record)
        record["content_hash"] = content_hash(record)
        return record

    def test_an_honest_unknown_is_not_read_as_an_empty_shell(self):
        # The whole point of the unknown branch: a structural check that punished
        # "we do not know, and this is what would tell us" would make the one
        # behaviour this layer wants the one it penalises.
        record = self.record(answers={"q1": answered("q1", self.ref)})
        result = run_deterministic(
            gate_artefact(record), DEEP_INSIGHT_GATE_RUBRIC,
            core=self.fixture.store.connection)
        checks = {item["check"]: item for item in result["checks"]}
        self.assertEqual(checks["required_sections_present"]["status"], "pass")
        self.assertEqual(checks["claim_refs_resolve"]["status"], "pass")

    def test_a_figure_with_no_cited_source_fails_the_hard_check(self):
        record = self.record(answers={"q1": answered(
            "q1", self.ref, text="收入增长了 12.7 个百分点。")})
        result = run_deterministic(
            gate_artefact(record), DEEP_INSIGHT_GATE_RUBRIC,
            core=self.fixture.store.connection)
        self.assertIn("numbers_without_refs", result["failed_checks"])

    def test_the_constitution_criterion_nobody_bound_is_itself_a_finding(self):
        record = self.record(answers={"q1": answered("q1", self.ref)})
        findings = output_rubric_findings(
            record, constitution=constitution(["一条没人绑定的标准"]),
            policy=policy(), prior=None)
        self.assertEqual([item["code"] for item in findings],
                         ["unmapped_output_rubric_criterion"])

    def test_an_investment_conclusion_is_a_finding(self):
        record = self.record(answers={"q1": answered(
            "q1", self.ref, text="这家公司被市场低估了。")})
        # Under a policy that binds the criterion to a different check, the
        # phrase is nobody's business: a consumer reports what the standard was
        # bound to and never what it privately disapproves of.
        findings = output_rubric_findings(
            record, constitution=constitution(), policy=policy(), prior=None)
        self.assertEqual(findings, [])
        findings = output_rubric_findings(
            record, constitution=constitution(),
            policy=policy(check="no_investment_conclusion"), prior=None)
        self.assertEqual([item["code"] for item in findings], ["investment_conclusion"])

    def test_the_gate_rubric_is_frozen_and_is_not_registered_with_q1s(self):
        from dalton_core.research_quality_rubrics import RUBRICS

        self.assertNotIn(DEEP_INSIGHT_GATE_RUBRIC.rubric_ref, RUBRICS)
        self.assertEqual(
            DEEP_INSIGHT_GATE_RUBRIC.content_hash,
            "d78bb55115b192c74af0876dea74f5d2a9d7d56e7106b096aafbc1c846d204d6")


if __name__ == "__main__":
    unittest.main()
