"""Cockpit v2 (ADR-0016): routes, authentication, and a fresh workspace.

* ``register_v2_routes`` attaches one router per application; extension modules
  (administrator interface, charts) join by name when present and can never
  take the cockpit down.
* ``/v2`` is served with the same authentication as ``/``.
* Batch ad: ``/`` and ``/admin`` serve the v2 shell and administrator page
  (``/v2`` and ``/v2/admin`` stay as identical aliases); the previous cockpit
  is at ``/legacy`` and the ADR-0006 review page at ``/legacy/review``.
* On a fresh workspace every v2 GET -- the router's own routes and every
  ``/v1/cockpit/*`` read the v2 pages make -- answers without a 5xx (the batch z
  canary pattern, reused).
"""

from __future__ import annotations

import http.client
import json
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.cockpit_v2 import routes as v2routes
from dalton_core.cockpit_v2.routes import V2NotFound, V2Router, register_v2_routes
from dalton_core.cockpit_plane import CockpitError

from tests.test_blank_workspace_no_5xx import HOST, LOGIN, BlankWorkspaceNo5xxTests, QUERIES

V2_DIR = Path(v2routes.__file__).resolve().parent

# Reads a v2 page makes need a parameter; give one that cannot exist.
V2_QUERIES = {
    **QUERIES,
    "/v1/cockpit/claims": "?company=company:none&limit=20",
    "/v1/cockpit/log": "?limit=1",
    "/v1/cockpit/history": "?kind=goal",
    # The claim feedback bindings (migration 2026-10-08): a ref that cannot exist.
    "/v2/data/feedback": "?company=company:none&refs=claim-version:none",
    # batch ah: the evidence chain and the one comparison, with refs that cannot exist.
    "/v2/data/evidence/source": "?claim=claim-version:none",
    "/v2/data/evidence/document": "?claim=claim-version:none&source=0",
    "/v2/data/evidence/statement-line": "?line=statement-ingest:none%231",
    "/v2/data/charts/comparison": "?sort=risk&dir=asc",
}


def v1_paths_used_by_v2() -> list[str]:
    """Every /v1 path literal in the v2 front end (shell and pages)."""

    found: set[str] = set()
    for path in [V2_DIR / "shell.js", *sorted((V2_DIR / "pages").glob("*.js"))]:
        for match in re.findall(r"/v1/cockpit/[a-z_\-]+", path.read_text(encoding="utf-8")):
            found.add(match)
    found.discard("/v1/cockpit/ui-texts")  # asked with keys; covered by the batch z canary
    return sorted(found)


class App:
    pass


class PackagingTests(unittest.TestCase):
    def test_every_v2_static_file_is_packaged(self):
        import tomllib
        patterns = tomllib.loads((V2_DIR.parents[2] / "pyproject.toml").read_text(encoding="utf-8"))[
            "tool"]["setuptools"]["package-data"]["dalton_core"]
        package = V2_DIR.parent
        files = [p for p in V2_DIR.rglob("*") if p.is_file() and p.suffix in v2routes.STATIC_TYPES]
        self.assertGreater(len(files), 8)
        for path in files:
            relative = path.relative_to(package)
            with self.subTest(file=str(relative)):
                # setuptools globs do not cross "/": the directory must match exactly.
                self.assertTrue(any(Path(pattern).parent == relative.parent and relative.match(pattern)
                                    for pattern in patterns), relative)


class RouterTests(unittest.TestCase):
    def test_register_is_idempotent_and_has_the_manifest(self):
        app = App()
        router = register_v2_routes(app)
        self.assertIs(register_v2_routes(app), router)
        self.assertIs(app.v2_router, router)
        self.assertIn("/v2/data/manifest", router.get_routes())
        manifest = router.get("/v2/data/manifest", "x", {}).json_value
        self.assertEqual(manifest["schema_version"], "cockpit-v2-manifest-0.1")
        for url in manifest["shell_modules"]:
            self.assertTrue((V2_DIR / url[len("/v2/static/"):]).is_file(), url)

    def test_shell_and_static_files(self):
        router = register_v2_routes(App())
        shell = router.get("/v2", "x", {})
        self.assertEqual(shell.content_type, "text/html; charset=utf-8")
        self.assertIn(b'src="/v2/static/shell.js"', shell.body)
        self.assertEqual(router.get("/v2/", "x", {}).body, shell.body)
        js = router.get("/v2/static/pages/overview.js", "x", {})
        self.assertTrue(js.content_type.startswith("text/javascript"))
        self.assertTrue(router.get("/v2/static/shell.css", "x", {}).content_type.startswith("text/css"))
        for bad in ("/v2/static/routes.py", "/v2/static/__init__.py", "/v2/static/../routes.py",
                    "/v2/static/pages/../../routes.py", "/v2/static/", "/v2/static//etc/passwd",
                    "/v2/static/.hidden.js", "/v2/static/missing.js", "/v2/nothing"):
            with self.subTest(path=bad), self.assertRaises(V2NotFound):
                router.get(bad, "x", {})

    def test_extensions_register_pages_json_and_modules(self):
        calls = []

        def register(router: V2Router) -> None:
            router.add_page("/v2/admin", V2_DIR / "shell.html")
            router.add_json("/v2/data/charts/", lambda app, login, query, path: {"path": path, "q": dict(query)},
                            prefix=True)
            router.add_module("/v2/static/charts/index.js")
            calls.append(router)

        module = types.ModuleType("dalton_core.cockpit_v2.charts.routes")
        module.register = register
        with patch.dict(sys.modules, {"dalton_core.cockpit_v2.charts.routes": module}):
            app = App()
            router = register_v2_routes(app)
        self.assertEqual(len(calls), 1)
        self.assertEqual(router.extensions["dalton_core.cockpit_v2.charts.routes"], "registered")
        self.assertEqual(router.get("/v2/admin", "x", {}).content_type, "text/html; charset=utf-8")
        value = router.get("/v2/data/charts/returns", "x", {"company": "c"}).json_value
        self.assertEqual(value, {"path": "/v2/data/charts/returns", "q": {"company": "c"}})
        manifest = router.manifest()
        self.assertEqual(manifest["modules"], ["/v2/static/charts/index.js"])
        self.assertEqual(manifest["pages"], ["/v2/admin"])
        self.assertIn("/v2/data/charts/", router.get_routes())

    def test_raw_gets_named_register_functions_and_v2_modules(self):
        def register_chart_routes(router):
            router.add_get("/v2/assets/charts/x.mjs", lambda query, context: (200, "text/javascript", b"export {}"))
            router.add_get("/v2/data/charts/none", lambda query, context: (404, "application/json", b'{"error":"x"}'))
            router.add_get("/v2/assets/charts/charts.mjs", lambda query, context: (200, "text/javascript", b""))
            router.add_get("/v2/assets/charts/charts.css", lambda query, context: (200, "text/css", b""))

        module = types.ModuleType("dalton_core.cockpit_v2.charts.routes")
        module.register_chart_routes = register_chart_routes
        module.V2_MODULES = ["/v2/assets/charts/x.mjs"]
        with patch.dict(sys.modules, {"dalton_core.cockpit_v2.charts.routes": module}):
            app = App()
            app.cockpit_plane = "plane"
            router = register_v2_routes(app)
        self.assertEqual(router.extensions["dalton_core.cockpit_v2.charts.routes"], "registered")
        # The chart module's own front end is loaded because its routes exist.
        self.assertEqual(router.manifest()["modules"], ["/v2/assets/charts/x.mjs", "/v2/assets/charts/charts.mjs"])
        self.assertEqual(router.manifest()["styles"], ["/v2/assets/charts/charts.css"])
        answer = router.get("/v2/assets/charts/x.mjs", "x", {})
        self.assertEqual((answer.status, answer.body, answer.json_value), (200, b"export {}", None))
        refusal = router.get("/v2/data/charts/none", "x", {})
        self.assertEqual(refusal.status, 404)
        self.assertEqual(json.loads(refusal.body)["display_message"], "这项数据暂时无法读取。")
        with self.assertRaises(ValueError):
            router.add_get("/v2/static/x.js", lambda q, c: (200, "", b""))

    def test_a_broken_extension_never_takes_the_cockpit_down(self):
        def register(_router):
            raise RuntimeError("boom")

        module = types.ModuleType("dalton_core.cockpit_v2.admin.routes")
        module.register = register
        with patch.dict(sys.modules, {"dalton_core.cockpit_v2.admin.routes": module}):
            router = register_v2_routes(App())
        self.assertEqual(router.extensions["dalton_core.cockpit_v2.admin.routes"], "register_failed:RuntimeError")
        self.assertEqual(router.get("/v2", "x", {}).status, 200)

    def test_registration_rejects_paths_outside_v2(self):
        router = register_v2_routes(App())
        with self.assertRaises(ValueError):
            router.add_page("/admin", V2_DIR / "shell.html")
        with self.assertRaises(ValueError):
            router.add_json("/v1/cockpit/x", lambda *a: {})
        with self.assertRaises(ValueError):
            router.add_module("/static/x.js")
        with self.assertRaises(ValueError):
            router.add_post("/v1/cockpit/decide", lambda *a: {})

    def test_an_absent_extension_is_quiet(self):
        router = register_v2_routes(App())
        for name in v2routes.EXTENSION_MODULES:
            if name not in router.extensions:
                continue
            self.assertEqual(router.extensions[name], "registered")


class FreshWorkspaceV2Tests(unittest.TestCase):
    setUp = BlankWorkspaceNo5xxTests.setUp
    get = BlankWorkspaceNo5xxTests.get

    def raw(self, path: str, headers: dict[str, str]) -> tuple[int, bytes, dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=60)
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_every_v2_get_answers_without_a_server_error(self):
        router = self.server.RequestHandlerClass  # noqa: F841 -- the handler built the router
        routes = ["/", "/admin", "/legacy", "/legacy/review",
                  "/v2", "/v2/", "/v2/data/manifest", "/v2/static/shell.js", "/v2/static/shell.css",
                  "/v2/static/copy.js", *[f"/v2/static/pages/{p.name}" for p in sorted((V2_DIR / "pages").glob("*.js"))]]
        # Whatever the extensions registered (administrator interface,
        # charts) is in this canary once merged, without anyone adding it.
        routes += [r for r in self._app_router().get_routes() if r not in routes]
        reads = v1_paths_used_by_v2()
        # The scan found the pages' reads, not an empty list.
        for expected in ("/v1/cockpit/overview", "/v1/cockpit/needs-human", "/v1/cockpit/approvals",
                         "/v1/cockpit/research", "/v1/cockpit/model", "/v1/cockpit/claims",
                         "/v1/cockpit/workspaces", "/v1/cockpit/levers", "/v1/cockpit/history",
                         # migrated from /legacy (2026-10-08)
                         "/v1/cockpit/trajectory", "/v1/cockpit/reflection", "/v1/cockpit/fund-templates",
                         "/v1/cockpit/conviction-policy", "/v1/cockpit/export"):
            self.assertIn(expected, reads)
        self.assertIn("/v2/data/feedback", routes)
        for added in ("/v2/data/evidence/source", "/v2/data/evidence/document", "/v2/data/evidence/statement-line",
                      "/v2/data/charts/comparison"):
            self.assertIn(added, routes)
        self.assertIn("/v2/data/scenarios/revise-group", self._app_router().posts)
        for route in [*routes, *reads]:
            with self.subTest(route=route):
                status, body = self.get(route + V2_QUERIES.get(route, ""))
                self.assertLess(status, 500, f"{route} -> {status} {body[:200]!r}")
                if status >= 400:
                    message = json.loads(body).get("display_message", "")
                    self.assertTrue(re.search(r"[一-鿿]", message), (route, body[:200]))
                elif route.startswith("/v2/static/") or route in ("/", "/v2", "/v2/", "/legacy"):
                    self.assertEqual(status, 200)

    def test_root_is_the_v2_shell_and_the_old_cockpit_is_at_legacy(self):
        shell = (V2_DIR / "shell.html").read_bytes()
        for path in ("/", "/v2", "/v2/"):
            with self.subTest(path=path):
                status, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertEqual(body, shell)
        old = (V2_DIR.parent / "cockpit_control.html").read_bytes()
        status, body = self.get("/legacy")
        self.assertEqual(status, 200)
        self.assertEqual(body, old)
        self.assertNotEqual(body, shell)
        status, body = self.get("/legacy/review")
        self.assertEqual(status, 200)
        self.assertEqual(body, (V2_DIR.parent / "cockpit_control_legacy.html").read_bytes())
        # The old page's own reads and writes are absolute /v1 paths, so it
        # works unchanged one level down; its top bar leads back to "/".
        text = old.decode("utf-8")
        self.assertNotRegex(text, r"""(?:fetch|getJson|postJson)\(\s*["'`](?![/$])""")
        self.assertIn('href="/#/overview" id="try-v2">返回新版<', text)
        self.assertNotIn('href="/v2', text)

    def test_admin_is_an_alias_of_v2_admin(self):
        router = self._app_router()
        if "/v2/admin" not in router.pages:
            self.skipTest("administrator interface not installed")
        self.assertIn("/admin", router.get_routes())
        expected = router.pages["/v2/admin"].read_bytes()
        for path in ("/admin", "/admin/", "/v2/admin"):
            with self.subTest(path=path):
                status, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertEqual(body, expected)

    def test_v2_asset_and_data_paths_are_unchanged_under_the_new_root(self):
        # The shell and admin page reference /v2/static, /v2/assets and
        # /v2/data; those keep answering, and nothing new appears at root.
        self.assertEqual(self.get("/v2/static/shell.js")[0], 200)
        self.assertEqual(self.get("/v2/data/manifest")[0], 200)
        for path in ("/static/shell.js", "/data/manifest", "/admin/nothing", "/legacy/nothing"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)
        router = self._app_router()
        self.assertEqual(router.get("/", "x", {}).body, router.get("/v2", "x", {}).body)

    def test_v2_json_carries_the_session_csrf_token(self):
        status, body = self.get("/v2/data/manifest")
        self.assertEqual(status, 200)
        value = json.loads(body)
        self.assertTrue(value["csrf_token"])
        self.assertIn("/v2/static/pages/overview.js", value["shell_modules"])

    def test_v2_has_the_same_authentication_as_the_old_cockpit(self):
        for path in ("/", "/v2", "/admin", "/legacy", "/legacy/review", "/v2/static/shell.js",
                     "/v2/data/manifest"):
            with self.subTest(path=path):
                status, _, _ = self.raw(path, {"Host": HOST})
                self.assertEqual(status, 403)
                status, _, _ = self.raw(path, {"Host": HOST, "Tailscale-User-Login": "someone@else.example"})
                self.assertEqual(status, 403)
                status, _, _ = self.raw(path, {"Host": "evil.example", "Tailscale-User-Login": LOGIN})
                self.assertEqual(status, 421)
        status, body, headers = self.raw("/v2", {"Host": HOST, "Tailscale-User-Login": LOGIN})
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn(b"<html lang=\"zh-CN\">", body)

    def test_unknown_v2_paths_are_404_not_5xx(self):
        for path in ("/v2/nothing", "/v2/static/routes.py", "/v2/static/../agenda_control.py", "/v2/data/none"):
            with self.subTest(path=path):
                status, _ = self.get(path)
                self.assertEqual(status, 404)

    def _app_router(self) -> V2Router:
        for cell in self.server.RequestHandlerClass.do_GET.__closure__ or ():
            if isinstance(cell.cell_contents, V2Router):
                return cell.cell_contents
        raise AssertionError("the handler holds no V2Router")

    def post(self, path: str, body: bytes, *, csrf: str | None, cookie: str | None,
             content_type: str = "application/json") -> tuple[int, bytes]:
        headers = {"Host": HOST, "Tailscale-User-Login": LOGIN, "Content-Type": content_type,
                   "Content-Length": str(len(body))}
        if csrf is not None:
            headers["X-Dalton-CSRF"] = csrf
        if cookie is not None:
            headers["Cookie"] = cookie
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=60)
        try:
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_extension_posts_are_routed_with_csrf_checked_first(self):
        router = self._app_router()
        seen = []

        def action(app, login, session, csrf, body, path):
            seen.append((login, csrf, json.loads(body), path))
            if json.loads(body).get("refuse"):
                raise CockpitError("请写明理由")
            return {"status": "done"}

        router.add_post("/v2/data/test/action", action)
        status, body, headers = self.raw("/v2/data/manifest", {"Host": HOST, "Tailscale-User-Login": LOGIN})
        self.assertEqual(status, 200)
        self.assertIn("/v2/data/test/action", json.loads(body)["posts"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        csrf = json.loads(body)["csrf_token"]
        payload = json.dumps({"action": "x", "reason": "r"}).encode()

        self.assertEqual(self.post("/v2/data/test/action", payload, csrf=None, cookie=cookie)[0], 403)
        self.assertEqual(self.post("/v2/data/test/action", payload, csrf="wrong", cookie=cookie)[0], 403)
        self.assertEqual(seen, [])                                   # refused before the handler ran
        self.assertEqual(self.post("/v2/data/test/action", payload, csrf=csrf, cookie=cookie,
                                   content_type="text/plain")[0], 415)
        self.assertEqual(self.post("/v2/data/none", payload, csrf=csrf, cookie=cookie)[0], 404)
        status, answer = self.post("/v2/data/test/action", payload, csrf=csrf, cookie=cookie)
        self.assertEqual((status, json.loads(answer)), (200, {"status": "done"}))
        self.assertEqual(seen, [(LOGIN, csrf, {"action": "x", "reason": "r"}, "/v2/data/test/action")])
        status, answer = self.post("/v2/data/test/action", json.dumps({"refuse": True}).encode(), csrf=csrf, cookie=cookie)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(answer)["display_message"], "请写明理由")

    def test_an_extension_refusal_is_a_400_in_chinese(self):
        router = register_v2_routes(object.__new__(App))  # a separate app; the server has its own
        self.assertIsInstance(router, V2Router)
        app_router = None
        # Reach the running application's router through the handler closure.
        for cell in self.server.RequestHandlerClass.do_GET.__closure__ or ():
            if isinstance(cell.cell_contents, V2Router):
                app_router = cell.cell_contents
        self.assertIsNotNone(app_router)

        def refuse(*_args):
            raise CockpitError("请先选择公司")

        app_router.add_json("/v2/data/test-refusal", refuse)
        status, body = self.get("/v2/data/test-refusal")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["display_message"], "请先选择公司")


if __name__ == "__main__":
    unittest.main()
