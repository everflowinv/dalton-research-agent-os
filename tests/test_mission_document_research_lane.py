"""Writer dispatch for immutable mission document-research admissions."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.lane_registry import lane_for_operation, registered_lanes
from dalton_core.mission_document_research_lane import (
    LANE,
    LANE_CONFIG,
    MissionDocumentResearchCoordinator,
    MissionDocumentResearchLaneError,
    argv_fragment,
    lane_configuration,
)
from dalton_core.scheduler import Scheduler
from dalton_core.store import canonical_json, content_hash


class _Store:
    def __init__(self) -> None:
        self.auto_commit = False
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE mission_document_research_admissions (
                admission_id TEXT PRIMARY KEY,
                record_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE mission_document_research_outcomes (
                outcome_id TEXT PRIMARY KEY,
                admission_ref TEXT NOT NULL UNIQUE
            );
            CREATE TABLE mission_document_research_starts (
                start_id TEXT PRIMARY KEY,
                admission_ref TEXT NOT NULL UNIQUE
            );
            """
        )
        Scheduler(
            connection=self.connection, max_attempts=2,
            default_lease_seconds=30, max_lease_seconds=60,
            max_total_lease_seconds=120,
        )

    def active_policy(self) -> dict:
        return {
            "policy": {
                "research_candidate_auto_commit": {
                    "enabled": self.auto_commit,
                    "rules": (
                        ["research-auto-commit:mission-document-qualitative:v1"]
                        if self.auto_commit else []
                    ),
                    "max_records": 10,
                }
            }
        }

    def add(self, ordinal: int) -> dict:
        body = {
            "schema_version": "0.1",
            "id": f"mission-document-research-admission:{ordinal:032x}",
            "created_at": f"2026-09-11T12:00:0{ordinal}.000000+00:00",
            "identity_hash": content_hash({"ordinal": ordinal}),
        }
        wire = {**body, "content_hash": content_hash(body)}
        self.connection.execute(
            "INSERT INTO mission_document_research_admissions VALUES(?,?,?,?)",
            (wire["id"], canonical_json(wire), wire["content_hash"], wire["created_at"]),
        )
        self.connection.commit()
        return wire

    def refused(self, admission_ref: str) -> dict:
        """Record the refusal an executor writes for a candidate the rule declined."""

        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS "
            "mission_document_research_candidate_rejections("
            "rejection_id TEXT PRIMARY KEY,admission_ref TEXT NOT NULL UNIQUE,"
            "outcome_ref TEXT NOT NULL UNIQUE,rule_ref TEXT NOT NULL,"
            "reason TEXT NOT NULL,candidate_claim_ref TEXT NOT NULL,"
            "candidate_claim_hash TEXT NOT NULL,record_json TEXT NOT NULL,"
            "content_hash TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL)"
        )
        digest = content_hash(admission_ref)
        body = {
            "schema_version": "0.1",
            "id": "mission-document-research-candidate-rejection:" + digest[:32],
            "admission_ref": admission_ref,
            "outcome_ref": "mission-document-research-outcome:" + digest[:24],
            "rule_ref": "research-auto-commit:mission-document-qualitative:v1",
            "reason": "document qualitative rule admits no numeric statement",
            "candidate_claim_ref": "candidate-claim-version:" + digest,
            "candidate_claim_hash": digest,
            "research_status": "candidate_rejected",
            "created_at": "2026-09-17T04:02:03.184671+00:00",
        }
        wire = {**body, "content_hash": content_hash(body)}
        self.connection.execute(
            "INSERT INTO mission_document_research_candidate_rejections "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (wire["id"], wire["admission_ref"], wire["outcome_ref"],
             wire["rule_ref"], wire["reason"], wire["candidate_claim_ref"],
             wire["candidate_claim_hash"], canonical_json(wire),
             wire["content_hash"], wire["created_at"]),
        )
        self.connection.commit()
        return wire

    def started(self, admission_ref: str) -> None:
        self.connection.execute(
            "INSERT INTO mission_document_research_starts VALUES(?,?)",
            ("start:" + content_hash(admission_ref)[:24], admission_ref),
        )
        self.connection.commit()

    def completed(self, admission_ref: str) -> None:
        self.connection.execute(
            "INSERT INTO mission_document_research_outcomes VALUES(?,?)",
            ("mission-document-research-outcome:" + content_hash(admission_ref)[:24],
             admission_ref),
        )
        self.connection.commit()


class _Launcher:
    def __init__(self, tickets_dir: Path) -> None:
        self.tickets_dir = tickets_dir
        tickets_dir.mkdir(parents=True)
        self.tickets: dict[str, dict] = {}
        self.started: list[tuple[str, str]] = []
        self.resumed: list[dict] = []
        self.resume_error: Exception | None = None
        self.rebind_to: str | None = None
        self.claims: set[tuple[str, str]] = set()

    def start(self, *, admission_ref: str, admission_hash: str) -> dict:
        self.started.append((admission_ref, admission_hash))
        ticket = {
            "id": "mission-document-research:" + f"{len(self.started):024x}",
            "status": "running",
            "summary": None,
        }
        self.tickets[ticket["id"]] = ticket
        return ticket

    def status(self, ticket_ref: str) -> dict:
        return dict(self.tickets[ticket_ref])

    def resume(self, **kwargs) -> dict:
        self.resumed.append(dict(kwargs))
        if self.resume_error is not None:
            raise self.resume_error
        if self.rebind_to is None:
            return {"id": kwargs["prior_ticket_ref"], "status": "running"}
        prior = kwargs["prior_ticket_ref"]
        self.tickets[self.rebind_to] = {
            "id": self.rebind_to, "status": "running", "summary": None,
            "admission_ref": kwargs["admission_ref"],
            "admission_hash": kwargs["admission_hash"],
            "rebound_from_ticket_ref": prior,
        }
        return dict(self.tickets[self.rebind_to])

    def controlled_reentry_consumed(self, ticket_ref: str, authorization: str) -> bool:
        # "Consumed" is "a child actually ran under this claim", not "the
        # marker was taken"; the lane escalates only on the former.
        return (ticket_ref, authorization) in self.claims


def _write_latest(path: Path, admission: dict, ticket_ref: str) -> None:
    body = {
        "ticket_ref": ticket_ref,
        "admission_ref": admission["id"],
        "admission_hash": admission["content_hash"],
    }
    path.write_text(
        canonical_json({**body, "content_hash": content_hash(body)}) + "\n",
        encoding="utf-8",
    )


class MissionDocumentResearchLaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = _Store()
        self.addCleanup(self.store.connection.close)
        self.launcher = _Launcher(self.root / "tickets")
        self.lane = MissionDocumentResearchCoordinator(
            store=self.store, launcher=self.launcher,
        )

    def _model_authority_terminal_hold(self, admission: dict, *, error: str | None = None):
        from dalton_core.mission_document_research_lane import (
            MODEL_AUTHORITY_PREEXECUTION_ERROR,
        )

        ticket_ref = (
            "mission-document-research:" + content_hash(admission["id"])[:24]
        )
        body = {
            "schema_version": "0.1",
            "created_at": "2026-09-22T12:00:00.000000+00:00",
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "status": "failed",
            "outcomes": [],
            "error": error or MODEL_AUTHORITY_PREEXECUTION_ERROR,
        }
        summary = {**body, "content_hash": content_hash(body)}
        self.launcher.tickets[ticket_ref] = {
            "id": ticket_ref,
            "status": "failed",
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "summary": summary,
        }
        ticket_dir = self.launcher.tickets_dir / ticket_ref.split(":", 1)[1]
        ticket_dir.mkdir(parents=True, exist_ok=True)
        (ticket_dir / "summary.json").write_text(
            canonical_json(summary) + "\n", encoding="utf-8"
        )
        from dalton_core.mission_document_research_lane import _read_holds
        holds = (
            _read_holds(self.lane.holds_path)
            if self.lane.holds_path.is_file() else {}
        )
        self.lane._hold(
            holds, admission, reason="failed", ticket_ref=ticket_ref,
            disposition="terminal_hold",
        )
        return ticket_ref

    def test_never_started_model_authority_failure_reenters_through_audited_ticket(self):
        admission = self.store.add(1)
        ticket_ref = self._model_authority_terminal_hold(admission)

        result = self.lane.dispatch_once()

        self.assertEqual(result["status"], "resumed")
        self.assertEqual(len(self.launcher.resumed), 1)
        replay = self.launcher.resumed[0]
        self.assertEqual(replay["prior_ticket_ref"], ticket_ref)
        authorization = json.loads(replay["authorization"])
        self.assertEqual(
            authorization["reason"],
            "infrastructure_model_authority_revalidation",
        )
        self.assertIsNone(authorization["work_order_ref"])
        self.assertNotIn(
            admission["id"],
            json.loads(self.lane.holds_path.read_text(encoding="utf-8"))["holds"],
        )
        self.launcher.claims.add((ticket_ref, replay["authorization"]))
        self.assertEqual(self.lane.dispatch_once()["status"], "idle")
        self.assertEqual(len(self.launcher.resumed), 1)
        held = json.loads(
            self.lane.holds_path.read_text(encoding="utf-8")
        )["holds"][admission["id"]]
        self.assertEqual(held["disposition"], "terminal_hold")

    def test_model_authority_terminal_reentry_refuses_started_or_model_work(self):
        started = self.store.add(1)
        self._model_authority_terminal_hold(started)
        self.store.started(started["id"])
        self.assertIn(
            self.lane.dispatch_once()["status"], {"idle", "recovery_required"}
        )
        self.assertEqual(self.launcher.resumed, [])

        unstarted = self.store.add(2)
        self._model_authority_terminal_hold(unstarted)
        self._scheduler_work(unstarted, stage="qualitative_model_draft")
        self.assertIn(
            self.lane.dispatch_once()["status"], {"idle", "recovery_required"}
        )
        self.assertEqual(self.launcher.resumed, [])

    def test_model_authority_terminal_reentry_requires_exact_sealed_failure(self):
        admission = self.store.add(1)
        ticket_ref = self._model_authority_terminal_hold(
            admission, error="MissionDocumentResearchError: unrelated failure",
        )
        self.assertEqual(self.lane.dispatch_once()["status"], "idle")
        self.assertEqual(self.launcher.resumed, [])
        self.launcher.tickets[ticket_ref]["summary"]["error"] = (
            "MissionDocumentResearchError: mission document admission is no longer executable"
        )
        # The hash still covers the prior error, so the otherwise exact words
        # cannot turn a tampered summary into re-entry authority.
        self.assertEqual(self.lane.dispatch_once()["status"], "idle")
        self.assertEqual(self.launcher.resumed, [])

    def test_model_authority_terminal_reentry_starts_at_most_one_per_tick(self):
        first = self.store.add(1)
        second = self.store.add(2)
        self._model_authority_terminal_hold(first)
        self._model_authority_terminal_hold(second)

        result = self.lane.dispatch_once()

        self.assertEqual(result["status"], "resumed")
        self.assertEqual(len(self.launcher.resumed), 1)
        remaining = json.loads(
            self.lane.holds_path.read_text(encoding="utf-8")
        )["holds"]
        self.assertEqual(set(remaining), {second["id"]})

    def test_fresh_admission_launches_only_by_exact_ref_and_hash(self) -> None:
        admission = self.store.add(1)
        result = self.lane.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(
            self.launcher.started,
            [(admission["id"], admission["content_hash"])],
        )
        pointer = json.loads(self.lane.latest_path.read_text(encoding="utf-8"))
        asserted = pointer.pop("content_hash")
        self.assertEqual(asserted, content_hash(pointer))

    def test_failed_admission_is_held_while_next_admission_runs(self) -> None:
        first = self.store.add(1)
        second = self.store.add(2)
        ticket_ref = "mission-document-research:" + "a" * 24
        self.launcher.tickets[ticket_ref] = {
            "id": ticket_ref,
            "status": "failed",
            "summary": {"status": "failed"},
        }
        _write_latest(self.lane.latest_path, first, ticket_ref)

        result = self.lane.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["admission_ref"], second["id"])
        holds = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))
        self.assertEqual(holds["holds"][first["id"]]["reason"], "failed")
        self.assertEqual(len(self.launcher.started), 1)

    def test_timed_recovery_wait_does_not_block_an_independent_admission(self) -> None:
        first = self.store.add(1)
        second = self.store.add(2)
        ticket_ref = "mission-document-research:" + "e" * 24
        self.launcher.tickets[ticket_ref] = {
            "id": ticket_ref, "status": "failed",
            "summary": {"status": "incomplete"},
        }
        self.store.started(first["id"])
        _write_latest(self.lane.latest_path, first, ticket_ref)
        retry_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.lane._execution_state = lambda _admission: {
            "action": "waiting", "reason": "fresh_work_recovery_backoff",
            "retry_at": retry_at, "work_order_ref": "work:waiting",
        }

        result = self.lane.dispatch_once()

        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["admission_ref"], second["id"])
        holds = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))
        self.assertEqual(holds["holds"][first["id"]]["disposition"], "recovery_wait")
        self.assertEqual(holds["holds"][first["id"]]["retry_at"], retry_at)

    def test_due_recovery_wait_reenters_and_clears_its_hold(self) -> None:
        admission = self.store.add(1)
        ticket_ref = "mission-document-research:" + "f" * 24
        self.launcher.tickets[ticket_ref] = {
            "id": ticket_ref, "status": "failed",
            "summary": {"status": "incomplete"},
        }
        self.store.started(admission["id"])
        _write_latest(self.lane.latest_path, admission, ticket_ref)
        retry_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.lane._execution_state = lambda _admission: {
            "action": "waiting", "reason": "fresh_work_recovery_backoff",
            "retry_at": retry_at, "work_order_ref": "work:waiting",
        }
        self.assertEqual(self.lane.dispatch_once()["status"], "waiting")
        self.lane._execution_state = lambda _admission: {
            "action": "resume", "reason": "typed_recovery_due",
            "work_order_ref": "work:waiting",
        }

        result = self.lane.dispatch_once()

        self.assertEqual(result["status"], "resumed")
        holds = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))
        self.assertNotIn(admission["id"], holds["holds"])

    def test_rebound_ticket_identity_clears_its_hold_and_says_so(self) -> None:
        """A moved ticket name must not be a permanent hold for a person."""

        admission = self.store.add(1)
        self.store.started(admission["id"])
        ticket_ref = "mission-document-research:" + "c" * 24
        rebound_ref = "mission-document-research:" + "7" * 24
        self.launcher.tickets[ticket_ref] = {
            "id": ticket_ref, "status": "failed",
            "summary": {"status": "incomplete"},
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
        }
        _write_latest(self.lane.latest_path, admission, ticket_ref)
        self.lane._execution_state = lambda _admission: {
            "action": "resume", "reason": "typed_recovery_due",
            "work_order_ref": "work:resumable",
        }
        self.launcher.rebind_to = rebound_ref

        result = self.lane.dispatch_once()

        self.assertEqual(result["status"], "resumed")
        self.assertEqual(result["ticket_ref"], rebound_ref)
        self.assertEqual(result["rebound_from_ticket_ref"], ticket_ref)
        self.assertIn("已自动改绑", result["reason"])
        self.assertFalse(self.lane.holds_path.exists())
        pointer = json.loads(self.lane.latest_path.read_text(encoding="utf-8"))
        self.assertEqual(pointer["ticket_ref"], rebound_ref)

    def test_reentry_that_fails_after_a_rebinding_escalates_to_the_owner(self) -> None:
        from dalton_core.lane_child_launcher import LaneChildRejected
        from dalton_core.mission_document_research_lane import (
            REENTRY_ESCALATED_REASON, REENTRY_ESCALATION_NOTE,
        )

        admission = self.store.add(1)
        self.store.started(admission["id"])
        ticket_ref = "mission-document-research:" + "b" * 24
        self.launcher.tickets[ticket_ref] = {
            "id": ticket_ref, "status": "failed",
            "summary": {"status": "incomplete"},
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            # This ticket is itself the product of an automatic rebinding, so
            # the lane has already spent its one free attempt here.
            "rebound_from_ticket_ref": "mission-document-research:" + "a" * 24,
        }
        _write_latest(self.lane.latest_path, admission, ticket_ref)
        self.lane._execution_state = lambda _admission: {
            "action": "resume", "reason": "typed_recovery_due",
            "work_order_ref": "work:resumable",
        }
        self.launcher.resume_error = LaneChildRejected("ticket is unavailable")

        result = self.lane.dispatch_once()

        self.assertEqual(result["status"], "recovery_required")
        holds = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))
        self.assertTrue(holds["holds"][admission["id"]]["reason"].startswith(
            REENTRY_ESCALATED_REASON))
        self.assertEqual(result["waiting_on_owner"], 1)
        self.assertEqual(result["holds"][0]["owner_action"], REENTRY_ESCALATION_NOTE)
        self.assertIn("已经自动把它", REENTRY_ESCALATION_NOTE)

    def test_first_refused_reentry_stays_the_lanes_own_business(self) -> None:
        from dalton_core.lane_child_launcher import LaneChildRejected

        admission = self.store.add(1)
        self.store.started(admission["id"])
        ticket_ref = "mission-document-research:" + "8" * 24
        self.launcher.tickets[ticket_ref] = {
            "id": ticket_ref, "status": "failed",
            "summary": {"status": "incomplete"},
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
        }
        _write_latest(self.lane.latest_path, admission, ticket_ref)
        self.lane._execution_state = lambda _admission: {
            "action": "resume", "reason": "typed_recovery_due",
            "work_order_ref": "work:resumable",
        }
        self.launcher.resume_error = LaneChildRejected("ticket is unavailable")

        result = self.lane.dispatch_once()

        holds = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))
        self.assertTrue(holds["holds"][admission["id"]]["reason"].startswith(
            "controlled_reentry_unavailable:"))
        self.assertEqual(result["status"], "recovery_required")

    def test_a_claim_that_never_ran_a_child_is_not_an_attempt(self) -> None:
        """The live 2026-09-18 shape: marker taken, nothing ever re-entered."""

        from dalton_core.lane_child_launcher import LaneChildRejected
        from dalton_core.mission_document_research_lane import REENTRY_ESCALATED_REASON

        admission = self.store.add(1)
        self.store.started(admission["id"])
        ticket_ref = "mission-document-research:" + "2" * 24
        self.launcher.tickets[ticket_ref] = {
            "id": ticket_ref, "status": "failed",
            "summary": {"status": "incomplete"},
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
        }
        _write_latest(self.lane.latest_path, admission, ticket_ref)
        recovery = {
            "action": "resume", "reason": "typed_recovery_due",
            "work_order_ref": "work:resumable",
        }
        self.lane._execution_state = lambda _admission: dict(recovery)
        self.launcher.resume_error = LaneChildRejected(
            "controlled reentry was already attempted")

        # The marker exists but no child ever ran under it: the lane keeps this
        # to itself and tries again, rather than handing a person an admission
        # it never actually retried.
        result = self.lane.dispatch_once()

        self.assertEqual(result["status"], "recovery_required")
        holds = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))
        self.assertTrue(holds["holds"][admission["id"]]["reason"].startswith(
            "controlled_reentry_unavailable:"))

        # Once a child really ran under that claim it is an attempt, and the
        # next refusal is a person's problem.
        self.launcher.claims.add((
            ticket_ref,
            self.lane._reentry_authorization(admission, ticket_ref, recovery),
        ))
        self.lane.dispatch_once()
        holds = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))
        self.assertTrue(holds["holds"][admission["id"]]["reason"].startswith(
            REENTRY_ESCALATED_REASON))

    def test_required_hold_finds_exact_owned_ticket_when_work_becomes_resumable(self) -> None:
        admission = self.store.add(1)
        self.store.started(admission["id"])
        suffix = "9" * 24
        ticket_ref = "mission-document-research:" + suffix
        self.launcher.tickets[ticket_ref] = {
            "id": ticket_ref, "status": "succeeded", "summary": {"status": "blocked"},
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
        }
        ticket_dir = self.launcher.tickets_dir / suffix
        ticket_dir.mkdir()
        (ticket_dir / "ticket.json").write_text("{}\n", encoding="utf-8")
        body = {"schema_version": "0.1", "holds": {admission["id"]: {
            "admission_hash": admission["content_hash"], "ticket_ref": None,
            "reason": "started_without_owned_live_ticket",
            "disposition": "recovery_required", "retry_at": None,
        }}}
        self.lane.holds_path.write_text(
            canonical_json({**body, "content_hash": content_hash(body)}) + "\n"
        )
        self.lane._execution_state = lambda _admission: {
            "action": "resume", "reason": "outcome_commit_not_yet_finished",
            "work_order_ref": None,
        }

        result = self.lane.dispatch_once()

        self.assertEqual(result["status"], "resumed")
        self.assertEqual(self.launcher.resumed[0]["prior_ticket_ref"], ticket_ref)

    def test_legacy_daily_budget_required_hold_is_reclassified_to_wait(self) -> None:
        admission = self.store.add(1)
        self.store.started(admission["id"])
        ticket_ref = "mission-document-research:" + "d" * 24
        self.launcher.tickets[ticket_ref] = {
            "id": ticket_ref, "status": "failed",
            "summary": {"status": "incomplete"},
        }
        retry_at = "2026-09-12T00:00:00.000000+00:00"
        self.lane.holds_path.write_text(canonical_json({
            "schema_version": "0.1",
            "holds": {admission["id"]: {
                "admission_hash": admission["content_hash"],
                "ticket_ref": ticket_ref,
                "reason": "fresh_work_recovery_deadline_exceeded",
                "disposition": "recovery_required",
                "retry_at": None,
            }},
            "content_hash": content_hash({
                "schema_version": "0.1",
                "holds": {admission["id"]: {
                    "admission_hash": admission["content_hash"],
                    "ticket_ref": ticket_ref,
                    "reason": "fresh_work_recovery_deadline_exceeded",
                    "disposition": "recovery_required",
                    "retry_at": None,
                }},
            }),
        }) + "\n", encoding="utf-8")
        self.lane._execution_state = lambda _admission: {
            "action": "waiting", "reason": "fresh_work_recovery_backoff",
            "retry_at": retry_at, "work_order_ref": "work:daily-budget-refused",
        }

        result = self.lane.dispatch_once()

        self.assertEqual(result["status"], "waiting")
        hold = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))[
            "holds"
        ][admission["id"]]
        self.assertEqual(hold["disposition"], "recovery_wait")
        self.assertEqual(hold["retry_at"], retry_at)

    def test_started_without_owned_ticket_is_not_blindly_replayed(self) -> None:
        first = self.store.add(1)
        second = self.store.add(2)
        self.store.started(first["id"])

        result = self.lane.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["admission_ref"], second["id"])
        holds = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))
        self.assertEqual(
            holds["holds"][first["id"]]["reason"],
            "started_without_owned_live_ticket",
        )

    def test_terminal_observation_clears_stale_started_hold_only(self) -> None:
        complete = self.store.add(1)
        failed = self.store.add(2)
        staged = self.store.add(3)
        self.store.started(complete["id"])
        self.store.started(failed["id"])
        self.store.started(staged["id"])
        self.assertEqual(self.lane.dispatch_once()["status"], "recovery_required")

        observations = [
            {"admission_ref": complete["id"], "outcome": "no_verified_claim"},
            {"admission_ref": failed["id"], "outcome": "recovery_required"},
            {"admission_ref": staged["id"], "outcome": "candidate_staged"},
        ]
        self.lane._execution_state = lambda _admission: {
            "action": "recovery_required", "reason": "send_state_unproved",
            "work_order_ref": "work:failed",
        }
        with patch(
            "dalton_core.mission_document_research_executor."
            "read_mission_document_research_observations",
            return_value=observations,
        ):
            result = self.lane.dispatch_once()

        self.assertEqual(result["status"], "recovery_required")
        self.assertEqual(result["held"], 2)
        holds = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))["holds"]
        self.assertNotIn(complete["id"], holds)
        self.assertIn(failed["id"], holds)
        self.assertIn(staged["id"], holds)

    def test_feedback_settles_admission_even_when_recovery_rows_sort_after_it(self) -> None:
        """Live ca9bac39: complete at 06:29, still held as an escalation.

        Research feedback is stamped with the admission's own ``created_at``,
        so the recovery observations an admission collected on its way there
        always sort *after* it.  The latest-row rule therefore never saw the
        completion, and the hold outlived the finished run.
        """

        completed = self.store.add(1)
        self.store.started(completed["id"])
        self.assertEqual(self.lane.dispatch_once()["status"], "recovery_required")
        holds = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))["holds"]
        self.assertIn(completed["id"], holds)

        observations = [
            # Written by today's run, stamped 2026-09-11T20:30:27 (admission time).
            {"admission_ref": completed["id"], "outcome": "no_verified_claim"},
            # Written on 2026-09-11 by the failures before it, stamped 20:35:12.
            {"admission_ref": completed["id"], "outcome": "recovery_required"},
            {"admission_ref": completed["id"], "outcome": "recovery_required"},
        ]
        with patch(
            "dalton_core.mission_document_research_executor."
            "read_mission_document_research_observations",
            return_value=observations,
        ):
            self.lane.dispatch_once()

        holds = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))["holds"]
        self.assertNotIn(completed["id"], holds)

    def test_staged_outcome_is_pending_only_when_policy_requires_promotion(self) -> None:
        admission = self.store.add(1)
        self.store.completed(admission["id"])
        self.assertEqual(self.lane._admissions(), [])

        self.store.auto_commit = True
        with self.assertRaisesRegex(
            MissionDocumentResearchLaneError, "promotion authority is unavailable"
        ):
            self.lane._admissions()
        self.store.connection.execute(
            "CREATE TABLE mission_document_research_promotions("
            "promotion_id TEXT PRIMARY KEY,admission_ref TEXT NOT NULL UNIQUE)"
        )
        self.assertEqual(
            [item["id"] for item in self.lane._admissions()], [admission["id"]]
        )
        self.store.connection.execute(
            "INSERT INTO mission_document_research_promotions VALUES(?,?)",
            ("mission-document-research-promotion:" + content_hash(admission["id"])[:24],
             admission["id"]),
        )
        self.store.connection.commit()
        self.assertEqual(self.lane._admissions(), [])

    def test_refused_candidate_settles_its_admission_and_frees_the_lane(self) -> None:
        """A refusal is not a failure to retry and not a question for a person.

        Live, the same admission was re-dispatched, refused again, held, and
        finally listed for the owner -- for a decision the governance rule had
        already made.  Once the refusal is written down the admission is
        settled: its hold goes, it is never dispatched again, and the next
        admission runs.
        """

        self.store.auto_commit = True
        self.store.connection.execute(
            "CREATE TABLE mission_document_research_promotions("
            "promotion_id TEXT PRIMARY KEY,admission_ref TEXT NOT NULL UNIQUE)"
        )
        refused = self.store.add(1)
        other = self.store.add(2)
        self.store.completed(refused["id"])
        ticket_ref = "mission-document-research:" + "d" * 24
        self.launcher.tickets[ticket_ref] = {
            "id": ticket_ref, "status": "failed",
            "summary": {
                "status": "failed", "outcomes": [],
                "error": ("ResearchAutoCommitRejected: document qualitative rule "
                          "admits no numeric statement"),
            },
        }
        _write_latest(self.lane.latest_path, refused, ticket_ref)

        # Before the refusal is recorded: held for a person, the next
        # admission launched in its place.
        first = self.lane.dispatch_once()
        self.assertEqual(first["status"], "launched")
        self.assertEqual(first["admission_ref"], other["id"])
        holds = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))["holds"]
        self.assertIn(refused["id"], holds)

        # The re-run completes and writes the refusal down.
        self.launcher.tickets[ticket_ref]["summary"] = {
            "status": "complete",
            "outcomes": [{"status": "complete",
                          "research_status": "candidate_rejected"}],
        }
        self.store.refused(refused["id"])

        self.assertEqual([item["id"] for item in self.lane._admissions()],
                         [other["id"]])
        self.lane.dispatch_once()
        holds = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))["holds"]
        self.assertNotIn(refused["id"], holds)
        self.assertEqual(len(self.launcher.started), 1)

    def test_tampered_latest_and_hold_authority_fail_closed(self) -> None:
        admission = self.store.add(1)
        ticket_ref = "mission-document-research:" + "b" * 24
        _write_latest(self.lane.latest_path, admission, ticket_ref)
        latest = json.loads(self.lane.latest_path.read_text(encoding="utf-8"))
        latest["admission_hash"] = "0" * 64
        self.lane.latest_path.write_text(canonical_json(latest) + "\n")
        with self.assertRaisesRegex(MissionDocumentResearchLaneError, "pointer drifted"):
            self.lane.dispatch_once()

        self.lane.latest_path.unlink()
        body = {
            "schema_version": "0.1",
            "holds": {
                admission["id"]: {
                    "admission_hash": admission["content_hash"],
                    "ticket_ref": None,
                    "reason": "orphaned",
                    "disposition": "terminal_hold",
                    "retry_at": None,
                    "extra": True,
                }
            },
        }
        self.lane.holds_path.write_text(
            canonical_json({**body, "content_hash": content_hash(body)}) + "\n"
        )
        with self.assertRaisesRegex(MissionDocumentResearchLaneError, "invalid entry"):
            self.lane.dispatch_once()

    def test_hold_for_same_ref_cannot_hide_changed_admission_hash(self) -> None:
        admission = self.store.add(1)
        body = {
            "schema_version": "0.1",
            "holds": {
                admission["id"]: {
                    "admission_hash": "0" * 64,
                    "ticket_ref": None,
                    "reason": "orphaned",
                    "disposition": "terminal_hold",
                    "retry_at": None,
                }
            },
        }
        self.lane.holds_path.write_text(
            canonical_json({**body, "content_hash": content_hash(body)}) + "\n"
        )
        with self.assertRaisesRegex(MissionDocumentResearchLaneError, "hash drifted"):
            self.lane.dispatch_once()

    def _scheduler_work(
        self, admission: dict, *, stage: str = "registered_source_retrieval",
        work_ref: str | None = None,
    ) -> tuple[Scheduler, dict]:
        scheduler = Scheduler(
            connection=self.store.connection, max_attempts=2,
            default_lease_seconds=30, max_lease_seconds=60,
            max_total_lease_seconds=120,
        )
        work_ref = work_ref or "work:mission-document-research-" + content_hash({
            "admission_identity_hash": admission["identity_hash"], "ordinal": 1,
        })[:32]
        work = {
            "schema_version": "0.1", "id": work_ref,
            "created_at": admission["created_at"],
            "updated_at": admission["created_at"],
            "question": "retrieve exact registered document source",
            "requested_capabilities": ["registered_source_retrieval"],
            "runtime_profile_ref": "runtime:registered-source-local",
            "budget": {"max_seconds": 60},
            "idempotency_key": "enqueue:" + work_ref,
            "declared_side_effects": [], "status": "ready",
            "input_refs": [admission["id"]],
            "metadata": {
                "mission_document_research_admission_ref": admission["id"],
                "mission_document_research_admission_hash": admission["content_hash"],
                "stage": stage,
            },
        }
        scheduler.enqueue(work)
        return scheduler, work

    def test_failed_child_with_ready_exact_work_uses_controlled_reentry(self) -> None:
        admission = self.store.add(1)
        self.store.started(admission["id"])
        self._scheduler_work(admission)
        ticket_ref = "mission-document-research:" + "c" * 24
        self.launcher.tickets[ticket_ref] = {
            "id": ticket_ref, "status": "failed",
            "summary": {"status": "incomplete"},
        }
        _write_latest(self.lane.latest_path, admission, ticket_ref)

        result = self.lane.dispatch_once()
        self.assertEqual(result["status"], "resumed")
        self.assertEqual(len(self.launcher.resumed), 1)
        authorization = json.loads(self.launcher.resumed[0]["authorization"])
        self.assertEqual(authorization["kind"], "exact_scheduler_replay")
        self.assertEqual(authorization["work_order_ref"], result["last"]["recovery"]["work_order_ref"])

    def test_unclassified_terminal_gets_one_executor_classification_reentry(self) -> None:
        admission = self.store.add(1)
        self.store.started(admission["id"])
        scheduler, work = self._scheduler_work(admission)
        claim = scheduler.claim("worker:test", work_order_id=work["id"])
        assert claim is not None
        result = {
            "schema_version": "0.1",
            "id": "result:capacity:" + "d" * 24,
            "created_at": "2026-09-11T12:00:03.000000+00:00",
            "work_order_ref": work["id"],
            "invocation_ref": "invocation:not-started:" + "d" * 24,
            "status": "failed", "outputs": {},
            "actual_side_effects": [], "usage_refs": [], "artifact_refs": [],
            "error": {"code": "BUSY"},
            "metadata": {"control_plane_failure": True},
        }
        scheduler.complete(
            work["id"], 1, "worker:test", claim["lease_token"], result,
            idempotency_key="capacity-terminal",
        )
        ticket_ref = "mission-document-research:" + "d" * 24
        self.launcher.tickets[ticket_ref] = {
            "id": ticket_ref, "status": "succeeded",
            "summary": {"status": "blocked"},
        }
        _write_latest(self.lane.latest_path, admission, ticket_ref)

        dispatched = self.lane.dispatch_once()
        self.assertEqual(dispatched["status"], "resumed")
        authorization = json.loads(self.launcher.resumed[0]["authorization"])
        self.assertEqual(
            authorization["reason"], "recovery_classification_not_yet_recorded"
        )

    # ------------------------------------------------------------------
    # Recovery hints: one unreadable chain is one admission's problem.
    # ------------------------------------------------------------------

    def _recovery_link(
        self, admission: dict, *, stage: int, failed_ref: str,
        failed_hash: str | None = None, number: int = 1,
    ) -> dict:
        self.store.connection.execute(
            "CREATE TABLE IF NOT EXISTS mission_document_research_recovery_links("
            "recovery_link_id TEXT PRIMARY KEY,admission_ref TEXT NOT NULL,"
            "stage_ordinal INTEGER NOT NULL,recovery_number INTEGER NOT NULL,"
            "failed_work_order_ref TEXT NOT NULL,"
            "recovery_work_order_ref TEXT NOT NULL,record_json TEXT NOT NULL,"
            "content_hash TEXT NOT NULL,created_at TEXT NOT NULL)"
        )
        digest = content_hash({
            "admission": admission["id"], "stage": stage, "number": number,
        })
        body = {
            "schema_version": "0.1",
            "id": "mission-document-research-recovery-link:" + digest[:32],
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "stage_ordinal": stage,
            "recovery_number": number,
            "failed_work_order_ref": failed_ref,
            "failed_work_order_hash": failed_hash or content_hash(failed_ref),
            "recovery_work_order_ref": "work:mission-document-recovery-" + digest[:32],
            "created_at": "2026-09-22T15:19:34.675988+00:00",
        }
        wire = {**body, "content_hash": content_hash(body)}
        self.store.connection.execute(
            "INSERT INTO mission_document_research_recovery_links "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (wire["id"], wire["admission_ref"], wire["stage_ordinal"],
             wire["recovery_number"], wire["failed_work_order_ref"],
             wire["recovery_work_order_ref"], canonical_json(wire),
             wire["content_hash"], wire["created_at"]),
        )
        self.store.connection.commit()
        return wire

    def _refreshed_stage_work(self, admission: dict, *, stage: int) -> tuple[str, str]:
        """A stage Work whose identity this lane cannot re-derive.

        The executor mixes a model-authority refresh hash into stages 2..4, so
        the live identity is not ``content_hash({identity_hash, ordinal})``.
        """

        work_ref = "work:mission-document-research-" + content_hash({
            "admission_identity_hash": admission["identity_hash"],
            "ordinal": stage,
            "model_authority_refresh_hash": content_hash({"verifier": "refreshed"}),
        })[:32]
        stage_name = {
            2: "qualitative_model_draft", 3: "independent_qualitative_verifier",
        }[stage]
        self._scheduler_work(admission, stage=stage_name, work_ref=work_ref)
        work_hash = self.store.connection.execute(
            "SELECT work_order_hash FROM scheduler_work_orders WHERE work_order_id=?",
            (work_ref,),
        ).fetchone()["work_order_hash"]
        return work_ref, work_hash

    def test_recovery_chain_rooted_at_refreshed_stage_work_is_read(self) -> None:
        """The live 2026-09-23 outage: a chain root the lane cannot re-derive.

        The recovery link is authentic -- sealed, bound to this admission, and
        rooted at this admission's own verifier Work -- so it must be read as
        a hint rather than taking the lane down.
        """

        admission = self.store.add(1)
        work_ref, work_hash = self._refreshed_stage_work(admission, stage=3)
        link = self._recovery_link(
            admission, stage=3, failed_ref=work_ref, failed_hash=work_hash,
        )

        hints = self.lane._effective_work_hints(admission)

        self.assertEqual(hints[2], link["recovery_work_order_ref"])
        self.assertNotIn(work_ref, hints)

    def test_recovery_chain_root_must_be_this_admission_and_stage(self) -> None:
        from dalton_core.mission_document_research_lane import (
            MissionDocumentResearchHintDrift,
        )

        admission = self.store.add(1)
        other = self.store.add(2)
        # Sealed, but the named Work belongs to another admission.
        foreign_ref, foreign_hash = self._refreshed_stage_work(other, stage=3)
        self._recovery_link(
            admission, stage=3, failed_ref=foreign_ref, failed_hash=foreign_hash,
        )
        with self.assertRaises(MissionDocumentResearchHintDrift):
            self.lane._effective_work_hints(admission)

        self.store.connection.execute(
            "DELETE FROM mission_document_research_recovery_links"
        )
        # Sealed and this admission's, but it is the draft stage, not stage 3.
        wrong_stage_ref, wrong_stage_hash = self._refreshed_stage_work(
            admission, stage=2
        )
        self._recovery_link(
            admission, stage=3, failed_ref=wrong_stage_ref,
            failed_hash=wrong_stage_hash,
        )
        with self.assertRaises(MissionDocumentResearchHintDrift):
            self.lane._effective_work_hints(admission)

        self.store.connection.execute(
            "DELETE FROM mission_document_research_recovery_links"
        )
        # Named Work exists and binds, but the link asserts a different hash.
        self._recovery_link(
            admission, stage=3, failed_ref=wrong_stage_ref,
            failed_hash=content_hash("not the sealed work"),
        )
        with self.assertRaises(MissionDocumentResearchHintDrift):
            self.lane._effective_work_hints(admission)

    def _epoch_rebind(self, admission: dict, link: dict, *, stage: int) -> tuple:
        """Record what the executor writes when a policy roll renames a recovery.

        The authorized recovery Work is enqueued and left ``ready`` for good;
        the rebound Work next to it is the one that actually runs.
        """

        from dalton_core.mission_document_research_executor import (
            SCHEMA_VERSION, _epoch_rebound_work,
        )

        stage_name = {
            2: "qualitative_model_draft", 3: "independent_qualitative_verifier",
        }[stage]
        scheduler, authorized = self._scheduler_work(
            admission, stage=stage_name,
            work_ref=link["recovery_work_order_ref"],
        )
        _, current_base = self._refreshed_stage_work_order(
            admission, stage=stage, tag="09-25")
        self.store.connection.execute(
            "CREATE TABLE IF NOT EXISTS "
            "mission_document_research_model_authority_epoch_rebinds("
            "rebind_id TEXT PRIMARY KEY,admission_ref TEXT NOT NULL,"
            "stage_ordinal INTEGER NOT NULL,recovery_link_ref TEXT NOT NULL UNIQUE,"
            "authorized_recovery_work_ref TEXT NOT NULL UNIQUE,"
            "current_base_work_ref TEXT NOT NULL,"
            "rebound_work_order_ref TEXT NOT NULL UNIQUE,record_json TEXT NOT NULL,"
            "content_hash TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL)"
        )
        rebind_ref = "mission-document-model-authority-epoch-rebind:" + content_hash(
            link["id"])[:32]
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": rebind_ref,
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "stage_ordinal": stage,
            "recovery_link_ref": link["id"],
            "recovery_link_hash": link["content_hash"],
            "recovery_number": link["recovery_number"],
            "authorized_recovery_work_ref": authorized["id"],
            "authorized_recovery_work_hash": content_hash(authorized),
            "current_base_work_ref": current_base["id"],
            "current_base_work_hash": content_hash(current_base),
            "current_base_work_order": current_base,
            "rebound_work_order_ref": "work:mission-document-authority-rebind-"
            + content_hash(rebind_ref)[:32],
            "created_at": link["created_at"],
        }
        record["content_hash"] = content_hash(record)
        self.store.connection.execute(
            "INSERT INTO mission_document_research_model_authority_epoch_rebinds "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (record["id"], admission["id"], stage, link["id"], authorized["id"],
             current_base["id"], record["rebound_work_order_ref"],
             canonical_json(record), record["content_hash"],
             record["created_at"]),
        )
        self.store.connection.commit()
        rebound = _epoch_rebound_work(current_base, record)
        scheduler.enqueue(rebound)
        return scheduler, authorized, rebound, record

    def _refreshed_stage_work_order(
        self, admission: dict, *, stage: int, tag: str,
    ) -> tuple[str, dict]:
        stage_name = {
            2: "qualitative_model_draft", 3: "independent_qualitative_verifier",
        }[stage]
        work_ref = "work:mission-document-research-" + content_hash({
            "admission_identity_hash": admission["identity_hash"],
            "ordinal": stage, "epoch": tag,
        })[:32]
        return work_ref, {
            "schema_version": "0.1", "id": work_ref,
            "created_at": admission["created_at"],
            "updated_at": admission["created_at"],
            "question": "draft the qualitative claim",
            "requested_capabilities": ["registered_source_retrieval"],
            "runtime_profile_ref": "runtime:registered-source-local",
            "budget": {"max_seconds": 60},
            "idempotency_key": "enqueue:" + work_ref,
            "declared_side_effects": [], "status": "ready",
            "input_refs": [admission["id"]],
            "metadata": {
                "mission_document_research_admission_ref": admission["id"],
                "mission_document_research_admission_hash": admission["content_hash"],
                "stage": stage_name,
            },
        }

    def test_recovery_renamed_by_authority_roll_is_followed_to_rebound_work(
        self,
    ) -> None:
        """Live legacy 6a2bcd/a9e588b0/918307dc on 2026-09-25.

        Each had a recovery the automatic door authorized under one model
        authority; the roll made the executor run a rebound Work instead, which
        failed again.  The lane kept watching the never-claimed authorized
        Work, called the admission resumable, and the refused re-entry was
        filed as ``reentry_failed_after_automatic_rebind`` -- the one door the
        owner's CLI then refused for the paid and unproved doors.
        """

        admission = self.store.add(1)
        self.store.started(admission["id"])
        failed_ref = "work:mission-document-research-" + content_hash({
            "admission_identity_hash": admission["identity_hash"], "ordinal": 2,
        })[:32]
        link = self._recovery_link(admission, stage=2, failed_ref=failed_ref)
        scheduler, authorized, rebound, _record = self._epoch_rebind(
            admission, link, stage=2)

        hints = self.lane._effective_work_hints(admission)
        self.assertEqual(hints[1], rebound["id"])
        self.assertNotIn(authorized["id"], hints)

        # Retrieval succeeded; the rebound Work failed; the authorized one is
        # still ``ready``.
        _, retrieval = self._scheduler_work(admission)
        claim = scheduler.claim("worker:test", work_order_id=retrieval["id"])
        assert claim is not None
        scheduler.complete(
            retrieval["id"], 1, "worker:test", claim["lease_token"], {
                "schema_version": "0.1",
                "id": "result:retrieval:" + "b" * 24,
                "created_at": "2026-09-11T12:00:03.000000+00:00",
                "work_order_ref": retrieval["id"],
                "invocation_ref": "invocation:retrieval:" + "b" * 24,
                "status": "succeeded", "outputs": {},
                "actual_side_effects": [], "usage_refs": [], "artifact_refs": [],
                "metadata": {},
            }, idempotency_key="retrieval-terminal",
        )
        claim = scheduler.claim("worker:test", work_order_id=rebound["id"])
        assert claim is not None
        scheduler.complete(
            rebound["id"], 1, "worker:test", claim["lease_token"], {
                "schema_version": "0.1",
                "id": "result:rebound:" + "a" * 24,
                "created_at": "2026-09-25T07:04:34.631146+00:00",
                "work_order_ref": rebound["id"],
                "invocation_ref": "invocation:rebound:" + "a" * 24,
                "status": "failed", "outputs": {},
                "actual_side_effects": [], "usage_refs": [], "artifact_refs": [],
                "error": {"code": "OUTPUT_CONTRACT"}, "metadata": {},
            }, idempotency_key="rebound-terminal",
        )
        state = self.lane._execution_state(admission)
        self.assertEqual(state["work_order_ref"], rebound["id"])
        self.assertNotEqual(state["reason"], "scheduler_ready")

    def test_rebind_hint_must_match_its_recovery_link(self) -> None:
        from dalton_core.mission_document_research_lane import (
            MissionDocumentResearchHintDrift,
        )

        admission = self.store.add(1)
        failed_ref = "work:mission-document-research-" + content_hash({
            "admission_identity_hash": admission["identity_hash"], "ordinal": 2,
        })[:32]
        link = self._recovery_link(admission, stage=2, failed_ref=failed_ref)
        _scheduler, _authorized, rebound, record = self._epoch_rebind(
            admission, link, stage=2)
        # A record whose body no longer hashes to what the rebound Work names.
        forged = {**record, "recovery_number": 7}
        self.store.connection.execute(
            "UPDATE mission_document_research_model_authority_epoch_rebinds "
            "SET record_json=? WHERE rebind_id=?",
            (canonical_json(forged), record["id"]),
        )
        with self.assertRaises(MissionDocumentResearchHintDrift):
            self.lane._effective_work_hints(admission)

    def test_unreadable_recovery_chain_holds_only_its_own_admission(self) -> None:
        """One bad record must not be a lane-wide outage.

        Live on 2026-09-23 this raised out of ``dispatch_once`` for 143 of 143
        ticks in two hours, so no admission in the lane could be dispatched.
        """

        from dalton_core.mission_document_research_lane import (
            HINT_DRIFT_ESCALATION_NOTE, HINT_DRIFT_HOLD_REASON, _read_holds,
        )

        drifted = self.store.add(1)
        healthy = self.store.add(2)
        self.store.started(drifted["id"])
        # Neither re-derivable nor a persisted Work of this admission.
        self._recovery_link(
            drifted, stage=3,
            failed_ref="work:mission-document-research-" + "0" * 32,
        )
        ticket_ref = "mission-document-research:" + "e" * 24
        self.launcher.tickets[ticket_ref] = {
            "id": ticket_ref, "status": "failed",
            "summary": {"status": "incomplete"},
        }
        _write_latest(self.lane.latest_path, drifted, ticket_ref)

        result = self.lane.dispatch_once()

        # The other admission was still dispatched on this very tick.
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["admission_ref"], healthy["id"])
        self.assertEqual(
            self.launcher.started, [(healthy["id"], healthy["content_hash"])]
        )
        held = _read_holds(self.lane.holds_path)
        self.assertEqual(set(held), {drifted["id"]})
        self.assertEqual(held[drifted["id"]]["reason"], HINT_DRIFT_HOLD_REASON)
        self.assertEqual(held[drifted["id"]]["disposition"], "recovery_required")
        self.assertEqual(result["last"]["recovery"]["action"], "recovery_required")
        self.assertIn("recovery_link_ref", result["last"]["recovery"])
        # And the owner is told what the errand is, not just given a reason.
        detail = self.lane._hold_detail(held, [drifted, healthy])
        self.assertEqual(detail[0]["owner_action"], HINT_DRIFT_ESCALATION_NOTE)
        # A later tick keeps the hold and still never raises.
        self.assertEqual(self.lane.dispatch_once()["status"], "busy")

    def test_lane_is_opt_in_and_registered_once(self) -> None:
        self.assertIs(
            lane_for_operation("dispatch_mission_document_research"), LANE
        )
        self.assertEqual(LANE.driver_key, "mission_document_research")
        orders = [item.order for item in registered_lanes()]
        self.assertEqual(len(orders), len(set(orders)))
        context = type("Context", (), {"state": self.root})()
        self.assertEqual(argv_fragment(context), [])
        config = self.root / LANE_CONFIG
        config.write_text(
            canonical_json({"schema_version": "0.1", "enabled": True}) + "\n"
        )
        self.assertEqual(
            lane_configuration(config), {"schema_version": "0.1", "enabled": True}
        )
        self.assertEqual(
            argv_fragment(context), ["--mission-document-research-lane", str(config)]
        )

        configured = {
            "schema_version": "0.2", "enabled": True,
            "directed_admission": {
                "max_admissions_per_tick": 4,
                "task_budget": {"max_rounds": 8, "max_seconds": 1200},
            },
        }
        config.write_text(canonical_json(configured) + "\n")
        self.assertEqual(lane_configuration(config), configured)


    # -- 2026-09-24: one admission must not stop the lane -------------------

    def _started_hold(self, admission: dict, ticket_ref: str | None = None) -> None:
        from dalton_core.mission_document_research_lane import _read_holds

        self.store.started(admission["id"])
        holds = (
            _read_holds(self.lane.holds_path)
            if self.lane.holds_path.is_file() else {}
        )
        self.lane._hold(
            holds, admission, reason="started_without_owned_live_ticket",
            ticket_ref=ticket_ref, disposition="recovery_required",
        )

    def _owned_ticket(
        self, admission: dict, suffix: str, status: str, started_at: str,
    ) -> str:
        ticket_ref = "mission-document-research:" + suffix
        self.launcher.tickets[ticket_ref] = {
            "id": ticket_ref, "status": status,
            "summary": {"status": "complete" if status == "succeeded" else "failed"},
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "started_at": started_at,
            "completed_at": started_at,
        }
        ticket_dir = self.launcher.tickets_dir / suffix
        ticket_dir.mkdir()
        (ticket_dir / "ticket.json").write_text("{}\n", encoding="utf-8")
        return ticket_ref

    def _resumable(self) -> None:
        self.lane._execution_state = lambda _admission: {
            "action": "resume", "reason": "outcome_commit_not_yet_finished",
            "work_order_ref": None,
        }

    def test_several_owned_terminal_tickets_pick_newest_success_deterministically(self):
        # The live shape of ...ca9bac39 on 2026-09-24: four owned terminal
        # tickets, two succeeded and two failed, and a hold with no ticket.
        admission = self.store.add(1)
        self._started_hold(admission)
        self._owned_ticket(admission, "1" * 24, "succeeded",
                           "2026-09-12T00:14:27.425555+00:00")
        newest_success = self._owned_ticket(
            admission, "2" * 24, "succeeded", "2026-09-22T15:34:28.917873+00:00")
        self._owned_ticket(admission, "3" * 24, "failed",
                           "2026-09-18T13:25:46.522898+00:00")
        self._owned_ticket(admission, "4" * 24, "failed",
                           "2026-09-23T10:07:48.051431+00:00")
        # A ticket of another admission hash is never a candidate.
        other = dict(admission, content_hash="f" * 64)
        self._owned_ticket(other, "5" * 24, "succeeded",
                           "2026-09-23T23:00:00.000000+00:00")
        self._resumable()

        self.assertEqual(self.lane._owned_terminal_ticket_ref(admission),
                         newest_success)
        result = self.lane.dispatch_once()

        self.assertEqual(result["status"], "resumed")
        self.assertEqual(self.launcher.resumed[0]["prior_ticket_ref"],
                         newest_success)

    def test_owned_terminal_tickets_without_success_pick_the_newest(self):
        admission = self.store.add(1)
        self._owned_ticket(admission, "1" * 24, "failed",
                           "2026-09-12T00:00:00.000000+00:00")
        newest = self._owned_ticket(admission, "2" * 24, "orphaned",
                                    "2026-09-20T00:00:00.000000+00:00")
        self._owned_ticket(admission, "3" * 24, "failed",
                           "2026-09-18T00:00:00.000000+00:00")
        for _ in range(3):
            self.assertEqual(self.lane._owned_terminal_ticket_ref(admission), newest)

    def test_one_admissions_error_holds_it_and_the_tick_goes_on(self):
        broken = self.store.add(1)
        healthy = self.store.add(2)
        self._started_hold(broken)
        self._started_hold(healthy)
        healthy_ticket = self._owned_ticket(
            healthy, "2" * 24, "succeeded", "2026-09-22T00:00:00.000000+00:00")

        def state(admission):
            if admission["id"] == broken["id"]:
                raise MissionDocumentResearchLaneError("unreadable recovery chain")
            return {"action": "resume", "reason": "outcome_commit_not_yet_finished",
                    "work_order_ref": None}

        self.lane._execution_state = state
        result = self.lane.dispatch_once()

        self.assertEqual(result["status"], "resumed")
        self.assertEqual(result["admission_ref"], healthy["id"])
        self.assertEqual(self.launcher.resumed[0]["prior_ticket_ref"], healthy_ticket)
        self.assertEqual(result["admission_errors"][0]["admission_ref"], broken["id"])
        held = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))["holds"]
        # The broken admission keeps the hold it had: its reason is what an
        # owner door keys on.
        self.assertEqual(held[broken["id"]]["reason"],
                         "started_without_owned_live_ticket")
        self.assertNotIn(healthy["id"], held)

    def test_an_unexpected_error_on_a_fresh_admission_holds_only_it(self):
        first = self.store.add(1)
        second = self.store.add(2)
        original = self.launcher.start

        def start(*, admission_ref, admission_hash):
            if admission_ref == first["id"]:
                raise OSError("disk full")
            return original(admission_ref=admission_ref, admission_hash=admission_hash)

        self.launcher.start = start
        result = self.lane.dispatch_once()

        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["admission_ref"], second["id"])
        held = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))["holds"]
        self.assertTrue(held[first["id"]]["reason"].startswith("dispatch_error:OSError"))
        self.assertEqual(held[first["id"]]["disposition"], "recovery_required")

    def _finish_latest(self) -> None:
        pointer = json.loads(self.lane.latest_path.read_text(encoding="utf-8"))
        ticket = self.launcher.tickets.setdefault(pointer["ticket_ref"], {
            "id": pointer["ticket_ref"],
        })
        ticket.update({"status": "succeeded", "summary": {"status": "complete"}})

    def test_recoveries_and_fresh_admissions_take_turns_for_the_one_slot(self):
        first = self.store.add(1)
        second = self.store.add(2)
        fresh = self.store.add(3)
        self._started_hold(first)
        self._started_hold(second)
        self._owned_ticket(first, "1" * 24, "failed", "2026-09-20T00:00:00+00:00")
        self._owned_ticket(second, "2" * 24, "failed", "2026-09-20T00:00:00+00:00")
        self._resumable()

        one = self.lane.dispatch_once()
        self.assertEqual((one["status"], one["admission_ref"]),
                         ("resumed", first["id"]))
        self.assertEqual(one["recoveries_queued"], 1)
        self._finish_latest()
        self.store.completed(first["id"])
        two = self.lane.dispatch_once()
        self.assertEqual((two["status"], two["admission_ref"]),
                         ("launched", fresh["id"]))
        self._finish_latest()
        self.store.completed(fresh["id"])
        three = self.lane.dispatch_once()
        self.assertEqual((three["status"], three["admission_ref"]),
                         ("resumed", second["id"]))

    def test_recovery_sweep_is_capped_per_tick_and_resumes_where_it_stopped(self):
        admissions = [self.store.add(ordinal) for ordinal in range(1, 6)]
        for admission in admissions:
            self._started_hold(admission)
        seen: list[str] = []

        def state(admission):
            seen.append(admission["id"])
            return {"action": "recovery_required", "reason": "send_state_unproved",
                    "work_order_ref": None}

        self.lane._execution_state = state
        with patch("dalton_core.mission_document_research_lane."
                   "MAX_RECOVERY_EVALUATIONS_PER_TICK", 2):
            self.lane.dispatch_once()
            self.lane.dispatch_once()
            self.lane.dispatch_once()
        refs = [admission["id"] for admission in admissions]
        self.assertEqual(seen, refs[0:2] + refs[2:4] + [refs[4], refs[0]])


if __name__ == "__main__":
    unittest.main()
