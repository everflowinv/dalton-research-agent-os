#!/usr/bin/env python3
"""Prepare and apply one closed successor-release configuration transition.

Preparation is read-only.  It proves that two new directed-document model
configs and one model-spec repair delta preserve the current model inventory,
and that a separate document-research activation config is runnable.  The
read-only audit config is supporting evidence only and is never installable.

Schema 0.1 retains the original five-file activation behavior. Schema 0.2
preserves an already-installed model/document/lane configuration and applies
one separately reviewed planner service-budget CAS. Schema 0.3 preserves those
bytes and the service config while binding one reviewed external OpenClaw
config transition. Schema 0.4 is a code-only successor: all reviewed Dalton
and OpenClaw configuration bytes remain exact. Schema 0.5 additionally proves
the exact core writer-operation append that bootstrap must apply; this module
only predicts those bytes. ``--apply`` is intended for an already controlled
stopped window and writes an exclusive receipt. It does not stop/start
services, install code, call a model, or publish a release.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from dalton_core.annual_report_runtime import load_annual_report_model_config
from dalton_core.call_budget import validate_budget_overrides
from dalton_core.document_research_inventory import validate_inventory_config


SCHEMA_VERSION = "successor-config-transition-0.1"
PRESERVE_SCHEMA_VERSION = "successor-config-transition-0.2"
EXTERNAL_CAS_SCHEMA_VERSION = "successor-config-transition-0.3"
PURE_PRESERVE_SCHEMA_VERSION = "successor-config-transition-0.4"
WRITER_APPEND_SCHEMA_VERSION = "successor-config-transition-0.5"
RECEIPT_SCHEMA_VERSION = "successor-config-transition-receipt-0.1"
PRESERVE_RECEIPT_SCHEMA_VERSION = "successor-config-transition-receipt-0.2"
EXTERNAL_CAS_RECEIPT_SCHEMA_VERSION = "successor-config-transition-receipt-0.3"
PURE_PRESERVE_RECEIPT_SCHEMA_VERSION = "successor-config-transition-receipt-0.4"
WRITER_APPEND_RECEIPT_SCHEMA_VERSION = "successor-config-transition-receipt-0.5"
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
MODEL_ADDITIONS = (
    "mission-document-draft-model-config.json",
    "mission-document-verifier-model-config.json",
)
MODEL_REPLACEMENT = "initial-screen-model-config.json"
DOCUMENT_CONFIG = "document-research-config.json"
LANE_CONFIG = "mission-document-research-lane.json"
PRESERVED_TARGETS = (*MODEL_ADDITIONS, MODEL_REPLACEMENT, DOCUMENT_CONFIG, LANE_CONFIG)
PLANNER_BUDGET_PATH = ("bounded_planner", "config", "planner_call_budget")
OPENCLAW_FRAME_PATH = (
    "plugins", "entries", "dalton-openclaw-model-broker", "config",
    "maxFrameBytes",
)
OPENCLAW_DEFAULT_MAX_FRAME_BYTES = 262_144
OPENCLAW_TARGET_MAX_FRAME_BYTES = 1_048_576
OPENCLAW_JOURNAL_SCHEMA_VERSION = "0.1"
OPENCLAW_JOURNAL_MAX_BYTES = 8_388_608
OPENCLAW_WEB_SEARCH_PLUGIN_ID = "dalton-openclaw-web-search-broker"
OPENCLAW_WEB_SEARCH_SOURCE_RELATIVE = "integrations/openclaw-web-search-broker"
OPENCLAW_MODEL_PLUGIN_ID = "dalton-openclaw-model-broker"
OPENCLAW_MODEL_SOURCE_RELATIVE = "integrations/openclaw-model-broker"
OPENCLAW_WEB_SEARCH_LEGACY_PATH = (
    "/Users/everflow/Projects/dalton-research-agent-os/"
    "integrations/openclaw-web-search-broker"
)


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


def _record_hash(value: Any) -> str:
    return sha256_bytes(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))


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


def _direct_absolute_path(path: Path, label: str) -> Path:
    """Reject aliases so a manifest names the checkout it actually validates."""

    _need(path.is_absolute(), f"{label} must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        _need(not current.is_symlink(), f"{label} contains a symlink")
    return path.resolve()


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


def _validated_service_delta(value: Mapping[str, Any]) -> dict[str, Any]:
    expected_keys = {
        "schema_version", "activation", "target", "expected_before_sha256",
        "json_path", "expected_before", "after", "expected_after_sha256",
        "preservation", "content_hash",
    }
    _need(set(value) == expected_keys
          and value.get("schema_version") == "planner-call-budget-activation-0.1"
          and value.get("activation")
          == "stopped-window CAS; refuse if target bytes or expected absent path drift"
          and value.get("target") == "config/service.json"
          and value.get("json_path") == list(PLANNER_BUDGET_PATH)
          and value.get("expected_before") == {"state": "absent"}
          and value.get("preservation")
          == "all other service configuration, model configuration and routing authority remain unchanged",
          "planner service budget delta has an invalid closed shape")
    after = value.get("after")
    try:
        validated_after = validate_budget_overrides(after)
    except Exception as exc:
        raise ConfigTransitionError("planner service budget delta values are invalid") from exc
    _need(isinstance(after, Mapping)
        and set(after) == {"max_input_tokens", "max_output_tokens",
                           "max_cost_usd", "timeout_seconds"}
        and validated_after == after
        and HEX64.fullmatch(str(value.get("expected_before_sha256", ""))) is not None
        and HEX64.fullmatch(str(value.get("expected_after_sha256", ""))) is not None,
        "planner service budget delta values are invalid")
    unsigned = {key: item for key, item in value.items() if key != "content_hash"}
    _need(value.get("content_hash") == _record_hash(unsigned),
          "planner service budget delta content hash differs")
    return json.loads(json.dumps(value))


def _service_after(before: Mapping[str, Any], delta: Mapping[str, Any]) -> dict[str, Any]:
    after = json.loads(json.dumps(before))
    cursor: dict[str, Any] = after
    for part in PLANNER_BUDGET_PATH[:-1]:
        child = cursor.get(part)
        _need(isinstance(child, dict),
              "planner service budget parent path is unavailable")
        cursor = child
    leaf = PLANNER_BUDGET_PATH[-1]
    _need(leaf not in cursor, "planner service budget path is no longer absent")
    cursor[leaf] = json.loads(json.dumps(delta["after"]))
    return after


def _leaf_state(value: Mapping[str, Any], path: Sequence[str]) -> dict[str, Any]:
    current: Any = value
    for key in path[:-1]:
        _need(isinstance(current, Mapping) and key in current,
              "OpenClaw broker config path is absent")
        current = current[key]
    _need(isinstance(current, Mapping), "OpenClaw broker config parent is invalid")
    key = path[-1]
    if key not in current:
        return {"state": "absent",
                "effective_default": OPENCLAW_DEFAULT_MAX_FRAME_BYTES}
    leaf = current[key]
    _need(isinstance(leaf, int) and not isinstance(leaf, bool),
          "OpenClaw maxFrameBytes baseline is not an integer")
    return {"state": "present", "value": leaf}


def _set_leaf(value: Mapping[str, Any], path: Sequence[str], leaf: int) -> dict[str, Any]:
    result = json.loads(json.dumps(value))
    current: Any = result
    for key in path[:-1]:
        _need(isinstance(current, dict) and key in current,
              "OpenClaw broker config path is absent")
        current = current[key]
    _need(isinstance(current, dict), "OpenClaw broker config parent is invalid")
    current[path[-1]] = leaf
    return result


def _plugin_tree(path: Path) -> dict[str, Any]:
    _need(path.is_dir() and not path.is_symlink(),
          "managed web-search plugin root is unsafe")
    files = []
    for item in sorted(path.rglob("*")):
        _need(not item.is_symlink(), "managed web-search plugin contains a symlink")
        if item.is_dir():
            continue
        _need(item.is_file(), "managed web-search plugin contains an unsafe entry")
        files.append({"path": item.relative_to(path).as_posix(),
                      "sha256": sha256_bytes(item.read_bytes())})
    _need(bool(files), "managed web-search plugin is empty")
    return {"files": files, "tree_sha256": canonical_hash(files)}


def _validate_plugin_tree_manifest(value: Any) -> dict[str, Any]:
    _need(isinstance(value, Mapping) and set(value) == {"files", "tree_sha256"}
          and isinstance(value.get("files"), list) and bool(value["files"]),
          "managed web-search plugin tree authority differs")
    seen = set()
    for row in value["files"]:
        rel = Path(str(row.get("path", ""))) if isinstance(row, Mapping) else Path()
        _need(isinstance(row, Mapping) and set(row) == {"path", "sha256"}
              and not rel.is_absolute() and ".." not in rel.parts
              and rel.as_posix() not in seen
              and HEX64.fullmatch(str(row.get("sha256", ""))) is not None,
              "managed web-search plugin tree row differs")
        seen.add(rel.as_posix())
    rows = [dict(row) for row in value["files"]]
    _need(rows == sorted(rows, key=lambda row: row["path"])
          and value["tree_sha256"] == canonical_hash(rows),
          "managed web-search plugin tree hash differs")
    return {"files": rows, "tree_sha256": value["tree_sha256"]}


def _reviewed_historical_unresolved(path: Path) -> dict[str, Any]:
    _need(path.is_file() and not path.is_symlink()
          and path.stat().st_size <= OPENCLAW_JOURNAL_MAX_BYTES,
          "OpenClaw broker journal is unavailable or too large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigTransitionError("OpenClaw broker journal is invalid JSON") from exc
    _need(isinstance(value, Mapping)
          and set(value) == {"schemaVersion", "records"}
          and value.get("schemaVersion") == OPENCLAW_JOURNAL_SCHEMA_VERSION
          and isinstance(value.get("records"), list),
          "OpenClaw broker journal shape differs")
    pending = []
    allowed = {"createdAtMs", "expiresAtMs", "invocationId",
               "requestHash", "response", "state"}
    for row in value["records"]:
        _need(isinstance(row, Mapping) and set(row) == allowed
              and row.get("state") in {"pending", "completed"}
              and isinstance(row.get("createdAtMs"), int)
              and isinstance(row.get("expiresAtMs"), int)
              and row["expiresAtMs"] >= row["createdAtMs"]
              and isinstance(row.get("invocationId"), str)
              and isinstance(row.get("requestHash"), str),
              "OpenClaw broker journal record differs")
        if row["state"] == "pending":
            _need(row.get("response") is None,
                  "pending OpenClaw broker row has a response")
            pending.append(dict(row))
    return _validate_historical_unresolved({
        "schema_version": "openclaw-broker-historical-unresolved-0.1",
        "journal_schema_version": OPENCLAW_JOURNAL_SCHEMA_VERSION,
        "records": pending,
        "records_sha256": canonical_hash(pending),
        "retry_authorized": False,
        "refund_authorized": False,
    })


def _validate_historical_unresolved(value: Any) -> dict[str, Any]:
    _need(isinstance(value, Mapping)
          and set(value) == {
              "schema_version", "journal_schema_version", "records",
              "records_sha256", "retry_authorized", "refund_authorized",
          }
          and value.get("schema_version")
              == "openclaw-broker-historical-unresolved-0.1"
          and value.get("journal_schema_version")
              == OPENCLAW_JOURNAL_SCHEMA_VERSION
          and isinstance(value.get("records"), list)
          and value.get("retry_authorized") is False
          and value.get("refund_authorized") is False,
          "historical unresolved broker authority differs")
    records = []
    seen = set()
    allowed = {"createdAtMs", "expiresAtMs", "invocationId",
               "requestHash", "response", "state"}
    for raw in value["records"]:
        key = ((raw.get("invocationId"), raw.get("requestHash"))
               if isinstance(raw, Mapping) else None)
        _need(isinstance(raw, Mapping) and set(raw) == allowed
              and raw.get("state") == "pending" and raw.get("response") is None
              and isinstance(raw.get("createdAtMs"), int)
              and not isinstance(raw.get("createdAtMs"), bool)
              and isinstance(raw.get("expiresAtMs"), int)
              and not isinstance(raw.get("expiresAtMs"), bool)
              and raw["expiresAtMs"] >= raw["createdAtMs"]
              and isinstance(raw.get("invocationId"), str)
              and bool(raw["invocationId"])
              and HEX64.fullmatch(str(raw.get("requestHash", ""))) is not None
              and key not in seen,
              "historical unresolved broker record differs")
        seen.add(key); records.append(dict(raw))
    _need(value.get("records_sha256") == canonical_hash(records),
          "historical unresolved broker record hash differs")
    return {
        "schema_version": value["schema_version"],
        "journal_schema_version": value["journal_schema_version"],
        "records": records,
        "records_sha256": value["records_sha256"],
        "retry_authorized": False,
        "refund_authorized": False,
    }


def _validate_model_broker_host_patch(value: Any, *, packet_root: Path,
                                      source_commit: str) -> dict[str, Any] | None:
    if value is None:
        return None
    keys = {"source_commit", "helper_relative_path", "helper_sha256",
            "target_relative_path", "before", "before_sha256", "after",
            "after_sha256", "capability_check"}
    _need(isinstance(value, Mapping) and set(value) == keys
          and value.get("source_commit") == source_commit
          and value.get("capability_check") == "repo_helper_check_no_call",
          "model broker host patch authority differs")
    helper = Path(str(value["helper_relative_path"]))
    target = Path(str(value["target_relative_path"]))
    _need(helper.as_posix()
          == "integrations/openclaw_host_patches/patch_provider_output_control_endpoint.py"
          and not target.is_absolute() and ".." not in target.parts
          and target.parts and target.parts[0] == "dist",
          "model broker host patch path is outside reviewed scope")
    result = dict(value)
    for name in ("before", "after"):
        rel = Path(str(value[name]))
        path = packet_root / rel
        _need(not rel.is_absolute() and ".." not in rel.parts
              and path.is_file() and not path.is_symlink()
              and sha256_bytes(path.read_bytes()) == value[f"{name}_sha256"],
              "model broker host patch artifact differs")
    _need(HEX64.fullmatch(str(value["helper_sha256"])) is not None,
          "model broker host patch helper hash differs")
    return result


def build_model_broker_host_patch_artifact(
    *, packet_root: Path, source_root: Path, source_commit: str,
    target_relative_path: str, before_path: Path, after_path: Path,
) -> dict[str, Any]:
    """Stage the closed metadata for the repo-owned, no-call host guard."""
    helper_rel = Path(
        "integrations/openclaw_host_patches/patch_provider_output_control_endpoint.py")
    helper = source_root / helper_rel
    source_resolved = source_root.resolve(strict=True)
    packet_resolved = packet_root.resolve(strict=True)
    _need(HEX40.fullmatch(source_commit) is not None
          and subprocess.check_output(
              ["git", "-C", str(source_root), "rev-parse", "HEAD"],
              text=True).strip() == source_commit
          and not subprocess.check_output(
              ["git", "-C", str(source_root), "status", "--porcelain",
               "--untracked-files=all"], text=True)
          and helper.is_file() and not helper.is_symlink()
          and helper.resolve(strict=True).is_relative_to(source_resolved)
          and before_path.resolve(strict=True).is_relative_to(packet_resolved)
          and after_path.resolve(strict=True).is_relative_to(packet_resolved),
          "model broker host patch source is not frozen")
    assignments = {}
    for statement in ast.parse(helper.read_text(encoding="utf-8")).body:
        if isinstance(statement, ast.Assign):
            for name in statement.targets:
                if isinstance(name, ast.Name) and name.id in {"ORIGINAL", "PATCHED"}:
                    assignments[name.id] = ast.literal_eval(statement.value)
    original, patched = assignments.get("ORIGINAL"), assignments.get("PATCHED")
    before_text = before_path.read_text(encoding="utf-8")
    _need(isinstance(original, str) and isinstance(patched, str)
          and before_text.count(original) == 1 and before_text.count(patched) == 0
          and after_path.read_text(encoding="utf-8")
              == before_text.replace(original, patched, 1),
          "model broker host patch after artifact is not the exact helper transform")
    row = {
        "source_commit": source_commit,
        "helper_relative_path": helper_rel.as_posix(),
        "helper_sha256": sha256_bytes(helper.read_bytes()),
        "target_relative_path": target_relative_path,
        "before": _artifact(before_path, packet_root)["file"],
        "before_sha256": sha256_bytes(before_path.read_bytes()),
        "after": _artifact(after_path, packet_root)["file"],
        "after_sha256": sha256_bytes(after_path.read_bytes()),
        "capability_check": "repo_helper_check_no_call",
    }
    return _validate_model_broker_host_patch(
        row, packet_root=packet_root, source_commit=source_commit)


def _validated_openclaw_frame_transition(
    *, before_path: Path, after_path: Path, packet_root: Path,
    web_search_plugin: Mapping[str, Any] | None = None,
    model_broker_plugin: Mapping[str, Any] | None = None,
    model_broker_host_patch: Mapping[str, Any] | None = None,
    historical_unresolved: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    packet_root = packet_root.resolve()
    before, before_bytes = _read_json(before_path, "OpenClaw config before")
    after, after_bytes = _read_json(after_path, "OpenClaw config after")
    before_state = _leaf_state(before, OPENCLAW_FRAME_PATH)
    _need(before_state in (
        {"state": "absent", "effective_default": OPENCLAW_DEFAULT_MAX_FRAME_BYTES},
        {"state": "present", "value": OPENCLAW_DEFAULT_MAX_FRAME_BYTES},
        {"state": "present", "value": OPENCLAW_TARGET_MAX_FRAME_BYTES},
    ), "OpenClaw maxFrameBytes baseline differs from reviewed default")
    already_target = before_state == {
        "state": "present", "value": OPENCLAW_TARGET_MAX_FRAME_BYTES}
    expected = (json.loads(json.dumps(before)) if already_target else
                _set_leaf(before, OPENCLAW_FRAME_PATH,
                          OPENCLAW_TARGET_MAX_FRAME_BYTES))
    semantic_mutations = ([] if already_target else [{
        "kind": "json_leaf_compare_and_patch", "json_path": list(OPENCLAW_FRAME_PATH),
        "before_presence": before_state, "after_value": OPENCLAW_TARGET_MAX_FRAME_BYTES,
    }])
    managed_plugins: list[dict[str, Any]] = []
    for plugin_input, expected_id, expected_source, default_before in (
        (model_broker_plugin, OPENCLAW_MODEL_PLUGIN_ID,
         OPENCLAW_MODEL_SOURCE_RELATIVE, None),
        (web_search_plugin, OPENCLAW_WEB_SEARCH_PLUGIN_ID,
         OPENCLAW_WEB_SEARCH_SOURCE_RELATIVE, OPENCLAW_WEB_SEARCH_LEGACY_PATH),
    ):
        if plugin_input is None:
            continue
        plugin = dict(plugin_input)
        expected_keys = {
            "plugin_id", "source_relative_path", "destination",
            "source_tree", "source_commit",
        }
        if expected_id == OPENCLAW_MODEL_PLUGIN_ID:
            expected_keys.add("replaces")
        _need(set(plugin) == expected_keys
          and plugin.get("plugin_id") == expected_id
          and plugin.get("source_relative_path") == expected_source
          and HEX40.fullmatch(str(plugin.get("source_commit", ""))) is not None,
          "managed plugin authority differs")
        plugin["source_tree"] = _validate_plugin_tree_manifest(
            plugin["source_tree"])
        paths = expected.get("plugins", {}).get("load", {}).get("paths")
        before_destination = plugin.get("replaces", default_before)
        _need(isinstance(paths, list)
              and isinstance(before_destination, str)
              and paths.count(before_destination) == 1
              and plugin["destination"] not in paths,
              "OpenClaw managed plugin path baseline differs")
        paths[paths.index(before_destination)] = plugin["destination"]
        semantic_mutations.append({
            "kind": "json_array_unique_replace",
            "json_path": ["plugins", "load", "paths"],
            "before_value": before_destination,
            "after_value": plugin["destination"],
        })
        managed_plugins.append(plugin)
    _need(after == expected,
          "OpenClaw candidate changes more than reviewed broker fields")
    _need(_leaf_state(after, OPENCLAW_FRAME_PATH)
          == {"state": "present", "value": OPENCLAW_TARGET_MAX_FRAME_BYTES},
          "OpenClaw candidate maxFrameBytes differs")
    return {
        "name": "openclaw_model_broker_max_frame",
        "kind": "compare_and_patch", "mutation_count": 1,
        "target": "~/.openclaw/openclaw.json",
        "json_path": list(OPENCLAW_FRAME_PATH),
        "before_presence": before_state,
        "after_value": OPENCLAW_TARGET_MAX_FRAME_BYTES,
        "semantic_mutations": semantic_mutations,
        "managed_plugins": managed_plugins,
        "managed_host_patch": _validate_model_broker_host_patch(
            model_broker_host_patch, packet_root=packet_root,
            source_commit=(model_broker_host_patch or {}).get("source_commit", "")),
        "historical_unresolved": _validate_historical_unresolved(
            historical_unresolved),
        "before": _artifact(before_path, packet_root),
        "after": _artifact(after_path, packet_root),
        "before_sha256": sha256_bytes(before_bytes),
        "after_sha256": sha256_bytes(after_bytes),
    }


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def build_preserve_existing_transition(
    *, packet_root: Path, release_ref: str, source_commit: str,
    baseline_models_path: Path,
    model_config_paths: Mapping[str, Path],
    preserved_config_paths: Mapping[str, Path],
    preserved_state_authority_paths: Mapping[str, Path],
    service_config_before_path: Path, service_delta_path: Path | None = None,
    openclaw_config_before_path: Path | None = None,
    openclaw_config_after_path: Path | None = None,
    web_search_plugin_source_path: Path | None = None,
    web_search_plugin_destination_path: Path | None = None,
    model_broker_plugin_source_path: Path | None = None,
    model_broker_plugin_destination_path: Path | None = None,
    model_broker_plugin_before_path: Path | None = None,
    model_broker_host_patch: Mapping[str, Any] | None = None,
    openclaw_broker_journal_path: Path | None = None,
    writer_token_before_path: Path | None = None,
    predecessor_source_root: Path | None = None,
    predecessor_source_commit: str | None = None,
    successor_source_root: Path | None = None,
) -> dict[str, Any]:
    """Build a 0.2 service delta through 0.5 writer-append transition.

    The 0.3 form preserves the service and every reviewed Dalton configuration
    byte. It changes the fixed OpenClaw broker ``maxFrameBytes`` leaf and may
    replace the unique legacy web-search plugin path with one reviewed copy.
    """

    packet_root = packet_root.resolve()
    _need(packet_root.is_dir() and not packet_root.is_symlink(),
          "packet root is unavailable")
    _need(isinstance(release_ref, str) and release_ref.strip(),
          "release ref is unresolved")
    _need(HEX40.fullmatch(source_commit) is not None,
          "source commit is unresolved")
    baseline, _ = _read_json(baseline_models_path, "baseline model snapshot")
    _need(baseline and all(isinstance(name, str)
                           and name.endswith("-model-config.json")
                           and isinstance(value, Mapping)
                           for name, value in baseline.items()),
          "baseline model inventory is invalid")
    _need(set(preserved_config_paths) == set(PRESERVED_TARGETS),
          "preserved target inventory differs from the five reviewed configs")
    _need(set(model_config_paths) == set(baseline),
          "model config artifact inventory differs from the full snapshot")
    model_artifacts = {}
    model_file_hashes = {}
    for name in sorted(baseline):
        path = model_config_paths[name]
        value, data = _read_json(path, f"model config artifact {name}")
        _need(value == baseline[name],
              f"model config artifact differs from snapshot: {name}")
        model_artifacts[name] = _artifact(path, packet_root)
        model_file_hashes[name] = sha256_bytes(data)
    _need(bool(preserved_state_authority_paths),
          "preserved state authority inventory is empty")
    preserved_state_authorities = []
    for relative, path in sorted(preserved_state_authority_paths.items()):
        rel = Path(relative)
        _need(isinstance(relative, str) and relative
              and not rel.is_absolute() and ".." not in rel.parts
              and rel.suffix == ".json" and len(rel.parts) >= 2,
              "preserved state authority path is unsafe")
        value, data = _read_json(path, f"preserved state authority {relative}")
        asserted = value.get("content_hash")
        unsigned = {key: item for key, item in value.items() if key != "content_hash"}
        _need(value.get("status") == "approved"
              and HEX64.fullmatch(str(asserted or "")) is not None
              and asserted == _record_hash(unsigned),
              f"preserved state authority is not approved and canonical: {relative}")
        preserved_state_authorities.append({
            "path": rel.as_posix(), "before": _artifact(path, packet_root),
            "after_sha256": sha256_bytes(data),
            "content_hash": asserted, "status": "approved",
        })

    targets = []
    for name in PRESERVED_TARGETS:
        path = preserved_config_paths[name]
        value, data = _read_json(path, f"preserved config {name}")
        if name.endswith("-model-config.json"):
            _need(path.resolve() == model_config_paths[name].resolve(),
                  f"{name} critical artifact differs from full inventory artifact")
            _need(name in baseline and value == baseline[name],
                  f"{name} differs from the full model snapshot")
            try:
                parsed = load_annual_report_model_config(path, name)
            except Exception as exc:
                raise ConfigTransitionError(
                    f"{name} is not a valid model config") from exc
            _need(parsed == value, f"{name} is not canonical model configuration")
        elif name == DOCUMENT_CONFIG:
            try:
                parsed = validate_inventory_config(value)
            except Exception as exc:
                raise ConfigTransitionError(
                    "preserved document research config is invalid") from exc
            _need(parsed == value, "preserved document research config is not canonical")
        else:
            _need(value == {"schema_version": "0.1", "enabled": True},
                  "preserved mission document lane config has an invalid closed shape")
        artifact = _artifact(path, packet_root)
        targets.append({
            "name": name, "kind": "preserve_existing",
            "before": artifact, "after_sha256": sha256_bytes(data),
        })

    service_before, service_before_bytes = _read_json(
        service_config_before_path, "service config before")
    writer_inputs = (
        writer_token_before_path, predecessor_source_root,
        predecessor_source_commit, successor_source_root,
    )
    _need(all(value is None for value in writer_inputs)
          or all(value is not None for value in writer_inputs),
          "writer operation transition inputs are incomplete")
    writer_append = all(value is not None for value in writer_inputs)
    pure_preserve = (
        service_delta_path is None
        and openclaw_config_before_path is not None
        and openclaw_config_after_path is None
        and web_search_plugin_source_path is None
        and web_search_plugin_destination_path is None
        and model_broker_plugin_source_path is None
        and model_broker_plugin_destination_path is None
        and model_broker_plugin_before_path is None
        and model_broker_host_patch is None
        and openclaw_broker_journal_path is None
    )
    _need(not writer_append or pure_preserve,
          "writer operation append requires a pure-preserve transition")
    external_cas = service_delta_path is None and not pure_preserve
    if pure_preserve:
        openclaw_before, openclaw_before_bytes = _read_json(
            openclaw_config_before_path, "OpenClaw config before")
        _need(bool(openclaw_before), "preserved OpenClaw config is empty")
        schema_version = (
            WRITER_APPEND_SCHEMA_VERSION if writer_append
            else PURE_PRESERVE_SCHEMA_VERSION
        )
        service_transition = {
            "kind": "preserve_exact", "mutation_count": 0,
            "before": _artifact(service_config_before_path, packet_root),
            "after_sha256": sha256_bytes(service_before_bytes),
        }
        openclaw_transition = {
            "kind": "preserve_exact", "mutation_count": 0,
            "before": _artifact(openclaw_config_before_path, packet_root),
            "after_sha256": sha256_bytes(openclaw_before_bytes),
        }
        boundaries = {
            "configuration_mutations": 0, "service_config_mutations": 0,
            "external_config_mutations": 0,
            **({"writer_token_mutations": 1} if writer_append else {}),
            "live_mutation": False, "manifest_publication": False,
            "service_lifecycle": False, "model_calls": False,
        }
    elif external_cas:
        _need(openclaw_config_before_path is not None
              and openclaw_config_after_path is not None
              and openclaw_broker_journal_path is not None,
              "0.3 requires exact OpenClaw before and after artifacts")
        plugin_inputs = (web_search_plugin_source_path,
                         web_search_plugin_destination_path)
        _need(all(path is None for path in plugin_inputs)
              or all(path is not None for path in plugin_inputs),
              "managed web-search plugin inputs are incomplete")
        managed_plugin = None
        _need(model_broker_host_patch is None
              or model_broker_host_patch.get("source_commit") == source_commit,
              "model broker host patch source commit differs")
        if web_search_plugin_source_path is not None:
            source = web_search_plugin_source_path.resolve()
            destination = web_search_plugin_destination_path.resolve()
            _need(source.as_posix().endswith(OPENCLAW_WEB_SEARCH_SOURCE_RELATIVE)
                  and destination.is_relative_to(
                      (packet_root / "managed-plugins").resolve())
                  and source_commit in destination.name,
                  "managed web-search plugin paths are outside reviewed scope")
            source_tree = _plugin_tree(source)
            _need(_plugin_tree(destination) == source_tree,
                  "managed web-search plugin copy differs from frozen source")
            managed_plugin = {
                "plugin_id": OPENCLAW_WEB_SEARCH_PLUGIN_ID,
                "source_relative_path": OPENCLAW_WEB_SEARCH_SOURCE_RELATIVE,
                "destination": str(destination),
                "source_tree": source_tree,
                "source_commit": source_commit,
            }
        model_inputs = (model_broker_plugin_source_path,
                        model_broker_plugin_destination_path,
                        model_broker_plugin_before_path)
        _need(all(path is None for path in model_inputs)
              or all(path is not None for path in model_inputs),
              "managed model broker plugin inputs are incomplete")
        managed_model_plugin = None
        if model_broker_plugin_source_path is not None:
            source = model_broker_plugin_source_path.resolve()
            destination = model_broker_plugin_destination_path.resolve()
            before_destination = model_broker_plugin_before_path.resolve()
            _need(source.as_posix().endswith(OPENCLAW_MODEL_SOURCE_RELATIVE)
                  and destination.is_relative_to(
                      (packet_root / "managed-plugins").resolve())
                  and source_commit in destination.name
                  and before_destination != destination,
                  "managed model broker plugin paths are outside reviewed scope")
            source_tree = _plugin_tree(source)
            _need(_plugin_tree(destination) == source_tree,
                  "managed model broker plugin copy differs from frozen source")
            managed_model_plugin = {
                "plugin_id": OPENCLAW_MODEL_PLUGIN_ID,
                "source_relative_path": OPENCLAW_MODEL_SOURCE_RELATIVE,
                "destination": str(destination), "replaces": str(before_destination),
                "source_tree": source_tree, "source_commit": source_commit,
            }
        openclaw_transition = _validated_openclaw_frame_transition(
            before_path=openclaw_config_before_path,
            after_path=openclaw_config_after_path,
            packet_root=packet_root,
            web_search_plugin=managed_plugin,
            model_broker_plugin=managed_model_plugin,
            model_broker_host_patch=model_broker_host_patch,
            historical_unresolved=_reviewed_historical_unresolved(
                openclaw_broker_journal_path),
        )
        schema_version = EXTERNAL_CAS_SCHEMA_VERSION
        service_transition = {
            "kind": "preserve_exact", "mutation_count": 0,
            "before": _artifact(service_config_before_path, packet_root),
            "after_sha256": sha256_bytes(service_before_bytes),
        }
        boundaries = {
            "configuration_mutations": 0, "service_config_mutations": 0,
            "external_config_mutations": 1,
            "live_mutation": False, "manifest_publication": False,
            "service_lifecycle": False, "model_calls": False,
        }
    else:
        _need(openclaw_config_before_path is None
              and openclaw_config_after_path is None
              and web_search_plugin_source_path is None
              and web_search_plugin_destination_path is None
              and model_broker_plugin_source_path is None
              and model_broker_plugin_destination_path is None
              and model_broker_plugin_before_path is None
              and model_broker_host_patch is None
              and openclaw_broker_journal_path is None,
              "0.2 cannot carry an external config transition")
        delta_value, _ = _read_json(
            service_delta_path, "planner service budget delta")
        delta = _validated_service_delta(delta_value)
        _need(delta["expected_before_sha256"] == sha256_bytes(service_before_bytes),
              "planner service budget delta does not bind the service baseline")
        service_after = _service_after(service_before, delta)
        _need(sha256_bytes(_json_bytes(service_after)) == delta["expected_after_sha256"],
              "planner service budget expected-after hash differs")
        schema_version = PRESERVE_SCHEMA_VERSION
        service_transition = {
            "kind": "compare_and_patch", "mutation_count": 1,
            "before": _artifact(service_config_before_path, packet_root),
            "delta": _artifact(service_delta_path, packet_root),
            "after_sha256": delta["expected_after_sha256"],
        }
        openclaw_transition = None
        boundaries = {
            "configuration_mutations": 0, "service_config_mutations": 1,
            "live_mutation": False, "manifest_publication": False,
            "service_lifecycle": False, "model_calls": False,
        }

    body = {
        "schema_version": schema_version,
        "transition_kind": "preserve_existing",
        "status": "prepared_inert",
        "release_ref": release_ref,
        "source_commit": source_commit,
        "acceptance": {
            "state": "pending", "full_suite_receipt_sha256": None,
            "wheel_sha256": None,
            "copied_state_rehearsal_binding_sha256": None,
            "health_acceptance_required": True,
        },
        "model_inventory": {
            "before_count": len(baseline), "after_count": len(baseline),
            "before_semantic_sha256": canonical_hash(baseline),
            "after_semantic_sha256": canonical_hash(baseline),
            "file_sha256": model_file_hashes,
        },
        "targets": targets,
        "preserved_state_authorities": preserved_state_authorities,
        "service_transition": service_transition,
        "supporting_evidence": {
            "baseline_model_snapshot": _artifact(baseline_models_path, packet_root),
            "model_config_files": model_artifacts,
        },
        "preserved_authorities": [
            "all_model_configs", "five_reviewed_runtime_configs",
            "cockpit_model_selection", "active_mission", "router_policies",
            ("openclaw_config_outside_reviewed_deltas" if external_cas
             else "openclaw_config"),
            "host_external_configuration",
            "connector_governance", "owner_metadata", "credentials",
            "signatures", "disabled_thesis_impact", "backup_keep_latest_3",
        ],
        "boundaries": boundaries,
    }
    if schema_version in {PURE_PRESERVE_SCHEMA_VERSION, WRITER_APPEND_SCHEMA_VERSION}:
        body["external_config_transition"] = openclaw_transition
    elif openclaw_transition is not None:
        body["external_config_transitions"] = [openclaw_transition]
    if schema_version == WRITER_APPEND_SCHEMA_VERSION:
        from scripts.successor_writer_token_transition import (
            build_transition as build_writer_transition,
        )
        assert writer_token_before_path is not None
        assert predecessor_source_root is not None
        assert predecessor_source_commit is not None
        assert successor_source_root is not None
        writer_token_before_path = _direct_absolute_path(
            writer_token_before_path, "writer token before path")
        _need(writer_token_before_path.is_relative_to(packet_root),
              "writer token before path escapes packet")
        predecessor_source_root = _direct_absolute_path(
            predecessor_source_root, "predecessor source root")
        successor_source_root = _direct_absolute_path(
            successor_source_root, "successor source root")
        proof = build_writer_transition(
            before_path=writer_token_before_path,
            predecessor_root=predecessor_source_root,
            predecessor_commit=predecessor_source_commit,
            successor_root=successor_source_root,
            successor_commit=source_commit,
        )
        body["writer_operation_transition"] = {
            "before": _artifact(writer_token_before_path, packet_root),
            "predecessor_source_root": str(predecessor_source_root),
            "proof": proof,
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
    version = manifest.get("schema_version")
    _need(version in {SCHEMA_VERSION, PRESERVE_SCHEMA_VERSION,
                      EXTERNAL_CAS_SCHEMA_VERSION, PURE_PRESERVE_SCHEMA_VERSION,
                      WRITER_APPEND_SCHEMA_VERSION},
          "transition schema version is unsupported")
    document = None
    lane = None
    for row in manifest.get("targets", []):
        artifact = (row.get("before") if isinstance(row, Mapping)
                    and version in {PRESERVE_SCHEMA_VERSION,
                                    EXTERNAL_CAS_SCHEMA_VERSION,
                                    PURE_PRESERVE_SCHEMA_VERSION,
                                    WRITER_APPEND_SCHEMA_VERSION} else row.get("after")
                    if isinstance(row, Mapping) else None)
        if not isinstance(row, Mapping) or not isinstance(artifact, Mapping):
            raise ConfigTransitionError("transition target is invalid")
        _, data = _resolve_artifact(packet_root, artifact)
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigTransitionError("transition target is invalid JSON") from exc
        if row.get("name", "").endswith("-model-config.json") and version == SCHEMA_VERSION:
            models[row["name"]] = value
        elif row.get("name", "").endswith("-model-config.json"):
            _need(row["name"] in models and models[row["name"]] == value,
                  "preserved model target differs from full inventory")
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


def expected_service_transition_state(
    *, packet_root: Path, manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the exact semantic service before/after state for 0.2 or 0.3."""

    version = manifest.get("schema_version")
    _need(version in {PRESERVE_SCHEMA_VERSION, EXTERNAL_CAS_SCHEMA_VERSION,
                      PURE_PRESERVE_SCHEMA_VERSION, WRITER_APPEND_SCHEMA_VERSION}
          and manifest.get("transition_kind") == "preserve_existing",
          "service transition requires preserve-existing schema 0.2 through 0.5")
    row = manifest.get("service_transition")
    if version in {EXTERNAL_CAS_SCHEMA_VERSION, PURE_PRESERVE_SCHEMA_VERSION,
                   WRITER_APPEND_SCHEMA_VERSION}:
        _need(isinstance(row, Mapping)
              and set(row) == {"kind", "mutation_count", "before", "after_sha256"}
              and row.get("kind") == "preserve_exact"
              and row.get("mutation_count") == 0,
              "preserved service transition shape differs")
        _, before_bytes = _resolve_artifact(packet_root, row["before"])
        try:
            before = json.loads(before_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigTransitionError(
                "preserved service transition artifact is invalid JSON") from exc
        _need(isinstance(before, dict)
              and row["after_sha256"] == sha256_bytes(before_bytes),
              "preserved service transition hash differs")
        return before, json.loads(json.dumps(before))
    _need(isinstance(row, Mapping)
          and set(row) == {"kind", "mutation_count", "before", "delta", "after_sha256"}
          and row.get("kind") == "compare_and_patch"
          and row.get("mutation_count") == 1,
          "service transition shape differs")
    _, before_bytes = _resolve_artifact(packet_root, row["before"])
    _, delta_bytes = _resolve_artifact(packet_root, row["delta"])
    try:
        before = json.loads(before_bytes.decode("utf-8"))
        delta_value = json.loads(delta_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigTransitionError("service transition artifact is invalid JSON") from exc
    _need(isinstance(before, dict) and isinstance(delta_value, dict),
          "service transition artifact is invalid")
    delta = _validated_service_delta(delta_value)
    after = _service_after(before, delta)
    _need(delta["expected_before_sha256"] == sha256_bytes(before_bytes)
          and row["after_sha256"] == delta["expected_after_sha256"]
          and sha256_bytes(_json_bytes(after)) == row["after_sha256"],
          "service transition hashes differ")
    return before, after


def expected_openclaw_frame_transition_state(
    *, packet_root: Path, manifest: Mapping[str, Any],
) -> tuple[bytes, bytes, dict[str, Any]]:
    """Return exact OpenClaw before/after bytes and the closed 0.3 row."""

    _need(manifest.get("schema_version") == EXTERNAL_CAS_SCHEMA_VERSION,
          "OpenClaw frame transition requires schema 0.3")
    rows = manifest.get("external_config_transitions")
    _need(isinstance(rows, list) and len(rows) == 1
          and isinstance(rows[0], Mapping),
          "external config transition inventory differs")
    row = dict(rows[0])
    expected_keys = {
        "name", "kind", "mutation_count", "target", "json_path",
        "before_presence", "after_value", "before", "after",
        "before_sha256", "after_sha256", "semantic_mutations",
        "managed_plugins", "historical_unresolved",
    }
    _need(frozenset(row) in {frozenset(expected_keys),
                             frozenset(expected_keys | {"managed_host_patch"})}
          and row.get("name") == "openclaw_model_broker_max_frame"
          and row.get("kind") == "compare_and_patch"
          and row.get("mutation_count") == 1
          and row.get("target") == "~/.openclaw/openclaw.json"
          and row.get("json_path") == list(OPENCLAW_FRAME_PATH)
          and row.get("after_value") == OPENCLAW_TARGET_MAX_FRAME_BYTES,
          "OpenClaw frame transition shape differs")
    before_path, before_bytes = _resolve_artifact(packet_root, row["before"])
    after_path, after_bytes = _resolve_artifact(packet_root, row["after"])
    managed_plugins = row.get("managed_plugins")
    _need(isinstance(managed_plugins, list) and len(managed_plugins) <= 2
          and len({row.get("plugin_id") for row in managed_plugins
                   if isinstance(row, Mapping)}) == len(managed_plugins),
          "managed plugin transition inventory differs")
    for plugin in managed_plugins:
        destination = Path(str(plugin.get("destination", "")))
        _need(destination.is_absolute()
              and destination.is_relative_to(
                  (packet_root / "managed-plugins").resolve())
              and plugin.get("source_commit") == manifest.get("source_commit")
              and manifest["source_commit"] in destination.name,
              "managed plugin destination authority differs")
    unresolved = _validate_historical_unresolved(
        row.get("historical_unresolved"))
    _need(row.get("managed_host_patch") is None
          or row["managed_host_patch"].get("source_commit")
          == manifest.get("source_commit"),
          "model broker host patch source commit differs")
    validated = _validated_openclaw_frame_transition(
        before_path=before_path, after_path=after_path, packet_root=packet_root,
        web_search_plugin=next((p for p in managed_plugins if p.get("plugin_id")
                                == OPENCLAW_WEB_SEARCH_PLUGIN_ID), None),
        model_broker_plugin=next((p for p in managed_plugins if p.get("plugin_id")
                                  == OPENCLAW_MODEL_PLUGIN_ID), None),
        model_broker_host_patch=row.get("managed_host_patch"),
        historical_unresolved=unresolved)
    if "managed_host_patch" not in row:
        validated.pop("managed_host_patch")
    _need(row == validated
          and row["before_sha256"] == sha256_bytes(before_bytes)
          and row["after_sha256"] == sha256_bytes(after_bytes),
          "OpenClaw frame transition authority differs")
    return before_bytes, after_bytes, row


def expected_preserved_openclaw_state(
    *, packet_root: Path, manifest: Mapping[str, Any],
) -> bytes:
    """Return exact OpenClaw bytes bound by a pure-preserve successor."""

    _need(manifest.get("schema_version") in {
              PURE_PRESERVE_SCHEMA_VERSION, WRITER_APPEND_SCHEMA_VERSION},
          "pure OpenClaw preservation requires schema 0.4 or 0.5")
    row = manifest.get("external_config_transition")
    _need(isinstance(row, Mapping)
          and set(row) == {"kind", "mutation_count", "before", "after_sha256"}
          and row.get("kind") == "preserve_exact"
          and row.get("mutation_count") == 0,
          "pure OpenClaw transition shape differs")
    _, before = _resolve_artifact(packet_root, row["before"])
    _need(row.get("after_sha256") == sha256_bytes(before),
          "pure OpenClaw transition hash differs")
    return before


def expected_writer_operation_transition_state(
    *, packet_root: Path, manifest: Mapping[str, Any],
    successor_root: Path,
    predecessor_source_root: Path | None = None,
) -> tuple[bytes, bytes, dict[str, Any]]:
    """Return exact writer bytes for the bootstrap executor; mutate nothing."""

    _need(manifest.get("schema_version") == WRITER_APPEND_SCHEMA_VERSION,
          "writer operation append requires schema 0.5")
    row = manifest.get("writer_operation_transition")
    _need(isinstance(row, Mapping)
          and set(row) == {"before", "predecessor_source_root", "proof"}
          and isinstance(row.get("proof"), Mapping),
          "writer operation transition shape differs")
    before_artifact = row["before"]
    _need(isinstance(before_artifact, Mapping)
          and set(before_artifact) == {"file", "sha256"},
          "writer token before artifact shape differs")
    before_rel = Path(str(before_artifact.get("file", "")))
    _need(not before_rel.is_absolute() and ".." not in before_rel.parts,
          "writer token before artifact escapes packet")
    _direct_absolute_path(
        packet_root.resolve() / before_rel, "writer token before artifact")
    before_path, before_bytes = _resolve_artifact(packet_root, row["before"])
    _need(before_path.read_bytes() == before_bytes,
          "writer token before artifact differs")
    proof = dict(row["proof"])
    predecessor = _direct_absolute_path(
        Path(str(row.get("predecessor_source_root", ""))),
        "writer predecessor source root",
    )
    if predecessor_source_root is not None:
        supplied_predecessor = _direct_absolute_path(
            predecessor_source_root, "supplied writer predecessor source root")
        _need(supplied_predecessor == predecessor,
              "writer predecessor source root differs")
    successor_root = _direct_absolute_path(
        successor_root, "writer successor source root")
    _need(proof.get("successor", {}).get("commit") == manifest.get("source_commit"),
          "writer successor source identity differs")
    from scripts.successor_writer_token_transition import validate_transition
    try:
        after_bytes = validate_transition(
            proof, before_bytes=before_bytes,
            predecessor_root=predecessor,
            successor_root=successor_root,
        )
    except Exception as exc:
        raise ConfigTransitionError("writer operation transition differs") from exc
    return before_bytes, after_bytes, dict(row)


def verify_preserved_state_authorities(
    *, packet_root: Path, state_dir: Path, manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Verify exact approved authority bytes without mutating the state tree."""

    rows = manifest.get("preserved_state_authorities")
    _need(isinstance(rows, list) and rows,
          "preserved state authority inventory is absent")
    _need(state_dir.is_dir() and not state_dir.is_symlink(),
          "preserved state authority root is unsafe")
    paths = []
    verified = []
    for row in rows:
        _need(isinstance(row, Mapping)
              and set(row) == {"path", "before", "after_sha256",
                               "content_hash", "status"}
              and row.get("status") == "approved",
              "preserved state authority row differs")
        rel = Path(str(row.get("path", "")))
        _need(not rel.is_absolute() and ".." not in rel.parts
              and rel.suffix == ".json" and len(rel.parts) >= 2
              and rel.as_posix() not in paths,
              "preserved state authority path is unsafe or duplicated")
        paths.append(rel.as_posix())
        _, reviewed = _resolve_artifact(packet_root, row["before"])
        target = state_dir / rel
        ancestors = [state_dir.joinpath(*rel.parts[:index])
                     for index in range(1, len(rel.parts))]
        _need(target.is_file() and not target.is_symlink()
              and all(path.is_dir() and not path.is_symlink()
                      for path in ancestors)
              and target.resolve().is_relative_to(state_dir.resolve())
              and target.read_bytes() == reviewed
              and row.get("after_sha256") == sha256_bytes(reviewed),
              f"preserved state authority bytes differ: {rel.as_posix()}")
        try:
            value = json.loads(reviewed.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigTransitionError(
                f"preserved state authority is invalid JSON: {rel.as_posix()}") from exc
        _need(isinstance(value, dict) and value.get("status") == "approved"
              and value.get("content_hash") == row.get("content_hash")
              and value["content_hash"] == _record_hash({
                  key: item for key, item in value.items() if key != "content_hash"}),
              f"preserved state authority content differs: {rel.as_posix()}")
        verified.append({"path": rel.as_posix(),
                         "sha256": row["after_sha256"],
                         "content_hash": row["content_hash"],
                         "status": "approved"})
    return verified


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_exclusive_json(path: Path, value: Mapping[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".successor-receipt-", dir=path.parent)
    temporary = Path(temporary_name)
    published_identity: tuple[int, int] | None = None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes(value)); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.link(temporary, path)
        linked = temporary.lstat()
        published_identity = (linked.st_dev, linked.st_ino)
        _fsync_directory(path.parent)
    except Exception:
        if (published_identity is not None and path.is_file()
                and not path.is_symlink()):
            current = path.lstat()
            if (current.st_dev, current.st_ino) == published_identity:
                path.unlink()
                _fsync_directory(path.parent)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _apply_preserve_transition(
    *, packet_root: Path, state_dir: Path, service_config_path: Path,
    external_config_path: Path | None,
    manifest: Mapping[str, Any], expected_manifest_sha256: str,
    receipt_path: Path, accepted_evidence: Mapping[str, Any] | None,
    fault_hook: Callable[[str], None] | None,
    successor_source_root: Path | None,
) -> dict[str, Any]:
    version = manifest.get("schema_version")
    expected_fields = {
        "schema_version", "transition_kind", "status", "release_ref",
        "source_commit", "acceptance", "model_inventory", "targets",
        "preserved_state_authorities", "service_transition",
        "supporting_evidence", "preserved_authorities",
        "boundaries", "content_hash",
    }
    expected_boundaries = {
        "configuration_mutations": 0,
        "service_config_mutations": 1 if version == PRESERVE_SCHEMA_VERSION else 0,
        **({} if version == PRESERVE_SCHEMA_VERSION
           else {"external_config_mutations": (
               1 if version == EXTERNAL_CAS_SCHEMA_VERSION else 0
           )}),
        "live_mutation": False, "manifest_publication": False,
        "service_lifecycle": False, "model_calls": False,
    }
    if version == EXTERNAL_CAS_SCHEMA_VERSION:
        expected_fields.add("external_config_transitions")
    elif version in {PURE_PRESERVE_SCHEMA_VERSION, WRITER_APPEND_SCHEMA_VERSION}:
        expected_fields.add("external_config_transition")
        if version == WRITER_APPEND_SCHEMA_VERSION:
            expected_fields.add("writer_operation_transition")
            expected_boundaries["writer_token_mutations"] = 1
    _need(version in {PRESERVE_SCHEMA_VERSION, EXTERNAL_CAS_SCHEMA_VERSION,
                      PURE_PRESERVE_SCHEMA_VERSION, WRITER_APPEND_SCHEMA_VERSION}
          and set(manifest) == expected_fields
          and manifest.get("boundaries") == expected_boundaries,
          "preserve-existing transition boundary differs")
    _need(manifest.get("transition_kind") == "preserve_existing",
          "preserve-existing transition kind differs")
    rows = manifest.get("targets")
    _need(isinstance(rows, list) and len(rows) == len(PRESERVED_TARGETS)
          and {row.get("name") for row in rows if isinstance(row, Mapping)}
          == set(PRESERVED_TARGETS),
          "preserve-existing transition must contain the five reviewed configs")
    supporting = manifest.get("supporting_evidence")
    _need(isinstance(supporting, Mapping)
          and set(supporting) == {"baseline_model_snapshot", "model_config_files"}
          and isinstance(supporting.get("model_config_files"), Mapping),
          "preserve-existing supporting evidence differs")
    _, baseline_bytes = _resolve_artifact(
        packet_root, supporting["baseline_model_snapshot"])
    try:
        baseline_models = json.loads(baseline_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigTransitionError("baseline model snapshot is invalid") from exc
    _need(isinstance(baseline_models, dict) and baseline_models,
          "baseline model snapshot is invalid")
    actual_model_paths = sorted(state_dir.glob("*-model-config.json"))
    _need(all(path.is_file() and not path.is_symlink()
              for path in actual_model_paths),
          "model configuration inventory contains an unsafe entry")
    actual_models = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in actual_model_paths
    }
    model_byte_hashes = {path.name: sha256_bytes(path.read_bytes())
                         for path in actual_model_paths}
    inventory = manifest.get("model_inventory")
    reviewed_model_files = supporting["model_config_files"]
    _need(isinstance(inventory, Mapping)
          and set(inventory) == {"before_count", "after_count",
                                 "before_semantic_sha256", "after_semantic_sha256",
                                 "file_sha256"}
          and isinstance(inventory.get("file_sha256"), Mapping)
          and set(reviewed_model_files) == set(baseline_models)
          and dict(inventory["file_sha256"]) == model_byte_hashes
          and inventory.get("before_count") == len(baseline_models)
          and inventory.get("after_count") == len(baseline_models)
          and inventory.get("before_semantic_sha256") == canonical_hash(baseline_models)
          and inventory.get("after_semantic_sha256") == canonical_hash(baseline_models)
          and actual_models == baseline_models,
          "full model configuration inventory differs from preserved baseline")
    for name in sorted(baseline_models):
        _, reviewed_bytes = _resolve_artifact(packet_root, reviewed_model_files[name])
        _need(sha256_bytes(reviewed_bytes) == inventory["file_sha256"][name]
              and json.loads(reviewed_bytes.decode("utf-8")) == baseline_models[name],
              f"reviewed model config artifact differs: {name}")

    for row in rows:
        _need(isinstance(row, Mapping)
              and set(row) == {"name", "kind", "before", "after_sha256"}
              and row.get("kind") == "preserve_existing"
              and row.get("name") in PRESERVED_TARGETS,
              "preserved target shape differs")
        _, before = _resolve_artifact(packet_root, row["before"])
        target = state_dir / row["name"]
        _need(HEX64.fullmatch(str(row.get("after_sha256", ""))) is not None
              and row["after_sha256"] == sha256_bytes(before)
              and target.is_file() and not target.is_symlink()
              and target.read_bytes() == before,
                  f"{row['name']} differs from preserved exact bytes")

    verified_state_authorities = verify_preserved_state_authorities(
        packet_root=packet_root, state_dir=state_dir, manifest=manifest)

    if version in {EXTERNAL_CAS_SCHEMA_VERSION, PURE_PRESERVE_SCHEMA_VERSION,
                   WRITER_APPEND_SCHEMA_VERSION}:
        service_before_value, service_after_value = expected_service_transition_state(
            packet_root=packet_root, manifest=manifest)
        _, service_before_bytes = _resolve_artifact(
            packet_root, manifest["service_transition"]["before"])
        _need(service_before_value == service_after_value
              and service_config_path.is_file()
              and not service_config_path.is_symlink()
              and service_config_path.read_bytes() == service_before_bytes,
              "service config differs from reviewed preserved bytes")
        if version == EXTERNAL_CAS_SCHEMA_VERSION:
            external_before, external_after, external_row = (
                expected_openclaw_frame_transition_state(
                    packet_root=packet_root, manifest=manifest))
        else:
            external_before = expected_preserved_openclaw_state(
                packet_root=packet_root, manifest=manifest)
            external_after = external_before
            external_row = None
        _need(external_config_path is not None
              and external_config_path.is_file()
              and not external_config_path.is_symlink()
              and external_config_path.read_bytes() == external_after,
              "OpenClaw config differs from reviewed applied bytes")
        writer_projection = None
        if version == WRITER_APPEND_SCHEMA_VERSION:
            _need(successor_source_root is not None,
                  "schema 0.5 requires the accepted successor source root")
            writer_before, writer_after, _ = expected_writer_operation_transition_state(
                packet_root=packet_root, manifest=manifest,
                successor_root=successor_source_root,
            )
            writer_projection = {
                "writer_token_mutations": 1,
                "writer_token_before_sha256": sha256_bytes(writer_before),
                "writer_token_predicted_after_sha256": sha256_bytes(writer_after),
                "writer_token_apply_actor": "bootstrap",
                "writer_token_apply_status": "pending",
            }
        receipt = {
            "schema_version": (
                EXTERNAL_CAS_RECEIPT_SCHEMA_VERSION
                if version == EXTERNAL_CAS_SCHEMA_VERSION
                else WRITER_APPEND_RECEIPT_SCHEMA_VERSION
                if version == WRITER_APPEND_SCHEMA_VERSION
                else PURE_PRESERVE_RECEIPT_SCHEMA_VERSION
            ),
            "status": "configured_controller_start_pending",
            "release_ref": manifest["release_ref"],
            "source_commit": manifest["source_commit"],
            "transition_manifest_sha256": expected_manifest_sha256,
            "acceptance_evidence_hash": (
                None if accepted_evidence is None
                else canonical_hash(dict(accepted_evidence))),
            "configuration_mutations": 0,
            "model_config_byte_sha256": model_byte_hashes,
            "preserved_targets": [
                {"name": row["name"], "sha256": row["after_sha256"]}
                for row in rows],
            "preserved_state_authorities": verified_state_authorities,
            "service_config_mutations": 0,
            "service_config_before_sha256": sha256_bytes(service_before_bytes),
            "service_config_after_sha256": sha256_bytes(service_before_bytes),
            "external_config_mutations": (
                1 if version == EXTERNAL_CAS_SCHEMA_VERSION else 0
            ),
            "external_config_before_sha256": sha256_bytes(external_before),
            "external_config_after_sha256": sha256_bytes(external_after),
            "service_lifecycle_mutations": 0, "model_calls": 0,
            "manifest_publication": False,
        }
        if writer_projection is not None:
            receipt.update(writer_projection)
        if external_row is not None:
            receipt.update({
                "historical_unresolved_sha256": external_row[
                    "historical_unresolved"]["records_sha256"],
                "retry_authorized": False, "refund_authorized": False,
            })
        receipt["content_hash"] = canonical_hash(receipt)
        if fault_hook is not None:
            fault_hook("before_receipt")
        _publish_exclusive_json(receipt_path, receipt)
        return receipt

    service_row = manifest.get("service_transition")
    _need(isinstance(service_row, Mapping)
          and set(service_row) == {
              "kind", "mutation_count", "before", "delta", "after_sha256"}
          and service_row.get("kind") == "compare_and_patch"
          and service_row.get("mutation_count") == 1,
          "service transition shape differs")
    _, service_before = _resolve_artifact(packet_root, service_row["before"])
    _, delta_bytes = _resolve_artifact(packet_root, service_row["delta"])
    try:
        service_before_value = json.loads(service_before.decode("utf-8"))
        delta_value = json.loads(delta_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigTransitionError("service transition artifact is invalid JSON") from exc
    _need(isinstance(service_before_value, dict) and isinstance(delta_value, dict),
          "service transition artifact is invalid")
    delta = _validated_service_delta(delta_value)
    _need(not service_config_path.is_symlink(),
          "service config cannot be a symlink")
    service_config_path = service_config_path.resolve()
    _need(service_config_path.is_file()
          and service_config_path.read_bytes() == service_before
          and delta["expected_before_sha256"] == sha256_bytes(service_before)
          and service_row["after_sha256"] == delta["expected_after_sha256"],
          "service config differs from reviewed CAS precondition")
    service_after = _json_bytes(_service_after(service_before_value, delta))
    _need(sha256_bytes(service_after) == service_row["after_sha256"],
          "service transition expected-after bytes differ")

    before_stat = service_config_path.stat()
    before_mode = stat.S_IMODE(before_stat.st_mode)
    before_identity = (before_stat.st_dev, before_stat.st_ino)
    owned_identity: tuple[int, int] | None = None

    def rollback_service() -> bool:
        if owned_identity is None:
            return True
        try:
            current = service_config_path.stat()
            if (not service_config_path.is_file() or service_config_path.is_symlink()
                    or (current.st_dev, current.st_ino) != owned_identity
                    or service_config_path.read_bytes() != service_after):
                return False
            fd, temporary_name = tempfile.mkstemp(
                prefix=".successor-service-rollback-",
                dir=service_config_path.parent)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(service_before); stream.flush(); os.fsync(stream.fileno())
                os.chmod(temporary, before_mode)
                os.replace(temporary, service_config_path)
                _fsync_directory(service_config_path.parent)
            finally:
                temporary.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=".successor-service-", dir=service_config_path.parent)
        temporary = Path(temporary_name)
        held_fd, held_name = tempfile.mkstemp(
            prefix=".successor-service-held-", dir=service_config_path.parent)
        os.close(held_fd)
        held = Path(held_name)
        held.unlink()
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(service_after); stream.flush(); os.fsync(stream.fileno())
            os.chmod(temporary, before_mode)
            os.rename(service_config_path, held)
            moved = held.lstat()
            if (held.is_symlink()
                    or (moved.st_dev, moved.st_ino) != before_identity
                    or held.read_bytes() != service_before):
                if not service_config_path.exists() and not service_config_path.is_symlink():
                    os.rename(held, service_config_path)
                raise ConfigTransitionError(
                    "service config changed during compare-and-patch")
            os.link(temporary, service_config_path)
            temporary.unlink()
            held.unlink()
        finally:
            temporary.unlink(missing_ok=True)
            if ((held.exists() or held.is_symlink())
                    and not service_config_path.exists()
                    and not service_config_path.is_symlink()):
                os.rename(held, service_config_path)
            elif ((held.exists() or held.is_symlink())
                  and (held.is_symlink() or held.is_file())):
                held.unlink()
        current = service_config_path.stat()
        owned_identity = (current.st_dev, current.st_ino)
        _fsync_directory(service_config_path.parent)
        if fault_hook is not None:
            fault_hook("service_config")

        after_model_paths = sorted(state_dir.glob("*-model-config.json"))
        _need({path.name: sha256_bytes(path.read_bytes()) for path in after_model_paths}
              == model_byte_hashes,
              "model configuration bytes changed during service transition")
        for row in rows:
            target = state_dir / row["name"]
            _need(target.is_file() and not target.is_symlink()
                  and sha256_bytes(target.read_bytes()) == row["after_sha256"],
                  f"{row['name']} changed during preserve-existing transition")
        _need(verify_preserved_state_authorities(
            packet_root=packet_root, state_dir=state_dir, manifest=manifest)
            == verified_state_authorities,
            "preserved state authority changed during service transition")
        _need(service_config_path.read_bytes() == service_after,
              "installed service config differs from reviewed result")
        receipt = {
            "schema_version": PRESERVE_RECEIPT_SCHEMA_VERSION,
            "status": "configured_controller_start_pending",
            "release_ref": manifest["release_ref"],
            "source_commit": manifest["source_commit"],
            "transition_manifest_sha256": expected_manifest_sha256,
            "acceptance_evidence_hash": (
                None if accepted_evidence is None
                else canonical_hash(dict(accepted_evidence))
            ),
            "configuration_mutations": 0,
            "model_config_byte_sha256": model_byte_hashes,
            "preserved_targets": [
                {"name": row["name"], "sha256": row["after_sha256"]}
                for row in rows
            ],
            "preserved_state_authorities": verified_state_authorities,
            "service_config_mutations": 1,
            "service_config_before_sha256": sha256_bytes(service_before),
            "service_config_after_sha256": sha256_bytes(service_after),
            "service_lifecycle_mutations": 0, "model_calls": 0,
            "manifest_publication": False,
        }
        receipt["content_hash"] = canonical_hash(receipt)
        if fault_hook is not None:
            fault_hook("before_receipt")
        receipt_fd, receipt_temp_name = tempfile.mkstemp(
            prefix=".successor-receipt-", dir=receipt_path.parent)
        receipt_temp = Path(receipt_temp_name)
        receipt_published = False
        try:
            with os.fdopen(receipt_fd, "wb") as stream:
                stream.write(_json_bytes(receipt)); stream.flush(); os.fsync(stream.fileno())
            if fault_hook is not None:
                fault_hook("receipt_temp_written")
            os.chmod(receipt_temp, 0o600)
            os.link(receipt_temp, receipt_path)
            receipt_published = True
            _fsync_directory(receipt_path.parent)
        except Exception:
            if (receipt_published and receipt_path.is_file()
                    and not receipt_path.is_symlink()
                    and receipt_path.stat().st_ino == receipt_temp.stat().st_ino):
                receipt_path.unlink()
                _fsync_directory(receipt_path.parent)
            raise
        finally:
            receipt_temp.unlink(missing_ok=True)
    except Exception as exc:
        if not rollback_service():
            raise ConfigTransitionError(
                "transition failed and refused to overwrite concurrent service config"
            ) from exc
        raise
    return receipt


def apply_transition(
    *, packet_root: Path, state_dir: Path, manifest_path: Path,
    expected_manifest_sha256: str, receipt_path: Path,
    service_config_path: Path | None = None,
    external_config_path: Path | None = None,
    fault_hook: Callable[[str], None] | None = None,
    _require_accepted: bool = True,
    accepted_evidence: Mapping[str, Any] | None = None,
    successor_source_root: Path | None = None,
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
          and manifest.get("schema_version") in {
              SCHEMA_VERSION, PRESERVE_SCHEMA_VERSION, EXTERNAL_CAS_SCHEMA_VERSION,
              PURE_PRESERVE_SCHEMA_VERSION, WRITER_APPEND_SCHEMA_VERSION}
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

    if manifest.get("schema_version") in {
            PRESERVE_SCHEMA_VERSION, EXTERNAL_CAS_SCHEMA_VERSION,
            PURE_PRESERVE_SCHEMA_VERSION, WRITER_APPEND_SCHEMA_VERSION}:
        _need(service_config_path is not None,
              "preserve-existing transition requires the service config path")
        return _apply_preserve_transition(
            packet_root=packet_root, state_dir=state_dir,
            service_config_path=service_config_path, manifest=manifest,
            external_config_path=external_config_path,
            expected_manifest_sha256=expected_manifest_sha256,
            receipt_path=receipt_path, accepted_evidence=accepted_evidence,
            fault_hook=fault_hook,
            successor_source_root=successor_source_root,
        )

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
    service_config_path: Path | None = None,
    external_config_path: Path | None = None,
    successor_source_root: Path | None = None,
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
    if service_config_path is not None:
        service_config_path = service_config_path.resolve()
        _need(service_config_path.is_relative_to(scratch_root),
              "scratch service config escapes scratch root")
    manifest, _ = _read_json(manifest_path, "scratch transition manifest")
    external_before = external_after = None
    if manifest.get("schema_version") == EXTERNAL_CAS_SCHEMA_VERSION:
        _need(external_config_path is not None,
              "schema 0.3 scratch requires an OpenClaw config path")
        external_config_path = external_config_path.resolve()
        _need(external_config_path.is_relative_to(scratch_root)
              and external_config_path.is_file()
              and not external_config_path.is_symlink(),
              "scratch OpenClaw config escapes scratch root")
        external_before, external_after, _ = expected_openclaw_frame_transition_state(
            packet_root=packet_root, manifest=manifest)
        _need(external_config_path.read_bytes() == external_before,
              "scratch OpenClaw config differs from reviewed before bytes")
        fd, name = tempfile.mkstemp(
            prefix=".scratch-openclaw-", dir=external_config_path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(external_after); stream.flush(); os.fsync(stream.fileno())
            os.chmod(temporary, stat.S_IMODE(external_config_path.stat().st_mode))
            os.replace(temporary, external_config_path)
            _fsync_directory(external_config_path.parent)
        finally:
            temporary.unlink(missing_ok=True)
    elif manifest.get("schema_version") in {
            PURE_PRESERVE_SCHEMA_VERSION, WRITER_APPEND_SCHEMA_VERSION}:
        _need(external_config_path is not None,
              "schema 0.4/0.5 scratch requires an OpenClaw config path")
        external_config_path = external_config_path.resolve()
        expected = expected_preserved_openclaw_state(
            packet_root=packet_root, manifest=manifest)
        _need(external_config_path.is_relative_to(scratch_root)
              and external_config_path.is_file()
              and not external_config_path.is_symlink()
              and external_config_path.read_bytes() == expected,
              "scratch OpenClaw config differs from preserved bytes")
    try:
        receipt = apply_transition(
            packet_root=packet_root, state_dir=state_dir,
            manifest_path=manifest_path,
            expected_manifest_sha256=expected_manifest_sha256,
            receipt_path=receipt_path, service_config_path=service_config_path,
            external_config_path=external_config_path,
            successor_source_root=successor_source_root,
            _require_accepted=False,
        )
    except Exception:
        if (external_config_path is not None and external_before is not None
                and external_after is not None
                and external_config_path.read_bytes() == external_after):
            _atomic_json_bytes(external_config_path, external_before)
        raise
    return {**receipt, "status": "scratch_configuration_applied",
            "live_mutation": False}


def _atomic_json_bytes(path: Path, value: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".scratch-config-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


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
