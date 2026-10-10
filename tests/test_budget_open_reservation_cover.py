"""A covering read path preserves old costs and releases locks before dispatch."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from dalton_core.thesis_impact_budget import (
    ThesisImpactBudgetStore, ThesisImpactBudgetValidationError,
    maintain_open_reservation_index)
from tests.test_thesis_impact_budget import store, admit
from tests import test_cockpit_model_fallback as fallback
NOW = fallback.NOW

INDEX = 'idx_thesis_impact_admission_policy_open_cover'


class OpenReservationCoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'budget.sqlite'
        self.budget = store(self.path); self.addCleanup(self.budget.close)
        self.budget.register_policy(policy_version_id='budget-policy:day:1', day_cap_micros=10_000_000)

    def test_legacy_prior_day_open_and_today_actual_cost_keep_exact_arithmetic(self):
        a = admit(self.budget, work='old-open', day='2026-08-21', reserved=300_000)
        b = admit(self.budget, work='today-actual', reserved=400_000)
        self.budget.settle(b['admission_id'], actual_micros=25_000)
        old_actual = admit(self.budget, work='old-settled', day='2026-08-21', reserved=900_000)
        self.budget.settle(old_actual['admission_id'], actual_micros=600_000)
        self.budget.register_policy(policy_version_id='budget-policy:day:2', day_cap_micros=10_000_000,
                                    prior_version_id='budget-policy:day:1')
        q = 'SELECT admission_id,reserved_micros FROM thesis_impact_day_admissions WHERE policy_version_id=? AND NOT EXISTS (SELECT 1 FROM thesis_impact_day_settlements s WHERE s.admission_id=thesis_impact_day_admissions.admission_id)'
        plans = [r[3] for r in self.budget.connection.execute('EXPLAIN QUERY PLAN '+q, ('budget-policy:day:1',))]
        self.assertTrue(any('COVERING INDEX '+INDEX in p for p in plans), plans)
        self.assertEqual(self.budget.day_summary(policy_version_id='budget-policy:day:2',day='2026-08-22')['committed_micros'],325_000)
        self.budget.settle(a['admission_id'], actual_micros=8_000)
        self.assertEqual(self.budget.day_summary(policy_version_id='budget-policy:day:2',day='2026-08-22')['committed_micros'],25_000)

    def test_existing_ledger_index_reentry_has_no_record_hash_mutation(self):
        a = admit(self.budget, work='preserve'); self.budget.settle(a['admission_id'], actual_micros=20_000)
        tables = ('thesis_impact_budget_policies','thesis_impact_day_admissions','thesis_impact_day_settlements')
        def rows(): return {t:[tuple(r) for r in self.budget.connection.execute('SELECT * FROM '+t)] for t in tables}
        before=rows(); self.budget.connection.execute('DROP INDEX '+INDEX)
        preview = maintain_open_reservation_index(self.path)
        self.assertFalse(preview['applied']); self.assertFalse(preview['index_present_before'])
        result=maintain_open_reservation_index(self.path,apply=True)
        self.assertTrue(result['applied']); self.assertEqual(before,rows())
        self.assertTrue(maintain_open_reservation_index(self.path,apply=True)['index_present_before'])
        self.assertEqual(before,rows())

    def test_wrong_schema_and_same_name_wrong_index_are_refused(self):
        bad=Path(self.tmp.name)/'wrong.sqlite'; sqlite3.connect(bad).close()
        with self.assertRaises(ThesisImpactBudgetValidationError): maintain_open_reservation_index(bad,apply=True)
        self.budget.connection.execute('DROP INDEX '+INDEX)
        self.budget.connection.execute('CREATE INDEX '+INDEX+' ON thesis_impact_day_admissions(day)')
        with self.assertRaises(ThesisImpactBudgetValidationError): maintain_open_reservation_index(self.path,apply=True)

    def test_lock_wait_is_bounded_and_does_not_change_admissions(self):
        self.budget.connection.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaisesRegex(sqlite3.OperationalError,'locked'):
                maintain_open_reservation_index(self.path,apply=True,busy_timeout_ms=10)
        finally: self.budget.connection.rollback()
        self.assertEqual(self.budget.connection.execute('SELECT COUNT(*) FROM thesis_impact_day_admissions').fetchone()[0],0)

    def test_actual_cockpit_dispatch_allows_other_admission_and_settlement(self):
        fixture=fallback.CockpitChainTests('test_only_a_proved_pre_send_failure_may_fall_back'); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        entered=threading.Event(); release=threading.Event(); outcomes=[]; errors=[]
        class WaitingAdapter(fallback.ChainAdapter):
            def execute(inner,work,route,profile):
                entered.set()
                if not release.wait(10): raise AssertionError('test transport was not released')
                return super().execute(work,route,profile)
        adapter=WaitingAdapter({}); model=fixture._model(adapter,policy_version_ref=fixture.chain_policy)
        def run():
            try: outcomes.append(model.call(purpose='plan',request_id='held-network',prompt='draft',mission=fixture.mission))
            except BaseException as e: errors.append(e)
        thread=threading.Thread(target=run);thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with ThesisImpactBudgetStore(fixture.root/'budget.sqlite',clock=lambda:NOW) as competitor:
                competitor.connection.execute('PRAGMA busy_timeout=500')
                admission=competitor.admit(policy_version_id=model.config['budget_policy_ref'],day=NOW.date().isoformat(),
                    work_order_ref='work:other-offline-call',attempt_number=1,phase='assessment',
                    route_decision_ref='route:offline',reserved_micros=1000)
                settlement=competitor.settle(admission['admission_id'],actual_micros=123)
                self.assertEqual(settlement['actual_micros'],123)
        finally:
            release.set();thread.join(10)
        self.assertFalse(thread.is_alive());self.assertFalse(errors,errors);self.assertEqual(len(outcomes),1)
        ledger=sqlite3.connect((fixture.root/'budget.sqlite').as_uri()+'?mode=ro',uri=True)
        try:
            ledger.execute('PRAGMA query_only=ON')
            self.assertEqual(ledger.execute('SELECT COUNT(*) FROM thesis_impact_day_settlements').fetchone()[0],2)
        finally:ledger.close()


if __name__ == '__main__': unittest.main()
