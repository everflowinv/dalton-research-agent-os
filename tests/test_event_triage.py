"""2026-09-30: a cheap screen in front of the event judge, and price-move repeats.

The screen (``event_triage``, cheap tier) can only send a group to the judge:
it never writes a judgement.  A group it does not flag leaves the backlog as
``screened_out``; a deterministic audit sample goes to the judge anyway; a
failed screen fails open.  Separately, a price move re-recorded by a later
price-series refresh is answered from the earlier NO_CHANGE at no cost.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from dalton_core.budget_pools import PURPOSE_POOLS
from dalton_core.call_budget import default_call_budget
from dalton_core.cockpit_model import purposes
from dalton_core.event_judgement import POOL_LEDGER_PURPOSES
from dalton_core.event_triage import (
    AUDIT_SHARE,
    TRIAGE_PURPOSE,
    TRIAGE_SIGNALS,
    EventTriageValidationError,
    audit_report,
    audit_selected,
    build_triage_prompt,
    gate,
    triage_for,
    validate_triage_output,
)
from dalton_core.model_fallback_chain import TIER_CHEAP, purpose_tiers
from dalton_core.model_selection import PURPOSE_LABELS, PURPOSE_MODEL_CONFIGS
from dalton_core.research_event import record_event
from tests.p14a_fixtures import ACN, AUTOMATION
from tests.test_event_judgement import (
    JUDGE_ROUTE, PASS, REJECT, VERIFIER_ROUTE, FakeModel, decision,
)
from tests.test_event_judgement_ledger_pool import NOW, Harness

TRIAGE_ROUTE = "route-decision:triage"
QUIET = {"escalate": False, "signals": [],
         "reason": "Company boilerplate; nothing a thesis or driver rests on."}
FLAG = {"escalate": True, "signals": ["results_or_guidance"],
        "reason": "New bookings figure below the driver assumption."}


def triage_model(*replies):
    return FakeModel(list(replies), route=TRIAGE_ROUTE, cost_micros=1_500)


class RegistrationTests(unittest.TestCase):
    """Registered like every other purpose: tier, pool, budget, label, config."""

    def test_the_purpose_is_registered_on_the_cheap_tier(self):
        self.assertIn(TRIAGE_PURPOSE, purposes())
        self.assertEqual(purpose_tiers()[TRIAGE_PURPOSE], TIER_CHEAP)

    def test_it_spends_from_and_is_read_into_the_event_response_pool(self):
        self.assertEqual(PURPOSE_POOLS[TRIAGE_PURPOSE], "event_response")
        self.assertIn(TRIAGE_PURPOSE, POOL_LEDGER_PURPOSES)

    def test_it_has_a_packaged_call_budget_label_and_configuration(self):
        budget = default_call_budget(TRIAGE_PURPOSE)
        # The cost ceiling is the shared one every purpose has; what keeps a
        # screen cheap is its tier and its short answer.
        self.assertEqual(budget["max_cost_usd"], 1.0)
        self.assertLessEqual(budget["max_output_tokens"], 1_000)
        self.assertEqual(PURPOSE_MODEL_CONFIGS[TRIAGE_PURPOSE],
                         ("event-judgement-model-config.json",))
        self.assertIn(TRIAGE_PURPOSE, PURPOSE_LABELS)


class ContractTests(unittest.TestCase):
    def test_the_two_honest_answers_are_accepted(self):
        self.assertFalse(validate_triage_output(QUIET)["escalate"])
        self.assertEqual(validate_triage_output(FLAG)["signals"], ["results_or_guidance"])

    def test_anything_outside_the_closed_contract_is_refused(self):
        for bad in (
            None, [], {**QUIET, "decision": "NO_CHANGE"}, {"escalate": False, "signals": []},
            {**QUIET, "escalate": "no"}, {**FLAG, "signals": []},
            {**QUIET, "signals": ["results_or_guidance"]},
            {**FLAG, "signals": ["vibes"]}, {**FLAG, "signals": list(TRIAGE_SIGNALS[:5])},
            {**QUIET, "reason": ""}, {**QUIET, "reason": "x" * 401},
        ):
            with self.subTest(bad=bad), self.assertRaises(EventTriageValidationError):
                validate_triage_output(bad)

    def test_the_gate_can_only_escalate_or_screen_out(self):
        group = [{"id": "research-event:a", "kind": "claim"}]
        failed = {"status": "failed", "reason": "timeout"}
        self.assertEqual(gate(event_group=group, verdict=failed)["route"], "triage_failed")
        self.assertEqual(gate(event_group=group, verdict=failed)["outcome"], "escalated")
        self.assertEqual(gate(event_group=[{"id": "x", "kind": "filing"}],
                              verdict=None)["route"], "bypass")
        flagged = {"status": "screened", **FLAG}
        self.assertEqual(gate(event_group=group, verdict=flagged)["outcome"], "escalated")
        quiet = {"status": "screened", **QUIET}
        self.assertEqual(gate(event_group=group, verdict=quiet, share=Decimal(0)),
                         {"outcome": "screened_out", "route": "triage",
                          "verdict": "no_signal"})
        self.assertEqual(gate(event_group=group, verdict=quiet, share=Decimal(1)),
                         {"outcome": "escalated", "route": "audit", "verdict": "no_signal"})

    def test_the_audit_sample_is_deterministic_and_about_its_share(self):
        refs = [f"research-event:{index:05d}" for index in range(20_000)]
        picked = [ref for ref in refs if audit_selected([ref])]
        self.assertEqual(picked, [ref for ref in refs if audit_selected([ref])])
        self.assertAlmostEqual(len(picked) / len(refs), float(AUDIT_SHARE), delta=0.006)
        # Order within a group does not matter.
        self.assertEqual(audit_selected(["b", "a"]), audit_selected(["a", "b"]))


class TriageRunTests(Harness):
    def run_with(self, triage, *, judge=None, verifier=None, now=NOW):
        return self.judge_run(judge=judge, verifier=verifier, triage_model=triage, now=now)

    def transcript(self, name="alphaengine-doc:1", **kwargs):
        return self.document("transcript", name, **kwargs)

    def test_a_group_the_screen_does_not_flag_leaves_the_backlog_without_a_judgement(self):
        event = self.transcript()
        screen = triage_model(QUIET)
        with patch("dalton_core.event_triage.AUDIT_SHARE", Decimal(0)):
            summary, judge, verifier = self.run_with(screen)
        self.assertEqual(screen.purposes, [TRIAGE_PURPOSE])
        self.assertEqual((judge.prompts, verifier.prompts), ([], []))
        self.assertIsNone(self.judgements.judgement_for(event["id"]))
        row = triage_for(self.store.connection, event["id"])
        self.assertEqual((row["outcome"], row["route"], row["verdict"]),
                         ("screened_out", "triage", "no_signal"))
        self.assertEqual(summary["triage"]["screened_out"], 1)
        self.assertEqual(summary["judgement_status"], "screened_out")
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["cost_micros"], 1_500)
        spend = self.store.connection.execute(
            "SELECT purpose, cost_micros FROM event_response_spend").fetchall()
        self.assertEqual([tuple(row) for row in spend], [(TRIAGE_PURPOSE, 1_500)])
        # Settled: not offered again, and the next run has nothing to do.
        self.assertEqual(self.groups(), [])
        again, judge, _ = self.run_with(triage_model())
        self.assertEqual(again["judgement_status"], "nothing_unjudged")

    def test_a_flagged_group_goes_to_the_judge_exactly_as_before(self):
        event = self.transcript()
        screen = triage_model(FLAG)
        summary, judge, verifier = self.run_with(screen)
        self.assertEqual(len(judge.prompts), 1)
        self.assertEqual(judge.purposes, ["event_judgement"])
        self.assertEqual(len(verifier.prompts), 1)
        self.assertEqual(self.judgements.judgement_for(event["id"])["decision"], "NO_CHANGE")
        row = triage_for(self.store.connection, event["id"])
        self.assertEqual((row["outcome"], row["route"], row["signals"]),
                         ("escalated", "triage", ["results_or_guidance"]))
        self.assertEqual(summary["judged"], 1)
        self.assertEqual(summary["triage"]["escalated"], 1)

    def test_the_screen_never_writes_a_judgement_whatever_it_answers(self):
        event = self.transcript()
        # A screener that answers in the judge's vocabulary is refused whole
        # and the group goes to the judge: the judge's word is the only one.
        screen = triage_model(decision(word="THESIS_BROKEN", action="research"))
        summary, judge, _ = self.run_with(screen)
        self.assertEqual(len(judge.prompts), 1)
        record = self.judgements.judgement_for(event["id"])
        self.assertEqual(record["model"]["route_decision_ref"], JUDGE_ROUTE)
        self.assertEqual(record["decision"], "NO_CHANGE")

    def test_a_failed_screen_fails_open_and_writes_no_decision(self):
        event = self.transcript()
        summary, judge, _ = self.run_with(triage_model("not json at all"))
        self.assertEqual(len(judge.prompts), 1)
        self.assertIsNotNone(self.judgements.judgement_for(event["id"]))
        self.assertIsNone(triage_for(self.store.connection, event["id"]))
        self.assertEqual(summary["triage"]["routes"], {"escalated:triage_failed": 1})
        self.assertEqual(len(summary["triage"]["failures"]), 1)

    def test_the_audit_sample_is_judged_with_the_screen_s_verdict_kept_beside_it(self):
        event = self.transcript()
        with patch("dalton_core.event_triage.AUDIT_SHARE", Decimal(1)):
            summary, judge, _ = self.run_with(triage_model(QUIET), judge=FakeModel([decision(
                action="research", word="THESIS_WEAKENED", citations=[event["id"]],
                research_question="Is the slowdown company specific?")], route=JUDGE_ROUTE))
        self.assertEqual(len(judge.prompts), 1)
        row = triage_for(self.store.connection, event["id"])
        self.assertEqual((row["outcome"], row["route"], row["verdict"]),
                         ("escalated", "audit", "no_signal"))
        report = audit_report(self.store.connection)
        self.assertEqual(report["audited_groups_judged"], 1)
        self.assertEqual(report["audit_misses"], [event["id"]])

    def test_a_refused_judgement_is_retried_without_a_second_screen(self):
        event = self.transcript()
        first = triage_model(FLAG)
        summary, _, _ = self.run_with(
            first, verifier=FakeModel([REJECT], route=VERIFIER_ROUTE))
        self.assertEqual(summary["refused"], 1)
        self.assertIsNone(self.judgements.judgement_for(event["id"]))
        second = triage_model()
        summary, judge, _ = self.run_with(second, now=NOW + timedelta(hours=1))
        self.assertEqual(second.prompts, [])
        self.assertEqual(len(judge.prompts), 1)
        self.assertEqual(summary["triage"]["routes"], {"prior": 1})
        self.assertIsNotNone(self.judgements.judgement_for(event["id"]))

    def test_a_primary_filing_is_never_screened(self):
        event = self.document("filing", "sec:filing:0001467373-26-000001")
        screen = triage_model()
        summary, judge, _ = self.run_with(screen)
        self.assertEqual(screen.prompts, [])
        self.assertEqual(len(judge.prompts), 1)
        self.assertEqual(triage_for(self.store.connection, event["id"])["route"], "bypass")

    def test_a_grouped_batch_is_one_screen_and_one_row_per_member(self):
        notes = [self.document("sales_note", f"sales-note:{index}", hour=9 + index)
                 for index in range(3)]
        screen = triage_model(QUIET)
        with patch("dalton_core.event_triage.AUDIT_SHARE", Decimal(0)):
            summary, judge, _ = self.run_with(screen)
        self.assertEqual(len(screen.prompts), 1)
        for note in notes:
            self.assertIn(note["id"], screen.prompts[0])
            row = triage_for(self.store.connection, note["id"])
            self.assertEqual(row["outcome"], "screened_out")
        costs = self.store.connection.execute(
            "SELECT SUM(cost_micros) FROM event_triage_decisions").fetchone()[0]
        self.assertEqual(costs, 1_500)

    def test_triage_rows_are_append_only(self):
        event = self.transcript()
        with patch("dalton_core.event_triage.AUDIT_SHARE", Decimal(0)):
            self.run_with(triage_model(QUIET))
        for statement in ("UPDATE event_triage_decisions SET outcome='escalated'",
                          "DELETE FROM event_triage_decisions"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                self.store.connection.execute(statement)
        self.assertEqual(triage_for(self.store.connection, event["id"])["outcome"],
                         "screened_out")

    def test_the_mission_lane_does_not_offer_a_screened_out_group(self):
        from dalton_core.mission_event_judgement_lane import (
            newest_unjudged, pending_event_groups)

        event = self.transcript()
        self.assertEqual(newest_unjudged(self.store, self.mission), event["id"])
        with patch("dalton_core.event_triage.AUDIT_SHARE", Decimal(0)):
            self.run_with(triage_model(QUIET))
        self.assertIsNone(newest_unjudged(self.store, self.mission))
        self.assertEqual(pending_event_groups(self.store, self.missions, self.mission), [])

    def test_without_a_screen_the_lane_behaves_as_before(self):
        self.transcript()
        summary, judge, _ = self.judge_run()
        self.assertEqual(summary["triage"]["mode"], "off")
        self.assertEqual(len(judge.prompts), 1)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM event_triage_decisions").fetchone()[0], 0)

    def test_the_prompt_prints_every_input_and_the_closed_contract(self):
        event = self.transcript()
        from dalton_core.event_judgement import build_context

        context = build_context(event=event, mission=self.mission,
                                connection=self.store.connection,
                                judgements=self.judgements)
        prompt = build_triage_prompt(context)
        self.assertIn(event["id"], prompt)
        self.assertIn("alphaengine-doc:1", prompt)
        for signal in TRIAGE_SIGNALS:
            self.assertIn(signal, prompt)
        self.assertNotIn("NO_CHANGE|", prompt)


class PriceMoveRepeatTests(Harness):
    def price_move(self, version, *, as_of="2026-09-08", direction="down",
                   ret="-4.1185"):
        return record_event(
            self.events, company_ref=ACN, kind="price_move",
            occurred_at=f"{as_of}T00:00:00+00:00",
            source_refs=[f"market-price-series-version:{version}"],
            payload={"as_of": as_of, "close": "240", "previous_close": "250",
                     "return_percent": ret, "direction": direction,
                     "basket_return_percent": "-2.5", "excess_vs_basket_percent": "-1.5",
                     "basket_members": 4, "benchmark_ref": None,
                     "benchmark_return_percent": None, "excess_vs_benchmark_percent": None,
                     "trigger": "absolute", "threshold_percent": "3.0",
                     "price_version_ref": f"market-price-series-version:{version}",
                     "invocation_ref": f"connector-invocation:yfinance:{version}"},
            mission=self.mission, actor_ref=AUTOMATION)

    def test_the_same_day_s_move_seen_by_a_later_refresh_is_not_paid_for_again(self):
        first = self.price_move("a")
        self.judge_run()
        again = self.price_move("b")
        self.assertNotEqual(first["id"], again["id"])
        summary, judge, _ = self.judge_run(now=NOW + timedelta(hours=6))
        self.assertEqual(judge.prompts, [])
        self.assertEqual(summary["repeats"], 1)
        self.assertEqual(summary["cost_micros"], 0)
        record = self.judgements.judgement_for(again["id"])
        self.assertEqual(record["decision"], "NO_CHANGE")
        self.assertEqual(record["effect"]["kind"], "repeated_event")
        self.assertEqual(record["effect"]["primary_event_ref"], first["id"])
        self.assertEqual(record["model"]["repeat_of_event_ref"], first["id"])

    def test_another_day_or_direction_is_judged(self):
        self.price_move("a")
        self.judge_run()
        self.price_move("b", as_of="2026-09-09")
        self.price_move("c", direction="up", ret="4.0")
        summary, judge, _ = self.judge_run(now=NOW + timedelta(hours=6))
        self.assertEqual(len(judge.prompts), 2)
        self.assertEqual(summary["repeats"], 0)

    def test_a_move_once_judged_to_matter_is_judged_again(self):
        first = self.price_move("a")
        self.judge_run(judge=FakeModel([decision(
            action="research", word="THESIS_WEAKENED", citations=[first["id"]],
            research_question="Is the fall company specific?")], route=JUDGE_ROUTE))
        self.price_move("b")
        summary, judge, _ = self.judge_run(now=NOW + timedelta(hours=6))
        self.assertEqual(len(judge.prompts), 1)
        self.assertEqual(summary["repeats"], 0)


class ParityTests(unittest.TestCase):
    def environment(self, tiers):
        from dalton_core.workspace_health_parity import Environment

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        state = root / "state"
        state.mkdir()
        router = state / "model-router.sqlite"
        connection = sqlite3.connect(router)
        connection.execute("CREATE TABLE model_routing_policy_versions "
                           "(policy_version_ref TEXT, policy_json TEXT)")
        connection.execute("INSERT INTO model_routing_policy_versions VALUES(?,?)", (
            "policy:ej:1", json.dumps({"fallback_chains": {"tiers": tiers}})))
        connection.commit()
        connection.close()
        (state / "event-judgement-model-config.json").write_text(json.dumps({
            "model_router_db": str(router), "routing_policy_ref": "policy:ej:1"}))
        env = Environment("t", state)
        self.addCleanup(env.close)
        return env

    def test_a_policy_with_a_cheap_chain_routes_the_screen(self):
        from dalton_core.workspace_health_parity import _event_triage_rows

        rows = _event_triage_rows(self.environment({"cheap": ["profile:deepseek-v4-flash"],
                                                    "brain": ["profile:claude-opus-5"]}))
        self.assertEqual(rows[0]["check"], "event_triage.route")
        self.assertEqual(rows[0]["status"], "ok")
        self.assertIn("profile:deepseek-v4-flash", rows[0]["detail"])

    def test_a_policy_without_one_is_a_warning_with_a_fix(self):
        from dalton_core.workspace_health_parity import _event_triage_rows

        rows = _event_triage_rows(self.environment({"brain": ["profile:claude-opus-5"]}))
        self.assertEqual(rows[0]["status"], "warn")
        self.assertIn("fix", rows[0])


if __name__ == "__main__":
    unittest.main()
