"""P12c drafting: the prompt, the refusal paths and the independent verifier."""

from __future__ import annotations

import json
import unittest

from dalton_core.cockpit_model import CockpitModelError, purposes
from dalton_core.debate_map import index_claims
from dalton_core.debate_map_draft import (
    DRAFT_CONTRACT_HASH,
    DRAFT_CONTRACT_VERSION,
    MAX_CLAIM_ROWS,
    MAX_DEBATES,
    MAX_PROMPT_BYTES,
    PURPOSE,
    DebateDraftRefused,
    assemble_debates,
    build_input_table,
    build_prompt,
    build_verifier_prompt,
    change_evidence,
    change_reason_for,
    debate_ref_for,
    draft_debate_map,
    independent,
    parse_draft,
    parse_verdict,
    route_family,
)

SUBJECT = "company:sec-cik:0001467373"
METHOD = {
    "question_admission": ["A question must change a thesis or a driver view.",
                           "A question must be answerable within the quarter."],
    "causal_chain": ["Bookings lead revenue by two to four quarters.",
                     "Utilisation leads margin by one quarter."],
}
DRIVERS = [
    {"driver_ref": "driver:bookings", "label": "Bookings", "mechanism": "leads revenue"},
]
MISSION = {"id": "mission-version:x", "mission_ref": "coverage-mission:x",
           "content_hash": "b" * 64, "created_at": "2026-09-09T00:00:00+00:00",
           "budget": {"max_daily_paid_calls": 10, "max_daily_cost_usd": "5"}}
NOW = "2026-09-09T00:00:00+00:00"


def claim_rows() -> list[dict]:
    return [
        {"claim_version_ref": "cv-1", "subject_ref": SUBJECT,
         "index_aspect": "demand_drivers", "importance": "sell_side",
         "document_title": "TD Cowen: bookings review", "as_of": "2026-08-01",
         "normalized_statement": "Bookings are accelerating."},
        {"claim_version_ref": "cv-2", "subject_ref": SUBJECT,
         "index_aspect": "demand_drivers", "importance": "sell_side",
         "document_title": "Wolfe Research: demand check", "as_of": "2026-08-02",
         "normalized_statement": "Discretionary demand is decelerating."},
        {"claim_version_ref": "cv-3", "subject_ref": SUBJECT,
         "index_aspect": "demand_drivers", "importance": "sell_side",
         "document_title": "HSBC: pipeline note", "as_of": "2026-08-03",
         "normalized_statement": "Pipeline conversion is weak."},
        {"claim_version_ref": "cv-4", "subject_ref": SUBJECT,
         "index_aspect": "demand_drivers", "importance": "sell_side",
         "document_title": "RBC Capital: bookings", "as_of": "2026-08-04",
         "normalized_statement": "Bookings beat again."},
    ]


def table(previous=None, thesis=None):
    return build_input_table(
        subject_ref=SUBJECT, subject_kind="company", claim_rows=claim_rows(),
        driver_rows=DRIVERS, thesis=thesis, method=METHOD, previous=previous,
    )


def draft_reply(**overrides) -> str:
    debate = {
        "debate_ref": "new-1",
        "question": "Will bookings growth hold above ten per cent?",
        "driver_refs": ["driver:bookings"],
        "question_admission_index": 0,
        "causal_chain_index": 0,
        "bull": {"statement": "Bookings accelerating.", "claim_refs": ["C1", "C4"]},
        "bear": {"statement": "Demand decelerating.", "claim_refs": ["C2", "C3"]},
        "market": {"available": True, "lean": "bear",
                   "statement": "Most brokers model deceleration.",
                   "refs": ["C2", "C3"]},
        "ours": {"state": "none_yet", "side": None, "statement": None, "refs": []},
        "gaining": "neither",
        "resolution": None,
    }
    debate.update(overrides)
    return json.dumps({"debates": [debate]})


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
        return {
            "text": self.replies[index], "replayed": False, "cost_micros": 1_000,
            "cost_status": "charged", "work_order_ref": f"wo-{index}",
            "invocation_ref": f"inv-{index}", "result_envelope_ref": f"env-{index}",
            "route_decision_ref": f"route-{index}",
        }

    def family_of(self, route_decision_ref):
        if not isinstance(route_decision_ref, str):
            return None
        index = int(route_decision_ref.rsplit("-", 1)[1])
        return self.families[index] if index < len(self.families) else None


PASS = json.dumps({"verdict": "pass", "findings": []})


class InputTableTests(unittest.TestCase):
    def test_the_purpose_is_registered_from_this_module(self) -> None:
        self.assertEqual(PURPOSE, "debate_map")
        self.assertIn("debate_map", purposes())

    def test_contested_aspects_come_first_and_rows_are_bounded(self) -> None:
        built = table()
        self.assertEqual([row["aspect"] for row in built["claims"]],
                         ["demand_drivers"] * 4)
        self.assertLessEqual(len(built["claims"]), MAX_CLAIM_ROWS)
        self.assertEqual(built["contested"][0]["aspect"], "demand_drivers")
        self.assertEqual(built["citable"]["C1"], "cv-1")

    def test_the_prompt_asks_for_the_market_and_our_position_separately(self) -> None:
        prompt = build_prompt(table())
        self.assertIn("market_position is where consensus stands", prompt)
        self.assertIn("our_position is what OUR THESIS commits us to", prompt)
        self.assertIn("Do not copy the market into it.", prompt)
        self.assertIn("which side has been gaining ground since PREVIOUS", prompt)
        self.assertIn("agree about is worth nothing", prompt)
        # the tier and the publisher are on every claim row, so two notes
        # from one house never look like two sources to the drafter either
        self.assertIn("C1\tdemand_drivers\tsell_side\ttd\t", prompt)
        self.assertIn("C2\tdemand_drivers\tsell_side\twolfe\t", prompt)

    def test_the_prompt_shows_the_numbered_constitution_lists(self) -> None:
        prompt = build_prompt(table())
        self.assertIn("[0] A question must change a thesis or a driver view.", prompt)
        self.assertIn("[0] Bookings lead revenue by two to four quarters.", prompt)

    def test_a_thesis_becomes_the_one_row_our_position_may_cite(self) -> None:
        built = table(thesis={
            "status": "current", "thesis_version_ref": "thesis-version:1",
            "statement": "Bookings hold up", "confidence": "medium",
            "driver_refs": ["driver:bookings"],
        })
        self.assertEqual(built["citable"]["T1"], "thesis-version:1")


class ParseDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = table()

    def test_a_well_formed_draft_resolves_the_row_ids_to_claim_refs(self) -> None:
        parsed = parse_draft(draft_reply(), self.table)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["bull"]["claim_refs"], ["cv-1", "cv-4"])
        self.assertEqual(parsed[0]["market"]["refs"], ["cv-2", "cv-3"])
        self.assertEqual(
            parsed[0]["debate_ref"],
            debate_ref_for(SUBJECT, ["driver:bookings"],
                           "Will bookings growth hold above ten per cent?"),
        )

    def test_a_row_id_that_was_not_shown_refuses_the_whole_draft(self) -> None:
        with self.assertRaises(DebateDraftRefused):
            parse_draft(draft_reply(bull={"statement": "up", "claim_refs": ["C99"]}),
                        self.table)

    def test_a_driver_that_was_not_shown_refuses_the_whole_draft(self) -> None:
        with self.assertRaises(DebateDraftRefused):
            parse_draft(draft_reply(driver_refs=["driver:invented"]), self.table)

    def test_an_extra_key_refuses_the_whole_draft(self) -> None:
        reply = json.loads(draft_reply())
        reply["debates"][0]["confidence"] = "high"
        with self.assertRaises(DebateDraftRefused):
            parse_draft(json.dumps(reply), self.table)

    def test_live_style_top_level_commentary_is_refused_and_contract_is_explicit(self) -> None:
        reply = json.loads(draft_reply())
        reply["evidence_limits"] = ["claims are historical"]
        reply["template_coverage"] = {"covered": True}
        with self.assertRaisesRegex(
            DebateDraftRefused, "evidence_limits.*template_coverage"
        ):
            parse_draft(json.dumps(reply), self.table)
        prompt = build_prompt(self.table)
        self.assertIn(DRAFT_CONTRACT_VERSION, prompt)
        self.assertIn(DRAFT_CONTRACT_HASH, prompt)
        self.assertIn("only top-level key is debates", prompt)

    def test_prose_around_the_object_refuses_the_whole_draft(self) -> None:
        with self.assertRaises(DebateDraftRefused):
            parse_draft("Here is my analysis. There are no debates.", self.table)
        with self.assertRaises(DebateDraftRefused):
            parse_draft("", self.table)

    def test_an_empty_debates_array_is_a_refusal_not_an_empty_map(self) -> None:
        with self.assertRaises(DebateDraftRefused):
            parse_draft(json.dumps({"debates": []}), self.table)

    def test_an_unavailable_market_position_needs_no_refs(self) -> None:
        parsed = parse_draft(draft_reply(market={
            "available": False, "lean": None, "statement": None, "refs": []
        }), self.table)
        self.assertEqual(parsed[0]["market"],
                         {"available": False, "lean": None, "statement": None, "refs": []})

    def test_a_debate_ref_that_is_neither_previous_nor_new_is_refused(self) -> None:
        with self.assertRaises(DebateDraftRefused):
            parse_draft(draft_reply(debate_ref="debate:made-up"), self.table)

    def test_a_previous_debate_ref_is_reused_so_the_chain_stays_one_argument(self) -> None:
        previous = {"debates": [{
            "debate_ref": "debate:known", "question": "Will bookings hold?",
            "status": "open", "driver_refs": ["driver:bookings"],
            "first_seen_at": "2026-08-01T00:00:00+00:00",
        }]}
        parsed = parse_draft(draft_reply(debate_ref="debate:known"), table(previous))
        self.assertEqual(parsed[0]["debate_ref"], "debate:known")

    def test_more_debates_than_the_bound_refuses_the_whole_draft(self) -> None:
        reply = json.loads(draft_reply())
        one = reply["debates"][0]
        reply["debates"] = [
            dict(one, debate_ref=f"new-{index}",
                 question=f"Question number {index}?")
            for index in range(1, MAX_DEBATES + 2)
        ]
        with self.assertRaises(DebateDraftRefused):
            parse_draft(json.dumps(reply), self.table)

    def test_the_same_debate_twice_is_refused(self) -> None:
        reply = json.loads(draft_reply())
        reply["debates"].append(dict(reply["debates"][0], debate_ref="new-2"))
        with self.assertRaises(DebateDraftRefused):
            parse_draft(json.dumps(reply), self.table)


class VerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = table()
        parsed = parse_draft(draft_reply(), self.table)
        from dalton_core.debate_map import screen_candidates

        screened = screen_candidates(
            parsed, method=METHOD, driver_refs=["driver:bookings"],
            claims=self.table["claim_index"], observed_at=NOW,
        )
        self.debates = assemble_debates(
            screened["admitted"], previous=None, created_at=NOW
        )

    def test_the_verifier_prompt_shows_the_rows_each_position_cites(self) -> None:
        prompt = build_verifier_prompt(self.table, self.debates)
        self.assertIn("You are an independent verifier", prompt)
        self.assertIn("[C1 C4]", prompt)
        self.assertIn("market (bear)", prompt)
        self.assertIn("ours: none yet", prompt)

    def test_the_prompt_prints_the_placement_the_drafter_named(self) -> None:
        prompt = build_verifier_prompt(self.table, self.debates)
        self.assertIn("QUESTION ADMISSION rules:", prompt)
        self.assertIn("admission [0] / link [0]", prompt)
        self.assertIn("question_not_on_the_named_causal_link", prompt)

    def test_the_placement_is_carried_into_the_stored_debate(self) -> None:
        self.assertEqual(self.debates[0]["admission_index"], 0)
        self.assertEqual(self.debates[0]["causal_link_index"], 0)

    def test_a_verifier_prompt_over_the_byte_bound_is_refused(self) -> None:
        huge = dict(self.table, causal_chain=["x" * (MAX_PROMPT_BYTES + 1)])
        with self.assertRaises(DebateDraftRefused):
            build_verifier_prompt(huge, self.debates)

    def test_a_pass_verdict_carrying_findings_is_refused(self) -> None:
        with self.assertRaises(DebateDraftRefused):
            parse_verdict(json.dumps({"verdict": "pass", "findings": [
                {"debate_ref": self.debates[0]["debate_ref"],
                 "code": "sides_are_not_actually_opposed", "detail": "no"}]}),
                self.debates)

    def test_a_reject_verdict_must_say_what_is_wrong(self) -> None:
        with self.assertRaises(DebateDraftRefused):
            parse_verdict(json.dumps({"verdict": "reject", "findings": []}), self.debates)

    def test_a_verdict_about_a_debate_not_under_review_is_refused(self) -> None:
        with self.assertRaises(DebateDraftRefused):
            parse_verdict(json.dumps({"verdict": "reject", "findings": [
                {"debate_ref": "debate:elsewhere",
                 "code": "market_position_misstated", "detail": "no"}]}),
                self.debates)

    def test_an_unknown_finding_code_is_refused(self) -> None:
        with self.assertRaises(DebateDraftRefused):
            parse_verdict(json.dumps({"verdict": "reject", "findings": [
                {"debate_ref": self.debates[0]["debate_ref"],
                 "code": "i_do_not_like_it", "detail": "no"}]}),
                self.debates)


class IndependenceTests(unittest.TestCase):
    def test_the_same_family_on_both_sides_is_not_independent(self) -> None:
        ok, reason = independent(
            {"work_order_ref": "wo-0", "model_family": "family-a"},
            {"work_order_ref": "wo-1", "model_family": "family-a"},
        )
        self.assertFalse(ok)
        self.assertIn("family-a", reason)

    def test_an_unknown_family_fails_closed(self) -> None:
        ok, reason = independent(
            {"work_order_ref": "wo-0", "model_family": "family-a"},
            {"work_order_ref": "wo-1", "model_family": None},
        )
        self.assertFalse(ok)
        self.assertIn("could not be established", reason)

    def test_the_same_call_cannot_verify_itself(self) -> None:
        ok, _ = independent(
            {"work_order_ref": "wo-0", "model_family": "family-a"},
            {"work_order_ref": "wo-0", "model_family": "family-b"},
        )
        self.assertFalse(ok)

    def test_two_families_are_independent(self) -> None:
        ok, reason = independent(
            {"work_order_ref": "wo-0", "model_family": "family-a"},
            {"work_order_ref": "wo-1", "model_family": "family-b"},
        )
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_an_absent_router_database_resolves_to_no_family(self) -> None:
        self.assertIsNone(route_family("/nonexistent/router.sqlite", "route-0"))
        self.assertIsNone(route_family("/nonexistent/router.sqlite", None))


class DraftRunTests(unittest.TestCase):
    def run_draft(self, model, previous=None):
        return draft_debate_map(
            table=table(previous), method=METHOD, model=model, mission=MISSION,
            created_at=NOW, previous=previous, family_of=model.family_of,
        )

    def test_a_verified_draft_comes_back_publishable(self) -> None:
        model = FakeModel([draft_reply(), PASS])
        result = self.run_draft(model)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(len(result["debates"]), 1)
        self.assertEqual(result["debates"][0]["status"], "open")
        self.assertEqual(result["debates"][0]["source_independence"],
                         {"bull_sources": 2, "bear_sources": 2})
        self.assertEqual(result["cost_micros"], 2_000)
        self.assertEqual(result["drafted_by"]["model_family"], "family-a")
        self.assertEqual(result["verified_by"]["model_family"], "family-b")
        self.assertEqual([call["purpose"] for call in model.calls],
                         ["debate_map", "debate_map"])

    def test_a_deviant_draft_is_refused_whole_and_never_verified(self) -> None:
        model = FakeModel([draft_reply(driver_refs=["driver:invented"]), PASS])
        result = self.run_draft(model)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(result["debates"], [])

    def test_a_candidate_the_gate_refuses_is_kept_and_nothing_is_published(self) -> None:
        model = FakeModel([draft_reply(causal_chain_index=7), PASS])
        result = self.run_draft(model)
        self.assertEqual(result["status"], "no_admitted_debates")
        self.assertEqual(result["rejected"][0]["reasons"], ["unknown_causal_chain_link"])
        self.assertEqual(len(model.calls), 1)

    def test_a_verifier_on_the_producer_s_family_blocks_publication(self) -> None:
        model = FakeModel([draft_reply(), PASS], families=("family-a", "family-a"))
        result = self.run_draft(model)
        self.assertEqual(result["status"], "not_independent")
        self.assertEqual(result["debates"], [])

    def test_an_unresolvable_verifier_family_fails_closed(self) -> None:
        model = FakeModel([draft_reply(), PASS], families=("family-a",))
        result = self.run_draft(model)
        self.assertEqual(result["status"], "not_independent")
        self.assertIn("could not be established", result["reason"])

    def test_a_rejecting_verifier_blocks_publication(self) -> None:
        reject = json.dumps({"verdict": "reject", "findings": [{
            "debate_ref": debate_ref_for(
                SUBJECT, ["driver:bookings"],
                "Will bookings growth hold above ten per cent?"),
            "code": "sides_are_not_actually_opposed",
            "detail": "Both sides cite the same bookings number.",
        }]})
        result = self.run_draft(FakeModel([draft_reply(), reject]))
        self.assertEqual(result["status"], "verifier_rejected")
        self.assertEqual(result["debates"], [])
        self.assertIn("sides_are_not_actually_opposed", result["reason"])

    def test_the_gate_cannot_tell_a_lazy_placement_but_the_verifier_can(self) -> None:
        # Every debate claims admission rule 0 and causal link 0, which are
        # both valid numbers, so the gate -- which only checks the numbers
        # exist -- admits all three. Whether a question about utilisation
        # really sits on the bookings link is a reading, and the reader is the
        # verifier, which is why the numbers are printed to it.
        reply = json.loads(draft_reply())
        one = reply["debates"][0]
        reply["debates"] = [
            dict(one, debate_ref=f"new-{index}", question=q,
                 question_admission_index=0, causal_chain_index=0)
            for index, q in enumerate(
                ["Will bookings growth hold?", "Will utilisation roll over?",
                 "Will pricing hold?"], start=1)
        ]
        refs = [debate_ref_for(SUBJECT, ["driver:bookings"], question)
                for question in ("Will bookings growth hold?",
                                 "Will utilisation roll over?",
                                 "Will pricing hold?")]
        reject = json.dumps({"verdict": "reject", "findings": [{
            "debate_ref": ref,
            "code": "question_not_on_the_named_causal_link",
            "detail": "Utilisation is link 1; this debate named link 0.",
        } for ref in refs[1:]]})
        model = FakeModel([json.dumps(reply), reject])
        result = self.run_draft(model)
        self.assertEqual(result["rejected"], [])
        self.assertEqual(result["status"], "verifier_rejected")
        self.assertIn("question_not_on_the_named_causal_link", result["reason"])
        self.assertEqual(result["debates"], [])
        # The verifier was shown the placement it is being asked to judge.
        self.assertEqual(model.prompts[1].count("admission [0] / link [0]"), 3)

    def test_an_unparseable_verdict_leaves_the_draft_unverified(self) -> None:
        result = self.run_draft(FakeModel([draft_reply(), "looks fine to me"]))
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["debates"], [])

    def test_a_model_that_is_unavailable_is_not_a_refusal(self) -> None:
        model = FakeModel([draft_reply(), PASS],
                          raises={0: CockpitModelError("no route")})
        result = self.run_draft(model)
        self.assertEqual(result["status"], "model_unavailable")

    def test_a_verifying_call_that_cannot_run_leaves_the_draft_unverified(self) -> None:
        model = FakeModel([draft_reply(), PASS],
                          raises={1: CockpitModelError("no route")})
        result = self.run_draft(model)
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["debates"], [])

    def test_a_continued_debate_keeps_its_first_seen_at(self) -> None:
        previous = {"debates": [{
            "debate_ref": "debate:known",
            "question": "Will bookings hold?", "status": "open",
            "driver_refs": ["driver:bookings"],
            "first_seen_at": "2026-08-01T00:00:00+00:00",
            "bull_position": {"statement": "up", "claim_refs": ["cv-1"]},
            "bear_position": {"statement": "down", "claim_refs": ["cv-2"]},
            "market_position": {"available": False, "lean": None,
                                "statement": None, "refs": []},
            "our_position": {"state": "none_yet", "side": None,
                             "statement": None, "refs": []},
            "last_shift_reason": None,
            "source_independence": {"bull_sources": 1, "bear_sources": 1},
        }]}
        model = FakeModel([draft_reply(debate_ref="debate:known", gaining="bull"), PASS])
        result = self.run_draft(model, previous=previous)
        self.assertEqual(result["status"], "verified")
        debate = result["debates"][0]
        self.assertEqual(debate["debate_ref"], "debate:known")
        self.assertEqual(debate["first_seen_at"], "2026-08-01T00:00:00+00:00")
        self.assertEqual(debate["status"], "shifting")
        self.assertEqual(debate["last_shift_reason"]["refs"], ["cv-1", "cv-4"])

    def test_change_evidence_is_only_what_the_previous_version_did_not_cite(self) -> None:
        model = FakeModel([draft_reply(), PASS])
        debates = self.run_draft(model)["debates"]
        self.assertEqual(change_evidence(debates, None),
                         ["cv-1", "cv-2", "cv-3", "cv-4"])
        self.assertEqual(change_evidence(debates, {"debates": debates}), [])
        self.assertEqual(change_reason_for(None), "evidence_thicker")


if __name__ == "__main__":
    unittest.main()
