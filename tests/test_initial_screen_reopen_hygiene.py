"""D3: forty-seven demands on the owner's attention, and none of them current.

Two things are true of the live approvals queue and both are bugs rather than
work.  The reopen proposals waiting on it all name a screen version that has
since been superseded, so no verdict on any of them changes anything.  And they
keep arriving, because the authority's idempotency key includes the evidence
counts and therefore changes every time a filing lands.

The first is a display defect and is fixed by a predicate: computed, explained,
and never written into the human decision ledger, because that table's whole
value is that every row in it is a decision a person made.  The second is a
generation defect and is fixed by a guard on the lane that proposes.
"""

from __future__ import annotations

import unittest

from dalton_core.initial_screen_reopen_hygiene import (
    SUPERSEDED_REASON,
    already_open_for_current,
    current_passed_version_ref,
    is_superseded,
    low_information_call,
    partition,
    superseded_refs,
    superseded_summary,
    undecided_proposals,
)
from tests.p14a_fixtures import ACN, OWNER
from tests.test_deliverable_reopen import ReopenHarness


class HygieneTests(ReopenHarness):
    def setUp(self):
        super().setUp()
        self.connection = self.store.connection
        self.version = self.publish()
        self.pass_gate(self.version)

    def propose(self):
        from dalton_core.deliverable_reopen import reopen_assessment

        assessment = reopen_assessment(self.connection, company_ref=ACN)
        self.assertEqual(assessment["status"], "reopen_proposed")
        return self.reopens.propose(
            assessment=assessment, mission=self.mission,
            actor_ref=self.mission["autonomy"]["automation_principal"])

    def reissue_screen(self, approved):
        """Approve one reopen and let the re-issued screen pass, as the live Core did.

        This is where the stale proposals come from.  One of the pile gets
        approved, the screen is re-issued and passes, and every *other* proposal
        for that company is left naming the version before it -- which is
        exactly the shape of the forty-seven on the live Core.
        """

        self.reopens.decide(
            proposal_ref=approved["id"], proposal_hash=approved["content_hash"],
            verdict="approve", reason="同意重出一版", actor_ref=OWNER)
        newer = self.publish(sections=self.sections(self.claim_refs[3:8]),
                             summary="重出一版的摘要。")
        self.pass_gate(newer)
        return newer

    def test_a_proposal_against_the_current_passed_version_is_live(self):
        self.thicken(lines=250)
        proposal = self.propose()
        self.assertFalse(is_superseded(self.connection, {
            "company_ref": ACN, "stage_ref": "initial_screen",
            "passed_version_ref": proposal["passed_version_ref"]}))
        split = partition(self.connection)
        self.assertEqual(len(split["live"]), 1)
        self.assertEqual(split["superseded"], [])
        self.assertIsNone(superseded_summary(self.connection))

    def pile_up(self):
        """Two proposals for one company against one passed version.

        Through the authority directly, because that is how they arrived: the
        weekly lane's only guard was (company, assessment hash), and thickening
        the evidence changes the hash.  D3's guard on the lane is what stops
        this happening again; this reproduces what is already on disk.
        """

        self.thicken(lines=250)
        first = self.propose()
        self.thicken(lines=60)
        second = self.propose()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["passed_version_ref"], second["passed_version_ref"])
        return first, second

    def test_a_new_screen_version_makes_every_waiting_proposal_stale(self):
        first, second = self.pile_up()
        self.assertEqual(len(undecided_proposals(self.connection)), 2)
        # The exact thing that happened on the live Core on the 14th: one
        # reopen was approved, a newer screen was published and passed, and the
        # rest of the pile still names the version before it.
        newer = self.reissue_screen(first)
        self.assertEqual(current_passed_version_ref(self.connection, ACN), newer["id"])
        self.assertNotEqual(newer["id"], second["passed_version_ref"])
        self.assertEqual(superseded_refs(self.connection), {second["id"]})
        summary = superseded_summary(self.connection)
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["reason"], SUPERSEDED_REASON)
        self.assertEqual(summary["companies"], {ACN: 1})

    def test_nothing_is_written_when_a_proposal_is_set_aside(self):
        first, second = self.pile_up()
        self.reissue_screen(first)
        superseded_summary(self.connection)
        # The human decision ledger is untouched: a machine verdict written
        # there would make the one table that proves a person looked at
        # something into a table that no longer proves it.
        self.assertIsNone(self.reopens.decision_for(second["id"]))
        self.assertEqual(len(undecided_proposals(self.connection)), 1)

    def test_one_open_question_per_company_and_passed_version(self):
        self.thicken(lines=250)
        proposal = self.propose()
        found = already_open_for_current(self.connection, company_ref=ACN)
        self.assertIsNotNone(found)
        self.assertEqual(found["proposal_ref"], proposal["id"])

    def test_an_answered_proposal_reopens_the_question(self):
        self.thicken(lines=250)
        proposal = self.propose()
        self.reopens.decide(
            proposal_ref=proposal["id"], proposal_hash=proposal["content_hash"],
            verdict="decline", reason="保留当前版本", actor_ref=OWNER)
        self.assertIsNone(already_open_for_current(self.connection, company_ref=ACN))

    def test_a_core_with_no_proposals_answers_emptily_rather_than_raising(self):
        self.assertEqual(undecided_proposals(self.connection), [])
        self.assertEqual(superseded_refs(self.connection), set())
        self.assertIsNone(superseded_summary(self.connection))


class LowInformationCallTests(unittest.TestCase):
    """avoid / NO_CHANGE / low is the machine saying it has nothing to say."""

    def test_all_three_or_none(self):
        row = {"direction": "avoid", "risk_reward_status": "NO_CHANGE",
               "confidence": "low"}
        self.assertTrue(low_information_call(row))
        self.assertFalse(low_information_call({**row, "confidence": "high"}))
        self.assertFalse(low_information_call({**row, "direction": "long"}))
        self.assertFalse(low_information_call({**row, "risk_reward_status": "MEETS"}))

    def test_a_row_shaped_object_is_read_the_same_way(self):
        import sqlite3

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE p(direction TEXT, risk_reward_status TEXT, confidence TEXT)")
        connection.execute("INSERT INTO p VALUES('avoid','NO_CHANGE','low')")
        row = connection.execute("SELECT * FROM p").fetchone()
        self.assertTrue(low_information_call(row))
        connection.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
