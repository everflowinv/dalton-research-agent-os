"""P12e: a discovery tick returns before the writer gives up on it.

The acquisition loop was bounded per acquisition and not in total: twelve waits
of ninety seconds is eighteen minutes inside a request the writer abandons
after thirty seconds. Live, that reported the whole discovery lane as
unavailable:RemoteError for hours -- the acquisitions were real work, but the
tick's budget and status never reached the cockpit, so the lane looked dead and
the owner could not see what it was spending.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from dalton_core import mission_source_discovery as m


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TickBudgetTests(unittest.TestCase):
    def coordinator(self, *, per_tick=12, budget=20.0, wait_cost=5.0, launches=99):
        clock = FakeClock()
        waits: list[float] = []

        class Launcher:
            def wait(self_inner, timeout=None):
                waits.append(timeout)
                clock.now += wait_cost
                return 0

        coordinator = object.__new__(m.MissionSourceDiscoveryCoordinator)
        coordinator.acquisitions_per_tick = per_tick
        coordinator.acquisition_wait_seconds = 90.0
        coordinator.tick_budget_seconds = budget
        coordinator.acquisition_launcher = Launcher()
        coordinator.remaining_launches = launches
        return coordinator, clock, waits

    def run_loop(self, coordinator, clock, launches):
        """The real loop, not a copy of it."""

        taken = {"n": 0}

        def launch_acquisition():
            taken["n"] += 1
            return {"status": "launched" if taken["n"] <= launches else "idle"}

        coordinator.launch_acquisition = launch_acquisition
        coordinator.settle_documents = lambda: ["settled"]
        with patch.object(m, "_monotonic", clock):
            acquisitions, out_of_time, settled = coordinator._acquire_within_budget([])
        self.settled = settled
        return acquisitions, out_of_time

    def test_the_loop_stops_at_its_deadline_rather_than_running_for_minutes(self):
        c, clock, _ = self.coordinator(budget=20.0, wait_cost=5.0)
        acquisitions, out_of_time = self.run_loop(c, clock, launches=99)
        self.assertTrue(out_of_time)
        self.assertLessEqual(clock.now, 25.0)
        self.assertEqual(len(acquisitions), 4)

    def test_no_single_wait_can_outlast_the_remaining_budget(self):
        # The per-acquisition wait is 90s; the tick's whole budget is 20s.
        c, clock, waits = self.coordinator(budget=20.0, wait_cost=5.0)
        self.run_loop(c, clock, launches=99)
        self.assertTrue(waits)
        for timeout in waits:
            self.assertLessEqual(timeout, 20.0)
        self.assertEqual(waits[0], 20.0)
        self.assertEqual(waits[-1], 5.0)

    def test_the_tick_budget_is_inside_the_writer_request_timeout(self):
        from dalton_core.writer_server import STORE_REQUEST_TIMEOUT

        self.assertLess(m.TICK_BUDGET_SECONDS, STORE_REQUEST_TIMEOUT)

    def test_a_fast_queue_still_gets_the_whole_per_tick_allowance(self):
        # The throughput win this loop exists for must survive the deadline.
        c, clock, _ = self.coordinator(per_tick=12, budget=20.0, wait_cost=0.1)
        acquisitions, out_of_time = self.run_loop(c, clock, launches=99)
        self.assertEqual(len(acquisitions), 12)
        self.assertFalse(out_of_time)

    def test_an_empty_queue_ends_the_loop_without_blaming_the_clock(self):
        c, clock, _ = self.coordinator(budget=20.0, wait_cost=0.1)
        acquisitions, out_of_time = self.run_loop(c, clock, launches=0)
        self.assertEqual([a["status"] for a in acquisitions], ["idle"])
        self.assertFalse(out_of_time)

    def test_each_finished_child_is_settled_before_the_next_one_starts(self):
        # Otherwise the next launch sees a document still marked launched and
        # reports itself busy, which is the bug the in-tick wait exists to fix.
        c, clock, _ = self.coordinator(per_tick=3, budget=20.0, wait_cost=0.1)
        self.run_loop(c, clock, launches=99)
        self.assertEqual(self.settled, ["settled", "settled", "settled"])

    def test_a_launcher_that_cannot_be_waited_on_keeps_one_per_tick(self):
        c, clock, _ = self.coordinator(budget=20.0, wait_cost=0.1)
        del type(c.acquisition_launcher).wait
        acquisitions, out_of_time = self.run_loop(c, clock, launches=99)
        self.assertEqual(len(acquisitions), 1)
        self.assertFalse(out_of_time)


if __name__ == "__main__":
    unittest.main()
