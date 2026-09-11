#!/usr/bin/env python3
"""CAS-publish one health-verified successor without signing research."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts import execute_successor_stopped_window_candidate as execute
from scripts import finalize_successor_health_candidate as finalizer
from scripts.prepare_successor_config_transition import (
    DOCUMENT_CONFIG, LANE_CONFIG, expected_transition_state,
)


class SuccessorPublicationError(RuntimeError):
    pass


def need(value: Any, reason: str) -> None:
    if not value:
        raise SuccessorPublicationError(reason)


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha(path: Path) -> str:
    return execute.sha(path)


def canonical_hash(value: Any) -> str:
    return execute.canonical_hash(value)


def load_json_bytes(value: bytes, label: str) -> dict[str, Any]:
    try:
        wire = json.loads(value.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SuccessorPublicationError(f"{label} is invalid JSON") from exc
    need(isinstance(wire, dict), f"{label} is not an object")
    return wire


def exact_file(path: Path, expected: str, label: str) -> bytes:
    need(path.is_file() and not path.is_symlink(), f"{label} is unavailable")
    value = path.read_bytes()
    need(sha_bytes(value) == expected, f"{label} changed")
    return value


def regular_bytes(path: Path, label: str) -> bytes:
    need(path.is_file() and not path.is_symlink(), f"{label} is unavailable")
    return path.read_bytes()


def packet_file(packet: Path, path: Path, expected: str, label: str) -> bytes:
    resolved = path.resolve()
    need(resolved.is_relative_to(packet) and path.is_file() and not path.is_symlink(),
         f"{label} is outside the release packet")
    return exact_file(path, expected, label)


def json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def exclusive_or_exact(path: Path, value: bytes) -> None:
    if path.exists() or path.is_symlink():
        need(path.is_file() and not path.is_symlink() and path.read_bytes() == value,
             f"existing publication artifact differs: {path.name}")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value); stream.flush(); os.fsync(stream.fileno())


def atomic_draft(directory: Path, name: str, value: bytes) -> Path:
    fd, raw = tempfile.mkstemp(prefix=f".{name}.", dir=directory)
    path = Path(raw)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value); stream.flush(); os.fsync(stream.fileno())
        os.chmod(path, 0o600)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(
    *, packet: Path, owner: Path, expected_manifest_sha256: str,
    deployment_path: Path, expected_deployment_sha256: str,
    health_path: Path, expected_health_sha256: str,
    installed_path: Path, expected_installed_sha256: str,
    finalization_path: Path, expected_finalization_sha256: str,
    expected_current_release_sha256: str,
    expected_current_runtime_config_sha256: str,
    receipt_path: Path,
    fault_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Publish exact successor pointers; all model/source work is already complete."""
    packet = packet.resolve(); owner = owner.resolve()
    need(packet.is_dir() and not packet.is_symlink(), "release packet is unavailable")
    need(owner.is_dir() and not owner.is_symlink(), "owner packet is unavailable")
    receipt_path = receipt_path.resolve()
    need(receipt_path.parent == packet and not receipt_path.is_symlink(),
         "publication receipt must be a packet-local file")
    manifest_path = packet / "release-manifest.candidate.json"
    exact_file(manifest_path, expected_manifest_sha256, "accepted successor manifest")
    manifest, artifacts = execute.packet_preflight(packet)
    deployment_bytes = packet_file(packet, deployment_path, expected_deployment_sha256,
                                   "deployment receipt")
    health_bytes = packet_file(packet, health_path, expected_health_sha256,
                               "health summary")
    installed_bytes = packet_file(packet, installed_path, expected_installed_sha256,
                                  "installed verification")
    finalization_bytes = packet_file(packet, finalization_path, expected_finalization_sha256,
                                     "successor finalization")
    deployment = load_json_bytes(deployment_bytes, "deployment receipt")
    accepted = load_json_bytes(finalization_bytes, "successor finalization")
    commit = manifest["source"]["commit"]
    need(
        accepted.get("schema_version") == "successor-runtime-verification-candidate-0.1"
        and accepted.get("status") == "passed_pending_publication"
        and accepted.get("source_commit") == commit
        and accepted.get("candidate_manifest_sha256") == expected_manifest_sha256
        and accepted.get("deployment_receipt_sha256") == expected_deployment_sha256
        and accepted.get("health_summary_sha256") == expected_health_sha256
        and accepted.get("installed_verification_sha256") == expected_installed_sha256
        and accepted.get("manifest_publication") is False,
        "successor finalization does not bind the publication inputs",
    )
    rollback = deployment.get("fresh_rollback_snapshot", {})
    rollback_path = Path(rollback.get("path", "")).resolve()
    need(
        deployment.get("status") == "installer_finished_runtime_health_pending"
        and deployment.get("exit_code") == 0
        and deployment.get("source_commit") == commit
        and deployment.get("candidate_manifest_sha256") == expected_manifest_sha256
        and rollback_path.is_relative_to(packet)
        and rollback_path.is_dir() and not rollback_path.is_symlink()
        and (rollback_path / "initial-state.json").is_file()
        and not (rollback_path / "initial-state.json").is_symlink()
        and isinstance(rollback.get("database_snapshot_id"), str)
        and bool(rollback["database_snapshot_id"]),
        "successor deployment or rollback authority differs",
    )

    current_release = owner / "current-release.json"
    current_runtime = owner / "current-runtime-config.json"
    release_snapshot = packet / "previous-current-release.json"
    runtime_snapshot = packet / "previous-current-runtime-config.json"
    # Each predecessor can be recovered independently if the process died
    # while creating the pair, before either live pointer was changed.
    predecessors = []
    for snapshot, current, expected, label in (
        (release_snapshot, current_release, expected_current_release_sha256, "current release"),
        (runtime_snapshot, current_runtime, expected_current_runtime_config_sha256, "current runtime config"),
    ):
        if snapshot.exists() or snapshot.is_symlink():
            value = exact_file(snapshot, expected, "previous " + label + " snapshot")
            regular_bytes(current, label + " pointer")
        else:
            value = exact_file(current, expected, label + " pointer")
            exclusive_or_exact(snapshot, value)
        predecessors.append(value)
    release_before, runtime_before = predecessors
    previous_release = load_json_bytes(release_before, "current release pointer")
    previous_runtime = load_json_bytes(runtime_before, "current runtime config pointer")
    need(previous_release.get("status") == "deployed_verified"
         and previous_runtime.get("base_release_commit")
             == previous_release.get("source_commit")
         and (
             (previous_runtime.get("schema_version") == "dalton-runtime-config-pointer-0.1"
              and previous_runtime.get("base_release_pointer_sha256") == expected_current_release_sha256)
             or (previous_runtime.get("schema_version") == "dalton-runtime-config-pointer-0.2"
                 and previous_release.get("schema_version") == "dalton-current-release-0.2"
                 and previous_release.get("current_runtime_config_sha256")
                     == expected_current_runtime_config_sha256)
         ),
         "current release/runtime predecessor binding differs")

    with tempfile.TemporaryDirectory(prefix=".successor-publish-check-", dir=packet) as raw:
        check = Path(raw)
        verified = finalizer.finalize(
            packet, deployment_path, health_path, installed_path,
            check / "runtime-verification.json", check / "post-observation.log")
        need(
            verified["status"] == "passed_pending_publication"
            and verified["source_commit"] == commit
            and verified["candidate_manifest_sha256"] == expected_manifest_sha256
            and verified["deployment_receipt_sha256"] == expected_deployment_sha256
            and verified["health_summary_sha256"] == expected_health_sha256
            and verified["installed_verification_sha256"] == expected_installed_sha256
            and canonical_hash(verified["runtime_verification"])
                == canonical_hash(accepted["runtime_verification"]),
            "fresh publication verification differs from accepted finalization",
        )

        expected_models, expected_document, expected_lane = expected_transition_state(
            packet_root=packet,
            manifest=execute.load_json(artifacts["transition_manifest"]),
        )
        actual_models = execute.r11.current_models()
        need(actual_models == expected_models
             and len(actual_models) == manifest["runtime"]["model_config_count_after"],
             "published model inventory differs from the installed successor")
        model_hashes = {}
        for name in sorted(actual_models):
            path = execute.r11.STATE / name
            need(path.is_file() and not path.is_symlink(),
                 f"installed model config is unavailable: {name}")
            model_hashes[name] = sha(path)
        document_path = execute.r11.STATE / DOCUMENT_CONFIG
        lane_path = execute.r11.STATE / LANE_CONFIG
        need(execute.load_json(document_path) == expected_document
             and execute.load_json(lane_path) == expected_lane,
             "published document research configuration differs")

        runtime_record = {
            "schema_version": "dalton-runtime-config-pointer-0.2",
            "status": "deployed_verified",
            "at": accepted["verified_at"],
            "base_release_commit": commit,
            "candidate_manifest_sha256": expected_manifest_sha256,
            "prior_runtime_config_sha256": expected_current_runtime_config_sha256,
            "deployment_receipt_sha256": expected_deployment_sha256,
            "finalization_sha256": expected_finalization_sha256,
            "model_configs": model_hashes,
            "model_config_snapshot_sha256": sha(artifacts["model_config_after_snapshot"]),
            "document_research_config_sha256": sha(document_path),
            "mission_document_lane_config_sha256": sha(lane_path),
        }
        runtime_after = json_bytes(runtime_record)
        runtime_after_sha = sha_bytes(runtime_after)
        release_record = {
            "schema_version": "dalton-current-release-0.2",
            "status": "deployed_verified",
            "source_commit": commit,
            "release_ref": manifest["release_ref"],
            "release_packet": str(packet),
            "verified_at": accepted["verified_at"],
            "runtime_health_only": True,
            "research_completion_claimed": False,
            "research_governance_signed": False,
            "deployment_authorization": "owner previously authorized autonomous deployment",
            "candidate_manifest_sha256": expected_manifest_sha256,
            "deployment_receipt_sha256": expected_deployment_sha256,
            "health_summary_sha256": expected_health_sha256,
            "installed_verification_sha256": expected_installed_sha256,
            "finalization_sha256": expected_finalization_sha256,
            "current_runtime_config_sha256": runtime_after_sha,
            "previous_release": {
                "source_commit": previous_release["source_commit"],
                "release_ref": previous_release["release_ref"],
                "pointer_sha256": expected_current_release_sha256,
                "pointer_snapshot": str(release_snapshot),
            },
            "previous_runtime_config_sha256": expected_current_runtime_config_sha256,
            "fresh_rollback_snapshot": {
                "path": str(rollback_path),
                "database_snapshot_id": rollback["database_snapshot_id"],
            },
            "runtime_verification": accepted["runtime_verification"],
        }
        release_after = json_bytes(release_record)
        release_after_sha = sha_bytes(release_after)
        receipt = {
            "schema_version": "successor-publication-receipt-0.1",
            "status": "published_verified",
            "source_commit": commit,
            "release_ref": manifest["release_ref"],
            "candidate_manifest_sha256": expected_manifest_sha256,
            "prior_current_release_sha256": expected_current_release_sha256,
            "prior_runtime_config_sha256": expected_current_runtime_config_sha256,
            "prior_current_release_snapshot": str(release_snapshot),
            "prior_runtime_config_snapshot": str(runtime_snapshot),
            "current_release_sha256": release_after_sha,
            "current_runtime_config_sha256": runtime_after_sha,
            "deployment_receipt_sha256": expected_deployment_sha256,
            "health_summary_sha256": expected_health_sha256,
            "installed_verification_sha256": expected_installed_sha256,
            "finalization_sha256": expected_finalization_sha256,
            "research_governance_signed": False,
        }
        receipt_bytes = json_bytes(receipt)

        current_release_bytes = regular_bytes(current_release, "current release pointer")
        current_runtime_bytes = regular_bytes(current_runtime, "current runtime config pointer")
        if current_release_bytes == release_after and current_runtime_bytes == runtime_after:
            exclusive_or_exact(receipt_path, receipt_bytes)
            return {**receipt, "status": "publication_already_complete"}
        # Recover an exact pointer written by this helper if the process died
        # between the two atomic replacements.  Any third-party bytes remain a
        # conflict and are never overwritten.
        if current_release_bytes == release_before and current_runtime_bytes == runtime_after:
            replacement = atomic_draft(owner, "restore-current-runtime", runtime_before)
            os.replace(replacement, current_runtime); fsync_directory(owner)
            current_runtime_bytes = runtime_before
        elif current_release_bytes == release_after and current_runtime_bytes == runtime_before:
            replacement = atomic_draft(owner, "restore-current-release", release_before)
            os.replace(replacement, current_release); fsync_directory(owner)
            current_release_bytes = release_before
        need(current_release_bytes == release_before
             and current_runtime_bytes == runtime_before,
             "current release/runtime pointer changed before publication")
        runtime_draft = atomic_draft(owner, "current-runtime-config", runtime_after)
        release_draft = atomic_draft(owner, "current-release", release_after)
        published_runtime = published_release = False
        try:
            need(regular_bytes(current_runtime, "current runtime config pointer")
                    == runtime_before,
                 "current runtime config changed during publication")
            os.replace(runtime_draft, current_runtime); published_runtime = True
            fsync_directory(owner)
            if fault_hook is not None: fault_hook("after_runtime_pointer")
            need(regular_bytes(current_release, "current release pointer")
                    == release_before,
                 "current release changed during publication")
            os.replace(release_draft, current_release); published_release = True
            fsync_directory(owner)
            if fault_hook is not None: fault_hook("after_release_pointer")
            need(regular_bytes(current_release, "current release pointer") == release_after
                 and regular_bytes(current_runtime, "current runtime config pointer") == runtime_after,
                 "published pointer pair changed before verification")
        except Exception:
            if published_release and regular_bytes(
                    current_release, "current release pointer") == release_after:
                replacement = atomic_draft(owner, "restore-current-release", release_before)
                os.replace(replacement, current_release)
            if published_runtime and regular_bytes(
                    current_runtime, "current runtime config pointer") == runtime_after:
                replacement = atomic_draft(owner, "restore-current-runtime", runtime_before)
                os.replace(replacement, current_runtime)
            fsync_directory(owner)
            raise
        finally:
            runtime_draft.unlink(missing_ok=True); release_draft.unlink(missing_ok=True)
        exclusive_or_exact(receipt_path, receipt_bytes)
        return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--owner-packet", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--deployment-receipt", type=Path, required=True)
    parser.add_argument("--expected-deployment-sha256", required=True)
    parser.add_argument("--health-summary", type=Path, required=True)
    parser.add_argument("--expected-health-sha256", required=True)
    parser.add_argument("--installed-verification", type=Path, required=True)
    parser.add_argument("--expected-installed-sha256", required=True)
    parser.add_argument("--finalization", type=Path, required=True)
    parser.add_argument("--expected-finalization-sha256", required=True)
    parser.add_argument("--expected-current-release-sha256", required=True)
    parser.add_argument("--expected-current-runtime-config-sha256", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    result = publish(
        packet=args.packet, owner=args.owner_packet,
        expected_manifest_sha256=args.expected_manifest_sha256,
        deployment_path=args.deployment_receipt,
        expected_deployment_sha256=args.expected_deployment_sha256,
        health_path=args.health_summary, expected_health_sha256=args.expected_health_sha256,
        installed_path=args.installed_verification,
        expected_installed_sha256=args.expected_installed_sha256,
        finalization_path=args.finalization,
        expected_finalization_sha256=args.expected_finalization_sha256,
        expected_current_release_sha256=args.expected_current_release_sha256,
        expected_current_runtime_config_sha256=args.expected_current_runtime_config_sha256,
        receipt_path=args.receipt,
    )
    print(json.dumps({"status": result["status"],
                      "source_commit": result["source_commit"]}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, SuccessorPublicationError, execute.SuccessorExecuteError,
            finalizer.SuccessorFinalizeError) as exc:
        raise SystemExit(f"STOP: {exc}") from exc
