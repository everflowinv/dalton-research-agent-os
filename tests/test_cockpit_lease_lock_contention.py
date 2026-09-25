"""2026-09-24: a locked SQLite file must not strand a cockpit attempt's lease.

Legacy ``work:cockpit-event_judgement-e8200806…`` was claimed at 10:55:17 and
answered by the broker at 10:55:22 ($0.272); ``scheduler.complete`` lost to
``database is locked``, the backstop's own completion lost to the same lock
and only printed it, and the lease hung until 13:05:57 -- every re-ask in
between was "this request is already running".  ``c25dbb7c…``'s second
attempt was claimed at 12:40:56 and never completed at all.

These tests keep module-level imports to what already existed before the fix
so the lock tests can be run against the old code and seen to fail there.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.cockpit_model import CockpitModelError, _ReleaseLeaseOnError
from dalton_core.scheduler import Scheduler
# The module, not its classes: a TestCase imported by name would be collected
# and run a second time as part of this module.
from tests import test_cockpit_model_fallback as fallback


class _LockedCompletions:
    """``Scheduler.complete`` that loses to a writer for its first ``n`` calls."""

    def __init__(self, failures: int) -> None:
        self.remaining = failures
        self.calls = 0
        self.original = Scheduler.complete

    def install(self):
        def complete(scheduler, *args, **kwargs):
            return self(scheduler, *args, **kwargs)
        return patch.object(Scheduler, "complete", complete)

    def __call__(self, scheduler, *args, **kwargs):
        self.calls += 1
        if self.remaining is None or self.remaining > 0:
            if self.remaining is not None:
                self.remaining -= 1
            raise sqlite3.OperationalError("database is locked")
        return self.original(scheduler, *args, **kwargs)


class LockedAdapter(fallback.ChainAdapter):
    """The route fails the way the traceback did: locked mid-attempt."""

    def execute(self, work, route, profile):
        self.served.append(profile["id"])
        raise sqlite3.OperationalError("database is locked")


class CockpitLeaseLockContentionTests(unittest.TestCase):
    setUp = fallback.CockpitChainTests.setUp
    _model = fallback.CockpitChainTests._model

    def _ask(self, adapter, request_id="locked"):
        return self._model(adapter, policy_version_ref=self.chain_policy).call(
            purpose="event_judgement", request_id=request_id, prompt="judge",
            mission=self.mission)

    def _work_id(self) -> str:
        with Scheduler(self.root / "scheduler.sqlite") as scheduler:
            return scheduler.connection.execute(
                "SELECT work_order_id FROM scheduler_work_orders").fetchone()[0]

    def _history(self, work_id):
        with Scheduler(self.root / "scheduler.sqlite") as scheduler:
            return ([(e["attempt_number"], e["state"], e["reason"])
                     for e in scheduler.attempt_history(work_id)],
                    scheduler.status(work_id))

    def test_a_locked_completion_is_retried_and_the_answer_is_kept(self) -> None:
        locked = _LockedCompletions(3)
        with locked.install(), \
                patch("time.sleep"):
            answer = self._ask(fallback.ChainAdapter({}))
        self.assertIn("answered by", answer["text"])
        self.assertEqual(locked.calls, 4)
        history, status = self._history(answer["work_order_ref"])
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual([state for _, state, _ in history],
                         ["ready", "leased", "succeeded"])

    def test_a_backstop_completion_lost_to_the_lock_does_not_strand_the_lease(self) -> None:
        # The c25dbb7c shape: the attempt dies of the lock, and while the lock
        # is held every completion -- the backstop's included -- fails too.
        adapter = LockedAdapter({})
        with _LockedCompletions(None).install(), \
                patch("dalton_core.cockpit_model.LEASE_RELEASE_RETRY_SECONDS", 0.05,
                      create=True), \
                patch("time.sleep"), \
                self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
            self._ask(adapter)
        self.assertTrue(adapter.served)
        work_id = self._work_id()
        _, status = self._history(work_id)
        self.assertEqual(status["state"], "leased")  # nothing could be written
        # The lock is gone. The next ask must not be "already running" for
        # the rest of a 2h10m lease.
        again = fallback.ChainAdapter({})
        answer = self._ask(again)
        self.assertIn("answered by", answer["text"])
        history, status = self._history(work_id)
        self.assertEqual(status["state"], "succeeded")
        states = [(attempt, state) for attempt, state, _ in history]
        # Attempt 1 is settled with the holder's own abandon envelope, then
        # exactly one fresh attempt runs: never two in flight.
        self.assertEqual(states, [(1, "ready"), (1, "leased"), (1, "retryable"),
                                  (2, "ready"), (2, "leased"), (2, "succeeded")])

    def test_an_answer_whose_completion_was_lost_is_committed_not_paid_again(self) -> None:
        # The e8200806 shape: the broker answered, the completion lost.
        first = fallback.ChainAdapter({})
        with _LockedCompletions(None).install(), \
                patch("dalton_core.cockpit_model.LEASE_RELEASE_RETRY_SECONDS", 0.05), \
                patch("time.sleep"), \
                self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
            self._ask(first)
        self.assertEqual(len(first.served), 1)
        work_id = self._work_id()
        records = list((self.root / "scheduler.sqlite.lease-holders").glob("*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "abandoned")
        self.assertEqual(record["pending_completion"]["result_envelope"]["status"],
                         "succeeded")
        self.assertEqual(records[0].stat().st_mode & 0o777, 0o600)
        failures = (self.root / "scheduler.sqlite.lease-holders"
                    / "release-failures.jsonl").read_text(encoding="utf-8").splitlines()
        entry = json.loads(failures[-1])
        self.assertEqual(entry["work_order_id"], work_id)
        self.assertIn("database is locked", entry["error"])
        self.assertNotIn("pending_completion", entry)  # no token in the log
        self.assertTrue(entry["traceback"])

        again = fallback.ChainAdapter({})
        answer = self._ask(again)
        self.assertEqual(again.served, [])  # nothing paid for twice
        self.assertIn("answered by", answer["text"])
        history, status = self._history(work_id)
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual([s for _, s, _ in history], ["ready", "leased", "succeeded"])
        self.assertEqual(list((self.root / "scheduler.sqlite.lease-holders")
                              .glob("*.json")), [])

    def _strand_a_lease(self) -> tuple[str, Path]:
        """A holder that vanished without completing: no backstop ran."""

        with patch.object(_ReleaseLeaseOnError, "__exit__", return_value=False), \
                self.assertRaisesRegex(RuntimeError, "crash"):
            class Crash(fallback.ChainAdapter):
                def execute(self, work, route, profile):
                    raise RuntimeError("crash")
            self._ask(Crash({}))
        records = list((self.root / "scheduler.sqlite.lease-holders").glob("*.json"))
        self.assertEqual(len(records), 1)
        return self._work_id(), records[0]

    def _rewrite_pid(self, record_path: Path, pid: int) -> None:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "held")
        record["pid"] = pid
        record_path.write_text(json.dumps(record), encoding="utf-8")

    def test_a_lease_whose_holder_process_exited_is_reclaimed(self) -> None:
        work_id, record_path = self._strand_a_lease()
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        self._rewrite_pid(record_path, gone.pid)
        answer = self._ask(fallback.ChainAdapter({}))
        self.assertIn("answered by", answer["text"])
        history, status = self._history(work_id)
        self.assertEqual(status["state"], "succeeded")
        self.assertIn((1, "expired", "lease_holder_gone:process_exited"), history)

    def test_a_lease_whose_holder_may_be_alive_is_left_alone(self) -> None:
        work_id, record_path = self._strand_a_lease()
        alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(alive.wait)
        self.addCleanup(alive.kill)
        self._rewrite_pid(record_path, alive.pid)
        with self.assertRaisesRegex(CockpitModelError, "already running"):
            self._ask(fallback.ChainAdapter({}))
        # And a holder that is this very process is never presumed gone.
        self._rewrite_pid(record_path, os.getpid())
        with self.assertRaisesRegex(CockpitModelError, "already running"):
            self._ask(fallback.ChainAdapter({}))
        _, status = self._history(work_id)
        self.assertEqual(status["state"], "leased")

    def _past_one_call_inside_the_lease(self, work_id):
        """A moment older than one call of the work, before its lease runs out."""

        from datetime import datetime, timedelta

        from dalton_core import cockpit_model
        from dalton_core.sqlite_contention import LEASE_RELEASE_RETRY_SECONDS

        with Scheduler(self.root / "scheduler.sqlite") as scheduler:
            work = scheduler.work_order_authority(work_id)["work_order"]
            expires = datetime.fromisoformat(scheduler.connection.execute(
                "SELECT expires_at FROM scheduler_leases WHERE work_order_id=? "
                "ORDER BY lease_version DESC LIMIT 1", (work_id,)).fetchone()[0])
        moment = fallback.NOW + timedelta(seconds=float(work["budget"]["max_seconds"])
                                          + cockpit_model._LEASE_GRACE_SECONDS
                                          + LEASE_RELEASE_RETRY_SECONDS + 1)
        self.assertLess(moment, expires)
        return moment

    def test_an_unrecorded_lease_from_before_this_process_is_reclaimed(self) -> None:
        # 2026-09-25: the release before 2026-09-24b never wrote holder
        # records, so a lease it left behind at the switch could only wait out
        # its frozen lifetime.
        from datetime import timedelta

        from dalton_core import cockpit_model

        work_id, record_path = self._strand_a_lease()
        record_path.unlink()  # what the old release left: no record at all
        later = self._past_one_call_inside_the_lease(work_id)
        # Claimed before this process started, and older than one call.
        with patch.object(cockpit_model, "_PROCESS_STARTED_AT",
                          fallback.NOW + timedelta(minutes=1)):
            answer = self._model(fallback.ChainAdapter({}), policy_version_ref=self.chain_policy,
                                 clock=lambda: later).call(
                purpose="event_judgement", request_id="locked", prompt="judge",
                mission=self.mission)
        self.assertIn("answered by", answer["text"])
        history, status = self._history(work_id)
        self.assertEqual(status["state"], "succeeded")
        self.assertIn((1, "expired", "lease_holder_gone:unrecorded_before_process_start"),
                      history)

    def test_an_unrecorded_lease_that_may_be_live_is_left_alone(self) -> None:
        from datetime import timedelta

        from dalton_core import cockpit_model

        work_id, record_path = self._strand_a_lease()
        record_path.unlink()
        later = self._past_one_call_inside_the_lease(work_id)
        # Claimed after this process started: a sibling of this release that
        # is (for whatever reason) unrecorded is not presumed gone.
        with patch.object(cockpit_model, "_PROCESS_STARTED_AT",
                          fallback.NOW - timedelta(minutes=1)), \
                self.assertRaisesRegex(CockpitModelError, "already running"):
            self._model(fallback.ChainAdapter({}), policy_version_ref=self.chain_policy,
                        clock=lambda: later).call(
                purpose="event_judgement", request_id="locked", prompt="judge",
                mission=self.mission)
        # Claimed before it, but no older than one call could take.
        with patch.object(cockpit_model, "_PROCESS_STARTED_AT",
                          fallback.NOW + timedelta(minutes=1)), \
                self.assertRaisesRegex(CockpitModelError, "already running"):
            self._model(fallback.ChainAdapter({}), policy_version_ref=self.chain_policy,
                        clock=lambda: fallback.NOW + timedelta(seconds=20)).call(
                purpose="event_judgement", request_id="locked", prompt="judge",
                mission=self.mission)
        _, status = self._history(work_id)
        self.assertEqual(status["state"], "leased")

    def test_a_locked_budget_ledger_is_waited_out_on_admission(self) -> None:
        # 2026-09-25 06:19: the planner's admission timed out at BEGIN
        # IMMEDIATE on the budget file and the call failed without a retry.
        from dalton_core import cockpit_model
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore

        original = ThesisImpactBudgetStore.admit
        calls = []

        def locked_once(store, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return original(store, *args, **kwargs)

        with patch.object(ThesisImpactBudgetStore, "admit", locked_once), \
                patch.object(cockpit_model, "_lock_retry_sleep", lambda _s: None):
            answer = self._ask(fallback.ChainAdapter({}), request_id="budget-locked")
        self.assertIn("answered by", answer["text"])
        self.assertEqual(len(calls), 2)

    def _open_admissions(self):
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore

        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as budget:
            return [dict(row) for row in budget.connection.execute(
                "SELECT a.admission_id,a.work_order_ref,a.reserved_micros "
                "FROM thesis_impact_day_admissions a WHERE NOT EXISTS ("
                "SELECT 1 FROM thesis_impact_day_settlements s "
                "WHERE s.admission_id=a.admission_id)").fetchall()]

    def test_a_settlement_lost_to_the_lock_is_replayed_by_the_next_call(self) -> None:
        # 2026-09-25, legacy: two event judgements and two language calls
        # finished, their settlements lost to "database is locked", and the
        # open reservations kept charging the day ledger (~$1.8, $0.9 of it in
        # the event pool) with nothing left to ever close them.
        from dalton_core import cockpit_model
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore

        original = ThesisImpactBudgetStore.settle
        with patch.object(ThesisImpactBudgetStore, "settle",
                          side_effect=sqlite3.OperationalError("database is locked")), \
                patch.object(cockpit_model, "_lock_retry_sleep", lambda _s: None), \
                patch.object(cockpit_model, "LEASE_RELEASE_RETRY_SECONDS", 0):
            answer = self._ask(fallback.ChainAdapter({}), request_id="settle-locked")
        self.assertIn("answered by", answer["text"])
        [stranded] = self._open_admissions()
        pending = list((self.root / "budget.sqlite.pending-settlements").glob("*.json"))
        self.assertEqual(len(pending), 1)
        recorded = json.loads(pending[0].read_text(encoding="utf-8"))
        self.assertEqual(recorded["admission_id"], stranded["admission_id"])

        # Any later call that opens the ledger writes it, at the cost the
        # lost settlement carried, before it takes out its own reservation.
        # (The fixture's event pool is smaller than two reservations, so the
        # next ask may itself be refused for the pool -- after the replay.)
        with patch.object(ThesisImpactBudgetStore, "settle", original):
            try:
                self._ask(fallback.ChainAdapter({}), request_id="the-next-one")
            except CockpitModelError:
                pass
        self.assertEqual(self._open_admissions(), [])
        self.assertEqual(
            list((self.root / "budget.sqlite.pending-settlements").glob("*.json")), [])
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as budget:
            settled = budget.connection.execute(
                "SELECT actual_micros FROM thesis_impact_day_settlements "
                "WHERE admission_id=?", (stranded["admission_id"],)).fetchone()
        self.assertEqual(settled["actual_micros"], recorded["actual_micros"])

    def test_an_admission_the_caller_gave_up_on_is_voided_if_it_landed(self) -> None:
        # The admit landed, but the lock the caller saw said otherwise: the
        # caller raised and never called the model under it.
        from dalton_core import cockpit_model
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore

        original = ThesisImpactBudgetStore.admit

        def landed_then_locked(store, *args, **kwargs):
            original(store, *args, **kwargs)
            raise sqlite3.OperationalError("database is locked")

        adapter = fallback.ChainAdapter({})
        with patch.object(ThesisImpactBudgetStore, "admit", landed_then_locked), \
                patch.object(cockpit_model, "_lock_retry_sleep", lambda _s: None), \
                patch.object(cockpit_model, "LEASE_RELEASE_RETRY_SECONDS", 0), \
                self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
            self._ask(adapter, request_id="admit-locked")
        self.assertEqual(adapter.served, [])
        [stranded] = self._open_admissions()

        self._ask(fallback.ChainAdapter({}), request_id="the-next-one")
        self.assertEqual(self._open_admissions(), [])
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as budget:
            settled = budget.connection.execute(
                "SELECT actual_micros FROM thesis_impact_day_settlements "
                "WHERE admission_id=?", (stranded["admission_id"],)).fetchone()
        self.assertEqual(settled["actual_micros"], 0)

    def test_a_record_for_an_admission_that_never_landed_is_simply_dropped(self) -> None:
        from dalton_core.pending_settlement_registry import PendingSettlements
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore

        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as budget:
            registry = PendingSettlements.for_budget(budget)
            self.assertTrue(registry.record_unconfirmed_admission(
                work_order_ref="work:cockpit-event_judgement-" + "0" * 32,
                attempt_number=1, phase="assessment", reason="locked"))
            self.assertTrue(registry.record_settlement(
                admission_id="thesis-impact-admission:" + "0" * 32,
                actual_micros=5, reason="locked"))
            results = registry.drain(budget)
        self.assertEqual(sorted(item["status"] for item in results),
                         ["never_admitted", "superseded"])
        self.assertEqual(registry.pending(), [])

    def test_a_still_locked_ledger_keeps_the_record_for_later(self) -> None:
        from dalton_core.pending_settlement_registry import PendingSettlements
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore

        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as budget:
            registry = PendingSettlements.for_budget(budget)
            registry.record_settlement(
                admission_id="thesis-impact-admission:" + "1" * 32, actual_micros=5)
            with patch.object(ThesisImpactBudgetStore, "settle",
                              side_effect=sqlite3.OperationalError("database is locked")):
                self.assertEqual([item["status"] for item in registry.drain(budget)],
                                 ["locked"])
        self.assertEqual(len(registry.pending()), 1)

    def test_the_abandon_envelope_names_the_locked_database(self) -> None:
        from dalton_core import model_router

        blocker = sqlite3.connect(str(self.router_db), isolation_level=None)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        self.addCleanup(blocker.rollback)
        with patch.object(model_router, "SQLITE_BUSY_TIMEOUT_MS", 50), \
                self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
            self._ask(fallback.ChainAdapter({}))
        work_id = self._work_id()
        with Scheduler(self.root / "scheduler.sqlite") as scheduler:
            envelope = json.loads(scheduler.connection.execute(
                "SELECT result_envelope_json FROM scheduler_result_envelopes "
                "WHERE work_order_id=?", (work_id,)).fetchone()[0])
        self.assertEqual(envelope["error"]["code"], "COCKPIT_ATTEMPT_ABANDONED")
        self.assertIn(str(self.router_db), envelope["error"]["message"])
        diagnostics = envelope["metadata"]["failure_diagnostics"]
        self.assertEqual(diagnostics["database_path"], str(self.router_db))
        self.assertEqual(diagnostics["exception_type"], "OperationalError")
        self.assertTrue(any("model_router.py" in frame
                            for frame in diagnostics["traceback"]))


class OrphanedLeaseSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.scheduler = Scheduler(Path(self.directory.name) / "s.sqlite", max_attempts=2)
        self.addCleanup(self.scheduler.close)

    def test_expiry_is_a_compare_and_set_on_the_claimed_revision(self) -> None:
        from dalton_core.scheduler import LeaseRejected
        from tests.test_scheduler import result, work_order

        self.scheduler.enqueue(work_order())
        lease = self.scheduler.claim("worker:a")
        revision = lease["lease"]["id"]
        stale = self.scheduler.expire_orphaned_lease(
            "work-1", 1, "lease-revision-other", reason="process_exited")
        self.assertEqual(stale["status"], "not_current")
        done = self.scheduler.expire_orphaned_lease(
            "work-1", 1, revision, reason="process_exited")
        self.assertEqual(done["status"], "expired")
        self.assertEqual(done["expired"]["reason"], "lease_holder_gone:process_exited")
        self.assertEqual(done["next"]["state"], "ready")
        # The old holder can no longer complete: exactly one attempt in flight.
        with self.assertRaises(LeaseRejected):
            self.scheduler.complete("work-1", 1, "worker:a", lease["lease_token"],
                                    result("r1"), idempotency_key="late")
        again = self.scheduler.expire_orphaned_lease(
            "work-1", 1, revision, reason="process_exited")
        self.assertEqual(again["status"], "not_current")
        second = self.scheduler.claim("worker:b")
        self.assertEqual(second["attempt"]["attempt_number"], 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
