"""A long scheduler write transaction, and what every other writer sees.

2026-09-29: after a deploy, eleven of seventeen web fetches died at
``scheduler.enqueue``'s ``BEGIN IMMEDIATE`` with ``database is locked`` (the
same error hit AlphaEngine search, deep_insight_gate and event_judgement
completions).  Read-only lock polling of the live scheduler showed every
targeted ``claim`` holding the write lock for ~0.65 s: it joined a
``GROUP BY work_order_id`` over all 206k attempt events to find the one
WorkOrder it had been asked for.  These tests pin the fix and the behaviour
around a held lock: readers and constructors keep working, other writers wait
the busy timeout and fail typed, lane children retry the governed call, and a
fetch whose lock never clears is reported as retryable contention -- and is
retried -- instead of an unknown acquisition failure.
"""

from __future__ import annotations

import io
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from dalton_core.public_web_core_fetch import (
    WebFetchConnectorGovernance,
    build_web_fetch_governance_record,
)
from dalton_core.public_web_fetch_cli import fake_page_transport, run_fetch
from dalton_core.scheduler import Scheduler
from dalton_core.sqlite_contention import (
    SQLITE_LOCKED_FAILURE_CODE,
    is_sqlite_lock_error,
    retry_lane_call_on_sqlite_lock,
    sqlite_failure_location,
    sqlite_lock_failure,
)
from tests.test_public_web_fetch_lane import (
    BODY,
    OWNER,
    URL_A,
    FetchChildTests,
    FetchCoordinatorTests,
)
from tests.test_scheduler import MutableClock, result, work_order


# Short enough that a held lock fails a test writer in a fraction of a second.
SHORT_BUSY_MS = 200


class LockHolder:
    """Another process holding the scheduler's write lock in an open transaction.

    A separate process, as in production: the controller, the writer, lane
    children and the publication worker each open the file themselves.
    ``seconds=None`` holds until :meth:`release`.
    """

    def __init__(self, path: Path, seconds: float | None = None) -> None:
        self.path = path
        self.seconds = seconds
        self.process: subprocess.Popen[str] | None = None

    def __enter__(self) -> "LockHolder":
        wait = (f"time.sleep({self.seconds})" if self.seconds is not None
                else "sys.stdin.readline()")
        self.process = subprocess.Popen(
            [sys.executable, "-c",
             "import sqlite3, sys, time\n"
             f"c = sqlite3.connect({str(self.path)!r}, isolation_level=None)\n"
             "c.execute('BEGIN IMMEDIATE')\n"
             "print('held', flush=True)\n"
             f"{wait}\n"
             "c.execute('COMMIT')\n"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        assert self.process.stdout is not None
        if self.process.stdout.readline().strip() != "held":
            raise RuntimeError("lock holder did not take the write lock")
        return self

    def release(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None and self.seconds is None:
            assert self.process.stdin is not None
            self.process.stdin.write("\n")
            self.process.stdin.flush()
        self.process.wait(timeout=30)
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                stream.close()
        self.process = None

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _old_whole_queue_claim_query(cur: sqlite3.Cursor, work_order_id: str, now: str):
    """The claim predicate before the fix, kept here as the semantic oracle."""

    return cur.execute(
        "SELECT e.* FROM scheduler_attempt_events e "
        "JOIN (SELECT work_order_id, MAX(event_seq) AS max_seq "
        "      FROM scheduler_attempt_events GROUP BY work_order_id) current "
        "ON current.max_seq=e.event_seq "
        "WHERE e.state='ready' "
        "AND (e.not_before IS NULL OR e.not_before<=?) "
        "AND NOT EXISTS (SELECT 1 FROM scheduler_formal_results r "
        "                WHERE r.work_order_id=e.work_order_id) "
        "AND e.work_order_id=? ORDER BY e.event_seq LIMIT 1",
        (now, work_order_id),
    ).fetchone()


class LongWriterContentionTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "scheduler.sqlite"
        busy = patch("dalton_core.scheduler.SQLITE_BUSY_TIMEOUT_MS", SHORT_BUSY_MS)
        busy.start()
        self.addCleanup(busy.stop)
        self.scheduler = Scheduler(str(self.path))
        self.addCleanup(self.scheduler.close)
        self.scheduler.enqueue(work_order("work-before"))

    def test_held_lock_blocks_writers_only_and_names_the_scheduler(self) -> None:
        with LockHolder(self.path) as holder:
            # WAL readers and a fresh Scheduler on the same file are not blocked.
            self.assertEqual(self.scheduler.status("work-before")["state"], "ready")
            other = Scheduler(str(self.path))
            self.addCleanup(other.close)
            self.assertIsNone(other.formal_result("work-before"))
            started = time.monotonic()
            # Caught by hand: assertRaises clears the frames the store's path
            # is read from.
            error: sqlite3.OperationalError | None = None
            try:
                other.enqueue(work_order("work-during"))
            except sqlite3.OperationalError as exc:
                error = exc
            waited = time.monotonic() - started
            self.assertTrue(is_sqlite_lock_error(error))
            # It waited out the busy timeout instead of failing at once.
            self.assertGreaterEqual(waited, SHORT_BUSY_MS / 1000 * 0.8)
            self.assertEqual(sqlite_failure_location(error.__traceback__), str(self.path))
            # Nothing of the failed enqueue was written.
            self.assertIsNone(
                other.connection.execute(
                    "SELECT 1 FROM scheduler_work_orders WHERE work_order_id='work-during'"
                ).fetchone())
            holder.release()
        self.assertEqual(other.enqueue(work_order("work-during"))["status"], "fresh")

    def test_lane_retry_rides_out_a_transaction_shorter_than_its_deadline(self) -> None:
        notes: list[str] = []
        with patch("dalton_core.sqlite_contention.LANE_LOCK_RETRY_INITIAL_DELAY", 0.05), \
                LockHolder(self.path, seconds=1.0):
            enqueued = retry_lane_call_on_sqlite_lock(
                lambda: self.scheduler.enqueue(work_order("work-after")),
                label="test lane", deadline_seconds=20, log=notes.append,
            )
        self.assertEqual(enqueued["status"], "fresh")
        self.assertTrue(notes)
        self.assertIn("database is locked", notes[0])
        self.assertTrue(notes[0].startswith("test lane: try 1 "))
        # A repeat after an ambiguous failure replays instead of doubling.
        again = self.scheduler.enqueue(work_order("work-after"))
        self.assertEqual(again["status"], "duplicate")

    def test_lane_retry_gives_up_with_the_lock_error_after_its_deadline(self) -> None:
        error: sqlite3.OperationalError | None = None
        with patch("dalton_core.sqlite_contention.LANE_LOCK_RETRY_INITIAL_DELAY", 0.05), \
                LockHolder(self.path):
            try:
                retry_lane_call_on_sqlite_lock(
                    lambda: self.scheduler.enqueue(work_order("work-never")),
                    label="test lane", deadline_seconds=0.5, log=lambda _m: None,
                )
            except sqlite3.OperationalError as exc:
                error = exc
        self.assertIsNotNone(error)
        fields = sqlite_lock_failure(error)
        self.assertEqual(fields["failure_code"], SQLITE_LOCKED_FAILURE_CODE)
        self.assertIs(fields["failure_retryable"], True)
        self.assertIn("database is locked", fields["failure_reason"])
        self.assertIn("scheduler.sqlite", fields["failure_reason"])

    def test_non_lock_errors_are_never_retried(self) -> None:
        calls: list[int] = []

        def broken() -> None:
            calls.append(1)
            raise sqlite3.OperationalError("no such table: nowhere")

        with self.assertRaises(sqlite3.OperationalError):
            retry_lane_call_on_sqlite_lock(broken, label="x", log=lambda _m: None)
        self.assertEqual(len(calls), 1)


class TargetedClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.scheduler = Scheduler(
            ":memory:", clock=self.clock, max_attempts=2, default_lease_seconds=10,
            max_lease_seconds=20, max_renew_seconds=5, max_total_lease_seconds=25,
        )
        self.addCleanup(self.scheduler.close)

    def _finish(self, work_id: str, status: str = "succeeded", **kwargs) -> None:
        self.scheduler.enqueue(work_order(work_id))
        lease = self.scheduler.claim("worker:a", work_order_id=work_id)
        self.scheduler.complete(
            work_id, lease["attempt"]["attempt_number"], "worker:a", lease["lease_token"],
            result(f"result:{work_id}:{lease['attempt']['attempt_number']}",
                   work_ref=work_id, status=status),
            idempotency_key=f"complete:{work_id}:{lease['attempt']['attempt_number']}",
            **kwargs,
        )

    def test_targeted_claim_matches_the_whole_queue_predicate(self) -> None:
        s = self.scheduler
        s.enqueue(work_order("fresh"))
        s.enqueue(work_order("leased"))
        s.claim("worker:a", work_order_id="leased")
        self._finish("done")
        self._finish("failed", status="failed")
        self._finish("backoff", status="retryable",
                     retry_at=self.clock.value + timedelta(seconds=60))
        self._finish("retry-now", status="retryable")
        s.enqueue(work_order("expired"))
        s.claim("worker:a", work_order_id="expired", lease_seconds=5)
        self._finish("exhausted", status="retryable")
        lease = s.claim("worker:a", work_order_id="exhausted")
        s.complete("exhausted", 2, "worker:a", lease["lease_token"],
                   result("result:exhausted:2", work_ref="exhausted", status="retryable"),
                   idempotency_key="complete:exhausted:2")
        ids = ["fresh", "leased", "done", "failed", "backoff", "retry-now",
               "expired", "exhausted", "unknown"]
        for advance in (0, 6, 61):
            self.clock.advance(advance)
            now = self.clock().isoformat(timespec="microseconds")
            cur = s.connection.cursor()
            for work_id in ids:
                with self.subTest(work_id=work_id, advance=advance):
                    expected = _old_whole_queue_claim_query(cur, work_id, now)
                    got = s._claimable_event(cur, work_id, now)
                    self.assertEqual(
                        None if got is None else got["event_seq"],
                        None if expected is None else expected["event_seq"])
        # And claim() itself honours it end to end.  67 s on, every 10 s lease
        # has lapsed: claim expires it and the next attempt is claimable.
        self.assertIsNone(s.claim("worker:b", work_order_id="done"))
        self.assertIsNone(s.claim("worker:b", work_order_id="exhausted"))
        for work_id, attempt in (("leased", 2), ("expired", 2), ("backoff", 2),
                                 ("retry-now", 2), ("fresh", 1)):
            with self.subTest(claim=work_id):
                claimed = s.claim("worker:b", work_order_id=work_id)
                self.assertEqual(claimed["attempt"]["attempt_number"], attempt)
        self.assertIsNone(s.claim("worker:c", work_order_id="fresh"))

    def test_targeted_claim_still_expires_every_overdue_lease(self) -> None:
        """Claim's lazy expiry of other lapsed leases survives the lock-free scan."""

        s = self.scheduler
        s.enqueue(work_order("stale"))
        s.claim("worker:a", work_order_id="stale", lease_seconds=5)
        s.enqueue(work_order("live"))
        s.claim("worker:a", work_order_id="live", lease_seconds=20)
        self.clock.advance(6)
        s.enqueue(work_order("next"))
        self.assertIsNotNone(s.claim("worker:b", work_order_id="next"))
        self.assertEqual(
            [row["state"] for row in s.attempt_history("stale")],
            ["ready", "leased", "expired", "ready"])
        self.assertEqual(s.status("live")["state"], "leased")
        # sweep_expired finds the rest the same way.
        self.clock.advance(15)
        expired = s.sweep_expired()
        self.assertEqual([item["expired"]["work_order_ref"] for item in expired],
                         ["live", "next"])
        self.assertEqual(s.sweep_expired(), [])

    def _claim_cost(self, work_id: str) -> tuple[int, int]:
        """VM work (progress ticks) under the write lock: new claim, old query.

        Only work done while a transaction is open counts -- that is the time
        every other writer waits for.  The old predicate is timed the way it
        used to run, inside claim's BEGIN IMMEDIATE.
        """

        ticks = [0]
        conn = self.scheduler.connection

        def tick() -> int:
            if conn.in_transaction:
                ticks[0] += 1
            return 0

        self.scheduler.enqueue(work_order(work_id))
        now = self.clock().isoformat(timespec="microseconds")
        conn.set_progress_handler(tick, 100)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self.scheduler._expire_due(conn.cursor(), self.clock(), now)
                _old_whole_queue_claim_query(conn.cursor(), work_id, now)
            finally:
                conn.execute("ROLLBACK")
            old = ticks[0]
            ticks[0] = 0
            self.assertIsNotNone(self.scheduler.claim("worker:a", work_order_id=work_id))
            new = ticks[0]
        finally:
            conn.set_progress_handler(None, 100)
        return new, old

    def test_targeted_claim_cost_does_not_grow_with_history(self) -> None:
        for index in range(100):
            self._finish(f"history-{index}")
        small_new, small_old = self._claim_cost("probe-small")
        for index in range(100, 800):
            self._finish(f"history-{index}")
        large_new, large_old = self._claim_cost("probe-large")
        # The old predicate scanned every event under BEGIN IMMEDIATE...
        self.assertGreater(large_old, small_old * 4)
        # ...the targeted claim does the same bounded work at 8x the history.
        self.assertLessEqual(large_new, small_new + 5)


class FetchChildLockTests(unittest.TestCase):
    """The real fetch child against a scheduler another process holds locked."""

    setUp = FetchChildTests.setUp

    def _run(self, *, lock_retry_seconds: float) -> tuple[dict, str]:
        summary_dir = self.root / "summary"
        err = io.StringIO()
        with redirect_stderr(err):
            summary = run_fetch(
                state_dir=self.state,
                governance=WebFetchConnectorGovernance(
                    build_web_fetch_governance_record(approved_by=OWNER, status="approved")),
                url_ref=URL_A, requested_by=OWNER,
                transport=fake_page_transport(BODY), transport_label="fake",
                summary_dir=summary_dir, spool_dir=self.state / "spool",
                lock_retry_seconds=lock_retry_seconds,
            )
        return summary, err.getvalue()

    def _prepare(self) -> Path:
        path = self.state / "scheduler.sqlite"
        # The fetch lane's policy row already exists on a live scheduler.
        Scheduler(str(path), default_lease_seconds=30, max_lease_seconds=60).close()
        for target, value in (
            ("dalton_core.scheduler.SQLITE_BUSY_TIMEOUT_MS", SHORT_BUSY_MS),
            ("dalton_core.sqlite_contention.LANE_LOCK_RETRY_INITIAL_DELAY", 0.1),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return path

    def test_fetch_rides_out_a_long_scheduler_transaction(self) -> None:
        path = self._prepare()
        with LockHolder(path, seconds=1.5):
            summary, log = self._run(lock_retry_seconds=30)
        self.assertEqual(summary["status"], "succeeded", summary)
        self.assertEqual(summary["provider_calls"], 1)
        self.assertIsNone(summary["failure_reason"])
        self.assertIn("database is locked; retrying", log)

    def test_fetch_whose_lock_never_clears_is_retryable_contention(self) -> None:
        path = self._prepare()
        with LockHolder(path):
            summary, _log = self._run(lock_retry_seconds=0.5)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["failure_code"], SQLITE_LOCKED_FAILURE_CODE)
        self.assertIs(summary["failure_retryable"], True)
        self.assertNotIn("unexpected", summary["failure_reason"])
        self.assertIn("scheduler.sqlite", summary["failure_reason"])
        # It never reached the network, and the parent can read why.
        self.assertEqual(summary["provider_calls"], 0)
        self.assertIsNone(summary["fetch"])
        self.assertTrue((self.root / "summary" / "summary.json").is_file())
        # The next launch, once the lock is gone, fetches the page normally.
        summary, _log = self._run(lock_retry_seconds=30)
        self.assertEqual(summary["status"], "succeeded", summary)


class LockFailedDocumentRetryTests(unittest.TestCase):
    """A document failed by contention is retried; one failed as terminal is not."""

    setUp = FetchCoordinatorTests.setUp

    def _fail_both_then_recover(self, fail_summary: dict) -> list[dict]:
        self.fetch_launcher.fail = True
        self.fetch_launcher.fail_summary = fail_summary
        # discover, launch A, settle A + launch B, settle B (as in
        # test_failed_fetch_is_recorded_and_retried_after_interval).
        for _ in range(4):
            tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["acquisition"]["status"], "idle")
        failed = self.missions.discovered_documents(
            self.mission["id"], status="acquisition_failed")
        self.assertEqual(len(failed), 2)
        self.fetch_launcher.fail = False
        before = len(self.fetch_launcher.calls)
        self.clock.advance(hours=1, minutes=1)
        tick = self.coordinator.dispatch_once()
        self.assertEqual((tick["acquisition"]["status"], tick["acquisition"]["retry"]),
                         ("launched", True))
        # Settling the first retry launches the second in the same tick.
        tick = self.coordinator.dispatch_once()
        self.assertEqual([item["status"] for item in tick["settled_documents"]], ["acquired"])
        self.assertEqual((tick["acquisition"]["status"], tick["acquisition"]["retry"]),
                         ("launched", True))
        tick = self.coordinator.dispatch_once()
        self.assertEqual([item["status"] for item in tick["settled_documents"]], ["acquired"])
        self.assertEqual(len(self.fetch_launcher.calls), before + 2)
        self.assertEqual(self.missions.discovered_documents(
            self.mission["id"], status="acquisition_failed"), [])
        self.assertEqual(len(self.missions.discovered_documents(
            self.mission["id"], status="acquired")), 2)
        return failed

    def test_lock_contention_failure_settles_retryable_and_is_retried(self) -> None:
        try:
            raise sqlite3.OperationalError("database is locked")
        except sqlite3.OperationalError as exc:
            fail_summary = sqlite_lock_failure(exc)
        failed = self._fail_both_then_recover(fail_summary)
        self.assertTrue(all(row["failure_retryable"] is True for row in failed))
        self.assertTrue(all(row["failure_reason"].startswith(SQLITE_LOCKED_FAILURE_CODE)
                            for row in failed))

    def test_rows_failed_by_the_old_unexpected_lock_error_are_retried(self) -> None:
        # What the live ledger holds for the 2026-09-29 failures.
        failed = self._fail_both_then_recover(
            {"failure_reason": "unexpected OperationalError: database is locked"})
        self.assertTrue(all(row["failure_retryable"] is None for row in failed))


if __name__ == "__main__":
    unittest.main()
