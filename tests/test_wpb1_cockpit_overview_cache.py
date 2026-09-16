"""WP-B1-5: the overview stops getting slower the longer the process is up.

Two problems, one symptom.  Every sequential reader rebuilt the whole page --
two Core connections, four more databases, 6,279 run directories -- and the
ticket cache that was supposed to make the directory walk cheap grew without
a bound or an eviction, so a control process that had been up for two minutes
answered in 30 s where a fresh one answered in 5.
"""

import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dalton_core.cockpit_plane import (
    MAX_CACHED_TICKETS,
    OVERVIEW_TTL_SECONDS,
    TICKET_SCAN_TTL_SECONDS,
    SUMMARY_FIELDS_THE_COCKPIT_READS,
    CockpitPlane,
    _TicketCache,
    _trimmed_summary,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class OverviewTtlTests(unittest.TestCase):
    def plane(self, ttl=OVERVIEW_TTL_SECONDS):
        plane = CockpitPlane.__new__(CockpitPlane)
        plane._overview_condition = threading.Condition()
        plane._overview_building = False
        plane._overview_generation = 0
        plane._overview_result = None
        plane._overview_built_at = None
        plane._overview_ttl_seconds = ttl
        plane._overview_monotonic = self.clock
        return plane

    def setUp(self):
        self.clock = _Clock()
        self.builds = []

    def _builder(self, plane):
        def build():
            self.builds.append(1)
            return {"snapshot": len(self.builds)}

        plane._build_overview = build
        return plane

    def test_a_second_reader_inside_the_window_gets_the_same_snapshot(self):
        plane = self._builder(self.plane())
        self.assertEqual(plane.overview(), {"snapshot": 1})
        self.clock.advance(OVERVIEW_TTL_SECONDS / 2)
        self.assertEqual(plane.overview(), {"snapshot": 1})
        self.assertEqual(len(self.builds), 1)

    def test_a_reader_after_the_window_rebuilds(self):
        plane = self._builder(self.plane())
        plane.overview()
        self.clock.advance(OVERVIEW_TTL_SECONDS + 0.1)
        self.assertEqual(plane.overview(), {"snapshot": 2})

    def test_a_write_invalidates_the_snapshot_so_read_after_write_is_exact(self):
        plane = self._builder(self.plane())
        plane.overview()
        plane.invalidate_overview()
        # No time passed at all, and the next reader still sees the rebuild:
        # a person who just approved something must not be shown the picture
        # from before they approved it.
        self.assertEqual(plane.overview(), {"snapshot": 2})

    def test_the_default_plane_keeps_exact_read_after_write(self):
        # Every construction outside the long-lived control process -- a test,
        # a one-shot command -- takes the zero default and behaves exactly as
        # it did before the cache existed.
        plane = self._builder(self.plane(ttl=0.0))
        self.assertEqual(plane.overview(), {"snapshot": 1})
        self.assertEqual(plane.overview(), {"snapshot": 2})

    def test_a_failed_build_is_not_cached(self):
        plane = self.plane()
        calls = []

        def build():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("broken snapshot")
            return {"snapshot": 2}

        plane._build_overview = build
        with self.assertRaisesRegex(RuntimeError, "broken snapshot"):
            plane.overview()
        self.assertEqual(plane.overview(), {"snapshot": 2})

    def test_singleflight_still_coalesces_concurrent_readers(self):
        plane = self.plane()
        entered, release = threading.Event(), threading.Event()
        calls = []

        def build():
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(2))
            return {"snapshot": len(calls)}

        plane._build_overview = build
        results = []
        first = threading.Thread(target=lambda: results.append(plane.overview()))
        second = threading.Thread(target=lambda: results.append(plane.overview()))
        first.start()
        self.assertTrue(entered.wait(2))
        second.start()
        release.set()
        first.join(2)
        second.join(2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(results, [{"snapshot": 1}, {"snapshot": 1}])

    def test_the_serving_process_opts_in(self):
        # agenda_control builds the one plane that serves HTTP and is the only
        # construction that turns the cache on.
        source = Path("src/dalton_core/agenda_control.py").read_text(encoding="utf-8")
        self.assertIn("overview_ttl_seconds=OVERVIEW_TTL_SECONDS", source)
        self.assertIn('getattr(plane, "invalidate_overview", None)', source)


class SummaryTrimTests(unittest.TestCase):
    """The allowlist is only safe while it is derived from the readers."""

    def _keys_read_by_the_cockpit(self):
        import re

        source = Path("src/dalton_core/cockpit_plane.py").read_text(encoding="utf-8")
        return (set(re.findall(r'summary\.get\(\s*"([^"]+)"', source))
                | set(re.findall(r'summary\[\s*"([^"]+)"\s*\]', source)))

    def test_every_field_a_reader_asks_for_is_still_kept(self):
        missing = self._keys_read_by_the_cockpit() - SUMMARY_FIELDS_THE_COCKPIT_READS
        self.assertEqual(
            missing, set(),
            "a view started reading a summary field the ticket cache drops; "
            "add it to SUMMARY_FIELDS_THE_COCKPIT_READS or the panel will be "
            "silently empty",
        )

    def test_the_extracted_text_is_not_kept(self):
        # The specific 126 MB: an extraction summary's body is never shown on
        # any page and was retained for the life of the process.
        for field in ("text", "extracted_text", "body", "content", "records"):
            self.assertNotIn(field, SUMMARY_FIELDS_THE_COCKPIT_READS)

    def test_a_summary_is_trimmed_to_the_allowlist(self):
        trimmed = _trimmed_summary({"status": "succeeded", "text": "x" * 10_000})
        self.assertEqual(trimmed, {"status": "succeeded"})

    def test_an_absent_or_malformed_summary_stays_none(self):
        self.assertIsNone(_trimmed_summary(None))
        self.assertIsNone(_trimmed_summary(["not", "an", "object"]))


class TicketCacheBoundsTests(unittest.TestCase):
    def setUp(self):
        self.root = TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.state = Path(self.root.name)
        self.clock = _Clock()

    def _ticket(self, lane: str, name: str, status: str = "running") -> Path:
        item = self.state / lane / name
        item.mkdir(parents=True, exist_ok=True)
        (item / "ticket.json").write_text(json.dumps({"status": status}))
        return item

    def test_a_scan_is_reused_inside_its_window(self):
        self._ticket("discoveries", "one")
        cache = _TicketCache(self.state, ttl_seconds=TICKET_SCAN_TTL_SECONDS,
                             monotonic=self.clock)
        self.assertEqual(len(cache.tickets()), 1)
        self._ticket("discoveries", "two")
        self.clock.advance(TICKET_SCAN_TTL_SECONDS / 2)
        # Three callers inside one page render used to pay for three walks of
        # 6,279 directories; now they pay for one.
        self.assertEqual(len(cache.tickets()), 1)
        self.clock.advance(TICKET_SCAN_TTL_SECONDS)
        self.assertEqual(len(cache.tickets()), 2)

    def test_the_default_cache_has_no_scan_window(self):
        self._ticket("discoveries", "one")
        cache = _TicketCache(self.state)
        self.assertEqual(len(cache.tickets()), 1)
        self._ticket("discoveries", "two")
        self.assertEqual(len(cache.tickets()), 2)

    def test_a_deleted_run_stops_being_held_in_memory(self):
        item = self._ticket("discoveries", "gone")
        cache = _TicketCache(self.state)
        cache.tickets()
        self.assertEqual(len(cache._entries), 1)
        (item / "ticket.json").unlink()
        item.rmdir()
        cache.tickets()
        self.assertEqual(cache._entries, {})

    def test_the_cache_is_capped(self):
        for index in range(6):
            self._ticket("discoveries", f"run-{index}")
        cache = _TicketCache(self.state, max_entries=4)
        # Every ticket is still returned -- the cap bounds what is remembered,
        # not what is read.
        self.assertEqual(len(cache.tickets()), 6)
        self.assertLessEqual(len(cache._entries), 4)

    def test_the_cap_is_a_real_number_and_the_window_is_shorter_than_the_overview(self):
        self.assertGreater(MAX_CACHED_TICKETS, 0)
        self.assertLess(TICKET_SCAN_TTL_SECONDS, OVERVIEW_TTL_SECONDS)

    def test_a_changed_ticket_is_re_read_when_the_window_has_passed(self):
        item = self._ticket("discoveries", "one", status="running")
        cache = _TicketCache(self.state, ttl_seconds=TICKET_SCAN_TTL_SECONDS,
                             monotonic=self.clock)
        self.assertEqual(cache.tickets()[0]["ticket"]["status"], "running")
        (item / "ticket.json").write_text(json.dumps({"status": "succeeded"}))
        self.clock.advance(TICKET_SCAN_TTL_SECONDS + 0.1)
        self.assertEqual(cache.tickets()[0]["ticket"]["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
