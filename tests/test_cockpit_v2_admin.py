"""Cockpit v2 administrator interface (「管理员设置」, 2026-10-08).

Pinned down here:

* route registration: the shell's four registration shapes, the standalone
  handler (page, assets, data, fall-through to the cockpit), and -- when the
  v2 shell's ``cockpit_v2.routes`` exists -- that it registers ours;
* every administrator GET, and every ``/v1/cockpit/*`` read the page makes,
  answers a fresh workspace without a 5xx; the route list is read from the
  page's own source so a read added later is covered;
* writes: only ``human:`` principals, only the allowlisted cockpit actions,
  a reason is required, CSRF is checked, actor fields in the payload are
  refused, the reason travels with the action or is journalled beside it, and
  the receipt names the per-environment results;
* the page under node: every tab renders from the fresh workspace's real
  responses, standalone and embedded through ``window.DaltonV2``; a write
  without a reason sends nothing, and with one it posts to the action route;
  dangerous actions confirm and are styled apart.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core.agenda_control import (
    AgendaControlApplication, AgendaControlConfig, AgendaControlPlane,
)
from dalton_core.cockpit_plane import CockpitConfig, CockpitPlane
from dalton_core.cockpit_v2.admin import data as admin_data
from dalton_core.cockpit_v2.admin import routes as admin_routes
from dalton_core.cockpit_v2.admin.routes import (
    ACTIONS, ADMIN_ACTION_PATH, DATA_SECTIONS, ROUTES, AdminRefusal, admin_handler,
    dispatch, perform_action, register_admin_routes, require_human,
)
from dalton_core.store import content_hash
from dalton_core.workspace_creation import create_blank_workspace

ROOT = Path(__file__).resolve().parents[1]
ADMIN_DIR = ROOT / "src" / "dalton_core" / "cockpit_v2" / "admin"
ADMIN_JS = ADMIN_DIR / "admin.js"
LOGIN = "owner@example.com"
HOST = "dalton.example.ts.net"


def _node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    fallback = Path.home() / ".openclaw" / "tools" / "node" / "bin" / "node"
    return str(fallback) if fallback.is_file() else None


def page_get_paths() -> list[str]:
    """Every path the page reads with getJson, from its own source."""

    source = ADMIN_JS.read_text(encoding="utf-8")
    return sorted(set(re.findall(r'getJson\("(/[^"]+)"\)', source)))


class Dashboard:
    def list_agenda_feedback_targets(self, **_kwargs):
        return []

    def answer_subjects(self):
        return []

    def intent_context_bindings(self):
        return []


class FreshWorkspace:
    """A blank workspace, its cockpit, and the cockpit's HTTP surface with the
    administrator routes in front (the standalone handler)."""

    def __init__(self, case: unittest.TestCase) -> None:
        tmp = tempfile.TemporaryDirectory(dir="/tmp")
        case.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        release = root / "release"
        release.mkdir()
        catalog = {"schema_version": "dalton-shared-connection-catalog-0.1", "models": [], "sources": []}
        catalog["content_hash"] = content_hash(catalog)
        catalog_path = root / "connections.json"
        catalog_path.write_text(json.dumps(catalog))
        receipt = create_blank_workspace(
            root, "new", 18953, "release:sha256:" + "c" * 64, release,
            request_id="admin-canary", display_name="新研究", shared_readonly_paths=[catalog_path],
            connection_catalog=dict(path=str(catalog_path), content_hash=catalog["content_hash"]))
        env = patch.dict(os.environ, DALTON_WORKSPACE_MANIFEST=receipt["manifest_path"])
        env.start()
        case.addCleanup(env.stop)
        self.state = root / "workspaces/new/state/dalton-core"
        self.governance_calls: list[tuple[str, dict]] = []

        def governance(*_args, operation, params, actor_ref, **_kwargs):
            self.governance_calls.append((operation, dict(params), actor_ref))
            return {"status": "applied", "receipt": {"outcome": "applied", "id": "receipt:test"}}

        self.cockpit = CockpitPlane(
            CockpitConfig(core_db=self.state / "core.sqlite", state_dir=self.state,
                          heartbeat_path=self.state / "run/heartbeat.json",
                          scheduler_db=self.state / "scheduler.sqlite",
                          journal_path=self.state / "cockpit/journal.sqlite"),
            writer_socket=self.state / "run/writer.sock", token_config=self.state / "writer-tokens.json",
            governance_call=governance)
        case.addCleanup(self.cockpit.close)
        config = AgendaControlConfig.from_mapping({
            "host": "127.0.0.1", "port": 8794, "tailscale_host": HOST,
            "tailscale_executable": sys.executable, "allowed_tailscale_logins": [LOGIN],
            "writer_socket": str(root / "writer.sock"), "token_config": str(root / "tokens.json"),
            "endpoint_ref": "openclaw:discord:test", "feedback_timeout_seconds": 86400,
            "sweep_interval_seconds": 60,
        })
        plane = AgendaControlPlane(config, dashboard_client=Dashboard(), timeout_client=Dashboard())
        self.app = AgendaControlApplication(config, plane, None, None, self.cockpit)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), admin_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        case.addCleanup(self.server.server_close)
        case.addCleanup(self.server.shutdown)

    def request(self, method: str, path: str, *, body: dict | None = None, login: str = LOGIN,
                csrf: str | None = None, cookie: str | None = None) -> tuple[int, dict, bytes]:
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

    def session(self) -> tuple[str, str]:
        status, headers, body = self.request("GET", "/v2/data/admin/actions")
        assert status == 200, body
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        return cookie, json.loads(body)["csrf_token"]


class RouteRegistrationTests(unittest.TestCase):
    def test_add_route_shape_registers_page_assets_data_and_action(self):
        seen = []

        class App:
            def add_route(self, method, path, handler, prefix=False):
                seen.append((method, path, prefix, handler))

        register_admin_routes(App())
        self.assertIn(("GET", "/v2/admin", False), [(m, p, x) for m, p, x, _ in seen])
        self.assertIn(("GET", "/v2/data/admin/", True), [(m, p, x) for m, p, x, _ in seen])
        self.assertIn(("POST", ADMIN_ACTION_PATH, False), [(m, p, x) for m, p, x, _ in seen])
        self.assertTrue(all(callable(h) for *_rest, h in seen))

    def test_add_route_without_prefix_keyword_still_registers(self):
        seen = []

        class App:
            def add_route(self, method, path, handler):
                seen.append((method, path))

        register_admin_routes(App())
        self.assertEqual(len(seen), len(ROUTES))

    def test_routes_list_dict_and_plain_object_shapes(self):
        listed = SimpleNamespace(routes=[])
        register_admin_routes(listed)
        self.assertEqual(len(listed.routes), len(ROUTES))
        table: dict = {}
        register_admin_routes(table)
        self.assertIn(("POST", ADMIN_ACTION_PATH), table)
        bare = SimpleNamespace()
        self.assertIs(register_admin_routes(bare), ROUTES)
        self.assertIs(bare.v2_admin_dispatch, dispatch)

    def test_dispatch_ignores_paths_that_are_not_the_administrators(self):
        self.assertIsNone(dispatch(object(), "GET", "/v1/cockpit/overview", login=LOGIN))
        self.assertIsNone(dispatch(object(), "POST", "/v1/cockpit/lever", login=LOGIN))
        self.assertIsNone(dispatch(object(), "GET", "/v2/research", login=LOGIN))

    def test_embedded_shell_registers_the_admin_routes_when_present(self):
        try:
            from dalton_core.cockpit_v2 import routes as shell_routes  # agent A's module
        except ImportError:
            self.skipTest("the v2 shell's routes module is not on this branch yet")
        source = Path(shell_routes.__file__).read_text(encoding="utf-8")
        self.assertIn("register_admin_routes", source)

    def test_v2_router_shape_adds_page_reads_and_post_when_served(self):
        calls = []

        class Router:
            extensions: dict = {}

            def add_page(self, path, html):
                calls.append(("page", path, Path(html)))

            def add_json(self, path, handler, *, prefix=False):
                calls.append(("json", path, prefix, handler))

            def add_post(self, path, handler):
                calls.append(("post", path, handler))

        register_admin_routes(Router())
        self.assertEqual(calls[0][:2], ("page", "/v2/admin"))
        self.assertTrue(calls[0][2].is_file())
        self.assertEqual(calls[1][:3], ("json", "/v2/data/admin/", True))
        self.assertEqual(calls[2][:2], ("post", ADMIN_ACTION_PATH))
        self.assertIs(admin_routes.register, register_admin_routes)
        # A router without POST support still gets the page and the reads.
        calls.clear()
        router = Router()
        router.add_post = None
        register_admin_routes(router)
        self.assertEqual([c[0] for c in calls], ["page", "json"])

    def test_v2_json_handler_answers_and_refuses_in_words(self):
        from dalton_core.cockpit_plane import CockpitError

        w = FreshWorkspace(self)
        index = admin_routes.v2_json_handler(w.app, LOGIN, {}, "/v2/data/admin/")
        self.assertEqual(index["sections"], list(DATA_SECTIONS))
        health = admin_routes.v2_json_handler(w.app, LOGIN, {}, "/v2/data/admin/health")
        self.assertTrue(health["enabled"])
        with self.assertRaises(CockpitError):
            admin_routes.v2_json_handler(w.app, LOGIN, {}, "/v2/data/admin/action")
        with self.assertRaises(CockpitError):
            admin_routes.v2_json_handler(w.app, LOGIN, {}, "/v2/data/admin/nothing")

    def test_the_shells_router_registers_the_admin_page_when_present(self):
        try:
            from dalton_core.cockpit_v2.routes import register_v2_routes
        except ImportError:
            self.skipTest("the v2 shell's routes module is not on this branch yet")
        router = register_v2_routes(SimpleNamespace())
        self.assertEqual(router.extensions.get("dalton_core.cockpit_v2.admin.routes"), "registered")
        self.assertIn("/v2/admin", router.pages)
        self.assertEqual(router.get("/v2/static/admin/admin.js", LOGIN, {}).status, 200)

    def test_assets_ship_beside_the_routes_module(self):
        for name in ("admin.html", "admin.css", "admin.js"):
            self.assertTrue((ADMIN_DIR / name).is_file(), name)
        html = (ADMIN_DIR / "admin.html").read_text(encoding="utf-8")
        self.assertIn('id="admin-root"', html)
        self.assertIn("/v2/static/admin/admin.js", html)
        self.assertIn("/v2/static/admin/admin.css", html)


class StandaloneHttpTests(unittest.TestCase):
    def setUp(self):
        self.w = FreshWorkspace(self)

    def test_page_and_assets_are_served_with_the_cockpit_headers(self):
        for path, kind in (("/v2/admin", "text/html"), ("/v2/admin/", "text/html"),
                           ("/v2/static/admin/admin.css", "text/css"),
                           ("/v2/static/admin/admin.js", "text/javascript"),
                           ("/v2/admin/admin.js", "text/javascript")):
            with self.subTest(path=path):
                status, headers, body = self.w.request("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith(kind))
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertTrue(body)
        status, _h, _b = self.w.request("GET", "/v2/admin/nothing.js")
        self.assertEqual(status, 404)

    def test_same_auth_as_the_cockpit(self):
        status, _h, _b = self.w.request("GET", "/v2/admin", login="someone@else.example")
        self.assertEqual(status, 403)
        status, _h, _b = self.w.request("GET", "/v2/data/admin/health", login="someone@else.example")
        self.assertEqual(status, 403)

    def test_other_paths_fall_through_to_the_cockpit(self):
        status, _h, body = self.w.request("GET", "/v1/cockpit/levers")
        self.assertEqual(status, 200, body[:200])
        status, _h, _b = self.w.request("GET", "/v1/nothing")
        self.assertEqual(status, 404)

    def test_every_admin_and_page_read_answers_a_fresh_workspace_without_5xx(self):
        paths = page_get_paths()
        # The source scan found the page's reads, not an empty list.
        self.assertIn("/v1/cockpit/models", paths)
        self.assertIn("/v2/data/admin/health", paths)
        paths = sorted(set(paths) | {f"/v2/data/admin/{name}" for name in DATA_SECTIONS}
                       | {"/v2/data/admin/actions"})
        for path in paths:
            with self.subTest(path=path):
                status, _h, body = self.w.request("GET", path)
                self.assertLess(status, 500, f"{path} -> {status} {body[:300]!r}")
                value = json.loads(body)
                if status >= 400:
                    self.assertTrue(re.search(r"[一-鿿]", value.get("display_message", "")),
                                    (path, body[:200]))
                elif path.startswith("/v2/data/admin/"):
                    self.assertTrue(value.get("enabled"))
                    self.assertTrue(value.get("csrf_token"))

    def test_sections_report_fresh_state_in_words_not_errors(self):
        for name in DATA_SECTIONS:
            with self.subTest(section=name):
                status, _h, body = self.w.request("GET", f"/v2/data/admin/{name}")
                self.assertEqual(status, 200, body[:300])
        health = json.loads(self.w.request("GET", "/v2/data/admin/health")[2])
        self.assertTrue(health["parity"]["available"], health["parity"])
        self.assertIsInstance(health["incidents"], list)
        kinds = {i["kind"] for i in health["incidents"]}
        self.assertIn("store_thread_stall", kinds)
        self.assertFalse(health["writer"]["heartbeat_present"])
        runtime = json.loads(self.w.request("GET", "/v2/data/admin/runtime")[2])
        self.assertEqual(runtime["owner_door_jobs"]["items"], [])
        commands = [c for s in runtime["guidance"]["sections"] for c in s["commands"]]
        self.assertTrue(any("deploy.sh" in (c.get("apply") or "") for c in commands))
        self.assertTrue(any("space.lumos.dalton.workspace." in (c.get("apply") or "") for c in commands))
        conn = json.loads(self.w.request("GET", "/v2/data/admin/connections")[2])
        self.assertFalse(conn["feishu"]["enabled"])
        self.assertIn("owner-delivery-enable.sh", json.dumps(conn["feishu"]["commands"]))
        costs = json.loads(self.w.request("GET", "/v2/data/admin/costs")[2])
        self.assertIsNone(costs["mission"])
        # No section leaked the owner's login.
        for name in DATA_SECTIONS:
            body = self.w.request("GET", f"/v2/data/admin/{name}")[2].decode()
            self.assertNotIn(LOGIN, body)

    def test_get_on_the_action_route_is_refused_in_words(self):
        status, _h, body = self.w.request("GET", ADMIN_ACTION_PATH)
        self.assertEqual(status, 405)
        self.assertIn("POST", json.loads(body)["display_message"])


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.w = FreshWorkspace(self)
        self.cookie, self.csrf = self.w.session()

    def post(self, body, *, csrf=None, login=LOGIN):
        return self.w.request("POST", ADMIN_ACTION_PATH, body=body, login=login,
                              csrf=self.csrf if csrf is None else csrf, cookie=self.cookie)

    def test_a_login_outside_the_allowlist_cannot_write(self):
        status, _h, _b = self.post({"action": "lever", "reason": "x", "payload": {"lever": "emergency_stop"}},
                                   login="agent@bots.example")
        self.assertEqual(status, 403)
        self.assertEqual(self.w.governance_calls, [])

    def test_payload_naming_a_non_human_actor_is_refused(self):
        for actor in ("agent:planner", "system:cron", "", "human:"):
            with self.subTest(actor=actor):
                status, _h, body = self.post({"action": "lever", "reason": "测试",
                                              "payload": {"lever": "emergency_stop", "actor_ref": actor}})
                self.assertEqual(status, 403, body)
                self.assertEqual(json.loads(body)["error"], "not_human")
        status, _h, body = self.post({"action": "lever", "reason": "测试",
                                      "payload": {"lever": "pause", "approved_by": "human:someone"}})
        self.assertEqual(status, 400)
        self.assertEqual(self.w.governance_calls, [])

    def test_require_human_refuses_every_non_human_principal(self):
        for actor in ("agent:x", "service:writer", None, 3, "human:"):
            with self.subTest(actor=actor):
                with self.assertRaises(AdminRefusal):
                    require_human(actor)
        self.assertEqual(require_human("human:tailscale-abc"), "human:tailscale-abc")

    def test_reason_csrf_shape_and_allowlist(self):
        status, _h, body = self.post({"action": "lever", "reason": "  ", "payload": {"lever": "emergency_stop"}})
        self.assertEqual((status, json.loads(body)["error"]), (400, "reason_required"))
        status, _h, body = self.post({"action": "lever", "reason": "x" * 301, "payload": {}})
        self.assertEqual(status, 400)
        status, _h, body = self.post({"action": "conviction_policy", "reason": "x", "payload": {}})
        self.assertEqual((status, json.loads(body)["error"]), (400, "unknown_action"))
        status, _h, body = self.post({"action": "lever", "payload": {}})
        self.assertEqual(status, 400)
        status, _h, body = self.post({"action": "lever", "reason": "x", "payload": {}}, csrf="wrong")
        self.assertEqual(status, 403)
        self.assertEqual(self.w.governance_calls, [])

    def test_emergency_stop_goes_through_the_cockpit_lever_as_the_owner_with_its_reason(self):
        status, _h, body = self.post({"action": "lever", "reason": "测试停机",
                                      "payload": {"lever": "emergency_stop", "request_id": "r-1"}})
        self.assertLess(status, 500, body)
        if status == 200:
            out = json.loads(body)
            self.assertEqual(out["receipt"]["reason"], "测试停机")
            self.assertTrue(out["receipt"]["actor_ref"].startswith("human:tailscale-"))
            self.assertEqual(out["receipt"]["reason_recorded_with"], "action")
        else:  # the fresh Core refused the lever itself: said in words, no 5xx
            self.assertTrue(re.search(r"[一-鿿]", json.loads(body)["display_message"]))
        for _operation, params, actor in self.w.governance_calls:
            self.assertTrue(actor.startswith("human:"))
            self.assertEqual(params.get("reason"), "测试停机")

    def test_non_native_reason_is_journalled_beside_the_action(self):
        calls = []

        def post_cockpit(action, login, session, csrf, body):
            calls.append((action, json.loads(body)))
            return {"status": "ok", "sync": {"synced": [{"name": "美股 IT 服务", "environment_id": "ws-a"}],
                                             "skipped": [], "failed": [{"name": "空白", "reason": "未运行"}],
                                             "note": "已同步 1 个环境"}}

        journal = []
        app = SimpleNamespace(
            config=None, post_cockpit=post_cockpit,
            cockpit_plane=SimpleNamespace(journal=SimpleNamespace(
                record_event=lambda **kw: journal.append(kw))))
        session = SimpleNamespace(csrf="t")
        out = perform_action(app, login=LOGIN, session=session, csrf="t", body=json.dumps({
            "action": "model_budget", "reason": "提高输出上限",
            "payload": {"purpose": "x", "kind": "call", "budget": {"max_output_tokens": 4096},
                        "expected_config_hash": "h"}}).encode())
        self.assertEqual(calls[0][0], "model_budget")
        self.assertNotIn("reason", calls[0][1])  # closed shapes are forwarded unchanged
        self.assertEqual(journal[0]["detail"], "提高输出上限")
        self.assertEqual(journal[0]["kind"], "admin_reason")
        self.assertEqual(out["receipt"]["reason_recorded_with"], "cockpit_journal")
        self.assertEqual([e["result"] for e in out["receipt"]["environments"]], ["synced", "failed"])
        out = perform_action(app, login=LOGIN, session=session, csrf="t", body=json.dumps({
            "action": "model_select", "reason": "改顺序",
            "payload": {"tier": "verifier", "mode": "tier"}}).encode())
        self.assertEqual(calls[1][1]["reason"], "改顺序")
        self.assertEqual(len(journal), 1)  # native: the action records it

    def test_every_allowlisted_action_is_a_real_cockpit_action(self):
        source = (ROOT / "src/dalton_core/agenda_control.py").read_text(encoding="utf-8")
        for action in ACTIONS:
            with self.subTest(action=action):
                self.assertTrue(re.search(rf'["\']{action}["\']', source), action)


class DataUnitTests(unittest.TestCase):
    def test_cost_fold_by_lane_pool_and_day(self):
        rows = [
            {"day": "2026-10-08", "lane": "document_research", "pool": "coverage", "phase": "assessment",
             "calls": 2, "micros": 1_500_000},
            {"day": "2026-10-05", "lane": "document_research", "pool": "coverage", "phase": "assessment",
             "calls": 1, "micros": 500_000},
            {"day": "2026-10-08", "lane": None, "pool": None, "phase": "verification",
             "calls": 1, "micros": 250_000},
        ]
        folded = admin_data._fold(rows, "2026-10-08")
        self.assertEqual(folded["today"], {"calls": 3, "cost_usd": 1.75})
        self.assertEqual(folded["week"], {"calls": 4, "cost_usd": 2.25})
        lane = folded["by_lane"][0]
        self.assertEqual(lane["lane"], "document_research")
        self.assertEqual(lane["week"]["cost_usd"], 2.0)
        self.assertEqual(folded["by_lane"][1]["label"], "未标注流程（较早的记录）")

    def _plane(self, state: Path) -> SimpleNamespace:
        return SimpleNamespace(config=SimpleNamespace(
            state_dir=state, heartbeat_path=state / "run/heartbeat.json", core_db=state / "core.sqlite",
            model_config_path=None), workspace_context={"mode": "legacy"})

    def test_publication_summary_counts_states_and_offers_language_retry(self):
        state = Path(tempfile.mkdtemp(dir="/tmp"))
        self.addCleanup(shutil.rmtree, state, True)
        products = state / "research-publication-work" / "products"
        products.mkdir(parents=True)
        for index, status in enumerate(["completed", "exhausted", "pending", "exhausted"]):
            (products / f"{index:064x}.json").write_text(json.dumps({
                "status": status, "product_hash": f"{index:064x}",
                "identity": {"kind": "initial_screen", "subject_ref": "company:x", "version_ref": f"v:{index}"}}))
        (products / "broken.json").write_text("{")
        out = admin_data._publication_summary(self._plane(state))
        self.assertEqual(out["counts"], {"completed": 1, "exhausted": 2, "pending": 1})
        self.assertEqual(out["unreadable"], 1)
        self.assertEqual(len(out["exhausted_items"]), 2)
        item = out["exhausted_items"][0]
        self.assertEqual(len(item["identity_hash"]), 64)
        self.assertEqual(item["kind"], "initial_screen")

    def test_connector_holds_read_the_health_records(self):
        from dalton_core.connector_health import ConnectorHealth

        state = Path(tempfile.mkdtemp(dir="/tmp"))
        self.addCleanup(shutil.rmtree, state, True)
        ConnectorHealth(state).record_auth_failure(
            "source:alphaengine", kind="search", ref="q:1", detail={"message": "login expired"})
        out = admin_data.connector_holds(self._plane(state))
        self.assertEqual(len(out["items"]), 1)
        hold = out["items"][0]
        self.assertTrue(hold["held"])
        self.assertEqual(hold["status"], "login_expired")
        self.assertEqual(hold["blocked_searches"], 1)
        self.assertEqual(hold["provider_message"], "login expired")

    def test_governance_records_offer_show_and_approve_commands_only_when_unapproved(self):
        state = Path(tempfile.mkdtemp(dir="/tmp"))
        self.addCleanup(shutil.rmtree, state, True)
        directory = state / "connector-governance"
        directory.mkdir()
        (directory / "a.json").write_text(json.dumps({"id": "a", "status": "approved"}))
        (directory / "b.json").write_text(json.dumps({"id": "b", "status": "proposed",
                                                      "allowed_permissions": {"risk_class": "low"}}))
        out = admin_data.governance_records(self._plane(state), "human:tailscale-abc")
        self.assertEqual(out["pending"], 1)
        by_id = {item["id"]: item for item in out["items"]}
        self.assertNotIn("commands", by_id["a"])
        command = by_id["b"]["commands"][0]
        self.assertIn("connector_governance_cli show", command["preview"])
        self.assertIn("--approved-by human:tailscale-abc", command["apply"])

    def test_known_incident_names_the_store_thread_stall(self):
        incident = admin_data.KNOWN_INCIDENTS[0]
        self.assertEqual(incident["kind"], "store_thread_stall")
        self.assertIn("owner_door_job_events", incident["fix"])


NODE_DOM = r"""
const fs = require("fs");
class ClassList { constructor(el){this.el=el} _l(){return (this.el.className||"").split(/\s+/).filter(Boolean)}
  add(...c){const l=this._l();c.forEach(x=>{if(!l.includes(x))l.push(x)});this.el.className=l.join(" ")}
  remove(...c){this.el.className=this._l().filter(x=>!c.includes(x)).join(" ")}
  contains(c){return this._l().includes(c)} toggle(c,on){if(on===undefined)on=!this.contains(c);on?this.add(c):this.remove(c)} }
class Node_ { constructor(){this.childNodes=[];this.parentNode=null}
  append(...ns){for(const n of ns){const x=n instanceof Node_?n:new Text_(String(n));if(x.parentNode)x.parentNode.childNodes.splice(x.parentNode.childNodes.indexOf(x),1);x.parentNode=this;this.childNodes.push(x)}}
  replaceChildren(...ns){this.childNodes.forEach(c=>c.parentNode=null);this.childNodes=[];this.append(...ns)}
  replaceWith(n){const p=this.parentNode;if(!p)return;const i=p.childNodes.indexOf(this);p.childNodes[i]=n;n.parentNode=p;this.parentNode=null}
  prepend(...ns){const keep=this.childNodes;this.childNodes=[];this.append(...ns);keep.forEach(c=>{c.parentNode=this;this.childNodes.push(c)})}
  remove(){if(this.parentNode)this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this),1);this.parentNode=null}
  get textContent(){return this.childNodes.map(c=>c.textContent).join("")}
  set textContent(v){this.replaceChildren(new Text_(String(v)))} }
class Text_ extends Node_ { constructor(t){super();this.data=t} get textContent(){return this.data} set textContent(v){this.data=String(v)} }
class El extends Node_ { constructor(tag){super();this.tagName=tag.toUpperCase();this.attrs={};this.className="";this.dataset={};this.listeners={};this.classList=new ClassList(this);this.value="";this.checked=false;this.disabled=false;this.hidden=false}
  setAttribute(k,v){if(k==="class")this.className=v;else if(k==="id")this.id=v;else if(k==="value")this.value=v;else if(k==="checked")this.checked=true;else if(k==="disabled")this.disabled=true;else if(k==="hidden")this.hidden=true;else this.attrs[k]=String(v)}
  getAttribute(k){return k==="id"?this.id:k==="class"?this.className:this.attrs[k]}
  addEventListener(t,f){(this.listeners[t]=this.listeners[t]||[]).push(f)}
  dispatch(t){(this.listeners[t]||[]).forEach(f=>f({preventDefault(){},key:"",target:this}))}
  click(){if(!this.disabled)this.dispatch("click")} focus(){}
  set tabIndex(v){this.attrs.tabindex=String(v)} get tabIndex(){return Number(this.attrs.tabindex||0)}
  get all(){const out=[];const walk=n=>n.childNodes.forEach(c=>{if(c instanceof El){out.push(c);walk(c)}});walk(this);return out}
  matches(sel){let m;
    if((m=sel.match(/^\[([a-z-]+)=([^\]]+)\]$/))){const [,k,v]=m;if(k.startsWith("data-")&&this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]===v)return true;return this.getAttribute(k)===v}
    if(sel.startsWith("#"))return this.id===sel.slice(1);
    if(sel.startsWith("."))return this.classList.contains(sel.slice(1));
    return this.tagName===sel.toUpperCase()}
  querySelectorAll(sel){return this.all.filter(e=>e.matches(sel))} querySelector(sel){return this.querySelectorAll(sel)[0]||null} }
const body = new El("body"); const root = new El("main"); root.id = "admin-root"; body.append(root);
global.Node = Node_;
global.document = { readyState: "complete", createElement: t => new El(t), createTextNode: t => new Text_(t),
  getElementById: id => (body.all.find(e => e.id === id) || null), addEventListener(){}, body };
global.location = { hash: "" }; global.history = { replaceState(){} };
const listeners = {}; global.window = global; window.addEventListener = (t,f)=>{listeners[t]=f};
global.crypto = { randomUUID: () => "uuid-" + Math.random().toString(16).slice(2) };
global.navigator = {};
const confirms = []; window.confirm = m => { confirms.push(m); return true; };
const responses = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const posts = [];
global.fetch = async (path, opts = {}) => {
  if (opts.method === "POST" && process.argv[3] === "direct" && path === "/v2/data/admin/action") {
    posts.push({ path, body: JSON.parse(opts.body), headers: opts.headers });
    return { ok: false, status: 404, json: async () => ({ error: "not_found" }) }; }
  if (opts.method === "POST" && path.startsWith("/v1/cockpit/")) { posts.push({ path, body: { action: path.split("/").pop(), reason: JSON.parse(opts.body).reason, payload: JSON.parse(opts.body) }, headers: opts.headers });
    return { ok: true, status: 200, json: async () => ({ status: "applied", receipt: { outcome: "applied" } }) }; }
  if (opts.method === "POST") { posts.push({ path, body: JSON.parse(opts.body), headers: opts.headers });
    return { ok: true, status: 200, json: async () => ({ status: "done", receipt: { action: JSON.parse(opts.body).action, action_label: "测试", reason: JSON.parse(opts.body).reason, actor_ref: "human:tailscale-x", at: "2026-10-08T00:00:00Z" } }) }; }
  const hit = responses[path] || responses[path.split("?")[0]];
  if (!hit) return { ok: false, status: 404, json: async () => ({ display_message: "没有" }) };
  return { ok: hit.status < 400, status: hit.status, json: async () => hit.body };
};
const flush = () => new Promise(r => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 30; i++) await flush(); }
"""

NODE_RUN = r"""
const mode = process.argv[3];
let registered = null;
if (mode === "embedded") {
  window.DaltonV2 = { state: {}, registerPage(name, page) { registered = { name, page }; } };
  root.id = "shell-slot";
}
require(process.argv[4]);
(async () => {
  let mountRoot = root;
  if (mode === "embedded") {
    if (!registered || registered.name !== "admin" || typeof registered.page.render !== "function") throw new Error("not registered");
    registered.page.render(root);
  }
  await settle();
  const tabs = mountRoot.querySelectorAll("[role=tab]").map(t => t.textContent);
  const result = { tabs, panels: {} };
  for (const id of ["ai", "connections", "run", "budget", "health", "projects"]) {
    window.DaltonAdmin.select(id); await settle();
    const p = mountRoot.querySelector("#adm-panel-" + id);
    result.panels[id] = { text: p.textContent, hidden: p.hidden,
      danger: p.querySelectorAll(".danger").length, failures: p.querySelectorAll(".err").length };
  }
  // a write without a reason sends nothing; with one it posts to the action route
  window.DaltonAdmin.select("run"); await settle();
  const panel = mountRoot.querySelector("#adm-panel-run");
  const stop = panel.querySelectorAll("button").find(b => b.textContent === "紧急停机");
  result.has_emergency = !!stop;
  if (stop) {
    stop.click(); await settle();
    result.posts_without_reason = posts.length;
    const reason = panel.querySelectorAll("textarea").find(i => (i.attrs["aria-label"] || "") === "紧急停机的理由");
    reason.value = "测试紧急停机"; stop.click(); await settle();
    // the danger confirmation: consequence in words, the project's name typed back
    const dlg = mountRoot.querySelector("[data-role=confirm]");
    result.dialog = dlg ? dlg.textContent : null;
    if (dlg) {
      const ok = dlg.querySelector("[data-role=confirm-ok]");
      const input = dlg.querySelector("input");
      result.ok_disabled_untyped = ok.disabled;
      ok.click(); await settle();
      result.posts_before_typing = posts.length;
      input.value = "别的项目"; input.dispatch("input");
      result.ok_disabled_wrong = ok.disabled;
      input.value = window.DaltonAdmin._state.env.name; input.dispatch("input");
      result.ok_disabled_right = ok.disabled;
      ok.click(); await settle();
      result.dialog_closed = !mountRoot.querySelector("[data-role=confirm]");
    }
    result.posts = posts.map(p => ({ path: p.path, action: p.body.action, reason: p.body.reason, lever: p.body.payload.lever, csrf: p.headers["X-Dalton-CSRF"] }));
    result.confirms = confirms;
    result.receipt = panel.textContent.includes("已完成：测试");
  }
  result.header = mountRoot.querySelector("[data-role=admin-head]").textContent;
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


class AdminPageNodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = _node()

    def setUp(self):
        if self.node is None:
            self.skipTest("node is unavailable")
        self.w = FreshWorkspace(self)
        responses = {}
        for path in sorted(set(page_get_paths()) | {"/v1/cockpit/log?limit=200"}):
            status, _h, body = self.w.request("GET", path)
            responses[path] = {"status": status, "body": json.loads(body)}
        tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "responses.json").write_text(json.dumps(responses, ensure_ascii=False))
        (self.dir / "run.js").write_text(NODE_DOM + NODE_RUN)

    def run_page(self, mode: str) -> dict:
        result = subprocess.run(
            [self.node, str(self.dir / "run.js"), str(self.dir / "responses.json"), mode, str(ADMIN_JS)],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        return json.loads(result.stdout.strip().splitlines()[-1])

    def check(self, out: dict) -> None:
        self.assertEqual(out["tabs"], ["AI 服务", "数据连接", "运行控制", "预算与费用", "健康与故障", "研究项目管理"])
        for tab, panel in out["panels"].items():
            with self.subTest(tab=tab):
                self.assertFalse(panel["hidden"])
                self.assertNotIn("正在读取…", panel["text"][:20])
                self.assertNotIn("undefined", panel["text"])
                self.assertNotIn("[object ", panel["text"])
                self.assertNotIn("null", panel["text"])
        self.assertIn("飞书推送", out["panels"]["connections"]["text"])
        self.assertIn("检索计划", out["panels"]["connections"]["text"])
        self.assertIn("重启与部署", out["panels"]["run"]["text"])
        self.assertIn("deploy.sh", out["panels"]["run"]["text"])
        self.assertIn("失败台账", out["panels"]["health"]["text"])
        self.assertIn("写入服务存储线程阻塞", out["panels"]["health"]["text"])
        self.assertIn("研究预算上限", out["panels"]["budget"]["text"])
        self.assertIn("Excel 模板登记", out["panels"]["projects"]["text"])
        self.assertGreater(out["panels"]["run"]["danger"], 0)
        self.assertIn("研究项目", out["header"])
        if out["has_emergency"]:
            self.assertEqual(out["posts_without_reason"], 0)
            self.assertEqual(len(out["posts"]), 1)
            post = out["posts"][0]
            self.assertEqual((post["path"], post["action"], post["reason"], post["lever"]),
                             (ADMIN_ACTION_PATH, "lever", "测试紧急停机", "emergency_stop"))
            self.assertTrue(post["csrf"])
            # 2026-10-09 (MUSE P0 #9): no window.confirm; a danger dialog whose
            # confirm stays disabled until the project's name is typed back.
            self.assertEqual(out["confirms"], [])
            self.assertIn("后果", out["dialog"])
            self.assertIn("新研究", out["dialog"])
            self.assertTrue(out["ok_disabled_untyped"])
            self.assertEqual(out["posts_before_typing"], 0)
            self.assertTrue(out["ok_disabled_wrong"])
            self.assertFalse(out["ok_disabled_right"])
            self.assertTrue(out["dialog_closed"])
            self.assertTrue(out["receipt"])

    def test_standalone_page_renders_every_tab_from_a_fresh_workspace(self):
        self.check(self.run_page("standalone"))

    def test_embedded_page_registers_with_the_shell_and_renders(self):
        self.check(self.run_page("embedded"))

    def test_writes_fall_back_to_the_cockpit_action_route_when_the_post_is_not_served(self):
        out = self.run_page("direct")
        if not out["has_emergency"]:
            self.skipTest("no emergency control rendered")
        self.assertEqual([p["path"] for p in out["posts"]], [ADMIN_ACTION_PATH, "/v1/cockpit/lever"])
        direct = out["posts"][1]
        self.assertEqual((direct["lever"], direct["reason"]), ("emergency_stop", "测试紧急停机"))
        self.assertTrue(direct["csrf"])

    def test_page_uses_no_inner_html(self):
        source = ADMIN_JS.read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("outerHTML", source)
        result = subprocess.run([self.node, "--check", str(ADMIN_JS)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


NODE_POPULATED = r"""
require(process.argv[4]);
(async () => {
  await settle();
  const out = { panels: {} };
  for (const id of ["ai", "connections", "run", "budget", "health", "projects"]) {
    window.DaltonAdmin.select(id); await settle();
    const p = root.querySelector("#adm-panel-" + id);
    out.panels[id] = p.textContent;
  }
  window.DaltonAdmin.select("ai"); await settle();
  const ai = root.querySelector("#adm-panel-ai");
  const save = ai.querySelectorAll("button").find(b => b.textContent.startsWith("保存「") && b.textContent.includes("选择顺序"));
  out.has_tier_save = !!save;
  if (save) {
    const row = (function up(n){ while (n && !(n.classList && n.classList.contains("adm-row"))) n = n.parentNode; return n; })(save);
    const sel = row.querySelectorAll("select").find(x => (x.attrs["aria-label"] || "").endsWith("添加备用服务"));
    const option = sel && sel.querySelectorAll("option").find(o => o.value && !o.disabled);
    if (option) { sel.value = option.value; row.querySelectorAll("button").find(b => b.textContent === "添加备用服务").click(); }
    out.added = option ? option.value : null;
    const reason = row.querySelectorAll("textarea").find(i => (i.attrs["aria-label"] || "").endsWith("的理由") && (i.attrs["aria-label"] || "").startsWith("保存「"));
    reason.value = "测试顺序"; save.click(); await settle();
    const dlg = root.querySelector("[data-role=confirm]");
    out.dialog = dlg ? dlg.textContent : null;
    if (dlg) {
      const input = dlg.querySelector("input");
      if (input) { input.value = window.DaltonAdmin._state.env.name; input.dispatch("input"); }
      dlg.querySelector("[data-role=confirm-ok]").click(); await settle();
    }
    out.posts = posts.map(p => p.body);
  }
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


class PopulatedAdminPageNodeTests(unittest.TestCase):
    """The page over a workspace with a mission, models, verifier pairs, companies
    and templates (the cockpit plane test harness), not only empty states."""

    def setUp(self):
        self.node = _node()
        if self.node is None:
            self.skipTest("node is unavailable")
        from tests.test_cockpit_plane import CockpitHarness

        tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        harness = CockpitHarness(Path(tmp.name))
        self.addCleanup(harness.close)
        app = AgendaControlApplication(None, None, None, None, harness.plane)
        responses = {}
        for path in sorted(set(page_get_paths()) | {"/v1/cockpit/log?limit=200"}):
            base, _sep, query = path.partition("?")
            if base.startswith("/v2/"):
                response = dispatch(app, "GET", base, login=LOGIN, session=SimpleNamespace(csrf="t"))
                responses[path] = {"status": response.status, "body": json.loads(response.body)}
            else:
                params = dict(item.split("=", 1) for item in query.split("&")) if query else {}
                responses[path] = {"status": 200, "body": json.loads(json.dumps(
                    app.cockpit_view(base, LOGIN, params), default=str))}
        self.responses = responses
        self.dir = Path(tempfile.mkdtemp(dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "responses.json").write_text(json.dumps(responses, ensure_ascii=False))
        (self.dir / "run.js").write_text(NODE_DOM + NODE_POPULATED)

    def test_populated_tabs_render_and_a_tier_save_posts_its_chain(self):
        for path in ("/v2/data/admin/connections", "/v2/data/admin/costs", "/v2/data/admin/health"):
            self.assertEqual(self.responses[path]["status"], 200, path)
        costs = self.responses["/v2/data/admin/costs"]["body"]
        self.assertNotEqual(costs["local"].get("available"), False, costs["local"])
        result = subprocess.run(
            [self.node, str(self.dir / "run.js"), str(self.dir / "responses.json"), "standalone", str(ADMIN_JS)],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        out = json.loads(result.stdout.strip().splitlines()[-1])
        for tab, text in out["panels"].items():
            with self.subTest(tab=tab):
                self.assertNotIn("[object ", text)
                self.assertNotIn("undefined", text)
                self.assertNotIn("没有完成：", text)  # no read failed
        self.assertIn("核验独立性", out["panels"]["ai"])
        self.assertIn("暂停研究", out["panels"]["run"])
        self.assertIn("提高跟踪频率", out["panels"]["run"])
        self.assertIn("保存研究预算上限", out["panels"]["budget"])
        models = self.responses["/v1/cockpit/models"]["body"]
        if models.get("available") and models.get("tier_cards"):
            self.assertTrue(out["has_tier_save"])
            self.assertIn("全部研究项目", out["dialog"])  # the blast radius is named
            self.assertEqual(len(out["posts"]), 1)
            post = out["posts"][0]
            self.assertEqual(post["action"], "model_select")
            self.assertEqual(post["reason"], "测试顺序")
            self.assertEqual(post["payload"]["mode"], "explicit")
            self.assertEqual(post["payload"]["chain"], [out["added"]])
            self.assertTrue(post["payload"]["tier"])


class AdminStyleTests(unittest.TestCase):
    """admin.css: the first cockpit's navy by default, its light tokens on
    prefers-color-scheme: light, every text/background pair >= 4.5:1 in both."""

    ADMIN_AA_PAIRS = [("fg", "bg"), ("fg", "card"), ("fg", "raised"), ("muted", "bg"), ("muted", "card"), ("muted", "raised"),
                      ("accent", "bg"), ("accent", "card"), ("accent", "raised"), ("ok", "card"), ("ok", "ok-bg"),
                      ("warn", "card"), ("warn", "warn-bg"), ("err", "card"), ("err", "danger-bg"), ("danger", "danger-bg"),
                      ("fg", "ok-bg"), ("fg", "warn-bg"), ("fg", "danger-bg"), ("fg", "code"), ("muted", "danger-bg"),
                      ("danger-btn-ink", "danger-btn"), ("accent-ink", "grad-a"), ("accent-ink", "grad-b")]

    @staticmethod
    def _ratio(a, b):
        def lum(h):
            c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
            return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
        x, y = lum(a), lum(b)
        return (max(x, y) + 0.05) / (min(x, y) + 0.05)

    def test_both_themes_meet_aa(self):
        css = (ADMIN_DIR / "admin.css").read_text(encoding="utf-8")
        dark = re.search(r"\.adm-root,\.dv2-admin-standalone\{(.*?)\}", css, re.S).group(1)
        light = re.search(r"@media \(prefers-color-scheme: light\)\{\s*\.adm-root,\.dv2-admin-standalone\{(.*?)\}", css, re.S).group(1)
        self.assertIn("color-scheme:dark", dark)
        self.assertIn("--adm-bg:#0d1326", dark)
        for theme, block in (("dark", dark), ("light", light)):
            colours = dict(re.findall(r"--adm-([a-z0-9-]+):(#[0-9a-f]{6})", block))
            for fg, bg in self.ADMIN_AA_PAIRS:
                with self.subTest(theme=theme, pair=(fg, bg)):
                    self.assertGreaterEqual(self._ratio(colours[fg], colours[bg]), 4.5)

    def test_focus_is_visible_and_motion_respects_the_setting(self):
        css = (ADMIN_DIR / "admin.css").read_text(encoding="utf-8")
        self.assertIn("outline:3px solid var(--adm-focus)", css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)


if __name__ == "__main__":
    unittest.main()
