#!/usr/bin/env python3
"""Verify R11a health and installed evidence, writing a candidate receipt only.

This finalizer cannot publish or mutate an accepted release manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


COMMIT = "424c22529d5317c4c5c9dc8ba88f37299cea3f49"
WHEEL_SHA256 = "418bf77f7b5285f9e53e041bdcf9d36463e57a7fa0bd4ce368ded2ec1e50d046"


class FinalizeError(RuntimeError):
    pass


def need(ok: Any, reason: str) -> None:
    if not ok:
        raise FinalizeError(reason)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_file(path: Path) -> Any:
    need(path.is_file() and not path.is_symlink(), f"artifact is not regular: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FinalizeError(f"artifact is invalid JSON: {path}") from exc


def timestamp(value: Any) -> datetime:
    need(isinstance(value, str), "timestamp is absent")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    need(parsed.tzinfo is not None, "timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def verify_health_samples(summary_path: Path, summary: Mapping[str, Any],
                          deployment: Mapping[str, Any]) -> None:
    samples = summary.get("samples")
    need(isinstance(samples, list) and len(samples) == 45
         and isinstance(summary.get("elapsed_seconds"), (int, float))
         and not isinstance(summary.get("elapsed_seconds"), bool)
         and summary["elapsed_seconds"] >= 660,
         "health observation coverage differs")
    need([sample.get("raw_file") for sample in samples] == [f"{index:02d}.json" for index in range(45)],
         "health sample inventory differs")
    sample_times = [timestamp(sample.get("at")) for sample in samples]
    need(all(left < right for left, right in zip(sample_times, sample_times[1:]))
         and sample_times[0] >= timestamp(deployment.get("finished_at"))
         and (sample_times[-1] - sample_times[0]).total_seconds() >= 660,
         "health sample time span differs")
    identities = {(sample.get("pid"), sample.get("started_at")) for sample in samples}
    need(len(identities) == 1, "health controller identity changed")
    pid, controller_started = next(iter(identities))
    need(isinstance(pid, int) and pid > 0
         and timestamp(deployment.get("started_at")) <= timestamp(controller_started) <= timestamp(deployment.get("finished_at")),
         "health controller is not the postdeployment controller")
    for sample in samples:
        raw_path = summary_path.parent / sample["raw_file"]
        need(raw_path.parent == summary_path.parent and sha(raw_path) == sample.get("raw_sha256"),
             "raw health sample changed")
        raw = json_file(raw_path)
        need(raw.get("at") == sample.get("at") and raw.get("exit_code") == sample.get("exit_code") == 0,
             "raw health process result differs")
        try:
            wire = json.loads(raw["stdout"]); heartbeat = wire["heartbeat"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise FinalizeError("raw health output is invalid") from exc
        checks = wire.get("checks")
        need(wire.get("ok") is sample.get("ok") is True and checks == sample.get("checks")
             and isinstance(checks, Mapping) and checks
             and all(value is True for key, value in checks.items() if key != "heartbeat_age_seconds")
             and heartbeat.get("pid") == pid and heartbeat.get("started_at") == controller_started,
             "raw health contract differs")


def verify_wheel(wheel: Path, installed_root: Path) -> int:
    need(sha(wheel) == WHEEL_SHA256, "accepted wheel changed")
    with zipfile.ZipFile(wheel) as archive:
        files = {name: archive.read(name) for name in archive.namelist()
                 if name.startswith("dalton_core/") and Path(name).suffix in {".py", ".sql", ".json", ".html"}}
    need(files, "accepted wheel has no runtime files")
    need(all((installed_root / name).is_file() and (installed_root / name).read_bytes() == body
             for name, body in files.items()), "installed runtime bytes differ from accepted wheel")
    installed = {path.relative_to(installed_root).as_posix()
                 for path in (installed_root / "dalton_core").rglob("*")
                 if path.is_file() and path.suffix in {".py", ".sql", ".json", ".html"}}
    need(installed == set(files), "installed runtime file inventory differs")
    return len(files)


def finalize(manifest_path: Path, deployment_path: Path, summary_path: Path,
             installed_verification_path: Path, wheel: Path, installed_root: Path,
             output: Path) -> dict[str, Any]:
    manifest_bytes = manifest_path.read_bytes()
    manifest = json_file(manifest_path); deployment = json_file(deployment_path)
    summary = json_file(summary_path); installed = json_file(installed_verification_path)
    need(manifest.get("source", {}).get("commit") == deployment.get("source_commit") == summary.get("source_commit") == COMMIT,
         "release identity differs")
    need(manifest.get("status") == "staged_pending_owner_acceptance"
         and deployment.get("status") == "installer_finished_runtime_health_pending" and deployment.get("exit_code") == 0
         and deployment.get("candidate_manifest_sha256") == sha(manifest_path)
         and deployment.get("installed_verification") == installed_verification_path.name
         and deployment.get("installed_verification_sha256") == sha(installed_verification_path),
         "deployment proof differs")
    need(summary.get("accepted") is True and summary.get("all_healthy") is True
         and summary.get("same_controller") is True and summary.get("postdeployment_controller") is True
         and summary.get("observed_long_enough") is True
         and summary.get("sample_count") == 45
         and summary.get("candidate_manifest_sha256") == sha(manifest_path)
         and summary.get("deployment_receipt_sha256") == sha(deployment_path),
         "sustained health proof differs")
    verify_health_samples(summary_path, summary, deployment)
    need(installed.get("status") == "installed_bytes_verified_runtime_pending"
         and installed.get("source_commit") == COMMIT
         and installed.get("candidate_manifest_sha256") == sha(manifest_path)
         and installed.get("wheel_sha256") == WHEEL_SHA256
         and installed.get("service_backup_keep_latest") == 3
         and isinstance(installed.get("model_config_count"), int)
         and installed["model_config_count"] > 0,
         "installed configuration proof differs")
    runtime_files = verify_wheel(wheel, installed_root)
    need(manifest_path.read_bytes() == manifest_bytes, "approved manifest changed during finalization")
    receipt = {
        "schema_version": "r11a-runtime-verification-candidate-0.1",
        "status": "passed_pending_owner_publication",
        "source_commit": COMMIT,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "candidate_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "deployment_receipt_sha256": sha(deployment_path),
        "health_summary_sha256": sha(summary_path),
        "installed_verification_sha256": sha(installed_verification_path),
        "runtime_files_verified": runtime_files,
        "model_configs_verified": installed["model_config_count"],
        "backup_keep_latest": 3,
        "manifest_modified": False,
        "manifest_publication": False,
    }
    need(not output.exists() and not output.is_symlink(), "candidate receipt already exists")
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-manifest", type=Path, required=True)
    parser.add_argument("--deployment-receipt", type=Path, required=True)
    parser.add_argument("--health-summary", type=Path, required=True)
    parser.add_argument("--installed-verification", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = finalize(args.approved_manifest.resolve(), args.deployment_receipt.resolve(),
                      args.health_summary.resolve(), args.installed_verification.resolve(),
                      args.wheel.resolve(), args.installed_root.resolve(), args.output.resolve())
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FinalizeError, OSError, ValueError, zipfile.BadZipFile) as exc:
        raise SystemExit(f"STOP: {exc}") from exc
