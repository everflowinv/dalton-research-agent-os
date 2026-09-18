"""C2-3: the document-research lane escapes its own deadlock, or names the owner's job.

The live shape (2026-09-16): 557 of 557 ticks returned
``{"status": "recovery_required", "held": 10,
   "reason": "no unstarted document research admission"}``.
``mission_document_research_controlled_recovery_authorizations`` held zero
rows, and there is no CLI or writer operation that could have written one -- so
"needs a controlled recovery authorization" was a state only an owner reading
the source could have diagnosed.  23 starts, 27 observations, 0 promotions and
0 outcomes in total.

Two things were wrong and they need different answers:

  * an admission that was held but **never started** sent nothing, cost
    nothing and has no open reservation, so reopening it is free -- the lane
    does that itself, bounded;
  * an admission that **did** start may have been charged for a send nobody
    can prove, so it is left exactly where it is and the lane says, in the
    reason a person reads, what has to be authorised and how.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.mission_document_research_lane import (
    CONTRACT_ESCALATION_NOTE,
    DEADLOCK_ESCAPE_AFTER,
    MAX_ESCAPES_PER_ADMISSION,
    OWNER_AUTHORIZATION_NOTE,
    MissionDocumentResearchCoordinator,
)

from tests.test_mission_document_research_lane import _Launcher, _Store


class Clock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs) -> None:
        self.moment = self.moment + timedelta(**kwargs)


class DeadlockEscapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = _Store(); self.addCleanup(self.store.connection.close)
        self.launcher = _Launcher(self.root / "tickets")
        self.clock = Clock(datetime(2026, 9, 16, 0, 0, tzinfo=timezone.utc))
        self.lane = MissionDocumentResearchCoordinator(
            store=self.store, launcher=self.launcher, clock=self.clock)
        # A held admission whose recovery is ``stopped`` is exactly the live
        # state: the executor recorded it and every later tick re-reads the
        # same verdict.  Stubbed here because the verdict, not the Scheduler
        # bookkeeping behind it, is what this file is about.
        self.lane._execution_state = lambda _admission: {
            "action": "recovery_required", "reason": "fresh_work_recovery_disabled",
        }

    def _hold(self, admission, *, reason: str, started: bool) -> None:
        holds = {}
        if self.lane.holds_path.is_file():
            from dalton_core.mission_document_research_lane import _read_holds
            holds = _read_holds(self.lane.holds_path)
        self.lane._hold(holds, admission, reason=reason, ticket_ref=None,
                        disposition="recovery_required")
        if started:
            self.store.started(admission["id"])

    def test_a_never_started_admission_is_reopened_after_the_deadlock_window(self) -> None:
        admission = self.store.add(1)
        self._hold(admission, reason="fresh_work_recovery_disabled", started=False)

        first = self.lane.dispatch_once()
        self.assertEqual(first["status"], "recovery_required")
        self.assertEqual(first["held"], 1)
        # The clock starts on the first all-held tick, not before it.
        self.assertEqual(self.launcher.started, [])

        self.clock.advance(hours=1)
        self.assertEqual(self.lane.dispatch_once()["status"], "recovery_required")

        self.clock.moment = self.clock.moment + DEADLOCK_ESCAPE_AFTER
        escaped = self.lane.dispatch_once()
        self.assertEqual(escaped["status"], "recovered")
        self.assertEqual(escaped["escaped"]["admission_ref"], admission["id"])
        self.assertEqual(escaped["escaped"]["prior_reason"], "fresh_work_recovery_disabled")
        self.assertIn("没有发出过模型调用", escaped["escaped"]["rationale"])
        # The next tick actually starts it.
        self.assertEqual(self.lane.dispatch_once()["status"], "launched")
        self.assertEqual(self.launcher.started,
                         [(admission["id"], admission["content_hash"])])

    def test_an_admission_that_started_is_never_reopened_automatically(self) -> None:
        admission = self.store.add(1)
        self._hold(admission, reason="send_state_unproved", started=True)
        self.clock.moment = self.clock.moment + timedelta(days=1)
        self.lane.dispatch_once()
        self.clock.moment = self.clock.moment + timedelta(days=1)
        result = self.lane.dispatch_once()
        self.assertEqual(result["status"], "recovery_required")
        self.assertEqual(self.launcher.started, [])
        self.assertEqual(result["waiting_on_owner"], 1)

    def test_the_reason_a_person_reads_names_what_to_authorise(self) -> None:
        admission = self.store.add(1)
        self.lane._execution_state = lambda _admission: {
            "action": "recovery_required", "reason": "send_state_unproved",
        }
        self._hold(admission, reason="send_state_unproved", started=True)
        result = self.lane.dispatch_once()
        self.assertIn("send_state_unproved", result["reason"])
        # The owner reads a command they can paste, not an executor method name.
        self.assertIn("dalton_core.document_recovery_cli holds", result["reason"])
        self.assertNotIn("authorize_paid_contract_recovery(", result["reason"])
        self.assertEqual(result["reason"][-len(OWNER_AUTHORIZATION_NOTE):],
                         OWNER_AUTHORIZATION_NOTE)

    def test_every_hold_reports_its_own_reason_rather_than_one_fixed_sentence(self) -> None:
        first = self.store.add(1)
        second = self.store.add(2)
        self._hold(first, reason="fresh_work_recovery_disabled", started=False)
        self._hold(second, reason="controlled_reentry_unavailable:LaneChildRejected", started=True)
        result = self.lane.dispatch_once()
        detail = {item["admission_ref"]: item for item in result["holds"]}
        self.assertEqual(detail[first["id"]]["reason"], "fresh_work_recovery_disabled")
        self.assertFalse(detail[first["id"]]["needs_owner_authorization"])
        self.assertTrue(detail[second["id"]]["needs_owner_authorization"])
        self.assertIsNotNone(detail[second["id"]]["owner_action"])

    def test_the_escape_is_bounded_per_admission(self) -> None:
        # Two attempts are worth making; a third is a loop.  Driven directly
        # so the bound is what is under test rather than the ticket dance.
        admission = self.store.add(1)
        detail = [{"admission_ref": admission["id"], "started": False,
                   "reason": "fresh_work_recovery_disabled",
                   "needs_owner_authorization": False}]
        for attempt in range(1, MAX_ESCAPES_PER_ADMISSION + 2):
            self.lane._write_escapes({
                "stuck_since": (self.clock.moment - DEADLOCK_ESCAPE_AFTER
                                - timedelta(minutes=1)).isoformat(),
                "escapes": (self.lane._read_escapes().get("escapes") or {}),
            })
            holds: dict = {}
            self.lane._hold(holds, admission, reason="fresh_work_recovery_disabled",
                            ticket_ref=None, disposition="recovery_required")
            escaped = self.lane._escape_deadlock(holds, detail)
            if attempt <= MAX_ESCAPES_PER_ADMISSION:
                self.assertIsNotNone(escaped, attempt)
                self.assertEqual(escaped["attempt"], attempt)
            else:
                self.assertIsNone(escaped, attempt)
        record = json.loads(self.lane.escapes_path.read_text(encoding="utf-8"))
        self.assertEqual(record["escapes"][admission["id"]], MAX_ESCAPES_PER_ADMISSION)

    def test_the_escalated_contract_hold_says_the_retry_already_happened(self) -> None:
        # The only contract state a person is asked about now.  If the ask did
        # not say the lane already bought one reply, the owner would authorise
        # the same purchase again without knowing.
        admission = self.store.add(1)
        self.lane._execution_state = lambda _admission: {
            "action": "recovery_required",
            "reason": "contract_failed_after_automatic_retry",
        }
        self._hold(admission, reason="contract_failed_after_automatic_retry",
                   started=True)
        result = self.lane.dispatch_once()
        self.assertEqual(result["waiting_on_owner"], 1)
        self.assertIn("contract_failed_after_automatic_retry", result["reason"])
        self.assertIn("已经自动重试过一次", result["reason"])
        self.assertIn("dalton_core.document_recovery_cli authorize-paid", result["reason"])
        self.assertEqual(result["reason"][-len(CONTRACT_ESCALATION_NOTE):],
                         CONTRACT_ESCALATION_NOTE)
        self.assertEqual(result["holds"][0]["owner_action"], CONTRACT_ESCALATION_NOTE)

    def test_a_daily_cap_wait_is_never_put_in_front_of_a_person(self) -> None:
        # The cap means "tomorrow", not "a person".  Showing it on the owner's
        # list is exactly the pile the owner asked not to be shown.
        admission = self.store.add(1)
        retry_at = (self.clock.moment + timedelta(hours=2)).isoformat()
        self.lane._execution_state = lambda _admission: {
            "action": "waiting",
            "reason": "automatic_contract_retry_day_cap_reached",
            "retry_at": retry_at,
        }
        holds: dict = {}
        self.lane._hold(holds, admission,
                        reason="automatic_contract_retry_day_cap_reached",
                        ticket_ref=None, disposition="recovery_wait",
                        retry_at=retry_at)
        self.store.started(admission["id"])
        result = self.lane.dispatch_once()
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["waiting_on_owner"], 0)
        self.assertFalse(result["holds"][0]["needs_owner_authorization"])
        self.assertIsNone(result["holds"][0]["owner_action"])
        self.assertEqual(result["holds"][0]["reason"],
                         "automatic_contract_retry_day_cap_reached")

    def test_a_lane_that_is_working_forgets_the_deadlock_clock(self) -> None:
        self.store.add(1)
        self.assertEqual(self.lane.dispatch_once()["status"], "launched")
        self.store.completed("mission-document-research-admission:" + f"{1:032x}")
        self.launcher.tickets["mission-document-research:" + f"{1:024x}"]["status"] = "succeeded"
        idle = self.lane.dispatch_once()
        self.assertIn(idle["status"], ("idle", "busy"))
        # Nothing was ever stuck, so there is nothing to write down: a healthy
        # lane does not rewrite a file every tick.
        self.assertFalse(self.lane.escapes_path.is_file())
        self.assertIsNone(self.lane._read_escapes()["stuck_since"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
