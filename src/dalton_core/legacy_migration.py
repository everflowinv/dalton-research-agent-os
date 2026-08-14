"""Lossless, shadow-only migration of the legacy Dalton workspace.

The importer never edits the source workspace and never activates legacy cron
definitions or research beliefs.  Files are copied into an owner-only,
content-addressed artifact store; the live SQLite database is captured through
the SQLite backup API; legacy schedules and beliefs are explicitly quarantined
until native Core verification and cutover exist.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

from .contracts import ResultEnvelope
from .observability import ObservabilityStore
from .store import DaltonStore, canonical_json, content_hash


MIGRATION_VERSION = "legacy-workspace-chem-import:0.1"
CONSTRAINT_FILES = (
    ".gitignore",
    "AGENTS.md",
    "HEARTBEAT.md",
    "IDENTITY.md",
    "JOURNAL.md",
    "MEMORY.md",
    "RAMP.md",
    "SOUL.md",
    "TOOLS.md",
    "USER.md",
)
ARCHIVE_DIRS = (
    "config",
    "references",
    "research-outputs",
    "models",
    "wiki",
    "memory",
    "scripts",
    "data/prices",
)
EXCLUDED_NAMES = frozenset({".DS_Store", "__pycache__"})
LEGACY_TABLES = (
    "companies",
    "theses",
    "events",
    "tasks",
    "decisions",
    "evidence",
    "market_snapshots",
    "industry_snapshots",
    "filings",
    "runs",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("migration time must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _hash_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _media_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _category(relative_path: str) -> str:
    head = relative_path.split("/", 1)[0]
    if head in CONSTRAINT_FILES or relative_path in CONSTRAINT_FILES:
        return "constraint"
    return {
        "research-outputs": "research-output",
        "models": "financial-model",
        "wiki": "knowledge-base",
        "references": "operating-reference",
        "config": "legacy-config",
        "memory": "legacy-memory",
        "scripts": "legacy-runtime",
        "data": "legacy-data",
        "generated": "migration-record",
    }.get(head, "legacy-artifact")


def _iter_source_files(source_root: Path) -> Iterable[tuple[str, Path]]:
    for name in CONSTRAINT_FILES:
        path = source_root / name
        if path.is_file() and not path.is_symlink():
            yield name, path
    for directory in ARCHIVE_DIRS:
        root = source_root / directory
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(source_root)
            if any(part in EXCLUDED_NAMES for part in relative.parts):
                continue
            if path.name.endswith((".pyc", ".db-wal", ".db-shm")):
                continue
            yield relative.as_posix(), path


def _store_blob(
    artifact_root: Path, *, digest: str, source: Path | None = None, payload: bytes | None = None
) -> Path:
    if (source is None) == (payload is None):
        raise ValueError("provide exactly one of source or payload")
    target = artifact_root / digest[:2] / digest
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists():
        existing, _ = _hash_file(target)
        if existing != digest:
            raise RuntimeError(f"artifact store hash collision at {target}")
        return target
    descriptor, temporary_name = tempfile.mkstemp(prefix=".incoming-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            if source is not None:
                with source.open("rb") as source_handle:
                    shutil.copyfileobj(source_handle, handle, length=1024 * 1024)
            else:
                handle.write(payload or b"")
            handle.flush()
            os.fsync(handle.fileno())
        copied_hash, _ = _hash_file(temporary)
        if copied_hash != digest:
            raise RuntimeError("artifact changed while being copied")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def _artifact_ref(relative_path: str, digest: str) -> str:
    path_hash = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:20]
    return f"artifact:legacy-workspace-chem:{path_hash}:{digest}"


def _artifact_record(
    *,
    artifact_root: Path,
    relative_path: str,
    digest: str,
    size: int,
    media_type: str,
    source_mtime_ns: int | None,
) -> dict[str, Any]:
    stored = artifact_root / digest[:2] / digest
    return {
        "artifact_ref": _artifact_ref(relative_path, digest),
        "source_path": relative_path,
        "category": _category(relative_path),
        "media_type": media_type,
        "sha256": digest,
        "size_bytes": size,
        "source_mtime_ns": source_mtime_ns,
        "storage_locator": f"artifact-store:sha256/{digest[:2]}/{digest}",
        "storage_path": str(stored),
        "trust_status": "legacy_unverified",
    }


def _snapshot_sqlite(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    source_uri = f"file:{quote(str(source.resolve()))}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as reader:
        with sqlite3.connect(destination) as writer:
            reader.backup(writer)
            check = writer.execute("PRAGMA integrity_check").fetchone()
            if check is None or check[0] != "ok":
                raise RuntimeError(f"SQLite snapshot integrity check failed: {check}")
    os.chmod(destination, 0o600)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _legacy_export(snapshot: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot_uri = f"file:{quote(str(snapshot.resolve()))}?mode=ro&immutable=1"
    with sqlite3.connect(snapshot_uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        names = _table_names(connection)
        tables: dict[str, list[dict[str, Any]]] = {}
        counts: dict[str, int] = {}
        for name in LEGACY_TABLES:
            if name not in names:
                continue
            rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{name}"')]
            tables[name] = rows
            counts[name] = len(rows)
        theses = [
            {
                "legacy_id": row.get("id"),
                "company_slug": row.get("company_slug"),
                "thesis_key": row.get("thesis_key"),
                "legacy_status": row.get("status"),
                "migration_status": "quarantined_pending_core_verification",
            }
            for row in tables.get("theses", [])
        ]
        tasks = [
            {
                "legacy_id": row.get("id"),
                "task_key": row.get("task_key"),
                "company_slug": row.get("company_slug"),
                "legacy_status": row.get("status"),
                "due_at": row.get("due_at"),
                "migration_status": "shadow_only",
            }
            for row in tables.get("tasks", [])
        ]
    export = {
        "schema_version": "0.1",
        "source": "workspace-chem/data/coverage/coverage.db",
        "trust_status": "legacy_unverified",
        "tables": tables,
    }
    summary = {
        "table_counts": counts,
        "theses": theses,
        "tasks": tasks,
        "belief_policy": "quarantined_pending_core_verification",
        "task_policy": "shadow_only",
    }
    return export, summary


def _load_crons(path: Path | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if path is None:
        return {"jobs": []}, []
    document = json.loads(path.read_text(encoding="utf-8"))
    jobs = document.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("cron snapshot must contain a jobs array")
    selected = [
        job
        for job in jobs
        if isinstance(job, Mapping)
        and (job.get("agentId") == "chem" or str(job.get("name", "")).startswith("dalton-"))
    ]
    selected.sort(key=lambda job: (str(job.get("name", "")), str(job.get("id", ""))))
    snapshot = {
        "schema_version": "0.1",
        "source": "openclaw-cron",
        "migration_mode": "shadow",
        "jobs": selected,
    }
    summaries = [
        {
            "legacy_job_id": job.get("id"),
            "declaration_key": job.get("declarationKey"),
            "name": job.get("name"),
            "enabled_on_legacy": bool(job.get("enabled")),
            "schedule": job.get("schedule"),
            "payload_kind": (job.get("payload") or {}).get("kind"),
            "migration_status": "shadow_registered_not_scheduled",
        }
        for job in selected
    ]
    return snapshot, summaries


def _write_json_atomic(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".incoming-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _register_artifacts(
    core_db: Path,
    *,
    migration_id: str,
    created_at: str,
    artifacts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    output_refs = [str(item["artifact_ref"]) for item in artifacts]
    invocation_ref = f"invocation:{migration_id}"
    result_ref = f"result:{migration_id}"
    work_ref = f"work-order:{migration_id}"
    environment_hash = hashlib.sha256(MIGRATION_VERSION.encode("utf-8")).hexdigest()
    invocation = {
        "schema_version": "0.1",
        "id": invocation_ref,
        "created_at": created_at,
        "work_order_ref": work_ref,
        "profile_ref": "model-profile:deterministic-legacy-import",
        "granularity": "system",
        "capability": "legacy-import",
        "provider": "dalton-core",
        "model": "deterministic-archive",
        "model_family": "deterministic-import",
        "input_refs": ["workspace:workspace-chem"],
        "output_refs": output_refs,
        "started_at": created_at,
        "completed_at": created_at,
        "usage": {
            "files": len(artifacts),
            "bytes": sum(int(item["size_bytes"]) for item in artifacts),
        },
        "side_effects": ["artifact_archive_write"],
        "runtime_ref": f"runtime:{MIGRATION_VERSION}",
        "actor_ref": "system:dalton-legacy-migration",
        "parent_ref": None,
        "environment_hash": environment_hash,
    }
    result = ResultEnvelope.from_dict(
        {
            "schema_version": "0.1",
            "id": result_ref,
            "created_at": created_at,
            "work_order_ref": work_ref,
            "invocation_ref": invocation_ref,
            "status": "succeeded",
            "outputs": {"artifact_count": len(artifacts), "mode": "shadow"},
            "actual_side_effects": ["artifact_archive_write"],
            "usage_refs": [],
            "artifact_refs": output_refs,
            "error": None,
            "metadata": {"migration_version": MIGRATION_VERSION},
        }
    ).to_dict()
    result_hash = content_hash(result)
    core_db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with DaltonStore(core_db) as store:
        store.register_invocation(invocation)
        observability = ObservabilityStore(store)
        registrations = []
        for item in artifacts:
            version_id = "artifact-version:" + hashlib.sha256(
                f"{migration_id}\0{item['artifact_ref']}".encode("utf-8")
            ).hexdigest()
            registration = observability.register_artifact_version(
                    str(item["artifact_ref"]),
                    version_id=version_id,
                    title=str(item["source_path"]),
                    kind=str(item["category"]),
                    media_type=str(item["media_type"]),
                    artifact_content_hash=str(item["sha256"]),
                    size_bytes=int(item["size_bytes"]),
                    storage_locator=str(item["storage_locator"]),
                    producer_invocation_ref=invocation_ref,
                    result_envelope_ref=result_ref,
                    result_envelope_hash=result_hash,
                    access_class="restricted",
                    preview_status="unavailable",
                    actor_ref="system:dalton-artifact-registry",
                    idempotency_key="legacy-import:"
                    + hashlib.sha256(
                        f"{migration_id}\0{item['artifact_ref']}".encode("utf-8")
                    ).hexdigest(),
                )
            if registration["status"] not in {"fresh", "duplicate"}:
                raise RuntimeError(
                    f"artifact registration failed for {item['artifact_ref']}: {registration}"
                )
            registrations.append(registration)
    return {
        "producer_invocation_ref": invocation_ref,
        "result_envelope_ref": result_ref,
        "result_envelope_hash": result_hash,
        "registered_count": len(registrations),
        "core_db": str(core_db),
    }


def migrate_legacy_workspace(
    source_root: str | Path,
    state_root: str | Path,
    *,
    cron_snapshot: str | Path | None = None,
    migration_id: str = "legacy-workspace-chem-import-v1",
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Create one auditable, shadow-only migration snapshot."""

    source = Path(source_root).resolve()
    target = Path(state_root).resolve()
    if not source.is_dir():
        raise FileNotFoundError(source)
    if source == target or source in target.parents:
        raise ValueError("state_root must be outside the legacy source workspace")
    when = created_at or _now()
    created = _timestamp(when)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifact_root = target / "legacy-artifacts" / "sha256"
    artifact_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifacts: list[dict[str, Any]] = []
    for relative, path in _iter_source_files(source):
        digest, size = _hash_file(path)
        _store_blob(artifact_root, digest=digest, source=path)
        artifacts.append(
            _artifact_record(
                artifact_root=artifact_root,
                relative_path=relative,
                digest=digest,
                size=size,
                media_type=_media_type(path),
                source_mtime_ns=path.stat().st_mtime_ns,
            )
        )

    source_db = source / "data" / "coverage" / "coverage.db"
    temporary_db = target / f".{migration_id}.coverage.sqlite"
    _snapshot_sqlite(source_db, temporary_db)
    try:
        digest, size = _hash_file(temporary_db)
        _store_blob(artifact_root, digest=digest, source=temporary_db)
        artifacts.append(
            _artifact_record(
                artifact_root=artifact_root,
                relative_path="data/coverage/coverage.db",
                digest=digest,
                size=size,
                media_type="application/vnd.sqlite3",
                source_mtime_ns=source_db.stat().st_mtime_ns,
            )
        )
        legacy_export, authority_summary = _legacy_export(temporary_db)
    finally:
        temporary_db.unlink(missing_ok=True)
        Path(f"{temporary_db}-shm").unlink(missing_ok=True)
        Path(f"{temporary_db}-wal").unlink(missing_ok=True)

    cron_document, schedules = _load_crons(
        Path(cron_snapshot).resolve() if cron_snapshot is not None else None
    )
    generated = {
        "generated/coverage-export.json": legacy_export,
        "generated/cron-shadow-snapshot.json": cron_document,
    }
    for relative, document in generated.items():
        payload = (canonical_json(document) + "\n").encode("utf-8")
        digest = _hash_bytes(payload)
        _store_blob(artifact_root, digest=digest, payload=payload)
        artifacts.append(
            _artifact_record(
                artifact_root=artifact_root,
                relative_path=relative,
                digest=digest,
                size=len(payload),
                media_type="application/json",
                source_mtime_ns=None,
            )
        )

    artifacts.sort(key=lambda item: str(item["source_path"]))
    observability = _register_artifacts(
        target / "core.sqlite",
        migration_id=migration_id,
        created_at=created,
        artifacts=artifacts,
    )
    manifest: dict[str, Any] = {
        "schema_version": "0.1",
        "migration_version": MIGRATION_VERSION,
        "migration_id": migration_id,
        "created_at": created,
        "source_root": str(source),
        "state_root": str(target),
        "mode": "shadow",
        "source_live_status": "unchanged",
        "cutover_status": "not_started",
        "legacy_cron_status": "left_enabled_until_native_cutover",
        "belief_policy": "quarantined_pending_core_verification",
        "artifacts": artifacts,
        "artifact_count": len(artifacts),
        "artifact_bytes": sum(int(item["size_bytes"]) for item in artifacts),
        "legacy_authority": authority_summary,
        "schedules": schedules,
        "schedule_count": len(schedules),
        "exclusions": [
            "temp/",
            ".git/",
            "skills/ (shared symlinks; capability catalog owns live skills)",
            "*.db-wal",
            "*.db-shm",
            "__pycache__/",
            ".DS_Store",
        ],
        "observability": observability,
    }
    manifest["manifest_hash"] = content_hash(manifest)
    manifest_path = target / "migrations" / f"{migration_id}.json"
    _write_json_atomic(manifest_path, manifest)
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--cron-snapshot")
    parser.add_argument("--migration-id", default="legacy-workspace-chem-import-v1")
    args = parser.parse_args(argv)
    result = migrate_legacy_workspace(
        args.source_root,
        args.state_root,
        cron_snapshot=args.cron_snapshot,
        migration_id=args.migration_id,
    )
    print(
        json.dumps(
            {
                "migration_id": result["migration_id"],
                "mode": result["mode"],
                "artifact_count": result["artifact_count"],
                "artifact_bytes": result["artifact_bytes"],
                "schedule_count": result["schedule_count"],
                "manifest_hash": result["manifest_hash"],
                "manifest_path": result["manifest_path"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["migrate_legacy_workspace"]
