"""2026-10-07: owner doors no longer hold the writer's store thread.

Live legacy, 01:01-01:17 UTC: right after a deploy restarted the writer, the
owner's ``authorize-unproved`` built the whole document-research runtime on the
single store thread, sat in SQLite reads for more than fifteen minutes and
every lane op timed out until the writer was killed.  The door is now a
governed job: the store thread appends a ``requested`` receipt and answers;
the heavy half runs on the owner-door thread; the caller polls by ``job_ref``.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dalton_core import document_recovery_cli
from dalton_core import mission_document_research_lane as lane
from dalton_core.owner_door_jobs import OwnerDoorJobs
from dalton_core.writer_client import WriterClient
from dalton_core.writer_server import (
    CORE_OPERATIONS, HUMAN_GOVERNANCE_OPERATIONS, Principal, WriterServer,
)

ADMISSION = "mission-document-research-admission:" + "b1" * 16
UNPROVED = "authorize_mission_document_unproved_recovery"


class OwnerDoorJobsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.core = Path(self.directory.name) / "core.sqlite"
        self.connection = sqlite3.connect(self.core, isolation_level=None)
        self.addCleanup(self.connection.close)
        self.clock = [100.0]
        self.jobs = OwnerDoorJobs(self.core, monotonic=lambda: self.clock[0])
        self.addCleanup(self.jobs.close)

    def _request(self, work):
        return self.jobs.request(
            self.connection, operation=UNPROVED, admission_ref=ADMISSION,
            actor_ref="human:owner", params={"admission_ref": ADMISSION,
                                             "actor_ref": "human:owner"},
            work=work)

    def test_the_request_answers_before_the_work_runs(self):
        release = threading.Event()
        answer = self._request(lambda: (release.wait(5), {"status": "admitted"})[1])
        self.assertEqual(answer["status"], "requested")
        view = self.jobs.view(self.connection, answer["job_ref"])
        self.assertIn(view["state"], {"requested", "running"})
        self.assertEqual(view["status"], "in_progress")
        release.set()
        self.assertTrue(self.jobs.wait_idle())
        view = self.jobs.view(self.connection, answer["job_ref"])
        self.assertEqual(view["state"], "succeeded")
        self.assertEqual(view["status"], "finished")
        self.assertEqual(view["result"], {"status": "admitted"})

    def test_a_second_request_while_one_is_in_flight_is_the_same_job(self):
        release = threading.Event()
        self.addCleanup(release.set)
        first = self._request(lambda: (release.wait(5), {"status": "admitted"})[1])
        second = self._request(lambda: self.fail("must not run twice"))
        self.assertEqual(second["job_ref"], first["job_ref"])
        self.assertEqual(second["status"], "in_progress")

    def test_a_failure_is_a_receipt_not_a_raise(self):
        def boom():
            raise RuntimeError("recovery link authority drifted")

        answer = self._request(boom)
        self.assertTrue(self.jobs.wait_idle())
        view = self.jobs.view(self.connection, answer["job_ref"])
        self.assertEqual(view["state"], "failed")
        self.assertEqual(view["error"]["type"], "RuntimeError")
        self.assertIn("drifted", view["error"]["message"])

    def test_receipts_are_append_only(self):
        answer = self._request(lambda: {"status": "admitted"})
        self.assertTrue(self.jobs.wait_idle())
        with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
            self.connection.execute(
                "UPDATE owner_door_job_events SET state='failed' WHERE job_ref=?",
                (answer["job_ref"],))
        with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
            self.connection.execute("DELETE FROM owner_door_job_events")
        states = [row[0] for row in self.connection.execute(
            "SELECT state FROM owner_door_job_events WHERE job_ref=? ORDER BY sequence",
            (answer["job_ref"],))]
        self.assertEqual(states, ["requested", "running", "succeeded"])

    def test_a_cold_writer_defers_instead_of_queueing(self):
        self.jobs.mark_started()
        deferred = self.jobs.deferred(UNPROVED, ADMISSION)
        self.assertEqual(deferred["status"], "deferred")
        self.assertEqual(deferred["reason"], "writer_cold_start")
        self.assertEqual(deferred["model_calls"], 0)
        self.assertGreater(deferred["retry_after_seconds"], 0)
        self.assertIn("刚刚重启", deferred["explanation"])
        self.clock[0] += self.jobs.cold_start_seconds + 1
        self.assertIsNone(self.jobs.deferred(UNPROVED, ADMISSION))

    def test_a_job_another_writer_took_and_never_finished_is_interrupted(self):
        release = threading.Event()
        self.addCleanup(release.set)
        answer = self._request(lambda: (release.wait(5), {"status": "admitted"})[1])
        later = OwnerDoorJobs(self.core)
        view = later.view(self.connection, answer["job_ref"])
        self.assertEqual(view["status"], "interrupted")


class _Launcher(SimpleNamespace):
    def close(self):
        pass


class WriterOwnerDoorTests(unittest.TestCase):
    """A real writer: a lane op completes while an owner door is running."""

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        root = Path(self.root.name)
        socket_path = str(root / "writer.sock")
        principals = {
            "core": Principal("core", "core-token", CORE_OPERATIONS, unrestricted=True),
            "governance": Principal(
                "governance", "governance-token", HUMAN_GOVERNANCE_OPERATIONS,
                actor_ref="human:owner"),
        }
        self.server = WriterServer(
            root / "core.sqlite", socket_path, principals,
            **{lane.LAUNCHER_KWARG: _Launcher()})
        self.server.start()
        self.addCleanup(self.server.stop)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.core = WriterClient(socket_path, "core-token", timeout=10)
        self.owner = WriterClient(socket_path, "governance-token", timeout=10)
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.door_started = threading.Event()

        def heavy(*_args, **_kwargs):
            # Stands in for the fifteen-minute runtime build.
            self.door_started.set()
            self.release.wait(20)
            return {"status": "admitted", "stage_ordinal": 2, "max_cost_usd": 0.5}

        patches = [
            mock.patch.object(lane, "_run_owner_recovery", heavy),
            mock.patch.object(lane, "_admission_hash", lambda _s, _r: "hash"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_cold_writer_answers_deferred_at_once(self):
        started = time.monotonic()
        answer = self.owner.call(UNPROVED, {"admission_ref": ADMISSION})
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(answer["status"], "deferred")
        self.assertFalse(self.door_started.is_set())

    def test_a_lane_op_completes_while_an_owner_door_is_in_progress(self):
        self.server.owner_door_jobs.cold_start_seconds = 0
        answer = self.owner.call(UNPROVED, {"admission_ref": ADMISSION})
        self.assertEqual(answer["status"], "requested")
        self.assertTrue(self.door_started.wait(5), "the door never started")
        # The door is mid-flight on its own thread.  A lane op and a write
        # both go through the store thread and must not wait for it.
        started = time.monotonic()
        lane_answer = self.core.call("dispatch_industry_framework", {})
        self.core.call("active_policy", {})
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsInstance(lane_answer, dict)
        polled = self.owner.call(
            UNPROVED, {"admission_ref": ADMISSION, "job_ref": answer["job_ref"]})
        self.assertEqual(polled["status"], "in_progress")
        self.release.set()
        self.assertTrue(self.server.owner_door_jobs.wait_idle())
        polled = self.owner.call(
            UNPROVED, {"admission_ref": ADMISSION, "job_ref": answer["job_ref"]})
        self.assertEqual(polled["status"], "finished")
        self.assertEqual(polled["result"]["status"], "admitted")

    def test_the_cli_polls_the_job_to_its_result(self):
        self.server.owner_door_jobs.cold_start_seconds = 0
        self.release.set()
        calls = []

        def ephemeral(state, *, operation, actor, params):
            calls.append(dict(params))
            return self.owner.call(operation, params)

        with mock.patch.object(document_recovery_cli, "_ephemeral_call", ephemeral):
            result = document_recovery_cli._call(
                Path(self.root.name), operation=UNPROVED, actor="human:owner",
                params={"admission_ref": ADMISSION, "actor_ref": "human:owner"},
                sleep=lambda _s: time.sleep(0.05))
        self.assertEqual(result["status"], "admitted")
        self.assertTrue(result["job_ref"].startswith("owner-door-job:"))
        self.assertNotIn("job_ref", calls[0])
        self.assertTrue(all("job_ref" in call for call in calls[1:]))


class DoorReadPathTests(unittest.TestCase):
    """The reads that made the door take minutes on a cold legacy Core."""

    def test_the_core_carries_the_correction_and_document_stage_indexes(self):
        from dalton_core.store import DaltonStore

        with tempfile.TemporaryDirectory() as directory:
            store = DaltonStore(str(Path(directory) / "core.sqlite"))
            from dalton_core.connector import ConnectorStore
            from dalton_core.migrations import CORE_PROJECTION_INDEXES, ensure_indexes

            ConnectorStore(store)
            # The document executor's Scheduler lives on the Core connection.
            from dalton_core.scheduler import Scheduler

            Scheduler(connection=store.connection)
            ensure_indexes(store.connection, CORE_PROJECTION_INDEXES)
            names = {row[0] for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            for name in ("connector_usage_entries_correction_of",
                         "connector_quota_settlements_correction_of",
                         "connector_cost_entries_correction_of",
                         "scheduler_work_orders_document_stage"):
                self.assertIn(name, names)
            plan = " ".join(row[3] for row in store.connection.execute(
                "EXPLAIN QUERY PLAN SELECT usage_entry_id FROM connector_usage_entries "
                "WHERE physical_attempt_ref=? AND NOT EXISTS (SELECT 1 FROM "
                "connector_usage_entries n WHERE n.correction_of_ref="
                "connector_usage_entries.usage_entry_id)", ("x",)))
            self.assertNotIn("SCAN n", plan)
            store.close()

    def test_the_read_only_fetch_reader_scans_the_tickets_once(self):
        import json

        from dalton_core.public_web_fetch_launcher import (
            ReadOnlyPublicWebFetchManifestReader,
        )

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            for index in range(20):
                ticket = state / "fetches" / f"{index:024x}"
                ticket.mkdir(parents=True)
                (ticket / "ticket.json").write_text(json.dumps({
                    "id": f"public-web-fetch:{index:024x}", "status": "succeeded",
                    "document_ref": f"doc:{index % 5}",
                    "started_at": f"2026-10-0{1 + index % 5}T00:00:{index:02d}+00:00"}))
            reader = ReadOnlyPublicWebFetchManifestReader(state_dir=state)
            reads = []
            original = Path.read_text

            def counting(path, *args, **kwargs):
                reads.append(path)
                return original(path, *args, **kwargs)

            with mock.patch.object(Path, "read_text", counting), \
                    mock.patch.object(ReadOnlyPublicWebFetchManifestReader,
                                      "read_completed_manifest",
                                      lambda self, ref, doc: {"ticket": ref}):
                found = [reader.locate_completed_manifest_binding(f"doc:{n}")
                         for n in range(5)]
            self.assertEqual(len(reads), 20)
            # The newest succeeded ticket per document, as before.
            self.assertEqual(found[0]["ticket_ref"], f"public-web-fetch:{15:024x}")


if __name__ == "__main__":
    unittest.main()
