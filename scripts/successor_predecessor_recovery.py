"""Closed evidence for a recovered installed predecessor distinct from publication."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping


class PredecessorRecoveryError(RuntimeError):
    pass


ARTIFACT_KEYS = frozenset({
    "published_pointer", "published_previous_pointer", "failed_manifest",
    "failed_deployment", "recovery_prestart", "recovery_restart", "accepted_wheel",
    "external_before", "external_after",
    "model_snapshot", "service_snapshot", "openclaw_snapshot", "rollback_state",
    "published_manifest", "published_deployment", "published_installed",
    "published_health", "published_finalization", "published_publication",
    "rollback_initial", "published_runtime_pointer", "published_previous_runtime_pointer",
})
WRITER_IDENTITY_KEYS = frozenset({
    "snapshot_sha256", "live_sha256", "principal_inventory_exact",
    "non_core_principals_exact", "core_token_equal", "core_non_operation_fields_exact",
    "before_operation_count", "after_operation_count", "only_added_operation",
    "after_equals_source_core_operations",
})
EXTERNAL_AUTHORITY_KEYS = frozenset({
    "external_origin_release", "external_origin_acceptance", "r18b_source_commit",
    "r18b_transition_sha256", "r18b_outer_receipt_sha256",
    "r18b_inner_receipt_sha256", "r18b_installed_sha256", "openclaw_config_sha256",
    "host_target_sha256", "broker_tree_sha256", "host_helper_sha256",
})
HEX40 = set("0123456789abcdef")
HEX64 = set("0123456789abcdef")


def _need(value: Any, reason: str) -> None:
    if not value:
        raise PredecessorRecoveryError(reason)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Mapping[str, Any]) -> str:
    return _sha((json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")) + "\n").encode())


def _hex(value: Any, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and set(value) <= (HEX40 if length == 40 else HEX64)


def _content_hash_valid(value: Mapping[str, Any]) -> bool:
    asserted = value.get("content_hash")
    return _hex(asserted, 64) and asserted == _canonical(
        {key: item for key, item in value.items() if key != "content_hash"})


def _read(path: Path, *, json_value: bool = True) -> tuple[Any, bytes]:
    _need(path.is_file() and not path.is_symlink() and path.absolute() == path.resolve(),
          "recovery artifact is unavailable or unsafe")
    data = path.read_bytes()
    if not json_value:
        return None, data
    try:
        value = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PredecessorRecoveryError("recovery artifact is invalid JSON") from exc
    _need(isinstance(value, Mapping), "recovery artifact is not an object")
    return value, data


def _protected_rows(root: Path, *, exclude_writer: bool) -> list[list[Any]]:
    _need(root.is_dir() and not root.is_symlink() and root.absolute() == root.resolve(),
          "protected-state snapshot is unavailable or unsafe")
    managed = {"model-catalog-sync.json"}
    paths = [path for path in root.glob("*.json")
             if path.name not in managed
             and (not exclude_writer or path.name != "writer-tokens.json")]
    for name in ("connector-governance", "governance-decisions", "discovery-plans"):
        directory = root / name
        if directory.is_dir():
            paths.extend([directory, *directory.rglob("*")])
    rows = []
    for path in sorted(set(paths)):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode & 0o7777
        if path.is_symlink(): rows.append([relative, "symlink", mode, os.readlink(path)])
        elif path.is_file(): rows.append([relative, "file", mode, _sha(path.read_bytes())])
        elif path.is_dir(): rows.append([relative, "dir", mode])
        else: raise PredecessorRecoveryError("protected-state snapshot has special entry")
    return rows


def protected_state_hash(root: Path) -> str:
    rows = _protected_rows(root, exclude_writer=True)
    return _sha((json.dumps(rows, sort_keys=True, separators=(",", ":")) + "\n").encode())


def _full_protected_state_hash(root: Path) -> str:
    rows = _protected_rows(root, exclude_writer=False)
    return _sha((json.dumps(rows, sort_keys=True, separators=(",", ":")) + "\n").encode())


def _identity(artifacts: Mapping[str, Path]) -> dict[str, Any]:
    _need(isinstance(artifacts, Mapping) and set(artifacts) == ARTIFACT_KEYS,
          "recovery artifact inventory differs")
    loaded: dict[str, Any] = {}
    hashes: dict[str, str] = {}
    for name in sorted(ARTIFACT_KEYS):
        if name == "rollback_state":
            loaded[name] = None
            hashes[name] = protected_state_hash(Path(artifacts[name]))
            continue
        value, data = _read(Path(artifacts[name]), json_value=name not in {
            "accepted_wheel", "service_snapshot", "openclaw_snapshot"})
        loaded[name] = value
        hashes[name] = _sha(data)

    pointer = loaded["published_pointer"]
    previous = loaded["published_previous_pointer"]
    manifest = loaded["failed_manifest"]
    deployment = loaded["failed_deployment"]
    prestart = loaded["recovery_prestart"]
    restart = loaded["recovery_restart"]
    external_before = loaded["external_before"]
    external_after = loaded["external_after"]
    published_receipts = {
        name: loaded[name] for name in ("published_manifest", "published_deployment",
            "published_installed", "published_health", "published_finalization",
            "published_publication")}
    current_runtime = loaded["published_runtime_pointer"]
    previous_runtime = loaded["published_previous_runtime_pointer"]
    rollback_initial = loaded["rollback_initial"]

    _need(pointer.get("schema_version") == "dalton-current-release-0.2"
          and pointer.get("status") == "deployed_verified"
          and _hex(pointer.get("current_runtime_config_sha256"), 64)
          and isinstance(pointer.get("previous_release"), Mapping),
          "published release pointer differs")
    link = pointer["previous_release"]
    _need(link.get("pointer_sha256") == hashes["published_previous_pointer"]
          and link.get("release_ref") == previous.get("release_ref")
          and link.get("source_commit") == previous.get("source_commit"),
          "published release chain differs")
    published_hash_fields = {
        "published_manifest": "candidate_manifest_sha256",
        "published_deployment": "deployment_receipt_sha256",
        "published_installed": "installed_verification_sha256",
        "published_health": "health_summary_sha256",
        "published_finalization": "finalization_sha256",
    }
    _need(all(pointer.get(field) == hashes[name]
              for name, field in published_hash_fields.items())
          and published_receipts["published_deployment"].get("status")
              == "installer_finished_runtime_health_pending"
          and published_receipts["published_installed"].get("status")
              == "installed_bytes_verified_runtime_pending"
          and published_receipts["published_health"].get("status") == "passed"
          and published_receipts["published_finalization"].get("status")
              == "passed_pending_publication"
          and published_receipts["published_publication"].get("status")
              == "published_verified"
          and published_receipts["published_publication"].get("current_release_sha256")
              == hashes["published_pointer"],
          "published acceptance chain differs")
    publication = published_receipts["published_publication"]
    published_commit = pointer.get("source_commit")
    published_candidate_hash = hashes["published_manifest"]
    _need(publication.get("schema_version") == "successor-publication-receipt-0.1"
          and publication.get("release_ref") == pointer.get("release_ref")
          and publication.get("source_commit") == published_commit
          and publication.get("candidate_manifest_sha256") == published_candidate_hash
          and publication.get("current_runtime_config_sha256")
              == hashes["published_runtime_pointer"]
          and publication.get("prior_runtime_config_sha256")
              == hashes["published_previous_runtime_pointer"]
          and current_runtime.get("schema_version") == "dalton-runtime-config-pointer-0.2"
          and current_runtime.get("status") == "deployed_verified"
          and current_runtime.get("base_release_commit") == published_commit
          and current_runtime.get("candidate_manifest_sha256") == published_candidate_hash
          and previous_runtime.get("schema_version") == "dalton-runtime-config-pointer-0.2"
          and previous_runtime.get("status") == "deployed_verified"
          and previous_runtime.get("base_release_commit") == previous.get("source_commit")
          and previous_runtime.get("candidate_manifest_sha256")
              == previous.get("candidate_manifest_sha256"),
          "published runtime pointer chain differs")
    _need(pointer.get("current_runtime_config_sha256")
              == hashes["published_runtime_pointer"]
          and previous.get("current_runtime_config_sha256")
              == hashes["published_previous_runtime_pointer"],
          "published pointer runtime hashes differ")
    published_candidate = published_receipts["published_manifest"]
    published_deployment = published_receipts["published_deployment"]
    published_installed = published_receipts["published_installed"]
    published_health = published_receipts["published_health"]
    published_finalization = published_receipts["published_finalization"]
    _need(published_candidate.get("schema_version") == "successor-stopped-window-candidate-0.1"
          and published_candidate.get("release_ref") == pointer.get("release_ref")
          and published_candidate.get("source", {}).get("commit") == published_commit
          and published_deployment.get("schema_version")
              == "successor-stopped-window-execution-0.1"
          and published_deployment.get("exit_code") == 0
          and published_deployment.get("source_commit") == published_commit
          and published_deployment.get("candidate_manifest_sha256") == published_candidate_hash
          and published_installed.get("schema_version")
              == "successor-installed-verification-0.1"
          and published_installed.get("source_commit") == published_commit
          and published_installed.get("candidate_manifest_sha256") == published_candidate_hash
          and published_health.get("schema_version") == "successor-health-observation-0.1"
          and published_health.get("accepted") is True
          and published_health.get("source_commit") == published_commit
          and published_finalization.get("schema_version")
              == "successor-runtime-verification-candidate-0.1"
          and published_finalization.get("source_commit") == published_commit,
          "published receipt references differ")

    source = manifest.get("source")
    wheel = manifest.get("artifacts", {}).get("wheel") if isinstance(manifest.get("artifacts"), Mapping) else None
    _need(manifest.get("status") == "accepted_for_stopped_window"
          and isinstance(source, Mapping) and _hex(source.get("commit"), 40)
          and isinstance(wheel, Mapping), "failed candidate authority differs")
    commit = source["commit"]
    _need(wheel.get("sha256") == hashes["accepted_wheel"], "accepted wheel bytes differ")
    candidate_artifacts = manifest.get("artifacts")
    _need(isinstance(candidate_artifacts, Mapping)
          and candidate_artifacts.get("model_config_after_snapshot", {}).get("sha256")
              == hashes["model_snapshot"]
          and candidate_artifacts.get("service_config_snapshot", {}).get("sha256")
              == hashes["service_snapshot"]
          and candidate_artifacts.get("openclaw_config_snapshot", {}).get("sha256")
              == hashes["openclaw_snapshot"],
          "recovered configuration snapshots differ from failed candidate")
    _need(deployment.get("schema_version") == "successor-stopped-window-execution-0.1"
          and deployment.get("status") == "deployment_failed"
          and deployment.get("rollback", {}).get("status") == "rollback_failed"
          and deployment.get("source_commit") == commit
          and deployment.get("candidate_manifest_sha256") == hashes["failed_manifest"],
          "failed deployment history differs")
    snapshot = deployment.get("fresh_rollback_snapshot")
    _need(isinstance(snapshot, Mapping)
          and Path(str(snapshot.get("path", ""))).resolve()
              == Path(artifacts["rollback_state"]).resolve().parent
          and rollback_initial.get("protected_state_sha256")
              == _full_protected_state_hash(Path(artifacts["rollback_state"]))
          and _sha((Path(artifacts["rollback_state"]) / "writer-tokens.json").read_bytes())
              == prestart.get("writer_tokens", {}).get("snapshot_sha256"),
          "rollback protected-state authority differs")
    _need(prestart.get("schema_version")
              == "foundation-r21-recovery-prestart-verification-0.1"
          and prestart.get("status") == "verified_failed_deployment_runtime_safe_to_restart"
          and prestart.get("source_commit") == commit
          and prestart.get("failed_deployment_sha256") == hashes["failed_deployment"]
          and prestart.get("accepted_manifest_sha256") == hashes["failed_manifest"]
          and prestart.get("accepted_wheel_sha256") == hashes["accepted_wheel"]
          and prestart.get("runtime_matches_accepted_wheel") is True
          and prestart.get("model_configs_exact") is True
          and prestart.get("service_config_exact") is True
          and prestart.get("openclaw_config_exact") is True
          and prestart.get("protected_state_excluding_writer_tokens_exact") is True,
          "recovered installed identity differs")
    _need(prestart.get("boundaries") == {
        "database_restore_performed": False, "metadata_overwrite_performed": False,
        "service_calls": False, "provider_calls": False, "live_mutation": False,
    }, "recovery prestart boundaries differ")
    _need(_content_hash_valid(prestart), "recovery prestart content hash differs")
    _need(restart.get("schema_version") == "foundation-r21-service-recovery-0.1"
          and restart.get("status") == "verified_installed_runtime_restarted_healthy"
          and restart.get("source_commit") == commit
          and restart.get("failed_deployment_sha256") == hashes["failed_deployment"]
          and restart.get("prestart_proof_sha256") == hashes["recovery_prestart"]
          and restart.get("published_owner_release") == pointer.get("release_ref")
          and restart.get("deployment_reclassified") is False
          and restart.get("database_restore_performed") is False
          and restart.get("metadata_overwrite_performed") is False,
          "recovery restart history differs")

    _need(hashes["external_before"] == hashes["external_after"]
          and external_before == external_after
          and external_after.get("status") == "verified_read_only_inert"
          and external_after.get("candidate_commit") == commit
          and external_after.get("published_owner_predecessor_release") == pointer.get("release_ref")
          and external_after.get("external_origin_release") == "foundation-r18b"
          and external_after.get("external_origin_acceptance")
              == "runtime_installed_health_failed_unpublished"
          and external_after.get("live_mutation") is False
          and external_after.get("external_mutations_authorized") == 0
          and external_after.get("model_calls") == 0
          and external_after.get("network_calls") == 0
          and prestart.get("external_dependency_receipt_sha256") == hashes["external_after"],
          "R18b external dependency authority differs")
    _need(_content_hash_valid(external_after), "external dependency content hash differs")

    writer = prestart.get("writer_tokens")
    _need(isinstance(writer, Mapping) and set(writer) == WRITER_IDENTITY_KEYS
          and _hex(writer.get("snapshot_sha256"), 64)
          and _hex(writer.get("live_sha256"), 64)
          and all(writer.get(key) is True for key in {
              "principal_inventory_exact", "non_core_principals_exact", "core_token_equal",
              "core_non_operation_fields_exact", "after_equals_source_core_operations"})
          and type(writer.get("before_operation_count")) is int
          and writer.get("after_operation_count") == writer.get("before_operation_count") + 1
          and isinstance(writer.get("only_added_operation"), str)
          and bool(writer.get("only_added_operation")),
          "writer recovery identity differs")

    installed = {
        "source_commit": commit,
        "accepted_wheel_sha256": hashes["accepted_wheel"],
        "runtime_file_count": prestart.get("runtime_file_count"),
        "runtime_matches_accepted_wheel": True,
        "model_config_count": prestart.get("model_config_count"),
        "model_configs_exact": True,
        "model_config_semantic_sha256": _canonical(loaded["model_snapshot"]),
        "service_config_exact": True,
        "service_config_sha256": hashes["service_snapshot"],
        "openclaw_config_exact": True,
        "openclaw_config_sha256": hashes["openclaw_snapshot"],
        "reviewed_plist_sha256": prestart.get("reviewed_plist_sha256"),
        "protected_entries_excluding_writer_tokens": prestart.get(
            "protected_entries_excluding_writer_tokens"),
        "protected_state_excluding_writer_tokens_exact": True,
        "protected_state_excluding_writer_tokens_sha256": hashes["rollback_state"],
        "writer_tokens": dict(writer),
    }
    _need(type(installed["runtime_file_count"]) is int
          and installed["runtime_file_count"] > 0
          and type(installed["model_config_count"]) is int
          and installed["model_config_count"] > 0
          and isinstance(installed["reviewed_plist_sha256"], Mapping)
          and bool(installed["reviewed_plist_sha256"])
          and all(isinstance(key, str) and key and _hex(value, 64)
                  for key, value in installed["reviewed_plist_sha256"].items())
          and isinstance(installed["writer_tokens"], Mapping),
          "recovered installed evidence is incomplete")
    external_fields = {key: external_after.get(key) for key in sorted(EXTERNAL_AUTHORITY_KEYS)}
    _need(all(value is not None for value in external_fields.values())
          and _hex(external_fields["r18b_source_commit"], 40)
          and all(_hex(value, 64) for key, value in external_fields.items()
                  if key.endswith("_sha256")),
          "R18b external authority is incomplete")
    return {
        "schema_version": "successor-predecessor-recovery-0.1",
        "status": "failed_install_recovered_unpublished",
        "published_release": {
            "release_ref": pointer.get("release_ref"),
            "source_commit": pointer.get("source_commit"),
            "pointer_sha256": hashes["published_pointer"],
            "runtime_pointer_sha256": pointer.get("current_runtime_config_sha256"),
            "previous_pointer_sha256": hashes["published_previous_pointer"],
            "acceptance_artifact_sha256": {
                name: hashes[name] for name in sorted(published_receipts)},
        },
        "failed_install": {
            "release_ref": manifest.get("release_ref"), "source_commit": commit,
            "candidate_manifest_sha256": hashes["failed_manifest"],
            "deployment_receipt_sha256": hashes["failed_deployment"],
            "deployment_status": deployment.get("status"),
            "rollback_status": deployment.get("rollback", {}).get("status"),
        },
        "recovery": {
            "prestart_receipt_sha256": hashes["recovery_prestart"],
            "restart_receipt_sha256": hashes["recovery_restart"],
            "prestart_status": prestart.get("status"),
            "restart_status": restart.get("status"),
            "installed_identity": installed,
        },
        "external_dependency": {
            "before_receipt_sha256": hashes["external_before"],
            "after_receipt_sha256": hashes["external_after"],
            "unchanged": True, "authority": external_fields,
        },
        "artifacts": hashes,
    }


def build_recovery_proof(*, published_pointer_path: Path,
                         published_previous_pointer_path: Path,
                         failed_manifest_path: Path, failed_deployment_path: Path,
                         recovery_prestart_path: Path, recovery_restart_path: Path,
                         accepted_wheel_path: Path, external_before_path: Path,
                         external_after_path: Path, model_snapshot_path: Path,
                         service_snapshot_path: Path, openclaw_snapshot_path: Path,
                         rollback_state_path: Path, published_manifest_path: Path,
                         published_deployment_path: Path, published_installed_path: Path,
                         published_health_path: Path, published_finalization_path: Path,
                         published_publication_path: Path, rollback_initial_path: Path,
                         published_runtime_pointer_path: Path,
                         published_previous_runtime_pointer_path: Path) -> dict[str, Any]:
    body = _identity({
        "published_pointer": published_pointer_path,
        "published_previous_pointer": published_previous_pointer_path,
        "failed_manifest": failed_manifest_path,
        "failed_deployment": failed_deployment_path,
        "recovery_prestart": recovery_prestart_path,
        "recovery_restart": recovery_restart_path,
        "accepted_wheel": accepted_wheel_path,
        "external_before": external_before_path,
        "external_after": external_after_path,
        "model_snapshot": model_snapshot_path,
        "service_snapshot": service_snapshot_path,
        "openclaw_snapshot": openclaw_snapshot_path,
        "rollback_state": rollback_state_path,
        "published_manifest": published_manifest_path,
        "published_deployment": published_deployment_path,
        "published_installed": published_installed_path,
        "published_health": published_health_path,
        "published_finalization": published_finalization_path,
        "published_publication": published_publication_path,
        "rollback_initial": rollback_initial_path,
        "published_runtime_pointer": published_runtime_pointer_path,
        "published_previous_runtime_pointer": published_previous_runtime_pointer_path,
    })
    return {**body, "content_hash": _canonical(body)}


def validate_recovery_proof(proof: Mapping[str, Any], *,
                            artifacts: Mapping[str, Path]) -> dict[str, Any]:
    _need(isinstance(artifacts, Mapping) and set(artifacts) == ARTIFACT_KEYS,
          "recovery artifact inventory differs")
    expected = build_recovery_proof(**{
        f"{name}_path": path for name, path in artifacts.items()})
    _need(isinstance(proof, Mapping) and dict(proof) == expected,
          "predecessor recovery proof differs")
    return expected


def verify_installed_predecessor(proof: Mapping[str, Any],
                                 installed: Mapping[str, Any]) -> dict[str, Any]:
    _need(isinstance(proof, Mapping) and proof.get("schema_version")
          == "successor-predecessor-recovery-0.1"
          and proof.get("status") == "failed_install_recovered_unpublished",
          "predecessor recovery proof is invalid")
    expected = proof.get("recovery", {}).get("installed_identity")
    _need(isinstance(expected, Mapping) and isinstance(installed, Mapping)
          and dict(installed) == dict(expected),
          "installed predecessor no longer matches recovered identity")
    return dict(expected)
