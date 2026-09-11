#!/usr/bin/env python3
"""Install the reviewed R11 backup retention delta while services are stopped.

The deployment wrapper owns the stopped window and must run this helper after
installing the R11 runtime and before starting the controller. This helper
does not stop or start services and does not edit a release manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


REVIEWED_KEEP_LATEST = 3
HEX64 = re.compile(r"[0-9a-f]{64}")


class RetentionConfigError(RuntimeError):
    pass


def _need(condition: bool, reason: str) -> None:
    if not condition:
        raise RetentionConfigError(reason)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_sha256(value: Any) -> str:
    wire = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(wire.encode("utf-8"))


def configure(raw: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """Return the exact reviewed delta and a hash of every preserved value."""

    _need(isinstance(raw, Mapping), "service config must be an object")
    backup = raw.get("backup")
    _need(isinstance(backup, Mapping), "service config has no backup block")
    _need(backup.get("enabled") is True, "backup retention requires an enabled backup")
    _need(
        isinstance(backup.get("root"), str)
        and Path(backup["root"]).is_absolute()
        and isinstance(backup.get("interval_seconds"), (int, float))
        and not isinstance(backup.get("interval_seconds"), bool)
        and backup["interval_seconds"] > 0,
        "backup block is not runnable",
    )
    preserved = json.loads(json.dumps(raw))
    preserved_backup = dict(preserved["backup"])
    preserved_backup.pop("keep_latest", None)
    preserved["backup"] = preserved_backup
    updated = json.loads(json.dumps(raw))
    updated["backup"]["keep_latest"] = REVIEWED_KEEP_LATEST
    return updated, canonical_sha256(preserved)


def _validate_with_installed_runtime(raw: Mapping[str, Any]) -> None:
    """Use the newly installed R11 parser as the final config authority."""

    try:
        from dalton_core.service import ServiceConfig
        ServiceConfig.from_mapping(raw)
    except Exception as exc:
        raise RetentionConfigError(
            "newly installed runtime rejected backup retention config"
        ) from exc


def install(
    service_config: Path,
    expected_before_sha256: str,
    receipt_path: Path,
    *,
    config_validator: Callable[[Mapping[str, Any]], None] = _validate_with_installed_runtime,
) -> dict[str, Any]:
    _need(HEX64.fullmatch(expected_before_sha256) is not None, "expected SHA-256 is invalid")
    _need(not service_config.is_symlink() and service_config.is_file(), "service config is not a regular file")
    _need(
        receipt_path.parent.is_dir() and not receipt_path.parent.is_symlink(),
        "receipt directory is unavailable",
    )
    _need(not receipt_path.exists() and not receipt_path.is_symlink(), "receipt path already exists")
    before_mode = stat.S_IMODE(service_config.stat().st_mode)
    before = service_config.read_bytes()
    _need(sha256_bytes(before) == expected_before_sha256, "service config differs from reviewed precondition")
    try:
        raw = json.loads(before.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RetentionConfigError("service config is invalid JSON") from exc
    updated, preserved_sha256 = configure(raw)
    config_validator(updated)
    after = (json.dumps(updated, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    after_sha256 = sha256_bytes(after)
    changed = before != after
    receipt = {
        "schema_version": "r11-backup-retention-stopped-window-0.1",
        "status": "configured_controller_start_pending",
        "service_config": str(service_config.resolve()),
        "before_sha256": expected_before_sha256,
        "after_sha256": after_sha256,
        "preserved_without_backup_keep_latest_sha256": preserved_sha256,
        "delta": {"backup.keep_latest": REVIEWED_KEEP_LATEST},
        "changed": changed,
        "execution_phase_required": "after_runtime_install_before_controller_start",
        "service_process_checks": "owned_by_deployment_wrapper",
        "service_lifecycle_mutations": 0,
        "release_manifest_modified": False,
    }
    unsigned = dict(receipt)
    receipt["content_hash"] = canonical_sha256(unsigned)
    receipt_bytes = (json.dumps(receipt, indent=2) + "\n").encode("utf-8")

    fd, temporary_name = tempfile.mkstemp(prefix=".backup-retention-", dir=service_config.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, before_mode)
        _need(service_config.read_bytes() == before, "service config changed during stopped-window preparation")
        os.replace(temporary, service_config)
        directory_fd = os.open(service_config.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        _need(service_config.read_bytes() == after, "installed service config bytes differ")
        receipt_fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(receipt_fd, "wb") as stream:
            stream.write(receipt_bytes)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        temporary.unlink(missing_ok=True)
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-config", type=Path, required=True)
    parser.add_argument("--expected-before-sha256", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    receipt = install(
        args.service_config.expanduser().resolve(),
        args.expected_before_sha256,
        args.receipt.expanduser().resolve(),
    )
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RetentionConfigError as exc:
        raise SystemExit(f"STOP: {exc}") from exc
