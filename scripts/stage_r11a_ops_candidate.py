#!/usr/bin/env python3
"""Stage a hash-bound, inert R11a operations packet candidate.

This program can copy reviewed evidence and helper bytes into a new packet. It
cannot approve the packet, replace a release manifest, install code, stop a
service, or write Dalton live state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "r11a-ops-stage-input-0.1"
CANDIDATE_SCHEMA = "r11a-ops-packet-candidate-0.1"
COMMIT = "424c22529d5317c4c5c9dc8ba88f37299cea3f49"
SOURCE = Path("/Users/everflow/Projects/dalton-foundation-followup-r11a-acceptance-worktree")
WHEEL_SHA256 = "418bf77f7b5285f9e53e041bdcf9d36463e57a7fa0bd4ce368ded2ec1e50d046"
OPENCLAW_SHA256 = "f45fa4698e55e6404f2558b2aa5f558bd906fd68f1d2e8c0914c475bd5b6a9a6"
HEX64 = re.compile(r"[0-9a-f]{64}")
SNAPSHOT_ID = re.compile(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}Z")
BOUNDARIES = {
    "live_mutation": False,
    "manifest_publication": False,
    "acceptance_transition": False,
    "historical_archives_copied": False,
}
ARTIFACTS = (
    "wheel", "wheel_verification", "full_suite_log", "full_suite_receipt",
    "full_suite_runner", "full_suite_native_result", "rehearsal_binding",
    "rehearsal_report", "service_config_before", "service_config_after",
    "model_config_snapshot", "openclaw_config_snapshot", "runtime_snapshot_receipt",
    "provider_plugin_snapshot",
    "provider_bridge_install_receipt",
    "mission_authority_snapshot", "web_v6_activation_receipt",
    "provider_bridge_activation_receipt", "alpha_v3_activation_receipt",
    "latest_backup_manifest", "backup_retention_receipt",
)
HELPERS = (
    "configure_backup_retention_stopped_window.py",
    "execute_r11a_stopped_window_candidate.py",
    "observe_r11a_health_candidate.py",
    "finalize_r11a_health_candidate.py",
)


class StageError(RuntimeError):
    pass


def need(ok: Any, reason: str) -> None:
    if not ok:
        raise StageError(reason)


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    wire = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    return hashlib.sha256(wire.encode()).hexdigest()


def json_file(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StageError(f"{label} is not valid JSON") from exc


def verify_source() -> None:
    need(SOURCE.is_dir(), "frozen source is unavailable")
    actual = subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(SOURCE), "status", "--porcelain", "--untracked-files=all"],
        text=True,
    )
    need(actual == COMMIT and not dirty, "frozen source identity changed")


def artifact_rows(document: Mapping[str, Any]) -> dict[str, Path]:
    rows = document.get("artifacts")
    need(isinstance(rows, Mapping) and set(rows) == set(ARTIFACTS), "artifact inventory differs")
    paths: dict[str, Path] = {}
    for name in ARTIFACTS:
        row = rows[name]
        need(isinstance(row, Mapping) and set(row) == {"path", "sha256"}, f"{name} row differs")
        value, expected = row.get("path"), row.get("sha256")
        need(isinstance(value, str) and Path(value).is_absolute(), f"{name} path is unresolved")
        need(isinstance(expected, str) and HEX64.fullmatch(expected), f"{name} hash is unresolved")
        path = Path(value)
        need(path.is_file() and not path.is_symlink(), f"{name} is not a regular file")
        need(sha(path) == expected, f"{name} hash changed")
        need("archive" not in path.name.lower(), f"{name} references a historical archive")
        paths[name] = path
    need(rows["wheel"]["sha256"] == WHEEL_SHA256, "wheel differs from verified R11a build")
    need(rows["openclaw_config_snapshot"]["sha256"] == OPENCLAW_SHA256,
         "OpenClaw snapshot differs from the activated provider bridge")
    return paths


def validate_suite(paths: Mapping[str, Path]) -> dict[str, Any]:
    receipt = json_file(paths["full_suite_receipt"], "full-suite receipt")
    native = json_file(paths["full_suite_native_result"], "native unittest result")
    need(receipt.get("status") == "passed" and receipt.get("exit_code") == 0,
         "full suite did not pass")
    need(receipt.get("code_commit") == receipt.get("final_commit") == COMMIT
         and Path(receipt.get("source_root", "")).resolve() == SOURCE,
         "full suite does not bind R11a source")
    need(receipt.get("clean") is True
         and receipt.get("runner_sha256") == sha(paths["full_suite_runner"])
         and receipt.get("native_result_sha256") == sha(paths["full_suite_native_result"])
         and receipt.get("log_sha256") == sha(paths["full_suite_log"]),
         "full-suite artifact binding differs")
    command = receipt.get("command")
    need(isinstance(command, list) and len(command) == 3
         and Path(command[1]).resolve() == paths["full_suite_runner"].resolve()
         and command[2] == "--child"
         and receipt.get("discovery") == {"start_dir": "tests", "top_level_dir": "."},
         "full-suite command does not cover the source tests")
    fields = {"tests", "failures", "errors", "skipped", "expected_failures",
              "unexpected_successes", "successful"}
    need(isinstance(native, Mapping) and set(native) == fields,
         "native unittest result is not closed")
    need(native == {key: receipt.get(key) for key in fields}
         and native["successful"] is True
         and native["failures"] == native["errors"] == native["unexpected_successes"] == 0,
         "native unittest result did not pass")
    return {"tests": native["tests"], "skipped": native["skipped"],
            "elapsed_seconds": receipt.get("elapsed_seconds")}


def validate_configuration(document: Mapping[str, Any], paths: Mapping[str, Path]) -> dict[str, Any]:
    models = json_file(paths["model_config_snapshot"], "model config snapshot")
    runtime = document.get("runtime_configuration")
    need(isinstance(runtime, Mapping)
         and set(runtime) == {"model_config_count", "semantic_snapshot_sha256"},
         "runtime configuration binding differs")
    count = runtime.get("model_config_count")
    semantic = runtime.get("semantic_snapshot_sha256")
    need(isinstance(count, int) and not isinstance(count, bool) and count > 0
         and isinstance(models, Mapping) and len(models) == count
         and isinstance(semantic, str) and semantic == canonical_hash(models),
         "model configuration snapshot differs")
    before = json_file(paths["service_config_before"], "service config before")
    after = json_file(paths["service_config_after"], "service config after")
    backup = before.get("backup") if isinstance(before, Mapping) else None
    need(isinstance(backup, Mapping) and backup.get("enabled") is True,
         "service backup is not enabled")
    expected = json.loads(json.dumps(before))
    expected["backup"]["keep_latest"] = 3
    need(after == expected, "service config candidate changes more than backup.keep_latest")
    latest = document.get("latest_backup")
    need(isinstance(latest, Mapping) and set(latest) == {"snapshot_id"}
         and isinstance(latest.get("snapshot_id"), str)
         and SNAPSHOT_ID.fullmatch(latest["snapshot_id"]), "latest backup is unresolved")
    manifest = json_file(paths["latest_backup_manifest"], "latest backup manifest")
    retention = json_file(paths["backup_retention_receipt"], "backup retention receipt")
    need(manifest.get("snapshot_id") == latest["snapshot_id"]
         and manifest.get("status") in {"fresh", "duplicate"}
         and isinstance(manifest.get("files"), list) and manifest["files"],
         "latest backup manifest differs")
    retained = retention.get("retained_snapshot_ids")
    need(retention.get("status") == "pruned" and isinstance(retained, list)
         and retained and retained[0] == latest["snapshot_id"]
         and "archive" not in json.dumps(retention, sort_keys=True).lower(),
         "retention receipt does not retain the selected latest backup")
    rehearsal = json_file(paths["rehearsal_binding"], "copied-state rehearsal binding")
    need(rehearsal.get("status") == "passed" and rehearsal.get("code_commit") == COMMIT
         and rehearsal.get("execution_boundary") == {
             "live_mutation": False, "external_calls": False, "model_calls": False,
             "processes": ["temporary writer", "one temporary controller tick"],
         }
         and rehearsal.get("inputs", {}).get("model_config_snapshot_sha256") == sha(paths["model_config_snapshot"])
         and rehearsal.get("inputs", {}).get("service_config_before_sha256") == sha(paths["service_config_before"])
         and rehearsal.get("inputs", {}).get("service_config_after_sha256") == sha(paths["service_config_after"])
         and rehearsal.get("inputs", {}).get("openclaw_config_snapshot_sha256") == OPENCLAW_SHA256
         and rehearsal.get("results", {}).get("model_config_count") == count
         and rehearsal.get("results", {}).get("model_config_semantic_sha256") == semantic,
         "copied-state rehearsal does not bind final R11a inputs")
    provider_install = json_file(paths["provider_bridge_install_receipt"], "provider bridge install receipt")
    provider_activation = json_file(paths["provider_bridge_activation_receipt"], "provider bridge activation receipt")
    web = json_file(paths["web_v6_activation_receipt"], "web v6 activation receipt")
    alpha = json_file(paths["alpha_v3_activation_receipt"], "Alpha v3 activation receipt")
    mission = json_file(paths["mission_authority_snapshot"], "mission authority snapshot")
    current_runtime = json_file(paths["runtime_snapshot_receipt"], "current runtime snapshot receipt")
    need(current_runtime.get("status") == "current_web_provider_configuration_snapshotted"
         and current_runtime.get("source_commit") == COMMIT
         and current_runtime.get("model_config_count") == count
         and current_runtime.get("prior_openclaw_sha256") == provider_install.get("owned_sha256", {}).get("config")
         and current_runtime.get("model_catalog_and_model_broker_subtree_unchanged") is True
         and current_runtime.get("model_config_snapshot_unchanged") is True
         and current_runtime.get("service_before_unchanged") is True,
         "current web-provider configuration delta differs")
    need(provider_install.get("status") == "installed_verified"
         and provider_activation.get("status") == "activated_verified"
         and provider_activation.get("bridge_receipt_sha256") == sha(paths["provider_bridge_install_receipt"]),
         "provider bridge activation differs")
    plugin = json_file(paths["provider_plugin_snapshot"], "provider plugin snapshot")
    plugin_root = Path(plugin.get("root", ""))
    plugin_rows = plugin.get("files")
    need(plugin.get("schema_version") == "r11a-provider-plugin-snapshot-0.1"
         and plugin.get("openclaw_config_sha256") == OPENCLAW_SHA256
         and isinstance(plugin_rows, list) and plugin_rows
         and canonical_hash(plugin_rows) == plugin.get("tree_sha256")
         and plugin_root.is_dir() and not plugin_root.is_symlink(),
         "provider plugin snapshot differs")
    for row in plugin_rows:
        need(isinstance(row, Mapping) and set(row) == {"path", "sha256"}
             and isinstance(row["path"], str) and not Path(row["path"]).is_absolute()
             and ".." not in Path(row["path"]).parts
             and sha(plugin_root / row["path"]) == row["sha256"],
             "provider plugin bytes differ")
    need(web.get("status") == "activated_verified"
         and web.get("selected_plan", {}).get("path", "").endswith("us-it-services-web-search-v6-host-recovery.json"),
         "web v6 activation differs")
    need(alpha.get("status") == "activated_verified" and alpha.get("large_state_copied") is False
         and alpha.get("mission", {}).get("hash") == mission.get("active_hash")
         and mission.get("record_integrity_verified") is True,
         "Alpha or mission authority activation differs")
    return {"model_config_count": count, "semantic_snapshot_sha256": semantic,
            "service_config_before_sha256": sha(paths["service_config_before"]),
            "service_config_after_sha256": sha(paths["service_config_after"]),
            "backup_keep_latest": 3, "latest_backup_snapshot_id": latest["snapshot_id"]}


def validate(document: Mapping[str, Any]) -> tuple[dict[str, Path], dict[str, Any]]:
    need(document.get("schema_version") == SCHEMA and document.get("release_ref") == "foundation-followup-r11a",
         "release identity differs")
    need(document.get("status") == "complete_pending_staging", "stage inputs are incomplete")
    need(document.get("source") == {"root": str(SOURCE), "commit": COMMIT}, "source binding differs")
    need(document.get("boundaries") == BOUNDARIES, "stage boundaries differ")
    verify_source()
    paths = artifact_rows(document)
    suite = validate_suite(paths)
    configuration = validate_configuration(document, paths)
    wheel = json_file(paths["wheel_verification"], "wheel verification")
    need(wheel.get("code_commit") == COMMIT and Path(wheel.get("source_root", "")).resolve() == SOURCE
         and wheel.get("wheel_sha256") == WHEEL_SHA256 and wheel.get("source_bytes_match") is True,
         "wheel verification differs")
    verify_source()
    return paths, {"full_suite": suite, "runtime_configuration": configuration}


def stage(document: Mapping[str, Any], output: Path) -> dict[str, Any]:
    paths, evidence = validate(document)
    need(not output.exists() and not output.is_symlink(), "output packet already exists")
    need(output.parent.is_dir() and not output.parent.is_symlink(), "output parent is unavailable")
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        copied: dict[str, dict[str, str]] = {}
        for name, source in paths.items():
            destination = temporary / (name + source.suffix)
            destination.write_bytes(source.read_bytes()); os.chmod(destination, 0o600)
            copied_sha = sha(destination)
            need(copied_sha == document["artifacts"][name]["sha256"]
                 and sha(source) == copied_sha, f"{name} changed while staging")
            copied[name] = {"file": destination.name, "sha256": copied_sha}
        helper_rows = {}
        source_root = Path(__file__).resolve().parent
        for name in HELPERS:
            source = source_root / name
            need(source.is_file() and not source.is_symlink(), f"helper is unavailable: {name}")
            destination = temporary / name
            destination.write_bytes(source.read_bytes()); os.chmod(destination, 0o700 if name.endswith(".py") else 0o600)
            helper_rows[name] = sha(destination)
        candidate = {
            "schema_version": CANDIDATE_SCHEMA,
            "release_ref": "foundation-followup-r11a",
            "status": "staged_pending_owner_acceptance",
            "acceptance_state": "pending",
            "deployment_state": "not_started",
            "source": {"root": str(SOURCE), "commit": COMMIT},
            "artifacts": copied,
            "helpers": helper_rows,
            **evidence,
            "deployment_sequence": [
                "preflight exact accepted packet and live configuration",
                "controlled stop and child drain",
                "fresh verified SQLite/config backup",
                "pip --no-deps exact accepted wheel",
                "validate with new ServiceConfig and CAS backup.keep_latest=3",
                "run unchanged frozen install.sh and start services",
                "installed-byte/config verification",
                "sustained health observation and separate finalization",
            ],
            "boundaries": dict(BOUNDARIES),
        }
        candidate["content_hash"] = canonical_hash(candidate)
        (temporary / "release-manifest.candidate.json").write_text(
            json.dumps(candidate, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary / "release-manifest.candidate.json", 0o600)
        os.replace(temporary, output)
        return candidate
    finally:
        if temporary.exists(): shutil.rmtree(temporary)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-inert-template", action="store_true")
    args = parser.parse_args(argv)
    document = json_file(args.inputs, "stage inputs")
    if args.verify_inert_template:
        need(args.output is None, "inert verification cannot stage output")
        need(document.get("status") == "incomplete", "template is not inert")
        need(any(row.get("path") is None or row.get("sha256") is None
                 for row in document.get("artifacts", {}).values()), "template is unexpectedly complete")
        need(document.get("boundaries") == BOUNDARIES, "template boundaries differ")
        print(json.dumps({"status": "inert_template_verified", "live_mutation": False}))
        return 0
    need(args.output is not None, "--output is required for staging")
    candidate = stage(document, args.output.resolve())
    print(json.dumps({"status": candidate["status"], "content_hash": candidate["content_hash"]}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StageError as exc:
        raise SystemExit(f"STOP: {exc}") from exc
