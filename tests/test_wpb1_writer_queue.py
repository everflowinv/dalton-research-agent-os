"""WP-B1: the writer's single store thread stops being the whole system.

Three things are tested here because they are three halves of one failure.  A
lane whose refusal is a fact about an owner decision was rediscovered every
tick, on the store thread, for thirty seconds (B1-1).  Reads queued behind it
(B1-2).  And when the thirty seconds ran out, client and server expired in the
same instant, so the record said "transport error" and never said which of the
two had happened (B1-3).
"""

import concurrent.futures
import json
import socket
import sqlite3
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from dalton_core import writer_client
from dalton_core.writer_client import WriterClient, _client_timeout
from dalton_core.writer_protocol import decode_frame, parse_response, request_frame
from dalton_core.writer_server import (
    CLIENT_REQUEST_TIMEOUT,
    LANE_SOFT_BUDGET_SECONDS,
    OPERATION_FIELDS,
    READ_ONLY_OPERATIONS,
    STORE_REQUEST_TIMEOUT,
    LaneShortCircuit,
    WriterServer,
    _governance_signature,
    _lane_hold_kind,
    _quota_window_seconds,
    lane_budget_remaining,
)


class _Rejected(RuntimeError):
    """Stands in for feed_launcher.FeedLaunchRejected, matched by name."""


_Rejected.__name__ = "FeedLaunchRejected"


class _Quota(RuntimeError):
    pass


_Quota.__name__ = "ConnectorQuotaExceeded"


class LaneHoldClassificationTests(unittest.TestCase):
    def test_an_unapproved_governance_record_is_a_governance_hold(self):
        exc = _Rejected(
            "list_notes governance record is not approved; owner approval is required"
        )
        self.assertEqual(_lane_hold_kind(exc), "governance")

    def test_a_missing_record_is_also_a_governance_hold(self):
        exc = _Rejected("get_note governance record is missing; owner approval is required")
        self.assertEqual(_lane_hold_kind(exc), "governance")

    def test_a_spent_quota_is_a_quota_hold(self):
        self.assertEqual(_lane_hold_kind(_Quota("connector prior-research quota exceeded")),
                         "quota")

    def test_a_lane_that_says_blocked_with_its_reason_code_is_a_hold(self):
        self.assertEqual(
            _lane_hold_kind({"status": "blocked",
                             "reason_code": "connector_governance_rejected"}),
            "governance",
        )
        self.assertEqual(
            _lane_hold_kind({"status": "unavailable",
                             "reason_code": "connector_quota_exhausted"}),
            "quota",
        )
        self.assertEqual(_lane_hold_kind({"status": "unconfigured"}), "unconfigured")

    def test_a_transient_failure_is_never_a_hold(self):
        # This is the important negative: a lane held on a timeout or a
        # network fault would go dark until an hour passed, and the fault may
        # be gone on the next tick.
        self.assertIsNone(_lane_hold_kind(TimeoutError("store is busy")))
        self.assertIsNone(_lane_hold_kind(_Rejected("ticket digest must be 24 hex characters")))
        self.assertIsNone(_lane_hold_kind({"status": "dispatched", "read": 3}))
        self.assertIsNone(_lane_hold_kind({"status": "busy"}))
        self.assertIsNone(_lane_hold_kind(None))


class LaneShortCircuitTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.holds = LaneShortCircuit(
            monotonic=lambda: self.clock[0],
            now=lambda: datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc),
        )

    def test_a_recorded_hold_answers_the_next_tick_without_running_the_lane(self):
        self.holds.record(
            "dispatch_sales_notes_feed", "governance",
            reason="list_notes governance record is not approved",
            fingerprint="fp-1",
            detail={"source_ref": "source:sales-notes", "operation": "list_notes"},
        )
        held = self.holds.held("dispatch_sales_notes_feed", "fp-1")
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["reason_code"], "lane_governance_hold")
        self.assertEqual(held["reason"],
                         "list_notes governance record is not approved")
        # The owner's action is in the owner's language, and it names what to
        # do rather than what went wrong.
        self.assertIn("批准", held["owner_action"])
        self.assertEqual(held["source_ref"], "source:sales-notes")
        self.assertEqual(held["short_circuit"]["kind"], "governance")
        self.assertEqual(held["short_circuit"]["hits"], 1)
        self.assertEqual(
            self.holds.held("dispatch_sales_notes_feed", "fp-1")["short_circuit"]["hits"], 2
        )

    def test_a_changed_fingerprint_clears_the_hold_on_the_spot(self):
        self.holds.record("dispatch_prior_research", "governance",
                          reason="not approved", fingerprint="proposed|abc")
        self.assertIsNotNone(self.holds.held("dispatch_prior_research", "proposed|abc"))
        # The owner approved the record: its status and content hash moved, so
        # the lane runs again on the very next tick, with no restart.
        self.assertIsNone(self.holds.held("dispatch_prior_research", "approved|def"))
        self.assertEqual(self.holds.snapshot(), {})

    def test_a_hold_expires_and_the_lane_is_tried_again(self):
        self.holds.record("dispatch_company_wiki_feed", "quota",
                          reason="quota exceeded", fingerprint="fp", ttl_seconds=60.0)
        self.assertIsNotNone(self.holds.held("dispatch_company_wiki_feed", "fp"))
        self.clock[0] += 61.0
        self.assertIsNone(self.holds.held("dispatch_company_wiki_feed", "fp"))

    def test_re_observing_the_same_fact_keeps_the_original_since(self):
        first = self.holds.record("dispatch_prior_research", "governance",
                                  reason="not approved", fingerprint="fp")
        self.clock[0] += 10.0
        again = self.holds.record("dispatch_prior_research", "governance",
                                  reason="not approved", fingerprint="fp")
        self.assertEqual(again.recorded_at, first.recorded_at)

    def test_the_snapshot_is_what_a_heartbeat_would_print(self):
        self.holds.record("dispatch_prior_research", "governance",
                          reason="not approved", fingerprint="fp")
        snapshot = self.holds.snapshot()
        self.assertEqual(list(snapshot), ["dispatch_prior_research"])
        self.assertEqual(snapshot["dispatch_prior_research"]["status"], "held")
        self.assertGreater(snapshot["dispatch_prior_research"]["expires_in_seconds"], 0)


class QuotaWindowTests(unittest.TestCase):
    def test_a_quota_hold_lasts_until_the_next_utc_day(self):
        seconds = _quota_window_seconds(
            datetime(2026, 9, 16, 23, 0, tzinfo=timezone.utc))
        self.assertAlmostEqual(seconds, 3600.0, places=1)

    def test_it_is_never_zero_even_at_midnight(self):
        self.assertGreaterEqual(
            _quota_window_seconds(datetime(2026, 9, 16, 23, 59, 59, 999999,
                                           tzinfo=timezone.utc)),
            1.0,
        )


class GovernanceSignatureTests(unittest.TestCase):
    def test_the_signature_is_the_records_own_status_and_hash(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "sales-notes-list-notes-v1.json"
            path.write_text(json.dumps({"status": "proposed", "content_hash": "sha256:aa"}))
            proposed = _governance_signature(path)
            self.assertEqual(proposed, "proposed|sha256:aa")
            # Approving it moves the signature, which is what releases the
            # hold.  Note that the mtime is irrelevant here: the fact that
            # changed is the owner's decision, not the timestamp.
            path.write_text(json.dumps({"status": "approved", "content_hash": "sha256:aa"}))
            self.assertNotEqual(_governance_signature(path), proposed)

    def test_an_unreadable_record_falls_back_to_size_and_mtime(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "broken.json"
            path.write_text("{not json")
            self.assertTrue(_governance_signature(path).startswith("stat|"))

    def test_an_absent_record_is_absent_rather_than_an_error(self):
        self.assertEqual(_governance_signature("/nonexistent/none.json"), "absent")


class _Lane:
    def __init__(self, operation, handler, init_kwarg=None):
        self.operation = operation
        self.handler = handler
        self.init_kwarg = init_kwarg


class RunLaneTests(unittest.TestCase):
    """``_run_lane`` on a server object with only what the method touches."""

    def _server(self):
        server = object.__new__(WriterServer)
        server.lane_holds = LaneShortCircuit()
        server._lane_deadline = None
        server._lane_launchers = {}
        return server

    def test_a_refusing_lane_is_held_and_reports_the_owner_action(self):
        server = self._server()

        def handler(_server, _params):
            raise _Rejected(
                "list_notes governance record is not approved; owner approval is required"
            )

        with mock.patch("dalton_core.writer_server.lane_for_operation",
                        return_value=None):
            result = server._run_lane(_Lane("dispatch_sales_notes_feed", handler), {})
        self.assertEqual(result["status"], "held")
        self.assertIn("批准", result["owner_action"])
        self.assertIn("dispatch_sales_notes_feed", server.lane_holds.snapshot())

    def test_a_lane_that_succeeds_clears_an_earlier_hold(self):
        server = self._server()
        server.lane_holds.record("dispatch_prior_research", "governance",
                                 reason="not approved", fingerprint="fp")
        with mock.patch("dalton_core.writer_server.lane_for_operation",
                        return_value=None):
            server._run_lane(
                _Lane("dispatch_prior_research", lambda s, p: {"status": "dispatched"}), {})
        self.assertEqual(server.lane_holds.snapshot(), {})

    def test_a_lane_over_its_budget_reports_busy_and_keeps_its_own_status(self):
        server = self._server()

        def slow(_server, _params):
            # The budget is a deadline the handler may read, never a timer
            # that stops it; the overrun is reported after the fact.
            self.assertIsNotNone(lane_budget_remaining(_server))
            time.sleep(0.05)
            return {"status": "dispatched", "read": 1}

        with mock.patch("dalton_core.writer_server.LANE_SOFT_BUDGET_SECONDS", 0.01), \
                mock.patch("dalton_core.writer_server.lane_for_operation", return_value=None):
            result = server._run_lane(_Lane("dispatch_prior_research", slow), {})
        self.assertEqual(result["status"], "busy")
        self.assertEqual(result["lane_status"], "dispatched")
        self.assertGreater(result["over_budget_seconds"], 0)

    def test_the_budget_is_gone_once_the_lane_returns(self):
        server = self._server()
        with mock.patch("dalton_core.writer_server.lane_for_operation", return_value=None):
            server._run_lane(_Lane("dispatch_prior_research", lambda s, p: {}), {})
        self.assertIsNone(lane_budget_remaining(server))

    def test_a_lane_raising_something_transient_still_raises(self):
        server = self._server()

        def boom(_server, _params):
            raise TimeoutError("store is busy")

        with mock.patch("dalton_core.writer_server.lane_for_operation", return_value=None):
            with self.assertRaises(TimeoutError):
                server._run_lane(_Lane("dispatch_prior_research", boom), {})
        self.assertEqual(server.lane_holds.snapshot(), {})


class ReadOnlyOperationTests(unittest.TestCase):
    def test_every_read_operation_is_a_real_operation(self):
        self.assertTrue(READ_ONLY_OPERATIONS)
        self.assertEqual(READ_ONLY_OPERATIONS - set(OPERATION_FIELDS), set())

    def test_no_read_operation_is_a_lane_tick_or_a_write(self):
        """A few reads sit in HUMAN_GOVERNANCE_OPERATIONS because reading a
        constitution is governance-scoped; those are still reads.  What must
        never appear here is an operation that stages, verifies, registers or
        adjudicates."""

        from dalton_core.writer_server import (
            ADJUDICATOR_OPERATIONS,
            RESEARCHER_OPERATIONS,
            VERIFIER_OPERATIONS,
            WORKER_OPERATIONS,
        )
        from dalton_core.lane_registry import lane_operations

        self.assertEqual(READ_ONLY_OPERATIONS & lane_operations(), frozenset())
        for writes in (WORKER_OPERATIONS, VERIFIER_OPERATIONS,
                       RESEARCHER_OPERATIONS, ADJUDICATOR_OPERATIONS):
            self.assertEqual(READ_ONLY_OPERATIONS & writes, frozenset())

    def test_a_read_operation_looks_like_one(self):
        # The cheap half of the audit: a name that does not read like a read
        # is a name somebody should look at again before it joins the set.
        for operation in READ_ONLY_OPERATIONS:
            self.assertTrue(
                operation.startswith(("list_", "get_", "active_", "current_",
                                      "answer_", "intent_"))
                or operation.endswith(("_report", "_targets", "_bindings")),
                operation,
            )

    def test_reads_go_to_the_read_executor_and_writes_to_the_store(self):
        server = object.__new__(WriterServer)
        server._store_executor = "store"
        server._read_executor = "read"
        executor, handler = server._executor_for("list_agenda_feedback_targets")
        self.assertEqual(executor, "read")
        self.assertEqual(handler.__name__, "_handle_read")
        executor, handler = server._executor_for("register_invocation")
        self.assertEqual(executor, "store")
        self.assertEqual(handler.__name__, "_handle")

    def test_a_server_without_a_read_executor_keeps_the_store_thread(self):
        server = object.__new__(WriterServer)
        server._store_executor = "store"
        executor, handler = server._executor_for("list_agenda_feedback_targets")
        self.assertEqual(executor, "store")
        self.assertEqual(handler.__name__, "_handle")

    def test_a_replica_that_will_not_open_falls_back_rather_than_failing(self):
        server = object.__new__(WriterServer)
        server._read_local = threading.local()
        server._read_replicas = []
        server._read_replica_lock = threading.Lock()
        server._handle = lambda request: "answered by the store thread"
        with mock.patch.object(WriterServer, "_open_read_replica",
                               side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual(server._handle_read(object()), "answered by the store thread")
            # And it does not retry the failed open on every request.
            self.assertEqual(server._handle_read(object()), "answered by the store thread")


class ReadReplicaEndToEndTests(unittest.TestCase):
    """The replica is only worth having if a read really goes through it."""

    def setUp(self):
        from dalton_core.writer_server import CORE_OPERATIONS, Principal

        self.root = TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        root = Path(self.root.name)
        self.socket_path = str(root / "writer.sock")
        from dalton_core.writer_server import HUMAN_GOVERNANCE_OPERATIONS

        principals = {
            "core": Principal("core", "core-token", CORE_OPERATIONS, unrestricted=True),
            "governance": Principal(
                "governance", "governance-token", HUMAN_GOVERNANCE_OPERATIONS,
                actor_ref="human:owner",
            ),
        }
        self.server = WriterServer(root / "core.sqlite", self.socket_path, principals)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = WriterClient(self.socket_path, "core-token", timeout=10)
        self.governance = WriterClient(self.socket_path, "governance-token", timeout=10)

    def test_a_read_is_answered_and_the_store_thread_is_not_used(self):
        answered = self.client.call("list_events", {})
        self.assertIsInstance(answered, list)
        # It went to a read thread, and that thread built a replica: the whole
        # point is that this answer did not queue behind whatever the store
        # thread is doing.
        self.assertEqual(len(self.server._read_replicas), 1)
        replica = self.server._read_replicas[0]
        self.assertIsNot(replica._store, self.server._store)
        self.assertIsNot(replica._agenda, self.server._agenda)

    def test_a_write_still_goes_to_the_one_store_thread(self):
        self.client.call("active_policy", {})
        self.assertEqual(self.server._read_replicas, [])

    def test_the_replica_reads_the_same_database(self):
        # WAL, not isolation: the replica is a second handle on the same file,
        # so a committed write on the store thread is visible to it.  That it
        # has to be a second handle rather than a second user of the first is
        # SQLite's rule -- a connection belongs to the thread that made it --
        # and is why this exists at all rather than a shared connection.
        self.client.call("list_events", {})
        replica = self.server._read_replicas[0]
        self.assertEqual(replica._store.path, self.server._store.path)
        self.assertIsNot(replica._store.connection, self.server._store.connection)

    def test_a_read_does_not_wait_for_a_busy_store_thread(self):
        # The failure this whole work package is about: a lane holding the one
        # store thread used to hold every read behind it.
        released = threading.Event()
        self.addCleanup(released.set)
        self.server._store_executor.submit(released.wait)
        answered = []
        reader = threading.Thread(
            target=lambda: answered.append(self.client.call("list_events", {})))
        reader.start()
        reader.join(5)
        self.assertFalse(reader.is_alive(), "the read queued behind the store thread")
        self.assertEqual(len(answered), 1)

    def test_stopping_the_writer_closes_the_replica(self):
        self.client.call("list_events", {})
        replica = self.server._read_replicas[0]
        self.server.stop()
        self.assertEqual(self.server._read_replicas, [])
        with self.assertRaises(sqlite3.ProgrammingError):
            replica._store.connection.execute("SELECT 1")


class QueueTimeoutAttributionTests(unittest.TestCase):
    """B1-3: the error frame says which of the two timeouts happened."""

    def _server(self, executor, handler):
        server = object.__new__(WriterServer)
        server._store_executor = executor
        server._stop = threading.Event()
        server._handle = handler
        server._log_unhandled = mock.Mock()
        return server

    def _call(self, server):
        client, peer = socket.socketpair()
        thread = threading.Thread(target=server._serve_connection, args=(peer,))
        thread.start()
        client.sendall(request_frame("r-1", "register_invocation", {}, "token"))
        line = client.makefile("rb").readline()
        client.close()
        thread.join(2)
        peer.close()
        return parse_response(decode_frame(line))

    @mock.patch("dalton_core.writer_server.STORE_REQUEST_TIMEOUT", 0.05)
    def test_a_request_the_queue_never_reached_is_queued_timeout(self):
        blocker = threading.Event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            occupied = executor.submit(blocker.wait)
            server = self._server(executor, lambda request: None)
            response = self._call(server)
            self.assertFalse(response.ok)
            self.assertEqual(response.error["code"], "queued_timeout")
            self.assertIn("queue", response.error["message"])
            blocker.set()
            occupied.result(2)

    @mock.patch("dalton_core.writer_server.STORE_REQUEST_TIMEOUT", 0.05)
    def test_a_request_that_started_is_store_timeout(self):
        release = threading.Event()

        def handler(request):
            release.wait(2)
            return {"status": "completed"}

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            server = self._server(executor, handler)
            response = self._call(server)
            self.assertFalse(response.ok)
            self.assertEqual(response.error["code"], "store_timeout")
            release.set()
            executor.shutdown(wait=True)


class ClientTimeoutTests(unittest.TestCase):
    def test_the_client_outlives_the_server(self):
        self.assertGreater(CLIENT_REQUEST_TIMEOUT, STORE_REQUEST_TIMEOUT)
        self.assertGreater(writer_client.DEFAULT_TIMEOUT,
                           writer_client.SERVER_REQUEST_TIMEOUT)

    def test_the_repeated_server_timeout_has_not_drifted(self):
        # writer_client deliberately imports nothing from the server; this is
        # the assertion that keeps the copied number honest.
        self.assertEqual(writer_client.SERVER_REQUEST_TIMEOUT, STORE_REQUEST_TIMEOUT)

    def test_a_caller_asking_for_exactly_the_server_deadline_is_raised(self):
        self.assertEqual(_client_timeout(30.0), writer_client.DEFAULT_TIMEOUT)
        self.assertEqual(_client_timeout(31.0), writer_client.DEFAULT_TIMEOUT)

    def test_a_deliberately_short_caller_keeps_its_own_deadline(self):
        # A probe that would rather abandon the request than block a page
        # render has already decided; nothing is misattributed because it was
        # never going to read the answer.
        self.assertEqual(_client_timeout(2.0), 2.0)

    def test_a_longer_caller_is_left_alone(self):
        self.assertEqual(_client_timeout(120.0), 120.0)

    def test_the_constructed_client_carries_the_raised_value(self):
        client = WriterClient("/tmp/does-not-matter.sock", "token", timeout=30)
        self.assertEqual(client.timeout, writer_client.DEFAULT_TIMEOUT)

    def test_the_default_is_above_the_server_deadline(self):
        client = WriterClient("/tmp/does-not-matter.sock", "token")
        self.assertGreater(client.timeout, STORE_REQUEST_TIMEOUT)


class LaneBudgetTests(unittest.TestCase):
    def test_no_budget_means_the_caller_keeps_its_own_bound(self):
        self.assertIsNone(lane_budget_remaining(object()))
        self.assertEqual(lane_budget_remaining(object(), default=8.0), 8.0)

    def test_the_budget_counts_down(self):
        server = object.__new__(WriterServer)
        server._lane_deadline = time.monotonic() + LANE_SOFT_BUDGET_SECONDS
        first = lane_budget_remaining(server)
        self.assertLessEqual(first, LANE_SOFT_BUDGET_SECONDS)
        self.assertGreater(first, 0.0)

    def test_an_exhausted_budget_is_zero_and_never_negative(self):
        server = object.__new__(WriterServer)
        server._lane_deadline = time.monotonic() - 5.0
        self.assertEqual(lane_budget_remaining(server), 0.0)


if __name__ == "__main__":
    unittest.main()


class LaneFingerprintMissionTests(unittest.TestCase):
    """Publishing a research goal must lift a "no mission" hold on the next tick.

    Live 2026-09-17: a new environment published its first goal and fourteen
    lanes stayed held on the refusal recorded before it existed, because the
    fingerprint watched only governance records and the UTC day.
    """

    def _server(self, connection):
        from types import SimpleNamespace
        fake = SimpleNamespace(
            _now=lambda: datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
            _lane_launcher=lambda lane: SimpleNamespace(),
            store=SimpleNamespace(connection=connection),
        )
        fake._mission_pointer_signature = (
            lambda: WriterServer._mission_pointer_signature(fake))
        return fake

    def test_the_active_mission_is_part_of_every_lane_fingerprint(self):
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE coverage_mission_pointer (mission_ref TEXT, mission_version_id TEXT)")
        fake = self._server(connection)
        before = WriterServer.lane_fingerprint(fake, "dispatch_debate_map")
        connection.execute("INSERT INTO coverage_mission_pointer VALUES (?, ?)",
                           ("coverage-mission:x", "coverage-mission-version:x:1"))
        after = WriterServer.lane_fingerprint(fake, "dispatch_debate_map")
        self.assertNotEqual(before, after)
        connection.execute("UPDATE coverage_mission_pointer SET mission_version_id=?",
                           ("coverage-mission-version:x:2",))
        self.assertNotEqual(after, WriterServer.lane_fingerprint(fake, "dispatch_debate_map"))
        # A launcher-less lane watches the mission too.
        fake_absent = self._server(connection)
        fake_absent._lane_launcher = lambda lane: None
        first = WriterServer.lane_fingerprint(fake_absent, "dispatch_debate_map")
        connection.execute("DELETE FROM coverage_mission_pointer")
        self.assertNotEqual(first, WriterServer.lane_fingerprint(fake_absent, "dispatch_debate_map"))

    def test_a_core_without_the_pointer_table_still_fingerprints(self):
        fake = self._server(sqlite3.connect(":memory:"))
        self.assertTrue(WriterServer.lane_fingerprint(fake, "dispatch_debate_map"))
