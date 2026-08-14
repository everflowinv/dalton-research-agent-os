"""Owner-only OpenClaw metadata snapshot exporter with crash-safe retry state.

The exporter accepts already filtered skill/MCP catalog records. It never
persists skill paths, instructions, credentials, MCP server configuration, or
tool output. CapabilityCatalog remains the authority for source registration,
monotonic generations, prior-head equality, and descriptor withdrawal.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .capability_catalog import canonical_hash, canonical_json
from .openclaw_metadata import OpenClawMetadataImporter, SNAPSHOT_SCHEMA_VERSION


_SCHEMA_PATH = Path(__file__).with_name("openclaw_exporter_schema.sql")
_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+._-]*:[^\s]+$")


class OpenClawExporterError(Exception):
    pass


class OpenClawExporterConflict(OpenClawExporterError):
    pass


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise OpenClawExporterError(f"{name} must be a non-empty string")
    return value


def _ref(value: Any, name: str) -> str:
    value = _text(value, name)
    if not _REF_RE.fullmatch(value) or value.startswith(
        ("file:", "path:", "http:", "https:")
    ):
        raise OpenClawExporterError(f"{name} must be an opaque namespaced ref")
    return value


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise OpenClawExporterError("exporter clock must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _records(value: Sequence[Mapping[str, Any]], name: str) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise OpenClawExporterError(f"{name} must be an array")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise OpenClawExporterError(f"{name}[{index}] must be an object")
        try:
            wire = json.loads(canonical_json(item))
        except (TypeError, ValueError) as exc:
            raise OpenClawExporterError(f"{name}[{index}] must be finite JSON") from exc
        if "metadata_hash" in wire:
            raise OpenClawExporterError(
                f"{name}[{index}] metadata_hash is exporter-owned"
            )
        wire["metadata_hash"] = canonical_hash(wire)
        result.append(wire)
    return result


class OpenClawMetadataExporter:
    """Persist exactly one pending snapshot until Catalog acknowledges it."""

    def __init__(
        self,
        path: str | Path,
        *,
        source_instance_ref: str,
        openclaw_version: str,
        exporter_version: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = str(path)
        self.source_instance_ref = _ref(source_instance_ref, "source_instance_ref")
        self.openclaw_version = _text(openclaw_version, "openclaw_version")
        self.exporter_version = _text(exporter_version, "exporter_version")
        if len(self.exporter_version) > 256:
            raise OpenClawExporterError("exporter_version is too long")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        if self.path != ":memory:":
            os.chmod(self.path, 0o600)
        now = _timestamp(self.clock())
        row = self._conn.execute(
            "SELECT source_instance_ref FROM openclaw_exporter_state WHERE singleton=1"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO openclaw_exporter_state"
                "(singleton,source_instance_ref,acknowledged_generation,"
                "acknowledged_snapshot_ref,acknowledged_snapshot_hash,pending_generation,"
                "pending_snapshot_ref,pending_snapshot_hash,pending_snapshot_json,updated_at) "
                "VALUES(1,?,0,NULL,NULL,NULL,NULL,NULL,NULL,?)",
                (self.source_instance_ref, now),
            )
        elif row["source_instance_ref"] != self.source_instance_ref:
            self._conn.close()
            raise OpenClawExporterConflict(
                "exporter state belongs to another source instance"
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "OpenClawMetadataExporter":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def prepare_snapshot(
        self,
        *,
        skills: Sequence[Mapping[str, Any]],
        mcp_tools: Sequence[Mapping[str, Any]],
        skills_complete: bool,
        mcp_servers_complete: Sequence[str],
    ) -> dict[str, Any]:
        if type(skills_complete) is not bool:
            raise OpenClawExporterError("skills_complete must be boolean")
        if isinstance(mcp_servers_complete, (str, bytes)) or not isinstance(
            mcp_servers_complete, Sequence
        ):
            raise OpenClawExporterError("mcp_servers_complete must be an array")
        servers = list(mcp_servers_complete)
        if not all(isinstance(server, str) and server for server in servers):
            raise OpenClawExporterError(
                "mcp_servers_complete must contain non-empty strings"
            )
        if len(servers) != len(set(servers)):
            raise OpenClawExporterError("mcp_servers_complete must be unique")

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            state = self._conn.execute(
                "SELECT * FROM openclaw_exporter_state WHERE singleton=1"
            ).fetchone()
            if state["pending_snapshot_json"] is not None:
                snapshot = json.loads(state["pending_snapshot_json"])
                self._conn.commit()
                return snapshot

            generation = int(state["acknowledged_generation"]) + 1
            instance_key = canonical_hash(
                {"source_instance_ref": self.source_instance_ref}
            )[:20]
            snapshot_ref = f"openclaw-snapshot:{instance_key}:{generation}"
            wire = {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "id": snapshot_ref,
                "created_at": _timestamp(self.clock()),
                "producer": {
                    "openclaw_version": self.openclaw_version,
                    "source_instance_ref": self.source_instance_ref,
                    "exporter_version": self.exporter_version,
                    "catalog_generation": generation,
                    "prior_snapshot_ref": state["acknowledged_snapshot_ref"],
                    "prior_snapshot_hash": state["acknowledged_snapshot_hash"],
                },
                "scope": {
                    "skills_complete": skills_complete,
                    "mcp_servers_complete": servers,
                },
                "skills": _records(skills, "skills"),
                "mcp_tools": _records(mcp_tools, "mcp_tools"),
            }
            wire["content_hash"] = canonical_hash(wire)
            snapshot = OpenClawMetadataImporter.validate_snapshot(wire)
            snapshot_hash = canonical_hash(snapshot)
            self._conn.execute(
                "UPDATE openclaw_exporter_state SET "
                "pending_generation=?,pending_snapshot_ref=?,pending_snapshot_hash=?,"
                "pending_snapshot_json=?,updated_at=? WHERE singleton=1",
                (
                    generation, snapshot_ref, snapshot_hash,
                    canonical_json(snapshot), _timestamp(self.clock()),
                ),
            )
            self._conn.commit()
            return snapshot
        except BaseException:
            self._conn.rollback()
            raise

    def acknowledge(self, snapshot_ref: str, snapshot_hash: str) -> dict[str, Any]:
        snapshot_ref = _ref(snapshot_ref, "snapshot_ref")
        if not isinstance(snapshot_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", snapshot_hash
        ):
            raise OpenClawExporterError("snapshot_hash must be lowercase SHA-256 hex")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            state = self._conn.execute(
                "SELECT * FROM openclaw_exporter_state WHERE singleton=1"
            ).fetchone()
            if state["pending_snapshot_ref"] is None:
                if (
                    state["acknowledged_snapshot_ref"] == snapshot_ref
                    and state["acknowledged_snapshot_hash"] == snapshot_hash
                ):
                    self._conn.commit()
                    return {"write_status": "duplicate", "snapshot_ref": snapshot_ref}
                raise OpenClawExporterConflict("no matching pending snapshot")
            if (
                state["pending_snapshot_ref"] != snapshot_ref
                or state["pending_snapshot_hash"] != snapshot_hash
            ):
                raise OpenClawExporterConflict(
                    "acknowledgement does not match the pending snapshot"
                )
            self._conn.execute(
                "UPDATE openclaw_exporter_state SET "
                "acknowledged_generation=pending_generation,"
                "acknowledged_snapshot_ref=pending_snapshot_ref,"
                "acknowledged_snapshot_hash=pending_snapshot_hash,"
                "pending_generation=NULL,pending_snapshot_ref=NULL,"
                "pending_snapshot_hash=NULL,pending_snapshot_json=NULL,updated_at=? "
                "WHERE singleton=1",
                (_timestamp(self.clock()),),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return {"write_status": "fresh", "snapshot_ref": snapshot_ref}

    def sync(
        self,
        importer: OpenClawMetadataImporter,
        *,
        skills: Sequence[Mapping[str, Any]],
        mcp_tools: Sequence[Mapping[str, Any]],
        skills_complete: bool,
        mcp_servers_complete: Sequence[str],
    ) -> dict[str, Any]:
        snapshot = self.prepare_snapshot(
            skills=skills,
            mcp_tools=mcp_tools,
            skills_complete=skills_complete,
            mcp_servers_complete=mcp_servers_complete,
        )
        result = importer.import_snapshot(snapshot)
        self.acknowledge(snapshot["id"], canonical_hash(snapshot))
        return result


__all__ = [
    "OpenClawExporterConflict", "OpenClawExporterError",
    "OpenClawMetadataExporter",
]
