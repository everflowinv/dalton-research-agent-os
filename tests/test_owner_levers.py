"""The owner's levers (vision-gap #11/#12, 2026-10-06): authority, fold, controller, cockpit."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from dalton_core.owner_levers import (
    OwnerLeverAuthority, OwnerLeverConflict, OwnerLeverValidationError, dispatch_scope,
    dispatch_view, lane_refusal, lever_state, lever_state_for_state_dir, spawn_refusal,
    tick_summary, ttl_until,
)
from dalton_core.store import DaltonStore

OWNER = "human:owner"
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
ACME = "company:ticker:acme"


class OwnerLeverAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.store = DaltonStore(self.state / "core.sqlite")
        self.addCleanup(self.store.close)
        self.levers = OwnerLeverAuthority(self.store)

    def pause(self, **extra):
        values = {"lever": "pause", "scope_kind": "company", "scope_ref": ACME,
                  "reason": "earnings", "actor_ref": OWNER, "now": NOW}
        values.update(extra)
        return self.levers.record(**values)

    def test_a_pause_with_ttl_is_in_force_then_expires_without_a_write(self) -> None:
        receipt = self.pause(until=NOW + timedelta(hours=2))
        self.assertEqual(receipt["until"], "2026-10-06T11:00:00Z")
        state = lever_state(self.store.connection, now=NOW + timedelta(hours=1))
        self.assertIn(ACME, state["companies"])
        later = lever_state(self.store.connection, now=NOW + timedelta(hours=3))
        self.assertEqual(later["companies"], {})
        self.assertEqual([r["scope_ref"] for r in later["expired"]], [ACME])
        self.assertEqual(later["receipt_count"], 1)

    def test_the_newest_receipt_per_scope_wins_and_chains_to_the_prior(self) -> None:
        first = self.pause()
        resumed = self.levers.record(lever="resume", scope_kind="company", scope_ref=ACME,
                                     reason="done", actor_ref=OWNER, now=NOW)
        self.assertEqual(resumed["prior_receipt_ref"], first["id"])
        self.assertEqual(lever_state(self.store.connection, now=NOW)["companies"], {})

    def test_the_closed_contract(self) -> None:
        with self.assertRaises(OwnerLeverValidationError):
            self.pause(actor_ref="automation:coverage-mission")
        with self.assertRaises(OwnerLeverValidationError):
            self.pause(until=NOW - timedelta(minutes=1))
        with self.assertRaises(OwnerLeverValidationError):
            self.pause(until=NOW + timedelta(days=31))
        with self.assertRaises(OwnerLeverValidationError):
            self.pause(reason="  ")
        with self.assertRaises(OwnerLeverValidationError):
            self.levers.record(lever="prioritize", scope_kind="company", scope_ref=ACME,
                               reason="x", actor_ref=OWNER, until=NOW + timedelta(hours=1), now=NOW)
        with self.assertRaises(OwnerLeverValidationError):
            self.levers.record(lever="emergency_stop", scope_kind="company", scope_ref=ACME,
                               reason="x", actor_ref=OWNER, now=NOW)

    def test_an_idempotency_key_replays_and_refuses_a_different_request(self) -> None:
        first = self.pause(idempotency_key="k1")
        again = self.pause(idempotency_key="k1")
        self.assertEqual((again["status"], again["id"]), ("replayed", first["id"]))
        with self.assertRaises(OwnerLeverConflict):
            self.pause(idempotency_key="k1", reason="something else")

    def test_receipts_are_append_only(self) -> None:
        self.pause()
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute("UPDATE owner_lever_receipts SET reason='x'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute("DELETE FROM owner_lever_receipts")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute(
                "INSERT INTO owner_lever_receipts(receipt_id,sequence,lever,scope_kind,scope_ref,"
                "reason,outcome,actor_ref,record_json,content_hash,created_at) "
                "VALUES('r',99,'pause','company','c','x','applied','human:x','{}','h','t')")

    def test_readers_without_a_table_or_a_core_see_nothing_paused(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            self.assertEqual(lever_state_for_state_dir(other)["companies"], {})
            sqlite3.connect(Path(other) / "core.sqlite").close()
            self.assertIsNone(lever_state_for_state_dir(other)["emergency"])
        self.assertIsNone(spawn_refusal(None, {"company_ref": ACME}))

    def test_the_emergency_stop_reaches_every_reader(self) -> None:
        self.levers.record(lever="emergency_stop", scope_kind="global", scope_ref="global",
                           reason="runaway", actor_ref=OWNER)
        state = lever_state_for_state_dir(self.state)
        self.assertIsNotNone(state["emergency"])
        refusal = lane_refusal(state, "dispatch_anything")
        self.assertEqual(refusal["reason_code"], "owner_emergency_stop")
        self.assertTrue(refusal["status"].startswith("paused:"))
        summary = tick_summary(state)
        self.assertTrue(summary["emergency_stop"])
        self.assertIn("until resumed", summary["status"])
        self.assertIn("no new lane children", spawn_refusal(self.state, {}))

    def test_nothing_paused_writes_no_tick_row(self) -> None:
        self.assertIsNone(tick_summary(lever_state(self.store.connection)))

    def test_the_dispatch_view_is_scoped_to_one_thread_and_one_block(self) -> None:
        mission = {"universe": [{"company_ref": ACME}, {"company_ref": "company:ticker:b"}]}
        self.assertIs(dispatch_view(mission), mission)
        with dispatch_scope(frozenset({ACME})):
            view = dispatch_view(mission)
        self.assertEqual([m["company_ref"] for m in view["universe"]], ["company:ticker:b"])
        self.assertEqual(len(mission["universe"]), 2)
        self.assertIs(dispatch_view(mission), mission)

    def test_ttl_hours_from_the_page(self) -> None:
        self.assertIsNone(ttl_until(None))
        self.assertIsNone(ttl_until("0"))
        self.assertEqual(ttl_until(24, now=NOW), "2026-10-07T09:00:00Z")
        with self.assertRaises(OwnerLeverValidationError):
            ttl_until(1000, now=NOW)


class FailureClassTests(unittest.TestCase):
    def test_an_owner_stop_is_transient_not_an_outage(self) -> None:
        from dalton_core.lane_failure_class import TRANSIENT, classify

        for text in ("thesis-impact budget refused the call (owner_emergency_stop): refused",
                     "company company:ticker:x paused by owner until 2026-10-07T00:00:00Z (x)",
                     "LaneChildOwnerPaused: lane paused"):
            self.assertEqual(classify(text).failure_class, TRANSIENT, text)


class DriverTests(unittest.TestCase):
    """The controller tick skips SEC dispatch and planner rounds, and records why."""

    class _Client:
        def __init__(self, levers):
            self.levers = levers
            self.calls: list[str] = []

        def call(self, operation, params=None):
            self.calls.append(operation)
            if operation == "owner_lever_state":
                return self.levers
            if operation == "bounded_planner_active_loops":
                return {"loops": [{"loop_version_ref": "bounded-planner-loop-version:1",
                                   "loop_ref": "bounded-loop:1"}]}
            if operation.startswith("bounded_planner_") or operation.startswith("llm_planner"):
                raise AssertionError(f"{operation} must not run during an emergency stop")
            return {"status": "idle"}

    def test_an_emergency_stop_skips_dispatch_and_rounds(self) -> None:
        from dalton_core.bounded_planner_driver import (
            BoundedPlannerDriver, BoundedPlannerDriverConfig,
        )

        with tempfile.TemporaryDirectory() as name:
            store = DaltonStore(Path(name) / "core.sqlite")
            levers = OwnerLeverAuthority(store)
            levers.record(lever="emergency_stop", scope_kind="global", scope_ref="global",
                          reason="runaway", actor_ref=OWNER, until=NOW + timedelta(hours=4), now=NOW)
            state = lever_state(store.connection, now=NOW)
            store.close()
            client = self._Client({**state, "tick": tick_summary(state)})
            config = BoundedPlannerDriverConfig(
                writer_socket=Path(name) / "w.sock", token_config=Path(name) / "t.json",
                scheduler_db=Path(name) / "s.sqlite", user_agent="Dalton Test",
                max_response_bytes=1_000_000, timeout_seconds=10.0, max_probes_per_tick=1,
                filed_window_days=400, observation_mandate_version_ref=None,
                doctrine_pack_version_ref=None, doctrine_pack_version_hash=None,
                planner_routing_policy_ref=None, planner_credential_slot_refs=None,
                planner_model_router_db=None, planner_broker_socket=None,
                planner_broker_auth_key=None, planner_broker_client_id="client:dalton-core",
                planner_expected_agent_id="chem", planner_max_cost_usd=0.5)
            driver = BoundedPlannerDriver(config, client=client, transport=object(),
                                          clock=lambda: NOW)
            result = driver.run_once()
        self.assertNotIn("dispatch_coverage_mission_sec_lane", client.calls)
        self.assertTrue(result["mission_sec_dispatch"]["status"].startswith("paused:"))
        self.assertEqual(result["skipped"][0]["reason"], "paused_by_owner:emergency_stop")
        self.assertIn("emergency stop by owner until 2026-10-06T13:00:00Z",
                      result["owner_levers"]["status"])


class CockpitLeverTests(unittest.TestCase):
    """The page side: the view, the lever posts, the scope_expansion card."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.core = root / "core.sqlite"
        store = DaltonStore(self.core)
        OwnerLeverAuthority(store).record(
            lever="pause", scope_kind="company", scope_ref=ACME, reason="earnings",
            actor_ref=OWNER, until=datetime.now(timezone.utc) + timedelta(hours=5))
        store.close()
        from dalton_core.cockpit_plane import CockpitPlane, _iso

        plane = object.__new__(CockpitPlane)
        plane.config = SimpleNamespace(core_db=self.core, mission_ref=None)
        plane.clock = lambda: datetime.now(timezone.utc)
        self.calls: list[tuple[str, dict]] = []
        self.events: list[dict] = []

        def governance(login, operation, params, *, failure):
            self.calls.append((operation, params))
            return {"status": "recorded", "receipt": {"id": "owner-lever-receipt:1"},
                    "mission_version_ref": "coverage-mission-version:x:2", "version": 2}

        plane._governance = governance
        plane.journal = SimpleNamespace(record_event=lambda **kw: self.events.append(kw),
                                        write=lambda *a, **k: None)
        mission = {"mission_ref": "coverage-mission:x", "id": "coverage-mission-version:x:1",
                   "content_hash": "h" * 64, "universe": [
                       {"company_ref": ACME, "ticker": "ACME", "coverage_tier": "A",
                        "bootstrap_priority": "P0"}]}
        plane._mission = lambda core: mission
        self.mission = mission
        self.plane = plane
        self.iso = _iso

    def test_the_view_says_what_is_paused_in_the_owners_words(self) -> None:
        from dalton_core.cockpit_owner_levers import levers_view

        view = levers_view(self.plane)
        company = view["companies"][0]
        self.assertTrue(company["paused"])
        self.assertIn("已由你暂停", company["paused_label"])
        self.assertIsNone(view["banner"])
        self.assertEqual(view["paused_count"], 1)
        self.assertEqual(view["receipts"][0]["lever_label"], "暂停")
        self.assertTrue(any(row["lane"] == "dispatch_coverage_mission_sec_lane" for row in view["lanes"]))

    def test_each_lever_goes_out_through_the_writer_with_a_reason(self) -> None:
        from dalton_core.cockpit_owner_levers import apply_lever, prioritize, stop_line
        from dalton_core.cockpit_plane import CockpitError

        with self.assertRaises(CockpitError):
            apply_lever(self.plane, "owner@x", {"lever": "pause", "scope_kind": "company",
                                                "scope_ref": ACME, "reason": "", "request_id": "r"})
        apply_lever(self.plane, "owner@x", {"lever": "pause", "scope_kind": "company",
                                            "scope_ref": ACME, "reason": "why", "hours": 6,
                                            "request_id": "r1"})
        apply_lever(self.plane, "owner@x", {"lever": "emergency_stop", "reason": "stop",
                                            "request_id": "r2"})
        stop_line(self.plane, "owner@x", {"line_ref": "mission-document-research-admission:ab",
                                          "reason": "dead end", "request_id": "r3"})
        prioritize(self.plane, "owner@x", {"company_ref": ACME, "reason": "catalyst",
                                           "request_id": "r4"})
        operations = [operation for operation, _ in self.calls]
        self.assertEqual(operations, ["apply_owner_lever", "apply_owner_lever",
                                      "stop_research_line", "prioritize_company"])
        self.assertIn("until", self.calls[0][1])
        self.assertEqual(self.calls[1][1]["scope_ref"], "global")
        self.assertTrue(all(params["idempotency_key"].startswith("cockpit:") for _, params in self.calls))
        self.assertEqual(self.events[0]["title"], "暂停了：ACME")

    def test_the_scope_expansion_card_and_its_publish(self) -> None:
        from dalton_core.cockpit_owner_levers import coverage_approval_item, publish_coverage

        draft = {"kind": "scope_expansion", "summary": "新增 NEWC（Newco）", "reason": "r",
                 "based_on": {"mission_version_ref": self.mission["id"],
                              "mission_version_hash": self.mission["content_hash"]},
                 "additions": [{"ticker": "NEWC", "cik": "0000000003", "name": "Newco",
                                "coverage_tier": "B", "resolution": "resolved",
                                "company_ref": "company:ticker:newc"}],
                 "removals": [], "plans": [{"source_ref": "source:alphaengine",
                                            "added": ["company:ticker:newc"], "removed": []}],
                 "budget": {"max_daily_cost_usd": 100.0, "max_daily_paid_calls": 100,
                            "coverage_pool_usd_per_company": {"before": 55.0, "after": 27.5},
                            "searches_per_day_delta": 0.21}}
        row = {"draft_id": "cockpit-draft:coverage:1", "content_hash": "c" * 64,
               "created_at": "2026-10-06T09:00:00Z"}
        item = coverage_approval_item(row, draft)
        self.assertEqual(item["kind"], "draft:coverage")
        self.assertEqual(item["title"], "待你确认的覆盖名单扩大")
        self.assertIn("SEC CIK 0000000003", item["details"]["add_1"])
        self.assertIn("27.5", item["details"]["budget"])
        out = publish_coverage(self.plane, "owner@x", row["draft_id"], draft)
        self.assertEqual(out["status"], "published")
        operation, params = self.calls[-1]
        self.assertEqual(operation, "publish_coverage_change")
        self.assertEqual(params["add"], [{"ticker": "NEWC", "coverage_tier": "B",
                                          "cik": "0000000003", "name": "Newco"}])
        self.assertEqual(params["expected_mission_hash"], "h" * 64)
        self.assertEqual(params["idempotency_key"], row["draft_id"])

    def test_the_page_has_the_banner_and_the_levers(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "src/dalton_core/cockpit_control.html").read_text()
        for needle in ('id="lever-banner"', '"/v1/cockpit/lever"', '"/v1/cockpit/stop_line"',
                       '"/v1/cockpit/prioritize"', '"/v1/cockpit/coverage_draft"',
                       '"/v1/cockpit/levers"', "紧急停机", "恢复全部运行", "优先看这家",
                       "调整覆盖名单", "停止这条研究线"):
            self.assertIn(needle, html)


class CoverageListChangeTests(unittest.TestCase):
    def test_a_cik_named_universe_needs_a_cik_and_plans_keep_their_specs(self) -> None:
        from dalton_core.coverage_list_change import (
            CoverageListChangeError, new_universe, normalize_request, plan_after,
            resolve_additions,
        )
        from dalton_core.mission_source_discovery import build_discovery_plan

        mission = {"universe": [{"company_ref": "company:sec-cik:0000000001", "ticker": "AAA",
                                 "coverage_tier": "A", "bootstrap_priority": "P0"}],
                   "budget": {"max_daily_paid_calls": 10, "max_daily_cost_usd": 10.0,
                              "max_alphaengine_calls_24h": 10}}
        adds, removes = normalize_request("BBB".split(), [])
        failing = resolve_additions(mission, adds, lambda t: (_ for _ in ()).throw(RuntimeError("down")))
        self.assertIsNone(failing[0]["company_ref"])
        with self.assertRaises(CoverageListChangeError):
            new_universe(mission, failing, [])
        resolved = resolve_additions(mission, adds, lambda t: {"cik": "2", "name": "Bee Corp"})
        self.assertEqual(resolved[0]["company_ref"], "company:sec-cik:0000000002")
        plan = build_discovery_plan(
            plan_id="discovery-plan:x:web-search:1", created_at="2026-10-06T00:00:00.000000+00:00",
            mission_ref="coverage-mission:x", companies={"company:sec-cik:0000000001": "AAA"},
            source_ref="source:web-search", max_calls_24h=5,
            specs=[{"spec_ref": "s", "query_template": "{terms} news", "lookback_days": 9,
                    "rediscovery_interval_days": 2, "retry_interval_days": 1}])
        nxt, change = plan_after(plan, resolved, [], mission_version=3,
                                 created_at="2026-10-06T01:00:00.000000+00:00")
        self.assertEqual(nxt["id"], "discovery-plan:x:web-search:1:coverage-v3")
        self.assertEqual(nxt["specs"], plan["specs"])
        self.assertEqual(nxt["companies"]["company:sec-cik:0000000002"], {"search_terms": "BBB"})
        self.assertEqual(change["searches_per_company_per_day"], 0.5)
        emptied, change = plan_after(plan, [], ["company:sec-cik:0000000001"], mission_version=3,
                                     created_at="2026-10-06T01:00:00.000000+00:00")
        self.assertIsNone(emptied)
        self.assertTrue(change["empty"])
        with self.assertRaises(CoverageListChangeError):
            normalize_request([], [])
        with self.assertRaises(CoverageListChangeError):
            normalize_request(["not a ticker!"], [])


class WriterRegistrationTests(unittest.TestCase):
    def test_the_levers_are_human_doors_and_the_state_is_a_core_read(self) -> None:
        from dalton_core.writer_server import (
            CORE_OPERATIONS, HUMAN_GOVERNANCE_OPERATIONS, OPERATION_ACTOR_FIELDS,
            OPERATION_FIELDS,
        )

        for operation in ("apply_owner_lever", "stop_research_line", "prioritize_company",
                          "draft_coverage_change", "publish_coverage_change"):
            self.assertIn(operation, HUMAN_GOVERNANCE_OPERATIONS)
            self.assertNotIn(operation, CORE_OPERATIONS)
            self.assertEqual(OPERATION_ACTOR_FIELDS[operation], "actor_ref")
            self.assertIn("actor_ref", OPERATION_FIELDS[operation])
        self.assertIn("owner_lever_state", CORE_OPERATIONS)
        self.assertNotIn("owner_lever_state", HUMAN_GOVERNANCE_OPERATIONS)

    def test_the_schema_is_installed_with_the_core(self) -> None:
        from dalton_core.bootstrap import SCHEMA_DATABASES

        self.assertIn(("owner_lever_schema.sql", None), SCHEMA_DATABASES)


if __name__ == "__main__":
    unittest.main()
