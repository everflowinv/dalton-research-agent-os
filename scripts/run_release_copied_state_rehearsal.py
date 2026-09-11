#!/usr/bin/env python3
"""Run the frozen deploy rehearsal with one final configuration snapshot.

The frozen R10a setup option proves a historical 12-to-14 transition. R11
starts from an already activated inventory, which may contain 14 or 15 model
configs. This wrapper runs the ordinary copied-state rehearsal without that
historical setup option, installs the reviewed retention candidate only in the
scratch copy, and proves the dynamic model inventory did not drift.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, Sequence


class RehearsalBindingError(RuntimeError):
    pass


def _need(condition: bool, reason: str) -> None:
    if not condition:
        raise RehearsalBindingError(reason)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    wire = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def _artifact(path: Path, expected_sha256: str, label: str) -> bytes:
    _need(path.is_file() and not path.is_symlink(), f"{label} is not a regular file")
    _need(_sha256(path) == expected_sha256, f"{label} SHA-256 changed")
    return path.read_bytes()


def _json_artifact(path: Path, expected_sha256: str, label: str) -> Any:
    data = _artifact(path, expected_sha256, label)
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RehearsalBindingError(f"{label} is invalid JSON") from exc


def _verify_frozen_source(source_root: Path, commit: str) -> None:
    try:
        actual = subprocess.check_output(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"], text=True
        ).strip()
        dirty = subprocess.check_output(
            ["git", "-C", str(source_root), "status", "--porcelain", "--untracked-files=all"],
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RehearsalBindingError("source root is not a readable Git checkout") from exc
    _need(actual == commit, "source checkout HEAD differs from frozen commit")
    _need(not dirty, "source checkout is not clean")


def _load_frozen_rehearsal(source_root: Path, commit: str) -> ModuleType:
    _verify_frozen_source(source_root, commit)
    source_script = source_root / "scripts" / "rehearse_deploy.py"
    _need(source_script.is_file() and not source_script.is_symlink(), "frozen rehearsal script is unavailable")
    sys.path.insert(0, str(source_root / "src"))
    spec = importlib.util.spec_from_file_location("r11_frozen_rehearse_deploy", source_script)
    _need(spec is not None and spec.loader is not None, "cannot load frozen rehearsal")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _normalised_model_configs(module: ModuleType, rehearsal: Any) -> dict[str, Any]:
    raw = module.model_config_inventory(rehearsal.temp_state)
    return module.rewrite_paths(raw, module.invert(rehearsal.replacements))


def _normalised_service_config(module: ModuleType, rehearsal: Any) -> dict[str, Any]:
    raw = json.loads(rehearsal.temp_config.read_text(encoding="utf-8"))
    return module.rewrite_paths(raw, module.invert(rehearsal.replacements))


def validate_final_snapshots(
    module: ModuleType,
    rehearsal: Any,
    *,
    expected_models: Mapping[str, Any],
    expected_service: Mapping[str, Any],
) -> dict[str, Any]:
    observed_models = _normalised_model_configs(module, rehearsal)
    observed_service = _normalised_service_config(module, rehearsal)
    _need(observed_models == expected_models, "copied-state model configuration drifted")
    _need(observed_service == expected_service, "copied-state service configuration drifted")
    return {
        "model_config_count": len(observed_models),
        "model_config_semantic_sha256": _canonical_sha256(observed_models),
        "service_config_semantic_sha256": _canonical_sha256(observed_service),
    }


def _write_exclusive(path: Path, data: bytes, mode: int = 0o600) -> None:
    _need(path.parent.is_dir() and not path.parent.is_symlink(), "output directory is unavailable")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def run(args: argparse.Namespace) -> dict[str, Any]:
    try:
        source_root = args.source_root.expanduser().resolve(strict=True)
        live_root = args.live_root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise RehearsalBindingError("source or live root is unavailable") from exc
    temp_root = args.temp_root.expanduser().resolve()
    _need(not temp_root.exists(), "temporary rehearsal root already exists")
    _need(
        any(temp_root.is_relative_to(Path(root).resolve()) for root in ("/tmp", "/private/tmp", "/var/folders")),
        "temporary rehearsal root is outside a temporary filesystem",
    )
    _need(
        not temp_root.is_relative_to(source_root) and not source_root.is_relative_to(temp_root),
        "temporary rehearsal root overlaps the frozen source",
    )
    inputs = (
        args.model_config_snapshot,
        args.service_config_before,
        args.service_config_after,
        args.openclaw_config_snapshot,
    )
    resolved_inputs = {path.expanduser().resolve() for path in inputs}
    for output in (args.log, args.report, args.binding):
        _need(not output.exists() and not output.is_symlink(), f"output already exists: {output}")
        resolved = output.expanduser().resolve()
        _need(
            not resolved.is_relative_to(source_root)
            and not resolved.is_relative_to(live_root)
            and resolved not in resolved_inputs,
            f"output overlaps a source: {output}",
        )

    models = _json_artifact(args.model_config_snapshot, args.model_config_snapshot_sha256, "model config snapshot")
    before_service = _json_artifact(args.service_config_before, args.service_config_before_sha256, "service config before")
    after_service = _json_artifact(args.service_config_after, args.service_config_after_sha256, "service config after")
    _artifact(args.openclaw_config_snapshot, args.openclaw_config_snapshot_sha256, "OpenClaw config snapshot")
    _need(isinstance(models, dict) and bool(models), "model config snapshot is empty")
    _need(isinstance(before_service, dict) and isinstance(after_service, dict), "service snapshots are not objects")
    try:
        from scripts.configure_backup_retention_stopped_window import configure
    except ModuleNotFoundError:
        from configure_backup_retention_stopped_window import configure
    expected_after, _ = configure(before_service)
    _need(after_service == expected_after, "service config candidate is not the retention-only delta")

    module = _load_frozen_rehearsal(source_root, args.code_commit)
    try:
        module.validate_cli_paths(
            live_root=live_root,
            source_root=live_root,
            temp_root=temp_root,
            report=args.report,
            input_files=inputs,
        )
    except ValueError as exc:
        raise RehearsalBindingError(str(exc)) from exc

    class FinalConfigurationRehearsal(module.Rehearsal):
        def post_catalog_sync_steps(self):
            return (("install reviewed backup retention in scratch config", self._install_final_config),)

        def _install_final_config(self):
            _need(
                _normalised_model_configs(module, self) == models,
                "copied live model configs differ from final activated snapshot",
            )
            _need(
                _normalised_service_config(module, self) == before_service,
                "copied live service config differs from final precondition",
            )
            rewritten = module.rewrite_paths(after_service, self.replacements)
            self.temp_config.write_text(
                json.dumps(rewritten, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(self.temp_config, 0o600)
            self.confine_to_temp_root()
            return ("backup.keep_latest=3 in scratch config only", [])

    log_fd = os.open(args.log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(log_fd, "w", encoding="utf-8") as log_stream:
        rehearsal = FinalConfigurationRehearsal(
            live_root,
            temp_root,
            source_root=live_root,
            openclaw_config=args.openclaw_config_snapshot,
            rehearse_reviewed_model_setup=False,
            log=lambda line: print(line, file=log_stream, flush=True),
        )
        code = rehearsal.run()
        summary = rehearsal.report()
        print(summary, file=log_stream, flush=True)
    _write_exclusive(args.report, (summary + "\n").encode("utf-8"))
    _need(code == 0, "copied-state rehearsal failed; report and log retained")
    final = validate_final_snapshots(
        module,
        rehearsal,
        expected_models=models,
        expected_service=after_service,
    )
    # A long copied-state run must not bind a source that changed after its
    # initial preflight. This is deliberately immediately before the binding.
    _verify_frozen_source(source_root, args.code_commit)
    binding = {
        "schema_version": "r11-copied-state-rehearsal-binding-0.1",
        "status": "passed",
        "acceptance_state": "candidate_evidence_only",
        "code_commit": args.code_commit,
        "source_root": str(source_root),
        "execution_boundary": {
            "live_mutation": False,
            "external_calls": False,
            "model_calls": False,
            "processes": ["temporary writer", "one temporary controller tick"],
        },
        "inputs": {
            "model_config_snapshot_sha256": args.model_config_snapshot_sha256,
            "service_config_before_sha256": args.service_config_before_sha256,
            "service_config_after_sha256": args.service_config_after_sha256,
            "openclaw_config_snapshot_sha256": args.openclaw_config_snapshot_sha256,
        },
        "results": {
            **final,
            "step_count": len(rehearsal.steps),
            "tick_entries": len(rehearsal.rows),
            "escaped": 0,
        },
        "artifacts": {
            "log": {"path": str(args.log.resolve()), "sha256": _sha256(args.log)},
            "report": {"path": str(args.report.resolve()), "sha256": _sha256(args.report)},
            "scratch_root": str(temp_root),
        },
    }
    binding["content_hash"] = _canonical_sha256(binding)
    _write_exclusive(args.binding, (json.dumps(binding, indent=2) + "\n").encode("utf-8"))
    return binding


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--code-commit", required=True)
    parser.add_argument("--live-root", type=Path, required=True)
    parser.add_argument("--temp-root", type=Path, required=True)
    parser.add_argument("--model-config-snapshot", type=Path, required=True)
    parser.add_argument("--model-config-snapshot-sha256", required=True)
    parser.add_argument("--service-config-before", type=Path, required=True)
    parser.add_argument("--service-config-before-sha256", required=True)
    parser.add_argument("--service-config-after", type=Path, required=True)
    parser.add_argument("--service-config-after-sha256", required=True)
    parser.add_argument("--openclaw-config-snapshot", type=Path, required=True)
    parser.add_argument("--openclaw-config-snapshot-sha256", required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--binding", type=Path, required=True)
    args = parser.parse_args(argv)
    binding = run(args)
    print(json.dumps({"status": binding["status"], "content_hash": binding["content_hash"]}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RehearsalBindingError as exc:
        raise SystemExit(f"STOP: {exc}") from exc
