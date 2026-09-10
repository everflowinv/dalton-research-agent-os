"""P15d: the gate, the contract, the risk/reward standards and the decision."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from dalton_core.conviction_call import (
    CONVICTION_POLICY,
    DIRECTIONS,
    GATE_REASONS,
    POLICY_HASH,
    POLICY_REF,
    TIME_HORIZONS,
    ConvictionCallAuthority,
    ConvictionCallConflict,
    ConvictionCallNotFound,
    ConvictionCallValidationError,
    call_ref_for,
    check_risk_reward,
    cited_refs,
    conviction_call_artefact,
    divergent_debates,
    evidence_fingerprint,
    precheck,
    rubric_findings,
    standard_for,
    table_exists,
    validate_consensus_gap,
    validate_event_pathway,
    validate_proposal,
    validate_risk_reward,
    validate_variant_view,
    week_key,
    wide_consensus_gaps,
)
from dalton_core.research_playbook import DECISION_VOCABULARY
from dalton_core.research_quality_rubrics import CONVICTION_CALL, rubric
from dalton_core.store import DaltonStore

ROOT = Path(__file__).resolve().parents[1]
POLICY_FILE = ROOT / "deploy/phase9/p15d-conviction-policy-v1.json"

ACN = "company:sec-cik:0001467373"
THESIS = "thesis-version:1"
DEBATE = "debate:bookings"
CATALYST = "catalyst-entry:acn:earnings:2026-09-25"
MISSION = {"id": "coverage-mission-version:x", "content_hash": "b" * 64}
NOW = "2026-09-09T00:00:00+00:00"


def thesis_row(**overrides) -> dict:
    row = {"ref": THESIS, "thesis_ref": "thesis:acn", "statement": "Bookings hold",
           "confidence": "medium", "falsifier_refs": ["falsifier:bookings"]}
    row.update(overrides)
    return row


def debate_row(*, lean="bear", side="bull", available=True, state="held", status="open") -> dict:
    return {
        "debate_ref": DEBATE, "question": "Will bookings hold?", "status": status,
        "market_position": {
            "available": available, "lean": lean if available else None,
            "statement": "brokers model deceleration" if available else None,
            "refs": ["claim-version:a"] if available else [],
        },
        "our_position": {
            "state": state, "side": side if state == "held" else None,
            "statement": "we model acceleration" if state == "held" else None,
            "refs": [THESIS] if state == "held" else [],
        },
    }


NO_CONSENSUS = {"status": "unavailable", "metrics": [],
                "reason": "this Core has no consensus authority"}


def consensus(percent="18") -> dict:
    return {"status": "available", "reason": None, "metrics": [{
        "metric": "metric:revenue-usd", "period": "FY2027", "ours": "82000",
        "consensus": "69000", "unit": "USDm", "gap_percent": percent,
        "refs": ["forecast-model-version:1"],
    }]}


def gate(**overrides) -> dict:
    params = {"company_ref": ACN, "theses": [thesis_row()],
              "open_debates": [debate_row()], "consensus_gap": NO_CONSENSUS,
              "dossier_variant_view": None}
    params.update(overrides)
    return precheck(**params)


def variant_view(**overrides) -> dict:
    block = {
        "our_view": {"statement": "the booking-to-revenue lag is two to four quarters",
                     "refs": [THESIS]},
        "market_view": {"available": True, "reason": None,
                        "statement": "the street models the two as concurrent",
                        "refs": [DEBATE], "sources": ["debate_market_position"]},
        "where_market_is_wrong": {"statement": "the lag, not the direction", "refs": [DEBATE]},
        "convergence_pathway": {"statement": "the September print shows the lag",
                                "refs": [CATALYST]},
    }
    block.update(overrides)
    return block


def pathway() -> list[dict]:
    return [{"signal": "book-to-bill above 1.1 on the call",
             "window": {"kind": "date", "date": "2026-09-25", "from": None, "to": None},
             "catalyst_ref": CATALYST, "refs": [CATALYST]}]


def risk_reward(up="55", down="20") -> dict:
    return {
        "upside": {"statement": "re-rating to the five-year median", "percent": up,
                   "refs": [DEBATE]},
        "downside": {"statement": "the lag is wrong and growth stays low", "percent": down,
                     "refs": [DEBATE]},
        "standard": None,
    }


def proposal_kwargs(**overrides) -> dict:
    params = {
        "company_ref": ACN, "direction": "long", "decision": "THESIS_STRENGTHENED",
        "confidence": "medium", "time_horizon": "6_12_months",
        "variant_view": variant_view(), "consensus_gap": NO_CONSENSUS,
        "event_pathway": pathway(), "risk_reward": risk_reward(),
        "falsifiers": [{"statement": "book-to-bill under 1.0 for two quarters",
                        "falsifier_ref": "falsifier:bookings",
                        "thesis_version_ref": THESIS}],
        "thesis_refs": [THESIS], "debate_refs": [DEBATE],
        "evidence_fingerprint": evidence_fingerprint([THESIS, DEBATE]),
        "change_reason": "evidence_thicker",
        "change_evidence_refs": [DEBATE],
        "precheck_record": gate(),
        "rubric": {"rubric_ref": CONVICTION_CALL.rubric_ref,
                   "rubric_hash": CONVICTION_CALL.content_hash, "findings": []},
        "mission": MISSION, "actor_ref": "automation:dalton", "created_at": NOW,
    }
    params.update(overrides)
    return params


class PolicyTests(unittest.TestCase):
    def test_the_published_policy_is_the_one_the_code_reads(self):
        published = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
        self.assertEqual(published.pop("content_hash"), POLICY_HASH)
        self.assertEqual(published, dict(CONVICTION_POLICY))
        self.assertEqual(POLICY_REF, "conviction-policy:p15d:v1")

    def test_every_standard_names_the_playbook_sentence_it_reads(self):
        # The table is a reading of free text. A row that did not carry the
        # sentence it came from would be an unfalsifiable claim about the
        # Playbook.
        for row in CONVICTION_POLICY["risk_reward_standards"]:
            with self.subTest(standard=row["standard_ref"]):
                self.assertTrue(row["playbook_text"].strip())
                self.assertTrue(set(row["directions"]) <= set(DIRECTIONS))
                self.assertTrue(set(row["horizons"]) <= set(TIME_HORIZONS))

    def test_a_week_is_the_iso_week(self):
        self.assertEqual(week_key("2026-09-09T00:00:00+00:00"), "2026-W37")
        self.assertEqual(week_key("2026-09-13"), "2026-W37")
        # Monday starts the next one, which is the point of using ISO weeks.
        self.assertEqual(week_key("2026-09-14"), "2026-W38")


class RiskRewardTests(unittest.TestCase):
    def test_a_value_call_needs_fifty_per_cent(self):
        met = check_risk_reward(direction="long", time_horizon="6_12_months",
                                upside_percent="55", downside_percent="20")
        self.assertEqual(met["status"], "met")
        self.assertEqual(met["standard_ref"], "risk-reward:value:6-12m")
        short = check_risk_reward(direction="long", time_horizon="6_12_months",
                                  upside_percent="30", downside_percent="20")
        self.assertEqual(short["status"], "not_met")

    def test_a_compounder_is_measured_against_three_times(self):
        self.assertEqual(
            check_risk_reward(direction="long", time_horizon="3_5_years",
                              upside_percent="240")["status"], "met")
        self.assertEqual(
            check_risk_reward(direction="long", time_horizon="3_5_years",
                              upside_percent="120")["status"], "not_met")

    def test_a_catalyst_trade_is_measured_as_a_ratio(self):
        found = check_risk_reward(direction="long", time_horizon="1_3_years",
                                  upside_percent="60", downside_percent="20")
        self.assertEqual((found["status"], found["observed"]), ("met", "3.00"))
        self.assertEqual(
            check_risk_reward(direction="long", time_horizon="1_3_years",
                              upside_percent="30", downside_percent="20")["status"],
            "not_met")

    def test_a_zero_downside_is_an_unwritten_bear_case_not_infinity(self):
        found = check_risk_reward(direction="long", time_horizon="1_3_years",
                                  upside_percent="60", downside_percent="0")
        self.assertEqual(found["status"], "unavailable")

    def test_a_short_outside_three_to_six_months_fails_the_playbooks_own_rule(self):
        self.assertIsNone(standard_for("short", "6_12_months"))
        found = check_risk_reward(direction="short", time_horizon="6_12_months",
                                  downside_percent="40")
        self.assertEqual(found["status"], "not_met")
        self.assertIn("3–6", found["reason"])

    def test_standing_aside_has_no_return_standard_and_says_so(self):
        found = check_risk_reward(direction="avoid", time_horizon="1_3_years")
        self.assertEqual(found["status"], "not_applicable")

    def test_a_missing_number_is_unavailable_and_never_a_pass(self):
        found = check_risk_reward(direction="long", time_horizon="6_12_months")
        self.assertEqual(found["status"], "unavailable")
        self.assertNotEqual(found["status"], "met")


class PlaybookDriftTests(unittest.TestCase):
    """The standards table is a reading of the Playbook. Pin what it read."""

    def test_every_standard_quotes_a_line_the_playbook_actually_contains(self):
        published = json.loads(
            (ROOT / "deploy/phase9/p9a-research-playbook-v1.json")
            .read_text(encoding="utf-8"))
        lines = set(published["risk_reward_standards"])
        for row in CONVICTION_POLICY["risk_reward_standards"]:
            with self.subTest(standard=row["standard_ref"]):
                # Verbatim, not "contains": a paraphrase drifting away from the
                # sentence it claims to implement is exactly what this catches,
                # and it is silent otherwise -- the reading lives in a JSON file
                # nobody rereads once the numbers look right.
                self.assertIn(row["playbook_text"], lines)

    def test_the_one_standard_with_no_number_is_deliberately_not_in_the_table(self):
        # "Dalton 只提出研究观点和仓位建议，人类团队决定交易" is not a threshold;
        # it is the reason this whole module ends in a human decision.
        published = json.loads(
            (ROOT / "deploy/phase9/p9a-research-playbook-v1.json")
            .read_text(encoding="utf-8"))
        read = {row["playbook_text"] for row in CONVICTION_POLICY["risk_reward_standards"]}
        unread = [line for line in published["risk_reward_standards"] if line not in read]
        self.assertEqual(unread, [
            "Dalton 只提出研究观点和仓位建议，人类团队决定交易"])


class NonFiniteNumberTests(unittest.TestCase):
    """Infinity clears every threshold; NaN raises out of the comparison."""

    def test_the_authority_refuses_an_infinite_or_undefined_percentage(self):
        for value in ("Infinity", "inf", "-Infinity", "NaN", "nan", "1e999",
                      float("inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ConvictionCallValidationError):
                    check_risk_reward(direction="long", time_horizon="6_12_months",
                                      upside_percent=value, downside_percent="20")

    def test_an_infinite_upside_never_reads_as_meeting_the_standard(self):
        with self.assertRaises(ConvictionCallValidationError):
            check_risk_reward(direction="long", time_horizon="3_5_years",
                              upside_percent="Infinity")

    def test_a_ratio_against_an_undefined_downside_is_refused_not_computed(self):
        with self.assertRaises(ConvictionCallValidationError):
            check_risk_reward(direction="long", time_horizon="1_3_years",
                              upside_percent="60", downside_percent="NaN")

    def test_a_stored_call_carrying_one_is_refused_by_the_contract(self):
        for value in ("Infinity", "NaN", "1e999"):
            with self.subTest(value=value):
                with self.assertRaises(ConvictionCallValidationError):
                    validate_risk_reward(risk_reward(up=value), direction="long",
                                         time_horizon="6_12_months")

    def test_a_consensus_gap_of_an_absurd_size_is_refused_too(self):
        with self.assertRaises(ConvictionCallValidationError):
            validate_consensus_gap(consensus("1e999"))
        with self.assertRaises(ConvictionCallValidationError):
            validate_consensus_gap(consensus("NaN"))


class GateTests(unittest.TestCase):
    def test_a_company_with_a_thesis_and_a_disagreement_is_eligible(self):
        found = gate()
        self.assertTrue(found["eligible"])
        self.assertEqual(found["reasons"], [])
        self.assertEqual(found["variant_sources"], ["debate_market_position"])
        self.assertEqual(found["divergent_debates"][0]["debate_ref"], DEBATE)

    def test_no_thesis_means_no_call(self):
        found = gate(theses=[])
        self.assertFalse(found["eligible"])
        self.assertIn("no_active_thesis", found["reasons"])

    def test_agreeing_with_the_market_is_refused_and_named_as_such(self):
        # The owner's rule: 市场看涨你也看涨没有价值. Standing where the
        # street stands is not a call, and the reason says which silence it is.
        found = gate(open_debates=[debate_row(lean="bull", side="bull")])
        self.assertFalse(found["eligible"])
        self.assertIn("we_agree_with_the_market", found["reasons"])

    def test_an_unknown_market_position_is_not_a_disagreement(self):
        found = gate(open_debates=[debate_row(available=False)])
        self.assertFalse(found["eligible"])
        self.assertIn("we_agree_with_the_market", found["reasons"])
        self.assertIn("no_variant_material", found["reasons"])

    def test_no_map_and_no_consensus_is_a_different_silence_from_agreement(self):
        found = gate(open_debates=[])
        self.assertIn("no_debate_map_and_no_consensus", found["reasons"])
        self.assertNotIn("we_agree_with_the_market", found["reasons"])

    def test_a_wide_consensus_gap_is_a_disagreement_on_its_own(self):
        found = gate(open_debates=[], consensus_gap=consensus("18"))
        self.assertTrue(found["eligible"])
        self.assertEqual(len(found["consensus_gaps"]), 1)
        self.assertEqual(found["variant_sources"], ["consensus"])

    def test_a_narrow_gap_is_a_modelling_difference_not_a_view(self):
        found = gate(open_debates=[], consensus_gap=consensus("3"))
        self.assertFalse(found["eligible"])
        self.assertIn("we_agree_with_the_market", found["reasons"])

    def test_a_negative_gap_counts_as_much_as_a_positive_one(self):
        self.assertEqual(len(wide_consensus_gaps(consensus("-22"))), 1)

    def test_no_variant_material_stops_a_company_with_a_gap_it_cannot_source(self):
        found = precheck(company_ref=ACN, theses=[thesis_row()], open_debates=[],
                         consensus_gap={"status": "available", "reason": None,
                                        "metrics": []},
                         dossier_variant_view=None)
        self.assertFalse(found["eligible"])
        self.assertIn("no_variant_material", found["reasons"])

    def test_a_drafted_dossier_variant_view_is_material_on_its_own(self):
        found = gate(open_debates=[debate_row(lean="bull", side="bull")],
                     dossier_variant_view={"status": "drafted",
                                           "market_view_available": True})
        self.assertIn("dossier_variant_view", found["variant_sources"])

    def test_every_reason_is_in_the_closed_vocabulary(self):
        for found in (gate(theses=[]), gate(open_debates=[]),
                      gate(open_debates=[debate_row(lean="bull", side="bull")])):
            for reason in found["reasons"]:
                self.assertIn(reason, GATE_REASONS)

    def test_declining_to_pick_a_side_is_not_a_disagreement(self):
        # P12c's ``neither`` means "we hold a view and it is that neither camp
        # is right". That is a position but not a direction, and a call is a
        # direction. Without this the string comparison would come out unequal
        # against a bear market and the company would be admitted.
        found = gate(open_debates=[debate_row(lean="bear", side="neither")])
        self.assertFalse(found["eligible"])
        self.assertIn("we_agree_with_the_market", found["reasons"])
        self.assertEqual(divergent_debates([debate_row(lean="bear", side="neither")]), [])

    def test_a_split_market_is_something_we_can_differ_from(self):
        # Deliberately kept: holding bull against a genuinely divided street is
        # the ordinary shape of an early call.
        found = gate(open_debates=[debate_row(lean="split", side="bull")])
        self.assertTrue(found["eligible"])
        self.assertEqual(found["divergent_debates"][0]["market_lean"], "split")

    def test_a_debate_that_is_not_live_is_not_a_disagreement_here(self):
        # ``open_debates`` already filters to open and shifting; the predicate
        # only judges the positions, which is why a caller may hand it any row.
        self.assertEqual(divergent_debates([]), [])
        self.assertEqual(divergent_debates([debate_row(state="none_yet")]), [])


class ContractTests(unittest.TestCase):
    def test_a_variant_view_without_a_market_view_is_refused(self):
        with self.assertRaises(ConvictionCallValidationError):
            validate_variant_view(variant_view(market_view={
                "available": False, "reason": "nobody has told us", "statement": None,
                "refs": [], "sources": []}))

    def test_an_unavailable_market_view_may_not_also_state_one(self):
        with self.assertRaises(ConvictionCallValidationError):
            validate_variant_view(variant_view(market_view={
                "available": False, "reason": "nobody has told us",
                "statement": "the street is bearish", "refs": [], "sources": []}))

    def test_a_market_view_must_say_where_it_came_from(self):
        with self.assertRaises(ConvictionCallValidationError):
            validate_variant_view(variant_view(market_view={
                "available": True, "reason": None, "statement": "bearish",
                "refs": [DEBATE], "sources": []}))
        with self.assertRaises(ConvictionCallValidationError):
            validate_variant_view(variant_view(market_view={
                "available": True, "reason": None, "statement": "bearish",
                "refs": [DEBATE], "sources": ["a friend told me"]}))

    def test_an_unavailable_consensus_gap_carries_a_reason_and_no_metrics(self):
        checked = validate_consensus_gap(NO_CONSENSUS)
        self.assertEqual(checked["metrics"], [])
        with self.assertRaises(ConvictionCallValidationError):
            validate_consensus_gap({"status": "unavailable", "reason": "x",
                                    "metrics": consensus()["metrics"]})
        with self.assertRaises(ConvictionCallValidationError):
            validate_consensus_gap({"status": "available", "reason": None, "metrics": []})

    def test_a_pathway_needs_at_least_one_signal_and_every_step_needs_refs(self):
        with self.assertRaises(ConvictionCallValidationError):
            validate_event_pathway([])
        with self.assertRaises(ConvictionCallValidationError):
            validate_event_pathway([{**pathway()[0], "refs": []}])

    def test_an_unknown_window_may_not_smuggle_a_date(self):
        with self.assertRaises(ConvictionCallValidationError):
            validate_event_pathway([{**pathway()[0], "window": {
                "kind": "unknown", "date": "2026-09-25", "from": None, "to": None}}])

    def test_a_range_window_is_checked_end_to_end(self):
        checked = validate_event_pathway([{**pathway()[0], "window": {
            "kind": "range", "date": None, "from": "2026-09-01", "to": "2026-12-31"}}])
        self.assertEqual(checked[0]["window"]["to"], "2026-12-31")
        with self.assertRaises(ConvictionCallValidationError):
            validate_event_pathway([{**pathway()[0], "window": {
                "kind": "range", "date": None, "from": "2026-12-31", "to": "2026-09-01"}}])


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.authority = ConvictionCallAuthority(self.store)

    def test_the_table_is_absent_until_the_authority_opens_it(self):
        other = DaltonStore(":memory:")
        self.addCleanup(other.close)
        self.assertFalse(table_exists(other.connection))
        self.assertTrue(table_exists(self.store.connection))

    def test_a_proposal_reads_back_and_derives_its_own_identity(self):
        written = self.authority.propose(**proposal_kwargs())
        self.assertEqual(written["status"], "fresh")
        self.assertEqual(written["call_ref"], call_ref_for(ACN))
        self.assertEqual(written["week_key"], "2026-W37")
        self.assertEqual(written["checkpoint_kind"], "conviction_call")
        self.assertEqual(written["policy_hash"], POLICY_HASH)
        self.assertIn(written["decision"], DECISION_VOCABULARY)
        read = self.authority.proposal(written["id"])
        self.assertEqual(read["content_hash"], written["content_hash"])

    def test_the_standard_is_recomputed_not_taken_from_the_caller(self):
        # A drafter that asserted it had cleared the bar would be believed by
        # nothing: the block is derived from the frozen policy every time.
        written = self.authority.propose(**proposal_kwargs())
        self.assertEqual(written["risk_reward"]["standard"]["status"], "met")
        with self.assertRaises(ConvictionCallValidationError):
            self.authority.propose(**proposal_kwargs(
                company_ref="company:other",
                evidence_fingerprint="other",
                risk_reward={**risk_reward(up="10"),
                             "standard": written["risk_reward"]["standard"]}))

    def test_the_same_evidence_twice_is_one_call(self):
        first = self.authority.propose(**proposal_kwargs())
        again = self.authority.propose(**proposal_kwargs())
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self.authority.counts()["proposals"], 1)

    def test_a_second_call_in_the_same_week_is_refused(self):
        self.authority.propose(**proposal_kwargs())
        again = self.authority.propose(**proposal_kwargs(evidence_fingerprint="moved"))
        self.assertEqual(again["status"], "rate_limited")
        self.assertIn("2026-W37", again["reason"])
        # The next week is a new allowance.
        later = self.authority.propose(**proposal_kwargs(
            evidence_fingerprint="moved", created_at="2026-09-16T00:00:00+00:00"))
        self.assertEqual(later["status"], "fresh")

    def test_a_falsifier_for_a_thesis_the_call_does_not_cite_is_refused(self):
        with self.assertRaises(ConvictionCallValidationError):
            self.authority.propose(**proposal_kwargs(falsifiers=[{
                "statement": "x", "falsifier_ref": None,
                "thesis_version_ref": "thesis-version:someone-elses"}]))

    def test_a_call_the_gate_did_not_admit_is_refused(self):
        with self.assertRaises(ConvictionCallValidationError):
            self.authority.propose(**proposal_kwargs(
                precheck_record=gate(theses=[])))

    def test_a_call_with_open_rubric_findings_is_never_written(self):
        with self.assertRaises(ConvictionCallValidationError):
            self.authority.propose(**proposal_kwargs(rubric={
                "rubric_ref": CONVICTION_CALL.rubric_ref,
                "rubric_hash": CONVICTION_CALL.content_hash,
                "findings": ["pathway_is_observable"]}))

    def test_a_stored_proposal_is_immutable(self):
        written = self.authority.propose(**proposal_kwargs())
        with self.assertRaises(Exception):
            self.store.connection.execute(
                "UPDATE conviction_call_proposals SET direction='short' WHERE proposal_id=?",
                (written["id"],))
        with self.assertRaises(Exception):
            self.store.connection.execute(
                "DELETE FROM conviction_call_proposals WHERE proposal_id=?",
                (written["id"],))

    def test_a_proposal_cannot_be_written_without_the_store(self):
        import sqlite3

        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.create_function("dalton_authorized", 0, lambda: 0)
        connection.executescript(
            (Path(__file__).resolve().parents[1]
             / "src/dalton_core/conviction_call_schema.sql").read_text(encoding="utf-8"))
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO conviction_call_proposals(proposal_id,call_ref,company_ref,"
                "week_key,direction,decision_word,confidence,time_horizon,"
                "risk_reward_status,evidence_fingerprint,record_json,content_hash,"
                "actor_ref,created_at) VALUES('a','b','c','d','long','NO_CHANGE',"
                "'low','6_12_months','met','f','{}','h','automation:x','t')")

    def test_a_missing_proposal_is_not_found(self):
        with self.assertRaises(ConvictionCallNotFound):
            self.authority.proposal("conviction-call-proposal:nobody")


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.authority = ConvictionCallAuthority(self.store)
        self.call = self.authority.propose(**proposal_kwargs())

    def decide(self, **overrides):
        params = {"proposal_ref": self.call["id"],
                  "proposal_hash": self.call["content_hash"],
                  "decision": "accept", "reason": "the lag argument is the right one",
                  "actor_ref": "human:owner", "created_at": "2026-09-10T00:00:00+00:00"}
        params.update(overrides)
        return self.authority.decide(**params)

    def test_automation_may_propose_and_may_never_decide(self):
        with self.assertRaises(ConvictionCallValidationError):
            self.decide(actor_ref="automation:dalton")
        self.assertEqual(self.authority.status_of(self.call["id"]), "open")

    def test_a_decision_is_bound_to_the_bytes_it_was_made_about(self):
        with self.assertRaises(ConvictionCallConflict):
            self.decide(proposal_hash="f" * 64)

    def test_an_accepted_call_leaves_the_queue_and_enters_the_reader(self):
        self.assertEqual(len(self.authority.open_calls()), 1)
        recorded = self.decide()
        self.assertEqual(recorded["call_status"], "accepted")
        self.assertEqual(self.authority.open_calls(), [])
        accepted = self.authority.accepted_calls()
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["id"], self.call["id"])
        self.assertEqual(accepted[0]["decision_record"]["decision"], "accept")

    def test_the_window_is_over_the_decision_not_the_draft(self):
        # A call drafted in one week and accepted in the next belongs to the
        # week it was accepted in: that is the week the owner decided anything.
        self.decide(created_at="2026-09-16T00:00:00+00:00")
        self.assertEqual(
            self.authority.accepted_calls(("2026-09-01", "2026-09-14")), [])
        self.assertEqual(
            len(self.authority.accepted_calls(("2026-09-14", "2026-09-21"))), 1)

    def test_a_rejected_call_is_out_of_the_queue_and_out_of_the_brief(self):
        self.decide(decision="reject", reason="the lag is already in the price")
        self.assertEqual(self.authority.status_of(self.call["id"]), "rejected")
        self.assertEqual(self.authority.accepted_calls(), [])
        self.assertEqual(self.authority.open_calls(), [])

    def test_a_deferred_call_waits_and_can_still_be_answered(self):
        self.decide(decision="defer", reason="after the print")
        self.assertEqual(self.authority.status_of(self.call["id"]), "deferred")
        # Not re-raised in the approvals queue, and not silently dropped.
        self.assertEqual(self.authority.open_calls(), [])
        self.assertEqual(len(self.authority.deferred_calls()), 1)
        second = self.decide(created_at="2026-09-26T00:00:00+00:00")
        self.assertEqual(second["decision_number"], 2)
        self.assertEqual(self.authority.status_of(self.call["id"]), "accepted")
        chain = self.authority.decisions(self.call["id"])
        self.assertEqual([item["decision"] for item in chain], ["defer", "accept"])

    def test_a_settled_call_cannot_be_decided_twice(self):
        self.decide()
        with self.assertRaises(ConvictionCallConflict):
            self.decide(decision="reject", reason="changed my mind")

    def test_every_decision_says_why(self):
        with self.assertRaises(ConvictionCallValidationError):
            self.decide(reason="   ")

    def test_decisions_are_append_only(self):
        self.decide(decision="defer", reason="after the print")
        with self.assertRaises(Exception):
            self.store.connection.execute(
                "UPDATE conviction_call_decisions SET decision='accept'")


class VersionChainTests(unittest.TestCase):
    """ADR-0008 on a call: no terminal version, and the mind change is legible."""

    def setUp(self):
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.authority = ConvictionCallAuthority(self.store)
        self.first = self.authority.propose(**proposal_kwargs())

    def second(self, **overrides):
        params = {"evidence_fingerprint": "a-later-fingerprint",
                  "created_at": "2026-09-16T00:00:00+00:00",
                  "change_evidence_refs": [CATALYST]}
        params.update(overrides)
        return self.authority.propose(**proposal_kwargs(**params))

    def test_a_company_s_calls_are_one_chain(self):
        self.assertEqual((self.first["version"], self.first["prior_version_ref"]),
                         (1, None))
        later = self.second()
        self.assertEqual(later["version"], 2)
        self.assertEqual(later["prior_version_ref"], self.first["id"])
        self.assertEqual([item["version"] for item in self.authority.versions(ACN)],
                         [1, 2])
        self.assertEqual(self.authority.current(ACN)["id"], later["id"])
        self.assertIsNone(self.authority.current("company:nobody"))

    def test_a_version_names_why_it_exists_and_what_it_learned(self):
        self.assertEqual(self.first["change_reason"], "evidence_thicker")
        self.assertEqual(self.first["change_evidence_refs"], [DEBATE])
        with self.assertRaises(ConvictionCallValidationError):
            self.second(change_reason="because_i_said_so")
        # The refs have to be refs the call actually stands on.
        with self.assertRaises(ConvictionCallValidationError):
            self.second(change_evidence_refs=["claim-version:some-other-document"])
        with self.assertRaises(ConvictionCallValidationError):
            self.second(change_evidence_refs=[])

    def test_accepting_a_newer_call_supersedes_the_one_that_was_standing(self):
        self.authority.decide(
            proposal_ref=self.first["id"], proposal_hash=self.first["content_hash"],
            decision="accept", reason="the lag argument holds", actor_ref="human:owner",
            created_at="2026-09-10T00:00:00+00:00")
        self.assertEqual(len(self.authority.accepted_calls()), 1)
        later = self.second()
        recorded = self.authority.decide(
            proposal_ref=later["id"], proposal_hash=later["content_hash"],
            decision="accept", reason="the print moved the horizon",
            actor_ref="human:owner", created_at="2026-09-17T00:00:00+00:00")
        # The mark lives on the act that caused it; nothing was rewritten.
        self.assertEqual(recorded["supersedes_ref"], self.first["id"])
        self.assertEqual(self.authority.superseded_by(self.first["id"]), later["id"])
        self.assertIsNone(self.authority.superseded_by(later["id"]))
        self.assertEqual(self.authority.status_of(self.first["id"]), "superseded")
        # The brief sees one view of one company, not two.
        accepted = self.authority.accepted_calls()
        self.assertEqual([item["id"] for item in accepted], [later["id"]])

    def test_a_superseded_call_is_still_in_the_chain_and_still_readable(self):
        self.authority.decide(
            proposal_ref=self.first["id"], proposal_hash=self.first["content_hash"],
            decision="accept", reason="held", actor_ref="human:owner",
            created_at="2026-09-10T00:00:00+00:00")
        later = self.second()
        self.authority.decide(
            proposal_ref=later["id"], proposal_hash=later["content_hash"],
            decision="accept", reason="replaced", actor_ref="human:owner",
            created_at="2026-09-17T00:00:00+00:00")
        replay = self.authority.replay(ACN)
        self.assertEqual([row["version"] for row in replay], [1, 2])
        self.assertEqual([row["status"] for row in replay], ["superseded", "accepted"])
        self.assertEqual(replay[0]["superseded_by"], later["id"])
        self.assertEqual(replay[0]["decision_reason"], "held")
        # And the record itself is untouched: same bytes as when it was written.
        self.assertEqual(self.authority.proposal(self.first["id"])["content_hash"],
                         self.first["content_hash"])

    def test_a_rejection_supersedes_nothing(self):
        self.authority.decide(
            proposal_ref=self.first["id"], proposal_hash=self.first["content_hash"],
            decision="accept", reason="held", actor_ref="human:owner",
            created_at="2026-09-10T00:00:00+00:00")
        later = self.second()
        recorded = self.authority.decide(
            proposal_ref=later["id"], proposal_hash=later["content_hash"],
            decision="reject", reason="not convinced", actor_ref="human:owner",
            created_at="2026-09-17T00:00:00+00:00")
        self.assertIsNone(recorded["supersedes_ref"])
        self.assertEqual([item["id"] for item in self.authority.accepted_calls()],
                         [self.first["id"]])

    def test_the_weekly_cap_is_in_the_schema_as_well_as_the_authority(self):
        # The authority refuses first, with a reason a lane can report. This is
        # the constraint that holds when two processes race past that read.
        self.assertEqual(CONVICTION_POLICY["max_calls_per_company_per_week"], 1)
        indexes = self.store.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND "
            "name='conviction_call_proposals'").fetchone()[0]
        self.assertIn("UNIQUE(company_ref, week_key)", indexes)
        self.assertIn("UNIQUE(call_ref, version_number)", indexes)


class DecisionIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.authority = ConvictionCallAuthority(self.store)
        self.call = self.authority.propose(**proposal_kwargs())

    def defer(self, **overrides):
        params = {"proposal_ref": self.call["id"],
                  "proposal_hash": self.call["content_hash"],
                  "decision": "defer", "reason": "after the print",
                  "actor_ref": "human:owner", "idempotency_key": "cockpit:req-1",
                  "created_at": "2026-09-10T00:00:00+00:00"}
        params.update(overrides)
        return self.authority.decide(**params)

    def test_a_retried_deferral_is_the_same_deferral(self):
        first = self.defer()
        self.assertEqual(first["status"], "recorded")
        again = self.defer(created_at="2026-09-10T00:05:00+00:00")
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(self.authority.decisions(self.call["id"])), 1)

    def test_a_different_key_is_a_different_decision(self):
        self.defer()
        self.defer(idempotency_key="cockpit:req-2",
                   created_at="2026-09-11T00:00:00+00:00")
        self.assertEqual([item["decision"] for item
                          in self.authority.decisions(self.call["id"])],
                         ["defer", "defer"])

    def test_a_key_never_smuggles_a_second_decision_past_a_settled_call(self):
        self.defer(decision="accept", reason="taking it")
        with self.assertRaises(ConvictionCallConflict):
            self.defer(idempotency_key="cockpit:req-9", decision="reject",
                       reason="changed my mind")


class RubricTests(unittest.TestCase):
    def test_the_rubric_is_registered_and_its_ids_match_the_mechanical_half(self):
        self.assertIs(rubric("conviction_call"), CONVICTION_CALL)
        mechanical = {
            "variant_view_is_variant", "pathway_is_observable", "consensus_gap_named",
            "risk_reward_against_the_standard", "falsifiers_bound_to_a_thesis",
            "horizon_matches_the_direction",
        }
        self.assertTrue(mechanical <= set(CONVICTION_CALL.criterion_ids))
        # It names no shared check: its deterministic half is per-record and
        # lives in conviction_call.rubric_findings.
        self.assertEqual(CONVICTION_CALL.deterministic_checks, ())

    def test_a_well_formed_call_has_no_mechanical_findings(self):
        record = {
            "direction": "long", "time_horizon": "6_12_months",
            "variant_view": variant_view(), "consensus_gap": NO_CONSENSUS,
            "event_pathway": pathway(),
            "risk_reward": {**risk_reward(),
                            "standard": check_risk_reward(
                                direction="long", time_horizon="6_12_months",
                                upside_percent="55", downside_percent="20")},
            "falsifiers": [{"statement": "x", "falsifier_ref": None,
                            "thesis_version_ref": THESIS}],
            "thesis_refs": [THESIS],
        }
        self.assertEqual(rubric_findings(record), [])
        # An undated step is not a mechanical failure: the calendar may
        # genuinely not know. A step with nothing behind it is.
        self.assertEqual(
            rubric_findings({**record, "event_pathway": [
                {**pathway()[0],
                 "window": {"kind": "unknown", "date": None, "from": None,
                            "to": None}}]}),
            [])
        self.assertEqual(
            rubric_findings({**record, "event_pathway": []}),
            ["pathway_is_observable"])
        self.assertEqual(
            rubric_findings({**record, "direction": "short"}),
            ["horizon_matches_the_direction"])
        self.assertEqual(
            rubric_findings({**record, "falsifiers": [
                {"statement": "x", "falsifier_ref": None,
                 "thesis_version_ref": "thesis-version:other"}]}),
            ["falsifiers_bound_to_a_thesis"])


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.authority = ConvictionCallAuthority(self.store)
        self.call = self.authority.propose(**proposal_kwargs())

    def test_the_calls_of_a_week_are_readable_by_company(self):
        self.assertEqual(len(self.authority.calls_this_week(ACN, NOW)), 1)
        self.assertEqual(
            self.authority.calls_this_week(ACN, "2026-09-16T00:00:00+00:00"), [])

    def test_every_ref_a_call_stands_on_is_reachable(self):
        refs = cited_refs(self.call)
        self.assertIn(THESIS, refs)
        self.assertIn(DEBATE, refs)
        self.assertIn(CATALYST, refs)

    def test_a_call_renders_into_the_artefact_the_quality_layer_grades(self):
        art = conviction_call_artefact(self.call)
        self.assertEqual(art["artefact_kind"], "conviction_call")
        self.assertEqual(art["ref"], self.call["id"])
        titles = [section["title"] for section in art["sections"]]
        self.assertIn("市场错在哪", titles)
        self.assertIn("可观察信号", titles)
        # The consensus section carries the honest gap rather than nothing.
        gap_section = next(s for s in art["sections"] if s["title"] == "预期差")
        self.assertEqual(gap_section["body"], "")
        self.assertTrue(gap_section["gaps"][0])

    def test_a_record_that_lost_its_hash_is_a_conflict_not_a_read(self):
        stored = self.authority.proposal(self.call["id"])
        self.assertEqual(validate_proposal(stored), stored)
        with self.assertRaises(ConvictionCallConflict):
            validate_proposal({**stored, "content_hash": "0" * 64})


if __name__ == "__main__":
    unittest.main()
