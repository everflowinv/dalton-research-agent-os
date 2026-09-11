#!/usr/bin/env python3
"""Recheck successor bytes and sustained health; write no release pointer."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.execute_successor_stopped_window_candidate import (
    SuccessorExecuteError, SuccessorOrchestrator, canonical_hash, load_json,
    packet_preflight, sha,
)
from scripts.observe_successor_health_candidate import sample_passes, timestamp


class SuccessorFinalizeError(RuntimeError):
    pass


def need(value: Any, reason: str) -> None:
    if not value:
        raise SuccessorFinalizeError(reason)


def exclusive_json(path: Path, value: Mapping[str, Any]) -> None:
    need(path.parent.is_dir() and not path.parent.is_symlink()
         and not path.exists() and not path.is_symlink(),
         "successor finalization output must be new")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2); stream.write("\n")
        stream.flush(); os.fsync(stream.fileno())


def verify_health(
    summary_path: Path, summary: Mapping[str, Any], deployment: Mapping[str, Any],
    policy: Mapping[str, int],
) -> None:
    samples = summary.get("samples")
    count = policy["sample_count"]
    minimum = policy["minimum_duration_seconds"]
    need(summary.get("schema_version") == "successor-health-observation-0.1"
         and summary.get("status") == "passed"
         and summary.get("health_acceptance") == policy
         and summary.get("accepted") is True
         and summary.get("all_healthy") is True
         and summary.get("same_controller") is True
         and summary.get("postdeployment_controller") is True
         and summary.get("observed_long_enough") is True
         and summary.get("sample_count") == count
         and isinstance(summary.get("elapsed_seconds"), (int, float))
         and not isinstance(summary.get("elapsed_seconds"), bool)
         and summary["elapsed_seconds"] >= minimum
         and isinstance(samples, list) and len(samples) == count,
         "successor health summary did not satisfy its accepted policy")
    names = [f"{index:04d}.json" for index in range(count)]
    need([sample.get("raw_file") for sample in samples] == names,
         "successor health sample inventory differs")
    identities = {(sample.get("pid"), sample.get("started_at")) for sample in samples}
    need(len(identities) == 1, "successor controller identity changed")
    pid, controller_started = next(iter(identities))
    need(isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
         and timestamp(deployment["started_at"]) <= timestamp(controller_started)
             <= timestamp(deployment["finished_at"]),
         "health samples are not from the deployed controller")
    times = [timestamp(sample.get("at")) for sample in samples]
    need(all(left < right for left, right in zip(times, times[1:]))
         and times[0] >= timestamp(deployment["finished_at"])
         and (times[-1] - times[0]).total_seconds() >= minimum,
         "successor health sample time span differs")
    for sample in samples:
        path = summary_path.parent / sample["raw_file"]
        need(path.parent == summary_path.parent and path.is_file()
             and not path.is_symlink() and sha(path) == sample.get("raw_sha256")
             and sample_passes(sample),
             "successor raw health sample differs")
        raw = load_json(path)
        try:
            wire = json.loads(raw["stdout"])
            heartbeat = wire["heartbeat"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise SuccessorFinalizeError("raw successor health output is invalid") from exc
        need(raw.get("at") == sample.get("at")
             and raw.get("exit_code") == sample.get("exit_code") == 0
             and wire.get("ok") is sample.get("ok") is True
             and wire.get("checks") == sample.get("checks")
             and heartbeat.get("pid") == pid
             and heartbeat.get("started_at") == controller_started,
             "raw successor health contract differs")


def finalize(
    packet: Path, deployment_path: Path, summary_path: Path,
    installed_path: Path, output: Path, post_observation_log: Path,
) -> dict[str, Any]:
    manifest_path = packet / "release-manifest.candidate.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest, artifacts = packet_preflight(packet)
    deployment = load_json(deployment_path)
    summary = load_json(summary_path)
    installed = load_json(installed_path)
    commit = manifest["source"]["commit"]
    manifest_sha = sha(manifest_path)
    need(deployment.get("schema_version") == "successor-stopped-window-execution-0.1"
         and deployment.get("source_commit") == commit
         and deployment.get("status") == "installer_finished_runtime_health_pending"
         and deployment.get("exit_code") == 0
         and deployment.get("candidate_manifest_sha256") == manifest_sha
         and deployment.get("installed_verification") == installed_path.name
         and deployment.get("installed_verification_sha256") == sha(installed_path),
         "successor deployment proof differs")
    need(summary.get("source_commit") == commit
         and summary.get("candidate_manifest_sha256") == manifest_sha
         and summary.get("deployment_receipt_sha256") == sha(deployment_path),
         "successor health proof identity differs")
    verify_health(summary_path, summary, deployment, manifest["health_acceptance"])
    need(installed.get("schema_version") == "successor-installed-verification-0.1"
         and installed.get("status") == "installed_bytes_verified_runtime_pending"
         and installed.get("source_commit") == commit
         and installed.get("candidate_manifest_sha256") == manifest_sha
         and installed.get("wheel_sha256") == manifest["acceptance"]["wheel_sha256"]
         and installed.get("backup_keep_latest") == 3
         and installed.get("thesis_impact_enabled") is False
         and installed.get("writer_lane_enabled") is True
         and installed.get("model_config_count")
             == manifest["runtime"]["model_config_count_after"],
         "installed successor verification differs")

    source = Path(manifest["source"]["root"])
    need(subprocess.check_output(
             ["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
         == commit and not subprocess.check_output(
             ["git", "-C", str(source), "status", "--porcelain",
              "--untracked-files=all"], text=True),
         "frozen successor source changed after deployment")
    rollback = Path(deployment.get("fresh_rollback_snapshot", {}).get("path", ""))
    initial_path = rollback / "initial-state.json"
    need(rollback.is_dir() and not rollback.is_symlink()
         and initial_path.is_file() and not initial_path.is_symlink(),
         "fresh successor rollback authority is unavailable")
    initial = load_json(initial_path)
    need(post_observation_log.parent.is_dir() and not post_observation_log.parent.is_symlink()
         and not post_observation_log.exists() and not post_observation_log.is_symlink(),
         "post-observation log must be new")
    log_fd = os.open(post_observation_log,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(log_fd, "w", encoding="utf-8") as log:
        worker = SuccessorOrchestrator(packet, log)
        worker.rollback_root = rollback
        worker.initially_loaded = list(initial["loaded"])
        exact = worker.verify_successor(manifest, artifacts)
    need(exact["model_config_count"] == installed["model_config_count"]
         and exact["runtime_files"] == installed["runtime_files"]
         and canonical_hash(exact["authority"])
             == canonical_hash(installed["authority"]),
         "post-observation authority differs from installed verification")
    need(manifest_path.read_bytes() == manifest_bytes,
         "accepted successor manifest changed during finalization")
    result = {
        "schema_version": "successor-runtime-verification-candidate-0.1",
        "status": "passed_pending_publication",
        "source_commit": commit,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "candidate_manifest_sha256": manifest_sha,
        "deployment_receipt_sha256": sha(deployment_path),
        "health_summary_sha256": sha(summary_path),
        "installed_verification_sha256": sha(installed_path),
        "post_observation_log": post_observation_log.name,
        "post_observation_log_sha256": sha(post_observation_log),
        "runtime_verification": exact,
        "manifest_modified": False, "manifest_publication": False,
    }
    exclusive_json(output, result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--deployment-receipt", type=Path, required=True)
    parser.add_argument("--health-summary", type=Path, required=True)
    parser.add_argument("--installed-verification", type=Path, required=True)
    parser.add_argument("--post-observation-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = finalize(
        args.packet.resolve(), args.deployment_receipt.resolve(),
        args.health_summary.resolve(), args.installed_verification.resolve(),
        args.output.resolve(), args.post_observation_log.resolve(),
    )
    print(json.dumps(result)); return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SuccessorFinalizeError, SuccessorExecuteError, OSError,
            ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"STOP: {exc}") from exc
