from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import shutil
import threading
from unittest.mock import patch
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.contracts import ResultEnvelope, WorkOrder
from dalton_core.controlled_failure_redrive import (
    ControlledFailureRedriveError,
    _connect_existing_writable,
    apply,
    approved_request,
    prepare,
)
from dalton_core.scheduler import Scheduler
from dalton_core.store import content_hash
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from integrations.openclaw_host_patches.patch_controlled_completion_transport import PATCHED, PATCHED_BIND


NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


class ControlledFailureRedriveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.openclaw_root = root / "openclaw"
        fixture = (Path(__file__).parent / "fixtures" /
                   "openclaw-controlled-repair-2026.9.3")
        shutil.copytree(fixture, self.openclaw_root)
        self.scheduler_db = root / "scheduler.sqlite"
        self.budget_db = root / "budget.sqlite"
        self.scheduler = Scheduler(self.scheduler_db, clock=lambda: NOW)
        self.addCleanup(self.scheduler.close)
        self.budget = ThesisImpactBudgetStore(self.budget_db, clock=lambda: NOW)
        self.addCleanup(self.budget.close)
        self.budget.register_policy(
            policy_version_id="budget:test:1", day_cap_micros=1000
        )
        self.mission = {
            "id": "coverage-mission-version:test:1",
            "content_hash": "a" * 64,
        }
        self.work = WorkOrder(
            schema_version="0.1", id="work:cockpit-test-old",
            created_at=NOW.isoformat(), updated_at=NOW.isoformat(),
            question="test", requested_capabilities=("research",),
            runtime_profile_ref="runtime:test", budget={"max_attempts": 1},
            idempotency_key="test", declared_side_effects=(), status="ready",
            input_refs=(), metadata={
                "mission_version_ref": self.mission["id"],
                "mission_version_hash": self.mission["content_hash"],
            },
        )
        self.scheduler.enqueue(self.work)
        lease = self.scheduler.claim("worker:test", work_order_id=self.work.id)
        result = ResultEnvelope(
            schema_version="0.1", id="result:test-host-failed",
            created_at=NOW.isoformat(), work_order_ref=self.work.id,
            invocation_ref="invocation:test", status="failed", outputs={},
            actual_side_effects=(), usage_refs=(), artifact_refs=(),
            error={"code": "HOST_COMPLETION_FAILED"}, metadata={},
        )
        self.scheduler.complete(
            self.work.id, 1, "worker:test", lease["lease_token"], result,
            idempotency_key="complete:test",
        )
        self.admission = self.budget.admit(
            policy_version_id="budget:test:1", day="2026-09-10",
            work_order_ref=self.work.id, attempt_number=1, phase="assessment",
            route_decision_ref="route:test", reserved_micros=100,
            mission_binding={
                "mission_ref": "coverage-mission:test",
                "mission_version_ref": self.mission["id"],
                "mission_version_hash": self.mission["content_hash"],
                "max_daily_paid_calls": 10,
                "max_daily_cost_micros": 1000,
            },
        )
        self.settlement = self.budget.settle(
            self.admission["admission_id"], actual_micros=1
        )

    def test_prepare_is_read_only_and_apply_is_single_hash_bound_record(self):
        before_scheduler = self.scheduler_db.read_bytes()
        before_budget = self.budget_db.read_bytes()
        before_sidecars = sorted(path.name for path in self.budget_db.parent.iterdir())
        before_work = self.scheduler.work_order_authority(self.work.id)
        candidate = prepare(
            scheduler_db=self.scheduler_db, budget_db=self.budget_db,
            old_work_order_ref=self.work.id, openclaw_root=self.openclaw_root,
        )
        self.assertEqual(self.scheduler_db.read_bytes(), before_scheduler)
        self.assertEqual(self.budget_db.read_bytes(), before_budget)
        self.assertEqual(
            sorted(path.name for path in self.budget_db.parent.iterdir()),
            before_sidecars,
        )
        saved = apply(
            scheduler_db=self.scheduler_db, budget_db=self.budget_db,
            candidate=candidate, expected_candidate_hash=candidate["candidate_hash"],
        )
        self.assertEqual(saved["status"], "fresh")
        self.assertEqual(apply(
            scheduler_db=self.scheduler_db, budget_db=self.budget_db,
            candidate=candidate, expected_candidate_hash=candidate["candidate_hash"],
        )["status"], "duplicate")
        self.assertEqual(self.scheduler.work_order_authority(self.work.id), before_work)
        formal = self.scheduler.formal_result(self.work.id)
        self.assertEqual(
            approved_request(self.scheduler_db, self.budget_db, old_work_order_ref=self.work.id,
                             formal=formal, mission=self.mission),
            ":operator-recovery:" + saved["content_hash"][:16],
        )

    def test_apply_provisions_and_holds_missing_wal_sidecars(self):
        candidate = prepare(
            scheduler_db=self.scheduler_db, budget_db=self.budget_db,
            old_work_order_ref=self.work.id, openclaw_root=self.openclaw_root,
        )
        self.scheduler.close()
        self.budget.close()
        sidecars = [
            Path(str(database) + suffix)
            for database in (self.scheduler_db, self.budget_db)
            for suffix in ("-wal", "-shm")
        ]
        self.assertTrue(all(not path.exists() for path in sidecars))
        with self.assertRaisesRegex(sqlite3.OperationalError, "requires existing WAL/SHM"):
            prepare(
                scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                old_work_order_ref=self.work.id, openclaw_root=self.openclaw_root,
            )

        saved = apply(
            scheduler_db=self.scheduler_db, budget_db=self.budget_db,
            candidate=candidate, expected_candidate_hash=candidate["candidate_hash"],
        )
        self.assertEqual(saved["status"], "fresh")
        self.assertEqual(
            apply(
                scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                candidate=candidate,
                expected_candidate_hash=candidate["candidate_hash"],
            )["status"],
            "duplicate",
        )

    def test_apply_rejects_before_writable_open_and_never_creates_database(self):
        candidate = prepare(
            scheduler_db=self.scheduler_db, budget_db=self.budget_db,
            old_work_order_ref=self.work.id, openclaw_root=self.openclaw_root,
        )
        self.scheduler.close()
        self.budget.close()
        sidecars = [
            Path(str(database) + suffix)
            for database in (self.scheduler_db, self.budget_db)
            for suffix in ("-wal", "-shm")
        ]
        self.assertTrue(all(not path.exists() for path in sidecars))
        with self.assertRaisesRegex(
            ControlledFailureRedriveError, "reviewed candidate hash does not match"
        ):
            apply(
                scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                candidate=candidate, expected_candidate_hash="0" * 64,
            )
        self.assertTrue(all(not path.exists() for path in sidecars))

        missing = self.scheduler_db.parent / "missing-budget.sqlite"
        with self.assertRaisesRegex(
            ControlledFailureRedriveError, "existing writable authority database"
        ):
            apply(
                scheduler_db=self.scheduler_db, budget_db=missing,
                candidate=candidate,
                expected_candidate_hash=candidate["candidate_hash"],
            )
        self.assertFalse(missing.exists())

    def test_failed_writable_probe_closes_opened_connection(self):
        class FailedProbe:
            closed = False

            def execute(self, statement):
                if statement.startswith("SELECT name"):
                    raise sqlite3.OperationalError("probe failed")
                return self

            def close(self):
                self.closed = True

        opened = FailedProbe()
        with patch(
            "dalton_core.controlled_failure_redrive.sqlite3.connect",
            return_value=opened,
        ):
            with self.assertRaisesRegex(
                ControlledFailureRedriveError,
                "existing writable authority database",
            ):
                _connect_existing_writable(self.scheduler_db)
        self.assertTrue(opened.closed)

    def test_prepare_closes_strict_read_only_connections(self):
        from dalton_core.readonly_sqlite import connect_read_only as real_connect

        opened = []

        def tracked(path):
            connection = real_connect(path)
            opened.append(connection)
            return connection

        with patch("dalton_core.controlled_failure_redrive.connect_read_only",
                   side_effect=tracked):
            prepare(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                    old_work_order_ref=self.work.id, openclaw_root=self.openclaw_root)
        self.assertEqual(len(opened), 2)
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_tampered_review_or_changed_mission_is_refused(self):
        candidate = prepare(
            scheduler_db=self.scheduler_db, budget_db=self.budget_db,
            old_work_order_ref=self.work.id, openclaw_root=self.openclaw_root,
        )
        changed = copy.deepcopy(candidate)
        changed["mission_version_hash"] = "b" * 64
        with self.assertRaises(ControlledFailureRedriveError):
            apply(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                  candidate=changed,
                  expected_candidate_hash=candidate["candidate_hash"])
        self.assertIsNone(approved_request(
            self.scheduler_db, self.budget_db, old_work_order_ref=self.work.id,
            formal=self.scheduler.formal_result(self.work.id),
            mission={**self.mission, "content_hash": "b" * 64},
        ))

    def test_success_and_unaccounted_failures_are_ineligible(self):
        with self.assertRaises(ControlledFailureRedriveError):
            prepare(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                    old_work_order_ref="work:missing", openclaw_root=self.openclaw_root)

    def test_missing_work_mission_binding_is_ineligible(self):
        authority = self.scheduler.work_order_authority(self.work.id)
        wire = authority["work_order"]
        wire["metadata"] = {}
        encoded = json.dumps(wire, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.scheduler.connection.execute("DROP TRIGGER scheduler_work_no_update")
        self.scheduler.connection.execute(
            "UPDATE scheduler_work_orders SET work_order_json=?,work_order_hash=? WHERE work_order_id=?",
            (encoded, content_hash(wire), self.work.id),
        )
        self.scheduler.connection.commit()
        with self.assertRaisesRegex(ControlledFailureRedriveError, "mission bindings disagree"):
            prepare(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                    old_work_order_ref=self.work.id, openclaw_root=self.openclaw_root)

    def test_apply_is_convergent_across_two_connections(self):
        candidate = prepare(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                            old_work_order_ref=self.work.id, openclaw_root=self.openclaw_root)
        barrier = threading.Barrier(2)
        outcomes = []
        errors = []

        def run():
            try:
                barrier.wait()
                outcomes.append(apply(
                    scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                    candidate=candidate, expected_candidate_hash=candidate["candidate_hash"],
                )["status"])
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertCountEqual(outcomes, ["fresh", "duplicate"])

    def test_approved_request_fails_closed_on_host_or_correction_drift(self):
        candidate = prepare(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                            old_work_order_ref=self.work.id, openclaw_root=self.openclaw_root)
        apply(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
              candidate=candidate, expected_candidate_hash=candidate["candidate_hash"])
        formal = self.scheduler.formal_result(self.work.id)
        bundle = Path(candidate["repair"]["bundle_path"])
        original = bundle.read_bytes()
        bundle.write_bytes(original + b"\n")
        self.assertIsNone(approved_request(
            self.scheduler_db, self.budget_db, old_work_order_ref=self.work.id,
            formal=formal, mission=self.mission,
        ))
        bundle.write_bytes(original)
        self.budget.connection.execute(
            "DROP TRIGGER thesis_impact_settlement_corrections_no_delete"
        )
        self.budget.connection.execute("DELETE FROM thesis_impact_settlement_corrections")
        self.budget.connection.commit()
        self.assertIsNone(approved_request(
            self.scheduler_db, self.budget_db, old_work_order_ref=self.work.id,
            formal=formal, mission=self.mission,
        ))

    def test_original_and_patched_host_blocks_are_rejected(self):
        candidate = prepare(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                            old_work_order_ref=self.work.id, openclaw_root=self.openclaw_root)
        bundle = Path(candidate["repair"]["bundle_path"])
        bundle.write_text(bundle.read_text("utf-8") + "\n" +
                          "\tif (runtime) completionModel = bindModelLlmRuntime(completionModel, runtime);\n",
                          "utf-8")
        with self.assertRaisesRegex(ControlledFailureRedriveError, "not installed"):
            prepare(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                    old_work_order_ref=self.work.id, openclaw_root=self.openclaw_root)

    def test_read_only_open_refuses_unprovisioned_wal_without_side_effects(self):
        copied = self.scheduler_db.parent / "wal-header.sqlite"
        copied.write_bytes(self.scheduler_db.read_bytes())
        payload = bytearray(copied.read_bytes())
        payload[18:20] = b"\x02\x02"
        copied.write_bytes(payload)
        before = sorted(path.name for path in copied.parent.iterdir())
        with self.assertRaises(sqlite3.OperationalError):
            prepare(scheduler_db=copied, budget_db=self.budget_db,
                    old_work_order_ref=self.work.id, openclaw_root=self.openclaw_root)
        self.assertEqual(sorted(path.name for path in copied.parent.iterdir()), before)

    def test_approved_request_fails_closed_when_correction_schema_is_absent(self):
        candidate = prepare(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                            old_work_order_ref=self.work.id, openclaw_root=self.openclaw_root)
        apply(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
              candidate=candidate, expected_candidate_hash=candidate["candidate_hash"])
        old_budget = self.scheduler_db.parent / "old-budget.sqlite"
        sqlite3.connect(old_budget).close()
        self.assertIsNone(approved_request(
            self.scheduler_db, old_budget, old_work_order_ref=self.work.id,
            formal=self.scheduler.formal_result(self.work.id), mission=self.mission,
        ))


if __name__ == "__main__":
    unittest.main()
