#!/usr/bin/env python3
"""Collect a manifest-bound sustained R11a health observation."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SAMPLE_COUNT = 45
INTERVAL_SECONDS = 15
MIN_OBSERVATION_SECONDS = 660
COMMIT = "424c22529d5317c4c5c9dc8ba88f37299cea3f49"


class HealthError(RuntimeError):
    pass


def need(ok: Any, reason: str) -> None:
    if not ok:
        raise HealthError(reason)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def timestamp(value: Any) -> datetime:
    need(isinstance(value, str), "timestamp is absent")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    need(parsed.tzinfo is not None, "timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def sample_passes(sample: Mapping[str, Any]) -> bool:
    checks = sample.get("checks")
    age = checks.get("heartbeat_age_seconds") if isinstance(checks, Mapping) else None
    return bool(sample.get("exit_code") == 0 and sample.get("ok") is True
                and isinstance(checks, Mapping) and checks
                and all(value is True for key, value in checks.items()
                        if key != "heartbeat_age_seconds")
                and isinstance(age, (int, float)) and not isinstance(age, bool)
                and math.isfinite(age) and age >= 0)


def acceptance(samples: Sequence[Mapping[str, Any]], elapsed: float,
               deployment_started: datetime, deployment_finished: datetime) -> dict[str, bool]:
    identities = {(sample.get("pid"), sample.get("started_at")) for sample in samples}
    identity = next(iter(identities)) if len(identities) == 1 else (None, None)
    nonempty = isinstance(identity[0], int) and identity[0] > 0 and bool(identity[1])
    postdeployment = bool(nonempty and deployment_started <= timestamp(identity[1]) <= deployment_finished)
    healthy = len(samples) == SAMPLE_COUNT and all(sample_passes(sample) for sample in samples)
    same = len(identities) == 1 and nonempty
    long_enough = elapsed >= MIN_OBSERVATION_SECONDS
    return {"all_healthy": healthy, "same_controller": same,
            "postdeployment_controller": postdeployment, "observed_long_enough": long_enough,
            "accepted": bool(healthy and same and postdeployment and long_enough)}


def exclusive_json(path: Path, value: Mapping[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())


def observe(manifest_path: Path, deployment_receipt_path: Path, output: Path,
            health_command: Sequence[str], *,
            invoke: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
            sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    need(output.parent.is_dir() and not output.parent.is_symlink() and not output.exists(),
         "health output path is unavailable")
    manifest = json.loads(manifest_path.read_text())
    deployment = json.loads(deployment_receipt_path.read_text())
    need(manifest.get("source", {}).get("commit") == deployment.get("source_commit") == COMMIT
         and manifest.get("status") == "staged_pending_owner_acceptance"
         and deployment.get("status") == "installer_finished_runtime_health_pending" and deployment.get("exit_code") == 0,
         "deployment precondition differs")
    need(deployment.get("candidate_manifest_sha256") == sha(manifest_path),
         "deployment receipt is not bound to candidate manifest")
    start, end = timestamp(deployment["started_at"]), timestamp(deployment["finished_at"])
    output.mkdir(mode=0o700)
    samples: list[dict[str, Any]] = []
    started = clock()
    for index in range(SAMPLE_COUNT):
        if index:
            sleep(INTERVAL_SECONDS)
        at = datetime.now(timezone.utc).isoformat()
        raw: dict[str, Any] = {"at": at}
        try:
            result = invoke(list(health_command), capture_output=True, text=True, timeout=8)
            raw = {"at": at, "exit_code": result.returncode,
                   "stdout": result.stdout, "stderr": result.stderr}
            wire = json.loads(result.stdout)
            heartbeat = wire.get("heartbeat") or {}
            sample = {"at": at, "exit_code": result.returncode, "ok": wire.get("ok") is True,
                      "checks": wire.get("checks"), "pid": heartbeat.get("pid"),
                      "started_at": heartbeat.get("started_at"),
                      "last_tick_at": heartbeat.get("last_tick_at")}
        except Exception as exc:
            raw["exception"] = f"{type(exc).__name__}: {exc}"
            sample = {"at": at, "ok": False, "exception": raw["exception"]}
        raw_name = f"{index:02d}.json"; exclusive_json(output / raw_name, raw)
        sample.update(raw_file=raw_name, raw_sha256=sha(output / raw_name)); samples.append(sample)
    elapsed = clock() - started
    decision = acceptance(samples, elapsed, start, end)
    summary = {"schema_version": "r11a-health-observation-0.1", "source_commit": COMMIT,
               "candidate_manifest_sha256": sha(manifest_path),
               "deployment_receipt_sha256": sha(deployment_receipt_path),
               "sample_count": len(samples), "elapsed_seconds": elapsed,
               **decision, "samples": samples}
    exclusive_json(output / "summary.json", summary)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-manifest", type=Path, required=True)
    parser.add_argument("--deployment-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-python", type=Path, required=True)
    parser.add_argument("--service-config", type=Path, required=True)
    args = parser.parse_args(argv)
    summary = observe(args.approved_manifest.resolve(), args.deployment_receipt.resolve(),
                      args.output.resolve(), [str(args.runtime_python.absolute()), "-m", "dalton_core.health",
                                              "--config", str(args.service_config.resolve())])
    print(json.dumps({"status": "passed" if summary["accepted"] else "failed",
                      "summary": str((args.output / "summary.json").resolve())}))
    return 0 if summary["accepted"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HealthError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"STOP: {exc}") from exc
