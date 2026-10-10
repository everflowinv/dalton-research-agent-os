"""Cockpit v2 chart routes: the six data endpoints and the chart assets.

The shell's ``routes.py`` imports :func:`register_chart_routes` by name::

    try:
        from .charts.routes import register_chart_routes
    except ImportError:
        register_chart_routes = None
    if register_chart_routes is not None:
        register_chart_routes(app)

``app`` is whatever route table the shell keeps; this function adapts to it:

* ``app.add_get(path, handler)``                 -- preferred;
* ``app.add_route("GET", path, handler)``         -- method-first;
* ``app.route(path, methods=["GET"])(handler)``   -- decorator style;
* a ``MutableMapping`` -> ``app[("GET", path)] = handler``.

Handlers take ``(query, context)`` and return ``(status, content_type, body)``
with ``body`` as bytes: ``query`` is the parsed query string (one value per
key), ``context`` is the CockpitPlane (or a ``ChartReader``, or a mapping with
``plane``/``reader``/``core_db``).  Data routes are
``/v2/data/charts/<kind>``; assets are ``/v2/assets/charts/<file>``.

Nothing here writes, calls a model, or reaches the network.
"""

from __future__ import annotations

import json
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Callable, Mapping

from ..charts_api import KINDS, ChartRequestError, chart_payload
from ...terminology import scrub_tree

DATA_PREFIX = "/v2/data/charts/"
ASSET_PREFIX = "/v2/assets/charts/"
ASSET_DIR = Path(__file__).resolve().parent
ASSET_TYPES = {".mjs": "text/javascript; charset=utf-8", ".js": "text/javascript; charset=utf-8",
               ".css": "text/css; charset=utf-8"}
ASSETS = ("charts.mjs", "core.mjs", "price.mjs", "model.mjs", "scenarios.mjs", "waterfall.mjs",
          "returns.mjs", "bridge.mjs", "comparison.mjs", "labels.js", "vocabulary.js", "charts.css")
JSON_TYPE = "application/json; charset=utf-8"

Response = tuple[int, str, bytes]
Handler = Callable[[Mapping[str, Any], Any], Response]


def _json(status: int, value: Mapping[str, Any]) -> Response:
    return status, JSON_TYPE, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def data_handler(kind: str) -> Handler:
    def handle(query: Mapping[str, Any], context: Any) -> Response:
        try:
            # Batch am: every Chinese display string of a chart payload is
            # read in the registry's words (the chart modules render server
            # labels as they arrive).
            return _json(200, scrub_tree(chart_payload(kind, query or {}, context)))
        except ChartRequestError as exc:
            return _json(400, {"error": "chart_request", "message": str(exc), "kind": kind})
    handle.__name__ = f"chart_{kind}"
    return handle


def asset_bytes(name: str) -> Response:
    """One chart asset by file name; only the shipped files are served."""

    if name not in ASSETS:
        return 404, "text/plain; charset=utf-8", "没有这个图表文件".encode()
    path = ASSET_DIR / name
    return 200, ASSET_TYPES[path.suffix], path.read_bytes()


def asset_handler(name: str) -> Handler:
    def handle(query: Mapping[str, Any], context: Any) -> Response:
        return asset_bytes(name)
    handle.__name__ = f"chart_asset_{name.replace('.', '_')}"
    return handle


def chart_routes() -> dict[str, Handler]:
    """``path -> handler`` for every GET route this package serves."""

    routes: dict[str, Handler] = {f"{DATA_PREFIX}{kind}": data_handler(kind) for kind in KINDS}
    routes.update({f"{ASSET_PREFIX}{name}": asset_handler(name) for name in ASSETS})
    return routes


def handle_chart_request(path: str, query: Mapping[str, Any], context: Any) -> Response | None:
    """For a shell that dispatches by prefix: the response, or ``None`` if not ours."""

    handler = chart_routes().get(path)
    if handler is None:
        if path.startswith(DATA_PREFIX):
            return _json(404, {"error": "chart_kind", "message": "没有这种图表数据"})
        if path.startswith(ASSET_PREFIX):
            return asset_bytes(path[len(ASSET_PREFIX):])
        return None
    return handler(query, context)


#: Read/write modules beside the charts that register through this hook
#: (batch ah, 2026-10-09): the evidence chain behind 「查看原文与出处」 and the
#: atomic scenario-probability group.  Each exposes ``register(router)`` and
#: is skipped when the router cannot serve it (a plain route mapping).
EXTENSIONS = ("dalton_core.cockpit_v2.evidence_api", "dalton_core.cockpit_v2.scenario_api",
              # Batch A: full-text evidence search over every acquired document.
              "dalton_core.cockpit_v2.evidence_search_api",
              # Batch C: sufficiency, open MaterialRequests, 待消化, last pack.
              "dalton_core.cockpit_v2.material_status_api",
              # Batch G: the question page's 「正在获取」 chips poll their requests;
              # batch E: each request's source-ladder attempts and arrivals.
              "dalton_core.cockpit_v2.material_requests_api")
EXTENSION_STATUS: dict[str, str] = {}


def _register_extensions(app: Any) -> None:
    import importlib

    if not (hasattr(app, "add_get") and hasattr(app, "add_post")):
        return
    for name in EXTENSIONS:
        try:
            importlib.import_module(name).register(app)
        except Exception as exc:  # noqa: BLE001 - one extension must not take the charts down
            EXTENSION_STATUS[name] = f"register_failed:{type(exc).__name__}"
        else:
            EXTENSION_STATUS[name] = "registered"


def register_chart_routes(app: Any) -> dict[str, Handler]:
    _register_extensions(app)
    routes = chart_routes()
    for path, handler in routes.items():
        if hasattr(app, "add_get"):
            app.add_get(path, handler)
        elif hasattr(app, "add_route"):
            app.add_route("GET", path, handler)
        elif hasattr(app, "route"):
            app.route(path, methods=["GET"])(handler)
        elif isinstance(app, MutableMapping):
            app[("GET", path)] = handler
        else:
            raise TypeError("register_chart_routes: unsupported route table")
    return routes


__all__ = ["ASSETS", "ASSET_PREFIX", "DATA_PREFIX", "asset_bytes", "chart_routes",
           "handle_chart_request", "register_chart_routes"]
