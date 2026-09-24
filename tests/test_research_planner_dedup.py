"""WP-C1-5: one state, one decision; and a projection that keeps degrading.

Live evidence, from
``~/Library/Application Support/Dalton/state/dalton-core/research-plans/``
(2026-09-14 .. 09-16, 432 summaries):

* 401 rounds over **8 distinct states**, one of them decided 317 times;
  ``plan_status`` was ``model_unavailable`` on 333 of 432 and ``fresh`` on 53.
* 36 runs refused before routing with ``ResearchPlanInputTooLarge`` --
  ``d7a3c523cb405857172cf52f`` (09-16T08:52Z, 85,495 bytes, over by 5,495),
  ``c65ef9e851a9f2e77d90c4d0`` (08:38Z, over by 5,158),
  ``f6856542edb082e6cd4523b3`` (08:32Z, over by 4,936),
  ``d2909702e19fd71d468d784e`` (08:27Z, over by 4,575) -- an overshoot growing
  monotonically with the document inventory, while stage five still had four
  tighter retentions in it.
* every one of those rounds that reached the model minted a WorkOrder whose
  question carries the whole projected state inline
  (``research_planner.py`` ``RESEARCH_STATE={canonical_json(state)}``).
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from dalton_core.research_planner import (
    PROMPT_PROJECTION_REF,
    READABLE_KEEP_LADDER,
    READABLE_KEEP_PER_COMPANY,
    ResearchPlanInputTooLarge,
    build_prompt,
    document_reading_priorities,
    gap_filling_inquiries,
    project_state_for_prompt,
)
from dalton_core.research_planner_cli import (
    MAX_WORK_ORDER_PROMPT_BYTES,
    PLAN_ATTEMPT_LEDGER,
    TERMINAL_PLAN_STATUSES,
    TRANSIENT_RETRY_SECONDS,
    attempt_hold,
    read_attempt_ledger,
    record_attempt,
)
# Imported as modules, not as names: unittest collects every TestCase in a
# test module's namespace, and importing the classes directly would re-run
# both suites under a second name.
from tests import test_research_planner as _planner_fixtures
from tests import test_research_planner_cli as _cli_fixtures


NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


class AttemptLedgerTests(unittest.TestCase):
    def test_an_unseen_state_is_never_held(self):
        self.assertIsNone(attempt_hold({}, "abc", now=NOW))

    def test_a_refusal_of_this_exact_state_holds_for_good(self):
        # The state has not moved, so the refusal will not move either.
        ledger = {"abc": {"at": (NOW - timedelta(days=3)).isoformat(),
                          "status": "succeeded", "plan_status": "refused",
                          "attempts": 4, "reason": "directive 0 gives no reason"}}
        held = attempt_hold(ledger, "abc", now=NOW)
        self.assertEqual(held["plan_status"], "held")
        self.assertEqual(held["held_reason"], "terminal_for_this_state")
        self.assertEqual(held["held_attempts"], 4)
        self.assertIn("研究状态没有变化", held["failure_reason"])
        self.assertIn("directive 0 gives no reason", held["failure_reason"])

    def test_a_prompt_that_does_not_fit_is_terminal_for_this_state(self):
        self.assertIn("input_too_large", TERMINAL_PLAN_STATUSES)
        ledger = {"abc": {"at": (NOW - timedelta(days=1)).isoformat(),
                          "status": "succeeded", "plan_status": "input_too_large",
                          "attempts": 35, "reason": "exceeds the bound by 5495",
                          "projection_rule_ref": PROMPT_PROJECTION_REF}}
        self.assertEqual(attempt_hold(ledger, "abc", now=NOW)["held_reason"],
                         "terminal_for_this_state")

    def test_a_prompt_refused_under_an_older_projection_rule_is_asked_again(self):
        # 2026-09-24: a state refused as input_too_large under rule 0.3 stayed
        # held after rule 0.4 could fit it, for as long as the state stood.
        for recorded in (None, "rule:research-plan-input-projection:0.3"):
            with self.subTest(recorded=recorded):
                record = {"at": (NOW - timedelta(days=1)).isoformat(),
                          "status": "succeeded", "plan_status": "input_too_large",
                          "attempts": 74, "reason": "exceeds the bound by 18620"}
                if recorded is not None:
                    record["projection_rule_ref"] = recorded
                self.assertIsNone(attempt_hold({"abc": record}, "abc", now=NOW))

    def test_a_dead_route_is_held_for_a_while_and_then_asked_again(self):
        # An outage is about this moment, not this state, so it is worth
        # asking again -- but not every five minutes, because every ask mints
        # a WorkOrder.
        recent = {"abc": {"at": (NOW - timedelta(seconds=300)).isoformat(),
                          "status": "succeeded", "plan_status": "model_unavailable",
                          "attempts": 12, "reason": "no model route is available"}}
        held = attempt_hold(recent, "abc", now=NOW)
        self.assertEqual(held["held_reason"], "transient_backoff")
        self.assertIn("300 秒前", held["failure_reason"])
        stale = {"abc": {**recent["abc"],
                         "at": (NOW - timedelta(seconds=TRANSIENT_RETRY_SECONDS + 1)
                                ).isoformat()}}
        self.assertIsNone(attempt_hold(stale, "abc", now=NOW))

    def test_a_ledger_it_cannot_read_never_stops_the_planner(self):
        self.assertIsNone(attempt_hold({"abc": "not an object"}, "abc", now=NOW))
        self.assertIsNone(attempt_hold({"abc": {"attempts": 1}}, "abc", now=NOW))
        self.assertIsNone(attempt_hold(
            {"abc": {"at": "not a timestamp", "plan_status": "busy"}}, "abc", now=NOW))


class LedgerFileTests(unittest.TestCase):
    # The child harness, without re-running its own suite under a second name.
    setUp = _cli_fixtures.PlannerChildTests.setUp
    close = _cli_fixtures.PlannerChildTests.close
    publish_mission = _cli_fixtures.PlannerChildTests.publish_mission
    plan = _cli_fixtures.PlannerChildTests.plan

    def test_an_absent_or_broken_ledger_reads_as_no_memory(self):
        self.assertEqual(read_attempt_ledger(self.state), {})
        (self.state / PLAN_ATTEMPT_LEDGER).write_text("[]", encoding="utf-8")
        self.assertEqual(read_attempt_ledger(self.state), {})
        (self.state / PLAN_ATTEMPT_LEDGER).write_text("{", encoding="utf-8")
        self.assertEqual(read_attempt_ledger(self.state), {})

    def test_an_attempt_is_remembered_owner_only_and_counted(self):
        record_attempt(self.state, "abc", {
            "created_at": NOW.isoformat(), "status": "succeeded",
            "plan_status": "refused", "failure_reason": "bad shape"})
        path = self.state / PLAN_ATTEMPT_LEDGER
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(read_attempt_ledger(self.state)["abc"]["attempts"], 1)
        record_attempt(self.state, "abc", {
            "created_at": NOW.isoformat(), "status": "succeeded",
            "plan_status": "refused", "failure_reason": "bad shape"})
        self.assertEqual(read_attempt_ledger(self.state)["abc"]["attempts"], 2)

    def test_the_ledger_stays_bounded(self):
        from dalton_core.research_planner_cli import MAX_LEDGER_ENTRIES

        for index in range(MAX_LEDGER_ENTRIES + 5):
            record_attempt(self.state, f"state-{index:03d}", {
                "created_at": (NOW + timedelta(seconds=index)).isoformat(),
                "status": "succeeded", "plan_status": "model_unavailable",
                "failure_reason": None})
        self.assertEqual(len(read_attempt_ledger(self.state)), MAX_LEDGER_ENTRIES)

    def test_a_state_already_refused_never_reaches_a_model_config(self):
        # The property that matters: the hold is decided before the model is
        # built, so no WorkOrder, no route decision and no scheduler row.
        self.publish_mission()
        first = self.plan()
        record_attempt(self.state, first["state_hash"], {
            "created_at": NOW.isoformat(), "status": "succeeded",
            "plan_status": "refused", "failure_reason": "directive 0 gives no reason"})
        missing = self.root / "there-is-no-such-config.json"
        again = self.plan(dry_run=False, model_config_path=missing)
        self.assertEqual(again["status"], "held")
        self.assertEqual(again["plan_status"], "held")
        self.assertEqual(again["held_reason"], "terminal_for_this_state")
        self.assertEqual(again["cost_micros"], 0)
        self.assertFalse(missing.exists())

    def test_a_dry_run_is_never_held_by_the_ledger(self):
        self.publish_mission()
        first = self.plan()
        record_attempt(self.state, first["state_hash"], {
            "created_at": NOW.isoformat(), "status": "succeeded",
            "plan_status": "refused", "failure_reason": "x"})
        self.assertEqual(self.plan()["plan_status"], "gated")

    def test_the_run_records_its_own_attempt(self):
        self.publish_mission()
        summary = self.plan()
        remembered = read_attempt_ledger(self.state)[summary["state_hash"]]
        self.assertEqual(remembered["plan_status"], "gated")
        self.assertEqual(remembered["attempts"], 1)


class ProjectionLadderTests(unittest.TestCase):
    document = staticmethod(
        _planner_fixtures.PromptTests.__dict__["document"].__func__)
    document_state = _planner_fixtures.PromptTests.document_state
    heavy_state = _planner_fixtures.PromptTests.heavy_state

    def heavy_inventory(self, per_company=30):
        from dalton_core.store import content_hash

        built = self.heavy_state()
        for company in built["companies"]:
            company["readable_documents"] = [
                dict(document, document_ref=f"{document['document_ref']}:x{n}",
                     doc_date=f"2026-08-{(n % 28) + 1:02d}")
                for n, document in enumerate(company["readable_documents"] * 12)
            ][:per_company]
        built.pop("content_hash", None)
        built["content_hash"] = content_hash(built)
        return built

    def test_the_ladder_is_documented_and_descends_to_one(self):
        self.assertEqual(READABLE_KEEP_LADDER[0], READABLE_KEEP_PER_COMPANY)
        self.assertEqual(READABLE_KEEP_LADDER[-1], 1)
        self.assertEqual(sorted(READABLE_KEEP_LADDER, reverse=True),
                         list(READABLE_KEEP_LADDER))

    def projections(self, built):
        """A handful of bounds rather than a search: each one costs prompts."""

        full = len(build_prompt(built).encode("utf-8"))
        served = {}
        for divisor in (2, 3, 4, 6, 8, 12):
            try:
                candidate = project_state_for_prompt(
                    built, max_input_bytes=max(1_000, full // divisor))
            except ResearchPlanInputTooLarge:
                continue
            keep = candidate["prompt_projection"]["readable_keep_per_company"]
            served.setdefault(keep, candidate)
        return served

    def test_a_bound_eight_cannot_reach_is_reached_by_a_tighter_rung(self):
        served = self.projections(self.heavy_inventory())
        tighter = [keep for keep in served if keep < READABLE_KEEP_PER_COMPANY]
        self.assertTrue(tighter, f"only these retentions served: {sorted(served)}")
        for keep in tighter:
            meta = served[keep]["prompt_projection"]
            self.assertIn("aggregate_readable_document_inventory",
                          meta["stages_applied"])
            self.assertIn(keep, READABLE_KEEP_LADDER)
            for company in served[keep]["companies"]:
                self.assertLessEqual(len(company["readable_documents"]), keep)

    def test_every_rung_still_accounts_for_every_document(self):
        served = self.projections(self.heavy_inventory())
        checked = 0
        for keep, projected in served.items():
            if keep >= READABLE_KEEP_PER_COMPANY:
                continue
            for company in projected["companies"]:
                summary = company["readable_documents_summary"]
                self.assertEqual(summary["retained_recent"] + summary["aggregated"], 30)
                self.assertTrue(summary["omitted_rows_hash"])
                checked += 1
        self.assertGreater(checked, 0)

    def test_a_bound_nothing_can_reach_still_refuses_before_routing(self):
        with self.assertRaises(ResearchPlanInputTooLarge) as caught:
            project_state_for_prompt(self.heavy_inventory(), max_input_bytes=1)
        self.assertIn("aggregate_readable_document_inventory",
                      caught.exception.report["stages_applied"])


class WorkOrderSizeTests(unittest.TestCase):
    def test_one_work_order_is_bounded_at_sixty_four_kilobytes(self):
        # The prompt *is* the work order's question, and the state is inlined
        # into it: a 266KB prompt is a 266KB row in scheduler.sqlite on every
        # tick, which is how that database reached 76MB.
        self.assertLessEqual(MAX_WORK_ORDER_PROMPT_BYTES, 64_000)


class DirectiveProjectionTests(unittest.TestCase):
    PLAN = {
        "directives": [
            {"company_ref": "c:1", "item_ref": "i:1", "action": "search",
             "reason": "nothing held"},
            {"company_ref": "c:2", "item_ref": "i:2", "action": "extract_figures",
             "reason": "the 10-K is held and the figures are not"},
            {"company_ref": "c:3", "item_ref": "i:3", "action": "read",
             "reason": "statements unread"},
            {"company_ref": "c:4", "item_ref": "i:4", "action": "stop",
             "reason": "satisfied"},
        ],
        "inquiries": [
            {"company_ref": "c:2", "question": "what is the backlog?",
             "directed_document": "document:1"},
            {"company_ref": "c:3", "question": "and the mix?",
             "directed_document": None},
        ],
    }

    def test_only_the_reading_actions_come_out_and_the_rank_is_the_plans(self):
        ranked = document_reading_priorities(self.PLAN)
        self.assertEqual([row["action"] for row in ranked],
                         ["extract_figures", "read"])
        self.assertEqual([row["rank"] for row in ranked], [1, 2])
        self.assertEqual([row["plan_position"] for row in ranked], [1, 2])
        self.assertEqual(ranked[0]["company_ref"], "c:2")
        self.assertIn("10-K", ranked[0]["reason"])

    def test_an_inquiry_nobody_can_route_is_named_as_such(self):
        ranked = gap_filling_inquiries(self.PLAN)
        self.assertEqual([row["addressable"] for row in ranked], [True, False])
        self.assertEqual(ranked[0]["directed_document"], "document:1")

    def test_no_plan_is_no_work_rather_than_an_error(self):
        self.assertEqual(document_reading_priorities(None), [])
        self.assertEqual(gap_filling_inquiries(None), [])
        self.assertEqual(document_reading_priorities({"directives": [None, 3]}), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
