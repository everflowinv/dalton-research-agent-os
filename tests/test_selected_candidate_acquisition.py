"""2026-10-08 (ws-7d MSFT FY2026-Q4): a selected transcript that never moved.

The search found the quarter, the selection ticket succeeded and chose the
document, and then every tick said ``idle``.  Reproduced on a copy of the live
state, ``launch_acquisition`` returned ``budget_exhausted``: the plan's 50
calls a day had 49 spent (40 document pages, 9 searches) and the search floor
held the last one back.  The budget was right; what was wrong is that nothing
said so -- the ledger kept only selection holds, and the search note said the
quarter was "等选取/获取".

These tests rebuild that shape: a succeeded ticket, its document still
``discovered``, an exhausted budget.  The acquisition waits without consuming
the ticket, the ledger and the search note name the real step, and the moment
the budget allows it the selected document is the one launched.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dalton_core.mission_source_discovery import (
    MissionSourceDiscoveryCoordinator,
    acquisition_reason_text,
    build_discovery_plan,
    candidate_step_text,
    discovery_coordinator_state,
    missing_periods_note,
)
from dalton_core.tick_ledger import TickLedger, coordinator_states
from tests import test_mission_source_discovery as base

NEW_DOC, NOW = base.NEW_DOC, base.NOW
FakeAcquisitionLauncher, FakeSearchLauncher = base.FakeAcquisitionLauncher, base.FakeSearchLauncher

TRANSCRIPTS = "earnings-call-transcripts"


class _Selection:
    """A selection launcher whose one ticket succeeded and chose ``NEW_DOC``."""

    def __init__(self, selected):
        self.selected = list(selected)
        self.started: list[str] = []
        self.consumed: list[str] = []

    def currently_consumed(self, **kwargs):
        return []

    def current_selections(self, **kwargs):
        return {}

    def start(self, *, discovery_ref, view, **kwargs):
        self.started.append(discovery_ref)
        return {"id": "discovery-selection:" + "d" * 24, "status": "succeeded",
                "discovery_ref": discovery_ref, "identity_hash": "x",
                "summary": {"status": "succeeded",
                            "selection": {"selected": [{"document_ref": ref}
                                                       for ref in self.selected]}},
                "effective_selected": tuple(self.selected)}

    def mark_consumed(self, ticket):
        self.consumed.append(ticket["id"])


class _Spool:
    def read_object(self, digest):
        return b"{}"


class SelectedCandidateWaitsForBudgetTests(unittest.TestCase):
    setUp = base.CoordinatorTests.setUp
    create_mission = base.CoordinatorTests.create_mission
    mission_v2 = base.CoordinatorTests.mission_v2

    def build(self):
        mission = self.mission_v2(self.create_mission())
        company = mission["universe"][0]["company_ref"]
        plan = build_discovery_plan(
            plan_id="discovery-plan:selected-budget:test",
            created_at=NOW.isoformat(timespec="microseconds"),
            mission_ref=self.plan["mission_ref"], source_ref="source:alphaengine",
            max_calls_24h=30,
            companies={company: {"name": "Microsoft", "ticker": "MSFT",
                                 "aliases": ["Microsoft"]}},
            specs=[{"spec_ref": TRANSCRIPTS, "document_type": "meeting_minutes",
                    "query_variants": [{"query_template": "{name} {quarter_label}",
                                        "filters": {}}],
                    "lookback_days": 400, "rediscovery_interval_days": 7,
                    "retry_interval_days": 1}])
        search = FakeSearchLauncher(self.h, self.missions, plan)
        acquisition = FakeAcquisitionLauncher(self.h)
        selection = _Selection([NEW_DOC])
        spool = tempfile.TemporaryDirectory(); self.addCleanup(spool.cleanup)
        coordinator = MissionSourceDiscoveryCoordinator(
            store=self.h.core, missions=self.missions, plan=plan,
            search_launcher=search, acquisition_launcher=acquisition, clock=self.clock,
            selection_launcher=selection, spool_dir=spool.name)
        coordinator._spool = _Spool()
        self.assertEqual(coordinator.launch_discovery()["status"], "launched")
        coordinator.settle_dispatches()
        return mission, company, coordinator, search, acquisition, selection

    def view_patch(self):
        return patch("dalton_core.discovery_candidate_selection.candidate_view",
                     return_value={"content_hash": "v" * 64, "candidates": []})

    def period_patch(self, coordinator):
        def item(mission, company_ref, spec):
            return {"missing_periods": ["FY2026-Q4"], "candidate_periods": ["FY2026-Q4"],
                    "required_periods": ["FY2026-Q1", "FY2026-Q2", "FY2026-Q3", "FY2026-Q4"]}
        return patch.object(coordinator, "_period_item", side_effect=item)

    def test_exhausted_budget_keeps_the_ticket_and_says_so_then_the_selected_document_launches(self):
        mission, company, coordinator, search, acquisition, selection = self.build()
        # The one search already spent the whole owner cap: nothing is left
        # for a document page (live: 49 of 50 with the search floor holding 1).
        coordinator.owner_call_cap = 1
        with self.view_patch():
            held = coordinator.launch_acquisition()
        self.assertEqual(held["status"], "budget_exhausted", held)
        self.assertEqual(held["document_ref"], NEW_DOC)
        self.assertEqual(acquisition.calls, [])
        # Not consumed: the selection stands and is fetched when budget allows.
        self.assertEqual(selection.consumed, [])
        self.assertIn("候选已选中，等待获取", held["candidate_step"])
        self.assertIn("调用额度已用完", held["candidate_step"])
        ticker = company.rsplit(":", 1)[-1].upper()
        self.assertTrue(held["candidate_step"].startswith(f"{ticker}/earnings-call-transcripts："),
                        held["candidate_step"])

        # The ledger names it -- status, reason with the budget and document.
        state = discovery_coordinator_state({
            "status": "idle", "discovery": {"status": "idle"}, "acquisitions_launched": 0,
            "acquisition": held})
        self.assertEqual(state["status"], "idle")
        self.assertEqual(state["acquisition_status"], "budget_exhausted")
        self.assertIn("bound by owner", state["acquisition_reason"])
        self.assertIn(NEW_DOC, state["acquisition_reason"])
        self.assertIn("候选已选中，等待获取", state["candidate_step"])
        kept = coordinator_states({"coordinators": {"alphaengine": state}})["alphaengine"]
        self.assertEqual(kept["candidate_step"], state["candidate_step"])

        # The search note for the quarter says the same instead of "等选取/获取".
        spec = coordinator.plan["specs"][0]
        with self.period_patch(coordinator), \
                patch.object(coordinator, "_satisfied_block", return_value=None):
            reason = coordinator._spec_block(mission, company, spec)
        self.assertIn("仍缺 FY2026-Q4（FY2026-Q4 候选已选中，等待获取", reason)
        self.assertNotIn("等选取/获取", reason)

        # Budget back: the selected document is the one launched, and the note
        # goes away.
        coordinator.owner_call_cap = 100
        with self.view_patch():
            launched = coordinator.launch_acquisition()
        self.assertEqual(launched["status"], "launched", launched)
        self.assertEqual([call["document_ref"] for call in acquisition.calls], [NEW_DOC])
        self.assertIsNone(coordinator._candidate_step(company, TRANSCRIPTS))
        with self.period_patch(coordinator), \
                patch.object(coordinator, "_satisfied_block", return_value=None):
            reason = coordinator._spec_block(mission, company, spec)
        self.assertIn("已找到候选，等选取/获取", reason)

    def test_a_stale_step_falls_back_to_the_generic_words(self):
        _mission, company, coordinator, *_rest = self.build()
        coordinator.owner_call_cap = 1
        with self.view_patch():
            coordinator.launch_acquisition()
        self.assertIsNotNone(coordinator._candidate_step(company, TRANSCRIPTS))
        self.clock.advance(hours=2)
        self.assertIsNone(coordinator._candidate_step(company, TRANSCRIPTS))


class LedgerHonestyTests(unittest.TestCase):
    def state(self, acquisition):
        return discovery_coordinator_state({
            "status": "idle", "discovery": {"status": "idle"}, "acquisitions_launched": 0,
            "acquisition": acquisition})

    def test_every_non_launched_status_is_recorded_with_a_reason(self):
        cases = {
            "budget_exhausted": {"status": "budget_exhausted", "document_ref": "alphaengine-doc:1",
                                 "budget": {"spent": 49, "cap": 50, "bound_by": "search_floor",
                                            "search_floor_held": 1,
                                            "frees_at": "2026-10-08T20:23:44+00:00"}},
            "completed_selected": {"status": "completed_selected", "ticket_ref": "t:1",
                                   "discovery_ref": "d:1"},
            "completed_empty": {"status": "completed_empty", "ticket_ref": "t:2",
                                "discovery_ref": "d:2"},
            "busy": {"status": "busy", "reason": "a discovered-document acquisition is still open"},
            "health_hold": {"status": "health_hold", "reason": "login lapsed"},
            "unconfigured": {"status": "unconfigured", "reason": "acquisition launcher is not configured"},
            "already_in_authority": {"status": "already_in_authority", "document_ref": "alphaengine-doc:2",
                                     "settled_status": "already_in_authority"},
            "not_authorized": {"status": "not_authorized", "reason": "CoverageMissionError: no"},
        }
        for status, acquisition in cases.items():
            with self.subTest(status=status):
                state = self.state(acquisition)
                self.assertEqual(state["acquisition_status"], status)
                self.assertTrue(state.get("acquisition_reason"), state)
                self.assertLessEqual(len(state["acquisition_reason"]), 200)
        budget = self.state(cases["budget_exhausted"])["acquisition_reason"]
        self.assertIn("49/50", budget)
        self.assertIn("search floor holds 1", budget)
        self.assertIn("2026-10-08T20:23:44", budget)

    def test_launch_and_empty_queue_add_nothing(self):
        self.assertNotIn("acquisition_status", self.state({"status": "idle"}))
        self.assertNotIn("acquisition_status", self.state({"status": "launched"}))

    def test_reasons_are_bounded(self):
        state = self.state({"status": "rejected", "reason": "x" * 900,
                            "candidate_step": "MSFT/t：" + "y" * 900})
        self.assertEqual(len(state["acquisition_reason"]), 200)
        self.assertEqual(len(state["candidate_step"]), 200)

    def test_step_words_per_outcome(self):
        self.assertEqual(candidate_step_text({"status": "selection_pending", "reason": "running"}),
                         "候选选取中")
        self.assertEqual(
            candidate_step_text({"status": "selection_quarantined",
                                 "reason": "CandidateSelectionError: wrong issuer"}),
            "候选被规则拒绝：CandidateSelectionError: wrong issuer")
        self.assertIn("没有符合", candidate_step_text({"status": "completed_empty"}))
        step = candidate_step_text(
            {"status": "budget_exhausted", "document_ref": "alphaengine-doc:1",
             "budget": {"spent": 49, "cap": 50, "search_floor_held": 1,
                        "frees_at": "2026-10-08T20:23:44+00:00"}}, selected=True)
        self.assertEqual(step, "候选已选中，等待获取：AlphaEngine 24 小时调用额度已用完"
                               "（已用 49/50，另有 1 次留给搜索），约 2026-10-08 20:23 UTC 起恢复")
        self.assertIsNone(candidate_step_text({"status": "idle"}))
        self.assertIsNone(acquisition_reason_text({"status": "idle"}))

    def test_the_generic_note_is_unchanged_without_a_step(self):
        self.assertEqual(missing_periods_note(["FY2026-Q4"], {"FY2026-Q4"}),
                         "；仍缺 FY2026-Q4（FY2026-Q4 已找到候选，等选取/获取）")
        self.assertEqual(missing_periods_note(["FY2026-Q4"], {"FY2026-Q4"}, step="候选选取中"),
                         "；仍缺 FY2026-Q4（FY2026-Q4 候选选取中）")


class LedgerReadBackTests(unittest.TestCase):
    def test_latest_coordinator_state_skips_deferred_turns_only(self):
        with tempfile.TemporaryDirectory() as root:
            from dalton_core.tick_ledger import default_path
            path = default_path(root)
            ledger = TickLedger(path)
            connection = ledger.connection
            rows = [
                ("2026-10-08T12:00:00+00:00", {"status": "idle", "acquisition_status": "budget_exhausted",
                                               "candidate_step": "MSFT/earnings-call-transcripts：候选已选中，等待获取：x"}),
                ("2026-10-08T12:05:00+00:00", {"status": "deferred", "reason": "tick budget exhausted"}),
            ]
            connection.execute("PRAGMA foreign_keys=OFF")
            for at, state in rows:
                connection.execute(
                    "INSERT INTO tick_ledger_lanes (tick_id, day, started_at, driver_key, lane_operation, "
                    "pool, status, status_word, idle, pool_exhausted, counts_json, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"tick:{at}", at[:10], at, "mission_source_discovery", "op", "pool",
                     "idle", "idle", 1, 0, json.dumps({"coordinators": {"alphaengine": state}}), at))
            ledger.close()
            with TickLedger(path, read_only=True) as reader:
                state = reader.latest_coordinator_state("mission_source_discovery", "alphaengine")
            self.assertEqual(state["acquisition_status"], "budget_exhausted")
            self.assertEqual(state["started_at"], "2026-10-08T12:00:00+00:00")

            from dalton_core.cockpit_plane import _candidate_period_steps
            steps = _candidate_period_steps(
                Path(root), now=datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc))
            self.assertEqual(steps, {"MSFT": "候选已选中，等待获取：x"})
            self.assertEqual(_candidate_period_steps(
                Path(root), now=datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)), {})


if __name__ == "__main__":
    unittest.main()
