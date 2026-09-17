"""The parity table, the audit and the repair planner, on temp dirs only.

Every path here is a temp directory. Nothing in this file may touch
``~/Library/Application Support/Dalton``, ``~/.dalton`` or ``/Volumes``: the
whole point of the module under test is that it stages files into a live
environment, so a test that resolved a live path would do exactly that.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.workspace_lane_parity import (
    MISSION_CROWD_MAP_NAME,
    MISSION_FEED_PLAN_NAME,
    MISSION_GUIDEPOINT_PLAN_NAME,
    apply_parity_actions,
    audit_lanes,
    build_mission_crowd_map,
    build_mission_feed_plan,
    build_mission_guidepoint_plan,
    host_provisioned_source_refs,
    lane_parity_table,
    plan_parity_actions,
    read_active_mission,
    render_audit,
    resolve_host_sources,
    unknown_universe_tickers,
    writer_lane_flags,
)

MISSION = {
    "mission_ref": "coverage-mission:ws-test",
    "industry_ref": "industry:us-it-services",
    "title": "US IT services first coverage",
    "objective": "Understand demand, pricing and delivery cost for US IT services.",
    "actor_ref": "human:owner@example.com",
    "budget": {"max_alphaengine_calls_24h": 12, "max_daily_paid_calls": 40},
    "universe": [
        {"company_ref": "company:sec-cik:0001467373", "ticker": "ACN"},
        {"company_ref": "company:sec-cik:0001058290", "ticker": "CTSH"},
    ],
    "source_plan": [
        {"source_ref": "source:guidepoint", "status": "connected", "role": "x"},
        {"source_ref": "source:sales-notes", "status": "connected", "role": "x"},
        {"source_ref": "source:company-wiki", "status": "connected", "role": "x"},
        {"source_ref": "source:xueqiu", "status": "not_connected", "role": "x"},
    ],
}


def _host_tree(root: Path) -> dict[str, Path]:
    """A pretend host: the two OpenClaw corpora, the crowd tools and grants."""

    openclaw = root / "openclaw-workspace"
    (openclaw / "skills" / "market-digest" / "output").mkdir(parents=True)
    (openclaw / "wiki").mkdir(parents=True)
    (openclaw / "wiki" / "vectors.db").write_text("", encoding="utf-8")
    prior = root / "prior-research"
    prior.mkdir()
    tools = root / "tools"
    tools.mkdir()
    grants = root / "grants"
    grants.mkdir()
    sources: dict[str, Path] = {
        "feeds/company-wiki": openclaw,
        "feeds/market-digest-output": openclaw / "skills" / "market-digest" / "output",
        "feeds/prior-research": prior,
    }
    for name in ("xueqiu", "xreach", "xueqiu-hot-rank"):
        tool = tools / name
        tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(tool, 0o700)
        sources[f"host-tools/{name}"] = tool
    for name in ("xueqiu", "xreach"):
        grant = grants / f"{name}.json"
        grant.write_text("{}", encoding="utf-8")
        sources[f"credential-grants/{name}.json"] = grant
    return sources


class LaneParityTableTests(unittest.TestCase):
    def test_every_flag_a_lane_claims_is_a_flag_its_fragment_emits(self):
        """The table's promise is that these flags appear once the files do.

        Checked against ``lane_argv`` rather than against a second list, so a
        lane that renames a flag breaks this test instead of silently making
        the audit report a lane as unconfigured for ever.
        """

        with tempfile.TemporaryDirectory(dir="/tmp") as raw:
            root = Path(raw)
            state = root / "state"
            state.mkdir()
            sources = _host_tree(root)
            actions = plan_parity_actions(
                state, actor_ref="human:owner@example.com",
                host_sources=sources, mission=MISSION)
            apply_parity_actions(actions, actor_ref="human:owner@example.com",
                                 mission=MISSION)
            flags = writer_lane_flags(state)
            for lane in lane_parity_table():
                missing = [flag for flag in lane.flags if flag not in flags]
                self.assertEqual(missing, [], f"{lane.key} is missing {missing}")

    def test_the_hkex_buyback_tape_is_never_staged(self):
        """install.sh leaves it proposed on purpose; parity must not undo that."""

        staged = {name for lane in lane_parity_table() for name in lane.governance}
        self.assertNotIn("hkex-filings-daily-buyback-tape-v1.json", staged)

    def test_a_source_is_offered_only_when_its_host_input_exists(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as raw:
            sources = _host_tree(Path(raw))
            self.assertIn("source:xueqiu", host_provisioned_source_refs(sources))
            self.assertIn("source:sales-notes", host_provisioned_source_refs(sources))
            without_tools = {k: v for k, v in sources.items()
                             if not k.startswith("host-tools/")}
            self.assertNotIn("source:xueqiu",
                             host_provisioned_source_refs(without_tools))
            # A lane that never asks the mission anything -- public market
            # data, public filing disclosure -- contributes no source ref at
            # all. Naming its source in a mission's source plan would be an
            # authority statement that buys nothing, which is the difference
            # this function exists to keep.
            self.assertEqual(host_provisioned_source_refs({}), [])


class HostSourceResolutionTests(unittest.TestCase):
    def test_environment_wins_over_another_environment_and_the_default(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as raw:
            root = Path(raw)
            declared = root / "declared-prior"
            declared.mkdir()
            other_state = root / "other" / "dalton-core"
            (other_state / "feeds").mkdir(parents=True)
            (other_state / "feeds" / "prior-research").mkdir()
            resolved = resolve_host_sources(
                environ={"DALTON_PRIOR_RESEARCH_DIR": str(declared),
                         "DALTON_OPENCLAW_WORKSPACE": str(root / "absent")},
                source_state_dir=other_state)
            self.assertEqual(resolved["feeds/prior-research"], declared.resolve())

    def test_an_absent_host_input_is_absent_rather_than_invented(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as raw:
            resolved = resolve_host_sources(
                environ={"DALTON_OPENCLAW_WORKSPACE": str(Path(raw) / "nothing")},
                home=Path(raw))
            self.assertEqual(resolved, {})

    def test_a_tool_that_is_not_executable_is_not_a_tool(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as raw:
            root = Path(raw)
            state = root / "dalton-core"
            (state / "host-tools").mkdir(parents=True)
            (state / "host-tools" / "xueqiu").write_text("x", encoding="utf-8")
            resolved = resolve_host_sources(
                environ={"DALTON_OPENCLAW_WORKSPACE": str(root / "absent")},
                home=root, source_state_dir=state)
            self.assertNotIn("host-tools/xueqiu", resolved)


class MissionPlanTests(unittest.TestCase):
    def test_the_feed_plan_is_this_mission_s_universe_and_validates(self):
        from dalton_core.mission_feed_lane import validate_feed_discovery_plan

        plan = build_mission_feed_plan(MISSION)
        validate_feed_discovery_plan(plan)
        self.assertEqual(sorted(plan["companies"]),
                         ["company:sec-cik:0001058290", "company:sec-cik:0001467373"])
        self.assertEqual(plan["mission_ref"], MISSION["mission_ref"])

    def test_the_guidepoint_plan_asks_the_mission_s_own_objective(self):
        plan = build_mission_guidepoint_plan(MISSION)
        self.assertEqual(plan["mission_ref"], MISSION["mission_ref"])
        self.assertEqual([spec["query"] for spec in plan["industry_specs"]],
                         [MISSION["objective"]])
        self.assertIn(plan["industry_anchor_company_ref"], plan["companies"])

    def test_the_crowd_map_leaves_the_two_underivable_fields_out(self):
        from dalton_core.mission_crowd_source_lane import load_crowd_source_map

        with tempfile.TemporaryDirectory(dir="/tmp") as raw:
            path = Path(raw) / "map.json"
            path.write_text(json.dumps(build_mission_crowd_map(MISSION)),
                            encoding="utf-8")
            loaded = load_crowd_source_map(path)
            for entry in loaded["companies"]:
                self.assertTrue(entry.get("xueqiu_query"))
                self.assertFalse(entry.get("x_handles"))
                self.assertFalse(entry.get("employer_slug"))

    def test_a_mission_with_no_universe_yields_no_plan(self):
        from dalton_core.workspace_lane_parity import LaneParityError

        with self.assertRaises(LaneParityError):
            build_mission_feed_plan({**MISSION, "universe": []})


class ParityPlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.sources = _host_tree(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def _plan(self, **kwargs):
        return plan_parity_actions(
            self.state, actor_ref="human:owner@example.com",
            host_sources=self.sources, mission=MISSION, **kwargs)

    def test_the_dry_run_list_is_exactly_what_apply_performs(self):
        actions = self._plan()
        performed = apply_parity_actions(
            actions, actor_ref="human:owner@example.com", mission=MISSION)
        self.assertEqual([row["target"] for row in performed],
                         [action.target for action in actions])
        for action in actions:
            self.assertTrue(Path(action.target).exists()
                            or Path(action.target).is_symlink())

    def test_applying_twice_adds_nothing(self):
        apply_parity_actions(self._plan(), actor_ref="human:owner@example.com",
                             mission=MISSION)
        self.assertEqual(self._plan(), [])

    def test_a_record_the_owner_left_proposed_is_not_overwritten(self):
        from dalton_core.connector_governance import build_governance_record

        directory = self.state / "connector-governance"
        directory.mkdir(parents=True)
        target = directory / "yfinance-calendar-v1.json"
        proposed = build_governance_record(
            "yfinance-calendar", approved_by="human:owner@example.com",
            status="proposed")
        target.write_text(json.dumps(proposed), encoding="utf-8")
        apply_parity_actions(self._plan(), actor_ref="human:owner@example.com",
                             mission=MISSION)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["status"],
                         "proposed")

    def test_a_link_points_at_the_host_and_is_not_a_copy(self):
        apply_parity_actions(self._plan(), actor_ref="human:owner@example.com",
                             mission=MISSION)
        link = self.state / "feeds" / "company-wiki"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), self.sources["feeds/company-wiki"].resolve())

    def test_one_lane_may_be_repaired_alone(self):
        actions = self._plan(lanes=["catalyst_calendar"])
        self.assertEqual([Path(action.target).name for action in actions],
                         ["yfinance-calendar-v1.json"])

    def test_a_missing_host_input_stages_nothing_for_that_lane(self):
        without = {k: v for k, v in self.sources.items()
                   if k != "feeds/market-digest-output"}
        actions = plan_parity_actions(
            self.state, actor_ref="human:owner@example.com",
            host_sources=without, mission=MISSION, lanes=["sales_notes"])
        self.assertEqual(actions, [])

    def test_the_approved_record_names_this_workspace_s_owner(self):
        apply_parity_actions(self._plan(), actor_ref="human:owner@example.com",
                             mission=MISSION)
        record = json.loads(
            (self.state / "connector-governance" / "yfinance-daily-prices-v1.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "approved")
        self.assertEqual(record["approved_by"], "human:owner@example.com")


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.sources = _host_tree(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_a_blank_state_reports_every_lane_unconfigured_with_a_reason(self):
        report = audit_lanes(self.state, host_sources={})
        self.assertEqual(report["configured_lanes"], [])
        for row in report["lanes"]:
            self.assertTrue(row["blockers"], row["key"])
        page = render_audit(report)
        self.assertIn("未装", page)

    def test_after_repair_every_host_supplied_lane_is_configured(self):
        apply_parity_actions(
            plan_parity_actions(self.state, actor_ref="human:owner@example.com",
                                host_sources=self.sources, mission=MISSION),
            actor_ref="human:owner@example.com", mission=MISSION)
        report = audit_lanes(self.state, host_sources=self.sources)
        self.assertEqual(report["unconfigured_lanes"], [])

    def test_a_lane_whose_mission_never_granted_its_source_says_so(self):
        _write_mission_db(self.state, MISSION)
        apply_parity_actions(
            plan_parity_actions(self.state, actor_ref="human:owner@example.com",
                                host_sources=self.sources, mission=MISSION),
            actor_ref="human:owner@example.com", mission=MISSION)
        report = audit_lanes(self.state, host_sources=self.sources)
        crowd = next(row for row in report["lanes"] if row["key"] == "crowd_source")
        self.assertTrue(any("source:xueqiu" in blocker for blocker in crowd["blockers"]))
        self.assertTrue(crowd["owner_inputs"])

    def test_a_company_nobody_has_named_is_reported_and_skips_only_its_plan(self):
        """The feed plan cannot be built, and the other 29 actions still are."""

        other = {**MISSION, "universe": [
            {"company_ref": "company:ticker:msft", "ticker": "MSFT"}]}
        self.assertEqual(unknown_universe_tickers(other), ["MSFT"])
        _write_mission_db(self.state, other)
        performed = apply_parity_actions(
            plan_parity_actions(self.state, actor_ref="human:owner@example.com",
                                host_sources=self.sources, mission=other),
            actor_ref="human:owner@example.com", mission=other)
        skipped = [row for row in performed if row["result"] == "skipped"]
        self.assertEqual([row["kind"] for row in skipped], ["mission_plan"])
        self.assertIn("MSFT", skipped[0]["detail"])
        self.assertTrue(any(row["result"] == "approved" for row in performed))
        report = audit_lanes(self.state, host_sources=self.sources)
        wiki = next(row for row in report["lanes"] if row["key"] == "company_wiki")
        self.assertTrue(any("MSFT" in blocker for blocker in wiki["blockers"]))

    def test_a_named_universe_produces_a_plan_and_clears_the_blocker(self):
        """The four hyperscalers, named, run the feed lanes end to end."""

        from dalton_core.mission_company_names import mission_name_table
        from dalton_core.mission_feed_lane import (
            attribute_notes, plan_company_names, triage_notes,
        )

        universe = [{"company_ref": f"company:ticker:{t.lower()}", "ticker": t}
                    for t in ("AMZN", "GOOGL", "META", "MSFT")]
        names = {"AMZN": ["Amazon.com"], "GOOGL": ["Alphabet"],
                 "META": ["Meta Platforms"], "MSFT": ["Microsoft"]}
        other = {**MISSION, "universe": universe,
                 "industry_ref": "industry:us-hyperscalers",
                 "title": "美国 Hyperscaler 初次覆盖"}
        _write_mission_db(self.state, other)
        performed = apply_parity_actions(
            plan_parity_actions(self.state, actor_ref="human:owner@example.com",
                                host_sources=self.sources, mission=other),
            actor_ref="human:owner@example.com", mission=other,
            company_names=names)
        self.assertEqual([row for row in performed if row["result"] == "skipped"], [])
        plan = json.loads((self.state / "feed-plans" / MISSION_FEED_PLAN_NAME)
                          .read_text(encoding="utf-8"))
        self.assertEqual(plan["companies"]["company:ticker:msft"]["names"],
                         ["Microsoft", "MSFT"])
        table = plan_company_names(universe, plan)
        self.assertEqual(mission_name_table(universe, plan), table)
        # The lane can now do the thing it used to refuse: recognise a company
        # in a subject line that never says the ticker.
        note = {"note_id": "n1", "subject": "Microsoft Azure capacity check",
                "sender": "broker@example.com"}
        attributed = attribute_notes([note], universe, table)
        self.assertEqual(attributed["by_company"], {"company:ticker:msft": ["n1"]})
        triaged = triage_notes([note], universe, plan, table)
        self.assertEqual(triaged["header_company"], {"company:ticker:msft": ["n1"]})
        report = audit_lanes(self.state, host_sources=self.sources)
        wiki = next(row for row in report["lanes"] if row["key"] == "company_wiki")
        self.assertEqual([b for b in wiki["blockers"] if "还不知道" in b], [])
        self.assertTrue(wiki["configured"])

    def test_a_stale_plist_is_named_as_stale(self):
        import plistlib

        apply_parity_actions(
            plan_parity_actions(self.state, actor_ref="human:owner@example.com",
                                host_sources=self.sources, mission=MISSION),
            actor_ref="human:owner@example.com", mission=MISSION)
        plist = self.root / "writer.plist"
        with plist.open("wb") as stream:
            plistlib.dump({"ProgramArguments": ["python", "--db", "x"]}, stream)
        report = audit_lanes(self.state, host_sources=self.sources,
                             plist_path=plist)
        self.assertIn("--catalyst-calendar-governance", report["plist_missing_flags"])

    def test_the_audit_writes_nothing_into_the_state_directory(self):
        _write_mission_db(self.state, MISSION)
        before = sorted(path.name for path in self.state.rglob("*"))
        audit_lanes(self.state, host_sources=self.sources)
        self.assertEqual(sorted(path.name for path in self.state.rglob("*")), before)

    def test_the_mission_is_read_back_from_a_read_only_connection(self):
        _write_mission_db(self.state, MISSION)
        self.assertEqual(read_active_mission(self.state)["mission_ref"],
                         MISSION["mission_ref"])

    def test_no_mission_database_is_not_a_failure(self):
        self.assertIsNone(read_active_mission(self.state))


class MissionGeneratedPlanPreferenceTests(unittest.TestCase):
    """A mission-generated plan wins over the packaged us-it-services file."""

    def test_the_feed_lane_prefers_the_mission_plan(self):
        from dalton_core.mission_feed_lane import (
            MISSION_FEED_PLAN_NAME as NAME, resolve_feed_plan,
        )

        with tempfile.TemporaryDirectory(dir="/tmp") as raw:
            state = Path(raw)
            (state / "feed-plans").mkdir()
            packaged = "p9-us-it-services-feeds-v2.json"
            (state / "feed-plans" / packaged).write_text("{}", encoding="utf-8")
            self.assertEqual(resolve_feed_plan(state, packaged).name, packaged)
            (state / "feed-plans" / NAME).write_text("{}", encoding="utf-8")
            self.assertEqual(resolve_feed_plan(state, packaged).name, NAME)

    def test_the_guidepoint_lane_prefers_the_mission_plan(self):
        from dalton_core.mission_guidepoint_lane import (
            GUIDEPOINT_LANE_PLAN, MISSION_GUIDEPOINT_PLAN, resolve_guidepoint_plan,
        )

        with tempfile.TemporaryDirectory(dir="/tmp") as raw:
            state = Path(raw)
            (state / "discovery-plans").mkdir()
            (state / "discovery-plans" / GUIDEPOINT_LANE_PLAN).write_text(
                "{}", encoding="utf-8")
            self.assertEqual(resolve_guidepoint_plan(state).name, GUIDEPOINT_LANE_PLAN)
            (state / "discovery-plans" / MISSION_GUIDEPOINT_PLAN).write_text(
                "{}", encoding="utf-8")
            self.assertEqual(resolve_guidepoint_plan(state).name,
                             MISSION_GUIDEPOINT_PLAN)

    def test_the_crowd_lane_prefers_the_mission_map(self):
        from dalton_core.mission_crowd_source_lane import (
            CROWD_SOURCE_MAP, MISSION_CROWD_SOURCE_MAP, resolve_crowd_source_map,
        )

        with tempfile.TemporaryDirectory(dir="/tmp") as raw:
            state = Path(raw)
            (state / "phase9").mkdir()
            (state / "phase9" / CROWD_SOURCE_MAP).write_text("{}", encoding="utf-8")
            self.assertEqual(resolve_crowd_source_map(state).name, CROWD_SOURCE_MAP)
            (state / "phase9" / MISSION_CROWD_SOURCE_MAP).write_text(
                "{}", encoding="utf-8")
            self.assertEqual(resolve_crowd_source_map(state).name,
                             MISSION_CROWD_SOURCE_MAP)

    def test_the_generated_names_agree_across_the_two_modules(self):
        from dalton_core.mission_crowd_source_lane import MISSION_CROWD_SOURCE_MAP
        from dalton_core.mission_feed_lane import MISSION_FEED_PLAN_NAME as FEED
        from dalton_core.mission_guidepoint_lane import MISSION_GUIDEPOINT_PLAN

        self.assertEqual(FEED, MISSION_FEED_PLAN_NAME)
        self.assertEqual(MISSION_GUIDEPOINT_PLAN, MISSION_GUIDEPOINT_PLAN_NAME)
        self.assertEqual(MISSION_CROWD_SOURCE_MAP, MISSION_CROWD_MAP_NAME)


def _write_mission_db(state: Path, mission: dict) -> None:
    """The two mission tables the audit reads, in the real schema's shape.

    ``CREATE TABLE IF NOT EXISTS`` rather than ``CREATE TABLE`` because the
    fixture is used both on a bare temp directory and on a bootstrapped
    workspace whose core database already has these tables. What is under test
    is that the audit reads the *pointer's* version, so the fixture is the
    pointer and the version row and nothing else.
    """

    connection = sqlite3.connect(state / "core.sqlite")
    # A bootstrapped core database guards these tables with triggers that call
    # the writer's own authorization function. This fixture writes a mission
    # the way the authority would have, so the guard is satisfied rather than
    # bypassed -- and on a bare temp directory the function is simply unused.
    connection.create_function("dalton_coverage_mission_authorized", 0, lambda: 1)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS coverage_mission_versions ("
            "mission_version_id TEXT PRIMARY KEY, mission_ref TEXT NOT NULL,"
            "version_number INTEGER NOT NULL, prior_version_id TEXT,"
            "industry_ref TEXT NOT NULL, playbook_version_ref TEXT NOT NULL,"
            "constitution_version_ref TEXT NOT NULL, mandate_version_ref TEXT NOT NULL,"
            "record_json TEXT NOT NULL, content_hash TEXT NOT NULL,"
            "actor_ref TEXT NOT NULL, created_at TEXT NOT NULL)")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS coverage_mission_pointer ("
            "mission_ref TEXT PRIMARY KEY, mission_version_id TEXT NOT NULL,"
            "version_number INTEGER NOT NULL, content_hash TEXT NOT NULL,"
            "updated_at TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO coverage_mission_versions (mission_version_id, mission_ref,"
            " version_number, industry_ref, playbook_version_ref,"
            " constitution_version_ref, mandate_version_ref, record_json,"
            " content_hash, actor_ref, created_at)"
            " VALUES (?, ?, 1, ?, 'p:1', 'c:1', 'm:1', ?, 'h', ?, 't')",
            ("v1", mission["mission_ref"], mission["industry_ref"],
             json.dumps(mission), mission["actor_ref"]))
        connection.execute(
            "INSERT INTO coverage_mission_pointer (mission_ref, mission_version_id,"
            " version_number, content_hash, updated_at) VALUES (?, 'v1', 1, 'h', 't')",
            (mission["mission_ref"],))
        connection.commit()
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()


class RuntimeSetupStagingTests(unittest.TestCase):
    """A workspace created now must come up with the host's lanes already on.

    The end-to-end shape rather than the pieces: create a blank workspace,
    install the foundation against a pretend host, and check that the writer
    the installer would render carries the flags the legacy Core carries.
    """

    def setUp(self):
        from dalton_core.connector_governance import ALPHAENGINE_SEARCH_CAPABILITY_ID
        from dalton_core.store import content_hash
        from dalton_core.workspace_creation import create_blank_workspace

        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.temporary.name)
        self.release = self.root / "release"
        self.release.mkdir()
        body = {"schema_version": "dalton-shared-connection-catalog-0.1",
                "models": [], "sources": [{
                    "id": "connector-profile:alphaengine:1",
                    "connector_ref": "connector:alphaengine-library",
                    "capability_id": ALPHAENGINE_SEARCH_CAPABILITY_ID,
                    "auth_mode": "none", "credential_slot_refs": [],
                    "allowed_operations": ["search_library"],
                    "allowed_hosts": ["alphaengine.example"],
                    "transport": {"kind": "connector", "endpoint_ref": "adapter:ae",
                                  "socket_path": None, "config_path": None}}]}
        catalog = {**body, "content_hash": content_hash(body)}
        self.catalog_path = self.root / "connections.json"
        self.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        self.fleet = self.root / "fleet"
        receipt = create_blank_workspace(
            self.fleet, "blank", 18892, "release:sha256:" + "b" * 64, self.release,
            request_id="request-parity", display_name="Blank",
            shared_readonly_paths=(self.catalog_path, self.release),
            connection_catalog={"path": str(self.catalog_path),
                                "content_hash": catalog["content_hash"]})
        self.manifest = Path(receipt["manifest_path"])
        self.state = Path(receipt["config_path"]).parent.parent / "state/dalton-core"
        self.sources = _host_tree(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_install_stages_the_host_lanes_and_says_what_it_staged(self):
        from dalton_core.workspace_runtime_setup import install

        result = install(self.manifest, actor_ref="human:owner@example.com",
                         host_sources=self.sources)
        staged = result["host_lane_inputs"]["staged"]
        self.assertIn("yfinance-daily-prices-v1.json", staged)
        self.assertIn("sec-form4-transactions-v1.json", staged)
        self.assertIn("xueqiu-search-posts-v1.json", staged)
        flags = writer_lane_flags(self.state)
        for flag in ("--market-price-governance", "--catalyst-calendar-governance",
                     "--consensus-governance", "--sec-ownership-governance-dir",
                     "--hkex-filings-governance-dir", "--market-proxy-config"):
            self.assertIn(flag, flags)
        # The crowd lane's switch is its map, and a map is this mission's
        # companies -- so setup stages its tools, its grants and its contracts
        # and the lane still waits for a mission. That is the boundary between
        # the two halves of this work, asserted rather than assumed.
        self.assertNotIn("--crowd-source-map", flags)
        for name in ("host-tools/xueqiu", "host-tools/xreach",
                     "credential-grants/xueqiu.json"):
            self.assertTrue((self.state / name).is_symlink())

    def test_the_first_mission_may_grant_only_the_sources_this_host_has(self):
        from dalton_core.workspace_runtime_setup import install

        result = install(self.manifest, actor_ref="human:owner@example.com",
                         host_sources=self.sources)
        foundation = json.loads(
            (self.state / "research-foundation.json").read_text(encoding="utf-8"))
        plan = {row["source_ref"] for row in foundation["mission_defaults"]["source_plan"]}
        self.assertIn("source:xueqiu", plan)
        self.assertIn("source:sales-notes", plan)
        # Never a source whose lane does not ask the mission anything.
        self.assertNotIn("source:yahoo-finance", plan)
        self.assertEqual(result["host_lane_inputs"]["source_refs"],
                         sorted(plan - {"source:alphaengine"}))

    def test_a_host_without_the_crowd_tools_grants_no_crowd_source(self):
        from dalton_core.workspace_runtime_setup import install

        without = {k: v for k, v in self.sources.items()
                   if not k.startswith(("host-tools/", "credential-grants/"))}
        install(self.manifest, actor_ref="human:owner@example.com",
                host_sources=without)
        foundation = json.loads(
            (self.state / "research-foundation.json").read_text(encoding="utf-8"))
        plan = {row["source_ref"] for row in foundation["mission_defaults"]["source_plan"]}
        self.assertNotIn("source:xueqiu", plan)
        self.assertNotIn("--crowd-source-map", writer_lane_flags(self.state))

    def test_the_first_mission_generates_the_plans_its_lanes_need(self):
        from dalton_core.workspace import load_workspace_manifest
        from dalton_core.workspace_mission_setup import (
            materialize_first_mission_lane_plans,
        )
        from dalton_core.workspace_runtime_setup import install

        install(self.manifest, actor_ref="human:owner@example.com",
                host_sources=self.sources)
        workspace = load_workspace_manifest(self.manifest)
        mission = {**MISSION, "source_plan": [
            {"source_ref": ref, "status": "connected", "role": "x"}
            for ref in ("source:guidepoint", "source:sales-notes",
                        "source:company-wiki", "source:xueqiu")]}
        written = materialize_first_mission_lane_plans(workspace, mission)
        self.assertEqual(sorted(written), sorted([
            MISSION_CROWD_MAP_NAME, MISSION_FEED_PLAN_NAME,
            MISSION_GUIDEPOINT_PLAN_NAME]))
        flags = writer_lane_flags(self.state)
        for flag in ("--guidepoint-discovery-plan", "--guidepoint-mcp-endpoint",
                     "--feed-discovery-plan", "--sales-notes-digest-dir",
                     "--company-wiki-corpus-root", "--prior-research-corpus-root",
                     "--crowd-source-map", "--crowd-source-xueqiu-tool",
                     "--crowd-source-xreach-credential-grant"):
            self.assertIn(flag, flags)

    def test_a_mission_that_grants_no_feed_source_gets_no_feed_plan(self):
        from dalton_core.workspace import load_workspace_manifest
        from dalton_core.workspace_mission_setup import (
            materialize_first_mission_lane_plans,
        )
        from dalton_core.workspace_runtime_setup import install

        install(self.manifest, actor_ref="human:owner@example.com",
                host_sources=self.sources)
        workspace = load_workspace_manifest(self.manifest)
        mission = {**MISSION, "source_plan": [
            {"source_ref": "source:sec-edgar", "status": "connected", "role": "x"}]}
        self.assertEqual(materialize_first_mission_lane_plans(workspace, mission), {})


class RepairPlannerTests(unittest.TestCase):
    """The dry run has to be a list of files, and the same list apply performs."""

    def setUp(self):
        import importlib.util

        from dalton_core.store import content_hash
        from dalton_core.connector_governance import ALPHAENGINE_SEARCH_CAPABILITY_ID
        from dalton_core.workspace_creation import create_blank_workspace

        spec = importlib.util.spec_from_file_location(
            "repair_workspace_lane_parity",
            Path(__file__).resolve().parents[1] / "scripts"
            / "repair_workspace_lane_parity.py")
        self.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.script)
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.temporary.name)
        self.release = self.root / "release"
        self.release.mkdir()
        body = {"schema_version": "dalton-shared-connection-catalog-0.1",
                "models": [], "sources": [{
                    "id": "connector-profile:alphaengine:1",
                    "connector_ref": "connector:alphaengine-library",
                    "capability_id": ALPHAENGINE_SEARCH_CAPABILITY_ID,
                    "auth_mode": "none", "credential_slot_refs": [],
                    "allowed_operations": ["search_library"],
                    "allowed_hosts": ["alphaengine.example"],
                    "transport": {"kind": "connector", "endpoint_ref": "adapter:ae",
                                  "socket_path": None, "config_path": None}}]}
        catalog = {**body, "content_hash": content_hash(body)}
        catalog_path = self.root / "connections.json"
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        self.fleet = self.root / "fleet"
        receipt = create_blank_workspace(
            self.fleet, "blank", 18893, "release:sha256:" + "c" * 64, self.release,
            request_id="request-repair", display_name="Blank",
            shared_readonly_paths=(catalog_path, self.release),
            connection_catalog={"path": str(catalog_path),
                                "content_hash": catalog["content_hash"]})
        self.state = Path(receipt["config_path"]).parent.parent / "state/dalton-core"
        self.sources = _host_tree(self.root)
        _write_mission_db(self.state, MISSION)

    def tearDown(self):
        self.temporary.cleanup()

    def _plan(self):
        return self.script.build_plan(
            "blank", fleet_root=self.fleet / "workspaces",
            launch_agents_dir=self.root / "agents",
            actor_ref="human:owner@example.com",
            source_state_dir=None, lanes=None)

    def test_the_dry_run_names_every_file_and_its_reason(self):
        import os

        os.environ["DALTON_OPENCLAW_WORKSPACE"] = str(
            self.sources["feeds/company-wiki"])
        try:
            plan = self._plan()
        finally:
            del os.environ["DALTON_OPENCLAW_WORKSPACE"]
        page = self.script.render_plan(plan)
        self.assertIn("yfinance-daily-prices-v1.json", page)
        self.assertIn(MISSION_GUIDEPOINT_PLAN_NAME, page)
        for action in plan["actions"]:
            self.assertTrue(action["reason"])
            self.assertIn(action["target"], page)

    def test_the_dry_run_adds_no_file_to_the_state_directory(self):
        """No new content. SQLite's own -shm/-wal sidecars are not content.

        A read-only connection to a WAL database attaches its shared-memory
        file; that is the operating system, not this planner, and excluding it
        here is more honest than an assertion that would pass only against a
        database nobody had ever written to.
        """

        def content():
            return sorted(str(path) for path in self.state.rglob("*")
                          if not path.name.endswith(("-shm", "-wal")))

        before = content()
        self._plan()
        self.assertEqual(content(), before)

    def test_a_workspace_with_no_mission_is_refused_with_a_sentence(self):
        (self.state / "core.sqlite").unlink()
        with self.assertRaises(self.script.RepairError) as caught:
            self._plan()
        self.assertIn("研究任务", str(caught.exception))

    def test_a_live_workspace_is_told_to_stop_before_it_is_repaired(self):
        """A plist cannot be re-rendered under a running service, so say so."""

        import socket

        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        try:
            self.assertTrue(self.script.cockpit_port_busy(port))
        finally:
            listener.close()
        text = self.script.stop_instructions("blank")
        self.assertIn("space.lumos.dalton.workspace.blank.writer", text)
        self.assertIn("launchctl bootout", text)

    def test_the_restart_instruction_names_this_workspace_s_writer(self):
        text = self.script.restart_instructions("blank")
        self.assertIn("space.lumos.dalton.workspace.blank.writer.plist", text)
        self.assertIn("launchctl bootstrap", text)


class ActorTests(unittest.TestCase):
    def test_a_plan_refuses_to_be_signed_by_an_automation(self):
        from dalton_core.workspace_lane_parity import LaneParityError

        with tempfile.TemporaryDirectory(dir="/tmp") as raw:
            with self.assertRaises(LaneParityError):
                plan_parity_actions(Path(raw), actor_ref="automation:setup",
                                    host_sources={}, mission=None)
