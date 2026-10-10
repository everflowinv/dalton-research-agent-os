"""Conviction v2 (ADR-0013): evidence confidence x scenarios x alignment, priced.

The headline case is the one the owner asked for by name -- 同向高回报不再被拒:
a call that leans the same way as the street, with an expected return over
the owner's higher same-direction bar, is refused by v1 before a model is
asked and is admitted by v2.  Around it: the policy object (code default,
owner-published versions, comparison), the v0.2 shape and its derived
numbers, v1 proposals still readable in the same chain, the drafter and
verifier, the lane's precheck and daily cap, and the cockpit card.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from dalton_core.conviction_call import (
    ConvictionCallAuthority,
    ConvictionCallValidationError,
    evaluate_policy_gate,
    evidence_fingerprint,
    expected_return_of,
    normalise_proposal,
    precheck,
    precheck_v2,
    proposal_summary,
    validate_proposal,
    validate_variant_view,
)
from dalton_core.conviction_policy import (
    FIELD_LABELS,
    DEFAULT_POLICY,
    DEFAULT_POLICY_HASH,
    ConvictionPolicyAuthority,
    ConvictionPolicyConflict,
    ConvictionPolicyValidationError,
    compare,
    default_effective,
    effective_policy,
    validate_policy,
)
from dalton_core.research_quality_rubrics import CONVICTION_CALL
from dalton_core.store import DaltonStore
from tests.test_conviction_call import (
    ACN, DEBATE, MISSION, NO_CONSENSUS, THESIS, debate_row, proposal_kwargs, thesis_row,
    variant_view,
)

ROOT = Path(__file__).resolve().parents[1]
TODAY = "2026-10-06"
NOW = "2026-10-06T12:00:00+00:00"
NEXT_WEEK = "2026-10-13T12:00:00+00:00"
VAL = "valuation-snapshot-version:acn-1006"
PRICE = "market-price-series-version:acn-1006"
STREET = "consensus-estimate-version:acn-1006"
OWNER = "human:owner"


def valuation(**overrides) -> dict:
    row = {"id": VAL, "content_hash": "c" * 64, "company_ref": ACN, "as_of": TODAY,
           "price": {"version_ref": PRICE, "close": "193.4"},
           "metrics": [{"metric": "trailing_pe", "status": "available", "value": "18.1"}]}
    row.update(overrides)
    return row


def v2_gate(**overrides) -> dict:
    params = {"company_ref": ACN, "theses": [thesis_row(subject_kind="company")],
              "valuation": valuation(), "latest_price_ref": PRICE,
              "policy_effective": default_effective(), "today": date(2026, 10, 6)}
    params.update(overrides)
    return precheck_v2(**params)


def scenarios(base="30", bull="45", bear="-15") -> list[dict]:
    # 0.5 x 30 + 0.25 x 45 + 0.25 x -15 = 22.5% for a long.
    return [
        {"name": "基准", "probability": "0.5", "payoff": base,
         "trigger": "Q1 bookings above 1.1x revenue", "refs": [VAL], "source_ref": None},
        {"name": "乐观", "probability": "0.25", "payoff": bull,
         "trigger": "AI bookings disclosed above USD 3bn", "refs": [STREET],
         "source_ref": None},
        {"name": "悲观", "probability": "0.25", "payoff": bear,
         "trigger": "consulting revenue down for a third quarter", "refs": [THESIS],
         "source_ref": None},
    ]


def alignment(kind="same_direction", wrong=None) -> dict:
    return {"alignment": kind,
            "market_view": {"available": True, "reason": None,
                            "statement": "the street is also constructive (11 buys, 16 holds)",
                            "refs": [STREET], "sources": ["sell_side_rating"]},
            "where_market_is_wrong": wrong}


def v2_kwargs(**overrides) -> dict:
    params = {
        "company_ref": ACN, "direction": "long", "decision": "THESIS_STRENGTHENED",
        "time_horizon": "6_12_months",
        "our_view": {"statement": "AI-led modernisation offsets weak consulting",
                     "refs": [THESIS]},
        "evidence_confidence": {"level": "medium",
                                "statement": "two quarters of filed bookings support it",
                                "thesis_refs": [THESIS], "claim_refs": ["claim-version:a"]},
        "scenarios": scenarios(), "market_view_alignment": alignment(),
        "consensus_gap": NO_CONSENSUS,
        "valuation": {"snapshot_ref": VAL, "snapshot_hash": "c" * 64, "as_of": TODAY,
                      "price_version_ref": PRICE, "close": "193.4"},
        "invalidators": [{"statement": "book-to-bill under 1.0 for two quarters",
                          "thesis_version_ref": THESIS,
                          "falsifier_ref": "falsifier:bookings"}],
        "thesis_refs": [THESIS], "debate_refs": [],
        "evidence_fingerprint": evidence_fingerprint([THESIS, VAL, "v2"]),
        "change_reason": "evidence_thicker", "change_evidence_refs": [VAL],
        "precheck_record": v2_gate(),
        "rubric": {"rubric_ref": CONVICTION_CALL.rubric_ref,
                   "rubric_hash": CONVICTION_CALL.content_hash, "findings": []},
        "policy_effective": default_effective(), "mission": MISSION,
        "actor_ref": "automation:dalton", "created_at": NOW,
    }
    params.update(overrides)
    return params


def gate_for(position: str, alignment_kind: str, horizon: str = "6_12_months",
             **overrides) -> dict:
    params = {"direction": "long", "time_horizon": horizon, "evidence_level": "medium",
              "scenario_count": 3, "invalidator_count": 1, "alignment": alignment_kind,
              "position_percent": position, "policy": validate_policy(DEFAULT_POLICY)}
    params.update(overrides)
    return evaluate_policy_gate(**params)


# ---------------------------------------------------------------------------
# the policy object
# ---------------------------------------------------------------------------

class PolicyObjectTests(unittest.TestCase):
    def test_the_code_default_is_v2_and_is_the_published_default_file(self):
        policy = validate_policy(DEFAULT_POLICY)
        self.assertEqual(policy["gate"], "v2")
        on_disk = json.loads((ROOT / "deploy/phase9/conviction-policy-v2-default.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(on_disk, policy)
        self.assertEqual(default_effective()["policy_hash"], DEFAULT_POLICY_HASH)

    def test_agreeing_can_never_be_the_cheaper_way_in(self):
        body = json.loads(json.dumps(dict(DEFAULT_POLICY)))
        body["same_direction_min_expected_return_percent"]["6_12_months"] = "5"
        with self.assertRaisesRegex(ConvictionPolicyValidationError, "same-direction"):
            validate_policy(body)

    def test_the_shape_is_closed_and_v1_is_not_publishable(self):
        body = dict(DEFAULT_POLICY)
        with self.assertRaises(ConvictionPolicyValidationError):
            validate_policy({**body, "surprise": 1})
        with self.assertRaises(ConvictionPolicyValidationError):
            validate_policy({**body, "gate": "v1"})
        with self.assertRaises(ConvictionPolicyValidationError):
            validate_policy({**body, "daily_drafting_cost_cap_usd": "Infinity"})
        with self.assertRaises(ConvictionPolicyValidationError):
            validate_policy({**body, "allowed_horizons": ["10_years"]})


class PolicyPublishAndCompareTests(unittest.TestCase):
    def setUp(self):
        # Historical v2 policy/chain unit fixtures; new held payoff admission has independent acceptance tests.
        from unittest.mock import patch
        for target,result in (("dalton_core.scenario_version.validate_conviction_sources",[]),("dalton_core.conviction_call_cli.held_scenarios",[{"ref":"fixture:held"+str(i),"name":str(i),"probability":p,"payoff":v,"trigger":"Filed economic threshold"} for i,(p,v) in enumerate(((".5","30"),(".25","45"),(".25","-15")))])):
            mocked=patch(target,return_value=result);mocked.start();self.addCleanup(mocked.stop)
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)

    def body(self, **changes) -> dict:
        body = json.loads(json.dumps(dict(DEFAULT_POLICY)))
        body.update(changes)
        return body

    def test_a_fresh_core_runs_the_code_default_without_a_table(self):
        found = effective_policy(self.store.connection)
        self.assertEqual(found["source"], "code_default")
        self.assertEqual(found["policy"]["gate"], "v2")
        # Reading did not install anything.
        self.assertIsNone(self.store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='conviction_policy_versions'").fetchone())

    def test_the_owner_publishes_append_only_versions_and_compares_them(self):
        authority = ConvictionPolicyAuthority(self.store)
        first = authority.publish(policy=self.body(min_evidence_confidence="high"),
                                  reason="先收紧证据要求", actor_ref=OWNER,
                                  idempotency_key="k1")
        self.assertEqual(first["status"], "published")
        self.assertEqual(first["version"]["version"], 1)
        self.assertEqual(effective_policy(self.store.connection)["version_ref"],
                         first["version"]["id"])
        # A retry is the same publication.
        again = authority.publish(policy=self.body(min_evidence_confidence="high"),
                                  reason="先收紧证据要求", actor_ref=OWNER,
                                  idempotency_key="k1")
        self.assertEqual(again["status"], "duplicate")
        # A page that loaded before version 1 must reload, not overwrite.
        with self.assertRaises(ConvictionPolicyConflict):
            authority.publish(policy=self.body(max_drafts_per_day=1), reason="省钱",
                              actor_ref=OWNER, prior_version_ref=None)
        second = authority.publish(policy=self.body(max_drafts_per_day=1), reason="省钱",
                                   actor_ref=OWNER,
                                   prior_version_ref=first["version"]["id"])
        self.assertEqual(second["version"]["version"], 2)
        unchanged = authority.publish(policy=self.body(max_drafts_per_day=1),
                                      reason="再点一次", actor_ref=OWNER,
                                      prior_version_ref=second["version"]["id"])
        self.assertEqual(unchanged["status"], "unchanged")
        self.assertEqual([v["version"] for v in authority.versions()], [1, 2])
        rows = {row["field"]: row for row in compare(first["version"]["policy"],
                                                     second["version"]["policy"])}
        self.assertTrue(rows["min_evidence_confidence"]["changed"])
        self.assertTrue(rows["max_drafts_per_day"]["changed"])
        self.assertFalse(rows["thesis_scope"]["changed"])
        self.assertEqual(rows["min_evidence_confidence"]["label"], FIELD_LABELS["min_evidence_confidence"])
        self.assertEqual(rows["min_evidence_confidence"]["left_label"], "高")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute(
                "UPDATE conviction_policy_versions SET reason='x'")

    def test_only_a_person_publishes(self):
        authority = ConvictionPolicyAuthority(self.store)
        with self.assertRaises(ConvictionPolicyValidationError):
            authority.publish(policy=self.body(), reason="x", actor_ref="automation:dalton")
        with self.assertRaises(ConvictionPolicyValidationError):
            authority.publish(policy=self.body(), reason=" ", actor_ref=OWNER)

    def test_the_writer_door_is_human_governance_only(self):
        from dalton_core import writer_server
        from dalton_core.lane_registry import lane_operations

        self.assertIn("publish_conviction_policy", writer_server.HUMAN_GOVERNANCE_OPERATIONS)
        self.assertNotIn("publish_conviction_policy",
                         writer_server.MISSION_AUTOMATION_OPERATIONS)
        self.assertNotIn("publish_conviction_policy", lane_operations())
        self.assertEqual(writer_server.OPERATION_ACTOR_FIELDS["publish_conviction_policy"],
                         "actor_ref")
        self.assertEqual(writer_server.OPERATION_FIELDS["publish_conviction_policy"],
                         frozenset({"policy", "reason", "prior_version_ref",
                                    "idempotency_key", "actor_ref"}))


# ---------------------------------------------------------------------------
# the gate change: 同向高回报不再被拒
# ---------------------------------------------------------------------------

class SameDirectionHighReturnTests(unittest.TestCase):
    def setUp(self):
        # Historical v2 policy/chain unit fixtures; new held payoff admission has independent acceptance tests.
        from unittest.mock import patch
        for target,result in (("dalton_core.scenario_version.validate_conviction_sources",[]),("dalton_core.conviction_call_cli.held_scenarios",[{"ref":"fixture:held"+str(i),"name":str(i),"probability":p,"payoff":v,"trigger":"Filed economic threshold"} for i,(p,v) in enumerate(((".5","30"),(".25","45"),(".25","-15")))])):
            mocked=patch(target,return_value=result);mocked.start();self.addCleanup(mocked.stop)
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.authority = ConvictionCallAuthority(self.store)

    def test_同向高回报不再被拒(self):
        # v1: the street leans bull and so do we.  Refused before any model
        # is asked, however much the call would make ...
        v1 = precheck(company_ref=ACN, theses=[thesis_row()],
                      open_debates=[debate_row(lean="bull", side="bull")],
                      consensus_gap=NO_CONSENSUS, dossier_variant_view=None)
        self.assertFalse(v1["eligible"])
        self.assertIn("we_agree_with_the_market", v1["reasons"])
        # ... and a v1 call cannot even be written without a "where the
        # market is wrong" sentence.
        broken = variant_view()
        broken["where_market_is_wrong"] = None
        with self.assertRaises(ConvictionCallValidationError):
            validate_variant_view(broken)
        # v2: same direction, 22.5% expected over 6-12 months, against the
        # 20% same-direction bar.  Admitted, and proposed.
        self.assertEqual(expected_return_of(scenarios(), "long")["position_percent"], "22.50")
        call = self.authority.propose_v2(**v2_kwargs())
        self.assertEqual(call["status"], "fresh")
        self.assertEqual(call["schema_version"], "0.2")
        self.assertEqual(call["gate"]["status"], "passed")
        self.assertEqual(call["gate"]["threshold_kind"], "same_direction")
        self.assertEqual(call["gate"]["required_percent"], "20")
        self.assertIsNone(call["market_view_alignment"]["where_market_is_wrong"])
        self.assertEqual(self.authority.open_calls()[0]["id"], call["id"])

    def test_agreeing_still_has_to_pay_for_itself(self):
        # 15% clears the general 12% bar but not the 20% same-direction one.
        refused = gate_for("15", "same_direction")
        self.assertEqual(refused["status"], "refused")
        self.assertEqual(refused["reasons"], ["same_direction_return_below_threshold"])
        self.assertEqual(gate_for("15", "opposite_direction")["status"], "passed")
        # A street we cannot read is priced like agreement.
        self.assertEqual(gate_for("15", "market_view_unavailable")["status"], "refused")
        with self.assertRaisesRegex(ConvictionCallValidationError, "policy refuses"):
            self.authority.propose_v2(**v2_kwargs(scenarios=scenarios(base="10")))

    def test_the_other_thresholds_are_the_policys(self):
        self.assertIn("evidence_confidence_below_policy",
                      gate_for("30", "opposite_direction", evidence_level="low")["reasons"])
        self.assertIn("too_few_scenarios",
                      gate_for("30", "opposite_direction", scenario_count=2)["reasons"])
        self.assertIn("direction_not_allowed",
                      gate_for("30", "opposite_direction", direction="avoid")["reasons"])
        policy = validate_policy({**dict(DEFAULT_POLICY), "allowed_horizons": ["1_3_years"]})
        self.assertIn("horizon_not_allowed",
                      gate_for("30", "opposite_direction", policy=policy)["reasons"])
        # A short earns from a fall.
        self.assertEqual(expected_return_of(scenarios(), "short")["position_percent"], "-22.50")


# ---------------------------------------------------------------------------
# the v0.2 shape, and v1 still readable
# ---------------------------------------------------------------------------

class V2ShapeTests(unittest.TestCase):
    def setUp(self):
        # Historical v2 policy/chain unit fixtures; new held payoff admission has independent acceptance tests.
        from unittest.mock import patch
        for target,result in (("dalton_core.scenario_version.validate_conviction_sources",[]),("dalton_core.conviction_call_cli.held_scenarios",[{"ref":"fixture:held"+str(i),"name":str(i),"probability":p,"payoff":v,"trigger":"Filed economic threshold"} for i,(p,v) in enumerate(((".5","30"),(".25","45"),(".25","-15")))])):
            mocked=patch(target,return_value=result);mocked.start();self.addCleanup(mocked.stop)
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.authority = ConvictionCallAuthority(self.store)

    def test_probabilities_must_add_up_and_numbers_must_be_numbers(self):
        rows = scenarios()
        rows[0]["probability"] = "0.6"
        with self.assertRaisesRegex(ConvictionCallValidationError, "add up"):
            self.authority.propose_v2(**v2_kwargs(scenarios=rows))
        for bad in ("Infinity", "NaN", "1e999", "-120"):
            with self.subTest(payoff=bad), self.assertRaises(ConvictionCallValidationError):
                self.authority.propose_v2(**v2_kwargs(scenarios=scenarios(bull=bad)))
        with self.assertRaises(ConvictionCallValidationError):
            self.authority.propose_v2(**v2_kwargs(scenarios=scenarios()[:1]))

    def test_the_expected_return_and_gate_are_derived_not_accepted(self):
        call = self.authority.propose_v2(**v2_kwargs())
        stored = self.authority.proposal(call["id"])
        validate_proposal(stored)
        tampered = {**stored, "expected_return": {**stored["expected_return"],
                                                  "position_percent": "99"}}
        with self.assertRaisesRegex(ConvictionCallValidationError, "derived"):
            normalise_proposal(tampered)
        tampered = {**stored, "gate": {**stored["gate"], "required_percent": "1"}}
        with self.assertRaisesRegex(ConvictionCallValidationError, "derived"):
            normalise_proposal(tampered)

    def test_evidence_must_rest_on_cited_theses_and_alignment_must_match(self):
        with self.assertRaisesRegex(ConvictionCallValidationError, "rests on theses"):
            self.authority.propose_v2(**v2_kwargs(evidence_confidence={
                "level": "high", "statement": "x", "thesis_refs": ["thesis-version:other"],
                "claim_refs": []}))
        unavailable = alignment()
        unavailable["market_view"] = {"available": False, "reason": "no street rows",
                                      "statement": None, "refs": [], "sources": []}
        with self.assertRaises(ConvictionCallValidationError):
            self.authority.propose_v2(**v2_kwargs(market_view_alignment=unavailable))

    def test_v1_and_v2_live_in_one_chain_and_decide_the_same_way(self):
        v1 = self.authority.propose(**proposal_kwargs(created_at="2026-10-01T00:00:00+00:00"))
        self.assertEqual(v1["schema_version"], "0.1")
        v2 = self.authority.propose_v2(**v2_kwargs(created_at=NEXT_WEEK))
        self.assertEqual((v2["version"], v2["prior_version_ref"]), (2, v1["id"]))
        chain = self.authority.versions(ACN)
        self.assertEqual([row["schema_version"] for row in chain], ["0.1", "0.2"])
        for record in chain:
            validate_proposal(record)
        self.authority.decide(proposal_ref=v1["id"], proposal_hash=v1["content_hash"],
                              decision="accept", reason="v1 view", actor_ref=OWNER)
        decided = self.authority.decide(proposal_ref=v2["id"],
                                        proposal_hash=v2["content_hash"],
                                        decision="accept", reason="pays even agreeing",
                                        actor_ref=OWNER)
        self.assertEqual(decided["supersedes_ref"], v1["id"])
        self.assertEqual(self.authority.status_of(v1["id"]), "superseded")
        replay = self.authority.replay(ACN)
        self.assertEqual([row["schema_version"] for row in replay], ["0.1", "0.2"])
        self.assertEqual(replay[1]["confidence"], "medium")
        self.assertEqual([row["id"] for row in self.authority.accepted_calls()], [v2["id"]])
        old, new = proposal_summary(v1), proposal_summary(v2)
        self.assertEqual(old["where_market_is_wrong"], "the lag, not the direction")
        self.assertEqual(new["alignment"], "same_direction")
        self.assertEqual(new["signals"][0], "Q1 bookings above 1.1x revenue")
        row = self.store.connection.execute(
            "SELECT confidence, risk_reward_status FROM conviction_call_proposals "
            "WHERE proposal_id=?", (v2["id"],)).fetchone()
        self.assertEqual((row["confidence"], row["risk_reward_status"]), ("medium", "met"))

    def test_the_v2_precheck_needs_a_thesis_and_a_current_valuation(self):
        self.assertTrue(v2_gate()["eligible"])
        industry_only = v2_gate(theses=[thesis_row(subject_kind="industry")])
        self.assertEqual(industry_only["reasons"], ["no_active_thesis"])
        wide = {**default_effective()}
        wide["policy"] = {**wide["policy"], "thesis_scope": "company_or_industry"}
        self.assertTrue(v2_gate(theses=[thesis_row(subject_kind="industry")],
                                policy_effective=wide)["eligible"])
        self.assertEqual(v2_gate(valuation=None)["reasons"], ["no_valuation_snapshot"])
        self.assertIn("valuation_not_on_latest_price",
                      v2_gate(latest_price_ref="market-price-series-version:newer")["reasons"])
        self.assertIn("valuation_stale", v2_gate(valuation=valuation(as_of="2026-09-01"))[
            "reasons"])
        self.assertIn("valuation_has_no_metric", v2_gate(valuation=valuation(metrics=[]))[
            "reasons"])
        self.assertEqual(v2_gate(valuation=None)["reason_labels"], ["还没有估值快照"])


# ---------------------------------------------------------------------------
# drafting and verifying
# ---------------------------------------------------------------------------

def v2_reply(**overrides) -> str:
    reply = {
        "direction": "long", "decision": "THESIS_STRENGTHENED", "time_horizon": "6_12_months",
        "our_view": {"statement": "AI-led modernisation offsets weak consulting",
                     "refs": ["T1"]},
        "evidence_confidence": {"level": "medium", "statement": "filed bookings support it",
                                "refs": ["T1", "P1"]},
        "market_view": {"available": True, "reason": None,
                        "statement": "the street is constructive too", "refs": ["M1"],
                        "sources": ["sell_side_rating"]},
        "alignment": "same_direction", "where_market_is_wrong": None,
        "scenarios": [
            {"name": "基准", "probability": "0.5", "payoff": "30",
             "trigger": "bookings above 1.1x", "refs": ["P1"], "scenario_row_id": None},
            {"name": "乐观", "probability": "0.25", "payoff": "45",
             "trigger": "AI bookings above USD 3bn", "refs": ["M1"], "scenario_row_id": None},
            {"name": "悲观", "probability": "0.25", "payoff": "-15",
             "trigger": "consulting down again", "refs": ["T1"], "scenario_row_id": None},
        ],
        "invalidators": [{"statement": "book-to-bill under 1.0 twice", "thesis_row_id": "T1",
                          "falsifier_ref": "falsifier:x"}],
    }
    reply.update(overrides)
    return json.dumps(reply)


PASS = json.dumps({"verdict": "pass", "findings": []})


def v2_table(held=()):
    from dalton_core.conviction_call_draft_v2 import build_input_table_v2

    return build_input_table_v2(
        company_ref=ACN, precheck_record=v2_gate(),
        theses=[thesis_row(subject_kind="company", falsifier_refs=["falsifier:x"],
                           claim_refs=["claim-version:t"])],
        consensus_gap=NO_CONSENSUS,
        street={"id": STREET, "as_of": TODAY, "close": "193.4",
                "price_target": {"mean": "223.65", "number_of_analysts": 24},
                "recommendations": {"buy": 9, "hold": 16}},
        valuation=valuation(), held_scenarios=held, policy_effective=default_effective())


class DraftV2Tests(unittest.TestCase):
    def test_a_reply_resolves_to_refs_and_the_numbers_are_derived(self):
        from dalton_core.conviction_call_draft_v2 import assemble_call_v2, parse_draft_v2

        table = v2_table()
        call = assemble_call_v2(parse_draft_v2(v2_reply(), table), table,
                                {"snapshot_ref": VAL, "snapshot_hash": "c" * 64,
                                 "as_of": TODAY, "price_version_ref": PRICE,
                                 "close": "193.4"})
        self.assertEqual(call["evidence_confidence"]["thesis_refs"], [THESIS])
        self.assertEqual(call["evidence_confidence"]["claim_refs"],
                         [VAL, "claim-version:t"])
        self.assertEqual(call["expected_return"]["position_percent"], "22.50")
        self.assertEqual(call["gate"]["status"], "passed")
        self.assertIn("same direction as the street", __import__(
            "dalton_core.conviction_call_draft_v2", fromlist=["x"]).build_prompt_v2(table))

    def test_an_invented_row_refuses_the_whole_draft(self):
        from dalton_core.conviction_call_draft import ConvictionDraftRefused
        from dalton_core.conviction_call_draft_v2 import parse_draft_v2

        with self.assertRaises(ConvictionDraftRefused):
            parse_draft_v2(v2_reply(our_view={"statement": "x", "refs": ["T9"]}), v2_table())

    def test_a_held_structured_scenario_keeps_its_own_numbers(self):
        # The join point with ADR-0012: the drafter names the held row and its
        # probability and payoff are the held object's, not the model's.
        from dalton_core.conviction_call_draft_v2 import parse_draft_v2

        table = v2_table(held=[{"ref": "scenario-version:acn-base:1", "name": "基准",
                                "probability": "0.6", "payoff": "20",
                                "trigger": "bookings above 1.1x"}])
        reply = json.loads(v2_reply())
        reply["scenarios"][0].update({"scenario_row_id": "S1", "probability": "0.5",
                                      "payoff": "99"})
        reply["scenarios"][1]["probability"] = "0.2"
        reply["scenarios"][2]["probability"] = "0.2"
        parsed = parse_draft_v2(json.dumps(reply), table)
        held = parsed["scenarios"][0]
        self.assertEqual((held["probability"], held["payoff"], held["source_ref"]),
                         ("0.6", "20", "scenario-version:acn-base:1"))

    def test_a_policy_refusal_costs_one_call_not_two(self):
        from dalton_core.conviction_call_draft_v2 import draft_conviction_call_v2
        from tests.test_conviction_call_lane import FakeModel

        reply = json.loads(v2_reply())
        reply["scenarios"][0]["payoff"] = "10"  # 0.5x10 + 11.25 - 3.75 = 12.5%
        model = FakeModel([json.dumps(reply), PASS])
        result = draft_conviction_call_v2(
            table=v2_table(), valuation_binding={"snapshot_ref": VAL,
                                                 "snapshot_hash": "c" * 64, "as_of": TODAY,
                                                 "price_version_ref": PRICE,
                                                 "close": "193.4"},
            model=model, mission=MISSION, family_of=lambda ref: None,
            rubric_ref=CONVICTION_CALL.rubric_ref, rubric_hash=CONVICTION_CALL.content_hash)
        self.assertEqual(result["status"], "policy_refused")
        self.assertIn("同向", result["reason"])
        self.assertEqual(len(model.prompts), 1)


# ---------------------------------------------------------------------------
# the lane: precheck, run, daily cap
# ---------------------------------------------------------------------------

from tests.test_conviction_call_lane import (  # noqa: E402
    ConvictionHarness, FakeLauncher, FakeModel, fake_family,
)


class LaneV2Tests(ConvictionHarness):
    def setUp(self):
        # Historical v2 policy/chain unit fixtures; new held payoff admission has independent acceptance tests.
        from unittest.mock import patch
        for target,result in (("dalton_core.scenario_version.validate_conviction_sources",[]),("dalton_core.conviction_call_cli.held_scenarios",[{"ref":"fixture:held"+str(i),"name":str(i),"probability":p,"payoff":v,"trigger":"Filed economic threshold"} for i,(p,v) in enumerate(((".5","30"),(".25","45"),(".25","-15")))])):
            mocked=patch(target,return_value=result);mocked.start();self.addCleanup(mocked.stop)
        super().setUp()
        # The harness pins v1 for the v1 tests; this class is the default.
        gate = patch("dalton_core.conviction_call_cli.active_gate", return_value="v2")
        gate.start()
        self.addCleanup(gate.stop)
        from dalton_core.conviction_call import CHECKPOINT_KIND, WRITE_SCOPE

        self.grant(WRITE_SCOPE, checkpoints=(CHECKPOINT_KIND,))
        today = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).date().isoformat()
        self.snapshot = valuation(as_of=today)
        for target, value in (("latest_valuation", self.snapshot),
                              ("latest_price_ref", PRICE)):
            reader = patch(f"dalton_core.conviction_call_cli.{target}", return_value=value)
            reader.start()
            self.addCleanup(reader.stop)

    def run_model(self, replies):
        config = self.with_model()
        model = FakeModel(replies)
        with patch("dalton_core.conviction_call_cli.CockpitModel", model), \
                patch("dalton_core.conviction_call_cli.route_family", fake_family):
            return self.run_child(dry_run=False, model_config_path=config), model

    def test_an_industry_thesis_alone_is_not_a_company_view_by_default(self):
        self.admit_thesis()
        summary = self.run_child()
        self.assertEqual(summary["gate_version"], "v2")
        self.assertEqual(summary["call_status"], "not_eligible")
        self.assertEqual(summary["gate_reasons"], ["no_active_thesis"])

    def widen_to_industry_theses(self):
        """The owner's published version, read by the child like any other."""

        body = json.loads(json.dumps(dict(DEFAULT_POLICY)))
        body["thesis_scope"] = "company_or_industry"
        ConvictionPolicyAuthority(self.store).publish(
            policy=body, reason="行业论点也算", actor_ref=OWNER)

    def test_a_thesis_and_a_current_valuation_are_enough_to_draft(self):
        self.admit_thesis()
        self.widen_to_industry_theses()
        # We agree with the street on the only debate -- v1 would stop here.
        self.publish_debate(lean="bull", side="bull")
        summary = self.run_child()
        self.assertEqual((summary["eligible"], summary["call_status"]), (True, "dry_run"))
        self.assertEqual(summary["policy_source"], "published")
        reply = json.loads(v2_reply())
        reply["market_view"]["refs"] = ["D1"]
        reply["market_view"]["sources"] = ["debate_market_position"]
        reply["scenarios"][1]["refs"] = ["D1"]
        reply["evidence_confidence"]["refs"] = ["T1"]
        summary, model = self.run_model([json.dumps(reply), PASS])
        self.assertEqual(summary["call_status"], "fresh", summary)
        self.assertTrue(summary["model_called"])
        call = ConvictionCallAuthority(self.store).proposal(summary["proposal_ref"])
        self.assertEqual(call["schema_version"], "0.2")
        self.assertEqual(call["market_view_alignment"]["alignment"], "same_direction")
        self.assertEqual(call["valuation"]["snapshot_ref"], VAL)
        self.assertIn("evidence_confidence_overstated", model.prompts[1])

    def test_the_days_drafting_budget_holds_the_next_draft(self):
        self.admit_thesis()
        self.widen_to_industry_theses()
        spent = self.state_dir / "conviction-call-runs" / "earlier"
        spent.mkdir(parents=True)
        (spent / "summary.json").write_text(json.dumps({
            "created_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat(),
            "cost_micros": 1_200_000, "model_called": True}), encoding="utf-8")
        summary, model = self.run_model([v2_reply(), PASS])
        self.assertEqual((summary["status"], summary["call_status"]), ("held", "daily_cap"))
        self.assertIn("预算已用完", summary["failure_reason"])
        self.assertEqual(model.prompts, [])
        from dalton_core.mission_conviction_lane import MissionConvictionLaneCoordinator

        coordinator = MissionConvictionLaneCoordinator(
            store=self.store, launcher=FakeLauncher(), mission=lambda: self.mission,
            state_dir=self.state_dir)
        held = coordinator.dispatch_once()
        self.assertEqual((held["status"], held["company_ref"]), ("held", ACN))
        free = MissionConvictionLaneCoordinator(
            store=self.store, launcher=FakeLauncher(), mission=lambda: self.mission)
        self.assertEqual(free.dispatch_once()["status"], "launched")


class DailySpendReplayTests(unittest.TestCase):
    """Batch u: a replayed draft recorded cost_micros=0 with model_called=true."""

    def setUp(self):
        # Historical v2 policy/chain unit fixtures; new held payoff admission has independent acceptance tests.
        from unittest.mock import patch
        for target,result in (("dalton_core.scenario_version.validate_conviction_sources",[]),("dalton_core.conviction_call_cli.held_scenarios",[{"ref":"fixture:held"+str(i),"name":str(i),"probability":p,"payoff":v,"trigger":"Filed economic threshold"} for i,(p,v) in enumerate(((".5","30"),(".25","45"),(".25","-15")))])):
            mocked=patch(target,return_value=result);mocked.start();self.addCleanup(mocked.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.day = "2026-10-07"

    def summary(self, name, **body):
        run = self.state / "conviction-call-runs" / name
        run.mkdir(parents=True)
        body.setdefault("created_at", f"{self.day}T10:00:00+00:00")
        (run / "summary.json").write_text(json.dumps(body), encoding="utf-8")

    def spend(self):
        from dalton_core.conviction_call_cli import drafting_spend_today

        return drafting_spend_today(self.state, today=self.day)

    def test_a_replay_counts_the_booked_cost_once(self):
        # 2026-10-07 legacy: work order paid 09:54 outside a lane run
        # (US$0.297660 on the budget ledger); the 10:04 run replayed it.
        self.summary("replay", cost_micros=0, model_called=True, model_calls=[
            {"phase": "draft", "work_order_ref": "work:a", "cost_micros": 0,
             "cost_status": "replayed", "replayed": True,
             "settled_cost_micros": 297_660, "settled_day": self.day}])
        self.assertEqual(self.spend(), {"day": self.day, "cost_micros": 297_660, "drafts": 1})
        # A run that paid for work:b and a later replay of it: counted once.
        self.summary("paid", cost_micros=255_521, model_called=True, model_calls=[
            {"phase": "draft", "work_order_ref": "work:b", "cost_micros": 255_521,
             "cost_status": "actual", "replayed": False}])
        self.summary("again", cost_micros=0, model_called=True, model_calls=[
            {"phase": "draft", "work_order_ref": "work:b", "cost_micros": 0,
             "cost_status": "replayed", "replayed": True,
             "settled_cost_micros": 255_521, "settled_day": self.day}])
        self.assertEqual(self.spend()["cost_micros"], 297_660 + 255_521)
        # Paid yesterday: yesterday's cap counted it.
        self.summary("old", cost_micros=0, model_called=True, model_calls=[
            {"phase": "draft", "work_order_ref": "work:c", "cost_micros": 0,
             "replayed": True, "settled_cost_micros": 400_000, "settled_day": "2026-10-06"}])
        # A summary from before batch u still counts by its own cost.
        self.summary("legacy", cost_micros=100, model_called=True)
        self.assertEqual(self.spend(), {"day": self.day,
                                        "cost_micros": 297_660 + 255_521 + 100, "drafts": 5})

    def test_the_drafter_records_each_answer(self):
        from dalton_core.conviction_call_draft_v2 import model_call_record

        self.assertEqual(
            model_call_record("draft", {"work_order_ref": "work:a", "cost_micros": 0,
                                        "cost_status": "replayed", "replayed": True,
                                        "settled_cost_micros": 9, "settled_day": self.day}),
            {"phase": "draft", "work_order_ref": "work:a", "cost_micros": 0,
             "cost_status": "replayed", "replayed": True,
             "settled_cost_micros": 9, "settled_day": self.day})

    def test_the_model_reads_a_replayed_work_orders_booked_cost(self):
        import sqlite3

        from dalton_core.cockpit_model import CockpitModel

        db = self.state / "budget.sqlite"
        schema = (Path(__file__).resolve().parents[1]
                  / "src/dalton_core/thesis_impact_budget_schema.sql").read_text()
        connection = sqlite3.connect(db)
        self.addCleanup(connection.close)
        with connection:
            connection.executescript(schema)
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "INSERT INTO thesis_impact_day_admissions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("adm-1", "pol", self.day, "work:a", 1, "assessment", "route", 414_080,
                 "{}", "0" * 64, self.day))
            connection.execute(
                "INSERT INTO thesis_impact_day_settlements VALUES(?,?,?,?,?,?,?)",
                ("set-1", "adm-1", 297_660, None, "{}", "0" * 64, self.day))
            connection.execute(
                "INSERT INTO thesis_impact_day_admissions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("adm-2", "pol", self.day, "work:b", 1, "assessment", "route", 414_080,
                 "{}", "0" * 64, self.day))
        model = CockpitModel.__new__(CockpitModel)
        model.config = {"budget_db": str(db)}
        self.assertEqual(model._settled_cost("work:a"), (297_660, self.day))
        # Unsettled: the reservation, as the ledger counts it.
        self.assertEqual(model._settled_cost("work:b"), (414_080, self.day))
        self.assertIsNone(model._settled_cost("work:none"))
        model.config = {"budget_db": str(self.state / "absent.sqlite")}
        self.assertIsNone(model._settled_cost("work:a"))


# ---------------------------------------------------------------------------
# the cockpit
# ---------------------------------------------------------------------------

class CockpitV2Tests(unittest.TestCase):
    def setUp(self):
        # Historical v2 policy/chain unit fixtures; new held payoff admission has independent acceptance tests.
        from unittest.mock import patch
        for target,result in (("dalton_core.scenario_version.validate_conviction_sources",[]),("dalton_core.conviction_call_cli.held_scenarios",[{"ref":"fixture:held"+str(i),"name":str(i),"probability":p,"payoff":v,"trigger":"Filed economic threshold"} for i,(p,v) in enumerate(((".5","30"),(".25","45"),(".25","-15")))])):
            mocked=patch(target,return_value=result);mocked.start();self.addCleanup(mocked.stop)
        from tests.test_cockpit_plane import CockpitHarness

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.c = CockpitHarness(Path(self.temp.name))
        self.addCleanup(self.c.close)
        self.store = self.c.h.h.core
        self.authority = ConvictionCallAuthority(self.store)
        self.mission = self.c.h.missions.active_mission("coverage-mission:us-it-services")

    def test_the_card_shows_the_three_components_and_decides_as_today(self):
        call = self.authority.propose_v2(**v2_kwargs(mission=self.mission))
        item = next(row for row in self.c.plane.approvals()["items"]
                    if row["kind"] == "conviction_call")
        self.assertEqual(item["title"], "是否采纳这条投资建议：做多（与市场同向）")
        self.assertEqual(item["details"]["证据置信度"], "中")
        self.assertEqual(item["details"]["期望回报"],
                         "期望回报 +22.5%，高于同向门槛（更高） +20.0%")
        card = item["conviction_v2"]
        self.assertEqual([row["probability"] for row in card["scenarios"]],
                         ["50%", "25%", "25%"])
        self.assertEqual([row["payoff"] for row in card["scenarios"]],
                         ["+30.0%", "+45.0%", "-15.0%"])
        self.assertEqual(card["alignment"]["label"], "与市场同向")
        self.assertEqual(card["evidence"]["rests_on"], "1 条投资论点、1 条其他依据")
        self.assertEqual([a["decision"] for a in item["actions"]],
                         ["accept", "reject", "defer"])
        calls = []

        def governance(*args, operation, params, actor_ref, **kwargs):
            calls.append((operation, params))
            return self.authority.decide(actor_ref=actor_ref, **params)

        self.c.plane.governance_call = governance
        self.c.plane.decide("owner@example.com", {
            "kind": "conviction_call", "ref": item["ref"], "hash": item["hash"],
            "decision": "accept", "rationale": "同向也值得", "request_id": "r1"})
        self.assertEqual(calls[0][0], "decide_conviction_call")
        self.assertEqual(self.authority.status_of(call["id"]), "accepted")
        self.assertFalse(any(row["kind"] == "conviction_call"
                             for row in self.c.plane.approvals()["items"]))

    def test_the_policy_page_publishes_and_compares(self):
        from dalton_core.cockpit_conviction import policy_view, publish_policy

        view = policy_view(self.c.plane, {})
        self.assertEqual(view["effective"]["source"], "code_default")
        self.assertEqual((view["compare"]["left"], view["compare"]["right"]), ("v1", "default"))
        self.assertTrue(any(row["changed"] for row in view["compare"]["rows"]))
        self.assertIn("同向", view["explainer"])
        published = []

        def governance(*args, operation, params, actor_ref, **kwargs):
            published.append((operation, params, actor_ref))
            return ConvictionPolicyAuthority(self.store).publish(actor_ref=actor_ref, **params)

        self.c.plane.governance_call = governance
        out = publish_policy(self.c.plane, "owner@example.com", {
            "policy": {"max_drafts_per_day": 1, "thesis_scope": "company_or_industry"},
            "reason": "先放开到行业论点，但每天只起草一次", "request_id": "p1",
            "prior_version_ref": None})
        self.assertEqual(out["status"], "published")
        self.assertEqual(published[0][0], "publish_conviction_policy")
        self.assertTrue(published[0][2].startswith("human:"))
        view = policy_view(self.c.plane, {})
        self.assertEqual(view["effective"]["version"], 1)
        rows = {row["field"]: row for row in view["compare"]["rows"]}
        self.assertEqual((view["compare"]["left"], view["compare"]["right"]),
                         ("default", "conviction-policy-version:v2:1"))
        self.assertTrue(rows["max_drafts_per_day"]["changed"])
        self.assertEqual(rows["thesis_scope"]["right_label"], "公司论点或所属行业论点")
        self.assertEqual(len(view["versions"]), 1)


if __name__ == "__main__":
    unittest.main()
