#!/usr/bin/env python3
"""Validate or execute an owner-approved R11a stopped-window deployment plan.

The staged packet is inert.  Execution additionally requires an approved
manifest whose closed command inventory is hash-bound to regular files.  This
runner records an exclusive receipt and never publishes or edits the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


COMMIT = "424c22529d5317c4c5c9dc8ba88f37299cea3f49"
WHEEL_SHA256 = "418bf77f7b5285f9e53e041bdcf9d36463e57a7fa0bd4ce368ded2ec1e50d046"
SCHEMA = "r11a-approved-deployment-0.1"
HEX64 = re.compile(r"[0-9a-f]{64}")
ORDER = ("preflight", "controlled_stop", "fresh_backup", "preinstall_wheel",
         "configure_retention", "frozen_installer", "installed_verification")


class DeploymentError(RuntimeError):
    pass


def need(ok: Any, reason: str) -> None:
    if not ok:
        raise DeploymentError(reason)


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    wire = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    return hashlib.sha256(wire.encode()).hexdigest()


def exclusive_json(path: Path, value: Mapping[str, Any]) -> None:
    need(path.parent.is_dir() and not path.parent.is_symlink(), "receipt parent is unavailable")
    need(not path.exists() and not path.is_symlink(), "receipt already exists")
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


def load_approved(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    need(path.is_file() and not path.is_symlink(), "approved manifest is not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentError("approved manifest is invalid JSON") from exc
    need(isinstance(value, dict) and value.get("schema_version") == SCHEMA,
         "approved manifest schema differs")
    need(value.get("release_ref") == "foundation-followup-r11a"
         and value.get("source_commit") == COMMIT
         and value.get("acceptance_state") == "passed"
         and value.get("deployment_state") == "not_started",
         "approved manifest is not an unstarted accepted R11a")
    expected_hash = value.get("content_hash")
    unsigned = dict(value); unsigned.pop("content_hash", None)
    need(isinstance(expected_hash, str) and expected_hash == canonical_hash(unsigned),
         "approved manifest content hash differs")
    packet = path.resolve().parent
    wheel = packet / value.get("wheel_file", "")
    need(wheel.is_file() and not wheel.is_symlink() and wheel.parent == packet
         and sha(wheel) == value.get("wheel_sha256") == WHEEL_SHA256,
         "accepted wheel differs")
    steps = value.get("deployment_steps")
    need(isinstance(steps, list) and len(steps) == len(ORDER),
         "deployment step inventory differs")
    normalized: list[dict[str, Any]] = []
    for index, (row, expected_name) in enumerate(zip(steps, ORDER)):
        need(isinstance(row, Mapping) and set(row) == {"name", "argv", "program_sha256"},
             f"deployment step {index} is not closed")
        argv = row.get("argv")
        need(row.get("name") == expected_name and isinstance(argv, list) and argv
             and all(isinstance(part, str) and part for part in argv),
             f"deployment step {index} differs")
        program = Path(argv[0])
        need(program.is_absolute() and program.is_file() and not program.is_symlink()
             and isinstance(row.get("program_sha256"), str)
             and HEX64.fullmatch(row["program_sha256"])
             and sha(program) == row["program_sha256"],
             f"deployment step program changed: {expected_name}")
        normalized.append(dict(row))
    preinstall = normalized[3]["argv"]
    need(Path(preinstall[-1]).resolve() == wheel.resolve() and "--no-deps" in preinstall
         and ("--force-reinstall" in preinstall or "--upgrade" in preinstall),
         "preinstall step does not install the exact accepted wheel")
    retention = normalized[4]["argv"]
    need("--expected-before-sha256" in retention and "--service-config" in retention,
         "retention step lacks stopped-window CAS")
    installer = normalized[5]["argv"]
    need(Path(installer[0]).name in {"zsh", "bash"}
         and any(Path(part).name == "install.sh" for part in installer[1:]),
         "frozen installer step is not explicit")
    return value, normalized


def run(
    manifest_path: Path,
    receipt_path: Path,
    *,
    execute: bool,
    invoke: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    manifest_bytes = manifest_path.read_bytes()
    manifest, steps = load_approved(manifest_path)
    if not execute:
        return {
            "status": "approved_plan_verified_inert",
            "source_commit": COMMIT,
            "steps": [row["name"] for row in steps],
            "live_mutation": False,
            "manifest_modified": False,
        }
    need(not receipt_path.exists() and not receipt_path.is_symlink(), "receipt already exists")
    started_at = datetime.now(timezone.utc).isoformat()
    before = time.monotonic()
    results = []
    status = "installer_finished"
    exit_code = 0
    for row in steps:
        result = invoke(row["argv"], capture_output=True, text=True)
        results.append({
            "name": row["name"], "exit_code": result.returncode,
            "stdout_sha256": hashlib.sha256((result.stdout or "").encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256((result.stderr or "").encode()).hexdigest(),
        })
        if result.returncode:
            status, exit_code = "deployment_step_failed", result.returncode
            break
    need(manifest_path.read_bytes() == manifest_bytes,
         "approved manifest changed during deployment; no receipt written")
    receipt = {
        "schema_version": "r11a-deployment-execution-0.1",
        "status": status,
        "source_commit": COMMIT,
        "approved_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": time.monotonic() - before,
        "exit_code": exit_code,
        "steps": results,
        "runtime_verification_pending": exit_code == 0,
        "manifest_modified": False,
    }
    exclusive_json(receipt_path, receipt)
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-manifest", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    need(not args.execute or args.receipt is not None, "--execute requires --receipt")
    receipt = run(args.approved_manifest.resolve(),
                  args.receipt.resolve() if args.receipt else Path("/nonexistent"),
                  execute=args.execute)
    print(json.dumps(receipt))
    return int(receipt.get("exit_code", 0))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeploymentError as exc:
        raise SystemExit(f"STOP: {exc}") from exc
