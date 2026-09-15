"""P17d: the four-panel overview row and the ops backlog page behind it.

Chem's §8.8, and the retrospective's 3.5: run state, source gaps, pending
failures and output acceptance are four facts that only mean something
together.  Chem kept them on four pages, and its "health OK" page therefore sat
beside an unfilled research gap and a task that had been permanently suspended
for a week.  These tests are about the row, and about the one property that
makes it worth having: each tile counts what its own page shows.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.cockpit_plane import (
    DEPENDENCY_LABELS,
    FAILURE_CLASS_LABELS,
    LANE_STATUS_BUCKETS,
    LANE_STATUS_BUCKET_OF,
    REGISTRY_LANE_LABELS,
    _ops_superseded_mission,
    _ops_superseded_model_spec,
    _ops_model_spec_history_reason,
)
from dalton_core.extraction_backlog import observed_yield
from dalton_core.lane_failure_class import LaneFailureBudget
from dalton_core.lane_failure_ledger import LaneFailureLedger, default_path

from tests.test_cockpit_plane import CockpitHarness

OWNER = "owner@example.com"
TASK_62 = "AlphaEngine Desktop status=no_module_page"
PAGE = Path(__file__).resolve().parents[1] / "src" / "dalton_core" / "cockpit_control.html"


class PanelCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.c = CockpitHarness(self.root)
        self.addCleanup(self.c.close)
        self.plane = self.c.plane

    def park(self, *, lane: str = "research_task", item: str = "task:62",
             reason: str = TASK_62, at: datetime | None = None) -> None:
        moment = at or datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
        with LaneFailureLedger(default_path(self.root), clock=lambda: moment) as ledger:
            budget = LaneFailureBudget(lane, ledger=ledger, clock=lambda: moment)
            budget.record(item, reason=reason)

    def lanes(self, planner: dict) -> None:
        heartbeat = json.loads(self.c.heartbeat.read_text(encoding="utf-8"))
        heartbeat["bounded_planner"]["last_result"] = {
            **heartbeat["bounded_planner"]["last_result"], **planner}
        self.c.heartbeat.write_text(json.dumps(heartbeat), encoding="utf-8")


class OpsBacklogTests(PanelCase):
    def test_a_core_that_never_parked_anything_says_so_rather_than_erroring(self) -> None:
        backlog = self.plane.ops_backlog()
        self.assertFalse(backlog["available"])
        self.assertEqual(backlog["parked_items"], 0)
        self.assertIn("挂起", backlog["reason"])

    def test_a_parked_item_appears_under_its_dependency_with_first_and_last_seen(self) -> None:
        first = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)
        self.park(at=first)
        self.park(at=first + timedelta(hours=30))
        backlog = self.plane.ops_backlog()
        self.assertTrue(backlog["available"])
        self.assertEqual(backlog["parked_items"], 1)
        bucket = backlog["dependencies"][0]
        self.assertEqual(bucket["dependency"], "alphaengine_desktop")
        self.assertEqual(bucket["dependency_label"],
                         DEPENDENCY_LABELS["alphaengine_desktop"])
        self.assertEqual(bucket["item_count"], 1)
        item = bucket["items"][0]
        self.assertEqual(item["first_seen"][:10], "2026-09-08")
        self.assertEqual(item["last_seen"][:10], "2026-09-09")
        self.assertEqual(item["reason"], TASK_62)

    def test_two_lanes_waiting_on_one_source_are_one_row(self) -> None:
        """The reading Chem could not get: an outage, not two lane problems."""

        self.park(lane="research_task", item="task:62")
        self.park(lane="mission_ownership", item="company:acn")
        backlog = self.plane.ops_backlog()
        self.assertEqual(len(backlog["dependencies"]), 1)
        bucket = backlog["dependencies"][0]
        self.assertEqual(bucket["item_count"], 2)
        self.assertIn("mission_ownership", bucket["lanes"])
        self.assertIn(REGISTRY_LANE_LABELS["mission_ownership"], bucket["lane_labels"])

    def test_terminal_items_are_shown_apart_and_labelled(self) -> None:
        self.park(item="doc:9", reason="the scan is unreadable")
        backlog = self.plane.ops_backlog()
        self.assertEqual(backlog["parked_items"], 0)
        self.assertEqual(backlog["terminal_count"], 1)
        self.assertEqual(backlog["terminal_items"][0]["item_key"], "doc:9")
        self.assertEqual(
            backlog["terminal_items"][0]["display_reason"],
            "当前产出未通过内容或证据校验",
        )
        self.assertEqual(set(backlog["class_labels"]), set(FAILURE_CLASS_LABELS))

    def test_terminal_validator_reasons_are_translated_without_losing_raw_audit(self) -> None:
        reasons = {
            "doc:number": ("number_not_in_source", "数字缺少可核验来源"),
            "doc:length": ("assessment is longer than 1200 characters", "输出格式或长度不符合要求"),
            "doc:evidence": ("this draft cites nothing new", "现有证据不支持这份产出"),
            "doc:empty-web": (
                "fetch outcome failed; public web fetch returned an empty response body; not retryable",
                "来源页面没有返回正文，因此未登记为可用资料",
            ),
            "doc:http-web": (
                "fetch outcome failed; public web fetch returned HTTP 403; not retryable",
                "来源页面拒绝访问或返回了失败状态",
            ),
        }
        moment = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
        with LaneFailureLedger(default_path(self.root), clock=lambda: moment) as ledger:
            for item, (reason, _) in reasons.items():
                ledger.append_event(
                    lane="company_dossier", item_key=item, event="terminal",
                    failure_class="content_refused", dependency=None,
                    reason=reason, rule="content_refused", status=None,
                )
        rows = {row["item_key"]: row for row in self.plane.ops_backlog()["terminal_items"]}
        for item, (raw, display) in reasons.items():
            with self.subTest(item=item):
                self.assertEqual(rows[item]["reason"], raw)
                self.assertEqual(rows[item]["display_reason"], display)

    def test_historical_mission_lane_keys_have_owner_labels(self) -> None:
        for lane in (
            "mission_event_judgement", "mission_debate_map", "mission_model_spec",
            "mission_model_forecast", "mission_sensitivity", "mission_claim_index",
            "mission_conviction",
        ):
            with self.subTest(lane=lane):
                self.assertIn(lane, REGISTRY_LANE_LABELS)
                self.assertNotEqual(REGISTRY_LANE_LABELS[lane], lane)

    def test_permission_items_have_their_own_authorization_bucket(self) -> None:
        self.park(item="doc:permission",
                  reason="gated:mission does not grant document_extraction writes")
        backlog = self.plane.ops_backlog()
        self.assertEqual(backlog["permission_count"], 1)
        self.assertEqual(backlog["permission_items"][0]["item_key"], "doc:permission")
        self.assertEqual(backlog["parked_items"], 0)
        self.assertEqual(backlog["terminal_count"], 0)

    def test_approved_governance_keeps_old_permission_as_history_not_current_waiting(self) -> None:
        raw = ("LaneChildRejected: gated:governance invalid yfinance-calendar "
               "governance: yfinance calendar governance record is not approved")
        self.park(lane="mission_catalyst_calendar", item="permission|company:a|old",
                  reason=raw)
        directory = self.root / "connector-governance"
        directory.mkdir()
        (directory / "yfinance-calendar-v1.json").write_text(
            json.dumps({"status": "approved"}), encoding="utf-8")
        backlog = self.plane.ops_backlog()
        self.assertEqual(backlog["permission_count"], 0)
        self.assertEqual(backlog["historical_count"], 1)
        self.assertEqual(backlog["historical_items"][0]["history_status"],
                         "configuration_updated")
        self.assertEqual(backlog["historical_items"][0]["reason"], raw)

    def test_permission_projection_reloads_governance_after_file_changes(self) -> None:
        raw = ("LaneChildRejected: gated:governance invalid yfinance daily-prices "
               "governance: yfinance daily-prices governance record is not approved")
        self.park(lane="mission_market_prices", item="permission|company:a|old",
                  reason=raw)
        directory = self.root / "connector-governance"
        directory.mkdir()
        path = directory / "yfinance-daily-prices-v1.json"
        path.write_text(json.dumps({"status": "proposed"}), encoding="utf-8")
        self.assertEqual(self.plane.ops_backlog()["permission_count"], 1)
        path.write_text(json.dumps({"status": "approved"}), encoding="utf-8")
        changed = self.plane.ops_backlog()
        self.assertEqual(changed["permission_count"], 0)
        self.assertEqual(changed["historical_count"], 1)

    def test_unknown_permission_kind_remains_active_after_unrelated_approval(self) -> None:
        self.park(item="doc:permission",
                  reason="gated:mission does not grant document_extraction writes")
        directory = self.root / "connector-governance"
        directory.mkdir()
        (directory / "yfinance-calendar-v1.json").write_text(
            json.dumps({"status": "approved"}), encoding="utf-8")
        backlog = self.plane.ops_backlog()
        self.assertEqual(backlog["permission_count"], 1)
        self.assertEqual(backlog["historical_count"], 0)

    def test_old_version_of_current_mission_is_history_but_current_and_other_scope_stay_active(self) -> None:
        with self.plane._core() as core:
            version = self.plane._mission(core)["version"]
        self.park(item=f"coverage-mission-version:us-it-services:{version - 1}|old")
        self.park(item=f"coverage-mission-version:us-it-services:{version}|current")
        self.park(item="coverage-mission-version:other-scope:1|other")
        self.park(item=f"coverage-mission-version:us-it-services:{version + 1}|future")
        self.park(item="coverage-mission-version:us-it-services:not-a-version|unknown")
        backlog = self.plane.ops_backlog()
        active = {item["item_key"] for bucket in backlog["dependencies"]
                  for item in bucket["items"]}
        historical = {item["item_key"] for item in backlog["historical_items"]}
        self.assertEqual(active, {
            f"coverage-mission-version:us-it-services:{version}|current",
            "coverage-mission-version:other-scope:1|other",
            f"coverage-mission-version:us-it-services:{version + 1}|future",
            "coverage-mission-version:us-it-services:not-a-version|unknown",
        })
        self.assertEqual(historical,
                         {f"coverage-mission-version:us-it-services:{version - 1}|old"})

    def test_old_mission_binding_is_found_in_any_exact_key_segment(self) -> None:
        current = "coverage-mission-version:us-it-services:17"
        prefix = "company:sec-cik:0000051143|" + "a" * 64 + "|"
        self.assertTrue(_ops_superseded_mission(
            prefix + "coverage-mission-version:us-it-services:14|contract:x", current))
        for value in (
            prefix + "coverage-mission-version:us-it-services:17",
            prefix + "coverage-mission-version:us-it-services:18",
            prefix + "coverage-mission-version:other:14",
            prefix + "coverage-mission-version:us-it-services:not-a-version",
            prefix + "coverage-mission-version:us-it-services:14|coverage-mission-version:other:1",
        ):
            with self.subTest(value=value):
                self.assertFalse(_ops_superseded_mission(value, current))

    def test_model_spec_failure_requires_a_later_formal_success(self) -> None:
        company = "company:sec-cik:0001467373"
        item = {"lane": "mission_model_spec", "item_key": company + "|" + "a" * 64,
                "last_seen": "2026-09-12T10:00:00+00:00"}
        self.assertTrue(_ops_superseded_model_spec(item, {company: {
            "state_hash": "b" * 64, "created_at": "2026-09-13T10:00:00+00:00"}}))
        self.assertFalse(_ops_superseded_model_spec(item, {company: {
            "state_hash": "b" * 64, "created_at": "2026-09-11T10:00:00+00:00"}}))
        self.assertFalse(_ops_superseded_model_spec(
            {**item, "item_key": company + "|not-a-hash"}, {company: {
                "state_hash": "b" * 64, "created_at": "2026-09-13T10:00:00+00:00"}}))
        # A later, well-formed observed input also proves the older fingerprint
        # is historical even when the later attempt itself remains held.
        self.assertTrue(_ops_superseded_model_spec(item, {}, {company: {
            "state_hash": "c" * 64, "last_seen": "2026-09-13T10:00:00+00:00"}}))
        self.assertFalse(_ops_superseded_model_spec(item, {}, {company: {
            "state_hash": "a" * 64, "last_seen": "2026-09-13T10:00:00+00:00"}}))
        self.assertEqual(_ops_model_spec_history_reason(item, {company: {
            "state_hash": "b" * 64, "created_at": "2026-09-13T10:00:00+00:00"}}),
                         "later_success")
        self.assertEqual(_ops_model_spec_history_reason(item, {}, {company: {
            "state_hash": "c" * 64, "last_seen": "2026-09-13T10:00:00+00:00"}}),
                         "newer_input")

    def test_newer_terminal_model_input_moves_old_budget_wait_to_history(self) -> None:
        company = "company:sec-cik:0000051143"
        old = company + "|" + "a" * 64
        newer = company + "|" + "b" * 64
        for day, key, event, failure, dependency, reason in (
            (10, old, "parked", "dependency_unavailable", "model_budget", "BUDGET_REFUSED"),
            (11, newer, "terminal", "content_refused", None, "continuing-income does not tie to filed history"),
        ):
            moment = datetime(2026, 9, day, 9, tzinfo=timezone.utc)
            with LaneFailureLedger(default_path(self.root), clock=lambda: moment) as ledger:
                ledger.append_event(lane="mission_model_spec", item_key=key,
                                    event=event, failure_class=failure, dependency=dependency,
                                    reason=reason, rule=failure, status=None)
        backlog = self.plane.ops_backlog()
        self.assertEqual(backlog["parked_items"], 0)
        self.assertIn(old, {x["item_key"] for x in backlog["historical_items"]})
        self.assertIn(newer, {x["item_key"] for x in backlog["terminal_items"]})
        self.assertIn("输入已经更新", backlog["historical_items"][0]["history_note"])

    def test_the_page_carries_no_machine_words_for_a_dependency_it_knows(self) -> None:
        self.park()
        bucket = self.plane.ops_backlog()["dependencies"][0]
        self.assertNotEqual(bucket["dependency_label"], bucket["dependency"])

    def test_real_model_budget_payload_has_readable_company_and_keeps_raw_audit_folded(self) -> None:
        raw_item = "company:sec-cik:0000051143|" + "a" * 64 + "|" + "b" * 64
        raw_reason = "CockpitModelError: the model call did not succeed (BUDGET_REFUSED)"
        with LaneFailureLedger(default_path(self.root)) as ledger:
            ledger.append_event(
                lane="mission_model_spec", item_key=raw_item, event="parked",
                failure_class="dependency_unavailable", dependency="model_budget",
                reason=raw_reason, rule="model_budget", status=None,
            )
        item = self.plane.ops_backlog()["dependencies"][0]["items"][0]
        self.assertEqual(item["item_label"], "IBM")
        self.assertEqual(item["display_reason"],
                         "本次请求超出适用的调用或任务预算限制")
        self.assertEqual(item["technical_details"]["item_key"], raw_item)
        self.assertEqual(item["technical_details"]["reason"], raw_reason)

    def test_permission_payload_explains_the_specific_governance_without_raw_exception(self) -> None:
        raw = ("LaneChildRejected: gated:governance invalid yfinance-calendar "
               "governance: yfinance calendar governance record is not approved")
        self.park(lane="mission_catalyst", item="company:sec-cik:0001467373|calendar",
                  reason=raw)
        item = self.plane.ops_backlog()["permission_items"][0]
        self.assertEqual(item["item_label"], "ACN · Accenture")
        self.assertEqual(item["display_reason"], "财报与分红日程的数据源尚未获得使用批准")
        self.assertEqual(item["technical_details"]["reason"], raw)

    def test_ops_renderer_uses_display_fields_and_folds_raw_values(self) -> None:
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn("`${it.lane_label} · ${it.item_label}：${it.display_reason}`", page)
        self.assertIn("technicalDetails(it.technical_details)", page)
        self.assertNotIn("`${it.lane_label}：${it.item_key} — ${it.reason}`", page)


class FourPanelTests(PanelCase):
    def test_overview_reads_the_ticket_tree_once(self) -> None:
        tickets = self.plane.tickets.tickets
        with patch.object(self.plane.tickets, "tickets", wraps=tickets) as read:
            self.plane.overview()
        self.assertEqual(read.call_count, 1)

    def test_overview_measures_document_yield_once_for_all_companies(self) -> None:
        with patch("dalton_core.extraction_backlog.observed_yield",
                   wraps=observed_yield) as read:
            self.plane.overview()
        self.assertEqual(read.call_count, 1)

    def test_backlog_summary_preserves_old_core_degradation(self) -> None:
        connection = self.c.h.h.core.connection
        connection.execute("DROP TABLE coverage_mission_pointer")
        connection.commit()
        result = self.plane._extraction_backlog_total(
            connection, {"company:test": {}})
        self.assertEqual(result, {
            "available": False, "reason": "研究目标里没有可数的公司"})

    def test_empty_mission_skips_shared_yield_scan(self) -> None:
        connection = self.c.h.h.core.connection
        with patch("dalton_core.extraction_backlog.observed_yield") as read:
            result = self.plane._extraction_backlog_total(connection, {})
        read.assert_not_called()
        self.assertFalse(result["available"])

    def test_the_overview_carries_all_four_panels_each_linking_somewhere(self) -> None:
        ops = self.plane.overview()["ops"]
        self.assertEqual(set(ops), {"lanes", "gaps", "failures", "acceptance"})
        for name, panel in ops.items():
            with self.subTest(panel=name):
                self.assertIn("link", panel)
                self.assertTrue(panel["link"])
                self.assertIn("note", panel)

    def test_the_lane_panel_counts_the_six_words_the_owner_reads(self) -> None:
        self.lanes({
            "mission_market_prices": {"status": "ungranted", "reason": "no grant"},
            "claim_index": {"status": "idle", "reason": "every claim is indexed"},
            "mission_ownership": {"status": "launched"},
            "mission_consensus": {"status": "held", "reason": "three failures"},
        })
        view = self.plane.overview()
        panel = view["ops"]["lanes"]
        self.assertEqual(set(panel["counts"]), set(LANE_STATUS_BUCKETS))
        self.assertGreaterEqual(panel["counts"]["ungranted"], 1)
        self.assertGreaterEqual(panel["counts"]["running"], 1)
        self.assertGreaterEqual(panel["counts"]["held"], 1)
        self.assertEqual(panel["total"], len(view["activity"]["lanes"]))

    def test_the_lane_panel_and_the_lane_rows_cannot_disagree(self) -> None:
        """The tile is a count of the rows below it, not a second reading."""

        view = self.plane.overview()
        panel, rows = view["ops"]["lanes"], view["activity"]["lanes"]
        self.assertEqual(sum(panel["counts"].values()) + panel["other"], len(rows))

    def test_a_lane_word_the_buckets_never_saw_is_counted_not_dropped(self) -> None:
        self.lanes({"claim_index": {"status": "something_new"}})
        panel = self.plane.overview()["ops"]["lanes"]
        self.assertGreaterEqual(panel["other"], 1)
        self.assertNotIn("something_new", LANE_STATUS_BUCKET_OF)

    def test_terminal_recovery_and_duplicate_have_honest_buckets(self) -> None:
        self.lanes({
            "industry_framework": {"status": "terminal", "reason": "content refusal"},
            "mission_document_research": {"status": "recovery_required", "reason": "recover"},
            "mission_reflection": {"status": "duplicate", "reason": "same inputs"},
        })
        view = self.plane.overview()
        rows = {row["key"]: row for row in view["activity"]["lanes"]}
        self.assertEqual(rows["lane:industry_framework"]["note"],
                         "本次任务已结束，需更新资料或条件后再重新运行")
        self.assertEqual(rows["lane:mission_document_research"]["note"],
                         "上次执行留下待恢复事项，本轮未继续处理")
        self.assertEqual(rows["lane:mission_reflection"]["note"],
                         "已有相同结果，无需重复生成")
        self.assertEqual(LANE_STATUS_BUCKET_OF["terminal"], "held")
        self.assertEqual(LANE_STATUS_BUCKET_OF["recovery_required"], "held")
        self.assertEqual(LANE_STATUS_BUCKET_OF["duplicate"], "idle")
        self.assertEqual(LANE_STATUS_BUCKET_OF["proposed"], "unapproved")

    def test_the_failure_panel_reads_the_same_ledger_as_the_ops_page(self) -> None:
        self.park()
        self.park(lane="mission_ownership", item="company:acn")
        view = self.plane.overview()
        panel = view["ops"]["failures"]
        backlog = self.plane.ops_backlog()
        self.assertTrue(panel["available"])
        self.assertEqual(panel["parked_items"], backlog["parked_items"])
        self.assertEqual(panel["headline"],
                         backlog["parked_items"] + backlog["permission_count"])
        self.assertEqual(panel["dependencies"][0]["dependency"], "alphaengine_desktop")
        self.assertEqual(panel["link"], "ops")

    def test_stopped_attempt_history_does_not_inflate_current_work_count(self) -> None:
        self.park(item="doc:old", reason="the scan is unreadable")
        panel = self.plane.overview()["ops"]["failures"]
        self.assertEqual(panel["headline"], 0)
        self.assertEqual(panel["terminal_count"], 1)
        self.assertEqual(self.plane.ops_backlog()["terminal_items"][0]["item_key"], "doc:old")
        self.park(item="doc:permission", reason="gated:mission does not grant document_extraction writes")
        self.assertEqual(self.plane.overview()["ops"]["failures"]["headline"], 1)

    def test_the_failure_panel_is_honest_about_a_core_with_no_ledger(self) -> None:
        panel = self.plane.overview()["ops"]["failures"]
        self.assertFalse(panel["available"])
        self.assertEqual(panel["headline"], 0)

    def test_the_gap_panel_names_both_kinds_of_gap(self) -> None:
        panel = self.plane.overview()["ops"]["gaps"]
        self.assertIn("framework_gaps", panel)
        self.assertIn("extraction_backlog", panel)
        self.assertIsInstance(panel["headline"], int)
        self.assertEqual(panel["link"], "sources")

    def test_the_gap_panel_counts_the_open_framework_gaps(self) -> None:
        record = {
            "id": "industry-framework-version:1", "gaps": [
                {"gap_ref": "gap:pricing", "label": "定价权", "status": "open"},
                {"gap_ref": "gap:capex", "label": "资本开支", "status": "partially_covered"},
                {"gap_ref": "gap:mix", "label": "结构", "status": "covered"},
            ],
        }
        self.c.h.h.core.connection.executescript(
            "CREATE TABLE IF NOT EXISTS industry_framework_versions("
            "industry_ref TEXT, version_number INTEGER, record_json TEXT)")
        self.c.h.h.core.connection.execute(
            "INSERT INTO industry_framework_versions VALUES(?,?,?)",
            ("industry:us-it-services", 1, json.dumps(record)))
        self.c.h.h.core.connection.commit()
        panel = self.plane.overview()["ops"]["gaps"]
        self.assertTrue(panel["framework_gaps"]["available"])
        self.assertEqual(panel["framework_gaps"]["open"], 2)
        self.assertEqual(panel["framework_gaps"]["industries"][0]["gaps"], 3)

    def test_the_acceptance_panel_reports_last_closed_week(self) -> None:
        panel = self.plane.overview()["ops"]["acceptance"]
        self.assertEqual(panel["link"], "reflection")
        if panel["available"]:
            self.assertIn("W", panel["week"])
            self.assertIsInstance(panel["published"], int)
        else:
            self.assertTrue(panel["note"])

    def test_a_panel_never_raises_on_a_core_missing_the_table_it_reads(self) -> None:
        """Old-Core degradation, the rule every cockpit reader follows."""

        for table in ("industry_framework_versions", "research_quality_score_versions"):
            with self.subTest(table=table):
                with self.plane._core() as core:
                    from dalton_core.cockpit_plane import _table_exists

                    if _table_exists(core, table):
                        continue
                ops = self.plane.overview()["ops"]
                self.assertIn("gaps", ops)
                self.assertIn("acceptance", ops)


class RouteTests(unittest.TestCase):
    def application(self, plane):
        from dalton_core.agenda_control import AgendaControlApplication

        return AgendaControlApplication(None, None, cockpit_plane=plane)

    def test_the_ops_route_calls_the_reader_and_says_it_is_enabled(self) -> None:
        class Plane:
            def __init__(self) -> None:
                self.seen = 0

            def ops_backlog(self):
                self.seen += 1
                return {"available": True, "parked_items": 2}

        plane = Plane()
        view = self.application(plane).cockpit_view("/v1/cockpit/ops", OWNER, {})
        self.assertEqual(plane.seen, 1)
        self.assertEqual(view["parked_items"], 2)
        self.assertTrue(view["enabled"])


class PageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = PAGE.read_text(encoding="utf-8")

    def test_the_page_has_a_four_panel_row_and_asks_for_the_ops_route(self) -> None:
        self.assertIn('<div class="grid c4" id="ops-panels">', self.page)
        self.assertIn("/v1/cockpit/ops", self.page)
        self.assertIn("renderOpsPanels", self.page)

    def test_each_panel_opens_the_page_it_counts(self) -> None:
        for opener in ("openSources", "openOps", "openReflection"):
            with self.subTest(opener=opener):
                self.assertIn(opener, self.page)
        # The two existing readers are still reachable from their own buttons.
        self.assertIn('$("open-sources").onclick=openSources;', self.page)
        self.assertIn('$("open-reflection").onclick=openReflection;', self.page)

    def test_task_panel_opens_the_collapsed_lane_details_before_scrolling(self) -> None:
        self.assertIn('function openLaneDetails()', self.page)
        self.assertIn('fold.open=true', self.page)
        self.assertIn('requestAnimationFrame(()=>lanes.scrollIntoView', self.page)
        self.assertIn('openLaneDetails,L.waiting_on_you>0', self.page)

    def test_heavy_reads_are_single_flight_bounded_and_page_scoped(self) -> None:
        self.assertIn('if(logLoad)return logLoad', self.page)
        self.assertIn('if(current==="log")loadLog(false)', self.page)
        self.assertIn('getJson(`/v1/cockpit/log?limit=200`,{timeoutMs:30000})', self.page)
        self.assertIn('getJson("/v1/cockpit/overview",{timeoutMs:30000})', self.page)
        self.assertIn('页面保留上次结果', self.page)
        self.assertIn('b.onclick=()=>loadOverview(true)', self.page)

    def test_the_row_speaks_the_owner_s_language(self) -> None:
        for word in ("任务运行情况", "待补齐资料缺口", "当前受阻任务",
                     "上周交付物验收", "运维待办"):
            with self.subTest(word=word):
                self.assertIn(word, self.page)

    def test_terminal_copy_does_not_claim_every_failure_is_unreadable_bytes(self) -> None:
        self.assertIn('node("summary","查看已停止的研究尝试")', self.page)
        self.assertIn("任务已经结束，具体原因暂未记录", self.page)
        self.assertIn("历史尝试次数不计入当前受阻任务", self.page)
        self.assertNotIn("内容本身读不出来，再试一次读到的还是同样的字节", self.page)
        self.assertIn("const grouped=new Map()", self.page)
        self.assertIn("it.count>1", self.page)

    def test_model_copy_distinguishes_reload_and_token_budget(self) -> None:
        self.assertIn("保存后多数修改在下一次调用生效", self.page)
        self.assertIn("该类含常驻服务固定的环节，保存后需重启才完全生效", self.page)
        self.assertIn("每天阅读份数上限（留空＝不限）", self.page)
        self.assertNotIn("输入额度（按 UTF-8 字节预估）", self.page)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
