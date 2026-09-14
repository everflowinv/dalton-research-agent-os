from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.budget_pools import apply_pool_migration
from dalton_core.controlled_budget_reentry import (
    ControlledBudgetReentryError, apply, approved_business_key,
    approved_request, prepare,
)
from dalton_core.contracts import ResultEnvelope, WorkOrder
from dalton_core.company_dossier_launcher import CompanyDossierLauncher, run_digest
from dalton_core.scheduler import Scheduler
from dalton_core.store import canonical_json, content_hash
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore

NOW = datetime(2026, 9, 14, 11, 54, tzinfo=timezone.utc)


class ControlledBudgetReentryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.scheduler_db = root / "scheduler.sqlite"
        self.budget_db = root / "budget.sqlite"
        self.core_db = root / "core.sqlite"
        self.scheduler = Scheduler(self.scheduler_db, clock=lambda: NOW)
        self.addCleanup(self.scheduler.close)
        self.mission = {"id": "coverage-mission-version:test:17", "content_hash": "a" * 64}
        self.business = "company:test|state|permission:test"
        self.permission = self.business
        self.work = WorkOrder(
            schema_version="0.1", id="work:cockpit-dossier-old",
            created_at=NOW.isoformat(), updated_at=NOW.isoformat(), question="bounded",
            requested_capabilities=("research",), runtime_profile_ref="runtime:test",
            budget={"max_attempts": 1}, idempotency_key="old", declared_side_effects=(),
            status="ready", input_refs=(), metadata={
                "purpose": "dossier", "mission_version_ref": self.mission["id"],
                "mission_version_hash": self.mission["content_hash"],
            })
        self.scheduler.enqueue(self.work)
        lease = self.scheduler.claim("worker:test", work_order_id=self.work.id)
        envelope = ResultEnvelope(
            schema_version="0.1", id="result:pool-refused", created_at=NOW.isoformat(),
            work_order_ref=self.work.id, invocation_ref="invocation:none", status="failed", outputs={},
            actual_side_effects=(), usage_refs=(), artifact_refs=(),
            error={"code": "POOL_EXHAUSTED"}, metadata={})
        self.scheduler.complete(self.work.id, 1, "worker:test", lease["lease_token"], envelope,
                                idempotency_key="complete:old")
        store = ThesisImpactBudgetStore(self.budget_db); store.close()
        b = sqlite3.connect(self.budget_db); b.row_factory = sqlite3.Row
        apply_pool_migration(b)
        rejection = {
            "schema_version": "0.1", "rejection_id": "budget-pool-rejection:test",
            "day": "2026-09-14", "mission_ref": "coverage-mission:test",
            "pool": "coverage", "pool_lane": "dossier", "work_order_ref": self.work.id,
            "attempt_number": 1, "phase": "assessment", "reserved_micros": 1000000,
            "spent": 55000000, "cap": 55000000, "borrowable_micros": 0,
            "reason": "pool_exhausted", "status": "rejected", "created_at": NOW.isoformat(),
        }
        rejection["content_hash"] = content_hash(rejection)
        b.execute("INSERT INTO model_budget_pool_rejections VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            rejection["rejection_id"], rejection["day"], rejection["mission_ref"],
            rejection["pool"], rejection["pool_lane"], self.work.id, 1, "assessment",
            1000000, 55000000, 55000000, 0, canonical_json(rejection),
            rejection["content_hash"], rejection["created_at"]))
        b.commit(); self.budget_connection = b; self.addCleanup(b.close)
        c = sqlite3.connect(self.core_db)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE model_invocations(invocation_id TEXT, work_order_ref TEXT)")
        c.commit(); self.core_connection = c; self.addCleanup(c.close)

    def candidate(self):
        return prepare(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                       core_db=self.core_db, old_work_order_ref=self.work.id,
                       business_key=self.business, current_permission=self.permission,
                       mission=self.mission, allowed_purpose="dossier")

    def test_exact_no_send_refusal_authorizes_one_bound_reentry(self):
        candidate = self.candidate()
        old_formal = self.scheduler.formal_result(self.work.id)
        old_budget = self.budget_db.read_bytes()
        saved = apply(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                      core_db=self.core_db, candidate=candidate,
                      expected_candidate_hash=candidate["candidate_hash"], mission=self.mission)
        self.assertEqual(saved["status"], "fresh")
        self.assertEqual(self.budget_db.read_bytes(), old_budget)
        suffix = ":operator-recovery:" + saved["content_hash"][:16]
        self.assertEqual(approved_request(self.scheduler_db, self.budget_db,
                         old_work_order_ref=self.work.id, formal=old_formal,
                         mission=self.mission), suffix)
        self.assertEqual(approved_business_key(
            self.scheduler_db, business_key=self.business,
            current_permission=self.permission, mission=self.mission,
            allowed_purposes={"dossier"}),
            ":operator-recovery:" + saved["group_hash"][:16])
        self.assertEqual(apply(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                      core_db=self.core_db, candidate=candidate,
                      expected_candidate_hash=candidate["candidate_hash"],
                      mission=self.mission)["status"], "duplicate")
        self.assertEqual(self.scheduler.formal_result(self.work.id), old_formal)

    def test_paid_or_admitted_work_is_rejected(self):
        self.core_connection.execute("INSERT INTO model_invocations VALUES(?,?)", ("invocation:sent", self.work.id))
        self.core_connection.commit()
        with self.assertRaisesRegex(ControlledBudgetReentryError, "model invocation"):
            self.candidate()

    def test_tampered_candidate_and_permission_are_rejected(self):
        candidate = self.candidate(); candidate["business_key"] += ":changed"
        with self.assertRaisesRegex(ControlledBudgetReentryError, "candidate changed"):
            apply(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                  core_db=self.core_db, candidate=candidate,
                  expected_candidate_hash=candidate["candidate_hash"], mission=self.mission)
        self.assertIsNone(approved_business_key(
            self.scheduler_db, business_key=self.business,
            current_permission="permission:changed", mission=self.mission,
            allowed_purposes={"dossier"}))

    def test_nonempty_failed_output_is_not_eligible(self):
        # The closed predicate itself is covered by changing the immutable formal
        # authority in a copy, without weakening the real Scheduler tables.
        copydb = self.scheduler_db.parent / "copy.sqlite"
        self.scheduler.close(); copydb.write_bytes(self.scheduler_db.read_bytes())
        db = sqlite3.connect(copydb)
        db.execute("DROP TRIGGER scheduler_result_no_update")
        row = db.execute("SELECT result_record_id,result_envelope_json FROM scheduler_formal_results").fetchone()
        wire = json.loads(row[1]); wire["outputs"] = {"text": "paid"}
        db.execute("UPDATE scheduler_formal_results SET result_envelope_json=?,result_envelope_hash=? WHERE result_record_id=?",
                   (canonical_json(wire), content_hash(wire), row[0])); db.commit()
        self.addCleanup(db.close)
        with self.assertRaisesRegex(ControlledBudgetReentryError, "empty pool refusal"):
            prepare(scheduler_db=copydb, budget_db=self.budget_db, core_db=self.core_db,
                    old_work_order_ref=self.work.id, business_key=self.business,
                    current_permission=self.permission, mission=self.mission,
                    allowed_purpose="dossier")

    def test_group_requires_every_exact_ref_and_returns_one_marker(self):
        second = WorkOrder(
            schema_version="0.1", id="work:cockpit-dossier-old-2",
            created_at=NOW.isoformat(), updated_at=NOW.isoformat(), question="bounded two",
            requested_capabilities=("research",), runtime_profile_ref="runtime:test",
            budget={"max_attempts": 1}, idempotency_key="old-2", declared_side_effects=(),
            status="ready", input_refs=(), metadata={
                "purpose": "dossier", "mission_version_ref": self.mission["id"],
                "mission_version_hash": self.mission["content_hash"],
            })
        self.scheduler.enqueue(second)
        lease = self.scheduler.claim("worker:test", work_order_id=second.id)
        envelope = ResultEnvelope(
            schema_version="0.1", id="result:pool-refused-2", created_at=NOW.isoformat(),
            work_order_ref=second.id, invocation_ref="invocation:none", status="failed",
            outputs={}, actual_side_effects=(), usage_refs=(), artifact_refs=(),
            error={"code": "POOL_EXHAUSTED"}, metadata={})
        self.scheduler.complete(second.id, 1, "worker:test", lease["lease_token"], envelope,
                                idempotency_key="complete:old-2")
        rejection = {
            "schema_version": "0.1", "rejection_id": "budget-pool-rejection:test-2",
            "day": "2026-09-14", "mission_ref": "coverage-mission:test",
            "pool": "coverage", "pool_lane": "dossier", "work_order_ref": second.id,
            "attempt_number": 1, "phase": "assessment", "reserved_micros": 1000000,
            "spent": 55000000, "cap": 55000000, "borrowable_micros": 0,
            "reason": "pool_exhausted", "status": "rejected", "created_at": NOW.isoformat(),
        }
        rejection["content_hash"] = content_hash(rejection)
        self.budget_connection.execute(
            "INSERT INTO model_budget_pool_rejections VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                rejection["rejection_id"], rejection["day"], rejection["mission_ref"],
                rejection["pool"], rejection["pool_lane"], second.id, 1, "assessment",
                1000000, 55000000, 55000000, 0, canonical_json(rejection),
                rejection["content_hash"], rejection["created_at"]))
        self.budget_connection.commit()
        third = WorkOrder(
            schema_version="0.1", id="work:cockpit-dossier-old-3",
            created_at=NOW.isoformat(), updated_at=NOW.isoformat(), question="bounded three",
            requested_capabilities=("research",), runtime_profile_ref="runtime:test",
            budget={"max_attempts": 1}, idempotency_key="old-3", declared_side_effects=(),
            status="ready", input_refs=(), metadata={
                "purpose": "dossier", "mission_version_ref": self.mission["id"],
                "mission_version_hash": self.mission["content_hash"],
            })
        self.scheduler.enqueue(third)
        lease = self.scheduler.claim("worker:test", work_order_id=third.id)
        envelope = ResultEnvelope(
            schema_version="0.1", id="result:pool-refused-3", created_at=NOW.isoformat(),
            work_order_ref=third.id, invocation_ref="invocation:none", status="failed",
            outputs={}, actual_side_effects=(), usage_refs=(), artifact_refs=(),
            error={"code": "POOL_EXHAUSTED"}, metadata={})
        self.scheduler.complete(third.id, 1, "worker:test", lease["lease_token"], envelope,
                                idempotency_key="complete:old-3")
        rejection = {
            "schema_version": "0.1", "rejection_id": "budget-pool-rejection:test-3",
            "day": "2026-09-14", "mission_ref": "coverage-mission:test",
            "pool": "coverage", "pool_lane": "dossier", "work_order_ref": third.id,
            "attempt_number": 1, "phase": "assessment", "reserved_micros": 1000000,
            "spent": 55000000, "cap": 55000000, "borrowable_micros": 0,
            "reason": "pool_exhausted", "status": "rejected", "created_at": NOW.isoformat(),
        }
        rejection["content_hash"] = content_hash(rejection)
        self.budget_connection.execute(
            "INSERT INTO model_budget_pool_rejections VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                rejection["rejection_id"], rejection["day"], rejection["mission_ref"],
                rejection["pool"], rejection["pool_lane"], third.id, 1, "assessment",
                1000000, 55000000, 55000000, 0, canonical_json(rejection),
                rejection["content_hash"], rejection["created_at"]))
        self.budget_connection.commit()
        refs = [self.work.id, second.id, third.id]
        candidates = [prepare(
            scheduler_db=self.scheduler_db, budget_db=self.budget_db, core_db=self.core_db,
            old_work_order_ref=ref, business_key=self.business,
            current_permission=self.permission, mission=self.mission,
            allowed_purpose="dossier", group_work_order_refs=refs) for ref in refs]
        first = apply(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                      core_db=self.core_db, candidate=candidates[0],
                      expected_candidate_hash=candidates[0]["candidate_hash"], mission=self.mission)
        self.assertIsNone(approved_business_key(
            self.scheduler_db, business_key=self.business, current_permission=self.permission,
            mission=self.mission, allowed_purposes={"dossier"}))
        apply(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
              core_db=self.core_db, candidate=candidates[1],
              expected_candidate_hash=candidates[1]["candidate_hash"], mission=self.mission)
        self.assertIsNone(approved_business_key(
            self.scheduler_db, business_key=self.business, current_permission=self.permission,
            mission=self.mission, allowed_purposes={"dossier"}))
        apply(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
              core_db=self.core_db, candidate=candidates[2],
              expected_candidate_hash=candidates[2]["candidate_hash"], mission=self.mission)
        self.assertEqual(approved_business_key(
            self.scheduler_db, business_key=self.business, current_permission=self.permission,
            mission=self.mission, allowed_purposes={"dossier"}),
            ":operator-recovery:" + first["group_hash"][:16])

        model_config = self.scheduler_db.parent / "dossier-model.json"
        model_config.write_text(json.dumps({"budget_db": str(self.budget_db)}),
                                encoding="utf-8")
        launcher = CompanyDossierLauncher(
            state_dir=self.scheduler_db.parent, model_config_path=model_config,
            scheduler_db=self.scheduler_db, clock=lambda: NOW)
        ticket_id = "company-dossier-run:" + run_digest("company:test", self.business)
        ticket_dir = launcher.tickets_dir / ticket_id.split(":", 1)[1]
        ticket_dir.mkdir(parents=True, exist_ok=True)
        (ticket_dir / "ticket.json").write_text(canonical_json({
            "id": ticket_id, "status": "failed", "signature": self.business,
            "company_ref": "company:test",
        }) + "\n", encoding="utf-8")
        (ticket_dir / "summary.json").write_text("{}\n", encoding="utf-8")
        suffix = launcher.controlled_reentry(
            signature=self.business, company_ref="company:test", mission=self.mission)
        self.assertEqual(suffix, ":operator-recovery:" + first["group_hash"][:16])
        launcher.claim_controlled_reentry(ticket_id, suffix)
        self.assertIsNone(launcher.controlled_reentry(
            signature=self.business, company_ref="company:test", mission=self.mission))
