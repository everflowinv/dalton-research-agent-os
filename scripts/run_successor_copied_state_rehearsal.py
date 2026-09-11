#!/usr/bin/env python3
"""Rehearse a successor config transition in a confined copied state.

The wrapper consumes an inert pending transition.  It never accepts that
transition: its output is one of the evidence hashes that a later reviewed
stopped-window manifest must bind.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.prepare_successor_config_transition import (
    DOCUMENT_CONFIG, EXTERNAL_CAS_SCHEMA_VERSION, LANE_CONFIG,
    OPENCLAW_FRAME_PATH, OPENCLAW_TARGET_MAX_FRAME_BYTES,
    PRESERVE_SCHEMA_VERSION,
    _json_bytes, _record_hash, _service_after, _set_leaf,
    _validated_service_delta,
    apply_transition_to_scratch, canonical_hash,
    expected_openclaw_frame_transition_state,
    expected_service_transition_state, expected_transition_state,
)

PRESERVE_SCHEMA_VERSIONS = {
    PRESERVE_SCHEMA_VERSION, EXTERNAL_CAS_SCHEMA_VERSION,
}
from scripts.run_release_copied_state_rehearsal import (
    RehearsalBindingError, _artifact, _canonical_sha256,
    _load_frozen_rehearsal, _normalised_model_configs,
    _normalised_service_config, _verify_frozen_source, _write_exclusive,
)


def _need(ok: Any, reason: str) -> None:
    if not ok:
        raise RehearsalBindingError(reason)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


MODEL_CATALOG_CONFIG = "model-catalog-sync.json"
PHASE8_TEMPLATE = "phase8/p14e-adhoc-probe-templates-v1.json"
MARKET_DIGEST_LINK = "feeds/market-digest-output"
_MARKET_DIGEST_INSTALL_CONTRACT = (
    'digest_source="$openclaw_workspace/skills/market-digest/output"',
    'if [[ ! -e "$feeds_dir/market-digest-output" ]]; then',
    'ln -s "$digest_source" "$feeds_dir/market-digest-output"',
)


def _stable_regular_bytes(path: Path, label: str) -> tuple[bytes, os.stat_result]:
    """Read one present install authority without following a replacement."""

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    _need(bool(nofollow), "this host cannot open install authorities no-follow")
    try:
        descriptor = os.open(path, os.O_RDONLY | nofollow)
    except OSError as exc:
        raise RehearsalBindingError(
            f"{label} is not a regular owned file") from exc
    try:
        before = os.fstat(descriptor)
        _need(stat.S_ISREG(before.st_mode),
              f"{label} is not a regular owned file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            value = stream.read()
        after_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path = path.lstat()
    identity = lambda row: (
        row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns)
    _need(identity(before) == identity(after_open) == identity(after_path),
          f"{label} changed while copied")
    return value, before


def stage_existing_install_authorities(
    module: Any, rehearsal: Any, *, openclaw_snapshot: Path,
    openclaw_snapshot_sha256: str,
) -> dict[str, Any]:
    """Preserve present seed-once/dynamic install inputs in confined state."""

    live_state = rehearsal.live_root / module.STATE_SUBDIR
    _need(live_state.is_dir() and not live_state.is_symlink(),
          "live state root for install authorities is unsafe")
    proof: dict[str, Any] = {}
    for name in (PHASE8_TEMPLATE, MODEL_CATALOG_CONFIG):
        source = live_state / name
        source_bytes, source_stat = _stable_regular_bytes(
            source, f"live install authority {name}")
        target = rehearsal.temp_state / name
        _need(not target.exists() and not target.is_symlink(),
              f"copied install authority target is occupied: {name}")
        installed = source_bytes
        semantic_hash = None
        if name == MODEL_CATALOG_CONFIG:
            try:
                raw = json.loads(source_bytes.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise RehearsalBindingError(
                    "live model catalog sync config is invalid JSON") from exc
            _need(isinstance(raw, dict),
                  "live model catalog sync config is not an object")
            semantic_hash = _canonical_sha256(raw)
            confined = module.rewrite_paths(raw, rehearsal.replacements)
            stray = module.foreign_paths(confined, rehearsal.temp_root)
            _need(not stray,
                  "confined model catalog sync config retains live paths")
            installed = _json_bytes(confined)
            catalog_input = Path(confined.get("openclaw_config_path", ""))
            _need(catalog_input.is_absolute()
                  and catalog_input.is_relative_to(rehearsal.temp_root),
                  "confined model catalog input escapes scratch")
            snapshot = _artifact(
                openclaw_snapshot, openclaw_snapshot_sha256,
                "reviewed OpenClaw config snapshot")
            _need(not catalog_input.exists() and not catalog_input.is_symlink(),
                  "confined model catalog input target is occupied")
            catalog_input.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write_exclusive(catalog_input, snapshot)
            proof["openclaw_snapshot"] = {
                "source_sha256": openclaw_snapshot_sha256,
                "confined_path": str(catalog_input),
                "confined_sha256": _sha(catalog_input),
            }
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_exclusive(target, installed)
        proof[name] = {
            "source": str(source),
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "source_stat": {
                "device": source_stat.st_dev, "inode": source_stat.st_ino,
                "size": source_stat.st_size, "mtime_ns": source_stat.st_mtime_ns,
            },
            "source_semantic_sha256": semantic_hash,
            "confined_path": str(target),
            "confined_sha256": _sha(target),
        }
    return proof


def validate_existing_install_authorities(
    module: Any, rehearsal: Any, proof: Mapping[str, Any],
) -> dict[str, Any]:
    """Recheck the staged authorities and their confined path semantics."""

    phase8 = rehearsal.temp_state / PHASE8_TEMPLATE
    catalog = rehearsal.temp_state / MODEL_CATALOG_CONFIG
    _need(phase8.is_file() and not phase8.is_symlink()
          and _sha(phase8) == proof[PHASE8_TEMPLATE]["source_sha256"],
          "copied phase8 owner authority drifted")
    _need(catalog.is_file() and not catalog.is_symlink(),
          "copied model catalog sync config is unavailable")
    raw = json.loads(catalog.read_text(encoding="utf-8"))
    _need(not module.foreign_paths(raw, rehearsal.temp_root),
          "copied model catalog sync config escapes scratch")
    normalized = module.rewrite_paths(raw, module.invert(rehearsal.replacements))
    _need(_canonical_sha256(normalized)
          == proof[MODEL_CATALOG_CONFIG]["source_semantic_sha256"],
          "copied model catalog sync config changed production semantics")
    catalog_input = Path(raw["openclaw_config_path"])
    _need(catalog_input.is_file() and not catalog_input.is_symlink()
          and _sha(catalog_input)
          == proof["openclaw_snapshot"]["source_sha256"],
          "confined model catalog input drifted")
    return {
        "model_catalog_config_sha256": _sha(catalog),
        "model_catalog_source_sha256": proof[MODEL_CATALOG_CONFIG][
            "source_sha256"],
        "model_catalog_source_semantic_sha256": proof[MODEL_CATALOG_CONFIG][
            "source_semantic_sha256"],
        "phase8_template_sha256": _sha(phase8),
        "openclaw_snapshot_sha256": _sha(catalog_input),
    }


def capture_external_market_digest_preservation(
    module: Any, rehearsal: Any, *, installer: Path,
) -> dict[str, Any]:
    """Prove install preserves the live feed link without copying its corpus."""

    installer_bytes, _ = _stable_regular_bytes(installer, "frozen installer")
    installer_text = installer_bytes.decode("utf-8")
    _need(all(fragment in installer_text
              for fragment in _MARKET_DIGEST_INSTALL_CONTRACT),
          "frozen installer market-digest preservation contract differs")
    link = rehearsal.live_root / module.STATE_SUBDIR / MARKET_DIGEST_LINK
    before = link.lstat()
    _need(stat.S_ISLNK(before.st_mode),
          "live market-digest authority is not an owned symlink")
    link_text = os.readlink(link)
    after = link.lstat()
    identity = lambda row: (
        row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns)
    _need(identity(before) == identity(after),
          "live market-digest authority changed while inspected")
    expected = (
        rehearsal.real_home
        / ".openclaw/workspace/skills/market-digest/output"
    ).resolve(strict=True)
    target = (link.parent / link_text).resolve(strict=True)
    _need(target == expected and target.is_dir(),
          "live market-digest authority does not name the installed source")
    confined = rehearsal.temp_state / MARKET_DIGEST_LINK
    _need(not confined.exists() and not confined.is_symlink(),
          "external market-digest corpus was copied into rehearsal state")
    return {
        "live_link": str(link),
        "link_text_sha256": hashlib.sha256(os.fsencode(link_text)).hexdigest(),
        "resolved_external_target": str(target),
        "installer_sha256": hashlib.sha256(installer_bytes).hexdigest(),
        "install_preserves_existing_link": True,
        "external_corpus_copied": False,
        "source_stat": {
            "device": before.st_dev, "inode": before.st_ino,
            "size": before.st_size, "mtime_ns": before.st_mtime_ns,
        },
    }


def validate_external_market_digest_preservation(
    module: Any, rehearsal: Any, proof: Mapping[str, Any], *, installer: Path,
) -> dict[str, Any]:
    """Recheck the owner link and confirm the confined run never copied it."""

    current = capture_external_market_digest_preservation(
        module, rehearsal, installer=installer)
    _need(current["link_text_sha256"] == proof["link_text_sha256"]
          and current["source_stat"] == proof["source_stat"],
          "live market-digest authority drifted during rehearsal")
    return current


def stage_preserved_runtime_configs(
    module: Any, rehearsal: Any, *, packet_root: Path,
    manifest: Mapping[str, Any],
) -> dict[str, str]:
    """Copy the two v0.2 preserved configs into the confined state copy."""

    _need(manifest.get("schema_version") in PRESERVE_SCHEMA_VERSIONS,
          "preserved runtime config staging requires a 0.2 transition")
    rows = {row.get("name"): row for row in manifest.get("targets", [])
            if isinstance(row, Mapping)}
    _need(set((DOCUMENT_CONFIG, LANE_CONFIG)).issubset(rows),
          "preserved runtime config targets are absent")
    live_state = rehearsal.live_root / module.STATE_SUBDIR
    _need(live_state.is_dir() and not live_state.is_symlink(),
          "live state root for preserved configs is unsafe")
    staged = {}
    for name in (DOCUMENT_CONFIG, LANE_CONFIG):
        row = rows[name]
        _need(row.get("kind") == "preserve_existing",
              f"preserved runtime config operation differs: {name}")
        reviewed_path = packet_root / row["before"]["file"]
        reviewed = _artifact(
            reviewed_path, row["before"]["sha256"],
            f"reviewed preserved runtime config {name}")
        source = live_state / name
        before = source.lstat()
        _need(stat.S_ISREG(before.st_mode) and not source.is_symlink(),
              f"live preserved runtime config is unsafe: {name}")
        source_bytes = source.read_bytes()
        after = source.lstat()
        _need((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
              == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
              and source_bytes == reviewed
              and row.get("after_sha256") == _sha(reviewed_path),
              f"live preserved runtime config differs from review: {name}")
        target = rehearsal.temp_state / name
        _need(not target.exists() and not target.is_symlink(),
              f"copied preserved runtime config target is occupied: {name}")
        if name == DOCUMENT_CONFIG:
            try:
                value = json.loads(source_bytes.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise RehearsalBindingError(
                    "live preserved document config is invalid JSON") from exc
            _need(isinstance(value, dict),
                  "live preserved document config is not an object")
            installed = _json_bytes(
                module.rewrite_paths(value, rehearsal.replacements))
        else:
            installed = source_bytes
        _write_exclusive(target, installed)
        staged[name] = _sha(target)
    rehearsal.confine_to_temp_root()
    return staged


def _json(path: Path, expected: str, label: str) -> dict[str, Any]:
    data = _artifact(path, expected, label)
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RehearsalBindingError(f"{label} is invalid JSON") from exc
    _need(isinstance(value, dict), f"{label} is not an object")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _write_exclusive(path, (json.dumps(dict(value), ensure_ascii=False,
                                      indent=2) + "\n").encode("utf-8"))


def derive_confined_transition(
    module: Any, rehearsal: Any, *, packet_root: Path,
    manifest: Mapping[str, Any], original_manifest_sha256: str,
) -> tuple[Path, Path, dict[str, Any]]:
    """Derive a scratch-path CAS manifest from one exact reviewed transition."""
    derived_root = rehearsal.temp_root / "successor-confined-transition"
    _need(not derived_root.exists(), "confined transition root already exists")
    original_unsigned = dict(manifest)
    original_content_hash = original_unsigned.pop("content_hash", None)
    _need(original_content_hash == canonical_hash(original_unsigned)
          and manifest.get("status") == "prepared_inert",
          "original successor transition authority differs")
    derived_root.mkdir(mode=0o700)
    derived = json.loads(json.dumps(manifest))
    supporting = derived["supporting_evidence"]

    raw_models = module.model_config_inventory(rehearsal.temp_state)
    baseline_path = derived_root / "model-config-before.snapshot.json"
    _write_json(baseline_path, raw_models)
    supporting["baseline_model_snapshot"] = {
        "file": baseline_path.name, "sha256": _sha(baseline_path)}
    confined_model_artifacts = {}
    confined_model_hashes = {}
    original_model_artifacts = manifest["supporting_evidence"].get(
        "model_config_files")
    if manifest.get("schema_version") in PRESERVE_SCHEMA_VERSIONS:
        _need(isinstance(original_model_artifacts, Mapping)
              and set(original_model_artifacts) == set(raw_models),
              "original full model artifact inventory differs")
        model_artifact_root = derived_root / "model-config-files"
        model_artifact_root.mkdir(mode=0o700)
        for name in sorted(raw_models):
            original = packet_root / original_model_artifacts[name]["file"]
            _artifact(original, original_model_artifacts[name]["sha256"],
                      f"original model config {name}")
            confined = model_artifact_root / name
            _write_exclusive(confined, (rehearsal.temp_state / name).read_bytes())
            confined_model_artifacts[name] = {
                "file": confined.relative_to(derived_root).as_posix(),
                "sha256": _sha(confined),
            }
            confined_model_hashes[name] = _sha(confined)
        supporting["model_config_files"] = confined_model_artifacts
    if manifest.get("schema_version") not in PRESERVE_SCHEMA_VERSIONS:
        original_audit = packet_root / manifest["supporting_evidence"][
            "document_research_readonly_audit"]["file"]
        _artifact(
            original_audit,
            manifest["supporting_evidence"]["document_research_readonly_audit"]["sha256"],
            "original document research audit")
        audit_path = derived_root / "document-research-readonly-audit.json"
        _write_exclusive(audit_path, original_audit.read_bytes())
        supporting["document_research_readonly_audit"] = {
            "file": audit_path.name, "sha256": _sha(audit_path)}

    final_models = json.loads(json.dumps(raw_models))
    for index, (original, target) in enumerate(
            zip(manifest["targets"], derived["targets"], strict=True)):
        name = original["name"]
        if target["kind"] == "preserve_existing":
            original_before = packet_root / original["before"]["file"]
            _artifact(original_before, original["before"]["sha256"],
                      f"original preserved {name}")
            before_path = derived_root / f"{index:02d}-{name}.preserved.json"
            _write_exclusive(before_path, (rehearsal.temp_state / name).read_bytes())
            target["before"] = {"file": before_path.name, "sha256": _sha(before_path)}
            target["after_sha256"] = _sha(before_path)
            continue
        original_after = packet_root / original["after"]["file"]
        after_value = json.loads(original_after.read_text(encoding="utf-8"))
        confined_after = module.rewrite_paths(after_value, rehearsal.replacements)
        after_path = derived_root / f"{index:02d}-{name}.after.json"
        _write_json(after_path, confined_after)
        target["after"] = {"file": after_path.name, "sha256": _sha(after_path)}
        if name.endswith("-model-config.json"):
            final_models[name] = confined_after
        if target["kind"] == "compare_and_replace":
            original_before = packet_root / original["before"]["file"]
            _artifact(original_before, original["before"]["sha256"],
                      f"original {name} precondition")
            before_path = derived_root / f"{index:02d}-{name}.before.json"
            _write_exclusive(before_path, (rehearsal.temp_state / name).read_bytes())
            target["before"] = {"file": before_path.name,
                                "sha256": _sha(before_path)}

    if manifest.get("schema_version") in PRESERVE_SCHEMA_VERSIONS:
        authority_root = derived_root / "preserved-state-authorities"
        authority_root.mkdir(mode=0o700)
        for index, (original, target) in enumerate(zip(
                manifest["preserved_state_authorities"],
                derived["preserved_state_authorities"], strict=True)):
            original_path = packet_root / original["before"]["file"]
            _artifact(original_path, original["before"]["sha256"],
                      f"original preserved state authority {original['path']}")
            scratch_path = rehearsal.temp_state / original["path"]
            _need(scratch_path.is_file() and not scratch_path.is_symlink(),
                  f"copied preserved state authority is absent: {original['path']}")
            _need(scratch_path.read_bytes() == original_path.read_bytes(),
                  f"copied preserved state authority bytes differ: {original['path']}")
            confined = authority_root / f"{index:02d}.json"
            _write_exclusive(confined, scratch_path.read_bytes())
            target["before"] = {
                "file": confined.relative_to(derived_root).as_posix(),
                "sha256": _sha(confined),
            }
            target["after_sha256"] = _sha(confined)

        service_row = derived["service_transition"]
        service_before_path = derived_root / "service-config.before.json"
        _write_exclusive(service_before_path, rehearsal.temp_config.read_bytes())
        service_row["before"] = {
            "file": service_before_path.name, "sha256": _sha(service_before_path)}
        if manifest.get("schema_version") == PRESERVE_SCHEMA_VERSION:
            original_delta_row = manifest["service_transition"]["delta"]
            original_delta = packet_root / original_delta_row["file"]
            _artifact(original_delta, original_delta_row["sha256"],
                      "original planner service budget delta")
            delta = _validated_service_delta(json.loads(original_delta.read_text()))
            delta["expected_before_sha256"] = _sha(service_before_path)
            scratch_before = json.loads(service_before_path.read_text())
            scratch_after = _json_bytes(_service_after(scratch_before, delta))
            delta["expected_after_sha256"] = hashlib.sha256(scratch_after).hexdigest()
            delta.pop("content_hash", None)
            delta["content_hash"] = _record_hash(delta)
            delta_path = derived_root / "planner-call-budget.delta.json"
            _write_json(delta_path, delta)
            service_row["delta"] = {
                "file": delta_path.name, "sha256": _sha(delta_path)}
            service_row["after_sha256"] = delta["expected_after_sha256"]
        else:
            service_row["after_sha256"] = _sha(service_before_path)

            original_external = manifest["external_config_transitions"][0]
            original_before = packet_root / original_external["before"]["file"]
            original_after = packet_root / original_external["after"]["file"]
            _artifact(original_before, original_external["before"]["sha256"],
                      "original OpenClaw before")
            _artifact(original_after, original_external["after"]["sha256"],
                      "original OpenClaw after")
            scratch_openclaw = rehearsal.temp_root / "openclaw/openclaw.json"
            _need(scratch_openclaw.is_file() and not scratch_openclaw.is_symlink()
                  and scratch_openclaw.read_bytes() == original_before.read_bytes(),
                  "confined OpenClaw baseline differs from reviewed bytes")
            confined_before = derived_root / "openclaw-config.before.json"
            _write_exclusive(confined_before, scratch_openclaw.read_bytes())
            before_value = json.loads(confined_before.read_text())
            after_value = _set_leaf(
                before_value, OPENCLAW_FRAME_PATH,
                OPENCLAW_TARGET_MAX_FRAME_BYTES)
            external = derived["external_config_transitions"][0]
            if external["managed_plugins"]:
                plugin = external["managed_plugins"][0]
                original_destination = Path(plugin["destination"])
                _need(original_destination.is_dir()
                      and not original_destination.is_symlink(),
                      "reviewed managed plugin destination is unavailable")
                confined_destination = (derived_root / "managed-plugins" /
                                        original_destination.name)
                shutil.copytree(original_destination, confined_destination,
                                symlinks=False)
                plugin["destination"] = str(confined_destination.resolve())
                paths = after_value["plugins"]["load"]["paths"]
                old_path = external["semantic_mutations"][1]["before_value"]
                _need(paths.count(old_path) == 1,
                      "confined OpenClaw plugin baseline differs")
                paths[paths.index(old_path)] = plugin["destination"]
                external["semantic_mutations"][1]["after_value"] = plugin[
                    "destination"]
            confined_after = derived_root / "openclaw-config.after.json"
            _write_json(confined_after, after_value)
            external["before"] = {
                "file": confined_before.name, "sha256": _sha(confined_before)}
            external["after"] = {
                "file": confined_after.name, "sha256": _sha(confined_after)}
            external["before_sha256"] = _sha(confined_before)
            external["after_sha256"] = _sha(confined_after)

    derived["model_inventory"] = {
        "before_count": len(raw_models), "after_count": len(final_models),
        "before_semantic_sha256": canonical_hash(raw_models),
        "after_semantic_sha256": canonical_hash(final_models),
    }
    if manifest.get("schema_version") in PRESERVE_SCHEMA_VERSIONS:
        derived["model_inventory"]["file_sha256"] = confined_model_hashes
    derived.pop("content_hash", None)
    derived["content_hash"] = canonical_hash(derived)
    manifest_path = derived_root / "successor-config-transition.confined.json"
    _write_json(manifest_path, derived)
    proof = {
        "schema_version": (
            "successor-confined-transition-derivation-0.3"
            if manifest.get("schema_version") == EXTERNAL_CAS_SCHEMA_VERSION
            else "successor-confined-transition-derivation-0.2"
            if manifest.get("schema_version") == PRESERVE_SCHEMA_VERSION
            else "successor-confined-transition-derivation-0.1"),
        "original_transition_manifest_sha256": original_manifest_sha256,
        "confined_transition_manifest_sha256": _sha(manifest_path),
        "replacement_map_sha256": canonical_hash(rehearsal.replacements),
        "original_semantics_sha256": manifest["model_inventory"][
            "after_semantic_sha256"],
        "confined_semantics_sha256": derived["model_inventory"][
            "after_semantic_sha256"],
    }
    proof["content_hash"] = canonical_hash(proof)
    proof_path = derived_root / "derivation-proof.json"
    _write_json(proof_path, proof)
    return manifest_path, proof_path, proof


def validate_successor_snapshots(
    module: Any, rehearsal: Any, *, expected_models: Mapping[str, Any],
    expected_document: Mapping[str, Any], expected_service: Mapping[str, Any],
    expected_lane: Mapping[str, Any],
) -> dict[str, Any]:
    models = _normalised_model_configs(module, rehearsal)
    service = _normalised_service_config(module, rehearsal)
    document_path = rehearsal.temp_state / DOCUMENT_CONFIG
    _need(document_path.is_file() and not document_path.is_symlink(),
          "copied-state document research config is unavailable")
    try:
        document_raw = json.loads(document_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RehearsalBindingError(
            "copied-state document research config is invalid") from exc
    document = module.rewrite_paths(
        document_raw, module.invert(rehearsal.replacements))
    _need(models == expected_models,
          "copied-state successor model configuration drifted")
    _need(document == expected_document,
          "copied-state document research configuration drifted")
    lane_path = rehearsal.temp_state / LANE_CONFIG
    _need(lane_path.is_file() and not lane_path.is_symlink()
          and json.loads(lane_path.read_text(encoding="utf-8")) == expected_lane,
          "copied-state mission document lane configuration drifted")
    _need(service == expected_service,
          "copied-state preserved service configuration drifted")
    writer_plist = rehearsal.launch_agents_dir / "space.lumos.dalton.writer.plist"
    _need(writer_plist.is_file() and not writer_plist.is_symlink(),
          "copied-state writer LaunchAgent is unavailable")
    argv = plistlib.loads(writer_plist.read_bytes()).get("ProgramArguments", [])
    required_flags = {"--mission-document-research-lane"}
    _need(all(flag in argv and argv.index(flag) + 1 < len(argv)
              for flag in required_flags),
          "copied-state writer does not consume every directed-document config")
    return {
        "model_config_count": len(models),
        "model_config_semantic_sha256": _canonical_sha256(models),
        "document_research_config_sha256": _sha(document_path),
        "mission_document_lane_config_sha256": _sha(lane_path),
        "service_config_semantic_sha256": _canonical_sha256(service),
        "writer_directed_document_flags": sorted(required_flags),
    }


def replay_preserved_production_setup(module: Any, rehearsal: Any) -> tuple[str, list[str]]:
    """Run the install.sh setup entrypoints twice against only the confined copy."""

    _need(rehearsal.confined, "production setup replay is not confined")
    from dalton_core import (
        annual_report_setup, cockpit_setup, document_extraction_setup,
    )

    model_before = {
        path.name: path.read_bytes()
        for path in sorted(rehearsal.temp_state.glob("*-model-config.json"))
    }
    service_before = rehearsal.temp_config.read_bytes()
    document_before = (rehearsal.temp_state / DOCUMENT_CONFIG).read_bytes()
    lane_before = (rehearsal.temp_state / LANE_CONFIG).read_bytes()
    service = json.loads(service_before.decode("utf-8"))
    router_db = Path(service["model_router_db"]).expanduser().resolve()
    _need(router_db.is_relative_to(rehearsal.temp_root),
          "production setup replay Router escapes scratch root")

    def setup_once() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        extraction = document_extraction_setup.install(
            rehearsal.temp_config, tier="cheap")
        annual = annual_report_setup.install(rehearsal.temp_config)
        cockpit = cockpit_setup.install(rehearsal.temp_config)
        return extraction, annual, cockpit

    with module.configuration_setup_guard(router_db):
        first = setup_once()
        second = setup_once()
    for extraction, annual, cockpit in (first, second):
        _need(extraction.get("policy", {}).get("status") == "duplicate"
              and annual.get("created") == []
              and cockpit.get("service_config_changed") is False,
              "production setup replay was not an idempotent existing install")
    model_after = {
        path.name: path.read_bytes()
        for path in sorted(rehearsal.temp_state.glob("*-model-config.json"))
    }
    _need(model_after == model_before
          and rehearsal.temp_config.read_bytes() == service_before
          and (rehearsal.temp_state / DOCUMENT_CONFIG).read_bytes() == document_before
          and (rehearsal.temp_state / LANE_CONFIG).read_bytes() == lane_before,
          "production setup replay changed preserved configuration bytes")
    rehearsal.confine_to_temp_root()
    return (f"production setup replay preserved {len(model_after)} model configs "
            "and service/document/lane bytes across two installs", [])


def run(args: argparse.Namespace) -> dict[str, Any]:
    source_root = args.source_root.expanduser().resolve(strict=True)
    live_root = args.live_root.expanduser().resolve(strict=True)
    packet_root = args.packet_root.expanduser().resolve(strict=True)
    temp_root = args.temp_root.expanduser().resolve()
    _need(not temp_root.exists(), "temporary rehearsal root already exists")
    _need(any(temp_root.is_relative_to(Path(root).resolve())
              for root in ("/tmp", "/private/tmp", "/var/folders")),
          "temporary rehearsal root is outside a temporary filesystem")
    outputs = (args.log, args.report, args.binding)
    for output in outputs:
        _need(not output.exists() and not output.is_symlink(),
              f"output already exists: {output}")
    _verify_frozen_source(source_root, args.code_commit)
    ops_root = Path(__file__).resolve().parent.parent
    _need(subprocess.check_output(
        ["git", "-C", str(ops_root), "rev-parse", "HEAD"], text=True).strip()
        == args.ops_code_commit
        and not subprocess.check_output(
            ["git", "-C", str(ops_root), "status", "--porcelain",
             "--untracked-files=all"], text=True),
        "successor rehearsal helper checkout is not the frozen ops commit")
    manifest = _json(args.transition_manifest, args.transition_manifest_sha256,
                     "successor transition manifest")
    _need(manifest.get("status") == "prepared_inert"
          and manifest.get("acceptance", {}).get("state") == "pending",
          "copied rehearsal requires an unaccepted inert transition")
    expected_models, expected_document, expected_lane = expected_transition_state(
        packet_root=packet_root, manifest=manifest)
    service = _json(args.service_config_snapshot,
                    args.service_config_snapshot_sha256,
                    "preserved service config snapshot")
    expected_service = service
    if manifest.get("schema_version") in PRESERVE_SCHEMA_VERSIONS:
        service_before, expected_service = expected_service_transition_state(
            packet_root=packet_root, manifest=manifest)
        _need(service_before == service,
              "service snapshot differs from preserve-existing transition")
    _artifact(args.openclaw_config_snapshot,
              args.openclaw_config_snapshot_sha256, "OpenClaw config snapshot")
    module = _load_frozen_rehearsal(source_root, args.code_commit)
    inputs = (args.transition_manifest, args.service_config_snapshot,
              args.openclaw_config_snapshot)
    module.validate_cli_paths(
        live_root=live_root, source_root=live_root, temp_root=temp_root,
        report=args.report, input_files=inputs,
    )

    class SuccessorRehearsal(module.Rehearsal):
        def copy_state(self):
            detail, findings = super().copy_state()
            self.existing_install_authorities = None
            if manifest.get("schema_version") in PRESERVE_SCHEMA_VERSIONS:
                self.existing_install_authorities = (
                    stage_existing_install_authorities(
                        module, self,
                        openclaw_snapshot=args.openclaw_config_snapshot,
                        openclaw_snapshot_sha256=(
                            args.openclaw_config_snapshot_sha256),
                    )
                )
                self.external_market_digest = (
                    capture_external_market_digest_preservation(
                        module, self,
                        installer=source_root / "deploy/macos/install.sh",
                    )
                )
                findings.append(
                    "live feeds/market-digest-output is an external symlink; "
                    "the installer preserves it, but its corpus is deliberately "
                    "not copied, so sales-note content availability is outside "
                    "this confined rehearsal"
                )
                detail += "; 2 present install authorities preserved"
            self.successor_openclaw = None
            if manifest.get("schema_version") == EXTERNAL_CAS_SCHEMA_VERSION:
                self.successor_openclaw = self.temp_root / "openclaw/openclaw.json"
                self.successor_openclaw.parent.mkdir(mode=0o700)
                _write_exclusive(
                    self.successor_openclaw,
                    args.openclaw_config_snapshot.read_bytes())
                detail += "; reviewed OpenClaw before bytes staged in scratch"
            return detail, findings

        def post_catalog_sync_steps(self):
            steps = [("apply successor configuration in scratch",
                      self._apply_successor)]
            if manifest.get("schema_version") in PRESERVE_SCHEMA_VERSIONS:
                steps.append(("replay production setup against preserved scratch config",
                              self._replay_preserved_setup))
            return tuple(steps)

        def _replay_preserved_setup(self):
            return replay_preserved_production_setup(module, self)

        def _apply_successor(self):
            if manifest.get("schema_version") in PRESERVE_SCHEMA_VERSIONS:
                stage_preserved_runtime_configs(
                    module, self, packet_root=packet_root, manifest=manifest)
            baseline_row = manifest["supporting_evidence"]["baseline_model_snapshot"]
            baseline_path = packet_root / baseline_row["file"]
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            _need(_normalised_model_configs(module, self) == baseline,
                  "copied live model configs differ from successor baseline")
            _need(_normalised_service_config(module, self) == service,
                  "copied live service config differs from preserved baseline")
            if manifest.get("schema_version") in PRESERVE_SCHEMA_VERSIONS:
                current_document = json.loads(
                    (self.temp_state / DOCUMENT_CONFIG).read_text(encoding="utf-8"))
                current_document = module.rewrite_paths(
                    current_document, module.invert(self.replacements))
                _need(current_document == expected_document,
                      "copied document research config differs from preserved baseline")
                _need(json.loads((self.temp_state / LANE_CONFIG).read_text(
                    encoding="utf-8")) == expected_lane,
                    "copied mission document lane config differs from preserved baseline")
            else:
                _need(not (self.temp_state / DOCUMENT_CONFIG).exists(),
                      "document research config already exists in copied baseline")
                _need(not (self.temp_state / LANE_CONFIG).exists(),
                      "mission document lane config already exists in copied baseline")
            confined_manifest, proof_path, proof = derive_confined_transition(
                module, self, packet_root=packet_root, manifest=manifest,
                original_manifest_sha256=args.transition_manifest_sha256)
            receipt = apply_transition_to_scratch(
                packet_root=confined_manifest.parent, scratch_root=temp_root,
                state_dir=self.temp_state,
                manifest_path=confined_manifest,
                expected_manifest_sha256=_sha(confined_manifest),
                receipt_path=temp_root / "successor-config-transition-receipt.json",
                service_config_path=self.temp_config,
                external_config_path=self.successor_openclaw,
            )
            self.successor_derivation = {
                **proof, "proof_path": str(proof_path),
                "proof_sha256": _sha(proof_path),
                "receipt_sha256": _sha(
                    temp_root / "successor-config-transition-receipt.json"),
            }
            self.confine_to_temp_root()
            return (receipt["status"], [])

    log_fd = os.open(args.log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(log_fd, "w", encoding="utf-8") as log_stream:
        rehearsal = SuccessorRehearsal(
            live_root, temp_root, source_root=live_root,
            openclaw_config=args.openclaw_config_snapshot,
            rehearse_reviewed_model_setup=False,
            log=lambda line: print(line, file=log_stream, flush=True),
        )
        code = rehearsal.run()
        summary = rehearsal.report()
        print(summary, file=log_stream, flush=True)
    _write_exclusive(args.report, (summary + "\n").encode())
    _need(code == 0, "copied-state successor rehearsal failed")
    final = validate_successor_snapshots(
        module, rehearsal, expected_models=expected_models,
        expected_document=expected_document, expected_lane=expected_lane,
        expected_service=expected_service)
    if rehearsal.existing_install_authorities is not None:
        final["existing_install_authorities"] = (
            validate_existing_install_authorities(
                module, rehearsal, rehearsal.existing_install_authorities))
        final["external_market_digest"] = (
            validate_external_market_digest_preservation(
                module, rehearsal, rehearsal.external_market_digest,
                installer=source_root / "deploy/macos/install.sh"))
    if manifest.get("schema_version") == EXTERNAL_CAS_SCHEMA_VERSION:
        _before, expected_openclaw, _row = expected_openclaw_frame_transition_state(
            packet_root=packet_root, manifest=manifest)
        _need(rehearsal.successor_openclaw.is_file()
              and not rehearsal.successor_openclaw.is_symlink()
              and rehearsal.successor_openclaw.read_bytes() != _before,
              "confined OpenClaw transition did not apply")
        actual_openclaw = json.loads(rehearsal.successor_openclaw.read_text())
        expected_value = json.loads(expected_openclaw)
        if _row["managed_plugins"]:
            production_path = _row["managed_plugins"][0]["destination"]
            paths = actual_openclaw["plugins"]["load"]["paths"]
            confined_path = next(
                mutation["after_value"] for mutation in
                json.loads((Path(rehearsal.successor_derivation["proof_path"]).parent /
                            "successor-config-transition.confined.json").read_text())
                ["external_config_transitions"][0]["semantic_mutations"]
                if mutation["kind"] == "json_array_unique_replace")
            _need(paths.count(confined_path) == 1,
                  "confined managed plugin path is not unique")
            paths[paths.index(confined_path)] = production_path
        _need(actual_openclaw == expected_value,
              "confined OpenClaw result changes more than reviewed paths")
        final["openclaw_config_semantic_sha256"] = canonical_hash(actual_openclaw)
    _verify_frozen_source(source_root, args.code_commit)
    binding = {
        "schema_version": "successor-copied-state-rehearsal-binding-0.1",
        "status": "passed", "acceptance_state": "candidate_evidence_only",
        "code_commit": args.code_commit, "source_root": str(source_root),
        "transition_manifest_sha256": args.transition_manifest_sha256,
        "inputs": {
            "service_config_snapshot_sha256": args.service_config_snapshot_sha256,
            "openclaw_config_snapshot_sha256": args.openclaw_config_snapshot_sha256,
        },
        "results": {**final, "step_count": len(rehearsal.steps),
                    "tick_entries": len(rehearsal.rows), "escaped": 0,
                    "confined_transition_derivation":
                        rehearsal.successor_derivation},
        "ops_helpers": {
            "git_commit": args.ops_code_commit,
            "runner_sha256": _sha(Path(__file__).resolve()),
            "transition_helper_sha256": _sha(
                Path(__file__).with_name(
                    "prepare_successor_config_transition.py").resolve()),
        },
        "execution_boundary": {"live_mutation": False, "external_calls": False,
                               "model_calls": False},
        "artifacts": {
            "log": {"path": str(args.log.resolve()), "sha256": _sha(args.log)},
            "report": {"path": str(args.report.resolve()), "sha256": _sha(args.report)},
            "scratch_root": str(temp_root),
        },
    }
    binding["content_hash"] = _canonical_sha256(binding)
    _write_exclusive(args.binding,
                     (json.dumps(binding, ensure_ascii=False, indent=2) + "\n").encode())
    return binding


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--code-commit", required=True)
    parser.add_argument("--ops-code-commit", required=True)
    parser.add_argument("--live-root", type=Path, required=True)
    parser.add_argument("--packet-root", type=Path, required=True)
    parser.add_argument("--temp-root", type=Path, required=True)
    parser.add_argument("--transition-manifest", type=Path, required=True)
    parser.add_argument("--transition-manifest-sha256", required=True)
    parser.add_argument("--service-config-snapshot", type=Path, required=True)
    parser.add_argument("--service-config-snapshot-sha256", required=True)
    parser.add_argument("--openclaw-config-snapshot", type=Path, required=True)
    parser.add_argument("--openclaw-config-snapshot-sha256", required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--binding", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run(args)
    print(json.dumps({"status": result["status"],
                      "content_hash": result["content_hash"]}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RehearsalBindingError) as exc:
        raise SystemExit(f"STOP: {exc}") from exc
