"""The controller and writer read what changed, not the whole history.

2026-09-29, live release ae70706a: after a deploy the controller had read
~800 GB and the writers ~110-250 GB, in bursts of 0.5-1 GB per ten seconds,
and a reader held ``scheduler.sqlite``'s WAL at one frame for the whole of a
25-minute projection round, so no checkpoint could pass it.  These tests pin
the fixes:

* the scheduler's overdue-lease probe is one bounded index range, whatever
  the history, and matches the old full-scan predicate exactly;
* no scheduler call leaves a read transaction open behind it;
* the dashboard projection folds each append-only table by ``rowid``, gives
  the same snapshot as a rebuild from scratch, reads nothing on a round where
  nothing changed, and releases the scheduler snapshot before its in-memory
  work starts;
* the writer's per-tick Claim and work-order lookups are index seeks.
"""

from __future__ import annotations

import inspect
import json
import random
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from dalton_core import dashboard_projector
from dalton_core.migrations import (
    CORE_PROJECTION_INDEXES,
    SCHEDULER_PROJECTION_INDEXES,
    ensure_indexes,
)
from dalton_core.migrations.projection_indexes import (
    CLAIM_SUBJECT_METRIC_PERIOD,
    WORK_ORDER_DOCUMENT_STAGE,
    WORK_ORDER_REQUEST_ID,
)
from dalton_core.projection_folds import AppendOnlyFold, clear_folds, source_identity
from dalton_core.scheduler import (
    _DUE_LEASE_PROBE,
    DUE_LEASE_PROBE_LIMIT,
    Scheduler,
)
from tests.test_scheduler import MutableClock, result, work_order


def _old_current_leases(connection: sqlite3.Connection) -> dict[str, tuple]:
    """The pre-fix definition of a current lease, as an oracle.

    Every ``leased`` event with no newer event for its WorkOrder, with the
    expiry of its lease's newest revision and whether it is a recovery
    reservation.
    """

    rows = connection.execute(
        "SELECT e.work_order_id, e.event_seq, e.reason, e.lease_revision_id "
        "FROM scheduler_attempt_events e WHERE e.state='leased' AND NOT EXISTS ("
        " SELECT 1 FROM scheduler_attempt_events n WHERE n.work_order_id=e.work_order_id"
        " AND n.event_seq>e.event_seq)"
    ).fetchall()
    found = {}
    for work_id, seq, reason, revision in rows:
        lease_id = connection.execute(
            "SELECT lease_id FROM scheduler_leases WHERE lease_revision_id=?", (revision,)
        ).fetchone()[0]
        expires = connection.execute(
            "SELECT expires_at FROM scheduler_leases WHERE lease_id=? "
            "ORDER BY lease_version DESC LIMIT 1", (lease_id,)
        ).fetchone()[0]
        reserved = int(str(reason).startswith("operator_model_recovery_reserved:"))
        found[work_id] = (seq, lease_id, expires, reserved)
    return found


def _index_rows(connection: sqlite3.Connection) -> dict[str, tuple]:
    return {
        row[0]: tuple(row[1:])
        for row in connection.execute(
            "SELECT work_order_id, event_seq, lease_id, expires_at, reserved "
            "FROM scheduler_current_leases"
        )
    }


class CurrentLeaseIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.scheduler = Scheduler(
            ":memory:", clock=self.clock, max_attempts=3, default_lease_seconds=10,
            max_lease_seconds=20, max_renew_seconds=5, max_total_lease_seconds=60,
        )
        self.addCleanup(self.scheduler.close)

    def _finish(self, work_id: str) -> None:
        s = self.scheduler
        s.enqueue(work_order(work_id))
        lease = s.claim("worker:a", work_order_id=work_id)
        attempt = lease["attempt"]["attempt_number"]
        s.complete(work_id, attempt, "worker:a", lease["lease_token"],
                   result(f"result:{work_id}:{attempt}", work_ref=work_id),
                   idempotency_key=f"complete:{work_id}:{attempt}")

    def test_the_probe_is_an_index_range_with_a_limit(self) -> None:
        plan = " ".join(
            str(row[-1]) for row in self.scheduler.connection.execute(
                "EXPLAIN QUERY PLAN " + _DUE_LEASE_PROBE, ("2026-01-01", 5))
        )
        self.assertIn("USING INDEX scheduler_current_leases_due", plan)
        self.assertNotIn("SCAN scheduler_attempt_events", plan)
        self.assertNotIn("scheduler_attempt_events", plan)
        self.assertIn("LIMIT", _DUE_LEASE_PROBE)
        self.assertGreater(DUE_LEASE_PROBE_LIMIT, 0)

    def test_the_index_matches_the_full_scan_through_every_transition(self) -> None:
        s = self.scheduler
        rng = random.Random(20260929)
        leases: dict[str, dict] = {}
        for step in range(300):
            action = rng.choice(["enqueue", "claim", "renew", "complete", "fail",
                                 "advance", "sweep", "reserve", "orphan"])
            if action == "enqueue":
                s.enqueue(work_order(f"work-{step}"))
            elif action == "claim":
                ready = [row[0] for row in s.connection.execute(
                    "SELECT work_order_id FROM scheduler_work_orders")]
                if ready:
                    target = rng.choice(ready)
                    lease = s.claim("worker:a", work_order_id=target,
                                    lease_seconds=rng.choice([5, 10, 20]))
                    if lease is not None:
                        leases[target] = lease
            elif leases and action in ("renew", "complete", "fail", "reserve", "orphan"):
                target = rng.choice(sorted(leases))
                lease = leases.pop(target)
                attempt = lease["attempt"]["attempt_number"]
                try:
                    if action == "renew":
                        s.renew(target, attempt, "worker:a", lease["lease_token"],
                                extend_seconds=5)
                        leases[target] = lease
                    elif action in ("complete", "fail"):
                        s.complete(
                            target, attempt, "worker:a", lease["lease_token"],
                            result(f"r:{target}:{attempt}:{step}", work_ref=target,
                                   status="succeeded" if action == "complete" else "retryable"),
                            idempotency_key=f"c:{target}:{attempt}:{step}")
                    elif action == "reserve":
                        s.reserve_interrupted_model_recovery(
                            target, attempt, "worker:a",
                            lease_revision_ref=lease["lease"]["id"],
                            lease_hash=lease["lease"]["content_hash"],
                            work_order_hash=lease["work_order_hash"],
                            reservation_hash="9" * 64)
                    else:
                        s.expire_orphaned_lease(target, attempt, lease["lease"]["id"],
                                                reason="holder gone")
                except Exception:  # noqa: BLE001 - expired or moved on: both fine
                    pass
            elif action == "advance":
                self.clock.advance(rng.choice([1, 3, 7, 15]))
            elif action == "sweep":
                s.sweep_expired()
            with self.subTest(step=step, action=action):
                self.assertEqual(_index_rows(s.connection), _old_current_leases(s.connection))

    def test_due_candidates_cost_does_not_grow_with_history(self) -> None:
        def cost() -> int:
            ticks = [0]

            def tick() -> int:
                ticks[0] += 1
                return 0

            conn = self.scheduler.connection
            conn.set_progress_handler(tick, 50)
            try:
                self.scheduler._due_lease_candidates(self.clock())
            finally:
                conn.set_progress_handler(None, 50)
            return ticks[0]

        self.scheduler.enqueue(work_order("live"))
        self.scheduler.claim("worker:a", work_order_id="live")
        for index in range(100):
            self._finish(f"history-{index}")
        small = cost()
        for index in range(100, 800):
            self._finish(f"history-{index}")
        large = cost()
        # 8x the history (and 8x the historical leased events), same work.
        self.assertLessEqual(large, small + 2)

    def test_sweep_and_claim_still_expire_overdue_leases_in_event_order(self) -> None:
        s = self.scheduler
        for work_id in ("a", "b", "c"):
            s.enqueue(work_order(work_id))
        s.claim("worker:a", work_order_id="a", lease_seconds=20)
        s.claim("worker:a", work_order_id="b", lease_seconds=5)
        lease_c = s.claim("worker:a", work_order_id="c", lease_seconds=5)
        s.renew("c", 1, "worker:a", lease_c["lease_token"], extend_seconds=5)
        self.clock.advance(6)
        # b lapsed; c's renewal moved its expiry to +10; a has +20.
        self.assertEqual(s._due_lease_candidates(self.clock()), ["b"])
        self.clock.advance(15)
        expired = s.sweep_expired()
        self.assertEqual([item["expired"]["work_order_ref"] for item in expired],
                         ["a", "b", "c"])
        self.assertEqual(s._due_lease_candidates(self.clock()), [])

    def test_the_probe_hands_over_at_most_its_limit(self) -> None:
        s = self.scheduler
        for index in range(5):
            s.enqueue(work_order(f"w-{index}"))
            s.claim("worker:a", work_order_id=f"w-{index}", lease_seconds=5)
        self.clock.advance(6)
        with patch("dalton_core.scheduler.DUE_LEASE_PROBE_LIMIT", 2):
            first = s.sweep_expired()
            second = s.sweep_expired()
            third = s.sweep_expired()
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 2)
        self.assertEqual(len(third), 1)


class CurrentLeaseMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.path = Path(self.root.name) / "scheduler.sqlite"
        self.clock = MutableClock()

    def _open(self) -> Scheduler:
        scheduler = Scheduler(self.path, clock=self.clock, default_lease_seconds=10,
                              max_lease_seconds=20)
        self.addCleanup(scheduler.close)
        return scheduler

    def test_an_existing_scheduler_is_backfilled_once_on_open(self) -> None:
        s = self._open()
        for work_id in ("done", "leased", "reserved"):
            s.enqueue(work_order(work_id))
        done = s.claim("worker:a", work_order_id="done")
        s.complete("done", 1, "worker:a", done["lease_token"],
                   result("result:done", work_ref="done"), idempotency_key="c:done")
        s.claim("worker:a", work_order_id="leased")
        reserved = s.claim("worker:a", work_order_id="reserved")
        s.reserve_interrupted_model_recovery(
            "reserved", 1, "worker:a", lease_revision_ref=reserved["lease"]["id"],
            lease_hash=reserved["lease"]["content_hash"],
            work_order_hash=reserved["work_order_hash"], reservation_hash="9" * 64)
        s.close()
        # What a scheduler written by the previous release looks like.
        raw = sqlite3.connect(self.path, isolation_level=None)
        raw.execute("DROP TRIGGER scheduler_current_leases_on_event")
        raw.execute("DROP TRIGGER scheduler_current_leases_on_lease")
        raw.execute("DROP TABLE scheduler_current_leases")
        raw.close()

        reopened = self._open()
        self.assertTrue(reopened._current_leases_ready)
        self.assertEqual(_index_rows(reopened.connection),
                         _old_current_leases(reopened.connection))
        self.assertEqual(set(_index_rows(reopened.connection)), {"leased", "reserved"})
        self.clock.advance(11)
        # The reserved lease is never swept; the ordinary one is.
        self.assertEqual(
            [item["expired"]["work_order_ref"] for item in reopened.sweep_expired()],
            ["leased"])

    def test_a_read_only_open_falls_back_to_the_scan(self) -> None:
        s = self._open()
        s.enqueue(work_order("leased"))
        s.claim("worker:a", work_order_id="leased", lease_seconds=5)
        s.close()
        raw = sqlite3.connect(self.path, isolation_level=None)
        raw.execute("DROP TRIGGER scheduler_current_leases_on_event")
        raw.execute("DROP TRIGGER scheduler_current_leases_on_lease")
        raw.execute("DROP TABLE scheduler_current_leases")
        raw.close()
        with patch.object(Scheduler, "_ensure_current_lease_index", return_value=False):
            fallback = self._open()
        self.assertFalse(fallback._current_leases_ready)
        self.clock.advance(6)
        self.assertEqual(fallback._due_lease_candidates(self.clock()), ["leased"])


class NoReadTransactionLeftOpenTests(unittest.TestCase):
    """A WAL reader that outlives its call pins every later checkpoint."""

    def setUp(self) -> None:
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.path = Path(self.root.name) / "scheduler.sqlite"
        self.clock = MutableClock()
        self.scheduler = Scheduler(self.path, clock=self.clock, default_lease_seconds=10,
                                   max_lease_seconds=20)
        self.addCleanup(self.scheduler.close)

    def assert_no_reader(self, label: str) -> None:
        self.assertFalse(self.scheduler.connection.in_transaction, label)
        other = sqlite3.connect(self.path, isolation_level=None)
        try:
            busy, log, checkpointed = other.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            other.close()
        self.assertEqual(busy, 0, f"{label} left a reader on the WAL")
        self.assertEqual(log, checkpointed, label)

    def test_every_call_releases_its_snapshot(self) -> None:
        s = self.scheduler
        s.enqueue(work_order("w-1"))
        self.assert_no_reader("enqueue")
        s.enqueue(work_order("w-2"))
        lease = s.claim("worker:a", work_order_id="w-1")
        self.assert_no_reader("claim")
        s.validate_lease_for_use(
            "w-1", 1, "worker:a", lease["lease_token"],
            lease_revision_ref=lease["lease"]["id"],
            lease_hash=lease["lease"]["content_hash"],
            work_order_hash=lease["work_order_hash"])
        self.assert_no_reader("validate_lease_for_use")
        s.renew("w-1", 1, "worker:a", lease["lease_token"])
        self.assert_no_reader("renew")
        s.status("w-1")
        s.attempt_history("w-1")
        s.formal_result("w-1")
        s.work_order_authority("w-1")
        self.assert_no_reader("reads")
        s.complete("w-1", 1, "worker:a", lease["lease_token"],
                   result("result:w-1", work_ref="w-1"), idempotency_key="c:w-1")
        self.assert_no_reader("complete")
        s.claim("worker:a", work_order_id="w-2", lease_seconds=5)
        self.clock.advance(6)
        self.assertEqual(len(s.sweep_expired()), 1)
        self.assert_no_reader("sweep_expired")
        s.sweep_expired()
        self.assert_no_reader("an empty sweep")
        self.assertIsNone(s.claim("worker:a", work_order_id="missing"))
        self.assert_no_reader("an empty claim")


def _fold_db(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE facts (fact_id TEXT PRIMARY KEY, grp TEXT, value INTEGER,"
        " created_at TEXT)")
    return connection


def _latest_value_fold() -> AppendOnlyFold:
    def reduce(state: dict, row: sqlite3.Row) -> None:
        state[row["grp"]] = row["value"]

    return AppendOnlyFold("test.facts", "facts", "fact_id, grp, value, created_at",
                          key="fact_id", init=dict, reduce=reduce)


class AppendOnlyFoldTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_folds()
        self.addCleanup(clear_folds)
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.path = Path(self.root.name) / "facts.sqlite"

    def _insert(self, connection, *rows) -> None:
        connection.executemany("INSERT INTO facts VALUES (?,?,?,?)", rows)

    def test_a_round_reads_only_what_arrived_and_fingerprints_the_table(self) -> None:
        connection = _fold_db(self.path)
        fold = _latest_value_fold()
        self._insert(connection, ("f1", "a", 1, "2026-01-01"), ("f2", "b", 2, "2026-01-03"))
        first = fold.read(connection, source_identity(self.path))
        self.assertEqual((first.state, first.added, first.fresh), ({"a": 1, "b": 2}, 2, True))
        again = fold.read(connection, source_identity(self.path))
        self.assertEqual((again.added, again.fresh), (0, False))
        # A late row (older timestamp, committed later) is simply the next row.
        self._insert(connection, ("f3", "a", 3, "2026-01-02"))
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        third = fold.read(connection, source_identity(self.path))
        connection.set_trace_callback(None)
        self.assertEqual((third.state, third.added), ({"a": 3, "b": 2}, 1))
        self.assertEqual(
            (third.count, third.max_created),
            tuple(connection.execute("SELECT COUNT(*), MAX(created_at) FROM facts").fetchone()))
        self.assertTrue(all("rowid" in sql for sql in statements), statements)

    def test_a_replaced_database_is_rebuilt_not_mixed(self) -> None:
        connection = _fold_db(self.path)
        fold = _latest_value_fold()
        self._insert(connection, ("f1", "a", 1, "t"), ("f2", "b", 2, "t"))
        fold.read(connection, source_identity(self.path))
        connection.close()
        self.path.unlink()
        replaced = _fold_db(self.path)
        self._insert(replaced, ("g1", "z", 9, "t"), ("g2", "y", 8, "t"), ("g3", "x", 7, "t"))
        read = fold.read(replaced, source_identity(self.path))
        self.assertEqual(read.state, {"z": 9, "y": 8, "x": 7})
        self.assertTrue(read.fresh)

    def test_a_reduction_that_raises_leaves_no_half_folded_state(self) -> None:
        connection = _fold_db(self.path)
        calls = {"n": 0}

        def reduce(state: dict, row: sqlite3.Row) -> None:
            calls["n"] += 1
            if row["fact_id"] == "bad" and calls["n"] < 10:
                raise ValueError("corrupt row")
            state[row["grp"]] = row["value"]

        fold = AppendOnlyFold("test.raises", "facts", "fact_id, grp, value, created_at",
                              key="fact_id", init=dict, reduce=reduce)
        self._insert(connection, ("ok", "a", 1, "t"), ("bad", "b", 2, "t"))
        with self.assertRaises(ValueError):
            fold.read(connection, source_identity(self.path))
        calls["n"] = 10
        read = fold.read(connection, source_identity(self.path))
        self.assertTrue(read.fresh)
        self.assertEqual(read.state, {"a": 1, "b": 2})


class ProjectionRoundTests(unittest.TestCase):
    """The projector against the fixture authorities of its own tests."""

    def setUp(self) -> None:
        from tests.test_dashboard_projector import DashboardProjectorTests

        clear_folds()
        self.addCleanup(clear_folds)
        self.fixture = DashboardProjectorTests("test_end_to_end_latest_chains_tree_and_expired_lease")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.addCleanup(self.fixture.doCleanups)

    def _snapshot(self) -> dict:
        snapshot = self.fixture.projector().build_snapshot()
        snapshot["metadata"].pop("as_of")
        return snapshot

    def _append_scheduler_history(self) -> None:
        scheduler = Scheduler(
            self.fixture.scheduler_path, clock=self.fixture.clock,
            default_lease_seconds=5, max_lease_seconds=5, max_renew_seconds=2,
            max_total_lease_seconds=10,
        )
        try:
            for index in range(3):
                work_id = f"work:io-bounds-{index}"
                scheduler.enqueue(work_order(work_id))
                lease = scheduler.claim("worker:io", work_order_id=work_id)
                if index:
                    scheduler.complete(
                        work_id, 1, "worker:io", lease["lease_token"],
                        result(f"result:{work_id}", work_ref=work_id),
                        idempotency_key=f"complete:{work_id}")
        finally:
            scheduler.close()

    def test_an_incremental_round_equals_a_rebuild_from_scratch(self) -> None:
        self._snapshot()
        self._append_scheduler_history()
        incremental = self._snapshot()
        clear_folds()
        rebuilt = self._snapshot()
        self.assertEqual(incremental, rebuilt)
        self.assertIn("work:io-bounds-2",
                      {row["work_order_ref"] for row in incremental["work_items"]})

    def test_a_round_with_nothing_new_reads_no_rows(self) -> None:
        self._snapshot()
        folds = [value for value in vars(dashboard_projector).values()
                 if isinstance(value, AppendOnlyFold)]
        self.assertGreaterEqual(len(folds), 15)
        added: list[tuple[str, int]] = []
        original = AppendOnlyFold.read

        def spy(fold, *args, **kwargs):
            read = original(fold, *args, **kwargs)
            added.append((fold.name, read.added))
            return read

        with patch.object(AppendOnlyFold, "read", spy):
            self._snapshot()
        self.assertTrue(added)
        self.assertEqual([item for item in added if item[1]], [])

    def test_the_scheduler_snapshot_ends_before_the_in_memory_work(self) -> None:
        # Leave frames in the scheduler WAL so a checkpoint has work to do.
        self._append_scheduler_history()
        seen: list[tuple[int, int, int]] = []
        original = dashboard_projector.DashboardProjector._work_items

        def work_items(projector, *args, **kwargs):
            other = sqlite3.connect(self.fixture.scheduler_path, isolation_level=None)
            try:
                seen.append(other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
            finally:
                other.close()
            return original(projector, *args, **kwargs)

        with patch.object(dashboard_projector.DashboardProjector, "_work_items", work_items):
            self._snapshot()
        self.assertEqual(len(seen), 1)
        busy, log, checkpointed = seen[0]
        self.assertEqual(busy, 0, "the projection still held a scheduler snapshot")
        self.assertEqual(log, checkpointed)

    def test_the_watermark_is_the_count_and_newest_timestamp_of_each_table(self) -> None:
        snapshot = self._snapshot()
        # Recompute the fingerprint the old way, straight from SQL.
        sources = {"core": {}, "scheduler": {}}
        core = sqlite3.connect(self.fixture.core_path)
        scheduler = sqlite3.connect(self.fixture.scheduler_path)
        try:
            core_tables = {row[0] for row in core.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            names = sorted(dashboard_projector._CORE_TABLES
                           | (dashboard_projector._AGENDA_TABLES & core_tables))
            for table in names:
                if not table.endswith("_pointer"):
                    sources["core"][table] = list(core.execute(
                        f"SELECT COUNT(*), MAX(created_at) FROM {table}").fetchone())
            for table in sorted(dashboard_projector._CONNECTOR_PROJECTION_TABLES & core_tables):
                sources["core"][table] = list(core.execute(
                    f"SELECT COUNT(*), MAX(created_at) FROM {table}").fetchone())
            for table in sorted(dashboard_projector._SCHEDULER_TABLES):
                sources["scheduler"][table] = list(scheduler.execute(
                    f"SELECT COUNT(*), MAX(created_at) FROM {table}").fetchone())
        finally:
            core.close()
            scheduler.close()
        # Catalog/router sections are absent in this fixture.
        import hashlib

        encoded = json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(snapshot["metadata"]["source_watermark"],
                         "sha256:" + hashlib.sha256(encoded).hexdigest())


class WriterLookupPlanTests(unittest.TestCase):
    """The writer's per-tick lookups are seeks once ``migrations`` has run."""

    def setUp(self) -> None:
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)

    def _plan(self, connection: sqlite3.Connection, sql: str) -> str:
        return " ".join(str(row[-1]) for row in connection.execute("EXPLAIN QUERY PLAN " + sql))

    def test_forecast_reconciliation_claim_lookup_uses_the_expression_index(self) -> None:
        from dalton_core.forecast_reconciliation import ForecastReconciliationAuthority
        from dalton_core.store import DaltonStore

        store = DaltonStore(Path(self.root.name) / "core.sqlite")
        self.addCleanup(store.close)
        report = ensure_indexes(store.connection, CORE_PROJECTION_INDEXES)
        self.assertNotIn("claim_versions_subject_metric_period", report["deferred"])
        statements: list[str] = []
        store.connection.set_trace_callback(statements.append)
        reconciler = ForecastReconciliationAuthority.__new__(ForecastReconciliationAuthority)
        reconciler.connection = store.connection
        self.assertEqual(reconciler._latest_claims("company:x", "metric:y", "2026Q1"), [])
        store.connection.set_trace_callback(None)
        [sql] = [item for item in statements if "claim_versions" in item]
        plan = self._plan(store.connection, sql)
        self.assertIn("claim_versions_subject_metric_period", plan)
        self.assertNotIn("SCAN c", plan)
        # The query spells the indexed expressions exactly.
        for expression in CLAIM_SUBJECT_METRIC_PERIOD.split(", "):
            self.assertIn(expression.replace("claim_json", "c.claim_json"), sql)

    def test_the_work_order_metadata_lookups_use_their_partial_indexes(self) -> None:
        from dalton_core import controlled_lane_reentry, mission_document_research_executor

        scheduler = Scheduler(Path(self.root.name) / "scheduler.sqlite")
        self.addCleanup(scheduler.close)
        report = ensure_indexes(scheduler.connection, SCHEDULER_PROJECTION_INDEXES)
        self.assertEqual(report["deferred"], {})
        statements: list[str] = []
        scheduler.connection.set_trace_callback(statements.append)
        self.assertFalse(controlled_lane_reentry._already_consumed(
            scheduler.connection, "request:1"))
        scheduler.connection.set_trace_callback(None)
        [sql] = statements
        self.assertIn(WORK_ORDER_REQUEST_ID, sql)
        self.assertIn("scheduler_work_orders_request_id",
                      self._plan(scheduler.connection, sql))

        source = inspect.getsource(mission_document_research_executor._effective_stage)
        flat = "".join(part.strip().strip('"') for part in source.splitlines())
        for expression in WORK_ORDER_DOCUMENT_STAGE.split(", "):
            self.assertIn(expression, flat)
        stage_sql = (
            "SELECT work_order_json FROM scheduler_work_orders WHERE "
            + " AND ".join(f"{expression}='x'"
                           for expression in WORK_ORDER_DOCUMENT_STAGE.split(", "))
            + " ORDER BY rowid")
        self.assertIn("scheduler_work_orders_document_stage",
                      self._plan(scheduler.connection, stage_sql))


class BackupCadenceProbeTests(unittest.TestCase):
    """A controller start hashes one snapshot to learn its cadence, not all."""

    def setUp(self) -> None:
        from tests.test_backup_disk_preflight import _make_db

        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        base = Path(self.root.name)
        self.db = base / "core.sqlite"
        _make_db(self.db)
        from dalton_core.backup import DatabaseBackupManager

        self.manager = DatabaseBackupManager(base / "backups", {"core": self.db})
        for snapshot_id in ("20260901T000000.000000Z", "20260902T000000.000000Z",
                            "20260903T000000.000000Z"):
            self.manager.snapshot(snapshot_id)
        self.backups = base / "backups"

    def _hashed(self) -> tuple[dict | None, list[str]]:
        from dalton_core import backup

        hashed: list[str] = []
        original = backup._sha256

        self.manager.cancel_requested = lambda: False

        def spy(path, *, cancel_requested=None):
            self.assertIs(cancel_requested, self.manager.cancel_requested)
            hashed.append(Path(path).parent.name)
            return original(path, cancel_requested=cancel_requested)

        with patch.object(backup, "_sha256", spy):
            manifest = self.manager.latest_verified_manifest()
        return manifest, hashed

    def test_only_the_newest_snapshot_is_hashed_when_it_verifies(self) -> None:
        manifest, hashed = self._hashed()
        newest = max(path.name for path in self.backups.iterdir())
        self.assertEqual(manifest["snapshot_id"], newest)
        self.assertEqual(set(hashed), {newest})

    def test_a_newest_snapshot_that_fails_falls_back_to_the_next(self) -> None:
        names = sorted(path.name for path in self.backups.iterdir())
        broken = self.backups / names[-1] / "core.sqlite"
        broken.chmod(0o600)
        with open(broken, "r+b") as handle:
            handle.seek(200)
            handle.write(b"\x00corrupt\x00")
        manifest, hashed = self._hashed()
        self.assertEqual(manifest["snapshot_id"], names[-2])
        self.assertEqual(set(hashed), {names[-1], names[-2]})


class ServiceIndexRetryTests(unittest.TestCase):
    def test_a_deferred_build_is_retried_rather_than_forgotten(self) -> None:
        from dalton_core import service

        instance = service.DaltonService.__new__(service.DaltonService)
        instance.config = type("C", (), {"core_db": Path("core"), "scheduler_db": Path("s")})()
        reports = [
            {"core": {"created": [], "present": [], "deferred": {
                "claim_versions_created_at": "OperationalError: database is locked"}},
             "scheduler": None},
            {"core": {"created": [], "present": [], "deferred": {
                "observability_work_order_links_created_at": "table is absent"}},
             "scheduler": None},
        ]
        calls = []

        def apply(**kwargs):
            calls.append(kwargs)
            return reports[len(calls) - 1]

        clock = [1000.0]
        with patch("dalton_core.migrations.apply_projection_indexes", apply), \
                patch.object(service.time, "monotonic", lambda: clock[0]):
            instance._ensure_projection_indexes()
            self.assertFalse(getattr(instance, "_projection_indexes_applied", False))
            instance._ensure_projection_indexes()  # too soon: not retried
            self.assertEqual(len(calls), 1)
            clock[0] += service.PROJECTION_INDEX_RETRY_SECONDS + 1
            instance._ensure_projection_indexes()
            self.assertEqual(len(calls), 2)
            self.assertTrue(instance._projection_indexes_applied)
            clock[0] += service.PROJECTION_INDEX_RETRY_SECONDS + 1
            instance._ensure_projection_indexes()
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
