"""C2: the tick ledger the Core did not keep, and the four readers Q2 needs."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.bounded_planner_driver import (
    BoundedPlannerDriver,
    BoundedPlannerDriverConfig,
)
from dalton_core.lane_registry import RESERVED_DRIVER_KEYS
from dalton_core.tick_ledger import (
    RETENTION_DAYS,
    TickLedger,
    TickLedgerError,
    bounded_counts,
    status_word,
)


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def summary(**lanes) -> dict:
    return {
        "status": "idle", "active_loop_count": 0, "probes_executed": 0,
        "executed": [], "skipped": [],
        "mission_sec_dispatch": {"status": "idle"},
        "forecast_reconciliation": {"status": "idle"},
        **lanes,
    }


class TickLedgerWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = TickLedger(clock=lambda: NOW)
        self.addCleanup(self.ledger.close)

    def test_a_tick_writes_one_row_and_one_row_per_lane(self) -> None:
        recorded = self.ledger.append_tick(
            summary(
                research_task={"status": "skipped:pool_exhausted"},
                initial_screen={"status": "idle", "drafted": 0},
            ),
            started_at=NOW - timedelta(seconds=30), ended_at=NOW,
            lane_operations={"research_task": "dispatch_research_task"},
            lane_pools={"research_task": "adhoc"},
        )
        self.assertEqual(recorded["status"], "recorded")
        self.assertEqual(recorded["lane_count"], 2)
        ticks = self.ledger.ticks(now=NOW)["ticks"]
        self.assertEqual(len(ticks), 1)
        lanes = {lane["driver_key"]: lane for lane in ticks[0]["lanes"]}
        self.assertEqual(lanes["research_task"]["pool"], "adhoc")
        self.assertEqual(lanes["research_task"]["lane_operation"],
                         "dispatch_research_task")
        self.assertTrue(lanes["research_task"]["pool_exhausted"])
        self.assertEqual(lanes["research_task"]["status_word"], "skipped")
        self.assertTrue(lanes["initial_screen"]["idle"])

    def test_the_ticks_own_keys_are_not_lanes(self) -> None:
        self.ledger.append_tick(
            summary(statements={"status": "idle"}),
            started_at=NOW - timedelta(seconds=1), ended_at=NOW,
        )
        lanes = {lane["driver_key"]
                 for lane in self.ledger.ticks(now=NOW)["ticks"][0]["lanes"]}
        self.assertEqual(lanes, {"statements"})
        self.assertFalse(lanes & RESERVED_DRIVER_KEYS)

    def test_a_tick_is_idle_only_when_every_lane_is(self) -> None:
        self.ledger.append_tick(
            summary(a={"status": "idle"}, b={"status": "held"}),
            started_at=NOW - timedelta(seconds=2), ended_at=NOW)
        self.ledger.append_tick(
            summary(a={"status": "idle"}, b={"status": "launched"}),
            started_at=NOW - timedelta(seconds=1), ended_at=NOW)
        ticks = self.ledger.ticks(now=NOW)["ticks"]
        self.assertEqual([tick["idle"] for tick in ticks], [True, False])

    def test_appending_the_same_tick_twice_does_not_double_count(self) -> None:
        started = NOW - timedelta(seconds=5)
        first = self.ledger.append_tick(summary(a={"status": "idle"}),
                                        started_at=started, ended_at=NOW)
        again = self.ledger.append_tick(summary(a={"status": "idle"}),
                                        started_at=started, ended_at=NOW)
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["tick_id"], first["tick_id"])
        self.assertEqual(self.ledger.ticks(now=NOW)["tick_count"], 1)

    def test_a_lane_that_reported_nothing_is_missing_not_idle(self) -> None:
        self.assertEqual(status_word(None), "missing")
        self.assertEqual(status_word("skipped:pool_exhausted"), "skipped")
        self.ledger.append_tick(summary(a={}), started_at=NOW, ended_at=NOW)
        lane = self.ledger.ticks(now=NOW)["ticks"][0]["lanes"][0]
        self.assertEqual(lane["status_word"], "missing")
        self.assertFalse(lane["idle"])

    def test_the_counts_a_lane_reported_are_kept_and_bounded(self) -> None:
        counts = bounded_counts({
            "status": "launched", "admitted": 2, "refused": ["a", "b"],
            "ticket_ref": "ticket:1", "envelope": {"deeply": {"nested": 1}},
            "prose": "x" * 500, "reason": "y" * 500,
        })
        self.assertEqual(counts["admitted"], 2)
        self.assertEqual(counts["refused"], 2)
        self.assertEqual(counts["ticket_ref"], "ticket:1")
        self.assertNotIn("envelope", counts)
        self.assertNotIn("prose", counts)
        self.assertEqual(len(counts["reason"]), 200)

    def test_a_read_only_ledger_refuses_to_be_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tick-ledger.sqlite"
            TickLedger(path).close()
            with TickLedger(path, read_only=True) as ledger:
                with self.assertRaises(TickLedgerError):
                    ledger.append_tick(summary(), started_at=NOW, ended_at=NOW)


class TickLedgerReaderTests(unittest.TestCase):
    """Two synthetic weeks: a quiet one, then one with a stuck lane."""

    def setUp(self) -> None:
        self.ledger = TickLedger(clock=lambda: NOW)
        self.addCleanup(self.ledger.close)
        self.end = datetime(2026, 9, 9, 23, 0, tzinfo=timezone.utc)
        start = self.end.replace(hour=0, minute=0) - timedelta(days=13)
        for day in range(14):
            second_week = day >= 7
            # The day ledger's per-day spend, which starts again at midnight;
            # the tick row keeps the cumulative and the ledger derives the
            # delta, so a week can be summed without double counting.
            cumulative = {"coverage": 0, "adhoc": 0}
            for tick in range(4):
                moment = start + timedelta(days=day, hours=tick * 4)
                # Week one: the extraction lane works twice a day. Week two: it
                # has been unconfigured since the seventh day, and the ad-hoc
                # pool runs out every afternoon.
                extraction = ("unconfigured" if second_week
                              else ("launched" if tick < 2 else "idle"))
                research = ("skipped:pool_exhausted"
                            if second_week and tick >= 2 else "idle")
                if extraction == "launched":
                    cumulative["coverage"] += 100_000
                if research == "idle" and tick == 0:
                    cumulative["adhoc"] += 25_000
                self.ledger.append_tick(
                    summary(
                        document_extraction={"status": extraction},
                        research_task={"status": research},
                    ),
                    started_at=moment, ended_at=moment + timedelta(seconds=20),
                    pool_spend=dict(cumulative),
                    lane_operations={
                        "document_extraction": "dispatch_document_extraction",
                        "research_task": "dispatch_research_task"},
                    lane_pools={"document_extraction": "coverage",
                                "research_task": "adhoc"},
                )

    def test_the_idle_ratio_is_a_number_instead_of_available_false(self) -> None:
        ratio = self.ledger.idle_ratio(14, now=self.end)
        self.assertTrue(ratio["available"])
        self.assertEqual(ratio["ticks"], 56)
        # Week one: two idle ticks a day. Week two: never idle, because the
        # extraction lane is unconfigured rather than quiet.
        self.assertEqual(ratio["idle_ticks"], 14)
        self.assertEqual(ratio["ratio"], 0.25)
        self.assertEqual(ratio["idle_by_lane"]["document_extraction"], 0.25)

    def test_one_week_and_two_weeks_are_different_questions(self) -> None:
        week = self.ledger.idle_ratio(7, now=self.end)
        self.assertEqual(week["ticks"], 28)
        self.assertEqual(week["idle_ticks"], 0)
        explicit = self.ledger.idle_ratio(
            ((self.end.date() - timedelta(days=13)).isoformat(),
             (self.end.date() - timedelta(days=7)).isoformat()),
            now=self.end,
        )
        self.assertEqual(explicit["ticks"], 28)
        self.assertEqual(explicit["idle_ticks"], 14)

    def test_a_stuck_lane_is_named_with_how_long_it_has_been_stuck(self) -> None:
        stalls = self.ledger.lane_stalls(14, now=self.end)["lanes"]
        extraction = stalls["document_extraction"]
        self.assertEqual(extraction["ticks"], 56)
        self.assertEqual(extraction["stalls"], 28)
        self.assertEqual(extraction["longest_stall_run"], 28)
        self.assertEqual(extraction["last_status"], "unconfigured")
        self.assertEqual(extraction["pool"], "coverage")
        # A spent pool is not a stall: it is the budget deciding, not a fault.
        research = stalls["research_task"]
        self.assertEqual(research["stalls"], 0)
        self.assertEqual(research["pool_exhausted_ticks"], 14)

    def test_spend_is_attributed_by_pool_rather_than_by_prefix(self) -> None:
        spend = self.ledger.spend_by_pool(14, now=self.end)
        self.assertTrue(spend["available"])
        # Coverage only spent in week one -- in week two the lane that spends
        # it was unconfigured, which is the difference between a quiet lane
        # and a broken one that the ledger now records.
        self.assertEqual(spend["pools"]["coverage"], 7 * 2 * 100_000)
        self.assertEqual(spend["pools"]["adhoc"], 14 * 25_000)
        self.assertEqual(spend["total_micros"], 1_400_000 + 350_000)
        self.assertEqual(len(spend["by_day"]), 14)

    def test_a_window_with_no_ticks_says_so_rather_than_reporting_zero(self) -> None:
        empty = TickLedger(clock=lambda: NOW)
        self.addCleanup(empty.close)
        for reader in (empty.idle_ratio, empty.lane_stalls, empty.spend_by_pool):
            answer = reader(7, now=self.end)
            self.assertFalse(answer["available"])
            self.assertIn("tick", answer["reason"])

    def test_no_reader_looks_further_back_than_the_retention_window(self) -> None:
        window = self.ledger.idle_ratio(RETENTION_DAYS + 500, now=self.end)["window"]
        floor = (self.end.date() - timedelta(days=RETENTION_DAYS - 1)).isoformat()
        self.assertEqual(window["since"], floor)
        with self.assertRaises(TickLedgerError):
            self.ledger.idle_ratio("last tuesday", now=self.end)


class FakeWriterClient:
    """Answers the tick's RPCs with fixed lane results."""

    def __init__(self, lanes: dict[str, dict]) -> None:
        self.lanes = lanes
        self.calls: list[str] = []

    def call(self, operation: str, params: dict) -> dict:
        self.calls.append(operation)
        if operation == "bounded_planner_active_loops":
            return {"loops": []}
        return self.lanes.get(operation, {"status": "idle"})


class DriverTickLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def _driver(self, lanes: dict[str, dict], *,
                ledger_path: Path | None = None) -> BoundedPlannerDriver:
        config = BoundedPlannerDriverConfig(
            writer_socket=self.root / "writer.sock",
            token_config=self.root / "tokens.json",
            scheduler_db=self.root / "scheduler.sqlite",
            user_agent="Dalton Test", max_response_bytes=1024,
            timeout_seconds=5.0, max_probes_per_tick=1, filed_window_days=400,
            observation_mandate_version_ref=None,
            doctrine_pack_version_ref=None, doctrine_pack_version_hash=None,
            planner_routing_policy_ref=None, planner_credential_slot_refs=None,
            planner_model_router_db=None, planner_broker_socket=None,
            planner_broker_auth_key=None,
            planner_broker_client_id="client:dalton-core",
            planner_expected_agent_id="chem", planner_max_cost_usd=0.5,
        )
        return BoundedPlannerDriver(
            config, client=FakeWriterClient(lanes), clock=lambda: NOW,
            tick_ledger_path=ledger_path,
        )

    def test_a_tick_records_itself_and_says_that_it_did(self) -> None:
        driver = self._driver({
            "dispatch_research_task": {"status": "skipped:pool_exhausted"},
        })
        result = driver.run_once()
        self.assertEqual(result["tick_ledger"]["status"], "recorded")
        self.assertEqual(result["tick_ledger"]["day"], "2026-09-09")
        with TickLedger(self.root / "tick-ledger.sqlite", read_only=True) as ledger:
            ticks = ledger.ticks(now=NOW)["ticks"]
        self.assertEqual(len(ticks), 1)
        lanes = {lane["driver_key"]: lane for lane in ticks[0]["lanes"]}
        # Every registered tick lane appears, each with the pool it drinks
        # from -- assigned centrally, so no lane module had to be edited.
        self.assertEqual(lanes["research_task"]["pool"], "adhoc")
        self.assertTrue(lanes["research_task"]["pool_exhausted"])
        self.assertEqual(lanes["document_extraction"]["pool"], "coverage")

    def test_the_ledger_failing_is_reported_and_never_fails_the_tick(self) -> None:
        # A directory where the ledger file should be: openable by nothing.
        broken = self.root / "not-a-file"
        broken.mkdir()
        result = self._driver({}, ledger_path=broken).run_once()
        self.assertTrue(result["tick_ledger"]["status"].startswith("unrecorded:"))
        self.assertEqual(result["status"], "idle")
        self.assertIn("research_task", result)

    def test_the_ledger_key_is_reserved_so_no_lane_can_overwrite_it(self) -> None:
        self.assertIn("tick_ledger", RESERVED_DRIVER_KEYS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
