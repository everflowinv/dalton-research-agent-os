"""2026-09-26: the owner's doors no longer time out behind lane ticks.

Live, ``document_recovery_cli authorize-unproved`` reported "writer service is
unavailable" for more than half its calls while the writer went on to open
every one of those doors (BrokenPipeError on the success frame).  Three causes,
three regressions here: the owner queued behind automation on the one store
thread, the governance client waited ten seconds -- less than one lane tick
holds that thread -- and the CLI retried blindly instead of asking the ledger
whether the door it had just called was already open.
"""

from __future__ import annotations

import concurrent.futures
import json
import socket
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dalton_core import document_recovery_cli
from dalton_core.governance_cli import ephemeral_call
from dalton_core.writer_protocol import (
    RemoteError, decode_frame, parse_response, request_frame,
)
from dalton_core.writer_server import (
    CORE_OPERATIONS,
    OWNER_CLIENT_TIMEOUT,
    OWNER_REQUEST_TIMEOUT,
    STORE_REQUEST_TIMEOUT,
    Principal,
    PriorityStoreExecutor,
    WriterServer,
    write_token_config,
)

ADMISSION = "mission-document-research-admission:" + "a9e588b0" * 4


class PriorityStoreExecutorTests(unittest.TestCase):
    def test_urgent_work_goes_ahead_of_queued_automation(self):
        executor = PriorityStoreExecutor()
        self.addCleanup(executor.shutdown)
        release, started = threading.Event(), threading.Event()
        order: list[str] = []

        def lane():
            started.set()
            release.wait(2)
            order.append("lane")

        running = executor.submit(lane)
        self.assertTrue(started.wait(2))
        queued = [executor.submit(order.append, f"auto-{n}") for n in range(3)]
        owner = executor.submit_urgent(order.append, "owner")
        release.set()
        for future in (running, *queued, owner):
            future.result(2)
        # The running tick is never interrupted; the owner is next after it.
        self.assertEqual(order, ["lane", "owner", "auto-0", "auto-1", "auto-2"])

    def test_one_thread_named_like_the_old_store_thread(self):
        executor = PriorityStoreExecutor(thread_name_prefix="dalton-store")
        self.addCleanup(executor.shutdown)
        names = {executor.submit(lambda: threading.current_thread().name).result(2)
                 for _ in range(5)}
        self.assertEqual(names, {"dalton-store_0"})

    def test_a_queued_request_can_still_be_cancelled(self):
        executor = PriorityStoreExecutor()
        self.addCleanup(executor.shutdown)
        release = threading.Event()
        calls: list[str] = []
        busy = executor.submit(release.wait, 2)
        queued = executor.submit(calls.append, "never")
        self.assertTrue(queued.cancel())
        release.set()
        busy.result(2)
        self.assertEqual(executor.submit(lambda: "healthy").result(2), "healthy")
        self.assertEqual(calls, [])

    def test_shutdown_refuses_new_work(self):
        executor = PriorityStoreExecutor()
        executor.shutdown(wait=True)
        with self.assertRaises(RuntimeError):
            executor.submit(lambda: None)


class OwnerRequestRoutingTests(unittest.TestCase):
    def _server(self, executor, *, actor_ref):
        server = object.__new__(WriterServer)
        server._store_executor = executor
        server._stop = threading.Event()
        server._handle = lambda request: {"status": "admitted"}
        server._log_unhandled = mock.Mock()
        server._principal = lambda token: Principal(
            "p", token, frozenset(), actor_ref=actor_ref)
        return server

    def _call(self, server, operation):
        client, peer = socket.socketpair()
        thread = threading.Thread(target=server._serve_connection, args=(peer,))
        thread.start()
        client.sendall(request_frame("r-1", operation, {}, "token"))
        line = client.makefile("rb").readline()
        client.close()
        thread.join(2)
        peer.close()
        return parse_response(decode_frame(line))

    def test_a_person_at_a_door_is_urgent_and_waits_the_owner_deadline(self):
        executor = mock.Mock()
        future = concurrent.futures.Future()
        future.set_result({"status": "admitted"})
        executor.submit_urgent.return_value = future
        server = self._server(executor, actor_ref="human:owner")
        with mock.patch.object(future, "result", wraps=future.result) as result:
            response = self._call(
                server, "authorize_mission_document_unproved_recovery")
        self.assertTrue(response.ok)
        executor.submit_urgent.assert_called_once()
        executor.submit.assert_not_called()
        self.assertEqual(result.call_args.kwargs, {"timeout": OWNER_REQUEST_TIMEOUT})

    def test_automation_keeps_its_place_and_its_deadline(self):
        executor = mock.Mock()
        future = concurrent.futures.Future()
        future.set_result({"status": "ok"})
        executor.submit.return_value = future
        server = self._server(executor, actor_ref="automation:lane")
        with mock.patch.object(future, "result", wraps=future.result) as result:
            self._call(server, "authorize_mission_document_unproved_recovery")
        executor.submit_urgent.assert_not_called()
        self.assertEqual(result.call_args.kwargs, {"timeout": STORE_REQUEST_TIMEOUT})

    def test_a_person_calling_a_lane_operation_is_not_urgent(self):
        server = self._server(mock.Mock(), actor_ref="human:owner")
        self.assertFalse(server._owner_request(SimpleNamespace(
            operation="register_invocation", auth_token="token")))

    def test_the_owner_waits_longer_than_the_writer_does(self):
        self.assertGreater(OWNER_REQUEST_TIMEOUT, STORE_REQUEST_TIMEOUT)
        self.assertGreater(OWNER_CLIENT_TIMEOUT, OWNER_REQUEST_TIMEOUT)

    def test_the_governance_client_uses_the_owner_timeout_for_a_door(self):
        with tempfile.TemporaryDirectory() as directory:
            tokens = Path(directory) / "tokens.json"
            write_token_config(tokens, [
                Principal("core", "core-secret-token", CORE_OPERATIONS,
                          unrestricted=True)])
            observed = {}

            class Client:
                def __init__(self, socket_path, token, timeout):
                    observed["timeout"] = timeout

                def call(self, operation, params):
                    return {"status": "admitted"}

            with mock.patch("dalton_core.governance_cli.WriterClient", Client):
                ephemeral_call(
                    tokens, Path(directory) / "writer.sock", actor_ref="human:owner",
                    operation="authorize_mission_document_unproved_recovery",
                    params={})
        self.assertEqual(observed["timeout"], OWNER_CLIENT_TIMEOUT)


def _ledger(state: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(state / "core.sqlite")
    connection.execute(
        "CREATE TABLE mission_document_research_controlled_recovery_authorizations ("
        "authorization_id TEXT PRIMARY KEY, admission_ref TEXT NOT NULL, "
        "stage_ordinal INTEGER NOT NULL, failed_work_order_ref TEXT NOT NULL, "
        "record_json TEXT NOT NULL, content_hash TEXT NOT NULL, "
        "created_at TEXT NOT NULL)")
    connection.commit()
    return connection


def _open_door(connection: sqlite3.Connection, prefix: str, at: datetime) -> str:
    ref = f"{prefix}:{at.microsecond:032d}"
    connection.execute(
        "INSERT INTO mission_document_research_controlled_recovery_authorizations "
        "VALUES (?,?,?,?,?,?,?)",
        (ref, ADMISSION, 2, "work:failed",
         json.dumps({"id": ref, "max_cost_usd": 1.0}), "h",
         at.isoformat(timespec="microseconds")))
    connection.commit()
    return ref


class ConfirmBeforeRetryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.ledger = _ledger(self.state)
        self.addCleanup(self.ledger.close)

    def _run(self, side_effect):
        calls = []

        def call(_tokens, _socket, *, actor_ref, operation, params):
            calls.append(operation)
            return side_effect(len(calls))

        with mock.patch("dalton_core.governance_cli.ephemeral_call", side_effect=call):
            result = document_recovery_cli._call(
                self.state, operation=document_recovery_cli.UNPROVED_OPERATION,
                actor="human:owner",
                params={"admission_ref": ADMISSION, "actor_ref": "human:owner"},
                sleep=lambda _seconds: None)
        return result, calls

    def test_a_door_the_writer_opened_after_the_client_left_is_not_called_again(self):
        def timed_out(_n):
            # The writer finished the request; the client had already gone.
            _open_door(self.ledger,
                       "mission-document-unproved-send-recovery-authorization",
                       datetime.now(timezone.utc))
            raise RemoteError("transport_error", "writer service is unavailable")

        result, calls = self._run(timed_out)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "admitted")
        self.assertEqual(result["confirmed_after"], "transport_error")

    def test_a_request_the_queue_never_reached_is_called_again(self):
        def queued_then_ok(n):
            if n == 1:
                raise RemoteError("queued_timeout", "writer queue did not reach it")
            return {"status": "admitted", "model_calls": 0}

        result, calls = self._run(queued_then_ok)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result, {"status": "admitted", "model_calls": 0})

    def test_a_door_opened_before_this_call_does_not_count_as_this_one(self):
        # An older owner row (another run) is not proof that this call landed;
        # the call is repeated, and the writer's own replay answers it.
        _open_door(self.ledger,
                   "mission-document-unproved-send-recovery-authorization",
                   datetime.now(timezone.utc) - timedelta(hours=1))
        answers = iter([RemoteError("store_timeout", "late"),
                        {"status": "admitted", "model_calls": 0}])

        def side_effect(_n):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with mock.patch.object(document_recovery_cli, "CONFIRM_WAIT_SECONDS", 0.0):
            result, calls = self._run(side_effect)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["model_calls"], 0)

    def test_the_other_door_s_row_is_not_this_door(self):
        def timed_out(_n):
            _open_door(self.ledger, "mission-document-paid-recovery-authorization",
                       datetime.now(timezone.utc))
            raise RemoteError("queued_timeout", "never ran")

        with self.assertRaises(RemoteError):
            self._run(timed_out)

    def test_a_refusal_is_not_retried(self):
        seen = []

        def refused(n):
            seen.append(n)
            raise RemoteError("forbidden", "operation is not permitted")

        with self.assertRaises(RemoteError):
            self._run(refused)
        self.assertEqual(seen, [1])

    def test_the_ledger_is_opened_read_only(self):
        seen = []
        real = sqlite3.connect

        def spy(target, *args, **kwargs):
            seen.append((target, kwargs.get("uri")))
            return real(target, *args, **kwargs)

        with mock.patch("sqlite3.connect", side_effect=spy):
            document_recovery_cli.door_opened_since(
                self.state, operation=document_recovery_cli.UNPROVED_OPERATION,
                admission_ref=ADMISSION, actor="human:owner",
                since=datetime.now(timezone.utc))
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0][0].endswith("?mode=ro"))
        self.assertTrue(seen[0][1])


class ReentryGrantReplayTests(unittest.TestCase):
    def test_the_same_owner_asking_again_replays_the_pending_grant(self):
        from dalton_core.lane_reentry_claim import write_grant

        with tempfile.TemporaryDirectory() as directory:
            launcher = SimpleNamespace(tickets_dir=Path(directory))
            first = write_grant(launcher, ADMISSION, actor_ref="human:owner",
                                granted_at="2026-09-26T12:50:00+00:00")
            again = write_grant(launcher, ADMISSION, actor_ref="human:owner",
                                granted_at="2026-09-26T12:50:30+00:00")
            self.assertTrue(again["replayed"])
            self.assertEqual(again["granted_at"], first["granted_at"])
            self.assertEqual(len(list(Path(directory).glob("*.json"))), 1)
            with self.assertRaisesRegex(ValueError, "already pending"):
                write_grant(launcher, ADMISSION, actor_ref="human:someone-else",
                            granted_at="2026-09-26T12:51:00+00:00")

    def test_a_timed_out_reentry_is_confirmed_from_the_grant_file(self):
        from dalton_core.lane_reentry_claim import write_grant
        from dalton_core.mission_document_research_launcher import TICKETS_DIRNAME

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / TICKETS_DIRNAME).mkdir()
            since = datetime.now(timezone.utc)
            write_grant(SimpleNamespace(tickets_dir=state / TICKETS_DIRNAME),
                        ADMISSION, actor_ref="human:owner",
                        granted_at=since.isoformat())
            opened = document_recovery_cli.door_opened_since(
                state, operation=document_recovery_cli.OPERATION,
                admission_ref=ADMISSION, actor="human:owner", since=since)
            self.assertEqual(opened["status"], "granted")
            self.assertIsNone(document_recovery_cli.door_opened_since(
                state, operation=document_recovery_cli.OPERATION,
                admission_ref=ADMISSION, actor="human:owner",
                since=since + timedelta(minutes=5)))


if __name__ == "__main__":
    unittest.main()
