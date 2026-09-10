"""Install and verify immutable, content-addressed workspace releases."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable, Sequence

from .store import canonical_json, content_hash
from .workspace import WorkspaceError

MARKER = "dalton-release.json"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inventory(venv: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(venv.rglob("*")):
        if path.is_symlink():
            raise WorkspaceError("release contains a symbolic link")
        if path.is_file() and path.name != MARKER:
            rows.append(
                {
                    "path": path.relative_to(venv).as_posix(),
                    "size": path.stat().st_size,
                    "mode": path.stat().st_mode & 0o777,
                    "sha256": _sha(path),
                }
            )
    return rows


def validate_release(path: str | Path, expected_wheel_sha256: str) -> dict[str, Any]:
    venv = Path(path).expanduser().resolve()
    marker = venv / MARKER
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceError("release is incomplete or has no integrity manifest") from exc
    if not isinstance(record, dict) or set(record) != {
        "schema_version", "release_ref", "wheel_sha256", "files", "content_hash"
    }:
        raise WorkspaceError("release integrity manifest has an invalid closed shape")
    body = {key: value for key, value in record.items() if key != "content_hash"}
    if record["content_hash"] != content_hash(body):
        raise WorkspaceError("release integrity manifest hash mismatch")
    if record["schema_version"] != "0.1" or record["wheel_sha256"] != expected_wheel_sha256:
        raise WorkspaceError("release wheel identity mismatch")
    if record["release_ref"] != f"release:sha256:{expected_wheel_sha256}":
        raise WorkspaceError("release ref does not match wheel hash")
    if record["files"] != _inventory(venv):
        raise WorkspaceError("release files are incomplete or corrupt")
    return record


def _default_installer(wheel: Path, venv: Path) -> None:
    subprocess.run([sys.executable, "-m", "venv", "--copies", str(venv)], check=True)
    subprocess.run(
        [
            str(venv / "bin" / "python"), "-m", "pip", "install",
            "--disable-pip-version-check", "--no-index", "--find-links", str(wheel.parent),
            str(wheel),
        ],
        check=True,
    )


def _relocate(staging: Path, final: Path) -> None:
    old = str(staging).encode()
    new = str(final).encode()
    for path in [staging / "pyvenv.cfg", *(staging / "bin").glob("*")]:
        if path.is_file() and not path.is_symlink():
            raw = path.read_bytes()
            if old in raw:
                path.write_bytes(raw.replace(old, new))


def _verify_wheel_payload(wheel: Path, venv: Path) -> None:
    sites = list(venv.glob("lib/python*/site-packages"))
    if len(sites) != 1:
        raise WorkspaceError("installed release has no unique site-packages")
    with zipfile.ZipFile(wheel) as archive:
        members = [name for name in archive.namelist() if name.startswith("dalton_core/") and not name.endswith("/")]
        if not members:
            raise WorkspaceError("wheel contains no dalton_core package")
        for name in members:
            installed = sites[0] / name
            if not installed.is_file() or installed.read_bytes() != archive.read(name):
                raise WorkspaceError(f"installed Dalton payload differs from wheel: {name}")


def install_release(
    host_root: str | Path,
    wheel_path: str | Path,
    wheel_sha256: str,
    *,
    installer: Callable[[Path, Path], None] = _default_installer,
) -> dict[str, Any]:
    host = Path(host_root).expanduser().resolve()
    wheel = Path(wheel_path).expanduser().resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise WorkspaceError("wheel_path must be an existing wheel")
    if len(wheel_sha256) != 64 or any(char not in "0123456789abcdef" for char in wheel_sha256):
        raise WorkspaceError("wheel_sha256 must be lowercase SHA-256")
    if _sha(wheel) != wheel_sha256:
        raise WorkspaceError("wheel hash mismatch")
    releases = host / "runtime" / "releases"
    releases.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = releases / ".install.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        target = releases / wheel_sha256 / "venv"
        if target.exists() or target.is_symlink():
            record = validate_release(target, wheel_sha256)
            return {"status": "existing", "release_path": str(target), **record}
        staging_root = Path(tempfile.mkdtemp(prefix=f".{wheel_sha256}.", dir=releases))
        try:
            staging = staging_root / "venv"
            installer(wheel, staging)
            _relocate(staging, target)
            _verify_wheel_payload(wheel, staging)
            files = _inventory(staging)
            executables = [
                staging / "bin" / name
                for name in ("python", "daltond", "dalton-writer")
            ]
            if not files or any(
                not path.is_file() or not os.access(path, os.X_OK)
                for path in executables
            ):
                raise WorkspaceError("installed release is incomplete")
            body = {
                "schema_version": "0.1",
                "release_ref": f"release:sha256:{wheel_sha256}",
                "wheel_sha256": wheel_sha256,
                "files": files,
            }
            record = {**body, "content_hash": content_hash(body)}
            (staging / MARKER).write_text(canonical_json(record) + "\n", encoding="utf-8")
            os.chmod(staging / MARKER, 0o600)
            destination = target.parent
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.replace(staging_root, destination)
            validate_release(target, wheel_sha256)
            return {"status": "installed", "release_path": str(target), **record}
        except BaseException:
            shutil.rmtree(staging_root, ignore_errors=True)
            raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--wheel-sha256", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(install_release(args.host_root, args.wheel, args.wheel_sha256), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
