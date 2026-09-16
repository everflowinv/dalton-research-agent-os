"""WP-B1: the dashboard projection stops reading the whole database.

One rebuild read 660 MB of work-order JSON to reach one field, sorted every
model invocation through a TEMP B-TREE for want of an index, and was allowed
to start again two seconds after the last one began.  The controller sat at
12% CPU around the clock and the WAL never reached a checkpoint.
"""

import json
import sqlite3
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dalton_core.dashboard_projector import (
    _WORK_ORDER_CACHE,
    _json_array,
    _scheduled_work_orders,
)
from dalton_core.migrations import (
    CORE_PROJECTION_INDEXES,
    SCHEDULER_PROJECTION_INDEXES,
    apply_projection_indexes,
    ensure_indexes,
)
from dalton_core.service import DEFAULT_PROJECTION_MIN_INTERVAL_SECONDS

_WORK_ORDERS_DDL = """
CREATE TABLE scheduler_work_orders (
    work_order_id TEXT PRIMARY KEY,
    work_order_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _work_order_db(path: Path, rows):
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.executescript(_WORK_ORDERS_DDL)
    for work_id, capabilities, created_at in rows:
        connection.execute(
            "INSERT INTO scheduler_work_orders VALUES (?,?,?)",
            (work_id, json.dumps({"requested_capabilities": capabilities}), created_at),
        )
    connection.row_factory = sqlite3.Row
    return connection


class ScheduledWorkOrderCacheTests(unittest.TestCase):
    def setUp(self):
        _WORK_ORDER_CACHE.clear()
        self.addCleanup(_WORK_ORDER_CACHE.clear)
        self.root = TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.path = Path(self.root.name) / "scheduler.sqlite"

    def test_it_reads_the_capabilities_and_the_created_at_and_nothing_else(self):
        connection = _work_order_db(self.path, [
            ("work:1", ["capability:a"], "2026-09-01T00:00:00Z"),
        ])
        scheduled = _scheduled_work_orders(connection, self.path)
        self.assertEqual(
            scheduled,
            {"work:1": {"created_at": "2026-09-01T00:00:00Z",
                        "wire": {"requested_capabilities": ["capability:a"]}}},
        )

    def test_a_second_round_reads_only_what_arrived_since(self):
        connection = _work_order_db(self.path, [
            ("work:1", ["capability:a"], "2026-09-01T00:00:00Z"),
            ("work:2", ["capability:b"], "2026-09-02T00:00:00Z"),
        ])
        _scheduled_work_orders(connection, self.path)
        connection.execute(
            "INSERT INTO scheduler_work_orders VALUES (?,?,?)",
            ("work:3", json.dumps({"requested_capabilities": ["capability:c"]}),
             "2026-09-03T00:00:00Z"),
        )
        # Count the rows SQLite actually hands back, which is the thing the
        # change is for: the cost is the 26 KB documents, not the row count.
        reads = []
        connection.set_trace_callback(reads.append)
        scheduled = _scheduled_work_orders(connection, self.path)
        connection.set_trace_callback(None)
        self.assertEqual(set(scheduled), {"work:1", "work:2", "work:3"})
        incremental = [sql for sql in reads if "WHERE created_at >= " in sql]
        self.assertEqual(len(incremental), 1)
        self.assertFalse(
            [sql for sql in reads
             if "FROM scheduler_work_orders" in sql
             and "WHERE" not in sql and "COUNT" not in sql],
            "a full re-read happened when only three rows existed",
        )

    def test_two_work_orders_sharing_a_timestamp_are_both_seen(self):
        # The reason the watermark comparison is ``>=`` and not ``>``.
        connection = _work_order_db(self.path, [
            ("work:1", ["capability:a"], "2026-09-01T00:00:00Z"),
        ])
        _scheduled_work_orders(connection, self.path)
        connection.execute(
            "INSERT INTO scheduler_work_orders VALUES (?,?,?)",
            ("work:2", json.dumps({"requested_capabilities": []}),
             "2026-09-01T00:00:00Z"),
        )
        self.assertEqual(set(_scheduled_work_orders(connection, self.path)),
                         {"work:1", "work:2"})

    def test_a_replaced_database_is_reloaded_rather_than_mixed(self):
        connection = _work_order_db(self.path, [
            ("work:1", ["capability:a"], "2026-09-01T00:00:00Z"),
            ("work:2", ["capability:b"], "2026-09-02T00:00:00Z"),
        ])
        _scheduled_work_orders(connection, self.path)
        connection.close()
        self.path.unlink()
        replaced = _work_order_db(self.path, [
            ("work:9", ["capability:z"], "2026-08-01T00:00:00Z"),
        ])
        # A restore put an older, smaller database at the same path.  The
        # count disagrees with the cache, so the round starts over instead of
        # projecting rows from a database that no longer exists.
        self.assertEqual(set(_scheduled_work_orders(replaced, self.path)), {"work:9"})

    def test_a_work_order_that_requested_nothing_is_an_empty_list(self):
        connection = sqlite3.connect(str(self.path), isolation_level=None)
        connection.executescript(_WORK_ORDERS_DDL)
        connection.execute("INSERT INTO scheduler_work_orders VALUES (?,?,?)",
                           ("work:1", json.dumps({}), "2026-09-01T00:00:00Z"))
        connection.row_factory = sqlite3.Row
        scheduled = _scheduled_work_orders(connection, self.path)
        self.assertEqual(scheduled["work:1"]["wire"]["requested_capabilities"], [])


class JsonArrayTests(unittest.TestCase):
    def test_sql_null_is_an_absent_field_not_a_broken_row(self):
        self.assertEqual(_json_array(None, "capabilities"), [])

    def test_a_non_array_is_refused(self):
        from dalton_core.dashboard_projector import ProjectionSourceError

        with self.assertRaises(ProjectionSourceError):
            _json_array('{"not": "an array"}', "capabilities")
        with self.assertRaises(ProjectionSourceError):
            _json_array("not json at all", "capabilities")


class ProjectionIndexTests(unittest.TestCase):
    def setUp(self):
        self.root = TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.path = Path(self.root.name) / "scheduler.sqlite"
        self.connection = sqlite3.connect(str(self.path), isolation_level=None)
        self.addCleanup(self.connection.close)
        self.connection.executescript(
            "CREATE TABLE scheduler_leases (lease_revision_id TEXT PRIMARY KEY,"
            " work_order_id TEXT NOT NULL, attempt_number INTEGER NOT NULL,"
            " lease_version INTEGER NOT NULL, expires_at TEXT NOT NULL,"
            " created_at TEXT NOT NULL);"
        )

    def test_a_missing_index_is_built_and_then_merely_present(self):
        specs = [SCHEDULER_PROJECTION_INDEXES[0]]
        first = ensure_indexes(self.connection, specs)
        self.assertEqual([item["name"] for item in first["created"]],
                         [specs[0].name])
        second = ensure_indexes(self.connection, specs)
        self.assertEqual(second["present"], [specs[0].name])
        self.assertEqual(second["created"], [])

    def test_an_absent_table_is_deferred_rather_than_raised(self):
        report = ensure_indexes(self.connection, SCHEDULER_PROJECTION_INDEXES)
        self.assertIn("scheduler_result_envelopes_work_order_attempt",
                      report["deferred"])
        self.assertEqual(
            sorted(item["name"] for item in report["created"]),
            ["scheduler_leases_created_at", "scheduler_leases_work_order_attempt"],
        )

    def test_it_gives_the_connections_busy_timeout_back(self):
        # The store sets 15 s deliberately and an index build must not leave
        # it at five; a test asserts that number elsewhere.
        self.connection.execute("PRAGMA busy_timeout = 15000")
        ensure_indexes(self.connection, SCHEDULER_PROJECTION_INDEXES)
        self.assertEqual(
            self.connection.execute("PRAGMA busy_timeout").fetchone()[0], 15000)

    def test_the_index_is_the_one_the_projection_partitions_by(self):
        ensure_indexes(self.connection, SCHEDULER_PROJECTION_INDEXES)
        plan = self.connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM scheduler_leases "
            "WHERE work_order_id=? AND attempt_number=?", ("work:1", 1)
        ).fetchall()
        self.assertIn("scheduler_leases_work_order_attempt",
                      " ".join(str(row[-1]) for row in plan))

    def test_a_budget_stops_the_open_from_building_everything(self):
        from dalton_core.migrations import OPEN_BUDGET_SECONDS

        self.assertGreater(OPEN_BUDGET_SECONDS, 0.0)
        report = ensure_indexes(
            self.connection, SCHEDULER_PROJECTION_INDEXES, budget_seconds=0.0)
        # Nothing was built and nothing failed: the next open continues, and
        # meanwhile the writer started on time.
        self.assertEqual(report["created"], [])
        self.assertIn("scheduler_leases_work_order_attempt", report["deferred"])

    def test_without_a_budget_everything_that_can_be_built_is(self):
        report = ensure_indexes(self.connection, SCHEDULER_PROJECTION_INDEXES)
        self.assertEqual(
            {item["name"] for item in report["created"]},
            {"scheduler_leases_work_order_attempt", "scheduler_leases_created_at"},
        )

    def test_an_absent_database_is_reported_not_created(self):
        report = apply_projection_indexes(
            core_db=Path(self.root.name) / "nope.sqlite", scheduler_db=None)
        self.assertEqual(report["core"]["deferred"], {"database": "absent"})
        self.assertIsNone(report["scheduler"])

    def test_every_index_names_the_read_it_exists_for(self):
        for spec in CORE_PROJECTION_INDEXES + SCHEDULER_PROJECTION_INDEXES:
            self.assertTrue(spec.reason.strip(), spec.name)
            self.assertIn("CREATE INDEX IF NOT EXISTS", spec.statement)

    def test_the_watermark_tables_have_not_drifted(self):
        # ``migrations`` repeats these lists so that the store can import it
        # without importing the projector.  This is what keeps the copy true:
        # a table added to the fingerprint and not here would silently go back
        # to a full scan per round.
        from dalton_core.dashboard_projector import (
            _AGENDA_TABLES,
            _CONNECTOR_PROJECTION_TABLES,
            _CORE_TABLES,
            _SCHEDULER_TABLES,
        )
        from dalton_core.migrations.projection_indexes import (
            WATERMARK_CORE_TABLES,
            WATERMARK_SCHEDULER_TABLES,
        )

        expected_core = {
            table for table in
            _CORE_TABLES | _AGENDA_TABLES | _CONNECTOR_PROJECTION_TABLES
            if not table.endswith("_pointer")
        }
        self.assertEqual(set(WATERMARK_CORE_TABLES), expected_core)
        self.assertEqual(set(WATERMARK_SCHEDULER_TABLES), set(_SCHEDULER_TABLES))

    def test_every_watermark_table_gets_a_created_at_index(self):
        from dalton_core.migrations.projection_indexes import (
            WATERMARK_CORE_TABLES,
            WATERMARK_SCHEDULER_TABLES,
        )

        for tables, specs in (
            (WATERMARK_CORE_TABLES, CORE_PROJECTION_INDEXES),
            (WATERMARK_SCHEDULER_TABLES, SCHEDULER_PROJECTION_INDEXES),
        ):
            indexed = {spec.table for spec in specs if spec.columns == "created_at"}
            self.assertEqual(set(tables) - indexed, set())

    def test_the_formal_results_work_order_is_deliberately_not_indexed_again(self):
        # Its work_order_id is already UNIQUE, so SQLite has indexed it since
        # the table was created; a second index would only cost space on a
        # 1 GB database.  Its created_at is a different column and is indexed,
        # because the watermark reads MAX(created_at) from it every round.
        columns = {
            (spec.table, spec.columns) for spec in SCHEDULER_PROJECTION_INDEXES
        }
        self.assertNotIn(("scheduler_formal_results", "work_order_id"), columns)
        self.assertIn(("scheduler_formal_results", "created_at"), columns)


class StoreAppliesCoreIndexesTests(unittest.TestCase):
    def _index_names(self, connection):
        return {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}

    def test_opening_a_store_builds_the_index_for_its_own_table(self):
        from dalton_core.store import DaltonStore

        with TemporaryDirectory() as root:
            with DaltonStore(Path(root) / "core.sqlite") as store:
                self.assertIn("model_invocations_created_at_invocation",
                              self._index_names(store.connection))

    def test_an_index_whose_table_belongs_to_another_authority_waits_for_it(self):
        # ``observability_work_order_links`` is created by ObservabilityStore,
        # not by the core schema, so the first open defers that one and the
        # open that follows the authority builds it.  Deferring is the design:
        # nothing here may fail because a table is not there yet.
        from dalton_core.observability import ObservabilityStore
        from dalton_core.store import DaltonStore

        with TemporaryDirectory() as root:
            path = Path(root) / "core.sqlite"
            with DaltonStore(path) as store:
                self.assertNotIn("observability_work_order_links_created_at_link",
                                 self._index_names(store.connection))
                ObservabilityStore(store)
            with DaltonStore(path) as reopened:
                self.assertIn("observability_work_order_links_created_at_link",
                              self._index_names(reopened.connection))

    def test_an_index_that_cannot_be_built_does_not_fail_the_open(self):
        from unittest import mock

        from dalton_core.store import DaltonStore

        with TemporaryDirectory() as root:
            path = Path(root) / "core.sqlite"
            with mock.patch(
                "dalton_core.migrations.ensure_indexes",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                # A slow projection is not a writer that will not start.
                with DaltonStore(path) as store:
                    self.assertNotIn("model_invocations_created_at_invocation",
                                     self._index_names(store.connection))


class ProjectionIntervalTests(unittest.TestCase):
    def test_the_default_is_a_minute_not_two_seconds(self):
        self.assertEqual(DEFAULT_PROJECTION_MIN_INTERVAL_SECONDS, 60.0)

    def test_a_config_without_the_key_takes_the_default(self):
        from dalton_core.service import ServiceConfig

        raw = _service_mapping()
        raw.pop("projection_min_interval_seconds")
        config = ServiceConfig.from_mapping(raw)
        self.assertEqual(config.projection_min_interval_seconds,
                         DEFAULT_PROJECTION_MIN_INTERVAL_SECONDS)

    def test_a_config_that_names_it_still_wins(self):
        from dalton_core.service import ServiceConfig

        raw = _service_mapping()
        raw["projection_min_interval_seconds"] = 5
        self.assertEqual(
            ServiceConfig.from_mapping(raw).projection_min_interval_seconds, 5.0)

    def test_a_fresh_install_writes_the_new_default(self):
        import re

        source = Path(
            "src/dalton_core/bootstrap.py"
        ).read_text(encoding="utf-8")
        match = re.search(r'"projection_min_interval_seconds":\s*(\d+)', source)
        self.assertIsNotNone(match)
        self.assertEqual(int(match.group(1)),
                         int(DEFAULT_PROJECTION_MIN_INTERVAL_SECONDS))


def _service_mapping():
    from dalton_core.service import SCHEMA_VERSION

    return {
        "schema_version": SCHEMA_VERSION,
        "core_db": "/tmp/wpb1-core.sqlite",
        "scheduler_db": "/tmp/wpb1-scheduler.sqlite",
        "projection_db": "/tmp/wpb1-projection.sqlite",
        "model_router_db": None,
        "capability_catalog_db": None,
        "heartbeat_path": "/tmp/wpb1-heartbeat.json",
        "writer_socket": "/tmp/wpb1-writer.sock",
        "tick_seconds": 5,
        "projection_min_interval_seconds": 2,
        "plugin_retry_seconds": 60,
        "plugins": [],
    }


if __name__ == "__main__":
    unittest.main()
