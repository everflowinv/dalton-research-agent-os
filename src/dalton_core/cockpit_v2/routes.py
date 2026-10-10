"""One registration helper for everything under ``/v2`` (ADR-0016).

``agenda_control`` calls :func:`register_v2_routes` once per application and
hands every ``GET /v2...`` to :meth:`V2Router.get`; authentication (Tailscale
login, host check, session cookie) has already happened there, exactly as for
``/``.  This module owns the shell: ``/v2`` (and, since batch ad, ``/``) and
the static files beside it.

The administrator interface and the chart endpoints are owned by other
modules.  Each, when present, exposes ``register(router)`` in
``cockpit_v2/admin/routes.py`` or ``cockpit_v2/charts/routes.py`` and adds its
own pages, JSON endpoints and front-end modules through the router's methods
below.  Nobody edits this file to add a route.

    router.add_page("/v2/admin", path_to_html)
    router.add_json("/v2/data/charts/", handler, prefix=True)
        # handler(app, login, query, path) -> dict   (CockpitError -> 400)
    router.add_module("/v2/static/charts/index.js")
        # an ES module the shell imports after its own pages; it registers
        # pages/widgets through window.DaltonV2
    router.add_get("/v2/assets/charts/charts.mjs", raw)
        # raw(query, context) -> (status, content_type, body_bytes); exact
        # path; context is the application's CockpitPlane.  Served as is.
    router.add_post("/v2/data/admin/action", handler)
        # handler(app, login, session, csrf, body, path) -> dict
        # The router has already checked the session's CSRF token; the HTTP
        # surface has checked content type and body size.  PermissionError
        # -> 403, CockpitConflict -> 409, CockpitError -> 400 (display_message).

An extension module may instead (or also) expose ``V2_MODULES`` (a list of
module URLs) and/or ``register_<name>_routes(router)``; both are honoured.

Static files are served from this package directory under ``/v2/static/``:
only listed suffixes, never a path that leaves the directory.

Batch ad (2026-10-08): v2 is the default cockpit.  ``/`` serves the shell and
``/admin`` the administrator page -- the same bytes as ``/v2`` and
``/v2/admin`` (see :data:`V2_ALIASES`), not a redirect, so hash routes such as
``/#/company/<代码>/<章节>`` work on either path.  Everything else (static
files, chart assets, ``/v2/data/*``) keeps its ``/v2/...`` URL, and extensions
still register under ``/v2``.  The previous cockpit page is at ``/legacy``.
"""

from __future__ import annotations

import importlib
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

V2_ROOT = Path(__file__).resolve().parent
SHELL_HTML = V2_ROOT / "shell.html"
STATIC_PREFIX = "/v2/static/"

STATIC_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}

# The short top-level paths of the default cockpit and the registered /v2
# path each one serves (identically; the /v2 paths stay valid aliases).
V2_ALIASES = {"/": "/v2", "/admin": "/v2/admin", "/admin/": "/v2/admin"}

# The shell's own front-end modules, loaded in this order by shell.js.  The
# page modules register themselves through window.DaltonV2.
SHELL_MODULES = (
    "/v2/static/pages/overview.js",
    "/v2/static/pages/companies.js",
    "/v2/static/pages/ask.js",
    "/v2/static/pages/tasks.js",
    "/v2/static/pages/settings.js",
)

# Optional modules owned by the other two builders, imported by name, with the
# named registration function used when the module has no ``register``.
EXTENSION_MODULES = {
    "dalton_core.cockpit_v2.admin.routes": "register_admin_routes",
    "dalton_core.cockpit_v2.charts.routes": "register_chart_routes",
}

# The front end an extension serves from its own routes, loaded by the shell
# only when the extension registered and the route exists (so an absent
# extension costs no 404).  The chart module registers its widgets on load.
EXTENSION_FRONTENDS = {
    "dalton_core.cockpit_v2.charts.routes": {
        "modules": ("/v2/assets/charts/charts.mjs",),
        "styles": ("/v2/assets/charts/charts.css",),
    },
}

JsonHandler = Callable[[Any, str, Mapping[str, str], str], Mapping[str, Any]]
PostHandler = Callable[[Any, str, Any, Any, bytes, str], Mapping[str, Any]]
RawHandler = Callable[[Mapping[str, str], Any], tuple[int, str, bytes]]


class V2NotFound(LookupError):
    """No v2 route answers this path."""


@dataclass
class V2Response:
    status: int
    content_type: str
    body: bytes
    # JSON responses get the session's CSRF token added by the HTTP surface,
    # like every /v1/cockpit read, so a v2 page can POST to /v1/cockpit/*.
    json_value: dict[str, Any] | None = None


@dataclass
class V2Router:
    application: Any = None
    pages: dict[str, Path] = field(default_factory=dict)
    json_exact: dict[str, JsonHandler] = field(default_factory=dict)
    json_prefix: list[tuple[str, JsonHandler]] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    extensions: dict[str, str] = field(default_factory=dict)
    posts: dict[str, PostHandler] = field(default_factory=dict)
    raw_gets: dict[str, RawHandler] = field(default_factory=dict)

    # -- registration -------------------------------------------------------
    def add_page(self, path: str, html: str | Path) -> None:
        if not path.startswith("/v2"):
            raise ValueError("a v2 page lives under /v2")
        self.pages[path.rstrip("/") or "/v2"] = Path(html)

    def add_json(self, path: str, handler: JsonHandler, *, prefix: bool = False) -> None:
        if not path.startswith("/v2/data/"):
            raise ValueError("v2 JSON lives under /v2/data/")
        if prefix:
            self.json_prefix.append((path, handler))
            self.json_prefix.sort(key=lambda item: -len(item[0]))
        else:
            self.json_exact[path] = handler

    def add_get(self, path: str, handler: RawHandler) -> None:
        if not path.startswith("/v2/") or path.startswith(STATIC_PREFIX):
            raise ValueError("a v2 GET lives under /v2/ (outside /v2/static/)")
        self.raw_gets[path] = handler

    def add_style(self, url: str) -> None:
        if not url.startswith("/v2/") or not url.endswith(".css"):
            raise ValueError("a v2 stylesheet is a .css URL under /v2/")
        if url not in self.styles:
            self.styles.append(url)

    def serves(self, path: str) -> bool:
        if path in self.pages or path in self.json_exact or path in self.raw_gets:
            return True
        if path.startswith(STATIC_PREFIX):
            try:
                _static_response(path[len(STATIC_PREFIX):])
            except V2NotFound:
                return False
            return True
        return any(path.startswith(prefix) for prefix, _ in self.json_prefix)

    def add_post(self, path: str, handler: PostHandler) -> None:
        if not path.startswith("/v2/data/"):
            raise ValueError("a v2 POST lives under /v2/data/")
        self.posts[path] = handler

    def add_module(self, url: str) -> None:
        if not url.startswith("/v2/") or not url.endswith((".js", ".mjs")):
            raise ValueError("a v2 module is a .js/.mjs URL under /v2/")
        if url not in self.modules:
            self.modules.append(url)

    # -- reading ------------------------------------------------------------
    def get_routes(self) -> list[str]:
        """Every concrete GET path (for the fresh-workspace canary)."""

        routes = ["/v2", *self.pages, *self.json_exact, *self.raw_gets,
                  *(prefix for prefix, _ in self.json_prefix)]
        routes += [alias for alias, target in V2_ALIASES.items()
                   if alias != "/admin/" and (target == "/v2" or target in self.pages)]
        return sorted(set(routes))

    def manifest(self) -> dict[str, Any]:
        return {
            "schema_version": "cockpit-v2-manifest-0.1",
            "shell_modules": list(SHELL_MODULES),
            "modules": list(self.modules),
            "styles": list(self.styles),
            "pages": sorted(self.pages),
            "posts": sorted(self.posts),
            "extensions": dict(self.extensions),
        }

    def get(self, path: str, login: str, query: Mapping[str, str]) -> V2Response:
        path = V2_ALIASES.get(path, path)
        if path in ("/v2", "/v2/"):
            return _file_response(SHELL_HTML)
        page = self.pages.get(path.rstrip("/"))
        if page is not None:
            return _file_response(page)
        if path.startswith(STATIC_PREFIX):
            return _static_response(path[len(STATIC_PREFIX):])
        raw = self.raw_gets.get(path)
        if raw is not None:
            context = getattr(self.application, "cockpit_plane", None) or self.application
            status, content_type, body = raw(query, context)
            return V2Response(int(status), str(content_type), _owner_facing(int(status), str(content_type), bytes(body)))
        handler = self.json_exact.get(path)
        if handler is None:
            for prefix, candidate in self.json_prefix:
                if path.startswith(prefix):
                    handler = candidate
                    break
        if handler is None:
            raise V2NotFound(path)
        value = dict(handler(self.application, login, query, path))
        return V2Response(200, "application/json; charset=utf-8", b"", json_value=value)


    def handles_post(self, path: str) -> bool:
        return path in self.posts

    def post(self, path: str, login: str, session: Any, csrf: Any, body: bytes) -> dict[str, Any]:
        """One v2 write.  The session's CSRF token is checked here, before any
        handler runs, so no extension can forget it."""

        handler = self.posts.get(path)
        if handler is None:
            raise V2NotFound(path)
        expected = getattr(session, "csrf", None)
        if not isinstance(csrf, str) or not isinstance(expected, str) or not secrets.compare_digest(csrf, expected):
            raise PermissionError("invalid CSRF token")
        return dict(handler(self.application, login, session, csrf, body, path))


def _owner_facing(status: int, content_type: str, body: bytes) -> bytes:
    """A raw refusal carries ``display_message`` like every cockpit refusal:
    the page shows it verbatim.  A Chinese ``message`` is promoted; anything
    else gets a plain sentence (the original stays in ``message``)."""

    if status < 400 or "json" not in content_type:
        return body
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(value, dict) or isinstance(value.get("display_message"), str):
        return body
    message = value.get("message")
    chinese = isinstance(message, str) and any("一" <= ch <= "鿿" for ch in message)
    value["display_message"] = message if chinese else "这项数据暂时无法读取。"
    return json_body(value)


def _file_response(path: Path) -> V2Response:
    suffix = path.suffix.lower()
    if suffix not in STATIC_TYPES or not path.is_file():
        raise V2NotFound(str(path))
    return V2Response(200, STATIC_TYPES[suffix], path.read_bytes())


def _static_response(relative: str) -> V2Response:
    if not relative or "\\" in relative or relative.startswith("/"):
        raise V2NotFound(relative)
    parts = relative.split("/")
    if any(part in ("", ".", "..") or part.startswith(".") for part in parts):
        raise V2NotFound(relative)
    target = (V2_ROOT / relative).resolve()
    try:
        target.relative_to(V2_ROOT)
    except ValueError as exc:
        raise V2NotFound(relative) from exc
    if any(part in ("__pycache__",) for part in target.parts):
        raise V2NotFound(relative)
    return _file_response(target)


def _manifest(app: Any, login: str, _query: Mapping[str, str], _path: str) -> dict[str, Any]:
    value = app.v2_router.manifest()
    # 2026-10-09 (G02): 管理员设置 is role-gated server-side; a login that may
    # not open it does not see it listed, so the shell's adminHref() is null
    # and every analyst page hides its administrator link.
    is_admin = getattr(app, "is_admin_login", None)
    allowed = bool(is_admin(login)) if callable(is_admin) else True
    value["admin"] = {"allowed": allowed}
    if not allowed:
        value["pages"] = [page for page in value["pages"] if not page.startswith("/v2/admin")]
        value["posts"] = [post for post in value["posts"] if not post.startswith("/v2/data/admin/")]
    return value


def register_v2_routes(app: Any) -> V2Router:
    """Attach the v2 router to ``app`` once; return it.

    Idempotent: the HTTP surface may be built more than once per application
    (tests do), and extensions must not register twice.
    """

    existing = getattr(app, "v2_router", None)
    if isinstance(existing, V2Router):
        return existing
    router = V2Router(application=app)
    router.add_json("/v2/data/manifest", _manifest)
    # The shell's own reads beyond /v1/cockpit (each documented in its module).
    from . import feedback_data
    feedback_data.register(router)
    for name, named_register in EXTENSION_MODULES.items():
        try:
            module = importlib.import_module(name)
        except ImportError as exc:
            # Only "this extension is not installed" is quiet; an extension
            # that exists but fails to import is a bug and is named.
            missing = getattr(exc, "name", None) or ""
            if missing and name.startswith(missing):
                continue
            router.extensions[name] = f"import_failed:{type(exc).__name__}"
            continue
        register = getattr(module, "register", None)
        if not callable(register):
            register = getattr(module, named_register, None)
        try:
            if callable(register):
                register(router)
            for url in getattr(module, "V2_MODULES", ()) or ():
                router.add_module(url)
            for url in getattr(module, "V2_STYLES", ()) or ():
                router.add_style(url)
            frontend = EXTENSION_FRONTENDS.get(name, {})
            for url in frontend.get("styles", ()):
                if router.serves(url):
                    router.add_style(url)
            for url in frontend.get("modules", ()):
                if router.serves(url):
                    router.add_module(url)
        except Exception as exc:  # an extension must not take the cockpit down
            router.extensions[name] = f"register_failed:{type(exc).__name__}"
            continue
        router.extensions[name] = "registered" if callable(register) else "no_register"
    app.v2_router = router
    return router


def json_body(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


__all__ = ["V2NotFound", "V2Response", "V2Router", "register_v2_routes", "json_body",
           "SHELL_MODULES", "STATIC_PREFIX", "V2_ALIASES"]
