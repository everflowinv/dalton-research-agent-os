"""A missing research template still permits an honest, local progress issue.

These tests use the real mission, plan, cycle, weekly and outbox authorities;
the small client adapter substitutes only the RPC transport.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from dalton_core.agenda import AgendaStore
from dalton_core.cockpit_weekly_brief import _recent, weekly_brief_view, weekly_brief_document
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.industry_research import IndustryResearchAuthority
from dalton_core.store import DaltonStore
from dalton_core.weekly_brief import WeeklyBriefAuthority
from dalton_core.weekly_brief_coordinator import run_weekly_brief_cycle
from dalton_core.weekly_brief_workspace_plan import (
    LocalWeeklyBriefDelivery, publish_workspace_weekly_brief_plan,
)
from tests.p9a_fixtures import OWNER, bootstrap_method_authorities, mission_params


class _AuthorityClient:
    def __init__(self, weekly, agenda):
        self.weekly, self.agenda = weekly, agenda

    def claim_agenda_outbox(self, **params):
        return self.agenda.claim_outbox(actor_ref="core", **params)

    def record_scheduled_weekly_brief_delivery(self, **params):
        return self.weekly.record_scheduled_delivery(actor_ref="core", **params)

    def record_agenda_delivery(self, **params):
        return self.agenda.record_delivery(actor_ref="core", **params)


class MissionProgressLocalCycleTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=2)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.store = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(self.store.close)
        method = bootstrap_method_authorities(self.store)
        values = mission_params(method)
        self.mission = CoverageMissionAuthority(self.store).create_mission(
            values.pop("mission_ref"), **values)
        self.weekly = WeeklyBriefAuthority(
            self.store, IndustryResearchAuthority(self.store))
        self.agenda = AgendaStore(self.store)
        # Creation and the due slot use real, ordered instants in the fixture.
        self.published = publish_workspace_weekly_brief_plan(
            self.store, self.state, weekday=self.now.weekday(), hour=self.now.hour, minute=self.now.minute,
            timezone="UTC", destination_ref=None, expected_plan_hash=None,
            actor_ref=OWNER, now=self.now - timedelta(minutes=1))
        self.plan = self.published["plan"]

    def cycle(self, plan=None):
        return run_weekly_brief_cycle(
            self.store, self.weekly, self.agenda, plan=plan or self.plan,
            as_of=self.now.isoformat(), actor_ref="core")

    def view(self):
        return weekly_brief_view(SimpleNamespace(config=SimpleNamespace(
            state_dir=self.state, core_db=self.state / "core.sqlite")))

    def deliver(self):
        return LocalWeeklyBriefDelivery(
            _AuthorityClient(self.weekly, self.agenda),
            endpoint_ref=self.plan["destination_ref"],
            directory=self.state / "weekly-briefs").run_once(now=self.now)

    def test_normal_cycle_delivers_exact_progress_body_and_replays_once(self):
        first = self.cycle()
        self.assertEqual("ready", first["status"])
        issue = self.weekly.issue(first["issue_version_ref"])
        rendered = self.weekly.render_markdown(issue["id"])
        self.assertIn("研究进展周报", rendered["body"])
        self.assertEqual(1, len(self.agenda.pending_outbox()))
        self.assertEqual(1, self.deliver()["delivered"])
        written = list((self.state / "weekly-briefs").glob("*.md"))
        self.assertEqual([rendered["body"]], [p.read_text() for p in written])
        delivery = self.view()["deliveries"][0]
        self.assertEqual(issue["id"], delivery["issue_version_ref"])
        self.assertEqual("file:" + hashlib.sha256(written[0].read_bytes()).hexdigest(),
                         delivery["external_message_ref"])
        cycle = self.view()["cycles"][0]
        self.assertEqual("mission_progress", cycle["brief_kind"])
        self.assertEqual("研究进展周报", cycle["brief_kind_label"])
        again = self.cycle()
        self.assertEqual(issue["id"], again["issue_version_ref"])
        self.assertEqual(0, self.deliver()["delivered"])
        self.assertEqual(1, len(self.view()["cycles"]))

    def test_an_admitted_progress_slot_is_shared_with_a_new_research_plan(self):
        first = self.cycle()
        before = [tuple(row) for row in self.store.connection.execute(
            "SELECT * FROM weekly_brief_progress_issues")]
        # Even a replacement pinned plan cannot create another admission in
        # this slot. Resolving its named future research sources is not due
        # until the next slot; the existing slot must replay first.
        research = {key: value for key, value in self.plan.items()
                    if key not in {"evidence_refresh", "mission_progress"}}
        research.update({"schema_version": "0.1", "plan_ref": "weekly-brief-plan:replacement:v2",
                         "evidence_pack_version_id": "industry-evidence-pack-version:future",
                         "company_overlay_version_ids": ["company-overlay-version:future"]})
        second = self.cycle(research)
        self.assertEqual("already_issued", second["status"])
        self.assertEqual(first["issue_version_ref"], second["issue_version_ref"])
        self.assertEqual(before, [tuple(row) for row in self.store.connection.execute(
            "SELECT * FROM weekly_brief_progress_issues")])
        self.assertEqual(1, len(self.agenda.pending_outbox()))

    def test_actual_claim_record_is_frozen_and_tampering_cannot_replay(self):
        from dalton_core.weekly_brief import WeeklyBriefConflict
        from tests.test_industry_research import invocation

        self.store.register_invocation(invocation("invocation:progress-source"))
        company = self.mission["universe"][0]["company_ref"]
        claim = self.store.register_claim({
            "claim_ref": "claim:progress:source", "subject_ref": company,
            "metric_or_aspect": "aspect:synthetic-source", "period": "synthetic",
            "basis": "source-commentary", "normalized_statement": "Synthetic source observation.",
            "claim_kind": "qualitative", "value": None, "unit": None,
            "producer_invocation_refs": ["invocation:progress-source"],
            "actor_ref": "automation:researcher"})
        result = self.cycle()
        issue = self.weekly.issue(result["issue_version_ref"])
        entries = issue["snapshot"]["sources"]["claims"]["entries"]
        self.assertEqual([claim["claim_version_id"]], [entry["ref"] for entry in entries])
        self.assertEqual([company], [entry["company_ref"] for entry in entries])
        # The corrupt fixture has not gained a new claim version: byte drift
        # in the old held record must invalidate this already frozen issue.
        connection = self.store.connection
        for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='claim_versions'"):
            connection.execute(f'DROP TRIGGER "{row[0]}"')
        connection.execute("UPDATE claim_versions SET claim_json=claim_json || ' ' WHERE claim_version_id=?",
                           (claim["claim_version_id"],))
        with self.assertRaises(WeeklyBriefConflict):
            self.weekly.issue(issue["id"])

    @unittest.skipIf(shutil.which("node") is None, "node is not installed")
    def test_local_receipt_is_visible_in_the_real_settings_page(self):
        from tests.test_cockpit_v2_migration import fixtures, run

        result = self.cycle()
        self.deliver()
        view = self.view()
        document = weekly_brief_document(SimpleNamespace(config=SimpleNamespace(
            state_dir=self.state, core_db=self.state / "core.sqlite")),
            {"issue_version_ref": result["issue_version_ref"]})
        fixture = fixtures(**{"/v1/cockpit/weekly-brief": view,
                              "/v1/cockpit/weekly-brief-document": document})
        result = run({"settings": "#/settings"},
                     [{"page": "settings", "op": "click", "text": "查看周报"},
                      {"page": "settings", "op": "wait"}], fixtures=fixture)
        text = result["results"]["settings:after"]["text"]
        for expected in ("研究进展周报", "类型", "只在工作台保存", "已保存", "关闭周报", "研究目标", "资料缺项"):
            self.assertIn(expected, text)

    def test_document_route_is_exact_read_only_and_refuses_files_or_other_scope(self):
        from dalton_core.agenda_control import AgendaControlApplication
        from dalton_core.cockpit_plane import CockpitError

        result = self.cycle()
        before = [tuple(row) for row in self.store.connection.execute("SELECT * FROM weekly_brief_progress_issues")]
        plane = SimpleNamespace(config=SimpleNamespace(state_dir=self.state,
                    core_db=self.state / "core.sqlite", mission_ref=self.mission["mission_ref"]))
        app = AgendaControlApplication(None, None, cockpit_plane=plane)
        document = app.cockpit_view("/v1/cockpit/weekly-brief-document", OWNER,
                                   {"issue_version_ref": result["issue_version_ref"]})
        self.assertEqual(self.weekly.render_markdown(result["issue_version_ref"])["body"], document["body"])
        for query in ({"issue_version_ref": "../../core.sqlite"},
                      {"issue_version_ref": "weekly-brief-progress-version:../../core.sqlite"},
                      {"issue_version_ref": result["issue_version_ref"], "path": "/etc/passwd"},
                      {"path": "/etc/passwd"}):
            with self.assertRaises(CockpitError):
                app.cockpit_view("/v1/cockpit/weekly-brief-document", OWNER, query)
        plane.config.mission_ref = "coverage-mission:other"
        with self.assertRaises(CockpitError):
            app.cockpit_view("/v1/cockpit/weekly-brief-document", OWNER,
                            {"issue_version_ref": result["issue_version_ref"]})
        self.assertEqual(before, [tuple(row) for row in self.store.connection.execute("SELECT * FROM weekly_brief_progress_issues")])

    @unittest.skipIf(shutil.which("node") is None, "node is not installed")
    def test_document_text_cannot_inject_html_and_unbuilt_cycles_have_no_view_button(self):
        from tests.test_cockpit_v2_migration import fixtures, run

        result = self.cycle()
        view = self.view()
        document = {"issue_version_ref": result["issue_version_ref"], "title": "研究进展周报",
                    "period": {"start": "2026-10-01", "end": "2026-10-08"},
                    "body": "# 合成周报\n\n<script>alert(1)</script>\n\n[假链接](javascript:alert(1))\n"}
        fixture = fixtures(**{"/v1/cockpit/weekly-brief": view,
                              "/v1/cockpit/weekly-brief-document": document})
        result = run({"s": "#/settings"}, [{"page": "s", "op": "click", "text": "查看周报"},
                      {"page": "s", "op": "wait"}], fixtures=fixture)
        rendered = result["results"]["s:after"]
        self.assertIn("<script>alert(1)</script>", rendered["text"])
        self.assertFalse(any(str(link[1]).startswith("javascript:") for link in result["results"]["s"]["links"]))
        view["cycles"][0]["issue_version_ref"] = None
        fixture = fixtures(**{"/v1/cockpit/weekly-brief": view})
        rendered = run({"s": "#/settings"}, fixtures=fixture)["results"]["s"]
        self.assertNotIn("查看周报", rendered["buttons"])

    def test_old_database_without_progress_tables_remains_readable_and_unmodified(self):
        import sqlite3

        path = self.state / "old.sqlite"
        connection = sqlite3.connect(path)
        connection.executescript(
            "CREATE TABLE weekly_brief_cycle_admissions (cycle_id,plan_ref,scheduled_for,"
            "issue_version_ref,destination_ref);"
            "CREATE TABLE weekly_brief_deliveries (issue_version_ref,destination_ref,"
            "external_message_ref,delivered_at);")
        connection.execute("INSERT INTO weekly_brief_cycle_admissions VALUES (?,?,?,?,?)",
                           ("old-cycle", "old-plan", self.now.isoformat(), "old-issue", "remote"))
        connection.commit()
        connection.close()
        before = path.read_bytes()
        result = _recent(path)
        self.assertEqual([{"cycle_id": "old-cycle", "plan_ref": "old-plan",
                           "scheduled_for": self.now.isoformat(), "issue_version_ref": "old-issue",
                           "destination_ref": "remote"}], result["cycles"])
        self.assertEqual(before, path.read_bytes())

    def test_progress_and_research_history_share_one_limit_and_keep_research_shape(self):
        import sqlite3

        # A pre-migration reader may have either family independently; it
        # must not require the old tables in order to show a progress issue.
        path = self.state / "mixed.sqlite"
        connection = sqlite3.connect(path)
        connection.executescript(
            "CREATE TABLE weekly_brief_progress_cycle_admissions (cycle_id,plan_ref,"
            "scheduled_for,issue_version_ref,destination_ref);"
            "CREATE TABLE weekly_brief_progress_deliveries (issue_version_ref,destination_ref,"
            "external_message_ref,delivered_at);")
        connection.execute("INSERT INTO weekly_brief_progress_cycle_admissions VALUES (?,?,?,?,?)",
                           ("progress", "plan", self.now.isoformat(), "issue-progress", "local"))
        connection.execute("INSERT INTO weekly_brief_progress_deliveries VALUES (?,?,?,?)",
                           ("issue-progress", "local", "file:receipt", self.now.isoformat()))
        connection.commit()
        self.assertEqual("mission_progress", _recent(path)["cycles"][0]["brief_kind"])
        self.assertEqual("issue-progress", _recent(path)["deliveries"][0]["issue_version_ref"])
        connection.executescript(
            "CREATE TABLE weekly_brief_cycle_admissions (cycle_id,plan_ref,scheduled_for,"
            "issue_version_ref,destination_ref);"
            "CREATE TABLE weekly_brief_deliveries (issue_version_ref,destination_ref,"
            "external_message_ref,delivered_at);")
        next_week = (self.now + timedelta(days=7)).isoformat()
        connection.execute("INSERT INTO weekly_brief_cycle_admissions VALUES (?,?,?,?,?)",
                           ("research", "plan2", next_week, "issue-research", "remote"))
        connection.execute("INSERT INTO weekly_brief_deliveries VALUES (?,?,?,?)",
                           ("issue-research", "remote", "message:receipt", next_week))
        connection.commit()
        connection.close()
        result = _recent(path, limit=1)
        self.assertEqual(["research"], [r["cycle_id"] for r in result["cycles"]])
        self.assertEqual(["issue-research"], [r["issue_version_ref"] for r in result["deliveries"]])
        self.assertNotIn("brief_kind", result["cycles"][0])


class ExistingResearchSlotTests(unittest.TestCase):
    def setUp(self):
        from tests.test_weekly_brief_coordinator import WeeklyBriefCoordinatorTests

        self.fixture = WeeklyBriefCoordinatorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def progress_plan(self):
        research = self.fixture.plan(destination_ref="cockpit:weekly-brief:us-it-services")
        value = {
            key: item for key, item in research.items()
            if key not in {"evidence_pack_version_id", "company_overlay_version_ids"}}
        value.update({
            "schema_version": "0.3", "plan_ref": "weekly-brief-plan:us-it-services:v2",
            "mission_progress": {"mission_ref": "coverage-mission:us-it-services"},
            "evidence_refresh": {
                "evidence_pack_ref": "industry-evidence-pack:us-it-services",
                "company_overlay_refs": ["company-overlay:acn"], "claim_window_days": 120}})
        return value

    def test_an_existing_research_issue_is_not_duplicated_by_progress_plan(self):
        fixture = self.fixture
        research = fixture.plan(destination_ref="cockpit:weekly-brief:us-it-services")
        fixture.authorize(research)
        first = fixture.execute(research)
        old_rows = [tuple(row) for row in fixture.fixture.store.connection.execute(
            "SELECT * FROM weekly_brief_issue_versions")]
        second = fixture.execute(self.progress_plan())
        self.assertEqual("already_issued", second["status"])
        self.assertEqual(first["issue_version_ref"], second["issue_version_ref"])
        self.assertEqual(old_rows, [tuple(row) for row in fixture.fixture.store.connection.execute(
            "SELECT * FROM weekly_brief_issue_versions")])
        self.assertEqual(1, len(fixture.agenda.pending_outbox()))

    def test_a_legal_research_template_has_priority_over_progress(self):
        fixture = self.fixture
        plan = self.progress_plan()
        fixture.authorize(plan)
        result = fixture.execute(plan)
        self.assertEqual("ready", result["status"])
        self.assertTrue(result["issue_version_ref"].startswith("weekly-brief-version:"))
        self.assertEqual(0, fixture.fixture.store.connection.execute(
            "SELECT COUNT(*) FROM weekly_brief_progress_issues").fetchone()[0])

    def test_old_remote_research_issue_is_readable_through_the_same_document_reader(self):
        from dalton_core.research_constitution import ResearchConstitutionAuthority
        from dalton_core.research_playbook import ResearchPlaybookAuthority
        from tests.p9a_fixtures import constitution_method, playbook_params

        fixture = self.fixture
        store = fixture.fixture.store
        mandate = fixture.agenda.create_mandate(
            "mandate:weekly-reader", actor_ref=OWNER, objective="Synthetic coverage scope.",
            scope_refs=[fixture.pack["industry_ref"]], constraints={}, success_criteria={},
            effective_from="2026-08-20T00:00:00+00:00", effective_until=None,
            version_id="mandate-version:weekly-reader:1", idempotency_key="weekly-reader-mandate")
        policy = store.active_policy_version()
        constitution = ResearchConstitutionAuthority(store).publish_constitution(
            "constitution:weekly-reader", industry_ref=fixture.pack["industry_ref"], title="Synthetic reader scope",
            bindings={"mandate_version": {"ref": mandate["id"], "hash": mandate["content_hash"]},
                      "driver_pack_version": {"ref": fixture.fixture.driver_pack["id"], "hash": fixture.fixture.driver_pack["content_hash"]},
                      "governance_policy_version": {"ref": policy.id, "hash": policy.content_hash},
                      "doctrine_pack_version": None, "weekly_brief_plan": None},
            method=constitution_method(), actor_ref=OWNER, version_id="constitution-version:weekly-reader:1",
            prior_version_ref=None, idempotency_key="weekly-reader-constitution")
        values = playbook_params()
        playbook = ResearchPlaybookAuthority(store).publish_playbook(values.pop("playbook_ref"), **values)
        values = mission_params({"mandate": mandate, "constitution": constitution, "playbook": playbook})
        mission = CoverageMissionAuthority(store).create_mission(values.pop("mission_ref"), **values)
        plan = fixture.plan()  # Actual remote destination remains in this isolated fixture.
        fixture.authorize(plan)
        result = fixture.execute(plan)
        plane = SimpleNamespace(config=SimpleNamespace(core_db=Path(store.path),
                    mission_ref=mission["mission_ref"], state_dir=Path("/unused")))
        # The fixture uses memory; take a consistent read-only disk snapshot
        # instead of constructing an authority against the read connection.
        with tempfile.TemporaryDirectory() as temp:
            import sqlite3
            core = Path(temp) / "core.sqlite"
            target = sqlite3.connect(core)
            store.connection.backup(target)
            target.close()
            plane.config.core_db = core
            document = weekly_brief_document(plane, {"issue_version_ref": result["issue_version_ref"]})
        self.assertEqual("research", document["brief_kind"])
        self.assertEqual(fixture.weekly.render_markdown(result["issue_version_ref"])["body"], document["body"])

    @unittest.skipIf(shutil.which("node") is None, "node is not installed")
    def test_manual_owner_issue_is_visible_without_a_cycle_or_fake_delivery(self):
        import sqlite3
        from tests.test_cockpit_v2_migration import fixtures, run

        fixture = self.fixture
        issue = fixture.weekly.publish_issue(
            "weekly-brief:manual", period_start="2026-08-20T12:00:00+00:00",
            period_end="2026-08-27T12:00:00+00:00", evidence_pack_version_id=fixture.pack["id"],
            company_overlay_version_ids=[fixture.overlay["id"]], company_thesis_refs={},
            actor_ref=OWNER, version_id="weekly-brief-version:manual-owner:1",
            prior_version_ref=None, idempotency_key="manual-owner-issue")
        with tempfile.TemporaryDirectory() as temp:
            core = Path(temp) / "core.sqlite"
            connection = sqlite3.connect(core)
            fixture.fixture.store.connection.backup(connection)
            connection.close()
            view = _recent(core)
        self.assertEqual(1, len(view["cycles"]))
        self.assertEqual([], view["deliveries"])
        self.assertTrue(view["cycles"][0]["manual"])
        self.assertEqual(issue["id"], view["cycles"][0]["issue_version_ref"])
        result = run({"s": "#/settings"}, fixtures=fixtures(**{
            "/v1/cockpit/weekly-brief": {"enabled": True, "plan": None, "local_briefs": [], **view}}))
        text = result["results"]["s"]["text"]
        for expected in ("已生成", "未安排发送", "报告窗口结束", "查看周报"):
            self.assertIn(expected, text)
        self.assertNotIn("有周报尚未送达", text)

    @unittest.skipIf(shutil.which("node") is None, "node is not installed")
    def test_failed_document_read_can_be_closed_without_publishing_or_leaking_errors(self):
        from tests.test_cockpit_v2_migration import fixtures, run

        view = {"enabled": True, "plan": None, "local_briefs": [], "deliveries": [],
                "cycles": [{"issue_version_ref": "weekly-brief-version:unavailable", "scheduled_for": "2026-08-27T12:00:00+00:00",
                            "destination_ref": "cockpit:weekly-brief:synthetic"}]}
        result = run({"s": "#/settings"}, [{"page": "s", "op": "click", "text": "查看周报"},
                      {"page": "s", "op": "wait"}], fixtures=fixtures(**{"/v1/cockpit/weekly-brief": view}))
        self.assertIn("关闭周报", result["results"]["s"]["buttons"])
        self.assertIn("周报暂时读不出来", result["results"]["s:after"]["text"])
        self.assertEqual([], result["posted"])

    def test_corrupt_research_template_does_not_become_progress(self):
        from dalton_core.weekly_brief import WeeklyBriefError
        from dalton_core.weekly_brief_coordinator import WeeklyBriefCoordinatorError
        from dalton_core.industry_research import IndustryResearchError

        fixture = self.fixture
        plan = self.progress_plan()
        fixture.authorize(plan)
        connection = fixture.fixture.store.connection
        # Deliberately corrupt only this disposable test authority, to prove
        # that fallback is not an integrity-error recovery mechanism.
        connection.execute("DROP TRIGGER industry_evidence_pack_versions_no_update")
        connection.execute("UPDATE industry_evidence_pack_versions SET content_hash=? WHERE version_id=?",
                           ("0" * 64, fixture.pack["id"]))
        with self.assertRaises((WeeklyBriefError, WeeklyBriefCoordinatorError, IndustryResearchError)):
            fixture.execute(plan)
        self.assertEqual(0, connection.execute(
            "SELECT COUNT(*) FROM weekly_brief_progress_issues").fetchone()[0])
        self.assertEqual([], fixture.agenda.pending_outbox())

    def test_a_template_without_its_bound_overlay_does_not_become_progress(self):
        from dalton_core.weekly_brief_coordinator import WeeklyBriefCoordinatorError

        fixture = self.fixture
        plan = self.progress_plan()
        # A nonempty pack exists, but the requested overlay has never been
        # authorized. This is incomplete research authority, not no template.
        plan["evidence_refresh"]["company_overlay_refs"] = ["company-overlay:unbound"]
        fixture.authorize(plan)
        with self.assertRaises(WeeklyBriefCoordinatorError):
            fixture.execute(plan)
        self.assertEqual(0, fixture.fixture.store.connection.execute(
            "SELECT COUNT(*) FROM weekly_brief_progress_issues").fetchone()[0])
        self.assertEqual([], fixture.agenda.pending_outbox())


if __name__ == "__main__":
    unittest.main()
