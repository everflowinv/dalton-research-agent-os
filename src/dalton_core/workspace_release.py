"""Install and verify immutable, content-addressed workspace releases."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
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
DEPENDENCY_LOCK_SCHEMA = "dalton-workspace-dependencies-0.1"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# A release venv is built without ``--copies``: ``bin/python``, ``python3`` and
# ``python3.X`` are symbolic links to one base interpreter outside the venv.
# macOS TCC keys a grant for an ad-hoc signed binary (Homebrew python) on the
# executable's real path, so a copied interpreter inside every release is a
# new, ungranted client -- one "Removable Volumes" prompt per deploy.  The
# links let every release run the one interpreter the owner already granted.
# They are the only symbolic links a release may contain, and schema 0.3 pins
# their final target by real path and SHA-256 so that retargeting a link, or
# a Homebrew upgrade replacing the interpreter underneath it, fails
# validation instead of silently changing what the release runs.
INTERPRETER_LINK_SCHEMA = "0.3"
_INTERPRETER_LINK_NAMES = re.compile(r"python(?:3(?:\.[0-9]+)?)?")
# ``python -m venv`` on 3.14 also creates this novelty alias; it is removed so
# the one symbolic-link shape a release may contain stays exactly the above.
_VENV_NOVELTY_ALIASES = ("\U0001d70bthon",)


def _is_interpreter_link(venv: Path, path: Path) -> bool:
    return (path.is_symlink() and path.parent == venv / "bin"
            and _INTERPRETER_LINK_NAMES.fullmatch(path.name) is not None)


def drop_venv_novelty_aliases(venv: str | Path) -> None:
    """Remove the non-interpreter aliases ``python -m venv`` adds to ``bin/``."""

    for name in _VENV_NOVELTY_ALIASES:
        alias = Path(venv) / "bin" / name
        if alias.is_symlink():
            alias.unlink()


def _base_interpreter(venv: Path) -> dict[str, str] | None:
    """The real interpreter the venv's ``bin/python*`` links resolve to.

    ``None`` for a venv with no interpreter links (a legacy ``--copies``
    release, schema 0.1/0.2).  Every link must resolve to the same existing
    regular file outside the venv.
    """

    links = [path for path in sorted((venv / "bin").iterdir())
             if _is_interpreter_link(venv, path)] if (venv / "bin").is_dir() else []
    if not links:
        return None
    targets = {os.path.realpath(path) for path in links}
    if len(targets) != 1:
        raise WorkspaceError("release interpreter links do not share one base interpreter")
    target = Path(targets.pop())
    if (not target.is_absolute() or not target.is_file() or target.is_symlink()
            or target.is_relative_to(venv) or not os.access(target, os.X_OK)):
        raise WorkspaceError("release interpreter links do not resolve to an executable outside the release")
    return {"path": str(target), "sha256": _sha(target)}


def _check_base_interpreter(value: Any) -> Path:
    if (not isinstance(value, dict) or set(value) != {"path", "sha256"}
            or not isinstance(value["path"], str) or not isinstance(value["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None):
        raise WorkspaceError("release base interpreter declaration is invalid")
    target = Path(value["path"])
    if (not target.is_absolute() or os.path.realpath(target) != value["path"]
            or not target.is_file()):
        raise WorkspaceError(
            "release base interpreter is missing or moved (Homebrew Python upgraded?); "
            "rebuild the release against the current interpreter")
    if _sha(target) != value["sha256"]:
        raise WorkspaceError(
            "release base interpreter changed (Homebrew Python upgraded?); "
            "rebuild the release against the current interpreter")
    return target


def _inventory(venv: Path, interpreter: Path | None = None) -> list[dict[str, Any]]:
    """Every file of the venv; symbolic links only as pinned interpreter links.

    Without ``interpreter`` (schema 0.1/0.2) any symbolic link is refused.
    With it, only ``bin/python``, ``bin/python3`` and ``bin/python3.X`` may be
    links, each must resolve to ``interpreter``, and each is recorded by its
    literal link text.
    """

    rows = []
    for path in sorted(venv.rglob("*")):
        if path.is_symlink():
            if interpreter is None or not _is_interpreter_link(venv, path):
                raise WorkspaceError("release contains a symbolic link")
            if os.path.realpath(path) != str(interpreter):
                raise WorkspaceError(
                    "release interpreter link resolves to another interpreter than the pinned "
                    "one (Homebrew Python upgraded?); rebuild the release")
            rows.append({"path": path.relative_to(venv).as_posix(),
                         "symlink": os.readlink(path)})
            continue
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


def _marker_body(
    venv: Path,
    *,
    release_hash: str,
    wheel_sha256: str,
    dependency_lock_hash: str | None,
    dependency_wheels: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """The closed integrity record for ``venv``.

    Schema 0.3 when the interpreter is linked (base interpreter pinned, the
    dependency lock optional); otherwise the legacy 0.2 (with a dependency
    lock) or 0.1 (without) that ``--copies`` releases carry.
    """

    base = _base_interpreter(venv)
    files = _inventory(venv, None if base is None else Path(base["path"]))
    if not files:
        raise WorkspaceError("installed release is incomplete")
    if base is not None:
        version = INTERPRETER_LINK_SCHEMA
    else:
        version = "0.2" if dependency_lock_hash is not None else "0.1"
    body: dict[str, Any] = {
        "schema_version": version,
        "release_ref": f"release:sha256:{release_hash}",
        "wheel_sha256": wheel_sha256,
        "files": files,
    }
    if base is not None:
        body["base_interpreter"] = base
    if dependency_lock_hash is not None:
        body["dependency_lock_hash"] = dependency_lock_hash
        body["dependency_wheels"] = [dict(row) for row in dependency_wheels or []]
    return body


def validate_release(path: str | Path, expected_wheel_sha256: str) -> dict[str, Any]:
    venv = Path(path).expanduser().resolve()
    marker = venv / MARKER
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceError("release is incomplete or has no integrity manifest") from exc
    fields_01 = {"schema_version", "release_ref", "wheel_sha256", "files", "content_hash"}
    dependency_fields = {"dependency_lock_hash", "dependency_wheels"}
    fields_02 = fields_01 | dependency_fields
    fields_03 = fields_01 | {"base_interpreter"}
    version = record.get("schema_version") if isinstance(record, dict) else None
    allowed = {"0.1": [fields_01], "0.2": [fields_02],
               INTERPRETER_LINK_SCHEMA: [fields_03, fields_03 | dependency_fields]}
    if version not in allowed or set(record) not in allowed[version]:
        raise WorkspaceError("release integrity manifest has an invalid closed shape")
    body = {key: value for key, value in record.items() if key != "content_hash"}
    if record["content_hash"] != content_hash(body):
        raise WorkspaceError("release integrity manifest hash mismatch")
    expected_ref = f"release:sha256:{expected_wheel_sha256}"
    if record["release_ref"] != expected_ref:
        raise WorkspaceError("release ref does not match expected identity")
    has_lock = "dependency_lock_hash" in record
    if not has_lock and record["wheel_sha256"] != expected_wheel_sha256:
        raise WorkspaceError("release wheel identity mismatch")
    if has_lock:
        lock_hash = record["dependency_lock_hash"]
        wheels = record["dependency_wheels"]
        combined = content_hash({"wheel_sha256": record["wheel_sha256"],
                                 "dependency_lock_hash": lock_hash})
        if combined != expected_wheel_sha256:
            raise WorkspaceError("release dependency identity mismatch")
        if (not isinstance(lock_hash, str) or len(lock_hash) != 64
                or any(char not in "0123456789abcdef" for char in lock_hash)
                or not isinstance(wheels, list) or not wheels
                or any(not isinstance(row, dict) or set(row) != {"filename", "sha256"}
                       for row in wheels)
                or lock_hash != content_hash({
                    "schema_version": DEPENDENCY_LOCK_SCHEMA, "wheels": wheels})):
            raise WorkspaceError("release dependency declaration is invalid")
    interpreter = None
    if version == INTERPRETER_LINK_SCHEMA:
        interpreter = _check_base_interpreter(record["base_interpreter"])
        if not _is_interpreter_link(venv, venv / "bin" / "python"):
            raise WorkspaceError("release bin/python is not the pinned interpreter link")
    if record["files"] != _inventory(venv, interpreter):
        raise WorkspaceError("release files are incomplete or corrupt")
    return record


def _default_installer(wheel: Path, venv: Path) -> None:
    environment = dict(os.environ)
    # The installer may be launched from a source checkout or a workspace-bound
    # Cockpit.  Neither is part of the immutable release being assembled.  In
    # particular, pip considers same-version metadata on PYTHONPATH already
    # installed and can otherwise leave the new venv without the wheel payload.
    for key in ("PYTHONPATH", "PYTHONHOME", "DALTON_WORKSPACE_MANIFEST"):
        environment.pop(key, None)
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        check=True, env=environment,
    )
    drop_venv_novelty_aliases(venv)
    subprocess.run(
        [
            str(venv / "bin" / "python"), "-m", "pip", "install",
            "--disable-pip-version-check", "--no-index", "--find-links", str(wheel.parent),
            str(wheel),
        ],
        check=True, env=environment,
    )


def _dependency_lock(path: Path, wheelhouse: Path) -> tuple[dict[str, Any], list[Path]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceError("dependency lock is unavailable or invalid") from exc
    if (not isinstance(value, dict) or set(value) != {
            "schema_version", "wheels", "content_hash"}):
        raise WorkspaceError("dependency lock has an invalid closed shape")
    body = {"schema_version": value["schema_version"], "wheels": value["wheels"]}
    if (value["schema_version"] != DEPENDENCY_LOCK_SCHEMA
            or value["content_hash"] != content_hash(body)
            or not isinstance(value["wheels"], list) or not value["wheels"]):
        raise WorkspaceError("dependency lock identity or hash is invalid")
    if not wheelhouse.is_dir():
        raise WorkspaceError("dependency wheelhouse is unavailable")
    resolved: list[Path] = []
    seen: set[str] = set()
    for row in value["wheels"]:
        if (not isinstance(row, dict) or set(row) != {"filename", "sha256"}
                or not isinstance(row["filename"], str)
                or Path(row["filename"]).name != row["filename"]
                or not row["filename"].endswith(".whl") or row["filename"] in seen
                or not isinstance(row["sha256"], str) or len(row["sha256"]) != 64):
            raise WorkspaceError("dependency lock wheel entry is invalid")
        candidate = wheelhouse / row["filename"]
        if not candidate.is_file() or candidate.is_symlink() or _sha(candidate) != row["sha256"]:
            raise WorkspaceError(f"dependency wheel differs from lock: {row['filename']}")
        seen.add(row["filename"])
        resolved.append(candidate)
    if [row["filename"] for row in value["wheels"]] != sorted(seen):
        raise WorkspaceError("dependency lock wheels must be sorted by filename")
    return value, resolved


def _install_dependencies(venv: Path, wheels: list[Path]) -> None:
    environment = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "DALTON_WORKSPACE_MANIFEST"):
        environment.pop(key, None)
    subprocess.run([
        str(venv / "bin" / "python"), "-m", "pip", "install",
        "--disable-pip-version-check", "--no-index", "--no-deps",
        *(str(path) for path in wheels),
    ], check=True, env=environment)


def _relocate(staging: Path, final: Path) -> None:
    old = str(staging).encode()
    new = str(final).encode()
    for path in [staging / "pyvenv.cfg", *(staging / "bin").glob("*")]:
        if path.is_file() and not path.is_symlink():
            raw = path.read_bytes()
            first, separator, rest = raw.partition(b"\n")
            if path.parent.name == "bin" and first.startswith(b"#!") and old in first:
                raw = (
                    b"#!/bin/sh\n'''exec' \"$(dirname \"$0\")/python\" \"$0\" \"$@\" # '''\n"
                    + rest
                )
            if old in raw:
                path.write_bytes(raw.replace(old, new))
            elif raw != first + separator + rest:
                path.write_bytes(raw)


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


def write_release_marker(
    venv_path: str | Path,
    *,
    release_hash: str,
    wheel_sha256: str,
    dependency_lock_hash: str | None = None,
    dependency_wheels: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write the venv's integrity marker for a release built elsewhere.

    ``install_release`` writes this marker as its last step, and
    ``validate_release`` -- which every workspace install and repair runs --
    refuses a venv without it.  ``scripts/build_release.py`` built its venvs
    without one from 2026-09-17 on, so the first workspace repair against such
    a release died with "release is incomplete or has no integrity manifest".
    The record is the same closed shape ``install_release`` writes: schema 0.3
    when ``bin/python*`` are links to a base interpreter (pinned by real path
    and SHA-256, the dependency lock optional); for a legacy ``--copies``
    venv, 0.2 with the dependency lock when one is given, 0.1 otherwise.
    """

    venv = Path(venv_path).expanduser().resolve()
    for name, value in (("release_hash", release_hash), ("wheel_sha256", wheel_sha256)):
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise WorkspaceError(f"{name} must be lowercase SHA-256")
    if (dependency_lock_hash is None) != (dependency_wheels is None):
        raise WorkspaceError("dependency_lock_hash and dependency_wheels must be supplied together")
    executables = [venv / "bin" / name for name in ("python", "daltond", "dalton-writer")]
    if any(not path.is_file() or not os.access(path, os.X_OK) for path in executables):
        raise WorkspaceError("installed release is incomplete")
    marker = venv / MARKER
    if marker.exists():
        marker.unlink()
    body = _marker_body(
        venv, release_hash=release_hash, wheel_sha256=wheel_sha256,
        dependency_lock_hash=dependency_lock_hash, dependency_wheels=dependency_wheels)
    record = {**body, "content_hash": content_hash(body)}
    marker.write_text(canonical_json(record) + "\n", encoding="utf-8")
    os.chmod(marker, 0o600)
    validate_release(venv, release_hash)
    return record


def install_release(
    host_root: str | Path,
    wheel_path: str | Path,
    wheel_sha256: str,
    *,
    installer: Callable[[Path, Path], None] = _default_installer,
    wheelhouse: str | Path | None = None,
    dependency_lock: str | Path | None = None,
) -> dict[str, Any]:
    host = Path(host_root).expanduser().resolve()
    wheel = Path(wheel_path).expanduser().resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise WorkspaceError("wheel_path must be an existing wheel")
    if len(wheel_sha256) != 64 or any(char not in "0123456789abcdef" for char in wheel_sha256):
        raise WorkspaceError("wheel_sha256 must be lowercase SHA-256")
    if _sha(wheel) != wheel_sha256:
        raise WorkspaceError("wheel hash mismatch")
    if (wheelhouse is None) != (dependency_lock is None):
        raise WorkspaceError("wheelhouse and dependency_lock must be supplied together")
    dependency_manifest = None
    dependency_wheels: list[Path] = []
    identity = wheel_sha256
    if dependency_lock is not None:
        dependency_manifest, dependency_wheels = _dependency_lock(
            Path(dependency_lock).expanduser().resolve(),
            Path(wheelhouse).expanduser().resolve())
        identity = content_hash({"wheel_sha256": wheel_sha256,
                                 "dependency_lock_hash": dependency_manifest["content_hash"]})
    releases = host / "runtime" / "releases"
    releases.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = releases / ".install.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        target = releases / identity / "venv"
        if target.exists() or target.is_symlink():
            record = validate_release(target, identity)
            return {"status": "existing", "release_path": str(target),
                    "release_hash": identity, **record}
        staging_root = Path(tempfile.mkdtemp(prefix=f".{wheel_sha256}.", dir=releases))
        try:
            staging = staging_root / "venv"
            installer(wheel, staging)
            if dependency_wheels:
                _install_dependencies(staging, dependency_wheels)
            _relocate(staging, target)
            _verify_wheel_payload(wheel, staging)
            executables = [
                staging / "bin" / name
                for name in ("python", "daltond", "dalton-writer")
            ]
            if any(
                not path.is_file() or not os.access(path, os.X_OK)
                for path in executables
            ):
                raise WorkspaceError("installed release is incomplete")
            body = _marker_body(
                staging, release_hash=identity, wheel_sha256=wheel_sha256,
                dependency_lock_hash=(None if dependency_manifest is None
                                      else dependency_manifest["content_hash"]),
                dependency_wheels=(None if dependency_manifest is None
                                   else list(dependency_manifest["wheels"])))
            record = {**body, "content_hash": content_hash(body)}
            (staging / MARKER).write_text(canonical_json(record) + "\n", encoding="utf-8")
            os.chmod(staging / MARKER, 0o600)
            destination = target.parent
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.replace(staging_root, destination)
            validate_release(target, identity)
            return {"status": "installed", "release_path": str(target),
                    "release_hash": identity, **record}
        except BaseException:
            shutil.rmtree(staging_root, ignore_errors=True)
            raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--dependency-lock", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(install_release(
        args.host_root, args.wheel, args.wheel_sha256,
        wheelhouse=args.wheelhouse, dependency_lock=args.dependency_lock), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
