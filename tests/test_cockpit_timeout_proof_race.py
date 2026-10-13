"""A cold-reader exit race refuses before consuming or admitting a child."""
import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from tests import test_cockpit_timeout_recovery as fixture
from dalton_core.cockpit_model import CockpitModelProofUnavailable
from dalton_core.planner_paid_output_recovery import recovery_readers
from dalton_core.scheduler import Scheduler
from dalton_core.cockpit_timeout_recovery import TABLE, CONSUMERS


class TimeoutProofRaceTests(unittest.TestCase):
    setUp = fixture.CockpitTimeoutRecoveryTests.setUp
    timeout_parent = fixture.CockpitTimeoutRecoveryTests.timeout_parent
    snapshot = fixture.CockpitTimeoutRecoveryTests.snapshot
    def test_changed_cold_authority_on_exit_has_no_child_admission(self):
        self.timeout_parent(); self.clock.advance(61); before=self.snapshot()
        @contextmanager
        def changed(*args, **kwargs):
            with recovery_readers(*args, **kwargs) as readers:
                yield readers
            raise sqlite3.OperationalError('cold WAL authority changed during immutable snapshot')
        with patch('dalton_core.planner_paid_output_recovery.recovery_readers', changed):
            with self.assertRaises(CockpitModelProofUnavailable) as caught:
                self.model.call(**self.kw)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(self.snapshot(), before); self.assertEqual(len(self.adapter.served),1)
        with Scheduler(self.model.scheduler_db) as scheduler:
            self.assertEqual(scheduler.connection.execute('SELECT count(*) FROM scheduler_work_orders').fetchone()[0],1)
            # A reserved certificate can exist, but it has not been consumed.
            self.assertEqual((scheduler.connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?",(CONSUMERS,)).fetchone()[0] and scheduler.connection.execute('SELECT count(*) FROM '+CONSUMERS).fetchone()[0]),0)
        self.adapter.script={}
        self.assertTrue(self.model.call(**self.kw)['work_order_ref'])
        self.assertEqual(len(self.adapter.served),2)
