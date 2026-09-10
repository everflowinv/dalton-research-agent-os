"""P17d: the four-panel overview row and the ops backlog page behind it.

Chem's §8.8, and the retrospective's 3.5: run state, source gaps, pending
failures and output acceptance are four facts that only mean something
together.  Chem kept them on four pages, and its "health OK" page therefore sat
beside an unfilled research gap and a task that had been permanently suspended
for a week.  These tests are about the row, and about the one property that
makes it worth having: each tile counts what its own page shows.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.cockpit_plane import (
    DEPENDENCY_LABELS,
    FAILURE_CLASS_LABELS,
    LANE_STATUS_BUCKETS,
    LANE_STATUS_BUCKET_OF,
    REGISTRY_LANE_LABELS,
)
from dalton_core.lane_failure_class import LaneFailureBudget
from dalton_core.lane_failure_ledger import LaneFailureLedger, default_path

from tests.test_cockpit_plane import CockpitHarness

OWNER = "owner@example.com"
TASK_62 = "AlphaEngine Desktop status=no_module_page"
PAGE = Path(__file__).resolve().parents[1] / "src" / "dalton_core" / "cockpit_control.html"


class PanelCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.c = CockpitHarness(self.root)
        self.addCleanup(self.c.close)
        self.plane = self.c.plane

    def park(self, *, lane: str = "research_task", item: str = "task:62",
             reason: str = TASK_62, at: datetime | None = None) -> None:
        moment = at or datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
        with LaneFailureLedger(default_path(self.root), clock=lambda: moment) as ledger:
            budget = LaneFailureBudget(lane, ledger=ledger, clock=lambda: moment)
            budget.record(item, reason=reason)

    def lanes(self, planner: dict) -> None:
        heartbeat = json.loads(self.c.heartbeat.read_text(encoding="utf-8"))
        heartbeat["bounded_planner"]["last_result"] = {
            **heartbeat["bounded_planner"]["last_result"], **planner}
        self.c.heartbeat.write_text(json.dumps(heartbeat), encoding="utf-8")


class OpsBacklogTests(PanelCase):
    def test_a_core_that_never_parked_anything_says_so_rather_than_erroring(self) -> None:
        backlog = self.plane.ops_backlog()
        self.assertFalse(backlog["available"])
        self.assertEqual(backlog["parked_items"], 0)
        self.assertIn("挂起", backlog["reason"])

    def test_a_parked_item_appears_under_its_dependency_with_first_and_last_seen(self) -> None:
        first = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)
        self.park(at=first)
        self.park(at=first + timedelta(hours=30))
        backlog = self.plane.ops_backlog()
        self.assertTrue(backlog["available"])
        self.assertEqual(backlog["parked_items"], 1)
        bucket = backlog["dependencies"][0]
        self.assertEqual(bucket["dependency"], "alphaengine_desktop")
        self.assertEqual(bucket["dependency_label"],
                         DEPENDENCY_LABELS["alphaengine_desktop"])
        self.assertEqual(bucket["item_count"], 1)
        item = bucket["items"][0]
        self.assertEqual(item["first_seen"][:10], "2026-09-08")
        self.assertEqual(item["last_seen"][:10], "2026-09-09")
        self.assertEqual(item["reason"], TASK_62)

    def test_two_lanes_waiting_on_one_source_are_one_row(self) -> None:
        """The reading Chem could not get: an outage, not two lane problems."""

        self.park(lane="research_task", item="task:62")
        self.park(lane="mission_ownership", item="company:acn")
        backlog = self.plane.ops_backlog()
        self.assertEqual(len(backlog["dependencies"]), 1)
        bucket = backlog["dependencies"][0]
        self.assertEqual(bucket["item_count"], 2)
        self.assertIn("mission_ownership", bucket["lanes"])
        self.assertIn(REGISTRY_LANE_LABELS["mission_ownership"], bucket["lane_labels"])

    def test_terminal_items_are_shown_apart_and_labelled(self) -> None:
        self.park(item="doc:9", reason="the scan is unreadable")
        backlog = self.plane.ops_backlog()
        self.assertEqual(backlog["parked_items"], 0)
        self.assertEqual(backlog["terminal_count"], 1)
        self.assertEqual(backlog["terminal_items"][0]["item_key"], "doc:9")
        self.assertEqual(set(backlog["class_labels"]), set(FAILURE_CLASS_LABELS))

    def test_permission_items_have_their_own_authorization_bucket(self) -> None:
        self.park(item="doc:permission",
                  reason="gated:mission does not grant document_extraction writes")
        backlog = self.plane.ops_backlog()
        self.assertEqual(backlog["permission_count"], 1)
        self.assertEqual(backlog["permission_items"][0]["item_key"], "doc:permission")
        self.assertEqual(backlog["parked_items"], 0)
        self.assertEqual(backlog["terminal_count"], 0)

    def test_the_page_carries_no_machine_words_for_a_dependency_it_knows(self) -> None:
        self.park()
        bucket = self.plane.ops_backlog()["dependencies"][0]
        self.assertNotEqual(bucket["dependency_label"], bucket["dependency"])


class FourPanelTests(PanelCase):
    def test_the_overview_carries_all_four_panels_each_linking_somewhere(self) -> None:
        ops = self.plane.overview()["ops"]
        self.assertEqual(set(ops), {"lanes", "gaps", "failures", "acceptance"})
        for name, panel in ops.items():
            with self.subTest(panel=name):
                self.assertIn("link", panel)
                self.assertTrue(panel["link"])
                self.assertIn("note", panel)

    def test_the_lane_panel_counts_the_six_words_the_owner_reads(self) -> None:
        self.lanes({
            "mission_market_prices": {"status": "ungranted", "reason": "no grant"},
            "claim_index": {"status": "idle", "reason": "every claim is indexed"},
            "mission_ownership": {"status": "launched"},
            "mission_consensus": {"status": "held", "reason": "three failures"},
        })
        view = self.plane.overview()
        panel = view["ops"]["lanes"]
        self.assertEqual(set(panel["counts"]), set(LANE_STATUS_BUCKETS))
        self.assertGreaterEqual(panel["counts"]["ungranted"], 1)
        self.assertGreaterEqual(panel["counts"]["running"], 1)
        self.assertGreaterEqual(panel["counts"]["held"], 1)
        self.assertEqual(panel["total"], len(view["activity"]["lanes"]))

    def test_the_lane_panel_and_the_lane_rows_cannot_disagree(self) -> None:
        """The tile is a count of the rows below it, not a second reading."""

        view = self.plane.overview()
        panel, rows = view["ops"]["lanes"], view["activity"]["lanes"]
        self.assertEqual(sum(panel["counts"].values()) + panel["other"], len(rows))

    def test_a_lane_word_the_buckets_never_saw_is_counted_not_dropped(self) -> None:
        self.lanes({"claim_index": {"status": "something_new"}})
        panel = self.plane.overview()["ops"]["lanes"]
        self.assertGreaterEqual(panel["other"], 1)
        self.assertNotIn("something_new", LANE_STATUS_BUCKET_OF)

    def test_the_failure_panel_reads_the_same_ledger_as_the_ops_page(self) -> None:
        self.park()
        self.park(lane="mission_ownership", item="company:acn")
        view = self.plane.overview()
        panel = view["ops"]["failures"]
        backlog = self.plane.ops_backlog()
        self.assertTrue(panel["available"])
        self.assertEqual(panel["parked_items"], backlog["parked_items"])
        self.assertEqual(panel["headline"],
                         backlog["parked_items"] + backlog["terminal_count"])
        self.assertEqual(panel["dependencies"][0]["dependency"], "alphaengine_desktop")
        self.assertEqual(panel["link"], "ops")

    def test_the_failure_panel_is_honest_about_a_core_with_no_ledger(self) -> None:
        panel = self.plane.overview()["ops"]["failures"]
        self.assertFalse(panel["available"])
        self.assertEqual(panel["headline"], 0)

    def test_the_gap_panel_names_both_kinds_of_gap(self) -> None:
        panel = self.plane.overview()["ops"]["gaps"]
        self.assertIn("framework_gaps", panel)
        self.assertIn("extraction_backlog", panel)
        self.assertIsInstance(panel["headline"], int)
        self.assertEqual(panel["link"], "sources")

    def test_the_gap_panel_counts_the_open_framework_gaps(self) -> None:
        record = {
            "id": "industry-framework-version:1", "gaps": [
                {"gap_ref": "gap:pricing", "label": "定价权", "status": "open"},
                {"gap_ref": "gap:capex", "label": "资本开支", "status": "partially_covered"},
                {"gap_ref": "gap:mix", "label": "结构", "status": "covered"},
            ],
        }
        self.c.h.h.core.connection.executescript(
            "CREATE TABLE IF NOT EXISTS industry_framework_versions("
            "industry_ref TEXT, version_number INTEGER, record_json TEXT)")
        self.c.h.h.core.connection.execute(
            "INSERT INTO industry_framework_versions VALUES(?,?,?)",
            ("industry:us-it-services", 1, json.dumps(record)))
        self.c.h.h.core.connection.commit()
        panel = self.plane.overview()["ops"]["gaps"]
        self.assertTrue(panel["framework_gaps"]["available"])
        self.assertEqual(panel["framework_gaps"]["open"], 2)
        self.assertEqual(panel["framework_gaps"]["industries"][0]["gaps"], 3)

    def test_the_acceptance_panel_reports_last_closed_week(self) -> None:
        panel = self.plane.overview()["ops"]["acceptance"]
        self.assertEqual(panel["link"], "reflection")
        if panel["available"]:
            self.assertIn("W", panel["week"])
            self.assertIsInstance(panel["published"], int)
        else:
            self.assertTrue(panel["note"])

    def test_a_panel_never_raises_on_a_core_missing_the_table_it_reads(self) -> None:
        """Old-Core degradation, the rule every cockpit reader follows."""

        for table in ("industry_framework_versions", "research_quality_score_versions"):
            with self.subTest(table=table):
                with self.plane._core() as core:
                    from dalton_core.cockpit_plane import _table_exists

                    if _table_exists(core, table):
                        continue
                ops = self.plane.overview()["ops"]
                self.assertIn("gaps", ops)
                self.assertIn("acceptance", ops)


class RouteTests(unittest.TestCase):
    def application(self, plane):
        from dalton_core.agenda_control import AgendaControlApplication

        return AgendaControlApplication(None, None, cockpit_plane=plane)

    def test_the_ops_route_calls_the_reader_and_says_it_is_enabled(self) -> None:
        class Plane:
            def __init__(self) -> None:
                self.seen = 0

            def ops_backlog(self):
                self.seen += 1
                return {"available": True, "parked_items": 2}

        plane = Plane()
        view = self.application(plane).cockpit_view("/v1/cockpit/ops", OWNER, {})
        self.assertEqual(plane.seen, 1)
        self.assertEqual(view["parked_items"], 2)
        self.assertTrue(view["enabled"])


class PageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = PAGE.read_text(encoding="utf-8")

    def test_the_page_has_a_four_panel_row_and_asks_for_the_ops_route(self) -> None:
        self.assertIn('<div class="grid c4" id="ops-panels">', self.page)
        self.assertIn("/v1/cockpit/ops", self.page)
        self.assertIn("renderOpsPanels", self.page)

    def test_each_panel_opens_the_page_it_counts(self) -> None:
        for opener in ("openSources", "openOps", "openReflection"):
            with self.subTest(opener=opener):
                self.assertIn(opener, self.page)
        # The two existing readers are still reachable from their own buttons.
        self.assertIn('$("open-sources").onclick=openSources;', self.page)
        self.assertIn('$("open-reflection").onclick=openReflection;', self.page)

    def test_the_row_speaks_the_owner_s_language(self) -> None:
        for word in ("一眼看全", "还没填上的来源缺口", "挂起 / 不再重试的工作",
                     "上周产物验收", "运维待办"):
            with self.subTest(word=word):
                self.assertIn(word, self.page)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
