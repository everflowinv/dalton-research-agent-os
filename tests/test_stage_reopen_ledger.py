"""P14d sequel: a re-opened gate can be decided a second time (ADR-0008).

The hole the stage-ladder片 left open, closed. Before this there was no way to
write the second ``gate_passed``: the ladder is right to refuse a decision on a
settled stage, and nothing could say that a person had un-settled it.
"""

from __future__ import annotations

import json
import sqlite3
import unittest

from dalton_core.coverage_mission import (
    FOLDED_STAGE_STATUSES,
    STAGE_REOPENED,
    STAGE_STATUSES,
    CoverageMissionConflict,
    CoverageMissionValidationError,
    fold_stage_status,
    validate_mission_stage_reopen,
)
from dalton_core.deliverable_reopen import (
    DeliverableReopenConflict,
    approved_reopen,
    folded_stage_history,
    passed_version,
    reopen_assessment,
)
from dalton_core.initial_screen_cli import reopen_revision
from dalton_core.mission_reopen_lane import passed_companies
from dalton_core.mission_stage import STAGE_STATUS_LABELS
from dalton_core.store import content_hash
from tests.p14a_fixtures import ACN, AUTOMATION, CTSH, OWNER
from tests.test_deliverable_reopen import ReopenHarness

STAGE = "initial_screen"


class VocabularyTests(unittest.TestCase):
    def test_the_written_statuses_are_three_and_the_folded_ones_are_four(self):
        # The stage-records table is unchanged and so is what may be written
        # to it. ``reopened`` is a folded state only: it comes from its own
        # ledger, which is why the live rows keep their hashes.
        self.assertEqual(set(STAGE_STATUSES), {"entered", "gate_passed", "gate_failed"})
        self.assertEqual(set(FOLDED_STAGE_STATUSES),
                         {"entered", "gate_passed", "gate_failed", STAGE_REOPENED})
        self.assertNotIn(STAGE_REOPENED, STAGE_STATUSES)

    def test_every_folded_status_has_a_name_a_person_can_read(self):
        for status in FOLDED_STAGE_STATUSES:
            self.assertIn(status, STAGE_STATUS_LABELS)
            self.assertTrue(STAGE_STATUS_LABELS[status])

    def test_a_reopen_supersedes_the_decision_before_it_and_not_the_one_after(self):
        self.assertEqual(fold_stage_status(["entered", "gate_passed"]), "gate_passed")
        self.assertEqual(
            fold_stage_status(["entered", "gate_passed", STAGE_REOPENED]), STAGE_REOPENED)
        # A second ``entered`` during a reopen is bookkeeping, not a state.
        self.assertEqual(
            fold_stage_status(["entered", "gate_passed", STAGE_REOPENED, "entered"]),
            STAGE_REOPENED)
        self.assertEqual(
            fold_stage_status(["entered", "gate_passed", STAGE_REOPENED, "gate_passed"]),
            "gate_passed")
        self.assertEqual(
            fold_stage_status(["entered", "gate_passed", STAGE_REOPENED, "gate_failed"]),
            "gate_failed")
        # And the three-status behaviour is exactly what it was.
        self.assertIsNone(fold_stage_status([]))
        self.assertEqual(fold_stage_status(["entered"]), "entered")
        self.assertEqual(
            fold_stage_status(["entered", "gate_failed", "gate_passed"]), "gate_passed")
        self.assertEqual(
            fold_stage_status(["entered", "gate_passed", "entered"]), "gate_passed")


class LadderHarness(ReopenHarness):
    """ACN with a passed screen, and everything needed to re-open it."""

    def setUp(self):
        super().setUp()
        self.grant(checkpoints=("gate_reopen",))
        self.version_one = self.publish()
        self.pass_gate(self.version_one)
        self.thicken(lines=250)
        self.assessment = reopen_assessment(self.store.connection, company_ref=ACN)
        self.proposal = self.reopens.propose(
            assessment=self.assessment, mission=self.mission, actor_ref=AUTOMATION,
        )

    def approve(self, *, actor_ref=OWNER, reason="报表行进来了，这份筛选是在没有报表时写的。"):
        return self.reopens.decide(
            proposal_ref=self.proposal["id"], proposal_hash=self.proposal["content_hash"],
            verdict="approve", reason=reason, actor_ref=actor_ref,
        )

    def ladder(self):
        return self.missions.current_stage_state(self.mission_ref, ACN)

    def enter_again(self, key):
        """``entered`` with its own idempotency key, so a refusal is a refusal.

        ``P14aHarness.enter_screen`` reuses one key, so a second call replays
        the first answer instead of reaching the ladder.
        """

        return self.missions.record_stage(
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"],
            company_ref=ACN, stage_ref=STAGE, status="entered",
            evidence_refs=[self.mission["id"]], rationale="re-entered",
            actor_ref=AUTOMATION, idempotency_key=key,
        )

    def reissue(self):
        permission = approved_reopen(self.store.connection, ACN)
        self.assertIsNotNone(permission)
        return self.publish(
            sections=self.sections(self.claim_refs[3:9]),
            revision=reopen_revision(permission), summary="重出之后的摘要。",
        )

    def decide_again(self, version, *, passed=True):
        return self.missions.record_stage(
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"],
            company_ref=ACN, stage_ref=STAGE,
            status="gate_passed" if passed else "gate_failed",
            evidence_refs=[version["id"], self.mission["id"]],
            rationale="P10c 出口门自评：四问全部为是，文档非空壳，数字零无源",
            actor_ref=AUTOMATION,
            idempotency_key=f"{self.mission['id']}:{ACN}:{STAGE}:{version['id']}",
        )


class SecondDecisionTests(LadderHarness):
    def test_without_a_reopen_the_second_decision_is_still_refused(self):
        # The ladder is right to refuse a decision on a settled gate. That is
        # the rule this片 does not relax; it adds the one thing that unsettles.
        second = self.publish(sections=self.sections(self.claim_refs[3:9]),
                              summary="没有批准就重写。")
        with self.assertRaisesRegex(CoverageMissionConflict, "already passed"):
            self.decide_again(second)
        with self.assertRaisesRegex(CoverageMissionConflict, "already entered"):
            self.enter_again("no-reopen")

    def test_reopen_then_a_second_screen_then_a_second_pass(self):
        self.assertEqual(self.ladder()["stages"][STAGE]["status"], "gate_passed")
        decision = self.approve()
        marker = decision["stage_reopen"]
        self.assertEqual(marker["status_marker"], "fresh")

        # Mid-reopen: at the stage, re-opened. Not "not started", not passed.
        mid = self.ladder()
        self.assertEqual(mid["stages"][STAGE]["status"], STAGE_REOPENED)
        self.assertEqual(mid["current_stage"], STAGE)
        self.assertEqual(mid["current_status"], STAGE_REOPENED)
        self.assertEqual(mid["completed_stages"], [])
        self.assertEqual(mid["next_stage"], STAGE)

        version_two = self.reissue()
        self.assertEqual(version_two["version"], 2)
        self.assertEqual(version_two["prior_version_ref"], self.version_one["id"])

        recorded = self.decide_again(version_two)
        self.assertEqual(recorded["status_marker"], "fresh")
        self.assertEqual(recorded["status"], "gate_passed")

        after = self.ladder()
        self.assertEqual(after["stages"][STAGE]["status"], "gate_passed")
        self.assertEqual(after["completed_stages"], [STAGE])
        self.assertEqual(
            [item["status"] for item in after["stages"][STAGE]["history"]],
            ["entered", "gate_passed", STAGE_REOPENED, "gate_passed"],
        )
        # The record of the first pass is exactly where it was.
        first = self.store.connection.execute(
            "SELECT content_hash FROM coverage_mission_stage_records "
            "WHERE company_ref=? AND status='gate_passed' ORDER BY created_at",
            (ACN,),
        ).fetchall()
        self.assertEqual(len(first), 2)
        self.assertNotEqual(first[0]["content_hash"], first[1]["content_hash"])

    def test_the_lane_s_own_gate_evaluation_writes_the_second_decision(self):
        """Item (3), through the code the lane actually runs.

        Not a hand-picked status: the exit gate is assessed the way
        ``initial_screen_cli.run`` assesses it and the status is derived the
        way line 476 derives it, so what is pinned is that the lane's own
        sentence now lands instead of being refused.
        """

        from dalton_core.initial_screen import assess_exit_gate
        from dalton_core.initial_screen_cli import _target
        from dalton_core.mission_stage import evaluate_mission

        self.approve()
        rows = evaluate_mission(
            self.store.connection, self.mission, planned_specs=set(),
            stage_state=self.missions.stage_state_by_company(self.mission_ref),
        )
        for row in rows:
            for item in row["items"]:
                item["status"] = "complete"
        claims = {ACN: [{"ref": self.claim_refs[0], "created_at": "2026-09-01T00:00:00+00:00"}]}
        reopens = {ACN: approved_reopen(self.store.connection, ACN)}
        entry, skipped = _target(
            mission=self.mission, stage_rows=rows,
            deliverables={ACN: {"created_at": "2026-09-30T00:00:00+00:00"}},
            claims=claims, reopens=reopens,
        )
        self.assertIsNotNone(entry, skipped)
        self.assertEqual(entry["company_ref"], ACN)
        self.assertEqual(entry["stage_status"], STAGE_REOPENED)

        version_two = self.reissue()
        gate = assess_exit_gate(
            playbook=self.playbook, checklist_entry=entry,
            sections=version_two["sections"],
        )
        self.assertTrue(gate["passed"], gate["rationale"])
        recorded = self.missions.record_stage(
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"],
            company_ref=ACN, stage_ref=STAGE,
            status="gate_passed" if gate["passed"] else "gate_failed",
            evidence_refs=[version_two["id"], self.mission["id"]],
            rationale=f"P10c 出口门自评：{gate['rationale']}"[:2000],
            actor_ref=self.mission["autonomy"]["automation_principal"],
            idempotency_key=f"{self.mission['id']}:{ACN}:{STAGE}:{version_two['id']}",
        )
        self.assertEqual(recorded["status_marker"], "fresh")
        self.assertEqual(self.ladder()["stages"][STAGE]["status"], "gate_passed")

    def test_a_reopened_gate_can_also_fail_the_second_time(self):
        self.approve()
        version_two = self.reissue()
        self.decide_again(version_two, passed=False)
        self.assertEqual(self.ladder()["stages"][STAGE]["status"], "gate_failed")
        self.assertEqual(self.ladder()["completed_stages"], [])

    def test_a_second_entered_is_legal_only_while_the_gate_is_open(self):
        self.approve()
        again = self.enter_again("during-reopen")
        self.assertEqual(again["status"], "entered")
        # And it does not walk the state back to "in progress".
        self.assertEqual(self.ladder()["stages"][STAGE]["status"], STAGE_REOPENED)
        version_two = self.reissue()
        self.decide_again(version_two)
        with self.assertRaisesRegex(CoverageMissionConflict, "already entered"):
            self.enter_again("after-second-pass")


class ApprovePathTests(LadderHarness):
    def test_the_marker_names_the_decision_and_the_version_it_re_issues(self):
        decision = self.approve()
        marker = decision["stage_reopen"]
        self.assertEqual(marker["reopen_decision_ref"], decision["id"])
        self.assertEqual(marker["reopen_proposal_ref"], self.proposal["id"])
        self.assertEqual(marker["reopened_version_ref"], self.version_one["id"])
        self.assertEqual(marker["company_ref"], ACN)
        self.assertEqual(marker["stage_ref"], STAGE)
        self.assertEqual(marker["actor_ref"], OWNER)
        # Provenance: the marker binds the *active* mission version.
        self.assertEqual(marker["mission_version_ref"], self.mission["id"])
        self.assertEqual(marker["mission_version_hash"], self.mission["content_hash"])
        self.assertIn(decision["reason"], marker["rationale"])
        self.assertEqual(validate_mission_stage_reopen(
            {k: v for k, v in marker.items() if k != "status_marker"})["id"], marker["id"])
        self.assertEqual(
            [row["id"] for row in self.missions.stage_reopens(self.mission_ref, ACN)],
            [marker["id"]],
        )

    def test_a_declined_reopen_writes_nothing_to_the_ladder(self):
        declined = self.reopens.decide(
            proposal_ref=self.proposal["id"], proposal_hash=self.proposal["content_hash"],
            verdict="decline", reason="两百五十行报表不改变这份文件。", actor_ref=OWNER,
        )
        self.assertEqual(declined["verdict"], "decline")
        self.assertNotIn("stage_reopen", declined)
        self.assertEqual(self.missions.stage_reopens(self.mission_ref), [])
        self.assertEqual(self.ladder()["stages"][STAGE]["status"], "gate_passed")
        # And the gate is still settled, so no second decision lands.
        second = self.publish(sections=self.sections(self.claim_refs[3:9]), summary="x")
        with self.assertRaisesRegex(CoverageMissionConflict, "already passed"):
            self.decide_again(second)

    def test_the_same_approval_replayed_heals_a_missing_marker(self):
        decision = self.approve()
        marker = decision["stage_reopen"]
        again = self.approve()
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["stage_reopen"]["id"], marker["id"])
        self.assertEqual(len(self.missions.stage_reopens(self.mission_ref)), 1)

    def test_an_approval_whose_gate_is_not_passed_is_refused_before_it_is_written(self):
        self.approve()
        # A second proposal, while the first reopen is still open: there is
        # nothing to re-open, and nothing at all should be written.
        self.thicken(lines=140)
        second_assessment = reopen_assessment(self.store.connection, company_ref=ACN)
        self.assertEqual(second_assessment["status"], "not_passed")

    def test_an_open_reopen_says_so_instead_of_blaming_the_evidence(self):
        """Three situations used to arrive as one sentence about items.

        "No item flipped" sent a reader to look at the evidence base for
        something that is a pending decision on their own desk. The assessment
        now says which of the two ``not_passed`` cases it is, and ``propose``
        repeats it.
        """

        self.approve()
        self.thicken(lines=140)
        assessment = reopen_assessment(self.store.connection, company_ref=ACN)
        self.assertEqual(assessment["status"], "not_passed")
        self.assertTrue(assessment["reopened"])
        self.assertEqual(assessment["stage_status"], STAGE_REOPENED)
        self.assertIn("重开", assessment["reason"])
        with self.assertRaisesRegex(DeliverableReopenConflict, "先把重出的那一版裁决掉"):
            self.reopens.propose(
                assessment=assessment, mission=self.mission, actor_ref=AUTOMATION)
        self.assertEqual(len(self.reopens.proposals(ACN)), 1)

        # A company that simply never passed still gets the other sentence.
        never = reopen_assessment(self.store.connection, company_ref=CTSH)
        self.assertEqual(never["status"], "not_passed")
        self.assertFalse(never["reopened"])
        with self.assertRaisesRegex(DeliverableReopenConflict, "没有门可以重开"):
            self.reopens.propose(
                assessment=never, mission=self.mission, actor_ref=AUTOMATION)

        # And once the re-issued screen has passed, the ordinary refusal is
        # the ordinary one again.
        self.decide_again(self.reissue())
        quiet = reopen_assessment(self.store.connection, company_ref=ACN)
        self.assertEqual(quiet["status"], "no_flip")
        with self.assertRaisesRegex(DeliverableReopenConflict, "flipped"):
            self.reopens.propose(
                assessment=quiet, mission=self.mission, actor_ref=AUTOMATION)

    def test_a_reopen_can_only_be_spent_once(self):
        decision = self.approve()
        # Spend it, so the stage is passed again and "only a passed gate" is
        # not what refuses the replay -- the uniqueness on the decision is.
        self.decide_again(self.reissue())
        with self.assertRaisesRegex(CoverageMissionConflict, "already re-opened"):
            self.missions.record_stage_reopen(
                mission_version_ref=self.mission["id"],
                mission_version_hash=self.mission["content_hash"],
                company_ref=ACN, stage_ref=STAGE,
                reopen_decision_ref=decision["id"],
                reopen_proposal_ref=self.proposal["id"],
                reopened_version_ref=self.version_one["id"],
                rationale="second go", actor_ref=OWNER,
                idempotency_key="a different key",
            )


class MarkerRefusalTests(LadderHarness):
    def marker(self, **overrides):
        args = dict(
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"],
            company_ref=ACN, stage_ref=STAGE,
            reopen_decision_ref="gate-reopen-decision:x",
            reopen_proposal_ref=self.proposal["id"],
            reopened_version_ref=self.version_one["id"],
            rationale="because", actor_ref=OWNER, idempotency_key="k",
        )
        args.update(overrides)
        return self.missions.record_stage_reopen(**args)

    def test_automation_can_never_re_open_a_gate(self):
        with self.assertRaisesRegex(CoverageMissionConflict, "human checkpoint"):
            self.marker(actor_ref=AUTOMATION)
        self.assertEqual(self.missions.stage_reopens(self.mission_ref), [])

    def test_a_stage_that_never_passed_cannot_be_re_opened(self):
        with self.assertRaisesRegex(CoverageMissionConflict, "only a passed gate"):
            self.marker(company_ref=CTSH, idempotency_key="k2")

    def test_a_company_outside_the_universe_is_refused(self):
        with self.assertRaisesRegex(CoverageMissionConflict, "universe"):
            self.marker(company_ref="company:sec-cik:9999999999", idempotency_key="k3")

    def test_the_marker_binds_the_active_version(self):
        stale = self.mission["id"]
        self.grant("claim")  # publishes a new mission version
        with self.assertRaisesRegex(CoverageMissionConflict, "active mission version"):
            self.marker(mission_version_ref=stale,
                        mission_version_hash=self.missions.mission(stale)["content_hash"],
                        idempotency_key="k4")

    def test_the_marker_is_append_only(self):
        written = self.marker()
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute(
                "UPDATE coverage_mission_stage_reopens SET company_ref='x' WHERE record_id=?",
                (written["id"],),
            )
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute(
                "DELETE FROM coverage_mission_stage_reopens WHERE record_id=?",
                (written["id"],),
            )
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute(
                "INSERT INTO coverage_mission_stage_reopens(record_id,mission_version_ref,"
                "company_ref,stage_ref,reopen_decision_ref,reopen_proposal_ref,"
                "reopened_version_ref,record_json,content_hash,actor_ref,created_at) "
                "VALUES('r','m','c','initial_screen','d','p','v','{}','h','human:x','2026')"
            )

    def test_a_marker_with_the_wrong_shape_is_refused_by_contract(self):
        written = self.marker()
        wire = {k: v for k, v in written.items() if k != "status_marker"}
        with self.assertRaisesRegex(CoverageMissionValidationError, "closed shape"):
            validate_mission_stage_reopen({**wire, "extra": 1})
        with self.assertRaisesRegex(CoverageMissionValidationError, "always 'reopened'"):
            validate_mission_stage_reopen({**wire, "status": "gate_failed"})


class ReadersTests(LadderHarness):
    def test_residency_is_monotone_across_a_reopen(self):
        # P14a: a company leaves coverage by leaving the universe, which is a
        # human act -- never by a gate being re-opened.
        before = self.missions.companies_at_or_past("deep_insight_gate", self.mission_ref)
        self.assertEqual(before, [ACN])
        self.approve()
        self.assertEqual(
            self.missions.companies_at_or_past("deep_insight_gate", self.mission_ref),
            before,
        )
        self.assertEqual(
            self.missions.companies_at_or_past(STAGE, self.mission_ref), [ACN])

    def test_the_weekly_lane_does_not_offer_an_already_open_gate(self):
        self.assertEqual(passed_companies(self.store.connection, self.mission), [ACN])
        self.approve()
        self.assertEqual(passed_companies(self.store.connection, self.mission), [])
        version_two = self.reissue()
        self.decide_again(version_two)
        self.assertEqual(passed_companies(self.store.connection, self.mission), [ACN])

    def test_there_is_no_passed_version_to_diff_while_the_gate_is_open(self):
        self.assertIsNotNone(passed_version(self.store.connection, company_ref=ACN))
        self.approve()
        self.assertIsNone(passed_version(self.store.connection, company_ref=ACN))
        self.assertEqual(
            reopen_assessment(self.store.connection, company_ref=ACN)["status"],
            "not_passed")
        version_two = self.reissue()
        self.decide_again(version_two)
        # And afterwards the diff is measured from the version that just passed.
        self.assertEqual(
            passed_version(self.store.connection, company_ref=ACN)["version_id"],
            version_two["id"])

    def test_the_folded_history_helper_agrees_with_the_authority(self):
        self.approve()
        helper = folded_stage_history(self.store.connection, company_ref=ACN, stage_ref=STAGE)
        authority = [
            item["status"]
            for item in self.ladder()["stages"][STAGE]["history"]
        ]
        self.assertEqual(helper, authority)
        self.assertEqual(helper, ["entered", "gate_passed", STAGE_REOPENED])

    def test_the_checklist_says_reopened_in_the_owner_s_words(self):
        from dalton_core.mission_stage import evaluate_mission

        self.approve()
        rows = evaluate_mission(
            self.store.connection, self.mission, planned_specs=set(),
            stage_state=self.missions.stage_state_by_company(self.mission_ref),
        )
        acn = next(row for row in rows if row["company_ref"] == ACN)
        self.assertEqual(acn["stage"], STAGE)
        self.assertEqual(acn["stage_status"], STAGE_REOPENED)
        self.assertEqual(acn["stage_status_label"], STAGE_STATUS_LABELS[STAGE_REOPENED])
        self.assertNotEqual(acn["stage_status_label"], "还没开始")


class ExistingRowsTests(LadderHarness):
    def test_opening_the_authority_again_moves_no_hash(self):
        """The live check, run against a Core rather than described.

        Adding the marker table is a ``CREATE TABLE IF NOT EXISTS`` beside the
        others; nothing rebuilds ``coverage_mission_stage_records``, so no row
        it already holds can move. Re-opening the authority is what a deploy
        does, and this asserts what a deploy must not change.
        """

        from dalton_core.coverage_mission import CoverageMissionAuthority

        before = {
            row["record_id"]: (row["content_hash"], row["record_json"])
            for row in self.store.connection.execute(
                "SELECT record_id, content_hash, record_json "
                "FROM coverage_mission_stage_records"
            ).fetchall()
        }
        self.assertTrue(before)
        CoverageMissionAuthority(self.store)
        after = {
            row["record_id"]: (row["content_hash"], row["record_json"])
            for row in self.store.connection.execute(
                "SELECT record_id, content_hash, record_json "
                "FROM coverage_mission_stage_records"
            ).fetchall()
        }
        self.assertEqual(after, before)
        # And every one of them still hashes to what it says it does.
        moved = []
        for record_id, (digest, payload) in after.items():
            record = json.loads(payload)
            base = {k: v for k, v in record.items() if k != "content_hash"}
            if content_hash(base) != digest:
                moved.append(record_id)
        self.assertEqual(moved, [])


if __name__ == "__main__":
    unittest.main()
