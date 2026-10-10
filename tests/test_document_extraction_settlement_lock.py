"""Paid extraction finishes once while exact accounting survives ledger locks."""
import hashlib
import json
import sqlite3
import unittest
from datetime import timedelta
from unittest.mock import patch

from tests import test_document_extraction_admission as fixture
from dalton_core import document_extraction_settlement as settlement
from dalton_core.pending_settlement_registry import PendingSettlements
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore, ThesisImpactBudgetConflict


class ExtractionSettlementLockTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.BrokerAdmissionTests('test_broker_composition_reserves_then_accounts_once_and_replays')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def generate(self):
        with self.f.patch_execute():
            return self.f.h.generate()

    def rows(self):
        c = self.f.b.connection
        return c.execute('SELECT * FROM thesis_impact_day_settlements').fetchall()

    def test_one_lock_retries_without_repeating_provider_or_accounting(self):
        original = ThesisImpactBudgetStore.settle
        calls = []
        def once(budget, *args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise sqlite3.OperationalError('database is locked')
            return original(budget, *args, **kwargs)
        with patch.object(ThesisImpactBudgetStore, 'settle', autospec=True, side_effect=once):
            result = self.generate()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.f.calls, 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['actual_micros'], 1000)
        self.assertEqual(self.rows()[0]['usage_entry_ref'], calls[0]['usage_entry_ref'])
        self.generate()
        self.assertEqual(self.f.calls, 1)

    def test_continuous_lock_defers_exact_binding_then_recovers_idempotently(self):
        with patch.object(settlement, 'SETTLEMENT_RETRY_SECONDS', 0), patch.object(
                ThesisImpactBudgetStore, 'settle', side_effect=sqlite3.OperationalError('database is locked')):
            result = self.generate()
        self.assertEqual(result['status'], 'succeeded')
        self.assertIsNotNone(self.f.h.h.scheduler.formal_result(result['work_order_ref']))
        self.assertEqual(self.rows(), [])
        registry = PendingSettlements.for_budget(self.f.b)
        pending = registry.pending()
        self.assertEqual(len(pending), 1)
        record = pending[0][1]
        self.assertEqual(record['actual_micros'], 1000)
        self.assertEqual(record['work_order_ref'], result['work_order_ref'])
        self.assertEqual(record['attempt_number'], 1)
        self.assertTrue(record['usage_entry_ref'].startswith('usage-entry:'))
        before = self.f.h.h.core.connection.execute('SELECT count(*) FROM observability_cost_entries').fetchone()[0]
        registry.drain(self.f.b)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['usage_entry_ref'], record['usage_entry_ref'])
        self.assertEqual(registry.drain(self.f.b), [])
        self.generate()
        self.assertEqual(self.f.calls, 1)
        after = self.f.h.h.core.connection.execute('SELECT count(*) FROM observability_cost_entries').fetchone()[0]
        self.assertEqual(before, after)

    def test_registry_unavailable_surfaces_lock_and_never_claims_settled(self):
        with patch.object(settlement, 'SETTLEMENT_RETRY_SECONDS', 0), patch.object(
                ThesisImpactBudgetStore, 'settle', side_effect=sqlite3.OperationalError('database is locked')), patch.object(
                PendingSettlements, 'record_settlement', return_value=False):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                self.generate()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.f.calls, 1)

    def test_non_lock_sqlite_error_is_not_deferred(self):
        with patch.object(ThesisImpactBudgetStore, 'settle', side_effect=sqlite3.OperationalError('no such table: broken')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'no such table'):
                self.generate()
        self.assertEqual(PendingSettlements.for_budget(self.f.b).pending(), [])

    def test_governance_conflict_is_not_deferred(self):
        with patch.object(ThesisImpactBudgetStore, 'settle', side_effect=ThesisImpactBudgetConflict('wrong admission')):
            with self.assertRaisesRegex(ThesisImpactBudgetConflict, 'wrong admission'):
                self.generate()
        self.assertEqual(PendingSettlements.for_budget(self.f.b).pending(), [])

    def test_normal_worker_open_drains_pending_before_any_new_dispatch(self):
        with patch.object(settlement, 'SETTLEMENT_RETRY_SECONDS', 0), patch.object(
                ThesisImpactBudgetStore, 'settle', side_effect=sqlite3.OperationalError('database is locked')):
            result = self.generate()
        from dalton_core.contracts import WorkOrder
        authority = self.f.h.h.scheduler.work_order_authority(result['work_order_ref'])
        work = WorkOrder.from_dict(authority['work_order'])
        with self.f.patch_execute():
            self.f.h.service._run_broker_work(work, work.metadata['context'], fixture.OWNER)
        self.assertEqual(self.f.calls, 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(PendingSettlements.for_budget(self.f.b).pending(), [])

    def test_settlement_pending_cannot_replace_different_cost_or_usage(self):
        registry = PendingSettlements.for_budget(self.f.b)
        kw = dict(admission_id='admission:synthetic', actual_micros=1000,
                  usage_entry_ref='usage:synthetic', work_order_ref='work:synthetic', attempt_number=1)
        self.assertTrue(registry.record_settlement(**kw))
        path = registry.pending()[0][0]
        before = path.read_bytes()
        self.assertTrue(registry.record_settlement(**kw, reason='retry'))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(registry.record_settlement(**(kw | {'actual_micros': 2000})))
        self.assertFalse(registry.record_settlement(**(kw | {'usage_entry_ref': 'usage:other'})))
        self.assertEqual(path.read_bytes(), before)

    def test_old_pending_shape_stays_old_and_replays_without_usage_argument(self):
        registry = PendingSettlements.for_budget(self.f.b)
        self.assertTrue(registry.record_settlement(admission_id='old:synthetic', actual_micros=10))
        record = registry.pending()[0][1]
        self.assertNotIn('usage_entry_ref', record)
        with patch.object(self.f.b, 'settle', return_value={'status': 'duplicate'}) as replay:
            registry.drain(self.f.b)
        replay.assert_called_once_with('old:synthetic', actual_micros=10)

    def test_retry_is_capped_by_actual_lease_remaining(self):
        with patch.object(settlement, 'SETTLEMENT_RETRY_SECONDS', 0), patch.object(
                ThesisImpactBudgetStore, 'settle', side_effect=sqlite3.OperationalError('database is locked')):
            result = self.generate()
        # The exact lease history, not the model's timeout, caps this wait.
        connection = self.f.h.h.scheduler.connection
        expiry = connection.execute('SELECT expires_at FROM scheduler_leases ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        from datetime import datetime
        edge = datetime.fromisoformat(expiry)
        self.assertEqual(settlement.retry_within_lease(self.f.h.h.scheduler, result['work_order_ref'], 1, edge-timedelta(seconds=8)), 3)
        self.assertEqual(settlement.retry_within_lease(self.f.h.h.scheduler, result['work_order_ref'], 1, edge), 0)

    def test_real_sqlite_lock_keeps_record_and_restores_busy_timeout(self):
        admission = self.f.b.admit(policy_version_id='budget:owner:1', day='2026-10-10',
            work_order_ref='work:synthetic-locked', attempt_number=1, phase='assessment',
            route_decision_ref='route:synthetic', reserved_micros=10000)
        before = self.f.b.connection.execute('PRAGMA busy_timeout').fetchone()[0]
        with sqlite3.connect(self.f.b.path, isolation_level=None) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            result = settlement.settle_accounted_window(self.f.b, admission,
                actual_micros=123, usage_entry_ref='usage:synthetic', retry_seconds=0)
            self.assertIsNone(result)
            blocker.rollback()
        self.assertEqual(self.f.b.connection.execute('PRAGMA busy_timeout').fetchone()[0], before)
        PendingSettlements.for_budget(self.f.b).drain(self.f.b)
        self.assertEqual(self.rows()[0]['actual_micros'], 123)

    def test_same_process_concurrent_pending_records_cannot_truncate_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        registry = PendingSettlements.for_budget(self.f.b)
        gate = Barrier(2)
        def record(amount):
            gate.wait()
            return registry.record_settlement(admission_id='concurrent:synthetic',
                actual_micros=amount, usage_entry_ref=f'usage:{amount}',
                work_order_ref='work:synthetic', attempt_number=1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(record, [100, 200]))
        self.assertEqual(sorted(outcomes), [False, True])
        pending = registry.pending()
        self.assertEqual(len(pending), 1)
        body = json.loads(pending[0][0].read_bytes())
        self.assertEqual(body['usage_entry_ref'], f"usage:{body['actual_micros']}")
        self.assertEqual(list(registry.directory.glob('.*.tmp')), [])
        self.assertEqual([p for p in registry.directory.iterdir() if p.name.startswith('.')], [])

    def test_paid_pending_governance_conflict_is_kept_and_blocks_new_dispatch(self):
        registry = PendingSettlements.for_budget(self.f.b)
        registry.record_settlement(admission_id='missing:synthetic', actual_micros=100,
            usage_entry_ref='usage:synthetic')
        before = registry.pending()[0][0].read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'requires reconciliation'):
            settlement.drain_accounted_windows(self.f.b)
        self.assertEqual(registry.pending()[0][0].read_bytes(), before)
        self.assertEqual(self.f.calls, 0)

    def test_pending_drain_real_lock_is_bounded_and_restores_connection(self):
        registry = PendingSettlements.for_budget(self.f.b)
        registry.record_settlement(admission_id='missing:synthetic', actual_micros=100,
            usage_entry_ref='usage:synthetic')
        before = self.f.b.connection.execute('PRAGMA busy_timeout').fetchone()[0]
        import time
        with sqlite3.connect(self.f.b.path, isolation_level=None) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            began = time.monotonic()
            outcomes = settlement.drain_accounted_windows(self.f.b)
            elapsed = time.monotonic() - began
            blocker.rollback()
        self.assertLess(elapsed, 2.0)
        self.assertEqual(outcomes[0]['status'], 'locked')
        self.assertEqual(self.f.b.connection.execute('PRAGMA busy_timeout').fetchone()[0], before)
        self.assertEqual(len(registry.pending()), 1)

    def test_directory_sync_failure_is_not_reported_as_durable(self):
        import os
        import stat
        registry = PendingSettlements.for_budget(self.f.b)
        sync = os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('synthetic directory fsync failure')
            sync(fd)
        with patch('dalton_core.pending_settlement_registry.os.fsync', side_effect=fail_directory):
            self.assertFalse(registry.record_settlement(admission_id='sync:synthetic',
                actual_micros=100, usage_entry_ref='usage:synthetic'))
        self.assertEqual([p for p in registry.directory.iterdir() if p.name.startswith('.')], [])
