"""Read-only human supervision dashboard for Dalton projections.

The dashboard intentionally does not read authority databases.  A trusted
projector materializes a disposable SQLite file with :class:`ProjectionWriter`;
the HTTP process opens that file in read-only mode and exposes a fixed query
surface.  No prompt text, credentials, raw tool output, Core DB path, or writer
token belongs in this projection.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, unquote, urlparse


SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("dashboard_schema.sql")
_HTML_PATH = Path(__file__).with_name("dashboard.html")
_MAX_PAGE_SIZE = 200


class DashboardError(Exception):
    """Base dashboard error."""


class ProjectionValidationError(DashboardError):
    pass


class ProjectionNotReady(DashboardError):
    pass


_TABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "workflow_summaries": (
        "workflow_ref", "title", "objective", "display_status", "source_state",
        "source_ref", "status_reason", "total_tasks", "completed_tasks",
        "running_tasks", "failed_tasks", "total_tokens", "artifact_count",
        "recent_activity",
    ),
    "work_items": (
        "work_order_ref", "workflow_ref", "parent_work_order_ref", "sequence",
        "title", "question", "display_status", "source_state", "source_ref",
        "status_reason", "attempt_number", "model_count", "total_tokens",
        "artifact_count", "latest_result_ref", "latest_error_summary", "created_at",
        "updated_at",
    ),
    "invocation_slices": (
        "invocation_ref", "workflow_ref", "work_order_ref", "provider", "model",
        "model_family", "profile_ref", "runtime_ref", "capability", "granularity",
        "started_at", "completed_at", "duration_ms", "input_tokens", "output_tokens",
        "reasoning_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens",
        "metering_source", "measurement_status",
    ),
    "cost_slices": (
        "cost_entry_ref", "invocation_ref", "workflow_ref", "work_order_ref",
        "amount_micros", "currency", "cost_status", "price_rate_ref", "created_at",
    ),
    "artifact_index": (
        "artifact_ref", "workflow_ref", "work_order_ref", "title", "kind",
        "media_type", "size_bytes", "content_hash", "access_class", "preview_status",
        "producer_invocation_ref", "created_at",
    ),
    "capability_status": (
        "capability_id", "label", "kind", "source_type", "eligibility_state",
        "active_revision_ref", "decision_state", "updated_at",
    ),
    "model_status": (
        "profile_ref", "provider", "model", "model_family", "availability",
        "auth_state", "capabilities_json", "context_window", "cost_class",
        "last_used_at", "total_tokens",
    ),
}


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProjectionValidationError(f"{name} must be an object")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProjectionValidationError(f"{name} must be a non-empty string")
    return value


def _rows(value: Any, name: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise ProjectionValidationError(f"{name} must be an array")
    return [_object(item, f"{name} item") for item in value]


def _closed_row(table: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
    expected = _TABLE_FIELDS[table]
    if set(row) != set(expected):
        missing = sorted(set(expected) - set(row))
        extra = sorted(set(row) - set(expected))
        raise ProjectionValidationError(
            f"{table} row shape mismatch; missing={missing}, extra={extra}"
        )
    return tuple(row[field] for field in expected)


class ProjectionWriter:
    """Trusted builder for a disposable dashboard projection."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        os.chmod(self.path, 0o600)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ProjectionWriter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def replace(self, snapshot: Mapping[str, Any]) -> None:
        root = _object(snapshot, "snapshot")
        required = {"metadata", *_TABLE_FIELDS.keys()}
        if set(root) != required:
            raise ProjectionValidationError(
                f"snapshot shape mismatch; missing={sorted(required - set(root))}, "
                f"extra={sorted(set(root) - required)}"
            )
        metadata = _object(root["metadata"], "metadata")
        metadata_fields = {
            "schema_version", "as_of", "source_watermark", "build_state",
            "partial_data", "warnings",
        }
        if set(metadata) != metadata_fields:
            raise ProjectionValidationError("metadata has an invalid shape")
        if metadata["schema_version"] != SCHEMA_VERSION:
            raise ProjectionValidationError("unsupported projection schema_version")
        _string(metadata["as_of"], "metadata.as_of")
        _string(metadata["source_watermark"], "metadata.source_watermark")
        if metadata["build_state"] != "ready":
            raise ProjectionValidationError("only a ready snapshot can replace the projection")
        if not isinstance(metadata["partial_data"], bool):
            raise ProjectionValidationError("metadata.partial_data must be boolean")
        warnings = metadata["warnings"]
        if not isinstance(warnings, list) or not all(isinstance(x, str) for x in warnings):
            raise ProjectionValidationError("metadata.warnings must be an array of strings")

        normalized: dict[str, list[tuple[Any, ...]]] = {}
        for table in _TABLE_FIELDS:
            normalized[table] = [
                _closed_row(table, row) for row in _rows(root[table], table)
            ]

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for table in reversed(tuple(_TABLE_FIELDS)):
                self.connection.execute(f"DELETE FROM {table}")
            self.connection.execute("DELETE FROM projection_metadata")
            self.connection.execute(
                "INSERT INTO projection_metadata "
                "(singleton,schema_version,as_of,source_watermark,build_state,partial_data,warnings_json) "
                "VALUES (1,?,?,?,?,?,?)",
                (
                    SCHEMA_VERSION,
                    metadata["as_of"],
                    metadata["source_watermark"],
                    metadata["build_state"],
                    int(metadata["partial_data"]),
                    json.dumps(warnings, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            for table, rows in normalized.items():
                if not rows:
                    continue
                fields = _TABLE_FIELDS[table]
                placeholders = ",".join("?" for _ in fields)
                self.connection.executemany(
                    f"INSERT INTO {table} ({','.join(fields)}) VALUES ({placeholders})",
                    rows,
                )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise


class DashboardQueryService:
    """Fixed, read-only queries over a materialized projection database."""

    def __init__(self, path: str | Path) -> None:
        resolved = Path(path).resolve()
        uri = f"file:{resolved.as_posix()}?mode=ro"
        # ThreadingHTTPServer dispatches requests on worker threads.  The
        # connection is query-only and the projection file is immutable for
        # the lifetime of this service, so cross-thread reads are safe here.
        self.connection = sqlite3.connect(
            uri, uri=True, isolation_level=None, check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only = ON")

    def close(self) -> None:
        self.connection.close()

    def _metadata(self) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT schema_version,as_of,source_watermark,build_state,partial_data,warnings_json "
            "FROM projection_metadata WHERE singleton=1"
        ).fetchone()
        if row is None or row["build_state"] != "ready":
            raise ProjectionNotReady("dashboard projection is not ready")
        return {
            "schema_version": row["schema_version"],
            "as_of": row["as_of"],
            "source_watermark": row["source_watermark"],
            "build_state": row["build_state"],
            "partial_data": bool(row["partial_data"]),
            "warnings": json.loads(row["warnings_json"]),
        }

    def _envelope(self, data: Any) -> dict[str, Any]:
        meta = self._metadata()
        return {
            "as_of": meta["as_of"],
            "projection_watermark": meta["source_watermark"],
            "partial_data": meta["partial_data"],
            "warnings": meta["warnings"],
            "data": data,
        }

    def status(self) -> dict[str, Any]:
        return self._envelope(self._metadata())

    def summary(self) -> dict[str, Any]:
        workflow = dict(self.connection.execute(
            "SELECT COUNT(*) AS workflows, COALESCE(SUM(total_tasks),0) AS tasks, "
            "COALESCE(SUM(completed_tasks),0) AS completed_tasks, "
            "COALESCE(SUM(running_tasks),0) AS running_tasks, "
            "COALESCE(SUM(failed_tasks),0) AS failed_tasks, "
            "SUM(total_tokens) AS total_tokens, COALESCE(SUM(artifact_count),0) AS artifacts "
            "FROM workflow_summaries"
        ).fetchone())
        costs = [dict(row) for row in self.connection.execute(
            "SELECT currency, SUM(amount_micros) AS amount_micros, "
            "SUM(CASE WHEN cost_status='actual' THEN amount_micros ELSE 0 END) AS actual_micros, "
            "SUM(CASE WHEN cost_status='estimated' THEN amount_micros ELSE 0 END) AS estimated_micros, "
            "SUM(CASE WHEN cost_status='unpriced' THEN 1 ELSE 0 END) AS unpriced_entries "
            "FROM cost_slices GROUP BY currency ORDER BY currency"
        )]
        workflow["costs"] = costs
        return self._envelope(workflow)

    @staticmethod
    def _limit(value: Any) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError):
            limit = 50
        return max(1, min(_MAX_PAGE_SIZE, limit))

    def workflows(self, *, limit: int = 50) -> dict[str, Any]:
        rows = [dict(row) for row in self.connection.execute(
            "SELECT * FROM workflow_summaries ORDER BY recent_activity DESC, workflow_ref LIMIT ?",
            (self._limit(limit),),
        )]
        return self._envelope(rows)

    def workflow(self, workflow_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM workflow_summaries WHERE workflow_ref=?", (workflow_ref,)
        ).fetchone()
        if row is None:
            raise KeyError(workflow_ref)
        return self._envelope(dict(row))

    def workflow_tree(self, workflow_ref: str) -> dict[str, Any]:
        rows = [dict(row) for row in self.connection.execute(
            "SELECT * FROM work_items WHERE workflow_ref=? ORDER BY sequence, work_order_ref",
            (workflow_ref,),
        )]
        return self._envelope(rows)

    def work_order(self, work_order_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM work_items WHERE work_order_ref=?", (work_order_ref,)
        ).fetchone()
        if row is None:
            raise KeyError(work_order_ref)
        invocations = [dict(item) for item in self.connection.execute(
            "SELECT * FROM invocation_slices WHERE work_order_ref=? ORDER BY started_at",
            (work_order_ref,),
        )]
        result = dict(row)
        result["invocations"] = invocations
        return self._envelope(result)

    def invocations(self, *, limit: int = 100) -> dict[str, Any]:
        rows = [dict(row) for row in self.connection.execute(
            "SELECT * FROM invocation_slices ORDER BY started_at DESC LIMIT ?",
            (self._limit(limit),),
        )]
        return self._envelope(rows)

    def usage_summary(self, group_by: str) -> dict[str, Any]:
        columns = {
            "workflow": "workflow_ref",
            "work_order": "work_order_ref",
            "provider": "provider",
            "model": "model",
            "capability": "capability",
            "profile": "profile_ref",
            "day": "substr(started_at,1,10)",
        }
        column = columns.get(group_by)
        if column is None:
            raise ProjectionValidationError("unsupported usage group_by")
        rows = [dict(row) for row in self.connection.execute(
            f"SELECT {column} AS group_key, COUNT(*) AS invocations, "
            "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
            "SUM(reasoning_tokens) AS reasoning_tokens, SUM(cache_read_tokens) AS cache_read_tokens, "
            "SUM(cache_write_tokens) AS cache_write_tokens, SUM(total_tokens) AS total_tokens "
            f"FROM invocation_slices GROUP BY {column} ORDER BY total_tokens DESC"
        )]
        return self._envelope({"group_by": group_by, "rows": rows})

    def artifacts(self, *, limit: int = 100) -> dict[str, Any]:
        rows = [dict(row) for row in self.connection.execute(
            "SELECT * FROM artifact_index ORDER BY created_at DESC LIMIT ?",
            (self._limit(limit),),
        )]
        return self._envelope(rows)

    def capabilities(self, *, limit: int = 100) -> dict[str, Any]:
        rows = [dict(row) for row in self.connection.execute(
            "SELECT * FROM capability_status ORDER BY label, capability_id LIMIT ?",
            (self._limit(limit),),
        )]
        return self._envelope(rows)

    def models(self, *, limit: int = 100) -> dict[str, Any]:
        rows = []
        for row in self.connection.execute(
            "SELECT * FROM model_status ORDER BY total_tokens DESC, provider, model LIMIT ?",
            (self._limit(limit),),
        ):
            item = dict(row)
            item["capabilities"] = json.loads(item.pop("capabilities_json"))
            rows.append(item)
        return self._envelope(rows)


@dataclass(slots=True)
class DashboardApplication:
    service: DashboardQueryService

    def dispatch(self, raw_path: str) -> tuple[int, str, bytes]:
        parsed = urlparse(raw_path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/":
            return HTTPStatus.OK, "text/html; charset=utf-8", _HTML_PATH.read_bytes()
        try:
            if path == "/v1/dashboard/summary":
                value = self.service.summary()
            elif path == "/v1/projection/status":
                value = self.service.status()
            elif path == "/v1/workflows":
                value = self.service.workflows(limit=query.get("limit", [50])[0])
            elif path.startswith("/v1/workflows/"):
                tail = unquote(path.removeprefix("/v1/workflows/"))
                if tail.endswith("/tree"):
                    value = self.service.workflow_tree(tail.removesuffix("/tree"))
                else:
                    value = self.service.workflow(tail)
            elif path.startswith("/v1/work-orders/"):
                value = self.service.work_order(unquote(path.removeprefix("/v1/work-orders/")))
            elif path == "/v1/invocations":
                value = self.service.invocations(limit=query.get("limit", [100])[0])
            elif path == "/v1/usage/summary":
                value = self.service.usage_summary(query.get("group_by", ["model"])[0])
            elif path == "/v1/artifacts":
                value = self.service.artifacts(limit=query.get("limit", [100])[0])
            elif path == "/v1/capabilities":
                value = self.service.capabilities(limit=query.get("limit", [100])[0])
            elif path == "/v1/models":
                value = self.service.models(limit=query.get("limit", [100])[0])
            else:
                return HTTPStatus.NOT_FOUND, "application/json", b'{"error":"not_found"}'
        except KeyError:
            return HTTPStatus.NOT_FOUND, "application/json", b'{"error":"not_found"}'
        except ProjectionValidationError as exc:
            value = {"error": "invalid_query", "message": str(exc)}
            return HTTPStatus.BAD_REQUEST, "application/json", json.dumps(value).encode()
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return HTTPStatus.OK, "application/json; charset=utf-8", body


def _handler(application: DashboardApplication) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "DaltonDashboard/0.1"

        def do_GET(self) -> None:  # noqa: N802
            if not _is_loopback_host_header(self.headers.get("Host")):
                body = b'{"error":"invalid_host"}'
                self.send_response(HTTPStatus.MISDIRECTED_REQUEST)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            status, content_type, body = application.dispatch(self.path)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            if not _is_loopback_host_header(self.headers.get("Host")):
                body = b'{"error":"invalid_host"}'
                self.send_response(HTTPStatus.MISDIRECTED_REQUEST)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = b'{"error":"read_only"}'
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "GET")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return Handler


def _is_loopback_host_header(value: str | None) -> bool:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        return False
    try:
        hostname = urlparse(f"//{value}").hostname
    except ValueError:
        return False
    return hostname in {"127.0.0.1", "::1"}


def serve(path: str | Path, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    if host not in {"127.0.0.1", "::1"}:
        raise DashboardError("dashboard binds loopback only")
    service = DashboardQueryService(path)
    application = DashboardApplication(service)
    server = ThreadingHTTPServer((host, port), _handler(application))
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve a read-only Dalton dashboard projection")
    parser.add_argument("--projection", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args(list(argv) if argv is not None else None)
    serve(args.projection, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DashboardApplication", "DashboardError", "DashboardQueryService",
    "ProjectionNotReady", "ProjectionValidationError", "ProjectionWriter", "serve",
]
