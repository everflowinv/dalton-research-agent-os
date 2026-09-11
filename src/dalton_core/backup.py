"""Owner-only SQLite backup and restore verification for Dalton authorities."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote


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
    wal = Path(f"{path}-wal")
    if wal.exists() and (wal.is_symlink() or not wal.is_file() or wal.stat().st_size != 0):
        raise BackupError(f"non-empty or invalid WAL prevents immutable check for {path.name}")
    connection = sqlite3.connect(
        f"file:{quote(str(path), safe='/')}?mode=ro&immutable=1", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if result is None or result[0] != "ok":
        raise BackupError(f"SQLite integrity check failed for {path.name}")


def _snapshot_id(value: Any) -> str:
    if not isinstance(value, str) or not value or "/" in value or value.startswith("."):
        raise BackupError("snapshot_id is invalid")
    return value


def _signature(path: Path) -> tuple[int, int, int, int, int]:
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _owner_file(path: Path) -> bool:
    try:
        info = path.stat()
    except OSError:
        return False
    return (not path.is_symlink() and path.is_file() and info.st_uid == os.getuid()
            and info.st_mode & 0o077 == 0)


@contextlib.contextmanager
def _manifest_lock(snapshot: Path, *, exclusive: bool, nonblocking: bool = False):
    path = snapshot / "manifest.json"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if nonblocking:
            operation |= fcntl.LOCK_NB
        fcntl.flock(fd, operation)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _external_open_pids(snapshot: Path) -> list[int] | None:
    """Return other processes with files open below a snapshot, when available."""

    executable = shutil.which("lsof")
    if executable is None:
        return []
    try:
        completed = subprocess.run(
            [executable, "-F", "p", "+D", str(snapshot)], capture_output=True,
            text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode not in (0, 1):
        return None
    return sorted({int(line[1:]) for line in completed.stdout.splitlines()
                   if line.startswith("p") and line[1:].isdigit()
                   and int(line[1:]) != os.getpid()})


def _read_completed_snapshot(
    snapshot: Path,
) -> tuple[dict[str, Any], int, dict[str, tuple[int, int, int, int, int]]]:
    """Hash-check one immutable completed snapshot without making a copy."""

    if snapshot.is_symlink() or not snapshot.is_dir() or snapshot.name.startswith("."):
        raise BackupError("backup snapshot is not a completed directory")
    manifest_path = snapshot / "manifest.json"
    if not _owner_file(manifest_path):
        raise BackupError("backup manifest is unavailable")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError("backup manifest is invalid") from exc
    fields = {"schema_version", "snapshot_id", "created_at", "files", "status"}
    if (not isinstance(manifest, dict) or set(manifest) != fields
            or manifest.get("schema_version") != "0.1"
            or manifest.get("snapshot_id") != snapshot.name
            or manifest.get("status") != "fresh"
            or not isinstance(manifest.get("created_at"), str)
            or not isinstance(manifest.get("files"), list) or not manifest["files"]):
        raise BackupError("backup manifest has an invalid completed shape")
    try:
        created = datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupError("backup manifest created_at is invalid") from exc
    if created.tzinfo is None:
        raise BackupError("backup manifest created_at must include a timezone")
    expected = {"manifest.json"}
    signatures = {"manifest.json": _signature(manifest_path)}
    total = manifest_path.stat().st_size
    seen_names: set[str] = set()
    for item in manifest["files"]:
        if (not isinstance(item, dict)
                or set(item) != {"name", "file", "sha256", "size_bytes"}
                or not isinstance(item["name"], str) or not item["name"]
                or item["name"] in seen_names
                or not isinstance(item["file"], str) or Path(item["file"]).name != item["file"]
                or not isinstance(item["sha256"], str) or len(item["sha256"]) != 64
                or isinstance(item["size_bytes"], bool)
                or not isinstance(item["size_bytes"], int) or item["size_bytes"] < 0):
            raise BackupError("backup manifest file entry is invalid")
        seen_names.add(item["name"])
        expected.add(item["file"])
        path = snapshot / item["file"]
        if not _owner_file(path) or path.stat().st_size != item["size_bytes"]:
            raise BackupError("backup file is unavailable or has the wrong size")
        before = _signature(path)
        if _sha256(path) != item["sha256"]:
            raise BackupError("backup file hash mismatch")
        after = _signature(path)
        if before != after:
            raise BackupError("backup file changed during hash verification")
        signatures[item["file"]] = after
        total += item["size_bytes"]
        wal = Path(f"{path}-wal")
        shm = Path(f"{path}-shm")
        if wal.exists() != shm.exists():
            raise BackupError("backup SQLite sidecars are incomplete")
        if wal.exists():
            if not _owner_file(wal) or wal.stat().st_size != 0:
                raise BackupError("backup WAL must be an owner-only empty file")
            shm_size = shm.stat().st_size
            if (not _owner_file(shm) or shm_size < 32768 or shm_size > 64 * 1024 * 1024
                    or shm_size % 32768 != 0):
                raise BackupError("backup SHM has an invalid SQLite sidecar shape")
            for sidecar in (wal, shm):
                before_sidecar = _signature(sidecar)
                _sha256(sidecar)
                after_sidecar = _signature(sidecar)
                if before_sidecar != after_sidecar:
                    raise BackupError("backup SQLite sidecar changed during verification")
                signatures[sidecar.name] = after_sidecar
                expected.add(sidecar.name)
                total += sidecar.stat().st_size
    if {path.name for path in snapshot.iterdir()} != expected:
        raise BackupError("backup snapshot contains unmanifested files")
    return manifest, total, signatures


def _snapshot_matches_signatures(
    snapshot: Path, signatures: Mapping[str, tuple[int, int, int, int, int]],
) -> bool:
    try:
        return ({path.name for path in snapshot.iterdir()} == set(signatures)
                and all(not (snapshot / name).is_symlink()
                        and _signature(snapshot / name) == signature
                        for name, signature in signatures.items()))
    except OSError:
        return False


class DatabaseBackupManager:
    def __init__(self, backup_root: str | Path, databases: Mapping[str, str | Path]):
        self.backup_root = Path(backup_root).expanduser().resolve()
        self.databases = {name: Path(path).expanduser().resolve() for name, path in databases.items()}
        if not self.databases or any(not name or "/" in name for name in self.databases):
            raise BackupError("backup database names are invalid")

    def snapshot(self, snapshot_id: str | None = None) -> dict[str, Any]:
        snapshot_id = snapshot_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        snapshot_id = _snapshot_id(snapshot_id)
        self.backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.backup_root, 0o700)
        final = self.backup_root / snapshot_id
        if final.exists():
            with _manifest_lock(final, exclusive=False):
                manifest, _, _ = _read_completed_snapshot(final)
                for item in manifest["files"]:
                    _integrity(final / item["file"])
            manifest = dict(manifest)
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

    def prune_verified(self, *, keep_latest: int) -> dict[str, Any]:
        """Delete older completed snapshots only after proving the retained set."""

        if isinstance(keep_latest, bool) or not isinstance(keep_latest, int) or keep_latest < 1:
            raise BackupError("keep_latest must be a positive integer")
        if not self.backup_root.is_dir():
            raise BackupError("backup root is unavailable")
        verified: list[
            tuple[datetime, str, dict[str, Any], int,
                  dict[str, tuple[int, int, int, int, int]]]
        ] = []
        skipped: list[dict[str, str]] = []
        for path in sorted(self.backup_root.iterdir()):
            if path.name.startswith("."):
                skipped.append({"snapshot_id": path.name, "reason": "temporary_or_control_path"})
                continue
            try:
                with _manifest_lock(path, exclusive=False, nonblocking=True):
                    manifest, size, signatures = _read_completed_snapshot(path)
                created = datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00"))
            except BlockingIOError:
                skipped.append({"snapshot_id": path.name, "reason": "snapshot is in use"})
                continue
            except (BackupError, OSError) as exc:
                skipped.append({"snapshot_id": path.name, "reason": str(exc)})
                continue
            verified.append((created, path.name, manifest, size, signatures))
        verified.sort(key=lambda item: (item[0], item[1]), reverse=True)
        retained = verified[:keep_latest]
        if len(retained) < keep_latest:
            return {
                "status": "held", "keep_latest": keep_latest,
                "verified_snapshot_count": len(verified),
                "retained_snapshot_ids": [item[1] for item in retained],
                "deleted_snapshot_ids": [], "deleted_count": 0, "deleted_bytes": 0,
                "skipped": skipped,
                "reason": "fewer than keep_latest completed snapshots passed hash verification",
            }
        retained_checks: list[dict[str, Any]] = []
        deleted: list[str] = []
        deleted_bytes = 0
        try:
            with contextlib.ExitStack() as retained_locks:
                for _, snapshot_id, _, _, _ in retained:
                    retained_locks.enter_context(_manifest_lock(
                        self.backup_root / snapshot_id, exclusive=False, nonblocking=True))
                for _, snapshot_id, manifest, size, _ in retained:
                    for item in manifest["files"]:
                        _integrity(self.backup_root / snapshot_id / item["file"])
                    retained_checks.append({
                        "snapshot_id": snapshot_id, "file_count": len(manifest["files"]),
                        "size_bytes": size, "hashes_verified": True,
                        "sqlite_integrity": "ok",
                        "files": [
                            {key: item[key]
                             for key in ("name", "file", "sha256", "size_bytes")}
                            for item in manifest["files"]
                        ],
                    })
                if any(not _snapshot_matches_signatures(
                        self.backup_root / item[1], item[4]) for item in retained):
                    raise BackupError("retained snapshot changed before deletion")
                for _, snapshot_id, _, size, signatures in reversed(verified[keep_latest:]):
                    path = self.backup_root / snapshot_id
                    try:
                        with _manifest_lock(path, exclusive=True, nonblocking=True):
                            open_pids = _external_open_pids(path)
                            if open_pids is None:
                                skipped.append({"snapshot_id": snapshot_id,
                                                "reason": "open-handle check unavailable"})
                                continue
                            if open_pids:
                                skipped.append({
                                    "snapshot_id": snapshot_id,
                                    "reason": "snapshot is in use by pid(s) "
                                              + ",".join(map(str, open_pids)),
                                })
                                continue
                            if (path.is_symlink() or not path.is_dir()
                                    or not _snapshot_matches_signatures(path, signatures)):
                                skipped.append({"snapshot_id": snapshot_id,
                                                "reason": "snapshot changed before deletion"})
                                continue
                            if any(not _snapshot_matches_signatures(
                                    self.backup_root / item[1], item[4]) for item in retained):
                                raise BackupError("retained snapshot changed before deletion")
                            shutil.rmtree(path)
                            deleted.append(snapshot_id)
                            deleted_bytes += size
                    except BlockingIOError:
                        skipped.append({"snapshot_id": snapshot_id,
                                        "reason": "snapshot is in use"})
        except BlockingIOError as exc:
            raise BackupError("retained snapshot is in use") from exc
        return {
            "status": "pruned", "keep_latest": keep_latest,
            "verified_snapshot_count": len(verified),
            "retained_snapshot_ids": [item[1] for item in retained],
            "retained_checks": retained_checks,
            "deleted_snapshot_ids": deleted, "deleted_count": len(deleted),
            "deleted_bytes": deleted_bytes, "skipped": skipped,
        }

    def verify_restore(self, snapshot_id: str, restore_root: str | Path) -> dict[str, Any]:
        snapshot = self.backup_root / _snapshot_id(snapshot_id)
        restore = Path(restore_root).expanduser().resolve()
        restore.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(restore, 0o700)
        restored: list[dict[str, Any]] = []
        try:
            with _manifest_lock(snapshot, exclusive=False):
                manifest, _, _ = _read_completed_snapshot(snapshot)
                for item in manifest["files"]:
                    source = snapshot / item["file"]
                    destination = restore / item["file"]
                    if destination.exists():
                        raise BackupError("restore target already exists")
                    shutil.copyfile(source, destination)
                    os.chmod(destination, 0o600)
                    _integrity(destination)
                    restored.append({"name": item["name"], "sha256": _sha256(destination)})
        except FileNotFoundError as exc:
            raise BackupError("backup manifest is unavailable") from exc
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
    prune = sub.add_parser("prune")
    prune.add_argument("--backup-root", type=Path, required=True)
    prune.add_argument("--keep-latest", type=int, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "create":
        databases: dict[str, str] = {}
        for item in args.database:
            if "=" not in item:
                raise BackupError("database must use NAME=/absolute/path")
            name, path = item.split("=", 1)
            databases[name] = path
        result = DatabaseBackupManager(args.backup_root, databases).snapshot(args.snapshot_id)
    elif args.command == "verify-restore":
        result = DatabaseBackupManager(args.backup_root, {"placeholder": "/dev/null"}).verify_restore(args.snapshot_id, args.restore_root)
    else:
        result = DatabaseBackupManager(
            args.backup_root, {"placeholder": "/dev/null"}
        ).prune_verified(keep_latest=args.keep_latest)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
