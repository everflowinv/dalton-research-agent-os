"""The two pages D changed: a quieter approvals queue and a list of what to do.

The approvals page stops offering verdicts that change nothing -- a reopen
proposal against a screen version that has been superseded -- and says how many
it stopped offering, because a to-do list that empties for invisible reasons is
a broken page rather than a clean one.  The gate card gains the two things a
reviewer needs to revise rather than re-read: what the last person asked for and
what this version actually changed.  And the home page gains one panel that
answers the question the owner actually has.
"""

from __future__ import annotations

import unittest

from dalton_core.initial_screen_reopen_hygiene import SUPERSEDED_REASON
from tests.test_deep_insight_gate import OWNER
from tests.test_deep_insight_gate_lane import ACN, FakeModel, Harness


class PlaneHarness(unittest.TestCase):
    standard = {"max_unknown": 12, "min_evidence_refs": 1,
                "require_question_one_classified": False,
                "require_classification_agrees": False}

    def setUp(self):
        self.harness = Harness(submission_standard=self.standard)
        self.addCleanup(self.harness.close)

    def plane(self):
        from dalton_core.cockpit_plane import CockpitConfig, CockpitPlane

        state = self.harness.state_dir
        config = CockpitConfig(
            core_db=state / "core.sqlite", state_dir=state,
            heartbeat_path=state / "heartbeat.json",
            scheduler_db=state / "scheduler.sqlite",
            journal_path=state / "cockpit.sqlite",
        )
        plane = CockpitPlane(config, writer_socket=state / "w.sock",
                             token_config=state / "t.json")
        self.addCleanup(plane.close)
        return plane


class GateCardTests(PlaneHarness):
    def setUp(self):
        super().setUp()
        first = self.harness.run(model_factory=lambda: FakeModel(answer_all=True))
        self.assertEqual(first["gate_status"], "submitted")
        self.head = self.harness.gates().latest(ACN)

    def item(self, plane, kind="deep_insight_gate"):
        return next((row for row in plane.approvals()["items"]
                     if row["kind"] == kind), None)

    def test_a_first_version_says_it_is_the_first(self):
        item = self.item(self.plane())
        self.assertIn("第一版", item["change_note"])
        self.assertNotIn("prior_review_note", item)
        # And the box says what a return is for, so a reviewer knows their
        # words are going somewhere rather than into an audit log.
        self.assertIn("q3", item["rationale_hint"])

    def test_the_next_version_carries_the_previous_reviewers_note(self):
        self.harness.gates().decide(
            gate_version_ref=self.head["id"],
            gate_version_hash=self.head["content_hash"],
            decision="return_for_more_work", reason="第三问把订单和收入搞混了",
            actor_ref=OWNER, question_notes={"q3": "供给那一段没写"})
        self.harness.run(
            model_factory=lambda: FakeModel(answer_all=True,
                                            sentence="这一版按审阅意见重写了。"))
        self.harness.fixture.close()
        item = self.item(self.plane())
        self.assertIn("第三问把订单和收入搞混了", item["prior_review_note"])
        self.assertIn("供给那一段没写", item["prior_review_note"])
        self.assertIn("q3", item["change_note"])
        self.assertIn("沿用上一版", item["change_note"])

    def test_the_twelve_answers_are_on_the_card_as_readable_lines(self):
        item = self.item(self.plane())
        for ref in ("q1", "q7", "q12"):
            self.assertIn(ref, item["details"])
            self.assertIsInstance(item["details"][ref], str)
        self.assertEqual(len(item["question_refs"]), 12)


class HeldDraftCardTests(PlaneHarness):
    standard = None

    def test_a_held_back_draft_appears_as_information_with_no_buttons(self):
        self.assertEqual(self.harness.run()["gate_status"], "auto_returned")
        self.harness.fixture.close()
        items = self.plane().approvals()["items"]
        held = [row for row in items if row["kind"] == "deep_insight_gate_held"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["actions"], [])
        self.assertFalse(held[0]["needs_rationale"])
        self.assertTrue(held[0]["summary"].startswith("系统判定草稿尚不足以提交"))
        # And nothing is waiting for a verdict, because nothing was published.
        self.assertFalse([row for row in items
                          if row["kind"] == "deep_insight_gate"])
        # The badge says zero: the card is there to explain the empty queue,
        # not to add to it.
        self.assertEqual(self.plane().approvals()["count"], 0)


class NeedsHumanEndpointTests(PlaneHarness):
    def test_the_panel_reads_the_same_state_the_approvals_page_does(self):
        self.assertEqual(
            self.harness.run(model_factory=lambda: FakeModel(answer_all=True))[
                "gate_status"], "submitted")
        result = self.plane().needs_human()
        self.assertTrue(result["enabled"])
        kinds = {row["kind"] for row in result["items"]}
        self.assertIn("gate_decision", kinds)
        first = result["items"][0]
        for field in ("title", "why_blocked", "action", "consequence", "where"):
            self.assertTrue(first[field], field)

    def test_a_core_with_no_gate_draft_still_reports_its_unconnected_sources(self):
        # The fixture mission names sources it does not have, which is the
        # ordinary state of a young installation and exactly the kind of thing
        # that never surfaced anywhere before this panel.
        result = self.plane().needs_human()
        kinds = {row["kind"] for row in result["items"]}
        self.assertEqual(kinds, {"source_not_connected"})
        self.assertNotIn("gate_decision", kinds)

    def test_the_route_is_reachable_by_its_path(self):
        from dalton_core.agenda_control import AgendaControlApplication

        self.assertTrue(hasattr(self.plane(), "needs_human"))
        source = AgendaControlApplication.cockpit_view.__code__.co_consts
        self.assertIn("/v1/cockpit/needs-human", source)


class ReopenNoiseTests(unittest.TestCase):
    """The forty-seven, on the page they were clogging."""

    def setUp(self):
        from tests.test_initial_screen_reopen_hygiene import HygieneTests

        # The reopen fixture is a TestCase of its own; borrowing its setUp is
        # cheaper and more honest than a second copy of a ladder fixture.
        self.fixture = HygieneTests("test_a_core_with_no_proposals_answers_emptily_rather_than_raising")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def plane(self):
        from dalton_core.cockpit_plane import CockpitConfig, CockpitPlane

        state = self.fixture.state_dir
        config = CockpitConfig(
            core_db=state / "core.sqlite", state_dir=state,
            heartbeat_path=state / "heartbeat.json",
            scheduler_db=state / "scheduler.sqlite",
            journal_path=state / "cockpit.sqlite",
        )
        plane = CockpitPlane(config, writer_socket=state / "w.sock",
                             token_config=state / "t.json")
        self.addCleanup(plane.close)
        return plane

    def items(self, plane, kind):
        return [row for row in plane.approvals()["items"] if row["kind"] == kind]

    def test_a_live_proposal_is_still_offered(self):
        self.fixture.thicken(lines=250)
        self.fixture.propose()
        self.assertEqual(len(self.items(self.plane(), "gate_reopen")), 1)
        self.assertEqual(self.items(self.plane(), "gate_reopen_superseded"), [])

    def test_a_superseded_proposal_leaves_the_queue_and_says_so(self):
        first, _second = self.fixture.pile_up()
        self.fixture.reissue_screen(first)
        plane = self.plane()
        self.assertEqual(self.items(plane, "gate_reopen"), [])
        note = self.items(plane, "gate_reopen_superseded")
        self.assertEqual(len(note), 1)
        self.assertIn("1", note[0]["title"])
        self.assertEqual(note[0]["actions"], [])
        self.assertEqual(note[0]["details"]["收起的理由"], SUPERSEDED_REASON)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
