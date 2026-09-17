"""The two pages D changed: a quieter approvals queue and a list of what to do.

The approvals page stops offering verdicts that change nothing -- a reopen
proposal against a screen version that has been superseded -- and says how many
it stopped offering, because a to-do list that empties for invisible reasons is
a broken page rather than a clean one.  The gate card gains the two things a
reviewer needs to revise rather than re-read: what the last person asked for and
what this version actually changed.  And the approvals page gains one panel that
answers the question the owner actually has (H3 moved it there from the home
page: the two lists are the same errand).
"""

from __future__ import annotations

import unittest

from dalton_core.cockpit_plane import INFORMATIONAL_APPROVAL_KINDS
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

    def plane(self, **extra):
        from dalton_core.cockpit_plane import CockpitConfig, CockpitPlane

        state = self.harness.state_dir
        config = CockpitConfig(
            core_db=state / "core.sqlite", state_dir=state,
            heartbeat_path=state / "heartbeat.json",
            scheduler_db=state / "scheduler.sqlite",
            journal_path=state / "cockpit.sqlite",
            **extra,
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

    def test_a_held_back_draft_is_a_notice_rather_than_an_approval(self):
        # It used to be a buttonless card in 待办审批, where it sat for days:
        # the approvals page holds decisions, and an item nobody can answer
        # never leaves it. The same news, with the next step attached, is in
        # 需要你处理 -- which is the page for things that are not verdicts.
        self.assertEqual(self.harness.run()["gate_status"], "auto_returned")
        self.harness.fixture.close()
        plane = self.plane()
        items = plane.approvals()["items"]
        self.assertFalse([row for row in items
                          if row["kind"] == "deep_insight_gate_held"])
        # Nothing is waiting for a verdict either, because nothing was
        # published -- and the empty queue is an empty queue.
        self.assertFalse([row for row in items
                          if row["kind"] == "deep_insight_gate"])
        self.assertEqual(plane.approvals()["count"], 0)
        held = [row for row in plane.needs_human()["items"]
                if row["kind"] == "gate_auto_returned"]
        self.assertEqual(len(held), 1)
        self.assertTrue(held[0]["why_blocked"].startswith("系统判定草稿尚不足以提交"))

    def test_no_approval_card_is_information_without_a_decision(self):
        """Every card on 待办审批 is something the owner can finish."""

        self.assertEqual(self.harness.run()["gate_status"], "auto_returned")
        self.harness.fixture.close()
        items = self.plane().approvals()["items"]
        self.assertFalse([row for row in items
                          if row["kind"] in INFORMATIONAL_APPROVAL_KINDS])


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


def workspace_fixture(host, *slugs, with_mission=()):
    """A host root the workspace probe can read: a manager config and two Cores.

    Written by hand rather than by starting a workspace manager, because what
    is under test is the scope of the list rather than the manager: the probe
    reads a creation record and a Core at a known path, and that is all this
    has to be.
    """

    import json
    import sqlite3

    (host / "creation-requests").mkdir(parents=True, exist_ok=True)
    for slug in slugs:
        (host / "creation-requests" / f"{slug}.json").write_text(json.dumps(
            {"slug": slug, "name": f"研究环境 {slug}",
             "url": f"http://127.0.0.1/{slug}"}), encoding="utf-8")
        state = host / "workspaces" / slug / "state" / "dalton-core"
        state.mkdir(parents=True, exist_ok=True)
        core = state / "core.sqlite"
        if not core.exists():
            connection = sqlite3.connect(core)
            if slug in with_mission:
                connection.execute("CREATE TABLE coverage_mission_pointer("
                                   "mission_ref TEXT, mission_version_id TEXT)")
                connection.execute("INSERT INTO coverage_mission_pointer VALUES('m','v')")
            connection.commit()
            connection.close()
    config = host / "manager.json"
    config.write_text(json.dumps({"host_root": str(host)}), encoding="utf-8")
    return config


class EnvironmentScopeTests(PlaneHarness):
    """Each environment's page lists its own errands and nobody else's.

    Live, the legacy page was telling the owner that the Hyperscaler workspace
    has no research goal, and the Hyperscaler page was saying the same about
    its neighbour.  Neither can be acted on from the page showing it.
    """

    def setUp(self):
        super().setUp()
        import tempfile
        from pathlib import Path

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.host = Path(folder.name) / "host"
        self.host.mkdir()

    def kinds(self, result, kind="no_active_mission"):
        return [row for row in result["items"] if row["kind"] == kind]

    def test_the_legacy_page_does_not_list_a_workspace_without_a_goal(self):
        manager = workspace_fixture(self.host, "ws-a", "ws-b")
        plane = self.plane(workspace_manager_config_path=manager)
        self.assertEqual(plane.workspace_context["mode"], "legacy")
        self.assertEqual(self.kinds(plane.needs_human()), [])
        # The fixture is not empty: the same probes, scoped to a workspace,
        # do produce the item this page is declining to show.
        from dalton_core.needs_human import collect, workspace_probes

        probes = workspace_probes(manager)
        self.assertEqual(len(probes), 2)
        self.assertEqual(len(self.kinds(collect(workspaces=probes,
                                                environment="ws-a"))), 1)

    def test_a_workspace_page_lists_only_its_own_missing_goal(self):
        import os
        from unittest.mock import patch

        from dalton_core.cockpit_plane import CockpitConfig, CockpitPlane
        from dalton_core.workspace import create_workspace_manifest

        release = self.host / "release"
        release.mkdir(parents=True)
        # A real manifest, because the plane's identity is read from the one
        # DALTON_WORKSPACE_MANIFEST names and validated against these paths.
        mine = create_workspace_manifest(
            self.host, "analyst-a", 18910, "release:sha256:" + "a" * 64, release)
        # Both Cores are empty, which is what a workspace nobody has given a
        # goal to actually looks like -- and the reason both would otherwise
        # appear on this one page.
        manager = workspace_fixture(self.host, "analyst-a", "analyst-b")
        config = CockpitConfig(
            core_db=mine.state_dir / "core.sqlite", state_dir=mine.state_dir,
            heartbeat_path=mine.state_dir / "run" / "heartbeat.json",
            scheduler_db=mine.state_dir / "scheduler.sqlite",
            journal_path=mine.state_dir / "cockpit.sqlite",
            workspace_manager_config_path=manager)
        with patch.dict(os.environ,
                        {"DALTON_WORKSPACE_MANIFEST": str(mine.manifest_path)}):
            plane = CockpitPlane(config, writer_socket=mine.writer_socket,
                                 token_config=mine.state_dir / "writer-tokens.json")
        self.addCleanup(plane.close)
        self.assertEqual(plane.workspace_context["slug"], "analyst-a")
        items = self.kinds(plane.needs_human())
        self.assertEqual([row["ref"] for row in items], ["workspace:analyst-a"])
        self.assertEqual(items[0]["urgency"], 1)


class PanelPlacementTests(unittest.TestCase):
    """H3: the panel lives on the approvals page, beside the verdicts it names.

    Asserted against the page's own markup rather than a rendered DOM, which is
    what every other cockpit UI test in this repository does: the file is the
    artefact that ships.
    """

    def page(self) -> str:
        from pathlib import Path

        import dalton_core

        return (Path(dalton_core.__file__).with_name("cockpit_control.html")
                .read_text(encoding="utf-8"))

    def section(self, page: str, view: str) -> str:
        start = page.index(f'id="view-{view}"')
        return page[start:page.index("</section>", start)]

    def test_the_panel_is_on_the_approvals_page_and_not_the_home_page(self):
        page = self.page()
        self.assertEqual(page.count('id="needs-human-card"'), 1)
        self.assertIn('id="needs-human-card"', self.section(page, "approve"))
        self.assertNotIn('id="needs-human-card"', self.section(page, "goal"))
        # Above the queue: what to do first, then the verdicts.
        approve = self.section(page, "approve")
        self.assertLess(approve.index('id="needs-human-card"'),
                        approve.index('id="approvals"'))

    def test_it_is_loaded_with_the_approvals_page_and_keeps_its_cache(self):
        page = self.page()
        self.assertIn('if(view==="approve"){loadApprovals();loadNeedsHuman()}', page)
        self.assertIn('if(view==="goal")loadOverview(true)', page)
        # The 30-second cache and the read-only failure mode are untouched.
        self.assertIn("Date.now()-needsHumanLast<30000", page)
        self.assertIn('current==="approve")loadNeedsHuman()', page)

    def test_the_badge_still_counts_verdicts_only(self):
        # The navigation badge is set from the approvals count, which the
        # needs-human list never enters: it is a view over checkpoints, not a
        # queue of them, and counting it twice would tell the owner there is
        # more waiting than there is.
        page = self.page()
        badge = page[page.index('const b=$("approve-badge")'):]
        self.assertTrue(badge.startswith(
            'const b=$("approve-badge");if(r.count)'))
        self.assertNotIn("needs-human", page[
            page.index("async function loadApprovals()"):
            page.index("function needsHumanItem")])


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

    def test_a_superseded_proposal_leaves_the_queue_and_says_so_in_the_log(self):
        # D3's line still has to be said -- a to-do list that empties for
        # invisible reasons is a broken page -- but it is not a decision, so it
        # is said in the journal rather than as a card nobody can press.
        first, _second = self.fixture.pile_up()
        self.fixture.reissue_screen(first)
        plane = self.plane()
        self.assertEqual(self.items(plane, "gate_reopen"), [])
        self.assertEqual(self.items(plane, "gate_reopen_superseded"), [])
        rows = plane.journal.rows(
            "SELECT * FROM cockpit_events WHERE kind='hygiene'")
        self.assertEqual(len(rows), 1)
        self.assertIn("1", rows[0]["title"])
        self.assertIn(SUPERSEDED_REASON, rows[0]["refs_json"])
        # Read the page again and the log does not grow: housekeeping happened
        # once, however many times the owner reloads.
        plane.approvals()
        plane.approvals()
        self.assertEqual(len(plane.journal.rows(
            "SELECT * FROM cockpit_events WHERE kind='hygiene'")), 1)
        shown = [event for event in plane.log()["events"]
                 if event["kind"] == "hygiene"]
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0]["lane"], "研究系统")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
