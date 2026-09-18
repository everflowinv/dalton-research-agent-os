"""WP-I: the feed lanes stop paying for the same enumeration every tick.

Live, after B1 split the enumeration and read budgets and added a window
cursor, both human feeds still reported the same tick every five minutes::

    sales_notes_feed|busy|{"budget_seconds":5.0,"enumerated":211,
      "enumerations":1,"launched":0,"out_of_time":true,
      "over_budget_seconds":5.65,...}

The arithmetic is the whole story.  The writer gives a lane five seconds
(``writer_server.LANE_SOFT_BUDGET_SECONDS``); one enumeration is a governed
child process over a local corpus and costs about four of them; what is left
does not fit a document read, so the tick either overran or read nothing --
and the next tick threw the listing away and bought it again.

Two changes.  The listing is cached on disk, keyed by its window and
fingerprinted by what would make it wrong, so a tick that finds a live entry
spends its whole budget reading.  And a tick that has just paid for a listing
it could keep stops rather than overrunning, because its successor is now
cheap by exactly what it spent.
"""

import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from dalton_core import mission_feed_lane
from dalton_core.mission_feed_lane import (
    ENUMERATION_CACHE_SCHEMA_VERSION,
    ENUMERATION_CACHE_TTL_SECONDS,
    MIN_DOCUMENT_READ_SECONDS,
    SALES_NOTES_SOURCE_REF,
    FeedDiscoveryCoordinator,
    FeedEnumerationCache,
)

WINDOW = {"since": "2026-09-03", "until": "2026-09-16"}
OBSERVATIONS = [{"notes": [{"document_ref": "sales-note:1"}], "next_cursor": None}]


def _at(moment):
    return lambda: moment


class EnumerationCacheFileTests(unittest.TestCase):
    """The sidecar itself: it keeps, it expires, and it never raises."""

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tickets" / ".enumeration-cache.json"
        self.now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)

    def cache(self, **kwargs):
        kwargs.setdefault("clock", lambda: self.now)
        return FeedEnumerationCache(self.path, **kwargs)

    def test_a_kept_listing_comes_back_to_a_later_process(self) -> None:
        self.cache().put(fingerprint="fp", observations=OBSERVATIONS, **WINDOW)
        # A second instance, because the point of the sidecar is that a writer
        # restart does not put the lane back to enumerating every tick.
        self.assertEqual(
            self.cache().get(fingerprint="fp", **WINDOW), OBSERVATIONS
        )
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_a_different_fingerprint_is_a_miss(self) -> None:
        self.cache().put(fingerprint="fp", observations=OBSERVATIONS, **WINDOW)
        self.assertIsNone(self.cache().get(fingerprint="other", **WINDOW))

    def test_a_different_window_is_a_miss(self) -> None:
        self.cache().put(fingerprint="fp", observations=OBSERVATIONS, **WINDOW)
        self.assertIsNone(self.cache().get(
            since="2026-08-20", until="2026-09-02", fingerprint="fp"))

    def test_a_listing_older_than_the_ttl_is_a_miss(self) -> None:
        self.cache().put(fingerprint="fp", observations=OBSERVATIONS, **WINDOW)
        later = self.now + timedelta(seconds=ENUMERATION_CACHE_TTL_SECONDS + 1)
        self.assertIsNone(
            FeedEnumerationCache(self.path, clock=_at(later)).get(
                fingerprint="fp", **WINDOW)
        )
        # ...and one inside it is not.
        inside = self.now + timedelta(seconds=ENUMERATION_CACHE_TTL_SECONDS - 1)
        self.assertEqual(
            FeedEnumerationCache(self.path, clock=_at(inside)).get(
                fingerprint="fp", **WINDOW),
            OBSERVATIONS,
        )

    def test_a_clock_that_went_backwards_is_a_miss_not_a_crash(self) -> None:
        self.cache().put(fingerprint="fp", observations=OBSERVATIONS, **WINDOW)
        earlier = self.now - timedelta(hours=2)
        self.assertIsNone(
            FeedEnumerationCache(self.path, clock=_at(earlier)).get(
                fingerprint="fp", **WINDOW)
        )

    def test_a_forgotten_window_is_enumerated_again(self) -> None:
        cache = self.cache()
        cache.put(fingerprint="fp", observations=OBSERVATIONS, **WINDOW)
        cache.forget(**WINDOW)
        self.assertIsNone(self.cache().get(fingerprint="fp", **WINDOW))

    def test_a_corrupt_sidecar_is_a_miss_not_an_outage(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.cache().get(fingerprint="fp", **WINDOW))
        # ...and it is overwritten by the next honest enumeration.
        self.cache().put(fingerprint="fp", observations=OBSERVATIONS, **WINDOW)
        self.assertEqual(self.cache().get(fingerprint="fp", **WINDOW), OBSERVATIONS)

    def test_a_sidecar_from_another_schema_is_ignored(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"schema_version": "9.9", "windows": {
            "2026-09-03|2026-09-16": {
                "since": "2026-09-03", "until": "2026-09-16", "fingerprint": "fp",
                "cached_at": self.now.isoformat(), "observations": OBSERVATIONS,
            }}}), encoding="utf-8")
        self.assertIsNone(self.cache().get(fingerprint="fp", **WINDOW))

    def test_a_sidecar_larger_than_the_bound_is_ignored(self) -> None:
        self.cache().put(fingerprint="fp", observations=OBSERVATIONS, **WINDOW)
        self.assertIsNone(
            FeedEnumerationCache(
                self.path, max_bytes=8, clock=lambda: self.now
            ).get(fingerprint="fp", **WINDOW)
        )

    def test_the_oldest_windows_are_dropped_first(self) -> None:
        cache = self.cache(max_windows=2)
        for index in range(3):
            cache.clock = _at(self.now + timedelta(minutes=index))
            cache.put(since=f"2026-09-0{index + 1}", until=f"2026-09-0{index + 1}",
                      fingerprint="fp", observations=OBSERVATIONS)
        kept = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(kept["schema_version"], ENUMERATION_CACHE_SCHEMA_VERSION)
        self.assertEqual(
            sorted(kept["windows"]), ["2026-09-02|2026-09-02", "2026-09-03|2026-09-03"]
        )

    def test_a_launcher_without_state_has_no_cache_and_that_is_fine(self) -> None:
        self.assertIsNone(FeedEnumerationCache.for_launcher(object()))

    def test_the_sidecar_sits_beside_the_feed_tickets(self) -> None:
        from dalton_core.feed_launcher import SalesNotesFeedLauncher

        launcher = mock.Mock()
        launcher.enumeration_cache_path = self.path
        self.assertEqual(FeedEnumerationCache.for_launcher(launcher).path, self.path)
        self.assertEqual(
            SalesNotesFeedLauncher.ENUMERATION_CACHE_FILENAME,
            ".enumeration-cache.json",
        )


# -- the lane -----------------------------------------------------------


class _Monotonic:
    """A clock the test advances, so a four-second child costs no real time."""

    def __init__(self, started: float = 1_000.0) -> None:
        self.now = started

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Lane(FeedDiscoveryCoordinator):
    """The live sell-side shape: one fortnight, 211 notes, a slow listing."""

    def __init__(self, *, spans, notes, clock, enumeration_seconds,
                 read_seconds, held=(), **kwargs):
        self._spans = list(spans)
        self._notes = notes
        self._clock = clock
        self._enumeration_seconds = enumeration_seconds
        self._read_seconds = read_seconds
        self._held = set(held)
        self.enumerator_calls = 0
        self.documents_read: list[str] = []
        super().__init__(**kwargs)

    def windows(self, *, since=None):
        return list(self._spans)

    def settle_documents(self):
        return []

    def documents_in_authority(self):
        return set(self._held)

    def enumerate_via_runner(self, *, since, until):
        self.enumerator_calls += 1
        self._clock.advance(self._enumeration_seconds)
        return {
            "since": since, "until": until, "next_cursor": None,
            "notes": [
                {"document_ref": f"sales-note:{until}:{index}", "subject": "ACME"}
                for index in range(self._notes)
            ],
        }

    def headers_by_document(self, observation):
        return {note["document_ref"]: note for note in observation["notes"]}

    def triage(self, observation, universe):
        return {
            "read_queue": [note["document_ref"] for note in observation["notes"]],
            "header_company": {},
        }

    def _resolve_one(self, *, document_ref, **kwargs):
        self._clock.advance(self._read_seconds)
        self.documents_read.append(document_ref)
        return {
            "document_ref": document_ref, "ticket_ref": f"ticket:feed:{document_ref}",
            "outcome": "company", "company_refs": ["company:acme"],
            "industry_terms": [], "reason": None, "records": ["record:1"],
        }


class LaneCacheTests(unittest.TestCase):
    """One tick pays for the listing; every tick after it reads."""

    SPANS = [("2026-09-03", "2026-09-16")]
    NOTES = 211

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tickets" / ".enumeration-cache.json"
        self.wall = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        self.monotonic = _Monotonic()
        patch = mock.patch.object(mission_feed_lane, "time", self.monotonic)
        patch.start()
        self.addCleanup(patch.stop)

    def cache(self, **kwargs):
        return FeedEnumerationCache(self.path, clock=lambda: self.wall, **kwargs)

    def lane(self, *, budget=5.0, enumeration_seconds=4.0, read_seconds=0.3,
             cache=True, spans=None, notes=None, held=()):
        plan = {
            "schema_version": "0.1", "content_hash": "plan-hash",
            "source_ref": SALES_NOTES_SOURCE_REF, "lookback_days": 30,
            "body_reads_per_tick": 200, "companies": {},
        }
        with mock.patch.object(mission_feed_lane, "validate_feed_discovery_plan",
                               side_effect=lambda value: dict(value)):
            return _Lane(
                spans=self.SPANS if spans is None else spans,
                notes=self.NOTES if notes is None else notes,
                clock=self.monotonic,
                enumeration_seconds=enumeration_seconds,
                read_seconds=read_seconds, held=held,
                missions=object(), launcher=object(),
                source_ref=SALES_NOTES_SOURCE_REF, plan=plan,
                runner=object(), enumerator=object(),
                tick_budget_seconds=budget,
                enumeration_cache=self.cache() if cache else None,
            )

    def tick(self, lane):
        # A fresh tick starts where the last one left the clock, exactly as a
        # controller five minutes later would -- the monotonic reading only
        # ever goes forwards.
        return lane.dispatch_once(universe=[{"company_ref": "company:acme"}])

    # -- the live starvation -------------------------------------------

    def test_the_first_tick_buys_the_listing_and_the_second_one_reads(self) -> None:
        first = self.lane()
        result = self.tick(first)
        # Tick one is the live envelope: 211 notes enumerated, nothing read.
        self.assertEqual(result["enumerated"], self.NOTES)
        self.assertEqual(result["enumerations"], 1)
        self.assertEqual(result["cache_hits"], 0)
        self.assertEqual(len(result["launched"]), 0)
        self.assertTrue(result["read_deferred"])
        self.assertTrue(result["out_of_time"])

        # Tick two is a new coordinator -- a writer restart, even -- and it
        # does not spawn an enumeration child at all.
        second = self.lane()
        again = self.tick(second)
        self.assertEqual(second.enumerator_calls, 0)
        self.assertEqual(again["enumerations"], 0)
        self.assertEqual(again["cache_hits"], 1)
        self.assertEqual(again["enumerated"], self.NOTES)
        self.assertGreaterEqual(len(again["launched"]), 1)
        self.assertGreaterEqual(again["read"]["read"], 1)
        self.assertEqual(again["status"], "dispatched")
        self.assertEqual(again["enumeration_seconds"], 0.0)
        self.assertGreater(again["read_seconds"], 0.0)

    def test_the_cached_tick_spends_its_whole_budget_reading(self) -> None:
        self.tick(self.lane())
        lane = self.lane()
        result = self.tick(lane)
        # Five seconds at 0.3 s a document, and not one of them spent listing.
        self.assertGreaterEqual(len(result["launched"]), 15)
        self.assertEqual(result["enumeration_seconds"], 0.0)

    def test_without_a_cache_the_lane_still_reads_rather_than_starving(self) -> None:
        # The fallback matters: deferring is only safe because the next tick
        # is cheaper.  With nowhere to keep the listing it never would be, so
        # the read keeps its floor and the tick overruns, as B1 left it.
        lane = self.lane(cache=False)
        result = self.tick(lane)
        self.assertEqual(result["enumerations"], 1)
        self.assertGreaterEqual(len(result["launched"]), 1)
        self.assertTrue(result["out_of_time"])
        self.assertFalse(result["read_deferred"])

    def test_a_cached_tick_reads_even_when_the_budget_cannot_fit_one(self) -> None:
        # A lane whose windows all came from cache has no cheaper successor,
        # so it must read on the floor rather than defer for ever.
        self.tick(self.lane())
        lane = self.lane(budget=0.5, read_seconds=MIN_DOCUMENT_READ_SECONDS + 1)
        result = self.tick(lane)
        self.assertEqual(result["enumerations"], 0)
        self.assertEqual(result["cache_hits"], 1)
        self.assertEqual(len(result["launched"]), 1)

    def test_a_read_that_cannot_finish_in_the_budget_is_not_started(self) -> None:
        # The live overrun, exactly: five seconds of budget, a get_note child
        # that takes 0.86 s, and a fifth read started at 4.9 s that finished
        # at 5.65 s -- so every tick of both feeds was reported ``busy``.
        self.tick(self.lane())
        lane = self.lane(read_seconds=0.86)
        started = self.monotonic.now
        result = self.tick(lane)
        elapsed = self.monotonic.now - started
        self.assertLessEqual(elapsed, 5.0)
        self.assertGreaterEqual(len(result["launched"]), 4)
        self.assertEqual(result["status"], "dispatched")

    def test_the_first_read_is_always_allowed_however_slow_the_last_one_was(self) -> None:
        self.tick(self.lane())
        lane = self.lane(budget=0.5, read_seconds=9.0)
        self.assertEqual(len(self.tick(lane)["launched"]), 1)

    def test_a_plan_the_owner_re_signed_invalidates_every_kept_listing(self) -> None:
        self.tick(self.lane())
        lane = self.lane()
        lane.plan["content_hash"] = "a-different-plan"
        self.tick(lane)
        self.assertEqual(lane.enumerator_calls, 1)

    SWEEP = [("2026-09-03", "2026-09-16"), ("2026-08-20", "2026-09-02")]

    def test_a_listed_window_is_read_before_a_new_one_is_bought(self) -> None:
        # The live lookback is four hundred days -- twenty-nine windows, one
        # a tick, two and a half hours to come round again.  A cursor that
        # kept marching would reach every window long after its listing had
        # expired and the cache would never once be hit.
        first = self.lane(spans=self.SWEEP)
        self.tick(first)
        self.assertEqual(first.enumeration_cursor_ref, "2026-09-16")

        second = self.lane(spans=self.SWEEP)
        second.enumeration_cursor_ref = first.enumeration_cursor_ref
        result = self.tick(second)
        self.assertEqual(second.enumerator_calls, 0)
        self.assertEqual(result["cache_hits"], 1)
        self.assertGreaterEqual(len(result["launched"]), 1)
        # Every document read came from the window already paid for.
        self.assertTrue(
            all(":2026-09-16:" in ref for ref in second.documents_read),
            second.documents_read,
        )

    def test_the_cursor_rotates_once_reading_has_run_out_of_work(self) -> None:
        first = self.lane(spans=self.SWEEP)
        self.tick(first)
        # The mission now holds every note in the window this tick listed, so
        # there is nothing left to read from it and buying the next listing is
        # the useful thing this tick can do.
        held = [f"sales-note:2026-09-16:{index}" for index in range(self.NOTES)]
        second = self.lane(spans=self.SWEEP, held=held)
        second.enumeration_cursor_ref = first.enumeration_cursor_ref
        result = self.tick(second)
        self.assertEqual(second.enumerator_calls, 1)
        self.assertEqual(result["enumerations"], 1)
        self.assertEqual(result["cache_hits"], 1)
        self.assertEqual(second.enumeration_cursor_ref, "2026-09-02")


    def test_a_replayed_listing_never_re_submits_a_recorded_document(self) -> None:
        # The listing outlives its tick, so the same document refs come back
        # every tick for an hour.  What must not come back is the *recording*:
        # a document the mission already holds is not read again, so no second
        # envelope is produced for it and nothing is re-submitted to an
        # envelope binding that already exists.
        first = self.lane(notes=3, budget=60.0)
        self.tick(first)
        self.assertEqual(len(first.documents_read), 3)
        held = [f"sales-note:2026-09-16:{index}" for index in range(3)]
        second = self.lane(notes=3, budget=60.0, held=held)
        result = self.tick(second)
        self.assertEqual(result["cache_hits"], 1)
        self.assertEqual(second.enumerator_calls, 0)
        # Listed again, read nothing, recorded nothing.
        self.assertEqual(result["enumerated"], 3)
        self.assertEqual(second.documents_read, [])
        self.assertEqual(result["read"]["read"], 0)
        self.assertEqual(result["read"]["already_held"], 3)
        self.assertEqual(result["already_bound"], 0)


class LedgerShapeTests(unittest.TestCase):
    """What the owner reads in ``tick_ledger_lanes`` after this ships."""

    def test_every_number_this_lane_reports_survives_the_ledger(self) -> None:
        from dalton_core.tick_ledger import bounded_counts

        result = {
            "source_ref": SALES_NOTES_SOURCE_REF, "status": "dispatched",
            "settled": [], "launched": [{"ticket_ref": "t"}], "read": {"read": 1},
            "enumerated": 211, "windows": 1, "partial_windows": 0,
            "enumerations": 0, "cache_hits": 1, "enumeration_seconds": 0.0,
            "read_seconds": 4.1, "read_deferred": False, "already_bound": 2,
        }
        counts = bounded_counts(result)
        # The three numbers this work package is judged on.
        self.assertEqual(counts["launched"], 1)
        self.assertEqual(counts["enumerations"], 0)
        self.assertEqual(counts["cache_hits"], 1)
        # ...and the one the envelope-binding fix added. It has to be a scalar
        # at the top level: ``read`` is a nested result and is dropped here.
        self.assertEqual(counts["already_bound"], 2)
        self.assertNotIn("read", counts)
        for key in ("enumerated", "windows", "enumeration_seconds", "read_seconds"):
            self.assertIn(key, counts)


if __name__ == "__main__":
    unittest.main()
