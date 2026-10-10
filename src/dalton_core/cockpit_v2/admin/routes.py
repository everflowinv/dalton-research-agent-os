"""HTTP surface of the administrator page: ``/v2/admin`` and ``/v2/data/admin/*``.

Contract with the cockpit v2 shell (``cockpit_v2/routes.py``, ADR-0016):

* ``register(router)`` / ``register_admin_routes(router)`` -- the same function.
  Given the shell's ``V2Router`` it adds the page (``add_page("/v2/admin")``),
  the reads (``add_json("/v2/data/admin/", v2_json_handler, prefix=True)``) and,
  when the router serves POSTs (``add_post``), the action route.  The page's
  script and style are served by the shell's static route at
  ``/v2/static/admin/``.  Other shapes are accepted too: anything with
  ``add_route(method, path, handler, prefix=...)``, a ``routes`` list, a dict,
  or a plain object (the table is attached as ``v2_admin_routes``).
* ``dispatch(application, method, path, *, login, query, session, csrf, body)``
  -- the one function every entry calls.  Returns an ``AdminResponse`` or
  ``None`` when the path is not the administrator's.  ``application`` is the
  ``AgendaControlApplication`` (it holds ``cockpit_plane`` and the session
  check), ``login`` is the Tailscale login the cockpit's ``_context`` already
  admitted, ``session`` the cockpit session (for the CSRF token).
* ``admin_handler(application)`` -- the cockpit's request handler with the
  administrator routes in front; the standalone way to serve the page and what
  the tests use.

Reads: the page reuses the existing ``/v1/cockpit/*`` views; the few things no
view serves are under ``/v2/data/admin/<section>`` (see ``data.py``).

Writes: every administrator write goes through ``POST /v2/data/admin/action``
(where the HTTP surface does not route that POST yet, the page sends the same
action to ``/v1/cockpit/<action>`` -- the identical writer path -- and builds
the receipt itself)
with ``{action, reason, payload}``.  The action is one of the existing cockpit
actions and goes out through ``AgendaControlApplication.post_cockpit`` --
exactly the writer path the old page used, as the owner's ``human:`` principal.
This route adds three things and nothing else: a closed allowlist, a required
reason (passed through where the action already records one, otherwise kept
as a cockpit journal event beside the action's own), and a receipt.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

ADMIN_PAGE_PATH = "/v2/admin"
ADMIN_ASSET_PREFIX = "/v2/admin/"
ADMIN_DATA_PREFIX = "/v2/data/admin/"
ADMIN_ACTION_PATH = "/v2/data/admin/action"
#: The page's script and style live where the v2 shell's static route serves
#: files of this package (``/v2/static/admin/...``); the standalone handler
#: serves the same URLs, and the short ``/v2/admin/...`` aliases.
ADMIN_STATIC_PREFIX = "/v2/static/admin/"
ADMIN_SCRIPT_PATH = "/v2/static/admin/admin.js"
ADMIN_STYLE_PATH = "/v2/static/admin/admin.css"

_HERE = Path(__file__).resolve().parent
ADMIN_HTML = _HERE / "admin.html"
ASSETS: dict[str, tuple[str, str]] = {
    ADMIN_PAGE_PATH: ("admin.html", "text/html; charset=utf-8"),
    ADMIN_STYLE_PATH: ("admin.css", "text/css; charset=utf-8"),
    ADMIN_SCRIPT_PATH: ("admin.js", "text/javascript; charset=utf-8"),
    "/v2/admin/admin.css": ("admin.css", "text/css; charset=utf-8"),
    "/v2/admin/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
}

#: The data sections, each a read-only builder in ``data.py``.
DATA_SECTIONS = ("summary", "connections", "costs", "runtime", "health")

MAX_REASON_CHARS = 300
MAX_BODY_BYTES = 16384

#: Every cockpit action the administrator page may send, and how.
#:
#: ``native_reason``: the action already takes and records ``reason``; the
#: route passes the administrator's sentence through.  For the others the
#: payload is forwarded unchanged (several validate a closed shape) and the
#: reason is recorded as a journal event after the action succeeded.
#: ``danger``: the page confirms before sending and styles it apart.
ACTIONS: dict[str, dict[str, Any]] = {
    "model_select": {"label": "调整 AI 服务选择顺序", "native_reason": True},
    "model_budget": {"label": "调整 AI 环节调用上限", "native_reason": False},
    "research_budget": {"label": "调整研究预算上限", "native_reason": False},
    "model_allow": {"label": "允许使用一个 AI 服务", "native_reason": False},
    "model_metadata": {"label": "补充 AI 服务信息", "native_reason": False},
    "model_notice_ack": {"label": "标记 AI 服务变动通知为已读", "native_reason": False},
    "lever": {"label": "运行控制", "native_reason": True},
    "stop_line": {"label": "停止一项专项研究", "native_reason": True, "danger": True},
    "prioritize": {"label": "调整公司跟踪频率", "native_reason": True},
    "coverage_draft": {"label": "生成覆盖名单调整草案", "native_reason": True},
    "publish": {"label": "确认草案并生效", "native_reason": False},
    "decide": {"label": "放弃草案", "native_reason": False},
    "fund_template_select": {"label": "指定 Excel 模板", "native_reason": True},
    "language_retry": {"label": "重新进行语言质检", "native_reason": True},
    # 2026-10-09: a held research task's doors and a card's acknowledgement.
    "owner_door": {"label": "负责人放行 / 撤回一项专项研究", "native_reason": True,
                   "danger": True},
    "needs_human_ack": {"label": "待办「已知悉 / 暂不提醒 / 放弃资料」", "native_reason": True},
    "workspace_lifecycle": {"label": "管理研究项目状态", "native_reason": True},
    "workspace_create": {"label": "新建研究项目", "native_reason": False},
    "workspace_rename": {"label": "重命名研究项目", "native_reason": False},
}
#: Lever values that are dangerous on their own (emergency stop).
DANGEROUS_LEVERS = frozenset({"emergency_stop"})
#: Keys a payload may not carry: the actor is the cockpit login, never the body.
ACTOR_KEYS = frozenset({"actor_ref", "actor", "approved_by", "login", "principal"})


@dataclass(frozen=True)
class AdminResponse:
    status: int
    content_type: str
    body: bytes

    def as_tuple(self) -> tuple[int, str, bytes]:
        return self.status, self.content_type, self.body


class AdminRefusal(Exception):
    """A request the administrator route refuses before anything is sent."""

    def __init__(self, status: int, error: str, display_message: str) -> None:
        super().__init__(display_message)
        self.status = status
        self.error = error
        self.display_message = display_message


def _json(status: int, value: Mapping[str, Any]) -> AdminResponse:
    return AdminResponse(int(status), "application/json; charset=utf-8",
                         json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                                    default=str).encode())


def _refusal(exc: AdminRefusal) -> AdminResponse:
    return _json(exc.status, {"error": exc.error, "message": exc.display_message,
                              "display_message": exc.display_message})


def is_admin_path(path: str) -> bool:
    return (path == ADMIN_PAGE_PATH or path.startswith(ADMIN_ASSET_PREFIX)
            or path.startswith(ADMIN_DATA_PREFIX) or path.startswith(ADMIN_STATIC_PREFIX))


def actor_for_login(login: str) -> str:
    from ...cockpit_plane import _subject_for_login

    return _subject_for_login(login)


def require_human(actor_ref: Any) -> str:
    """The administrator writes as a person or not at all."""

    if not isinstance(actor_ref, str) or not actor_ref.startswith("human:") \
            or len(actor_ref) <= len("human:"):
        raise AdminRefusal(HTTPStatus.FORBIDDEN, "not_human",
                           "管理员写入只接受负责人本人（human: 身份）发起，没有执行。")
    return actor_ref


def _admitted(application: Any, login: Any) -> str:
    if not isinstance(login, str) or not login:
        raise AdminRefusal(HTTPStatus.FORBIDDEN, "forbidden", "没有登录身份，没有执行。")
    allowed = getattr(application, "allowed_login", None)
    if callable(allowed) and getattr(application, "config", None) is not None \
            and allowed(login) is None:
        raise AdminRefusal(HTTPStatus.FORBIDDEN, "forbidden", "这个登录身份不在允许名单里，没有执行。")
    # 2026-10-09 (G02): the page is role-gated server-side, not only hidden.
    # ``admin_logins`` in the service config; absent = every allowed login.
    is_admin = getattr(application, "is_admin_login", None)
    if callable(is_admin) and getattr(application, "config", None) is not None \
            and not is_admin(login):
        raise AdminRefusal(HTTPStatus.FORBIDDEN, "admin_only",
                           "管理员设置只对管理员开放；请联系管理员处理。")
    return login


def _plane(application: Any) -> Any:
    plane = getattr(application, "cockpit_plane", None)
    if plane is None:
        raise AdminRefusal(HTTPStatus.BAD_REQUEST, "cockpit",
                           "这台机器没有配置投研工作台，管理员页面没有数据。")
    return plane


# -- GET -------------------------------------------------------------------------


def _asset(path: str) -> AdminResponse | None:
    entry = ASSETS.get(ADMIN_PAGE_PATH if path == ADMIN_ASSET_PREFIX else path)
    if entry is None:
        return None
    name, content_type = entry
    return AdminResponse(HTTPStatus.OK, content_type, (_HERE / name).read_bytes())


def _section(application: Any, name: str, login: str) -> dict[str, Any]:
    from . import data

    plane = _plane(application)
    if name == "summary":
        return data.summary(plane)
    if name == "connections":
        return data.connections(plane, require_human(actor_for_login(login)))
    if name == "costs":
        return data.costs(plane)
    if name == "health":
        return data.health(plane)
    if name == "runtime":
        try:
            needs = plane.needs_human()
        except Exception:  # noqa: BLE001 - commands are a convenience here
            needs = None
        return data.runtime(plane, needs)
    raise AdminRefusal(HTTPStatus.NOT_FOUND, "not_found", "没有这个管理员数据分区。")


def data_value(application: Any, name: str, login: str) -> dict[str, Any]:
    """One ``/v2/data/admin/<name>`` read (without the CSRF token)."""

    if name == "":
        return {"sections": list(DATA_SECTIONS), "action_path": ADMIN_ACTION_PATH,
                "note": "管理员数据分区；写入一律 POST 到 action_path。"}
    if name == "action":
        raise AdminRefusal(HTTPStatus.METHOD_NOT_ALLOWED, "method", "管理员操作只接受 POST。")
    if name == "actions":
        return {"actions": {
            key: {"label": meta["label"], "danger": bool(meta.get("danger")),
                  "native_reason": bool(meta.get("native_reason"))}
            for key, meta in ACTIONS.items()},
            "dangerous_levers": sorted(DANGEROUS_LEVERS)}
    return _section(application, name, login)


def handle_get(application: Any, path: str, *, login: str,
               session: Any = None) -> AdminResponse | None:
    if not is_admin_path(path):
        return None
    try:
        _admitted(application, login)
        asset = _asset(path)
        if asset is not None:
            return asset
        if path.startswith(ADMIN_DATA_PREFIX):
            value = {**data_value(application, path[len(ADMIN_DATA_PREFIX):].strip("/"), login),
                     "enabled": True}
            csrf = getattr(session, "csrf", None)
            if csrf:
                value["csrf_token"] = csrf
            return _json(HTTPStatus.OK, value)
        raise AdminRefusal(HTTPStatus.NOT_FOUND, "not_found", "没有这个管理员页面。")
    except AdminRefusal as exc:
        return _refusal(exc)
    except Exception as exc:  # noqa: BLE001 - a read that fails says so, as 503
        return _json(HTTPStatus.SERVICE_UNAVAILABLE, {
            "error": "unavailable", "display_message": "管理员数据暂时读不到，请稍后重试。",
            "message": f"{type(exc).__name__}: {str(exc)[:200]}"})


# -- POST ------------------------------------------------------------------------


def _parse(body: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdminRefusal(HTTPStatus.BAD_REQUEST, "invalid_request",
                           "请求内容无法解析，没有执行。") from exc
    if not isinstance(value, Mapping) or not {"action", "reason", "payload"} <= set(value) \
            or set(value) - {"action", "reason", "payload"}:
        raise AdminRefusal(HTTPStatus.BAD_REQUEST, "invalid_request",
                           "管理员操作需要 action、reason 和 payload 三项，没有执行。")
    return value


def _reason(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdminRefusal(HTTPStatus.BAD_REQUEST, "reason_required",
                           "请写一句操作理由：它会和这次操作一起存档。")
    reason = value.strip()
    if len(reason) > MAX_REASON_CHARS:
        raise AdminRefusal(HTTPStatus.BAD_REQUEST, "reason_too_long",
                           f"理由请控制在 {MAX_REASON_CHARS} 字以内。")
    return reason


def _receipt_summary(result: Any) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {}
    keep = ("status", "outcome", "version", "reload_note", "reload_instruction", "message",
            "requires_restart", "draft_id", "application_status")
    out = {key: result[key] for key in keep if key in result and not isinstance(result[key], (dict, list))}
    receipt = result.get("receipt")
    if isinstance(receipt, Mapping):
        out["receipt_outcome"] = receipt.get("outcome")
        out["receipt_ref"] = receipt.get("id") or receipt.get("receipt_ref")
    sync = result.get("sync")
    if isinstance(sync, Mapping):
        out["environments"] = (
            [{"name": x.get("name") or x.get("environment_id"), "result": "synced",
              "note": x.get("note")} for x in sync.get("synced") or []]
            + [{"name": x.get("name") or x.get("environment_id"), "result": "unchanged",
                "note": x.get("reason")} for x in sync.get("skipped") or []]
            + [{"name": x.get("name") or x.get("environment_id"), "result": "failed",
                "note": x.get("reason")} for x in sync.get("failed") or []])
        out["sync_note"] = sync.get("note")
    return out


def perform_action(application: Any, *, login: str, session: Any, csrf: Any,
                   body: bytes) -> dict[str, Any]:
    """Validate, send through the cockpit's own action path, return the receipt."""

    login = _admitted(application, login)
    actor = require_human(actor_for_login(login))
    held = getattr(session, "csrf", None)
    if not isinstance(csrf, str) or not isinstance(held, str) \
            or not secrets.compare_digest(csrf, held):
        raise PermissionError("invalid CSRF token")
    value = _parse(body)
    action = value["action"]
    meta = ACTIONS.get(action) if isinstance(action, str) else None
    if meta is None:
        raise AdminRefusal(HTTPStatus.BAD_REQUEST, "unknown_action",
                           "管理员页面不支持这个操作，没有执行。")
    reason = _reason(value["reason"])
    payload = value["payload"]
    if not isinstance(payload, Mapping):
        raise AdminRefusal(HTTPStatus.BAD_REQUEST, "invalid_request", "操作内容格式不对，没有执行。")
    for key in ACTOR_KEYS & set(payload):
        if not (isinstance(payload[key], str) and payload[key].startswith("human:")
                and len(payload[key]) > len("human:")):
            raise AdminRefusal(HTTPStatus.FORBIDDEN, "not_human",
                               "操作内容里写了非负责人身份，没有执行：写入一律以当前登录的负责人身份发出。")
        raise AdminRefusal(HTTPStatus.BAD_REQUEST, "actor_in_payload",
                           "操作内容不能自带身份字段：写入一律以当前登录的负责人身份发出。")
    forwarded = dict(payload)
    if meta.get("native_reason") and not forwarded.get("reason"):
        forwarded["reason"] = reason
    result = application.post_cockpit(
        action, login, session, csrf, json.dumps(forwarded, ensure_ascii=False).encode())
    at = datetime.now(timezone.utc).isoformat()
    label = meta["label"]
    if action == "lever":
        label = f"{label}：{forwarded.get('lever')}"
    plane = getattr(application, "cockpit_plane", None)
    journal = getattr(plane, "journal", None)
    if not meta.get("native_reason") and journal is not None:
        try:
            journal.record_event(
                kind="admin_reason", title=f"管理员操作理由：{label}", detail=reason,
                login=login, refs={"action": action})
        except Exception:  # noqa: BLE001 - the action itself has committed
            pass
    return {
        "status": "done",
        "receipt": {"action": action, "action_label": label, "reason": reason,
                    "actor_ref": actor, "at": at,
                    "reason_recorded_with": ("action" if meta.get("native_reason")
                                             else "cockpit_journal"),
                    **_receipt_summary(result)},
        "result": result,
    }


def handle_post(application: Any, path: str, *, login: str, session: Any, csrf: Any,
                body: bytes) -> AdminResponse | None:
    if not path.startswith(ADMIN_DATA_PREFIX):
        return None
    from ...cockpit_plane import CockpitConflict, CockpitError

    try:
        if path != ADMIN_ACTION_PATH:
            raise AdminRefusal(HTTPStatus.NOT_FOUND, "not_found", "没有这个管理员操作。")
        return _json(HTTPStatus.OK, perform_action(
            application, login=login, session=session, csrf=csrf, body=body))
    except AdminRefusal as exc:
        return _refusal(exc)
    except PermissionError:
        return _json(HTTPStatus.FORBIDDEN, {"error": "csrf",
                                            "display_message": "登录状态已变化，请刷新页面"})
    except CockpitConflict as exc:
        from ...agenda_control import _cockpit_error_body
        return AdminResponse(HTTPStatus.CONFLICT, "application/json; charset=utf-8",
                             _cockpit_error_body("conflict", exc))
    except CockpitError as exc:
        from ...agenda_control import _cockpit_error_body
        return AdminResponse(HTTPStatus.BAD_REQUEST, "application/json; charset=utf-8",
                             _cockpit_error_body("cockpit", exc))
    except Exception as exc:  # noqa: BLE001 - every other refusal is a 400 in words
        from ...agenda_control import AgendaControlError
        if isinstance(exc, AgendaControlError) or isinstance(exc, ValueError):
            return _json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request",
                                                  "display_message": "请求内容无效，没有执行。"})
        raise


def dispatch(application: Any, method: str, path: str, *, login: str,
             query: Mapping[str, str] | None = None, session: Any = None,
             csrf: Any = None, body: bytes = b"") -> AdminResponse | None:
    """The administrator's answer to one request, or ``None`` if not ours."""

    del query  # the sections take no parameters today
    if method == "GET":
        return handle_get(application, path, login=login, session=session)
    if method == "POST":
        return handle_post(application, path, login=login, session=session, csrf=csrf, body=body)
    return None


# -- registration ---------------------------------------------------------------


def _get_route(application: Any, request: Any) -> Any:
    return dispatch(application, "GET", request.path, login=request.login,
                    session=getattr(request, "session", None))


def _post_route(application: Any, request: Any) -> Any:
    return dispatch(application, "POST", request.path, login=request.login,
                    session=getattr(request, "session", None),
                    csrf=getattr(request, "csrf", None), body=getattr(request, "body", b""))


ROUTES: tuple[dict[str, Any], ...] = (
    {"method": "GET", "path": ADMIN_PAGE_PATH, "prefix": False, "handler": _get_route},
    {"method": "GET", "path": ADMIN_ASSET_PREFIX, "prefix": True, "handler": _get_route},
    {"method": "GET", "path": ADMIN_DATA_PREFIX, "prefix": True, "handler": _get_route},
    {"method": "POST", "path": ADMIN_ACTION_PATH, "prefix": False, "handler": _post_route},
)


def v2_json_handler(application: Any, login: str, query: Mapping[str, str],
                    path: str) -> dict[str, Any]:
    """``/v2/data/admin/*`` for the v2 shell's router: ``handler(app, login, query,
    path) -> dict``; a refusal is a ``CockpitError`` (the surface's 400)."""

    from ...cockpit_plane import CockpitError

    del query
    try:
        _admitted(application, login)
        return {**data_value(application, path[len(ADMIN_DATA_PREFIX):].strip("/"), login),
                "enabled": True}
    except AdminRefusal as exc:
        if exc.status == HTTPStatus.FORBIDDEN:
            raise PermissionError(exc.display_message) from exc
        raise CockpitError(exc.display_message) from exc


def v2_post_handler(application: Any, login: str, session: Any, csrf: Any,
                    body: bytes, path: str = ADMIN_ACTION_PATH) -> dict[str, Any]:
    """``POST /v2/data/admin/action`` for a router that serves POSTs."""

    from ...cockpit_plane import CockpitError

    if path != ADMIN_ACTION_PATH:
        raise CockpitError("没有这个管理员操作。")
    try:
        return perform_action(application, login=login, session=session, csrf=csrf, body=body)
    except AdminRefusal as exc:
        if exc.status == HTTPStatus.FORBIDDEN:
            raise PermissionError(exc.display_message) from exc
        raise CockpitError(exc.display_message) from exc


def _register_on_v2_router(router: Any) -> None:
    router.add_page(ADMIN_PAGE_PATH, ADMIN_HTML)
    router.add_json(ADMIN_DATA_PREFIX, v2_json_handler, prefix=True)
    for name in ("add_post", "add_json_post", "add_action"):
        add_post = getattr(router, name, None)
        if callable(add_post):
            add_post(ADMIN_ACTION_PATH, v2_post_handler)
            break


def register_admin_routes(app: Any) -> tuple[dict[str, Any], ...]:
    """Make the administrator routes known to ``app`` (see the module docstring)."""

    if callable(getattr(app, "add_page", None)) and callable(getattr(app, "add_json", None)):
        # The v2 shell's ``V2Router`` (cockpit_v2/routes.py, ADR-0016).
        _register_on_v2_router(app)
        return ROUTES
    add: Callable[..., Any] | None = getattr(app, "add_route", None)
    if callable(add):
        for route in ROUTES:
            try:
                add(route["method"], route["path"], route["handler"], prefix=route["prefix"])
            except TypeError:
                add(route["method"], route["path"], route["handler"])
        return ROUTES
    routes = getattr(app, "routes", None)
    if isinstance(routes, list):
        routes.extend(ROUTES)
        return ROUTES
    if isinstance(app, dict):
        for route in ROUTES:
            app[(route["method"], route["path"])] = route
        return ROUTES
    try:
        setattr(app, "v2_admin_routes", ROUTES)
        setattr(app, "v2_admin_dispatch", dispatch)
    except AttributeError:
        pass
    return ROUTES


#: The v2 shell imports ``cockpit_v2.admin.routes`` and calls ``register(router)``.
register = register_admin_routes


def admin_handler(application: Any) -> type:
    """The cockpit's handler with ``/v2/admin`` and ``/v2/data/admin/*`` in front."""

    from ...agenda_control import _handler

    base = _handler(application)

    class AdminHandler(base):  # type: ignore[misc, valid-type]
        def _admin_send(self, response: AdminResponse, session_id: str, created: bool) -> None:
            self._send(response.status, response.content_type, response.body,
                       session_cookie=session_id if created else None)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not is_admin_path(parsed.path):
                return super().do_GET()
            context = self._context()
            if context is None:
                return None
            login, session_id, session, created = context
            query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
            response = dispatch(application, "GET", parsed.path, login=login,
                                query=query, session=session)
            if response is None:
                response = _json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            self._admin_send(response, session_id, created)
            return None

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if not path.startswith(ADMIN_DATA_PREFIX):
                return super().do_POST()
            context = self._context()
            if context is None:
                return None
            login, session_id, session, created = context
            if self.headers.get_content_type() != "application/json":
                self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "application/json",
                           b'{"error":"content_type"}')
                return None
            length = self.headers.get("Content-Length")
            if not isinstance(length, str) or not length.isdigit() or int(length) > MAX_BODY_BYTES:
                self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "application/json",
                           b'{"error":"body_size"}')
                return None
            response = dispatch(application, "POST", path, login=login, session=session,
                                csrf=self.headers.get("X-Dalton-CSRF"),
                                body=self.rfile.read(int(length)))
            if response is None:
                response = _json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            self._admin_send(response, session_id, created)
            return None

    AdminHandler.__name__ = "AdminHandler"
    return AdminHandler


__all__ = [
    "ACTIONS", "ADMIN_ACTION_PATH", "ADMIN_DATA_PREFIX", "ADMIN_PAGE_PATH", "ADMIN_SCRIPT_PATH",
    "ADMIN_STYLE_PATH", "AdminRefusal", "AdminResponse", "DATA_SECTIONS", "ROUTES",
    "admin_handler", "data_value", "dispatch", "is_admin_path", "perform_action", "register",
    "register_admin_routes", "require_human", "v2_json_handler", "v2_post_handler",
]
