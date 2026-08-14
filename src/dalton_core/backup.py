"""Owner-only SQLite backup and restore verification for Dalton authorities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


class BackupError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _integrity(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if result is None or result[0] != "ok":
        raise BackupError(f"SQLite integrity check failed for {path.name}")


class DatabaseBackupManager:
    def __init__(self, backup_root: str | Path, databases: Mapping[str, str | Path]):
        self.backup_root = Path(backup_root).expanduser().resolve()
        self.databases = {name: Path(path).expanduser().resolve() for name, path in databases.items()}
        if not self.databases or any(not name or "/" in name for name in self.databases):
            raise BackupError("backup database names are invalid")

    def snapshot(self, snapshot_id: str | None = None) -> dict[str, Any]:
        snapshot_id = snapshot_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        if not snapshot_id or "/" in snapshot_id or snapshot_id.startswith("."):
            raise BackupError("snapshot_id is invalid")
        self.backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.backup_root, 0o700)
        final = self.backup_root / snapshot_id
        if final.exists():
            manifest = json.loads((final / "manifest.json").read_text(encoding="utf-8"))
            manifest["status"] = "duplicate"
            return manifest
        temporary = Path(tempfile.mkdtemp(prefix=f".{snapshot_id}.", dir=self.backup_root))
        os.chmod(temporary, 0o700)
        try:
            files: list[dict[str, Any]] = []
            for name, source_path in sorted(self.databases.items()):
                if not source_path.is_file():
                    raise BackupError(f"authority database is unavailable: {name}")
                destination = temporary / f"{name}.sqlite"
                source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
                target = sqlite3.connect(destination)
                try:
                    source.backup(target)
                finally:
                    target.close()
                    source.close()
                os.chmod(destination, 0o600)
                _integrity(destination)
                files.append({
                    "name": name,
                    "file": destination.name,
                    "sha256": _sha256(destination),
                    "size_bytes": destination.stat().st_size,
                })
            manifest = {
                "schema_version": "0.1",
                "snapshot_id": snapshot_id,
                "created_at": _now(),
                "files": files,
                "status": "fresh",
            }
            manifest_path = temporary / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            os.chmod(manifest_path, 0o600)
            os.replace(temporary, final)
            return manifest
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def verify_restore(self, snapshot_id: str, restore_root: str | Path) -> dict[str, Any]:
        snapshot = self.backup_root / snapshot_id
        manifest_path = snapshot / "manifest.json"
        if not manifest_path.is_file():
            raise BackupError("backup manifest is unavailable")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        restore = Path(restore_root).expanduser().resolve()
        restore.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(restore, 0o700)
        restored: list[dict[str, Any]] = []
        for item in manifest.get("files", []):
            source = snapshot / item["file"]
            if _sha256(source) != item["sha256"]:
                raise BackupError("backup file hash mismatch")
            destination = restore / item["file"]
            if destination.exists():
                raise BackupError("restore target already exists")
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o600)
            _integrity(destination)
            restored.append({"name": item["name"], "sha256": _sha256(destination)})
        return {"snapshot_id": snapshot_id, "restored": restored, "status": "verified"}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up or verify-restore Dalton SQLite authorities")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--backup-root", type=Path, required=True)
    create.add_argument("--database", action="append", required=True, help="NAME=/absolute/path.sqlite")
    create.add_argument("--snapshot-id")
    verify = sub.add_parser("verify-restore")
    verify.add_argument("--backup-root", type=Path, required=True)
    verify.add_argument("--snapshot-id", required=True)
    verify.add_argument("--restore-root", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "create":
        databases: dict[str, str] = {}
        for item in args.database:
            if "=" not in item:
                raise BackupError("database must use NAME=/absolute/path")
            name, path = item.split("=", 1)
            databases[name] = path
        result = DatabaseBackupManager(args.backup_root, databases).snapshot(args.snapshot_id)
    else:
        result = DatabaseBackupManager(args.backup_root, {"placeholder": "/dev/null"}).verify_restore(args.snapshot_id, args.restore_root)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
