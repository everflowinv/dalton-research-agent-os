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
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.prepare_successor_config_transition import (
    DOCUMENT_CONFIG, LANE_CONFIG, apply_transition_to_scratch,
    expected_transition_state,
)
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


def _json(path: Path, expected: str, label: str) -> dict[str, Any]:
    data = _artifact(path, expected, label)
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RehearsalBindingError(f"{label} is invalid JSON") from exc
    _need(isinstance(value, dict), f"{label} is not an object")
    return value


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
        document = json.loads(document_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RehearsalBindingError(
            "copied-state document research config is invalid") from exc
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
        def post_catalog_sync_steps(self):
            return (("apply successor configuration in scratch",
                     self._apply_successor),)

        def _apply_successor(self):
            baseline_row = manifest["supporting_evidence"]["baseline_model_snapshot"]
            baseline_path = packet_root / baseline_row["file"]
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            _need(_normalised_model_configs(module, self) == baseline,
                  "copied live model configs differ from successor baseline")
            _need(_normalised_service_config(module, self) == service,
                  "copied live service config differs from preserved baseline")
            _need(not (self.temp_state / DOCUMENT_CONFIG).exists(),
                  "document research config already exists in copied baseline")
            _need(not (self.temp_state / LANE_CONFIG).exists(),
                  "mission document lane config already exists in copied baseline")
            receipt = apply_transition_to_scratch(
                packet_root=packet_root, scratch_root=temp_root,
                state_dir=self.temp_state,
                manifest_path=args.transition_manifest,
                expected_manifest_sha256=args.transition_manifest_sha256,
                receipt_path=temp_root / "successor-config-transition-receipt.json",
            )
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
        expected_service=service)
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
                    "tick_entries": len(rehearsal.rows), "escaped": 0},
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
