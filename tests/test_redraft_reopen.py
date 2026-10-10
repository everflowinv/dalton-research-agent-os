"""Batch C: gate_passed is no longer terminal -- the redraft thresholds reopen it.

On a real Core (the P14d harness: one published, passed Initial Screen), a new
quarter's transcript arriving in the evidence index makes the reopen lane
propose *and* accept a gate reopen as automation, with every trigger and an
owner-visible note recorded; a single sales note does not; the stage ladder
refuses an automated reopen that does not cite a policy decision.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dalton_core.coverage_mission import CoverageMissionConflict, fold_stage_status
from dalton_core.deliverable_reopen import GateReopenAuthority, approved_reopen, folded_stage_history
from dalton_core.mission_reopen_lane import CHECKPOINT_KIND, MissionReopenLaneCoordinator
from tests.p14a_fixtures import ACN, AUTOMATION
from tests.test_deliverable_reopen import ReopenHarness
from tests.test_material_pack import FILL, Corpus, transcript


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class PolicyReopenTests(ReopenHarness):
    def setUp(self) -> None:
        super().setUp()
        self.version = self.publish()
        self.pass_gate(self.version)
        self.grant(checkpoints=(CHECKPOINT_KIND,))
        self.clock = Clock(datetime.now(timezone.utc) + timedelta(minutes=5))
        self.corpus = Corpus(self.state_dir)
        self.today = self.clock.now.date().isoformat()
        self.later = (self.clock.now - timedelta(minutes=1)).isoformat()

    def arrive(self, kind: str, title: str, text: str) -> str:
        return self.corpus.add(kind, title, text, date=self.today, company=ACN, now=self.later, acquired=self.later)

    def coordinator(self) -> MissionReopenLaneCoordinator:
        return MissionReopenLaneCoordinator(
            mission=lambda: self.mission, connection=self.store.connection,
            authority=GateReopenAuthority(self.store), clock=self.clock, state_dir=self.state_dir)

    def stage(self) -> str | None:
        return fold_stage_status(folded_stage_history(self.store.connection, company_ref=ACN,
                                                      stage_ref="initial_screen"))

    def test_a_new_quarter_transcript_reopens_the_gate_automatically(self) -> None:
        self.arrive("transcript", "Accenture (ACN) Q1 2027 earnings call", transcript("Q1", 2027))
        result = self.coordinator().dispatch_once()
        self.assertEqual(result["status"], "reopened", result)
        self.assertEqual(len(result["reopened_by_policy"]), 1)
        entry = result["looked"][0]
        self.assertEqual(entry["status"], "reopened_by_policy")
        self.assertIn("必需材料出了新版本", entry["note"])
        decision = approved_reopen(self.store.connection, ACN)
        self.assertIsNotNone(decision)
        self.assertEqual(decision["decided_by"], "redraft_policy")
        self.assertEqual(decision["actor_ref"], AUTOMATION)
        self.assertEqual(decision["owner_note"], decision["reason"])
        self.assertIn("required_material", [t["code"] for t in decision["triggers"]])
        self.assertEqual(decision["proposal"]["change_reason"], "evidence_thicker")
        self.assertEqual(self.stage(), "reopened")
        # Re-running changes nothing: the gate is open, nothing is passed.
        again = self.coordinator().dispatch_once()
        self.assertEqual(again["reopened_by_policy"], [])
        self.assertEqual(len(self.reopens.proposals(ACN)), 1)

    def test_a_single_sales_note_is_only_pending(self) -> None:
        self.arrive("sales_note", "Desk note", "ACN +2% on a quiet tape. " + FILL)
        result = self.coordinator().dispatch_once()
        self.assertEqual(result["reopened_by_policy"], [])
        self.assertEqual(self.reopens.proposals(ACN), [])
        self.assertEqual(self.stage(), "gate_passed")

    def test_a_mission_that_opts_out_only_proposes(self) -> None:
        from dalton_core.redraft_policy import auto_reopen

        self.arrive("transcript", "Accenture (ACN) Q1 2027 earnings call", transcript("Q1", 2027))
        mission = {**self.mission, "autonomy": {**self.mission["autonomy"], "auto_reopen": False}}
        result = auto_reopen(GateReopenAuthority(self.store), self.store.connection, self.state_dir, ACN,
                             mission=mission, now=self.clock.now)
        self.assertEqual(result["status"], "proposed")
        self.assertIsNone(approved_reopen(self.store.connection, ACN))
        self.assertEqual(self.stage(), "gate_passed")

    def test_the_ladder_refuses_an_automated_reopen_without_a_policy_decision(self) -> None:
        with self.assertRaises(CoverageMissionConflict):
            self.missions.record_stage_reopen(
                mission_version_ref=self.mission["id"], mission_version_hash=self.mission["content_hash"],
                company_ref=ACN, stage_ref="initial_screen", reopen_decision_ref="gate-reopen-decision:nope",
                reopen_proposal_ref="gate-reopen-proposal:nope", reopened_version_ref=self.version["id"],
                rationale="x", actor_ref=AUTOMATION, idempotency_key="k", policy_automation=True)
        with self.assertRaises(CoverageMissionConflict):
            self.missions.record_stage_reopen(
                mission_version_ref=self.mission["id"], mission_version_hash=self.mission["content_hash"],
                company_ref=ACN, stage_ref="initial_screen", reopen_decision_ref="gate-reopen-decision:nope",
                reopen_proposal_ref="gate-reopen-proposal:nope", reopened_version_ref=self.version["id"],
                rationale="x", actor_ref=AUTOMATION, idempotency_key="k2")

    def test_a_blank_state_directory_is_a_quiet_canary(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as blank:
            coordinator = MissionReopenLaneCoordinator(
                mission=lambda: self.mission, connection=self.store.connection,
                authority=GateReopenAuthority(self.store), clock=self.clock, state_dir=blank)
            result = coordinator.dispatch_once()
            self.assertEqual(result["reopened_by_policy"], [])
            self.assertEqual(self.stage(), "gate_passed")


if __name__ == "__main__":
    import unittest

    unittest.main()
