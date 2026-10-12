"""Terminal visibility and native connection teardown follow durable journal work."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from dalton_core.cockpit_plane import CockpitError, CockpitJournal, CockpitPlane


class JobJournalLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = CockpitJournal(Path(self.temp.name) / 'journal.sqlite')
        self.addCleanup(self.journal.close)

    def plane(self, journal):
        plane = CockpitPlane.__new__(CockpitPlane)
        plane.journal = journal
        plane._jobs = {}
        plane._jobs_lock = threading.Lock()
        plane.clock = lambda: datetime.now(timezone.utc)
        return plane

    def terminal_visibility(self, *, failure):
        plane = self.plane(self.journal)
        started = threading.Event()
        release = threading.Event()
        worker = []
        write = self.journal.write
        def delayed_write(sql, params=()):
            if sql.startswith('UPDATE cockpit_jobs'):
                worker.append(threading.current_thread())
                started.set()
                if not release.wait(5):
                    raise RuntimeError('fixture never released durable write')
            return write(sql, params)
        def runner():
            if failure:
                raise CockpitError('fixture validation refused')
            return {'answer': 'verified'}
        self.journal.write = delayed_write
        job = plane._start_job('fixture', 'owner', {}, runner)
        try:
            self.assertTrue(started.wait(5))
            self.assertEqual(plane.job('owner', job['job_id'])['status'], 'running')
            stored = self.journal.rows('SELECT status FROM cockpit_jobs WHERE job_id=?', (job['job_id'],))
            self.assertEqual(stored[0]['status'], 'running')
            release.set()
            worker[0].join(5)
            self.assertFalse(worker[0].is_alive())
            completed = plane.job('owner', job['job_id'])
            self.assertEqual(completed['status'], 'failed' if failure else 'done')
            # A fresh plane sees exactly the same persisted result/error/status.
            reopened = CockpitJournal(self.journal.path)
            self.addCleanup(reopened.close)
            restarted = self.plane(reopened).job('owner', job['job_id'])
            for key in ('status', 'result', 'error', 'updated_at'):
                self.assertEqual(restarted[key], completed[key])
        finally:
            release.set()
            for thread in worker:
                thread.join(5)

    def test_completed_job_is_visible_only_after_persistence_and_restarts_consistently(self):
        self.terminal_visibility(failure=False)

    def test_failed_job_is_visible_only_after_persistence_and_restarts_consistently(self):
        self.terminal_visibility(failure=True)

    def close_during_operation(self, *, reading):
        actual = self.journal.connection
        operation_started = threading.Event()
        release_operation = threading.Event()
        closer_started = threading.Event()
        closed = threading.Event()
        errors = []
        class ObservedConnection:
            def execute(self, sql, params=()):
                operation_started.set()
                if not release_operation.wait(5):
                    raise RuntimeError('fixture never released journal operation')
                return actual.execute(sql, params)
            def close(self):
                closed.set()
                actual.close()
        self.journal.connection = ObservedConnection()
        def operation():
            try:
                if reading:
                    self.journal.rows('SELECT 1')
                else:
                    self.journal.write('SELECT 1')
            except BaseException as exc:
                errors.append(exc)
        def close():
            closer_started.set()
            self.journal.close()
        user = threading.Thread(target=operation)
        closer = threading.Thread(target=close)
        user.start()
        try:
            self.assertTrue(operation_started.wait(5))
            closer.start()
            self.assertTrue(closer_started.wait(5))
            self.assertFalse(closed.wait(0.1), 'native close overlapped a journal operation')
        finally:
            release_operation.set()
            user.join(5)
            if closer.ident is not None:
                closer.join(5)
        self.assertFalse(user.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(closed.is_set())

    def test_close_serializes_with_inflight_journal_read(self):
        self.close_during_operation(reading=True)

    def test_close_serializes_with_inflight_journal_write(self):
        self.close_during_operation(reading=False)
