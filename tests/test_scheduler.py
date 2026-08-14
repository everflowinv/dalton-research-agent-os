import hashlib
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.scheduler import (
    LeaseExpired,
    LeaseRejected,
    RetryExhausted,
    Scheduler,
    SchedulerConflict,
    SchedulerValidationError,
)
from dalton_core.contracts import ResultEnvelope
from dalton_core.store import content_hash


ROOT = Path(__file__).resolve().parents[1]


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def advance(self, seconds):
        with self.lock:
            self.value += timedelta(seconds=seconds)


def work_order(identifier="work-1"):
    return {
        "schema_version": "0.1",
        "id": identifier,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "question": "normalize records",
        "requested_capabilities": ["formatter"],
        "runtime_profile_ref": "runtime:test",
        "budget": {"max_seconds": 60},
        "idempotency_key": f"enqueue:{identifier}",
        "declared_side_effects": [],
        "status": "ready",
        "input_refs": ["fixture:1"],
        "metadata": {},
    }


def result(identifier, work_ref="work-1", status="succeeded"):
    return {
        "schema_version": "0.1",
        "id": identifier,
        "created_at": "2026-01-01T00:00:01+00:00",
        "work_order_ref": work_ref,
        "invocation_ref": f"invocation:{identifier}",
        "status": status,
        "outputs": {"ok": status == "succeeded"},
        "artifact_refs": [f"artifact:{identifier}"],
        "actual_side_effects": [],
        "usage_refs": [f"usage:{identifier}"],
        "error": None,
        "metadata": {},
    }


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock()
        self.scheduler = Scheduler(
            ":memory:",
            clock=self.clock,
            max_attempts=3,
            default_lease_seconds=10,
            max_lease_seconds=20,
            max_renew_seconds=5,
            max_total_lease_seconds=25,
        )
        self.addCleanup(self.scheduler.close)

    def enqueue_claim(self, work_id="work-1", owner="worker:a"):
        self.scheduler.enqueue(work_order(work_id))
        lease = self.scheduler.claim(owner, work_order_id=work_id)
        self.assertIsNotNone(lease)
        return lease

    def test_enqueue_claim_renew_and_success_are_append_only(self):
        self.assertEqual(self.scheduler.enqueue(work_order())["status"], "fresh")
        self.assertEqual(self.scheduler.enqueue(work_order())["status"], "duplicate")
        changed = work_order()
        changed["question"] = "different"
        self.assertEqual(self.scheduler.enqueue(changed)["status"], "conflict")
        other_id_same_key = work_order("work-other")
        other_id_same_key["idempotency_key"] = "enqueue:work-1"
        self.assertEqual(self.scheduler.enqueue(other_id_same_key)["status"], "conflict")

        claimed = self.scheduler.claim("worker:a")
        self.assertEqual(claimed["status"], "leased")
        self.assertEqual(
            hashlib.sha256(claimed["lease_token"].encode()).hexdigest(),
            claimed["lease"]["lease_token_hash"],
        )
        self.assertNotIn(
            claimed["lease_token"],
            self.scheduler.conn.execute(
                "SELECT lease_token_hash FROM scheduler_leases"
            ).fetchone()[0],
        )
        renewed = self.scheduler.renew(
            "work-1", 1, "worker:a", claimed["lease_token"], extend_seconds=5
        )
        self.assertEqual(renewed["status"], "renewed")
        self.assertEqual(len(self.scheduler.lease_history(claimed["lease"]["lease_id"])), 2)

        completed = self.scheduler.complete(
            "work-1",
            1,
            "worker:a",
            claimed["lease_token"],
            result("result-1"),
            idempotency_key="complete:1",
        )
        self.assertEqual(completed["status"], "fresh")
        self.assertEqual(completed["work_state"], "succeeded")
        self.assertEqual(self.scheduler.status("work-1")["state"], "succeeded")
        self.assertEqual(
            [row["state"] for row in self.scheduler.attempt_history("work-1")],
            ["ready", "leased", "succeeded"],
        )
        self.assertEqual(self.scheduler.formal_result("work-1")["result_envelope_id"], "result-1")
        self.assertIsNone(self.scheduler.claim("worker:b"))

    def test_validate_lease_for_use_requires_exact_current_revision(self):
        claimed = self.enqueue_claim()
        validated = self.scheduler.validate_lease_for_use(
            "work-1", 1, "worker:a", claimed["lease_token"],
            lease_revision_ref=claimed["lease"]["id"],
            lease_hash=claimed["lease"]["content_hash"],
            work_order_hash=claimed["work_order_hash"],
        )
        self.assertEqual(validated["status"], "usable")
        self.assertNotIn("lease_token", validated)
        with self.assertRaises(LeaseRejected):
            self.scheduler.validate_lease_for_use(
                "work-1", 1, "worker:a", claimed["lease_token"],
                lease_revision_ref=claimed["lease"]["id"],
                lease_hash="0" * 64,
                work_order_hash=claimed["work_order_hash"],
            )
        renewed = self.scheduler.renew(
            "work-1", 1, "worker:a", claimed["lease_token"], extend_seconds=5
        )
        with self.assertRaises(LeaseRejected):
            self.scheduler.validate_lease_for_use(
                "work-1", 1, "worker:a", claimed["lease_token"],
                lease_revision_ref=claimed["lease"]["id"],
                lease_hash=claimed["lease"]["content_hash"],
                work_order_hash=claimed["work_order_hash"],
            )
        validated = self.scheduler.validate_lease_for_use(
            "work-1", 1, "worker:a", claimed["lease_token"],
            lease_revision_ref=renewed["id"], lease_hash=renewed["content_hash"],
            work_order_hash=claimed["work_order_hash"],
        )
        self.assertEqual(validated["lease"]["id"], renewed["id"])
        self.clock.advance(16)
        with self.assertRaises(LeaseExpired):
            self.scheduler.validate_lease_for_use(
                "work-1", 1, "worker:a", claimed["lease_token"],
                lease_revision_ref=renewed["id"], lease_hash=renewed["content_hash"],
                work_order_hash=claimed["work_order_hash"],
            )
        self.assertEqual(self.scheduler.status("work-1")["state"], "ready")
        self.assertEqual(
            [row["state"] for row in self.scheduler.attempt_history("work-1")],
            ["ready", "leased", "expired", "ready"],
        )
        self.assertIsNone(self.scheduler.formal_result("work-1"))

    def test_owner_token_and_renewal_bounds_are_enforced(self):
        claimed = self.enqueue_claim()
        with self.assertRaises(LeaseRejected):
            self.scheduler.renew("work-1", 1, "worker:b", claimed["lease_token"])
        with self.assertRaises(LeaseRejected):
            self.scheduler.renew("work-1", 1, "worker:a", "wrong-token")
        with self.assertRaises(LeaseRejected):
            self.scheduler.renew(
                "work-1", 1, "worker:a", claimed["lease_token"], extend_seconds=6
            )
        self.scheduler.renew("work-1", 1, "worker:a", claimed["lease_token"])
        self.scheduler.renew("work-1", 1, "worker:a", claimed["lease_token"])
        self.scheduler.renew("work-1", 1, "worker:a", claimed["lease_token"])
        with self.assertRaises(LeaseRejected):
            self.scheduler.renew("work-1", 1, "worker:a", claimed["lease_token"])

    def test_expiry_reclaims_with_new_attempt_and_rejects_late_completion(self):
        first = self.enqueue_claim()
        self.clock.advance(11)
        second = self.scheduler.claim("worker:b", work_order_id="work-1")
        self.assertEqual(second["attempt"]["attempt_number"], 2)
        self.assertNotEqual(first["lease"]["lease_id"], second["lease"]["lease_id"])
        with self.assertRaises(LeaseRejected):
            self.scheduler.complete(
                "work-1",
                1,
                "worker:a",
                first["lease_token"],
                result("late-result"),
                idempotency_key="complete:late",
            )
        success = self.scheduler.complete(
            "work-1",
            2,
            "worker:b",
            second["lease_token"],
            result("result-2"),
            idempotency_key="complete:2",
        )
        self.assertEqual(success["work_state"], "succeeded")
        self.assertEqual(
            self.scheduler.conn.execute(
                "SELECT COUNT(*) FROM scheduler_formal_results WHERE work_order_id='work-1'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            [row["state"] for row in self.scheduler.attempt_history("work-1")],
            ["ready", "leased", "expired", "ready", "leased", "succeeded"],
        )

    def test_expiry_during_complete_commits_expired_transition_before_error(self):
        claimed = self.enqueue_claim()
        self.clock.advance(11)
        with self.assertRaises(LeaseExpired):
            self.scheduler.complete(
                "work-1",
                1,
                "worker:a",
                claimed["lease_token"],
                result("late"),
                idempotency_key="late-key",
            )
        self.assertEqual(self.scheduler.status("work-1")["state"], "ready")
        self.assertEqual(self.scheduler.status("work-1")["attempt_number"], 2)
        self.assertEqual(
            self.scheduler.conn.execute(
                "SELECT COUNT(*) FROM scheduler_completion_idempotency"
            ).fetchone()[0],
            0,
        )

    def test_completion_idempotency_fresh_duplicate_conflict_and_hash_assertion(self):
        claimed = self.enqueue_claim()
        envelope = result("result-1")
        expected_hash = content_hash(ResultEnvelope.from_dict(envelope).to_dict())
        fresh = self.scheduler.complete(
            "work-1",
            1,
            "worker:a",
            claimed["lease_token"],
            envelope,
            idempotency_key="completion-key",
            result_envelope_hash=expected_hash,
        )
        duplicate = self.scheduler.complete(
            "work-1",
            1,
            "worker:a",
            claimed["lease_token"],
            envelope,
            idempotency_key="completion-key",
            result_envelope_hash=expected_hash,
        )
        self.assertEqual(fresh["status"], "fresh")
        self.assertEqual(duplicate["status"], "duplicate")
        conflict = self.scheduler.complete(
            "work-1",
            1,
            "worker:a",
            claimed["lease_token"],
            result("different"),
            idempotency_key="completion-key",
        )
        self.assertEqual(conflict["status"], "conflict")
        with self.assertRaises(Exception):
            self.scheduler.complete(
                "work-1",
                1,
                "worker:a",
                claimed["lease_token"],
                envelope,
                idempotency_key="other-key",
                result_envelope_hash="0" * 64,
            )
        self.assertEqual(
            self.scheduler.conn.execute("SELECT COUNT(*) FROM scheduler_formal_results").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.scheduler.conn.execute("SELECT COUNT(*) FROM scheduler_result_envelopes").fetchone()[0],
            1,
        )

    def test_retryable_results_obey_trusted_retry_exhaustion(self):
        limited = Scheduler(
            ":memory:",
            clock=self.clock,
            max_attempts=2,
            default_lease_seconds=10,
            max_lease_seconds=10,
            max_renew_seconds=5,
            max_total_lease_seconds=20,
            policy_version_id="scheduler-policy-two-attempts",
        )
        self.addCleanup(limited.close)
        limited.enqueue(work_order())
        first = limited.claim("worker:a")
        one = limited.complete(
            "work-1",
            1,
            "worker:a",
            first["lease_token"],
            result("retry-1", status="retryable"),
            idempotency_key="retry:1",
        )
        self.assertEqual(one["work_state"], "ready")
        self.assertIsNone(limited.formal_result("work-1"))
        second = limited.claim("worker:b")
        two = limited.complete(
            "work-1",
            2,
            "worker:b",
            second["lease_token"],
            result("retry-2", status="retryable"),
            idempotency_key="retry:2",
        )
        self.assertEqual(two["work_state"], "failed")
        self.assertEqual(limited.status("work-1")["state"], "failed")
        self.assertIsNone(limited.formal_result("work-1"))
        self.assertEqual(
            limited.conn.execute("SELECT COUNT(*) FROM scheduler_result_envelopes").fetchone()[0],
            2,
        )
        self.assertIsNone(limited.claim("worker:c"))
        self.assertEqual(
            [row["state"] for row in limited.attempt_history("work-1")],
            ["ready", "leased", "retryable", "ready", "leased", "retryable", "failed"],
        )
        policy = limited.conn.execute(
            "SELECT policy_json FROM scheduler_policy_versions"
        ).fetchone()[0]
        self.assertIn('"max_attempts":2', policy)

    def test_retryable_result_can_be_rescheduled_without_worker_busy_wait(self):
        lease = self.enqueue_claim()
        retry_at = self.clock.value + timedelta(minutes=5)
        completed = self.scheduler.complete(
            "work-1",
            1,
            "worker:a",
            lease["lease_token"],
            result("rate-limited", status="retryable"),
            idempotency_key="retry:rate-limit",
            retry_at=retry_at,
        )
        self.assertEqual(completed["work_state"], "ready")
        self.assertEqual(
            self.scheduler.status("work-1")["not_before"],
            retry_at.isoformat(timespec="microseconds"),
        )
        latest = self.scheduler.attempt_history("work-1")[-1]
        self.assertEqual(latest["wire_version"], "0.2")
        self.assertIsNone(self.scheduler.claim("worker:b"))
        self.clock.advance(299)
        self.assertIsNone(self.scheduler.claim("worker:b"))
        self.clock.advance(1)
        claimed = self.scheduler.claim("worker:b")
        self.assertEqual(claimed["lease"]["attempt_number"], 2)

    def test_retry_at_is_bound_to_retryable_completion_idempotency(self):
        lease = self.enqueue_claim()
        retry_at = self.clock.value + timedelta(seconds=60)
        envelope = result("retry", status="retryable")
        first = self.scheduler.complete(
            "work-1", 1, "worker:a", lease["lease_token"], envelope,
            idempotency_key="retry:bound", retry_at=retry_at,
        )
        self.assertEqual(first["status"], "fresh")
        duplicate = self.scheduler.complete(
            "work-1", 1, "worker:a", lease["lease_token"], envelope,
            idempotency_key="retry:bound", retry_at=retry_at,
        )
        self.assertEqual(duplicate["status"], "duplicate")
        conflict = self.scheduler.complete(
            "work-1", 1, "worker:a", lease["lease_token"], envelope,
            idempotency_key="retry:bound", retry_at=retry_at + timedelta(seconds=1),
        )
        self.assertEqual(conflict["status"], "conflict")

        other = self.enqueue_claim("work-2")
        with self.assertRaises(SchedulerValidationError):
            self.scheduler.complete(
                "work-2", 1, "worker:a", other["lease_token"],
                result("done", work_ref="work-2"), idempotency_key="done",
                retry_at=retry_at,
            )

    def test_existing_scheduler_table_adds_not_before_before_use(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy-scheduler.db"
            legacy_schema = (ROOT / "src/dalton_core/scheduler_schema.sql").read_text(
                encoding="utf-8"
            ).replace("    not_before TEXT,\n", "").replace(
                "    wire_version TEXT,\n", ""
            )
            connection = sqlite3.connect(path)
            connection.executescript(legacy_schema)
            connection.close()
            migrated = Scheduler(path, clock=self.clock)
            self.addCleanup(migrated.close)
            columns = {
                row[1]
                for row in migrated.conn.execute(
                    "PRAGMA table_info(scheduler_attempt_events)"
                ).fetchall()
            }
            self.assertIn("not_before", columns)
            self.assertIn("wire_version", columns)
            migrated.enqueue(work_order())
            self.assertIsNotNone(migrated.claim("worker:legacy"))

    def test_expired_attempt_exhaustion_is_terminal_without_fake_result(self):
        limited = Scheduler(
            ":memory:",
            clock=self.clock,
            max_attempts=1,
            default_lease_seconds=5,
            max_lease_seconds=5,
            max_renew_seconds=2,
            max_total_lease_seconds=10,
            policy_version_id="scheduler-policy-one-attempt",
        )
        self.addCleanup(limited.close)
        limited.enqueue(work_order())
        limited.claim("worker:a")
        self.clock.advance(6)
        swept = limited.sweep_expired()
        self.assertEqual(swept[0]["next"]["state"], "failed")
        self.assertEqual(limited.status("work-1")["state"], "failed")
        self.assertIsNone(limited.formal_result("work-1"))

    def test_authority_rows_reject_direct_insert_update_and_delete(self):
        self.scheduler.enqueue(work_order())
        with self.assertRaises(sqlite3.DatabaseError):
            self.scheduler.conn.execute(
                "UPDATE scheduler_work_orders SET work_order_hash='evil'"
            )
        with self.assertRaises(sqlite3.DatabaseError):
            self.scheduler.conn.execute("DELETE FROM scheduler_attempt_events")
        with self.assertRaises(sqlite3.DatabaseError):
            self.scheduler.conn.execute(
                "INSERT INTO scheduler_attempt_events "
                "(event_id, work_order_id, attempt_number, state, content_hash, created_at) "
                "VALUES ('evil', 'work-1', 1, 'failed', 'evil', 'now')"
            )

    def test_two_connections_can_only_claim_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "scheduler.db"
            first = Scheduler(
                path,
                clock=self.clock,
                max_attempts=2,
                policy_version_id="scheduler-policy-concurrency",
            )
            self.addCleanup(first.close)
            first.enqueue(work_order())
            barrier = threading.Barrier(2)
            results = []
            errors = []

            def contender(owner):
                scheduler = Scheduler(
                    path,
                    clock=self.clock,
                    max_attempts=2,
                    policy_version_id="scheduler-policy-concurrency",
                )
                try:
                    barrier.wait()
                    results.append(scheduler.claim(owner, work_order_id="work-1"))
                except Exception as exc:  # pragma: no cover - assertion reports it
                    errors.append(exc)
                finally:
                    scheduler.close()

            threads = [
                threading.Thread(target=contender, args=("worker:a",)),
                threading.Thread(target=contender, args=("worker:b",)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(errors)
            self.assertEqual(sum(item is not None for item in results), 1)
            self.assertEqual(sum(item is None for item in results), 1)
            self.assertEqual(
                first.conn.execute(
                    "SELECT COUNT(*) FROM scheduler_attempt_events WHERE state='leased'"
                ).fetchone()[0],
                1,
            )

    def test_policy_version_cannot_be_reused_with_different_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "scheduler.db"
            first = Scheduler(path, clock=self.clock, max_attempts=2, policy_version_id="policy:v1")
            first.close()
            with self.assertRaises(SchedulerConflict):
                Scheduler(path, clock=self.clock, max_attempts=3, policy_version_id="policy:v1")

    def test_claim_and_renew_use_work_orders_frozen_policy_after_reopen(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "scheduler.db"
            first = Scheduler(
                path,
                clock=self.clock,
                max_attempts=2,
                default_lease_seconds=5,
                max_lease_seconds=5,
                max_renew_seconds=2,
                max_total_lease_seconds=10,
                policy_version_id="policy:v1",
            )
            first.enqueue(work_order())
            first.close()
            second = Scheduler(
                path,
                clock=self.clock,
                max_attempts=9,
                default_lease_seconds=50,
                max_lease_seconds=50,
                max_renew_seconds=20,
                max_total_lease_seconds=100,
                policy_version_id="policy:v2",
            )
            self.addCleanup(second.close)
            with self.assertRaises(LeaseRejected):
                second.claim("worker:a", work_order_id="work-1", lease_seconds=50)
            lease = second.claim("worker:a", work_order_id="work-1")
            issued = datetime.fromisoformat(lease["lease"]["issued_at"])
            expires = datetime.fromisoformat(lease["lease"]["expires_at"])
            self.assertEqual((expires - issued).total_seconds(), 5)
            self.assertEqual(lease["policy_version_id"], "policy:v1")
            with self.assertRaises(LeaseRejected):
                second.renew(
                    "work-1", 1, "worker:a", lease["lease_token"], extend_seconds=10
                )


if __name__ == "__main__":
    unittest.main()
