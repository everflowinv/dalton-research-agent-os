#!/usr/bin/env python3
"""Collect manifest-bound sustained health after a successor installation."""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from scripts.execute_successor_stopped_window_candidate import (
    SuccessorExecuteError, load_json, packet_preflight, sha,
)


class SuccessorHealthError(RuntimeError):
    pass


def need(value: Any, reason: str) -> None:
    if not value:
        raise SuccessorHealthError(reason)


def timestamp(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise SuccessorHealthError("health authority timestamp is invalid") from exc
    need(result.tzinfo is not None, "health authority timestamp lacks timezone")
    return result.astimezone(timezone.utc)


def sample_passes(sample: Mapping[str, Any]) -> bool:
    checks = sample.get("checks")
    age = checks.get("heartbeat_age_seconds") if isinstance(checks, Mapping) else None
    return bool(sample.get("exit_code") == 0 and sample.get("ok") is True
                and isinstance(checks, Mapping) and checks
                and all(value is True for key, value in checks.items()
                        if key != "heartbeat_age_seconds")
                and isinstance(age, (int, float)) and not isinstance(age, bool)
                and math.isfinite(age) and age >= 0)


def exclusive_json(path: Path, value: Mapping[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2); stream.write("\n")
        stream.flush(); os.fsync(stream.fileno())


def observe(
    packet: Path, deployment_receipt: Path, output: Path,
    health_command: Sequence[str], *,
    invoke: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    manifest, _artifacts = packet_preflight(packet)
    manifest_path = packet / "release-manifest.candidate.json"
    deployment = load_json(deployment_receipt)
    commit = manifest["source"]["commit"]
    need(deployment.get("schema_version") == "successor-stopped-window-execution-0.1"
         and deployment.get("source_commit") == commit
         and deployment.get("status") == "installer_finished_runtime_health_pending"
         and deployment.get("exit_code") == 0
         and deployment.get("candidate_manifest_sha256") == sha(manifest_path),
         "successor deployment proof differs")
    need(output.parent.is_dir() and not output.parent.is_symlink()
         and not output.exists() and not output.is_symlink(),
         "successor health output must be new")
    policy = manifest["health_acceptance"]
    count = policy["sample_count"]
    interval = policy["interval_seconds"]
    minimum = policy["minimum_duration_seconds"]
    deployed_start = timestamp(deployment["started_at"])
    deployed_finish = timestamp(deployment["finished_at"])
    output.mkdir(mode=0o700)
    samples: list[dict[str, Any]] = []
    started = clock()
    for index in range(count):
        if index:
            sleep(interval)
        at = datetime.now(timezone.utc).isoformat()
        raw: dict[str, Any] = {"at": at}
        try:
            result = invoke(list(health_command), capture_output=True, text=True, timeout=8)
            raw.update(exit_code=result.returncode, stdout=result.stdout,
                       stderr=result.stderr)
            wire = json.loads(result.stdout)
            heartbeat = wire.get("heartbeat") or {}
            sample = {"at": at, "exit_code": result.returncode,
                      "ok": wire.get("ok") is True, "checks": wire.get("checks"),
                      "pid": heartbeat.get("pid"),
                      "started_at": heartbeat.get("started_at"),
                      "last_tick_at": heartbeat.get("last_tick_at")}
        except Exception as exc:  # retain the exact failed observation
            raw["exception"] = f"{type(exc).__name__}: {exc}"
            sample = {"at": at, "ok": False, "exception": raw["exception"]}
        name = f"{index:04d}.json"
        exclusive_json(output / name, raw)
        sample.update(raw_file=name, raw_sha256=sha(output / name))
        samples.append(sample)
    elapsed = clock() - started
    identities = {(sample.get("pid"), sample.get("started_at")) for sample in samples}
    identity = next(iter(identities)) if len(identities) == 1 else (None, None)
    same = (len(identities) == 1 and isinstance(identity[0], int)
            and not isinstance(identity[0], bool) and identity[0] > 0
            and isinstance(identity[1], str) and bool(identity[1]))
    postdeployment = bool(same and deployed_start <= timestamp(identity[1]) <= deployed_finish)
    all_healthy = len(samples) == count and all(sample_passes(sample) for sample in samples)
    long_enough = elapsed >= minimum
    accepted = all_healthy and same and postdeployment and long_enough
    summary = {
        "schema_version": "successor-health-observation-0.1",
        "status": "passed" if accepted else "failed",
        "source_commit": commit,
        "candidate_manifest_sha256": sha(manifest_path),
        "deployment_receipt_sha256": sha(deployment_receipt),
        "health_acceptance": dict(policy), "sample_count": len(samples),
        "elapsed_seconds": elapsed, "all_healthy": all_healthy,
        "same_controller": same, "postdeployment_controller": postdeployment,
        "observed_long_enough": long_enough, "accepted": accepted,
        "samples": samples,
    }
    exclusive_json(output / "summary.json", summary)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--deployment-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-python", type=Path, required=True)
    parser.add_argument("--service-config", type=Path, required=True)
    args = parser.parse_args(argv)
    result = observe(
        args.packet.resolve(), args.deployment_receipt.resolve(), args.output.resolve(),
        [str(args.runtime_python.absolute()), "-m", "dalton_core.health",
         "--config", str(args.service_config.resolve())],
    )
    print(json.dumps({"status": result["status"],
                      "summary": str((args.output / "summary.json").resolve())}))
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SuccessorHealthError, SuccessorExecuteError, OSError,
            ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"STOP: {exc}") from exc
