"""W4: the monthly review that forgets what we already decided.

Chem designed this and never ran it once, so the tests here are heavier on the
plumbing than on the prose: when is it owed, what does it refuse, and where do
the things it proposes end up.  The one thing it must never do -- change a
thesis -- is checked by the absence of any writer for one in this module and
by the candidate arriving in the existing human queue instead.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from dalton_core.store import DaltonStore
from dalton_core.thesis_revision import ThesisRevisionAuthority
from dalton_core.zero_base_review import (
    CHECKPOINT_KIND,
    FORM_A_VIEW,
    QUESTIONS,
    REWRITE_DECISIONS,
    TRIGGERS,
    ZeroBaseReviewAuthority,
    ZeroBaseReviewValidationError,
    build_context,
    build_review_body,
    build_review_prompt,
    due_reviews,
    month_label,
    review,
    review_ref_for,
    review_state,
    validate_review_output,
    verify_review,
)

from tests.zero_base_fixtures import MISSION

MARCH = datetime(2026, 3, 12, 9, tzinfo=timezone.utc)
APRIL = datetime(2026, 4, 12, 9, tzinfo=timezone.utc)

THESIS = {
    "ref": "thesis-version:acn-1",
    "thesis_ref": "thesis:acn",
    "statement": "ACN 的 managed services 组合会在两年内把利润率抬高 100bp",
    "mechanism": "合同结构",
    "confidence": "medium",
    "falsifier_refs": [],
    "content_hash": "a" * 64,
    "subject_ref": "company:ACN",
    "created_at": "2026-01-01T00:00:00.000000+00:00",
}
DEBATE = {"ref": "debate:acn-pricing", "question": "定价会不会崩", "status": "open",
          "our_side": "bear", "our_state": "taken"}


def context(**overrides):
    body = {
        "schema_version": "0.1",
        "company_ref": "company:ACN",
        "mission_ref": MISSION["mission_ref"],
        "mission_version_ref": MISSION["id"],
        "trigger": "monthly",
        "period_label": "2026-03",
        "as_of": "2026-03-12",
        "theses": [THESIS],
        "debates": [DEBATE],
        "events": [{"ref": "research-event:one", "kind": "filing",
                    "evidence_tier": "primary_filing",
                    "occurred_at": "2026-03-01T00:00:00.000000+00:00"}],
        "claims": [{"ref": "claim-version:one", "statement": "毛利率 32.1%",
                    "aspect": "margin"}],
        "calibration": None,
        "prior_review": None,
        "outcome_counts": {"should_have_moved": 1, "held": 4},
    }
    body.update(overrides)
    body["allowed_refs"] = sorted({
        THESIS["ref"], DEBATE["ref"], "research-event:one", "claim-version:one",
    } | set(overrides.get("allowed_refs") or ()))
    body["inputs_hash"] = "d" * 64
    return body


def answer(**overrides):
    body = {
        "form_a_view": "yes",
        "because": "同一条链条今天仍然成立，只是速度慢了。",
        "rewritten_lines": [{
            "thesis_ref": THESIS["ref"],
            "decision": "THESIS_WEAKENED",
            "line": "利润率会抬高，但要三年而不是两年",
            "because": "两个季度的实际都低于我们的曲线",
            "refs": ["claim-version:one"],
        }],
        "stale_debates": [{"debate_ref": DEBATE["ref"], "because": "定价已经稳了四个季度"}],
        "next_verification": {"what": "Q2 的 managed services 毛利率",
                              "date": "2026-07-20", "because": "那是曲线第一次可证伪的点"},
        "citations": ["claim-version:one"],
    }
    body.update(overrides)
    return body


class FakeModel:
    """A cockpit model that returns exactly what the test hands it."""

    def __init__(self, payload, *, text: str | None = None) -> None:
        self.payload = payload
        self.text = text
        self.calls: list[dict] = []

    def call(self, *, purpose, request_id, prompt, mission):
        self.calls.append({"purpose": purpose, "request_id": request_id,
                           "prompt": prompt})
        return {
            "text": self.text if self.text is not None
            else json.dumps(self.payload, ensure_ascii=False),
            "work_order_ref": "work:zero-base-review-1",
            "invocation_ref": "invocation:1",
            "route_decision_ref": "route:1",
            "cost_micros": 4200,
        }


class CadenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.reviews = ZeroBaseReviewAuthority(self.store, clock=lambda: "2026-03-12T09:00:00.000000+00:00")

    def state(self, now=MARCH):
        return review_state(
            self.store.connection, mission_ref=MISSION["mission_ref"],
            company_refs=["company:ACN"], now=now,
        )[0]

    def record(self, *, trigger="monthly", period_label="2026-03", inputs_hash="1" * 64):
        body = build_review_body(
            context(trigger=trigger, period_label=period_label),
            {**answer(), "model": None},
        )
        body["inputs_hash"] = inputs_hash
        return self.reviews.record(body, actor_ref="automation:coverage-mission")

    def test_a_company_with_no_review_is_due_this_month(self) -> None:
        state = self.state()
        self.assertTrue(state["due"])
        self.assertEqual((state["trigger"], state["period_label"]), ("monthly", "2026-03"))

    def test_a_reviewed_month_is_not_due_again(self) -> None:
        self.record()
        self.assertFalse(self.state()["due"])

    def test_thirty_one_days_later_is_due_again(self) -> None:
        self.record()
        state = self.state(APRIL)
        self.assertTrue(state["due"])
        self.assertEqual(state["period_label"], "2026-04")

    def test_january_31_and_february_1_do_not_create_two_reviews(self) -> None:
        self.reviews = ZeroBaseReviewAuthority(
            self.store, clock=lambda: "2026-01-31T09:00:00.000000+00:00")
        self.record(period_label="2026-01")
        self.assertFalse(self.state(datetime(2026, 2, 1, 9, tzinfo=timezone.utc))["due"])

    def test_an_earnings_review_restarts_the_thirty_day_clock(self) -> None:
        self.record()
        self.add_calibration("mission-deliverable-version:cal-1")
        self.reviews = ZeroBaseReviewAuthority(
            self.store, clock=lambda: "2026-03-20T09:00:00.000000+00:00")
        self.record(trigger="earnings_calibration",
                    period_label="mission-deliverable-version:cal-1", inputs_hash="2" * 64)
        self.assertFalse(self.state(datetime(2026, 4, 11, 9, tzinfo=timezone.utc))["due"])
        self.assertTrue(self.state(datetime(2026, 4, 19, 9, tzinfo=timezone.utc))["due"])

    def test_a_new_calibration_makes_a_reviewed_month_due_again(self) -> None:
        self.record()
        self.add_calibration("mission-deliverable-version:cal-1")
        state = self.state()
        self.assertTrue(state["due"])
        self.assertEqual(state["trigger"], "earnings_calibration")
        self.assertEqual(state["period_label"], "mission-deliverable-version:cal-1")

    def test_the_same_calibration_is_not_reviewed_twice(self) -> None:
        self.record()
        self.add_calibration("mission-deliverable-version:cal-1")
        self.record(trigger="earnings_calibration",
                    period_label="mission-deliverable-version:cal-1",
                    inputs_hash="2" * 64)
        self.assertFalse(self.state()["due"])

    def test_a_second_calibration_is(self) -> None:
        self.record()
        self.add_calibration("mission-deliverable-version:cal-1")
        self.record(trigger="earnings_calibration",
                    period_label="mission-deliverable-version:cal-1",
                    inputs_hash="2" * 64)
        self.add_calibration("mission-deliverable-version:cal-2", at="2026-03-11",
                             version=2)
        self.assertTrue(self.state()["due"])

    def test_due_reviews_is_the_filtered_state(self) -> None:
        self.assertEqual(
            [row["company_ref"] for row in due_reviews(
                self.store.connection, mission_ref=MISSION["mission_ref"],
                company_refs=["company:ACN"], now=MARCH)],
            ["company:ACN"],
        )
        self.record()
        self.assertEqual(due_reviews(
            self.store.connection, mission_ref=MISSION["mission_ref"],
            company_refs=["company:ACN"], now=MARCH), [])

    def test_the_month_label_is_the_owner_s_local_calendar(self) -> None:
        self.assertEqual(month_label(datetime(2026, 12, 31, 20, 0)), "2026-12")

    def add_calibration(self, version_id: str, *, at: str = "2026-03-10",
                        version: int = 1) -> None:
        connection = self.store.connection
        connection.executescript(
            (__import__("pathlib").Path(
                __import__("dalton_core.mission_deliverable", fromlist=["x"]).__file__
            ).with_name("mission_deliverable_schema.sql")).read_text(encoding="utf-8")
        )
        connection.create_function("dalton_mission_deliverable_authorized", 0, lambda: 1)
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            with self.store._transaction() as cur:
                cur.execute(
                    "INSERT INTO mission_deliverable_versions(version_id,deliverable_ref,"
                    "version_number,prior_version_ref,mission_version_ref,"
                    "mission_version_hash,playbook_version_ref,playbook_version_hash,kind,"
                    "subject_ref,record_json,content_hash,actor_ref,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (version_id, "mission-deliverable:cal", version, None, MISSION["id"],
                     "0" * 64, "playbook-version:x", "0" * 64, "earnings_calibration",
                     "company:ACN", "{}", "h", "automation:coverage-mission",
                     f"{at}T00:00:00.000000+00:00"),
                )
        finally:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.create_function(
                "dalton_mission_deliverable_authorized", 0, lambda: 0
            )


class PromptTests(unittest.TestCase):
    def test_the_prompt_asks_the_four_questions_and_lists_the_refs(self) -> None:
        prompt = build_review_prompt(context())
        for question in QUESTIONS:
            self.assertIn(question, prompt)
        self.assertIn(THESIS["ref"], prompt)
        self.assertIn(DEBATE["ref"], prompt)
        # The outcome counts are in the prompt: the review is told how its own
        # past judgements turned out, which is the point of W4's other half.
        self.assertIn("should_have_moved=1", prompt)

    def test_the_prompt_does_not_offer_a_decision_word_that_cannot_be_accepted(self) -> None:
        prompt = build_review_prompt(context())
        self.assertNotIn("NEW_THESIS", prompt)
        self.assertNotIn("NO_CHANGE", prompt)
        self.assertIn("THESIS_WEAKENED", prompt)


class ValidationTests(unittest.TestCase):
    def test_a_well_formed_answer_survives(self) -> None:
        found = validate_review_output(answer(), context())
        self.assertEqual(found["form_a_view"], "yes")
        self.assertEqual(len(found["rewritten_lines"]), 1)

    def test_an_extra_key_is_refused_whole(self) -> None:
        with self.assertRaises(ZeroBaseReviewValidationError):
            validate_review_output({**answer(), "confidence": 0.8}, context())

    def test_a_citation_that_was_not_in_the_prompt_is_refused(self) -> None:
        with self.assertRaises(ZeroBaseReviewValidationError):
            validate_review_output(
                answer(citations=["claim-version:invented"]), context()
            )

    def test_event_refs_cannot_stand_in_for_archived_evidence(self) -> None:
        with self.assertRaisesRegex(ZeroBaseReviewValidationError, "archived"):
            validate_review_output(
                answer(rewritten_lines=[], stale_debates=[],
                       citations=["research-event:one"]), context()
            )

    def test_a_rewrite_cannot_use_an_event_as_its_evidence(self) -> None:
        line = {**answer()["rewritten_lines"][0], "refs": ["research-event:one"]}
        with self.assertRaisesRegex(ZeroBaseReviewValidationError, "archived"):
            validate_review_output(answer(rewritten_lines=[line]), context())

    def test_a_rewrite_of_a_thesis_this_company_has_not_is_refused(self) -> None:
        with self.assertRaises(ZeroBaseReviewValidationError):
            validate_review_output(
                answer(rewritten_lines=[{
                    **answer()["rewritten_lines"][0],
                    "thesis_ref": "thesis-version:someone-else",
                }]),
                context(),
            )

    def test_two_rewrites_of_one_thesis_are_refused(self) -> None:
        line = answer()["rewritten_lines"][0]
        with self.assertRaises(ZeroBaseReviewValidationError):
            validate_review_output(answer(rewritten_lines=[line, line]), context())

    def test_a_new_thesis_candidate_is_refused_here_not_at_the_checkpoint(self) -> None:
        # ADR-0007: a NEW_THESIS is a coverage admission. Letting it through
        # would put a candidate in the queue that can only ever be rejected.
        with self.assertRaises(ZeroBaseReviewValidationError):
            validate_review_output(
                answer(rewritten_lines=[{
                    **answer()["rewritten_lines"][0], "decision": "NEW_THESIS",
                }]),
                context(),
            )
        self.assertNotIn("NEW_THESIS", REWRITE_DECISIONS)
        self.assertNotIn("NO_CHANGE", REWRITE_DECISIONS)

    def test_a_rewrite_with_no_evidence_is_refused(self) -> None:
        with self.assertRaises(ZeroBaseReviewValidationError):
            validate_review_output(
                answer(rewritten_lines=[{
                    **answer()["rewritten_lines"][0], "refs": [],
                }]),
                context(),
            )

    def test_a_stale_debate_that_is_not_on_the_map_is_refused(self) -> None:
        with self.assertRaises(ZeroBaseReviewValidationError):
            validate_review_output(
                answer(stale_debates=[{"debate_ref": "debate:invented",
                                       "because": "x"}]),
                context(),
            )

    def test_a_verification_date_in_the_past_is_not_a_next_look(self) -> None:
        with self.assertRaises(ZeroBaseReviewValidationError):
            validate_review_output(
                answer(next_verification={"what": "x", "date": "2026-03-11",
                                          "because": "y"}),
                context(),
            )

    def test_the_as_of_date_itself_is_allowed(self) -> None:
        found = validate_review_output(
            answer(next_verification={"what": "x", "date": "2026-03-12",
                                      "because": "y"}),
            context(),
        )
        self.assertEqual(found["next_verification"]["date"], "2026-03-12")

    def test_empty_lists_are_a_correct_answer(self) -> None:
        found = validate_review_output(
            answer(rewritten_lines=[], stale_debates=[], form_a_view="no"), context()
        )
        self.assertEqual(found["rewritten_lines"], [])
        self.assertEqual(found["form_a_view"], "no")

    def test_the_three_words_of_the_first_question_are_closed(self) -> None:
        self.assertEqual(FORM_A_VIEW, ("yes", "no", "unclear"))
        with self.assertRaises(ZeroBaseReviewValidationError):
            validate_review_output(answer(form_a_view="probably"), context())


class ModelCallTests(unittest.TestCase):
    def test_one_call_produces_a_review(self) -> None:
        model = FakeModel(answer())
        found = review(context(), model=model, mission=MISSION, request_id="r1")
        self.assertEqual(found["status"], "reviewed")
        self.assertEqual(model.calls[0]["purpose"], "zero_base_review")
        self.assertEqual(found["model"]["invocation_ref"], "invocation:1")

    def test_an_answer_outside_the_contract_is_refused_and_writes_nothing(self) -> None:
        model = FakeModel(None, text="I think ACN is fine.")
        found = review(context(), model=model, mission=MISSION, request_id="r1")
        self.assertEqual(found["status"], "refused")
        self.assertNotIn("form_a_view", found)

    def test_an_independent_verifier_can_pass_all_four_answers(self) -> None:
        producer = review(context(), model=FakeModel(answer()), mission=MISSION,
                          request_id="r1")
        verifier = FakeModel({"verdict": "pass", "findings": []})
        original_call = verifier.call
        def call(**kwargs):
            result = original_call(**kwargs)
            result["route_decision_ref"] = "route:verifier"
            return result
        verifier.call = call
        found = verify_review(
            context(), producer, model=verifier, mission=MISSION,
            request_id="r1-verify",
            family_resolver={"route:1": "openai", "route:verifier": "anthropic"}.get,
        )
        self.assertEqual(found["status"], "verified")
        self.assertIn("Archived evidence", verifier.calls[0]["prompt"])
        self.assertIn("毛利率 32.1%", verifier.calls[0]["prompt"])
        self.assertIn('"next_verification"', verifier.calls[0]["prompt"])

    def test_a_same_family_verifier_is_refused(self) -> None:
        producer = review(context(), model=FakeModel(answer()), mission=MISSION,
                          request_id="r1")
        found = verify_review(
            context(), producer, model=FakeModel({"verdict": "pass", "findings": []}),
            mission=MISSION, request_id="r1-verify", family_resolver=lambda _: "openai",
        )
        self.assertEqual(found["status"], "refused")

    def test_an_oversize_verifier_prompt_is_refused_before_a_call(self) -> None:
        producer = review(context(), model=FakeModel(answer()), mission=MISSION,
                          request_id="r1")
        verifier = FakeModel({"verdict": "pass", "findings": []})
        oversized = context(claims=[{
            "ref": "claim-version:huge", "statement": "x" * 26_000,
            "aspect": "margin",
        }])
        found = verify_review(
            oversized, producer, model=verifier, mission=MISSION,
            request_id="r1-verify", family_resolver=lambda _: "openai",
        )
        self.assertEqual(found["status"], "refused")
        self.assertIn("verifier prompt", found["reason"])
        self.assertEqual(verifier.calls, [])

    def test_the_purpose_routes_to_the_brain_tier_and_the_coverage_pool(self) -> None:
        from dalton_core.budget_pools import pool_for_purpose
        from dalton_core.model_fallback_chain import tier_for

        self.assertEqual(tier_for("zero_base_review"), "brain")
        self.assertEqual(pool_for_purpose("zero_base_review"), "coverage")
        self.assertEqual(pool_for_purpose("zero_base_review_verifier"), "coverage")


class AuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.clock = ["2026-03-12T09:00:00.000000+00:00"]
        self.reviews = ZeroBaseReviewAuthority(self.store, clock=lambda: self.clock[0])

    def body(self, **overrides):
        built = build_review_body(context(**overrides), {**answer(), "model": None})
        return built

    def test_a_review_is_written_read_back_and_versioned(self) -> None:
        written = self.reviews.record(self.body(), actor_ref="automation:coverage-mission")
        self.assertEqual((written["status"], written["version"]), ("fresh", 1))
        self.assertIsNone(written["prior_version_ref"])
        ref = review_ref_for(MISSION["mission_ref"], "company:ACN")
        self.assertEqual(self.reviews.latest(ref)["id"], written["id"])

    def test_the_same_reading_twice_is_a_duplicate(self) -> None:
        self.reviews.record(self.body(), actor_ref="automation:coverage-mission")
        again = self.reviews.record(self.body(), actor_ref="automation:coverage-mission")
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(
            len(self.reviews.versions(review_ref_for(MISSION["mission_ref"], "company:ACN"))),
            1,
        )

    def test_a_moved_ledger_is_a_second_version_of_the_same_review(self) -> None:
        first = self.reviews.record(self.body(), actor_ref="automation:coverage-mission")
        moved = self.body()
        moved["inputs_hash"] = "e" * 64
        self.clock[0] = "2026-03-20T09:00:00.000000+00:00"
        second = self.reviews.record(moved, actor_ref="automation:coverage-mission")
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["prior_version_ref"], first["id"])

    def test_a_monthly_review_carries_a_month(self) -> None:
        body = self.body()
        body["period_label"] = "2026-03-12"
        with self.assertRaises(ZeroBaseReviewValidationError):
            self.reviews.record(body, actor_ref="automation:coverage-mission")

    def test_a_trigger_outside_the_vocabulary_is_refused(self) -> None:
        body = self.body()
        body["trigger"] = "because_i_felt_like_it"
        with self.assertRaises(ZeroBaseReviewValidationError):
            self.reviews.record(body, actor_ref="automation:coverage-mission")
        self.assertEqual(TRIGGERS, ("monthly", "earnings_calibration"))

    def test_the_table_refuses_a_write_that_did_not_come_through_the_store(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "INSERT INTO zero_base_review_versions(version_id,review_ref,"
                "version_number,prior_version_ref,mission_ref,mission_version_ref,"
                "company_ref,trigger,period_label,form_a_view,rewritten_line_count,"
                "stale_debate_count,candidate_count,next_verification_date,inputs_hash,"
                "record_json,content_hash,actor_ref,created_at) VALUES('v','r',1,NULL,"
                "'m','mv','company:ACN','monthly','2026-03','yes',0,0,0,NULL,'h','{}',"
                "'x','automation:x','2026-01-01')"
            )

    def test_a_written_review_can_never_be_updated_or_deleted(self) -> None:
        self.reviews.record(self.body(), actor_ref="automation:coverage-mission")
        for statement in (
            "UPDATE zero_base_review_versions SET form_a_view='no'",
            "DELETE FROM zero_base_review_versions",
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.store._transaction() as cur:
                    cur.execute(statement)

    def test_the_record_reads_as_a_document(self) -> None:
        written = self.reviews.record(self.body(), actor_ref="automation:coverage-mission")
        narrative = written["narrative"]
        self.assertEqual(len(narrative["sections"]), 4)
        self.assertIn("2026-07-20", narrative["sections"][3]["body"])
        self.assertIn("只提案", narrative["authority_note"])


class CandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.reviews = ZeroBaseReviewAuthority(
            self.store, clock=lambda: "2026-03-12T09:00:00.000000+00:00"
        )
        self.review = self.reviews.record(
            build_review_body(context(), {**answer(), "model": None}),
            actor_ref="automation:coverage-mission",
        )
        self.line = self.review["answers"]["rewritten_lines"][0]

    def propose(self, mission=None):
        return self.reviews.record_revision_candidate(
            review=self.review, thesis=THESIS, line=self.line,
            mission=mission or MISSION, actor_ref="automation:coverage-mission",
        )

    def test_a_candidate_is_adr_0007_s_shape(self) -> None:
        candidate = self.propose()
        self.assertEqual(candidate["checkpoint_kind"], CHECKPOINT_KIND)
        self.assertEqual(candidate["decision"], "THESIS_WEAKENED")
        self.assertEqual(candidate["origin"], "zero_base_review")
        self.assertIsNone(candidate["judgement_ref"])
        self.assertEqual(candidate["thesis_version_hash"], THESIS["content_hash"])

    def test_the_same_rewrite_twice_is_a_duplicate(self) -> None:
        self.propose()
        self.assertEqual(self.propose()["status"], "duplicate")
        self.assertEqual(len(self.reviews.candidates("company:ACN")), 1)

    def test_a_mission_without_the_scope_proposes_nothing(self) -> None:
        mission = {**MISSION, "autonomy": {**MISSION["autonomy"], "may_write": ["deliverable"]}}
        with self.assertRaises(ZeroBaseReviewValidationError):
            self.propose(mission)

    def test_a_mission_granting_the_scope_must_carry_the_checkpoint(self) -> None:
        mission = {**MISSION,
                   "autonomy": {**MISSION["autonomy"], "human_checkpoints": []}}
        with self.assertRaises(ZeroBaseReviewValidationError):
            self.propose(mission)

    def test_the_checkpoint_word_is_one_the_mission_vocabulary_already_has(self) -> None:
        # W4 extended no vocabulary: a zero-base rewrite is the same kind of
        # decision a judgement's rewrite is, and inventing a second word for it
        # would mean a mission version before the lane could run once.
        from dalton_core.coverage_mission import CHECKPOINT_KINDS

        self.assertIn(CHECKPOINT_KIND, CHECKPOINT_KINDS)

    def test_the_candidate_reaches_the_existing_human_queue(self) -> None:
        candidate = self.propose()
        revisions = ThesisRevisionAuthority(self.store)
        open_candidates = revisions.undecided("company:ACN")
        self.assertEqual([row["id"] for row in open_candidates], [candidate["id"]])
        # The review rides along in place of the judgement lane's reflection:
        # "nothing happened and we would still write it differently" is only an
        # argument if the four answers can be read beside it.
        self.assertEqual(open_candidates[0]["zero_base_review"]["form_a_view"], "yes")
        self.assertIsNone(open_candidates[0]["reflection"])

    def test_a_person_can_defer_it_through_the_existing_decision_loop(self) -> None:
        candidate = self.propose()
        revisions = ThesisRevisionAuthority(self.store)
        decision = revisions.decide(
            candidate_ref=candidate["id"], candidate_hash=candidate["content_hash"],
            verdict="defer", reason="想再看一个季度", actor_ref="human:owner",
        )
        self.assertEqual(decision["verdict"], "defer")
        self.assertFalse(decision["terminal"])
        still_open = revisions.undecided("company:ACN")
        self.assertTrue(still_open[0]["deferred"])
        self.assertEqual(still_open[0]["last_verdict"], "defer")

    def test_a_rejection_closes_it_and_it_leaves_the_queue(self) -> None:
        candidate = self.propose()
        revisions = ThesisRevisionAuthority(self.store)
        revisions.decide(
            candidate_ref=candidate["id"], candidate_hash=candidate["content_hash"],
            verdict="reject", reason="不同意", actor_ref="human:owner",
        )
        self.assertEqual(revisions.undecided("company:ACN"), [])

    def test_nothing_in_this_module_can_write_a_thesis(self) -> None:
        # The freeze, expressed as the absence of an import rather than as a
        # promise in a comment.
        import dalton_core.zero_base_review as module

        source = __import__("pathlib").Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("thesis_versions", "current_pointers", "coverage_admission",
                          "MissionDeliverableAuthority"):
            self.assertNotIn(forbidden, source)


class ContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)

    def test_a_bare_core_still_produces_a_context(self) -> None:
        built = build_context(
            self.store.connection, mission=MISSION, company_ref="company:ACN",
            trigger="monthly", period_label="2026-03", now=MARCH,
        )
        self.assertEqual(built["as_of"], "2026-03-12")
        self.assertEqual(built["theses"], [])
        self.assertEqual(built["allowed_refs"], [])
        # The hash is a function of the ledger, not of the clock: two builds a
        # week apart over unmoved rows are the same reading.
        later = build_context(
            self.store.connection, mission=MISSION, company_ref="company:ACN",
            trigger="monthly", period_label="2026-03",
            now=datetime(2026, 3, 19, 9, tzinfo=timezone.utc),
        )
        self.assertEqual(built["inputs_hash"], later["inputs_hash"])

    def test_a_trigger_outside_the_vocabulary_never_builds_a_prompt(self) -> None:
        with self.assertRaises(ZeroBaseReviewValidationError):
            build_context(
                self.store.connection, mission=MISSION, company_ref="company:ACN",
                trigger="whenever", period_label="2026-03", now=MARCH,
            )


class RegistrationTests(unittest.TestCase):
    def test_the_lane_is_registered_in_all_four_places(self) -> None:
        from dalton_core.bootstrap import SCHEMA_DATABASES
        from dalton_core.cockpit_plane import REGISTRY_LANE_LABELS
        from dalton_core.lane_registry import LANE_MODULES, lane_for_operation
        from scripts.rehearse_deploy import CORE_MIGRATIONS

        self.assertIn("dalton_core.mission_zero_base_lane", LANE_MODULES)
        spec = lane_for_operation("dispatch_zero_base_review")
        self.assertIsNotNone(spec)
        self.assertEqual((spec.order, spec.driver_key), (155, "zero_base_review"))
        # The pool is C2's central mapping's to say, not the lane's.
        self.assertIsNone(spec.budget_pool)
        # Before the weekly reflection, which reports the counts this lane
        # writes, and which keeps the last place in the tick.
        from dalton_core.lane_registry import tick_lanes

        driven = [lane.driver_key for lane in tick_lanes()]
        self.assertLess(driven.index("zero_base_review"),
                        driven.index("mission_reflection"))
        self.assertIn("zero_base_review", REGISTRY_LANE_LABELS)
        self.assertIn("zero_base_review_schema.sql", dict(SCHEMA_DATABASES))
        self.assertIn("zero_base_review_schema.sql",
                      {migration.schema for migration in CORE_MIGRATIONS})

    def test_the_lane_takes_the_coverage_pool(self) -> None:
        from dalton_core.budget_pools import LANE_POOLS, pool_for_operation

        self.assertEqual(LANE_POOLS["dispatch_zero_base_review"], "coverage")
        self.assertEqual(pool_for_operation("dispatch_zero_base_review"), "coverage")

    def test_the_installer_seeds_the_model_configuration_it_needs(self) -> None:
        from pathlib import Path

        install = (Path(__file__).resolve().parents[1] / "deploy" / "macos" / "install.sh"
                   ).read_text(encoding="utf-8")
        self.assertIn("zero-base-review-model-config.json", install)
        self.assertIn("DALTON_ZERO_BASE_REVIEW_MODEL_TIER", install)
        self.assertIn("zero-base-review-verifier-model-config.json", install)
        self.assertIn("DALTON_ZERO_BASE_REVIEW_VERIFIER_MODEL_TIER", install)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
