"""2026-10-09: one more round of fresh-work recovery, and who may open it.

Live legacy, EPAM 09b4bb82 / 3c49a49c.  The draft stage had spent its bound of
two fresh Works on 10-06 (the lane's automatic unproved-send retry) and 10-07
(the owner's door, whose rebound Work's route was rejected: every model of
the chain was unavailable).  Batch ag let the executor classify that failure
at last, and on the owner's 10-09 re-entry it stopped the stage at once as
``fresh_work_recovery_exhausted`` -- no Work ran after the press.  The bound
counted attempts from before it.

Here: the executor honours a recorded round (exactly one fresh Work, then the
cap again), the lane opens a round by itself once a model of the same routing
policy has been served since the failure (once per stage per UTC day), and a
cap that is a policy bound is the owner's door.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from tests import test_mission_document_research as base
from tests.test_epoch_rebind_superseded import GOOD, _Helpers


class ExecutorRoundTests(_Helpers):
    def _exhausted(self):
        """The live shape, with the bound already spent before the re-entry."""

        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=1)
        admission = authority.admit_from_plan(**args)
        adapter = base.ContractRejectOnceAdapter(
            GOOD, {"schema_version": "0.1", "status": "answered"})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        self._run_until(executor, admission, lambda item: item.get("status") == "failed")
        execution = copy.deepcopy(admission["model_execution"])
        v2 = copy.deepcopy(admission["model_authority"])
        v2["draft"]["routing_policy_hash"] = "e" * 64
        authority.model_execution_resolver = lambda: (execution, v2)
        admitted = executor.run_once(admission["id"])
        self.assertEqual(admitted["reason"], "automatic_bounded_contract_retry")
        router = executor.draft_worker.router
        original = router.route
        router.route = lambda work, **kw: original(
            work, **{**kw, "credential_slot_refs": ("credential-slot:nobody",)})
        self._run_until(executor, admission, lambda item: item.get("status") == "failed")
        del router.route
        v3 = copy.deepcopy(v2)
        v3["draft"]["routing_policy_hash"] = "f" * 64
        authority.model_execution_resolver = lambda: (execution, v3)
        stopped = executor.run_once(admission["id"])
        self.assertEqual(stopped["reason"], "fresh_work_recovery_exhausted")
        return fixture, admission, executor, adapter, stopped

    def test_exhausted_stop_is_stable_and_a_round_opens_exactly_one_fresh_work(self):
        from dalton_core.mission_document_research_executor import (
            append_recovery_round, read_recovery_round)

        fixture, admission, executor, adapter, stopped = self._exhausted()
        connection = fixture.store.connection
        # Reading the same stop again converges (no clock-taken deadline drift).
        fixture.harness.clock.value += timedelta(hours=3)
        again = executor.run_once(admission["id"])
        self.assertEqual(again["reason"], "fresh_work_recovery_exhausted")
        links = connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links").fetchone()[0]
        work = json.loads(connection.execute(
            "SELECT work_order_json FROM scheduler_work_orders WHERE work_order_id=?",
            (stopped["work_order_ref"],)).fetchone()[0])
        from dalton_core.store import content_hash

        recorded = append_recovery_round(
            connection, admission_ref=admission["id"], admission_hash=admission["content_hash"],
            stage_ordinal=2, failed_work_order_ref=stopped["work_order_ref"],
            failed_work_order_hash=content_hash(work), hold_reason="fresh_work_recovery_exhausted",
            kind="owner", actor_ref="human:owner", basis={"door": "retry_round"},
            now=fixture.harness.clock.value)
        self.assertEqual(recorded["status"], "recorded")
        replay = append_recovery_round(
            connection, admission_ref=admission["id"], admission_hash=admission["content_hash"],
            stage_ordinal=2, failed_work_order_ref=stopped["work_order_ref"],
            failed_work_order_hash=content_hash(work), hold_reason="fresh_work_recovery_exhausted",
            kind="owner", actor_ref="human:owner", basis={"door": "retry_round"},
            now=fixture.harness.clock.value + timedelta(minutes=1))
        self.assertEqual(replay["status"], "replayed")
        self.assertEqual(read_recovery_round(connection, stopped["work_order_ref"])["id"],
                         recorded["id"])
        calls = adapter.calls
        recovered = executor.run_once(admission["id"])
        self.assertEqual(recovered["reason"], "fresh_work_recovery")
        self.assertEqual(connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links").fetchone()[0],
            links + 1)
        link = json.loads(connection.execute(
            "SELECT record_json FROM mission_document_research_recovery_links "
            "ORDER BY recovery_number DESC LIMIT 1").fetchone()[0])
        self.assertEqual(link["failure_proof"]["recovery_round_ref"], recorded["id"])
        final = self._run_until(
            executor, admission, lambda item: item.get("research_status") == "candidate_staged")
        self.assertEqual(final["research_status"], "candidate_staged")
        self.assertEqual(adapter.calls, calls + 1)

    def test_rounds_are_append_only(self):
        from dalton_core.mission_document_research_executor import append_recovery_round

        fixture, admission, _executor, _adapter, stopped = self._exhausted()
        connection = fixture.store.connection
        append_recovery_round(
            connection, admission_ref=admission["id"], admission_hash=admission["content_hash"],
            stage_ordinal=2, failed_work_order_ref=stopped["work_order_ref"],
            failed_work_order_hash="0" * 64, hold_reason="fresh_work_recovery_exhausted",
            kind="automatic", actor_ref="automation:document-research-fresh-work-round",
            basis={}, now=fixture.harness.clock.value)
        with self.assertRaises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM mission_document_research_recovery_rounds")
        connection.rollback()

    # -- the lane: who opens the next round -------------------------------

    def _router(self, directory, proof, *, served):
        """A router database with the failed decision and, maybe, a later serve."""

        path = Path(directory) / "model-router.sqlite"
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE model_route_decisions (decision_sequence INTEGER PRIMARY KEY, "
            "decision_id TEXT, outcome TEXT, policy_version_ref TEXT, capability TEXT, "
            "selected_profile_version_ref TEXT, created_at TEXT)")
        connection.execute(
            "INSERT INTO model_route_decisions VALUES(1,?,?,?,?,?,?)",
            (proof["route_decision_ref"], "rejected", "model-routing-policy-version:dossier:55",
             "research", None, proof["failed_at"]))
        if served:
            connection.execute(
                "INSERT INTO model_route_decisions VALUES(2,?,?,?,?,?,?)",
                ("route-decision:later", "selected", "model-routing-policy-version:dossier:59",
                 "research", "model-profile-version:glm:5", "2099-01-01T00:00:00+00:00"))
        connection.commit()
        connection.close()
        return path

    def _stopped_observation(self, fixture, admission, stopped):
        from dalton_core.mission_document_research_executor import (
            read_mission_document_research_observations)

        return [item for item in read_mission_document_research_observations(
            fixture.store.connection)
            if item["work_order_ref"] == stopped["work_order_ref"]
            and item["recovery"]["status"] == "stopped"][-1]

    def test_the_lane_opens_a_round_itself_once_a_model_is_back_once_a_day(self):
        from dalton_core.mission_document_research_lane import (
            MissionDocumentResearchCoordinator)

        fixture, admission, executor, _adapter, stopped = self._exhausted()
        observation = self._stopped_observation(fixture, admission, stopped)
        proof = observation["recovery"]["proof"]
        self.assertEqual(proof["classification"], "route_rejected_definitely_not_sent")
        now = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as name:
            # Nothing served since the failure: the hold says it is waiting
            # for a model -- no owner door, no round.
            lane = MissionDocumentResearchCoordinator(
                store=fixture.store, launcher=None, clock=lambda: now,
                model_router_db=self._router(name, proof, served=False))
            waiting = lane._typed_recovery_state(admission, stopped["work_order_ref"])
            self.assertEqual(waiting["action"], "recovery_required")
            self.assertTrue(waiting["reason"].startswith(
                "fresh_work_recovery_exhausted:all_models_unavailable:awaiting_model_availability"))
            from dalton_core.mission_document_research_executor import available_doors

            self.assertEqual([door["door"] for door in available_doors(waiting)], ["withdraw"])
            self.assertTrue(available_doors(waiting)[0]["automatic_retry"])
        with tempfile.TemporaryDirectory() as name:
            lane = MissionDocumentResearchCoordinator(
                store=fixture.store, launcher=None, clock=lambda: now,
                model_router_db=self._router(name, proof, served=True))
            resumed = lane._typed_recovery_state(admission, stopped["work_order_ref"])
            self.assertEqual(resumed["action"], "resume")
            self.assertTrue(resumed["reason"].startswith("fresh_work_recovery_round:"))
            row = fixture.store.connection.execute(
                "SELECT kind, actor_ref, day FROM mission_document_research_recovery_rounds"
            ).fetchone()
            self.assertEqual(tuple(row), ("automatic",
                                          "automation:document-research-fresh-work-round",
                                          "2026-10-09"))
            # The same tick again resumes the same round, writes nothing more.
            self.assertEqual(lane._typed_recovery_state(
                admission, stopped["work_order_ref"]), resumed)
            self.assertEqual(fixture.store.connection.execute(
                "SELECT count(*) FROM mission_document_research_recovery_rounds").fetchone()[0], 1)
        # The executor runs it: one fresh Work.
        recovered = executor.run_once(admission["id"])
        self.assertEqual(recovered["reason"], "fresh_work_recovery")

    def test_a_second_automatic_round_the_same_day_waits_for_tomorrow(self):
        from dalton_core.mission_document_research_executor import (
            MissionDocumentResearchExecutorError, append_recovery_round)
        from dalton_core.mission_document_research_lane import (
            MissionDocumentResearchCoordinator)

        fixture, admission, _executor, _adapter, stopped = self._exhausted()
        observation = self._stopped_observation(fixture, admission, stopped)
        proof = observation["recovery"]["proof"]
        now = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
        # An automatic round already spent today on another failed Work.
        append_recovery_round(
            fixture.store.connection, admission_ref=admission["id"],
            admission_hash=admission["content_hash"], stage_ordinal=2,
            failed_work_order_ref="work:earlier", failed_work_order_hash="0" * 64,
            hold_reason="fresh_work_recovery_exhausted", kind="automatic",
            actor_ref="automation:document-research-fresh-work-round", basis={}, now=now)
        with self.assertRaises(MissionDocumentResearchExecutorError):
            append_recovery_round(
                fixture.store.connection, admission_ref=admission["id"],
                admission_hash=admission["content_hash"], stage_ordinal=2,
                failed_work_order_ref="work:other", failed_work_order_hash="0" * 64,
                hold_reason="fresh_work_recovery_exhausted", kind="automatic",
                actor_ref="automation:document-research-fresh-work-round", basis={}, now=now)
        with tempfile.TemporaryDirectory() as name:
            lane = MissionDocumentResearchCoordinator(
                store=fixture.store, launcher=None, clock=lambda: now,
                model_router_db=self._router(name, proof, served=True))
            held = lane._typed_recovery_state(admission, stopped["work_order_ref"])
        self.assertEqual(held["action"], "recovery_required")
        self.assertIn("automatic_round_used_today", held["reason"])
        self.assertIn("next=2026-10-10T00:00:00+00:00", held["reason"])
        from dalton_core.mission_document_research_executor import next_automatic_retry

        self.assertEqual(next_automatic_retry(held["reason"]), "2026-10-10T00:00:00+00:00")

    def test_the_owner_door_records_a_round_on_a_policy_cap(self):
        from dalton_core.mission_document_research_lane import (
            HOLDS_FILE, _write_holds, authorize_reentry)

        fixture, admission, executor, _adapter, stopped = self._exhausted()
        with tempfile.TemporaryDirectory() as name:
            tickets = Path(name)
            # The live ledger row: the bare reason the 10-09 run left.
            _write_holds(tickets / HOLDS_FILE, {admission["id"]: {
                "admission_hash": admission["content_hash"],
                "ticket_ref": "mission-document-research:" + "5" * 24,
                "reason": "fresh_work_recovery_exhausted",
                "disposition": "recovery_required", "retry_at": None}})
            granted = []
            launcher = SimpleNamespace(
                tickets_dir=tickets,
                authorize_controlled_reentry=lambda *a, **k: granted.append(a) or {})
            server = SimpleNamespace(store=fixture.store, lane_launcher=lambda _k: launcher)
            result = authorize_reentry(server, {"admission_ref": admission["id"],
                                                "actor_ref": "human:owner"})
            self.assertEqual(result["status"], "granted")
            self.assertEqual(result["kind"], "recovery_round")
            self.assertEqual(granted, [])     # no launcher grant: the round is the door
            again = authorize_reentry(server, {"admission_ref": admission["id"],
                                               "actor_ref": "human:owner"})
            self.assertEqual(again["round_status"], "replayed")
            # A models-unavailable hold is the lane's own: the door refuses.
            _write_holds(tickets / HOLDS_FILE, {admission["id"]: {
                "admission_hash": admission["content_hash"], "ticket_ref": None,
                "reason": "fresh_work_recovery_exhausted:all_models_unavailable:"
                          "awaiting_model_availability",
                "disposition": "recovery_required", "retry_at": None}})
            refused = authorize_reentry(server, {"admission_ref": admission["id"],
                                                 "actor_ref": "human:owner"})
            self.assertEqual(refused["status"], "refused")
        row = fixture.store.connection.execute(
            "SELECT kind, actor_ref FROM mission_document_research_recovery_rounds").fetchone()
        self.assertEqual(tuple(row), ("owner", "human:owner"))
        recovered = executor.run_once(admission["id"])
        self.assertEqual(recovered["reason"], "fresh_work_recovery")


if __name__ == "__main__":
    unittest.main()
