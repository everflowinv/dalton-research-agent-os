"""2026-09-30 cockpit display review (section 三): page-side checks.

The server-side fixes each have tests beside the code they change; these are
the checks on the one HTML page, which has no JS test harness of its own: the
inline script still parses, and the page renders the fields the fixes added.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "dalton_core" / "cockpit_control.html"


def _source() -> str:
    return HTML.read_text(encoding="utf-8")


class CockpitPageScriptTests(unittest.TestCase):
    def test_inline_script_still_parses(self):
        if shutil.which("node") is None:
            self.skipTest("node is unavailable")
        blocks = re.findall(r"<script[^>]*>(.*?)</script>", _source(), re.S)
        self.assertTrue(blocks)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cockpit.js"
            path.write_text("\n".join(blocks), encoding="utf-8")
            result = subprocess.run(["node", "--check", str(path)],
                                    capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


class NeedsHumanPageTests(unittest.TestCase):
    def test_where_links_to_a_real_view_and_commands_are_copyable(self):
        source = _source()
        # P1-5: "在哪里做" goes to the named view rather than naming a page
        # that does not exist.
        self.assertIn("whereNode(it)", source)
        self.assertIn("show(it.where_view)", source)
        self.assertIn('data-view="approve"', source)
        # The commands are raw text (no display rewriting) in 技术详情.
        self.assertIn("commandDetails((it.detail||{}).commands)", source)
        self.assertIn('rawNode("pre",v)', source)
        self.assertIn("技术详情 · 可复制的命令", source)



class LogDetailTests(unittest.TestCase):
    """P1-7: machine sentences in the log are translated, the original kept."""

    def test_known_machine_sentences_are_translated(self):
        from dalton_core.cockpit_plane import _log_detail

        raw = ("Document original is durably unreadable; no read-completion receipt or "
               "Claim was produced. PublicWebSourceError: fetched page is not valid UTF-8")
        shown, technical = _log_detail(raw)
        self.assertEqual(shown, "资料原文无法读取（网页编码无法识别），没有产生结论")
        self.assertEqual(technical, {"original_detail": raw})
        shown, _ = _log_detail("P1i: document never names this company; it is not about "
                               "it [document-subject-names:1-2-3b]")
        self.assertIn("没有提到这家公司", shown)

    def test_unknown_exceptions_get_the_generic_line_and_prose_is_left_alone(self):
        from dalton_core.cockpit_plane import _log_detail

        shown, technical = _log_detail("WeirdConnectorError: something odd happened")
        self.assertEqual(shown, "具体原因见技术详情")
        self.assertEqual(technical["original_detail"],
                         "WeirdConnectorError: something odd happened")
        self.assertEqual(_log_detail("Duplicate of the Q2 revenue claim"),
                         ("Duplicate of the Q2 revenue claim", None))
        self.assertEqual(_log_detail("文档里没有可用的观点"), ("文档里没有可用的观点", None))
        self.assertEqual(_log_detail(None), (None, None))

    def test_daily_digest_summary_reads_in_chinese(self):
        from dalton_core.cockpit_plane import _log_detail

        shown, technical = _log_detail(
            "daily digest company:sec-cik:0001467373 2026-09-29: 12 inputs, 11 NO_CHANGE; "
            "judgements: event-judgement:abc")
        self.assertEqual(shown, "2026-09-29 每日事件摘要：12 条输入，其中 11 条无需调整研究判断")
        self.assertIn("event-judgement:abc", technical["original_detail"])

    def test_reason_translates_known_exceptions_and_keeps_chinese(self):
        from dalton_core.cockpit_plane import _reason

        self.assertEqual(_reason(RuntimeError(
            "CockpitModelError: the model call did not succeed (POOL_EXHAUSTED)")),
            "本次工作使用的模型预算池今天余额不足")
        self.assertEqual(_reason(RuntimeError("写者拒绝了这次提交")), "写者拒绝了这次提交")
        self.assertEqual(_reason(RuntimeError("some unmapped english")), "some unmapped english")


class LogPlaneTests(unittest.TestCase):
    def setUp(self):
        from tests.test_cockpit_plane import CockpitHarness

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.c = CockpitHarness(self.root)
        self.addCleanup(self.c.close)

    def test_a_failed_fetch_shows_a_chinese_reason_with_the_original_in_technical(self):
        error = "PublicWebSourceError: fetched page is not valid UTF-8 at byte 1024"
        run = self.root / "fetches" / "t1"
        run.mkdir(parents=True)
        (run / "ticket.json").write_text(json.dumps({
            "status": "failed", "exit_code": 1,
            "completed_at": "2026-09-30T07:17:00+00:00", "document_ref": "document:x"}))
        (run / "summary.json").write_text(json.dumps({
            "error": error, "canonical_url": "https://www.sec.gov/msft-20250630.htm"}))
        self.c.plane.tickets._scan = None  # the harness already scanned once
        events = [e for e in self.c.plane.log()["events"] if e["kind"] == "fetches"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["detail"],
                         "https://www.sec.gov/msft-20250630.htm — 来源网页编码无法识别，无法读取正文")
        self.assertEqual(events[0]["technical"], {"original_error": error})

    def test_a_heartbeat_error_uses_the_shared_table_first(self):
        heartbeat = json.loads(self.c.heartbeat.read_text(encoding="utf-8"))
        heartbeat["outbox"] = {"last_error": "BudgetRefused: pool_exhausted for coverage",
                               "last_completed_at": "2026-09-30T07:00:00+00:00"}
        self.c.heartbeat.write_text(json.dumps(heartbeat), encoding="utf-8")
        problems = [e for e in self.c.plane.log()["events"] if e["kind"] == "problem"]
        self.assertEqual(problems[0]["detail"], "本次工作使用的模型预算池今天余额不足")
        self.assertIn("pool_exhausted", problems[0]["technical"]["original_error"])


class OpsPagingTests(unittest.TestCase):
    """P1-7: the ops page groups every stopped attempt and pages the rows."""

    def setUp(self):
        from datetime import datetime, timedelta, timezone

        from dalton_core.lane_failure_ledger import LaneFailureLedger, default_path
        from tests.test_cockpit_plane import CockpitHarness

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.c = CockpitHarness(self.root)
        self.addCleanup(self.c.close)
        start = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
        for index in range(130):
            moment = start + timedelta(minutes=index)
            with LaneFailureLedger(default_path(self.root), clock=lambda m=moment: m) as ledger:
                ledger.append_event(
                    lane="company_dossier", item_key=f"doc:{index:03d}", event="terminal",
                    failure_class="content_refused", dependency=None,
                    reason=("number_not_in_source" if index % 2 else "this draft cites nothing new"),
                    rule="content_refused", status=None)

    def test_default_response_carries_counts_groups_and_one_page(self):
        backlog = self.c.plane.ops_backlog()
        self.assertEqual(backlog["terminal_count"], 130)
        self.assertEqual(len(backlog["terminal_items"]), 50)
        self.assertEqual(backlog["terminal_page"], {
            "offset": 0, "limit": 50, "returned": 50, "total": 130, "next_offset": 50})
        # Newest first.
        self.assertEqual(backlog["terminal_items"][0]["item_key"], "doc:129")
        groups = {g["display_reason"]: g for g in backlog["terminal_groups"]}
        self.assertEqual(sum(g["count"] for g in groups.values()), 130)
        self.assertEqual(groups["数字缺少可核验来源"]["count"], 65)
        self.assertEqual(groups["现有证据不支持这份产出"]["count"], 65)
        self.assertLessEqual(len(groups["数字缺少可核验来源"]["technical_samples"]), 3)
        self.assertLess(len(json.dumps(backlog, ensure_ascii=False)), 60_000)

    def test_the_last_page_says_there_is_no_next_one(self):
        backlog = self.c.plane.ops_backlog(terminal_offset=100, terminal_limit=50)
        self.assertEqual(backlog["terminal_page"]["returned"], 30)
        self.assertIsNone(backlog["terminal_page"]["next_offset"])
        self.assertEqual(backlog["terminal_items"][-1]["item_key"], "doc:000")

    def test_the_route_reads_page_parameters_and_refuses_garbage_in_chinese(self):
        from dalton_core.agenda_control import AgendaControlApplication
        from dalton_core.cockpit_plane import CockpitError

        app = AgendaControlApplication(None, None, cockpit_plane=self.c.plane)
        view = app.cockpit_view("/v1/cockpit/ops", "owner@example.com",
                                {"terminal_offset": "120", "terminal_limit": "5"})
        self.assertEqual([row["item_key"] for row in view["terminal_items"]],
                         ["doc:009", "doc:008", "doc:007", "doc:006", "doc:005"])
        capped = app.cockpit_view("/v1/cockpit/ops", "owner@example.com",
                                  {"terminal_limit": "100000"})
        self.assertEqual(capped["terminal_page"]["limit"], 500)
        with self.assertRaises(CockpitError) as caught:
            app.cockpit_view("/v1/cockpit/ops", "owner@example.com", {"terminal_offset": "-1"})
        self.assertIn("terminal_offset", str(caught.exception))
        self.assertIn("整数", str(caught.exception))

    def test_the_page_renders_server_groups_and_pages_history(self):
        source = _source()
        self.assertIn("r.terminal_groups", source)
        self.assertIn("historical_offset=${next}", source)



class ParameterConsistencyTests(unittest.TestCase):
    """P2-9: every cockpit route reads parameters the same way, in Chinese."""

    class Plane:
        def __init__(self):
            self.seen = {}

        def log(self, **kwargs):
            self.seen["log"] = kwargs
            return {"events": []}

        def claims(self, **kwargs):
            self.seen["claims"] = kwargs
            return {"items": []}

        def history(self, login, kind, limit=20):
            return []

        def drafts(self, login, kind=None, limit=20):
            return []

        def job(self, login, job_id):
            from dalton_core.cockpit_plane import CockpitError
            raise CockpitError("job not found")

    def app(self):
        from dalton_core.agenda_control import AgendaControlApplication

        self.plane = self.Plane()
        return AgendaControlApplication(None, None, cockpit_plane=self.plane)

    def refused(self, path, query):
        from dalton_core.cockpit_plane import CockpitError

        with self.assertRaises(CockpitError) as caught:
            self.app().cockpit_view(path, "owner@example.com", query)
        return str(caught.exception)

    def test_non_integer_limits_are_refused_everywhere_in_chinese(self):
        for path in ("/v1/cockpit/log", "/v1/cockpit/claims"):
            with self.subTest(path=path):
                message = self.refused(path, {"limit": "abc"})
                self.assertIn("limit", message)
                self.assertIn("整数", message)
                self.assertIn("0", self.refused(path, {"limit": "0"}))

    def test_oversized_limits_are_capped_and_the_applied_limit_is_reported(self):
        from dalton_core.cockpit_plane import MAX_CLAIMS_IN_VIEW

        app = self.app()
        claims = app.cockpit_view("/v1/cockpit/claims", "owner@example.com",
                                  {"limit": "999999"})
        self.assertEqual(self.plane.seen["claims"]["limit"], MAX_CLAIMS_IN_VIEW)
        self.assertEqual(claims["limit"], MAX_CLAIMS_IN_VIEW)
        log = app.cockpit_view("/v1/cockpit/log", "owner@example.com", {"limit": "999999"})
        self.assertEqual(self.plane.seen["log"]["limit"], 500)
        self.assertEqual(log["limit"], 500)
        default = app.cockpit_view("/v1/cockpit/log", "owner@example.com", {})
        self.assertEqual(default["limit"], 150)

    def test_history_kind_and_job_id_are_refused_in_chinese(self):
        self.assertIn("ask、goal、steer 或 coverage", self.refused("/v1/cockpit/history", {"kind": "zzz"}))
        self.assertIn("缺少参数 id", self.refused("/v1/cockpit/job", {"id": ""}))
        self.assertIn("没有这个页面", self.refused("/v1/cockpit/nope", {}))

    def test_an_english_400_body_carries_a_chinese_display_message(self):
        from dalton_core.agenda_control import _cockpit_error_body
        from dalton_core.cockpit_plane import CockpitConflict, CockpitError

        body = json.loads(_cockpit_error_body("cockpit", CockpitError("job not found")))
        self.assertEqual(body["message"], "job not found")
        self.assertEqual(body["display_message"],
                         "找不到这项后台任务，它可能已经过期；请重新发起一次。")
        body = json.loads(_cockpit_error_body("cockpit", CockpitError("参数 limit 必须是整数")))
        self.assertEqual(body["display_message"], "参数 limit 必须是整数")
        body = json.loads(_cockpit_error_body(
            "conflict", CockpitConflict("budget refused the call (pool_exhausted)")))
        self.assertEqual(body["display_message"], "本次工作使用的模型预算池今天余额不足。")



class TrajectoryWorkTitleTests(unittest.TestCase):
    """P2-3: work rows read lane + company, never 工单retryable：goal-0ef0…"""

    MEMBERS = {"company:sec-cik:0001352010": {"ticker": "EPAM", "name": "EPAM Systems"},
               "company:sec-cik:0001467373": {"ticker": "ACN", "name": "Accenture"}}

    def test_every_scheduler_state_has_a_label(self):
        from dalton_core.cockpit_plane import WORK_STATE_LABELS

        for state in ("ready", "leased", "succeeded", "retryable", "failed", "expired",
                      "running"):
            self.assertIn(state, WORK_STATE_LABELS)
            self.assertRegex(WORK_STATE_LABELS[state], r"[\u4e00-\u9fff]")

    def test_title_parts_name_the_purpose_and_the_one_company(self):
        from dalton_core.cockpit_plane import _work_title_parts

        record = json.dumps({"metadata": {"purpose": "dossier"},
                             "question": "Write the dossier for company:sec-cik:0001352010"})
        self.assertEqual(
            _work_title_parts("work:cockpit-dossier-6f12513b" + "0" * 24, record, self.MEMBERS),
            ("编制公司研究档案", "EPAM · EPAM Systems"))
        two = json.dumps({"question": "company:sec-cik:0001352010 vs company:sec-cik:0001467373"})
        self.assertEqual(_work_title_parts("work:web-fetch:" + "a" * 32, two, self.MEMBERS),
                         ("获取网页", None))
        from dalton_core.model_selection import PURPOSE_LABELS

        self.assertEqual(_work_title_parts("work:cockpit-goal-0ef0" + "1" * 28, None, {}),
                         (PURPOSE_LABELS["goal"], None))
        lane, company = _work_title_parts("work:cockpit-goal-0ef0" + "1" * 28, None, {})
        self.assertNotIn("0ef0", lane)
        self.assertIsNone(company)
        self.assertEqual(_work_title_parts("work:something-new-" + "b" * 32, None, {}),
                         ("研究任务", None))

    def test_trajectory_rows_use_lane_and_company_and_keep_the_ref_technical(self):
        import dataclasses
        import sqlite3

        from tests.test_cockpit_plane import CockpitHarness

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        c = CockpitHarness(root)
        self.addCleanup(c.close)
        path = root / "scratch-scheduler.sqlite"
        connection = sqlite3.connect(path)
        connection.executescript(
            "CREATE TABLE scheduler_attempt_events(event_seq INTEGER PRIMARY KEY, "
            "work_order_id TEXT, state TEXT, reason TEXT, created_at TEXT);"
            "CREATE TABLE scheduler_work_orders(work_order_id TEXT PRIMARY KEY, "
            "work_order_json TEXT);")
        work = "work:cockpit-goal-0ef0" + "c" * 28
        connection.execute("INSERT INTO scheduler_work_orders VALUES(?,?)",
                           (work, json.dumps({"metadata": {"purpose": "goal"}})))
        connection.execute(
            "INSERT INTO scheduler_attempt_events(work_order_id,state,reason,created_at) "
            "VALUES(?,?,?,?)", (work, "retryable",
                                "CockpitModelError: [INVALID_HOST_RESULT]: host returned invalid text",
                                "2026-09-15T10:00:00+00:00"))
        connection.commit()
        connection.close()
        c.plane.config = dataclasses.replace(c.plane.config, scheduler_db=path)
        rows = [e for e in c.plane.trajectory()["events"] if e["kind"] == "work"]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("retryable", rows[0]["title"])
        self.assertNotIn("0ef0", rows[0]["title"])
        self.assertTrue(rows[0]["title"].startswith("研究任务失败，稍后重试："))
        self.assertEqual(rows[0]["detail"], "模型返回的内容无法解析")
        self.assertEqual(rows[0]["technical"]["work_order_ref"], work)
        self.assertIn("INVALID_HOST_RESULT", rows[0]["technical"]["reason"])

    def test_the_page_labels_claim_count_bases(self):
        source = _source()
        self.assertIn('c.claims.basis==="all_versions"?"（含重复版本）"', source)
        self.assertIn("入库的结论（含重复版本）", source)
        self.assertIn("r.count_basis", source)

    def test_the_page_labels_trajectory_statuses(self):
        source = _source()
        self.assertIn("TRAJ_STATUS_LABELS[e.status]", source)
        self.assertIn("...(e.technical||{})", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
