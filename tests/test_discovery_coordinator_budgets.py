"""The discovery tick shares its budget: a slow coordinator cannot starve the rest.

Legacy, 2026-09-25 to 09-29: the AlphaEngine reconciliation at the head of
``dispatch_mission_source_discovery`` took 14-16 s of the 20 s the AlphaEngine,
SEC and web search coordinators share, and the other two reported ``deferred:
tick budget exhausted`` every tick for four days -- while the tick ledger,
which kept only the lane's top-level word, showed an idle lane.

Hermetic: the coordinators are fakes, the clock is a counter, and nothing here
opens a socket or starts the writer.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dalton_core import mission_source_discovery as msd
from dalton_core.mission_source_discovery import (
    DISCOVERY_ROTATION_FILENAME,
    TICK_BUDGET_SECONDS,
    discovery_coordinator_order,
    discovery_coordinator_state,
)
from dalton_core.tick_ledger import TickLedger, bounded_counts
from dalton_core.writer_server import CORE_OPERATIONS, Principal, WriterServer

KEYS = ("alphaengine", "sec_filings_index", "web_search")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeCoordinator:
    """Records every call; takes ``cost`` seconds, or its whole share and more."""

    def __init__(self, name: str, clock: Clock, *, cost: float = 0.1,
                 overrun: float | None = None, source_ref: str = "source:fake") -> None:
        self.name, self.clock, self.cost, self.overrun = name, clock, cost, overrun
        self.source_ref = source_ref
        self.calls: list[dict[str, float]] = []

    def dispatch_once(self, deadline: float | None = None) -> dict:
        started = self.clock.now
        self.calls.append({"started": started, "deadline": deadline})
        if self.overrun is not None:
            # A reconciliation the deadline cannot interrupt: it runs past
            # its share, and past the op's whole budget.
            self.clock.now = started + self.overrun
            return {"status": "idle", "source_ref": self.source_ref,
                    "discovery": {"status": "deferred", "reason": "tick budget exhausted"},
                    "acquisitions_launched": 0, "tick_budget_exhausted": True}
        self.clock.now = started + self.cost
        return {"status": "launched", "source_ref": self.source_ref,
                "discovery": {"status": "launched"}, "acquisitions_launched": 1,
                "tick_budget_exhausted": False}


def _writer(root: Path) -> WriterServer:
    return WriterServer(root / "core.sqlite", root / "writer.sock", {
        "core": Principal("core", "core-token", CORE_OPERATIONS, unrestricted=True)})


def _install(writer: WriterServer, coordinators: dict[str, FakeCoordinator]) -> None:
    writer._source_discovery = coordinators.get("alphaengine")
    writer._sec_filings_source_discovery = coordinators.get("sec_filings_index")
    writer._web_source_discovery = coordinators.get("web_search")


class OrderTests(unittest.TestCase):
    def test_nobody_deferred_keeps_the_p10v_order(self) -> None:
        self.assertEqual(discovery_coordinator_order(list(KEYS)), list(KEYS))
        self.assertEqual(discovery_coordinator_order(
            ["web_search", "sec_filings_index", "alphaengine"], {"alphaengine": 0}), list(KEYS))

    def test_the_longest_deferred_goes_first(self) -> None:
        self.assertEqual(
            discovery_coordinator_order(list(KEYS), {"sec_filings_index": 1, "web_search": 2}),
            ["web_search", "sec_filings_index", "alphaengine"])
        # A tie keeps the filings index ahead of web search.
        self.assertEqual(
            discovery_coordinator_order(list(KEYS), {"sec_filings_index": 1, "web_search": 1}),
            ["sec_filings_index", "web_search", "alphaengine"])

    def test_a_coordinator_that_spent_its_share_on_reconciliation_is_deferred(self) -> None:
        self.assertEqual(discovery_coordinator_state({
            "status": "idle", "discovery": {"status": "deferred", "reason": "tick budget exhausted"},
        })["status"], "deferred")
        # One that acquired until its share ran out did work.
        state = discovery_coordinator_state({
            "status": "launched", "discovery": {"status": "deferred"},
            "acquisitions_launched": 3, "tick_budget_exhausted": True})
        self.assertEqual((state["status"], state["budget_exhausted"]), ("launched", True))


class FairShareTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.clock = Clock()
        guard = patch.object(msd, "_monotonic", self.clock)
        guard.start()
        self.addCleanup(guard.stop)

    def tick(self, writer: WriterServer) -> dict:
        started = self.clock.now
        result = writer._op_dispatch_mission_source_discovery({})
        self.clock.now = started + 300.0  # the controller's interval
        return result

    def test_each_coordinator_gets_a_share_and_an_idle_one_hands_its_time_on(self) -> None:
        writer = _writer(self.root)
        fakes = {key: FakeCoordinator(key, self.clock, cost=1.0) for key in KEYS}
        _install(writer, fakes)
        start = self.clock.now
        result = self.tick(writer)
        self.assertEqual(result["coordinator_order"], list(KEYS))
        deadlines = [fakes[key].calls[0]["deadline"] - start for key in KEYS]
        self.assertAlmostEqual(deadlines[0], TICK_BUDGET_SECONDS / 3)
        # 19 s remain after the first second, split two ways ...
        self.assertAlmostEqual(deadlines[1], 1.0 + 19.0 / 2)
        # ... and the last coordinator's share ends at the op's own deadline.
        self.assertAlmostEqual(deadlines[2], TICK_BUDGET_SECONDS)
        self.assertEqual({key: state["status"] for key, state in result["coordinators"].items()},
                         {key: "launched" for key in KEYS})
        # A healthy tick keeps no rotation state on disk.
        self.assertFalse((self.root / DISCOVERY_ROTATION_FILENAME).exists())

    def test_a_first_coordinator_that_exhausts_the_budget_cannot_starve_the_rest(self) -> None:
        writer = _writer(self.root)
        fakes = {
            "alphaengine": FakeCoordinator("alphaengine", self.clock, overrun=25.0),
            "sec_filings_index": FakeCoordinator("sec_filings_index", self.clock),
            "web_search": FakeCoordinator("web_search", self.clock),
        }
        _install(writer, fakes)
        first = self.tick(writer)
        # The first tick is the old failure: the others are not even started
        # once the op's deadline has passed, and they say why.
        self.assertEqual(first["coordinator_order"], list(KEYS))
        for key in ("sec_filings_index", "web_search"):
            self.assertEqual(fakes[key].calls, [])
            self.assertEqual(first[key]["status"], "deferred")
            self.assertEqual(first["coordinators"][key]["reason"], "tick budget exhausted")
            self.assertEqual(first["coordinators"][key]["deferred_streak"], 1)
        self.assertEqual(json.loads((self.root / DISCOVERY_ROTATION_FILENAME).read_text())[
            "deferred_streaks"], {"alphaengine": 0, "sec_filings_index": 1, "web_search": 1})
        # From the next tick on the starved ones go first, every time they
        # were starved, so neither is deferred two ticks running.
        results = [first] + [self.tick(writer) for _ in range(5)]
        self.assertEqual(results[1]["coordinator_order"],
                         ["sec_filings_index", "web_search", "alphaengine"])
        for key in ("sec_filings_index", "web_search"):
            self.assertGreaterEqual(len(fakes[key].calls), 2, key)
            streaks = [r["coordinators"][key]["deferred_streak"] for r in results]
            self.assertLessEqual(max(streaks), 1, (key, streaks))
        # AlphaEngine is started every tick too; its own reconciliation is
        # what is slow, and it is still the only thing that overruns.
        self.assertEqual(len(fakes["alphaengine"].calls), 6)

    def test_two_overrunning_coordinators_still_let_the_third_through(self) -> None:
        writer = _writer(self.root)
        fakes = {
            "alphaengine": FakeCoordinator("alphaengine", self.clock, overrun=25.0),
            "sec_filings_index": FakeCoordinator("sec_filings_index", self.clock, overrun=25.0),
            "web_search": FakeCoordinator("web_search", self.clock),
        }
        _install(writer, fakes)
        results = [self.tick(writer) for _ in range(9)]
        # Every coordinator runs in every window of three ticks.
        for key in KEYS:
            called_ticks = {int((call["started"] - 1000.0) // 300) for call in fakes[key].calls}
            for window in range(len(results) - 2):
                self.assertTrue(called_ticks & {window, window + 1, window + 2},
                                (key, sorted(called_ticks)))
        # The fast one is never deferred more than twice running.
        streaks = [r["coordinators"]["web_search"]["deferred_streak"] for r in results]
        self.assertLessEqual(max(streaks), 2, streaks)
        # And no call was ever handed a deadline past the op's budget.
        for fake in fakes.values():
            for call in fake.calls:
                tick_start = 1000.0 + 300.0 * ((call["started"] - 1000.0) // 300)
                self.assertLessEqual(call["deadline"] - tick_start, TICK_BUDGET_SECONDS + 1e-9)

    def test_the_rotation_survives_a_restart(self) -> None:
        writer = _writer(self.root)
        fakes = {
            "alphaengine": FakeCoordinator("alphaengine", self.clock, overrun=25.0),
            "sec_filings_index": FakeCoordinator("sec_filings_index", self.clock),
            "web_search": FakeCoordinator("web_search", self.clock),
        }
        _install(writer, fakes)
        self.tick(writer)
        restarted = _writer(self.root)
        _install(restarted, fakes)
        self.assertEqual(self.tick(restarted)["coordinator_order"],
                         ["sec_filings_index", "web_search", "alphaengine"])

    def test_unconfigured_coordinators_take_no_share(self) -> None:
        writer = _writer(self.root)
        fake = FakeCoordinator("web_search", self.clock)
        _install(writer, {"web_search": fake})
        start = self.clock.now
        result = self.tick(writer)
        self.assertEqual(result["status"], "unconfigured")
        self.assertEqual(result["sec_filings_index"]["status"], "unconfigured")
        self.assertEqual(result["coordinators"]["alphaengine"]["status"], "unconfigured")
        self.assertAlmostEqual(fake.calls[0]["deadline"] - start, TICK_BUDGET_SECONDS)


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)

    def _lane(self, deferred: set[str]) -> dict:
        return {
            "status": "idle", "source_ref": "source:alphaengine",
            "discovery": {"status": "idle"}, "acquisitions_launched": 0,
            "coordinators": {
                key: ({"status": "deferred", "reason": "tick budget exhausted",
                       "deferred_streak": 1, "share_seconds": 0.0}
                      if key in deferred else {"status": "idle", "deferred_streak": 0})
                for key in KEYS},
        }

    def test_a_deferred_coordinator_is_in_counts_json_and_the_lane_is_not_idle(self) -> None:
        counts = bounded_counts(self._lane({"web_search"}))
        self.assertEqual(counts["coordinators"]["web_search"],
                         {"status": "deferred", "reason": "tick budget exhausted",
                          "deferred_streak": 1, "share_seconds": 0.0})
        with TickLedger(self.state / "tick-ledger.sqlite") as ledger:
            ledger.append_tick({"status": "ok", "mission_source_discovery":
                                self._lane({"web_search"})},
                               started_at=datetime(2026, 9, 29, tzinfo=timezone.utc))
            ledger.append_tick({"status": "ok", "mission_source_discovery": self._lane(set())},
                               started_at=datetime(2026, 9, 29, 0, 5, tzinfo=timezone.utc))
            ticks = ledger.ticks(None, now=datetime(2026, 9, 29, 1, tzinfo=timezone.utc))["ticks"]
        lanes = [tick["lanes"][0] for tick in ticks]
        self.assertEqual([lane["idle"] for lane in lanes], [False, True])
        self.assertEqual(lanes[0]["counts"]["coordinators"]["web_search"]["status"], "deferred")

    def _parity_rows(self, deferred_ticks: list[set[str]]) -> dict[str, dict]:
        from dalton_core.workspace_health_parity import Environment, _discovery_coordinator_rows

        with TickLedger(self.state / "tick-ledger.sqlite") as ledger:
            start = datetime(2026, 9, 29, tzinfo=timezone.utc)
            for index, deferred in enumerate(deferred_ticks):
                ledger.append_tick({"status": "ok",
                                    "mission_source_discovery": self._lane(deferred)},
                                   started_at=start + timedelta(minutes=5 * index))
        env = Environment("test", self.state)
        self.addCleanup(env.close)
        return {row["check"]: row for row in _discovery_coordinator_rows(env)}

    def test_parity_warns_on_n_consecutive_deferred_ticks(self) -> None:
        rows = self._parity_rows([set(), {"web_search"}, {"web_search"}, {"web_search"}])
        web = rows["mission_source_discovery.web_search.deferred"]
        self.assertEqual((web["status"], web["deferred_ticks"]), ("warn", 3))
        self.assertIn("tick budget exhausted", web["detail"])
        self.assertEqual(rows["mission_source_discovery.alphaengine.deferred"]["status"], "ok")

    def test_parity_does_not_warn_on_a_broken_run(self) -> None:
        rows = self._parity_rows([{"web_search"}, {"web_search"}, set(), {"web_search"}])
        web = rows["mission_source_discovery.web_search.deferred"]
        self.assertEqual((web["status"], web["deferred_ticks"]), ("ok", 1))


if __name__ == "__main__":
    unittest.main()
