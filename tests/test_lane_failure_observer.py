"""F14 tail: every registered lane's failures reach the lane failure ledger.

Twenty-three lanes write the ledger through their own ``LaneFailureBudget``;
the rest are recorded by the writer's :class:`LaneOutcomeObserver`.  These
tests pin both halves: the registry says which is which for every lane, and
the observer turns what a budget-less lane already says into ledger rows --
and writes nothing for a lane that is merely quiet.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from dalton_core.lane_failure_ledger import LaneFailureLedger, default_path, summarise_events
from dalton_core.lane_failure_observer import LaneOutcomeObserver
from dalton_core.lane_registry import failure_ledger_coverage, tick_lanes

ACN = "company:sec-cik:0001467373"
IBM = "company:sec-cik:0000051143"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


class CoverageTests(unittest.TestCase):
    def test_every_driven_lane_reaches_the_ledger_one_way_or_the_other(self) -> None:
        coverage = failure_ledger_coverage()
        self.assertEqual(set(coverage), {spec.driver_key for spec in tick_lanes()})
        self.assertTrue(all(row["mode"] in {"own", "observed"} for row in coverage.values()))
        own = {key for key, row in coverage.items() if row["mode"] == "own"}
        observed = {key for key, row in coverage.items() if row["mode"] == "observed"}
        # The lanes that construct their own budget, by the name they write.
        self.assertEqual(coverage["company_model_spec"]["ledger_lane"], "mission_model_spec")
        self.assertEqual(coverage["mission_crowd_sources"]["ledger_lane"], "crowd_source")
        self.assertEqual(coverage["mission_hkex_filings"]["ledger_lane"], "hkex_filings")
        self.assertIn("mission_statements", observed)
        self.assertIn("mission_sec_quarters", observed)
        self.assertIn("mission_source_discovery", observed)
        self.assertEqual(len(own) + len(observed), len(coverage))

    def test_an_own_ledger_name_is_one_some_module_actually_writes(self) -> None:
        import re

        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (Path(__file__).resolve().parents[1] / "src" / "dalton_core").glob("*.py"))
        written = set(re.findall(r'lane_budget\(\s*"([a-z_]+)"', source))
        written |= set(re.findall(r'^DRIVER_KEY = "([a-z_]+)"', source, re.M))
        written |= {"hkex_filings", "document_extraction"}
        for key, row in failure_ledger_coverage().items():
            if row["mode"] == "own":
                self.assertIn(row["ledger_lane"], written, key)

    def test_every_ledger_lane_has_a_chinese_label(self) -> None:
        from dalton_core.cockpit_plane import REGISTRY_LANE_LABELS

        for key, row in failure_ledger_coverage().items():
            self.assertIn(row["ledger_lane"], REGISTRY_LANE_LABELS, key)

    def test_the_panel_says_how_many_lanes_it_sees(self) -> None:
        from dalton_core.cockpit_plane import _failure_ledger_coverage

        panel = _failure_ledger_coverage()
        self.assertTrue(panel["available"])
        self.assertEqual(panel["total"], len(tick_lanes()))
        self.assertEqual(panel["uncovered"], [])
        self.assertEqual(panel["own"] + panel["observed"], panel["total"])
        self.assertIn("失败账本", panel["note"])
        self.assertTrue(all(row["lane_label"] != row["lane"] for row in panel["observed_lanes"]))


class ObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.clock = Clock()
        self.observer = LaneOutcomeObserver(self.state, clock=self.clock)

    def events(self) -> list[dict]:
        path = default_path(self.state)
        if not path.is_file():
            return []
        with LaneFailureLedger(path, read_only=True) as ledger:
            return ledger.events()

    def statements(self, *settled: dict) -> dict:
        return {"status": "idle", "settled": list(settled)}

    def test_a_quiet_lane_writes_nothing(self) -> None:
        for status in ("idle", "duplicate", "waiting", "unconfigured", "ungranted", "launched"):
            self.observer.observe("dispatch_mission_statements", {"status": status})
        # The quiet holds live lanes report thousands of times a day, and the
        # writer's own governance/quota short-circuit.
        for _ in range(5):
            self.observer.observe("dispatch_research_plan", {
                "status": "held", "reason": "研究状态没有变化，没有必要重新决策"})
            self.observer.observe("dispatch_scenario_proposal", {
                "status": "held", "reason": "两次起草之间的最短间隔未到"})
            self.observer.observe("dispatch_sales_notes_feed", {
                "status": "held", "reason_code": "lane_governance_hold",
                "reason": "governance record is not approved"})
        self.assertEqual(self.events(), [])

    def test_a_lane_with_its_own_budget_is_not_recorded_twice(self) -> None:
        self.assertIsNone(self.observer.observe(
            "dispatch_company_dossier", {"status": "failed", "reason": "ConnectionError"}))
        self.assertIsNone(self.observer.observe("record_claim", {"status": "failed"}))
        self.assertEqual(self.events(), [])

    def test_repeated_company_failures_are_held_once(self) -> None:
        failed = {"company_ref": IBM, "outcome": "failed",
                  "failure_reason": "AttributeError: 'list' object has no attribute 'xbrl'"}
        for _ in range(5):
            self.observer.observe("dispatch_mission_statements", self.statements(failed))
        rows = self.events()
        self.assertEqual([row["event"] for row in rows], ["held"])
        self.assertEqual(rows[0]["lane"], "mission_statements")
        self.assertEqual(rows[0]["item_key"], IBM)

    def test_a_source_outage_parks_and_a_success_resumes(self) -> None:
        outage = {"company_ref": ACN, "outcome": "failed",
                  "failure_reason": "ConnectionError: sec.gov did not answer"}
        self.observer.observe("dispatch_mission_sec_quarters", self.statements(outage))
        self.observer.observe("dispatch_mission_sec_quarters", self.statements(outage))
        backlog = summarise_events(self.events())
        self.assertEqual(backlog["parked_items"], 1)
        self.assertEqual(backlog["dependencies"][0]["dependency"], "sec")
        self.assertEqual(len(self.events()), 1)  # the repeat is the same park
        self.observer.observe("dispatch_mission_sec_quarters", self.statements(
            {"company_ref": ACN, "outcome": "succeeded"}))
        self.assertEqual(summarise_events(self.events())["parked_items"], 0)

    def test_a_lane_level_failure_and_its_recovery(self) -> None:
        self.observer.observe("dispatch_research_plan", {
            "status": "failed", "reason": "model_unavailable: no route"})
        backlog = summarise_events(self.events())
        self.assertEqual(backlog["parked_items"], 1)
        self.assertEqual(backlog["dependencies"][0]["items"][0]["item_key"], "lane")
        self.observer.observe("dispatch_research_plan", {"status": "busy"})
        self.assertEqual(summarise_events(self.events())["parked_items"], 1)
        self.observer.observe("dispatch_research_plan", {"status": "succeeded"})
        self.assertEqual(summarise_events(self.events())["parked_items"], 0)

    def test_a_terminal_child_is_retired_when_the_same_item_later_succeeds(self) -> None:
        refused = {"company_ref": ACN, "outcome": "failed",
                   "failure_reason": "returned no filing with XBRL"}
        self.observer.observe("dispatch_mission_statements", self.statements(refused))
        self.assertEqual(summarise_events(self.events())["terminal_count"], 1)
        self.observer.observe("dispatch_mission_statements", self.statements(
            {"company_ref": ACN, "outcome": "succeeded"}))
        self.assertEqual(summarise_events(self.events())["terminal_count"], 0)

    def test_nested_source_results_are_items_of_their_own(self) -> None:
        self.observer.observe("dispatch_mission_source_discovery", {
            "status": "idle",
            "web_search": {"status": "failed", "reason": "TimeoutError: search timed out"},
            "sec_filings_index": {"status": "idle"},
        })
        backlog = summarise_events(self.events())
        self.assertEqual(backlog["parked_items"], 1)
        self.assertEqual(backlog["dependencies"][0]["items"][0]["item_key"], "part:web_search")

    def test_a_restart_replays_the_park(self) -> None:
        self.observer.observe("dispatch_guidepoint_discovery", {
            "status": "failed", "reason": "ConnectionError: guidepoint"})
        again = LaneOutcomeObserver(self.state, clock=self.clock)
        self.assertIsNotNone(again.budget("guidepoint_discovery").parked("lane"))
        self.assertEqual(again.budget("guidepoint_discovery").dependencies(), ("guidepoint",))

    def test_a_bookkeeping_error_never_reaches_the_tick(self) -> None:
        def broken(lane: str):
            raise OSError("disk full")

        observer = LaneOutcomeObserver(self.state, budget_factory=broken)
        found = observer.observe("dispatch_mission_statements", {"status": "failed"})
        self.assertEqual(found["status"], "unrecorded:OSError")


class WriterHookTests(unittest.TestCase):
    def test_the_writer_returns_the_result_untouched_and_records_it(self) -> None:
        from dalton_core.writer_server import WriterServer

        with tempfile.TemporaryDirectory() as tmp:
            server = SimpleNamespace(state_dir=Path(tmp))
            result = {"status": "failed", "reason": "ConnectionError: edgar"}
            returned = WriterServer._observe_lane_outcome(
                server, "dispatch_mission_sec_quarters", result)
            self.assertIs(returned, result)
            with LaneFailureLedger(default_path(tmp), read_only=True) as ledger:
                rows = ledger.events()
            self.assertEqual([(row["lane"], row["event"]) for row in rows],
                             [("mission_sec_quarters", "parked")])
            # Not a lane: passes through without a ledger write.
            WriterServer._observe_lane_outcome(server, "record_claim", {"status": "failed"})
            with LaneFailureLedger(default_path(tmp), read_only=True) as ledger:
                self.assertEqual(len(ledger.events()), 1)


class DisplayTests(unittest.TestCase):
    def test_new_waiting_reasons_are_chinese_sentences(self) -> None:
        from dalton_core.cockpit_plane import _ops_item_label, _ops_waiting_reason

        self.assertIn("changedetection", _ops_waiting_reason(
            "changedetection is not answering at http://127.0.0.1:5055"))
        self.assertIn("SEC", _ops_waiting_reason("ConnectionError: sec.gov"))
        self.assertIn("超时", _ops_waiting_reason("TimeoutError: timed out"))
        self.assertEqual(_ops_item_label("lane", {}), "整条流程")
        self.assertEqual(_ops_item_label("ir-pages", {}), "投资者关系页面监测")


if __name__ == "__main__":
    unittest.main()
