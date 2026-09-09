"""P14a: the brain -- two closed words, no repairs, an independent verifier."""

from __future__ import annotations

import json
import unittest

from dalton_core.cockpit_model import CockpitModelError, purposes
from dalton_core.event_judgement import (
    ACTION_VOCABULARY,
    DECISION_ACTIONS,
    POOL_NAME,
    PURPOSE,
    EventJudgementAuthority,
    EventJudgementValidationError,
    allowed_refs,
    apply_effect,
    build_context,
    build_judge_prompt,
    build_reflection_prompt,
    build_verifier_prompt,
    company_theses,
    judge,
    model_drivers,
    pool,
    pool_state,
    reflect,
    reflection_is_owed,
    validate_judge_output,
    validate_reflection_output,
    validate_verifier_output,
    verify,
    verify_reflection,
)
from dalton_core.mission_deliverable import MissionDeliverableAuthority
from dalton_core.model_forecast_driver import ForecastModelAuthority
from dalton_core.research_event import ResearchEventAuthority, record_event
from dalton_core.research_playbook import DECISION_VOCABULARY
from dalton_core.store import content_hash
from tests.p14a_fixtures import ACN, AUTOMATION, P14aHarness

JUDGE_ROUTE = "route-decision:judge"
VERIFIER_ROUTE = "route-decision:verifier"
FAMILIES = {JUDGE_ROUTE: "anthropic", VERIFIER_ROUTE: "google"}


def resolver(mapping=None):
    table = FAMILIES if mapping is None else mapping
    return lambda ref: table.get(ref)


class FakeModel:
    """Answers with whatever was handed to it, and records what it was asked."""

    def __init__(self, replies, *, route=JUDGE_ROUTE, cost_micros=20_000):
        self.replies = list(replies)
        self.route = route
        self.cost_micros = cost_micros
        self.prompts: list[str] = []

    def call(self, *, purpose, request_id, prompt, mission):
        self.prompts.append(prompt)
        if not self.replies:
            raise CockpitModelError("the fake model has nothing left to say")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {
            "text": reply if isinstance(reply, str) else json.dumps(reply),
            "replayed": False, "cost_micros": self.cost_micros,
            "cost_status": "actual",
            "work_order_ref": f"work:cockpit-{purpose}-{request_id}",
            "result_envelope_ref": f"result:{request_id}",
            "invocation_ref": f"invocation:{request_id}",
            "route_decision_ref": self.route,
        }


def decision(action="no_change", word="NO_CHANGE", **extra):
    body = {
        "decision": word, "action": action, "driver_refs": [], "thesis_refs": [],
        "because": "The move was sector-wide and no company news accompanied it.",
        "citations": [],
    }
    body.update(extra)
    return body


PASS = {"verdict": "pass", "findings": []}
REJECT = {"verdict": "reject", "findings": [
    {"code": "decision_not_supported_by_the_event", "detail": "Nothing shown says this."},
]}


class VocabularyTests(unittest.TestCase):
    def test_the_purpose_is_registered_at_import(self):
        self.assertIn(PURPOSE, purposes())

    def test_every_playbook_word_maps_to_permitted_actions(self):
        self.assertEqual(set(DECISION_ACTIONS), set(DECISION_VOCABULARY))
        for word, actions in DECISION_ACTIONS.items():
            self.assertTrue(actions <= set(ACTION_VOCABULARY), word)

    def test_no_change_cannot_revise_anything(self):
        self.assertEqual(DECISION_ACTIONS["NO_CHANGE"], {"no_change", "note", "research"})

    def test_a_broken_thesis_cannot_be_answered_with_a_note(self):
        self.assertNotIn("note", DECISION_ACTIONS["THESIS_BROKEN"])


class JudgementHarness(P14aHarness):
    """One recorded price-move event and the two ledgers, with no tests of its own."""

    def setUp(self):
        super().setUp()
        self.events = ResearchEventAuthority(self.store)
        self.judgements = EventJudgementAuthority(self.store)
        self.event = record_event(
            self.events, company_ref=ACN, kind="price_move",
            occurred_at="2026-09-02T00:00:00+00:00",
            source_refs=["market-price-series-version:1"],
            payload={"as_of": "2026-09-02", "close": "104", "previous_close": "100",
                     "return_percent": "4.0000", "direction": "up",
                     "basket_return_percent": "0.0000",
                     "excess_vs_basket_percent": "4.0000", "basket_members": 4,
                     "benchmark_ref": None, "benchmark_return_percent": None,
                     "excess_vs_benchmark_percent": None, "trigger": "absolute",
                     "threshold_percent": "3.0",
                     "price_version_ref": "market-price-series-version:1",
                     "invocation_ref": None},
            mission=self.mission, actor_ref=AUTOMATION,
        )

    def context(self, **kwargs):
        return build_context(
            event=self.event, mission=self.mission, connection=self.store.connection,
            judgements=self.judgements, source_table="alphaengine | news | sell_side",
            **kwargs,
        )


class ContextTests(JudgementHarness):
    def test_the_prompt_renders_the_payload_the_tier_and_the_source_table(self):
        prompt = build_judge_prompt(self.context())
        self.assertIn("price_move", prompt)
        self.assertIn("market_price", prompt)
        self.assertIn("return_percent = 4.0000", prompt)
        self.assertIn("alphaengine | news | sell_side", prompt)
        self.assertIn(self.event["id"], prompt)

    def test_the_prompt_names_the_five_words_and_the_six_actions(self):
        prompt = build_judge_prompt(self.context())
        for word in DECISION_VOCABULARY:
            self.assertIn(word, prompt)
        for action in ACTION_VOCABULARY:
            self.assertIn(action, prompt)

    def test_the_allowed_refs_are_exactly_what_was_printed(self):
        refs = allowed_refs(self.context())
        self.assertIn(self.event["id"], refs)
        self.assertIn("market-price-series-version:1", refs)
        self.assertNotIn("claim-version:invented", refs)

    def test_a_claim_appears_in_the_prompt_and_becomes_citable(self):
        claim = self.claim(statement="ACN 的 bookings 同比增长 5%。")
        refs = allowed_refs(self.context())
        self.assertIn(claim, refs)
        self.assertIn(claim, build_judge_prompt(self.context()))

    def test_the_prompt_is_bounded(self):
        for _ in range(40):
            self.claim(statement="x" * 300)
        self.assertLessEqual(len(build_judge_prompt(self.context())), 24_000)

    def test_drivers_come_from_the_model_version_with_their_nearest_assumptions(self):
        from tests.test_model_forecast_driver import model as forecast_body

        body = forecast_body()
        drivers = model_drivers(body)
        self.assertTrue(drivers)
        with_assumptions = [row for row in drivers if row["assumptions"]]
        self.assertTrue(with_assumptions)
        self.assertLessEqual(len(with_assumptions[0]["assumptions"]), 3)

    def test_no_model_means_no_drivers_rather_than_an_invented_one(self):
        self.assertEqual(self.context()["drivers"], [])


class OutputContractTests(JudgementHarness):
    def check(self, value):
        return validate_judge_output(value, self.context())

    def test_the_minimal_no_change_output_is_accepted(self):
        result = self.check(decision())
        self.assertEqual(result["decision"], "NO_CHANGE")
        self.assertEqual(result["action"], "no_change")

    def test_an_extra_key_is_refused_whole(self):
        with self.assertRaises(EventJudgementValidationError) as caught:
            self.check({**decision(), "confidence": 0.8})
        self.assertIn("confidence", str(caught.exception))

    def test_a_missing_key_is_refused(self):
        body = decision()
        body.pop("because")
        with self.assertRaises(EventJudgementValidationError):
            self.check(body)

    def test_a_sixth_decision_word_is_refused(self):
        with self.assertRaises(EventJudgementValidationError):
            self.check(decision(word="THESIS_MOSTLY_FINE"))

    def test_an_incompatible_decision_and_action_pair_is_refused(self):
        with self.assertRaises(EventJudgementValidationError) as caught:
            self.check(decision(action="revise_thesis", word="NO_CHANGE",
                                citations=[self.event["id"]]))
        self.assertIn("does not permit", str(caught.exception))

    def test_a_citation_that_was_not_shown_is_refused(self):
        with self.assertRaises(EventJudgementValidationError) as caught:
            self.check(decision(action="note", word="THESIS_WEAKENED",
                                citations=["claim-version:invented"],
                                note="One sentence."))
        self.assertIn("were not shown", str(caught.exception))

    def test_a_driver_this_model_does_not_have_is_refused(self):
        with self.assertRaises(EventJudgementValidationError):
            self.check(decision(action="note", word="THESIS_WEAKENED",
                                driver_refs=["concept:us-gaap:Invented"],
                                citations=[self.event["id"]], note="One."))

    def test_an_action_other_than_no_change_must_cite_something(self):
        with self.assertRaises(EventJudgementValidationError) as caught:
            self.check(decision(action="note", word="THESIS_WEAKENED", note="One."))
        self.assertIn("must cite", str(caught.exception))

    def test_a_note_longer_than_four_sentences_is_refused(self):
        with self.assertRaises(EventJudgementValidationError):
            self.check(decision(action="note", word="THESIS_WEAKENED",
                                citations=[self.event["id"]],
                                note="One. Two. Three. Four. Five."))

    def test_a_note_where_no_note_was_asked_for_is_refused(self):
        with self.assertRaises(EventJudgementValidationError):
            self.check({**decision(), "note": "One."})

    def test_a_research_decision_must_carry_its_question(self):
        with self.assertRaises(EventJudgementValidationError):
            self.check(decision(action="research", word="NO_CHANGE",
                                citations=[self.event["id"]]))
        result = self.check(decision(action="research", word="NO_CHANGE",
                                     citations=[self.event["id"]],
                                     research_question="Did a customer cancel?"))
        self.assertEqual(result["research_question"], "Did a customer cancel?")

    def test_a_forecast_change_must_be_exactly_four_fields(self):
        with self.assertRaises(EventJudgementValidationError):
            self.check(decision(action="revise_forecast", word="THESIS_WEAKENED",
                                citations=[self.event["id"]],
                                forecast_change={"driver_ref": "x"}))

    def test_the_model_returning_prose_is_a_refusal_not_a_crash(self):
        with self.assertRaises(EventJudgementValidationError):
            self.check("I think nothing should change.")


class VerifierTests(JudgementHarness):
    def test_a_pass_with_findings_is_refused(self):
        with self.assertRaises(EventJudgementValidationError):
            validate_verifier_output({"verdict": "pass", "findings": [
                {"code": "criterion_misread", "detail": "x"}]})

    def test_a_reject_with_no_findings_is_refused(self):
        with self.assertRaises(EventJudgementValidationError):
            validate_verifier_output({"verdict": "reject", "findings": []})

    def test_an_unknown_finding_code_is_refused(self):
        with self.assertRaises(EventJudgementValidationError):
            validate_verifier_output({"verdict": "reject", "findings": [
                {"code": "vibes", "detail": "x"}]})

    def test_the_verifier_prompt_carries_the_decision_and_not_the_whole_context(self):
        context = self.context()
        prompt = build_verifier_prompt(context, {**decision(), "status": "judged"})
        self.assertIn("NO_CHANGE / no_change", prompt)
        self.assertIn("independent verifier", prompt)

    def test_a_same_family_verifier_is_refused_rather_than_recorded_as_a_pass(self):
        context = self.context()
        judged = judge(context, model=FakeModel([decision()]), mission=self.mission,
                       request_id="r1")
        checked = verify(
            context, judged, model=FakeModel([PASS], route=VERIFIER_ROUTE),
            mission=self.mission, request_id="v1",
            family_resolver=resolver({JUDGE_ROUTE: "anthropic",
                                      VERIFIER_ROUTE: "anthropic"}),
        )
        self.assertEqual(checked["status"], "refused")
        self.assertIn("model_family_not_independent", checked["reason"])

    def test_an_unresolvable_family_fails_closed(self):
        context = self.context()
        judged = judge(context, model=FakeModel([decision()]), mission=self.mission,
                       request_id="r1")
        checked = verify(
            context, judged, model=FakeModel([PASS], route=VERIFIER_ROUTE),
            mission=self.mission, request_id="v1", family_resolver=resolver({}),
        )
        self.assertEqual(checked["status"], "refused")
        self.assertIn("could not be resolved", checked["reason"])

    def test_two_families_and_a_pass_verdict_verifies(self):
        context = self.context()
        judged = judge(context, model=FakeModel([decision()]), mission=self.mission,
                       request_id="r1")
        checked = verify(
            context, judged, model=FakeModel([PASS], route=VERIFIER_ROUTE),
            mission=self.mission, request_id="v1", family_resolver=resolver(),
        )
        self.assertEqual(checked["status"], "verified")
        self.assertEqual(checked["verdict"], "pass")
        self.assertEqual(checked["independence"]["producer_family"], "anthropic")
        self.assertEqual(checked["independence"]["verifier_family"], "google")

    def test_the_verdict_is_bound_to_the_decision_it_read(self):
        context = self.context()
        judged = judge(context, model=FakeModel([decision()]), mission=self.mission,
                       request_id="r1")
        checked = verify(
            context, judged, model=FakeModel([PASS], route=VERIFIER_ROUTE),
            mission=self.mission, request_id="v1", family_resolver=resolver(),
        )
        self.assertEqual(
            checked["judged_decision_hash"],
            content_hash({"decision": judged["decision"], "action": judged["action"],
                          "because": judged["because"], "citations": judged["citations"]}),
        )

    def test_a_refused_judgement_is_not_verified(self):
        context = self.context()
        judged = judge(context, model=FakeModel(["not json"]), mission=self.mission,
                       request_id="r1")
        self.assertEqual(judged["status"], "refused")
        checked = verify(context, judged, model=FakeModel([PASS]), mission=self.mission,
                         request_id="v1", family_resolver=resolver())
        self.assertEqual(checked["status"], "skipped")

    def test_a_model_that_cannot_be_reached_is_a_refusal_with_a_reason(self):
        context = self.context()
        judged = judge(context, model=FakeModel([]), mission=self.mission, request_id="r1")
        self.assertEqual(judged["status"], "refused")
        self.assertIn("did not succeed", judged["reason"])


class LedgerTests(JudgementHarness):
    def judged(self, body=None):
        context = self.context()
        return context, judge(context, model=FakeModel([body or decision()]),
                              mission=self.mission, request_id="r1")

    def verified(self, judged):
        return verify(self.context(), judged, model=FakeModel([PASS], route=VERIFIER_ROUTE),
                      mission=self.mission, request_id="v1", family_resolver=resolver())

    def test_a_judgement_binds_the_event_and_the_work_order(self):
        _context, judged = self.judged()
        written = self.judgements.record(
            event=self.event, judgement=judged, verification=self.verified(judged),
            effect={"kind": "no_change", "status": "recorded"},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        self.assertEqual(written["status"], "fresh")
        self.assertEqual(written["event_ref"], self.event["id"])
        self.assertEqual(written["event_hash"], self.event["content_hash"])
        self.assertEqual(written["model"]["work_order_ref"],
                         judged["model"]["work_order_ref"])
        self.assertEqual(written["verifier"]["verdict"], "pass")

    def test_the_same_event_is_never_judged_twice(self):
        _context, judged = self.judged()
        first = self.judgements.record(
            event=self.event, judgement=judged, verification=self.verified(judged),
            effect={"kind": "no_change", "status": "recorded"},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        _c2, second_judgement = self.judged(decision(action="note", word="THESIS_WEAKENED",
                                                     citations=[self.event["id"]],
                                                     note="Different answer."))
        second = self.judgements.record(
            event=self.event, judgement=second_judgement,
            verification=self.verified(second_judgement),
            effect={"kind": "note", "status": "fresh"},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["action"], "no_change")

    def test_a_no_change_is_recorded_with_its_reason(self):
        _context, judged = self.judged()
        written = self.judgements.record(
            event=self.event, judgement=judged, verification=self.verified(judged),
            effect={"kind": "no_change", "status": "recorded",
                    "reason": judged["because"]},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        self.assertEqual(written["decision"], "NO_CHANGE")
        self.assertIn("sector-wide", written["effect"]["reason"])

    def test_recent_judgements_feed_the_next_prompt(self):
        _context, judged = self.judged()
        self.judgements.record(
            event=self.event, judgement=judged, verification=self.verified(judged),
            effect={"kind": "no_change", "status": "recorded"},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        prompt = build_judge_prompt(self.context())
        self.assertIn("NO_CHANGE / no_change", prompt)

    def test_the_pool_is_the_ledger_summed(self):
        state = pool(self.mission)
        self.assertEqual(state["pool"], POOL_NAME)
        _context, judged = self.judged()
        written = self.judgements.record(
            event=self.event, judgement=judged, verification=self.verified(judged),
            effect={"kind": "no_change", "status": "recorded"},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        day = written["created_at"][:10]
        after = pool_state(self.judgements, self.mission, day=day)
        self.assertEqual(after["spent_micros"], 40_000)
        self.assertEqual(after["remaining_micros"], after["cap_micros"] - 40_000)


class EffectTests(JudgementHarness):
    def setUp(self):
        super().setUp()
        self.deliverables = MissionDeliverableAuthority(self.store)

    def effect(self, body, **kwargs):
        context = self.context()
        judged = judge(context, model=FakeModel([body]), mission=self.mission,
                       request_id="r1")
        self.assertEqual(judged["status"], "judged", judged.get("reason"))
        defaults = {
            "playbook": self.playbook, "deliverables": self.deliverables,
            "forecast_models": None, "model_version": None,
            "research_admitter": None, "actor_ref": AUTOMATION,
        }
        defaults.update(kwargs)
        return judged, apply_effect(
            event=self.event, judgement=judged, context=context,
            mission=self.mission, **defaults,
        )

    def test_a_note_becomes_a_deliverable_with_the_claim_it_cites(self):
        claim = self.claim(statement="板块整体下跌，ACN 没有公司层面消息。")
        _judged, effect = self.effect(decision(
            action="note", word="THESIS_WEAKENED",
            citations=[self.event["id"], claim],
            note="ACN 今日走强：板块普遍上涨，没有公司层面消息。最近的 driver 是收入增速。",
        ))
        self.assertEqual(effect["kind"], "note")
        self.assertEqual(effect["status"], "fresh")
        self.assertEqual(effect["deliverable_ref"],
                         "mission-deliverable:event_note:0001467373")
        published = self.deliverables.latest(effect["deliverable_ref"])
        self.assertEqual(published["kind"], "event_note")
        self.assertIn(claim, published["sections"][0]["claim_refs"])

    def test_a_note_carrying_an_unsourced_figure_is_refused_by_the_authority(self):
        _judged, effect = self.effect(decision(
            action="note", word="THESIS_WEAKENED", citations=[self.event["id"]],
            note="收入下滑了 12.5%。",
        ))
        self.assertEqual(effect["status"], "refused")
        self.assertIn("缺来源", effect["reason"])

    def test_research_is_queued_with_a_reason_when_the_entry_point_is_absent(self):
        _judged, effect = self.effect(
            decision(action="research", word="NO_CHANGE", citations=[self.event["id"]],
                     research_question="Did a large client cancel?"),
            research_admitter=None,
        )
        self.assertEqual(effect["status"], "queued")
        self.assertIn("P14e", effect["reason"])

    def test_research_calls_the_admission_entry_point_by_name(self):
        seen = {}

        def admitter(event, judgement):
            seen["question"] = judgement["research_question"]
            return {"status": "admitted", "loop_ref": "bounded-loop:adhoc:1"}

        _judged, effect = self.effect(
            decision(action="research", word="NO_CHANGE", citations=[self.event["id"]],
                     research_question="Did a large client cancel?"),
            research_admitter=admitter,
        )
        self.assertEqual(effect["status"], "admitted")
        self.assertEqual(seen["question"], "Did a large client cancel?")

    def test_a_failing_admission_becomes_a_queued_reason_not_a_crash(self):
        def admitter(event, judgement):
            raise RuntimeError("out_of_mandate_scope")

        _judged, effect = self.effect(
            decision(action="research", word="NO_CHANGE", citations=[self.event["id"]],
                     research_question="Did a large client cancel?"),
            research_admitter=admitter,
        )
        self.assertEqual(effect["status"], "queued")
        self.assertIn("out_of_mandate_scope", effect["reason"])

    def test_a_dossier_revision_is_queued_because_the_dossier_is_wave_two(self):
        _judged, effect = self.effect(decision(
            action="revise_dossier", word="THESIS_STRENGTHENED",
            citations=[self.event["id"]]))
        self.assertEqual(effect["status"], "queued")
        self.assertIn("Wave 2", effect["reason"])

    def test_a_thesis_revision_without_the_grant_is_queued_naming_adr_0007(self):
        _judged, effect = self.effect(decision(
            action="revise_thesis", word="THESIS_WEAKENED",
            citations=[self.event["id"]]))
        self.assertEqual(effect["status"], "queued")
        self.assertIn("ADR-0007", effect["reason"])


class ForecastEffectTests(JudgementHarness):
    def setUp(self):
        super().setUp()
        from tests.test_model_forecast_driver import model as forecast_body

        self.models = ForecastModelAuthority(self.store)
        self.model_version = self.models.publish(forecast_body())
        self.grant("forecast_line")

    def context(self, **kwargs):
        return build_context(
            event=self.event, mission=self.mission, connection=self.store.connection,
            judgements=self.judgements, source_table="t",
            model_version=self.model_version, **kwargs,
        )

    def a_driver_and_period(self):
        drivers = model_drivers(self.model_version)
        for driver in drivers:
            if driver["assumptions"]:
                return driver["ref"], driver["assumptions"][0]["period_end"]
        raise AssertionError("the fixture model has no revisable assumption")

    def test_a_revise_forecast_publishes_a_version_with_driver_event(self):
        driver_ref, period_end = self.a_driver_and_period()
        context = self.context()
        judged = judge(context, model=FakeModel([decision(
            action="revise_forecast", word="THESIS_WEAKENED",
            driver_refs=[driver_ref], citations=[self.event["id"]],
            forecast_change={"driver_ref": driver_ref, "period_end": period_end,
                             "value": "0.02",
                             "because": "The move followed a contract win the "
                                        "trailing average cannot know about."},
        )]), mission=self.mission, request_id="r1")
        self.assertEqual(judged["status"], "judged", judged.get("reason"))
        effect = apply_effect(
            event=self.event, judgement=judged, context=context, mission=self.mission,
            playbook=self.playbook, deliverables=None,
            forecast_models=self.models, model_version=self.model_version,
            research_admitter=None, actor_ref=AUTOMATION,
        )
        self.assertEqual(effect["status"], "fresh")
        self.assertEqual(effect["change_reason"], "driver_event")
        self.assertEqual(effect["decision"], "THESIS_WEAKENED")
        published = self.models.model(effect["model_version_ref"])
        self.assertEqual(published["change_reason"], "driver_event")
        self.assertEqual(published["decision"], "THESIS_WEAKENED")
        self.assertEqual(published["evidence_refs"][0]["ref"], self.event["id"])
        self.assertEqual(published["version"], 2)

    def test_without_the_forecast_grant_the_revision_becomes_a_proposal(self):
        driver_ref, period_end = self.a_driver_and_period()
        params = dict(self.params)
        autonomy = dict(params["autonomy"])
        autonomy["may_write"] = [w for w in autonomy["may_write"] if w != "forecast_line"]
        params["autonomy"] = autonomy
        params.update({"version_id": "coverage-mission-version:us-it-services:20",
                       "prior_version_ref": self.mission["id"],
                       "idempotency_key": "coverage-mission:us-it-services:20"})
        ungranted = self.missions.create_mission(self.mission_ref, **params)
        context = self.context()
        judged = judge(context, model=FakeModel([decision(
            action="revise_forecast", word="THESIS_WEAKENED",
            driver_refs=[driver_ref], citations=[self.event["id"]],
            forecast_change={"driver_ref": driver_ref, "period_end": period_end,
                             "value": "0.02", "because": "A contract win."},
        )]), mission=ungranted, request_id="r1")
        effect = apply_effect(
            event=self.event, judgement=judged, context=context, mission=ungranted,
            playbook=self.playbook, deliverables=None, forecast_models=self.models,
            model_version=self.model_version, research_admitter=None,
            actor_ref=AUTOMATION,
        )
        self.assertEqual(effect["status"], "proposed")
        self.assertIn("human checkpoint", effect["reason"])

    def test_a_forecast_revision_proposal_records_its_shape(self):
        driver_ref, period_end = self.a_driver_and_period()
        context = self.context()
        judged = judge(context, model=FakeModel([decision()]), mission=self.mission,
                       request_id="r1")
        written = self.judgements.record(
            event=self.event, judgement=judged,
            verification={"status": "verified", "verdict": "pass", "findings": []},
            effect={"kind": "revise_forecast", "status": "proposed"},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        proposal = self.judgements.record_forecast_proposal(
            judgement_ref=written["id"], company_ref=ACN,
            model_version_ref=self.model_version["id"],
            change={"driver_ref": driver_ref, "period_end": period_end,
                    "value": "0.02", "because": "A contract win."},
            decision="THESIS_WEAKENED", because="The move followed a contract win.",
            evidence_refs=[self.event["id"]], mission=self.mission,
            actor_ref=AUTOMATION, reason="the mission does not grant forecast_line",
        )
        self.assertEqual(proposal["checkpoint_kind"], "forecast_overturn")
        self.assertEqual(proposal["proposed_value"], "0.02")
        self.assertEqual(self.judgements.forecast_proposals(ACN)[0]["id"], proposal["id"])


class ThesisCandidateTests(JudgementHarness):
    def setUp(self):
        super().setUp()
        self.grant("thesis_revision_candidate",
                   checkpoints=("thesis_revision_candidate",))

    def thesis(self):
        return {
            "id": "thesis-version:abc", "content_hash": "f" * 64,
            "thesis_ref": "thesis:acn:ai-reinvention-growth",
            "falsifier_refs": ["falsifier:bookings-contract"],
        }

    def test_a_candidate_carries_adr_0007_s_shape(self):
        context = self.context()
        judged = judge(context, model=FakeModel([decision()]), mission=self.mission,
                       request_id="r1")
        written = self.judgements.record(
            event=self.event, judgement=judged,
            verification={"status": "verified", "verdict": "pass", "findings": []},
            effect={"kind": "revise_thesis", "status": "candidate"},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        candidate = self.judgements.record_thesis_candidate(
            judgement_ref=written["id"], thesis=self.thesis(), company_ref=ACN,
            decision="THESIS_WEAKENED",
            because="A four percent move against flat peers with no company news.",
            evidence_refs=[self.event["id"]],
            falsifier_ref="falsifier:bookings-contract",
            proposed_statement=None, proposed_confidence="low",
            mission=self.mission, actor_ref=AUTOMATION,
        )
        self.assertEqual(candidate["checkpoint_kind"], "thesis_revision_candidate")
        self.assertEqual(candidate["decision"], "THESIS_WEAKENED")
        self.assertEqual(candidate["thesis_version_ref"], "thesis-version:abc")
        self.assertEqual(candidate["falsifier_ref"], "falsifier:bookings-contract")
        self.assertEqual(candidate["proposed_confidence"], "low")
        self.assertEqual(candidate["evidence_refs"], [self.event["id"]])
        self.assertEqual(len(self.judgements.thesis_candidates(ACN)), 1)

    def test_a_float_confidence_is_refused(self):
        with self.assertRaises(EventJudgementValidationError):
            self.judgements.record_thesis_candidate(
                judgement_ref="event-judgement:x", thesis=self.thesis(),
                company_ref=ACN, decision="THESIS_WEAKENED", because="b",
                evidence_refs=[self.event["id"]], falsifier_ref=None,
                proposed_statement=None, proposed_confidence="0.4",
                mission=self.mission, actor_ref=AUTOMATION,
            )

    def test_a_candidate_with_no_evidence_is_refused(self):
        with self.assertRaises(EventJudgementValidationError):
            self.judgements.record_thesis_candidate(
                judgement_ref="event-judgement:x", thesis=self.thesis(),
                company_ref=ACN, decision="THESIS_WEAKENED", because="b",
                evidence_refs=[], falsifier_ref=None, proposed_statement=None,
                proposed_confidence=None, mission=self.mission, actor_ref=AUTOMATION,
            )

    def test_a_sixth_decision_word_is_refused_by_contract(self):
        with self.assertRaises(EventJudgementValidationError):
            self.judgements.record_thesis_candidate(
                judgement_ref="event-judgement:x", thesis=self.thesis(),
                company_ref=ACN, decision="THESIS_MOSTLY_FINE", because="b",
                evidence_refs=[self.event["id"]], falsifier_ref=None,
                proposed_statement=None, proposed_confidence=None,
                mission=self.mission, actor_ref=AUTOMATION,
            )

    def test_the_effect_produces_candidates_when_the_grant_and_checkpoint_are_there(self):
        thesis_row = {"ref": "thesis-version:abc", "content_hash": "f" * 64,
                      "thesis_ref": "thesis:acn", "statement": "s",
                      "falsifier_refs": []}
        context = {**self.context(), "theses": [thesis_row]}
        judged = validate_judge_output(decision(
            action="revise_thesis", word="THESIS_WEAKENED",
            thesis_refs=["thesis-version:abc"], citations=[self.event["id"]],
        ), context)
        effect = apply_effect(
            event=self.event, judgement=judged, context=context, mission=self.mission,
            playbook=self.playbook, deliverables=None, forecast_models=None,
            model_version=None, research_admitter=None, actor_ref=AUTOMATION,
        )
        self.assertEqual(effect["status"], "candidate")
        self.assertEqual(effect["theses"][0]["ref"], "thesis-version:abc")


class ThesisReadTests(P14aHarness):
    def test_a_core_with_no_thesis_table_reads_as_no_theses(self):
        self.assertEqual(company_theses(self.store.connection, ACN), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


REFLECTION = {
    "thesis_refs": [],
    "what_we_expected": "Reinvention bookings would hold the top line while "
                        "discretionary consulting kept falling.",
    "what_happened": "The shares gave up eight points against flat peers over the "
                     "window with no company news we hold.",
    "why": "Nothing in the ledger explains it, which is itself the finding.",
    "citations": [],
    "missed_debates": [{"question": "Is federal exposure the debate the street has "
                                    "moved to?", "refs": []}],
    "followup_tracking": [{"source_key": "alphaengine", "interval_seconds": 21600,
                           "because": "Twice a day is not enough while the price is "
                                      "moving without an explanation we hold."}],
    "followup_research": [{"question": "Did a federal contract stop?",
                           "wants": "A filing or a management statement either way."}],
    "market_view_vs_ours": {
        "available": False, "our_direction": "long",
        "summary": "This system holds no consensus, no rating change and no desk note "
                   "for this company, so there is nothing to compare our view against.",
        "refs": [],
    },
    "convergence_pathway": "A bookings number at the next print that does not "
                           "decelerate would move the market toward our view.",
}


class ReflectionOwedTests(JudgementHarness):
    def test_a_divergence_owes_a_reflection_even_when_we_hold(self):
        # "Why we are holding" is the answer the weekly review most needs and
        # the one nothing in this system has ever written down.
        divergence = {**self.event, "kind": "price_divergence"}
        self.assertTrue(reflection_is_owed(divergence, decision()))

    def test_every_revise_shaped_decision_owes_one(self):
        for action in ("revise_forecast", "revise_thesis", "revise_dossier"):
            self.assertTrue(
                reflection_is_owed(self.event, decision(action=action,
                                                        word="THESIS_WEAKENED")),
                action,
            )

    def test_a_note_on_an_ordinary_event_owes_nothing(self):
        self.assertFalse(reflection_is_owed(self.event, decision(action="note",
                                                                 word="THESIS_WEAKENED")))
        self.assertFalse(reflection_is_owed(self.event, decision()))


class ReflectionContractTests(JudgementHarness):
    def reflection_context(self, **kwargs):
        return build_context(
            event=self.event, mission=self.mission, connection=self.store.connection,
            judgements=self.judgements, source_table="alphaengine | news | sell_side",
            source_keys=["alphaengine", "x-xreach"], **kwargs,
        )

    def check(self, body, **kwargs):
        return validate_reflection_output(body, self.reflection_context(**kwargs))

    def test_the_minimal_reflection_is_accepted_and_says_no_market_data(self):
        result = self.check({**REFLECTION, "citations": [self.event["id"]]})
        self.assertFalse(result["market_view_vs_ours"]["available"])
        self.assertEqual(result["followup_tracking"][0]["source_key"], "alphaengine")
        self.assertEqual(result["followup_research"][0]["question"],
                         "Did a federal contract stop?")

    def test_a_reflection_with_no_citation_is_an_opinion_about_nothing(self):
        with self.assertRaises(EventJudgementValidationError):
            self.check(REFLECTION)

    def test_an_extra_key_is_refused_whole(self):
        with self.assertRaises(EventJudgementValidationError):
            self.check({**REFLECTION, "citations": [self.event["id"]],
                        "confidence": "high"})

    def test_a_market_view_asserted_as_available_must_cite_something(self):
        with self.assertRaises(EventJudgementValidationError) as caught:
            self.check({**REFLECTION, "citations": [self.event["id"]],
                        "market_view_vs_ours": {**REFLECTION["market_view_vs_ours"],
                                                "available": True}})
        self.assertIn("must name what it rests on", str(caught.exception))

    def test_a_market_view_said_to_be_unavailable_cannot_cite_anything(self):
        with self.assertRaises(EventJudgementValidationError):
            self.check({**REFLECTION, "citations": [self.event["id"]],
                        "market_view_vs_ours": {**REFLECTION["market_view_vs_ours"],
                                                "refs": [self.event["id"]]}})

    def test_a_tracking_follow_up_naming_an_unknown_source_is_refused(self):
        with self.assertRaises(EventJudgementValidationError) as caught:
            self.check({**REFLECTION, "citations": [self.event["id"]],
                        "followup_tracking": [{"source_key": "bloomberg",
                                               "interval_seconds": 3600,
                                               "because": "b"}]})
        self.assertIn("no baseline cadence", str(caught.exception))

    def test_an_invented_citation_is_refused(self):
        with self.assertRaises(EventJudgementValidationError):
            self.check({**REFLECTION, "citations": ["claim-version:invented"]})

    def test_a_market_view_that_cites_a_recorded_rating_change_is_allowed(self):
        rating = record_event(
            self.events, company_ref=ACN, kind="rating_change",
            occurred_at="2026-09-08T00:00:00+00:00",
            source_refs=["source:alphaengine", "alphaengine-doc:7"],
            payload={"document_ref": "alphaengine-doc:7",
                     "source_ref": "source:alphaengine", "broker": "Wolfe",
                     "from_rating": "outperform", "to_rating": "peer perform",
                     "price_target": "310"},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        recent = self.events.events(company_ref=ACN, limit=20)
        result = self.check(
            {**REFLECTION, "citations": [self.event["id"]],
             "market_view_vs_ours": {"available": True, "our_direction": "long",
                                     "summary": "One broker cut to peer perform.",
                                     "refs": [rating["id"]]}},
            recent_events=recent,
        )
        self.assertTrue(result["market_view_vs_ours"]["available"])
        self.assertEqual(result["market_view_vs_ours"]["refs"], [rating["id"]])

    def test_the_prompt_says_out_loud_when_there_is_no_market_data(self):
        prompt = build_reflection_prompt(self.reflection_context(), decision())
        self.assertIn("no consensus authority exists yet", prompt)
        self.assertIn("if the market is bullish and we are bullish, our view",
                      prompt.lower())
        self.assertIn("convergence_pathway", prompt)

    def test_the_judge_prompt_carries_the_standing_instruction(self):
        prompt = build_judge_prompt(self.reflection_context())
        self.assertIn("agreeing with the market is worth nothing", prompt.lower())
        self.assertIn("move the market toward our view", prompt.lower())
        self.assertIn("price_divergence", prompt)


class ReflectionLedgerTests(JudgementHarness):
    def setUp(self):
        super().setUp()
        # Granting republishes the mission, and every later write has to bind
        # the version that is current; the event was recorded under the one
        # before it.
        self.grant("thesis_revision_candidate",
                   checkpoints=("thesis_revision_candidate",))
        self.events = ResearchEventAuthority(self.store)

    def reflection_context(self):
        return build_context(
            event=self.event, mission=self.mission, connection=self.store.connection,
            judgements=self.judgements, source_table="t",
            source_keys=["alphaengine"],
        )

    def judged(self):
        context = self.reflection_context()
        judged = judge(context, model=FakeModel([decision()]), mission=self.mission,
                       request_id="r1")
        written = self.judgements.record(
            event=self.event, judgement=judged,
            verification={"status": "verified", "verdict": "pass", "findings": []},
            effect={"kind": "no_change", "status": "recorded"},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        return context, judged, written

    def test_a_reflection_reads_back_and_is_bound_to_its_judgement(self):
        context, judged, written = self.judged()
        thought = reflect(context, judged,
                          model=FakeModel([{**REFLECTION,
                                            "citations": [self.event["id"]]}]),
                          mission=self.mission, request_id="f1")
        self.assertEqual(thought["status"], "reflected", thought.get("reason"))
        reviewed = verify_reflection(
            context, thought, model=FakeModel([PASS], route=VERIFIER_ROUTE),
            mission=self.mission, request_id="fv1", family_resolver=resolver(),
        )
        self.assertEqual(reviewed["verdict"], "pass")
        recorded = self.judgements.record_reflection(
            judgement=written, event=self.event, reflection=thought,
            verification=reviewed, mission=self.mission, actor_ref=AUTOMATION,
        )
        self.assertEqual(recorded["status"], "fresh")
        self.assertEqual(recorded["judgement_ref"], written["id"])
        self.assertEqual(recorded["trigger_event_ref"], self.event["id"])
        self.assertEqual(recorded["trigger_kind"], "revision")
        read = self.judgements.reflection_for(written["id"])
        self.assertEqual(read["content_hash"], recorded["content_hash"])
        self.assertEqual(read["convergence_pathway"], REFLECTION["convergence_pathway"])

    def test_a_divergence_trigger_is_recorded_as_one(self):
        divergence = record_event(
            self.events, company_ref=ACN, kind="price_divergence",
            occurred_at="2026-09-09T00:00:00+00:00",
            source_refs=["market-price-series-version:1", "thesis-version:acn"],
            payload={"window_days": 10, "from_date": "2026-08-26",
                     "as_of": "2026-09-09", "cumulative_return_percent": "-8.0000",
                     "basket_return_percent": "0.0000",
                     "excess_vs_basket_percent": "-8.0000", "basket_members": 4,
                     "thesis_ref": "thesis-version:acn", "thesis_stance": "long",
                     "divergence_percent": "8.0000", "threshold_percent": "6.0",
                     "price_version_ref": "market-price-series-version:1"},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        context = build_context(
            event=divergence, mission=self.mission, connection=self.store.connection,
            judgements=self.judgements, source_table="t", source_keys=["alphaengine"],
        )
        judged = judge(context, model=FakeModel([decision()]), mission=self.mission,
                       request_id="r2")
        written = self.judgements.record(
            event=divergence, judgement=judged,
            verification={"status": "verified", "verdict": "pass", "findings": []},
            effect={"kind": "no_change", "status": "recorded"},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        thought = reflect(context, judged,
                          model=FakeModel([{**REFLECTION,
                                            "citations": [divergence["id"]]}]),
                          mission=self.mission, request_id="f2")
        recorded = self.judgements.record_reflection(
            judgement=written, event=divergence, reflection=thought,
            verification={"status": "verified", "verdict": "pass", "findings": []},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        self.assertEqual(recorded["trigger_kind"], "price_divergence")
        self.assertEqual(recorded["decision"], "NO_CHANGE")

    def test_a_second_reflection_on_one_judgement_is_a_duplicate(self):
        context, judged, written = self.judged()
        thought = reflect(context, judged,
                          model=FakeModel([{**REFLECTION,
                                            "citations": [self.event["id"]]}]),
                          mission=self.mission, request_id="f1")
        passed = {"status": "verified", "verdict": "pass", "findings": []}
        first = self.judgements.record_reflection(
            judgement=written, event=self.event, reflection=thought,
            verification=passed, mission=self.mission, actor_ref=AUTOMATION,
        )
        second = self.judgements.record_reflection(
            judgement=written, event=self.event, reflection=thought,
            verification=passed, mission=self.mission, actor_ref=AUTOMATION,
        )
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["id"], first["id"])

    def test_the_follow_ups_are_candidates_and_not_writes(self):
        context, judged, written = self.judged()
        thought = reflect(context, judged,
                          model=FakeModel([{**REFLECTION,
                                            "citations": [self.event["id"]]}]),
                          mission=self.mission, request_id="f1")
        recorded = self.judgements.record_reflection(
            judgement=written, event=self.event, reflection=thought,
            verification={"status": "verified", "verdict": "pass", "findings": []},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        self.assertEqual(recorded["followup_tracking"][0]["interval_seconds"], 21600)
        # Nothing was published: a reflection proposes, it does not act.
        from dalton_core.tracking_cadence import TrackingCadenceAuthority

        cadences = TrackingCadenceAuthority(self.store)
        self.assertIsNone(cadences.latest(ACN, "alphaengine"))

    def test_a_refused_reflection_is_not_recorded(self):
        context, judged, _written = self.judged()
        thought = reflect(context, judged, model=FakeModel(["nonsense"]),
                          mission=self.mission, request_id="f1")
        self.assertEqual(thought["status"], "refused")
        reviewed = verify_reflection(
            context, thought, model=FakeModel([PASS], route=VERIFIER_ROUTE),
            mission=self.mission, request_id="fv1", family_resolver=resolver(),
        )
        self.assertEqual(reviewed["status"], "skipped")

    def test_the_reflection_verifier_obeys_the_same_independence_rule(self):
        context, judged, _written = self.judged()
        thought = reflect(context, judged,
                          model=FakeModel([{**REFLECTION,
                                            "citations": [self.event["id"]]}]),
                          mission=self.mission, request_id="f1")
        reviewed = verify_reflection(
            context, thought, model=FakeModel([PASS], route=VERIFIER_ROUTE),
            mission=self.mission, request_id="fv1",
            family_resolver=resolver({JUDGE_ROUTE: "anthropic",
                                      VERIFIER_ROUTE: "anthropic"}),
        )
        self.assertEqual(reviewed["status"], "refused")
        self.assertIn("model_family_not_independent", reviewed["reason"])

    def test_a_candidate_carries_the_reflection_the_person_should_read_beside_it(self):
        context, judged, written = self.judged()
        thought = reflect(context, judged,
                          model=FakeModel([{**REFLECTION,
                                            "citations": [self.event["id"]]}]),
                          mission=self.mission, request_id="f1")
        recorded = self.judgements.record_reflection(
            judgement=written, event=self.event, reflection=thought,
            verification={"status": "verified", "verdict": "pass", "findings": []},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        candidate = self.judgements.record_thesis_candidate(
            judgement_ref=written["id"],
            thesis={"id": "thesis-version:abc", "content_hash": "f" * 64,
                    "thesis_ref": "thesis:acn", "falsifier_refs": []},
            company_ref=ACN, decision="THESIS_WEAKENED", because="b",
            evidence_refs=[self.event["id"]], falsifier_ref=None,
            proposed_statement=None, proposed_confidence=None,
            mission=self.mission, actor_ref=AUTOMATION,
            reflection_ref=recorded["id"],
        )
        self.assertEqual(candidate["reflection_ref"], recorded["id"])
