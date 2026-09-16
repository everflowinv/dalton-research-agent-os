"""WP-B1-2: the feed lanes stop enumerating and never reading.

Live, three feed lanes reported the same tick forever -- ``enumerated: 222,
launched: 0, out_of_time: true`` for the sell-side notes, ``windows: 9,
enumerated: 0, out_of_time: true`` for the prior-research corpus.  Enumerating
is a governed child process per window (and per split of a truncated window),
and a shared budget spent in order is not shared: enumeration went first and
there was never anything left to read with.
"""

import time
import unittest
from unittest import mock

from dalton_core.mission_feed_lane import (
    FEED_CHILD_MIN_TIMEOUT_SECONDS,
    FEED_CHILD_TIMEOUT_SECONDS,
    MIN_DOCUMENT_READ_SECONDS,
    READ_RESERVE_SECONDS,
    TICK_BUDGET_SECONDS,
    FeedDiscoveryCoordinator,
    feed_child_timeout_seconds,
)


class ChildTimeoutTests(unittest.TestCase):
    def test_a_child_gets_a_workable_floor_however_small_the_tick_budget(self):
        # The old cap was ``budget / 2``.  With the tick budget down at five
        # seconds that is 2.5 s, which is barely a Python process start: the
        # lane would kill healthy children and report its own scheduling as a
        # connector failure.
        self.assertEqual(feed_child_timeout_seconds(2.0), FEED_CHILD_MIN_TIMEOUT_SECONDS)
        self.assertEqual(feed_child_timeout_seconds(5.0), FEED_CHILD_MIN_TIMEOUT_SECONDS)

    def test_a_hung_child_is_still_capped(self):
        self.assertEqual(feed_child_timeout_seconds(600.0), FEED_CHILD_TIMEOUT_SECONDS)

    def test_the_ceiling_matches_the_other_host_tool_lane(self):
        from dalton_core.mission_crowd_source_lane import CROWD_CHILD_TIMEOUT_SECONDS

        self.assertEqual(FEED_CHILD_TIMEOUT_SECONDS, CROWD_CHILD_TIMEOUT_SECONDS)

    def test_no_budget_at_all_means_the_ceiling(self):
        self.assertEqual(feed_child_timeout_seconds(None), FEED_CHILD_TIMEOUT_SECONDS)


class _Coordinator(FeedDiscoveryCoordinator):
    """A coordinator with the windows fixed, so ordering is the only variable."""

    def __init__(self, spans, **kwargs):
        self._spans = spans
        super().__init__(**kwargs)

    def windows(self, *, since=None):
        return list(self._spans)


def _coordinator(spans, **kwargs):
    from dalton_core.mission_feed_lane import SALES_NOTES_SOURCE_REF

    plan = {
        "schema_version": "0.1",
        "source_ref": SALES_NOTES_SOURCE_REF,
        "lookback_days": 30,
        "body_reads_per_tick": 4,
        "companies": {},
    }
    with mock.patch("dalton_core.mission_feed_lane.validate_feed_discovery_plan",
                    side_effect=lambda value: dict(value)):
        return _Coordinator(
            spans, missions=object(), launcher=object(),
            source_ref=SALES_NOTES_SOURCE_REF, plan=plan, **kwargs,
        )


class EnumerationCursorTests(unittest.TestCase):
    SPANS = [("2026-09-10", "2026-09-16"), ("2026-09-03", "2026-09-09"),
             ("2026-08-27", "2026-09-02")]

    def test_without_a_cursor_the_newest_window_is_first(self):
        coordinator = _coordinator(self.SPANS)
        self.assertEqual(coordinator.enumeration_order(), self.SPANS)

    def test_a_cursor_resumes_at_the_next_window(self):
        coordinator = _coordinator(self.SPANS, enumeration_cursor_ref="2026-09-16")
        self.assertEqual(coordinator.enumeration_order()[0], self.SPANS[1])

    def test_the_order_wraps_rather_than_running_out(self):
        coordinator = _coordinator(self.SPANS, enumeration_cursor_ref="2026-09-02")
        self.assertEqual(coordinator.enumeration_order(), self.SPANS)

    def test_a_cursor_the_lookback_has_moved_past_starts_at_the_newest(self):
        coordinator = _coordinator(self.SPANS, enumeration_cursor_ref="2026-01-01")
        self.assertEqual(coordinator.enumeration_order(), self.SPANS)

    def test_no_windows_is_no_windows(self):
        coordinator = _coordinator([], enumeration_cursor_ref="2026-09-16")
        self.assertEqual(coordinator.enumeration_order(), [])


class BudgetShapeTests(unittest.TestCase):
    def test_reading_keeps_a_reserve_the_enumeration_cannot_spend(self):
        self.assertGreater(READ_RESERVE_SECONDS, 0.0)
        self.assertLess(READ_RESERVE_SECONDS, TICK_BUDGET_SECONDS)

    def test_a_document_read_has_a_floor(self):
        self.assertGreater(MIN_DOCUMENT_READ_SECONDS, 0.0)

    def test_the_enumeration_stops_before_the_tick_does(self):
        coordinator = _coordinator([], tick_budget_seconds=TICK_BUDGET_SECONDS)
        started = time.monotonic()
        deadline = started + coordinator.tick_budget_seconds
        enumeration_deadline = max(started + 0.5, deadline - READ_RESERVE_SECONDS)
        self.assertLess(enumeration_deadline, deadline)


class WriterBudgetTests(unittest.TestCase):
    def test_the_lane_takes_the_smaller_of_its_own_budget_and_the_writers(self):
        from dalton_core.writer_server import lane_budget_remaining

        server = mock.Mock()
        server._lane_deadline = time.monotonic() + 3.0
        remaining = lane_budget_remaining(server)
        self.assertLess(min(TICK_BUDGET_SECONDS, remaining), TICK_BUDGET_SECONDS)

    def test_outside_the_writer_the_lane_keeps_its_own_budget(self):
        from dalton_core.writer_server import lane_budget_remaining

        self.assertIsNone(lane_budget_remaining(object()))


if __name__ == "__main__":
    unittest.main()
