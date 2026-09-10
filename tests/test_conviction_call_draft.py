"""P15d drafting: the table, the prompt, the refusal paths and the verifier."""

from __future__ import annotations

import json
import unittest

from dalton_core.cockpit_model import CockpitModelError, purposes
from dalton_core.conviction_call import precheck, rubric_findings
from dalton_core.conviction_call_draft import (
    MAX_PROMPT_BYTES,
    PURPOSE,
    ConvictionDraftRefused,
    assemble_call,
    build_input_table,
    build_prompt,
    build_verifier_prompt,
    draft_conviction_call,
    independent,
    parse_draft,
    parse_verdict,
    prompt_drafter,
)
from dalton_core.research_quality_rubrics import CONVICTION_CALL

ACN = "company:sec-cik:0001467373"
THESIS = "thesis-version:1"
DEBATE = "debate:bookings"
AGREED = "debate:margins"
CATALYST = "catalyst-entry:acn:earnings:2026-09-25"
MISSION = {"id": "coverage-mission-version:x", "content_hash": "b" * 64,
           "budget": {"max_daily_paid_calls": 10, "max_daily_cost_usd": "5"}}

THESES = [{"thesis_version_ref": THESIS, "statement": "Bookings hold above ten",
           "confidence": "medium", "falsifier_refs": ["falsifier:bookings"]}]
DEBATES = [
    {"debate_ref": DEBATE, "question": "Will bookings hold?", "status": "open",
     "market_position": {"available": True, "lean": "bear",
                         "statement": "brokers model deceleration",
                         "refs": ["claim-version:a"]},
     "our_position": {"state": "held", "side": "bull",
                      "statement": "we model acceleration", "refs": [THESIS]}},
    {"debate_ref": AGREED, "question": "Will margin hold?", "status": "open",
     "market_position": {"available": True, "lean": "bull",
                         "statement": "brokers model expansion",
                         "refs": ["claim-version:b"]},
     "our_position": {"state": "held", "side": "bull",
                      "statement": "we model expansion too", "refs": [THESIS]}},
]
CATALYSTS = [{"entry_ref": CATALYST, "company_ref": ACN, "event_kind": "earnings",
              "expected_date": "2026-09-25", "confidence": "confirmed",
              "date_unconfirmed": False}]
NO_CONSENSUS = {"status": "unavailable", "metrics": [],
                "reason": "this Core has no consensus authority"}


def table(**overrides):
    gate = precheck(company_ref=ACN, theses=THESES, open_debates=DEBATES,
                    consensus_gap=overrides.get("consensus_gap", NO_CONSENSUS),
                    dossier_variant_view=None)
    params = {
        "company_ref": ACN, "precheck_record": gate, "theses": THESES,
        "open_debates": DEBATES, "consensus_gap": NO_CONSENSUS,
        "catalyst_entries": CATALYSTS,
    }
    params.update(overrides)
    return build_input_table(**params)


def draft_reply(**overrides) -> str:
    reply = {
        "direction": "long", "decision": "THESIS_STRENGTHENED", "confidence": "medium",
        "time_horizon": "6_12_months",
        "our_view": {"statement": "the lag is two to four quarters", "refs": ["T1"]},
        "market_view": {"available": True, "reason": None,
                        "statement": "the street models the two as concurrent",
                        "refs": ["D1"], "sources": ["debate_market_position"]},
        "where_market_is_wrong": {"statement": "the lag, not the direction",
                                  "refs": ["D1"]},
        "convergence_pathway": {"statement": "the September print shows it",
                                "refs": ["K1"]},
        "event_pathway": [{"signal": "book-to-bill above 1.1",
                           "catalyst_row_id": "K1", "refs": ["K1"]}],
        "upside": {"statement": "re-rating to the median", "percent": "55",
                   "refs": ["D1"]},
        "downside": {"statement": "the lag is wrong", "percent": "20", "refs": ["D1"]},
        "falsifiers": [{"statement": "book-to-bill under 1.0 twice",
                        "thesis_row_id": "T1",
                        "falsifier_ref": "falsifier:bookings"}],
    }
    reply.update(overrides)
    return json.dumps(reply)


PASS = json.dumps({"verdict": "pass", "findings": []})


class FakeModel:
    """A model that answers from a script and records what it was asked."""

    def __init__(self, replies, *, families=("family-a", "family-b"), raises=None):
        self.replies = list(replies)
        self.families = list(families)
        self.raises = raises or {}
        self.calls: list[dict] = []

    @property
    def prompts(self):
        return [call["prompt"] for call in self.calls]

    def call(self, *, purpose, request_id, prompt, mission):
        index = len(self.calls)
        self.calls.append({"purpose": purpose, "request_id": request_id,
                           "prompt": prompt, "mission": mission})
        if index in self.raises:
            raise self.raises[index]
        return {"text": self.replies[index], "replayed": False, "cost_micros": 1_000,
                "work_order_ref": f"wo-{index}", "invocation_ref": f"inv-{index}",
                "route_decision_ref": f"route-{index}"}

    def family_of(self, route_decision_ref):
        if not isinstance(route_decision_ref, str):
            return None
        index = int(route_decision_ref.rsplit("-", 1)[1])
        return self.families[index] if index < len(self.families) else None


class InputTableTests(unittest.TestCase):
    def test_the_purpose_is_registered_from_this_module(self):
        self.assertEqual(PURPOSE, "conviction_call")
        self.assertIn("conviction_call", purposes())

    def test_the_divergent_debate_comes_first_and_is_labelled(self):
        built = table()
        self.assertEqual([row["ref"] for row in built["debates"]], [DEBATE, AGREED])
        self.assertTrue(built["debates"][0]["divergent"])
        self.assertFalse(built["debates"][1]["divergent"])

    def test_only_shown_rows_are_citable(self):
        built = table()
        self.assertEqual(sorted(built["citable"]), ["D1", "D2", "K1", "T1"])
        self.assertEqual(built["citable"]["T1"], THESIS)
        self.assertEqual(built["citable"]["K1"], CATALYST)

    def test_the_prompt_says_agreement_is_worth_nothing(self):
        prompt = build_prompt(table())
        self.assertIn("differs from the market", prompt)
        self.assertIn("Being bullish while the street is bullish is worth nothing",
                      prompt)
        self.assertIn("DIVERGENT means we already stand somewhere the street", prompt)
        self.assertIn("Do not write dates", prompt)
        self.assertIn("a fact, a timing, a transmission or a multiple", prompt)

    def test_the_prompt_carries_the_playbooks_own_standards(self):
        prompt = build_prompt(table())
        self.assertIn("risk-reward:value:6-12m", prompt)
        self.assertIn("做空：3–6 个月 30% downside", prompt)

    def test_an_absent_consensus_is_shown_as_absent_not_omitted(self):
        prompt = build_prompt(table())
        self.assertIn("no consensus on this Core", prompt)
        self.assertIn("this Core has no consensus authority", prompt)

    def test_a_consensus_metric_becomes_a_citable_row(self):
        gap = {"status": "available", "reason": None, "metrics": [{
            "metric": "metric:revenue-usd", "period": "FY2027", "ours": "82000",
            "consensus": "69000", "unit": "USDm", "gap_percent": "18",
            "refs": ["forecast-model-version:1"]}]}
        built = table(consensus_gap=gap)
        self.assertEqual(built["citable"]["G1"], "forecast-model-version:1")
        self.assertIn("gap 18%", build_prompt(built))

    def test_the_drafter_ref_is_content_addressed(self):
        prompt = build_prompt(table())
        self.assertEqual(prompt_drafter("wo", prompt), prompt_drafter("wo", prompt))
        self.assertNotEqual(prompt_drafter("wo", prompt),
                            prompt_drafter("wo", prompt + " "))


class ParseDraftTests(unittest.TestCase):
    def setUp(self):
        self.table = table()

    def test_a_well_formed_draft_resolves_row_ids_to_refs(self):
        parsed = parse_draft(draft_reply(), self.table)
        self.assertEqual(parsed["variant_view"]["our_view"]["refs"], [THESIS])
        self.assertEqual(parsed["variant_view"]["market_view"]["refs"], [DEBATE])
        self.assertEqual(parsed["risk_reward"]["standard"]["status"], "met")

    def test_the_date_comes_from_the_calendar_row_not_the_model(self):
        parsed = parse_draft(draft_reply(), self.table)
        step = parsed["event_pathway"][0]
        self.assertEqual(step["window"], {"kind": "date", "date": "2026-09-25",
                                          "from": None, "to": None})
        self.assertEqual(step["catalyst_ref"], CATALYST)

    def test_a_step_off_the_calendar_is_undated_rather_than_guessed(self):
        parsed = parse_draft(draft_reply(event_pathway=[{
            "signal": "a named client moves work into its own GCC",
            "catalyst_row_id": None, "refs": ["D1"]}]), self.table)
        self.assertEqual(parsed["event_pathway"][0]["window"]["kind"], "unknown")
        self.assertIsNone(parsed["event_pathway"][0]["catalyst_ref"])

    def test_a_row_id_that_was_not_shown_refuses_the_whole_draft(self):
        with self.assertRaises(ConvictionDraftRefused):
            parse_draft(draft_reply(our_view={"statement": "x", "refs": ["T9"]}),
                        self.table)

    def test_a_calendar_row_that_was_not_shown_refuses_the_whole_draft(self):
        with self.assertRaises(ConvictionDraftRefused):
            parse_draft(draft_reply(event_pathway=[{
                "signal": "x", "catalyst_row_id": "K9", "refs": ["D1"]}]), self.table)

    def test_a_falsifier_nobody_carries_refuses_the_whole_draft(self):
        with self.assertRaises(ConvictionDraftRefused):
            parse_draft(draft_reply(falsifiers=[{
                "statement": "x", "thesis_row_id": "T1",
                "falsifier_ref": "falsifier:invented"}]), self.table)

    def test_a_falsifier_on_a_thesis_that_was_not_shown_is_refused(self):
        with self.assertRaises(ConvictionDraftRefused):
            parse_draft(draft_reply(falsifiers=[{
                "statement": "x", "thesis_row_id": "T7", "falsifier_ref": None}]),
                self.table)

    def test_an_extra_key_refuses_the_whole_draft(self):
        reply = json.loads(draft_reply())
        reply["position_size"] = "3%"
        with self.assertRaises(ConvictionDraftRefused):
            parse_draft(json.dumps(reply), self.table)

    def test_a_reply_that_is_not_an_object_refuses_the_whole_draft(self):
        with self.assertRaises(ConvictionDraftRefused):
            parse_draft("I do not think there is a call here.", self.table)
        with self.assertRaises(ConvictionDraftRefused):
            parse_draft("", self.table)

    def test_a_fenced_object_is_read_but_a_truncated_one_is_not(self):
        # The unwrapper tolerates a code fence, which models add; it cannot
        # rescue a reply that stopped mid-object, and that must not be
        # silently half-parsed.
        self.assertEqual(
            parse_draft("```json\n" + draft_reply() + "\n```", self.table)["direction"],
            "long")
        with self.assertRaises(ConvictionDraftRefused):
            parse_draft(draft_reply()[:-40], self.table)

    def test_a_word_outside_the_five_refuses_the_draft(self):
        with self.assertRaises(ConvictionDraftRefused):
            parse_draft(draft_reply(decision="THESIS_IMPROVED"), self.table)

    def test_a_signed_percentage_is_refused_rather_than_repaired(self):
        with self.assertRaises(ConvictionDraftRefused):
            parse_draft(draft_reply(downside={"statement": "x", "percent": "-20",
                                              "refs": ["D1"]}), self.table)

    def test_a_market_view_source_outside_the_ladder_is_refused(self):
        with self.assertRaises(ConvictionDraftRefused):
            parse_draft(draft_reply(market_view={
                "available": True, "reason": None, "statement": "bearish",
                "refs": ["D1"], "sources": ["a friend"]}), self.table)

    def test_an_unsourced_market_view_is_allowed_through_the_parser(self):
        # The parser's job is fidelity to the table, not judgement. "Nobody has
        # told us where the street is" is a legitimate reply; the *run* is what
        # turns it into "no call today".
        parsed = parse_draft(draft_reply(market_view={
            "available": False, "reason": "no broker note names a target",
            "statement": None, "refs": [], "sources": []}), self.table)
        self.assertFalse(parsed["variant_view"]["market_view"]["available"])


class AssembleTests(unittest.TestCase):
    def test_the_consensus_gap_and_the_divergent_debates_are_not_the_models(self):
        built = table()
        call = assemble_call(parse_draft(draft_reply(), built), built)
        self.assertEqual(call["consensus_gap"], built["consensus_gap"])
        # Only the debate we actually differ on is recorded as the ground.
        self.assertEqual(call["debate_refs"], [DEBATE])
        self.assertEqual(call["thesis_refs"], [THESIS])
        self.assertEqual(rubric_findings(call), [])


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.table = table()
        self.call = assemble_call(parse_draft(draft_reply(), self.table), self.table)

    def test_the_verifier_sees_the_call_and_the_rows_it_stands_on(self):
        prompt = build_verifier_prompt(self.table, self.call)
        self.assertIn("You are an independent verifier", prompt)
        self.assertIn("this_is_not_a_disagreement", prompt)
        self.assertIn("2026-09-25: book-to-bill above 1.1", prompt)
        # Refs are shown back as the row ids the drafter was given.
        self.assertIn("[T1]", prompt)
        self.assertLessEqual(len(prompt.encode("utf-8")), MAX_PROMPT_BYTES)

    def test_a_pass_with_findings_is_refused(self):
        with self.assertRaises(ConvictionDraftRefused):
            parse_verdict(json.dumps({"verdict": "pass", "findings": [
                {"code": "this_is_not_a_disagreement", "detail": "x"}]}))

    def test_a_reject_without_findings_is_refused(self):
        with self.assertRaises(ConvictionDraftRefused):
            parse_verdict(json.dumps({"verdict": "reject", "findings": []}))

    def test_an_unknown_finding_code_is_refused(self):
        with self.assertRaises(ConvictionDraftRefused):
            parse_verdict(json.dumps({"verdict": "reject", "findings": [
                {"code": "i_do_not_like_it", "detail": "x"}]}))

    def test_a_verifier_that_rewrites_instead_of_judging_is_refused(self):
        with self.assertRaises(ConvictionDraftRefused):
            parse_verdict(json.dumps({"verdict": "reject", "findings": [],
                                      "corrected_call": {}}))


class IndependenceTests(unittest.TestCase):
    def test_two_calls_on_one_family_are_not_a_verification(self):
        ok, reason = independent(
            {"work_order_ref": "a", "model_family": "f"},
            {"work_order_ref": "b", "model_family": "f"})
        self.assertFalse(ok)
        self.assertIn("family", reason)

    def test_an_unknown_family_fails_closed(self):
        ok, _ = independent({"work_order_ref": "a", "model_family": None},
                            {"work_order_ref": "b", "model_family": "g"})
        self.assertFalse(ok)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.table = table()

    def run_draft(self, model, family_of=None):
        return draft_conviction_call(
            table=self.table, model=model, mission=MISSION,
            family_of=family_of or model.family_of,
            rubric_ref=CONVICTION_CALL.rubric_ref,
            rubric_hash=CONVICTION_CALL.content_hash)

    def test_a_verified_draft_is_proposable_and_costs_two_calls(self):
        model = FakeModel([draft_reply(), PASS])
        result = self.run_draft(model)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["cost_micros"], 2_000)
        self.assertEqual(result["drafted_by"]["model_family"], "family-a")
        self.assertEqual(result["verified_by"]["model_family"], "family-b")
        self.assertEqual(result["call"]["direction"], "long")
        self.assertEqual(result["rubric"]["findings"], [])
        self.assertEqual([call["purpose"] for call in model.calls],
                         ["conviction_call", "conviction_call"])

    def test_an_honest_absence_of_a_market_view_ends_the_run_without_verifying(self):
        model = FakeModel([draft_reply(market_view={
            "available": False, "reason": "no broker note names a target",
            "statement": None, "refs": [], "sources": []})])
        result = self.run_draft(model)
        self.assertEqual(result["status"], "no_variant_view")
        self.assertIn("no broker note", result["reason"])
        # One call, not two: there is nothing to verify.
        self.assertEqual(len(model.calls), 1)

    def test_a_call_that_fails_the_rubric_mechanically_is_never_verified(self):
        # A short outside three to six months. The Playbook is explicit that a
        # short must state that horizon, so the call is refused before a second
        # call is paid for.
        model = FakeModel([draft_reply(direction="short",
                                       time_horizon="6_12_months")])
        result = self.run_draft(model)
        self.assertEqual(result["status"], "rubric_failed")
        self.assertEqual(result["rubric"]["findings"],
                         ["horizon_matches_the_direction"])
        self.assertEqual(len(model.calls), 1)

    def test_a_draft_outside_the_table_is_refused_whole(self):
        model = FakeModel([draft_reply(our_view={"statement": "x", "refs": ["T9"]})])
        result = self.run_draft(model)
        self.assertEqual(result["status"], "refused")
        self.assertIsNone(result["call"])

    def test_a_verifier_on_the_producers_family_publishes_nothing(self):
        model = FakeModel([draft_reply(), PASS], families=("family-a", "family-a"))
        result = self.run_draft(model)
        self.assertEqual(result["status"], "not_independent")
        self.assertIsNone(result["call"])

    def test_a_family_that_cannot_be_established_fails_closed(self):
        model = FakeModel([draft_reply(), PASS])
        result = self.run_draft(model, family_of=lambda ref: None)
        self.assertEqual(result["status"], "not_independent")

    def test_a_rejecting_verifier_publishes_nothing_and_says_which_code(self):
        model = FakeModel([draft_reply(), json.dumps({"verdict": "reject", "findings": [
            {"code": "this_is_not_a_disagreement",
             "detail": "the call and the market say the same thing"}]})])
        result = self.run_draft(model)
        self.assertEqual(result["status"], "verifier_rejected")
        self.assertIn("this_is_not_a_disagreement", result["reason"])
        self.assertIsNone(result["call"])

    def test_a_model_that_is_not_there_is_reported_not_raised(self):
        model = FakeModel([draft_reply(), PASS],
                          raises={0: CockpitModelError("no route")})
        self.assertEqual(self.run_draft(model)["status"], "model_unavailable")
        model = FakeModel([draft_reply(), PASS],
                          raises={1: CockpitModelError("no route")})
        result = self.run_draft(model)
        self.assertEqual(result["status"], "unverified")
        self.assertIsNone(result["call"])


if __name__ == "__main__":
    unittest.main()
