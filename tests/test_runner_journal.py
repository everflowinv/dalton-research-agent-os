from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from dalton_core.runner_journal import RunnerJournal, RunnerJournalConflict
from dalton_core.store import DaltonStore, content_hash


WHEN = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def request_wire() -> dict:
    base = {
        "schema_version": "0.1",
        "id": "runner-request:journal:1",
        "created_at": WHEN.isoformat(timespec="microseconds"),
        "connector_invocation_ref": "connector-invocation:journal:1",
        "connector_invocation_hash": "1" * 64,
        "execution_ref": "execution:journal:1",
        "execution_hash": "2" * 64,
        "work_order_ref": "work:journal:1",
        "work_order_hash": "3" * 64,
        "scheduler_attempt_number": 1,
        "scheduler_lease_revision_ref": "lease-revision:journal:1",
        "scheduler_lease_hash": "4" * 64,
        "connector_profile_ref": "connector-profile:journal:1",
        "connector_profile_hash": "5" * 64,
        "call_spec_ref": "connector-call:journal:1",
        "call_spec_hash": "6" * 64,
        "capability_lease_ref": "capability-lease:journal:1",
        "capability_lease_hash": "7" * 64,
        "principal_ref": "principal:journal",
        "runner_runtime_ref": "runtime:connector-runner:0.1",
        "runner_actor_ref": "runner:connector",
        "runner_environment_hash": "8" * 64,
        "idempotency_key": "runner-request:journal:1",
    }
    return {**base, "content_hash": content_hash(base)}


class RunnerJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.journal = RunnerJournal(self.store, clock=lambda: WHEN)

    def test_closed_state_machine_is_append_only_and_idempotent(self) -> None:
        admitted = self.journal.begin_request(request_wire())
        self.assertEqual(admitted["state"], "admitted")
        duplicate = self.journal.begin_request(request_wire())
        self.assertEqual(duplicate["write_status"], "duplicate")
        reservation = "connector-reservation:journal:1"
        self.journal.append(
            request_wire()["id"], "reserved", {"reservation_ref": reservation}
        )
        self.journal.append(
            request_wire()["id"],
            "transport_started",
            {"reservation_ref": reservation, "started_at": WHEN.isoformat()},
        )
        self.journal.append(
            request_wire()["id"],
            "observed",
            {"reservation_ref": reservation, "attempt_outcome": "succeeded"},
        )
        terminal = self.journal.append(
            request_wire()["id"],
            "responded",
            {"reservation_ref": reservation, "response": {"id": "response:1"}},
        )
        self.assertEqual(terminal["request_ordinal"], 5)
        self.assertEqual(self.journal.incomplete_requests(), [])
        self.assertEqual(self.journal.reservation_refs(), {reservation})
        with self.assertRaises(RunnerJournalConflict):
            self.journal.append(
                request_wire()["id"], "observed", {"reservation_ref": reservation}
            )
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute(
                "DELETE FROM runner_attempt_journal_events WHERE runner_request_ref=?",
                (request_wire()["id"],),
            )

    def test_transport_started_is_unique_per_reservation(self) -> None:
        first = request_wire()
        second_base = {**first, "id": "runner-request:journal:2"}
        second_base.pop("content_hash")
        second = {**second_base, "content_hash": content_hash(second_base)}
        reservation = "connector-reservation:shared"
        for request in (first, second):
            self.journal.begin_request(request)
            self.journal.append(
                request["id"], "reserved", {"reservation_ref": reservation}
            )
        self.journal.append(
            first["id"], "transport_started", {"reservation_ref": reservation}
        )
        with self.assertRaises(RunnerJournalConflict):
            self.journal.append(
                second["id"], "transport_started", {"reservation_ref": reservation}
            )

    def test_request_identity_conflict_is_rejected(self) -> None:
        self.journal.begin_request(request_wire())
        changed_base = {**request_wire(), "principal_ref": "principal:other"}
        changed_base.pop("content_hash")
        changed = {**changed_base, "content_hash": content_hash(changed_base)}
        with self.assertRaises(RunnerJournalConflict):
            self.journal.begin_request(changed)


if __name__ == "__main__":
    unittest.main()
