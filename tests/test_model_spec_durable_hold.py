"""WP-C2 follow-up: a refusal that outlives the rules it was reached under.

Live evidence, 2026-09-18.

* The legacy company-model-specification lane answers ``held: every pending
  company is durably held`` on every tick.  Its last two runs are
  ``1a4fab24801920d91c8f9839`` (2026-09-17T02:43Z, Accenture) and
  ``a2f25086778f02675411523e`` (02:59Z, Cognizant), both ``spec_status:
  "refused"``, both on a **structure** rule, both with ``repair_attempts``
  already holding one entry and ``repair_policy_hash``
  ``955625a3…`` = ``content_hash({"max_attempts": 1})``.  So the repair did
  fire there; what it could not do was repair *those* rules, because they were
  not on the allow-list.  Since then the list has changed twice -- and the
  refusal is keyed on the company, the disclosure, the task and the repair
  *policy*, none of which move when the rules do, so the new rules can never be
  tried.
* Hyperscaler, 09:24Z, GOOGL: ``financial_statement_structure is invalid:
  company-presented components must be filed and subtotals must be derived；
  缺口…`` with ``repair_attempts: 0`` -- a role/kind pairing the model chose
  from a list it was shown, refused whole.
* Hyperscaler, 09:52Z, META: ``CockpitModelError: this request is already
  running``.

Three defects, three fixes: the contract belongs in the key, a block that
nobody re-examines expires, and an in-flight lease is a reason to wait rather
than a failure to charge for.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from dalton_core.company_model_cli import (
    REPAIR_CONTRACT,
    REPAIR_CONTRACT_HASH,
    REPAIR_CONTRACT_REF,
    REQUEST_IN_FLIGHT_MESSAGE,
    REQUEST_IN_FLIGHT_STATUS,
    _repair_prompt,
    _request_in_flight,
)
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.company_model_spec import (
    REPAIRABLE_STRUCTURE_RULES,
    REPAIRABLE_STRUCTURE_RULES_REF,
    CompanyModelSpecError,
    structure_error_code,
)
from dalton_core.lane_failure_class import (
    DEFAULT_BLOCK_TTL_SECONDS,
    LaneFailureBudget,
    classify,
)
from dalton_core.mission_model_spec_lane import (
    CONTENT_REFUSAL_COOLDOWN_SECONDS,
    business_key,
    retire_superseded_contracts,
)

GOOGL_RULE = "company-presented components must be filed and subtotals must be derived"


class Clock:
    def __init__(self, now=None):
        self.now = now or datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


# ---------------------------------------------------------------------------
# (2) the GOOGL wiring rule
# ---------------------------------------------------------------------------


class GooglStructureRuleTests(unittest.TestCase):
    def test_the_role_and_kind_pairing_is_a_wiring_rule_a_model_can_fix(self):
        # Both halves are enumerated fields the model picked from a list it was
        # shown, and the message names which pairing it broke.  Same family as
        # "a derived line cannot claim a filed concept", repairable since 0.1.
        self.assertIn(GOOGL_RULE, REPAIRABLE_STRUCTURE_RULES)
        self.assertEqual(
            structure_error_code(
                f"financial_statement_structure is invalid: {GOOGL_RULE}；缺口：…"),
            "structure")

    def test_the_gap_suffix_does_not_stop_it_being_recognised(self):
        # The live message carries the ``缺口`` note about a filed line the
        # company does not have.  That note is why the refusal is readable; it
        # must not be why the rule stops matching.
        message = (f"financial_statement_structure is invalid: {GOOGL_RULE}；"
                   "缺口：company:sec-cik:0001652044 的cash报表里找不到「资本开支」")
        self.assertEqual(structure_error_code(message), "structure")

    def test_arithmetic_about_the_filings_is_still_never_repairable(self):
        for message in (
            "structure omits expense concepts selected by the company spec",
            "this formula does not tie to filed history",
        ):
            self.assertEqual(
                structure_error_code(
                    f"financial_statement_structure is invalid: {message}"),
                "semantic", message)

    def test_the_rule_list_and_the_contract_are_versioned_together(self):
        self.assertEqual(REPAIRABLE_STRUCTURE_RULES_REF,
                         "rule:company-model-spec-repairable-structure:0.4")
        self.assertEqual(REPAIR_CONTRACT["eligible_structure_rules_ref"],
                         REPAIRABLE_STRUCTURE_RULES_REF)
        self.assertIn(GOOGL_RULE, REPAIR_CONTRACT["eligible_structure_rules"])

    def test_the_repair_prompt_names_the_rule_and_forbids_inventing_a_line(self):
        prompt = _repair_prompt("{}", CompanyModelSpecError(
            f"financial_statement_structure is invalid: {GOOGL_RULE}",
            code="structure"))
        self.assertIn(GOOGL_RULE, prompt)
        self.assertIn("return the original content unchanged rather than "
                      "inventing a line", prompt)


# ---------------------------------------------------------------------------
# (1) the durable hold
# ---------------------------------------------------------------------------


class BusinessKeyTests(unittest.TestCase):
    def key(self, **overrides):
        args = {"company_ref": "company:sec-cik:0001467373",
                "state_hash": "a" * 64, "task_hash": "b" * 64,
                "repair_policy_hash": "c" * 64, "validation_hash": "d" * 64}
        args.update(overrides)
        return business_key(**args)

    def test_the_repair_contract_is_part_of_what_the_refusal_was_about(self):
        self.assertIn(f"repair-contract:{REPAIR_CONTRACT_HASH}", self.key())
        self.assertNotEqual(self.key(), self.key(repair_contract_hash="e" * 64))

    def test_a_block_from_an_older_contract_is_retired_on_the_next_tick(self):
        budget = LaneFailureBudget("mission_model_spec", clock=Clock())
        stale = self.key(repair_contract_hash="e" * 64)
        budget.record(stale, status="refused",
                      reason="CompanyModelSpecError: sum formula roles do not match")
        budget.record(stale, status="refused", reason="again")
        budget.record(stale, status="refused", reason="and again")
        self.assertIsNotNone(budget.blocked(stale))

        retired = retire_superseded_contracts(budget, self.key())
        self.assertEqual(retired, [stale])
        self.assertIsNone(budget.blocked(stale))

    def test_this_contract_s_own_bookkeeping_is_never_retired(self):
        budget = LaneFailureBudget("mission_model_spec", clock=Clock())
        current = self.key()
        # What the permission control decorates the key with.
        permissioned = f"{current}|permission:v2:abcdef"
        for key in (current, permissioned):
            for _ in range(3):
                budget.record(key, status="refused", reason="refused")
        self.assertEqual(retire_superseded_contracts(budget, current), [])
        self.assertIsNotNone(budget.blocked(current))
        self.assertIsNotNone(budget.blocked(permissioned))

    def test_another_company_is_never_retired_by_accident(self):
        budget = LaneFailureBudget("mission_model_spec", clock=Clock())
        other = self.key(company_ref="company:sec-cik:0001058290",
                         repair_contract_hash="e" * 64)
        for _ in range(3):
            budget.record(other, status="refused", reason="refused")
        self.assertEqual(retire_superseded_contracts(budget, self.key()), [])
        self.assertIsNotNone(budget.blocked(other))


class BlockExpiryTests(unittest.TestCase):
    """The other half: a block nobody re-examines stops being forever."""

    def budget(self, ttl=CONTENT_REFUSAL_COOLDOWN_SECONDS):
        self.clock = Clock()
        return LaneFailureBudget("mission_model_spec", clock=self.clock,
                                 block_ttl_seconds=ttl)

    def test_by_default_nothing_expires_and_every_other_lane_is_unchanged(self):
        self.assertIsNone(DEFAULT_BLOCK_TTL_SECONDS)
        budget = self.budget(ttl=None)
        budget.record("k", status="content_refused", reason="unreadable bytes")
        self.clock.now += timedelta(days=30)
        self.assertEqual(budget.blocked("k").action, "terminal")

    def test_an_exhausted_transient_budget_expires(self):
        # This is the shape the live holds actually had: a structural refusal
        # classifies ``transient``/``unmapped``, three ticks spend the budget,
        # and the count never decays.
        found = classify(
            "CompanyModelSpecError: financial_statement_structure is invalid: "
            "sum formula roles do not match its company statement output",
            status="refused", lane="mission_model_spec")
        self.assertEqual(found.failure_class, "transient")

        budget = self.budget()
        for _ in range(3):
            budget.record("k", status="refused", reason="refused")
        self.assertEqual(budget.blocked("k").action, "held")
        self.clock.now += timedelta(seconds=CONTENT_REFUSAL_COOLDOWN_SECONDS - 1)
        self.assertEqual(budget.blocked("k").action, "held")
        self.clock.now += timedelta(seconds=2)
        self.assertIsNone(budget.blocked("k"))

    def test_a_terminal_verdict_expires(self):
        budget = self.budget()
        budget.record("k", status="content_refused", reason="the verifier refused")
        self.assertEqual(budget.blocked("k").action, "terminal")
        self.clock.now += timedelta(seconds=CONTENT_REFUSAL_COOLDOWN_SECONDS + 1)
        self.assertIsNone(budget.blocked("k"))

    def test_repeating_the_verdict_does_not_restart_the_clock(self):
        # Otherwise a lane that re-asks every tick holds the item for ever,
        # which is the thing the bound exists to stop.
        budget = self.budget()
        budget.record("k", status="content_refused", reason="refused")
        for _ in range(5):
            self.clock.now += timedelta(hours=1)
            budget.record("k", status="content_refused", reason="refused")
            # Still inside the window that started with the *first* verdict.
            self.assertEqual(budget.blocked("k").action, "terminal")
        self.clock.now += timedelta(hours=1, seconds=1)
        self.assertIsNone(budget.blocked("k"))

    def test_expiry_leaves_a_readable_row_rather_than_forgetting_quietly(self):
        rows: list[dict] = []

        class Ledger:
            def append_event(self, **kwargs):
                rows.append(kwargs)
                return kwargs

        budget = LaneFailureBudget("mission_model_spec", clock=Clock(),
                                   ledger=Ledger(),
                                   block_ttl_seconds=CONTENT_REFUSAL_COOLDOWN_SECONDS)
        budget.record("k", status="content_refused", reason="refused")
        budget.clock = Clock(datetime(2026, 9, 19, tzinfo=timezone.utc))
        self.assertIsNone(budget.blocked("k"))
        self.assertEqual([row["event"] for row in rows], ["terminal", "superseded"])
        self.assertIn("has not been re-examined", rows[-1]["reason"])

    def test_a_replayed_verdict_keeps_the_moment_it_was_reached(self):
        # A restart that handed every replayed block a fresh lifetime would
        # turn an expiring hold into a permanent one on a writer that restarts
        # often -- which is exactly the situation being fixed.
        budget = self.budget()
        budget.replay([{
            "lane": "mission_model_spec", "item_key": "k", "event": "terminal",
            "failure_class": "content_refused", "reason": "refused",
            "rule": "content_refused", "dependency": None, "status": "refused",
            "recorded_at": (self.clock.now - timedelta(hours=7)).isoformat(),
        }])
        self.assertIsNone(budget.blocked("k"))

    def test_a_replayed_verdict_inside_the_window_still_blocks(self):
        budget = self.budget()
        budget.replay([{
            "lane": "mission_model_spec", "item_key": "k", "event": "terminal",
            "failure_class": "content_refused", "reason": "refused",
            "rule": "content_refused", "dependency": None, "status": "refused",
            "recorded_at": (self.clock.now - timedelta(hours=1)).isoformat(),
        }])
        self.assertEqual(budget.blocked("k").action, "terminal")

    def test_the_three_lanes_agree_on_the_number(self):
        from dalton_core.mission_deep_insight_lane import (
            CONTENT_REFUSAL_COOLDOWN_SECONDS as gate_seconds,
        )
        from dalton_core.mission_dossier_lane import (
            CONTENT_REFUSAL_COOLDOWN_SECONDS as dossier_seconds,
        )

        self.assertEqual(CONTENT_REFUSAL_COOLDOWN_SECONDS, 6 * 60 * 60)
        self.assertEqual(gate_seconds, CONTENT_REFUSAL_COOLDOWN_SECONDS)
        self.assertEqual(dossier_seconds, CONTENT_REFUSAL_COOLDOWN_SECONDS)


# ---------------------------------------------------------------------------
# (3) the in-flight request
# ---------------------------------------------------------------------------


class RequestInFlightTests(unittest.TestCase):
    def test_the_message_is_recognised_and_named(self):
        self.assertTrue(_request_in_flight(
            CockpitModelError(REQUEST_IN_FLIGHT_MESSAGE)))
        self.assertFalse(_request_in_flight(CockpitModelError("MODEL_UNAVAILABLE")))

    def test_it_no_longer_reads_as_the_model_being_unavailable(self):
        # It did: the child reported ``model_unavailable``, which parked the
        # whole ``model`` dependency of the lane for thirty minutes over a
        # Scheduler lease that had seconds left on it.
        found = classify(f"CockpitModelError: {REQUEST_IN_FLIGHT_MESSAGE}",
                         status=REQUEST_IN_FLIGHT_STATUS, lane="mission_model_spec")
        self.assertEqual(found.rule, "request_in_flight")
        self.assertEqual(found.failure_class, "transient")
        self.assertIsNone(found.dependency)

    def test_the_lane_waits_rather_than_charging_a_failure_budget(self):
        from tests import test_mission_model_spec_lane as _lane

        harness = _lane.ModelSpecLaneTests("test_a_running_child_is_left_alone")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        launched = harness.lane.dispatch_once()
        harness.launcher.finish(launched["ticket_ref"], summary={
            "spec_status": REQUEST_IN_FLIGHT_STATUS,
            "failure_reason": f"CockpitModelError: {REQUEST_IN_FLIGHT_MESSAGE}"})
        after = harness.lane.dispatch_once()
        # Nothing was charged, so the same company is simply asked again -- and
        # the second ask replays the formal result the first one is paying for.
        self.assertNotIn("failure", after["settled"])
        self.assertIn("another process holds the Scheduler lease",
                      after["settled"]["waiting"])
        self.assertEqual(after["status"], "launched")
        self.assertEqual(after["company_ref"], launched["company_ref"])

    def test_a_refusal_in_the_same_position_is_still_charged(self):
        # The waiting path must not become a way for a real refusal to escape
        # the failure budget.
        from tests import test_mission_model_spec_lane as _lane

        harness = _lane.ModelSpecLaneTests("test_a_running_child_is_left_alone")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        launched = harness.lane.dispatch_once()
        harness.launcher.finish(launched["ticket_ref"], summary={
            "spec_status": "refused",
            "failure_reason": "CompanyModelSpecError: structure omits expense concepts"})
        after = harness.lane.dispatch_once()
        self.assertIn("failure", after["settled"])
        self.assertNotIn("waiting", after["settled"])

    def test_a_distinct_request_id_is_the_one_wrong_answer(self):
        # Recorded as a decision, not as behaviour: the request id is content
        # addressed on the disclosure and the repair policy precisely so that
        # the same judgement is bought once.  Minting a fresh id to dodge the
        # lease would buy it twice.
        self.assertEqual(REQUEST_IN_FLIGHT_STATUS, "request_in_flight")
        self.assertIn("0.5", REPAIR_CONTRACT_REF)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
