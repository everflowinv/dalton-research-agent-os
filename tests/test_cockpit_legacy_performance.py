"""2026-09-30 cockpit review, section 三: the legacy performance findings.

P1-1  the model and sources pages (indexes on the link ledger, one shared read
      per router database, the sources page's digest, timing lines, the
      idempotency table that duplicated every decision);
P1-2  the Excel export read the Core in place instead of copying 4.2 GB;
P1-3  the static dashboard embeds what it shows, not every work item;
P2-4  the overview snapshot outlives a rebuild.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dalton_core import cockpit_plane, fund_xlsx_export
from dalton_core.agenda_control import SERVE_GIL_SWITCH_INTERVAL_SECONDS, serve
from dalton_core.cockpit_plane import (
    OVERVIEW_STALE_SECONDS,
    OVERVIEW_TTL_SECONDS,
    CockpitPlane,
    _PageTimer,
)
from dalton_core.dashboard import ProjectionWriter
from dalton_core.migrations import MODEL_ROUTER_INDEXES, apply_projection_indexes
from dalton_core.model_fallback_chain import (
    SKIPPED_LINKS_PER_TIER,
    TIERS,
    routing_overview,
)
from dalton_core.model_router import (
    IDEMPOTENCY_RESULT_REFERENCE,
    ModelRouter,
    ModelRouterValidationError,
    idempotent_route_replay,
)
from dalton_core.openclaw_catalog_reconcile import sync_openclaw_model_catalog
from dalton_core.plugins.static_dashboard import (
    EMBEDDED_TREE_ITEMS,
    StaticDashboardPlugin,
    dashboard_snapshot,
    render_static_dashboard,
)
from dalton_core.research_planner_setup import credential_slots_for, ensure_planner_policy
from dalton_core.store import canonical_json
from scripts import compact_model_route_idempotency as compaction
from tests.test_dashboard import snapshot as dashboard_fixture
from tests.test_mission_deliverable import ACN
from tests.test_model_fallback_chain import NOW, FakeBroker, _work
from tests.test_openclaw_catalog_reconcile import _config
from datetime import timedelta


INDEX_NAMES = {spec.name for spec in MODEL_ROUTER_INDEXES}

#: The shapes of every query ``ModelRouter.chain_link_digest`` makes.
DIGEST_QUERIES = (
    ("SELECT tier, MAX(link_sequence) FROM model_route_chain_links "
     "WHERE served=1 GROUP BY tier", ()),
    ("SELECT purpose, MAX(link_sequence) FROM model_route_chain_links "
     "WHERE served=1 GROUP BY purpose", ()),
    ("SELECT DISTINCT tier FROM model_route_chain_links WHERE 1=1", ()),
    ("SELECT link_json FROM model_route_chain_links WHERE served=0 AND tier=? "
     "ORDER BY link_sequence DESC LIMIT ?", ("brain", 12)),
    ("SELECT skip_reason, COUNT(*), MAX(created_at) FROM model_route_chain_links "
     "WHERE served=0 AND tier=? GROUP BY skip_reason "
     "ORDER BY COUNT(*) DESC, skip_reason", ("brain",)),
)


class _RouterCase(unittest.TestCase):
    """A router with a real catalog, tier policies and a few served chains."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "router.sqlite"
        self.router = ModelRouter(self.path)
        self.addCleanup(self.router.close)
        sync_openclaw_model_catalog(self.router, _config(), checked_at=NOW,
                                     availability_ttl=timedelta(days=3650))
        self.policies = {
            tier: ensure_planner_policy(
                self.router, tier=tier, now=NOW,
                policy_id=f"model-routing-policy:perf-test-{tier}",
            )["policy_version_ref"]
            for tier in TIERS
        }

    def run_chain(self, tier: str, work_id: str, broker: FakeBroker | None = None):
        from dalton_core.model_fallback_chain import execute_chain, tier_chain

        work = _work(work_id)
        return execute_chain(
            self.router, work, purpose="plan", tier=tier, capability="research",
            attempt_number=1, policy_version_ref=self.policies[tier],
            credential_slot_refs=credential_slots_for(self.router, list(tier_chain(tier))),
            required_modalities=["text"], required_context_tokens=2_000,
            estimated_input_tokens=1_000, estimated_output_tokens=500,
            idempotency_prefix=f"perf:{work.id}", call=broker or FakeBroker({}),
        )


class ChainLinkIndexTests(_RouterCase):
    def test_a_writable_open_builds_the_three_link_indexes(self) -> None:
        names = {row[0] for row in self.router.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='model_route_chain_links'")}
        self.assertLessEqual(INDEX_NAMES, names)
        self.assertEqual(
            {spec.columns for spec in MODEL_ROUTER_INDEXES},
            {"served, tier, link_sequence", "served, purpose, link_sequence",
             "served, tier, skip_reason"})

    def test_every_digest_query_is_answered_from_an_index(self) -> None:
        failing = FakeBroker({"profile:gpt-6-astra": {
            "outcome": "failed", "failure_class": "provider_failure"}})
        for index in range(5):
            self.run_chain("brain", f"work:perf-index-{index}", failing)
        for sql, args in DIGEST_QUERIES:
            with self.subTest(sql=sql):
                plan = " | ".join(row[3] for row in self.router.connection.execute(
                    "EXPLAIN QUERY PLAN " + sql, args))
                self.assertRegex(plan, r"INDEX model_route_chain_links_served_")
                self.assertNotRegex(
                    plan, r"SCAN model_route_chain_links(?! USING)")
        digest = self.router.chain_link_digest(skipped_limit=SKIPPED_LINKS_PER_TIER)
        self.assertEqual(digest["served_by_tier"]["brain"]["profile_id"],
                         "profile:claude-fable-5-1")
        self.assertEqual(digest["skipped_by_tier"]["brain"]["total"], 5)

    def test_a_read_only_open_never_builds_and_a_writable_one_rebuilds(self) -> None:
        for name in INDEX_NAMES:
            self.router.connection.execute(f"DROP INDEX {name}")
        with contextlib.closing(ModelRouter(self.path, read_only=True)) as reader:
            reader.chain_link_digest()
        present = lambda: {row[0] for row in self.router.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertFalse(INDEX_NAMES & present())
        ModelRouter(self.path).close()
        self.assertLessEqual(INDEX_NAMES, present())

    def test_an_index_that_cannot_be_built_never_fails_the_open(self) -> None:
        with mock.patch("dalton_core.migrations.ensure_indexes",
                        side_effect=sqlite3.OperationalError("database is locked")):
            ModelRouter(self.path).close()

    def test_the_owner_command_reports_the_router_indexes(self) -> None:
        report = apply_projection_indexes(model_router_db=self.path)
        self.assertEqual(set(report["model_router"]["present"]), INDEX_NAMES)
        self.assertNotIn("model_router", apply_projection_indexes())


class SharedRoutingReadTests(_RouterCase):
    def test_overviews_sharing_a_memo_read_each_decision_once_and_agree(self) -> None:
        self.run_chain("brain", "work:perf-memo-1")
        self.run_chain("cheap", "work:perf-memo-2")
        digest = self.router.chain_link_digest(skipped_limit=SKIPPED_LINKS_PER_TIER)
        expected = {
            tier: routing_overview(self.router, checked_at=NOW, openclaw_config=_config(),
                                   policy_version_ref=ref, link_digest=digest)
            for tier, ref in self.policies.items()
        }
        memo: dict = {}
        with mock.patch.object(self.router, "get_decision",
                               wraps=self.router.get_decision) as reads, \
                mock.patch.object(self.router, "latest_profiles",
                                  wraps=self.router.latest_profiles) as catalog:
            shared = {
                tier: routing_overview(self.router, checked_at=NOW,
                                       openclaw_config=_config(),
                                       policy_version_ref=ref, link_digest=digest,
                                       memo=memo)
                for tier, ref in self.policies.items()
            }
        self.assertEqual(shared, expected)
        served = {row["last_served"]["decision_id"]
                  for overview in shared.values() for row in overview["purposes"]
                  if row["last_served"]}
        self.assertTrue(served)
        self.assertEqual(reads.call_count, len(served))
        # One catalog read for the overview itself; catalog sync reads it too,
        # but only the first time.
        self.assertLessEqual(catalog.call_count, 3)


class SourcesPageTests(unittest.TestCase):
    def test_the_sources_routing_passes_the_indexed_link_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            router_path = Path(directory) / "router.sqlite"
            # Held open as a lane holds it live, so the WAL sidecars exist.
            writer = ModelRouter(router_path)
            self.addCleanup(writer.close)
            config_path = Path(directory) / "model-config.json"
            config_path.write_text(json.dumps({"model_router_db": str(router_path)}))
            plane = CockpitPlane.__new__(CockpitPlane)
            plane.config = SimpleNamespace(model_config_path=config_path,
                                           openclaw_config_path=None)
            plane.clock = lambda: NOW
            with mock.patch("dalton_core.model_fallback_chain.routing_overview",
                            wraps=routing_overview) as overview:
                result = plane._routing()
            self.assertIsNot(result.get("available"), False)
            digest = overview.call_args.kwargs["link_digest"]
            self.assertEqual(set(digest), {"served_by_tier", "served_by_purpose",
                                           "skipped_by_tier"})


class PageTimingTests(unittest.TestCase):
    def test_a_slow_page_logs_its_sections_and_a_fast_one_is_silent(self) -> None:
        plane = CockpitPlane.__new__(CockpitPlane)
        plane._overview_building = True

        def page(timer):
            timer.lap("bindings")
            timer.add("pin_overviews", 0.25)
            return {"ok": True}

        plane._models_page = page
        plane._sources_page = page
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"DALTON_COCKPIT_PAGE_TIMING_SECONDS": "0"}), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(plane.models(), {"ok": True})
            self.assertEqual(plane.sources(), {"ok": True})
        lines = stderr.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("page=models", lines[0])
        self.assertIn("page=sources", lines[1])
        for line in lines:
            self.assertRegex(line, r"total_ms=\d+ thread_cpu_ms=\d+ "
                                   r"overview_building=true bindings_ms=\d+ "
                                   r"pin_overviews_ms=250")
        quiet = io.StringIO()
        with mock.patch.dict(os.environ, {"DALTON_COCKPIT_PAGE_TIMING_SECONDS": "60"}), \
                contextlib.redirect_stderr(quiet):
            plane.models()
        self.assertEqual(quiet.getvalue(), "")

    def test_the_timer_still_reports_a_page_that_failed(self) -> None:
        timer = _PageTimer("models")
        with mock.patch.dict(os.environ, {"DALTON_COCKPIT_PAGE_TIMING_SECONDS": "0"}), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertIn("page=models", timer.finish())


class RouteIdempotencyTests(_RouterCase):
    def route(self, key: str):
        return self.router.route(
            _work("work:perf-idempotency"), attempt_number=1, capability="research",
            policy_version_ref=self.policies["brain"],
            credential_slot_refs=credential_slots_for(
                self.router, ["profile:gpt-6-astra", "profile:claude-fable-5-1"]),
            required_modalities=["text"], required_context_tokens=2_000,
            estimated_input_tokens=1_000, estimated_output_tokens=500,
            idempotency_key=key, tier="brain",
        )

    def stored(self, key: str) -> str:
        return self.router.connection.execute(
            "SELECT result_json FROM model_route_idempotency WHERE idempotency_key=?",
            (key,)).fetchone()[0]

    def test_a_new_row_stores_a_reference_not_a_copy_and_replays_exactly(self) -> None:
        fresh = self.route("perf:idem:1")
        self.assertEqual(fresh["status"], "fresh")
        stored = json.loads(self.stored("perf:idem:1"))
        self.assertEqual(stored, {"result": IDEMPOTENCY_RESULT_REFERENCE,
                                  "status": "fresh",
                                  "decision_ref": fresh["decision"]["id"]})
        self.assertLess(len(self.stored("perf:idem:1")), 200)
        decision_json = self.router.connection.execute(
            "SELECT decision_json FROM model_route_decisions WHERE decision_id=?",
            (fresh["decision"]["id"],)).fetchone()[0]
        self.assertGreater(len(decision_json), 5 * len(self.stored("perf:idem:1")))
        again = self.route("perf:idem:1")
        self.assertEqual(again, {"status": "duplicate", "decision": fresh["decision"]})

    def test_an_old_inline_row_still_replays_and_a_bad_reference_is_refused(self) -> None:
        decision = self.route("perf:idem:2")["decision"]
        inline = {"result_json": canonical_json({"status": "fresh", "decision": decision}),
                  "decision_id": decision["id"]}
        self.assertEqual(idempotent_route_replay(self.router.connection, inline),
                         {"status": "duplicate", "decision": decision})
        wrong = {"result_json": self.stored("perf:idem:2"),
                 "decision_id": "route-decision:other"}
        with self.assertRaises(ModelRouterValidationError):
            idempotent_route_replay(self.router.connection, wrong)


class IdempotencyCompactionTests(_RouterCase):
    def legacy_rows(self, count: int) -> list[str]:
        """Rows in the pre-2026-09-30 shape: the whole result inline."""

        keys = []
        for index in range(count):
            key = f"perf:legacy:{index}"
            decision = self.router.route(
                _work(f"work:perf-legacy-{index}"), attempt_number=1,
                capability="research", policy_version_ref=self.policies["brain"],
                credential_slot_refs=credential_slots_for(
                    self.router, ["profile:gpt-6-astra"]),
                required_modalities=["text"], required_context_tokens=2_000,
                estimated_input_tokens=1_000, estimated_output_tokens=500,
                idempotency_key=f"{key}:new", tier="brain")["decision"]
            with self.router._transaction() as cur:
                cur.execute(
                    "INSERT INTO model_route_idempotency (idempotency_key, request_hash, "
                    "decision_id, result_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (key, "0" * 64, decision["id"],
                     canonical_json({"status": "fresh", "decision": decision}),
                     decision["created_at"]))
            keys.append(key)
        return keys

    def test_dry_run_changes_nothing_and_apply_keeps_the_table_append_only(self) -> None:
        keys = self.legacy_rows(3)
        before = dict(self.router.connection.execute(
            "SELECT idempotency_key, result_json FROM model_route_idempotency"))
        survey = compaction.survey(self.path)
        self.assertEqual(survey["inline_copies"], 3)
        self.assertEqual(survey["already_references"], 3)
        self.assertEqual(before, dict(self.router.connection.execute(
            "SELECT idempotency_key, result_json FROM model_route_idempotency")))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            compaction.main(["--db", str(self.path)])
        self.assertEqual(json.loads(out.getvalue())["mode"], "dry-run")

        self.assertEqual(compaction.apply(self.path, batch=2), {"rewritten": 3})
        for key in keys:
            row = self.router.connection.execute(
                "SELECT decision_id, result_json FROM model_route_idempotency "
                "WHERE idempotency_key=?", (key,)).fetchone()
            replay = idempotent_route_replay(self.router.connection, row)
            self.assertEqual(json.loads(before[key])["decision"], replay["decision"])
        self.assertEqual(compaction.survey(self.path)["inline_copies"], 0)
        # The guard is back, and still refuses an in-place edit.
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.router.connection.execute(
                "UPDATE model_route_idempotency SET result_json='{}'")
        self.assertEqual(compaction.apply(self.path), {"rewritten": 0})

    def test_vacuum_needs_the_owner_and_no_other_process(self) -> None:
        with self.assertRaises(SystemExit) as refused:
            compaction.main(["--db", str(self.path), "--vacuum"])
        self.assertIn("--services-stopped", str(refused.exception))
        with mock.patch.object(compaction, "_other_holders", return_value=["4242"]):
            with self.assertRaisesRegex(SystemExit, "4242"):
                compaction.vacuum(self.path)


class ReadOnlyExportTests(unittest.TestCase):
    """P1-2: the Excel export reads the Core in place, in one snapshot."""

    def setUp(self) -> None:
        from tests.test_cockpit_research_export import ResearchDownloadTests

        harness = ResearchDownloadTests("test_missing_forecast_does_not_download_a_fabricated_model")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        self.harness = harness
        self.db = harness.db
        self.output = Path(harness.folder.name) / "model.xlsx"
        # A Core the writer has already opened with this code: every authority
        # the export constructs has its tables, as on a live Core.
        from dalton_core.store import DaltonStore
        with DaltonStore(self.db) as store:
            fund_xlsx_export.ForecastModelAuthority(store)
            fund_xlsx_export.CoverageMissionAuthority(store)
            fund_xlsx_export.ValuationSnapshotAuthority(store)

    def test_no_whole_database_copy_and_one_read_only_snapshot(self) -> None:
        seen = {}
        real = fund_xlsx_export._export_from_authorities

        def observe(store, *args, **kwargs):
            seen["in_transaction"] = store.connection.in_transaction
            seen["query_only"] = store.connection.execute("PRAGMA query_only").fetchone()[0]
            return real(store, *args, **kwargs)

        before = self.db.read_bytes()
        with mock.patch.object(fund_xlsx_export, "_export_from_private_copy",
                               side_effect=AssertionError("copied the Core")), \
                mock.patch.object(fund_xlsx_export, "_export_from_authorities",
                                  side_effect=observe):
            with self.assertRaisesRegex(fund_xlsx_export.FundWorkbookExportError,
                                        "no current model"):
                fund_xlsx_export.export_company_workbook(
                    self.db, ACN, self.output,
                    mission_ref=self.harness.mission_ref)
        self.assertEqual(seen, {"in_transaction": True, "query_only": 1})
        self.assertEqual(before, self.db.read_bytes())
        self.assertFalse(self.output.exists())

    def test_a_core_that_would_need_a_migration_falls_back_to_the_copy(self) -> None:
        with mock.patch.object(
                fund_xlsx_export, "CoverageMissionAuthority",
                side_effect=sqlite3.OperationalError("attempt to write a readonly database")), \
                mock.patch.object(fund_xlsx_export, "_export_from_private_copy",
                                  return_value={"copied": True}) as copied:
            self.assertEqual(fund_xlsx_export.export_company_workbook(
                self.db, "company:x", self.output), {"copied": True})
        copied.assert_called_once()

    def test_the_download_button_shows_progress_while_it_waits(self) -> None:
        html = (Path(cockpit_plane.__file__).with_name("cockpit_control.html")
                .read_text(encoding="utf-8"))
        start = html.index("async function downloadResearch(")
        body = html[start:html.index("\n}\n", start)]
        self.assertIn('button.setAttribute("aria-busy","true")', body)
        self.assertIn("已用 ${s} 秒", body)
        self.assertIn('clearInterval(progress);button.disabled=false;'
                      'button.removeAttribute("aria-busy")', body)
        self.assertIn('button[aria-busy="true"]::before', html)


class StaticDashboardSizeTests(unittest.TestCase):
    def projection(self, directory: Path, items: int) -> Path:
        data = dashboard_fixture()
        template = data["work_items"][0]
        data["work_items"] = data["work_items"] + [
            {**template, "work_order_ref": f"work:bulk-{index:05d}", "sequence": index,
             "parent_work_order_ref": None,
             "status_reason": "资料提取任务正在执行" * 40,
             "updated_at": f"2026-08-{1 + index % 28:02d}T{index % 24:02d}:00:{index % 60:02d}+00:00"}
            for index in range(items)
        ]
        data["workflow_summaries"][0]["total_tasks"] = items + 2
        path = directory / "projection.sqlite"
        with ProjectionWriter(path) as writer:
            writer.replace(data)
        return path

    def test_a_huge_tree_embeds_only_what_the_page_shows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projection = self.projection(root, 3_000)
            snapshot = dashboard_snapshot(projection)
            (key,) = [key for key in snapshot if key.endswith("/tree")]
            tree = snapshot[key]
            self.assertEqual(tree["total_items"], 3_002)
            self.assertEqual(tree["embedded_items"], EMBEDDED_TREE_ITEMS)
            self.assertEqual(len(tree["data"]), EMBEDDED_TREE_ITEMS)
            # Exactly the newest ones, i.e. what renderTasks would pick.
            from dalton_core.dashboard import DashboardQueryService
            service = DashboardQueryService(projection)
            try:
                full = service.workflow_tree("workflow:research")["data"]
            finally:
                service.close()
            newest = sorted(full, key=lambda row: row["updated_at"],
                            reverse=True)[:EMBEDDED_TREE_ITEMS]
            self.assertEqual(sorted(row["work_order_ref"] for row in tree["data"]),
                             sorted(row["work_order_ref"] for row in newest))
            result = render_static_dashboard(projection, root / "public" / "index.html")
            # 3,000 items of ~1 KB each would be ~3 MB; the page stays small.
            self.assertLess(result["bytes"], 200_000)

    def test_the_heartbeat_says_whether_the_page_is_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projection = self.projection(root, 3)
            plugin = StaticDashboardPlugin.from_mapping({
                "type": "static_dashboard", "enabled": True,
                "output_path": str(root / "public" / "index.html"),
                "publisher": None,
            })
            result = plugin.on_projection(projection)
            self.assertIsNone(result["publish"])
            self.assertTrue(result["public_publish"].startswith("disabled"))


class OverviewSnapshotTtlTests(unittest.TestCase):
    """P2-4: a snapshot must outlive both a rebuild and the page's retry."""

    def test_the_ttl_is_longer_than_a_legacy_rebuild_and_the_page_poll(self) -> None:
        legacy_rebuild_seconds, page_retry_seconds, page_poll_seconds = 16, 6, 60
        self.assertGreaterEqual(OVERVIEW_TTL_SECONDS, 30)
        self.assertGreater(OVERVIEW_TTL_SECONDS, 2 * legacy_rebuild_seconds)
        self.assertGreater(OVERVIEW_TTL_SECONDS, page_retry_seconds)
        self.assertLessEqual(OVERVIEW_TTL_SECONDS, page_poll_seconds)
        self.assertLess(OVERVIEW_TTL_SECONDS, OVERVIEW_STALE_SECONDS)

    def test_a_tab_polling_every_six_seconds_causes_one_rebuild_a_minute(self) -> None:
        now = [1000.0]
        builds = []
        plane = CockpitPlane.__new__(CockpitPlane)
        plane._overview_condition = threading.Condition()
        plane._overview_building = False
        plane._overview_generation = 0
        plane._overview_result = None
        plane._overview_built_at = None
        plane._overview_ttl_seconds = OVERVIEW_TTL_SECONDS
        plane._overview_stale_seconds = OVERVIEW_STALE_SECONDS
        plane._overview_monotonic = lambda: now[0]

        def build():
            builds.append(now[0])
            return {"snapshot": len(builds)}

        plane._build_overview = build
        plane.overview()
        for _ in range(int(OVERVIEW_TTL_SECONDS // 6)):
            now[0] += 6
            if now[0] - builds[0] < OVERVIEW_TTL_SECONDS:
                self.assertEqual(plane.overview(), {"snapshot": 1})
        self.assertEqual(len(builds), 1)
        now[0] = builds[0] + OVERVIEW_TTL_SECONDS + 1
        self.assertEqual(plane.overview(), {"snapshot": 1, "refreshing": True})
        for _ in range(500):
            if plane._overview_generation == 2:
                break
            threading.Event().wait(0.01)
        self.assertEqual(len(builds), 2)


class ServeSwitchIntervalTests(unittest.TestCase):
    def test_the_serving_process_shortens_the_gil_switch_interval(self) -> None:
        self.assertLessEqual(SERVE_GIL_SWITCH_INTERVAL_SECONDS, 0.001)
        self.assertIn("sys.setswitchinterval(SERVE_GIL_SWITCH_INTERVAL_SECONDS)",
                      inspect.getsource(serve))


if __name__ == "__main__":
    unittest.main()
