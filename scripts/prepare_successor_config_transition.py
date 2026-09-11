#!/usr/bin/env python3
"""Prepare and apply one closed successor-release configuration transition.

Preparation is read-only.  It proves that two new directed-document model
configs and one model-spec repair delta preserve the current model inventory,
and that a separate document-research activation config is runnable.  The
read-only audit config is supporting evidence only and is never installable.

``--apply`` is intended for an already controlled stopped window.  It performs
only the exact transition recorded by the prepared manifest and writes an
exclusive receipt.  It does not stop/start services, install code, call a
model, publish a release, or modify any other state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from dalton_core.annual_report_runtime import load_annual_report_model_config
from dalton_core.document_research_inventory import validate_inventory_config


SCHEMA_VERSION = "successor-config-transition-0.1"
RECEIPT_SCHEMA_VERSION = "successor-config-transition-receipt-0.1"
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
MODEL_ADDITIONS = (
    "mission-document-draft-model-config.json",
    "mission-document-verifier-model-config.json",
)
MODEL_REPLACEMENT = "initial-screen-model-config.json"
DOCUMENT_CONFIG = "document-research-config.json"
LANE_CONFIG = "mission-document-research-lane.json"


class ConfigTransitionError(RuntimeError):
    pass


def _need(ok: Any, reason: str) -> None:
    if not ok:
        raise ConfigTransitionError(reason)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256_bytes(
        (json.dumps(value, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")) + "\n").encode("utf-8")
    )


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    _need(path.is_file() and not path.is_symlink(), f"{label} is not a regular file")
    data = path.read_bytes()
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigTransitionError(f"{label} is invalid JSON") from exc
    _need(isinstance(value, dict), f"{label} is not an object")
    return value, data


def _artifact(path: Path, packet_root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    _need(resolved.is_relative_to(packet_root), "transition artifact escapes packet")
    return {"file": resolved.relative_to(packet_root).as_posix(),
            "sha256": sha256_bytes(path.read_bytes())}


def incomplete_template() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "inputs_incomplete",
        "release_ref": None,
        "source_commit": None,
        "acceptance": {
            "state": "pending",
            "full_suite_receipt_sha256": None,
            "wheel_sha256": None,
            "copied_state_rehearsal_binding_sha256": None,
            "health_acceptance_required": True,
        },
        "model_inventory": {
            "before_count": None, "after_count": None,
            "before_semantic_sha256": None, "after_semantic_sha256": None,
        },
        "targets": [],
        "boundaries": {
            "live_mutation": False, "manifest_publication": False,
            "service_lifecycle": False, "model_calls": False,
        },
    }


def build_transition(
    *, packet_root: Path, release_ref: str, source_commit: str,
    baseline_models_path: Path, draft_path: Path, verifier_path: Path,
    initial_before_path: Path, initial_after_path: Path,
    document_activation_path: Path, document_audit_path: Path,
    lane_activation_path: Path,
) -> dict[str, Any]:
    """Return one inert exact transition; no target files are changed."""

    packet_root = packet_root.resolve()
    _need(packet_root.is_dir() and not packet_root.is_symlink(), "packet root is unavailable")
    _need(isinstance(release_ref, str) and release_ref.strip(), "release ref is unresolved")
    _need(HEX40.fullmatch(source_commit) is not None, "source commit is unresolved")
    baseline, _ = _read_json(baseline_models_path, "baseline model snapshot")
    _need(baseline and all(isinstance(name, str) and name.endswith("-model-config.json")
                           and isinstance(value, Mapping)
                           for name, value in baseline.items()),
          "baseline model inventory is invalid")
    _need(all(name not in baseline for name in MODEL_ADDITIONS),
          "directed-document model config already exists in baseline")
    _need(MODEL_REPLACEMENT in baseline, "initial-screen baseline is unavailable")

    paths = {MODEL_ADDITIONS[0]: draft_path, MODEL_ADDITIONS[1]: verifier_path,
             MODEL_REPLACEMENT: initial_after_path, DOCUMENT_CONFIG: document_activation_path,
             LANE_CONFIG: lane_activation_path}
    parsed: dict[str, dict[str, Any]] = {}
    for name in (*MODEL_ADDITIONS, MODEL_REPLACEMENT):
        _value, _ = _read_json(paths[name], name)
        try:
            parsed[name] = load_annual_report_model_config(paths[name], name)
        except Exception as exc:
            raise ConfigTransitionError(f"{name} is not a valid model config") from exc

    initial_before, _ = _read_json(initial_before_path, "initial-screen before")
    _need(initial_before == baseline[MODEL_REPLACEMENT],
          "initial-screen before differs from baseline snapshot")
    expected_after = json.loads(json.dumps(initial_before))
    expected_after["structured_output_repair"] = {"max_attempts": 1}
    _need(parsed[MODEL_REPLACEMENT] == expected_after,
          "initial-screen candidate changes more than structured_output_repair.max_attempts=1")

    document_config, _ = _read_json(document_activation_path, "document research activation")
    try:
        document_config = validate_inventory_config(document_config)
    except Exception as exc:
        raise ConfigTransitionError("document research activation config is invalid") from exc
    _need(document_config["policy"]["policy_ref"] != "document-research-policy:readonly-audit",
          "read-only audit policy cannot be installed as activation authority")
    audit_config, _ = _read_json(document_audit_path, "document research audit evidence")
    try:
        audit_config = validate_inventory_config(audit_config)
    except Exception as exc:
        raise ConfigTransitionError("document research audit evidence is invalid") from exc
    _need(audit_config["policy"]["policy_ref"] == "document-research-policy:readonly-audit",
          "audit evidence does not carry the read-only audit policy")
    lane_config, _ = _read_json(lane_activation_path, "mission document lane activation")
    _need(lane_config == {"schema_version": "0.1", "enabled": True},
          "mission document lane activation has an invalid closed shape")

    final_models = json.loads(json.dumps(baseline))
    for name in MODEL_ADDITIONS:
        final_models[name] = parsed[name]
    final_models[MODEL_REPLACEMENT] = parsed[MODEL_REPLACEMENT]
    targets = []
    for name in MODEL_ADDITIONS:
        targets.append({"name": name, "kind": "exclusive_add",
                        "before_sha256": None,
                        "after": _artifact(paths[name], packet_root)})
    targets.append({"name": MODEL_REPLACEMENT, "kind": "compare_and_replace",
                    "before": _artifact(initial_before_path, packet_root),
                    "after": _artifact(initial_after_path, packet_root)})
    targets.append({"name": DOCUMENT_CONFIG, "kind": "exclusive_add",
                    "before_sha256": None,
                    "after": _artifact(document_activation_path, packet_root)})
    targets.append({"name": LANE_CONFIG, "kind": "exclusive_add",
                    "before_sha256": None,
                    "after": _artifact(lane_activation_path, packet_root)})
    body = {
        "schema_version": SCHEMA_VERSION, "status": "prepared_inert",
        "release_ref": release_ref, "source_commit": source_commit,
        "acceptance": {
            "state": "pending", "full_suite_receipt_sha256": None,
            "wheel_sha256": None,
            "copied_state_rehearsal_binding_sha256": None,
            "health_acceptance_required": True,
        },
        "model_inventory": {
            "before_count": len(baseline), "after_count": len(final_models),
            "before_semantic_sha256": canonical_hash(baseline),
            "after_semantic_sha256": canonical_hash(final_models),
        },
        "targets": targets,
        "supporting_evidence": {
            "baseline_model_snapshot": _artifact(baseline_models_path, packet_root),
            "document_research_readonly_audit": _artifact(document_audit_path, packet_root),
        },
        "preserved_authorities": [
            "all_other_model_configs", "cockpit_model_selection", "active_mission",
            "router_policies", "budget_policies", "openclaw_config",
            "host_external_configuration", "disabled_thesis_impact", "backup_keep_latest_3",
        ],
        "boundaries": {"live_mutation": False, "manifest_publication": False,
                       "service_lifecycle": False, "model_calls": False},
    }
    body["content_hash"] = canonical_hash(body)
    return body


def _resolve_artifact(packet_root: Path, row: Mapping[str, Any]) -> tuple[Path, bytes]:
    _need(set(row) == {"file", "sha256"}, "artifact shape differs")
    rel = Path(row.get("file", ""))
    _need(not rel.is_absolute() and ".." not in rel.parts, "artifact path escapes packet")
    path = packet_root / rel
    _need(path.is_file() and not path.is_symlink(), "transition artifact is unavailable")
    data = path.read_bytes()
    _need(HEX64.fullmatch(str(row.get("sha256", ""))) is not None
          and sha256_bytes(data) == row["sha256"], "transition artifact hash changed")
    return path, data


def expected_transition_state(
    *, packet_root: Path, manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Rebuild the exact post-transition model and document config snapshots."""

    supporting = manifest.get("supporting_evidence", {})
    _need(isinstance(supporting, Mapping)
          and "baseline_model_snapshot" in supporting,
          "baseline model snapshot authority is absent")
    _, baseline_bytes = _resolve_artifact(
        packet_root, supporting["baseline_model_snapshot"])
    try:
        models = json.loads(baseline_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigTransitionError("baseline model snapshot is invalid") from exc
    _need(isinstance(models, dict), "baseline model snapshot is invalid")
    document = None
    lane = None
    for row in manifest.get("targets", []):
        if not isinstance(row, Mapping) or "after" not in row:
            raise ConfigTransitionError("transition target is invalid")
        _, data = _resolve_artifact(packet_root, row["after"])
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigTransitionError("transition target is invalid JSON") from exc
        if row.get("name", "").endswith("-model-config.json"):
            models[row["name"]] = value
        elif row.get("name") == DOCUMENT_CONFIG:
            document = value
        elif row.get("name") == LANE_CONFIG:
            lane = value
    _need(isinstance(document, dict), "document research target is absent")
    _need(lane == {"schema_version": "0.1", "enabled": True},
          "mission document lane target is absent or invalid")
    _need(len(models) == manifest["model_inventory"]["after_count"]
          and canonical_hash(models)
          == manifest["model_inventory"]["after_semantic_sha256"],
          "transition final model inventory authority differs")
    return models, document, lane


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def apply_transition(
    *, packet_root: Path, state_dir: Path, manifest_path: Path,
    expected_manifest_sha256: str, receipt_path: Path,
    fault_hook: Callable[[str], None] | None = None,
    _require_accepted: bool = True,
    accepted_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply only manifest-owned bytes; caller owns the stopped window."""

    _need(HEX64.fullmatch(expected_manifest_sha256) is not None,
          "expected transition manifest hash is invalid")
    _need(state_dir.is_dir() and not state_dir.is_symlink(), "state directory is unavailable")
    _need(not receipt_path.exists() and not receipt_path.is_symlink(), "receipt already exists")
    manifest, manifest_bytes = _read_json(manifest_path, "transition manifest")
    _need(sha256_bytes(manifest_bytes) == expected_manifest_sha256,
          "transition manifest bytes changed")
    asserted = dict(manifest).pop("content_hash", None)
    _need(asserted == canonical_hash({k: v for k, v in manifest.items() if k != "content_hash"})
          and manifest.get("schema_version") == SCHEMA_VERSION
          and manifest.get("status") == "prepared_inert",
          "transition manifest authority differs")
    acceptance = manifest.get("acceptance", {})
    if _require_accepted:
        _need(acceptance.get("state") == "pending"
              and accepted_evidence is not None
              and accepted_evidence.get("state") == "accepted"
              and accepted_evidence.get("health_acceptance_required") is True
              and all(HEX64.fullmatch(str(accepted_evidence.get(name, ""))) is not None
                      for name in ("full_suite_receipt_sha256", "wheel_sha256",
                                   "copied_state_rehearsal_binding_sha256")),
              "accepted full-suite, wheel, rehearsal and health gates are required")
    else:
        _need(acceptance.get("state") == "pending"
              and acceptance.get("health_acceptance_required") is True,
              "scratch rehearsal requires the inert pending transition")

    rows = manifest.get("targets")
    _need(isinstance(rows, list) and len(rows) == 5
          and {row.get("name") for row in rows if isinstance(row, Mapping)}
          == {*MODEL_ADDITIONS, MODEL_REPLACEMENT, DOCUMENT_CONFIG, LANE_CONFIG},
          "transition must contain each of the five targets exactly once")
    expected_kinds = {name: "exclusive_add" for name in MODEL_ADDITIONS}
    expected_kinds.update({MODEL_REPLACEMENT: "compare_and_replace",
                           DOCUMENT_CONFIG: "exclusive_add",
                           LANE_CONFIG: "exclusive_add"})

    supporting = manifest.get("supporting_evidence")
    _need(isinstance(supporting, Mapping)
          and set(supporting) == {
              "baseline_model_snapshot", "document_research_readonly_audit"},
          "transition supporting evidence differs")
    _, baseline_bytes = _resolve_artifact(
        packet_root, supporting["baseline_model_snapshot"])
    try:
        baseline_models = json.loads(baseline_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigTransitionError("baseline model snapshot is invalid") from exc
    actual_models = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(state_dir.glob("*-model-config.json"))
        if path.is_file() and not path.is_symlink()
    }
    _need(actual_models == baseline_models
          and len(actual_models) == manifest["model_inventory"]["before_count"]
          and canonical_hash(actual_models)
          == manifest["model_inventory"]["before_semantic_sha256"],
          "full model configuration inventory differs from reviewed baseline")

    prepared = []
    for row in rows:
        _need(isinstance(row, Mapping) and row.get("name") in {
            *MODEL_ADDITIONS, MODEL_REPLACEMENT, DOCUMENT_CONFIG, LANE_CONFIG},
            "transition target is outside the closed inventory")
        _need(row.get("kind") == expected_kinds[row["name"]],
              "transition target kind differs from its closed operation")
        target = state_dir / row["name"]
        _, after = _resolve_artifact(packet_root, row["after"])
        if row.get("kind") == "exclusive_add":
            _need(set(row) == {"name", "kind", "before_sha256", "after"}
                  and row["before_sha256"] is None and not target.exists()
                  and not target.is_symlink(), f"{row['name']} is no longer absent")
            before = None
        elif row.get("kind") == "compare_and_replace":
            _need(set(row) == {"name", "kind", "before", "after"},
                  "replacement target shape differs")
            _, before = _resolve_artifact(packet_root, row["before"])
            _need(target.is_file() and not target.is_symlink() and target.read_bytes() == before,
                  f"{row['name']} differs from reviewed precondition")
        else:
            raise ConfigTransitionError("transition target kind is invalid")
        prepared.append((row, target, before, after))

    changed = []

    def rollback_changed() -> list[str]:
        conflicts = []
        for row, target, before, after, owned_identity in reversed(changed):
            try:
                current_identity = (target.stat().st_dev, target.stat().st_ino)
                if (not target.is_file() or target.is_symlink()
                        or current_identity != owned_identity
                        or target.read_bytes() != after):
                    conflicts.append(row["name"])
                    continue
                if before is None:
                    target.unlink()
                else:
                    fd, temporary_name = tempfile.mkstemp(
                        prefix=".successor-rollback-", dir=state_dir)
                    temporary = Path(temporary_name)
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(before); stream.flush(); os.fsync(stream.fileno())
                        os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
                        os.replace(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
            except OSError:
                conflicts.append(row["name"])
        return conflicts

    try:
        for row, target, before, after in prepared:
            if row["kind"] == "exclusive_add":
                fd, temporary_name = tempfile.mkstemp(
                    prefix=".successor-add-", dir=state_dir)
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(after); stream.flush(); os.fsync(stream.fileno())
                    os.chmod(temporary, 0o600)
                    os.link(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                mode = stat.S_IMODE(target.stat().st_mode)
                fd, temporary_name = tempfile.mkstemp(prefix=".successor-config-", dir=state_dir)
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(after); stream.flush(); os.fsync(stream.fileno())
                    os.chmod(temporary, mode)
                    _need(target.read_bytes() == before,
                          f"{row['name']} changed during compare-and-replace")
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            owned_identity = (target.stat().st_dev, target.stat().st_ino)
            changed.append((row, target, before, after, owned_identity))
            _fsync_directory(state_dir)
            if fault_hook is not None:
                fault_hook(row["name"])
        final_models = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(state_dir.glob("*-model-config.json"))
            if path.is_file() and not path.is_symlink()
        }
        _need(len(final_models) == manifest["model_inventory"]["after_count"]
              and canonical_hash(final_models)
              == manifest["model_inventory"]["after_semantic_sha256"],
              "installed model configuration inventory differs")
        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "status": "configured_controller_start_pending",
            "release_ref": manifest["release_ref"],
            "source_commit": manifest["source_commit"],
            "transition_manifest_sha256": expected_manifest_sha256,
            "acceptance_evidence_hash": (
                None if accepted_evidence is None
                else canonical_hash(dict(accepted_evidence))
            ),
            "targets": [{"name": row["name"], "after_sha256": sha256_bytes(after)}
                        for row, _target, _before, after in prepared],
            "service_lifecycle_mutations": 0, "model_calls": 0,
            "manifest_publication": False,
        }
        receipt["content_hash"] = canonical_hash(receipt)
        data = (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode()
        if fault_hook is not None:
            fault_hook("before_receipt")
        fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
    except Exception as exc:
        conflicts = rollback_changed()
        if conflicts:
            raise ConfigTransitionError(
                "transition failed; restored other owned targets but refused "
                "concurrent target changes: " + ",".join(sorted(conflicts))
            ) from exc
        raise
    return receipt


def apply_transition_to_scratch(
    *, packet_root: Path, scratch_root: Path, state_dir: Path,
    manifest_path: Path, expected_manifest_sha256: str, receipt_path: Path,
) -> dict[str, Any]:
    """Apply a pending transition only under an explicit scratch root."""

    scratch_root = scratch_root.resolve()
    state_dir = state_dir.resolve()
    receipt_path = receipt_path.resolve()
    _need(scratch_root.is_dir() and not scratch_root.is_symlink(),
          "scratch root is unavailable")
    _need(state_dir.is_relative_to(scratch_root)
          and receipt_path.is_relative_to(scratch_root),
          "scratch transition output escapes scratch root")
    receipt = apply_transition(
        packet_root=packet_root, state_dir=state_dir,
        manifest_path=manifest_path,
        expected_manifest_sha256=expected_manifest_sha256,
        receipt_path=receipt_path, _require_accepted=False,
    )
    return {**receipt, "status": "scratch_configuration_applied",
            "live_mutation": False}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--write-template", type=Path)
    args = parser.parse_args(argv)
    if args.write_template is not None:
        _need(not args.write_template.exists(), "template output already exists")
        args.write_template.write_text(json.dumps(incomplete_template(), indent=2) + "\n")
        print(json.dumps({"status": "incomplete_template_written",
                          "path": str(args.write_template)}))
        return 0
    raise ConfigTransitionError(
        "live apply is callable only by the reviewed stopped-window orchestrator"
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigTransitionError as exc:
        raise SystemExit(f"STOP: {exc}") from exc
