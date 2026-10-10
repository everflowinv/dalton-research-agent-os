"""2026-10-09 UX reviews: 管理员设置 role gating, collapsed commands, type-to-confirm,
problem-first sections, parked-item auto-release and the publication worker's
exhausted items.

* Role gating (independent review G02, MUSE §8, financial review §4.15):
  ``admin_logins`` in the service config.  Absent = every allowed login (a new
  workspace works out of the box); set = any other login gets 403 on
  ``/admin``, ``/v2/admin``, ``/v2/data/admin/*``, the administrator action and
  the administrator-only cockpit writes, and the shell's manifest does not
  list the page (so ``adminHref()`` is null and the analyst pages hide it).
* Ops noise: a parked item whose dependency recovered (a ``dependency_ok`` in
  any lane) or that has not been parked again for a week is released -- by an
  appended ``resumed`` event (append-only), and on read until that is written.
* The worker's ``exhausted`` is a list of products, and the administrator page
  names each one with its retry.
* The page under node: every command collapsed under 「技术详情（命令）」 behind
  「显示命令」, a dangerous one only after a typed confirmation; each section
  leads with 「现在有什么问题」; identifiers collapsed.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from dalton_core.agenda_control import (
    ADMIN_ONLY_COCKPIT_ACTIONS, AgendaControlApplication, AgendaControlConfig,
    AgendaControlError, AgendaControlPlane, _handler,
)
from dalton_core.cockpit_v2.admin import data as admin_data
from dalton_core.cockpit_v2.admin.routes import DATA_SECTIONS, dispatch
from dalton_core.lane_failure_ledger import (
    AUTO_ARCHIVED_RULE, AUTO_RESOLVED_RULE, LaneFailureLedger, LedgerWriter, default_path,
    summarise_events,
)
from dalton_core.lane_failure_observer import LaneOutcomeObserver
from dalton_core import research_publication_worker as worker
from tests.test_cockpit_v2_admin import (
    ADMIN_JS, HOST, LOGIN, NODE_DOM, Dashboard, FreshWorkspace, _node, page_get_paths,
)

ANALYST = "analyst@example.com"


def _config(root: Path, **extra) -> AgendaControlConfig:
    return AgendaControlConfig.from_mapping({
        "host": "127.0.0.1", "port": 8794, "tailscale_host": HOST,
        "tailscale_executable": sys.executable, "allowed_tailscale_logins": [LOGIN, ANALYST],
        "writer_socket": str(root / "writer.sock"), "token_config": str(root / "tokens.json"),
        "endpoint_ref": "openclaw:discord:test", "feedback_timeout_seconds": 86400,
        "sweep_interval_seconds": 60, **extra,
    })


class Served:
    """The blank workspace's cockpit behind the real HTTP surface
    (``agenda_control._handler``: what the deployed service runs)."""

    def __init__(self, case: unittest.TestCase, **config) -> None:
        fresh = FreshWorkspace(case)  # builds the workspace and its cockpit plane
        root = Path(tempfile.mkdtemp(dir="/tmp"))
        case.addCleanup(lambda: __import__("shutil").rmtree(root, True))
        self.config = _config(root, **config)
        plane = AgendaControlPlane(self.config, dashboard_client=Dashboard(), timeout_client=Dashboard())
        self.app = AgendaControlApplication(self.config, plane, None, None, fresh.cockpit)
        self.governance_calls = fresh.governance_calls
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        case.addCleanup(self.server.server_close)
        case.addCleanup(self.server.shutdown)

    def request(self, method, path, *, login=LOGIN, body=None, csrf=None, cookie=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=60)
        headers = {"Host": HOST, "Tailscale-User-Login": login}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(payload))})
        if csrf is not None:
            headers["X-Dalton-CSRF"] = csrf
        if cookie is not None:
            headers["Cookie"] = cookie
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def session(self, login):
        status, headers, body = self.request("GET", "/v2/data/manifest", login=login)
        assert status == 200, body
        return headers["Set-Cookie"].split(";", 1)[0], json.loads(body)["csrf_token"]


ADMIN_PATHS = ("/admin", "/v2/admin", *(f"/v2/data/admin/{name}" for name in DATA_SECTIONS),
               "/v2/data/admin/actions")


class RoleGatingTests(unittest.TestCase):
    def test_config_key_is_optional_and_must_be_a_subset_of_the_allowed_logins(self):
        root = Path(tempfile.mkdtemp(dir="/tmp"))
        self.assertIsNone(_config(root).admin_logins)
        self.assertEqual(_config(root, admin_logins=[LOGIN]).admin_logins, (LOGIN,))
        for bad in ([], ["nobody@example.com"], [LOGIN, LOGIN], "owner"):
            with self.subTest(bad=bad), self.assertRaises(AgendaControlError):
                _config(root, admin_logins=bad)

    def test_default_every_allowed_login_may_open_the_admin_page(self):
        served = Served(self)
        for login in (LOGIN, ANALYST):
            for path in ADMIN_PATHS:
                with self.subTest(login=login, path=path):
                    status, _h, body = served.request("GET", path, login=login)
                    self.assertEqual(status, 200, body[:200])
            manifest = json.loads(served.request("GET", "/v2/data/manifest", login=login)[2])
            self.assertTrue(manifest["admin"]["allowed"])
            self.assertIn("/v2/admin", manifest["pages"])

    def test_configured_list_refuses_everyone_else_with_403_in_words(self):
        served = Served(self, admin_logins=[LOGIN])
        for path in ADMIN_PATHS:
            with self.subTest(path=path):
                status, _h, body = served.request("GET", path, login=ANALYST)
                self.assertEqual(status, 403, body[:200])
                value = json.loads(body)
                self.assertEqual(value["error"], "admin_only")
                self.assertIn("管理员", value["display_message"])
                self.assertEqual(served.request("GET", path, login=LOGIN)[0], 200, path)
        # the analyst pages themselves stay open to the analyst
        self.assertEqual(served.request("GET", "/", login=ANALYST)[0], 200)
        self.assertEqual(served.request("GET", "/v1/cockpit/levers", login=ANALYST)[0], 200)

    def test_manifest_hides_the_admin_page_from_a_non_admin(self):
        served = Served(self, admin_logins=[LOGIN])
        analyst = json.loads(served.request("GET", "/v2/data/manifest", login=ANALYST)[2])
        self.assertFalse(analyst["admin"]["allowed"])
        self.assertFalse([p for p in analyst["pages"] if p.startswith("/v2/admin")])
        self.assertFalse([p for p in analyst["posts"] if p.startswith("/v2/data/admin/")])
        owner = json.loads(served.request("GET", "/v2/data/manifest", login=LOGIN)[2])
        self.assertTrue(owner["admin"]["allowed"])
        self.assertIn("/v2/admin", owner["pages"])
        # shell.js builds every administrator link from manifest.pages
        shell = (Path(ADMIN_JS).parents[1] / "shell.js").read_text(encoding="utf-8")
        self.assertRegex(shell, r'pages\.includes\("/v2/admin"\)')

    def test_admin_writes_are_refused_to_a_non_admin_before_anything_is_sent(self):
        served = Served(self, admin_logins=[LOGIN])
        cookie, csrf = served.session(ANALYST)
        status, _h, body = served.request(
            "POST", "/v2/data/admin/action", login=ANALYST, cookie=cookie, csrf=csrf,
            body={"action": "lever", "reason": "试试", "payload": {"lever": "emergency_stop", "request_id": "r"}})
        self.assertEqual((status, json.loads(body)["error"]), (403, "admin_only"))
        for action in sorted(ADMIN_ONLY_COCKPIT_ACTIONS):
            with self.subTest(action=action):
                status, _h, body = served.request(
                    "POST", f"/v1/cockpit/{action}", login=ANALYST, cookie=cookie, csrf=csrf,
                    body={"lever": "emergency_stop", "reason": "试试", "request_id": "r"})
                self.assertEqual((status, json.loads(body)["error"]), (403, "admin_only"))
        self.assertEqual(served.governance_calls, [])

    def test_dispatch_refuses_a_non_admin_too(self):
        served = Served(self, admin_logins=[LOGIN])
        response = dispatch(served.app, "GET", "/v2/data/admin/summary", login=ANALYST,
                            session=SimpleNamespace(csrf="t"))
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(response.body)["error"], "admin_only")


class BlankWorkspaceCanaryTests(unittest.TestCase):
    """``/admin`` and every ``/v2/data/admin/*`` read through the deployed
    handler on a blank workspace: 200, no 5xx, nothing leaks the login."""

    def test_admin_page_and_data_answer_a_blank_workspace(self):
        served = Served(self)
        status, headers, body = served.request("GET", "/admin")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn(b"admin.js", body)
        for path in sorted({*ADMIN_PATHS[2:], *[p for p in page_get_paths() if p.startswith("/v2/data/admin/")]}):
            with self.subTest(path=path):
                status, _h, body = served.request("GET", path)
                self.assertEqual(status, 200, body[:300])
                self.assertNotIn(LOGIN.encode(), body)
        health = json.loads(served.request("GET", "/v2/data/admin/health")[2])
        self.assertEqual(health["ledger"].get("available"), False)  # no ledger yet: said so
        self.assertFalse(health["publication"]["present"])


# -- the parked-item auto-release ------------------------------------------------

T0 = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)


def _park(ledger, lane, item, dependency, at):
    ledger.append_event(lane=lane, item_key=item, event="parked", failure_class="dependency_unavailable",
                        dependency=dependency, reason=f"{dependency} unavailable", rule="test", at=at)


class ParkedExpiryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.path = default_path(self.state)

    def ledger(self):
        ledger = LaneFailureLedger(self.path)
        self.addCleanup(ledger.close)
        return ledger

    def test_a_dependency_that_recovered_in_another_lane_releases_the_item(self):
        ledger = self.ledger()
        _park(ledger, "company_dossier", "company:a|1", "model_budget", T0)
        _park(ledger, "earnings_season", "season:1", "model", T0)
        ledger.append_dependency_ok(lane="mission_model_spec", dependency="model_budget",
                                    at=T0 + timedelta(hours=3))
        backlog = summarise_events(ledger.events(now=T0 + timedelta(hours=4)))
        self.assertEqual([b["dependency"] for b in backlog["dependencies"]], ["model"])
        self.assertEqual(backlog["parked_items"], 1)
        released = backlog["released_items"]
        self.assertEqual([(r["item_key"], r["resolution"]) for r in released], [("company:a|1", "resolved")])
        self.assertIn("mission_model_spec", released[0]["reason"])

    def test_an_item_not_parked_again_for_a_week_is_archived_and_comes_back_if_it_recurs(self):
        ledger = self.ledger()
        _park(ledger, "mission_claim_index", "company:b|1", "model", T0)
        _park(ledger, "mission_claim_index", "company:c|1", "model", T0 + timedelta(days=8))
        backlog = summarise_events(ledger.events(now=T0 + timedelta(days=8)))
        self.assertEqual(backlog["parked_items"], 1)  # the newest event is "now": b is 8 days old
        self.assertEqual(backlog["released_items"][0]["resolution"], "archived")
        self.assertEqual(backlog["released_count"], 1)
        _park(ledger, "mission_claim_index", "company:b|1", "model", T0 + timedelta(days=8, hours=1))
        backlog = summarise_events(ledger.events(now=T0 + timedelta(days=9)))
        self.assertEqual(backlog["parked_items"], 2)
        self.assertEqual(backlog["released_count"], 0)
        # expiry off: only the appended events count
        self.assertEqual(summarise_events(ledger.events(now=T0 + timedelta(days=9)), expiry_days=None,
                                          now=T0 + timedelta(days=30))["parked_items"], 2)

    def test_the_sweep_appends_resolution_events_once_and_the_ledger_stays_append_only(self):
        ledger = self.ledger()
        _park(ledger, "company_dossier", "company:a|1", "model_budget", T0)
        _park(ledger, "mission_debate_map", "company:b|1", "model", T0)
        ledger.append_dependency_ok(lane="mission_model_spec", dependency="model_budget",
                                    at=T0 + timedelta(hours=1))
        before = len(ledger.events(now=T0 + timedelta(days=10)))
        written = ledger.release_stale_parks(now=T0 + timedelta(days=10))
        self.assertEqual(sorted((w["item_key"], w["rule"]) for w in written),
                         [("company:a|1", AUTO_RESOLVED_RULE), ("company:b|1", AUTO_ARCHIVED_RULE)])
        self.assertEqual(ledger.release_stale_parks(now=T0 + timedelta(days=10)), [])
        events = ledger.events(now=T0 + timedelta(days=10))
        self.assertEqual(len(events), before + 2)
        self.assertEqual({e["event"] for e in events[before:]}, {"resumed"})
        backlog = summarise_events(events, expiry_days=None)
        self.assertEqual(backlog["parked_items"], 0)
        self.assertTrue(all(r["recorded"] for r in backlog["released_items"]))
        with self.assertRaises(sqlite3.DatabaseError):
            ledger.connection.execute("DELETE FROM lane_failure_events")
        # the lane's own budget replays the release as a resume
        from dalton_core.lane_failure_ledger import lane_budget
        budget = lane_budget("company_dossier", state_dir=self.state)
        self.assertFalse([row for row in budget.blocked_items() if row.get("item_key") == "company:a|1"])

    def test_the_writer_observer_sweeps_at_most_once_an_hour_and_never_raises(self):
        ledger = self.ledger()
        _park(ledger, "mission_claim_index", "company:b|1", "model", T0)
        ledger.close()
        observer = LaneOutcomeObserver(self.state, clock=lambda: T0 + timedelta(days=9))
        self.assertEqual(observer.sweep(), {"status": "swept", "released": 1})
        self.assertIsNone(observer.sweep())
        self.assertEqual(observer.sweep(force=True), {"status": "swept", "released": 0})
        self.assertIsNone(LaneOutcomeObserver(None).sweep())
        from unittest import mock
        with mock.patch("dalton_core.lane_failure_ledger.release_stale_parks", side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual(observer.sweep(force=True), {"status": "unswept:OperationalError"})
        events = LedgerWriter(self.path).events(now=T0 + timedelta(days=9))
        self.assertEqual(events[-1]["rule"], AUTO_ARCHIVED_RULE)


# -- the publication worker's exhausted products --------------------------------

PRODUCT = {"kind": "surface_event_judgement", "subject_ref": "company:sec-cik:0001467373",
           "version_ref": "event-judgement:1", "status": "available",
           "sections": [{"title": "结论", "body": "新披露的订单转化放缓，需要在下一次财报核实收入确认进度。", "gaps": []}]}


class PublicationExhaustedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name) / "research-publication-work"
        self.products = self.work / "products"

    def poll(self):
        return worker.poll_once(
            None, {"universe": [{"company_ref": PRODUCT["subject_ref"]}]}, state_dir=self.products,
            prepare=lambda product: {"status": worker.EXHAUSTED_STATUS, "receipt": {
                "status": "incomplete", "failures": [{"product": 0, "section_start": 0,
                                                      "error": "independent localization verifier did not pass cleanly"}]}},
            library_reader=lambda *_a: {"products": [PRODUCT]})

    def test_the_checkpoint_names_every_exhausted_product(self):
        result = self.poll()
        self.assertEqual(result["exhausted"], 1)
        self.assertEqual(len(result["exhausted_products"]), 1)
        row = result["exhausted_products"][0]
        self.assertEqual(row["identity"]["subject_ref"], PRODUCT["subject_ref"])
        self.assertEqual(row["identity_hash"], worker._hash(worker._identity(PRODUCT)))
        self.assertTrue(row["product_hash"])

    def test_the_admin_section_names_company_kind_reason_and_retry(self):
        result = self.poll()
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "worker-last-run.json").write_text(json.dumps(result), encoding="utf-8")
        plane = SimpleNamespace(_core=lambda: (_ for _ in ()).throw(RuntimeError("no core")))
        last = admin_data.publication_last_run(plane, self.work)
        self.assertEqual(last["counts"]["exhausted"], 1)
        item = last["exhausted_items"][0]
        self.assertEqual(item["kind_label"], "事件判断")
        self.assertEqual(item["identity_hash"], result["exhausted_products"][0]["identity_hash"])
        self.assertIn("verifier", item["reason"])
        self.assertTrue(item["current"])
        self.assertFalse(item["auto_retry"])
        # an older checkpoint without the list still names it (from ``products``)
        legacy = {k: v for k, v in result.items() if k != "exhausted_products"}
        (self.work / "worker-last-run.json").write_text(json.dumps(legacy), encoding="utf-8")
        self.assertEqual(admin_data.publication_last_run(plane, self.work)["exhausted_items"][0]["identity_hash"],
                         item["identity_hash"])


class ParityWordingTests(unittest.TestCase):
    def test_fixes_name_the_admin_sections_and_commands_are_technical_detail(self):
        source = (Path(ADMIN_JS).parents[2] / "workspace_health_parity.py").read_text(encoding="utf-8")
        self.assertNotIn("cockpit 模型页", source)
        self.assertNotIn("cockpit 调整方向", source)
        self.assertIn("管理员设置 → 运行控制", source)
        self.assertIn("管理员设置 → 预算与费用", source)
        self.assertIn("管理员设置 → AI 服务", source)
        self.assertEqual(admin_data.split_fix("python scripts/x.py --apply"), (None, "python scripts/x.py --apply"))
        self.assertEqual(admin_data.split_fix("在「管理员设置」保存"), ("在「管理员设置」保存", None))
        self.assertEqual(admin_data.plain("车道下一轮重新路由，改绑票据"), "研究流程下一轮重新路由，改绑执行凭证")


# -- the page under node -----------------------------------------------------------

NODE_LAYOUT = r"""
require(process.argv[4]);
const hiddenUp = n => { for (let x = n; x; x = x.parentNode) if (x.hidden) return true; return false; };
const inside = (n, cls, tag) => { for (let x = n.parentNode; x; x = x.parentNode) if ((!cls || (x.classList && x.classList.contains(cls))) && (!tag || x.tagName === tag)) return true; return false; };
(async () => {
  await settle();
  const out = { panels: {} };
  for (const id of ["ai", "connections", "run", "budget", "health", "projects"]) {
    window.DaltonAdmin.select(id); await settle();
    const p = root.querySelector("#adm-panel-" + id);
    const firstSection = p.childNodes.find(c => c.tagName === "SECTION");
    const pres = p.querySelectorAll("pre");
    const codes = p.querySelectorAll("code");
    out.panels[id] = {
      first: firstSection ? firstSection.dataset.role : null,
      firstText: firstSection ? firstSection.textContent.slice(0, 40) : null,
      pres: pres.length, presVisible: pres.filter(x => !hiddenUp(x)).length,
      presOutsideCommands: pres.filter(x => !inside(x, "adm-cmds", "DETAILS")).length,
      codesOutsideDetails: codes.filter(x => !inside(x, null, "DETAILS")).length,
      inputsAsReason: p.querySelectorAll("input").filter(i => (i.attrs["aria-label"] || "").endsWith("的理由")).length,
      text: p.textContent,
    };
  }
  // revealing a dangerous command needs the typed confirmation
  window.DaltonAdmin.select("run"); await settle();
  const run = root.querySelector("#adm-panel-run");
  const block = run.querySelectorAll("details").find(d => d.classList.contains("adm-cmds") && d.textContent.includes("deploy.sh"));
  const toggle = block.querySelectorAll("button").find(b => b.textContent === "显示命令");
  const body = block.querySelector("[data-role=command-body]");
  out.toggle_expanded_before = toggle.attrs["aria-expanded"];
  toggle.click(); await settle();
  let dlg = root.querySelector("[data-role=confirm]");
  out.reveal_dialog = dlg ? dlg.textContent : null;
  dlg.querySelector("[data-role=confirm-cancel]").click(); await settle();
  out.hidden_after_cancel = body.hidden;
  toggle.click(); await settle();
  dlg = root.querySelector("[data-role=confirm]");
  const input = dlg.querySelector("input"); input.value = window.DaltonAdmin._state.env.name; input.dispatch("input");
  dlg.querySelector("[data-role=confirm-ok]").click(); await settle();
  out.hidden_after_confirm = body.hidden;
  out.toggle_expanded_after = toggle.attrs["aria-expanded"];
  out.copy_buttons = block.querySelectorAll("button").filter(b => b.textContent === "复制命令").length;
  // a cancelled danger confirmation sends nothing
  const stop = run.querySelectorAll("button").find(b => b.textContent === "紧急停机");
  if (stop) {
    const reason = run.querySelectorAll("textarea").find(i => (i.attrs["aria-label"] || "") === "紧急停机的理由");
    reason.value = "测试"; stop.click(); await settle();
    root.querySelector("[data-role=confirm]").dispatch("keydown");
    root.querySelector("[data-role=confirm-cancel]").click(); await settle();
    out.posts_after_cancel = posts.length;
    out.cancel_note = run.textContent.includes("已取消，没有执行");
    out.stop_in_danger_slot = (function up(n){ for (let x = n; x; x = x.parentNode) if (x.classList && x.classList.contains("adm-danger-slot")) return true; return false; })(stop);
  }
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


class AdminLayoutNodeTests(unittest.TestCase):
    def setUp(self):
        self.node = _node()
        if self.node is None:
            self.skipTest("node is unavailable")
        self.w = FreshWorkspace(self)
        responses = {}
        for path in sorted(set(page_get_paths()) | {"/v1/cockpit/log?limit=200"}):
            status, _h, body = self.w.request("GET", path)
            responses[path] = {"status": status, "body": json.loads(body)}
        # a live-shaped runtime: a held task with a free door and a withdraw
        runtime = responses["/v2/data/admin/runtime"]["body"]
        runtime["held_tasks"] = [{
            "ref": "lane:x:admission:abc", "title": "等你决定：EPAM · 收入怎么拆分？",
            "company": "EPAM", "question": "EPAM 2025 年收入按地区怎么拆分？", "lane_label": "专题研究",
            "reason_label": "重新进入失败", "cost": "free", "cost_label": "所有选项都不花钱",
            "why": "车道下一轮会重新路由；已自动改绑票据", "admission_ref": "mission-document-research-admission:abc",
            "doors": [{"door": "reentry", "label": "放行一次重新进入（不花钱）", "needs_cost_cap": False, "cost": "free",
                       "cost_label": "不花钱", "admission_ref": "mission-document-research-admission:abc", "confirm": "确认？"},
                      {"door": "withdraw", "label": "撤回这项研究", "needs_cost_cap": False, "danger": True, "cost": "free",
                       "cost_label": "不花钱", "admission_ref": "mission-document-research-admission:abc", "confirm": "撤回？"}]}]
        health = responses["/v2/data/admin/health"]["body"]
        health["publication"] = {"available": True, "present": True, "counts": {"exhausted": 133}, "labels": {"exhausted": "重试次数用完"},
                                 "note": "累计", "last_run": {"checked_at": "2026-10-09T11:41:58+00:00", "counts": {"exhausted": 1, "pending": 32},
                                 "exhausted_items": [{"identity_hash": "b022696c" * 8, "product_hash": "1349a12b" * 8, "kind_label": "事件判断",
                                                      "company": "ACN · Accenture", "failed_at": "2026-10-09T11:17:38+00:00", "reason": "verifier did not pass",
                                                      "retries": 1, "auto_retry": False, "current": True, "version_ref": "event-judgement:d2fae5"}]}}
        self.responses = responses
        tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "responses.json").write_text(json.dumps(responses, ensure_ascii=False))
        (self.dir / "run.js").write_text(NODE_DOM + NODE_LAYOUT)

    def run_page(self):
        result = subprocess.run(
            [self.node, str(self.dir / "run.js"), str(self.dir / "responses.json"), "standalone", str(ADMIN_JS)],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_sections_lead_with_the_problem_and_hide_identifiers_and_commands(self):
        out = self.run_page()
        for tab, panel in out["panels"].items():
            with self.subTest(tab=tab):
                self.assertEqual(panel["first"], "issues", panel["firstText"])
                self.assertIn("现在有什么问题", panel["firstText"])
                self.assertEqual(panel["presVisible"], 0)
                self.assertEqual(panel["presOutsideCommands"], 0)
                self.assertEqual(panel["codesOutsideDetails"], 0)
                self.assertEqual(panel["inputsAsReason"], 0)  # the reason is a textarea
                for word in ("车道", "票据", "工单"):
                    self.assertNotIn(word, panel["text"])
        self.assertGreater(out["panels"]["run"]["pres"], 0)
        run = out["panels"]["run"]["text"]
        self.assertIn("技术详情（命令）", run)
        self.assertIn("EPAM 2025 年收入按地区怎么拆分？", run)
        self.assertIn("所有选项都不花钱", run)
        self.assertNotIn("mission-document-research-admission:abc", run)  # no hashes on the task card
        self.assertIn("1 项专项研究停下等你决定", run)
        health = out["panels"]["health"]["text"]
        self.assertIn("ACN · Accenture 的事件判断：中文定稿重试次数用完", health)
        self.assertIn("重新进行语言质检", health)
        self.assertIn("1 份报告的中文定稿重试次数用完", health)

    def test_commands_reveal_only_after_a_typed_confirmation_and_never_run(self):
        out = self.run_page()
        self.assertEqual(out["toggle_expanded_before"], "false")
        self.assertIn("后果", out["reveal_dialog"])
        self.assertTrue(out["hidden_after_cancel"])
        self.assertFalse(out["hidden_after_confirm"])
        self.assertEqual(out["toggle_expanded_after"], "true")
        self.assertGreater(out["copy_buttons"], 0)
        if "posts_after_cancel" in out:
            self.assertEqual(out["posts_after_cancel"], 0)
            self.assertTrue(out["cancel_note"])
            self.assertTrue(out["stop_in_danger_slot"])
        source = Path(ADMIN_JS).read_text(encoding="utf-8")
        self.assertNotIn("window.confirm", source)
        self.assertNotIn("child_process", source)
        self.assertNotIn("innerHTML", source)


if __name__ == "__main__":
    unittest.main()
