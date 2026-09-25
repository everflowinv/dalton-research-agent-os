"""2026-09-24: the planner re-asked Opus every five minutes over nothing.

Live, after deploy, ws-7d made 17 ``cockpit-plan`` calls and legacy 11, about
one every five minutes at ~$0.44 each.  Adjacent prompts differed only in
``as_of``, ``spend.model_today`` (calls and dollars), hashes over omitted rows
and the odd pipeline counter.  ``spend`` was hashed, so every lane settlement
made a "new" state and defeated both the stored-plan replay and the attempt
ledger.
"""

from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from dalton_core.research_planner_cli import (
    MIN_REPLAN_SECONDS,
    PLAN_LAST_DECISION,
    build_state,
    read_last_decision,
    replan_pacing_hold,
)
from dalton_core.research_state import (
    budget_bands,
    planning_core_hash,
    planning_hash,
    state_content_hash,
)
# Imported as modules so unittest does not collect their TestCases twice.
from tests import test_research_planner as _planner_fixtures
from tests import test_research_planner_cli as _cli_fixtures


def _spend(calls, cost, cap=500.0, ae_spent=5, ae_cap=130):
    return {
        "model_today": {"calls": calls, "cost_usd": cost, "cost_cap_usd": cap,
                        "remaining_usd": round(cap - cost, 4), "call_cap": 100000},
        "alphaengine_24h": {"spent": ae_spent, "cap": ae_cap,
                            "remaining": ae_cap - ae_spent},
        "web_search_24h": {"searches": 0, "fetches": 51},
    }


class StateHashTests(unittest.TestCase):
    def state(self, **overrides):
        return _planner_fixtures.state(**overrides)

    def test_spend_moving_inside_its_band_changes_neither_hash(self):
        # The live pair 12:24 -> 12:29 on ws-7d: 130 -> 139 calls,
        # $4.91 -> $5.35, 51 -> 54 fetches.
        before = self.state(spend=_spend(130, 4.9101))
        after = self.state(spend=_spend(139, 5.3476, ae_spent=9) | {
            "web_search_24h": {"searches": 0, "fetches": 54}})
        self.assertEqual(before["content_hash"], after["content_hash"])
        self.assertEqual(planning_hash(before), planning_hash(after))
        # The prompt still shows the planner the real numbers.
        self.assertEqual(after["spend"]["model_today"]["calls"], 139)

    def test_a_budget_crossing_into_near_cap_or_exhausted_is_material(self):
        normal = self.state(spend=_spend(10, 100.0))
        near = self.state(spend=_spend(10, 420.0))
        spent = self.state(spend=_spend(10, 500.0))
        self.assertEqual(budget_bands(normal["spend"])["model_today"], "normal")
        self.assertEqual(budget_bands(near["spend"])["model_today"], "near_cap")
        self.assertEqual(budget_bands(spent["spend"])["model_today"], "exhausted")
        hashes = {planning_hash(item) for item in (normal, near, spent)}
        self.assertEqual(len(hashes), 3)
        self.assertEqual(
            len({item["content_hash"] for item in (normal, near, spent)}), 3)

    def test_pipeline_counters_are_not_material_but_status_and_gaps_are(self):
        item, entry, ACN = (_planner_fixtures.item, _planner_fixtures.entry,
                            _planner_fixtures.ACN)
        base = self.state(checklist=[entry(ACN, "ACN", item("earnings_calls"))])
        counters = self.state(checklist=[entry(
            ACN, "ACN", item("earnings_calls", have=5, pending=1, failed=2))])
        gap = self.state(checklist=[entry(
            ACN, "ACN", item("earnings_calls", have=1, status="partial"),
            gaps=["earnings_calls"])])
        # A new document or a queued fetch is a different exact state...
        self.assertNotEqual(base["content_hash"], counters["content_hash"])
        # ...but not a different plan.
        self.assertEqual(planning_hash(base), planning_hash(counters))
        self.assertNotEqual(planning_hash(base), planning_hash(gap))

    def test_readable_document_counts_and_row_hashes_are_not_material(self):
        base = self.state()
        moved = copy.deepcopy(base)
        company = moved["companies"][0]
        company["readable_documents_summary"] = {"aggregated": 46,
                                                 "omitted_rows_hash": "f" * 64}
        company["unavailable_documents_summary"] = {"count": 51}
        company["metrics_watched"] = list(reversed(company["metrics_watched"]))
        moved["totals"]["figures_held"] += 1
        moved["as_of"] = "2026-09-24T13:04:54.562431+00:00"
        self.assertEqual(planning_hash(base), planning_hash(moved))

    def test_a_prompt_projection_hashes_like_its_state(self):
        base = self.state()
        projected = dict(base, prompt_projection={"full_state_hash": "x"})
        self.assertEqual(planning_hash(base), planning_hash(projected))
        self.assertEqual(state_content_hash(base), state_content_hash(projected))


class DossierFeedbackMaterialityTests(unittest.TestCase):
    """2026-09-25 06:24-06:49 legacy: five paid plans, $2.34, whose prompts
    differed only in the latest Dossier run's verdict (IBM, DXC, CTSH flipping
    between partial_published, rubric_refused and constitution_refused) or in
    the ticket-bound ids of its repair targets."""

    def feedback(self, status, *, ticket="c", targets=("b",), material=None):
        base = _planner_fixtures.repair_feedback()
        base["dossier_status"] = status
        base["source_ticket_ref"] = "company-dossier-run:" + ticket * 24
        base["id"] = "dossier-repair-feedback:" + ticket * 32
        base["content_hash"] = ticket * 64
        base["repair_targets"] = [
            dict(base["repair_targets"][0], id="dossier-repair-target:" + t * 32,
                 content_hash=t * 64) for t in targets]
        base["material"] = material or {
            "published_version_ref": "company-dossier-version:acn:6",
            "stable_repair_target_keys": [],
        }
        return base

    def state(self, feedback):
        return _planner_fixtures.state(
            dossier_feedback_by_company={_planner_fixtures.ACN: feedback})

    def test_verdict_and_target_ids_flipping_is_not_material(self):
        states = [
            self.state(self.feedback("partial_published", ticket="1", targets=())),
            self.state(self.feedback("rubric_refused", ticket="2", targets=("e",))),
            self.state(self.feedback("constitution_refused", ticket="3", targets=("f",))),
        ]
        self.assertEqual(len({item["content_hash"] for item in states}), 3)
        self.assertEqual(len({planning_hash(item) for item in states}), 1)

    def test_published_version_or_stable_targets_are_material_but_not_core(self):
        base = self.state(self.feedback("rubric_refused"))
        published = self.state(self.feedback("published", material={
            "published_version_ref": "company-dossier-version:acn:7",
            "stable_repair_target_keys": []}))
        stable = self.state(self.feedback("rubric_refused", material={
            "published_version_ref": "company-dossier-version:acn:6",
            "stable_repair_target_keys": ["k" * 64]}))
        self.assertEqual(len({planning_hash(item) for item in (base, published, stable)}), 3)
        self.assertEqual(len({planning_core_hash(item)
                              for item in (base, published, stable)}), 1)
        # A gap in the checklist is core: re-planned at once.
        item, entry, ACN = (_planner_fixtures.item, _planner_fixtures.entry,
                            _planner_fixtures.ACN)
        gap = _planner_fixtures.state(
            checklist=[entry(ACN, "ACN", item("earnings_calls", have=1, status="partial"),
                             gaps=["earnings_calls"])],
            dossier_feedback_by_company={ACN: self.feedback("rubric_refused")})
        self.assertNotEqual(planning_core_hash(base), planning_core_hash(gap))

    def test_the_tightest_prompt_projection_keeps_the_material_projection(self):
        from dalton_core.research_planner import project_state_for_prompt
        tests = _planner_fixtures.FeedbackProjectionTests(
            "test_twelve_companies_are_all_carried_at_the_live_bound")

        def projected(status, version):
            built = tests.worst_case(12)
            for company in built["companies"]:
                company["dossier_feedback"]["dossier_status"] = status
                company["dossier_feedback"]["material"] = {
                    "published_version_ref": version,
                    "stable_repair_target_keys": ["k" * 64]}
            return project_state_for_prompt(tests.rebind(built),
                                            max_input_bytes=tests.BOUND)

        base = projected("rubric_refused", "company-dossier-version:acn:6")
        # Reduced to each company's checklist core...
        self.assertIn("repair_target_ids", base["companies"][0]["dossier_feedback"])
        flipped = projected("constitution_refused", "company-dossier-version:acn:6")
        published = projected("published", "company-dossier-version:acn:7")
        self.assertEqual(planning_hash(base), planning_hash(flipped))
        self.assertNotEqual(planning_hash(base), planning_hash(published))
        self.assertEqual(planning_core_hash(base), planning_core_hash(published))


class PacingHoldTests(unittest.TestCase):
    NOW = datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc)

    def test_same_planning_hash_inside_the_interval_is_held(self):
        last = {"at": (self.NOW - timedelta(minutes=5)).isoformat(),
                "planning_hash": "p"}
        held = replan_pacing_hold(last, "p", now=self.NOW)
        self.assertEqual(held["held_reason"], "no_material_change")

    def test_a_material_change_or_an_elapsed_interval_is_asked(self):
        last = {"at": (self.NOW - timedelta(minutes=5)).isoformat(),
                "planning_hash": "p"}
        self.assertIsNone(replan_pacing_hold(last, "q", now=self.NOW))
        old = {"at": (self.NOW - timedelta(seconds=MIN_REPLAN_SECONDS)).isoformat(),
               "planning_hash": "p"}
        self.assertIsNone(replan_pacing_hold(old, "p", now=self.NOW))
        self.assertIsNone(replan_pacing_hold({}, "p", now=self.NOW))
        self.assertIsNone(replan_pacing_hold(
            {"at": "not a time", "planning_hash": "p"}, "p", now=self.NOW))

    def test_a_dossier_feedback_only_change_waits_for_the_interval(self):
        last = {"at": (self.NOW - timedelta(minutes=5)).isoformat(),
                "planning_hash": "p", "planning_core_hash": "c"}
        held = replan_pacing_hold(last, "q", now=self.NOW, planning_core_hash="c")
        self.assertEqual(held["held_reason"], "dossier_feedback_paced")
        # Same everything: the ordinary hold.
        self.assertEqual(replan_pacing_hold(
            last, "p", now=self.NOW, planning_core_hash="c")["held_reason"],
            "no_material_change")
        # A core change is asked at once, feedback moved or not.
        self.assertIsNone(replan_pacing_hold(
            last, "q", now=self.NOW, planning_core_hash="d"))
        # After the interval the feedback change buys one plan.
        old = dict(last, at=(self.NOW - timedelta(seconds=MIN_REPLAN_SECONDS)).isoformat())
        self.assertIsNone(replan_pacing_hold(
            old, "q", now=self.NOW, planning_core_hash="c"))

    def test_a_decision_recorded_before_the_core_hash_uses_the_full_hash(self):
        last = {"at": (self.NOW - timedelta(minutes=5)).isoformat(),
                "planning_hash": "p"}
        self.assertIsNone(replan_pacing_hold(
            last, "q", now=self.NOW, planning_core_hash="c"))
        self.assertEqual(replan_pacing_hold(
            last, "p", now=self.NOW, planning_core_hash="c")["held_reason"],
            "no_material_change")


class PacedRunTests(unittest.TestCase):
    setUp = _cli_fixtures.PlannerChildTests.setUp
    close = _cli_fixtures.PlannerChildTests.close
    publish_mission = _cli_fixtures.PlannerChildTests.publish_mission
    plan = _cli_fixtures.PlannerChildTests.plan

    def _states(self, mission):
        base = build_state(self.store, self.missions, mission,
                           plans_dir=self.state / "discovery-plans",
                           as_of="2026-09-24T12:24:20+00:00")
        base["spend"] = _spend(130, 4.9101)

        def variant(mutate):
            moved = copy.deepcopy(base)
            mutate(moved)
            moved["content_hash"] = state_content_hash(moved)
            return moved

        def counters(state):
            state["as_of"] = "2026-09-24T12:29:22+00:00"
            state["spend"] = _spend(139, 5.3476)
            state["totals"]["figures_held"] += 1

        def material(state):
            state["as_of"] = "2026-09-24T12:34:28+00:00"
            state["totals"]["open_gaps"] += 1

        base["content_hash"] = state_content_hash(base)
        return base, variant(counters), variant(material)

    def test_only_a_material_change_buys_a_new_plan_inside_the_interval(self):
        mission = self.publish_mission()
        first, noise, material = self._states(mission)
        self.assertNotEqual(first["content_hash"], noise["content_hash"])
        config = self.root / "model.json"
        config.write_text("{}", encoding="utf-8")
        response = json.dumps({
            "schema_version": "0.1", "assessment": "Hold course.",
            "directives": [], "inquiries": [], "sufficiency": [],
        })
        results = []
        with patch("dalton_core.research_planner_cli.CockpitModel") as model:
            model.return_value.budget_for.return_value = {
                "max_input_tokens": 80_000, "max_output_tokens": 4_000,
                "max_cost_usd": 1.0, "timeout_seconds": 300,
            }
            model.return_value.call.return_value = {
                "text": response, "cost_micros": 440_000, "replayed": False,
            }
            for built in (first, noise, material):
                with patch("dalton_core.research_planner_cli.build_state",
                           return_value=built):
                    results.append(self.plan(dry_run=False, model_config_path=config))
            calls_before_interval = model.return_value.call.call_count
            # Two hours later the same non-material noise is folded in once.
            path = self.state / PLAN_LAST_DECISION
            last = read_last_decision(self.state)
            last["at"] = (datetime.fromisoformat(last["at"])
                          - timedelta(seconds=MIN_REPLAN_SECONDS + 1)).isoformat()
            path.write_text(json.dumps(last), encoding="utf-8")
            late = copy.deepcopy(material)
            late["spend"] = _spend(300, 9.0)
            late["totals"]["figures_held"] += 5
            late["content_hash"] = state_content_hash(late)
            with patch("dalton_core.research_planner_cli.build_state",
                       return_value=late):
                results.append(self.plan(dry_run=False, model_config_path=config))

        self.assertEqual([item["plan_status"] for item in results],
                         ["fresh", "held", "fresh", "fresh"])
        self.assertEqual(results[1]["held_reason"], "no_material_change")
        self.assertEqual(results[1]["cost_micros"], 0)
        self.assertEqual(calls_before_interval, 2)
        self.assertEqual(model.return_value.call.call_count, 3)
        self.assertEqual(read_last_decision(self.state)["planning_hash"],
                         results[3]["planning_hash"])
        self.assertEqual(read_last_decision(self.state)["planning_core_hash"],
                         results[3]["planning_core_hash"])


if __name__ == "__main__":
    unittest.main()
