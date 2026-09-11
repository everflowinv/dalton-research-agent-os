#!/usr/bin/env python3
"""Build an inert, hash-bound release acceptance candidate.

This helper has no accept, publish, install, or live-mutation operation.  It
reads an explicitly prepared packet, verifies every named artifact, and writes
one new ``*.candidate.json`` with exclusive-create semantics.  The owner-facing
release stager remains a separate, reviewed action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "dalton-release-acceptance-candidate-input-0.1"
CANDIDATE_SCHEMA_VERSION = "dalton-release-acceptance-candidate-0.1"
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
SNAPSHOT_ID = re.compile(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}Z")

ARTIFACT_NAMES = (
    "wheel",
    "wheel_verification",
    "full_suite_log",
    "full_suite_receipt",
    "rehearsal_binding",
    "rehearsal_report",
    "release_helper_manifest",
    "deployment_candidate",
    "backup_retention_config_helper",
    "service_config_before_snapshot",
    "service_config_after_candidate",
    "final_activated_model_config_snapshot",
    "openclaw_config_snapshot",
    "plugin_tree_manifest",
    "mission_authority_snapshot",
    "provider_bridge_activation_receipt",
    "web_activation_receipt",
    "backup_retention_receipt",
    "latest_backup_manifest",
)

TOP_FIELDS = {
    "schema_version",
    "release_ref",
    "status",
    "acceptance_state",
    "deployment_state",
    "packet_root",
    "source",
    "artifacts",
    "runtime_configuration",
    "latest_backup",
    "boundaries",
}
ARTIFACT_FIELDS = {"path", "sha256"}
BOUNDARIES = {
    "live_mutation": False,
    "manifest_publication": False,
    "acceptance_transition": False,
    "historical_archives_copied": False,
}


class CandidateError(RuntimeError):
    pass


def _need(condition: bool, reason: str) -> None:
    if not condition:
        raise CandidateError(reason)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    wire = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n"
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def template(release_ref: str | None = None) -> dict[str, Any]:
    """Return the closed, deliberately incomplete review input."""

    return {
        "schema_version": SCHEMA_VERSION,
        "release_ref": release_ref,
        "status": "candidate_inputs_incomplete",
        "acceptance_state": "pending",
        "deployment_state": "not_started",
        "packet_root": None,
        "source": {"root": None, "commit": None},
        "artifacts": {
            name: {"path": None, "sha256": None} for name in ARTIFACT_NAMES
        },
        "runtime_configuration": {
            "model_config_count": None,
            "semantic_snapshot_sha256": None,
        },
        "latest_backup": {"snapshot_id": None},
        "boundaries": dict(BOUNDARIES),
    }


def _regular_packet_artifact(packet_root: Path, row: Mapping[str, Any], name: str) -> Path:
    _need(set(row) == ARTIFACT_FIELDS, f"{name} artifact fields are not closed")
    relative = row.get("path")
    expected = row.get("sha256")
    _need(isinstance(relative, str) and relative, f"{name} path is unresolved")
    rel_path = Path(relative)
    _need(not rel_path.is_absolute() and ".." not in rel_path.parts, f"{name} path escapes packet")
    _need(isinstance(expected, str) and HEX64.fullmatch(expected) is not None, f"{name} SHA-256 is unresolved")
    path = packet_root / rel_path
    _need(path.is_file() and not path.is_symlink(), f"{name} is not a regular packet artifact")
    _need(path.resolve().is_relative_to(packet_root), f"{name} resolved outside packet")
    _need(_sha256(path) == expected, f"{name} SHA-256 changed")
    return path


def _validate_source(source: Mapping[str, Any]) -> tuple[Path, str]:
    _need(set(source) == {"root", "commit"}, "source fields are not closed")
    root_value, commit = source.get("root"), source.get("commit")
    _need(isinstance(root_value, str) and Path(root_value).is_absolute(), "source root is unresolved")
    _need(isinstance(commit, str) and HEX40.fullmatch(commit) is not None, "source commit is unresolved")
    try:
        root = Path(root_value).resolve(strict=True)
    except OSError as exc:
        raise CandidateError("source root is unavailable") from exc
    _need(root.is_dir(), "source root is unavailable")
    try:
        actual = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
        dirty = subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CandidateError("source root is not a readable Git checkout") from exc
    _need(actual == commit, "source checkout HEAD differs from frozen commit")
    _need(not dirty, "source checkout is not clean")
    return root, commit


def _validate_runtime_snapshot(path: Path, runtime: Mapping[str, Any]) -> tuple[int, str]:
    _need(
        set(runtime) == {"model_config_count", "semantic_snapshot_sha256"},
        "runtime configuration fields are not closed",
    )
    count = runtime.get("model_config_count")
    semantic = runtime.get("semantic_snapshot_sha256")
    _need(isinstance(count, int) and not isinstance(count, bool) and count > 0, "model config count is unresolved")
    _need(isinstance(semantic, str) and HEX64.fullmatch(semantic) is not None, "model config semantic SHA-256 is unresolved")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CandidateError("final activated model config snapshot is invalid JSON") from exc
    _need(isinstance(snapshot, dict), "final activated model config snapshot is not an object")
    _need(len(snapshot) == count, "model config count differs from final activated snapshot")
    _need(
        all(isinstance(name, str) and name.endswith("-model-config.json") for name in snapshot),
        "final activated model config inventory contains an invalid name",
    )
    _need(_canonical_sha256(snapshot) == semantic, "model config semantic snapshot SHA-256 changed")
    return count, semantic


def _validate_latest_backup(
    manifest_path: Path, retention_path: Path, latest: Mapping[str, Any]
) -> str:
    _need(set(latest) == {"snapshot_id"}, "latest backup fields are not closed")
    snapshot_id = latest.get("snapshot_id")
    _need(isinstance(snapshot_id, str) and SNAPSHOT_ID.fullmatch(snapshot_id) is not None, "latest backup snapshot ID is unresolved")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        retention = json.loads(retention_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CandidateError("backup evidence is invalid JSON") from exc
    _need(
        isinstance(manifest, dict)
        and manifest.get("schema_version") == "0.1"
        and manifest.get("status") in {"fresh", "duplicate"}
        and manifest.get("snapshot_id") == snapshot_id
        and isinstance(manifest.get("files"), list)
        and bool(manifest["files"]),
        "latest backup manifest does not bind the selected snapshot",
    )
    _need(isinstance(retention, dict) and retention.get("status") == "pruned", "backup retention did not complete")
    retained = retention.get("retained_snapshot_ids")
    _need(isinstance(retained, list) and bool(retained), "backup retention has no verified retained snapshots")
    _need(retained[0] == snapshot_id, "selected backup is not the latest verified retained snapshot")
    _need("archive" not in json.dumps(retention, sort_keys=True).lower(), "backup retention evidence references an archive")
    return snapshot_id


def _validate_service_config_delta(before_path: Path, after_path: Path) -> dict[str, Any]:
    try:
        before = json.loads(before_path.read_text(encoding="utf-8"))
        after = json.loads(after_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CandidateError("service configuration evidence is invalid JSON") from exc
    try:
        from scripts.configure_backup_retention_stopped_window import (
            REVIEWED_KEEP_LATEST,
            configure,
        )
    except ModuleNotFoundError:  # direct ``python scripts/...`` execution
        from configure_backup_retention_stopped_window import (  # type: ignore[no-redef]
            REVIEWED_KEEP_LATEST,
            configure,
        )
    try:
        expected, preserved_sha256 = configure(before)
    except RuntimeError as exc:
        raise CandidateError(str(exc)) from exc
    _need(after == expected, "service config candidate changes more than backup.keep_latest")
    return {
        "phase": "after_runtime_install_before_controller_start",
        "delta": {"backup.keep_latest": REVIEWED_KEEP_LATEST},
        "before_sha256": _sha256(before_path),
        "after_sha256": _sha256(after_path),
        "preserved_without_backup_keep_latest_sha256": preserved_sha256,
        "receipt_file": "r11-backup-retention-config-receipt.json",
    }


def build_candidate(document: Mapping[str, Any]) -> dict[str, Any]:
    """Verify a complete input document and return an inert candidate."""

    _need(set(document) == TOP_FIELDS, "candidate input fields are not closed")
    _need(document.get("schema_version") == SCHEMA_VERSION, "candidate input schema differs")
    _need(isinstance(document.get("release_ref"), str) and document["release_ref"], "release ref is unresolved")
    _need(document.get("status") == "candidate_inputs_complete", "candidate inputs are not marked complete")
    _need(document.get("acceptance_state") == "pending", "candidate cannot carry acceptance")
    _need(document.get("deployment_state") == "not_started", "candidate cannot carry deployment state")
    _need(document.get("boundaries") == BOUNDARIES, "candidate execution boundaries differ")

    packet_value = document.get("packet_root")
    _need(isinstance(packet_value, str) and Path(packet_value).is_absolute(), "packet root is unresolved")
    try:
        packet_root = Path(packet_value).resolve(strict=True)
    except OSError as exc:
        raise CandidateError("packet root is unavailable") from exc
    _need(packet_root.is_dir(), "packet root is unavailable")
    source_root, commit = _validate_source(document.get("source", {}))

    artifacts = document.get("artifacts")
    _need(isinstance(artifacts, Mapping) and set(artifacts) == set(ARTIFACT_NAMES), "artifact inventory differs")
    paths = {
        name: _regular_packet_artifact(packet_root, artifacts[name], name)
        for name in ARTIFACT_NAMES
    }
    count, semantic = _validate_runtime_snapshot(
        paths["final_activated_model_config_snapshot"],
        document.get("runtime_configuration", {}),
    )
    snapshot_id = _validate_latest_backup(
        paths["latest_backup_manifest"],
        paths["backup_retention_receipt"],
        document.get("latest_backup", {}),
    )
    service_delta = _validate_service_config_delta(
        paths["service_config_before_snapshot"],
        paths["service_config_after_candidate"],
    )
    candidate = {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "release_ref": document["release_ref"],
        "status": "candidate_pending_owner_acceptance",
        "acceptance_state": "pending",
        "deployment_state": "not_started",
        "packet_root": str(packet_root),
        "source": {"root": str(source_root), "commit": commit},
        "artifacts": {
            name: {"path": artifacts[name]["path"], "sha256": artifacts[name]["sha256"]}
            for name in ARTIFACT_NAMES
        },
        "runtime_configuration": {
            "model_config_count": count,
            "semantic_snapshot_sha256": semantic,
        },
        "latest_backup": {"snapshot_id": snapshot_id},
        "deployment_operations": {"backup_retention": service_delta},
        "boundaries": dict(BOUNDARIES),
    }
    candidate["content_hash"] = _canonical_sha256(candidate)
    return candidate


def write_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    data = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    _need(path.parent.is_dir() and not path.parent.is_symlink(), "output directory is unavailable")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--write-template", type=Path)
    modes.add_argument("--inputs", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.write_template is not None:
        _need(args.output is None, "--output is invalid with --write-template")
        _need(args.write_template.name.endswith(".template.json"), "template output must end in .template.json")
        write_exclusive(args.write_template, template())
        print(json.dumps({"status": "template_written", "path": str(args.write_template)}))
        return 0
    _need(args.output is not None, "--output is required with --inputs")
    document = json.loads(args.inputs.read_text(encoding="utf-8"))
    candidate = build_candidate(document)
    _need(args.output.name.endswith(".candidate.json"), "candidate output must end in .candidate.json")
    output_parent = args.output.parent.resolve()
    packet_root = Path(candidate["packet_root"])
    _need(output_parent.is_relative_to(packet_root), "candidate output must stay inside packet root")
    write_exclusive(args.output, candidate)
    print(json.dumps({"status": candidate["status"], "content_hash": candidate["content_hash"]}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CandidateError as exc:
        raise SystemExit(f"STOP: {exc}") from exc
