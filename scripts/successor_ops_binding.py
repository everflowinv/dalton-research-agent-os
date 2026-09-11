"""Bind separately frozen release operations to the exact rehearsed helpers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping


class OpsBindingError(RuntimeError):
    pass


def frozen_ops_binding(root: Path | None = None) -> dict[str, Any]:
    root = (root or Path(__file__).resolve().parent.parent).resolve(strict=True)

    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(root), *args])

    try:
        commit = git("rev-parse", "HEAD").decode().strip()
        if git("status", "--porcelain", "--untracked-files=all"):
            raise OpsBindingError("release operations checkout is not clean")
        names = sorted(name.decode() for name in
                       git("ls-files", "-z", "--", "scripts").split(b"\0") if name)
        if not names:
            raise OpsBindingError("release operations helper inventory is empty")
        rows = []
        for name in names:
            path = root / name
            if path.is_symlink() or not path.is_file():
                raise OpsBindingError("release operations helper is not a regular file")
            actual = path.read_bytes()
            if actual != git("show", f"{commit}:{name}"):
                raise OpsBindingError("release operations helper differs from frozen commit")
            rows.append({"path": name, "sha256": hashlib.sha256(actual).hexdigest()})
        if git("rev-parse", "HEAD").decode().strip() != commit or git(
            "status", "--porcelain", "--untracked-files=all"
        ):
            raise OpsBindingError("release operations checkout changed during verification")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise OpsBindingError("release operations checkout cannot be verified") from exc
    digest = hashlib.sha256((json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n").encode()).hexdigest()
    return {"schema_version": "successor-ops-binding-0.1", "source_root": str(root),
            "git_commit": commit, "helper_count": len(rows), "helpers_sha256": digest}


def verify_ops_binding(expected: Mapping[str, Any], root: Path | None = None) -> None:
    if expected != frozen_ops_binding(root):
        raise OpsBindingError("executing release operations differ from copied-state rehearsal")
